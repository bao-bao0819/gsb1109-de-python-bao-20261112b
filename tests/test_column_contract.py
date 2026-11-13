import io
import os
from decimal import Decimal

import pytest

from pyexcel_io import get_data
from pyexcel_io.column_contract import (
    ColumnContract,
    ColumnRule,
    HeaderSelector,
    PositionSelector,
    convert_value,
)
from pyexcel_io.exceptions import (
    ColumnContractConfigError,
    CSVColumnContractError,
)


HEADER = "料号,数量,金额\n"
GOOD_ROWS = '0007,0007,"1,234.50"\nA-9,8,"2,000.00"\n'


def read_all_sources(tmp_path, content, **keywords):
    path = tmp_path / "data.csv"
    path.write_text(content, encoding="utf-8")
    file_result = get_data(str(path), **keywords)
    content_result = get_data(content, file_type="csv", **keywords)
    stream_result = get_data(
        io.StringIO(content), file_type="csv", **keywords
    )
    return (
        list(file_result[path.name]),
        list(content_result["csv"]),
        list(stream_result["csv"]),
    )


def test_text_rule_preserves_leading_zero_but_int_rule_converts():
    contract = ColumnContract({"料号": "text", "数量": "int"})
    row, violations = contract.bind(
        [["料号", "数量"]]
    ).convert_row(["0007", "0007"])
    assert row == ["0007", 7]
    assert violations == []


def test_position_rule_selects_original_column():
    contract = ColumnContract({0: "text", 2: "int"})
    row, violations = contract.bind([]).convert_row(
        ["0007", "ignored", "0007"]
    )
    assert row == ["0007", "ignored", 7]
    assert violations == []


def test_two_level_header_selects_column_by_header_path():
    contract = ColumnContract(
        {("订单", "料号"): "text", ("订单", "数量"): "int"}
    )
    row, violations = contract.bind(
        [["订单", "订单"], ["料号", "数量"]]
    ).convert_row(["0007", "0007"], data_row=0)
    assert row == ["0007", 7]
    assert violations == []


def test_header_contract_consumes_header_rows(tmp_path):
    content = HEADER + GOOD_ROWS
    contract = ColumnContract(
        {"料号": "text", "数量": "int", "金额": "amount"}
    )
    results = read_all_sources(tmp_path, content, column_contract=contract)
    expected = [["0007", 7, 1234.5], ["A-9", 8, 2000.0]]
    assert results[0] == results[1] == results[2] == expected


def test_empty_and_missing_columns_are_distinct():
    contract = ColumnContract({"料号": "text", "数量": "int"})
    bound = contract.bind([["料号", "数量"]])
    _, empty_violations = bound.convert_row(["A1", ""])
    _, missing_column_violations = ColumnContract(
        {"料号": "text", "数量": "int", "金额": "amount"}
    ).bind([["料号", "数量"]]).convert_row(["A1", "1"])

    assert [violation.reason for violation in empty_violations] == ["empty"]
    assert [
        violation.reason for violation in missing_column_violations
    ] == ["missing_column"]


def test_invalid_values_keep_original_spelling_and_location(tmp_path):
    content = "料号,数量,金额\n0007,x,y\n"
    contract = ColumnContract(
        {"料号": "text", "数量": "int", "金额": "amount"}
    )
    results = read_all_sources(tmp_path, content, column_contract=contract)
    assert results[0] == results[1] == results[2] == [["0007", "x", "y"]]
    violations = contract.violations
    assert [(v.row, v.column, v.raw_value, v.reason) for v in violations] == [
        (0, 1, "x", "invalid"),
        (0, 2, "y", "invalid"),
    ]


def test_file_content_and_stream_report_the_same_error_location(tmp_path):
    content = "h1,h2\n0007,x\n"
    contract = ColumnContract({"h1": "text", "h2": "int"})
    results = read_all_sources(tmp_path, content, column_contract=contract)
    assert results[0] == results[1] == results[2]
    violations = contract.violations
    assert len(violations) == 1
    violation = violations[0]
    assert (violation.row, violation.column, violation.raw_value) == (
        0,
        1,
        "x",
    )
    assert violation.line_number == 2


def test_strict_mode_raises_with_location_and_raw_value(tmp_path):
    content = "料号,数量\nA1,x\n"
    contract = ColumnContract({"料号": "text", "数量": "int"}, strict=True)
    with pytest.raises(CSVColumnContractError) as raised:
        read_all_sources(tmp_path, content, column_contract=contract)
    error = raised.value
    assert (error.row, error.column, error.raw_value) == (0, 1, "x")
    assert error.line_number == 2
    assert error.violations[0] is error.violation


def test_thousands_separator_is_accepted_for_amounts():
    contract = ColumnContract({"金额": "amount"})
    row, violations = contract.bind([["金额"]]).convert_row(["1,234.50"])
    assert row == [1234.5]
    assert violations == []


def test_locale_decimal_and_thousands_are_configurable():
    contract = ColumnContract(
        {"金额": ColumnRule("amount", locale="de")}
    )
    row, violations = contract.bind([["金额"]]).convert_row(["1.234,50"])
    assert row == [1234.5]
    assert violations == []


def test_decimal_rule_returns_decimal():
    contract = ColumnContract({"金额": "decimal"})
    row, _ = contract.bind([["金额"]]).convert_row(["1,234.50"])
    assert row == [Decimal("1234.50")]


def test_reader_header_overrides_contract():
    content = "skip,skip\n料号,数量\n0007,0007\n"
    contract = ColumnContract({0: "text", 1: "int"})
    result = get_data(
        content,
        file_type="csv",
        column_contract=contract,
        header_rows=2,
    )
    assert result["csv"] == [["0007", 7]]


def test_missing_configured_column_is_a_violation():
    contract = ColumnContract({"料号": "text", "金额": "amount"})
    row, violations = contract.bind([["料号"]]).convert_row(["0007"])
    assert row == ["0007"]
    assert [violation.reason for violation in violations] == [
        "missing_column"
    ]


def test_ambiguous_header_selector_is_rejected():
    contract = ColumnContract({("订单", "数量"): "int"})
    with pytest.raises(ColumnContractConfigError):
        contract.bind([["订单", "订单"], ["数量", "数量"]])


def test_rule_selector_aliases():
    assert HeaderSelector("数量") == ColumnRule(
        "int", header="数量"
    ).selector
    assert PositionSelector(2) == ColumnRule(
        "int", position=2
    ).selector


def test_convert_value_does_not_convert_identifier_text():
    value, error = convert_value("0007", ColumnRule("text", position=0))
    assert value == "0007"
    assert error is None
