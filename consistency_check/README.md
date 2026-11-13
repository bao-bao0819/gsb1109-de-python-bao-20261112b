# “第一笔订单”一致性检查

## 问题

本地用 inline runner 跑“每个客户的第一笔订单”结果稳定，换成分布式（真实
Hadoop / mrjob 的 local runner）同一个客户却可能选出另一个单号。根因是
**reducer 隐式依赖了同键值（同一客户的多笔订单）的到达顺序，而这个顺序
框架并不保证**。

mrjob 的模拟实现把这件事暴露得很清楚：

- `mrjob/sim.py` 的 `_sort_lines_in_memory` 默认只按 key 做
  `list.sort(key=...)`——**稳定**排序，同键记录保留进入 shuffle 前的相对顺序；
- `mrjob/local.py` 对应的是 `sort -t $'\t' -k1,1 -s`，`-s` 同样是稳定排序；
- 同键记录进入 shuffle 前的顺序，取决于 mapper 分片如何切分、reducer 拉取
  各分片输出做 k 路归并时的段优先级，以及 combiner 在哪些分片上被调用——
  这三者都由框架决定，作业无法依赖。

inline 下单进程、通常单分片、单文件，输入行序被一路稳定保留，所以“碰巧
稳定”；分布式下分片和归并顺序一变，同键值序就变。

## 目录内容

- `engine.py`：只保留真实顺序保证的参考引擎。一次调度（schedule）由
  `splits`（mapper 分片，分片内行序不变）、`merge_order`（reducer 归并的
  段优先级）、`combine_flags`（各分片是否运行 combiner）三部分组成；
  shuffle 是对 key 的稳定排序，`tie` 给出作业显式声明的次排序键。
- `jobs.py`：三个 mrjob 作业，业务逻辑同时以纯函数（`*_fn`）给出，引擎和
  真实作业共用同一份逻辑：
  - `MRFirstOrderByArrival`：无平局规则，reducer 取同键值第一个（问题作业）；
  - `MRFirstOrderTieByOrderId`：`SORT_VALUES=True`，平局规则=订单号升序；
  - `MRFirstOrderTieByTimestamp`：`SORT_VALUES=True`，平局规则=(时间戳,
    订单号) 升序。
- `data/orders.tsv`：**最小重现输入，2 行**，同一客户、订单号序与时间序
  相反（1 行不存在平局，故 2 行是可证明的最小集）。
- `data/orders_large.tsv`：7 行的较大输入，供收缩演示。
- `check_consistency.py`：端到端检查脚本。
- `run_job.py`：作业的命令行入口。

## 最小输入与两份合法调度

`data/orders.tsv`：

```
C1	O-0002	2026-01-01T09:00:00Z
C1	O-0001	2026-01-10T09:00:00Z
```

对无平局规则的作业，下面两份调度都合法（都只做了框架允许的变化，没有
破坏任何真实顺序保证），但结果不同：

| 调度 | mapper 分片 | reducer 归并优先级 | combiner | 结果 |
|------|-------------|--------------------|----------|------|
| A（inline 形态） | `[[0,1]]` 单分片 | `[0]` | 不运行 | `C1 -> O-0002` |
| B（分布式形态） | `[[0],[1]]` 每行一分片 | `[1,0]` 段1优先 | 不运行 | `C1 -> O-0001` |

`check_consistency.py` 用真实 `InlineMRJobRunner` 复现了这对调度：把两行
分别写进两个**不可切分的 `.gz`**（压缩文件不切分，各成一个 mapper 分片），
仅调换输入路径的先后顺序——两种顺序都合法，无规则作业的输出就在
`O-0002` / `O-0001` 间翻转；两个声明了次排序的作业输出不变。

## 次排序作业按其声明的约束变换

显式 `SORT_VALUES=True` 的作业，shuffle 同键内按声明的平局规则全排序
（配合 `KeyFieldBasedPartitioner -k1,1` 保证同键进同一 reducer），参考引擎
在所有 3 种合法调度下都只得到 1 种结果。注意平局规则必须是**全序**：
若业务只声明粗粒度规则（例如只按自然日），规则未覆盖的维度（日内先后）
仍是残余平局，测试 `test_coarse_declared_tie_rule_keeps_residual_ambiguity`
演示了这种情况仍会漂移——需要把平局规则补成完整次序（如再追加订单号）。

## 运行

```bash
# 端到端检查（参考引擎枚举 + 真实 mrjob 复现 + delta-debugging 收缩）
python -m consistency_check.check_consistency

# 直接跑某个作业
python -m consistency_check.run_job --job arrival   -r inline consistency_check/data/orders.tsv
python -m consistency_check.run_job --job order-id  -r inline consistency_check/data/orders.tsv
python -m consistency_check.run_job --job timestamp -r inline consistency_check/data/orders.tsv

# 测试
python -m pytest tests/test_first_order_consistency.py -q
```

`jobs.py` 内联了一个最小的 `pipes` 垫片（`pipes.quote` 即 `shlex.quote`），
让这份面向 Python ≤3.12 的 mrjob 快照能在 Python 3.13+ 上运行；`distutils`
由环境中的 setuptools 提供。

## 结论

- “取同键值第一个”不是确定性计算：它依赖框架不承诺的同键值序。
- 要让“第一笔订单”与调度无关，必须用次排序把同键值序变成声明出来的全序
  （`SORT_VALUES` + 定宽可字典序比较的值，或自定义 key/比较器），并保证
  平局规则覆盖到没有残余并列。
- 修复后无论 mapper 分片、reducer 归并优先级、combiner 调用方式如何变化，
  结果都一致。
