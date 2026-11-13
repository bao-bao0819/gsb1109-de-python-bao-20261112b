#!/usr/bin/env bash
#
# 使用 q 把原始 CSV 入库为 sqlite 文件（q 的 -S 会为每个输入文件保留一张原始表，
# 表内 rowid 即数据行顺序；配合 -H 跳过表头，物理行号 = rowid + 1）。
#
# 用法:
#   import_raw_db.sh <raw_tx.sqlite> <raw_cov.sqlite> <coverage.csv> <tx1.csv> [tx2.csv ...]
#
set -euo pipefail

if [[ $# -lt 4 ]]; then
  echo "usage: $0 <raw_tx.sqlite> <raw_cov.sqlite> <coverage.csv> <tx1.csv> [tx2.csv ...]" >&2
  exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
# 允许 retention-analysis 放在 q 仓库内（.venv/bin 与 bin/q.py 在上一级）
if [[ -x "$ROOT_DIR/.venv/bin/python" ]]; then
  PY="$ROOT_DIR/.venv/bin/python"
elif [[ -x "$ROOT_DIR/../.venv/bin/python" ]]; then
  PY="$ROOT_DIR/../.venv/bin/python"
else
  PY="$(command -v python3)"
fi
if [[ -f "$ROOT_DIR/bin/q.py" ]]; then
  Q_PY="$ROOT_DIR/bin/q.py"
else
  Q_PY="$ROOT_DIR/../bin/q.py"
fi
DELIM="${DELIM:-,}"

RAW_TX="$1"; shift
RAW_COV="$1"; shift
COV_FILE="$1"; shift
TX_FILES=("$@")

rm -f "$RAW_TX" "$RAW_COV"
mkdir -p "$(dirname "$RAW_TX")"

# 交易文件：一条 UNION ALL 查询引用全部文件，q 会把每张源表都写进同一个 sqlite 文件。
union_query=""
for f in "${TX_FILES[@]}"; do
  if [[ -n "$union_query" ]]; then
    union_query+=" UNION ALL "
  fi
  union_query+="SELECT * FROM $f"
done

"$PY" "$Q_PY" -H -d "$DELIM" -S "$RAW_TX" "$union_query" >/dev/null
"$PY" "$Q_PY" -H -d "$DELIM" -S "$RAW_COV" "SELECT * FROM $COV_FILE" >/dev/null

echo "raw transaction db: $RAW_TX"
echo "raw coverage db   : $RAW_COV"
