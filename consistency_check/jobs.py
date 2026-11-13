"""三个"每客户第一笔订单"作业，平局规则严格性依次增强。

输入为 TSV 行：``customer_id<TAB>order_id<TAB>created_at``。

* :class:`MRFirstOrderByArrival` 不声明任何平局规则，reducer 直接取同键
  值序列的第一个。inline 下因为单进程、单分片、稳定排序而"碰巧稳定"；
  分布式下同键值序不受保证，结果可能变化。
* :class:`MRFirstOrderTieByOrderId` 显式开启 ``SORT_VALUES``（次排序），
  声明平局规则为订单号字典序（订单号定宽零填充，字典序即数值序）。
* :class:`MRFirstOrderTieByTimestamp` 同样显式次排序，平局规则为
  下单时间戳（ISO 8601 定宽，字典序即时间序），时间戳并列时回退订单号。

每个作业同时给出纯函数形式的 mapper / reducer / combiner（``*_fn``），
供 :mod:`consistency_check.engine` 直接复用；mrjob 作业方法也调用同一批
函数，保证参考引擎与真实作业跑的是同一份业务逻辑。
"""

import shlex
import sys
import types


def _install_stdlib_shims():
    # 本仓库的 mrjob 快照面向 Python <=3.12；Python 3.13+ 移除了 pipes，
    # 而 pipes.quote 自 3.3 起就是 shlex.quote 的别名。local runner 会把
    # 本文件作为独立脚本启动，因此垫片直接内联在这里，不依赖包内导入。
    try:
        import pipes
    except ImportError:
        pipes = None

    if pipes is None:
        pipes_shim = types.ModuleType('pipes')
        pipes_shim.quote = shlex.quote
        sys.modules['pipes'] = pipes_shim


_install_stdlib_shims()

from mrjob.job import MRJob


def parse_line(line):
    customer_id, order_id, created_at = line.rstrip('\n').split('\t')
    return customer_id, order_id, created_at


# --- 作业 1：无平局规则，隐式依赖同键值的到达顺序 -------------------------

def arrival_mapper_fn(record):
    customer_id, order_id, created_at = record
    yield customer_id, (order_id, created_at)


def arrival_combiner_fn(key, values):
    # combiner 也取"见到的第一个"。框架不保证 combiner 被调用，
    # 因此它修不了顺序问题，只会让结果再依赖一层 combiner 调度。
    order_id, created_at = next(iter(values))
    yield key, (order_id, created_at)


def arrival_reducer_fn(key, values):
    order_id, _ = next(iter(values))
    yield key, order_id


# --- 作业 2：显式次排序，平局规则 = 订单号升序 ----------------------------

def order_id_mapper_fn(record):
    customer_id, order_id, _ = record
    # 值只放订单号：JSON 编码后的字典序就是声明的平局次序。
    yield customer_id, order_id


def order_id_reducer_fn(key, values):
    yield key, next(iter(values))


def order_id_tie_fn(key, value):
    # value 本身即订单号。
    return value


# --- 作业 3：显式次排序，平局规则 = (时间戳升序, 订单号升序) --------------

def timestamp_mapper_fn(record):
    customer_id, order_id, created_at = record
    # 值为 (时间戳, 订单号)：JSON 编码后先比时间戳、再比订单号，
    # 与声明的平局规则一致。
    yield customer_id, (created_at, order_id)


def timestamp_reducer_fn(key, values):
    _, order_id = next(iter(values))
    yield key, order_id


def timestamp_tie_fn(key, value):
    # value 本身即 (时间戳, 订单号)，元组字典序即声明的平局规则。
    return value


class MRFirstOrderByArrival(MRJob):
    """无平局规则：reducer 隐式依赖同键值的到达顺序。"""

    def mapper(self, _, line):
        for pair in arrival_mapper_fn(parse_line(line)):
            yield pair

    def combiner(self, key, values):
        for pair in arrival_combiner_fn(key, values):
            yield pair

    def reducer(self, key, values):
        for pair in arrival_reducer_fn(key, values):
            yield pair


class MRFirstOrderTieByOrderId(MRJob):
    """显式次排序，平局规则：同键值按订单号升序，取首个。"""

    SORT_VALUES = True

    def mapper(self, _, line):
        for pair in order_id_mapper_fn(parse_line(line)):
            yield pair

    def reducer(self, key, values):
        for pair in order_id_reducer_fn(key, values):
            yield pair


class MRFirstOrderTieByTimestamp(MRJob):
    """显式次排序，平局规则：同键值按时间戳升序，再按订单号升序。"""

    SORT_VALUES = True

    def mapper(self, _, line):
        for pair in timestamp_mapper_fn(parse_line(line)):
            yield pair

    def reducer(self, key, values):
        for pair in timestamp_reducer_fn(key, values):
            yield pair


# local/hadoop runner 会把本文件作为独立脚本重新启动，靠 __main__ 决定
# 运行哪个作业类。run_job 用环境变量 FIRST_ORDER_JOB 传递选择，子进程
# 继承该变量；缺省为 arrival。
JOB_CLASSES = {
    'arrival': MRFirstOrderByArrival,
    'order-id': MRFirstOrderTieByOrderId,
    'timestamp': MRFirstOrderTieByTimestamp,
}


if __name__ == '__main__':
    import os

    JOB_CLASSES[os.environ.get('FIRST_ORDER_JOB', 'arrival')].run()
