"""sales_audit 核查工具测试。"""

from __future__ import annotations

import itertools
import json
import random
from pathlib import Path

import pytest

from sales_audit import (
    CONFLICT,
    MULTIPLE,
    UNIQUE,
    NeedBoundsError,
    SpecError,
    audit_sheet,
    format_report,
    main,
    parse_sheet,
    run_spec,
)
from sales_audit.hidden_cells import _as_int, _cell_bounds

EXAMPLES = Path(__file__).resolve().parent.parent / "examples" / "sales_audit"


def run_example(name: str):
    return run_spec(EXAMPLES / name / "spec.json")


def verify_fill(model, fill):
    """独立复核：一份填法必须满足整张表的行小计、列小计与上下限。"""
    for r in model.data_rows:
        total = _as_int(model.matrix[r][model.row_total_col], "行小计")
        got = 0
        for c in model.data_cols:
            value = fill[(r, c)] if (r, c) in model.hidden else _as_int(
                model.matrix[r][c], "可见格"
            )
            lo, hi = _cell_bounds(model.bounds_spec, r, c)
            assert lo <= value <= hi, f"({r},{c})={value} 超出上下限 [{lo},{hi}]"
            got += value
        assert got == total, f"第{r + 1}行和 {got} != 行小计 {total}"
    for q in model.col_total_rows:
        for c in model.data_cols:
            total = _as_int(model.matrix[q][c], "列小计")
            got = sum(
                fill[(r, c)] if (r, c) in model.hidden else _as_int(
                    model.matrix[r][c], "可见格"
                )
                for r in model.data_rows
            )
            assert got == total, f"第{c + 1}列和 {got} != 列小计 {total}"


def test_unique_example_is_solved_with_basis():
    result = run_example("unique")
    assert result.status == UNIQUE
    expected = {(1, 2): 12, (2, 1): 8, (2, 3): 6, (3, 2): 6}
    assert result.solution == expected
    verify_fill(result.model, result.solution)
    assert set(result.bases) == set(expected)
    assert all(isinstance(reason, str) and reason for reason in result.bases.values())
    report = format_report(result)
    assert "唯一整数解" in report
    assert "逐格依据" in report


def test_blank_cells_are_unknown_not_zero():
    result = run_example("blank_unknown")
    assert result.status == UNIQUE
    # 空白格必须被当作遮盖未知格，而不是按 0 处理；解与 ? 记号版本一致。
    assert {(1, 2), (2, 1), (2, 3), (3, 2)} <= result.model.hidden
    assert result.solution == {(1, 2): 12, (2, 1): 8, (2, 3): 6, (3, 2): 6}
    verify_fill(result.model, result.solution)


def test_explicit_zero_is_not_treated_as_hidden():
    matrix = [
        ["供应商", "A", "B", "行小计"],
        ["甲", 0, 5, 5],
        ["乙", 3, 4, 7],
        ["列小计", 3, 9, 12],
    ]
    spec = {
        "header_rows": [0], "label_col": 0, "row_total_col": 3,
        "col_total_rows": [3], "grand_total_row": 3,
        "bounds": {"default": [0, 100]},
    }
    model = parse_sheet(matrix, spec)
    assert (1, 1) not in model.hidden  # 显式 0 仍是公开数据
    result = audit_sheet(model)
    assert result.status == UNIQUE
    assert result.solution == {}  # 没有任何遮盖格


def test_multiple_example_gives_two_full_valid_fillings():
    result = run_example("multiple")
    assert result.status == MULTIPLE
    assert len(result.solutions) == 2
    first, second = result.solutions
    verify_fill(result.model, first)
    verify_fill(result.model, second)
    assert first != second
    report = format_report(result)
    assert "不能唯一确定" in report
    assert "可行填法 1" in report and "可行填法 2" in report
    assert "填法一" in report and "填法二" in report


def test_conflict_example_is_flagged_not_reported_as_success():
    result = run_example("conflict")
    assert result.status == CONFLICT
    assert result.solution is None
    assert result.solutions == []
    assert result.contradictions  # 必须单独指出矛盾
    report = format_report(result)
    assert "矛盾" in report
    assert "不是遮盖反推成功" in report


def test_bounds_driven_conflict_is_detected():
    # 行/列小计彼此自洽，但每格业务下限 5 使行目标 3 根本凑不出来。
    matrix = [
        ["供应商", "A", "B", "行小计"],
        ["甲", "?", "?", 3],
        ["乙", "?", "?", 3],
        ["列小计", 3, 3, 6],
    ]
    spec = {
        "header_rows": [0], "label_col": 0, "row_total_col": 3,
        "col_total_rows": [3], "grand_total_row": 3,
        "masks": ["?"], "bounds": {"default": [5, 10]},
    }
    result = audit_sheet(parse_sheet(matrix, spec))
    assert result.status == CONFLICT
    assert result.solutions == []
    assert any("上下限" in msg for msg in result.contradictions)


def test_grand_total_mismatch_is_contradiction():
    matrix = [
        ["供应商", "A", "B", "行小计"],
        ["甲", 1, "?", 4],
        ["乙", "?", 2, 5],
        ["列小计", 4, 5, 99],  # 总计 99 与行小计之和 9 不符
    ]
    spec = {
        "header_rows": [0], "label_col": 0, "row_total_col": 3,
        "col_total_rows": [3], "grand_total_row": 3,
        "masks": ["?"], "bounds": {"default": [0, 100]},
    }
    result = audit_sheet(parse_sheet(matrix, spec))
    assert result.status == CONFLICT
    assert any("总计" in msg for msg in result.contradictions)


def test_unbounded_free_cell_requires_bounds():
    matrix = [
        ["供应商", "A", "B", "行小计"],
        ["甲", "?", "?", 5],
        ["乙", "?", "?", 7],
        ["列小计", 6, 6, 12],
    ]
    spec = {
        "header_rows": [0], "label_col": 0, "row_total_col": 3,
        "col_total_rows": [3], "grand_total_row": 3,
        "masks": ["?"], "bounds": {"default": [None, None]},
    }
    with pytest.raises(NeedBoundsError):
        audit_sheet(parse_sheet(matrix, spec))


def test_half_unbounded_is_bounded_by_propagation():
    # 只放开下限：方程配合有限上限会在传播中导出有限下界，正常求解且不崩溃。
    matrix = [
        ["供应商", "A", "B", "行小计"],
        ["甲", "?", "?", 5],
        ["乙", "?", "?", 7],
        ["列小计", 6, 6, 12],
    ]
    spec = {
        "header_rows": [0], "label_col": 0, "row_total_col": 3,
        "col_total_rows": [3], "grand_total_row": 3,
        "masks": ["?"], "bounds": {"default": [None, 100]},
    }
    result = audit_sheet(parse_sheet(matrix, spec))
    assert result.status == MULTIPLE
    for fill in result.solutions:
        verify_fill(result.model, fill)


def test_half_unbounded_cell_pinned_by_equations_still_solves():
    # 单数据行时列小计直接钉死每格，即使下限放开也应正常求解、不崩溃。
    matrix = [
        ["供应商", "A", "B", "行小计"],
        ["甲", "?", "?", 5],
        ["列小计", 2, 3, 5],
    ]
    spec = {
        "header_rows": [0], "label_col": 0, "row_total_col": 3,
        "col_total_rows": [2], "grand_total_row": 2,
        "masks": ["?"], "bounds": {"default": [None, 100]},
    }
    result = audit_sheet(parse_sheet(matrix, spec))
    assert result.status == UNIQUE
    assert result.solution == {(1, 1): 2, (1, 2): 3}


def test_bounds_can_pin_a_cell():
    matrix = [
        ["供应商", "A", "B", "行小计"],
        ["甲", "?", "?", 5],
        ["列小计", 2, 3, 5],
    ]
    spec = {
        "header_rows": [0], "label_col": 0, "row_total_col": 3,
        "col_total_rows": [2], "grand_total_row": 2,
        "masks": ["?"],
        "bounds": {"default": [0, 100], "cells": {"1,1": [2, 2]}},
    }
    result = audit_sheet(parse_sheet(matrix, spec))
    assert result.status == UNIQUE
    assert result.solution == {(1, 1): 2, (1, 2): 3}
    assert "区间钉死" in result.bases[(1, 1)]


def test_missing_row_total_is_spec_error():
    matrix = [
        ["供应商", "A", "B", "行小计"],
        ["甲", "?", 1, ""],
        ["列小计", 3, 3, 6],
    ]
    spec = {
        "header_rows": [0], "label_col": 0, "row_total_col": 3,
        "col_total_rows": [2], "bounds": {"default": [0, 100]},
    }
    with pytest.raises(SpecError):
        parse_sheet(matrix, spec)


def test_cli_exit_codes(capsys, tmp_path):
    for name, expected_code in [
        ("unique", 0), ("multiple", 3), ("conflict", 2),
    ]:
        spec_src = json.loads((EXAMPLES / name / "spec.json").read_text("utf-8"))
        spec_dst = tmp_path / f"{name}.json"
        table_dst = tmp_path / f"{name}.csv"
        table_dst.write_text((EXAMPLES / name / "sales.csv").read_text("utf-8"))
        spec_src["table"] = table_dst.name
        spec_dst.write_text(json.dumps(spec_src), encoding="utf-8")
        assert main([str(spec_dst)]) == expected_code
        assert capsys.readouterr().out.strip()


def brute_force_solutions(model):
    """小定义域下暴力枚举全部整数解，作为求解器的对照基准。"""
    hidden = sorted(model.hidden)
    domains = []
    for r, c in hidden:
        lo, hi = _cell_bounds(model.bounds_spec, r, c)
        assert hi != float("inf")
        domains.append(range(lo, hi + 1))
    solutions = set()

    def cell_value(r, c, fill):
        if (r, c) in fill:
            return fill[(r, c)]
        return _as_int(model.matrix[r][c], "可见格")

    for combo in itertools.product(*domains):
        fill = dict(zip(hidden, combo))
        ok = True
        for r in model.data_rows:
            target = _as_int(model.matrix[r][model.row_total_col], "行小计")
            if sum(cell_value(r, c, fill) for c in model.data_cols) != target:
                ok = False
                break
        if not ok:
            continue
        for q in model.col_total_rows:
            for c in model.data_cols:
                target = _as_int(model.matrix[q][c], "列小计")
                if sum(cell_value(r, c, fill) for r in model.data_rows) != target:
                    ok = False
                    break
            if not ok:
                break
        if ok:
            solutions.add(tuple(sorted(fill.items())))
    return solutions


def test_solver_matches_brute_force_on_random_small_tables():
    rng = random.Random(20261113)
    n_rows, n_cols = 3, 3
    for _ in range(60):
        truth = [[rng.randint(0, 3) for _ in range(n_cols)] for _ in range(n_rows)]
        matrix = [["标签"] + [f"P{c}" for c in range(n_cols)] + ["行小计"]]
        hidden = set()
        for r in range(n_rows):
            row = [f"R{r}"]
            for c in range(n_cols):
                if rng.random() < 0.45:
                    row.append("?")
                    hidden.add((r + 1, c + 1))  # +1 换算成含表头的矩阵坐标
                else:
                    row.append(truth[r][c])
            row.append(sum(truth[r]))
            matrix.append(row)
        col_row = ["列小计"] + [sum(truth[r][c] for r in range(n_rows))
                                for c in range(n_cols)]
        col_row.append(sum(map(sum, truth)))
        matrix.append(col_row)
        if rng.random() < 0.3:  # 故意破坏一个行小计，制造无解情形
            bad = rng.randrange(n_rows)
            matrix[bad + 1][-1] += 1

        spec = {
            "header_rows": [0], "label_col": 0,
            "row_total_col": n_cols + 1,
            "col_total_rows": [n_rows + 1], "grand_total_row": n_rows + 1,
            "masks": ["?"], "bounds": {"default": [0, 3]},
        }
        if len(hidden) > 5:  # 控制暴力枚举规模
            continue
        model = parse_sheet(matrix, spec)
        assert model.hidden == hidden
        expected = brute_force_solutions(model)
        result = audit_sheet(model)
        if not expected:
            assert result.status == CONFLICT
            assert result.solutions == []
        elif len(expected) == 1:
            assert result.status == UNIQUE
            assert tuple(sorted(result.solution.items())) == next(iter(expected))
        else:
            assert result.status == MULTIPLE
            found = {tuple(sorted(sol.items())) for sol in result.solutions}
            assert found <= expected and len(found) == 2
