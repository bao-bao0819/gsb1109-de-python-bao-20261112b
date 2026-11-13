# Copyright (c) 2026 Spotify AB
#
# Licensed under the Apache License, Version 2.0 (the "License"); you may not
# use this file except in compliance with the License. You may obtain a copy of
# the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
# WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
# License for the specific language governing permissions and limitations under
# the License.

import datetime
import json
import os
import tempfile
import unittest

import luigi
from luigi.contrib.revisions import (
    LocalRevisionManifest,
    RevisedDailyDetail,
    RevisionBackfill,
    RevisionBackfillAndReports,
    RevisionEntry,
    RevisionStateStore,
    RollingWeekReport,
    load_manifest,
    rolling_window_ends_for,
)

SOURCE = "sales"
D1 = datetime.date(2026, 1, 10)
D2 = datetime.date(2026, 1, 11)
HOLIDAY = datetime.date(2026, 1, 12)


class FakeDetail(RevisedDailyDetail):
    fetch_runs: dict[str, int] = {}

    def fetch_revised_detail(self, temp_path):
        FakeDetail.fetch_runs[self.date.isoformat()] = FakeDetail.fetch_runs.get(self.date.isoformat(), 0) + 1
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(
                {
                    "source": self.source_name,
                    "date": self.date.isoformat(),
                    "version": self.revision_version,
                    "empty": False,
                    "records": [{"value": self.revision_version}],
                },
                handle,
            )


class FakeManifest(LocalRevisionManifest):
    manifest_dir = luigi.Parameter()


class FakeBackfill(RevisionBackfill):
    manifest_dir = luigi.Parameter()

    def get_manifest_task(self):
        return FakeManifest(manifest_id=self.manifest_id, manifest_dir=self.manifest_dir)

    def get_detail_task_class(self):
        return FakeDetail


class FakeReport(RollingWeekReport):
    run_runs: dict[str, int] = {}

    def get_detail_task_class(self):
        return FakeDetail

    def run(self):
        FakeReport.run_runs[str(self.week_end)] = FakeReport.run_runs.get(str(self.week_end), 0) + 1
        super().run()


class FakeOrchestration(RevisionBackfillAndReports):
    manifest_dir = luigi.Parameter()

    def get_backfill_task(self):
        return FakeBackfill(
            manifest_id=self.manifest_id,
            source_name=self.source_name,
            state_dir=self.state_dir,
            output_dir=self.output_dir,
            detail_output_dir=self.detail_output_dir,
            lookback=self.lookback,
            manifest_dir=self.manifest_dir,
        )

    def get_report_task_class(self):
        return FakeReport


def write_manifest(root, manifest_id, revisions, issued_on=datetime.date(2026, 2, 1)):
    manifest_dir = os.path.join(root, "manifests")
    os.makedirs(manifest_dir, exist_ok=True)
    path = os.path.join(manifest_dir, f"{manifest_id}.json")
    payload = {
        "manifest_id": manifest_id,
        "issued_on": issued_on.isoformat(),
        "revisions": [
            {
                "date": entry.date.isoformat(),
                "source": entry.source,
                "version": entry.version,
                "empty": entry.empty,
            }
            for entry in revisions
        ],
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle)
    return path


class ManifestParsingTest(unittest.TestCase):
    def test_load_manifest_parses_entries(self):
        with tempfile.TemporaryDirectory() as root:
            path = write_manifest(
                root,
                "m1",
                [RevisionEntry(D1, SOURCE, 1), RevisionEntry(D2, SOURCE, 2, empty=True)],
            )
            manifest = load_manifest(path)
            self.assertEqual(manifest.manifest_id, "m1")
            self.assertEqual(manifest.issued_on, datetime.date(2026, 2, 1))
            self.assertEqual(len(manifest.entries), 2)
            self.assertTrue(manifest.entries[1].empty)
            self.assertEqual(manifest.entries_for(SOURCE), manifest.entries)
            self.assertEqual(manifest.entries_for("other"), ())

    def test_rejects_bad_version(self):
        with tempfile.TemporaryDirectory() as root:
            manifest_dir = os.path.join(root, "manifests")
            os.makedirs(manifest_dir)
            path = os.path.join(manifest_dir, "bad.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(
                    {
                        "manifest_id": "bad",
                        "issued_on": "2026-02-01",
                        "revisions": [{"date": "2026-01-10", "source": SOURCE, "version": 0}],
                    },
                    handle,
                )
            with self.assertRaises(ValueError):
                load_manifest(path)

    def test_rolling_window_ends(self):
        ends = rolling_window_ends_for(D1, lookback=7)
        self.assertEqual(ends[0], D1)
        self.assertEqual(ends[-1], D1 + datetime.timedelta(days=6))
        self.assertEqual(len(ends), 7)


class StateStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = RevisionStateStore(os.path.join(self.tmp.name, "state"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_three_states_are_distinct(self):
        entry = RevisionEntry(D1, SOURCE, 1)
        # No manifest ever received: no state, needs revision, not known empty
        self.assertIsNone(self.store.get_state(SOURCE, D1))
        self.assertTrue(self.store.needs_revision(entry))
        self.assertFalse(self.store.is_known_empty(SOURCE, D1))

        self.store.mark_applied(entry, manifest_id="m1")
        # Same version resubmitted: no revision needed
        self.assertFalse(self.store.needs_revision(RevisionEntry(D1, SOURCE, 1)))
        self.assertFalse(self.store.is_known_empty(SOURCE, D1))

        # Formally corrected to empty is a distinct state from "not received"
        empty_entry = RevisionEntry(D1, SOURCE, 2, empty=True)
        self.assertTrue(self.store.needs_revision(empty_entry))
        self.store.mark_applied(empty_entry, manifest_id="m2")
        self.assertTrue(self.store.is_known_empty(SOURCE, D1))
        state = self.store.get_state(SOURCE, D1)
        self.assertTrue(state["empty"])
        self.assertEqual(state["version"], 2)
        # An untouched day still has no state at all
        self.assertIsNone(self.store.get_state(SOURCE, D2))


class BackfillPipelineTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        FakeDetail.fetch_runs.clear()
        FakeReport.run_runs.clear()

    def tearDown(self):
        self.tmp.cleanup()

    def _dirs(self):
        return {
            "state_dir": os.path.join(self.root, "state"),
            "output_dir": os.path.join(self.root, "plans"),
            "detail_output_dir": os.path.join(self.root, "details"),
            "report_dir": os.path.join(self.root, "reports"),
            "manifest_dir": os.path.join(self.root, "manifests"),
        }

    def _backfill(self, manifest_id):
        dirs = self._dirs()
        return FakeBackfill(
            manifest_id=manifest_id,
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            output_dir=dirs["output_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            manifest_dir=dirs["manifest_dir"],
        )

    def test_only_changed_details_are_refetched(self):
        dirs = self._dirs()
        write_manifest(self.root, "m1", [RevisionEntry(D1, SOURCE, 1), RevisionEntry(D2, SOURCE, 1)])

        first = self._backfill("m1")
        self.assertTrue(luigi.build([first], local_scheduler=True, log_level="ERROR"))

        with first.output().open("r") as handle:
            plan = json.load(handle)
        self.assertEqual(len(plan["revised_details"]), 2)
        self.assertEqual(plan["skipped_unchanged"], [])
        self.assertEqual(FakeDetail.fetch_runs, {D1.isoformat(): 1, D2.isoformat(): 1})
        # Seven rolling windows per revised day, two adjacent days -> 8 windows
        self.assertEqual(len(plan["impacted_reports"]), 8)

        # Second manifest: D1 bumped to v2, D2 resubmitted unchanged
        write_manifest(self.root, "m2", [RevisionEntry(D1, SOURCE, 2), RevisionEntry(D2, SOURCE, 1)])
        second = self._backfill("m2")
        self.assertTrue(luigi.build([second], local_scheduler=True, log_level="ERROR"))

        with second.output().open("r") as handle:
            plan2 = json.load(handle)
        self.assertEqual([item["date"] for item in plan2["revised_details"]], [D1.isoformat()])
        self.assertEqual([item["date"] for item in plan2["skipped_unchanged"]], [D2.isoformat()])
        # Unchanged detail was not pulled again; old version output stays in place
        self.assertEqual(FakeDetail.fetch_runs, {D1.isoformat(): 2, D2.isoformat(): 1})
        d2_versions = os.listdir(os.path.join(dirs["detail_output_dir"], "sources", SOURCE, D2.isoformat()))
        self.assertEqual(d2_versions, ["v1"])
        d1_versions = sorted(os.listdir(os.path.join(dirs["detail_output_dir"], "sources", SOURCE, D1.isoformat())))
        self.assertEqual(d1_versions, ["v1", "v2"])

    def test_resubmitting_same_manifest_runs_nothing(self):
        write_manifest(self.root, "m1", [RevisionEntry(D1, SOURCE, 1)])
        backfill = self._backfill("m1")
        self.assertTrue(luigi.build([backfill], local_scheduler=True, log_level="ERROR"))
        self.assertEqual(FakeDetail.fetch_runs[D1.isoformat()], 1)

        # Same manifest again: backfill already complete, no refetch
        self.assertTrue(luigi.build([self._backfill("m1")], local_scheduler=True, log_level="ERROR"))
        self.assertEqual(FakeDetail.fetch_runs[D1.isoformat()], 1)

    def test_configurable_lookback_drives_impacted_windows(self):
        dirs = self._dirs()
        write_manifest(self.root, "m3", [RevisionEntry(D1, SOURCE, 1)])
        backfill = FakeBackfill(
            manifest_id="m3",
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            output_dir=dirs["output_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            manifest_dir=dirs["manifest_dir"],
            lookback=3,
        )
        self.assertTrue(luigi.build([backfill], local_scheduler=True, log_level="ERROR"))
        with backfill.output().open("r") as handle:
            plan = json.load(handle)
        ends = [item["week_end"] for item in plan["impacted_reports"]]
        self.assertEqual(
            ends,
            [(D1 + datetime.timedelta(days=offset)).isoformat() for offset in range(3)],
        )

    def test_empty_correction_differs_from_unsampled_holiday(self):
        dirs = self._dirs()
        write_manifest(self.root, "m-empty", [RevisionEntry(HOLIDAY, SOURCE, 1, empty=True)])
        self.assertTrue(luigi.build([self._backfill("m-empty")], local_scheduler=True, log_level="ERROR"))

        store = RevisionStateStore(dirs["state_dir"])
        self.assertTrue(store.is_known_empty(SOURCE, HOLIDAY))
        empty_detail = os.path.join(dirs["detail_output_dir"], "sources", SOURCE, HOLIDAY.isoformat(), "v1", "detail.json")
        with open(empty_detail, encoding="utf-8") as handle:
            self.assertTrue(json.load(handle)["empty"])

        untouched = HOLIDAY - datetime.timedelta(days=2)
        report = FakeReport(
            week_end=HOLIDAY,
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            report_dir=dirs["report_dir"],
            holidays=[HOLIDAY.isoformat(), untouched.isoformat()],
        )
        self.assertTrue(luigi.build([report], local_scheduler=True, log_level="ERROR"))
        with report.output().open("r") as handle:
            payload = json.load(handle)

        statuses = {item["date"]: item for item in payload["details"]}
        # Formally emptied day appears in lineage as an empty correction, versioned
        self.assertEqual(statuses[HOLIDAY.isoformat()]["status"], "empty_correction")
        self.assertEqual(statuses[HOLIDAY.isoformat()]["version"], 1)
        # A day never covered by any manifest is not fetched; labelled by context
        self.assertEqual(statuses[untouched.isoformat()]["status"], "holiday")
        plain_report = FakeReport(
            week_end=HOLIDAY,
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            report_dir=os.path.join(self.root, "reports-plain"),
        )
        self.assertTrue(luigi.build([plain_report], local_scheduler=True, log_level="ERROR"))
        with plain_report.output().open("r") as handle:
            plain_payload = json.load(handle)
        plain_statuses = {item["date"]: item for item in plain_payload["details"]}
        self.assertEqual(plain_statuses[untouched.isoformat()]["status"], "not_sampled")
        # Empty day contributes no records
        self.assertEqual(payload["metrics"]["record_count"], 0)

    def test_report_lineage_versions_and_targeted_recompute(self):
        dirs = self._dirs()
        write_manifest(self.root, "m1", [RevisionEntry(D1, SOURCE, 1), RevisionEntry(D2, SOURCE, 1)])
        first = FakeOrchestration(
            manifest_id="m1",
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            output_dir=dirs["output_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            report_dir=dirs["report_dir"],
            manifest_dir=dirs["manifest_dir"],
        )
        self.assertTrue(luigi.build([first], local_scheduler=True, log_level="ERROR"))

        # Every recomputed report names the exact detail versions it used
        report_16 = FakeReport(
            week_end=datetime.date(2026, 1, 16),
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            report_dir=dirs["report_dir"],
        )
        with report_16.output().open("r") as handle:
            payload = json.load(handle)
        lineage = {item["date"]: item for item in payload["details"]}
        self.assertEqual(lineage[D1.isoformat()]["version"], 1)
        self.assertEqual(lineage[D2.isoformat()]["version"], 1)
        self.assertEqual(payload["metrics"]["value_total"], 2)

        # Resubmitting the same manifest must not rerun the whole week
        rerun = FakeOrchestration(
            manifest_id="m1",
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            output_dir=dirs["output_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            report_dir=dirs["report_dir"],
            manifest_dir=dirs["manifest_dir"],
        )
        self.assertTrue(rerun.complete())
        runs_before = dict(FakeReport.run_runs)
        self.assertTrue(luigi.build([rerun], local_scheduler=True, log_level="ERROR"))
        self.assertEqual(FakeReport.run_runs, runs_before)

        # New manifest bumps only D1: impacted windows recompute, others stay
        unaffected = FakeReport(
            week_end=datetime.date(2026, 1, 17),
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            report_dir=dirs["report_dir"],
        )
        untouched_mtime = os.path.getmtime(unaffected.output().path)
        write_manifest(self.root, "m2", [RevisionEntry(D1, SOURCE, 2), RevisionEntry(D2, SOURCE, 1)])
        second = FakeOrchestration(
            manifest_id="m2",
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            output_dir=dirs["output_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            report_dir=dirs["report_dir"],
            manifest_dir=dirs["manifest_dir"],
        )
        self.assertTrue(luigi.build([second], local_scheduler=True, log_level="ERROR"))

        with report_16.output().open("r") as handle:
            recomputed = json.load(handle)
        new_lineage = {item["date"]: item for item in recomputed["details"]}
        self.assertEqual(new_lineage[D1.isoformat()]["version"], 2)
        self.assertEqual(new_lineage[D2.isoformat()]["version"], 1)
        # Window ending 01-17 does not contain D1: never recomputed
        self.assertEqual(os.path.getmtime(unaffected.output().path), untouched_mtime)
        self.assertEqual(FakeReport.run_runs["2026-01-17"], 1)
        self.assertEqual(FakeReport.run_runs["2026-01-16"], 2)

    def test_manifest_with_only_unchanged_entries_schedules_no_reports(self):
        dirs = self._dirs()
        kwargs = dict(
            source_name=SOURCE,
            state_dir=dirs["state_dir"],
            output_dir=dirs["output_dir"],
            detail_output_dir=dirs["detail_output_dir"],
            report_dir=dirs["report_dir"],
            manifest_dir=dirs["manifest_dir"],
        )
        write_manifest(self.root, "m1", [RevisionEntry(D1, SOURCE, 1)])
        first = FakeOrchestration(manifest_id="m1", **kwargs)
        self.assertTrue(luigi.build([first], local_scheduler=True, log_level="ERROR"))
        report_runs_after_m1 = dict(FakeReport.run_runs)

        # New manifest id, but every entry matches already applied state
        write_manifest(self.root, "m2-noop", [RevisionEntry(D1, SOURCE, 1)])
        second = FakeOrchestration(manifest_id="m2-noop", **kwargs)
        self.assertTrue(luigi.build([second], local_scheduler=True, log_level="ERROR"))
        self.assertTrue(second.complete())
        with second.get_backfill_task().output().open("r") as handle:
            plan = json.load(handle)
        self.assertEqual(plan["revised_details"], [])
        self.assertEqual(plan["impacted_reports"], [])
        self.assertEqual(FakeReport.run_runs, report_runs_after_m1)


if __name__ == "__main__":
    unittest.main()
