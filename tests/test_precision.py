# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import io
import math
import unittest
from decimal import Decimal

import xlrd

import xlwt
from xlwt.Cell import NumberCell, StrCell
from xlwt.Precision import PrecisionError, PrecisionPolicy

from .utils import in_tst_output_dir


# A reading whose binary64 representation is exact ...
EXACT = Decimal('0.25')
# ... and one with far more significant digits than a double can preserve.
LONG_READING = Decimal('1.234567890123456789012345')
STRICT_TOLERANCE = 1e-18


def _book_and_sheet(policy=None, **policy_kwargs):
    book = xlwt.Workbook()
    sheet = book.add_sheet('precision')
    if policy is not None or policy_kwargs:
        sheet.set_precision_policy(0, policy, **policy_kwargs)
    return book, sheet


def _cell(sheet, rowx, colx=0):
    return sheet.row(rowx).cells[colx]


class TestPrecisionPolicy(unittest.TestCase):

    def test_sign_symmetry_of_error(self):
        policy = PrecisionPolicy(tolerance=STRICT_TOLERANCE)
        # Each pair is built from two full-precision string literals; deriving
        # the negative via unary minus would itself be rounded by the default
        # 28-digit decimal context before the policy ever sees it.
        pairs = (
            (EXACT, -EXACT),
            (LONG_READING, -LONG_READING),
            (Decimal('3.14159265358979323846'),
             Decimal('-3.14159265358979323846')),
            (Decimal('3.141592653589793238462643383279'),
             Decimal('-3.141592653589793238462643383279')),
        )
        for positive, negative in pairs:
            self.assertEqual(policy.rounding_error(positive),
                             policy.rounding_error(negative))
            self.assertEqual(policy.fits(positive), policy.fits(negative))

    def test_midpoint_positive_and_negative_share_verdict(self):
        # Build the exact midpoint between two adjacent binary64 numbers.
        lower = 1.0
        upper = math.nextafter(lower, math.inf)
        midpoint = (Decimal(repr(lower)) + Decimal(repr(upper))) / 2
        midpoint_error = midpoint - Decimal(repr(float(midpoint)))
        relative_error = abs(midpoint_error / midpoint)

        tight = PrecisionPolicy(tolerance=float(relative_error * Decimal('0.99')))
        loose = PrecisionPolicy(tolerance=float(relative_error * Decimal('1.01')))

        self.assertFalse(tight.fits(midpoint))
        self.assertFalse(tight.fits(-midpoint))
        self.assertTrue(loose.fits(midpoint))
        self.assertTrue(loose.fits(-midpoint))

    def test_tolerance_boundary(self):
        policy = PrecisionPolicy(tolerance=STRICT_TOLERANCE)
        self.assertTrue(policy.fits(EXACT))
        self.assertFalse(policy.fits(LONG_READING))
        self.assertFalse(policy.fits(-LONG_READING))

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            PrecisionPolicy(tolerance=-1)
        with self.assertRaises(ValueError):
            PrecisionPolicy(on_overflow='explode')
        with self.assertRaises(ValueError):
            PrecisionPolicy(text_formatter='not callable')


class TestWritePrecision(unittest.TestCase):

    def test_legacy_behaviour_without_policy(self):
        book, sheet = _book_and_sheet()
        sheet.write(0, 0, LONG_READING)
        cell = _cell(sheet, 0)
        self.assertIsInstance(cell, NumberCell)
        self.assertEqual(sheet.non_numeric_cells, [])

    def test_in_tolerance_value_stays_numeric(self):
        book, sheet = _book_and_sheet(
            PrecisionPolicy(tolerance=STRICT_TOLERANCE))
        sheet.write(0, 0, EXACT)
        self.assertIsInstance(_cell(sheet, 0), NumberCell)
        self.assertEqual(sheet.non_numeric_cells, [])

    def test_reject_overflow(self):
        book, sheet = _book_and_sheet(
            PrecisionPolicy(tolerance=STRICT_TOLERANCE,
                            on_overflow=xlwt.REJECT))
        with self.assertRaises(PrecisionError):
            sheet.write(0, 0, LONG_READING)
        with self.assertRaises(PrecisionError):
            sheet.write(1, 0, -LONG_READING)
        self.assertEqual(sheet.non_numeric_cells, [])

    def test_text_overflow_is_reversible_and_marked(self):
        book, sheet = _book_and_sheet(
            PrecisionPolicy(tolerance=STRICT_TOLERANCE,
                            on_overflow=xlwt.TEXT))
        sheet.write(0, 0, LONG_READING)
        sheet.write(1, 0, -LONG_READING)
        sheet.write(2, 0, EXACT)

        text_cell = _cell(sheet, 0)
        self.assertIsInstance(text_cell, StrCell)
        self.assertEqual(sheet.non_numeric_cells, [(0, 0), (1, 0)])
        self.assertIsInstance(_cell(sheet, 2), NumberCell)

        # The written text must parse back to the original reading.
        stored_text = book._Workbook__sst
        # Round-trip through a real file instead of poking private storage.
        stream = io.BytesIO()
        book.save(stream)
        reopened = xlrd.open_workbook(file_contents=stream.getvalue())
        table = reopened.sheet_by_index(0)
        self.assertEqual(table.cell_type(0, 0), xlrd.XL_CELL_TEXT)
        self.assertEqual(Decimal(table.cell_value(0, 0)), LONG_READING)
        self.assertEqual(Decimal(table.cell_value(1, 0)), -LONG_READING)
        self.assertEqual(table.cell_type(2, 0), xlrd.XL_CELL_NUMBER)

    def test_force_numeric_keeps_number_type(self):
        policy = PrecisionPolicy(tolerance=STRICT_TOLERANCE,
                                 on_overflow=xlwt.FORCE_NUMERIC)
        book, sheet = _book_and_sheet(policy)
        sheet.write(0, 0, LONG_READING)
        self.assertIsInstance(_cell(sheet, 0), NumberCell)
        self.assertEqual(sheet.non_numeric_cells, [])

    def test_policy_kwargs_and_removal(self):
        book, sheet = _book_and_sheet()
        sheet.set_precision_policy(0, tolerance=STRICT_TOLERANCE,
                                   on_overflow=xlwt.REJECT)
        self.assertIsInstance(sheet.get_precision_policy(0), PrecisionPolicy)
        with self.assertRaises(PrecisionError):
            sheet.write(0, 0, LONG_READING)

        sheet.set_precision_policy(0, None)
        self.assertIsNone(sheet.get_precision_policy(0))
        sheet.write(0, 0, LONG_READING)
        self.assertIsInstance(_cell(sheet, 0), NumberCell)

    def test_overwrite_clears_non_numeric_marker(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('precision', cell_overwrite_ok=True)
        sheet.set_precision_policy(
            0, tolerance=STRICT_TOLERANCE, on_overflow=xlwt.TEXT)
        sheet.write(0, 0, LONG_READING)
        self.assertEqual(sheet.non_numeric_cells, [(0, 0)])
        sheet.write(0, 0, EXACT)
        self.assertEqual(sheet.non_numeric_cells, [])
        self.assertIsInstance(_cell(sheet, 0), NumberCell)

    def test_integer_reading_keeps_every_digit(self):
        big = 10 ** 20 + 123456789
        policy = PrecisionPolicy(tolerance=STRICT_TOLERANCE,
                                 on_overflow=xlwt.TEXT)
        book, sheet = _book_and_sheet(policy)
        sheet.write(0, 0, big)
        stream = io.BytesIO()
        book.save(stream)
        table = xlrd.open_workbook(file_contents=stream.getvalue()).sheet_by_index(0)
        self.assertEqual(table.cell_type(0, 0), xlrd.XL_CELL_TEXT)
        self.assertEqual(int(table.cell_value(0, 0)), big)


class TestFinanceSums(unittest.TestCase):

    def test_sum_uses_only_numeric_cells(self):
        book, sheet = _book_and_sheet(
            PrecisionPolicy(tolerance=STRICT_TOLERANCE,
                            on_overflow=xlwt.TEXT))
        # Two ordinary readings that remain numbers ...
        sheet.write(0, 0, Decimal('0.25'))
        sheet.write(1, 0, 1)
        # ... and two long readings that are downgraded to text.
        sheet.write(2, 0, LONG_READING)
        sheet.write(3, 0, -LONG_READING)

        numeric = list(sheet.iter_numeric_cells(0))
        self.assertEqual([(r, c) for r, c, _ in numeric], [(0, 0), (1, 0)])
        self.assertEqual(sheet.sum_numeric(0), 1.25)

        # The same numeric cells are the only ones the exported sheet can sum.
        stream = io.BytesIO()
        book.save(stream)
        table = xlrd.open_workbook(file_contents=stream.getvalue()).sheet_by_index(0)
        direct_sum = sum(
            table.cell_value(r, 0)
            for r in range(table.nrows)
            if table.cell_type(r, 0) == xlrd.XL_CELL_NUMBER)
        self.assertEqual(direct_sum, 1.25)

    def test_saved_file_marks_text_cells(self):
        book, sheet = _book_and_sheet(
            PrecisionPolicy(tolerance=STRICT_TOLERANCE,
                            on_overflow=xlwt.TEXT))
        for rowx, value in enumerate((EXACT, LONG_READING, Decimal('2'))):
            sheet.write(rowx, 0, value)
        path = in_tst_output_dir('precision.xls')
        book.save(path)
        table = xlrd.open_workbook(path).sheet_by_index(0)
        self.assertEqual([table.cell_type(r, 0) for r in range(3)], [
            xlrd.XL_CELL_NUMBER, xlrd.XL_CELL_TEXT, xlrd.XL_CELL_NUMBER])


if __name__ == '__main__':
    unittest.main()
