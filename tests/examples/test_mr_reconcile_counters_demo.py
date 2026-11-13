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
"""Test the offline counter reconciliation demo."""
from __future__ import print_function

import os
from os.path import join

from mrjob.examples.mr_reconcile_counters_demo import GROUP
from mrjob.examples.mr_reconcile_counters_demo import COUNTER
from mrjob.examples.mr_reconcile_counters_demo import build_fixtures
from mrjob.examples.mr_reconcile_counters_demo import main as demo_main
from mrjob.logs.reconcile import CounterReconciler
from mrjob.py2 import StringIO

from tests.py2 import patch
from tests.sandbox import SandboxedTestCase


class ReconcileCountersDemoTestCase(SandboxedTestCase):

    def _reconcile(self, root):
        reconciler = CounterReconciler(GROUP, COUNTER)

        history_path = None
        task_log_paths = []

        for dirpath, _, filenames in os.walk(root):
            for filename in filenames:
                path = join(dirpath, filename)

                if filename.endswith('.jhist'):
                    history_path = path
                elif filename == 'stderr':
                    task_log_paths.append(path)

        if history_path:
            with open(history_path) as f:
                reconciler.add_yarn_history(f.readlines())

        for path in task_log_paths:
            with open(path, 'rb') as f:
                reconciler.add_task_log(path, f.read())

        return reconciler.report(platform_total=1600)

    def test_identical_logs_yield_different_accepted_totals(self):
        scenarios = build_fixtures(self.tmp_dir)

        retry_report = self._reconcile(scenarios['retry'])
        two_splits_report = self._reconcile(scenarios['two_splits'])
        missing_report = self._reconcile(scenarios['missing_history'])

        # one split: only one 800 is committed
        self.assertEqual(retry_report['totals']['accepted'], 800)
        self.assertEqual(
            retry_report['totals']['naive_stderr_sum'], 1600)
        self.assertTrue(retry_report['discrepancies'])

        # two splits: both 800s are committed
        self.assertEqual(two_splits_report['totals']['accepted'], 1600)
        self.assertEqual(
            two_splits_report['totals']['naive_stderr_sum'], 1600)
        self.assertFalse(two_splits_report['discrepancies'])

        # no history: the single feasible total is 800, never 1600
        self.assertEqual(missing_report['totals']['accepted'], 0)
        self.assertEqual(missing_report['totals']['feasible'], [800])
        self.assertTrue(missing_report['missing_commit_records'])
        self.assertTrue(missing_report['discrepancies'])

    def test_demo_main_runs_and_prints_all_scenarios(self):
        out_dir = join(self.tmp_dir, 'demo')

        stdout = StringIO()
        with patch('sys.stdout', stdout):
            result = demo_main(out_dir)

        output = stdout.getvalue()

        self.assertEqual(result, out_dir)
        self.assertIn('scenario 1', output)
        self.assertIn('scenario 2', output)
        self.assertIn('scenario 3', output)
        self.assertIn('platform_double_counts_retries', output)
