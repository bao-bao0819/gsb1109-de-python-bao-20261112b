#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import datetime
import struct
import unittest

import fitdecode
from fitdecode.hr import (
    HR_EVENT_TIMESTAMP_SCALE, HrBeatIndex, HrMessageIndexingProcessor,
    index_hr_stream)

# a small device-relative second value (below fitdecode.FIT_DATETIME_MIN), so
# the timestamp field stays an int and every event_timestamp tick fits a uint32
ANCHOR_SECOND = 100000

MESG_NUM_FILE_ID = 0
MESG_NUM_HR = 132

FIELD_NUM_TIMESTAMP = 253
FIELD_NUM_HR_FILTERED_BPM = 6
FIELD_NUM_HR_EVENT_TIMESTAMP = 9
FIELD_NUM_HR_EVENT_TIMESTAMP_12 = 10

BASE_UINT8 = 0x02
BASE_UINT32 = 0x86
BASE_BYTE = 0x0d


def _def_message(local_mesg_num, global_mesg_num, field_defs):
    data = struct.pack('<B', 0x40 | local_mesg_num)
    # reserved byte, architecture (0 = little endian), global num, field count
    data += struct.pack('<xBHB', 0, global_mesg_num, len(field_defs))
    for def_num, size, base_type in field_defs:
        data += struct.pack('<3B', def_num, size, base_type)
    return data


def _data_message(local_mesg_num, payload):
    return struct.pack('<B', local_mesg_num) + payload


def _file_id_message(local_mesg_num=0):
    data = _def_message(local_mesg_num, MESG_NUM_FILE_ID, [
        (3, 4, 0x8c),  # serial_number, uint32z
        (4, 4, BASE_UINT32),  # time_created, uint32
        (1, 2, 0x84),  # manufacturer, uint16
        (2, 2, 0x84),  # product, uint16
        (0, 1, 0x00)])  # type, enum
    payload = struct.pack('<IIHHB', 558069241, ANCHOR_SECOND, 1, 1036, 4)
    return data + _data_message(local_mesg_num, payload)


def _anchor_hr_message(local_mesg_num, tick, bpm,
                       timestamp_second=ANCHOR_SECOND):
    """An hr message carrying a single, absolute event_timestamp (field 9)."""
    data = _def_message(local_mesg_num, MESG_NUM_HR, [
        (FIELD_NUM_TIMESTAMP, 4, BASE_UINT32),
        (FIELD_NUM_HR_EVENT_TIMESTAMP, 4, BASE_UINT32),
        (FIELD_NUM_HR_FILTERED_BPM, 1, BASE_UINT8)])
    payload = struct.pack('<IIB', timestamp_second, tick, bpm)
    return data + _data_message(local_mesg_num, payload)


def _pack_12bit(values):
    bit_buffer = 0
    bit_count = 0
    out = bytearray()
    for value in values:
        bit_buffer |= value << bit_count
        bit_count += 12
        while bit_count >= 8:
            out.append(bit_buffer & 0xFF)
            bit_buffer >>= 8
            bit_count -= 8
    if bit_count:
        out.append(bit_buffer & 0xFF)
    return bytes(out)


def _packed_hr_message(local_mesg_num, component_ticks, bpms,
                       timestamp_second=ANCHOR_SECOND):
    """An hr message carrying event_timestamp_12 (field 10) components."""
    assert len(component_ticks) == len(bpms)
    assert len(component_ticks) % 2 == 0  # keeps 12-bit packing byte-exact
    ts12 = _pack_12bit(component_ticks)
    data = _def_message(local_mesg_num, MESG_NUM_HR, [
        (FIELD_NUM_TIMESTAMP, 4, BASE_UINT32),
        (FIELD_NUM_HR_EVENT_TIMESTAMP_12, len(ts12), BASE_BYTE),
        (FIELD_NUM_HR_FILTERED_BPM, len(bpms), BASE_UINT8)])
    payload = (
        struct.pack('<I', timestamp_second) +
        ts12 +
        bytes(bpms))
    return data + _data_message(local_mesg_num, payload)


def _fit_file(body):
    header = struct.pack(
        '<2BHI4s', 14, 16, 152, len(body), b'.FIT')
    header += struct.pack('<H', fitdecode.utils.compute_crc(header))
    file_data = header + body
    return file_data + struct.pack(
        '<H', fitdecode.utils.compute_crc(file_data))


def _build_overlap_stream():
    body = _file_id_message()

    # message 0: absolute anchor beat at tick T*1024+100, bpm 70
    body += _anchor_hr_message(
        1, ANCHOR_SECOND * HR_EVENT_TIMESTAMP_SCALE + 100, 70)

    # message 1: four packed beats, ticks T*1024+200/900/1600/2300
    body += _packed_hr_message(1, [200, 900, 1600, 2300], [71, 72, 73, 74])

    # message 2: reconnect anchor re-advertising tick T*1024+1600 with a
    # different bpm (75 instead of 73): same event position, new measurement
    body += _anchor_hr_message(
        1, ANCHOR_SECOND * HR_EVENT_TIMESTAMP_SCALE + 1600, 75)

    # message 3: re-sends tick T*1024+2300 (same bpm, overlapping packet) and
    # appends a new beat at T*1024+3000
    body += _packed_hr_message(1, [2300, 3000], [74, 80])

    return _fit_file(body)


class HrBeatIndexTestCase(unittest.TestCase):

    def test_per_beat_observations_keep_device_precision(self):
        index = index_hr_stream(fitdecode.FitReader(_build_overlap_stream()))

        # 6 unique beats: anchor + four packed + one new, duplicates merged
        self.assertEqual(len(index), 6)

        ticks = [beat.tick for beat in index.observations()]
        self.assertEqual(ticks, sorted(ticks))
        self.assertEqual(ticks, [
            ANCHOR_SECOND * 1024 + offset
            for offset in (100, 200, 900, 1600, 2300, 3000)])

        # raw integer tick precision is preserved end-to-end
        beat = index.get_observation(ANCHOR_SECOND * 1024 + 200)
        self.assertEqual(
            beat.timestamp_seconds,
            ANCHOR_SECOND + 200 / HR_EVENT_TIMESTAMP_SCALE)
        self.assertIsInstance(beat.timestamp_datetime, datetime.datetime)
        self.assertEqual(
            beat.timestamp_datetime.tzinfo, datetime.timezone.utc)

        # direct lookups
        self.assertIs(
            index.get_observation(seconds=beat.timestamp_seconds), beat)
        self.assertIs(
            index.get_observation(moment=beat.timestamp_datetime), beat)
        self.assertIn(beat.tick, index)
        with self.assertRaises(KeyError):
            index.get_observation(beat.tick + 1)
        with self.assertRaises(ValueError):
            index.get_observation()
        with self.assertRaises(ValueError):
            index.get_observation(tick=1, seconds=2.0)

    def test_two_beats_in_same_second_are_not_merged(self):
        index = index_hr_stream(fitdecode.FitReader(_build_overlap_stream()))

        # offsets 100, 200 and 900 share the anchor second
        same_second = list(
            index.observations_at_second(ANCHOR_SECOND))
        self.assertEqual(len(same_second), 3)
        self.assertEqual(
            {beat.tick for beat in same_second},
            {ANCHOR_SECOND * 1024 + offset for offset in (100, 200, 900)})

        # offsets 2300 and 3000 land two seconds later: two distinct beats in
        # the same second, kept separate
        later_second = list(
            index.observations_at_second(ANCHOR_SECOND + 2))
        self.assertEqual(len(later_second), 2)
        self.assertEqual(
            {beat.tick for beat in later_second},
            {ANCHOR_SECOND * 1024 + offset for offset in (2300, 3000)})

    def test_overlap_adds_source_without_duplicating_beat(self):
        index = index_hr_stream(fitdecode.FitReader(
            _build_overlap_stream(), keep_raw_chunks=True))

        beat = index.get_observation(ANCHOR_SECOND * 1024 + 2300)
        self.assertEqual(beat.filtered_bpm, 74)
        self.assertFalse(beat.conflict)

        # advertised by message 1 (event index 3) and message 3 (event index 0)
        self.assertEqual(len(beat.sources), 2)
        first_ref, second_ref = beat.sources
        self.assertEqual(
            (first_ref.message_index, first_ref.event_index), (1, 3))
        self.assertEqual(
            (second_ref.message_index, second_ref.event_index), (3, 0))
        self.assertEqual(first_ref.fit_file_index, 0)
        self.assertIsNotNone(first_ref.offset)
        self.assertIsNotNone(first_ref.chunk_index)

        # refs trace back to their original packets
        packets = index.source_messages(beat.tick)
        self.assertEqual(len(packets), 2)
        self.assertTrue(all(packet.name == 'hr' for packet in packets))
        self.assertIs(index.get_message(first_ref), packets[0])
        self.assertIs(index.get_message(second_ref), packets[1])
        self.assertEqual(index.sources(beat.tick), beat.sources)

    def test_conflicting_measurements_are_kept_not_averaged(self):
        index = index_hr_stream(fitdecode.FitReader(_build_overlap_stream()))

        beat = index.get_observation(ANCHOR_SECOND * 1024 + 1600)
        self.assertTrue(beat.conflict)
        # both raw measurements survive, in first-seen order; no averaging
        self.assertEqual(beat.bpm_values, (73, 75))
        self.assertEqual(beat.filtered_bpm, 73)
        self.assertEqual(
            [beat.tick for beat in index.conflicts()],
            [ANCHOR_SECOND * 1024 + 1600])

        # the conflict is traceable to the two packets that disagree
        packets = index.source_messages(beat.tick)
        self.assertEqual(len(packets), 2)
        for ref, expected_bpm in zip(beat.sources, (73, 75)):
            packet = index.get_message(ref)
            bpm = packet.get_value('filtered_bpm', idx=0)
            if isinstance(bpm, tuple):
                bpm = bpm[ref.event_index]
            self.assertEqual(bpm, expected_bpm)

    def test_repeated_identical_value_does_not_create_conflict(self):
        index = index_hr_stream(fitdecode.FitReader(_build_overlap_stream()))
        beat = index.get_observation(ANCHOR_SECOND * 1024 + 2300)
        self.assertEqual(beat.bpm_values, (74,))
        self.assertEqual(len(beat.sources), 2)

    def test_indexing_processor(self):
        index = HrBeatIndex()
        processor = HrMessageIndexingProcessor(index)
        with fitdecode.FitReader(
                _build_overlap_stream(), processor=processor) as fit:
            self.assertEqual(len(tuple(fit)), 12)

        # same result as the stream-based indexing
        reference = index_hr_stream(
            fitdecode.FitReader(_build_overlap_stream()))
        self.assertEqual(
            [beat.tick for beat in index.observations()],
            [beat.tick for beat in reference.observations()])

        conflict = next(index.conflicts())
        self.assertEqual(conflict.bpm_values, (73, 75))
        self.assertEqual(conflict.sources[0].fit_file_index, 0)

    def test_indexing_without_data_processor(self):
        # no processor: expanded event_timestamp values are plain floats,
        # anchor values keep an integer raw_value
        index = index_hr_stream(fitdecode.FitReader(
            _build_overlap_stream(), processor=None))
        beat = index.get_observation(ANCHOR_SECOND * 1024 + 900)
        self.assertEqual(beat.tick, ANCHOR_SECOND * 1024 + 900)
        self.assertEqual(beat.filtered_bpm, 72)

    def test_chained_fit_files_get_distinct_file_indices(self):
        stream = _build_overlap_stream() + _fit_file(
            _file_id_message() +
            _anchor_hr_message(
                1, ANCHOR_SECOND * 1024 + 100, 70))

        index = index_hr_stream(fitdecode.FitReader(stream))

        # the anchor tick exists in both chained files
        beat = index.get_observation(ANCHOR_SECOND * 1024 + 100)
        self.assertEqual(len(beat.sources), 2)
        self.assertEqual(
            [ref.fit_file_index for ref in beat.sources], [0, 1])
        self.assertEqual(
            [ref.message_index for ref in beat.sources], [0, 0])

    def test_add_message_rejects_non_hr_message(self):
        index = HrBeatIndex()
        with fitdecode.FitReader(_build_overlap_stream()) as fit:
            file_id = next(
                frame for frame in fit
                if frame.frame_type == fitdecode.FIT_FRAME_DATA
                and frame.name == 'file_id')
        with self.assertRaises(TypeError):
            index.add_message(file_id)

    def test_message_ref_equality(self):
        index = index_hr_stream(fitdecode.FitReader(
            _build_overlap_stream(), keep_raw_chunks=True))
        ref = index.get_observation(
            ANCHOR_SECOND * 1024 + 100).sources[0]
        other = index.messages()[0]
        self.assertEqual(ref, other)
        self.assertEqual(hash(ref), hash(other))
        self.assertNotEqual(ref, object())
        self.assertIn('message_index=0', repr(ref))


if __name__ == '__main__':
    unittest.main()
