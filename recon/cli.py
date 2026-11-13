"""命令行入口：python -m recon 账册.csv"""

from __future__ import annotations

import argparse
import sys

from .core import Reconciler
from .reader import CsvFormatError, stream_csv
from .report import render_report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="recon",
        description="流式库存区间对账：计算相邻实盘间的可成立余额范围",
    )
    parser.add_argument("csv_path", help="按发生顺序排列的流水 CSV")
    args = parser.parse_args(argv)

    engine = Reconciler()
    try:
        for record in stream_csv(args.csv_path):
            engine.feed(record)
        segments = engine.finish()
    except CsvFormatError as exc:
        print(f"CSV 格式错误：{exc}", file=sys.stderr)
        return 2

    print(render_report(segments, engine))
    return 1 if engine.infeasible_record is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
