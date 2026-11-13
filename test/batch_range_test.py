# -*- coding: utf-8 -*-
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
#

import datetime
import json
import os
import shutil
import tempfile

from helpers import LuigiTestCase, unittest

import luigi
import luigi.task_register
from luigi.parameter import ParameterException
from luigi.tools.batch_range import (
    Batch,
    BatchedRangeDaily,
    WindowedFetchTask,
    coverage,
    plan_batches,
    plan_windows,
)

D = datetime.date  # shorthand


def dates(start, n):
    return [start + datetime.timedelta(days=i) for i in range(n)]


class PlanWindowsTest(LuigiTestCase):
    def test_adjacent_dates_merge_into_one_window(self):
        windows = plan_windows(dates(D(2026, 1, 1), 5), max_days=7)
        self.assertEqual(windows, [(D(2026, 1, 1), D(2026, 1, 5))])

    def test_max_days_is_respected_and_request_count_minimal(self):
        windows = plan_windows(dates(D(2026, 1, 1), 10), max_days=4)
        self.assertEqual(
            windows,
            [(D(2026, 1, 1), D(2026, 1, 4)), (D(2026, 1, 5), D(2026, 1, 8)), (D(2026, 1, 9), D(2026, 1, 10))],
        )
        for start, end in windows:
            self.assertLessEqual((end - start).days + 1, 4)

    def test_window_may_span_unrequested_dates_to_save_requests(self):
        # Jan 2..4 are already complete; billing is per window, so one trip
        windows = plan_windows([D(2026, 1, 1), D(2026, 1, 5)], max_days=7)
        self.assertEqual(windows, [(D(2026, 1, 1), D(2026, 1, 5))])

    def test_forbidden_date_is_never_crossed(self):
        windows = plan_windows(dates(D(2026, 1, 1), 5), forbidden_dates={D(2026, 1, 3)}, max_days=7)
        self.assertEqual(windows, [(D(2026, 1, 1), D(2026, 1, 2)), (D(2026, 1, 4), D(2026, 1, 5))])

    def test_forbidden_requested_date_stays_uncovered(self):
        windows = plan_windows([D(2026, 1, 1), D(2026, 1, 2), D(2026, 1, 3)], forbidden_dates={D(2026, 1, 2)}, max_days=7)
        self.assertEqual(windows, [(D(2026, 1, 1), D(2026, 1, 1)), (D(2026, 1, 3), D(2026, 1, 3))])

    def test_max_days_one_means_one_request_per_date(self):
        windows = plan_windows(dates(D(2026, 1, 1), 3), max_days=1)
        self.assertEqual(windows, [(d, d) for d in dates(D(2026, 1, 1), 3)])

    def test_empty_input_gives_no_windows(self):
        self.assertEqual(plan_windows([], max_days=7), [])

    def test_invalid_max_days_rejected(self):
        self.assertRaises(ValueError, plan_windows, dates(D(2026, 1, 1), 3), frozenset(), 0)

    def test_accepts_datetimes_and_iso_strings(self):
        windows = plan_windows(
            [datetime.datetime(2026, 1, 1, 12), "2026-01-02"],
            forbidden_dates=["2026-01-04"],
            max_days=7,
        )
        self.assertEqual(windows, [(D(2026, 1, 1), D(2026, 1, 2))])


class PlanBatchesTest(LuigiTestCase):
    def test_scopes_and_versions_are_never_merged(self):
        missing = [(d, scope, version) for scope in ("s1", "s2") for version in ("v1", "v2") for d in dates(D(2026, 1, 1), 3)]
        batches = plan_batches(missing, max_days=7)
        self.assertEqual(len(batches), 4)
        for batch in batches:
            self.assertEqual(batch.start, D(2026, 1, 1))
            self.assertEqual(batch.end, D(2026, 1, 3))
        self.assertEqual({(b.scope, b.version) for b in batches}, {("s1", "v1"), ("s1", "v2"), ("s2", "v1"), ("s2", "v2")})

    def test_missing_mapping_input(self):
        batches = plan_batches({("s1", "v1"): dates(D(2026, 1, 1), 2)}, max_days=7)
        self.assertEqual(batches, [Batch(scope="s1", version="v1", start=D(2026, 1, 1), end=D(2026, 1, 2))])

    def test_windows_within_a_group_are_newest_first(self):
        missing = [(d, "s1", "v1") for d in dates(D(2026, 1, 1), 6)]
        batches = plan_batches(missing, max_days=2)
        self.assertEqual(
            batches,
            [
                Batch("s1", "v1", D(2026, 1, 5), D(2026, 1, 6)),
                Batch("s1", "v1", D(2026, 1, 3), D(2026, 1, 4)),
                Batch("s1", "v1", D(2026, 1, 1), D(2026, 1, 2)),
            ],
        )

    def test_per_scope_forbidden_dates(self):
        missing = [(d, scope, "v1") for scope in ("s1", "s2") for d in dates(D(2026, 1, 1), 3)]
        batches = plan_batches(missing, forbidden_dates={"s1": [D(2026, 1, 2)]}, max_days=7)
        by_scope = {}
        for batch in batches:
            by_scope.setdefault(batch.scope, []).append((batch.start, batch.end))
        self.assertEqual(by_scope["s1"], [(D(2026, 1, 3), D(2026, 1, 3)), (D(2026, 1, 1), D(2026, 1, 1))])
        self.assertEqual(by_scope["s2"], [(D(2026, 1, 1), D(2026, 1, 3))])

    def test_callable_forbidden_dates(self):
        missing = [(d, "s1", "v1") for d in dates(D(2026, 1, 1), 3)]
        batches = plan_batches(missing, forbidden_dates=lambda scope, version: {D(2026, 1, 2)}, max_days=7)
        self.assertEqual([(b.start, b.end) for b in batches], [(D(2026, 1, 3), D(2026, 1, 3)), (D(2026, 1, 1), D(2026, 1, 1))])

    def test_coverage_matches_missing_minus_forbidden(self):
        missing = {(d, "s1", "v1") for d in dates(D(2026, 1, 1), 10)}
        forbidden = {D(2026, 1, 4)}
        batches = plan_batches(missing, forbidden_dates=forbidden, max_days=3)
        covered = coverage(batches)
        self.assertEqual(set(covered[("s1", "v1")]), {d for d, s, v in missing} - forbidden)
        # windows of one group never overlap
        for group, ds in covered.items():
            self.assertEqual(len(ds), len(set(ds)))


class MockVendorTask(WindowedFetchTask):
    # (scope, version) -> set of dates the fake vendor has data for
    available = {}
    # set by tests to a per-test temporary directory
    root = None

    @classmethod
    def target_for_date(cls, d, scope, version):
        return luigi.LocalTarget(os.path.join(cls.root, "MockVendorTask", str(scope), str(version), d.isoformat()))

    def receipt_target(self):
        return luigi.LocalTarget(os.path.join(self.root, "MockVendorTask", str(self.scope), str(self.version), "receipts", "%s_%s" % (self.start, self.end)))

    def fetch(self, start, end):
        have = self.available.get((self.scope, self.version), set())
        return [d for d in self.window_dates() if d in have]


class VendorTaskTestCase(LuigiTestCase):
    def setUp(self):
        super().setUp()
        MockVendorTask.available = {}
        MockVendorTask.root = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, MockVendorTask.root, True)


class WindowedFetchTaskTest(VendorTaskTestCase):
    def test_partial_delivery_only_completes_delivered_dates(self):
        MockVendorTask.available = {("s1", "v1"): {D(2026, 1, 2), D(2026, 1, 4)}}
        task = MockVendorTask(scope="s1", version="v1", start=D(2026, 1, 1), end=D(2026, 1, 5))
        task.run()
        self.assertTrue(task.complete())  # receipt written, no wholesale re-request
        self.assertEqual(task.delivered_dates(), [D(2026, 1, 2), D(2026, 1, 4)])
        for d in dates(D(2026, 1, 1), 5):
            self.assertEqual(MockVendorTask.date_complete(d, "s1", "v1"), d in {D(2026, 1, 2), D(2026, 1, 4)})

    def test_mapping_payloads_are_stored_per_date(self):
        class MappingVendorTask(MockVendorTask):
            def fetch(self, start, end):
                return {D(2026, 1, 1): {"rows": 3}}

        task = MappingVendorTask(scope="s1", version="v1", start=D(2026, 1, 1), end=D(2026, 1, 2))
        task.run()
        with MappingVendorTask.target_for_date(D(2026, 1, 1), "s1", "v1").open("r") as f:
            self.assertEqual(json.load(f), {"rows": 3})
        self.assertFalse(MappingVendorTask.date_complete(D(2026, 1, 2), "s1", "v1"))

    def test_delivery_outside_window_is_rejected(self):
        class StrayVendorTask(MockVendorTask):
            def fetch(self, start, end):
                return [D(2026, 2, 1)]

        task = StrayVendorTask(scope="s1", version="v1", start=D(2026, 1, 1), end=D(2026, 1, 2))
        self.assertRaises(ValueError, task.run)

    def test_delivered_dates_none_before_running(self):
        task = MockVendorTask(scope="s1", version="v1", start=D(2026, 1, 1), end=D(2026, 1, 2))
        self.assertIsNone(task.delivered_dates())


class BatchedRangeDailyTest(VendorTaskTestCase):
    def make_range(self, **kwargs):
        kwargs.setdefault("of", MockVendorTask)
        kwargs.setdefault("scopes", ["s1"])
        kwargs.setdefault("versions", ["v1"])
        kwargs.setdefault("start", D(2026, 1, 1))
        kwargs.setdefault("stop", D(2026, 1, 11))
        kwargs.setdefault("days_back", 10000)
        kwargs.setdefault("max_window_days", 4)
        return BatchedRangeDaily(**kwargs)

    def test_requires_batches_adjacent_dates_newest_first(self):
        batches = self.make_range().batch_plan()
        self.assertEqual(
            batches,
            [
                Batch("s1", "v1", D(2026, 1, 9), D(2026, 1, 10)),
                Batch("s1", "v1", D(2026, 1, 5), D(2026, 1, 8)),
                Batch("s1", "v1", D(2026, 1, 1), D(2026, 1, 4)),
            ],
        )
        reqs = self.make_range().requires()
        self.assertEqual([(t.scope, t.version, t.start, t.end) for t in reqs], [(b.scope, b.version, b.start, b.end) for b in batches])

    def test_already_complete_dates_are_not_requested(self):
        for d in dates(D(2026, 1, 3), 3):
            MockVendorTask.target_for_date(d, "s1", "v1").open("w").close()
        batches = self.make_range().batch_plan()
        self.assertEqual(coverage(batches)[("s1", "v1")], dates(D(2026, 1, 1), 2) + dates(D(2026, 1, 6), 5))

    def test_forbidden_dates_split_windows(self):
        rng = self.make_range(forbidden_dates={"s1": ["2026-01-05"]})
        batches = rng.batch_plan()
        self.assertEqual(
            [(b.start, b.end) for b in batches],
            [(D(2026, 1, 10), D(2026, 1, 10)), (D(2026, 1, 6), D(2026, 1, 9)), (D(2026, 1, 1), D(2026, 1, 4))],
        )
        for batch in batches:
            self.assertFalse(batch.covers(D(2026, 1, 5)))

    def test_groups_are_planned_independently(self):
        rng = self.make_range(scopes=["s1", "s2"], versions=["v1", "v2"], max_window_days=30)
        batches = rng.batch_plan()
        self.assertEqual(len(batches), 4)
        self.assertEqual([(b.scope, b.version) for b in batches], [("s1", "v1"), ("s1", "v2"), ("s2", "v1"), ("s2", "v2")])

    def test_task_limit_keeps_newest_windows(self):
        rng = self.make_range(task_limit=2)
        self.assertEqual(len(rng.requires()), 2)
        self.assertEqual(rng.requires()[0].start, D(2026, 1, 9))

    def test_nothing_missing_means_no_requests(self):
        for d in dates(D(2026, 1, 1), 10):
            MockVendorTask.target_for_date(d, "s1", "v1").open("w").close()
        self.assertEqual(self.make_range().batch_plan(), [])
        self.assertEqual(self.make_range().requires(), [])

    def test_invalid_configuration_rejected(self):
        self.assertRaises(ParameterException, self.make_range(scopes=[]).batch_plan)
        self.assertRaises(ParameterException, self.make_range(versions=[]).batch_plan)
        self.assertRaises(ParameterException, self.make_range(start=None, stop=None).batch_plan)
        self.assertRaises(ParameterException, self.make_range(of=luigi.Task).batch_plan)

    def test_partial_delivery_replans_only_undelivered_dates(self):
        # vendor delivers everything except Jan 3 and Jan 7 in the first round
        MockVendorTask.available = {("s1", "v1"): set(dates(D(2026, 1, 1), 10)) - {D(2026, 1, 3), D(2026, 1, 7)}}
        first_round = self.make_range(max_window_days=10)
        self.assertEqual(first_round.batch_plan(), [Batch("s1", "v1", D(2026, 1, 1), D(2026, 1, 10))])
        for task in first_round.requires():
            task.run()

        # a fresh invocation would not reuse the cached plan
        luigi.task_register.Register.clear_instance_cache()
        second_round = self.make_range(max_window_days=10)
        self.assertEqual(second_round.batch_plan(), [Batch("s1", "v1", D(2026, 1, 3), D(2026, 1, 7))])
        # vendor now has the remaining dates too
        MockVendorTask.available = {("s1", "v1"): set(dates(D(2026, 1, 1), 10))}
        for task in second_round.requires():
            task.run()

        # final consistency: every date in the range is complete, nothing else was requested
        for d in dates(D(2026, 1, 1), 10):
            self.assertTrue(MockVendorTask.date_complete(d, "s1", "v1"), d)
        luigi.task_register.Register.clear_instance_cache()
        self.assertEqual(self.make_range(max_window_days=10).batch_plan(), [])

    def test_final_coverage_and_states_are_consistent(self):
        MockVendorTask.available = {("s1", "v1"): set(dates(D(2026, 1, 1), 10))}
        forbidden = {"s1": ["2026-01-05"]}
        rng = self.make_range(max_window_days=3, forbidden_dates=forbidden)
        for task in rng.requires():
            task.run()
        requested = set(coverage(rng.batch_plan())[("s1", "v1")])
        complete = {d for d in dates(D(2026, 1, 1), 10) if MockVendorTask.date_complete(d, "s1", "v1")}
        self.assertEqual(requested, complete)
        self.assertEqual(complete, set(dates(D(2026, 1, 1), 10)) - {D(2026, 1, 5)})


if __name__ == "__main__":
    unittest.main()
