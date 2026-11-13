# Copyright 2019 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from collections import Counter

from mrjob.examples.salted_join_plan import CATALOG
from mrjob.examples.salted_join_plan import CATALOG_BYTES_COUNTER_GROUP
from mrjob.examples.salted_join_plan import DETAIL
from mrjob.examples.salted_join_plan import DETAIL_BYTES_COUNTER_GROUP
from mrjob.examples.salted_join_plan import aggregate_stats
from mrjob.examples.salted_join_plan import choose_buckets
from mrjob.examples.salted_join_plan import decode_bucket_counter
from mrjob.examples.salted_join_plan import encode_bucket_counter
from mrjob.examples.salted_join_plan import plain_left_join
from mrjob.examples.salted_join_plan import read_plan
from mrjob.examples.salted_join_plan import record_byte_size
from mrjob.examples.salted_join_plan import side_from_input_file
from mrjob.examples.salted_join_plan import summarize_actual_buckets
from mrjob.examples.salted_join_plan import write_plan

from tests.sandbox import SandboxedTestCase


class ChooseBucketsTestCase(SandboxedTestCase):

    def test_empty_input_gets_empty_plan(self):
        plan = choose_buckets({}, {}, max_buckets=4)

        self.assertEqual(plan['buckets'], {})
        self.assertEqual(plan['broadcast_bytes'], 0)
        self.assertEqual(plan['replication_overhead_bytes'], 0)
        self.assertEqual(plan['heaviest_expected_buckets'], [])

    def test_cold_keys_stay_in_one_bucket(self):
        catalog_stats = {'a': (1, 10), 'b': (1, 10)}
        detail_stats = {'a': (1, 20), 'b': (1, 20)}

        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=8)

        self.assertEqual(plan['buckets'], {'a': 1, 'b': 1})
        # catalog is shuffled once per key, nothing is replicated
        self.assertEqual(plan['broadcast_bytes'], 20)
        self.assertEqual(plan['replication_overhead_bytes'], 0)

    def test_hot_key_is_split_up_to_max_buckets(self):
        catalog_stats = {'hot': (1, 100)}
        detail_stats = {'hot': (10, 1000)}

        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=4)

        self.assertEqual(plan['buckets'], {'hot': 4})
        # one base copy plus three replicated catalog copies
        self.assertEqual(plan['broadcast_bytes'], 400)
        self.assertEqual(plan['replication_overhead_bytes'], 300)

    def test_max_buckets_must_be_positive(self):
        self.assertRaises(
            ValueError, choose_buckets, {}, {}, max_buckets=0)

    def test_single_detail_row_is_never_split(self):
        catalog_stats = {'k': (1, 100)}
        detail_stats = {'k': (1, 1)}

        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=8)

        self.assertEqual(plan['buckets'], {'k': 1})

    def test_catalog_only_key_is_never_split(self):
        catalog_stats = {'k': (5, 100)}
        detail_stats = {}

        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=8)

        self.assertEqual(plan['buckets'], {'k': 1})

    def test_greedy_splits_heaviest_bucket_first(self):
        catalog_stats = {'big': (1, 10), 'small': (1, 10)}
        detail_stats = {'big': (8, 800), 'small': (4, 40)}

        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=2)

        # both keys may split, but the heavy key must be split before the
        # light one; with a cap of two buckets both end up split
        self.assertEqual(plan['buckets'], {'big': 2, 'small': 2})
        self.assertEqual(plan['broadcast_bytes'], 40)

        # the big key's expected per-bucket load must be lower than if it
        # had stayed in a single bucket
        self.assertLess(
            plan['expected_bucket_bytes']['big'], 810)

    def test_replication_budget_blocks_further_splits(self):
        catalog_stats = {'hot': (1, 100), 'warm': (1, 50)}
        detail_stats = {'hot': (10, 1000), 'warm': (10, 500)}

        baseline = 150
        # allow exactly one extra catalog copy, of the cheapest key
        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=8,
            replication_budget=baseline + 50)

        total_overhead = sum(
            (plan['buckets'][key] - 1) *
            catalog_stats.get(key, (0, 0))[1]
            for key in plan['buckets'])

        self.assertEqual(total_overhead, 50)
        self.assertEqual(plan['replication_overhead_bytes'], 50)
        self.assertEqual(plan['broadcast_bytes'], baseline + 50)
        self.assertEqual(plan['buckets']['warm'], 2)
        self.assertEqual(plan['buckets']['hot'], 1)

    def test_budget_below_baseline_raises(self):
        catalog_stats = {'k': (1, 100)}
        detail_stats = {'k': (4, 400)}

        self.assertRaises(
            ValueError,
            choose_buckets, catalog_stats, detail_stats,
            max_buckets=4, replication_budget=99)

    def test_budget_equal_to_baseline_forbids_replication(self):
        catalog_stats = {'k': (1, 100)}
        detail_stats = {'k': (4, 400)}

        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=4,
            replication_budget=100)

        self.assertEqual(plan['buckets'], {'k': 1})
        self.assertEqual(plan['broadcast_bytes'], 100)

    def test_heaviest_buckets_are_sorted_by_expected_bytes(self):
        catalog_stats = {'a': (1, 10), 'b': (1, 10)}
        detail_stats = {'a': (8, 800), 'b': (2, 20)}

        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=4)

        weights = [item[2] for item in plan['heaviest_expected_buckets']]
        self.assertEqual(weights, sorted(weights, reverse=True))
        # the heaviest bucket belongs to the hot key
        self.assertEqual(plan['heaviest_expected_buckets'][0][0], 'a')

    def test_bucket_never_exceeds_detail_row_count(self):
        catalog_stats = {'k': (1, 10)}
        detail_stats = {'k': (3, 300)}

        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=99)

        self.assertEqual(plan['buckets'], {'k': 3})


class AggregateStatsTestCase(SandboxedTestCase):

    def test_aggregates_rows_and_bytes_per_side_and_key(self):
        records = [
            ((CATALOG, 'k'), (2, 50)),
            ((CATALOG, 'k'), (1, 25)),
            ((DETAIL, 'k'), (3, 90)),
            ((DETAIL, 'other'), (1, 10)),
        ]

        catalog_stats, detail_stats = aggregate_stats(records)

        self.assertEqual(catalog_stats, {'k': (3, 75)})
        self.assertEqual(
            detail_stats, {'k': (3, 90), 'other': (1, 10)})


class RecordByteSizeTestCase(SandboxedTestCase):

    def test_matches_json_protocol_line_plus_newline(self):
        from mrjob.protocol import JSONProtocol

        line = JSONProtocol().write(('k', 2), (CATALOG, 'p'))

        self.assertEqual(
            record_byte_size(('k', 2), (CATALOG, 'p')),
            len(line) + 1)


class BucketCounterCodecTestCase(SandboxedTestCase):

    def test_round_trip(self):
        for key, bucket in [('uncategorized', 0), ('k, with comma', 3),
                            ('中文键', 7), ('', 1)]:
            name = encode_bucket_counter(key, bucket)
            # Hadoop counter names must not contain commas
            self.assertNotIn(',', name)
            self.assertEqual(decode_bucket_counter(name), (key, bucket))


class SummarizeActualBucketsTestCase(SandboxedTestCase):

    def test_reports_bucket_weights_and_broadcast(self):
        counters = {
            CATALOG_BYTES_COUNTER_GROUP: {
                encode_bucket_counter('hot', 0): 100,
                encode_bucket_counter('hot', 1): 100,
                encode_bucket_counter('cold', 0): 40,
            },
            DETAIL_BYTES_COUNTER_GROUP: {
                encode_bucket_counter('hot', 0): 30,
                encode_bucket_counter('hot', 1): 20,
                encode_bucket_counter('cold', 0): 10,
            },
        }

        report = summarize_actual_buckets(counters)

        # catalog bytes counted once per replicated copy
        self.assertEqual(report['broadcast_bytes'], 240)

        totals = dict(
            ((key, bucket), total)
            for key, bucket, total, _, _ in report['heaviest_buckets'])
        self.assertEqual(
            totals,
            {('hot', 0): 130, ('hot', 1): 120, ('cold', 0): 50})

        # sorted heaviest first
        ordered = [total for _, _, total, _, _ in
                   report['heaviest_buckets']]
        self.assertEqual(ordered, sorted(ordered, reverse=True))

    def test_empty_counters(self):
        report = summarize_actual_buckets({})

        self.assertEqual(report['broadcast_bytes'], 0)
        self.assertEqual(report['heaviest_buckets'], [])


class PlanFileTestCase(SandboxedTestCase):

    def test_write_and_read_plan_round_trip(self):
        catalog_stats = {'hot': (1, 100)}
        detail_stats = {'hot': (6, 600)}
        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=3)

        path = self.makefile('plan.json')
        write_plan(plan, path)

        reloaded = read_plan(path)
        self.assertEqual(reloaded['buckets'], plan['buckets'])
        self.assertEqual(
            reloaded['broadcast_bytes'], plan['broadcast_bytes'])


class PlainLeftJoinTestCase(SandboxedTestCase):

    def test_duplicate_catalog_rows_multiply_matches(self):
        catalog = [('k', 'c1'), ('k', 'c2')]
        detail = [('k', 'd1')]

        self.assertEqual(
            Counter(_normalized(plain_left_join(catalog, detail))),
            Counter([
                ('k', ('d1', 'c1')),
                ('k', ('d1', 'c2')),
            ]))

    def test_identical_detail_rows_are_both_kept(self):
        catalog = [('k', 'c1')]
        detail = [('k', 'd1'), ('k', 'd1')]

        self.assertEqual(
            Counter(_normalized(plain_left_join(catalog, detail))),
            Counter([
                ('k', ('d1', 'c1')),
                ('k', ('d1', 'c1')),
            ]))

    def test_unmatched_detail_row_emitted_once_with_none(self):
        catalog = [('k', 'c1')]
        detail = [('lonely', 'd9')]

        self.assertEqual(
            plain_left_join(catalog, detail),
            [('lonely', {'detail': 'd9', 'catalog': None})])

    def test_catalog_only_rows_are_dropped(self):
        catalog = [('k', 'c1')]

        self.assertEqual(plain_left_join(catalog, []), [])

    def test_full_multiplicity_combination(self):
        catalog = [('k', 'c1'), ('k', 'c1'), ('k', 'c2')]
        detail = [('k', 'd1'), ('k', 'd1'), ('k', 'd2')]

        results = plain_left_join(catalog, detail)

        # 3 catalog rows x 3 detail rows = 9 output rows
        self.assertEqual(len(results), 9)
        normalized = Counter(_normalized(results))
        self.assertEqual(normalized[('k', ('d1', 'c1'))], 4)
        self.assertEqual(normalized[('k', ('d1', 'c2'))], 2)
        self.assertEqual(normalized[('k', ('d2', 'c1'))], 2)
        self.assertEqual(normalized[('k', ('d2', 'c2'))], 1)


class SideFromInputFileTestCase(SandboxedTestCase):

    def test_detects_catalog_by_basename(self):
        self.assertEqual(
            side_from_input_file('s3://b/dim/catalog-2026.tsv'),
            CATALOG)
        self.assertEqual(
            side_from_input_file('/tmp/join/catalog.tsv'), CATALOG)
        self.assertEqual(
            side_from_input_file('/tmp/detail/orders.tsv'), DETAIL)
        self.assertEqual(side_from_input_file(None), DETAIL)


def _normalized(rows):
    return [
        (key, (value['detail'], value['catalog']))
        for key, value in rows
    ]
