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
"""End-to-end tests for the two-stage salted left join.

The small sample below is reconciled row by row with an ordinary left join
(:py:func:`mrjob.examples.salted_join_plan.plain_left_join`), including:

* a hot "uncategorized" key with several catalog rows,
* two byte-for-byte identical detail rows,
* a detail row that has no catalog match (must appear exactly once),
* a catalog row that has no detail match (must be dropped).
"""
import os
from collections import Counter

from mrjob.examples.mr_salted_join_stats import MRSaltedJoinStats
from mrjob.examples.mr_salted_left_join import MRSaltedLeftJoin
from mrjob.examples.salted_join import run_salted_left_join
from mrjob.examples.salted_join_plan import aggregate_stats
from mrjob.examples.salted_join_plan import choose_buckets
from mrjob.examples.salted_join_plan import plain_left_join
from mrjob.examples.salted_join_plan import record_byte_size
from mrjob.examples.salted_join_plan import summarize_actual_buckets
from mrjob.examples.salted_join_plan import write_plan

from tests.sandbox import SandboxedTestCase

CATALOG_LINES = (
    # hot key, duplicated catalog rows
    'uncategorized\tcat-A\n'
    'uncategorized\tcat-B\n'
    # ordinary key
    'sku-1\tcat-sku-1\n'
    # catalog-only key, no detail rows; must be dropped by the join
    'sku-ghost\tcat-ghost\n'
)

DETAIL_LINES = (
    # hot key, including two fully identical rows
    'uncategorized\tdet-1\n'
    'uncategorized\tdet-1\n'
    'uncategorized\tdet-2\n'
    'uncategorized\tdet-3\n'
    'uncategorized\tdet-4\n'
    # ordinary key
    'sku-1\torder-1\n'
    # orphan detail row, no catalog match; emitted once with None
    'sku-orphan\tlonely-order\n'
)

MAX_BUCKETS = 4
REPLICATION_BUDGET = 100000


class SaltedLeftJoinTestCase(SandboxedTestCase):

    def setUp(self):
        super(SaltedLeftJoinTestCase, self).setUp()

        self.catalog_path = self.makefile('catalog.tsv', CATALOG_LINES)
        self.detail_path = self.makefile('detail.tsv', DETAIL_LINES)
        self.input_paths = [self.catalog_path, self.detail_path]
        self.plan_path = os.path.join(self.tmp_dir, 'plan.json')

    def _stats(self):
        job = MRSaltedJoinStats(['-r', 'inline'] + self.input_paths)
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()
            return list(job.parse_output(runner.cat_output()))

    def _plan(self, max_buckets=MAX_BUCKETS,
              replication_budget=REPLICATION_BUDGET):
        catalog_stats, detail_stats = aggregate_stats(self._stats())
        return choose_buckets(
            catalog_stats, detail_stats, max_buckets,
            replication_budget)

    def _run_join(self, plan):
        write_plan(plan, self.plan_path)

        job = MRSaltedLeftJoin(
            ['-r', 'inline', '--plan-file', self.plan_path] +
            self.input_paths)
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()
            output = [
                (key, tuple(value))
                for key, value in job.parse_output(runner.cat_output())
            ]
            actual = summarize_actual_buckets(runner.counters()[0])

        return output, actual

    def test_plan_file_is_required(self):
        self.assertRaises(
            ValueError,
            MRSaltedLeftJoin, ['-r', 'inline'] + self.input_paths)

    def test_hot_key_is_split_but_cold_keys_are_not(self):
        plan = self._plan()

        self.assertGreater(plan['buckets']['uncategorized'], 1)
        self.assertLessEqual(
            plan['buckets']['uncategorized'], MAX_BUCKETS)
        self.assertEqual(plan['buckets']['sku-1'], 1)
        self.assertEqual(plan['buckets']['sku-orphan'], 1)
        self.assertEqual(plan['buckets']['sku-ghost'], 1)

    def test_join_output_matches_plain_left_join_row_by_row(self):
        output, _ = self._run_join(self._plan())

        expected = plain_left_join(
            _read_rows(self.catalog_path),
            _read_rows(self.detail_path))
        expected = [
            (key, (value['detail'], value['catalog']))
            for key, value in expected
        ]

        self.assertEqual(Counter(output), Counter(expected))

    def test_small_sample_explicit_rows(self):
        output, _ = self._run_join(self._plan())
        counts = Counter(output)

        # each of the 5 hot detail rows matches 2 catalog rows
        self.assertEqual(
            counts[('uncategorized', ('det-1', 'cat-A'))], 2)
        self.assertEqual(
            counts[('uncategorized', ('det-1', 'cat-B'))], 2)
        for detail in ('det-2', 'det-3', 'det-4'):
            self.assertEqual(
                counts[('uncategorized', (detail, 'cat-A'))], 1)
            self.assertEqual(
                counts[('uncategorized', (detail, 'cat-B'))], 1)

        # ordinary key
        self.assertEqual(
            counts[('sku-1', ('order-1', 'cat-sku-1'))], 1)

        # unmatched detail row appears exactly once, catalog is None
        self.assertEqual(
            counts[('sku-orphan', ('lonely-order', None))], 1)

        # catalog-only key never produces output
        self.assertFalse(
            any(key == 'sku-ghost' for key, _ in output))

        # 5 hot rows x 2 catalog rows + 1 ordinary + 1 orphan
        self.assertEqual(len(output), 12)

    def test_actual_buckets_match_the_plan_and_measure_broadcast(self):
        plan = self._plan()
        output, actual = self._run_join(plan)
        self.assertTrue(output)

        planned_pairs = set()
        for key, bucket_count in plan['buckets'].items():
            for bucket in range(bucket_count):
                planned_pairs.add((key, bucket))

        actual_pairs = set(
            (key, bucket)
            for key, bucket, _, _, _ in actual['heaviest_buckets'])

        self.assertEqual(actual_pairs, planned_pairs)

        # broadcast volume is every replicated catalog copy, so it must
        # equal the planner's predicted broadcast volume
        self.assertEqual(
            actual['broadcast_bytes'], plan['broadcast_bytes'])

        # bucket totals are sorted heaviest first
        totals = [total for _, _, total, _, _ in
                  actual['heaviest_buckets']]
        self.assertEqual(totals, sorted(totals, reverse=True))

        # catalog + detail bytes add up to each bucket's total
        for _, _, total, catalog_bytes, detail_bytes in (
                actual['heaviest_buckets']):
            self.assertEqual(total, catalog_bytes + detail_bytes)

    def test_replication_budget_forces_a_single_bucket(self):
        catalog_stats, detail_stats = aggregate_stats(self._stats())
        baseline = sum(
            bytes_for_key for _, bytes_for_key in catalog_stats.values())

        plan = choose_buckets(
            catalog_stats, detail_stats, MAX_BUCKETS,
            replication_budget=baseline)

        # no room for any extra catalog copy
        self.assertTrue(
            all(bucket_count == 1
                for bucket_count in plan['buckets'].values()))
        self.assertEqual(plan['replication_overhead_bytes'], 0)
        self.assertEqual(plan['broadcast_bytes'], baseline)

        output, actual = self._run_join(plan)

        # with one bucket per key, broadcast equals the catalog bytes
        self.assertEqual(actual['broadcast_bytes'], baseline)
        # and join semantics are still intact
        self.assertEqual(len(output), 12)

    def test_driver_runs_both_stages_and_reconciles(self):
        result = run_salted_left_join(
            self.input_paths, self.plan_path,
            max_buckets=MAX_BUCKETS,
            replication_budget=REPLICATION_BUDGET)

        output = [
            (key, tuple(value)) for key, value in result['join_output']]

        expected = plain_left_join(
            _read_rows(self.catalog_path),
            _read_rows(self.detail_path))
        expected = [
            (key, (value['detail'], value['catalog']))
            for key, value in expected
        ]

        self.assertEqual(Counter(output), Counter(expected))
        self.assertGreater(
            result['plan']['buckets']['uncategorized'], 1)
        self.assertEqual(
            result['actual']['broadcast_bytes'],
            result['plan']['broadcast_bytes'])


class IdenticalDetailRowsSpreadTestCase(SandboxedTestCase):

    def test_identical_rows_land_in_different_buckets_and_repeat(self):
        # every detail row is byte-for-byte identical
        catalog_path = self.makefile(
            'catalog.tsv', 'uncategorized\tcat\n')
        detail_path = self.makefile(
            'detail.tsv', 'uncategorized\tsame\n' * 12)
        plan_path = os.path.join(self.tmp_dir, 'plan.json')

        stats_job = MRSaltedJoinStats(
            ['-r', 'inline', catalog_path, detail_path])
        stats_job.sandbox()
        with stats_job.make_runner() as runner:
            runner.run()
            stats = list(stats_job.parse_output(runner.cat_output()))

        catalog_stats, detail_stats = aggregate_stats(stats)
        plan = choose_buckets(
            catalog_stats, detail_stats, max_buckets=4)
        self.assertEqual(plan['buckets']['uncategorized'], 4)
        write_plan(plan, plan_path)

        def run_once():
            job = MRSaltedLeftJoin(
                ['-r', 'inline', '--plan-file', plan_path,
                 catalog_path, detail_path])
            job.sandbox()
            with job.make_runner() as runner:
                runner.run()
                output = sorted(
                    job.parse_output(runner.cat_output()))
                actual = summarize_actual_buckets(runner.counters()[0])
            return output, actual

        output, actual = run_once()

        # all 12 identical detail rows join the single catalog row
        self.assertEqual(len(output), 12)
        self.assertTrue(
            all(row == ('uncategorized', ['same', 'cat'])
                for row in output))

        # the identical rows were spread over all four buckets, so no
        # single reducer swallowed the whole hot key
        detail_buckets = [
            (key, bucket)
            for key, bucket, _, catalog_bytes, detail_bytes in
            actual['heaviest_buckets']
            if detail_bytes]
        self.assertEqual(len(detail_buckets), 4)

        # the assignment is deterministic: a second run gives the same
        # per-bucket detail bytes
        _, rerun_actual = run_once()
        self.assertEqual(
            rerun_actual['heaviest_buckets'],
            actual['heaviest_buckets'])

    def test_salt_seed_rebalances_without_changing_the_join(self):
        catalog_path = self.makefile('catalog.tsv', 'k\tcat\n')
        detail_path = self.makefile(
            'detail.tsv', 'k\tsame\n' * 16)
        plan_path = os.path.join(self.tmp_dir, 'plan.json')

        stats_job = MRSaltedJoinStats(
            ['-r', 'inline', catalog_path, detail_path])
        stats_job.sandbox()
        with stats_job.make_runner() as runner:
            runner.run()
            stats = list(stats_job.parse_output(runner.cat_output()))

        catalog_stats, detail_stats = aggregate_stats(stats)
        write_plan(
            choose_buckets(catalog_stats, detail_stats, max_buckets=4),
            plan_path)

        def run_with_seed(seed):
            job = MRSaltedLeftJoin(
                ['-r', 'inline', '--plan-file', plan_path,
                 '--salt-seed', seed, catalog_path, detail_path])
            job.sandbox()
            with job.make_runner() as runner:
                runner.run()
                output = sorted(job.parse_output(runner.cat_output()))
                actual = summarize_actual_buckets(runner.counters()[0])
            return output, actual

        output_a, actual_a = run_with_seed('one')
        output_b, actual_b = run_with_seed('two')

        # join result is identical regardless of the seed
        self.assertEqual(output_a, output_b)
        self.assertEqual(len(output_a), 16)

        # different seeds permute which bytes land in which bucket
        self.assertNotEqual(
            actual_a['heaviest_buckets'],
            actual_b['heaviest_buckets'])
        # but total detail and broadcast bytes are preserved
        self.assertEqual(
            actual_a['broadcast_bytes'], actual_b['broadcast_bytes'])


class SaltedJoinStatsTestCase(SandboxedTestCase):
    def test_empty_input(self):
        job = MRSaltedJoinStats(['-r', 'inline'])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()
            self.assertEqual(
                list(job.parse_output(runner.cat_output())), [])

    def test_counts_rows_and_shuffle_bytes_per_side_and_key(self):
        catalog_path = self.makefile('catalog.tsv', 'k\tc1\nk\tc2\n')
        detail_path = self.makefile('detail.tsv', 'k\td1\n')

        job = MRSaltedJoinStats(
            ['-r', 'inline', catalog_path, detail_path])
        job.sandbox()

        with job.make_runner() as runner:
            runner.run()
            stats = dict(
                (tuple(key), tuple(value))
                for key, value in job.parse_output(runner.cat_output()))

        expected_catalog_bytes = (
            record_byte_size(('k', 0), ('catalog', 'c1')) +
            record_byte_size(('k', 0), ('catalog', 'c2')))
        expected_detail_bytes = record_byte_size(
            ('k', 0), ('detail', 'd1'))

        self.assertEqual(
            stats[('catalog', 'k')], (2, expected_catalog_bytes))
        self.assertEqual(
            stats[('detail', 'k')], (1, expected_detail_bytes))


def _read_rows(path):
    rows = []
    with open(path) as f:
        for line in f:
            key, payload = line.rstrip('\n').split('\t', 1)
            rows.append((key, payload))
    return rows
