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
"""Build a pair of fixture log trees and reconcile them offline.

This demonstrates the core gotcha behind a platform counter that doesn't
match the product: two stderr logs that are byte-for-byte identical can be
either two attempts of one logical split (a retry, where only one attempt
commits) or two separate splits (where both commit). The task stderr cannot
tell those apart -- only the job history can.

It writes three scenarios under a directory (a temp directory by default):

* ``retry/``           -- one split, failed attempt + retry, history present
* ``two_splits/``      -- the same two stderr logs, two committed splits
* ``missing_history/`` -- the retry logs with no history, so the totals that
                          can actually hold are reported instead of a guess

Run it with::

    PYTHONPATH=. python mrjob/examples/mr_reconcile_counters_demo.py
"""
from __future__ import print_function

import json
import os
import tempfile

from mrjob.tools.reconcile_counters import main as reconcile_main


GROUP = 'Warehouse'
COUNTER = 'Lines Read'

JOB = '202601010000_0001'
APPLICATION = 'application_%s' % JOB

TASK_0 = 'task_%s_m_000000' % JOB
TASK_1 = 'task_%s_m_000001' % JOB

ATTEMPT_0_0 = 'attempt_%s_m_000000_0' % JOB
ATTEMPT_0_1 = 'attempt_%s_m_000000_1' % JOB
ATTEMPT_1_0 = 'attempt_%s_m_000001_0' % JOB

CONTAINER_0 = 'container_%s_01_000000' % JOB
CONTAINER_1 = 'container_%s_01_000001' % JOB

# both attempts emit the exact same stderr, including the human-readable
# "读取 800 行" line; only reporter:counter lines are counters
STDERR_800 = (
    'reporter:counter:%s,%s,800\n'
    u'读取 800 行\n' % (GROUP, COUNTER)
).encode('utf_8')


def _counters(amount):
    return {
        'name': 'COUNTERS',
        'groups': [{
            'name': GROUP,
            'displayName': GROUP,
            'counts': [{
                'name': COUNTER,
                'displayName': COUNTER,
                'value': amount,
            }],
        }],
    }


def _jhist(record_type, event_key, event):
    return json.dumps({'type': record_type, 'event': {event_key: event}})


def _task_finished(task_id, amount):
    return _jhist(
        'TASK_FINISHED',
        'org.apache.hadoop.mapreduce.jobhistory.TaskFinished',
        {'taskid': task_id, 'taskType': 'MAP', 'status': 'SUCCEEDED',
         'counters': _counters(amount)})


def _attempt_failed(attempt_id, task_id):
    return _jhist(
        'MAP_ATTEMPT_FAILED',
        'org.apache.hadoop.mapreduce.jobhistory'
        '.TaskAttemptUnsuccessfulCompletion',
        {'taskid': task_id, 'attemptId': attempt_id,
         'status': 'FAILED', 'error': 'boom'})


def _attempt_started(attempt_id, task_id, container_id):
    return _jhist(
        'MAP_ATTEMPT_STARTED',
        'org.apache.hadoop.mapreduce.jobhistory.TaskAttemptStarted',
        {'taskid': task_id, 'attemptId': attempt_id,
         'containerId': container_id})


def _write(path, contents):
    directory = os.path.dirname(path)
    if not os.path.isdir(directory):
        os.makedirs(directory)

    mode = 'wb' if isinstance(contents, bytes) else 'w'
    with open(path, mode) as f:
        f.write(contents)


def _jhist_name():
    return (
        'job_%s-1700000000000-hadoop-MRJOB-1700000001000-1-1'
        '-SUCCEEDED-default-1700000002000.jhist' % JOB)


def _write_task_logs(base_dir, container_a, container_b):
    for container_id in (container_a, container_b):
        _write(os.path.join(base_dir, 'userlogs', APPLICATION,
                            container_id, 'stderr'),
               STDERR_800)


def _write_pre_yarn_task_logs(base_dir, attempt_a, attempt_b):
    # pre-YARN userlogs dirs are named after the attempt ID, so the path
    # itself reveals that both attempts belong to one logical task
    for attempt_id in (attempt_a, attempt_b):
        _write(os.path.join(base_dir, 'userlogs', attempt_id, 'stderr'),
               STDERR_800)


def build_fixtures(base_dir):
    """Create the three fixture trees under *base_dir*.

    Returns a map from scenario name to its root directory.
    """
    scenarios = {}

    # scenario 1: one split, container 0 failed and retried as container 1
    retry_dir = os.path.join(base_dir, 'retry')
    _write_task_logs(retry_dir, CONTAINER_0, CONTAINER_1)
    _write(os.path.join(retry_dir, 'history', _jhist_name()),
           '\n'.join([
               _attempt_started(ATTEMPT_0_0, TASK_0, CONTAINER_0),
               _attempt_started(ATTEMPT_0_1, TASK_0, CONTAINER_1),
               _attempt_failed(ATTEMPT_0_0, TASK_0),
               _task_finished(TASK_0, 800),
           ]) + '\n')
    scenarios['retry'] = retry_dir

    # scenario 2: byte-identical stderr logs, but two committed splits
    two_splits_dir = os.path.join(base_dir, 'two_splits')
    _write_task_logs(two_splits_dir, CONTAINER_0, CONTAINER_1)
    _write(os.path.join(two_splits_dir, 'history', _jhist_name()),
           '\n'.join([
               _attempt_started(ATTEMPT_0_0, TASK_0, CONTAINER_0),
               _attempt_started(ATTEMPT_1_0, TASK_1, CONTAINER_1),
               _task_finished(TASK_0, 800),
               _task_finished(TASK_1, 800),
           ]) + '\n')
    scenarios['two_splits'] = two_splits_dir

    # scenario 3: the retry logs, but the history is gone. Use the pre-YARN
    # userlogs layout, where attempt IDs in the paths still identify the
    # logical split; the only missing fact is which attempt committed.
    missing_history_dir = os.path.join(base_dir, 'missing_history')
    _write_pre_yarn_task_logs(
        missing_history_dir, ATTEMPT_0_0, ATTEMPT_0_1)
    scenarios['missing_history'] = missing_history_dir

    return scenarios


def _run(label, root, platform_total):
    print('=' * 72)
    print('%s (logs under %s)' % (label, root))
    print('=' * 72)

    reconcile_main([
        '--group', GROUP,
        '--counter', COUNTER,
        '--platform-total', str(platform_total),
        root,
    ])

    print('')


def main(out_dir=None):
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix='mrjob-reconcile-demo-')

    scenarios = build_fixtures(out_dir)

    # the platform exported the naive sum of both stderr logs in every case
    _run('scenario 1: retry of one logical split',
         scenarios['retry'], 1600)
    _run('scenario 2: two separate logical splits',
         scenarios['two_splits'], 1600)
    _run('scenario 3: retry logs with no commit record',
         scenarios['missing_history'], 1600)

    return out_dir


if __name__ == '__main__':
    print('fixtures and reports written under: %s\n' % main())
