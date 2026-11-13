"""核心引擎测试，含随机暴力对拍。"""

from __future__ import annotations

import itertools
import random

import pytest

from recon.core import Kind, Path, Record, Reconciler, Segment


def rec(seq: int, kind: Kind, lo: int, hi: int | None = None, ref: str = "") -> Record:
    return Record(seq, kind, lo, lo if hi is None else hi, ref)


def run(records: list[Record]) -> tuple[list[Segment], Reconciler]:
    engine = Reconciler()
    for record in records:
        engine.feed(record)
    return engine.finish(), engine


def test_exact_ledger_reconciles() -> None:
    records = [
        rec(1, Kind.COUNT, 100),
        rec(2, Kind.IN, 30),
        rec(3, Kind.OUT, 50),
        rec(4, Kind.COUNT, 80),
    ]
    segments, engine = run(records)
    assert len(segments) == 1
    seg = segments[0]
    assert seg.feasible
    assert seg.steps[-1].bal_lo == seg.steps[-1].bal_hi == 80
    assert engine.infeasible_record is None


def test_nonnegativity_cannot_be_checked_by_endpoint_sums() -> None:
    # 净变动 +10 看似可行，但第一笔出库 60 在期初只有 50 时必然穿负。
    records = [
        rec(1, Kind.COUNT, 50),
        rec(2, Kind.OUT, 60),
        rec(3, Kind.IN, 70),
        rec(4, Kind.COUNT, 60),
    ]
    segments, engine = run(records)
    assert not segments[0].feasible
    assert engine.infeasible_record is not None
    assert engine.infeasible_record.seq == 2


def test_fuzzy_outbound_clamps_at_zero() -> None:
    records = [
        rec(1, Kind.COUNT, 10),
        rec(2, Kind.OUT, 8, 12),
        rec(3, Kind.COUNT, 0, 2),
    ]
    segments, _ = run(records)
    seg = segments[0]
    assert seg.feasible
    assert seg.steps[0].bal_lo == 0
    assert seg.steps[0].bal_hi == 2


def test_disjoint_count_window_marks_infeasible() -> None:
    records = [
        rec(1, Kind.COUNT, 10, 12),
        rec(2, Kind.IN, 5),
        rec(3, Kind.COUNT, 20, 22),
    ]
    segments, engine = run(records)
    assert not segments[0].feasible
    assert engine.infeasible_record is not None
    assert engine.infeasible_record.seq == 3
    assert "不相交" in segments[0].reason


def test_tail_without_closing_count_reports_range() -> None:
    records = [
        rec(1, Kind.COUNT, 100),
        rec(2, Kind.IN, 10, 20),
        rec(3, Kind.OUT, 30),
    ]
    segments, _ = run(records)
    tail = segments[-1]
    assert tail.feasible and tail.end_count_seq is None
    assert (tail.steps[-1].bal_lo, tail.steps[-1].bal_hi) == (80, 90)


def test_build_two_distinct_legal_paths() -> None:
    records = [
        rec(1, Kind.COUNT, 100),
        rec(2, Kind.IN, 10, 20),
        rec(3, Kind.OUT, 30, 40),
        rec(4, Kind.COUNT, 75, 85),
    ]
    segments, engine = run(records)
    seg = segments[0]
    assert seg.feasible
    low, high = engine.build_paths(seg)
    _assert_path_legal(seg, low)
    _assert_path_legal(seg, high)
    assert low != high
    assert low.quantities != high.quantities or low.start_balance != high.start_balance


def test_unique_trajectory_raises() -> None:
    records = [
        rec(1, Kind.COUNT, 100),
        rec(2, Kind.IN, 10),
        rec(3, Kind.OUT, 30),
        rec(4, Kind.COUNT, 80),
    ]
    segments, engine = run(records)
    with pytest.raises(ValueError, match="唯一"):
        engine.build_paths(segments[0])


def test_multiple_segments_and_early_stop() -> None:
    records = [
        rec(1, Kind.COUNT, 100),
        rec(2, Kind.IN, 10),
        rec(3, Kind.COUNT, 110),
        rec(4, Kind.OUT, 200),
        rec(5, Kind.IN, 5),
        rec(6, Kind.COUNT, 0),
    ]
    segments, engine = run(records)
    assert [seg.index for seg in segments] == [0, 1]
    assert segments[0].feasible
    assert not segments[1].feasible
    # 行4 出库即穿负；行5 属于其后记录，不应出现在区间步骤里
    assert [step.seq for step in segments[1].steps] == []
    assert engine.infeasible_record is not None
    assert engine.infeasible_record.seq == 4


def test_ledger_without_any_count_treats_start_as_zero() -> None:
    records = [
        rec(1, Kind.IN, 30),
        rec(2, Kind.OUT, 10),
    ]
    segments, engine = run(records)
    assert engine.infeasible_record is None
    tail = segments[0]
    assert tail.start_count_seq == 0
    assert tail.steps[-1].bal_lo == tail.steps[-1].bal_hi == 20


def test_infeasible_segment_refuses_paths() -> None:
    records = [
        rec(1, Kind.COUNT, 10),
        rec(2, Kind.OUT, 20),
        rec(3, Kind.COUNT, 0),
    ]
    segments, engine = run(records)
    with pytest.raises(ValueError, match="不可对平"):
        engine.build_paths(segments[0])


def _assert_path_legal(segment: Segment, path: Path) -> None:
    assert segment.start_lo <= path.start_balance <= segment.start_hi
    balance = path.start_balance
    assert segment.end_lo is not None and segment.end_hi is not None
    for step, qty in zip(segment.steps, path.quantities, strict=True):
        assert step.qty_lo <= qty <= step.qty_hi
        balance += qty if step.kind is Kind.IN else -qty
        assert balance >= 0
    assert segment.end_lo <= balance <= segment.end_hi
    assert tuple(path.balances[-1:]) == (balance,)


# ----------------------------------------------------------------------
# 暴力对拍：小域枚举所有取值组合，核对区间引擎的每一步结论
# ----------------------------------------------------------------------

def _brute_force(
    start_window: tuple[int, int],
    movements: list[tuple[Kind, int, int]],
    end_window: tuple[int, int],
) -> tuple[bool, list[set[int]], set[int], set[tuple[int, tuple[int, ...]]]]:
    reachable_per_step: list[set[int]] = [set() for _ in movements]
    ending: set[int] = set()
    full_paths: set[tuple[int, tuple[int, ...]]] = set()
    starts = range(start_window[0], start_window[1] + 1)
    domains = [range(lo, hi + 1) for _, lo, hi in movements]
    for start, combo in itertools.product(starts, itertools.product(*domains)):
        balance = start
        ok = True
        for idx, ((kind, _, _), qty) in enumerate(zip(movements, combo, strict=True)):
            balance += qty if kind is Kind.IN else -qty
            if balance < 0:
                ok = False
                break
            reachable_per_step[idx].add(balance)
        if ok and end_window[0] <= balance <= end_window[1]:
            ending.add(balance)
            full_paths.add((start, combo))
    return bool(ending), reachable_per_step, ending, full_paths


def test_randomized_brute_force_cross_check() -> None:
    rng = random.Random(20261113)
    for trial in range(400):
        start = (rng.randint(0, 6),)
        start_hi = start[0] + rng.randint(0, 3)
        start_window = (start[0], start_hi)
        count = rng.randint(1, 4)
        movements: list[tuple[Kind, int, int]] = []
        records: list[Record] = [rec(1, Kind.COUNT, start_window[0], start_window[1])]
        for i in range(count):
            kind = Kind.IN if rng.random() < 0.5 else Kind.OUT
            lo = rng.randint(0, 5)
            hi = lo + rng.randint(0, 3)
            movements.append((kind, lo, hi))
            records.append(rec(i + 2, kind, lo, hi))
        # 先用引擎拿到无末端约束的可达范围，再随机挑一个可能相交/不相交的实盘窗口
        probe, probe_engine = run(records)
        if probe_engine.infeasible_record is not None:
            # 中途必穿负的用例：末端窗口任取，结论必然仍是不可对平
            end_window = (0, rng.randint(0, 3))
        else:
            reach_lo = probe[-1].steps[-1].bal_lo
            reach_hi = probe[-1].steps[-1].bal_hi
            pick = rng.randint(reach_lo - 3, reach_hi + 1)
            end_lo = max(0, pick)
            end_window = (end_lo, end_lo + rng.randint(0, 3))
        records.append(rec(count + 2, Kind.COUNT, end_window[0], end_window[1]))

        segments, engine = run(records)
        seg = segments[0]
        feasible, brute_sets, brute_ending, full_paths = _brute_force(
            start_window, movements, end_window
        )
        assert seg.feasible == feasible, trial
        if not feasible:
            assert engine.infeasible_record is not None
            continue
        for step, brute in zip(seg.steps, brute_sets, strict=True):
            assert brute, "引擎判定可行但暴力枚举无可达余额"
            assert (step.bal_lo, step.bal_hi) == (min(brute), max(brute)), trial
            # 该动力学下可达集必为连续整数区间
            assert brute == set(range(min(brute), max(brute) + 1)), trial
        assert min(brute_ending) >= seg.end_lo
        assert max(brute_ending) <= seg.end_hi

        if len(full_paths) > 1:
            low, high = engine.build_paths(seg)
            for path in (low, high):
                _assert_path_legal(seg, path)
                for idx, balance in enumerate(path.balances):
                    assert balance in brute_sets[idx]
                assert (path.start_balance, path.quantities) in full_paths
            assert low != high
        else:
            with pytest.raises(ValueError, match="唯一"):
                engine.build_paths(seg)
