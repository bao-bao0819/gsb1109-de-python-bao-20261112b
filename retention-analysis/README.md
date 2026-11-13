# 客户留存/流失判定（覆盖度感知）

两个月的消费记录都可以用 `q` 查询，但“上月有、本月无”不等于流失——某门店本月可能
根本没有交齐数据。本项目结合**每月各店覆盖清单**，把客户划分为确定留存、确定进入、
确定退出（流失）和证据不足四类，并对无法缩小的不确定性给出人数区间。支持跨店消费、
文件内重复记录，并保留从任一分类追溯到原始 CSV 物理行的完整链路。

## 判定口径

覆盖度只影响对“本月缺席”的解释；只要本月在**任一门店**观测到消费，观测本身就是
留存证据，与该店是否交齐无关。

| 分类 | 条件 |
|---|---|
| `retained_confirmed` 确定留存 | 上月有记录，且本月在任一店有消费（含跨店） |
| `new_confirmed` 确定进入 | 上月无任何记录，本月有消费 |
| `churn_confirmed` 确定退出 | 上月有记录、本月全平台无消费，且其上月消费过的**每个**门店本月都已交齐 |
| `insufficient_evidence` 证据不足 | 上月有记录、本月无消费，但至少一个上月门店本月未交齐（或缺登记） |

证据不足客户的可能标签是 `churn_or_retained`：若他在缺交门店仍有消费则为留存，
否则为流失。不确定性**不外推**——缺交只影响与该门店有历史关系的客户。

## 无法缩小的不确定区间

设证据不足人数为 U、确定流失为 C、确定留存为 R、确定进入为 N：

- 流失人数：`[C, C+U]`
- 留存人数：`[R, R+U]`（证据不足客户上月都存在，若仍活跃只可能是留存，不会变成新客）
- 新客人数：`[N, N]`（基于已观测交易，无覆盖歧义）

这些结果物化在 `uncertainty_bounds` 表，逐客户的可能标签在 `customer_classification.possible_labels`。

## 输入数据约定

- 交易 CSV：表头至少含 `month, store_id, customer_id`，可选 `amount` 等其他列；
  同一人可在多店出现，同一 人×月×店 允许重复行（会折叠并计数）。
- 覆盖清单 CSV：表头 `month, store_id, status`，`status = complete` 视为交齐，
  其他取值视为未交齐；同月同店出现冲突登记时按**未交齐**保守处理并进数据质量表。
- 分隔符默认逗号，可用环境变量 `DELIM='|'` 等覆盖。

## 运行方式

```bash
# 端到端：CSV -> q 入库 -> 分类模型 -> 汇总/区间/CSV 导出
./scripts/run_all.sh 2026-09 2026-10 \
  sample_data/coverage.csv \
  sample_data/transactions_2026-09.csv \
  sample_data/transactions_2026-10.csv

# 也可分步执行
./scripts/import_raw_db.sh work/raw_transactions.sqlite work/raw_coverage.sqlite \
  sample_data/coverage.csv sample_data/transactions_2026-09.csv sample_data/transactions_2026-10.csv
python3 scripts/build_model.py work/raw_transactions.sqlite work/raw_coverage.sqlite \
  work/retention_analysis.sqlite 2026-09 2026-10

# 自检（合成边界数据，断言分类/区间/溯源/去重）
../.venv/bin/python tests/selftest.py
```

## 输出（`work/retention_analysis.sqlite`，同时在 `work/exports/` 导出同名 CSV）

| 表 | 内容 |
|---|---|
| `customer_classification` | 每客户分类、可能标签、两月门店、判定理由 |
| `category_summary` | 四类各自人数 |
| `uncertainty_bounds` | 流失/留存/新客的不可缩小上下界 |
| `customer_record_trace` | 客户 → 每条原始记录的来源文件与物理行号 |
| `source_records` | 全量原始记录（含来源文件、物理行号、金额） |
| `tx_unique` | 人×月×店去重结果及折叠的原始行数、行号区间 |
| `coverage_status` | 月×店覆盖状态（冲突保守处理） |
| `dq_duplicate_coverage` | 覆盖清单重复/冲突登记 |
| `dq_missing_coverage` | 交易缺覆盖登记、门店从当月清单消失两类缺口 |
| `source_files` | q 表名到原始文件名的映射 |

常用查询见 `sql/example_queries.sql`，例如：

```bash
# 查某客户从分类到原始 CSV 行的完整证据链
q -H -O "SELECT month, store_id, source_file, source_rowid, amount
FROM work/retention_analysis.sqlite:::customer_record_trace
WHERE customer_id = 'C001' ORDER BY month, source_rowid"
```

## 设计要点

- **q 的角色**：用 `q -S` 把每个 CSV 落成 sqlite 原始表（保留 rowid），日常查数继续用
  `q` 直接查分析库。该 q 版本的解析器不支持 CTE（会把 CTE 名误判为文件名），模型 SQL
  全部采用派生表写法，在标准 sqlite 3 引擎上执行。
- **溯源**：q 落库表的 `rowid + 1`（`-H` 跳过表头）即 CSV 物理行号；`source_files`
  逆向 q 的文件名归一化规则（`-`→`_dash_`、`.`→`_dot_` 等）还原来源文件名。
- **去重**：分析在 人×月×店 粒度去重，重复行计数保留在 `raw_record_count`，
  原始明细仍可通过 `customer_record_trace` 逐行回溯。
- **保守原则**：覆盖状态缺失或冲突一律按未交齐处理，宁入证据不足也不误判流失。

## 样例数据预期结果（8 个客户）

| 客户 | 分类 | 关键原因 |
|---|---|---|
| C001 | retained_confirmed | 上月 S1/S2，本月在 S2 消费（跨店） |
| C002 | churn_confirmed | 上月 S1/S2，两店本月均交齐且无消费 |
| C003 | new_confirmed | 上月无记录，本月 S1 消费 |
| C004 | insufficient_evidence | 仅 S3，S3 本月未交齐 |
| C005 | insufficient_evidence | S1 交齐但 S3 未交齐，本月无消费 |
| C006 | new_confirmed | 上月无记录（S4 上月未交齐不影响新客判定） |
| C007 | retained_confirmed | 本月 S2 有消费（重复行已折叠） |
| C008 | retained_confirmed | 上月 S1，本月跨店到 S3 消费 |

汇总：留存 3、新客 2、流失 1、证据不足 2；流失区间 `[1,3]`、留存区间 `[3,5]`、
新客区间 `[2,2]`。
