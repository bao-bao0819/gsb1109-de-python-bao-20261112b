#!/usr/bin/env bash
#
# 端到端流程：CSV --(q -S)--> 原始 sqlite --(build_model.py)--> 分析库 + CSV 导出
#
# 用法:
#   run_all.sh <prev_month> <curr_month> <coverage.csv> <tx_prev.csv> <tx_curr.csv> [more_tx.csv ...]
#
set -euo pipefail

if [[ $# -lt 5 ]]; then
  echo "usage: $0 <prev_month> <curr_month> <coverage.csv> <tx_prev.csv> <tx_curr.csv> [more_tx.csv ...]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
WORK_DIR="$ROOT_DIR/work"
mkdir -p "$WORK_DIR"

PREV_MONTH="$1"; shift
CURR_MONTH="$1"; shift
COV_FILE="$1"; shift
TX_FILES=("$@")

RAW_TX="$WORK_DIR/raw_transactions.sqlite"
RAW_COV="$WORK_DIR/raw_coverage.sqlite"
ANALYSIS_DB="$WORK_DIR/retention_analysis.sqlite"

"$SCRIPT_DIR/import_raw_db.sh" "$RAW_TX" "$RAW_COV" "$COV_FILE" "${TX_FILES[@]}"
if [[ -x "$ROOT_DIR/../.venv/bin/python" ]]; then
  BUILD_PY="$ROOT_DIR/../.venv/bin/python"
else
  BUILD_PY="$(command -v python3)"
fi
"$BUILD_PY" "$SCRIPT_DIR/build_model.py" "$RAW_TX" "$RAW_COV" "$ANALYSIS_DB" \
  "$PREV_MONTH" "$CURR_MONTH"

echo
echo "== 分类汇总 =="
if [[ -x "$ROOT_DIR/../.venv/bin/python" ]]; then
  Q=("$ROOT_DIR/../.venv/bin/python" "$ROOT_DIR/../bin/q.py")
elif [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  Q=("$ROOT_DIR/.venv/bin/python" "$ROOT_DIR/bin/q.py")
else
  Q=(q)
fi
"${Q[@]}" -H -O "SELECT category, customer_count FROM $ANALYSIS_DB:::category_summary ORDER BY category"
echo
echo "== 不确定区间 =="
"${Q[@]}" -H -O "SELECT metric, lower_bound, upper_bound, note FROM $ANALYSIS_DB:::uncertainty_bounds ORDER BY metric"
