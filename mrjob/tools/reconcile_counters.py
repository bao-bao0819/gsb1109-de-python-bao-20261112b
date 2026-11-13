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
"""Offline reconciliation of warehouse counters against task logs.

A retried task leaves one stderr per attempt, and every attempt emits the
same ``reporter:counter:`` lines, so naively summing the logs double-counts
the failed attempt. This tool reuses mrjob's log parsers to group attempts
by logical task, use the job history to decide which attempt committed, and
-- when that record is missing -- report the totals that can actually hold
instead of guessing.

Everything runs locally on log files already downloaded from the cluster;
no cluster or cloud credentials are needed.

Usage::

    mrjob reconcile-counters --group GROUP --counter COUNTER [options] PATH ...

Each PATH is a file or directory scanned recursively. Job history files and
task ``stderr`` logs are recognized by their Hadoop path layouts. Options::

  --group GROUP             counter group to reconcile
  --counter COUNTER         counter name to reconcile
  --history PATH            add this history file (repeatable)
  --task-log PATH           add this task log or directory of logs
                            (repeatable)
  --output TASK_ID=LINES    accepted product line count for a logical task
                            (repeatable)
  --platform-total N        counter total exported by the platform, to check
  --json                    print the raw reconciliation report as JSON
  -h, --help                show this help message and exit
"""
from __future__ import print_function

import argparse
import json
import os

from mrjob.logs.history import _match_history_log_path
from mrjob.logs.reconcile import CounterReconciler
from mrjob.logs.task import _match_task_log_path


def main(cl_args=None):
    arg_parser = _make_arg_parser()
    options = arg_parser.parse_args(cl_args)

    reconciler = CounterReconciler(options.group, options.counter)

    history_paths = []
    task_log_paths = []
    seen_paths = set()

    # every path input (positional, --history, --task-log) may be a file or
    # a directory tree; classify what we find inside each one
    for path in list(options.paths or ()) + list(options.history or ()) + \
            list(options.task_log or ()):
        for kind, matched_path in _scan(path):
            if matched_path in seen_paths:
                continue
            seen_paths.add(matched_path)

            if kind == 'history':
                history_paths.append(matched_path)
            else:
                task_log_paths.append(matched_path)

    for history_path in history_paths:
        match = _match_history_log_path(history_path) or {}

        with open(history_path) as f:
            lines = f.readlines()

        if match.get('yarn'):
            reconciler.add_yarn_history(lines)
        else:
            reconciler.add_pre_yarn_history(lines)

    for task_log_path in task_log_paths:
        with open(task_log_path, 'rb') as f:
            reconciler.add_task_log(task_log_path, f.read())

    for output in options.output or ():
        task_id, _, num_lines = output.partition('=')
        if not task_id or not num_lines.isdigit():
            arg_parser.error(
                '--output expects TASK_ID=LINES, got %r' % output)
        reconciler.add_output(task_id, int(num_lines))

    report = reconciler.report(platform_total=options.platform_total)

    if options.as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_report(report, options.group, options.counter))

    return 1 if report['discrepancies'] else 0


def _scan(path):
    """Yield ('history', path) / ('task', path) pairs for a file or dir."""
    if os.path.isfile(path):
        matched = _classify(path)
        if matched:
            yield matched
        return

    for dirpath, _, filenames in os.walk(path):
        for filename in sorted(filenames):
            matched_path = os.path.join(dirpath, filename)
            matched = _classify(matched_path)
            if matched:
                yield matched


def _classify(path):
    if _match_history_log_path(path):
        return ('history', path)

    match = _match_task_log_path(path)
    if match and match.get('log_type') == 'stderr':
        return ('task', path)

    return None


def _format_report(report, group, counter):
    lines = []
    lines.append('Reconciling counter %s / %s' % (group, counter))
    lines.append('')

    for task_id in sorted(report['tasks']):
        task = report['tasks'][task_id]
        lines.append('task %s' % task_id)

        for attempt in task['attempts']:
            lines.append('  %-9s amount=%-6s %s' % (
                attempt['status'],
                _dash_if_none(attempt['amount']),
                attempt['path']))

        if task['history_amount'] is not None:
            lines.append('  history (TASK_FINISHED): %d' %
                         task['history_amount'])

        if task['output_lines'] is not None:
            lines.append('  accepted product lines: %d' %
                         task['output_lines'])

        if task['accepted_amount'] is not None:
            lines.append('  accepted counter: %d' %
                         task['accepted_amount'])
        elif task['feasible_amounts']:
            lines.append('  no commit record; feasible: %s' %
                         _format_list(task['feasible_amounts']))

        lines.append('')

    for record in report['unlinked_logs']:
        lines.append('unlinked container log (no history mapping): %s'
                     % record['path'])
        lines.append('  amount=%s' % _dash_if_none(record['amount']))
        lines.append('')

    if report['missing_commit_records']:
        lines.append('missing commit records for: %s' %
                     _format_list(report['missing_commit_records']))
        lines.append('')

    totals = report['totals']
    lines.append('totals:')
    lines.append('  naive sum of every stderr log : %d'
                 % totals['naive_stderr_sum'])
    lines.append('  failed-attempt stderr only    : %d'
                 % totals['failed_stderr_sum'])
    lines.append('  accepted (de-duplicated)      : %s'
                 % _dash_if_zero(totals['accepted']))
    lines.append('  feasible grand totals         : %s'
                 % _format_list(totals['feasible']))
    lines.append('')

    if report['discrepancies']:
        lines.append('discrepancies:')
        for discrepancy in report['discrepancies']:
            lines.append('  ' + _format_discrepancy(discrepancy))
    else:
        lines.append('no discrepancies')

    return '\n'.join(lines)


def _format_discrepancy(discrepancy):
    kind = discrepancy['kind']

    if kind == 'output_mismatch':
        return (
            '%s: %s counter=%d but product has %d lines' % (
                kind, discrepancy['task_id'],
                discrepancy['counter_amount'],
                discrepancy['output_lines']))

    if kind == 'stderr_history_mismatch':
        return (
            '%s: %s %s stderr=%d history=%d' % (
                kind, discrepancy['task_id'],
                discrepancy['attempt_id'],
                discrepancy['stderr_amount'],
                discrepancy['history_amount']))

    if kind == 'output_not_in_logs':
        return (
            '%s: %s product=%d not supported by any log (feasible: %s)' % (
                kind, discrepancy['task_id'],
                discrepancy['output_lines'],
                _format_list(discrepancy['feasible_amounts'])))

    if kind == 'platform_double_counts_retries':
        return (
            '%s: platform=%d equals the naive stderr sum; accepted=%s'
            ', feasible=%s' % (
                kind, discrepancy['platform_total'],
                _dash_if_zero(discrepancy['accepted_total']),
                _format_list(discrepancy['feasible_totals'])))

    return '%s: %r' % (kind, discrepancy)


def _dash_if_none(value):
    return '-' if value is None else str(value)


def _dash_if_zero(value):
    return '-' if value == 0 else str(value)


def _format_list(values):
    return ', '.join(str(value) for value in values) if values else '-'


def _make_arg_parser():
    arg_parser = argparse.ArgumentParser(
        prog='mrjob reconcile-counters',
        description='Offline reconciliation of a warehouse counter against'
                    ' task stderr logs and the job history.')

    arg_parser.add_argument('--group', required=True,
                            help='counter group to reconcile')
    arg_parser.add_argument('--counter', required=True,
                            help='counter name to reconcile')
    arg_parser.add_argument('--history', action='append',
                            help='job history file (.jhist or pre-YARN)')
    arg_parser.add_argument('--task-log', action='append',
                            help='task stderr file or directory of them')
    arg_parser.add_argument('--output', action='append',
                            help='accepted product lines as TASK_ID=LINES')
    arg_parser.add_argument('--platform-total', type=int, default=None,
                            help='counter total exported by the platform')
    arg_parser.add_argument('--json', dest='as_json', action='store_true',
                            help='print the raw report as JSON')
    arg_parser.add_argument('paths', nargs='*',
                            help='files or directories to scan recursively')

    return arg_parser


if __name__ == '__main__':
    raise SystemExit(main())
