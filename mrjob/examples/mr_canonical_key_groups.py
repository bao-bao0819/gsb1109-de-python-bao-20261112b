"""Count values grouped by a canonical JSON key.

This is a worked example of the cross-language key protocol in
:py:mod:`mrjob.canonical`. A second mapper (written in any other
language) just has to emit the same canonical bytes for the same key
and it can share this reducer.

Each input line is one JSON object::

    {"partition": "p07", "id": "r42", "key": {...}, "v": 3}

* ``partition`` / ``id`` identify where the record came from, so a
  quarantined key can point back at the exact source partition
* ``key`` is the grouping key; nested objects, arrays, and missing
  fields are all allowed
* ``v`` is the value to sum

Rollout is per batch: pass ``--canonical-batches p0,p1`` to switch
those partitions onto the canonical protocol. Records from other
partitions keep the old JSON behavior under a separate namespace so
the two shuffles can never mix.
"""
from mrjob.canonical import Quarantine
from mrjob.canonical import canonical_shuffle_key
from mrjob.canonical import decanonicalize
from mrjob.canonical import legacy_value
from mrjob.canonical import loads_checked
from mrjob.job import MRJob
from mrjob.protocol import CanonicalJSONProtocol

#: Reserved key under which quarantine notices are emitted, so they can
#: be inspected in the output instead of killing the task.
QUARANTINE_KEY = u'__quarantine__'

#: Tag wrapping keys from batches that have not been cut over yet.
LEGACY_TAG = u'__legacy__'


class MRCanonicalKeyGroupsJob(MRJob):

    INTERNAL_PROTOCOL = CanonicalJSONProtocol
    OUTPUT_PROTOCOL = CanonicalJSONProtocol

    def configure_args(self):
        super(MRCanonicalKeyGroupsJob, self).configure_args()

        self.add_passthru_arg(
            '--canonical-batches', default='',
            help=('comma-separated partition names switched onto the '
                  'canonical key protocol; other partitions stay legacy'))

    def _enabled_partitions(self):
        return set(
            name.strip()
            for name in self.options.canonical_batches.split(',')
            if name.strip())

    def mapper(self, _, line):
        # Keep the checked wrappers on the key sub-tree; the top-level
        # record fields are plain enough to view as a dictionary.
        record = dict(loads_checked(line))

        partition = record['partition']
        record_id = record['id']
        key = record['key']
        value = record['v']

        if partition not in self._enabled_partitions():
            # Legacy batch: keep the old last-wins, rounding JSON
            # semantics, but in their own namespace so legacy groups can
            # never collide with a canonical group.
            yield (LEGACY_TAG, partition, legacy_value(key)), value
            return

        quarantine = Quarantine()
        raw_key = canonical_shuffle_key(
            key,
            quarantine=quarantine,
            partition=partition,
            record_id=record_id)

        if raw_key is None:
            # The key is unsafe to group (duplicate member or lost
            # precision). Point the operator at the partition to
            # re-process, and keep the record out of the shuffle.
            for entry in quarantine.entries():
                self.increment_counter(
                    'Canonical Key Quarantine', entry['reason'])
                yield QUARANTINE_KEY, entry
            return

        # Re-decoding the canonical bytes gives back a plain value that
        # the output protocol re-canonicalizes to the same bytes.
        yield decanonicalize(raw_key), value

    def reducer(self, key, values):
        if key == QUARANTINE_KEY:
            for notice in values:
                yield key, notice
        else:
            yield key, sum(values)


if __name__ == '__main__':
    MRCanonicalKeyGroupsJob.run()
