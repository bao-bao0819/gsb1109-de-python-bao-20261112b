"""端到端一致性检查脚本。

做四件事：

1. 在最小订单集上枚举**全部合法调度**（mapper 分片 × reducer 归并优先级
   × combiner 开关），统计三个作业各自有多少种结果；
2. 给无平局规则的作业附上两份都合法、但结果不同的调度；
3. 用真实 mrjob ``InlineMRJobRunner`` 复现：两份不可切分的 ``.gz`` 输入
   仅调换路径顺序（两种都合法的调度），无规则作业输出翻转，两个显式
   次排序作业输出不变；
4. 用 delta-debugging 把一份较大的订单集收缩到仍能重现差异的最小子集。

直接运行：``python -m consistency_check.check_consistency``
"""

import gzip
import logging
import os
from itertools import combinations

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

HERE = os.path.dirname(os.path.abspath(__file__))
MINIMAL_INPUT = os.path.join(HERE, 'data', 'orders.tsv')
LARGE_INPUT = os.path.join(HERE, 'data', 'orders_large.tsv')


def load_records(path):
    with open(path) as f:
        return [parse_line(line) for line in f if line.strip()]


def format_schedule(splits, merge_order, combine_flags):
    return (
        'mapper 分片=%s（行索引，0 = 输入第一行），'
        'reducer 归并优先级=%s，combiner 开关=%s'
        % (splits, merge_order, combine_flags))


def summarize(name, records, mapper, reducer, *, combiner=None, tie='stable'):
    outcomes = distinct_results(
        records, mapper, reducer, combiner=combiner, tie=tie)

    print('\n=== %s ===' % name)
    print('合法调度总数: %d，去重后结果数: %d'
          % (len(legal_schedules(len(records),
                                 with_combiner=combiner is not None)),
             len(outcomes)))
    for result, witness in outcomes.items():
        rendered = ', '.join('%s -> %s' % pair for pair in result)
        print('  结果 %s' % rendered)
        print('    见证调度: %s' % format_schedule(*witness))
    return outcomes


def minimize(records, mapper, reducer, *, combiner=None, tie='stable'):
    """保持行序，按子集长度递增搜索仍能产生多种结果的最小行子集。"""
    for size in range(1, len(records) + 1):
        for indices in combinations(range(len(records)), size):
            subset = [records[i] for i in indices]
            if len(distinct_results(
                    subset, mapper, reducer,
                    combiner=combiner, tie=tie)) > 1:
                return subset, list(indices)
    return None, None


def run_real_inline(job_cls, input_paths):
    """用真实 InlineMRJobRunner 跑一遍，返回 {客户: 订单号} 字典。"""
    job = job_cls(['-r', 'inline'] + list(input_paths))
    job.sandbox()
    with job.make_runner() as runner:
        runner.run()
        return dict(job.parse_output(runner.cat_output()))


def main():
    logging.basicConfig(level=logging.WARNING)
    logging.getLogger('mrjob.conf').setLevel(logging.ERROR)

    records = load_records(MINIMAL_INPUT)
    print('最小订单集（%d 行，行序即输入文件顺序）:' % len(records))
    for i, record in enumerate(records):
        print('  行%d: %s' % (i, '\t'.join(record)))

    # 1) 参考引擎：枚举全部合法调度
    arrival_outcomes = summarize(
        '作业1 无平局规则（reducer 取同键值第一个）',
        records, arrival_mapper_fn, arrival_reducer_fn,
        combiner=arrival_combiner_fn)

    order_id_outcomes = summarize(
        '作业2 显式次排序，平局规则=订单号升序',
        records, order_id_mapper_fn, order_id_reducer_fn,
        tie=order_id_tie_fn)

    timestamp_outcomes = summarize(
        '作业3 显式次排序，平局规则=(时间戳升序, 订单号升序)',
        records, timestamp_mapper_fn, timestamp_reducer_fn,
        tie=timestamp_tie_fn)

    # 2) 两份合法调度的对照（与真实 mrjob 的两种 .gz 路径顺序一一对应）
    print('\n=== 两份合法调度的逐步对照（作业1）===')
    schedule_a = ([[0, 1]], [0], [False])      # 单分片、无 combiner：inline 形态
    schedule_b = ([[0], [1]], [1, 0], [False, False])  # 双分片、归并时段1优先
    result_a = run_pipeline(
        records, arrival_mapper_fn, arrival_reducer_fn,
        splits=schedule_a[0], merge_order=schedule_a[1],
        combine_flags=schedule_a[2], combiner=arrival_combiner_fn)
    result_b = run_pipeline(
        records, arrival_mapper_fn, arrival_reducer_fn,
        splits=schedule_b[0], merge_order=schedule_b[1],
        combine_flags=schedule_b[2], combiner=arrival_combiner_fn)
    print('调度 A: %s' % format_schedule(*schedule_a))
    print('  => %s（等价 inline：单个 .gz 含全部行）' % result_a)
    print('调度 B: %s' % format_schedule(*schedule_b))
    print('  => %s（等价分布式：两个 .gz，reducer 先拉到第二个文件的分片）'
          % result_b)
    assert result_a != result_b, '两份调度应产生不同结果'

    # 3) 真实 mrjob：两个不可切分 .gz，仅调换输入路径顺序
    print('\n=== 真实 InlineMRJobRunner 复现 ===')
    import tempfile

    tmp_dir = tempfile.mkdtemp(prefix='first-order-check-')
    gz_paths = []
    for i, record in enumerate(records):
        path = os.path.join(tmp_dir, 'part-%d.gz' % i)
        with gzip.open(path, 'wb') as f:
            f.write(('\t'.join(record) + '\n').encode('utf-8'))
        gz_paths.append(path)

    order_ab = list(gz_paths)
    order_ba = list(reversed(gz_paths))

    real_arrival_a = run_real_inline(MRFirstOrderByArrival, order_ab)
    real_arrival_b = run_real_inline(MRFirstOrderByArrival, order_ba)
    print('作业1，输入顺序 part-0.gz,part-1.gz => %s' % real_arrival_a)
    print('作业1，输入顺序 part-1.gz,part-0.gz => %s' % real_arrival_b)
    assert real_arrival_a != real_arrival_b, '真实 mrjob 下结果应翻转'

    for job_cls, label in (
            (MRFirstOrderTieByOrderId, '作业2'),
            (MRFirstOrderTieByTimestamp, '作业3')):
        out_a = run_real_inline(job_cls, order_ab)
        out_b = run_real_inline(job_cls, order_ba)
        print('%s 两种输入顺序 => %s / %s（一致: %s）'
              % (label, out_a, out_b, out_a == out_b))
        assert out_a == out_b, '显式次排序作业不应受调度影响'

    # 4) 从大输入收缩到最小重现集
    print('\n=== delta-debugging 收缩 ===')
    large_records = load_records(LARGE_INPUT)
    minimal, indices = minimize(
        large_records, arrival_mapper_fn, arrival_reducer_fn,
        combiner=arrival_combiner_fn)
    print('大输入 %d 行 -> 最小重现子集 %d 行（原行索引 %s）:'
          % (len(large_records), len(minimal), indices))
    for record in minimal:
        print('  %s' % '\t'.join(record))

    # 结论断言
    assert len(arrival_outcomes) == 2, '无规则作业应有两种结果'
    assert len(order_id_outcomes) == 1, '订单号次排序应确定'
    assert len(timestamp_outcomes) == 1, '时间戳次排序应确定'
    assert minimal is not None and len(minimal) == 2, \
        '应能收缩到 2 行（同一客户的一对订单）'
    assert minimal[0][0] == minimal[1][0], '两行必须属于同一客户'
    assert len(distinct_results(
        minimal, arrival_mapper_fn, arrival_reducer_fn,
        combiner=arrival_combiner_fn)) == 2, '最小子集本身必须重现差异'

    print('\n全部检查通过：无平局规则的作业结果依赖调度；'
          '显式声明次排序与平局规则的作业在所有合法调度下结果一致。')


if __name__ == '__main__':
    main()
