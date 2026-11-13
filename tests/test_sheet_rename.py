# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import io
import struct
import unittest

import xlwt
import xlwt.CompoundDoc as cd


def save_to_bytes(workbook):
    stream = io.BytesIO()
    cd.XlsDoc().save(stream, workbook.get_biff_data())
    return stream.getvalue()


def iter_records(workbook_stream):
    pos = 0
    while pos + 4 <= len(workbook_stream):
        rec_id, length = struct.unpack_from('<HH', workbook_stream, pos)
        if rec_id == 0 and length == 0:
            break
        yield rec_id, workbook_stream[pos + 4:pos + 4 + length]
        pos += 4 + length


def biff_facts(xls_bytes):
    """Return (worksheet names, externsheet refs, formula aptgs)."""
    stream = xls_bytes[512:]
    names = []
    extern = []
    formulas = []
    for rec_id, data in iter_records(stream):
        if rec_id == 0x0085:  # BOUNDSHEET
            name_len, flags = data[6], data[7]
            raw = data[8:]
            if flags & 1:
                name = raw[:name_len * 2].decode('utf-16-le')
            else:
                name = raw[:name_len].decode('latin1')
            names.append(name)
        elif rec_id == 0x0017:  # EXTERNSHEET
            count = struct.unpack_from('<H', data, 0)[0]
            extern = [struct.unpack_from('<3H', data, 2 + 6 * i)
                      for i in range(count)]
        elif rec_id == 0x0006:  # FORMULA
            size = struct.unpack_from('<H', data, 20)[0]
            formulas.append(data[22:22 + size])
    return names, extern, formulas


def stored_formulas(sheet):
    out = []
    for row in sheet._Worksheet__rows.values():
        for cell in row._Row__cells.values():
            if hasattr(cell, 'frmla'):
                out.append(cell.frmla)
    return out


class TestRenameSheets(unittest.TestCase):
    def make_book(self):
        book = xlwt.Workbook()
        jan = book.add_sheet('Jan')
        feb = book.add_sheet('Feb')
        jan.write(0, 0, 1)
        feb.write(0, 0, 2)
        jan.write(1, 0, xlwt.Formula('Feb!A1'))
        feb.write(1, 0, xlwt.Formula('Jan!A1'))
        return book, jan, feb

    def test_swap_keeps_formula_binding(self):
        book, jan, feb = self.make_book()
        book.rename_sheets({'Jan': 'Feb', 'Feb': 'Jan'})
        self.assertEqual('Feb', jan.name)
        self.assertEqual('Jan', feb.name)
        self.assertEqual(0, book.sheet_index('Feb'))
        self.assertEqual(1, book.sheet_index('Jan'))

        names, extern, formulas = biff_facts(save_to_bytes(book))
        self.assertEqual(['Feb', 'Jan'], names)
        self.assertEqual([(0, 1, 1), (0, 0, 0)], extern[1:])
        self.assertEqual(1, struct.unpack_from('<H', formulas[0], 1)[0])
        self.assertEqual(2, struct.unpack_from('<H', formulas[1], 1)[0])

    def test_rotation_chain(self):
        book = xlwt.Workbook()
        for name in ('A', 'B', 'C'):
            book.add_sheet(name)
        book.rename_sheets({'A': 'B', 'B': 'C', 'C': 'A'})
        self.assertEqual(['B', 'C', 'A'], [s.name for s in book._Workbook__worksheets])

    def test_names_with_spaces_and_quotes(self):
        book, jan, feb = self.make_book()
        book.rename_sheets([('Jan', 'Dept A'), ('Feb', "Dept's B")])
        jan.write(1, 1, xlwt.Formula("'Dept''s B'!A1"))
        feb.write(1, 1, xlwt.Formula("'Dept A'!A1"))
        for formula in stored_formulas(jan) + stored_formulas(feb):
            self.assertIsNotNone(formula.get_references())
        save_to_bytes(book)

    def test_string_literal_is_not_touched(self):
        book, jan, _ = self.make_book()
        jan.write(1, 1, xlwt.Formula('"Feb!A1"&"Jan"'))
        book.rename_sheets({'Feb': 'March'})
        formula = jan._Worksheet__rows[1]._Row__cells[1].frmla
        self.assertEqual('"Feb!A1"&"Jan"', formula.text())
        save_to_bytes(book)

    def test_external_sheet_name_is_not_touched(self):
        book, jan, _ = self.make_book()
        # An external workbook may use the same sheet name text; xlwt's
        # internal references are unaffected by a rename.
        jan.write(1, 1, xlwt.Formula('Feb!A1'))
        book.rename_sheets({'Feb': 'Budget'})
        names, extern, formulas = biff_facts(save_to_bytes(book))
        self.assertEqual(['Jan', 'Budget'], names)
        self.assertEqual(1, struct.unpack_from('<H', formulas[0], 1)[0])

    def test_conflict_leaves_workbook_unchanged_and_saveable(self):
        book, jan, feb = self.make_book()
        with self.assertRaises(ValueError):
            book.rename_sheets({'Jan': 'feb'})
        self.assertEqual('Jan', jan.name)
        self.assertEqual('Feb', feb.name)
        self.assertEqual(0, book.sheet_index('Jan'))
        self.assertEqual(1, book.sheet_index('Feb'))
        names, _, _ = biff_facts(save_to_bytes(book))
        self.assertEqual(['Jan', 'Feb'], names)

    def test_conflict_can_be_non_strict(self):
        book, jan, feb = self.make_book()
        self.assertFalse(book.rename_sheets({'Jan': 'feb'}, strict=False))
        self.assertEqual(['Jan', 'Feb'], [jan.name, feb.name])

    def test_invalid_name_in_group_rolls_back_all(self):
        book, jan, feb = self.make_book()
        with self.assertRaises(ValueError):
            book.rename_sheets([('Jan', 'Good'), ('Feb', 'bad/name')])
        self.assertEqual(['Jan', 'Feb'], [jan.name, feb.name])
        save_to_bytes(book)

    def test_duplicate_source_rejected(self):
        book, _, _ = self.make_book()
        with self.assertRaises(ValueError):
            book.rename_sheets([('Jan', 'X'), ('jan', 'Y')])
        self.assertEqual('Jan', book.get_sheet('Jan').name)

    def test_unknown_source(self):
        book, _, _ = self.make_book()
        with self.assertRaises(Exception):
            book.rename_sheets({'Nope': 'X'})

    def test_worksheet_set_name_keeps_index_in_sync(self):
        book, jan, feb = self.make_book()
        jan.name = 'January'
        self.assertEqual(0, book.sheet_index('January'))
        self.assertEqual('January', book.get_sheet('January').name)
        with self.assertRaises(ValueError):
            feb.name = 'january'
        self.assertEqual('Feb', feb.name)
