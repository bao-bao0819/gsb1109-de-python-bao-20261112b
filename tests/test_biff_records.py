# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import unittest
from io import BytesIO
from struct import unpack

import xlwt

SST_ID = 0x00FC
CONTINUE_ID = 0x003C
LABELSST_ID = 0x00FD


def biff_records(stream):
    """Split a BIFF8 substream into (record id, payload) pairs."""
    pos = 0
    while pos + 4 <= len(stream):
        rec_id, length = unpack('<HH', stream[pos:pos+4])
        yield rec_id, stream[pos+4:pos+4+length]
        pos += 4 + length


def read_sst(stream):
    """Independent BIFF8 SST reader; returns (total, unique, strings, runs).

    Each chunk of character data is decoded as it is encountered (the way
    Excel and xlrd do it), so a surrogate pair split over a CONTINUE
    boundary shows up as invalid text just as it does in those readers.
    """
    pieces = []
    for rec_id, data in biff_records(stream):
        if rec_id == SST_ID:
            pieces.append(data)
        elif pieces and rec_id == CONTINUE_ID:
            pieces.append(data)
        elif pieces:
            break
    assert pieces, 'no SST record found'
    total, unique = unpack('<II', pieces[0][0:8])
    seg, pos = 0, 8
    strings, runlists = [], []
    for _ in range(unique):
        data = pieces[seg]
        nchars = unpack('<H', data[pos:pos+2])[0]
        pos += 2
        options = data[pos]
        pos += 1
        rtcount = 0
        if options & 0x08:
            rtcount = unpack('<H', data[pos:pos+2])[0]
            pos += 2
        phon_size = unpack('<I', data[pos:pos+4])[0] if options & 0x04 else 0
        if options & 0x04:
            pos += 4
        wide = bool(options & 0x01)
        text = []
        got = 0
        while got < nchars:
            data = pieces[seg]
            avail = len(data) - pos
            cap = avail // 2 if wide else avail
            take = min(cap, nchars - got)
            if take <= 0:
                seg += 1
                data = pieces[seg]
                wide = bool(data[0] & 0x01)
                pos = 1
                continue
            step = 2 * take if wide else take
            text.append(bytes(data[pos:pos+step]).decode(
                'utf-16-le' if wide else 'latin-1'))
            pos += step
            got += take
        strings.append(''.join(text))
        runs = []
        for _ in range(rtcount):
            while pos >= len(pieces[seg]):
                seg += 1
                pos = 0
            data = pieces[seg]
            runs.append(unpack('<HH', data[pos:pos+4]))
            pos += 4
        pos += phon_size
        runlists.append(runs)
    return total, unique, strings, runlists


def utf16_units(text):
    return len(text.encode('utf-16-le')) // 2


def build_sheet(pieces):
    book = xlwt.Workbook(encoding='utf-8')
    sheet = book.add_sheet('S')
    sheet.write_rich_text(0, 0, pieces)
    return book


def rich_pieces(total, char, bold_font, plain_font, run_len=37):
    pieces = []
    written = 0
    while written < total:
        pieces.append((char * run_len,
                       bold_font if len(pieces) % 2 == 0 else plain_font))
        written += utf16_units(char) * run_len
    return pieces


class TestSharedStringTable(unittest.TestCase):
    def test_shared_string_table(self):
        expected_result = b'\xfc\x00\x11\x00\x01\x00\x00\x00\x01\x00\x00\x00\x03\x00\x01\x1e\x04;\x04O\x04'
        string_record = xlwt.BIFFRecords.SharedStringTable(encoding='cp1251')
        string_record.add_str('Оля')
        self.assertEqual(expected_result, string_record.get_biff_record())

    def test_short_string_stays_compressed(self):
        sst = xlwt.BIFFRecords.SharedStringTable(encoding='utf-8')
        sst.add_str('ascii')
        data = sst.get_biff_record()
        # record header (4) + counts (8) + length (2) + options (1) + 5 chars
        self.assertEqual(len(data), 4 + 8 + 2 + 1 + 5)

    def test_rich_string_without_bmp_forces_wide_encoding(self):
        sst = xlwt.BIFFRecords.SharedStringTable(encoding='utf-8')
        sst.add_rt([('abc', 8), ('\U0001F600', 9)])
        total, unique, strings, runs = read_sst(sst.get_biff_record())
        self.assertEqual((total, unique), (1, 1))
        self.assertEqual(strings[0], 'abc\U0001F600')
        self.assertEqual(runs[0], [(0, 8), (3, 9)])

    def test_long_rich_string_runs_survive_continue(self):
        bold = xlwt.Font()
        bold.name = 'Arial'
        bold.bold = True
        plain = xlwt.Font()
        plain.name = 'Arial'
        for char in ('a', '中', '✈'):
            for total in (4000, 4100, 8200, 9000):
                pieces = rich_pieces(total, char, bold, plain)
                book = build_sheet(pieces)
                total_refs, unique, strings, runs = read_sst(book.get_biff_data())
                expected_text = ''.join(text for text, _ in pieces)
                expected = []
                offset = 0
                for text, font in pieces:
                    expected.append((offset, 8 if font.bold else 9))
                    offset += utf16_units(text)
                self.assertEqual(unique, 1)
                self.assertEqual(total_refs, 1)
                self.assertEqual(strings[0], expected_text)
                self.assertEqual(runs[0], expected)

    def test_long_rich_notes_with_emoji_round_trip(self):
        bold = xlwt.Font()
        bold.name = 'Arial'
        bold.bold = True
        plain = xlwt.Font()
        plain.name = 'Arial'
        for total in (3000, 4098, 4100, 8196, 9000):
            pieces = rich_pieces(total, '\U0001F600', bold, plain)
            book = build_sheet(pieces)
            total_refs, unique, strings, runs = read_sst(book.get_biff_data())
            expected_text = ''.join(text for text, _ in pieces)
            expected = []
            offset = 0
            for text, font in pieces:
                expected.append((offset, 8 if font.bold else 9))
                offset += utf16_units(text)
            self.assertEqual(strings[0], expected_text)
            self.assertEqual(runs[0], expected)
            self.assertEqual(total_refs, 1)
            self.assertEqual(unique, 1)

    def test_surrogate_pair_not_split_across_continue(self):
        sst = xlwt.BIFFRecords.SharedStringTable(encoding='utf-8')
        sst.add_rt([('\U0001F600' * 4100, 8)])
        pieces = []
        for rec_id, data in biff_records(sst.get_biff_record()):
            pieces.append(data)
        for piece in pieces[:-1]:
            last = piece[-2:]
            self.assertTrue(0xDC00 <= (last[0] | (last[1] << 8)) <= 0xDFFF,
                            'CONTINUE record ended with a lone high surrogate')
        for piece in pieces[1:]:
            first = piece[1:3]
            self.assertTrue(0xD800 <= (first[0] | (first[1] << 8)) <= 0xDBFF,
                            'CONTINUE record started with a lone low surrogate')

    def test_same_text_different_bold_is_distinct_shared_string(self):
        bold = xlwt.Font()
        bold.name = 'Arial'
        bold.bold = True
        plain = xlwt.Font()
        plain.name = 'Arial'
        book = xlwt.Workbook(encoding='utf-8')
        sheet = book.add_sheet('S')
        first = [('abc', plain), ('note', bold), ('def', plain)]
        second = [('ab', plain), ('cnote', bold), ('def', plain)]
        for col in range(3):
            sheet.write_rich_text(0, col, first)
        for col in range(3, 6):
            sheet.write_rich_text(0, col, second)
        total_refs, unique, strings, runs = read_sst(book.get_biff_data())
        self.assertEqual(unique, 2)
        self.assertEqual(total_refs, 6)
        self.assertEqual(strings[0], strings[1])
        self.assertEqual(runs[0], [(0, 8), (3, 9), (7, 8)])
        self.assertEqual(runs[1], [(0, 8), (2, 9), (7, 8)])

    def test_deleted_rich_string_slot_is_reused_by_plain_text(self):
        book = xlwt.Workbook(encoding='utf-8')
        sheet = book.add_sheet('S')
        bold = xlwt.Font()
        bold.name = 'Arial'
        bold.bold = True
        plain = xlwt.Font()
        plain.name = 'Arial'
        sheet.write_rich_text(0, 0, [('abc', plain), ('x', bold)])
        sheet._cell_overwrite_ok = True
        sheet.write(0, 0, 'plain')   # replaces the rich string cell
        total_refs, unique, strings, _ = read_sst(book.get_biff_data())
        self.assertEqual(total_refs, 1)
        self.assertEqual(unique, 2)
        self.assertEqual(strings, ['', 'plain'])


class TestIntersheetsRef(unittest.TestCase):
    def test_intersheets_ref(self):
        book = xlwt.Workbook()
        sheet_a = book.add_sheet('A')
        sheet_a.write(0, 0, 'A1')
        sheet_a.write(0, 1, 'A2')
        sheet_b = book.add_sheet('B')
        sheet_b.write(0, 0, xlwt.Formula("'A'!$A$1&'A'!$A$2"))
        out = BytesIO()
        book.save(out)
