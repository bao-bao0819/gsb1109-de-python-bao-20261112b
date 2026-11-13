# -*- coding: utf-8 -*-
# Copyright 2026 Yelp and Contributors
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
"""Offline reconciliation of task counters against committed output.

When a map (or reduce) task fails and is retried, Hadoop keeps the stderr of
*every* attempt. Each attempt emits its own ``reporter:counter:`` lines as it
runs, so a counter that shows up in both the failed and the successful
attempt is counted twice if the stderr logs are simply summed. Only one
attempt per logical task (input split) ever has its output committed; the
output of the other attempts is discarded.

The job history file is the record of which attempt committed:

* ``*_ATTEMPT_FAILED`` events name the failed attempts
* ``TASK_FINISHED`` carries the counters of the one successful attempt,
  keyed by task ID (the logical split)
* ``attempt_to_container_id`` links YARN container log dirs back to attempts

This module reuses mrjob's existing parsers rather than re-implementing any
log format:

* :func:`mrjob.parse.parse_mr_job_stderr` for ``reporter:counter:`` lines
* :func:`mrjob.logs.history._parse_yarn_history_log` and
  :func:`mrjob.logs.history._parse_pre_yarn_history_log` (plus the
  ``_extract_yarn_counters`` / ``_parse_pre_yarn_counters`` helpers) for
  history events and counters
* :func:`mrjob.logs.task._match_task_log_path` for the attempt/container IDs
  embedded in task log paths

If the history (commit) record is missing, stderr alone cannot prove which
attempt's output is live. In that case the reconciler reports the set of
*feasible* totals (one candidate value per ambiguous logical task, combined
across tasks) instead of guessing -- in particular, it never adds two
attempts of the same task together.
"""
from itertools import product

import json

from mrjob.parse import parse_mr_job_stderr
from mrjob.py2 import integer_types
from mrjob.py2 import string_types
from mrjob.logs.counters import _sum_counters
from mrjob.logs.history import _extract_yarn_counters
from mrjob.logs.history import _parse_pre_yarn_counters
from mrjob.logs.history import _parse_pre_yarn_history_log
from mrjob.logs.history import _parse_pre_yarn_history_records
from mrjob.logs.history import _parse_yarn_history_log
from mrjob.logs.ids import _attempt_id_to_task_id
from mrjob.logs.task import _match_task_log_path


# attempt statuses
FAILED = 'failed'        # history records this attempt as failed/killed
COMMITTED = 'committed'  # surviving attempt of a task with a TASK_FINISHED
UNKNOWN = 'unknown'      # we have a log, but no record of whether it committed

# discrepancy kinds
OUTPUT_MISMATCH = 'output_mismatch'
STDERR_HISTORY_MISMATCH = 'stderr_history_mismatch'
OUTPUT_NOT_IN_LOGS = 'output_not_in_logs'
PLATFORM_DOUBLE_COUNTS_RETRIES = 'platform_double_counts_retries'
PLATFORM_MISMATCH = 'platform_mismatch'


def _counter_amount(counters, group, counter):
    """Pull one counter amount out of a group -> counter -> amount map,
    returning None if the counter wasn't reported."""
    if not isinstance(counters, dict):
        return None

    amount = counters.get(group, {}).get(counter)

    if isinstance(amount, integer_types):
        return amount
    else:
        return None


def _yarn_task_counters(lines):
    """Scan YARN ``.jhist`` lines for per-task TASK_FINISHED counters.

    Returns a map from task ID to group -> counter -> amount, reusing
    :func:`mrjob.logs.history._extract_yarn_counters` for the actual parsing.
    """
    task_counters = {}

    for line in lines:
        if not line.startswith('{'):
            continue

        try:
            record = json.loads(line)
        except ValueError:
            continue

        if record.get('type') != 'TASK_FINISHED':
            continue

        event_record = record.get('event')
        if not isinstance(event_record, dict):
            continue

        for event in event_record.values():
            if not isinstance(event, dict):
                continue

            task_id = event.get('taskid')
            counters_record = event.get('counters')

            if (isinstance(task_id, string_types) and
                    isinstance(counters_record, dict)):
                task_counters[task_id] = _extract_yarn_counters(
                    counters_record)

    return task_counters


def _pre_yarn_task_counters(lines):
    """Scan pre-YARN history lines for per-task counters, reusing
    :func:`mrjob.logs.history._parse_pre_yarn_history_records` and
    :func:`mrjob.logs.history._parse_pre_yarn_counters`."""
    task_counters = {}

    for record in _parse_pre_yarn_history_records(lines):
        fields = record['fields']

        if (record['type'] == 'Task' and
                'COUNTERS' in fields and 'TASKID' in fields):
            task_counters[fields['TASKID']] = _parse_pre_yarn_counters(
                fields['COUNTERS'])

    return task_counters


class CounterReconciler(object):
    """Reconcile one counter (a group/name pair) across task attempts.

    Feed it task stderr logs (via :meth:`add_task_log`), zero or one job
    history log (via :meth:`add_yarn_history` or
    :meth:`add_pre_yarn_history`), and optionally the accepted product row
    counts (via :meth:`add_output`), then call :meth:`report`.
    """

    def __init__(self, group, counter):
        self.group = group
        self.counter = counter

        # one evidence record per task log (attempt ids may not be known
        # until a YARN history links containers to attempts)
        self.evidence = []

        # task_id -> group -> counter -> amount, from TASK_FINISHED
        self.task_counters = {}
        # attempt IDs that the history says failed
        self.failed_attempt_ids = set()
        # attempt_id -> container_id (YARN history)
        self.attempt_to_container_id = {}
        # container_id -> attempt_id (reverse of the above)
        self.container_to_attempt_id = {}
        # task_id -> accepted number of product lines
        self.outputs = {}

        self.history_present = False

    ### ingest ###

    def add_task_log(self, path, stderr, attempt_id=None):
        """Add one task stderr log.

        *stderr* is bytes or a list of byte lines, as accepted by
        :func:`mrjob.parse.parse_mr_job_stderr`. *attempt_id* is inferred
        from *path* when not given; a YARN container path is linked later
        when a history supplies ``attempt_to_container_id``.
        """
        parsed = parse_mr_job_stderr(stderr)

        if attempt_id is None:
            attempt_id = self._infer_attempt_id(path)

        match = _match_task_log_path(path) or {}

        if attempt_id is None and not match.get('container_id'):
            # a pre-YARN path would have yielded an attempt ID; a YARN
            # path yields a container ID we can link later. Anything else
            # can't be tied to a logical task without help.
            raise ValueError(
                'could not infer attempt ID from path: %s' % path)

        record = dict(
            path=path,
            attempt_id=attempt_id,
            container_id=match.get('container_id'),
            task_id=(
                _attempt_id_to_task_id(attempt_id)
                if attempt_id else None),
            counters=parsed['counters'],
            amount=_counter_amount(
                parsed['counters'], self.group, self.counter),
            other=parsed['other'],
            statuses=parsed['statuses'],
            status=UNKNOWN)

        self.evidence.append(record)

    def add_yarn_history(self, lines):
        """Add a YARN ``.jhist`` history log (an iterable of text lines)."""
        lines = list(lines)
        interpretation = _parse_yarn_history_log(lines)

        self._record_history(
            interpretation,
            _yarn_task_counters(lines))

    def add_pre_yarn_history(self, lines):
        """Add a pre-YARN history log (an iterable of text lines)."""
        lines = list(lines)
        interpretation = _parse_pre_yarn_history_log(lines)

        self._record_history(
            interpretation,
            _pre_yarn_task_counters(lines))

    def add_output(self, task_id, num_lines):
        """Record the accepted product line count for a logical task."""
        self.outputs[task_id] = num_lines

    ### report ###

    def report(self, platform_total=None):
        """Build the reconciliation report.

        Returns a dictionary with the keys:

        tasks
            map from task ID to a per-task report (see below)
        unlinked_logs
            container logs whose logical task can't be determined (no
            history mapping); each is treated as its own evidence group
        totals
            naive/accepted/log-only sums and the set of feasible grand totals
        missing_commit_records
            task IDs whose stderr exists but whose commit status is unknown
        discrepancies
            things that don't line up, each with a ``kind`` field

        Each per-task report contains:

        attempts
            one record per attempt, with ``status`` and stderr ``amount``
        accepted_amount
            counter amount of the committed attempt (or None)
        feasible_amounts
            sorted distinct amounts the task could have, when no commit
            record exists
        output_lines
            accepted product line count, if provided
        """
        self._link_containers()
        self._assign_statuses()

        tasks = {}

        for record in self.evidence:
            task_id = record['task_id']

            if task_id is None:
                # container log with no history mapping; can't place it
                continue

            task = tasks.setdefault(task_id, self._new_task(task_id))
            task['attempts'].append(record)

        # tasks known from history even if no stderr survived
        for task_id, counters in self.task_counters.items():
            tasks.setdefault(task_id, self._new_task(task_id, counters))

        unlinked_logs = [
            dict(path=r['path'],
                 container_id=r['container_id'],
                 amount=r['amount'])
            for r in self.evidence
            if r['task_id'] is None]

        missing_commit_records = []
        discrepancies = []

        for task_id in sorted(tasks):
            task = tasks[task_id]
            self._resolve_task(task)

            if any(a['status'] == UNKNOWN for a in task['attempts']):
                missing_commit_records.append(task_id)

            discrepancies.extend(self._task_discrepancies(task_id, task))

        totals = self._totals(tasks, unlinked_logs)

        if platform_total is not None:
            discrepancies.extend(
                self._platform_discrepancies(platform_total, totals))

        return dict(
            tasks=tasks,
            unlinked_logs=unlinked_logs,
            totals=totals,
            missing_commit_records=missing_commit_records,
            discrepancies=discrepancies)

    ### internals ###

    def _new_task(self, task_id, counters=None):
        return dict(
            attempts=[],
            history_amount=_counter_amount(
                counters if counters is not None
                else self.task_counters.get(task_id),
                self.group, self.counter),
            output_lines=self.outputs.get(task_id),
            accepted_amount=None,
            feasible_amounts=[],
            committed_attempt_ids=[])

    def _infer_attempt_id(self, path):
        match = _match_task_log_path(path)
        if not match:
            return None

        if match.get('attempt_id'):
            return match['attempt_id']

        container_id = match.get('container_id')
        if container_id:
            return self.container_to_attempt_id.get(container_id)

        return None

    def _record_history(self, interpretation, task_counters):
        self.history_present = True
        self.task_counters = task_counters

        self.attempt_to_container_id = interpretation.get(
            'attempt_to_container_id', {})
        self.container_to_attempt_id = dict(
            (container_id, attempt_id)
            for attempt_id, container_id
            in self.attempt_to_container_id.items())

        for error in interpretation.get('errors') or ():
            attempt_id = error.get('attempt_id')
            if attempt_id:
                self.failed_attempt_ids.add(attempt_id)

    def _link_containers(self):
        """Resolve container-only evidence to attempt IDs once (if) a
        history mapping is available."""
        for record in self.evidence:
            if record['attempt_id'] is None and record['container_id']:
                attempt_id = self.container_to_attempt_id.get(
                    record['container_id'])
                if attempt_id:
                    record['attempt_id'] = attempt_id
                    record['task_id'] = _attempt_id_to_task_id(attempt_id)

    def _assign_statuses(self):
        for record in self.evidence:
            attempt_id = record['attempt_id']

            if attempt_id and attempt_id in self.failed_attempt_ids:
                record['status'] = FAILED
            elif (self.history_present and
                    record['task_id'] in self.task_counters):
                # task has a TASK_FINISHED; any attempt not recorded as
                # failed is a surviving (committed) attempt
                record['status'] = COMMITTED
            else:
                record['status'] = UNKNOWN

    def _resolve_task(self, task):
        history_amount = task['history_amount']
        output_lines = task['output_lines']

        unknown = [a for a in task['attempts']
                   if a['status'] == UNKNOWN]

        if unknown:
            # no commit record for these logs: each attempt's value is a
            # possibility, but the attempts are never summed together
            task['feasible_amounts'] = sorted(set(
                a['amount'] for a in unknown if a['amount'] is not None))

        if history_amount is not None:
            # TASK_FINISHED counter is the authoritative value of the
            # one committed attempt
            task['accepted_amount'] = history_amount
        elif output_lines is not None:
            # no history; the accepted product itself decides, but only
            # if some surviving log actually supports it
            if (not unknown or
                    not task['feasible_amounts'] or
                    output_lines in task['feasible_amounts']):
                task['accepted_amount'] = output_lines

        task['committed_attempt_ids'] = [
            a['attempt_id'] for a in task['attempts']
            if a['status'] == COMMITTED]

    def _task_discrepancies(self, task_id, task):
        discrepancies = []

        accepted_amount = task['accepted_amount']
        output_lines = task['output_lines']

        if (accepted_amount is not None and
                output_lines is not None and
                accepted_amount != output_lines):
            discrepancies.append(dict(
                kind=OUTPUT_MISMATCH,
                task_id=task_id,
                counter_amount=accepted_amount,
                output_lines=output_lines))

        for attempt in task['attempts']:
            if (attempt['status'] == COMMITTED and
                    task['history_amount'] is not None and
                    attempt['amount'] is not None and
                    attempt['amount'] != task['history_amount']):
                discrepancies.append(dict(
                    kind=STDERR_HISTORY_MISMATCH,
                    task_id=task_id,
                    attempt_id=attempt['attempt_id'],
                    stderr_amount=attempt['amount'],
                    history_amount=task['history_amount']))

        if (accepted_amount is None and
                output_lines is not None and
                task['feasible_amounts'] and
                output_lines not in task['feasible_amounts']):
            discrepancies.append(dict(
                kind=OUTPUT_NOT_IN_LOGS,
                task_id=task_id,
                output_lines=output_lines,
                feasible_amounts=task['feasible_amounts']))

        return discrepancies

    def _totals(self, tasks, unlinked_logs):
        naive_stderr_sum = 0
        failed_stderr_sum = 0
        accepted_total = 0
        accepted_components = []  # one candidate list per evidence group

        for task_id in sorted(tasks):
            task = tasks[task_id]

            for attempt in task['attempts']:
                if attempt['amount'] is None:
                    continue

                naive_stderr_sum += attempt['amount']

                if attempt['status'] == FAILED:
                    failed_stderr_sum += attempt['amount']

            if task['accepted_amount'] is not None:
                accepted_total += task['accepted_amount']
                accepted_components.append([task['accepted_amount']])
            elif task['feasible_amounts']:
                accepted_components.append(task['feasible_amounts'])

        # unlinked container logs can't be shown to be retries of the same
        # split, so each stands as its own one-attempt evidence group
        unlinked_sum = 0
        for record in unlinked_logs:
            if record['amount'] is None:
                continue

            naive_stderr_sum += record['amount']
            unlinked_sum += record['amount']
            accepted_components.append([record['amount']])

        feasible_totals = sorted(set(
            sum(combo) for combo in product(*accepted_components)))

        return dict(
            accepted=accepted_total,
            failed_stderr_sum=failed_stderr_sum,
            unlinked_stderr_sum=unlinked_sum,
            naive_stderr_sum=naive_stderr_sum,
            feasible=feasible_totals)

    def _platform_discrepancies(self, platform_total, totals):
        feasible = totals['feasible']

        if platform_total in feasible:
            # the exported number is one of the totals that can hold
            return []

        if platform_total == totals['naive_stderr_sum']:
            kind = PLATFORM_DOUBLE_COUNTS_RETRIES
        else:
            kind = PLATFORM_MISMATCH

        return [dict(
            kind=kind,
            platform_total=platform_total,
            accepted_total=totals['accepted'],
            naive_stderr_sum=totals['naive_stderr_sum'],
            feasible_totals=feasible)]

    def sum_history_counters(self):
        """Sum the full TASK_FINISHED counter maps across logical tasks,
        mirroring a correctly de-duplicated job-level counter. Uses
        :func:`mrjob.logs.counters._sum_counters`."""
        return _sum_counters(*self.task_counters.values())
