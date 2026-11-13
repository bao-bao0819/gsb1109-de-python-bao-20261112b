#!/usr/bin/env python3
"""
基于 q 落库的两个原始 sqlite 文件构建客户留存/流失分类模型。

输入:
  raw_tx.sqlite   q -S 生成，每个交易 CSV 对应一张原始表，rowid 即数据行顺序
  raw_cov.sqlite  q -S 生成，覆盖清单原始表
  analysis.sqlite 输出的分析库（全部物化为表，可用 q 直接查询）
  prev_month, curr_month  对比的两个月（与 CSV 中 month 列取值一致）

分类口径（覆盖度只影响“缺席”的推断，不影响已观测到的消费）:
  retained_confirmed     上月有、本月任一店有消费（观测即证据）
  new_confirmed          上月无、本月有消费
  churn_confirmed        上月有、本月全平台无消费，且其上月消费过的所有店
                         在本月都已交齐（缺席才是可推断的真缺席）
  insufficient_evidence  上月有、本月无消费，但至少一个上月门店本月未交齐，
                         无法区分“流失”与“在缺交门店继续消费”

不确定区间按客户给出可能标签集合，并在汇总中输出无法缩小的人数范围。
"""

import csv
import os
import sqlite3
import sys

COMPLETE = "complete"


def denormalize(qtable: str) -> str:
    """逆向 q 的文件名->表名归一化，仅用于展示，可能不唯一。"""
    return (
        qtable.replace("_slash_", "/")
        .replace("_backslash_", "\\")
        .replace("_colon_", ":")
        .replace("_space_", " ")
        .replace("_dash_", "-")
        .replace("_dot_", ".")
        .replace("_qm_", "?")
        .replace("_plus_", "+")
    )


def table_names(con: sqlite3.Connection) -> list:
    return [
        r[0]
        for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def require_columns(con: sqlite3.Connection, table: str, expected: set) -> None:
    cols = {r[1] for r in con.execute(f'PRAGMA table_info("{table}")')}
    missing = expected - cols
    if missing:
        raise SystemExit(f"表 {table} 缺少列: {sorted(missing)}; 实际列: {sorted(cols)}")


def main() -> int:
    if len(sys.argv) != 6:
        print(
            "usage: build_model.py <raw_tx.sqlite> <raw_cov.sqlite> "
            "<analysis.sqlite> <prev_month> <curr_month>",
            file=sys.stderr,
        )
        return 2

    raw_tx_path, raw_cov_path, out_path, prev_month, curr_month = sys.argv[1:]
    for p in (raw_tx_path, raw_cov_path):
        if not os.path.exists(p):
            raise SystemExit(f"找不到原始库: {p}")
    if os.path.exists(out_path):
        os.remove(out_path)

    tx_con = sqlite3.connect(raw_tx_path)
    cov_con = sqlite3.connect(raw_cov_path)
    out = sqlite3.connect(out_path)

    tx_tables = table_names(tx_con)
    cov_tables = table_names(cov_con)
    if not tx_tables:
        raise SystemExit("交易库中没有任何表")
    if len(cov_tables) != 1:
        raise SystemExit(f"覆盖清单库应只有一张表，实际: {cov_tables}")
    cov_table = cov_tables[0]

    for t in tx_tables:
        require_columns(tx_con, t, {"month", "store_id", "customer_id"})
    require_columns(cov_con, cov_table, {"month", "store_id", "status"})

    out.execute("ATTACH DATABASE ? AS raw_tx", (raw_tx_path,))
    out.execute("ATTACH DATABASE ? AS raw_cov", (raw_cov_path,))

    # 1. 来源文件清单（q 表名 -> 原始文件名）
    out.execute(
        "CREATE TABLE source_files (file_kind TEXT, qtable TEXT PRIMARY KEY, "
        "source_file TEXT)"
    )
    out.executemany(
        "INSERT INTO source_files VALUES ('transaction', ?, ?)",
        [(t, denormalize(t)) for t in tx_tables],
    )
    out.execute(
        "INSERT INTO source_files VALUES ('coverage', ?, ?)",
        (cov_table, denormalize(cov_table)),
    )

    # 2. 原始记录级溯源表：物理行号 = q 表 rowid + 1（-H 跳过表头）
    union_parts = []
    for t in tx_tables:
        cols = {r[1] for r in tx_con.execute(f'PRAGMA table_info("{t}")')}
        extra = ", amount" if "amount" in cols else ", NULL AS amount"
        union_parts.append(
            f"SELECT (SELECT source_file FROM source_files WHERE qtable='{t}') "
            f"AS source_file, rowid + 1 AS source_rowid, month, store_id, "
            f"customer_id{extra} FROM raw_tx.\"{t}\""
        )
    out.execute("CREATE TABLE source_records AS " + " UNION ALL ".join(union_parts))

    # 3. 覆盖清单（按月×店去重；只要出现过非 complete 登记就按未交齐处理，
    #    冲突或重复登记进数据质量表，避免 JOIN 扇出）
    out.execute(
        "CREATE TABLE coverage_status AS "
        "SELECT month, store_id, "
        "GROUP_CONCAT(DISTINCT status) AS status_values, "
        f"CASE WHEN SUM(CASE WHEN status = '{COMPLETE}' THEN 0 ELSE 1 END) = 0 "
        "THEN 1 ELSE 0 END AS is_complete "
        f"FROM raw_cov.\"{cov_table}\" GROUP BY month, store_id"
    )
    out.execute(
        "CREATE TABLE dq_duplicate_coverage AS "
        f"SELECT month, store_id, COUNT(*) AS row_count, "
        "COUNT(DISTINCT status) AS distinct_status_count "
        f"FROM raw_cov.\"{cov_table}\" "
        "GROUP BY month, store_id HAVING COUNT(*) > 1"
    )

    # 4. 交易去重：同一 customer × month × store 折叠为一条，保留行号区间
    out.execute(
        "CREATE TABLE tx_unique AS "
        "SELECT customer_id, month, store_id, "
        "GROUP_CONCAT(DISTINCT source_file) AS source_files, "
        "MIN(source_rowid) AS first_source_rowid, "
        "MAX(source_rowid) AS last_source_rowid, "
        "COUNT(*) AS raw_record_count "
        "FROM source_records GROUP BY customer_id, month, store_id"
    )

    # 5. 数据质量：覆盖缺口。两类：
    #    a) 交易引用了覆盖清单里不存在的 月×店
    #    b) 上月清单里的店在当月清单整条缺失（无法判断当月是否交齐）
    out.execute(
        "CREATE TABLE dq_missing_coverage AS "
        "SELECT u.month, u.store_id, 'transaction_without_coverage_entry' AS gap_kind, "
        "COUNT(DISTINCT u.customer_id) AS affected_customers "
        "FROM tx_unique u "
        "LEFT JOIN coverage_status c ON c.month = u.month AND c.store_id = u.store_id "
        "WHERE c.is_complete IS NULL GROUP BY u.month, u.store_id "
        "UNION ALL "
        f"SELECT '{curr_month}' AS month, p.store_id, "
        "'store_dropped_from_current_coverage' AS gap_kind, 0 AS affected_customers "
        "FROM (SELECT DISTINCT store_id FROM coverage_status WHERE month = ?) p "
        "WHERE NOT EXISTS (SELECT 1 FROM coverage_status c "
        f"WHERE c.month = '{curr_month}' AND c.store_id = p.store_id)",
        (prev_month,),
    )

    # 6. 客户 × 门店的两月观测与覆盖证据
    #    注意：门店当月覆盖状态与客户在该行的月份无关，直接取该门店的覆盖标记，
    #    否则“上月消费、本月缺席”的行永远拿不到本月覆盖证据。
    out.execute(
        "CREATE TABLE customer_store_evidence AS "
        "SELECT u.customer_id, u.store_id, "
        f"MAX(CASE WHEN u.month = '{prev_month}' THEN 1 ELSE 0 END) AS seen_prev, "
        f"MAX(CASE WHEN u.month = '{curr_month}' THEN 1 ELSE 0 END) AS seen_curr, "
        "MAX(cp.is_complete) AS prev_store_complete, "
        "MAX(cc.is_complete) AS curr_store_complete "
        "FROM tx_unique u "
        f"LEFT JOIN coverage_status cp ON cp.month = '{prev_month}' "
        "AND cp.store_id = u.store_id "
        f"LEFT JOIN coverage_status cc ON cc.month = '{curr_month}' "
        "AND cc.store_id = u.store_id "
        "GROUP BY u.customer_id, u.store_id"
    )

    # 7. 客户级汇总证据
    out.execute(
        "CREATE TABLE customer_evidence AS "
        "SELECT customer_id, "
        "MAX(seen_prev) AS seen_prev, "
        "MAX(seen_curr) AS seen_curr, "
        "SUM(CASE WHEN seen_curr = 1 AND curr_store_complete = 1 THEN 1 ELSE 0 END) "
        "AS curr_complete_store_count, "
        "SUM(CASE WHEN seen_prev = 1 AND COALESCE(curr_store_complete, 0) = 0 "
        "THEN 1 ELSE 0 END) AS prev_stores_incomplete_curr, "
        "GROUP_CONCAT(DISTINCT CASE WHEN seen_prev = 1 THEN store_id END) AS prev_stores, "
        "GROUP_CONCAT(DISTINCT CASE WHEN seen_curr = 1 THEN store_id END) AS curr_stores "
        "FROM customer_store_evidence GROUP BY customer_id"
    )

    # 8. 分类 + 每客户可能标签集合（不确定性不外推，只在缺交覆盖范围内）
    out.execute(
        """
        CREATE TABLE customer_classification AS
        SELECT customer_id,
            CASE
                WHEN seen_prev = 1 AND seen_curr = 1 THEN 'retained_confirmed'
                WHEN seen_prev = 0 AND seen_curr = 1 THEN 'new_confirmed'
                WHEN seen_prev = 1 AND seen_curr = 0
                     AND prev_stores_incomplete_curr = 0 THEN 'churn_confirmed'
                ELSE 'insufficient_evidence'
            END AS category,
            CASE
                WHEN seen_prev = 1 AND seen_curr = 1 THEN 'retained'
                WHEN seen_prev = 0 AND seen_curr = 1 THEN 'new'
                WHEN seen_prev = 1 AND seen_curr = 0
                     AND prev_stores_incomplete_curr = 0 THEN 'churn'
                ELSE 'churn_or_retained'
            END AS possible_labels,
            seen_prev, seen_curr, curr_complete_store_count,
            prev_stores_incomplete_curr, prev_stores, curr_stores,
            CASE
                WHEN seen_prev = 1 AND seen_curr = 1
                     THEN 'observed transaction in current month'
                WHEN seen_prev = 0 AND seen_curr = 1
                     THEN 'no prior-month record, observed in current month'
                WHEN seen_prev = 1 AND seen_curr = 0
                     AND prev_stores_incomplete_curr = 0
                     THEN 'absent at every fully-reporting current-month store'
                ELSE 'absent current month but a prior store has incomplete coverage'
            END AS reason
        FROM customer_evidence
        """
    )

    # 9. 客户 -> 原始记录溯源表（从分类一路追到 CSV 物理行）
    out.execute(
        "CREATE TABLE customer_record_trace AS "
        "SELECT cl.category, cl.customer_id, r.month, r.store_id, "
        "r.source_file, r.source_rowid, r.amount "
        "FROM customer_classification cl "
        "JOIN source_records r ON r.customer_id = cl.customer_id "
        "ORDER BY cl.category, cl.customer_id, r.month, r.source_rowid"
    )

    # 10. 分类汇总
    out.execute(
        "CREATE TABLE category_summary AS "
        "SELECT category, COUNT(*) AS customer_count "
        "FROM customer_classification GROUP BY category"
    )

    # 11. 不可缩小的不确定区间
    churn_fixed = out.execute(
        "SELECT COUNT(*) FROM customer_classification WHERE category='churn_confirmed'"
    ).fetchone()[0]
    retained_fixed = out.execute(
        "SELECT COUNT(*) FROM customer_classification WHERE category='retained_confirmed'"
    ).fetchone()[0]
    new_fixed = out.execute(
        "SELECT COUNT(*) FROM customer_classification WHERE category='new_confirmed'"
    ).fetchone()[0]
    unknown = out.execute(
        "SELECT COUNT(*) FROM customer_classification WHERE category='insufficient_evidence'"
    ).fetchone()[0]
    out.execute(
        "CREATE TABLE uncertainty_bounds (metric TEXT, lower_bound INTEGER, "
        "upper_bound INTEGER, note TEXT)"
    )
    rows = [
        ("churn_count", churn_fixed, churn_fixed + unknown,
         "unknowns may still be active at stores with incomplete current coverage"),
        ("retained_count", retained_fixed, retained_fixed + unknown,
         "unknowns were present last month; if active they are retained, not new"),
        ("new_count", new_fixed, new_fixed,
         "new entrants rest on observed transactions, no coverage ambiguity"),
        ("insufficient_evidence_count", unknown, unknown,
         "headcount of customers whose label cannot be narrowed further"),
    ]
    out.executemany("INSERT INTO uncertainty_bounds VALUES (?, ?, ?, ?)", rows)

    # 12. 导出 CSV（与分析库表内容一致，便于不装工具时查看）
    export_dir = os.path.join(os.path.dirname(out_path) or ".", "exports")
    os.makedirs(export_dir, exist_ok=True)
    for table in (
        "customer_classification",
        "category_summary",
        "uncertainty_bounds",
        "customer_record_trace",
        "coverage_status",
        "dq_duplicate_coverage",
        "dq_missing_coverage",
    ):
        path = os.path.join(export_dir, f"{table}.csv")
        cursor = out.execute(f'SELECT * FROM "{table}"')
        cols = [d[0] for d in cursor.description]
        with open(path, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(cols)
            writer.writerows(cursor.fetchall())

    out.commit()
    out.close()
    tx_con.close()
    cov_con.close()
    print(f"analysis db: {out_path}")
    print(f"csv exports: {export_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
