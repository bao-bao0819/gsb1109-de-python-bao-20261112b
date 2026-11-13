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

"""
Batches missing dates of a recurring collection task into vendor request windows.

Made for the case where a vendor bills per requested window, so adjacent
dates should be fetched in one request ("one trip") instead of one request
per date. Windows are planned per authorization scope and data version -
a request never mixes scopes or versions - and never span dates that are
explicitly forbidden to request. Each window is at most a configurable
number of days long, and the planning minimizes the number of requests.

See ``BatchedRangeDaily`` for the luigi integration and ``plan_batches`` /
``plan_windows`` for the pure planning functions, whose results are plain
``Batch`` tuples that are easy to inspect and assert on.

When the vendor only delivers part of a window, ``WindowedFetchTask`` stores
one output per delivered date plus a receipt listing them, so exactly the
dates that actually came back become complete and the rest is re-planned on
the next invocation.
"""

import json
import logging
import os
import tempfile
import time
from bisect import bisect_right
from collections import namedtuple
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone

import luigi
from luigi.parameter import ParameterException
from luigi.tools.range import RangeDailyBase, RangeEvent

logger = logging.getLogger("luigi-interface")


def _as_date(d):
    """
    Coerces a date, datetime or ISO ``YYYY-MM-DD`` string to a ``date``.
    """
    if isinstance(d, datetime):
        return d.date()
    if isinstance(d, date):
        return d
    if isinstance(d, str):
        return date.fromisoformat(d)
    raise TypeError("Expected a date, datetime or ISO date string, got %r" % (d,))


class Batch(namedtuple("Batch", ["scope", "version", "start", "end"])):
    """
    One vendor request: a contiguous, inclusive date window for a single
    authorization scope and data version.
    """

    __slots__ = ()

    @property
    def days(self):
        """
        Number of calendar days the window spans, both endpoints included.
        """
        return (self.end - self.start).days + 1

    def dates(self):
        """
        Every calendar date in the window, in ascending order.
        """
        return [self.start + timedelta(days=i) for i in range(self.days)]

    def covers(self, d):
        """
        True if the date falls inside the window.
        """
        return self.start <= _as_date(d) <= self.end


def _spans_forbidden(forbidden_sorted, last, candidate):
    """
    True if any forbidden date falls in (last, candidate], i.e. a window
    ending at ``last`` cannot be extended to ``candidate``.
    """
    pos = bisect_right(forbidden_sorted, last)
    return pos < len(forbidden_sorted) and forbidden_sorted[pos] <= candidate


def plan_windows(dates, forbidden_dates=frozenset(), max_days=1):
    """
    Packs ``dates`` into a minimal number of contiguous windows.

    Returns a sorted list of inclusive ``(start, end)`` date pairs such that:

    * every requested date is covered by exactly one window, except dates
      that are themselves forbidden - those can never be requested and are
      dropped (a warning is logged),
    * no window contains a forbidden date, not even as a day in between two
      requested dates,
    * every window is at most ``max_days`` calendar days long.

    Windows may span dates that were not requested (e.g. already complete
    ones) when that merges two requested dates into a single billed request.
    The greedy leftmost-first packing is optimal, so the number of windows
    is the minimal number of requests needed.
    """
    if max_days < 1:
        raise ValueError("max_days must be at least 1, got %r" % (max_days,))
    forbidden = sorted(_as_date(d) for d in forbidden_dates)
    forbidden_set = set(forbidden)
    requested = {_as_date(d) for d in dates}
    skipped = sorted(requested & forbidden_set)
    if skipped:
        logger.warning("Dates %s are forbidden to request and will stay uncovered", [d.isoformat() for d in skipped])
    pending = sorted(requested - forbidden_set)
    windows = []
    index = 0
    while index < len(pending):
        start = pending[index]
        limit = start + timedelta(days=max_days - 1)
        end = start
        index += 1
        while index < len(pending) and pending[index] <= limit and not _spans_forbidden(forbidden, end, pending[index]):
            end = pending[index]
            index += 1
        windows.append((start, end))
    return windows


def _normalize_missing(missing):
    groups = {}
    if isinstance(missing, Mapping):
        items = ((d, scope, version) for (scope, version), dates in missing.items() for d in dates)
    else:
        items = missing
    for d, scope, version in items:
        groups.setdefault((scope, version), set()).add(_as_date(d))
    return groups


def _forbidden_for_group(forbidden_dates, scope, version):
    if callable(forbidden_dates):
        return forbidden_dates(scope, version)
    if isinstance(forbidden_dates, Mapping):
        return forbidden_dates.get((scope, version), forbidden_dates.get(scope, ()))
    return forbidden_dates


def plan_batches(missing, forbidden_dates=frozenset(), max_days=1):
    """
    Groups missing dates into ``Batch`` requests.

    ``missing`` is either an iterable of ``(date, scope, version)`` triples
    or a mapping of ``(scope, version)`` to an iterable of dates. Dates of
    different scopes or versions are never merged into one request.

    ``forbidden_dates`` is either an iterable of dates applied to every
    group, a mapping of scope or ``(scope, version)`` to dates, or a
    callable ``(scope, version) -> dates``.

    Returns a list of ``Batch``. Groups are ordered by ``(scope, version)``;
    within a group windows are ordered newest first, so submitting the list
    in order catches up the most recent gaps of each group first.
    """
    groups = _normalize_missing(missing)
    batches = []
    for scope, version in sorted(groups, key=lambda key: (str(key[0]), str(key[1]))):
        forbidden = _forbidden_for_group(forbidden_dates, scope, version)
        for start, end in reversed(plan_windows(groups[(scope, version)], forbidden, max_days)):
            batches.append(Batch(scope=scope, version=version, start=start, end=end))
    return batches


def coverage(batches):
    """
    Maps each ``(scope, version)`` group to the sorted list of dates its
    windows request. Useful to verify that a plan, the already complete
    dates and the forbidden dates add up to the intended range.
    """
    result = {}
    for batch in batches:
        result.setdefault((batch.scope, batch.version), set()).update(batch.dates())
    return {key: sorted(dates) for key, dates in result.items()}


class WindowedFetchTask(luigi.Task):
    """
    Base class for a single vendor request ("one trip") covering a
    contiguous, inclusive date window for one scope and one data version.

    Subclasses implement ``fetch()`` to perform the actual request and
    usually override ``target_for_date()`` to control where per-date
    payloads land.
    """

    scope = luigi.Parameter(description="authorization scope the request is billed under")
    version = luigi.Parameter(description="data version the request is billed under")
    start = luigi.DateParameter(description="first requested date, inclusive")
    end = luigi.DateParameter(description="last requested date, inclusive")

    def window_dates(self):
        """
        Every calendar date in the requested window, in ascending order.
        """
        return [self.start + timedelta(days=i) for i in range((self.end - self.start).days + 1)]

    def fetch(self, start, end):
        """
        Performs the vendor request for the inclusive window [start, end].

        Must return the dates that actually came back with data, either as
        an iterable of dates or as a mapping of date to payload. The vendor
        may deliver only part of the window; only the delivered dates get
        outputs and become complete.
        """
        raise NotImplementedError

    @classmethod
    def target_for_date(cls, d, scope, version):
        """
        Target holding one date's payload. Must be deterministic in
        (date, scope, version) so that completeness can be probed per date.
        """
        return luigi.LocalTarget(os.path.join(tempfile.gettempdir(), cls.get_task_family(), str(scope), str(version), _as_date(d).isoformat()))

    @classmethod
    def date_complete(cls, d, scope, version):
        """
        True if the payload of a single date is durably stored.
        """
        return cls.target_for_date(_as_date(d), scope, version).exists()

    def receipt_target(self):
        return luigi.LocalTarget(
            os.path.join(
                tempfile.gettempdir(),
                self.get_task_family(),
                str(self.scope),
                str(self.version),
                "receipts",
                "%s_%s.json" % (self.start.isoformat(), self.end.isoformat()),
            )
        )

    def output(self):
        # The receipt records which dates the vendor actually delivered, so
        # a partially filled window counts as done and is not re-requested
        # wholesale; the undelivered dates simply stay incomplete.
        return self.receipt_target()

    def serialize_payload(self, d, payload):
        if payload is None:
            return ""
        if isinstance(payload, str):
            return payload
        return json.dumps(payload, sort_keys=True)

    def run(self):
        if self.start > self.end:
            raise ParameterException("Can't have start > end")
        results = self.fetch(self.start, self.end)
        items = results.items() if isinstance(results, Mapping) else ((d, None) for d in results)
        delivered = set()
        for d, payload in items:
            d = _as_date(d)
            if not self.start <= d <= self.end:
                raise ValueError("fetch() returned %s, outside the requested window [%s, %s]" % (d, self.start, self.end))
            target = self.target_for_date(d, self.scope, self.version)
            logger.info("Storing %s payload for %s", self.get_task_family(), d)
            with target.open("w") as f:
                f.write(self.serialize_payload(d, payload))
            delivered.add(d)
        receipt = {
            "scope": self.scope,
            "version": self.version,
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "delivered": sorted(d.isoformat() for d in delivered),
        }
        with self.output().open("w") as f:
            f.write(json.dumps(receipt, indent=2, sort_keys=True))

    def delivered_dates(self):
        """
        Sorted list of dates the vendor actually delivered, per the stored
        receipt, or None if the task has not run yet.
        """
        if not self.output().exists():
            return None
        with self.output().open("r") as f:
            return [date.fromisoformat(d) for d in json.load(f)["delivered"]]


class BatchedRangeDaily(RangeDailyBase):
    """
    Produces a contiguous completed range of a daily collection task that is
    fetched from a vendor billing per request window.

    Adjacent missing dates are merged into a single ``WindowedFetchTask``
    ("one trip") of at most ``max_window_days`` days, minimizing the number
    of billed requests. A request never mixes authorization scopes or data
    versions and never spans a date listed in ``forbidden_dates``. Within
    each scope/version group the windows are required newest first.

    Convenient to use even from command line, like:

    .. code-block:: console

        luigi --module your.module BatchedRangeDaily --of YourWindowedFetchTask \\
            --scopes '["eur"]' --versions '["v2"]' --start 2026-01-01
    """

    scopes = luigi.ListParameter(default=[], description="authorization scopes to keep contiguous; a request never mixes scopes")
    versions = luigi.ListParameter(default=[], description="data versions to keep contiguous; a request never mixes versions")
    max_window_days = luigi.IntParameter(default=7, description="longest window in days billable as a single request")
    forbidden_dates = luigi.DictParameter(
        default=dict(),
        description='map of scope (or "scope/version") to a list of ISO dates that must never be part of a request',
    )

    def requires(self):
        # cache because we anticipate a fair amount of computation
        if hasattr(self, "_cached_requires"):
            return self._cached_requires
        batches = self.batch_plan()[: self.task_limit]
        self._cached_requires = [self._instantiate_batch(batch) for batch in batches]
        return self._cached_requires

    def batch_plan(self):
        """
        The inspectable list of ``Batch`` requests that ``requires()``
        submits: grouped by (scope, version), each group's windows ordered
        newest first.
        """
        if hasattr(self, "_cached_plan"):
            return self._cached_plan
        if not isinstance(self.of, type) or not issubclass(self.of, WindowedFetchTask):
            raise ParameterException("of must be a WindowedFetchTask subclass")
        if not self.start and not self.stop:
            raise ParameterException("At least one of start and stop needs to be specified")
        if self.start and self.stop and self.start > self.stop:
            raise ParameterException("Can't have start > stop")
        if not self.scopes or not self.versions:
            raise ParameterException("scopes and versions both need at least one entry")

        now = datetime.fromtimestamp(time.time() if self.now is None else self.now, tz=timezone.utc).replace(tzinfo=None)
        finite_start = max(self.parameter_to_datetime(self.start), self.moving_start(now)) if self.start else self.moving_start(now)
        finite_stop = min(self.parameter_to_datetime(self.stop), self.moving_stop(now)) if self.stop else self.moving_stop(now)

        dates = [d.date() for d in self.finite_datetimes(finite_start, finite_stop)] if finite_start <= finite_stop else []
        missing = list(self.missing_combos(dates))
        self._emit_metrics(missing, len(dates) * len(self.scopes) * len(self.versions))

        self._cached_plan = plan_batches(missing, self._normalized_forbidden(), self.max_window_days)
        for batch in self._cached_plan:
            logger.info(
                "Planned request for %s: scope=%s version=%s [%s, %s] (%d days)",
                self.of.get_task_family(),
                batch.scope,
                batch.version,
                batch.start,
                batch.end,
                batch.days,
            )
        return self._cached_plan

    def missing_combos(self, dates):
        """
        Yields ``(date, scope, version)`` triples that are not complete.

        This is a conservative base implementation that probes
        ``date_complete`` date by date. Override to do bulk checks against
        e.g. a catalog or history service.
        """
        for scope in self.scopes:
            for version in self.versions:
                for d in dates:
                    if not self.of.date_complete(d, scope, version):
                        yield (d, scope, version)

    def _normalized_forbidden(self):
        normalized = {}
        for key, dates in self.forbidden_dates.items():
            dates = {_as_date(d) for d in dates}
            if isinstance(key, (tuple, list)) and len(key) == 2:
                normalized[(key[0], key[1])] = dates
            elif isinstance(key, str) and "/" in key:
                scope, version = key.split("/", 1)
                normalized[(scope, version)] = dates
            else:
                normalized[key] = dates
        return normalized

    def _emit_metrics(self, missing, expected_count):
        complete_count = expected_count - len(missing)
        task_family = self.of.get_task_family()
        self.trigger_event(RangeEvent.COMPLETE_COUNT, task_family, complete_count)
        self.trigger_event(RangeEvent.COMPLETE_FRACTION, task_family, float(complete_count) / expected_count if expected_count else 1)

    def _instantiate_batch(self, batch):
        kwargs = dict(self.of_params)
        kwargs.update(scope=batch.scope, version=batch.version, start=batch.start, end=batch.end)
        return self.of(**kwargs)
