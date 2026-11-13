#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import os
import struct
import unittest

import fitdecode
from fitdecode import FitStreamSegmenter, SegmentState
from fitdecode.utils import compute_crc

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), 'files')


def _make_fit(body, *, header_size=14):
    """Build a syntactically valid, CRC-consistent FIT file from a body."""
    fixed_header = struct.pack(
        '<2BHI4s', header_size, 16, 152, len(body), b'.FIT')

    if header_size == 14:
        header = fixed_header + struct.pack(
            '<H', compute_crc(fixed_header))
    else:
        header = fixed_header

    pre_crc = header + body
    return pre_crc + struct.pack('<H', compute_crc(pre_crc))


def _corrupt(data):
    """Flip a body byte so the file keeps its shape but fails its CRC."""
    corrupted = bytearray(data)
    corrupted[14] ^= 0xFF
    return bytes(corrupted)


def _ranges(results):
    return [(r.state, r.offset, r.end_offset) for r in results]


class FitStreamSegmenterTestCase(unittest.TestCase):

    def test_complete_segments_expose_offsets_and_boundaries(self):
        first = _make_fit(b'\x01' * 20)
        second = _make_fit(b'\x02' * 30, header_size=12)
        stream = first + second

        results = FitStreamSegmenter.segment(stream, keep_raw_chunks=True)

        self.assertEqual(
            _ranges(results),
            [
                (SegmentState.COMPLETE, 0, len(first)),
                (SegmentState.COMPLETE, len(first), len(stream)),
            ])

        first_result, second_result = results
        self.assertEqual(first_result.data, first)
        self.assertEqual(second_result.data, second)
        self.assertEqual(first_result.segment.body_size, 20)
        self.assertEqual(second_result.segment.header_size, 12)
        self.assertTrue(first_result.segment.crc_matched)
        self.assertEqual(
            first_result.end_offset - first_result.offset, len(first))

    def test_first_two_commit_while_third_waits_for_more_bytes(self):
        first = _make_fit(b'\x01' * 20)
        second = _make_fit(b'\x02' * 30)
        third = _make_fit(b'\x03' * 25)
        partial = first + second + third[:len(third) // 2]

        segmenter = FitStreamSegmenter(keep_raw_chunks=True)

        # feed the partial stream one byte at a time, like a slow receiver
        committed = []
        for index in range(len(partial)):
            committed.extend(segmenter.consume(partial[index:index + 1]))

        # only the two fully-received, CRC-valid files are committed
        self.assertEqual(
            _ranges(committed),
            [
                (SegmentState.COMPLETE, 0, len(first)),
                (SegmentState.COMPLETE, len(first), len(first) + len(second)),
            ])
        self.assertEqual(committed[0].data, first)
        self.assertEqual(committed[1].data, second)

        # the third file is pending and explicitly reported as incomplete
        pending = segmenter.flush()
        self.assertEqual(len(pending), 1)
        pending_result = pending[0]
        self.assertIs(pending_result.state, SegmentState.INCOMPLETE)
        self.assertEqual(
            pending_result.offset, len(first) + len(second))
        self.assertEqual(pending_result.segment.total_size, len(third))
        self.assertEqual(len(pending_result.data), len(third) // 2)

        # resume by sending ONLY the missing tail: nothing is re-received
        tail = third[len(third) // 2:]
        resumed = segmenter.consume(tail)
        self.assertEqual(
            _ranges(resumed),
            [(
                SegmentState.COMPLETE,
                len(first) + len(second),
                len(first) + len(second) + len(third))])
        self.assertEqual(resumed[0].data, third)
        self.assertEqual(segmenter.flush(), [])

    def test_corrupt_is_distinct_from_incomplete(self):
        good_shape = _make_fit(b'\x04' * 18)

        # fully received but wrong CRC -> corrupt, never complete/incomplete
        bad = _corrupt(good_shape)
        segmenter = FitStreamSegmenter(keep_raw_chunks=True)
        self.assertEqual(segmenter.consume(bad), [])
        closed = segmenter.close()
        self.assertEqual(len(closed), 1)
        self.assertIs(closed[0].state, SegmentState.CORRUPT)
        self.assertEqual(closed[0].offset, 0)
        self.assertEqual(closed[0].end_offset, len(bad))
        self.assertFalse(closed[0].segment.crc_matched)

        # header + part of the body -> incomplete, waiting for more bytes
        truncated = good_shape[:len(good_shape) - 5]
        segmenter = FitStreamSegmenter(keep_raw_chunks=True)
        self.assertEqual(segmenter.consume(truncated), [])
        flushed = segmenter.flush()
        self.assertEqual(len(flushed), 1)
        self.assertIs(flushed[0].state, SegmentState.INCOMPLETE)
        self.assertEqual(flushed[0].offset, 0)
        self.assertEqual(flushed[0].segment.total_size, len(good_shape))

    def test_header_looking_bytes_inside_corrupt_span_are_not_valid(self):
        fake_header = (
            struct.pack('<2BHI4s', 14, 16, 152, 40, b'.FIT')
            + b'\x00\x00')
        garbage = b'\xAA\xBB' + fake_header + b'\x51' * 30
        good = _make_fit(b'\x05' * 8)

        results = FitStreamSegmenter.segment(garbage + good, keep_raw_chunks=True)

        complete = [r for r in results if r.state is SegmentState.COMPLETE]
        corrupt = [r for r in results if r.state is SegmentState.CORRUPT]

        # the fake header never gets announced as a valid file
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0].data, good)
        self.assertEqual(complete[0].offset, len(garbage))

        # the whole garbage region is accounted for as one corrupt run
        self.assertEqual(len(corrupt), 1)
        self.assertEqual(corrupt[0].offset, 0)
        self.assertEqual(corrupt[0].end_offset, len(garbage))
        self.assertEqual(corrupt[0].data, garbage)

    def test_mismatching_header_crc_is_rejected_and_recovered(self):
        good = _make_fit(b'\x06' * 12)

        bad = bytearray(_make_fit(b'\x05' * 16))
        bad[12:14] = b'\xff\xff'  # corrupt the extended header's CRC field
        bad = bytes(bad)

        results = FitStreamSegmenter.segment(bad + good, keep_raw_chunks=True)
        corrupt = [r for r in results if r.state is SegmentState.CORRUPT]
        complete = [r for r in results if r.state is SegmentState.COMPLETE]

        self.assertEqual(len(corrupt), 1)
        self.assertEqual(corrupt[0].offset, 0)
        self.assertEqual(corrupt[0].end_offset, len(bad))
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0].offset, len(bad))
        self.assertEqual(complete[0].data, good)

    def test_recovery_resumes_after_corrupt_segment(self):
        bad = _corrupt(_make_fit(b'\x06' * 16))
        good = _make_fit(b'\x07' * 12)

        results = FitStreamSegmenter.segment(bad + good, keep_raw_chunks=True)
        corrupt = [r for r in results if r.state is SegmentState.CORRUPT]
        complete = [r for r in results if r.state is SegmentState.COMPLETE]

        self.assertEqual(len(corrupt), 1)
        self.assertEqual(corrupt[0].offset, 0)
        self.assertEqual(corrupt[0].end_offset, len(bad))
        self.assertEqual(len(complete), 1)
        self.assertEqual(complete[0].offset, len(bad))
        self.assertEqual(complete[0].end_offset, len(bad) + len(good))
        self.assertEqual(complete[0].data, good)

    def test_boundaries_are_stable_for_every_feed_cut(self):
        first = _make_fit(b'\x01' * 20)
        second = _make_fit(b'\x02' * 30)
        third = _make_fit(b'\x03' * 25)
        stream = first + second + third
        expected = [
            (SegmentState.COMPLETE, 0, len(first)),
            (SegmentState.COMPLETE, len(first), len(first) + len(second)),
            (
                SegmentState.COMPLETE,
                len(first) + len(second),
                len(stream))]

        for cut in range(1, len(stream)):
            segmenter = FitStreamSegmenter()
            first_results = segmenter.consume(stream[:cut])
            first_results.extend(segmenter.flush())
            second_results = segmenter.consume(stream[cut:])
            second_results.extend(segmenter.close())

            complete = [
                (r.state, r.offset, r.end_offset)
                for r in first_results + second_results
                if r.state is SegmentState.COMPLETE]
            self.assertEqual(complete, expected, msg=f'cut={cut}')

    def test_extended_header_split_across_feeds(self):
        data = _make_fit(b'\x08' * 10, header_size=14)

        segmenter = FitStreamSegmenter(keep_raw_chunks=True)
        # only the fixed 12-byte header part is there at first
        self.assertEqual(segmenter.consume(data[:12]), [])
        pending = segmenter.flush()
        self.assertIs(pending[0].state, SegmentState.INCOMPLETE)
        self.assertEqual(pending[0].segment.total_size, len(data))

        # the 2 header-CRC bytes and the rest arrive later
        results = segmenter.consume(data[12:])
        self.assertEqual(len(results), 1)
        self.assertIs(results[0].state, SegmentState.COMPLETE)
        self.assertEqual(results[0].data, data)

    def test_flush_does_not_consume_or_duplicate(self):
        first = _make_fit(b'\x09' * 10)
        tail = first + b'\x0A'  # a complete file plus an extra stray byte

        segmenter = FitStreamSegmenter(keep_raw_chunks=True)
        committed = segmenter.consume(tail)
        self.assertEqual(len(committed), 1)

        offset_before = segmenter.offset
        buffered_before = segmenter.buffered_size
        first_flush = segmenter.flush()
        # flush is non-destructive: state and buffer are untouched
        self.assertEqual(segmenter.offset, offset_before)
        self.assertEqual(segmenter.buffered_size, buffered_before)

        second_flush = segmenter.flush()
        # the stray byte is not re-announced, no overlapping corrupt range
        self.assertEqual(second_flush, [])
        self.assertEqual(first_flush, [])

        # closing finally classifies the residual stray byte as corrupt
        closed = segmenter.close()
        self.assertEqual(len(closed), 1)
        self.assertIs(closed[0].state, SegmentState.CORRUPT)
        self.assertEqual(closed[0].offset, len(first))
        self.assertEqual(closed[0].end_offset, len(tail))

        with self.assertRaises(ValueError):
            segmenter.consume(b'\x00')
        self.assertEqual(segmenter.close(), [])

    def test_offsets_available_without_raw_chunks(self):
        first = _make_fit(b'\x0B' * 8)
        results = FitStreamSegmenter.segment(first, keep_raw_chunks=False)
        self.assertEqual(len(results), 1)
        self.assertIs(results[0].state, SegmentState.COMPLETE)
        self.assertIsNone(results[0].data)
        self.assertEqual(results[0].offset, 0)
        self.assertEqual(results[0].end_offset, len(first))

    def test_reported_ranges_tile_the_whole_stream_without_gap(self):
        bad = _corrupt(_make_fit(b'\x0C' * 12))
        good = _make_fit(b'\x0D' * 9)
        stray = b'\xEE'
        stream = bad + good + stray

        results = FitStreamSegmenter.segment(stream, keep_raw_chunks=True)

        cursor = 0
        for result in results:
            self.assertEqual(result.offset, cursor)
            cursor = result.end_offset
        self.assertEqual(cursor, len(stream))

        # every byte is also reconstructable from the kept raw chunks
        self.assertEqual(b''.join(r.data for r in results), stream)

    def test_real_chained_fit_file(self):
        path = os.path.join(TEST_FILES_DIR, 'activity-settings.fit')
        with open(path, mode='rb') as handle:
            data = handle.read()

        results = FitStreamSegmenter.segment(data, keep_raw_chunks=True)
        complete = [r for r in results if r.state is SegmentState.COMPLETE]

        # this sample is two chained FIT files
        self.assertEqual(len(complete), 2)
        self.assertEqual(complete[0].offset, 0)
        self.assertEqual(complete[-1].end_offset, len(data))

        # each committed segment decodes on its own through the strict reader
        for result in complete:
            frames = tuple(fitdecode.FitReader(
                result.data,
                check_crc=fitdecode.CrcCheck.RAISE))
            self.assertIsInstance(frames[0], fitdecode.FitHeader)
            self.assertIsInstance(frames[-1], fitdecode.FitCRC)


if __name__ == '__main__':
    unittest.main()
