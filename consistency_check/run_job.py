"""命令行入口：选择三个作业之一并交给 mrjob 执行。

用法::

    python -m consistency_check.run_job --job arrival --runner inline \
        consistency_check/data/orders.tsv
    python -m consistency_check.run_job --job order-id --runner inline \
        part-1.gz part-0.gz
    python -m consistency_check.run_job --job timestamp --runner local \
        consistency_check/data/orders.tsv

``--job`` 取值：

* ``arrival``  : 无平局规则（默认，用于重现不稳定）；
* ``order-id`` : 显式次排序，平局规则=订单号升序；
* ``timestamp``: 显式次排序，平局规则=(时间戳, 订单号) 升序。

``--job`` 由本脚本消费，其余参数原样传给 MRJob。
"""
import argparse
import os
import sys

from consistency_check.jobs import JOB_CLASSES


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--job', choices=sorted(JOB_CLASSES),
                        default='arrival')
    args, rest = parser.parse_known_args(argv)

    # 让 local/hadoop 子进程重新启动本文件时能选到同一个作业类
    os.environ['FIRST_ORDER_JOB'] = args.job

    sys.argv = [sys.argv[0]] + rest
    JOB_CLASSES[args.job].run()


if __name__ == '__main__':
    main()
