# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import gc
import os
import struct
import unittest

import xlwt
from xlwt.Revision import RevisionConflict


def decode_rk(rk):
    if rk & 0x02:
        value = rk >> 2
        return float(value)
    if rk & 0x01:
        return (rk >> 2) / 100.0
    return struct.unpack('<d', struct.pack('<Q', (rk & 0xFFFFFFFC) << 32))[0]


def records(data):
    pos = 0
    while pos + 4 <= len(data):
        rec_id, length = struct.unpack('<HH', data[pos:pos + 4])
        body = data[pos + 4:pos + 4 + length]
        yield pos, rec_id, body
        pos += 4 + length


class SheetSnapshot(object):
    """Read the first worksheet back from a workbook's BIFF stream."""

    def __init__(self, book):
        data = book.get_biff_data()
        self.cells = {}
        self.xf_indexes = []
        sheet_starts = []
        xf_count = 0
        for _pos, rec_id, body in records(data):
            if rec_id == 0x00E0:
                xf_count += 1
            elif rec_id == 0x0085:
                sheet_starts.append(struct.unpack('<L', body[0:4])[0])
        if not sheet_starts:
            raise AssertionError('workbook has no worksheet stream')

        sst = {}
        table = book._Workbook__sst
        for text, index in table._str_indexes.items():
            sst[index] = text
        for rich, index in table._rt_indexes.items():
            sst[index] = ''.join(piece for piece, _font in rich)

        pending_mulrk = None
        for _pos, rec_id, body in records(data[sheet_starts[0]:]):
            if rec_id == 0x000A:  # EOF
                break
            if rec_id == 0x0203:  # NUMBER
                row, col, xf, value = struct.unpack('<HHHd', body[:14])
                self.cells[(row, col)] = value
                self.xf_indexes.append(xf)
            elif rec_id == 0x027E:  # RK
                row, col, xf, rk = struct.unpack('<HHHi', body[:10])
                self.cells[(row, col)] = decode_rk(rk)
                self.xf_indexes.append(xf)
            elif rec_id == 0x00BD:  # MULRK
                row, first_col = struct.unpack('<HH', body[:4])
                count = (len(body) - 6) // 6
                for index in range(count):
                    xf, rk = struct.unpack('<Hi', body[4 + 6 * index:
                                                      10 + 6 * index])
                    self.cells[(row, first_col + index)] = decode_rk(rk)
                    self.xf_indexes.append(xf)
            elif rec_id == 0x00FD:  # LABELSST
                row, col, xf, sst_index = struct.unpack('<HHHL', body[:10])
                self.cells[(row, col)] = sst[sst_index]
                self.xf_indexes.append(xf)
            elif rec_id == 0x0201:  # BLANK
                row, col, xf = struct.unpack('<HHH', body[:6])
                self.cells[(row, col)] = ''
                self.xf_indexes.append(xf)
            elif rec_id == 0x00BE:  # MULBLANK
                row, first_col = struct.unpack('<HH', body[:4])
                count = (len(body) - 6) // 2
                for index in range(count):
                    xf = struct.unpack('<H', body[4 + 2 * index:
                                                  6 + 2 * index])[0]
                    self.cells[(row, first_col + index)] = ''
                    self.xf_indexes.append(xf)
            elif rec_id == 0x0205:  # BOOLERR
                row, col, xf, value, is_error = struct.unpack(
                    '<HHHBB', body[:8])
                self.cells[(row, col)] = bool(value) if not is_error else value
                self.xf_indexes.append(xf)
            elif rec_id == 0x0204:  # LABEL
                row, col, xf = struct.unpack('<HHH', body[:6])
                self.cells[(row, col)] = body[8:].decode('utf-16-le')
                self.xf_indexes.append(xf)
        self.xf_count = xf_count

    def rows(self):
        if not self.cells:
            return []
        max_row = max(row for row, _col in self.cells)
        max_col = max(col for _row, col in self.cells)
        return [[self.cells.get((row, col), '') for col in range(max_col + 1)]
                for row in range(max_row + 1)]


def build(revision_before_flush):
    """Build a small inventory sheet with a correction and a conflict."""
    book = xlwt.Workbook()
    sheet = book.add_sheet('Stock')
    sheet.write(0, 0, 'SKU')
    sheet.write(0, 1, 'Revision')
    sheet.write(0, 2, 'Name')
    sheet.write(0, 3, 'Qty')
    stream = sheet.get_revision_stream(
        key='sku', revision='revision', start_row=1)

    stream.write({'sku': 'A-100', 'revision': 1, 'name': 'widget', 'qty': 4})
    stream.write({'sku': 'B-200', 'revision': 2, 'name': 'bolt', 'qty': 9})
    if revision_before_flush:
        stream.write({'sku': 'A-100', 'revision': 3,
                      'name': 'widget v3', 'qty': 7})
        sheet.flush_row_data()
        stream.write({'sku': 'B-200', 'revision': 2,
                      'name': 'bolt', 'qty': 11})
    else:
        sheet.flush_row_data()
        stream.write({'sku': 'A-100', 'revision': 3,
                      'name': 'widget v3', 'qty': 7})
        stream.write({'sku': 'B-200', 'revision': 2,
                      'name': 'bolt', 'qty': 11})
    return book, stream


class TestRevisionRowStream(unittest.TestCase):

    def test_accepts_higher_revision_and_keeps_first_order(self):
        book, stream = build(revision_before_flush=False)
        self.assertEqual(stream.row_count, 2)
        self.assertEqual(stream.conflict_count, 1)
        self.assertEqual([item.key for item in stream.conflicts], ['B-200'])

        snapshot = SheetSnapshot(book)
        self.assertEqual(snapshot.rows(), [
            ['SKU', 'Revision', 'Name', 'Qty'],
            ['A-100', 3.0, 'widget v3', 7.0],
            ['B-200', 2.0, 'bolt', 9.0],
        ])
        self.assertTrue(all(xf < snapshot.xf_count
                            for xf in snapshot.xf_indexes))

    def test_correction_before_or_after_flush_is_identical(self):
        before, _ = build(revision_before_flush=True)
        after, _ = build(revision_before_flush=False)
        self.assertEqual(before.get_biff_data(), after.get_biff_data())

    def test_equal_revision_equal_values_is_not_a_conflict(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(key='sku', revision='rev')
        result = stream.write({'sku': 'A', 'rev': 1, 'v': 'x'})
        self.assertEqual(result.status, 'new')
        result = stream.write({'sku': 'A', 'rev': 1, 'v': 'x'})
        self.assertEqual(result.status, 'duplicate')
        self.assertEqual(stream.conflict_count, 0)

    def test_equivalent_numbers_are_a_duplicate(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(key='sku', revision='rev')
        stream.write({'sku': 'A', 'rev': 1, 'qty': 3})
        result = stream.write({'sku': 'A', 'rev': 1, 'qty': 3.0})
        self.assertEqual(result.status, 'duplicate')
        self.assertEqual(stream.conflict_count, 0)

    def test_stale_revision_is_rejected(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(key='sku', revision='rev')
        stream.write({'sku': 'A', 'rev': 5, 'v': 'new'})
        result = stream.write({'sku': 'A', 'rev': 2, 'v': 'old'})
        self.assertEqual(result.status, 'stale')
        self.assertFalse(result.accepted)
        self.assertEqual(stream.row_count, 1)

    def test_raise_on_conflict(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(
            key='sku', revision='rev', on_conflict='raise')
        stream.write({'sku': 'A', 'rev': 1, 'v': 'x'})
        with self.assertRaises(RevisionConflict):
            stream.write({'sku': 'A', 'rev': 1, 'v': 'y'})
        self.assertEqual(stream.conflict_count, 1)

    def test_sequence_rows_and_explicit_call_form(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(key=0, revision=1)
        stream.write(('A', 1, 'first'))
        stream.write('A', 2, ('second', 'extra'))
        stream.write(('B', 1, 'other'))
        snapshot = SheetSnapshot(book)
        self.assertEqual(snapshot.rows(), [
            ['second', 'extra', ''],
            ['B', 1.0, 'other'],
        ])

    def test_columns_subset_of_mapping_rows(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(
            key='sku', revision='rev', columns=['sku', 'qty'])
        stream.write({'sku': 'A', 'rev': 1, 'qty': 3, 'note': 'ignored'})
        self.assertEqual(SheetSnapshot(book).rows(), [['A', 3.0]])

    def test_narrower_correction_removes_old_cells(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(key=0, revision=1)
        stream.write(('A', 1, 'one', 'two'))
        stream.write('A', 2, ('only',))
        self.assertEqual(SheetSnapshot(book).rows(), [['only']])

    def test_blank_boolean_and_unicode_values(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(key=0, revision=1)
        stream.write(('A', 1, None, '', True))
        stream.write(('B', 1, '中文'))
        snapshot = SheetSnapshot(book)
        self.assertEqual(snapshot.cells[(0, 0)], 'A')
        self.assertEqual(snapshot.cells[(0, 2)], '')
        self.assertEqual(snapshot.cells[(0, 3)], '')
        self.assertEqual(snapshot.cells[(1, 2)], '中文')

    def test_disk_payload_lifecycle(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        stream = sheet.get_revision_stream(key=0, revision=1)
        stream.write(('A', 1, 'x' * 2000))
        stream.flush()
        path = stream._path
        self.assertTrue(os.path.exists(path))
        stream.close()
        self.assertTrue(os.path.exists(path))
        stream.discard()
        self.assertFalse(os.path.exists(path))

    def test_workbook_survives_gc(self):
        book, _stream = build(revision_before_flush=False)
        gc.collect()
        rows = SheetSnapshot(book).rows()
        self.assertEqual(rows[1][2], 'widget v3')

    def test_two_streams_keep_disjoint_ranges(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('Stock')
        sheet.write(0, 0, 'header')
        first = sheet.get_revision_stream(key=0, revision=1, start_row=1)
        second = sheet.get_revision_stream(key=0, revision=1, start_row=3)
        first.write(('A', 1, 'a'))
        second.write(('B', 1, 'b'))
        self.assertEqual(SheetSnapshot(book).rows(), [
            ['header', '', ''],
            ['A', 1.0, 'a'],
            ['', '', ''],
            ['B', 1.0, 'b'],
        ])
