import io
import json
import os
import sys
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from qa_audit import audit_csvz
from qa_audit import cli

HEADER = ["件号", "层", "类型", "原始件号", "原始层", "原始班别", "候选层"]

ZAOBAN = [
    HEADER,
    ["a1", "A", "", "", "", "", ""],
    ["a2", "A", "", "", "", "", ""],
    ["r1", "A", "", "", "", "", ""],
    ["b1", "B", "", "", "", "", ""],
    ["X", "", "", "", "", "", "A;B"],
    ["Y", "B", "", "", "", "", ""],
]

ZHONGBAN = [
    HEADER,
    ["a3", "A", "", "", "", "", ""],
    ["a4", "A", "", "", "", "", ""],
    ["a5", "A", "", "", "", "", ""],
    ["b2", "B", "", "", "", "", ""],
    ["Y", "B", "", "", "", "", ""],
    ["r1", "A", "返修", "r1", "A", "早班", ""],
]


def _csv_text(rows):
    buffer = io.StringIO()
    import csv

    writer = csv.writer(buffer)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _make_package(tmp_path):
    path = tmp_path / "book.csvz"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("早班.csv", _csv_text(ZAOBAN))
        zf.writestr("中班.csv", _csv_text(ZHONGBAN))
        zf.writestr("临时页.csv", _csv_text([HEADER]))
    return str(path)


def _manifest():
    return {
        "min_per_layer_default": 2,
        "columns": {},
        "shifts": [
            {"shift_id": "早班", "production_total": 480, "allowed_layers": ["A", "B", "C"],
             "min_per_layer": {"A": 2, "B": 2, "C": 2}},
            {"shift_id": "中班", "production_total": 510, "allowed_layers": ["A", "B", "C"],
             "min_per_layer": {"A": 2, "B": 2, "C": 2}},
            {"shift_id": "晚班", "production_total": 500, "allowed_layers": ["A", "B", "C"],
             "min_per_layer": {"A": 2, "B": 2, "C": 2}},
        ],
    }


def _report(tmp_path):
    csvz = _make_package(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(), ensure_ascii=False), encoding="utf-8")
    sheets = audit_csvz.read_zip_sheets(csvz)
    return audit_csvz.evaluate(json.loads(manifest_path.read_text("utf-8")), sheets, {})


def test_missing_page_not_zero(tmp_path):
    report = _report(tmp_path)
    missing = {item["shift"]: item for item in report["missing_shifts"]}
    assert "晚班" in missing
    assert missing["晚班"]["production_total"] == 500
    assert missing["晚班"]["missing_page_is_not_zero"] is True
    assert "临时页.csv" in report["unexpected_sheets"]


def test_layer_statuses_and_ranges(tmp_path):
    report = _report(tmp_path)
    rollup = {row["layer"]: row for row in report["layer_rollup"]}
    assert rollup["A"]["status"] == "保证达标"
    assert (rollup["A"]["worst"], rollup["A"]["best"]) == (6, 7)
    assert rollup["B"]["status"] == "确定不足"
    assert (rollup["B"]["worst"], rollup["B"]["best"]) == (2, 4)
    assert rollup["C"]["status"] == "确定不足"


def test_repair_counted_once_and_forced(tmp_path):
    report = _report(tmp_path)
    repair = {item["item"]: item for item in report["repair_decisions"]}
    assert repair["r1"]["counted_once"] is True
    assert repair["r1"]["forced"] == ["早班", "A"]
    cell_a_zaoban = next(c for c in report["cells"] if c["shift"] == "早班" and c["layer"] == "A")
    assert cell_a_zaoban["worst"] == 3


def test_ambiguous_never_arbitrarily_assigned(tmp_path):
    report = _report(tmp_path)
    ambiguous = {item["item"]: item for item in report["ambiguous_items"]}
    assert set(ambiguous) == {"X", "Y"}
    assert ambiguous["Y"]["cross_shift_unresolved"] is True
    assert not ambiguous["X"]["cross_shift_unresolved"]
    cell_b_zaoban = next(c for c in report["cells"] if c["shift"] == "早班" and c["layer"] == "B")
    assert cell_b_zaoban["worst"] == 1
    assert set(cell_b_zaoban["ambiguous_items"]) == {"X", "Y"}


def test_todos_skip_guaranteed_layer(tmp_path):
    report = _report(tmp_path)
    assert len(report["recollection_todos"]) == 1
    todo = report["recollection_todos"][0]
    assert todo["missing_shift"] == "晚班"
    affected = {entry["layer"] for entry in todo["can_still_affect"]}
    assert affected == {"B", "C"}


def test_markdown_and_cli(tmp_path):
    report = _report(tmp_path)
    markdown = audit_csvz.to_markdown(report)
    assert "缺页≠零产量" in markdown
    assert "补采待办" in markdown
    csvz = _make_package(tmp_path)
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(json.dumps(_manifest(), ensure_ascii=False), encoding="utf-8")
    prefix = tmp_path / "out"
    cli.main(["audit", csvz, str(manifest_path), "-o", str(prefix)])
    assert (tmp_path / "out.md").exists()
    assert (tmp_path / "out.json").exists()
