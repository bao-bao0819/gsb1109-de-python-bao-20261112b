"""
Revision-manifest driven backfills for daily collection details.

Data sources periodically hand in *revision manifests*: lists of daily details
that have been corrected.  This module turns such a manifest into a precise
Luigi plan:

* only the details that actually changed are (re-)fetched, each revision into a
  versioned output path, so unchanged details are never pulled again;
* a day that is *formally corrected to empty* is recorded as a known empty
  marker and is distinct from a day for which no manifest has ever been
  received (e.g. an intentionally unsampled holiday);
* every recalculated rolling report records the exact detail versions it was
  built from, and resubmitting an already applied manifest does not rerun the
  whole week.

The manifest is a JSON document::

    {
      "manifest_id": "rev-2026-02-03",
      "issued_on": "2026-02-03",
      "revisions": [
        {"date": "2026-01-28", "source": "sales", "version": 3},
        {"date": "2026-01-29", "source": "sales", "version": 2, "empty": true}
      ]
    }

A minimal end-to-end setup looks like this::

    import luigi
    from luigi.contrib.revisions import (
        LocalRevisionManifest,
        RevisedDailyDetail,
        RevisionBackfill,
        RollingWeekReport,
    )

    class SalesDetail(RevisedDailyDetail):
        source_name = luigi.Parameter(default="sales")
        output_dir = luigi.Parameter(default="/data/details")

        def fetch_revised_detail(self, temp_path):
            ...  # write the corrected records for self.date at temp_path

    class SalesManifest(LocalRevisionManifest):
        manifest_dir = luigi.Parameter(default="/data/manifests")

    class SalesBackfill(RevisionBackfill):
        source_name = luigi.Parameter(default="sales")
        state_dir = luigi.Parameter(default="/data/state")
        output_dir = luigi.Parameter(default="/data/plans")

        def get_manifest_task(self):
            return SalesManifest(manifest_id=self.manifest_id)

        def get_detail_task_class(self):
            return SalesDetail

    class SalesWeekReport(RollingWeekReport):
        source_name = luigi.Parameter(default="sales")
        state_dir = luigi.Parameter(default="/data/state")
        detail_output_dir = luigi.Parameter(default="/data/details")
        report_dir = luigi.Parameter(default="/data/reports")

        def get_detail_task_class(self):
            return SalesDetail
"""

from __future__ import annotations

import datetime
import json
import os
from dataclasses import dataclass
from typing import Any, cast

import luigi
from luigi.local_target import LocalTarget


@dataclass(frozen=True)
class RevisionEntry:
    """One corrected daily detail in a revision manifest."""

    date: datetime.date
    source: str
    version: int
    empty: bool = False


@dataclass(frozen=True)
class RevisionManifest:
    """A parsed revision manifest."""

    manifest_id: str
    issued_on: datetime.date
    entries: tuple[RevisionEntry, ...]

    def entries_for(self, source: str) -> tuple[RevisionEntry, ...]:
        return tuple(entry for entry in self.entries if entry.source == source)


def _parse_date(value: Any, field: str) -> datetime.date:
    if isinstance(value, datetime.date):
        return value
    if isinstance(value, str):
        try:
            return datetime.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field} must be an ISO date string, got {value!r}") from exc
    raise ValueError(f"{field} must be an ISO date string, got {value!r}")


def load_manifest(source: Any) -> RevisionManifest:
    """Load and validate a manifest from a path string or a Luigi target."""
    target = LocalTarget(source) if isinstance(source, str) else source
    with target.open("r") as handle:
        payload = json.load(handle)

    manifest_id = payload.get("manifest_id")
    if not isinstance(manifest_id, str) or not manifest_id:
        raise ValueError("manifest must contain a non-empty 'manifest_id'")
    issued_on = _parse_date(payload.get("issued_on"), "issued_on")

    raw_entries = payload.get("revisions", [])
    if not isinstance(raw_entries, list):
        raise ValueError("'revisions' must be a list")

    entries: list[RevisionEntry] = []
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            raise ValueError(f"revisions[{index}] must be an object")
        source_name = raw.get("source")
        if not isinstance(source_name, str) or not source_name:
            raise ValueError(f"revisions[{index}] must contain a non-empty 'source'")
        version = raw.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError(f"revisions[{index}].version must be a positive integer, got {version!r}")
        entries.append(
            RevisionEntry(
                date=_parse_date(raw.get("date"), f"revisions[{index}].date"),
                source=source_name,
                version=version,
                empty=bool(raw.get("empty", False)),
            )
        )

    return RevisionManifest(manifest_id=manifest_id, issued_on=issued_on, entries=tuple(entries))


def rolling_window_ends_for(day: datetime.date, lookback: int = 7) -> list[datetime.date]:
    """Rolling-window end dates whose ``lookback``-day window contains ``day``."""
    return [day + datetime.timedelta(days=offset) for offset in range(lookback)]


class RevisionStateStore:
    """Persistent record of which detail version is currently applied per day.

    The store distinguishes three situations for any ``(source, date)``:

    * no state file: no manifest has ever covered that day (for example an
      intentionally unsampled holiday);
    * state with ``empty = true``: the day was formally corrected to empty;
    * state with ``empty = false``: the day has a concrete detail at ``version``.
    """

    def __init__(self, state_dir: str):
        self.state_dir = state_dir

    def state_path(self, source: str, day: datetime.date) -> str:
        return os.path.join(self.state_dir, "sources", source, f"{day.isoformat()}.state.json")

    def get_state(self, source: str, day: datetime.date) -> dict[str, Any] | None:
        target = LocalTarget(self.state_path(source, day))
        if not target.exists():
            return None
        with target.open("r") as handle:
            return json.load(handle)

    def needs_revision(self, entry: RevisionEntry) -> bool:
        """Return whether ``entry`` differs from the currently applied state."""
        state = self.get_state(entry.source, entry.date)
        if state is None:
            return True
        return state.get("version") != entry.version or bool(state.get("empty")) != entry.empty

    def is_known_empty(self, source: str, day: datetime.date) -> bool:
        state = self.get_state(source, day)
        return state is not None and bool(state.get("empty"))

    def mark_applied(
        self,
        entry: RevisionEntry,
        manifest_id: str,
        applied_on: datetime.date | None = None,
    ) -> None:
        target = LocalTarget(self.state_path(entry.source, entry.date))
        if target.exists():
            target.remove()
        payload = {
            "source": entry.source,
            "date": entry.date.isoformat(),
            "version": entry.version,
            "empty": entry.empty,
            "manifest_id": manifest_id,
            "applied_on": (applied_on or datetime.date.today()).isoformat(),
        }
        with target.temporary_path() as temp_path:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)


class RevisionManifestTask(luigi.ExternalTask):
    """External task pointing at one revision manifest document."""

    manifest_id = luigi.Parameter()

    def manifest_target(self) -> luigi.target.Target:
        raise NotImplementedError

    def output(self) -> luigi.target.Target:
        return self.manifest_target()

    def load(self) -> RevisionManifest:
        return load_manifest(self.output())


class LocalRevisionManifest(RevisionManifestTask):
    """Manifest task backed by a JSON file in a local directory."""

    manifest_dir = luigi.Parameter()

    def manifest_target(self) -> LocalTarget:
        return LocalTarget(os.path.join(self.manifest_dir, f"{self.manifest_id}.json"))


class RevisedDailyDetail(luigi.Task):
    """One versioned fetch of a corrected daily detail.

    Subclasses implement :meth:`fetch_revised_detail`, which writes the
    corrected records to the supplied temporary path.  Outputs are versioned,
    so a new revision lands next to (never overwrites) the previous one.
    """

    date = luigi.DateParameter()
    revision_version = luigi.IntParameter()
    revised_to_empty = luigi.BoolParameter(default=False)
    source_name = luigi.Parameter()
    output_dir = luigi.Parameter()

    def detail_path(self) -> str:
        return os.path.join(
            self.output_dir,
            "sources",
            self.source_name,
            self.date.isoformat(),
            f"v{self.revision_version}",
            "detail.json",
        )

    def output(self) -> LocalTarget:
        return LocalTarget(self.detail_path())

    def fetch_revised_detail(self, temp_path: str) -> None:
        raise NotImplementedError

    def run(self) -> None:
        target = self.output()
        with target.temporary_path() as temp_path:
            if self.revised_to_empty:
                with open(temp_path, "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "source": self.source_name,
                            "date": self.date.isoformat(),
                            "version": self.revision_version,
                            "empty": True,
                            "records": [],
                        },
                        handle,
                        indent=2,
                        sort_keys=True,
                    )
            else:
                self.fetch_revised_detail(temp_path)


class RevisionBackfill(luigi.Task):
    """Applies one manifest, touching only the details that really changed.

    Subclasses provide the manifest task and the detail task class.  The task
    output is a JSON plan naming the details actually refetched, the unchanged
    details skipped, and the rolling reports impacted by those dates.
    """

    manifest_id = luigi.Parameter()
    source_name = luigi.Parameter()
    state_dir = luigi.Parameter()
    output_dir = luigi.Parameter()
    detail_output_dir = luigi.Parameter(default="")
    lookback = luigi.IntParameter(default=7)

    def get_manifest_task(self) -> RevisionManifestTask:
        raise NotImplementedError

    def get_detail_task_class(self) -> type[RevisedDailyDetail]:
        raise NotImplementedError

    def _store(self) -> RevisionStateStore:
        return RevisionStateStore(self.state_dir)

    def _detail_dir(self) -> str:
        """Directory holding versioned detail outputs.

        Defaults to ``output_dir`` when no separate detail directory is given.
        """
        return self.detail_output_dir or self.output_dir

    def effective_revisions(self) -> tuple[RevisionEntry, ...]:
        """Manifest entries for this source that differ from applied state."""
        store = self._store()
        manifest = self.get_manifest_task().load()
        return tuple(entry for entry in manifest.entries_for(self.source_name) if store.needs_revision(entry))

    def requires(self):
        detail_cls = self.get_detail_task_class()
        return {
            entry.date.isoformat(): detail_cls(
                date=entry.date,
                revision_version=entry.version,
                revised_to_empty=entry.empty,
                source_name=self.source_name,
                output_dir=self._detail_dir(),
            )
            for entry in self.effective_revisions()
        }

    def output(self) -> LocalTarget:
        return LocalTarget(os.path.join(self.output_dir, "backfills", f"{self.manifest_id}.json"))

    def run(self) -> None:
        manifest = self.get_manifest_task().load()
        entries = self.effective_revisions()
        store = self._store()
        applied_on = datetime.date.today()

        revised: list[dict[str, Any]] = []
        for entry in entries:
            previous = store.get_state(entry.source, entry.date)
            store.mark_applied(entry, manifest_id=self.manifest_id, applied_on=applied_on)
            revised.append(
                {
                    "date": entry.date.isoformat(),
                    "version": entry.version,
                    "empty": entry.empty,
                    "previous": previous,
                }
            )

        skipped = [
            {
                "date": entry.date.isoformat(),
                "version": entry.version,
                "empty": entry.empty,
            }
            for entry in manifest.entries_for(self.source_name)
            if entry not in entries
        ]

        impacted: dict[str, list[str]] = {}
        for entry in entries:
            for week_end in rolling_window_ends_for(entry.date, lookback=self.lookback):
                impacted.setdefault(week_end.isoformat(), []).append(entry.date.isoformat())
        impacted_reports = [{"week_end": week_end, "revised_dates": sorted(dates)} for week_end, dates in sorted(impacted.items())]

        plan = {
            "manifest_id": self.manifest_id,
            "source": self.source_name,
            "issued_on": manifest.issued_on.isoformat(),
            "applied_on": applied_on.isoformat(),
            "revised_details": revised,
            "skipped_unchanged": skipped,
            "impacted_reports": impacted_reports,
        }
        with self.output().temporary_path() as temp_path:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(plan, handle, indent=2, sort_keys=True)


class RollingWeekReport(luigi.Task):
    """Rolling multi-day summary with explicit detail-version lineage.

    Days with no applied state are treated as intentionally unsampled and are
    not fetched.  Days formally corrected to empty contribute no records but
    still appear in the lineage with their version.  :meth:`complete` only
    holds while the report's recorded lineage matches the currently applied
    state, so a new revision invalidates the report for affected windows while
    resubmitting the same manifest leaves it untouched.
    """

    week_end = luigi.DateParameter()
    lookback = luigi.IntParameter(default=7)
    source_name = luigi.Parameter()
    state_dir = luigi.Parameter()
    detail_output_dir = luigi.Parameter()
    report_dir = luigi.Parameter()
    holidays = luigi.ListParameter(default=())

    def get_detail_task_class(self) -> type[RevisedDailyDetail]:
        """Task class producing the versioned daily details."""
        raise NotImplementedError

    def _store(self) -> RevisionStateStore:
        return RevisionStateStore(self.state_dir)

    def window_dates(self) -> list[datetime.date]:
        return [self.week_end - datetime.timedelta(days=offset) for offset in reversed(range(self.lookback))]

    def _holiday_dates(self) -> set[datetime.date]:
        raw_holidays = cast(tuple[Any, ...], self.holidays)
        parsed: list[datetime.date] = [_parse_date(value, "holidays") for value in raw_holidays]
        return set(parsed)

    def lineage(self) -> list[dict[str, Any]]:
        store = self._store()
        holidays = self._holiday_dates()
        details: list[dict[str, Any]] = []
        for day in self.window_dates():
            state = store.get_state(self.source_name, day)
            if state is None:
                details.append(
                    {
                        "date": day.isoformat(),
                        "status": "holiday" if day in holidays else "not_sampled",
                    }
                )
            elif state.get("empty"):
                details.append(
                    {
                        "date": day.isoformat(),
                        "status": "empty_correction",
                        "version": state["version"],
                    }
                )
            else:
                details.append(
                    {
                        "date": day.isoformat(),
                        "status": "used",
                        "version": state["version"],
                    }
                )
        return details

    def requires(self):
        detail_cls = self.get_detail_task_class()
        store = self._store()
        required: dict[str, RevisedDailyDetail] = {}
        for day in self.window_dates():
            state = store.get_state(self.source_name, day)
            if state is None:
                continue
            required[day.isoformat()] = detail_cls(
                date=day,
                revision_version=state["version"],
                revised_to_empty=bool(state.get("empty")),
                source_name=self.source_name,
                output_dir=self.detail_output_dir,
            )
        return required

    def output(self) -> LocalTarget:
        return LocalTarget(os.path.join(self.report_dir, f"rolling-{self.week_end.isoformat()}.json"))

    def complete(self) -> bool:
        if not super().complete():
            return False
        try:
            with self.output().open("r") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            return False
        return payload.get("details") == self.lineage()

    def read_detail(self, target: LocalTarget) -> list[Any]:
        """Read one detail output into a list of records. Override as needed."""
        with target.open("r") as handle:
            payload = json.load(handle)
        if isinstance(payload, list):
            return payload
        return list(payload.get("records", []))

    def aggregate(self, records_by_date: dict[str, list[Any]]) -> dict[str, Any]:
        """Default metrics: record count and, when present, sum of ``value``."""
        total_records = 0
        value_total = 0
        has_value = False
        for records in records_by_date.values():
            total_records += len(records)
            for record in records:
                if isinstance(record, dict) and "value" in record:
                    has_value = True
                    value_total += record["value"]
        metrics: dict[str, Any] = {"record_count": total_records}
        if has_value:
            metrics["value_total"] = value_total
        return metrics

    def run(self) -> None:
        output_target = self.output()
        if output_target.exists():
            output_target.remove()
        records_by_date: dict[str, list[Any]] = {}
        inputs = cast(dict[str, LocalTarget], self.input())
        for day, target in inputs.items():
            records_by_date[day] = self.read_detail(target)

        payload = {
            "report": "rolling",
            "source": self.source_name,
            "window": {
                "start": self.window_dates()[0].isoformat(),
                "week_end": self.week_end.isoformat(),
                "lookback": self.lookback,
            },
            "generated_on": datetime.date.today().isoformat(),
            "details": self.lineage(),
            "metrics": self.aggregate(records_by_date),
        }
        with self.output().temporary_path() as temp_path:
            with open(temp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)


class RevisionBackfillAndReports(luigi.Task):
    """Run one backfill and every rolling report impacted by its revisions.

    Dependencies are dynamic: the backfill has to run first because its plan
    names the impacted window end dates.  Only those report instances are
    scheduled, and their :meth:`~RollingWeekReport.complete` skips windows
    whose applied versions have not changed, so resubmitting the same manifest
    does not rerun anything.
    """

    manifest_id = luigi.Parameter()
    source_name = luigi.Parameter()
    state_dir = luigi.Parameter()
    output_dir = luigi.Parameter()
    detail_output_dir = luigi.Parameter()
    report_dir = luigi.Parameter()
    lookback = luigi.IntParameter(default=7)
    holidays = luigi.ListParameter(default=())

    def get_backfill_task(self) -> RevisionBackfill:
        """Build the backfill task. Implementations must forward ``lookback``."""
        raise NotImplementedError

    def get_report_task_class(self) -> type[RollingWeekReport]:
        """Build the rolling report class used for impacted windows."""
        raise NotImplementedError

    def _report_kwargs(self) -> dict[str, Any]:
        return {
            "source_name": self.source_name,
            "state_dir": self.state_dir,
            "detail_output_dir": self.detail_output_dir,
            "report_dir": self.report_dir,
            "lookback": self.lookback,
            "holidays": self.holidays,
        }

    def _impacted_reports(self) -> list[RollingWeekReport]:
        backfill = self.get_backfill_task()
        with backfill.output().open("r") as handle:
            plan = json.load(handle)
        report_cls = self.get_report_task_class()
        return [report_cls(week_end=datetime.date.fromisoformat(item["week_end"]), **self._report_kwargs()) for item in plan["impacted_reports"]]

    def complete(self) -> bool:
        backfill = self.get_backfill_task()
        if not backfill.complete():
            return False
        return all(report.complete() for report in self._impacted_reports())

    def run(self) -> Any:
        backfill = self.get_backfill_task()
        yield backfill
        yield self._impacted_reports()
