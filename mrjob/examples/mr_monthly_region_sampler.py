"""Keep a fixed-size, replayable monthly sample of visit records per region.

Each input line is a JSON object with at least:

    region:   the region the visit belongs to
    event_id: a stable event ID assigned by the source
    version:  (optional) declared version of the record (default 0)
    payload:  (optional) the replayable payload of the event

Every event gets a deterministic score computed from --seed and its
stable event ID, so a region's monthly sample is simply the k events
with the lowest scores. Mappers ("shards") pre-select their local top-k
per region and only emit those candidates plus evidence of same-version
conflicts. Because scores, de-duplication, and conflict resolution depend
only on the records themselves, re-ordering the input, changing the
number of mappers, or re-shaping the combine/merge tree never changes
the final sample.

De-duplication and conflict handling:

* re-sent events (same stable event ID) never take up extra sample slots
* if the same event ID appears with different payloads, the record with
  the highest declared version wins
* if the same event ID appears with different payloads for the *same*
  version, the conflict shows up in the report, and the payload with the
  smaller hash is used for the sample (so output stays deterministic)

To keep the report stable no matter how records are sharded, mappers
also emit a small digest (event ID, declared version, payload hashes)
for every event that didn't make their local top-k. These digests are
the "necessary conflict evidence" that lets the reducer detect
same-version contradictions even when the contradictory records landed
in different shards.
"""
import hashlib
import json

from mrjob.job import MRJob
from mrjob.protocol import JSONValueProtocol


def _canonical_json(obj):
    """Deterministic JSON encoding, used for hashing and ordering."""
    return json.dumps(obj, sort_keys=True, separators=(',', ':'))


def score_for_event(seed, event_id):
    """Deterministic sampling score for an event, as a hex string.

    Depends only on the sampling seed and the stable event ID, never on
    which shard or mapper happens to see the event.
    """
    material = '%s\n%s' % (seed, _canonical_json(event_id))
    return hashlib.sha256(material.encode('utf-8')).hexdigest()


def _payload_hash(payload):
    return hashlib.sha256(_canonical_json(payload).encode('utf-8')).hexdigest()


def _entry_key(entry):
    """Hashable, totally ordered key for an entry's event ID."""
    return _canonical_json(entry['event_id'])


def _make_entry(event_id, version, payload, score):
    return {
        'event_id': event_id,
        'score': score,
        'version': version,
        # payload hash -> payload, for the winning version only
        'payloads': {_payload_hash(payload): payload},
    }


def _merge_entries(entry, other):
    """Merge two entries for the same event ID.

    The higher declared version wins; payloads for the winning version
    are unioned so that same-version contradictions stay visible.
    """
    if other['version'] > entry['version']:
        entry, other = other, entry

    if other['version'] == entry['version']:
        payloads = dict(entry['payloads'])
        payloads.update(other['payloads'])
        entry = dict(entry, payloads=payloads)

    return entry


def _absorb(index, entry):
    """Merge an entry into a dict of entries keyed by event ID."""
    key = _entry_key(entry)
    if key in index:
        index[key] = _merge_entries(index[key], entry)
    else:
        index[key] = entry


def _top_k_entries(index, k):
    """The k entries with the lowest (score, event ID), in order."""
    return sorted(
        index.values(),
        key=lambda entry: (entry['score'], _entry_key(entry)))[:k]


def _canonical_payload(entry):
    """The payload to replay for an entry: smallest hash wins, so the
    choice is deterministic even when same-version payloads conflict."""
    return entry['payloads'][min(entry['payloads'])]


def _evidence_from_entry(entry):
    """Compact conflict evidence for an event: its declared version and
    the hashes of all payloads seen for that version."""
    return {
        'event_id': entry['event_id'],
        'version': entry['version'],
        'hashes': sorted(entry['payloads']),
    }


def _merge_evidence(evidence, other):
    """Merge two digests for the same event ID. The higher declared
    version wins; hashes for the winning version are unioned."""
    if other['version'] > evidence['version']:
        evidence, other = other, evidence

    if other['version'] == evidence['version']:
        hashes = sorted(set(evidence['hashes']) | set(other['hashes']))
        evidence = dict(evidence, hashes=hashes)

    return evidence


def _absorb_evidence(index, evidence):
    key = _entry_key(evidence)
    if key in index:
        index[key] = _merge_evidence(index[key], evidence)
    else:
        index[key] = evidence


def _merge_shard_values(values, k):
    """Merge partial shard results for one region.

    Associative and commutative, so any combine/merge tree (and any
    number of mappers) yields the same result. Candidates are truncated
    back to the top-k; this is safe because every event in the global
    top-k is necessarily in the top-k of every shard that saw it.
    """
    candidates = {}
    evidence = {}

    for value in values:
        for entry in value['candidates']:
            _absorb(candidates, entry)
        for digest in value['evidence']:
            _absorb_evidence(evidence, digest)

    top_k = _top_k_entries(candidates, k)
    top_keys = set(_entry_key(entry) for entry in top_k)

    # evicted candidates still contribute their payload hashes as evidence
    for key, entry in candidates.items():
        if key not in top_keys:
            _absorb_evidence(evidence, _evidence_from_entry(entry))

    # an event with evidence can never make the final sample (some shard
    # saw k better-scoring events), so evidence never hides a candidate
    evidence = dict((key, digest) for key, digest in evidence.items()
                    if key not in top_keys)

    return {
        'candidates': top_k,
        'evidence': sorted(evidence.values(), key=_entry_key),
    }


class MRMonthlyRegionSampler(MRJob):
    """Per-region monthly sample of exactly k replayable events."""

    # each input line is one JSON record
    INPUT_PROTOCOL = JSONValueProtocol

    def configure_args(self):
        super(MRMonthlyRegionSampler, self).configure_args()
        self.add_passthru_arg(
            '--sample-size',
            type=int,
            help='Number of events to keep per region.'
        )
        self.add_passthru_arg(
            '--seed',
            type=str,
            default='',
            help=('Sampling seed provided by the source. The same seed'
                  ' always reproduces the same sample.')
        )

    def load_args(self, args):
        super(MRMonthlyRegionSampler, self).load_args(args)

        if self.options.sample_size is None:
            self.arg_parser.error('You must specify the --sample-size')
        else:
            self.sample_size = self.options.sample_size

        self.seed = self.options.seed

    def mapper_init(self):
        # region -> {event key: entry}; one merged entry per distinct
        # event, which is the only way to de-duplicate re-sent events
        # and resolve their versions exactly
        self._events = {}

    def mapper(self, _, record):
        region = record['region']

        events = self._events.get(region)
        if events is None:
            events = self._events[region] = {}

        _absorb(events, _make_entry(
            record['event_id'],
            record.get('version', 0),
            record.get('payload'),
            score_for_event(self.seed, record['event_id'])))

    def mapper_final(self):
        # only the shard's selected samples and conflict evidence
        # (digests) are ever transmitted
        for region, events in self._events.items():
            candidates = _top_k_entries(events, self.sample_size)
            candidate_keys = set(_entry_key(entry) for entry in candidates)

            evidence = [
                _evidence_from_entry(entry)
                for key, entry in events.items()
                if key not in candidate_keys
            ]

            yield region, {
                'candidates': candidates,
                'evidence': sorted(evidence, key=_entry_key),
            }

    def combiner(self, region, values):
        yield region, _merge_shard_values(values, self.sample_size)

    def reducer(self, region, values):
        merged = _merge_shard_values(values, self.sample_size)

        sample = []
        conflicts = []

        # sampled events always carry every payload seen for the winning
        # version (any shard that saw the event selected it), so their
        # conflicts can be reported with the actual payloads
        for entry in merged['candidates']:
            sample.append({
                'event_id': entry['event_id'],
                'version': entry['version'],
                'payload': _canonical_payload(entry),
            })
            if len(entry['payloads']) > 1:
                conflicts.append({
                    'event_id': entry['event_id'],
                    'version': entry['version'],
                    'payloads': [
                        entry['payloads'][payload_hash]
                        for payload_hash in sorted(entry['payloads'])
                    ],
                })

        # unsampled events are reported by payload hash, which is all the
        # evidence the shards needed to transmit
        for digest in merged['evidence']:
            if len(digest['hashes']) > 1:
                conflicts.append({
                    'event_id': digest['event_id'],
                    'version': digest['version'],
                    'payload_hashes': digest['hashes'],
                })

        yield region, {
            'sample': sample,
            'conflicts': sorted(conflicts, key=_entry_key),
        }


if __name__ == '__main__':
    MRMonthlyRegionSampler.run()
