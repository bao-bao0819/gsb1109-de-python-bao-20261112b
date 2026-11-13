# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import unittest

import xlwt
from xlwt import Style
from xlwt.Formatting import Font

try:
    import xlrd
except ImportError:  # pragma: no cover
    xlrd = None


def save_to_book(workbook):
    import io
    stream = io.BytesIO()
    workbook.save(stream)
    return xlrd.open_workbook(
        file_contents=stream.getvalue(), formatting_info=True)


def make_money_style():
    # Fresh objects every call, as if produced independently by a department.
    return xlwt.easyxf(num_format_str='$#,##0.00')


def make_percent_style():
    return xlwt.easyxf(num_format_str='0%')


@unittest.skipIf(xlrd is None, "xlrd is required to read the generated .xls")
class TestStyleReuse(unittest.TestCase):

    def test_equivalent_styles_are_reused_across_rows(self):
        wb = xlwt.Workbook(style_compression=2)
        ws = wb.add_sheet('sales')
        for rowx in range(200):
            ws.write(rowx, 0, rowx, make_money_style())
        book = save_to_book(wb)
        sheet = book.sheet_by_index(0)
        xf_indexes = {sheet.cell_xf_index(rowx, 0) for rowx in range(200)}
        self.assertEqual(len(xf_indexes), 1)
        # Every reuse after the first registration is counted in stats[4].
        self.assertEqual(wb.get_style_stats()[4], 199)

    def test_reuse_avoids_xf_limit_that_compression_off_hits(self):
        # Without compression the 4094 XF ceiling stops the export.
        wb_plain = xlwt.Workbook(style_compression=0)
        ws_plain = wb_plain.add_sheet('s')
        with self.assertRaises(ValueError):
            for rowx in range(4100):
                ws_plain.write(rowx, 0, rowx, make_money_style())
            save_to_book(wb_plain)

        # Equivalent styles collapse to one XF with compression enabled.
        wb = xlwt.Workbook(style_compression=2)
        ws = wb.add_sheet('s')
        for rowx in range(4100):
            ws.write(rowx, 0, rowx, make_money_style())
        book = save_to_book(wb)
        sheet = book.sheet_by_index(0)
        xf_indexes = {sheet.cell_xf_index(rowx, 0) for rowx in range(4100)}
        self.assertEqual(len(xf_indexes), 1)


class TestNumberFormatSeparation(unittest.TestCase):

    def test_money_and_percent_not_merged_when_all_values_are_zero(self):
        collection = Style.StyleCollection(style_compression=2)
        money_idx = collection.add(make_money_style())
        percent_idx = collection.add(make_percent_style())
        self.assertNotEqual(money_idx, percent_idx)

        diffs = collection.unmerged_style_diffs()
        format_indexes = {item['num_format_idx'] for item in diffs}
        self.assertIn(collection._num_formats['$#,##0.00'], format_indexes)
        self.assertIn(collection._num_formats['0%'], format_indexes)
        format_strings = {item['num_format_str'] for item in diffs}
        self.assertIn('$#,##0.00', format_strings)
        self.assertIn('0%', format_strings)

    @unittest.skipIf(xlrd is None, "xlrd is required to read the generated .xls")
    def test_zero_cells_keep_their_own_number_format(self):
        wb = xlwt.Workbook(style_compression=2)
        ws = wb.add_sheet('s')
        ws.write(0, 0, 0, make_money_style())
        ws.write(1, 0, 0, make_percent_style())
        ws.write(2, 0, 0, make_money_style())
        book = save_to_book(wb)
        sheet = book.sheet_by_index(0)

        money_xf = sheet.cell_xf_index(0, 0)
        percent_xf = sheet.cell_xf_index(1, 0)
        self.assertNotEqual(money_xf, percent_xf)
        self.assertEqual(sheet.cell_xf_index(2, 0), money_xf)

        def format_str(xf_index):
            format_key = book.xf_list[xf_index].format_key
            return book.format_map[format_key].format_str

        self.assertEqual(format_str(money_xf), '$#,##0.00')
        self.assertEqual(format_str(percent_xf), '0%')
        # The stored values are still plain numbers; only rendering differs.
        self.assertEqual(sheet.cell_value(0, 0), 0)
        self.assertEqual(sheet.cell_value(1, 0), 0)


class TestMutationAfterUse(unittest.TestCase):

    def test_mutated_font_gets_new_index_without_touching_old_rows(self):
        collection = Style.StyleCollection(style_compression=2)
        style = xlwt.XFStyle()
        style.font.colour_index = xlwt.Style.colour_map['red']
        first_idx = collection.add(style)
        first_frozen = collection._xf_x2id[first_idx]
        first_font_idx = first_frozen[0]

        style.font.colour_index = xlwt.Style.colour_map['blue']
        second_idx = collection.add(style)
        self.assertNotEqual(first_idx, second_idx)

        # The definition held by the earlier index keeps the original colour.
        first_font = collection._font_x2frozen[first_font_idx]
        self.assertEqual(first_font.colour_index,
                         xlwt.Style.colour_map['red'])
        second_font_idx = collection._xf_x2id[second_idx][0]
        second_font = collection._font_x2frozen[second_font_idx]
        self.assertEqual(second_font.colour_index,
                         xlwt.Style.colour_map['blue'])

    def test_mutated_pattern_component_gets_new_xf_index(self):
        collection = Style.StyleCollection(style_compression=2)
        style = xlwt.easyxf('pattern: pattern solid, fore_colour gray25;')
        first_idx = collection.add(style)

        style.pattern.pattern_fore_colour = xlwt.Style.colour_map['yellow']
        second_idx = collection.add(style)
        self.assertNotEqual(first_idx, second_idx)

        first_pattern = collection._xf_x2id[first_idx][4]
        self.assertEqual(
            first_pattern.pattern_fore_colour,
            xlwt.Style.colour_map['gray25'],
        )

    def test_frozen_components_are_read_only(self):
        collection = Style.StyleCollection(style_compression=2)
        style = xlwt.easyxf('font: bold on;')
        xf_idx = collection.add(style)
        frozen_font = collection._font_x2frozen[collection._xf_x2id[xf_idx][0]]
        with self.assertRaises(AttributeError):
            frozen_font.colour_index = xlwt.Style.colour_map['green']
        frozen_pattern = collection._xf_x2id[xf_idx][4]
        with self.assertRaises(AttributeError):
            frozen_pattern.pattern = 1

    @unittest.skipIf(xlrd is None, "xlrd is required to read the generated .xls")
    def test_old_row_keeps_original_colour_after_font_recolour(self):
        wb = xlwt.Workbook(style_compression=2)
        ws = wb.add_sheet('s')
        style = xlwt.XFStyle()
        style.font.colour_index = xlwt.Style.colour_map['red']
        ws.write(0, 0, 'old row', style)

        style.font.colour_index = xlwt.Style.colour_map['blue']
        ws.write(1, 0, 'new row', style)

        book = save_to_book(wb)
        sheet = book.sheet_by_index(0)
        first_xf = book.xf_list[sheet.cell_xf_index(0, 0)]
        second_xf = book.xf_list[sheet.cell_xf_index(1, 0)]
        self.assertEqual(
            book.font_list[first_xf.font_index].colour_index,
            xlwt.Style.colour_map['red'],
        )
        self.assertEqual(
            book.font_list[second_xf.font_index].colour_index,
            xlwt.Style.colour_map['blue'],
        )
        self.assertEqual(sheet.cell_value(0, 0), 'old row')
        self.assertEqual(sheet.cell_value(1, 0), 'new row')


class TestUnmergedDiffs(unittest.TestCase):

    def test_diff_entries_name_the_separating_components(self):
        collection = Style.StyleCollection(style_compression=2)
        border_a = xlwt.easyxf('borders: left thin, left_colour red;')
        border_b = xlwt.easyxf('borders: left thin, left_colour blue;')
        idx_a = collection.add(border_a)
        idx_b = collection.add(border_b)
        self.assertNotEqual(idx_a, idx_b)

        diffs = collection.unmerged_style_diffs()
        border_keys = {item['borders'] for item in diffs}
        self.assertEqual(len(border_keys), 2)
        for item in diffs:
            self.assertEqual(
                set(item),
                {'num_format_idx', 'num_format_str', 'font_idx',
                 'alignment', 'borders', 'pattern', 'protection'},
            )
            self.assertEqual(item['num_format_str'], 'General')

    def test_repeated_definition_is_reported_once(self):
        collection = Style.StyleCollection(style_compression=2)
        style = make_percent_style()
        collection.add(style)
        before = len(collection.unmerged_style_diffs())
        collection.add(style)
        collection.add(make_percent_style())
        self.assertEqual(len(collection.unmerged_style_diffs()), before)

    def test_stats_distinguish_identity_and_value_reuse(self):
        collection = Style.StyleCollection(style_compression=2)
        style = make_percent_style()
        collection.add(style)
        identity_stats = collection.stats[3]
        value_stats = collection.stats[4]

        collection.add(style)  # the very same object: identity reuse
        self.assertEqual(collection.stats[3], identity_stats + 1)

        collection.add(make_percent_style())  # fresh, value-equal object
        self.assertEqual(collection.stats[4], value_stats + 1)


def _build_style_order_a():
    # Font configured first, then the remaining components.
    font = Font()
    font.bold = True
    font.colour_index = xlwt.Style.colour_map['green']
    style = xlwt.XFStyle()
    style.font = font
    style.alignment.horz = xlwt.Formatting.Alignment.HORZ_CENTER
    style.pattern.pattern = xlwt.Formatting.Pattern.SOLID_PATTERN
    style.pattern.pattern_fore_colour = xlwt.Style.colour_map['gray25']
    style.num_format_str = '#,##0.00'
    return style


def _build_style_order_b():
    # Components configured first, font last; same logical definition.
    style = xlwt.XFStyle()
    style.pattern.pattern_fore_colour = xlwt.Style.colour_map['gray25']
    style.pattern.pattern = xlwt.Formatting.Pattern.SOLID_PATTERN
    style.alignment.horz = xlwt.Formatting.Alignment.HORZ_CENTER
    style.num_format_str = '#,##0.00'
    font = Font()
    font.colour_index = xlwt.Style.colour_map['green']
    font.bold = True
    style.font = font
    return style


class TestConstructionOrder(unittest.TestCase):

    def test_merge_result_is_independent_of_object_construction_order(self):
        first = Style.StyleCollection(style_compression=2)
        idx_a = first.add(_build_style_order_a())
        idx_b = first.add(_build_style_order_b())
        self.assertEqual(idx_a, idx_b)

        second = Style.StyleCollection(style_compression=2)
        idx_b_first = second.add(_build_style_order_b())
        idx_a_second = second.add(_build_style_order_a())
        self.assertEqual(idx_b_first, idx_a_second)

    @unittest.skipIf(xlrd is None, "xlrd is required to read the generated .xls")
    def test_cell_interpretation_matches_across_construction_orders(self):
        values = [0, 1, 2, 3, 4]

        def build(use_reversed_order):
            wb = xlwt.Workbook(style_compression=2)
            ws = wb.add_sheet('s')
            builders = [_build_style_order_a, _build_style_order_b]
            if use_reversed_order:
                builders = list(reversed(builders))
            styles = [builder() for builder in builders]
            for rowx, value in enumerate(values):
                ws.write(rowx, 0, value, styles[rowx % len(styles)])
            return save_to_book(wb)

        book_a = build(False)
        book_b = build(True)
        sheet_a = book_a.sheet_by_index(0)
        sheet_b = book_b.sheet_by_index(0)

        for rowx, value in enumerate(values):
            self.assertEqual(sheet_a.cell_type(rowx, 0),
                             sheet_b.cell_type(rowx, 0))
            self.assertEqual(sheet_a.cell_value(rowx, 0),
                             sheet_b.cell_value(rowx, 0))
            xf_a = book_a.xf_list[sheet_a.cell_xf_index(rowx, 0)]
            xf_b = book_b.xf_list[sheet_b.cell_xf_index(rowx, 0)]
            self.assertEqual(
                book_a.format_map[xf_a.format_key].format_str,
                book_b.format_map[xf_b.format_key].format_str,
            )
            self.assertEqual(
                book_a.font_list[xf_a.font_index].colour_index,
                book_b.font_list[xf_b.font_index].colour_index,
            )
            self.assertEqual(
                book_a.font_list[xf_a.font_index].bold,
                book_b.font_list[xf_b.font_index].bold,
            )


if __name__ == '__main__':
    unittest.main()
