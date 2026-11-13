"""把对账结果渲染成仓管可读的文本报告。"""

from __future__ import annotations

from .core import Kind, Path, Reconciler, Segment


def _qty_text(lo: int, hi: int) -> str:
    return str(lo) if lo == hi else f"{lo}~{hi}"


def _kind_label(kind: Kind) -> str:
    return {Kind.IN: "入库", Kind.OUT: "出库", Kind.COUNT: "实盘"}[kind]


def render_segment(segment: Segment, engine: Reconciler) -> str:
    lines: list[str] = []
    start = _qty_text(segment.start_lo, segment.start_hi)
    if segment.end_count_seq is None:
        header = (
            f"区间 {segment.index}：起始实盘(行{segment.start_count_seq})={start}，"
            "之后再无实盘"
        )
    else:
        end = _qty_text(segment.end_lo, segment.end_hi)
        header = (
            f"区间 {segment.index}：实盘 行{segment.start_count_seq}={start}"
            f" → 行{segment.end_count_seq}={end}"
        )
    lines.append(header)

    if not segment.feasible:
        lines.append(f"  结论：不可能对平 —— {segment.reason}")
        return "\n".join(lines)

    lines.append("  逐笔可达余额区间（已含全程非负约束）：")
    for step in segment.steps:
        tag = f"[{step.ref}] " if step.ref else ""
        lines.append(
            f"    行{step.seq} {_kind_label(step.kind)} {_qty_text(step.qty_lo, step.qty_hi)}"
            f" {tag}→ 余额 [{step.bal_lo}, {step.bal_hi}]"
        )
    if segment.end_count_seq is None:
        tail = segment.steps[-1] if segment.steps else None
        if tail is None:
            lines.append("  结论：区间内没有业务记录")
        else:
            lines.append(f"  末端可成立余额范围：[{tail.bal_lo}, {tail.bal_hi}]")
        return "\n".join(lines)

    try:
        path_low, path_high = engine.build_paths(segment)
    except ValueError:
        lines.append("  结论：约束把每笔数量都钉死了，走势唯一，可正常对平")
        return "\n".join(lines)
    lines.append("  结论：可对平但不唯一，以下两种走势均合法，不能据此指认错账：")
    lines.extend(_render_path(segment, path_low))
    lines.extend(_render_path(segment, path_high))
    return "\n".join(lines)


def _render_path(segment: Segment, path: Path) -> list[str]:
    lines = [f"  · {path.name}：起点余额 {path.start_balance}"]
    balance = path.start_balance
    for step, qty in zip(segment.steps, path.quantities, strict=True):
        if step.kind is Kind.IN:
            balance += qty
        else:
            balance -= qty
        lines.append(
            f"      行{step.seq} {_kind_label(step.kind)} {qty} → 余额 {balance}"
        )
    lines.append(f"      末端余额 {balance}，落入实盘窗口"
                 f" [{segment.end_lo}, {segment.end_hi}]")
    return lines


def render_report(segments: list[Segment], engine: Reconciler) -> str:
    blocks = [render_segment(segment, engine) for segment in segments]
    bad = engine.infeasible_record
    if bad is not None:
        blocks.append(
            f"最早不可能对平的业务记录：行{bad.seq} {_kind_label(bad.kind)}"
            f"（数量 {_qty_text(bad.lo, bad.hi)}），其后流水不再继续推断。"
        )
    return "\n\n".join(blocks)
