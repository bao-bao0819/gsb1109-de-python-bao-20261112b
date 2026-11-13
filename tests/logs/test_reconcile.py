# -*- encoding: utf-8 -*-
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
import json

from mrjob.logs.reconcile import COMMITTED
from mrjob.logs.reconcile import FAILED
from mrjob.logs.reconcile import OUTPUT_MISMATCH
from mrjob.logs.reconcile import OUTPUT_NOT_IN_LOGS
from mrjob.logs.reconcile import PLATFORM_DOUBLE_COUNTS_RETRIES
from mrjob.logs.reconcile import PLATFORM_MISMATCH
from mrjob.logs.reconcile import STDERR_HISTORY_MISMATCH
from mrjob.logs.reconcile import UNKNOWN
from mrjob.logs.reconcile import CounterReconciler

from tests.sandbox import BasicTestCase


GROUP = 'Warehouse'
COUNTER = 'Lines Read'

JOB = '202601010000_0001'
TASK_0 = 'task_%s_m_000000' % JOB
TASK_1 = 'task_%s_m_000001' % JOB


def _attempt(task_num, attempt_num):
    return 'attempt_%s_m_%06d_%d' % (JOB, task_num, attempt_num)


def _task(task_num):
    return 'task_%s_m_%06d' % (JOB, task_num)


def _container(container_num):
    # container_<ts>_<step>_<attempt>_<container>
    return 'container_%s_01_%06d' % (JOB, container_num)


def _pre_yarn_stderr_path(attempt_id):
    return '/logs/userlogs/%s/stderr' % attempt_id


def _yarn_stderr_path(container_id):
    return '/logs/userlogs/application_%s/%s/stderr' % (JOB, container_id)


def _stderr(amount):
    # the human-readable line is emitted by every attempt; only the
    # reporter line is a counter
    return (
        'reporter:counter:%s,%s,%d\n'
        u'读取 %d 行\n'
        % (GROUP, COUNTER, amount, amount)
    ).encode('utf_8')


def _counter_record(group, counter, value):
    return {
        'name': 'COUNTERS',
        'groups': [{
            'name': group,
            'displayName': group,
            'counts': [{
                'name': counter,
                'displayName': counter,
                'value': value,
            }],
        }],
    }


def _jhist(record_type, event_key, event):
    return json.dumps({'type': record_type, 'event': {event_key: event}})


def _task_finished_line(task_id, amount):
    return _jhist(
        'TASK_FINISHED',
        'org.apache.hadoop.mapreduce.jobhistory.TaskFinished',
        {'taskid': task_id, 'taskType': 'MAP', 'status': 'SUCCEEDED',
         'counters': _counter_record(GROUP, COUNTER, amount)})


def _attempt_failed_line(attempt_id, task_id):
    return _jhist(
        'MAP_ATTEMPT_FAILED',
        'org.apache.hadoop.mapreduce.jobhistory'
        '.TaskAttemptUnsuccessfulCompletion',
        {'taskid': task_id, 'attemptId': attempt_id,
         'status': 'FAILED', 'error': 'boom'})


def _attempt_started_line(attempt_id, task_id, container_id):
    return _jhist(
        'MAP_ATTEMPT_STARTED',
        'org.apache.hadoop.mapreduce.jobhistory.TaskAttemptStarted',
        {'taskid': task_id, 'attemptId': attempt_id,
         'containerId': container_id})


def _pre_yarn_task_line(task_id, amount):
    return (
        'Task TASKID="%s" TASK_TYPE="MAP" TASK_STATUS="SUCCESS"'
        ' COUNTERS="{(%s)(%s)[(%s)(%s)(%d)]}" .\n'
        % (task_id, GROUP, GROUP, COUNTER, COUNTER, amount))


def _pre_yarn_failed_line(attempt_id, task_id):
    return (
        'MapAttempt TASK_TYPE="MAP" TASKID="%s" TASK_ATTEMPT_ID="%s"'
        ' TASK_STATUS="FAILED" ERROR="boom" .\n'
        % (task_id, attempt_id))


def _kinds(discrepancies):
    return [d['kind'] for d in discrepancies]


class ReconcilerTestCase(BasicTestCase):

    def test_retried_map_task_dedupes_attempts(self):
        # the failing attempt and the retry both read 800 lines
        failed_attempt = _attempt(0, 0)
        retry_attempt = _attempt(0, 1)
        other_attempt = _attempt(1, 0)

        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(failed_attempt), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(retry_attempt), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(other_attempt), _stderr(200))

        reconciler.add_yarn_history([
            _attempt_failed_line(failed_attempt, TASK_0),
            _task_finished_line(TASK_0, 800),
            _task_finished_line(TASK_1, 200),
        ])

        report = reconciler.report()

        # attempts of the same split are classified distinctly
        task_0 = report['tasks'][TASK_0]
        self.assertEqual(
            [a['status'] for a in task_0['attempts']],
            [FAILED, COMMITTED])
        self.assertEqual(task_0['accepted_amount'], 800)
        self.assertEqual(task_0['committed_attempt_ids'], [retry_attempt])

        task_1 = report['tasks'][TASK_1]
        self.assertEqual(
            [a['status'] for a in task_1['attempts']], [COMMITTED])
        self.assertEqual(task_1['accepted_amount'], 200)

        # the two 800-line logs must not both be counted
        totals = report['totals']
        self.assertEqual(totals['naive_stderr_sum'], 1800)
        self.assertEqual(totals['failed_stderr_sum'], 800)
        self.assertEqual(totals['accepted'], 1000)
        self.assertEqual(totals['feasible'], [1000])
        self.assertEqual(report['missing_commit_records'], [])

        self.assertEqual(
            reconciler.sum_history_counters(),
            {GROUP: {COUNTER: 1000}})

    def test_platform_counter_that_double_counts_retries(self):
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 1)), _stderr(800))

        reconciler.add_yarn_history([
            _attempt_failed_line(_attempt(0, 0), TASK_0),
            _task_finished_line(TASK_0, 800),
        ])

        # platform exported the naive stderr sum
        report = reconciler.report(platform_total=1600)
        self.assertEqual(
            _kinds(report['discrepancies']),
            [PLATFORM_DOUBLE_COUNTS_RETRIES])

        # the de-duplicated total matches the platform
        clean_report = reconciler.report(platform_total=800)
        self.assertEqual(clean_report['discrepancies'], [])

        # some other wrong number is a plain mismatch
        mismatch = reconciler.report(platform_total=1234)
        self.assertEqual(_kinds(mismatch['discrepancies']),
                         [PLATFORM_MISMATCH])

    def test_human_readable_line_is_not_a_counter(self):
        # mrjob's parser only treats reporter:counter: lines as counters;
        # the "读取 800 行" line lands in 'other'
        reconciler = CounterReconciler(GROUP, COUNTER)
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))

        record = reconciler.evidence[0]
        self.assertEqual(record['amount'], 800)
        self.assertEqual(
            record['other'],
            [u'读取 800 行\n'])

    def test_missing_commit_record_lists_feasible_totals_only(self):
        # no history at all: both logs exist, either could have committed
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 1)), _stderr(800))

        report = reconciler.report()

        self.assertEqual(report['missing_commit_records'], [TASK_0])
        self.assertEqual(
            [a['status'] for a in report['tasks'][TASK_0]['attempts']],
            [UNKNOWN, UNKNOWN])

        totals = report['totals']
        # never the 1600 naive sum: one split contributes at most 800
        self.assertEqual(totals['naive_stderr_sum'], 1600)
        self.assertEqual(totals['feasible'], [800])

        # platform's 1600 is provably the double-counted stderr sum
        platform_report = reconciler.report(platform_total=1600)
        self.assertEqual(
            _kinds(platform_report['discrepancies']),
            [PLATFORM_DOUBLE_COUNTS_RETRIES])

    def test_missing_commit_record_combined_feasible_totals(self):
        # two ambiguous splits: 800 vs (200 or 300) -> 1000 or 1100
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 1)), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(1, 0)), _stderr(200))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(1, 1)), _stderr(300))

        totals = reconciler.report()['totals']

        self.assertEqual(totals['naive_stderr_sum'], 2100)
        self.assertEqual(totals['feasible'], [1000, 1100])

    def test_accepted_output_resolves_missing_commit_record(self):
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 1)), _stderr(800))

        # product row count is known even though the history is gone
        reconciler.add_output(TASK_0, 800)

        report = reconciler.report()
        self.assertEqual(report['tasks'][TASK_0]['accepted_amount'], 800)
        self.assertEqual(report['totals']['feasible'], [800])
        self.assertEqual(report['discrepancies'], [])

    def test_output_lines_disagree_with_counter(self):
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 1)), _stderr(800))
        reconciler.add_yarn_history([
            _attempt_failed_line(_attempt(0, 0), TASK_0),
            _task_finished_line(TASK_0, 800),
        ])

        reconciler.add_output(TASK_0, 750)

        report = reconciler.report()
        self.assertEqual(
            _kinds(report['discrepancies']), [OUTPUT_MISMATCH])
        self.assertEqual(
            report['discrepancies'][0]['output_lines'], 750)

    def test_output_not_supported_by_any_log(self):
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))
        reconciler.add_output(TASK_0, 700)

        discrepancies = reconciler.report()['discrepancies']
        self.assertEqual(_kinds(discrepancies), [OUTPUT_NOT_IN_LOGS])
        self.assertEqual(discrepancies[0]['feasible_amounts'], [800])

    def test_stderr_disagrees_with_history_counter(self):
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))
        reconciler.add_yarn_history([
            _task_finished_line(TASK_0, 799),
        ])

        discrepancies = reconciler.report()['discrepancies']
        self.assertEqual(
            _kinds(discrepancies), [STDERR_HISTORY_MISMATCH])
        self.assertEqual(discrepancies[0]['stderr_amount'], 800)
        self.assertEqual(discrepancies[0]['history_amount'], 799)

    def test_identical_logs_different_commit_histories(self):
        # the two stderr files are byte-identical; only the commit history
        # decides whether they are retries of one split or two splits
        stderr_a = _stderr(800)
        stderr_b = _stderr(800)
        self.assertEqual(stderr_a, stderr_b)

        # history A: one logical split, first attempt failed and retried
        reconciler_a = CounterReconciler(GROUP, COUNTER)
        reconciler_a.add_task_log(
            'case-a/attempt-zero/stderr', stderr_a,
            attempt_id=_attempt(0, 0))
        reconciler_a.add_task_log(
            'case-a/attempt-one/stderr', stderr_b,
            attempt_id=_attempt(0, 1))
        reconciler_a.add_yarn_history([
            _attempt_failed_line(_attempt(0, 0), TASK_0),
            _task_finished_line(TASK_0, 800),
        ])

        report_a = reconciler_a.report()
        self.assertEqual(report_a['totals']['accepted'], 800)
        self.assertEqual(report_a['totals']['naive_stderr_sum'], 1600)
        self.assertEqual(
            [a['status'] for a in report_a['tasks'][TASK_0]['attempts']],
            [FAILED, COMMITTED])

        # history B: same log contents, but two successful splits
        reconciler_b = CounterReconciler(GROUP, COUNTER)
        reconciler_b.add_task_log(
            'case-b/attempt-zero/stderr', stderr_a,
            attempt_id=_attempt(0, 0))
        reconciler_b.add_task_log(
            'case-b/attempt-one/stderr', stderr_b,
            attempt_id=_attempt(1, 0))
        reconciler_b.add_yarn_history([
            _task_finished_line(TASK_0, 800),
            _task_finished_line(TASK_1, 800),
        ])

        report_b = reconciler_b.report()
        self.assertEqual(report_b['totals']['accepted'], 1600)
        self.assertEqual(
            [a['status'] for a in
             report_b['tasks'][TASK_0]['attempts']],
            [COMMITTED])
        self.assertEqual(
            [a['status'] for a in
             report_b['tasks'][TASK_1]['attempts']],
            [COMMITTED])

    def test_yarn_container_logs_linked_via_history(self):
        # logs are ingested first, when only container IDs are known
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _yarn_stderr_path(_container(0)), _stderr(800))
        reconciler.add_task_log(
            _yarn_stderr_path(_container(1)), _stderr(800))
        reconciler.add_task_log(
            _yarn_stderr_path(_container(2)), _stderr(200))

        # without a history, nothing can be placed
        unlinked = reconciler.report()['unlinked_logs']
        self.assertEqual(len(unlinked), 3)
        self.assertEqual(reconciler.report()['tasks'], {})

        reconciler.add_yarn_history([
            _attempt_started_line(
                _attempt(0, 0), TASK_0, _container(0)),
            _attempt_started_line(
                _attempt(0, 1), TASK_0, _container(1)),
            _attempt_started_line(
                _attempt(1, 0), TASK_1, _container(2)),
            _attempt_failed_line(_attempt(0, 0), TASK_0),
            _task_finished_line(TASK_0, 800),
            _task_finished_line(TASK_1, 200),
        ])

        report = reconciler.report()
        self.assertEqual(report['unlinked_logs'], [])
        self.assertEqual(
            [a['status'] for a in report['tasks'][TASK_0]['attempts']],
            [FAILED, COMMITTED])
        self.assertEqual(report['totals']['accepted'], 1000)
        self.assertEqual(report['totals']['naive_stderr_sum'], 1800)

    def test_yarn_container_logs_without_history_stay_unlinked(self):
        # YARN container dirs don't encode the task number, so without a
        # history there is no proof these two logs belong to one split.
        # They stay unlinked rather than being guessed to be retries.
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _yarn_stderr_path(_container(0)), _stderr(800))
        reconciler.add_task_log(
            _yarn_stderr_path(_container(1)), _stderr(800))

        report = reconciler.report()

        self.assertEqual(len(report['unlinked_logs']), 2)
        self.assertEqual(report['tasks'], {})
        self.assertEqual(report['totals']['feasible'], [1600])
        self.assertEqual(report['totals']['unlinked_stderr_sum'], 1600)

    def test_pre_yarn_history(self):
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)), _stderr(800))
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 1)), _stderr(800))

        reconciler.add_pre_yarn_history([
            _pre_yarn_failed_line(_attempt(0, 0), TASK_0),
            _pre_yarn_task_line(TASK_0, 800),
        ])

        report = reconciler.report()
        self.assertEqual(
            [a['status'] for a in report['tasks'][TASK_0]['attempts']],
            [FAILED, COMMITTED])
        self.assertEqual(report['totals']['accepted'], 800)
        self.assertEqual(report['totals']['naive_stderr_sum'], 1600)

    def test_unrecognized_path_requires_explicit_attempt_id(self):
        reconciler = CounterReconciler(GROUP, COUNTER)

        self.assertRaises(
            ValueError,
            reconciler.add_task_log, 'not/a/task/log.txt', _stderr(800))

        # explicit attempt id keeps it usable
        reconciler.add_task_log(
            'not/a/task/log.txt', _stderr(800), attempt_id=_attempt(0, 0))
        self.assertEqual(reconciler.evidence[0]['task_id'], TASK_0)

    def test_log_without_the_reconciled_counter(self):
        reconciler = CounterReconciler(GROUP, COUNTER)

        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 0)),
            b'reporter:counter:Other,Thing,5\n')
        reconciler.add_task_log(
            _pre_yarn_stderr_path(_attempt(0, 1)), _stderr(800))

        report = reconciler.report()
        amounts = [
            a['amount'] for a in report['tasks'][TASK_0]['attempts']]
        self.assertEqual(amounts, [None, 800])
        # None amounts don't pollute the naive sum or feasible set
        self.assertEqual(report['totals']['naive_stderr_sum'], 800)
        self.assertEqual(report['totals']['feasible'], [800])
