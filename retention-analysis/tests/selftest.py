#!/usr/bin/env python3
"""
端到端自检：合成覆盖各种边界的数据，跑 import_raw_db.sh + build_model.py，
断言分类、不确定区间、溯源行号与去重行为。可直接 python3 运行，也可被 pytest 收集。
"""

import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)
REPO = os.path.dirname(PROJECT)
IMPORT = os.path.join(PROJECT, "scripts", "import_raw_db.sh")
BUILD = os.path.join(PROJECT, "scripts", "build_model.py")
VENV_PY = os.path.join(REPO, ".venv", "bin", "python")


def write_csv(path, header, rows):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(",".join(header) + "\n")
        for row in rows:
            fh.write(",".join(str(v) for v in row) + "\n")


def run_pipeline(tmpdir, prev, curr, tx_rows, cov_rows):
    prev_csv = os.path.join(tmpdir, "tx_prev.csv")
    curr_csv = os.path.join(tmpdir, "tx_curr.csv")
    cov_csv = os.path.join(tmpdir, "coverage.csv")
    write_csv(prev_csv, ["month", "store_id", "customer_id", "amount"], tx_rows)
    write_csv(
        curr_csv,
        ["month", "store_id", "customer_id", "amount"],
        [r for r in tx_rows if r[0] == curr] or [],
    )
    write_csv(cov_csv, ["month", "store_id", "status"], cov_rows)
    raw_tx = os.path.join(tmpdir, "raw_tx.sqlite")
    raw_cov = os.path.join(tmpdir, "raw_cov.sqlite")
    out_db = os.path.join(tmpdir, "analysis.sqlite")
    subprocess.run(
        ["bash", IMPORT, raw_tx, raw_cov, cov_csv, prev_csv, curr_csv],
        check=True, capture_output=True, text=True,
    )
    subprocess.run(
        [VENV_PY if os.path.exists(VENV_PY) else sys.executable, BUILD,
         raw_tx, raw_cov, out_db, prev, curr],
        check=True, capture_output=True, text=True,
    )
    return sqlite3.connect(out_db)


def class_map(con):
    return dict(
        con.execute("SELECT customer_id, category FROM customer_classification")
    )


def test_main_scenario():
    # 与 sample_data 同构，覆盖四类 + 跨店 + 重复行 + 覆盖冲突
    tx = [
        ("2026-09", "S1", "C001", 88), ("2026-09", "S1", "C001", 88),
        ("2026-09", "S2", "C001", 120),
        ("2026-09", "S1", "C002", 45), ("2026-09", "S2", "C002", 60),
        ("2026-09", "S3", "C004", 32),
        ("2026-09", "S1", "C005", 77), ("2026-09", "S3", "C005", 77),
        ("2026-10", "S2", "C001", 99),
        ("2026-10", "S1", "C003", 210),
        ("2026-10", "S3", "C005", 10),
    ]
    cov = [
        ("2026-09", "S1", "complete"), ("2026-09", "S2", "complete"),
        ("2026-09", "S3", "complete"),
        ("2026-10", "S1", "complete"), ("2026-10", "S2", "complete"),
        ("2026-10", "S3", "incomplete"),
    ]
    with tempfile.TemporaryDirectory(dir=PROJECT) as tmp:
        con = run_pipeline(tmp, "2026-09", "2026-10", tx, cov)
        cm = class_map(con)
        assert cm == {
            "C001": "retained_confirmed",   # 本月在 S2 被观测到
            "C002": "churn_confirmed",      # 上月 S1/S2，本月两店都交齐且无消费
            "C003": "new_confirmed",        # 上月无记录
            "C004": "insufficient_evidence",  # 仅 S3，本月未交齐
            "C005": "retained_confirmed",   # S3 虽未交齐，但有观测消费即留存
        }, cm
        summary = dict(con.execute("SELECT category, customer_count FROM category_summary"))
        assert summary == {
            "retained_confirmed": 2, "churn_confirmed": 1,
            "new_confirmed": 1, "insufficient_evidence": 1,
        }
        bounds = dict(
            (r[0], (r[1], r[2]))
            for r in con.execute("SELECT metric, lower_bound, upper_bound FROM uncertainty_bounds")
        )
        assert bounds["churn_count"] == (1, 2)
        assert bounds["retained_count"] == (2, 3)
        assert bounds["new_count"] == (1, 1)
        # 去重：C001 上月 S1 两条重复折叠，计数为 2 条原始记录
        dup = con.execute(
            "SELECT raw_record_count FROM tx_unique "
            "WHERE customer_id='C001' AND month='2026-09' AND store_id='S1'"
        ).fetchone()[0]
        assert dup == 2
        # 溯源：物理行号 = CSV 行号（含表头）
        first_row = con.execute(
            "SELECT source_file, source_rowid FROM customer_record_trace "
            "WHERE customer_id='C001' ORDER BY month, source_rowid LIMIT 1"
        ).fetchone()
        assert first_row == ("tx_prev.csv", 2)
        con.close()


def test_full_coverage_has_no_unknowns():
    tx = [
        ("2026-09", "S1", "C001", 10),
        ("2026-09", "S1", "C002", 10),
        ("2026-10", "S1", "C001", 20),
    ]
    cov = [
        ("2026-09", "S1", "complete"),
        ("2026-10", "S1", "complete"),
    ]
    with tempfile.TemporaryDirectory(dir=PROJECT) as tmp:
        con = run_pipeline(tmp, "2026-09", "2026-10", tx, cov)
        cm = class_map(con)
        assert cm == {"C001": "retained_confirmed", "C002": "churn_confirmed"}
        unknown = con.execute(
            "SELECT COUNT(*) FROM customer_classification "
            "WHERE category='insufficient_evidence'"
        ).fetchone()[0]
        assert unknown == 0
        con.close()


def test_conflicting_coverage_is_conservative():
    tx = [("2026-09", "S1", "C001", 10)]
    cov = [
        ("2026-09", "S1", "complete"),
        ("2026-10", "S1", "complete"),
        ("2026-10", "S1", "incomplete"),
    ]
    with tempfile.TemporaryDirectory(dir=PROJECT) as tmp:
        con = run_pipeline(tmp, "2026-09", "2026-10", tx, cov)
        assert class_map(con)["C001"] == "insufficient_evidence"
        conflicts = con.execute("SELECT COUNT(*) FROM dq_duplicate_coverage").fetchone()[0]
        assert conflicts == 1
        con.close()


def test_missing_coverage_entry_is_conservative():
    # 覆盖清单完全没有 S1 的当月条目 -> 按未交齐处理，不能判流失
    tx = [("2026-09", "S1", "C001", 10)]
    cov = [("2026-09", "S1", "complete")]
    with tempfile.TemporaryDirectory(dir=PROJECT) as tmp:
        con = run_pipeline(tmp, "2026-09", "2026-10", tx, cov)
        assert class_map(con)["C001"] == "insufficient_evidence"
        gaps = con.execute("SELECT COUNT(*) FROM dq_missing_coverage").fetchone()[0]
        assert gaps == 1
        con.close()


def main():
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except AssertionError as exc:
                failures += 1
                print(f"FAIL {name}: {exc}")
            except Exception:
                failures += 1
                import traceback
                traceback.print_exc()
                print(f"ERROR {name}")
    if failures:
        print(f"{failures} test(s) failed")
        return 1
    print("all tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
