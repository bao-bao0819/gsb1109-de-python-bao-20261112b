"""遮盖格反推唯一性核查。

输入模型（见 examples/sales_audit/*.json）：

- 一张含行小计列、列小计行（可选含总计）的销售矩阵；
- 遮盖格：显式坐标、遮盖记号（mask）或空白格（空白按"未知"处理，绝不当零）；
- 业务上下限：默认区间 + 按格/行/列覆盖，区间端点可用 null 表示无穷。

求解方式是精确的整数约束求解（行/列小计方程 + 每格整数上下限，
传播 + 分支限界枚举，最多收集两个解），不做任何猜测：

- 恰好一个整数解   -> UNIQUE，逐格列出反推依据；
- 存在多个整数解   -> MULTIPLE，给出两张都满足整张表的完整填法；
- 无任何整数解     -> CONFLICT，单独指出小计/上下限矛盾，
  绝不会把"无解"误报成"隐藏成功"。
"""

from __future__ import annotations

import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

try:  # 入库环节使用 pyexcel-io 读取，工具本身复用同一条读取链路
    from pyexcel_io import get_data
except Exception:  # pragma: no cover - 环境缺依赖时给出明确提示
    get_data = None

INF = float("inf")
NODE_BUDGET = 200_000

UNIQUE = "UNIQUE"
MULTIPLE = "MULTIPLE"
CONFLICT = "CONFLICT"


class SpecError(ValueError):
    """表结构或规格本身不合法（区别于供应商小计矛盾）。"""


class NeedBoundsError(RuntimeError):
    """变量定义域无界且方程无法收敛，需要补充业务上限才能枚举。"""


# --------------------------------------------------------------------------- #
# 数据模型
# --------------------------------------------------------------------------- #
@dataclass
class SheetModel:
    sheet_name: str
    matrix: list[list[Any]]
    label_col: Optional[int]
    row_total_col: int
    col_total_rows: list[int]
    grand_total_row: Optional[int]
    header_rows: list[int]
    hidden: set[tuple[int, int]]
    data_rows: list[int]
    data_cols: list[int]
    bounds_spec: dict[str, Any]
    mask_token: str

    def cell_label(self, r: int, c: int) -> str:
        """给人看的格子定位，尽量带上行标签与列表头。"""
        parts = [f"行{r + 1}列{c + 1}"]
        names = []
        if self.label_col is not None:
            row_name = str(self.matrix[r][self.label_col]).strip()
            if row_name:
                names.append(row_name)
        if self.header_rows:
            header_r = self.header_rows[0]
            col_name = str(self.matrix[header_r][c]).strip()
            if col_name:
                names.append(col_name)
        return f"{parts[0]}（{'/'.join(names)}）" if names else parts[0]


@dataclass
class Constraint:
    label: str
    var_ids: list[int]
    const_sum: int  # 可见格之和
    target: int


@dataclass
class Variable:
    vid: int
    coord: tuple[int, int]
    lo: int
    hi: Any  # int 或 INF
    val: Optional[int] = None
    reason: Optional[str] = None

    @property
    def fixed(self) -> bool:
        return self.val is not None


@dataclass
class AuditResult:
    status: str
    sheet_name: str
    model: SheetModel
    solution: Optional[dict[tuple[int, int], int]] = None
    solutions: list[dict[tuple[int, int], int]] = field(default_factory=list)
    search_completed: bool = True
    needed_branch: bool = False
    contradictions: list[str] = field(default_factory=list)
    bases: dict[tuple[int, int], str] = field(default_factory=dict)
    constraints: list[Constraint] = field(default_factory=list)


# --------------------------------------------------------------------------- # # 读取与解析
# --------------------------------------------------------------------------- #
def load_workbook(path: str | Path, sheet_name: Optional[str] = None) -> tuple[str, list[list[Any]]]:
    """通过 pyexcel-io 读表，返回 (sheet 名, 二维矩阵)。"""
    if get_data is None:
        raise RuntimeError("未安装 pyexcel_io，无法读取销售表，请先安装依赖。")
    book = get_data(str(path))
    if not book:
        raise SpecError(f"{path} 中没有可读的 sheet")
    if sheet_name is None:
        sheet_name = next(iter(book))
    if sheet_name not in book:
        raise SpecError(f"sheet {sheet_name!r} 不存在，可选：{list(book)}")
    return sheet_name, book[sheet_name]


def _as_int(value: Any, where: str) -> int:
    if isinstance(value, bool):
        raise SpecError(f"{where} 是布尔值而非整数：{value!r}")
    if isinstance(value, int):
        return value
    if isinstance(value, float) and math.isfinite(value) and value.is_integer():
        return int(value)
    if isinstance(value, str) and re.fullmatch(r"-?\d+", value.strip()):
        return int(value.strip())
    raise SpecError(f"{where} 不是整数：{value!r}")


def _is_blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and value.strip() == "")


def _parse_bound(raw: Any, where: str) -> tuple[int, Any]:
    if not isinstance(raw, (list, tuple)) or len(raw) != 2:
        raise SpecError(f"{where} 的上下限必须是 [下限, 上限]：{raw!r}")
    lo_raw, hi_raw = raw
    lo = -math.inf if lo_raw is None else _as_int(lo_raw, f"{where} 下限")
    hi = INF if hi_raw is None else _as_int(hi_raw, f"{where} 上限")
    if lo > hi:
        raise SpecError(f"{where} 下限大于上限：{raw!r}")
    return lo, hi


def _cell_bounds(bounds: dict[str, Any], r: int, c: int) -> tuple[int, Any]:
    lo, hi = _parse_bound(bounds.get("default", [0, None]), "默认区间")
    for row_key, raw in (bounds.get("rows") or {}).items():
        if int(row_key) == r:
            blo, bhi = _parse_bound(raw, f"第{r + 1}行区间")
            lo, hi = max(lo, blo), min(hi, bhi)
    for col_key, raw in (bounds.get("cols") or {}).items():
        if int(col_key) == c:
            blo, bhi = _parse_bound(raw, f"第{c + 1}列区间")
            lo, hi = max(lo, blo), min(hi, bhi)
    cell_raw = (bounds.get("cells") or {}).get(f"{r},{c}")
    if cell_raw is not None:
        blo, bhi = _parse_bound(cell_raw, f"格({r + 1},{c + 1})区间")
        lo, hi = max(lo, blo), min(hi, bhi)
    return lo, hi


def parse_sheet(matrix: list[list[Any]], spec: dict[str, Any], sheet_name: str = "sheet") -> SheetModel:
    if not matrix:
        raise SpecError("空表，没有任何行")
    width = max(len(row) for row in matrix)
    if width < 2:
        raise SpecError("表至少需要一个数据列和一个行小计列")
    matrix = [list(row) + [None] * (width - len(row)) for row in matrix]

    ncols = width
    label_col = spec.get("label_col", 0)
    row_total_col = spec.get("row_total_col", ncols - 1)
    col_total_rows = list(spec.get("col_total_rows") or [])
    header_rows = list(spec.get("header_rows") or [])
    grand_total_row = spec.get("grand_total_row")
    masks = {str(m) for m in (spec.get("masks") or [])}
    if spec.get("mask") is not None:
        masks.add(str(spec["mask"]))

    if not (0 <= row_total_col < ncols):
        raise SpecError(f"行小计列越界：{row_total_col}")
    if label_col is not None and not (0 <= label_col < ncols):
        raise SpecError(f"标签列越界：{label_col}")
    if label_col == row_total_col:
        raise SpecError("标签列与行小计列不能是同一列")
    for r in col_total_rows:
        if not (0 <= r < len(matrix)):
            raise SpecError(f"列小计行越界：{r}")
    for r in header_rows:
        if not (0 <= r < len(matrix)):
            raise SpecError(f"表头行越界：{r}")
    if set(header_rows) & set(col_total_rows):
        raise SpecError("表头行与列小计行不能重叠")
    if grand_total_row is not None and grand_total_row not in col_total_rows:
        raise SpecError("grand_total_row 必须同时出现在 col_total_rows 中")

    data_rows = [
        r for r in range(len(matrix))
        if r not in col_total_rows and r not in header_rows
    ]
    data_cols = [c for c in range(ncols) if c != row_total_col and c != label_col]
    if not data_rows or not data_cols:
        raise SpecError("表中没有可核查的数据格")

    hidden: set[tuple[int, int]] = {
        (int(r), int(c)) for r, c in (spec.get("hidden") or [])
    }
    for r, c in hidden:
        if r not in data_rows or c not in data_cols:
            raise SpecError(f"显式遮盖坐标不在数据区内：({r + 1},{c + 1})")

    # 扫描数据区：空白/遮盖记号 -> 未知；其余必须是整数。空白绝不当零。
    for r in data_rows:
        for c in data_cols:
            value = matrix[r][c]
            if _is_blank(value) or (isinstance(value, str) and value.strip() in masks):
                hidden.add((r, c))
            else:
                _as_int(value, f"数据格({r + 1},{c + 1})")

    # 小计/总计单元格必须公开且为整数，缺了直接报规格问题。
    for r in data_rows:
        if _is_blank(matrix[r][row_total_col]):
            raise SpecError(f"第{r + 1}行的行小计缺失，无法建立行约束")
        _as_int(matrix[r][row_total_col], f"行小计({r + 1},{row_total_col + 1})")
    for q in col_total_rows:
        for c in data_cols:
            if _is_blank(matrix[q][c]):
                raise SpecError(f"第{q + 1}行的列小计缺失（列{c + 1}），无法建立列约束")
            _as_int(matrix[q][c], f"列小计({q + 1},{c + 1})")
    if grand_total_row is not None:
        gt_cell = matrix[grand_total_row][row_total_col]
        if _is_blank(gt_cell):
            raise SpecError("声明了总计行但总计单元格为空")
        _as_int(gt_cell, "总计单元格")

    return SheetModel(
        sheet_name=sheet_name,
        matrix=matrix,
        label_col=label_col,
        row_total_col=row_total_col,
        col_total_rows=col_total_rows,
        grand_total_row=grand_total_row,
        header_rows=header_rows,
        hidden=hidden,
        data_rows=data_rows,
        data_cols=data_cols,
        bounds_spec=spec.get("bounds") or {},
        mask_token=next(iter(masks), "？"),
    )


# --------------------------------------------------------------------------- #
# 建模
# --------------------------------------------------------------------------- #
def build_constraints(model: SheetModel) -> tuple[list[Variable], list[Constraint]]:
    variables: dict[tuple[int, int], Variable] = {}
    for vid, (r, c) in enumerate(sorted(model.hidden)):
        lo, hi = _cell_bounds(model.bounds_spec, r, c)
        if lo == hi:
            variables[(r, c)] = Variable(vid, (r, c), lo, hi, val=lo,
                                         reason=f"业务上下限均为 {lo}，该格被区间钉死")
        else:
            variables[(r, c)] = Variable(vid, (r, c), lo, hi)

    constraints: list[Constraint] = []

    def make_constraint(label: str, cells: list[tuple[int, int]], target: int) -> Constraint:
        var_ids: list[int] = []
        const_sum = 0
        for coord in cells:
            if coord in variables:
                var_ids.append(variables[coord].vid)
            else:
                const_sum += _as_int(model.matrix[coord[0]][coord[1]],
                                     f"可见格{coord}")
        return Constraint(label=label, var_ids=var_ids, const_sum=const_sum, target=target)

    for r in model.data_rows:
        cells = [(r, c) for c in model.data_cols]
        target = _as_int(matrix_get(model, r, model.row_total_col), f"行{r + 1}小计")
        constraints.append(make_constraint(f"行{r + 1}小计={target}", cells, target))

    for q in model.col_total_rows:
        for c in model.data_cols:
            cells = [(r, c) for r in model.data_rows]
            target = _as_int(matrix_get(model, q, c), f"列{c + 1}小计")
            constraints.append(
                make_constraint(f"列{c + 1}小计={target}（小计行{q + 1}）", cells, target)
            )

    return list(variables.values()), constraints


def matrix_get(model: SheetModel, r: int, c: int) -> Any:
    return model.matrix[r][c]


# --------------------------------------------------------------------------- #
# 小计矛盾的显式算术诊断（不靠求解器兜底，直接指出矛盾数字）
# --------------------------------------------------------------------------- #
def arithmetic_checks(model: SheetModel) -> list[str]:
    problems: list[str] = []
    row_targets = [
        _as_int(model.matrix[r][model.row_total_col], f"行{r + 1}小计")
        for r in model.data_rows
    ]
    row_sum = sum(row_targets)

    col_sums: list[int] = []
    for q in model.col_total_rows:
        total = sum(
            _as_int(model.matrix[q][c], f"列小计({q + 1},{c + 1})")
            for c in model.data_cols
        )
        col_sums.append(total)
        if total != row_sum:
            problems.append(
                f"小计行{q + 1}各列小计之和={total}，与各行小计之和={row_sum}不一致"
            )

    if model.grand_total_row is not None:
        grand = _as_int(
            model.matrix[model.grand_total_row][model.row_total_col], "总计"
        )
        if grand != row_sum:
            problems.append(
                f"总计={grand}，但各行小计之和={row_sum}（差 {grand - row_sum:+d}）"
            )
        for q, total in zip(model.col_total_rows, col_sums):
            if total != grand:
                problems.append(
                    f"小计行{q + 1}列小计之和={total}，与总计={grand}不一致"
                )

    # 用每格上下限给出每行/每列可行和的闭区间，落不进区间就是硬矛盾。
    def feasible_range(cells: list[tuple[int, int]]) -> tuple[int, Any]:
        lo_sum = 0
        hi_sum: Any = 0
        for r, c in cells:
            if (r, c) in model.hidden:
                lo, hi = _cell_bounds(model.bounds_spec, r, c)
            else:
                value = _as_int(model.matrix[r][c], f"可见格({r + 1},{c + 1})")
                lo, hi = value, value
            lo_sum += lo
            hi_sum = hi_sum + hi
        return lo_sum, hi_sum

    for r, target in zip(model.data_rows, row_targets):
        lo_sum, hi_sum = feasible_range([(r, c) for c in model.data_cols])
        if not lo_sum <= target <= hi_sum:
            problems.append(
                f"第{r + 1}行：可见格与遮盖格按上下限只能凑出 [{lo_sum}, {_fmt_inf(hi_sum)}]，"
                f"行小计却声明为 {target}"
            )
    for q in model.col_total_rows:
        for c in model.data_cols:
            target = _as_int(model.matrix[q][c], f"列小计({q + 1},{c + 1})")
            lo_sum, hi_sum = feasible_range([(r, c) for r in model.data_rows])
            if not lo_sum <= target <= hi_sum:
                problems.append(
                    f"第{c + 1}列：按上下限只能凑出 [{lo_sum}, {_fmt_inf(hi_sum)}]，"
                    f"列小计却声明为 {target}"
                )
    return problems


def _fmt_inf(value: Any) -> str:
    return "∞" if value == INF else str(value)


# --------------------------------------------------------------------------- #
# 精确整数求解：约束传播 + 分支限界，最多收集两个解
# --------------------------------------------------------------------------- #
def _propagate(variables: list[Variable], constraints: list[Constraint]) -> Optional[str]:
    """单点传播至不动点。返回 None 表示目前相容，否则返回矛盾描述。"""
    by_id = {v.vid: v for v in variables}
    changed = True
    while changed:
        changed = False
        for con in constraints:
            free = [by_id[i] for i in con.var_ids if not by_id[i].fixed]
            fixed_sum = con.const_sum + sum(by_id[i].val for i in con.var_ids if by_id[i].fixed)
            sum_lo = sum(v.lo for v in free)
            sum_hi: Any = sum((v.hi for v in free), start=0)
            if not (fixed_sum + sum_lo <= con.target <= fixed_sum + sum_hi):
                return (
                    f"{con.label}：当前定义域下可行和区间为 "
                    f"[{fixed_sum + sum_lo}, {_fmt_inf(fixed_sum + sum_hi)}]，"
                    f"无法等于目标 {con.target}"
                )
            for v in free:
                others_lo = sum(u.lo for u in free if u is not v)
                others_hi: Any = sum((u.hi for u in free if u is not v), start=0)
                new_lo = max(v.lo, con.target - fixed_sum - others_hi)
                new_hi = min(v.hi, con.target - fixed_sum - others_lo)
                if isinstance(new_lo, float) and math.isinf(new_lo):
                    new_lo = v.lo
                if new_lo > new_hi:
                    return f"{con.label} 收紧后 {model_cell(v)} 定义域为空"
                if new_lo != v.lo or new_hi != v.hi:
                    v.lo, v.hi = int(new_lo), (INF if new_hi == INF else int(new_hi))
                    changed = True
                if v.lo == v.hi and not v.fixed:
                    v.val = int(v.lo)
                    v.reason = f"{con.label}：定义域被收紧为单点，该格只能取 {v.val}"
                    changed = True
    return None


def model_cell(v: Variable) -> str:
    return f"({v.coord[0] + 1},{v.coord[1] + 1})"


def _clone_state(variables: list[Variable]) -> list[Variable]:
    return [
        Variable(v.vid, v.coord, v.lo, v.hi, v.val, v.reason) for v in variables
    ]


def solve(variables: list[Variable], constraints: list[Constraint]) -> tuple[
    list[dict[tuple[int, int], int]], Optional[list[Variable]], bool, bool, Optional[str]
]:
    """返回 (至多两个解, 首个解的变量状态(含依据), 搜索是否完整, 是否分支, 错误)。"""
    solutions: list[dict[tuple[int, int], int]] = []
    solved_state: Optional[list[Variable]] = None
    state = {"completed": True, "branched": False, "nodes": 0, "error": None}

    def search(vars_: list[Variable]) -> None:
        nonlocal solved_state
        state["nodes"] += 1
        if state["nodes"] > NODE_BUDGET:
            state["completed"] = False
            state["error"] = (
                f"搜索节点超过预算 {NODE_BUDGET}，请补充更窄的业务上下限后再核查"
            )
            return
        conflict = _propagate(vars_, constraints)
        if conflict is not None:
            return
        unfixed = [v for v in vars_ if not v.fixed]
        if not unfixed:
            solutions.append({v.coord: v.val for v in vars_})
            if solved_state is None:
                solved_state = vars_
            return
        finite = [
            v for v in unfixed
            if v.hi != INF and v.lo != -math.inf
        ]
        if not finite:
            state["completed"] = False
            state["error"] = "存在定义域无界的自由格（缺业务下限或上限），无法保证枚举完整"
            return
        state["branched"] = True
        pivot = min(finite, key=lambda v: (v.hi - v.lo, v.vid))
        mid = pivot.lo + (pivot.hi - pivot.lo) // 2
        for split_lo, split_hi in ((pivot.lo, mid), (mid + 1, pivot.hi)):
            if split_lo > split_hi or len(solutions) >= 2 or state["error"]:
                continue
            child = _clone_state(vars_)
            child_var = next(v for v in child if v.vid == pivot.vid)
            child_var.lo, child_var.hi = split_lo, split_hi
            search(child)

    search(_clone_state(variables))
    return solutions, solved_state, state["completed"], state["branched"], state["error"]


# --------------------------------------------------------------------------- #
# 顶层核查流程
# --------------------------------------------------------------------------- #
def audit_sheet(model: SheetModel) -> AuditResult:
    contradictions = arithmetic_checks(model)
    variables, constraints = build_constraints(model)

    result = AuditResult(
        status=CONFLICT if contradictions else UNIQUE,
        sheet_name=model.sheet_name,
        model=model,
        contradictions=contradictions,
        constraints=constraints,
    )
    if contradictions:
        # 小计本身已经打架，不再继续反推，避免把无解包装成成功。
        return result

    if not variables:
        result.status = UNIQUE
        result.solution = {}
        return result

    solutions, solved_state, completed, branched, error = solve(variables, constraints)
    result.search_completed = completed
    result.needed_branch = branched
    if error is not None:
        raise NeedBoundsError(error)
    if not solutions:
        result.status = CONFLICT
        result.contradictions.append(
            "行/列小计与业务上下限联立后无任何整数解（供应商数据矛盾）"
        )
        return result

    result.solutions = solutions
    result.solution = solutions[0]
    if len(solutions) == 1:
        result.status = UNIQUE
        fallback = ("分支限界枚举全部可行分支后该格取值唯一"
                    if branched else "方程联立后唯一确定")
        result.bases = {
            v.coord: (v.reason or fallback) for v in (solved_state or variables)
        }
    else:
        result.status = MULTIPLE
    return result


# --------------------------------------------------------------------------- #
# 报告渲染
# --------------------------------------------------------------------------- #
def _render_table(model: SheetModel, fill: Optional[dict[tuple[int, int], int]]) -> str:
    matrix = model.matrix
    widths = [1] * len(matrix[0])
    display: list[list[str]] = []
    for r, row in enumerate(matrix):
        line: list[str] = []
        for c, value in enumerate(row):
            if fill is not None and (r, c) in fill:
                text = str(fill[(r, c)])
            elif (r, c) in model.hidden:
                text = f"[{model.mask_token}]"
            elif _is_blank(value):
                text = ""
            else:
                text = str(value)
            line.append(text)
            widths[c] = max(widths[c], _display_width(text))
        display.append(line)

    def pad(text: str, width: int) -> str:
        return text + " " * (width - _display_width(text))

    border = "+" + "+".join("-" * (w + 2) for w in widths) + "+"
    lines = [border]
    for r, line in enumerate(display):
        lines.append("| " + " | ".join(pad(text, widths[c]) for c, text in enumerate(line)) + " |")
        if r in model.col_total_rows or r in model.header_rows:
            lines.append(border)
    if lines[-1] != border:
        lines.append(border)
    return "\n".join(lines)


def _display_width(text: str) -> int:
    import unicodedata

    return sum(2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1 for ch in text)


def format_report(result: AuditResult) -> str:
    model = result.model
    out: list[str] = []
    out.append(f"核查 sheet：{model.sheet_name}")
    out.append(f"遮盖格数量：{len(model.hidden)}（空白格按未知处理，不当作零）")
    out.append("")
    out.append("原始表（[ ] 内为遮盖/空白格）：")
    out.append(_render_table(model, None))

    if result.status == CONFLICT:
        out.append("")
        out.append("结论：供应商小计/上下限矛盾，不存在任何满足整张表的整数填法。")
        for i, problem in enumerate(result.contradictions, 1):
            out.append(f"  {i}. {problem}")
        out.append("注意：这是数据矛盾，不是遮盖反推成功，请勿入库。")
        return "\n".join(out)

    if result.status == UNIQUE:
        out.append("")
        out.append("结论：遮盖格可由公开的行小计、列小计与业务上限反推出唯一整数解。")
        out.append("")
        out.append("反推后的完整表：")
        out.append(_render_table(model, result.solution))
        out.append("")
        out.append("逐格依据：")
        for coord in sorted(result.bases):
            out.append(f"  - {model.cell_label(*coord)} = {result.solution[coord]}；{result.bases[coord]}")
        if result.needed_branch:
            out.append("")
            out.append("说明：部分格子需要联立全部行/列约束做分支限界枚举，"
                       "所有可行分支均指向同一组值，唯一性已全域验证。")
        return "\n".join(out)

    # MULTIPLE
    out.append("")
    out.append("结论：遮盖格不能唯一确定，存在多组都满足整张表的整数填法。")
    if not result.search_completed:
        out.append("（搜索未跑满，以下至少是两组可行填法）")
    for i, fill in enumerate(result.solutions, 1):
        out.append("")
        out.append(f"可行填法 {i}：")
        out.append(_render_table(model, fill))
    first, second = result.solutions[0], result.solutions[1]
    diffs = sorted(k for k in first if first[k] != second[k])
    if diffs:
        out.append("")
        out.append("两组填法取值不同的遮盖格（证明不唯一）：")
        for coord in diffs:
            out.append(
                f"  - {model.cell_label(*coord)}：填法一={first[coord]}，填法二={second[coord]}"
            )
    out.append("")
    out.append("处理建议：留在待核区，要求供应商补充公开信息后再入库。")
    return "\n".join(out)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def run_spec(spec_path: str | Path) -> AuditResult:
    spec_path = Path(spec_path)
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    table_path = spec.get("table")
    if table_path:
        table_path = (spec_path.parent / table_path).resolve()
        sheet_name, matrix = load_workbook(
            table_path, spec.get("sheet_name")
        )
    else:
        raise SpecError("spec 中缺少 table 字段（销售表路径，相对 spec 解析）")
    model = parse_sheet(matrix, spec, sheet_name=spec.get("sheet_name") or sheet_name)
    return audit_sheet(model)


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) != 1:
        print("用法：python -m sales_audit <核查规格.json>", file=sys.stderr)
        return 1
    try:
        result = run_spec(argv[0])
    except (SpecError, NeedBoundsError, RuntimeError) as exc:
        print(f"核查未能进行：{exc}", file=sys.stderr)
        return 1
    print(format_report(result))
    if result.status == CONFLICT:
        return 2
    if result.status == MULTIPLE:
        return 3
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
