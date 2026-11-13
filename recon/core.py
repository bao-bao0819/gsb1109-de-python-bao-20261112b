"""区间对账核心：维护每一步余额可行区间，检测不可对平，构造两条合法走势。

数据模型
========
流水按发生顺序逐条读入，每条记录为入库（IN）、出库（OUT）或实盘（COUNT）。
数量看不清的记录给出整数上下界 ``[lo, hi]``，清晰记录 ``lo == hi``。

关键不变量
==========
余额任何时刻不得为负。设第 i 步后余额为 b_i，对每条记录 x：

* 入库：b_i = b_{i-1} + x
* 出库：b_i = b_{i-1} - x，且 b_i >= 0（即出库量不得超过当时余额）
* 实盘：b_i 被钉死为实盘值（实盘本身同样只知道上下界）

只把区间两端的净变动相加会丢掉中途的非负约束，因此这里逐步维护
可达区间 ``[lo, hi]``。所有量都是整数，区间端点为闭区间整数。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Iterable, Optional


class Kind(str, Enum):
    IN = "IN"
    OUT = "OUT"
    COUNT = "COUNT"

    @classmethod
    def parse(cls, token: str) -> "Kind":
        text = token.strip().upper()
        aliases = {
            "IN": cls.IN,
            "INBOUND": cls.IN,
            "入库": cls.IN,
            "收": cls.IN,
            "OUT": cls.OUT,
            "OUTBOUND": cls.OUT,
            "出库": cls.OUT,
            "发": cls.OUT,
            "COUNT": cls.COUNT,
            "STOCKTAKE": cls.COUNT,
            "CHECK": cls.COUNT,
            "实盘": cls.COUNT,
            "盘点": cls.COUNT,
        }
        if text not in aliases:
            raise ValueError(f"未知记录类型: {token!r}")
        return aliases[text]


@dataclass(frozen=True)
class Record:
    """一条流水。seq 为从 1 开始的行序号（不含表头）。"""

    seq: int
    kind: Kind
    lo: int
    hi: int
    ref: str = ""

    def __post_init__(self) -> None:
        if self.lo < 0 or self.hi < 0:
            raise ValueError(f"第 {self.seq} 行数量不能为负")
        if self.lo > self.hi:
            raise ValueError(f"第 {self.seq} 行下界超过上界")

    @property
    def exact(self) -> bool:
        return self.lo == self.hi


@dataclass(frozen=True)
class Step:
    """区间内一条业务记录作用后的可达余额区间。"""

    seq: int
    kind: Kind
    qty_lo: int
    qty_hi: int
    bal_lo: int
    bal_hi: int
    ref: str = ""


@dataclass(frozen=True)
class Segment:
    """相邻两次实盘之间的一段流水（首尾实盘本身不在 steps 内）。"""

    index: int
    start_count_seq: int
    start_lo: int
    start_hi: int
    end_count_seq: Optional[int]
    end_lo: Optional[int]
    end_hi: Optional[int]
    steps: tuple[Step, ...]
    feasible: bool
    reason: str = ""


@dataclass(frozen=True)
class Path:
    """一条逐笔合法的中间走势：起点实盘值、每笔数量与作用后的余额。"""

    name: str
    start_balance: int
    quantities: tuple[int, ...]
    balances: tuple[int, ...]


class Reconciler:
    """单遍流式对账器。

    用法::

        rec = Reconciler()
        for record in stream:
            rec.feed(record)
        report = rec.finish()
    """

    def __init__(self) -> None:
        self._segments: list[Segment] = []
        self._steps: list[Step] = []
        self._lo = 0
        self._hi = 0
        self._cur_count_seq = 0
        self._cur_lo = 0
        self._cur_hi = 0
        self._segment_index = 0
        self._have_count = False
        self._infeasible: Optional[Segment] = None
        self._infeasible_record: Optional[Record] = None

    @property
    def infeasible_record(self) -> Optional[Record]:
        """最早一段不可能对平的区间中，最先导致矛盾的那条业务记录。"""
        return self._infeasible_record

    def feed(self, record: Record) -> None:
        if self._infeasible is not None:
            return
        if record.kind is Kind.COUNT:
            # 首条实盘只是期初锚点；若它之前已有业务记录，则按虚拟期初
            # 余额 0 正常关闭第一段。
            if not self._have_count and not self._steps:
                self._anchor(record.seq, record.lo, record.hi)
                return
            self._have_count = True
            self._close_segment(record)
            return
        self._apply_movement(record)

    def finish(self) -> list[Segment]:
        if self._infeasible is not None:
            self._segments.append(self._infeasible)
            self._infeasible = None
            return list(self._segments)
        if self._steps:
            tail = Segment(
                index=self._segment_index,
                start_count_seq=self._cur_count_seq,
                start_lo=self._cur_lo,
                start_hi=self._cur_hi,
                end_count_seq=None,
                end_lo=None,
                end_hi=None,
                steps=tuple(self._steps),
                feasible=True,
            )
            self._segments.append(tail)
        return list(self._segments)

    def _close_segment(self, count: Record) -> None:
        feasible = True
        reason = ""
        end_lo: Optional[int]
        end_hi: Optional[int]
        if count.lo > self._hi or count.hi < self._lo:
            feasible = False
            reason = (
                f"实盘值区间 [{count.lo}, {count.hi}] 与流水可达余额区间 "
                f"[{self._lo}, {self._hi}] 不相交"
            )
            end_lo, end_hi = count.lo, count.hi
        else:
            self._lo = max(self._lo, count.lo)
            self._hi = min(self._hi, count.hi)
            end_lo, end_hi = self._lo, self._hi

        segment = Segment(
            index=self._segment_index,
            start_count_seq=self._cur_count_seq,
            start_lo=self._cur_lo,
            start_hi=self._cur_hi,
            end_count_seq=count.seq,
            end_lo=end_lo,
            end_hi=end_hi,
            steps=tuple(self._steps),
            feasible=feasible,
            reason=reason,
        )
        if not feasible:
            self._infeasible = segment
            self._infeasible_record = count
            return
        self._segments.append(segment)
        self._begin_next(count.seq, self._lo, self._hi)

    def _anchor(self, seq: int, lo: int, hi: int) -> None:
        self._have_count = True
        self._cur_count_seq = seq
        self._cur_lo, self._cur_hi = lo, hi
        self._lo, self._hi = lo, hi
        self._steps = []

    def _begin_next(self, seq: int, lo: int, hi: int) -> None:
        self._segment_index += 1
        self._cur_count_seq = seq
        self._cur_lo, self._cur_hi = lo, hi
        self._lo, self._hi = lo, hi
        self._steps = []

    def _apply_movement(self, record: Record) -> None:
        if record.kind is Kind.IN:
            new_lo = self._lo + record.lo
            new_hi = self._hi + record.hi
        else:
            # 出库量 x ∈ [lo, hi]，余额 b' = b - x >= 0。
            # 给定 b 时 x ≤ b；可达 b' 下界取 max(0, b_lo - x_hi)，
            # 上界取 b_hi - x_lo（x_lo ≤ b_hi 自动满足，否则下界已为空）。
            new_lo = max(0, self._lo - record.hi)
            new_hi = self._hi - record.lo
            if new_hi < 0 or new_lo > new_hi:
                self._mark_empty(record)
                return
        self._lo, self._hi = new_lo, new_hi
        self._steps.append(
            Step(
                seq=record.seq,
                kind=record.kind,
                qty_lo=record.lo,
                qty_hi=record.hi,
                bal_lo=self._lo,
                bal_hi=self._hi,
                ref=record.ref,
            )
        )

    def _mark_empty(self, record: Record) -> None:
        segment = Segment(
            index=self._segment_index,
            start_count_seq=self._cur_count_seq,
            start_lo=self._cur_lo,
            start_hi=self._cur_hi,
            end_count_seq=None,
            end_lo=None,
            end_hi=None,
            steps=tuple(self._steps),
            feasible=False,
            reason=(
                f"第 {record.seq} 行出库量下界 {record.lo} 超过当时可达余额上界 "
                f"{self._hi}，任何取值都会使余额为负"
            ),
        )
        self._infeasible = segment
        self._infeasible_record = record

    # ------------------------------------------------------------------
    # 合法走势构造
    # ------------------------------------------------------------------
    def build_paths(self, segment: Segment) -> tuple[Path, Path]:
        """对可对平且不唯一的区间，构造两条不同但逐笔合法的中间走势。

        采用两遍法：

        1. 反向 pass：从末端实盘窗口倒推每一步余额的可行窗口 ``W_i``，
           同时纳入非负约束与起点实盘窗口；
        2. 前向 pass：在窗口内分别取逐点最小 / 最大余额，得到保守与激进
           两条走势。每笔数量始终落在该记录自己的 ``[lo, hi]`` 内，
           因此不指认任何一笔为错账，只展示两种都说得通的走法。
        """
        if not segment.feasible:
            raise ValueError("区间不可对平，无法构造合法走势")
        if segment.end_lo is None:
            raise ValueError("末端没有实盘钉住，余额范围本身就是答案，无需构造走势")
        windows = self._backward_windows(segment)
        low = self._extreme_path(segment, windows, minimize=True)
        high = self._extreme_path(segment, windows, minimize=False)
        if (
            low.start_balance == high.start_balance
            and low.quantities == high.quantities
        ):
            raise ValueError("区间走势唯一，不存在第二种合法走法")
        return low, high

    def _backward_windows(self, segment: Segment) -> list[tuple[int, int]]:
        """返回 W_0..W_n：第 i 步后余额在后缀约束下的可行闭区间。"""
        end_lo = segment.end_lo
        end_hi = segment.end_hi
        assert end_lo is not None and end_hi is not None
        windows: list[tuple[int, int]] = [(end_lo, end_hi)]
        for step in reversed(segment.steps):
            win_lo, win_hi = windows[0]
            if step.kind is Kind.IN:
                # b_{i-1} = b_i - q
                prev_lo = max(0, win_lo - step.qty_hi)
                prev_hi = win_hi - step.qty_lo
            else:
                # b_{i-1} = b_i + q
                prev_lo = win_lo + step.qty_lo
                prev_hi = win_hi + step.qty_hi
            if prev_lo > prev_hi:
                raise RuntimeError("内部错误：反向可行窗口为空")
            windows.insert(0, (prev_lo, prev_hi))
        lo, hi = windows[0]
        lo = max(lo, segment.start_lo)
        hi = min(hi, segment.start_hi)
        if lo > hi:
            raise RuntimeError("内部错误：起点实盘与后缀约束无交集")
        windows[0] = (lo, hi)
        return windows

    def _extreme_path(
        self,
        segment: Segment,
        windows: list[tuple[int, int]],
        minimize: bool,
    ) -> Path:
        balance = windows[0][0] if minimize else windows[0][1]
        start_balance = balance
        quantities: list[int] = []
        balances: list[int] = []
        for i, step in enumerate(segment.steps):
            win_lo, win_hi = windows[i + 1]
            if step.kind is Kind.IN:
                if minimize:
                    qty = step.qty_lo
                    next_balance = balance + qty
                    if next_balance < win_lo:
                        qty += win_lo - next_balance
                        next_balance = win_lo
                else:
                    qty = step.qty_hi
                    next_balance = balance + qty
                    if next_balance > win_hi:
                        qty -= next_balance - win_hi
                        next_balance = win_hi
            else:
                if minimize:
                    qty = min(step.qty_hi, balance)
                    next_balance = balance - qty
                    if next_balance < win_lo:
                        qty -= win_lo - next_balance
                        next_balance = win_lo
                else:
                    qty = step.qty_lo
                    next_balance = balance - qty
                    if next_balance > win_hi:
                        qty += next_balance - win_hi
                        next_balance = win_hi
            if not (step.qty_lo <= qty <= step.qty_hi):
                raise RuntimeError("内部错误：取值超出记录自身上下界")
            if not (win_lo <= next_balance <= win_hi) or next_balance < 0:
                raise RuntimeError("内部错误：走势跳出可行窗口或出现负余额")
            quantities.append(qty)
            balances.append(next_balance)
            balance = next_balance
        name = (
            "保守走法（余额逐点取最小）"
            if minimize
            else "激进走法（余额逐点取最大）"
        )
        return Path(name, start_balance, tuple(quantities), tuple(balances))


def reconcile(records: Iterable[Record]) -> list[Segment]:
    """便捷封装：喂入整条记录流，返回各实盘区间的结论。"""
    engine = Reconciler()
    for record in records:
        engine.feed(record)
    return engine.finish()
