"""Stage two of the salted left join.

Given a bucket plan produced from :py:mod:`mrjob.examples.mr_salted_join_stats`
and :py:func:`mrjob.examples.salted_join_plan.choose_buckets`, this job
performs a left join that keeps every detail row while spreading hot keys
over several reducers.

Shuffle keys are ``(join_key, bucket)``:

* catalog rows of a key are replicated into every one of its buckets;
* each detail row is sent to exactly one bucket, chosen by hashing the
  seed, key, payload, input-split identity and a per-mapper sequence
  number, so even byte-identical rows land in different buckets and the
  assignment stays deterministic for a given seed.

The reducer therefore sees the full catalog list of the key next to every
detail row, so the real join multiplicity is preserved:

* a detail row matching ``m`` catalog rows (duplicates included) is
  emitted once per catalog row;
* a detail row with no catalog match is emitted exactly once with a
  ``None`` catalog payload;
* catalog rows with no matching detail produce nothing (this is a left
  join with detail as the preserved side).

Byte counters record the actual shuffle bytes landing in each bucket,
including every replicated catalog copy, so the real broadcast volume and
the heaviest bucket can be reported after the run.
"""
from hashlib import md5

from mrjob.compat import jobconf_from_env
from mrjob.job import MRJob
from mrjob.protocol import RawValueProtocol

from mrjob.examples.salted_join_plan import CATALOG
from mrjob.examples.salted_join_plan import CATALOG_BYTES_COUNTER_GROUP
from mrjob.examples.salted_join_plan import DETAIL
from mrjob.examples.salted_join_plan import DETAIL_BYTES_COUNTER_GROUP
from mrjob.examples.salted_join_plan import encode_bucket_counter
from mrjob.examples.salted_join_plan import read_plan
from mrjob.examples.salted_join_plan import record_byte_size
from mrjob.examples.salted_join_plan import side_from_input_file


class MRSaltedLeftJoin(MRJob):

    # input is "<key>\\t<payload>" text, not JSON
    INPUT_PROTOCOL = RawValueProtocol

    # secondary sort puts ("catalog", ...) values before ("detail", ...)
    # within every (key, bucket) group, so the replicated catalog rows
    # are all available before the detail rows stream past
    SORT_VALUES = True

    def configure_args(self):
        super(MRSaltedLeftJoin, self).configure_args()

        self.add_file_arg(
            '--plan-file', dest='plan_file', default=None,
            help='JSON bucket plan produced by the planning stage')
        self.add_passthru_arg(
            '--salt-seed', dest='salt_seed', default='0',
            help=('deterministic seed mixed into the detail bucket hash;'
                  ' change it to rebalance rows without changing the plan'))

    def load_args(self, args):
        super(MRSaltedLeftJoin, self).load_args(args)

        if not self.options.plan_file:
            self.arg_parser.error('You must specify the --plan-file')

        self._bucket_map = read_plan(self.options.plan_file)['buckets']
        # per-mapper sequence number; mixed into the bucket hash so that
        # byte-identical detail rows are spread across buckets
        self._detail_seq = 0
        self._split_identity = '0'

    def mapper_init(self):
        # the framework builds a fresh job instance per mapper, but reset
        # the state explicitly so the contract is obvious
        self._detail_seq = 0

        input_file = (
            jobconf_from_env('mapreduce.map.input.file') or '')
        start = jobconf_from_env('mapreduce.map.input.start') or '0'
        # file + offset uniquely identifies the input split, which keeps
        # the per-mapper sequence numbers from colliding across mappers
        self._split_identity = '%s@%s' % (input_file, start)

    def mapper(self, _, line):
        if not line:
            return

        if isinstance(line, bytes):
            line = line.decode('utf_8')

        key, payload = line.split('\t', 1)
        side = side_from_input_file(
            jobconf_from_env('mapreduce.map.input.file'))
        bucket_count = self._bucket_map.get(key, 1)

        if side == CATALOG:
            # replicate the catalog row into every bucket of the key
            for bucket in range(bucket_count):
                emit_key = (key, bucket)
                value = (CATALOG, payload)

                self.increment_counter(
                    CATALOG_BYTES_COUNTER_GROUP,
                    encode_bucket_counter(key, bucket),
                    record_byte_size(emit_key, value))

                yield emit_key, value
        else:
            # each detail row goes to exactly one bucket; the sequence
            # number and split identity ensure byte-identical rows are
            # spread out
            bucket = self._detail_bucket(
                key, payload, bucket_count, self._detail_seq)
            self._detail_seq += 1
            emit_key = (key, bucket)
            value = (DETAIL, payload)

            self.increment_counter(
                DETAIL_BYTES_COUNTER_GROUP,
                encode_bucket_counter(key, bucket),
                record_byte_size(emit_key, value))

            yield emit_key, value

    def reducer(self, key_bucket, values):
        key, _bucket = key_bucket
        catalog_payloads = []

        # catalog rows arrive first thanks to SORT_VALUES; they are small
        # dimension data replicated in full, so buffer them and stream the
        # detail rows past them
        for side, payload in values:
            if side == CATALOG:
                catalog_payloads.append(payload)
                continue

            if catalog_payloads:
                for catalog_payload in catalog_payloads:
                    yield key, (payload, catalog_payload)
            else:
                # unmatched detail row: emit it exactly once, preserving
                # the left-join semantics
                yield key, (payload, None)

        # keys with catalog rows but no detail side simply produce nothing,
        # which is correct for a left join (detail is the preserved side)

    def _detail_bucket(self, key, payload, bucket_count, sequence):
        if bucket_count == 1:
            return 0

        # hash the whole salt rather than adding aligned byte offsets,
        # which otherwise collapse modulo the bucket count for rows of a
        # uniform width
        salt = '\x1f'.join([
            self.options.salt_seed, key, payload,
            self._split_identity, str(sequence)])
        digest = int(md5(salt.encode('utf_8')).hexdigest(), 16)
        return digest % bucket_count


if __name__ == '__main__':
    MRSaltedLeftJoin.run()
