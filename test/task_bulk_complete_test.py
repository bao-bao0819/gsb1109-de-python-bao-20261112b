# -*- coding: utf-8 -*-
#
# Copyright 2012-2016 Spotify AB
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

from helpers import unittest

from luigi import Parameter, Task
from luigi.task import (
    BulkCompleteEvidence,
    BulkCompleteResult,
    BulkCompleteStatus,
    MixinBulkCompleteBySource,
    MixinNaiveBulkComplete,
)

COMPLETE_TASKS = ["A", "B", "C"]


class MockTask(MixinNaiveBulkComplete, Task):
    param_a = Parameter()
    param_b = Parameter(default="Not Mandatory")

    def complete(self):
        return self.param_a in COMPLETE_TASKS


class MixinNaiveBulkCompleteTest(unittest.TestCase):
    """
    Test that the MixinNaiveBulkComplete can handle
    input as
     - iterable of parameters (for single param tasks)
     - iterable of parameter tuples (for multi param tasks)
     - iterable of parameter dicts (for multi param tasks)
    """

    def test_single_arg_list(self):
        single_arg_list = ["A", "B", "x"]
        expected_single_arg_list = {p for p in single_arg_list if p in COMPLETE_TASKS}
        self.assertEqual(expected_single_arg_list, set(MockTask.bulk_complete(single_arg_list)))

    def test_multiple_arg_tuple(self):
        multiple_arg_tuple = (("A", "1"), ("B", "2"), ("X", "3"), ("C", "2"))
        expected_multiple_arg_tuple = {p for p in multiple_arg_tuple if p[0] in COMPLETE_TASKS}
        self.assertEqual(expected_multiple_arg_tuple, set(MockTask.bulk_complete(multiple_arg_tuple)))

    def test_multiple_arg_dict(self):
        multiple_arg_dict = ({"param_a": "X", "param_b": "1"}, {"param_a": "C", "param_b": "1"})
        expected_multiple_arg_dict = [p for p in multiple_arg_dict if p["param_a"] in COMPLETE_TASKS]
        self.assertEqual(expected_multiple_arg_dict, MockTask.bulk_complete(multiple_arg_dict))


class _PantryTask(Task):
    """A task whose partitions live in several independently readable sources."""

    partition = Parameter()
    source = Parameter()

    # Anything in here is considered present.
    present = set()
    # Raising from these sources simulates a temporarily unreadable store.
    unavailable = set()
    # Sources whose version advances invalidate previously reaped evidence.
    versions = {}
    reads = []

    @classmethod
    def bulk_complete_source(cls, parameter_tuple):
        return parameter_tuple[1]

    @classmethod
    def bulk_complete_source_version(cls, source):
        return cls.versions.get(source)

    @classmethod
    def bulk_complete_source_items(cls, source, parameter_tuples):
        cls.reads.append((source, tuple(parameter_tuples)))
        if source in cls.unavailable:
            raise IOError("source %s is temporarily unreadable" % source)
        return {p: (p[0] in cls.present) for p in parameter_tuples}


class BulkCompleteResultTest(unittest.TestCase):
    def setUp(self):
        _PantryTask.present = {"p1", "p2", "p4"}
        _PantryTask.unavailable = set()
        _PantryTask.versions = {}
        _PantryTask.reads = []

    def _check(self, *pairs, **kwargs):
        return _PantryTask.bulk_complete_result(list(pairs), **kwargs)

    def test_tristate_outcome_keeps_per_item_results(self):
        result = self._check(("p1", "s1"), ("p2", "s1"), ("p3", "s1"))
        self.assertEqual(result.status(("p1", "s1")), BulkCompleteStatus.COMPLETE)
        self.assertEqual(result.status(("p2", "s1")), BulkCompleteStatus.COMPLETE)
        self.assertEqual(result.status(("p3", "s1")), BulkCompleteStatus.MISSING)
        self.assertEqual(result.complete_items(), [("p1", "s1"), ("p2", "s1")])
        self.assertEqual(result.missing_items(), [("p3", "s1")])
        self.assertEqual(result.unknown_items(), [])

    def test_unreadable_source_does_not_block_other_sources(self):
        _PantryTask.unavailable = {"broken"}
        result = self._check(("p1", "healthy"), ("p3", "healthy"), ("p2", "broken"), ("p4", "broken"))

        self.assertEqual(result.status(("p1", "healthy")), BulkCompleteStatus.COMPLETE)
        self.assertEqual(result.status(("p3", "healthy")), BulkCompleteStatus.MISSING)
        self.assertEqual(result.status(("p2", "broken")), BulkCompleteStatus.UNKNOWN)
        self.assertEqual(result.status(("p4", "broken")), BulkCompleteStatus.UNKNOWN)
        self.assertEqual(set(result.errors), {"broken"})
        # The broken source's results are not turned into missing ones, so they
        # can never be claimed for work.
        self.assertNotIn(("p2", "broken"), result.missing_items())

    def test_complete_evidence_is_reused_only_within_its_source_version(self):
        _PantryTask.versions = {"s1": "v1"}
        evidence = BulkCompleteEvidence()
        self._check(("p1", "s1"), evidence=evidence)
        _PantryTask.reads = []

        self._check(("p1", "s1"), ("p3", "s1"), evidence=evidence)
        self.assertEqual(_PantryTask.reads, [("s1", (("p3", "s1"),))])

        _PantryTask.versions = {"s1": "v2"}
        self._check(("p1", "s1"), evidence=evidence)
        self.assertEqual(_PantryTask.reads[-1], ("s1", (("p1", "s1"),)))

    def test_no_version_means_no_cross_check_reuse(self):
        evidence = BulkCompleteEvidence()
        self._check(("p1", "s1"), evidence=evidence)
        _PantryTask.reads = []

        self._check(("p1", "s1"), evidence=evidence)
        self.assertEqual(_PantryTask.reads, [("s1", (("p1", "s1"),))])

    def test_missing_and_unknown_are_never_recorded_as_evidence(self):
        _PantryTask.versions = {"s1": "v1", "s2": "v1"}
        _PantryTask.unavailable = {"s2"}
        evidence = BulkCompleteEvidence()
        self._check(("p3", "s1"), ("p1", "s2"), evidence=evidence)

        self.assertEqual(evidence.get(_PantryTask.get_task_family(), "s1", "v1"), frozenset())
        self.assertEqual(evidence.get(_PantryTask.get_task_family(), "s2", "v1"), frozenset())

    def test_failed_source_contributes_no_evidence(self):
        _PantryTask.versions = {"s1": "v1", "s2": "v1"}
        _PantryTask.unavailable = {"s2"}
        evidence = BulkCompleteEvidence()
        result = self._check(("p1", "s1"), ("p2", "s2"), evidence=evidence)

        self.assertEqual(result.status(("p1", "s1")), BulkCompleteStatus.COMPLETE)
        self.assertEqual(result.status(("p2", "s2")), BulkCompleteStatus.UNKNOWN)
        self.assertEqual(evidence.get(_PantryTask.get_task_family(), "s2", "v1"), frozenset())

    def test_per_source_version_is_read_once_and_rechecked_after_bump(self):
        _PantryTask.versions = {"s1": "v1", "s2": "v2"}
        evidence = BulkCompleteEvidence()
        self._check(("p1", "s1"), ("p2", "s2"), evidence=evidence)
        self.assertEqual(sorted(_PantryTask.reads), [("s1", (("p1", "s1"),)), ("s2", (("p2", "s2"),))])

        _PantryTask.reads = []
        self._check(("p1", "s1"), ("p2", "s2"), evidence=evidence)
        self.assertEqual(_PantryTask.reads, [])

        # Bumping only s1's version invalidates only s1's evidence.
        _PantryTask.versions["s1"] = "v2"
        self._check(("p1", "s1"), ("p2", "s2"), evidence=evidence)
        self.assertEqual(_PantryTask.reads, [("s1", (("p1", "s1"),))])

    def test_evidence_is_scoped_to_task_family(self):
        _PantryTask.versions = {"s1": "v1"}

        class SiblingTask(_PantryTask):
            task_namespace = "sibling"

        evidence = BulkCompleteEvidence()
        _PantryTask.bulk_complete_result([("p1", "s1")], evidence=evidence)
        _PantryTask.reads = []

        result = SiblingTask.bulk_complete_result([("p1", "s1")], evidence=evidence)
        self.assertEqual(result.status(("p1", "s1")), BulkCompleteStatus.COMPLETE)
        # The sibling family is not served from the original family's cache.
        self.assertEqual(_PantryTask.reads, [("s1", (("p1", "s1"),))])

    def test_evidence_bounded_by_max_entries(self):
        evidence = BulkCompleteEvidence(max_entries=2)
        evidence.record("family", "source", "v1", ["p1"])
        evidence.record("family", "source", "v2", ["p1"])
        evidence.record("family", "source", "v3", ["p1"])

        self.assertEqual(evidence.get("family", "source", "v1"), frozenset())
        self.assertEqual(evidence.get("family", "source", "v2"), frozenset({"p1"}))
        self.assertEqual(evidence.get("family", "source", "v3"), frozenset({"p1"}))

    def test_mixin_reports_only_confirmed_complete_items(self):
        class PantryMixinTask(MixinBulkCompleteBySource, Task):
            partition = Parameter()
            source = Parameter()
            present = _PantryTask.present
            unavailable = _PantryTask.unavailable
            reads = _PantryTask.reads
            versions = _PantryTask.versions
            bulk_complete_source = _PantryTask.bulk_complete_source
            bulk_complete_source_items = _PantryTask.bulk_complete_source_items

        _PantryTask.unavailable = {"broken"}
        complete = PantryMixinTask.bulk_complete([("p2", "healthy"), ("p3", "healthy"), ("p1", "broken")])
        self.assertEqual(complete, [("p2", "healthy")])

    def test_legacy_iterable_result_still_understood(self):
        class LegacyTask(Task):
            partition = Parameter()

            @classmethod
            def bulk_complete(cls, parameter_tuples):
                return [p for p in parameter_tuples if p in ("p1", "p2")]

        result = LegacyTask.bulk_complete_result(["p1", "p3"])
        self.assertEqual(result.status("p1"), BulkCompleteStatus.COMPLETE)
        self.assertEqual(result.status("p3"), BulkCompleteStatus.MISSING)

    def test_mapping_result_carries_statuses(self):
        class MappingTask(Task):
            partition = Parameter()

            @classmethod
            def bulk_complete(cls, parameter_tuples):
                return {p: (p.startswith("p") if p != "broken" else BulkCompleteStatus.UNKNOWN) for p in parameter_tuples}

        result = MappingTask.bulk_complete_result(["p1", "x1", "broken"])
        self.assertEqual(result.status("p1"), BulkCompleteStatus.COMPLETE)
        self.assertEqual(result.status("x1"), BulkCompleteStatus.MISSING)
        self.assertEqual(result.status("broken"), BulkCompleteStatus.UNKNOWN)

    def test_not_implemented_propagates(self):
        class PlainTask(Task):
            partition = Parameter()

        with self.assertRaises(NotImplementedError):
            PlainTask.bulk_complete_result(["p1"])
