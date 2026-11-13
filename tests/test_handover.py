# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import unittest
from io import BytesIO

import xlrd
import xlwt
from xlwt import HandoverColumn, write_handover
from xlwt.Handover import (
    ORDER_TOTAL_LABEL,
    OWNER_TOTAL_LABEL,
    PAGE_SUBTOTAL_LABEL,
)

HEADER_ROWS = 2  # title row + column caption row


def make_row(owner, order, seq):
    return {
        'owner': owner,
        'order': order,
        'item': '货物-%d' % seq,
        'pieces': seq,
        'weight': seq * 10,
    }


def make_rows(owner, order, count, start=1):
    return [make_row(owner, order, seq)
            for seq in range(start, start + count)]


def render(rows, row_limit, **kw):
    workbook = xlwt.Workbook()
    write_handover(workbook, iter(rows), row_limit, **kw)
    stream = BytesIO()
    workbook.save(stream)
    return xlrd.open_workbook(file_contents=stream.getvalue())


def sheet_rows(book, index):
    sheet = book.sheet_by_index(index)
    return [[sheet.cell_value(r, c) for c in range(sheet.ncols)]
            for r in range(sheet.nrows)]


def body_rows(book, index):
    return sheet_rows(book, index)[HEADER_ROWS:]


class TestHandover(unittest.TestCase):

    def test_single_page_grouping_and_totals(self):
        rows = (make_rows('货主甲', 'A1', 2, start=1) +
                make_rows('货主甲', 'A2', 1, start=3) +
                make_rows('货主乙', 'B1', 1, start=4))
        book = render(rows, 50)
        self.assertEqual(book.nsheets, 1)
        body = body_rows(book, 0)
        # 货主甲 / A1: two details + 整单合计
        self.assertEqual(body[0][0], 'A1')
        self.assertEqual(body[1][0], 'A1')
        self.assertTrue(body[2][0].endswith(ORDER_TOTAL_LABEL))
        self.assertIn('A1', body[2][0])
        self.assertEqual(body[2][2], 3)    # pieces 1 + 2
        self.assertEqual(body[2][3], 30)   # weight 10 + 20
        # 货主甲 / A2
        self.assertTrue(body[4][0].endswith(ORDER_TOTAL_LABEL))
        self.assertEqual(body[4][2], 3)
        # 货主甲 合计
        self.assertTrue(body[5][0].endswith(OWNER_TOTAL_LABEL))
        self.assertIn('货主甲', body[5][0])
        self.assertEqual(body[5][2], 6)
        self.assertEqual(body[5][3], 60)
        # 货主乙 / B1 + 货主乙 合计
        self.assertTrue(body[7][0].endswith(ORDER_TOTAL_LABEL))
        self.assertEqual(body[7][2], 4)
        self.assertTrue(body[8][0].endswith(OWNER_TOTAL_LABEL))
        self.assertEqual(body[8][2], 4)

    def test_header_repeated_on_every_page(self):
        rows = make_rows('货主甲', 'A1', 2) + make_rows('货主甲', 'A2', 2, start=3)
        book = render(rows, 6)
        self.assertEqual(book.nsheets, 2)
        for page in range(book.nsheets):
            grid = sheet_rows(book, page)
            self.assertIn('第%d页' % (page + 1), grid[0][0])
            self.assertEqual(grid[1], ['发运单号', '货物名称', '件数', '重量'])

    def test_order_that_fits_is_not_split(self):
        # capacity = 6 - 2 = 4 body rows; each order needs 2 + 1 = 3,
        # so one order per page and no order may be split.
        rows = []
        for name in ('A1', 'A2', 'A3'):
            rows += make_rows('货主甲', name, 2)
        book = render(rows, 6)
        self.assertEqual(book.nsheets, 3)
        for page, name in enumerate(('A1', 'A2', 'A3')):
            body = body_rows(book, page)
            self.assertEqual([r[0] for r in body[:2]], [name, name])
            self.assertTrue(body[2][0].endswith(ORDER_TOTAL_LABEL))
        # the last page also carries the 货主合计, nothing is blank
        self.assertTrue(body_rows(book, 2)[3][0].endswith(OWNER_TOTAL_LABEL))

    def test_oversized_order_continuation(self):
        # capacity = 5 - 2 = 3 body rows; the order has 7 details.
        rows = make_rows('货主甲', 'A1', 7)
        book = render(rows, 5)
        self.assertEqual(book.nsheets, 4)
        # pages 1-3: two details + 本页小计 each
        subtotals = []
        for page in range(3):
            body = body_rows(book, page)
            self.assertEqual(len(body), 3)
            self.assertTrue(body[2][0].endswith(PAGE_SUBTOTAL_LABEL))
            self.assertIn('A1', body[2][0])
            subtotals.append(body[2][2])
        self.assertEqual(subtotals, [3, 7, 11])  # 1+2, 3+4, 5+6
        # last page: final detail + 整单合计 + 货主合计
        last = body_rows(book, 3)
        self.assertTrue(last[1][0].endswith(ORDER_TOTAL_LABEL))
        self.assertEqual(last[1][2], 28)
        self.assertTrue(last[2][0].endswith(OWNER_TOTAL_LABEL))
        self.assertEqual(last[2][2], 28)
        # page subtotals partition the order: every piece counted once
        self.assertEqual(sum(subtotals) + 7, 28)
        details_pieces = sum(
            row[2]
            for page in range(book.nsheets)
            for row in body_rows(book, page)
            if not (row[0].endswith(PAGE_SUBTOTAL_LABEL) or
                    row[0].endswith(ORDER_TOTAL_LABEL) or
                    row[0].endswith(OWNER_TOTAL_LABEL)))
        self.assertEqual(details_pieces, 28)

    def test_exact_fit_leaves_no_blank_page(self):
        # capacity = 4 - 2 = 2: one detail + 整单合计 exactly fill a page.
        rows = make_rows('货主甲', 'A1', 1)
        book = render(rows, 4)
        self.assertEqual(book.nsheets, 2)  # page 2 holds only 货主合计
        self.assertEqual(len(sheet_rows(book, 0)), 4)
        for page in range(book.nsheets):
            self.assertGreater(len(sheet_rows(book, page)), HEADER_ROWS)

    def test_exact_fit_followed_by_more_orders(self):
        rows = (make_rows('货主甲', 'A1', 1) +
                make_rows('货主甲', 'A2', 1, start=2))
        book = render(rows, 4)
        self.assertEqual(book.nsheets, 3)
        # page 2 starts directly with A2's detail, no blank page in between
        self.assertEqual(body_rows(book, 1)[0][0], 'A2')
        for page in range(book.nsheets):
            self.assertGreater(len(sheet_rows(book, page)), HEADER_ROWS)

    def test_ungrouped_stream_rejected(self):
        rows = (make_rows('货主甲', 'A1', 1) +
                make_rows('货主甲', 'A2', 1, start=2) +
                make_rows('货主甲', 'A1', 1, start=3))
        with self.assertRaises(ValueError):
            render(rows, 50)

    def test_owner_reappearing_rejected(self):
        rows = (make_rows('货主甲', 'A1', 1) +
                make_rows('货主乙', 'B1', 1, start=2) +
                make_rows('货主甲', 'A2', 1, start=3))
        with self.assertRaises(ValueError):
            render(rows, 50)

    def test_row_limit_too_small_rejected(self):
        with self.assertRaises(ValueError):
            render(make_rows('货主甲', 'A1', 1), 3)

    def test_custom_columns_and_keys(self):
        columns = [
            HandoverColumn('order_no', '发运单'),
            HandoverColumn('qty', '数量', total=True),
        ]
        rows = [
            {'shipper': '货主甲', 'order_no': 'A1', 'qty': 5},
            {'shipper': '货主甲', 'order_no': 'A1', 'qty': 7},
        ]
        book = render(rows, 10, owner_key='shipper', order_key='order_no',
                      columns=columns, sheet_name='交接', title='交接单')
        body = body_rows(book, 0)
        self.assertTrue(body[2][0].endswith(ORDER_TOTAL_LABEL))
        self.assertEqual(body[2][1], 12)
        self.assertTrue(body[3][0].endswith(OWNER_TOTAL_LABEL))
        self.assertEqual(body[3][1], 12)


if __name__ == '__main__':
    unittest.main()
