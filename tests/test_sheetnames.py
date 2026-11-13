# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import os
import shutil
import tempfile
import unittest

import xlwt
from xlwt import SheetNameAllocator, sanitize_sheet_name
from xlwt.Utils import valid_sheet_name

from .utils import in_tst_output_dir


class TestSanitizeSheetName(unittest.TestCase):

    def test_invalid_chars_replaced(self):
        name = sanitize_sheet_name('A/B\\C:D*E?F[G]H')
        self.assertEqual(name, 'A_B_C_D_E_F_G_H')
        self.assertTrue(valid_sheet_name(name))

    def test_control_chars_replaced(self):
        name = sanitize_sheet_name('a\x00b\x1fc\n')
        self.assertEqual(name, 'a_b_c_')
        self.assertTrue(valid_sheet_name(name))

    def test_long_prefix_truncated_to_31(self):
        name = sanitize_sheet_name('华东区供货商' * 10)
        self.assertEqual(len(name), 31)
        self.assertTrue(valid_sheet_name(name))

    def test_emoji_counted_as_two_utf16_units(self):
        name = sanitize_sheet_name('😀' * 40)
        self.assertEqual(len(name), 15)  # 15 emoji = 30 UTF-16 units
        self.assertTrue(valid_sheet_name(name))

    def test_surrogate_pair_never_split(self):
        name = sanitize_sheet_name('x' * 30 + '😀')
        self.assertEqual(name, 'x' * 30)  # 30 + 2 units would exceed 31

    def test_leading_apostrophe_dropped(self):
        self.assertEqual(sanitize_sheet_name("'quoted"), 'quoted')

    def test_nothing_usable_returns_empty(self):
        self.assertEqual(sanitize_sheet_name(''), '')
        self.assertEqual(sanitize_sheet_name(None), '')
        self.assertEqual(sanitize_sheet_name("'"), '')


class TestSheetNameAllocator(unittest.TestCase):

    def test_basic_assignment_produces_valid_unique_names(self):
        allocator = SheetNameAllocator()
        suppliers = [
            ('S1', 'Acme Corp'),
            ('S2', '华东/华南:供货商*特别特别特别特别特别特别特别长的名字'),
            ('S3', '😀😀😀 emoji 供货商 名字名字名字名字名字名字名字名字名字'),
        ]
        names = allocator.assign(suppliers)
        self.assertEqual(set(names), {'S1', 'S2', 'S3'})
        self.assertEqual(len(set(names.values())), 3)
        for name in names.values():
            self.assertTrue(valid_sheet_name(name), name)

    def test_assignment_independent_of_input_order(self):
        suppliers = [
            ('S3', '同名供货商'),
            ('S1', '同名供货商'),
            ('S2', '同名供货商'),
            ('S10', '另一个/名字'),
        ]
        first = SheetNameAllocator().assign(suppliers)
        second = SheetNameAllocator().assign(list(reversed(suppliers)))
        self.assertEqual(first, second)
        # Lowest ID wins the base name, later IDs get deterministic suffixes.
        self.assertEqual(first['S1'], '同名供货商')
        self.assertEqual(first['S2'], '同名供货商 (2)')
        self.assertEqual(first['S3'], '同名供货商 (3)')

    def test_case_insensitive_conflict(self):
        allocator = SheetNameAllocator()
        names = allocator.assign([('S1', 'Acme'), ('S2', 'ACME')])
        self.assertEqual(names['S1'], 'Acme')
        self.assertNotEqual(names['S2'].lower(), 'acme')
        self.assertEqual(names['S2'], 'ACME (2)')

    def test_suffix_keeps_name_within_limit(self):
        long_name = '长' * 40
        allocator = SheetNameAllocator()
        names = allocator.assign([('S1', long_name), ('S2', long_name)])
        for name in names.values():
            self.assertTrue(valid_sheet_name(name), name)
        self.assertEqual(names['S2'], '长' * 27 + ' (2)')

    def test_unusable_display_name_falls_back(self):
        allocator = SheetNameAllocator()
        names = allocator.assign([('S1', "'"), ('S2', '')])
        self.assertEqual(names['S1'], 'Supplier')
        self.assertEqual(names['S2'], 'Supplier (2)')

    def test_reassigning_same_id_keeps_name(self):
        allocator = SheetNameAllocator()
        first = allocator.assign([('S1', 'Acme')])
        second = allocator.assign([('S1', 'Acme Renamed')])
        self.assertEqual(first, second)

    def test_duplicate_id_in_input_rejected(self):
        allocator = SheetNameAllocator()
        self.assertRaises(
            ValueError, allocator.assign, [('S1', 'a'), ('S1', 'b')])

    def test_registry_rejects_conflicting_names(self):
        self.assertRaises(
            ValueError, SheetNameAllocator,
            {'S1': 'Acme', 'S2': 'ACME'})
        self.assertRaises(
            ValueError, SheetNameAllocator, {'S1': 'bad/name'})


class TestPersistence(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.path = os.path.join(self.tmpdir, 'registry.json')

    def tearDown(self):
        shutil.rmtree(self.tmpdir)

    def test_save_load_roundtrip(self):
        allocator = SheetNameAllocator()
        allocator.assign([('S1', 'Acme'), (7, '七/公司'), ('S2', 'ACME')])
        allocator.reserve_name('名称对照表')
        allocator.save(self.path)
        loaded = SheetNameAllocator.load(self.path)
        self.assertEqual(loaded.assignments, allocator.assignments)
        self.assertEqual(loaded.reserve_name('名称对照表'), '名称对照表')

    def test_load_or_create(self):
        allocator = SheetNameAllocator.load_or_create(self.path)
        self.assertEqual(allocator.assignments, {})
        allocator.assign([('S1', 'Acme')])
        allocator.save(self.path)
        self.assertEqual(
            SheetNameAllocator.load_or_create(self.path).assignments,
            {'S1': 'Acme'})

    def test_monthly_cycle_keeps_names_stable(self):
        # Month 1: supplier A gets a name.
        month1 = SheetNameAllocator.load_or_create(self.path)
        month1.assign([('A', 'Acme Corp'), ('B', 'Beta')])
        month1.save(self.path)

        # Month 2: A is deactivated; new supplier C uses the same display
        # name (different case). It must not take A's name.
        month2 = SheetNameAllocator.load_or_create(self.path)
        names2 = month2.assign([('B', 'Beta'), ('C', 'ACME CORP')])
        self.assertEqual(names2['B'], 'Beta')
        self.assertEqual(names2['C'], 'ACME CORP (2)')
        month2.save(self.path)

        # Month 3: A is reactivated and must get its original name back;
        # C keeps its own name from last month.
        month3 = SheetNameAllocator.load_or_create(self.path)
        names3 = month3.assign([('A', 'Acme Corp'), ('B', 'Beta'),
                                ('C', 'ACME CORP')])
        self.assertEqual(names3['A'], 'Acme Corp')
        self.assertEqual(names3['C'], 'ACME CORP (2)')

        # Deactivated suppliers stay in the registry.
        self.assertIn('A', SheetNameAllocator.load(self.path).assignments)


class TestMappingSheet(unittest.TestCase):

    def test_mapping_rows_sorted_and_complete(self):
        allocator = SheetNameAllocator()
        allocator.assign([('S2', '乙'), ('S1', '甲'), ('S3', '丙')])
        rows = allocator.mapping_rows([('S2', '乙'), ('S1', '甲')])
        self.assertEqual(
            rows, [('S1', '甲', '甲'), ('S2', '乙', '乙')])
        # Without arguments every known supplier is listed.
        self.assertEqual(len(allocator.mapping_rows()), 3)
        self.assertRaises(ValueError, allocator.mapping_rows, [('S9', '未知')])

    def test_write_mapping_sheet(self):
        allocator = SheetNameAllocator()
        suppliers = [('S1', 'Acme/Corp'), ('S2', '名称对照表')]
        names = allocator.assign(suppliers)
        workbook = xlwt.Workbook()
        for supplier_id, _display in suppliers:
            workbook.add_sheet(names[supplier_id])
        worksheet = allocator.write_mapping_sheet(workbook, suppliers)
        # The supplier was assigned first and keeps the base name; the
        # mapping sheet takes a suffixed, still valid name.
        self.assertEqual(names['S2'], '名称对照表')
        self.assertEqual(worksheet.get_name(), '名称对照表 (2)')
        workbook.save(in_tst_output_dir('supplier_sheets.xls'))

    def test_mapping_sheet_name_stable_across_exports(self):
        allocator = SheetNameAllocator()
        allocator.assign([('S1', '名称对照表')])
        # A supplier already holds the base name, so the mapping sheet
        # takes a suffixed name -- the same one at every export.
        self.assertEqual(
            allocator.write_mapping_sheet(xlwt.Workbook()).get_name(),
            '名称对照表 (2)')
        self.assertEqual(
            allocator.write_mapping_sheet(xlwt.Workbook()).get_name(),
            '名称对照表 (2)')
        # Reserving blocks later suppliers from taking the name.
        self.assertEqual(
            allocator.assign([('S2', '名称对照表')])['S2'], '名称对照表 (3)')


if __name__ == '__main__':
    unittest.main()
