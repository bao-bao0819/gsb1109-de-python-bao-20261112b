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
"""Test the offline reconcile-counters tool."""
from __future__ import print_function

import json
import os
from os.path import join

from mrjob.py2 import StringIO
from mrjob.tools.reconcile_counters import main

from tests.py2 import patch
from tests.sandbox import SandboxedTestCase


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
CONTAINER_2 = 'container_%s_01_000002' % JOB


def _stderr(amount):
    return (
        'reporter:counter:%s,%s,%d\n'
        u'读取 %d 行\n'
        % (GROUP, COUNTER, amount, amount)
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


class ReconcileCountersToolTestCase(SandboxedTestCase):

    def _make_yarn_log_tree(self):
        root = self.tmp_dir

        history_name = (
            'job_%s-1700000000000-hadoop-MRJOB-1700000001000-1-1'
            '-SUCCEEDED-default-1700000002000.jhist' % JOB)
        history_path = join(root, 'history', 'done', history_name)

        history_lines = [
            _jhist(
                'MAP_ATTEMPT_STARTED',
                'org.apache.hadoop.mapreduce.jobhistory.TaskAttemptStarted',
                {'taskid': TASK_0, 'attemptId': ATTEMPT_0_0,
                 'containerId': CONTAINER_0}),
            _jhist(
                'MAP_ATTEMPT_STARTED',
                'org.apache.hadoop.mapreduce.jobhistory.TaskAttemptStarted',
                {'taskid': TASK_0, 'attemptId': ATTEMPT_0_1,
                 'containerId': CONTAINER_1}),
            _jhist(
                'MAP_ATTEMPT_STARTED',
                'org.apache.hadoop.mapreduce.jobhistory.TaskAttemptStarted',
                {'taskid': TASK_1, 'attemptId': ATTEMPT_1_0,
                 'containerId': CONTAINER_2}),
            _jhist(
                'MAP_ATTEMPT_FAILED',
                'org.apache.hadoop.mapreduce.jobhistory'
                '.TaskAttemptUnsuccessfulCompletion',
                {'taskid': TASK_0, 'attemptId': ATTEMPT_0_0,
                 'status': 'FAILED', 'error': 'boom'}),
            _jhist(
                'TASK_FINISHED',
                'org.apache.hadoop.mapreduce.jobhistory.TaskFinished',
                {'taskid': TASK_0, 'taskType': 'MAP',
                 'status': 'SUCCEEDED', 'counters': _counters(800)}),
            _jhist(
                'TASK_FINISHED',
                'org.apache.hadoop.mapreduce.jobhistory.TaskFinished',
                {'taskid': TASK_1, 'taskType': 'MAP',
                 'status': 'SUCCEEDED', 'counters': _counters(200)}),
        ]

        self.makefile(history_path,
                      '\n'.join(history_lines) + '\n')

        self.makefile(
            join(root, 'userlogs', APPLICATION, CONTAINER_0, 'stderr'),
            _stderr(800))
        self.makefile(
            join(root, 'userlogs', APPLICATION, CONTAINER_1, 'stderr'),
            _stderr(800))
        self.makefile(
            join(root, 'userlogs', APPLICATION, CONTAINER_2, 'stderr'),
            _stderr(200))

        return root

    def _run(self, extra_args):
        stdout = StringIO()

        with patch('sys.stdout', stdout):
            returncode = main(extra_args)

        return returncode, stdout.getvalue()

    def test_directory_scan_detects_double_counted_platform_total(self):
        root = self._make_yarn_log_tree()

        returncode, output = self._run([
            '--group', GROUP,
            '--counter', COUNTER,
            '--platform-total', '1800',
            root,
        ])

        self.assertEqual(returncode, 1)
        self.assertIn('platform_double_counts_retries', output)
        self.assertIn('naive sum of every stderr log : 1800', output)
        self.assertIn('accepted (de-duplicated)      : 1000', output)
        self.assertIn('failed', output)
        self.assertIn('committed', output)

    def test_correct_platform_total_exits_clean(self):
        root = self._make_yarn_log_tree()

        returncode, output = self._run([
            '--group', GROUP,
            '--counter', COUNTER,
            '--platform-total', '1000',
            root,
        ])

        self.assertEqual(returncode, 0)
        self.assertIn('no discrepancies', output)

    def test_json_report(self):
        root = self._make_yarn_log_tree()

        returncode, output = self._run([
            '--group', GROUP,
            '--counter', COUNTER,
            '--json',
            root,
        ])

        report = json.loads(output)

        self.assertEqual(returncode, 0)
        self.assertEqual(report['totals']['naive_stderr_sum'], 1800)
        self.assertEqual(report['totals']['accepted'], 1000)
        self.assertEqual(report['totals']['feasible'], [1000])
        self.assertEqual(
            report['tasks'][TASK_0]['committed_attempt_ids'],
            [ATTEMPT_0_1])

    def test_missing_history_reports_feasible_totals_only(self):
        # pre-YARN layout: attempt IDs come from the paths themselves
        root = self.tmp_dir

        self.makefile(
            join(root, 'userlogs', ATTEMPT_0_0, 'stderr'), _stderr(800))
        self.makefile(
            join(root, 'userlogs', ATTEMPT_0_1, 'stderr'), _stderr(800))

        returncode, output = self._run([
            '--group', GROUP,
            '--counter', COUNTER,
            '--platform-total', '1600',
            root,
        ])

        self.assertEqual(returncode, 1)
        self.assertIn('platform_double_counts_retries', output)
        self.assertIn('feasible grand totals         : 800', output)
        self.assertIn('naive sum of every stderr log : 1600', output)
        self.assertIn('missing commit records', output)

    def test_explicit_flags_accept_a_task_log_directory(self):
        root = self._make_yarn_log_tree()
        history_path = None

        for dirpath, _, filenames in os.walk(join(root, 'history')):
            for filename in filenames:
                history_path = join(dirpath, filename)

        returncode, output = self._run([
            '--group', GROUP,
            '--counter', COUNTER,
            '--history', history_path,
            '--task-log', join(root, 'userlogs'),
            '--platform-total', '1800',
        ])

        self.assertEqual(returncode, 1)
        self.assertIn('platform_double_counts_retries', output)
        self.assertIn('accepted (de-duplicated)      : 1000', output)

    def test_explicit_output_matches_product_rows(self):
        root = self._make_yarn_log_tree()

        returncode, output = self._run([
            '--group', GROUP,
            '--counter', COUNTER,
            '--output', '%s=800' % TASK_0,
            '--output', '%s=200' % TASK_1,
            root,
        ])

        self.assertEqual(returncode, 0)
        self.assertIn('accepted product lines: 800', output)
        self.assertIn('no discrepancies', output)
