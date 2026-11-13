"""CSVZ 质检抽检覆盖核对引擎（纯标准库）。

业务规则（来自任务口径，逐条落实）：
1. CSVZ 每个页签（zip 内一个 csv）= 一班的抽检单；班别与产线总量、允许抽样的层、
   每层最少件数由随包 manifest 另行给出。
2. 读整包核对覆盖：按“班别 × 层”单元及整包分层汇总，与每层最少件数比对。
3. 返修件跨班重检只能归入一个原始层：同一物理件按规范件号去重，显式原始层/原始班
   即强制归位，整包只计一次，不在重检班重复计数。
4. 同一件来源有多个可能时保持歧义，绝不任选一个再宣称达标；歧义件只进入“最好情况”，
   不进入“保底（最坏）情况”。
5. 能达标但无保证的层给出最好/最坏覆盖量区间；最坏仍足额=保证达标，最好仍不足=确定不足。
6. 缺页（manifest 有、zip 内无）不得当零产量：manifest 中班别产线总量>0 即记为真实缺口。
7. 补采待办只列仍可能改变达标结论的缺页；已足额（保底即达标）的层不再催办。
"""

from __future__ import annotations

import csv
import io
import json
import re
import zipfile
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_COLUMNS = {
    "item_id": ["件号", "编号", "产品编号", "item_id", "id"],
    "layer": ["层", "层级", "抽样层", "layer"],
    "record_type": ["类型", "单据类型", "record_type", "type"],
    "origin_item_id": ["原始件号", "原编号", "origin_item_id"],
    "origin_layer": ["原始层", "原层级", "origin_layer"],
    "origin_shift": ["原始班别", "原班别", "origin_shift"],
    "candidate_layers": ["候选层", "可能来源层", "candidate_layers"],
}
REPAIR_VALUES = {"返修", "返修重检", "重检", "repair", "rework"}
CANDIDATE_SPLIT = re.compile(r"[;,，、/|]")


@dataclass
class Record:
    shift: str
    item_id: str
    layers: list[str]
    is_repair: bool
    origin_item_id: Optional[str]
    origin_layer: Optional[str]
    origin_shift: Optional[str]


@dataclass
class ItemAgg:
    canonical_id: str
    seen_shifts: set[str] = field(default_factory=set)
    candidate_cells: set[tuple[str, str]] = field(default_factory=set)
    forced_cell: Optional[tuple[str, str]] = None
    repair: bool = False
    cross_shift_unresolved: bool = False
    invalid_layers: list[str] = field(default_factory=list)


def _read_text(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "gb18030", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def read_zip_sheets(csvz_path: str) -> dict[str, list[list[str]]]:
    sheets: dict[str, list[list[str]]] = {}
    with zipfile.ZipFile(csvz_path) as zf:
        for name in zf.namelist():
            if not name.lower().endswith(".csv"):
                continue
            rows = list(csv.reader(io.StringIO(_read_text(zf.read(name)))))
            sheets[name] = [row for row in rows if any(cell.strip() for cell in row)]
    return sheets


def _pick_column(headers: list[str], aliases: list[str]) -> Optional[int]:
    normalized = [h.strip().lower() for h in headers]
    for alias in aliases:
        if alias.strip().lower() in normalized:
            return normalized.index(alias.strip().lower())
    return None


def _cell(row: list[str], idx: Optional[int]) -> str:
    if idx is None or idx >= len(row):
        return ""
    return row[idx].strip()


def _split_layers(value: str) -> list[str]:
    if not value:
        return []
    parts = CANDIDATE_SPLIT.split(value)
    return [p.strip() for p in parts if p.strip()]


def parse_records(shift: str, rows: list[list[str]], colmap: dict) -> list[Record]:
    if not rows:
        return []
    headers = rows[0]
    idx = {
        key: _pick_column(headers, colmap.get(key, DEFAULT_COLUMNS.get(key, [])))
        for key in DEFAULT_COLUMNS
    }
    records: list[Record] = []
    for row in rows[1:]:
        item_id = _cell(row, idx["item_id"])
        if not item_id:
            continue
        type_value = _cell(row, idx["record_type"])
        is_repair = type_value in REPAIR_VALUES
        candidate_value = _cell(row, idx["candidate_layers"])
        layers = _split_layers(candidate_value) or _split_layers(
            _cell(row, idx["layer"])
        )
        records.append(
            Record(
                shift=shift,
                item_id=item_id,
                layers=layers,
                is_repair=is_repair,
                origin_item_id=_cell(row, idx["origin_item_id"]) or None,
                origin_layer=_cell(row, idx["origin_layer"]) or None,
                origin_shift=_cell(row, idx["origin_shift"]) or None,
            )
        )
    return records


def match_shifts(sheet_names: list[str], shifts: list[dict]):
    matched: dict[str, str] = {}
    used_members: set[str] = set()
    for shift in shifts:
        sid = shift["shift_id"]
        want = {sid, shift.get("sheet_name", "")}
        want = {w for w in want if w}
        choice = None
        for member in sheet_names:
            base = member.rsplit("/", 1)[-1][:-4]
            if base in want or member in want:
                choice = member
                break
        if choice is None:
            for member in sheet_names:
                base = member.rsplit("/", 1)[-1][:-4]
                if sid in base or base in sid:
                    choice = member
                    break
        if choice:
            matched[sid] = choice
            used_members.add(choice)
    missing = [s["shift_id"] for s in shifts if s["shift_id"] not in matched]
    unexpected = [m for m in sheet_names if m not in used_members]
    return matched, missing, unexpected


def build_items(
    records: list[Record], shift_order: dict[str, int], allowed: dict[str, set[str]]
) -> tuple[dict[str, ItemAgg], list[dict]]:
    items: dict[str, ItemAgg] = {}
    anomalies: list[dict] = []
    for rec in records:
        canonical = rec.origin_item_id if (rec.is_repair and rec.origin_item_id) else rec.item_id
        agg = items.setdefault(canonical, ItemAgg(canonical_id=canonical))
        agg.seen_shifts.add(rec.shift)
        agg.repair = agg.repair or rec.is_repair
        allowed_here = allowed.get(rec.shift, set())
        valid_layers = [layer for layer in rec.layers if not allowed_here or layer in allowed_here]
        invalid = [layer for layer in rec.layers if allowed_here and layer not in allowed_here]
        if invalid:
            agg.invalid_layers.extend(invalid)
            anomalies.append(
                {"type": "层不在允许抽样范围", "item": canonical, "shift": rec.shift, "layers": invalid}
            )
        owner_shift = rec.origin_shift or rec.shift
        if rec.origin_layer:
            forced = (owner_shift, rec.origin_layer)
            if agg.forced_cell and agg.forced_cell != forced:
                anomalies.append(
                    {"type": "原始层声明冲突", "item": canonical, "cells": [agg.forced_cell, forced]}
                )
            agg.forced_cell = forced
        else:
            for layer in valid_layers:
                agg.candidate_cells.add((owner_shift, layer))

    for agg in items.values():
        if agg.forced_cell:
            agg.candidate_cells = {agg.forced_cell}
        elif len({shift for shift, _ in agg.candidate_cells}) > 1:
            agg.cross_shift_unresolved = True
            anomalies.append(
                {"type": "跨班件缺少原始层声明，保留歧义不任选", "item": agg.canonical_id,
                 "cells": sorted(agg.candidate_cells)}
            )
        if not agg.candidate_cells:
            anomalies.append({"type": "无有效归层，不计入任何层", "item": agg.canonical_id})
    return items, anomalies


def _quota(shift_def: dict, layer: str, global_default: int) -> int:
    per_layer = shift_def.get("min_per_layer", {})
    if layer in per_layer:
        return int(per_layer[layer])
    if "min_per_layer_default" in shift_def:
        return int(shift_def["min_per_layer_default"])
    return int(global_default)


def evaluate(manifest: dict, sheets: dict[str, list[list[str]]], colmap: dict):
    shift_defs = manifest["shifts"]
    shift_order = {s["shift_id"]: i for i, s in enumerate(shift_defs)}
    allowed = {s["shift_id"]: set(s.get("allowed_layers", [])) for s in shift_defs}
    global_default = int(manifest.get("min_per_layer_default", 0))
    sheet_names = sorted(sheets)
    matched, missing_shifts, unexpected = match_shifts(sheet_names, shift_defs)

    all_records: list[Record] = []
    for sid, member in matched.items():
        all_records.extend(parse_records(sid, sheets[member], colmap))
    items, anomalies = build_items(all_records, shift_order, allowed)

    definite: dict[tuple[str, str], set[str]] = {}
    ambiguous_incident: dict[tuple[str, str], set[str]] = {}
    ambiguous_items: list[dict] = []
    repair_decisions: list[dict] = []
    for canonical, agg in items.items():
        cells = agg.candidate_cells
        if len(cells) == 1:
            definite.setdefault(next(iter(cells)), set()).add(canonical)
        elif len(cells) > 1:
            ambiguous_items.append(
                {"item": canonical, "candidate_cells": sorted(cells),
                 "repair": agg.repair, "cross_shift_unresolved": agg.cross_shift_unresolved}
            )
            for cell in cells:
                ambiguous_incident.setdefault(cell, set()).add(canonical)
        if agg.repair:
            repair_decisions.append(
                {"item": canonical, "seen_shifts": sorted(agg.seen_shifts),
                 "attribution": sorted(cells) if cells else [],
                 "forced": list(agg.forced_cell) if agg.forced_cell else None,
                 "counted_once": len(cells) <= 1}
            )

    cells_report: list[dict] = []
    layer_definite: dict[str, set[str]] = {}
    layer_ambiguous: dict[str, set[str]] = {}
    layer_quota: dict[str, int] = {}
    layer_shifts: dict[str, list[str]] = {}
    for shift_def in shift_defs:
        sid = shift_def["shift_id"]
        for layer in shift_def.get("allowed_layers", []):
            cell = (sid, layer)
            quota = _quota(shift_def, layer, global_default)
            definite_items = definite.get(cell, set())
            ambiguous_items_here = ambiguous_incident.get(cell, set())
            d = len(definite_items)
            b = d + len(ambiguous_items_here)
            status = _status(d, b, quota)
            entry = {"shift": sid, "layer": layer, "quota": quota,
                     "worst": d, "best": b, "status": status,
                     "ambiguous_items": sorted(ambiguous_items_here)}
            cells_report.append(entry)
            layer_definite.setdefault(layer, set()).update(definite_items)
            layer_ambiguous.setdefault(layer, set()).update(ambiguous_items_here)
            layer_quota[layer] = layer_quota.get(layer, 0) + quota
            layer_shifts.setdefault(layer, []).append(sid)

    layer_rollup = []
    for layer in sorted(layer_quota):
        definite_set = layer_definite.get(layer, set())
        best_set = definite_set | layer_ambiguous.get(layer, set())
        worst = len(definite_set)
        best = len(best_set)
        layer_rollup.append(
            {"layer": layer, "quota_total": layer_quota[layer], "worst": worst,
             "best": best, "shifts": layer_shifts.get(layer, []),
             "status": _status(worst, best, layer_quota[layer])}
        )

    todos = _build_todos(shift_defs, missing_shifts, layer_rollup, cells_report)
    missing_detail = [
        {"shift": sid,
         "production_total": next(s.get("production_total") for s in shift_defs if s["shift_id"] == sid),
         "allowed_layers": next(s.get("allowed_layers", []) for s in shift_defs if s["shift_id"] == sid)}
        for sid in missing_shifts
    ]
    for detail in missing_detail:
        detail["missing_page_is_not_zero"] = (detail["production_total"] or 0) > 0

    return {
        "sheets_present": sorted(matched.values()),
        "shifts_expected": [s["shift_id"] for s in shift_defs],
        "missing_shifts": missing_detail,
        "unexpected_sheets": unexpected,
        "cells": cells_report,
        "layer_rollup": layer_rollup,
        "ambiguous_items": ambiguous_items,
        "repair_decisions": repair_decisions,
        "anomalies": anomalies,
        "recollection_todos": todos,
    }


def _status(worst: int, best: int, quota: int) -> str:
    if quota <= 0:
        return "无定额"
    if worst >= quota:
        return "保证达标"
    if best < quota:
        return "确定不足"
    return "能达标但无保证"


def _build_todos(shift_defs, missing_shifts, layer_rollup, cells_report):
    guaranteed_layers = {r["layer"] for r in layer_rollup if r["status"] == "保证达标"}
    cell_index = {(c["shift"], c["layer"]): c for c in cells_report}
    todos = []
    for sid in missing_shifts:
        shift_def = next(s for s in shift_defs if s["shift_id"] == sid)
        actionable = []
        for layer in shift_def.get("allowed_layers", []):
            if layer in guaranteed_layers:
                continue
            cell = cell_index.get((sid, layer))
            gap = cell["quota"] - cell["best"] if cell else shift_def.get("min_per_layer", {}).get(layer, 0)
            if gap and gap > 0:
                actionable.append({"layer": layer, "shortfall_vs_best": gap})
        if actionable:
            todos.append({"missing_shift": sid, "production_total": shift_def.get("production_total"),
                          "can_still_affect": actionable})
    return todos


def init_manifest(csvz_path: str) -> dict:
    sheets = read_zip_sheets(csvz_path)
    shifts = []
    for member in sorted(sheets):
        rows = sheets[member]
        base = member.rsplit("/", 1)[-1][:-4]
        observed_layers = []
        if rows:
            headers = rows[0]
            layer_idx = _pick_column(headers, DEFAULT_COLUMNS["layer"])
            if layer_idx is not None:
                seen = []
                for row in rows[1:]:
                    for layer in _split_layers(_cell(row, layer_idx)):
                        if layer not in seen:
                            seen.append(layer)
                observed_layers = seen
        shifts.append({"shift_id": base, "sheet_name": member, "production_total": None,
                       "allowed_layers": observed_layers,
                       "min_per_layer": {layer: 0 for layer in observed_layers}})
    return {"min_per_layer_default": 0, "columns": {}, "shifts": shifts}


def to_json(report: dict) -> str:
    return json.dumps(report, ensure_ascii=False, indent=2)


def to_markdown(report: dict) -> str:
    lines = ["# 质检抽检覆盖核对报告", "", "## 页签覆盖核对"]
    lines.append(f"- 应有班别：{', '.join(report['shifts_expected']) or '（空）'}")
    lines.append(f"- 实际页签：{', '.join(report['sheets_present']) or '（无）'}")
    if report["unexpected_sheets"]:
        lines.append(f"- ⚠  manifest 未声明的页签：{', '.join(report['unexpected_sheets'])}")
    if report["missing_shifts"]:
        lines.append("- 缺页（缺页≠零产量）：")
        for detail in report["missing_shifts"]:
            note = f"产线总量={detail['production_total']}"
            if detail["missing_page_is_not_zero"]:
                note += "（>0，确有产出，不得按零产量处理）"
            lines.append(f"  - {detail['shift']}：{note}，允许层={detail['allowed_layers']}")
    else:
        lines.append("- 无缺页。")

    lines += ["", "## 分层覆盖（整包汇总）", "| 层 | 每层最少件数(合计) | 最坏覆盖 | 最好覆盖 | 结论 |", "|---|---|---|---|---|"]
    for roll in report["layer_rollup"]:
        lines.append(f"| {roll['layer']} | {roll['quota_total']} | {roll['worst']} | {roll['best']} | {roll['status']} |")

    lines += ["", "## 班别 × 层 明细", "| 班别 | 层 | 最少件数 | 最坏 | 最好 | 结论 |", "|---|---|---|---|---|---|"]
    for cell in report["cells"]:
        lines.append(
            f"| {cell['shift']} | {cell['layer']} | {cell['quota']} | {cell['worst']} | {cell['best']} | {cell['status']} |"
        )

    if report["ambiguous_items"]:
        lines += ["", "## 来源歧义件（未任选，不计入保底）"]
        for item in report["ambiguous_items"]:
            cells = "；".join(f"{s}/{layer}" for s, layer in item["candidate_cells"])
            tag = "，跨班未声明原始层" if item["cross_shift_unresolved"] else ""
            lines.append(f"- {item['item']}{tag}：候选 {cells}")

    if report["repair_decisions"]:
        lines += ["", "## 返修跨班重检归位（每件只计一次）"]
        for decision in report["repair_decisions"]:
            attr = "；".join(f"{s}/{layer}" for s, layer in decision["attribution"]) or "无有效归层"
            forced = f"，强制归入 {decision['forced'][0]}/{decision['forced'][1]}" if decision["forced"] else ""
            lines.append(
                f"- {decision['item']}：出现于 {', '.join(decision['seen_shifts'])}，归位 {attr}{forced}"
            )

    if report["anomalies"]:
        lines += ["", "## 数据异常"]
        for anomaly in report["anomalies"]:
            lines.append(f"- {anomaly}")

    lines += ["", "## 补采待办（仅列仍可能影响达标结论的缺页）"]
    if report["recollection_todos"]:
        for todo in report["recollection_todos"]:
            gaps = "；".join(f"{x['layer']} 差{x['shortfall_vs_best']}件" for x in todo["can_still_affect"])
            lines.append(f"- 补采班别 {todo['missing_shift']}（产线总量 {todo['production_total']}）：{gaps}")
    else:
        lines.append("- 无：缺页涉及的层均已保底足额，或不存在缺页。")
    return "\n".join(lines)
