"""CSV 流式读取：逐行产出 Record，不把整份账册载入内存。

支持的表头（大小写不敏感，中英文均可）：

    type, lo, hi, ref
    类型, 下界, 上界, 备注

只有一列数量时可用 ``qty`` / ``数量`` 代替 ``lo``、``hi``，视为清晰数量。
类型取值：IN/入库、OUT/出库、COUNT/实盘（盘点）。
"""

from __future__ import annotations

import csv
from collections.abc import Iterator
from pathlib import Path
from typing import TextIO

from .core import Kind, Record

_TYPE_ALIASES = ("type", "类型", "kind", "种类")
_LO_ALIASES = ("lo", "lower", "下界", "下限")
_HI_ALIASES = ("hi", "upper", "上界", "上限")
_QTY_ALIASES = ("qty", "quantity", "数量")
_REF_ALIASES = ("ref", "reference", "备注", "单号")


class CsvFormatError(ValueError):
    pass


def _resolve_headers(fieldnames: list[str]) -> tuple[int, int, int, int]:
    normalized = [name.strip().lower() for name in fieldnames]
    type_idx = lo_idx = hi_idx = ref_idx = -1
    qty_idx = -1
    for idx, name in enumerate(normalized):
        if name in _TYPE_ALIASES:
            type_idx = idx
        elif name in _LO_ALIASES:
            lo_idx = idx
        elif name in _HI_ALIASES:
            hi_idx = idx
        elif name in _QTY_ALIASES:
            qty_idx = idx
        elif name in _REF_ALIASES:
            ref_idx = idx
    if type_idx < 0:
        raise CsvFormatError("CSV 缺少类型列（type/类型）")
    if lo_idx < 0 or hi_idx < 0:
        if qty_idx >= 0:
            lo_idx = hi_idx = qty_idx
        else:
            raise CsvFormatError("CSV 缺少数量列（lo/hi 或 qty/数量）")
    return type_idx, lo_idx, hi_idx, ref_idx


def _parse_int(cell: str, line_no: int, column: str) -> int:
    text = cell.strip()
    if text == "":
        raise CsvFormatError(f"第 {line_no} 行 {column} 列为空")
    try:
        value = int(text)
    except ValueError as exc:
        raise CsvFormatError(f"第 {line_no} 行 {column} 列不是整数: {text!r}") from exc
    return value


def stream_csv(source: str | Path | TextIO) -> Iterator[Record]:
    """逐行读取 CSV 并产出 Record。source 可为路径或已打开的文本流。"""
    if isinstance(source, (str, Path)):
        handle = open(source, newline="", encoding="utf-8-sig")
        close_after = True
    else:
        handle = source
        close_after = False
    try:
        reader = csv.reader(handle)
        try:
            header = next(reader)
        except StopIteration:
            raise CsvFormatError("CSV 为空，连表头都没有")
        type_idx, lo_idx, hi_idx, ref_idx = _resolve_headers(header)
        seq = 0
        for raw in reader:
            if not raw or all(cell.strip() == "" for cell in raw):
                continue
            line_no = reader.line_num
            if len(raw) <= max(type_idx, lo_idx, hi_idx):
                raise CsvFormatError(f"第 {line_no} 行列数不足")
            seq += 1
            kind = Kind.parse(raw[type_idx])
            lo = _parse_int(raw[lo_idx], line_no, "数量下界")
            hi = _parse_int(raw[hi_idx], line_no, "数量上界")
            ref = raw[ref_idx].strip() if ref_idx >= 0 and ref_idx < len(raw) else ""
            yield Record(seq=seq, kind=kind, lo=lo, hi=hi, ref=ref)
    finally:
        if close_after:
            handle.close()
