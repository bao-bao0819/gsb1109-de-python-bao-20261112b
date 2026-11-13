"""只保留真实顺序保证的 MapReduce 参考引擎。

引擎按 mrjob/Hadoop streaming 的实际约束建模，刻意不提供任何框架
并不保证的顺序。一次"调度"(schedule) 由三部分刻画，恰好对应
允许变化的三件事：

* ``splits``      : mapper 分片，即输入行的一种有序划分；分片内部保持
                    输入行序（单分片内 mapper 按行处理是真实保证），
                    分片之间没有顺序承诺；
* ``merge_order`` : reducer 拉取各 mapper 输出做归并时的段优先级。
                    Hadoop 的 k 路归并在 key 相等时按段顺序取段，效果等价于
                    "按 merge_order 拼接各分片输出，再对 key 做稳定排序"；
                    段优先级由框架决定，作业不能依赖；
* ``combine_flags``: 每个分片是否运行 combiner。combiner 是优化项，
                    框架不保证调用（可能 0 次，也可能每分片 1 次）。

shuffle 只按 key 排序（与 ``sim._sort_lines_in_memory`` 的
``sort(key=...)`` 和 ``sort -k1,1 -s`` 一致），是**稳定**排序：同键记录
保持归并后的相对顺序。partitioner 只决定 key 落在哪个 reducer，整组同键
记录一起搬运，不改变组内顺序，因此不单独建模。

当作业显式声明次排序时（``tie`` 传入次键函数），同键内按声明的平局规则
全排序，结果与 merge_order / 分片无关。
"""

from itertools import groupby
from itertools import permutations
from itertools import product


def run_pipeline(records, mapper, reducer, *, splits, merge_order=None,
                 combine_flags=None, combiner=None, tie='stable'):
    """按给定调度跑一遍 map-combine-shuffle-reduce。

    :param records: 输入记录列表（按文件中的真实行序）
    :param mapper:  f(record) -> 可迭代的 (key, value)
    :param reducer: f(key, [value, ...]) -> 可迭代结果
    :param splits:  分片方案，形如 ``[[行索引...], [行索引...]]``；
                    每个内层列表是一个 mapper 分片，索引在分片内保持
                    输入行序；所有行索引恰好出现一次
    :param merge_order: 分片 id 的一个排列，给出 reducer 归并的段优先级；
                    None 表示按分片编号顺序
    :param combine_flags: 与 splits 等长的布尔列表，True 表示该分片运行
                    combiner；None 表示全部不运行
    :param combiner: 与 reducer 同签名的可选 combiner
    :param tie: ``'stable'`` 表示只按 key 稳定排序；否则应为
                    ``f(key, value) -> 次排序键``，模拟显式次排序
    :return: 输出记录列表，顺序即 reducer 实际吐数据的顺序
    """
    _validate_splits(splits, len(records))

    num_splits = len(splits)
    if merge_order is None:
        merge_order = list(range(num_splits))
    if sorted(merge_order) != list(range(num_splits)):
        raise ValueError('merge_order 必须是分片 id 的一个排列')

    if combine_flags is None:
        combine_flags = [False] * num_splits
    if len(combine_flags) != num_splits:
        raise ValueError('combine_flags 必须与 splits 等长')
    if any(combine_flags) and combiner is None:
        raise ValueError('请求了 combiner 但没有提供 combiner 函数')

    # map 阶段（含可选 combiner）：每个分片产出一个有序 (key, value) 序列。
    per_split_outputs = []
    for split_rows, use_combine in zip(splits, combine_flags):
        pairs = []
        for row_index in split_rows:
            pairs.extend(mapper(records[row_index]))

        if use_combine:
            pairs = _run_combiner(pairs, combiner)

        per_split_outputs.append(pairs)

    # shuffle：按段优先级拼接，再按 key 稳定排序。
    # 同键记录因此按"段优先级 + 段内发射序"排列，这正是框架唯一的
    # 隐式行为，而它依赖的 merge_order 并不受作业控制。
    shuffled = []
    for split_id in merge_order:
        shuffled.extend(per_split_outputs[split_id])

    if tie == 'stable':
        shuffled.sort(key=lambda kv: kv[0])
    else:
        # 显式次排序：同键内按声明的平局规则排定，任何调度下结果一致。
        shuffled.sort(key=lambda kv: (kv[0], tie(kv[0], kv[1])))

    # reduce 阶段：按 key 分组（与 MRJob._combine_or_reduce_pairs 一样
    # 用 itertools.groupby，依赖上游已按键排好序）。
    output = []
    for key, grouped in groupby(shuffled, key=lambda kv: kv[0]):
        values = [value for _, value in grouped]
        output.extend(reducer(key, values))

    return output


def consecutive_split_plans(num_records):
    """枚举所有"连续切分"方案：保持行序，在任意行边界切开。"""
    if num_records == 0:
        return [[]]

    plans = []
    for mask in range(1 << (num_records - 1)):
        splits = []
        start = 0
        for boundary in range(num_records - 1):
            if mask & (1 << boundary):
                splits.append(list(range(start, boundary + 1)))
                start = boundary + 1
        splits.append(list(range(start, num_records)))
        plans.append(splits)
    return plans


def legal_schedules(num_records, with_combiner=False):
    """生成一份输入的全部合法调度（连续分片 × 归并优先级 × combiner 开关）。

    *with_combiner* 为 False 时（作业没有定义 combiner），combiner 开关
    只有全 False 一种。规模随行数指数增长，仅适用于缩小后的最小输入
    （个位数行）。
    """
    combine_choices = (False, True) if with_combiner else (False,)
    schedules = []
    for splits in consecutive_split_plans(num_records):
        num_splits = len(splits)
        for merge_order in permutations(range(num_splits)):
            for combine_flags in product(combine_choices, repeat=num_splits):
                schedules.append(
                    (splits, list(merge_order), list(combine_flags)))
    return schedules


def distinct_results(records, mapper, reducer, *, combiner=None,
                     tie='stable'):
    """跑全部合法调度，返回 ``{结果元组: 见证调度}`` 字典。"""
    seen = {}
    schedules = legal_schedules(
        len(records), with_combiner=combiner is not None)
    for splits, merge_order, combine_flags in schedules:
        result = tuple(
            run_pipeline(
                records, mapper, reducer,
                splits=splits, merge_order=merge_order,
                combine_flags=combine_flags,
                combiner=combiner, tie=tie))
        if result not in seen:
            seen[result] = (splits, merge_order, combine_flags)
    return seen


def _run_combiner(pairs, combiner):
    """在单个 mapper 分片的有序输出上跑一次 combiner。"""
    combined = []
    for key, grouped in groupby(pairs, key=lambda kv: kv[0]):
        values = [value for _, value in grouped]
        combined.extend(combiner(key, values))
    return combined


def _validate_splits(splits, num_records):
    flattened = [row_index for split in splits for row_index in split]
    if sorted(flattened) != list(range(num_records)):
        raise ValueError(
            'splits 必须是输入行索引的一个划分：每个索引恰好出现一次')
    for split in splits:
        if split != sorted(split):
            raise ValueError('分片内部必须保持输入行序（只允许改变切分边界）')
