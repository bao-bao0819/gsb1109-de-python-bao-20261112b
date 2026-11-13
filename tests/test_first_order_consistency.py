# Copyright 2026
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
"""一致性检查：每客户"第一笔订单"在不同合法调度下的行为。

覆盖三层证据：

1. 参考引擎枚举全部合法调度（mapper 分片 × reducer 归并优先级 ×
   combiner 开关），无平局规则的作业有两种结果，显式次排序作业只有一种；
2. 两份具名的合法调度对同一输入给出不同单号；
3. 真实 ``InlineMRJobRunner`` 上，两个不可切分的 ``.gz`` 仅调换输入
   路径顺序（两种都合法的调度），无规则作业翻转、次排序作业不变。
"""
import gzip
import os
from io import BytesIO

from consistency_check.engine import distinct_results
from consistency_check.engine import legal_schedules
from consistency_check.engine import run_pipeline
from consistency_check.jobs import MRFirstOrderByArrival
from consistency_check.jobs import MRFirstOrderTieByOrderId
from consistency_check.jobs import MRFirstOrderTieByTimestamp
from consistency_check.jobs import arrival_combiner_fn
from consistency_check.jobs import arrival_mapper_fn
from consistency_check.jobs import arrival_reducer_fn
from consistency_check.jobs import order_id_mapper_fn
from consistency_check.jobs import order_id_reducer_fn
from consistency_check.jobs import order_id_tie_fn
from consistency_check.jobs import parse_line
from consistency_check.jobs import timestamp_mapper_fn
from consistency_check.jobs import timestamp_reducer_fn
from consistency_check.jobs import timestamp_tie_fn

from tests.sandbox import BasicTestCase
from tests.sandbox import SandboxedTestCase

MINIMAL_TSV = (
    'C1\tO-0002\t2026-01-01T09:00:00Z\n'
    'C1\tO-0001\t2026-01-10T09:00:00Z\n'
)


def _records():
    return [parse_line(line) for line in MINIMAL_TSV.splitlines()]


class ReferenceEngineTestCase(BasicTestCase):

    def setUp(self):
        super(ReferenceEngineTestCase, self).setUp()
        self.records = _records()

    def test_no_tie_rule_has_two_legal_outcomes(self):
        outcomes = distinct_results(
            self.records, arrival_mapper_fn, arrival_reducer_fn,
            combiner=arrival_combiner_fn)

        winners = sorted(result[0][1] for result in outcomes)
        self.assertEqual(winners, ['O-0001', 'O-0002'])

    def test_named_schedules_witness_the_difference(self):
        # 调度 A：单 mapper 分片，保持输入行序（inline 的典型形态）
        result_a = run_pipeline(
            self.records, arrival_mapper_fn, arrival_reducer_fn,
            splits=[[0, 1]], merge_order=[0], combine_flags=[False],
            combiner=arrival_combiner_fn)

        # 调度 B：每行一个分片，reducer 归并时第二个分片优先
        # （分布式下 reducer 拉取顺序不受作业控制，完全合法）
        result_b = run_pipeline(
            self.records, arrival_mapper_fn, arrival_reducer_fn,
            splits=[[0], [1]], merge_order=[1, 0],
            combine_flags=[False, False],
            combiner=arrival_combiner_fn)

        self.assertEqual(result_a, [('C1', 'O-0002')])
        self.assertEqual(result_b, [('C1', 'O-0001')])

    def test_combiner_switch_is_part_of_every_schedule(self):
        # 2 行输入：单分片方案 1 种（2 个 combiner 开关）+
        # 双分片方案 1 种（2 种归并优先级 × 4 个 combiner 开关）= 10
        schedules = legal_schedules(2, with_combiner=True)
        self.assertEqual(len(schedules), 10)
        self.assertTrue(
            any(any(flags) for _, _, flags in schedules),
            '枚举中必须包含运行 combiner 的调度')

    def test_order_id_secondary_sort_is_deterministic(self):
        outcomes = distinct_results(
            self.records, order_id_mapper_fn, order_id_reducer_fn,
            tie=order_id_tie_fn)

        self.assertEqual(len(outcomes), 1)
        self.assertEqual(next(iter(outcomes)), (('C1', 'O-0001'),))

    def test_timestamp_secondary_sort_is_deterministic(self):
        outcomes = distinct_results(
            self.records, timestamp_mapper_fn, timestamp_reducer_fn,
            tie=timestamp_tie_fn)

        self.assertEqual(len(outcomes), 1)
        # 时间戳更早的是 O-0002；两种平局规则给出不同答案，
        # 说明引擎确实按各自声明的约束而不是统一地"取小订单号"。
        self.assertEqual(next(iter(outcomes)), (('C1', 'O-0002'),))

    def test_invalid_splits_are_rejected(self):
        self.assertRaises(
            ValueError,
            run_pipeline,
            self.records, arrival_mapper_fn, arrival_reducer_fn,
            splits=[[1], [1]])  # 行索引重复，不是合法划分

    def test_coarse_declared_tie_rule_keeps_residual_ambiguity(self):
        # 业务只声明"按自然日"的平局规则：同一天的两笔订单在该规则下
        # 不可区分。引擎只尊重声明出来的次排序键，规则没覆盖到的维度
        # （这里是日内先后/订单号）仍随框架的稳定排序与归并顺序漂移。
        # 注意这刻画的是"作业声明的业务约束不完整"；真实 mrjob 的
        # SORT_VALUES 会按整个编码值排序，要落地这种粗规则需自定义
        # 分组/比较器，而不能直接依赖整值编码。
        same_day = [
            parse_line('C1\tO-0002\t2026-01-01T09:00:00Z'),
            parse_line('C1\tO-0001\t2026-01-01T15:00:00Z'),
        ]

        def mapper(record):
            customer_id, order_id, created_at = record
            yield customer_id, (created_at[:10], order_id)

        def reducer(key, values):
            _, order_id = next(iter(values))
            yield key, order_id

        def tie_by_day(key, value):
            # 声明的次排序键只有日期，不含订单号：同日记录的次键相同。
            return value[0]

        outcomes = distinct_results(
            same_day, mapper, reducer, tie=tie_by_day)

        self.assertEqual(
            sorted(result[0][1] for result in outcomes),
            ['O-0001', 'O-0002'])

    def test_total_tie_rule_pins_every_permutation(self):
        # 对照：平局规则细化到订单号后，即使每行一个分片、任意归并优先级，
        # 结果都被钉死。
        for merge_order in ((0, 1), (1, 0)):
            result = run_pipeline(
                self.records, order_id_mapper_fn, order_id_reducer_fn,
                splits=[[0], [1]], merge_order=list(merge_order),
                combine_flags=[False, False],
                tie=order_id_tie_fn)
            self.assertEqual(result, [('C1', 'O-0001')])


class RealInlineRunnerTestCase(SandboxedTestCase):
    """用真实 mrjob 复现：两个 .gz 各成一个不可切分 mapper 分片。"""

    def _run_job(self, job_cls, input_paths):
        job = job_cls(['-r', 'inline'] + list(input_paths))
        job.sandbox(stdin=BytesIO(b''))
        with job.make_runner() as runner:
            runner.run()
            return dict(job.parse_output(runner.cat_output()))

    def _gz_inputs(self):
        paths = []
        for line in MINIMAL_TSV.splitlines():
            path = os.path.join(self.tmp_dir, 'part-%d.gz' % len(paths))
            with gzip.open(path, 'wb') as f:
                f.write((line + '\n').encode('utf-8'))
            paths.append(path)
        return paths

    def test_arrival_order_flips_with_input_path_order(self):
        paths = self._gz_inputs()

        first = self._run_job(MRFirstOrderByArrival, paths)
        second = self._run_job(MRFirstOrderByArrival, list(reversed(paths)))

        self.assertEqual(first, {'C1': 'O-0002'})
        self.assertEqual(second, {'C1': 'O-0001'})

    def test_secondary_sort_jobs_ignore_input_path_order(self):
        paths = self._gz_inputs()

        for job_cls, expected in (
                (MRFirstOrderTieByOrderId, {'C1': 'O-0001'}),
                (MRFirstOrderTieByTimestamp, {'C1': 'O-0002'})):
            forward = self._run_job(job_cls, paths)
            reversed_order = self._run_job(job_cls, list(reversed(paths)))

            self.assertEqual(forward, expected)
            self.assertEqual(reversed_order, expected)
