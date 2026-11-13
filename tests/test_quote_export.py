# -*- coding: utf-8 -*-

import unittest

import xlwt

from quote_export import (
    CACHED_MISMATCH, COMPUTED, EXPRESSION_REJECTED, INPUT, ROW_ERROR, TEXT,
    UNEXPECTED_FIELD, Column, ColumnContract, ContractError, ContractExporter,
    evaluate_expression, render_expression,
)


class FakeSheet(object):
    def __init__(self):
        self.cells = {}

    def write(self, row, col, value, style=None):
        self.cells[(row, col)] = value


def make_contract():
    return ColumnContract([
        Column("remark", kind=TEXT),
        Column("unit_price", kind=INPUT),
        Column("quantity", kind=INPUT),
        Column("total", kind=COMPUTED,
               expression="{unit_price}*{quantity}",
               allowed_refs=("unit_price", "quantity")),
    ])


def make_exporter():
    return ContractExporter(make_contract())


class RemarkTextTests(unittest.TestCase):
    def test_formula_like_remark_is_stored_verbatim(self):
        sheet = FakeSheet()
        make_exporter().export(
            [{"remark": "=1+1", "unit_price": 2, "quantity": 3}], sheet)
        self.assertEqual(sheet.cells[(1, 0)], "=1+1")
        self.assertIsInstance(sheet.cells[(1, 0)], str)

    def test_input_that_looks_like_formula_is_stored_verbatim(self):
        sheet = FakeSheet()
        make_exporter().export(
            [{"remark": "x", "unit_price": "=HYPERLINK(1)", "quantity": 3}],
            sheet)
        self.assertEqual(sheet.cells[(1, 1)], "=HYPERLINK(1)")

    def test_none_remark_exports_as_empty_text(self):
        sheet = FakeSheet()
        make_exporter().export(
            [{"unit_price": 2, "quantity": 3}], sheet)
        self.assertEqual(sheet.cells[(1, 0)], "")


class ComputedColumnTests(unittest.TestCase):
    def test_agreed_expression_is_written_as_formula(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": 2, "quantity": 3}], sheet)
        self.assertTrue(result.ok)
        formula = sheet.cells[(1, 3)]
        self.assertIsInstance(formula, xlwt.ExcelFormula.Formula)
        self.assertEqual(formula.text(), "B2*C2")

    def test_matching_upstream_expression_is_accepted(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": 2, "quantity": 3,
              "total": "= {unit_price} * {quantity}"}], sheet)
        self.assertTrue(result.ok)
        self.assertEqual(sheet.cells[(1, 3)].text(), "B2*C2")

    def test_foreign_expression_is_rejected_but_contract_formula_kept(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": 2, "quantity": 3,
              "total": "=SUM(A1:A9)"}], sheet)
        violations = result.violations_for(field="total",
                                           code=EXPRESSION_REJECTED)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].cell, (1, 3))
        self.assertEqual(violations[0].address, "D2")
        self.assertEqual(sheet.cells[(1, 3)].text(), "B2*C2")

    def test_expression_may_only_reference_allowed_inputs(self):
        with self.assertRaises(ContractError):
            ColumnContract([
                Column("unit_price"),
                Column("quantity"),
                Column("total", kind=COMPUTED,
                       expression="{unit_price}*{quantity}",
                       allowed_refs=("unit_price",)),
            ])

    def test_expression_must_reference_known_fields(self):
        with self.assertRaises(ContractError):
            ColumnContract([
                Column("unit_price"),
                Column("total", kind=COMPUTED,
                       expression="{unit_price}*{missing}"),
            ])

    def test_computed_column_requires_expression(self):
        with self.assertRaises(ContractError):
            Column("total", kind=COMPUTED)


class CachedResultTests(unittest.TestCase):
    def test_matching_cached_result_is_silent(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": 2, "quantity": 3}],
            sheet, cached={0: {"total": 6}})
        self.assertTrue(result.ok)

    def test_mismatched_cached_result_is_flagged_not_written(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": 2, "quantity": 3}],
            sheet, cached={0: {"total": 7}})
        violations = result.violations_for(code=CACHED_MISMATCH)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].cell, (1, 3))
        formula = sheet.cells[(1, 3)]
        self.assertIsInstance(formula, xlwt.ExcelFormula.Formula)
        self.assertEqual(formula.text(), "B2*C2")

    def test_numeric_value_in_record_is_treated_as_cached(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": 2, "quantity": 3, "total": 7}],
            sheet)
        self.assertEqual(
            len(result.violations_for(code=CACHED_MISMATCH)), 1)
        self.assertIsInstance(sheet.cells[(1, 3)],
                              xlwt.ExcelFormula.Formula)

    def test_unevaluable_rows_skip_the_cross_check(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": "n/a", "quantity": 3}],
            sheet, cached={0: {"total": 7}})
        self.assertTrue(result.ok)


class InsertColumnTests(unittest.TestCase):
    def test_references_follow_business_fields_after_insert(self):
        contract = make_contract()
        contract.insert_column(Column("discount"), before="total")
        exporter = ContractExporter(contract)
        sheet = FakeSheet()
        result = exporter.export(
            [{"remark": "r", "unit_price": 2, "quantity": 3,
              "discount": 0}], sheet)
        self.assertTrue(result.ok)
        # total moved to column E; its inputs are still B and C.
        self.assertEqual(sheet.cells[(1, 4)].text(), "B2*C2")

    def test_insert_before_an_input_rebinds_the_reference(self):
        contract = make_contract()
        contract.insert_column(Column("seq"), before="unit_price")
        sheet = FakeSheet()
        ContractExporter(contract).export(
            [{"remark": "r", "seq": 1, "unit_price": 2, "quantity": 3}],
            sheet)
        self.assertEqual(sheet.cells[(1, 4)].text(), "C2*D2")

    def test_inserted_computed_column_is_validated_by_field(self):
        contract = make_contract()
        with self.assertRaises(ContractError):
            contract.insert_column(
                Column("bogus", kind=COMPUTED, expression="{nope}*2"))
        with self.assertRaises(ContractError):
            contract.insert_column(Column("remark"))

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(ContractError):
            ColumnContract([Column("a"), Column("a")])


class RowIsolationTests(unittest.TestCase):
    def test_bad_row_does_not_block_independent_rows(self):
        sheet = FakeSheet()
        records = [
            {"remark": "ok-1", "unit_price": 2, "quantity": 3},
            ["not", "a", "mapping"],
            {"remark": "ok-2", "unit_price": 4, "quantity": 5},
        ]
        result = make_exporter().export(records, sheet)
        self.assertEqual(result.rows_exported, 2)
        self.assertEqual(len(result.violations_for(code=ROW_ERROR)), 1)
        self.assertEqual(sheet.cells[(1, 3)].text(), "B2*C2")
        self.assertEqual(sheet.cells[(3, 3)].text(), "B4*C4")

    def test_unexpected_fields_are_reported_per_row(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": 2, "quantity": 3, "smuggled": 1}],
            sheet)
        violations = result.violations_for(code=UNEXPECTED_FIELD)
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].field, "smuggled")
        self.assertEqual(result.rows_exported, 1)

    def test_flagged_cells_are_listed(self):
        sheet = FakeSheet()
        result = make_exporter().export(
            [{"remark": "r", "unit_price": 2, "quantity": 3, "total": 9}],
            sheet)
        self.assertEqual(result.cells(), [(1, 3)])


class HelpersTests(unittest.TestCase):
    def test_evaluate_expression(self):
        self.assertEqual(
            evaluate_expression("{a}*{b}+1", {"a": "2", "b": 3}), 7.0)
        self.assertIsNone(evaluate_expression("{a}*{b}", {"a": "x"}))
        self.assertIsNone(evaluate_expression("{a}/0", {"a": 1}))
        self.assertIsNone(
            evaluate_expression("__import__('os')", {"a": 1}))

    def test_render_expression_uses_sheet_row(self):
        contract = make_contract()
        self.assertEqual(
            render_expression(contract, "{unit_price}*{quantity}", 4),
            "B5*C5")


class WorkbookIntegrationTests(unittest.TestCase):
    def test_export_to_real_workbook(self):
        import io
        wb = xlwt.Workbook()
        ws = wb.add_sheet("quotes")
        contract = make_contract()
        contract.insert_column(Column("discount"), before="total")
        result = ContractExporter(contract).export(
            [{"remark": "=cmd|'/c calc'!A0", "unit_price": 2.5,
              "quantity": 4, "discount": 0, "total": 10.0}],
            ws)
        self.assertTrue(result.ok)
        buffer = io.BytesIO()
        wb.save(buffer)
        self.assertTrue(buffer.getvalue())


if __name__ == "__main__":
    unittest.main()
