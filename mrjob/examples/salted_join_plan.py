"""Pure planning logic for a salted (skew-handling) left join.

The catalog (dimension / left side of the join key lookup) can contain
several rows with the same key, and the detail (fact / left side of the
join, whose rows must all be preserved) may contain rows that are
byte-for-byte identical. A single hot key such as "uncategorized" would
otherwise swamp one reducer.

This module keeps the bucket allocation math and reference join semantics
in plain functions so they can be unit tested without a Hadoop runner; it
only imports the JSON protocol to measure shuffle bytes accurately.

The split model is:

* the detail rows of a hot key are spread across ``n`` buckets;
* the catalog rows of that key are replicated into all ``n`` buckets;
* every (key, bucket) pair is handled by one reducer call, so the hot key
  is no longer handled by a single reducer.

Catalog replication is the only extra shuffle traffic, which is what the
replication budget bounds.
"""
import json
from base64 import urlsafe_b64decode
from base64 import urlsafe_b64encode
from os.path import basename

from mrjob.protocol import JSONProtocol

CATALOG = 'catalog'
DETAIL = 'detail'

# Hadoop streaming counter names must not contain commas; URL-safe
# base64-encoded JSON keeps arbitrary join keys round-trippable through
# real Hadoop counters.
CATALOG_BYTES_COUNTER_GROUP = 'Salted Join Catalog Bytes'
DETAIL_BYTES_COUNTER_GROUP = 'Salted Join Detail Bytes'

_PROTOCOL = JSONProtocol()


def side_from_input_file(input_file):
    """Which join side does *input_file* (a path or URI) belong to?

    Files whose basename contains ``catalog`` are catalog input; all other
    input is treated as detail input.
    """
    if input_file and CATALOG in basename(input_file):
        return CATALOG
    return DETAIL


def encode_bucket_counter(key, bucket):
    """Encode a ``(key, bucket)`` pair as a Hadoop-safe counter name.

    The JSON form is URL-safe base64 with padding stripped, so arbitrary
    join keys (including commas) survive Hadoop counter names.
    """
    raw = json.dumps([key, bucket], separators=(',', ':')).encode('utf_8')
    return urlsafe_b64encode(raw).decode('ascii').rstrip('=')


def decode_bucket_counter(name):
    """Inverse of :py:func:`encode_bucket_counter`."""
    padded = name + '=' * (-len(name) % 4)
    raw = urlsafe_b64decode(padded.encode('ascii'))
    key, bucket = json.loads(raw.decode('utf_8'))
    return key, bucket


def record_byte_size(emit_key, emit_value):
    """Bytes a shuffled (key, value) record takes up, including the line
    terminator.

    This mirrors what mrjob writes between the mapper and reducer when the
    default :py:class:`~mrjob.protocol.JSONProtocol` is in use, so the
    planner reasons about the same bytes that actually cross the shuffle.
    """
    return len(_PROTOCOL.write(emit_key, emit_value)) + 1


def aggregate_stats(stat_records):
    """Combine stats-job output into per-key catalog/detail stats.

    Each record is a pair of ``(side, key)`` and
    ``(row_count, byte_count)`` as emitted by the stats job. Returns a pair
    of dicts::

        ({key: (catalog_rows, catalog_bytes)},
         {key: (detail_rows, detail_bytes)})
    """
    catalog_stats = {}
    detail_stats = {}

    for side_key, counts in stat_records:
        side, key = side_key
        row_count, byte_count = counts
        target = catalog_stats if side == CATALOG else detail_stats
        old_rows, old_bytes = target.get(key, (0, 0))
        target[key] = (old_rows + row_count, old_bytes + byte_count)

    return catalog_stats, detail_stats


def _bucket_load(catalog_bytes, detail_bytes, buckets):
    """Expected bytes landing in one bucket of a key split this many ways.
    """
    return catalog_bytes + float(detail_bytes) / buckets


def choose_buckets(catalog_stats, detail_stats, max_buckets,
                   replication_budget=None):
    """Decide how many salt buckets each key gets.

    * ``catalog_stats`` / ``detail_stats`` map each key to
      ``(row_count, byte_count)`` (missing keys have zero rows/bytes).
    * ``max_buckets`` bounds the number of buckets any single key may use.
    * ``replication_budget`` bounds the total catalog bytes shuffled to
      reducers (each extra bucket re-sends the whole catalog payload of the
      key). ``None`` means unlimited.

    Every key starts in a single bucket. We then repeatedly hand one more
    bucket to the key whose current buckets are the heaviest, until every
    eligible key is at the cap or the budget is exhausted. Greedily
    splitting the heaviest bucket is the standard makespan-minimizing move.

    Keys with fewer than two detail rows are not split: there is no skew to
    spread and an empty bucket would only re-broadcast catalog rows for
    nothing.
    """
    if max_buckets < 1:
        raise ValueError('max_buckets must be at least 1')

    keys = set(catalog_stats) | set(detail_stats)

    buckets = dict((key, 1) for key in keys)

    baseline_broadcast = sum(
        catalog_stats.get(key, (0, 0))[1] for key in keys)

    if (replication_budget is not None and
            baseline_broadcast > replication_budget):
        raise ValueError(
            'replication_budget of %d bytes is below the %d bytes of'
            ' catalog data that must be shuffled even without any'
            ' replication' % (replication_budget, baseline_broadcast))

    broadcast_bytes = baseline_broadcast

    while True:
        best_key = None
        best_load = None

        for key in keys:
            current = buckets[key]
            detail_rows = detail_stats.get(key, (0, 0))[0]

            if current >= max_buckets or current >= detail_rows:
                continue

            catalog_bytes = catalog_stats.get(key, (0, 0))[1]
            if (replication_budget is not None and
                    broadcast_bytes + catalog_bytes > replication_budget):
                continue

            load = _bucket_load(
                catalog_bytes, detail_stats.get(key, (0, 0))[1], current)

            if best_load is None or load > best_load or (
                    load == best_load and _sort_key(key) <
                    _sort_key(best_key)):
                best_key = key
                best_load = load

        if best_key is None:
            break

        buckets[best_key] += 1
        broadcast_bytes += catalog_stats.get(best_key, (0, 0))[1]

    return _build_plan(
        buckets, catalog_stats, detail_stats, broadcast_bytes,
        baseline_broadcast)


def _sort_key(value):
    # JSON gives a deterministic, type-agnostic ordering for tie-breaks.
    return json.dumps(value, sort_keys=True)


def _build_plan(buckets, catalog_stats, detail_stats,
                broadcast_bytes, baseline_broadcast):
    expected_bucket_bytes = {}
    heaviest_buckets = []

    for key in sorted(buckets, key=_sort_key):
        bucket_count = buckets[key]
        catalog_bytes = catalog_stats.get(key, (0, 0))[1]
        detail_bytes = detail_stats.get(key, (0, 0))[1]

        per_bucket = _bucket_load(
            catalog_bytes, detail_bytes, bucket_count)
        expected_bucket_bytes[key] = per_bucket

        for bucket in range(bucket_count):
            heaviest_buckets.append((key, bucket, per_bucket))

    heaviest_buckets.sort(key=lambda item: (-item[2], _sort_key(item[0]),
                                            item[1]))

    return {
        'buckets': buckets,
        'broadcast_bytes': broadcast_bytes,
        'replication_overhead_bytes':
            broadcast_bytes - baseline_broadcast,
        'expected_bucket_bytes': expected_bucket_bytes,
        'heaviest_expected_buckets': heaviest_buckets,
    }


def summarize_actual_buckets(counters):
    """Turn stage-two byte counters into an actual bucket weight report.

    *counters* is the per-step counter dictionary produced by mrjob (a map
    of counter group to counter name -> amount). Catalog bytes are counted
    once per replicated copy, so their total across buckets is also the
    actual broadcast volume.
    """
    catalog_counters = counters.get(CATALOG_BYTES_COUNTER_GROUP, {})
    detail_counters = counters.get(DETAIL_BYTES_COUNTER_GROUP, {})

    catalog_counter_bytes = dict(
        (decode_bucket_counter(name), amount)
        for name, amount in catalog_counters.items())
    detail_counter_bytes = dict(
        (decode_bucket_counter(name), amount)
        for name, amount in detail_counters.items())

    pairs = set(catalog_counter_bytes) | set(detail_counter_bytes)

    bucket_report = []
    actual_broadcast = 0

    for pair in sorted(pairs, key=lambda item: (_sort_key(item[0]),
                                                item[1])):
        catalog_bytes = catalog_counter_bytes.get(pair, 0)
        detail_bytes = detail_counter_bytes.get(pair, 0)
        actual_broadcast += catalog_bytes
        bucket_report.append(
            (pair[0], pair[1], catalog_bytes + detail_bytes,
             catalog_bytes, detail_bytes))

    bucket_report.sort(key=lambda item: (-item[2], _sort_key(item[0]),
                                         item[1]))

    return {
        'buckets': bucket_report,
        'broadcast_bytes': actual_broadcast,
        'heaviest_buckets': bucket_report,
    }


def write_plan(plan, path):
    """Persist a bucket plan as JSON."""
    serializable = dict(plan)
    serializable['buckets'] = [
        [key, bucket_count]
        for key, bucket_count in plan['buckets'].items()]
    serializable.pop('heaviest_expected_buckets', None)
    serializable.pop('expected_bucket_bytes', None)

    with open(path, 'w') as f:
        json.dump(serializable, f, sort_keys=True)


def read_plan(path):
    """Load a bucket plan written by :py:func:`write_plan`."""
    with open(path) as f:
        serializable = json.load(f)

    serializable['buckets'] = dict(
        (key, bucket_count)
        for key, bucket_count in serializable['buckets'])
    return serializable


def plain_left_join(catalog_rows, detail_rows):
    """Reference semantics of an ordinary (unsalted) left join.

    ``catalog_rows`` and ``detail_rows`` are lists of ``(key, payload)``
    pairs. Every detail row is preserved exactly once per catalog match,
    including fully duplicated rows on either side, and detail rows with no
    catalog match are emitted a single time with a ``None`` catalog
    payload.
    """
    catalog_index = {}
    for key, payload in catalog_rows:
        catalog_index.setdefault(key, []).append(payload)

    results = []
    for key, payload in detail_rows:
        matches = catalog_index.get(key)
        if not matches:
            results.append(
                (key, {'detail': payload, 'catalog': None}))
        else:
            for catalog_payload in matches:
                results.append(
                    (key, {'detail': payload,
                           'catalog': catalog_payload}))

    return results
