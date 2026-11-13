# -*- coding: utf-8 -*-
#
# Copyright 2026 Spotify AB
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

import json
import subprocess
import sys
import tempfile

from helpers import unittest

from luigi.tools import retention


def make_manifest(targets, reports, budget=None, audit_date="2026-11-13"):
    data = {"audit_date": audit_date, "targets": targets, "reports": reports}
    if budget is not None:
        data["budget"] = budget
    return retention.parse_manifest(data)


class ParseManifestTest(unittest.TestCase):
    def test_unknown_dependency_rejected(self):
        with self.assertRaises(ValueError):
            make_manifest(
                {"a": {"size": 1, "depends_on": [["ghost"]]}},
                ["a"],
            )

    def test_unknown_report_rejected(self):
        with self.assertRaises(ValueError):
            make_manifest({"a": {"size": 1}}, ["missing"])

    def test_negative_size_rejected(self):
        with self.assertRaises(ValueError):
            make_manifest({"a": {"size": -1}}, ["a"])

    def test_bad_date_rejected(self):
        with self.assertRaises(ValueError):
            make_manifest({"a": {"size": 1, "expires": "not-a-date"}}, ["a"])


class RebuildPlansTest(unittest.TestCase):
    def test_source_is_its_own_plan(self):
        manifest = make_manifest({"raw": {"size": 10}}, ["raw"])
        self.assertEqual(retention.rebuild_plans(manifest, "raw"), [frozenset({"raw"})])

    def test_expired_target_has_no_plan(self):
        manifest = make_manifest(
            {
                "raw": {"size": 10, "expires": "2026-01-01"},
                "report": {"size": 5, "depends_on": [["raw"]]},
            },
            ["report"],
        )
        self.assertEqual(retention.rebuild_plans(manifest, "report"), [])

    def test_expiry_on_audit_date_is_still_valid(self):
        manifest = make_manifest(
            {"raw": {"size": 10, "expires": "2026-11-13"}},
            ["raw"],
        )
        self.assertEqual(retention.rebuild_plans(manifest, "raw"), [frozenset({"raw"})])

    def test_cycle_without_source_is_not_evidence(self):
        manifest = make_manifest(
            {
                "a": {"size": 3, "depends_on": [["b"]]},
                "b": {"size": 3, "depends_on": [["a"]]},
                "report": {"size": 1, "depends_on": [["a"]]},
            },
            ["report"],
        )
        self.assertEqual(retention.rebuild_plans(manifest, "report"), [])

    def test_cycle_alternative_falls_back_to_grounded_path(self):
        manifest = make_manifest(
            {
                "raw": {"size": 7},
                "a": {"size": 3, "depends_on": [["b"], ["raw"]]},
                "b": {"size": 3, "depends_on": [["a"]]},
            },
            ["a"],
        )
        self.assertEqual(retention.rebuild_plans(manifest, "a"), [frozenset({"a", "raw"})])


class PlanRetentionTest(unittest.TestCase):
    def test_picks_cheaper_of_two_equivalent_paths(self):
        manifest = make_manifest(
            {
                "raw_a": {"size": 100},
                "raw_b": {"size": 10},
                "report": {"size": 1, "depends_on": [["raw_a"], ["raw_b"]]},
            },
            ["report"],
        )
        plan = retention.plan_retention(manifest)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.total_size, 11)
        self.assertEqual({t.name for t in plan.kept}, {"raw_b", "report"})

    def test_shared_original_counted_once(self):
        manifest = make_manifest(
            {
                "raw": {"size": 50},
                "mid": {"size": 20, "depends_on": [["raw"]]},
                "report_1": {"size": 5, "depends_on": [["mid"]]},
                "report_2": {"size": 5, "depends_on": [["raw"]]},
            },
            ["report_1", "report_2"],
        )
        plan = retention.plan_retention(manifest)
        self.assertTrue(plan.feasible)
        # raw (50) + mid (20) + both reports (5 + 5); raw is only paid once.
        self.assertEqual(plan.total_size, 80)
        self.assertEqual({t.name for t in plan.kept}, {"raw", "mid", "report_1", "report_2"})

    def test_sharing_changes_path_choice(self):
        # report_2 forces "raw" to be kept anyway, so report_1's longer
        # looking path through raw becomes the cheaper option overall.
        manifest = make_manifest(
            {
                "raw": {"size": 40},
                "shortcut": {"size": 30},
                "report_1": {"size": 1, "depends_on": [["raw"], ["shortcut"]]},
                "report_2": {"size": 1, "depends_on": [["raw"]]},
            },
            ["report_1", "report_2"],
        )
        plan = retention.plan_retention(manifest)
        self.assertTrue(plan.feasible)
        self.assertEqual(plan.total_size, 42)
        self.assertNotIn("shortcut", {t.name for t in plan.kept})

    def test_budget_too_small_is_infeasible_without_deletion_advice(self):
        manifest = make_manifest(
            {
                "raw": {"size": 100},
                "report": {"size": 10, "depends_on": [["raw"]]},
            },
            ["report"],
            budget=50,
        )
        plan = retention.plan_retention(manifest)
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.minimum_size, 110)
        text = retention.render(plan)
        self.assertIn("INFEASIBLE", text)
        self.assertIn("no deletion suggestions", text)
        self.assertNotIn("Keep", text)

    def test_uncoverable_report_is_infeasible(self):
        manifest = make_manifest(
            {
                "a": {"size": 3, "depends_on": [["b"]]},
                "b": {"size": 3, "depends_on": [["a"]]},
                "report": {"size": 1, "depends_on": [["a"]]},
            },
            ["report"],
        )
        plan = retention.plan_retention(manifest)
        self.assertFalse(plan.feasible)
        self.assertEqual(plan.uncoverable, ["report"])
        self.assertIn("report", retention.render(plan))

    def test_budget_exactly_equal_is_feasible(self):
        manifest = make_manifest(
            {"raw": {"size": 10}, "report": {"size": 5, "depends_on": [["raw"]]}},
            ["report"],
            budget=15,
        )
        self.assertTrue(retention.plan_retention(manifest).feasible)

    def test_render_explains_rebuild_per_report(self):
        manifest = make_manifest(
            {
                "raw": {"size": 10},
                "mid": {"size": 4, "depends_on": [["raw"]]},
                "report": {"size": 1, "depends_on": [["mid"]]},
            },
            ["report"],
        )
        text = retention.render(retention.plan_retention(manifest))
        self.assertIn("view-only", text)
        self.assertIn("keep original input raw", text)
        self.assertIn("recompute mid from raw", text)
        self.assertIn("recompute report from mid", text)

    def test_to_dict_marks_view_only(self):
        manifest = make_manifest({"raw": {"size": 10}}, ["raw"], budget=10)
        data = retention.to_dict(retention.plan_retention(manifest))
        self.assertTrue(data["view_only"])
        self.assertTrue(data["feasible"])
        self.assertEqual(
            data["keep"],
            [
                {"name": "raw", "size": 10, "expires": None},
            ],
        )


class CliTest(unittest.TestCase):
    MANIFEST = {
        "audit_date": "2026-11-13",
        "targets": {
            "raw_events": {"size": 120, "expires": "2027-03-31"},
            "archive_extract": {"size": 200},
            "daily_rollup": {"size": 40, "depends_on": [["raw_events"]]},
            "q_report": {"size": 10, "depends_on": [["daily_rollup"], ["archive_extract"]]},
        },
        "reports": ["q_report"],
    }

    def _write_manifest(self, data):
        handle = tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False, encoding="utf-8")
        json.dump(data, handle)
        handle.close()
        return handle.name

    def test_cli_feasible(self):
        path = self._write_manifest(self.MANIFEST)
        proc = subprocess.run([sys.executable, "-m", "luigi.tools.retention", path, "--budget", "200"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("Keep 3 file(s), 170 B", proc.stdout)
        self.assertIn("recompute q_report from daily_rollup", proc.stdout)

    def test_cli_infeasible_exit_code_and_no_deletion_advice(self):
        path = self._write_manifest(self.MANIFEST)
        proc = subprocess.run([sys.executable, "-m", "luigi.tools.retention", path, "--budget", "10"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("INFEASIBLE", proc.stdout)
        self.assertIn("no deletion suggestions", proc.stdout)

    def test_cli_json_output(self):
        path = self._write_manifest(self.MANIFEST)
        proc = subprocess.run([sys.executable, "-m", "luigi.tools.retention", path, "--json"], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        data = json.loads(proc.stdout)
        self.assertTrue(data["feasible"])
        self.assertEqual(data["total_size"], 170)
        self.assertIn("q_report", data["reports"])


if __name__ == "__main__":
    unittest.main()
