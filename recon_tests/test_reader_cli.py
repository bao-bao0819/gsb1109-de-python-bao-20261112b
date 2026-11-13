"""CSV 读取与 CLI 端到端测试。"""

from __future__ import annotations

from io import StringIO

import pytest

from recon.cli import main
from recon.core import Kind
from recon.reader import CsvFormatError, stream_csv


def test_stream_csv_basic_and_fuzzy() -> None:
    text = (
        "type,lo,hi,ref\n"
        "COUNT,100,100,期初\n"
        "IN,10,20,模糊入库\n"
        "OUT,30,30,\n"
    )
    records = list(stream_csv(StringIO(text)))
    assert [r.kind for r in records] == [Kind.COUNT, Kind.IN, Kind.OUT]
    assert records[1].lo == 10 and records[1].hi == 20
    assert not records[1].exact
    assert records[2].exact
    assert records[0].ref == "期初"
    assert [r.seq for r in records] == [1, 2, 3]


def test_stream_csv_accepts_single_qty_and_chinese_headers() -> None:
    text = "类型,数量,备注\n入库,5,A\n出库,2,B\n实盘,3,C\n"
    records = list(stream_csv(StringIO(text)))
    assert records[0].kind is Kind.IN and records[0].lo == records[0].hi == 5
    assert records[2].kind is Kind.COUNT


def test_stream_csv_rejects_bad_int_and_missing_column(tmp_path) -> None:
    bad = tmp_path / "bad.csv"
    bad.write_text("type,lo,hi\nIN,x,3\n", encoding="utf-8")
    with pytest.raises(CsvFormatError, match="不是整数"):
        list(stream_csv(bad))
    no_qty = tmp_path / "noqty.csv"
    no_qty.write_text("type,ref\nIN,x\n", encoding="utf-8")
    with pytest.raises(CsvFormatError, match="数量列"):
        list(stream_csv(no_qty))


def test_stream_csv_from_path(tmp_path) -> None:
    path = tmp_path / "ledger.csv"
    path.write_text(
        "type,lo,hi\nCOUNT,10,10\nOUT,4,6\nCOUNT,5,7\n", encoding="utf-8"
    )
    records = list(stream_csv(path))
    assert len(records) == 3


def test_cli_end_to_end(tmp_path, capsys) -> None:
    path = tmp_path / "ledger.csv"
    path.write_text(
        "type,lo,hi,ref\n"
        "COUNT,100,100,期初实盘\n"
        "IN,10,20,模糊入库\n"
        "OUT,30,40,模糊出库\n"
        "COUNT,75,85,复盘\n",
        encoding="utf-8",
    )
    code = main([str(path)])
    out = capsys.readouterr().out
    assert code == 0
    assert "保守走法" in out
    assert "激进走法" in out
    assert "余额 [0" not in out  # 此例不会出现穿负提示


def test_cli_returns_nonzero_on_infeasible(tmp_path, capsys) -> None:
    path = tmp_path / "bad.csv"
    path.write_text(
        "type,lo,hi\nCOUNT,10,10\nOUT,60,60\nCOUNT,0,0\n", encoding="utf-8"
    )
    code = main([str(path)])
    out = capsys.readouterr().out
    assert code == 1
    assert "不可能对平" in out
    assert "最早不可能对平的业务记录：行2" in out


def test_cli_format_error_exit_code(tmp_path, capsys) -> None:
    path = tmp_path / "broken.csv"
    path.write_text("type,lo\nIN,1\n", encoding="utf-8")
    code = main([str(path)])
    err = capsys.readouterr().err
    assert code == 2
    assert "CSV 格式错误" in err
