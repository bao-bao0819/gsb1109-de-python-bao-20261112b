# Copyright 2019 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for mrjob.framing"""
from io import BytesIO

from tests.sandbox import BasicTestCase

from mrjob.framing import AMBIGUOUS
from mrjob.framing import CONFIRMED
from mrjob.framing import Calibration
from mrjob.framing import CorruptFrameError
from mrjob.framing import FRAME_HEADER_SIZE
from mrjob.framing import FrameDecoder
from mrjob.framing import IncompleteFrameError
from mrjob.framing import InvalidCalibrationError
from mrjob.framing import KEY_VALUE
from mrjob.framing import LegacyCandidate
from mrjob.framing import PartitionSpec
from mrjob.framing import VALUE_ONLY
from mrjob.framing import decode_frame
from mrjob.framing import decode_legacy_record
from mrjob.framing import encode_frame
from mrjob.framing import encode_legacy_record
from mrjob.framing import frame_length
from mrjob.framing import iter_frames
from mrjob.framing import migrate_partition
from mrjob.framing import migrate_partition_stream
from mrjob.framing import migrate_partitions
from mrjob.framing import normalize_legacy_protocol
from mrjob.framing import read_frames
from mrjob.framing import split_frames
from mrjob.framing import split_legacy_records


FRAME_CASES = [
    (None, None),
    (None, b''),
    (b'', None),
    (b'', b''),
    (b'key', b'value'),
    (b'key', b''),
    (b'key\twith\ttabs', b'value\twith\ttabs'),
    (b'\x00key', b'value\x00with\x00nuls'),
    (b'line\nbreaks', b'\r\n\r\n'),
    (b'', b'\t\x00\n'),
]


class FrameTestCase(BasicTestCase):

    def test_round_trip(self):
        for key, value in FRAME_CASES:
            data = encode_frame(key, value)
            frame, next_offset = decode_frame(data)

            self.assertEqual(frame.key, key)
            self.assertEqual(frame.value, value)
            self.assertEqual(frame.raw, data)
            self.assertEqual(next_offset, len(data))

    def test_absent_and_empty_are_distinct(self):
        self.assertNotEqual(
            encode_frame(None, b''), encode_frame(b'', b''))
        self.assertNotEqual(
            encode_frame(b'', None), encode_frame(b'', b''))

        self.assertEqual(decode_frame(encode_frame(None, b''))[0].key, None)
        self.assertEqual(decode_frame(encode_frame(b'', b''))[0].key, b'')

    def test_unicode_is_utf_8_encoded(self):
        frame, _ = decode_frame(encode_frame(u'caf\xe9', u'ol\xe9'))

        self.assertEqual((frame.key, frame.value),
                         (b'caf\xc3\xa9', b'ol\xc3\xa9'))

    def test_frame_length(self):
        for key, value in FRAME_CASES:
            self.assertEqual(frame_length(key, value),
                             len(encode_frame(key, value)))

    def test_offset(self):
        first = encode_frame(b'a', b'1')
        second = encode_frame(b'b', b'2')
        data = first + second

        frame, next_offset = decode_frame(data, len(first))

        self.assertEqual((frame.key, frame.value), (b'b', b'2'))
        self.assertEqual(next_offset, len(data))

    def test_bad_type(self):
        self.assertRaises(TypeError, encode_frame, 1, b'value')
        self.assertRaises(TypeError, encode_frame, b'key', 1)

    def test_split_frames(self):
        frames = [encode_frame(b'a', b'1'), encode_frame(b'b', b'2')]
        decoded, trailing = split_frames(b''.join(frames))

        self.assertEqual(len(decoded), 2)
        self.assertEqual(trailing, b'')

    def test_split_frames_keeps_truncated_tail_separate(self):
        complete = encode_frame(b'a', b'1')
        truncated = encode_frame(b'b', b'2')[:-1]

        decoded, trailing = split_frames(complete + truncated)

        self.assertEqual(len(decoded), 1)
        self.assertEqual((decoded[0].key, decoded[0].value), (b'a', b'1'))
        self.assertEqual(trailing, truncated)

    def test_iter_frames_does_not_swallow_complete_frame(self):
        complete = encode_frame(b'a', b'1')
        truncated = encode_frame(b'b', b'2')[:FRAME_HEADER_SIZE - 1]

        frames = list(iter_frames(complete + truncated))

        self.assertEqual(len(frames), 1)
        self.assertEqual((frames[0].key, frames[0].value), (b'a', b'1'))

    def test_incomplete_header(self):
        self.assertRaises(
            IncompleteFrameError, decode_frame, b'MRF\x01')

    def test_incomplete_payload(self):
        data = encode_frame(b'key', b'value')

        try:
            decode_frame(data[:-1])
        except IncompleteFrameError as error:
            self.assertEqual(error.needed, len(data))
            self.assertEqual(error.offset, 0)
            self.assertEqual(error.data, data[:-1])
        else:
            self.fail('expected IncompleteFrameError')

    def test_bad_magic(self):
        data = b'XXX' + encode_frame(b'key', b'value')[3:]
        self.assertRaises(CorruptFrameError, decode_frame, data)

    def test_bad_version(self):
        data = bytearray(encode_frame(b'key', b'value'))
        data[3] = 99
        self.assertRaises(CorruptFrameError, decode_frame, bytes(data))

    def test_bad_flags(self):
        data = bytearray(encode_frame(b'key', b'value'))
        data[4] = 0x80
        self.assertRaises(CorruptFrameError, decode_frame, bytes(data))

    def test_bad_checksum(self):
        data = bytearray(encode_frame(b'key', b'value'))
        data[-1] ^= 0xff
        self.assertRaises(CorruptFrameError, decode_frame, bytes(data))

    def test_absent_field_with_length(self):
        data = bytearray(encode_frame(None, b'value'))
        # key length is bytes 5-8
        data[5:9] = b'\x00\x00\x00\x01'
        self.assertRaises(CorruptFrameError, decode_frame, bytes(data))

    def test_max_frame_size(self):
        data = encode_frame(b'key', b'value')
        self.assertRaises(
            CorruptFrameError, decode_frame, data, max_frame_size=10)


class FrameDecoderTestCase(BasicTestCase):

    def test_feed_one_byte_at_a_time(self):
        data = encode_frame(b'a', b'1') + encode_frame(b'b', b'2')
        decoder = FrameDecoder()

        frames = []
        for i in range(len(data)):
            frames.extend(decoder.feed(data[i:i + 1]))

        self.assertEqual(
            [(f.key, f.value) for f in frames],
            [(b'a', b'1'), (b'b', b'2')])
        self.assertEqual(decoder.buffered_bytes, 0)
        self.assertEqual(decoder.finish(), b'')

    def test_truncated_tail(self):
        complete = encode_frame(b'a', b'1')
        truncated = encode_frame(b'b', b'2')[:-2]

        decoder = FrameDecoder()
        frames = decoder.feed(complete + truncated)

        self.assertEqual(len(frames), 1)
        self.assertEqual((frames[0].key, frames[0].value), (b'a', b'1'))
        self.assertEqual(decoder.buffered_bytes, len(truncated))
        self.assertEqual(decoder.finish(), truncated)

    def test_finish_strict(self):
        decoder = FrameDecoder()
        decoder.feed(encode_frame(b'a', b'1')[:-1])

        self.assertRaises(IncompleteFrameError, decoder.finish, strict=True)

    def test_complete_frame_is_not_delayed(self):
        decoder = FrameDecoder()
        frames = decoder.feed(encode_frame(b'a', b'1'))

        self.assertEqual(len(frames), 1)
        self.assertEqual(decoder.buffered_bytes, 0)


class LegacyRecordTestCase(BasicTestCase):

    def test_key_value_round_trip(self):
        cases = [
            (b'key', b'value'),
            (b'key', b''),
            (b'key', b'value\twith\ttabs'),
            (b'', b'value'),
            (b'', b''),
            (b'key', b'\x00value'),
        ]

        for key, value in cases:
            raw = encode_legacy_record(KEY_VALUE, key, value)
            self.assertEqual(decode_legacy_record(raw, KEY_VALUE),
                             (key, value))

    def test_key_value_no_tab_has_no_value(self):
        self.assertEqual(decode_legacy_record(b'key', KEY_VALUE),
                         (b'key', None))
        self.assertEqual(encode_legacy_record(KEY_VALUE, b'key', None),
                         b'key')

    def test_value_only_round_trip(self):
        for value in [b'', b'value', b'value\twith\ttabs', b'\x00\n']:
            raw = encode_legacy_record(VALUE_ONLY, None, value)
            self.assertEqual(decode_legacy_record(raw, VALUE_ONLY),
                             (None, value))

    def test_value_only_discards_key(self):
        self.assertEqual(
            encode_legacy_record(VALUE_ONLY, b'ignored', b'value'), b'value')

    def test_value_only_requires_value(self):
        self.assertRaises(
            ValueError, encode_legacy_record, VALUE_ONLY, None, None)

    def test_normalize_aliases(self):
        self.assertEqual(normalize_legacy_protocol('kv'), KEY_VALUE)
        self.assertEqual(normalize_legacy_protocol('key-value'), KEY_VALUE)
        self.assertEqual(normalize_legacy_protocol('raw'), VALUE_ONLY)
        self.assertEqual(normalize_legacy_protocol(b'value'), VALUE_ONLY)
        self.assertRaises(ValueError, normalize_legacy_protocol, 'json')

    def test_split_legacy_records(self):
        self.assertEqual(
            split_legacy_records(b'a\nb\n'), ([b'a', b'b'], b''))
        self.assertEqual(
            split_legacy_records(b'a\nb'), ([b'a'], b'b'))
        self.assertEqual(split_legacy_records(b''), ([], b''))
        self.assertEqual(
            split_legacy_records(b'a\n'), ([b'a'], b''))
        self.assertEqual(
            split_legacy_records(b'a\n\nb\n'), ([b'a', b'', b'b'], b''))


class PartitionSpecTestCase(BasicTestCase):

    def test_default(self):
        spec = PartitionSpec()

        self.assertEqual(spec.protocols, (KEY_VALUE,))
        self.assertEqual(spec.resolved_protocol, KEY_VALUE)

    def test_unknown_protocol(self):
        self.assertRaises(ValueError, PartitionSpec, protocols=('json',))

    def test_empty_protocols(self):
        self.assertRaises(ValueError, PartitionSpec, protocols=())

    def test_known_keys_ignore_none_and_empty(self):
        spec = PartitionSpec(known_keys=[None, b'', b'user'])

        self.assertEqual(spec.known_keys, frozenset([b'user']))

    def test_calibration_resolves_key_value(self):
        spec = PartitionSpec(
            protocols=(KEY_VALUE, VALUE_ONLY),
            calibrations=[Calibration(b'user\talice', b'user', b'alice')])

        self.assertEqual(spec.resolved_protocol, KEY_VALUE)

    def test_calibration_resolves_value_only(self):
        spec = PartitionSpec(
            protocols=(KEY_VALUE, VALUE_ONLY),
            calibrations=[Calibration(b'user\talice', None, b'user\talice')])

        self.assertEqual(spec.resolved_protocol, VALUE_ONLY)

    def test_tab_calibration_resolves_key_value_as_empty_key(self):
        spec = PartitionSpec(
            protocols=(KEY_VALUE, VALUE_ONLY),
            calibrations=[Calibration(b'\tvalue', b'', b'value')])

        self.assertEqual(spec.resolved_protocol, KEY_VALUE)

    def test_calibration_tuple_form(self):
        spec = PartitionSpec(
            protocols=(KEY_VALUE, VALUE_ONLY),
            calibrations=[(b'k\tv', (b'k', b'v'))])

        self.assertEqual(spec.resolved_protocol, KEY_VALUE)

    def test_inconsistent_calibration(self):
        self.assertRaises(
            InvalidCalibrationError, PartitionSpec,
            protocols=(KEY_VALUE, VALUE_ONLY),
            calibrations=[Calibration(b'a\tb', b'x', b'y')])

    def test_contradictory_calibrations(self):
        self.assertRaises(
            InvalidCalibrationError, PartitionSpec,
            protocols=(KEY_VALUE, VALUE_ONLY),
            calibrations=[Calibration(b'a\tb', b'a', b'b'),
                          Calibration(b'c\td', None, b'c\td')])


class MigratePartitionTestCase(BasicTestCase):

    def test_single_key_value_protocol(self):
        records = [b'user\talice', b'user\tbob\tadmin', b'\tvalue']

        result = migrate_partition(records, PartitionSpec(KEY_VALUE))

        self.assertEqual([i.status for i in result.items],
                         [CONFIRMED, CONFIRMED, CONFIRMED])
        self.assertEqual(
            [(i.key, i.value) for i in result.items],
            [(b'user', b'alice'), (b'user', b'bob\tadmin'), (b'', b'value')])
        self.assertEqual(result.restore_confirmed(), tuple(records))
        self.assertTrue(result.verify_restorable())

    def test_single_value_only_protocol(self):
        records = [b'alice', b'bob\tadmin', b'\x00\n']

        result = migrate_partition(records, PartitionSpec(VALUE_ONLY))

        self.assertEqual(
            [(i.key, i.value) for i in result.items],
            [(None, b'alice'), (None, b'bob\tadmin'), (None, b'\x00\n')])
        self.assertEqual(result.restore_confirmed(), tuple(records))

    def test_ambiguous_protocols(self):
        records = [b'user\talice', b'\tvalue', b'plain']

        result = migrate_partition(
            records,
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY)))

        self.assertEqual([i.status for i in result.items],
                         [AMBIGUOUS, AMBIGUOUS, AMBIGUOUS])
        self.assertEqual(result.ambiguous_records, tuple(records))
        self.assertEqual(result.confirmed, ())

        for item in result.items:
            self.assertEqual(
                item.candidates,
                (LegacyCandidate(KEY_VALUE, *decode_legacy_record(
                    item.raw, KEY_VALUE)),
                 LegacyCandidate(VALUE_ONLY, *decode_legacy_record(
                     item.raw, VALUE_ONLY))))

    def test_empty_key_vs_no_key_reason(self):
        result = migrate_partition(
            [b'\tvalue'],
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY)))

        item = result.items[0]

        self.assertEqual(item.status, AMBIGUOUS)
        self.assertEqual(item.reason, 'empty key or no key')
        self.assertEqual(
            [c.pair for c in item.candidates],
            [(b'', b'value'), (None, b'\tvalue')])

    def test_known_key_confirms_key_value(self):
        result = migrate_partition(
            [b'user\talice', b'other\tthing'],
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY),
                          known_keys=[b'user']))

        self.assertEqual(result.items[0].status, CONFIRMED)
        self.assertEqual(result.items[0].protocol, KEY_VALUE)
        self.assertEqual(result.items[0].pair, (b'user', b'alice'))

        self.assertEqual(result.items[1].status, AMBIGUOUS)
        self.assertEqual(result.ambiguous_records, (b'other\tthing',))

    def test_known_key_must_match_whole_key(self):
        result = migrate_partition(
            [b'username\talice'],
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY),
                          known_keys=[b'user']))

        self.assertEqual(result.items[0].status, AMBIGUOUS)

    def test_calibration_pins_protocol_for_all_records(self):
        records = [b'user\talice', b'\tvalue']

        result = migrate_partition(
            records,
            PartitionSpec(
                protocols=(KEY_VALUE, VALUE_ONLY),
                calibrations=[Calibration(
                    b'user\talice', None, b'user\talice')]))

        self.assertEqual([i.status for i in result.items],
                         [CONFIRMED, CONFIRMED])
        self.assertEqual(
            [(i.key, i.value) for i in result.items],
            [(None, b'user\talice'), (None, b'\tvalue')])
        self.assertTrue(result.verify_restorable())

    def test_calibration_record_is_confirmed(self):
        calibration = Calibration(b'\tvalue', b'', b'value')

        result = migrate_partition(
            [b'\tvalue'],
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY),
                          calibrations=[calibration]))

        self.assertEqual(result.items[0].status, CONFIRMED)
        self.assertEqual(result.items[0].pair, calibration.pair)

    def test_ambiguous_item_cannot_be_framed_or_restored(self):
        result = migrate_partition(
            [b'\tvalue'],
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY)))
        item = result.items[0]

        self.assertRaises(ValueError, item.encode_frame)
        self.assertRaises(ValueError, item.restore)

    def test_frames_can_be_decoded_back_to_pairs(self):
        records = [b'key\tvalue\twith\ttab', b'\x00key\t\x00value']

        result = migrate_partition(records, PartitionSpec(KEY_VALUE))

        frames, trailing = split_frames(result.frames())

        self.assertEqual(trailing, b'')
        self.assertEqual(
            [(f.key, f.value) for f in frames],
            [(b'key', b'value\twith\ttab'), (b'\x00key', b'\x00value')])

    def test_summary(self):
        result = migrate_partition(
            [b'user\talice', b'\tvalue'],
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY),
                          known_keys=[b'user']))

        self.assertEqual(
            result.summary(),
            {'total': 2, 'confirmed': 1, 'ambiguous': 1,
             'resolved_protocol': None})

    def test_bad_spec_type(self):
        self.assertRaises(TypeError, migrate_partition, [b'a'], 'key_value')

    def test_empty_partition(self):
        result = migrate_partition([])

        self.assertEqual(result.items, ())
        self.assertEqual(result.frames(), b'')
        self.assertEqual(result.restore_confirmed(), ())


class MigratePartitionStreamTestCase(BasicTestCase):

    def test_complete_records(self):
        result, trailing = migrate_partition_stream(
            b'user\talice\nuser\tbob\n', PartitionSpec(KEY_VALUE))

        self.assertEqual(trailing, b'')
        self.assertEqual(
            result.restore_confirmed(), (b'user\talice', b'user\tbob'))

    def test_half_record_is_not_merged_or_migrated(self):
        result, trailing = migrate_partition_stream(
            b'user\talice\nuser\tbo', PartitionSpec(KEY_VALUE))

        self.assertEqual(trailing, b'user\tbo')
        self.assertEqual(result.restore_confirmed(), (b'user\talice',))

    def test_ambiguous_records_are_preserved(self):
        result, trailing = migrate_partition_stream(
            b'\tvalue\nplain\n',
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY)))

        self.assertEqual(trailing, b'')
        self.assertEqual(result.ambiguous_records, (b'\tvalue', b'plain'))
        self.assertEqual(result.confirmed, ())


class CalibrationTestCase(BasicTestCase):

    def test_equality_and_repr(self):
        a = Calibration(b'k\tv', b'k', b'v')
        b = Calibration(b'k\tv', b'k', b'v')
        c = Calibration(b'k\tv', None, b'k\tv')

        self.assertEqual(a, b)
        self.assertNotEqual(a, c)
        self.assertIn('Calibration', repr(a))

    def test_normalizes_strings_to_bytes(self):
        calibration = Calibration(u'k\tv', u'k', u'v')

        self.assertEqual(calibration.raw, b'k\tv')
        self.assertEqual(calibration.key, b'k')
        self.assertEqual(calibration.value, b'v')


class RestoreAllTestCase(BasicTestCase):

    def test_restore_all_includes_ambiguous_bytes(self):
        records = [b'user\talice', b'\tvalue']

        result = migrate_partition(
            records,
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY),
                          known_keys=[b'user']))

        self.assertEqual(result.restore_all(), tuple(records))
        self.assertEqual(result.restore_confirmed(), (b'user\talice',))


class ReadFramesTestCase(BasicTestCase):

    def test_read_frames(self):
        data = encode_frame(b'a', b'1') + encode_frame(b'b', b'2')

        frames = list(read_frames(BytesIO(data), chunk_size=3))

        self.assertEqual(
            [(f.key, f.value) for f in frames],
            [(b'a', b'1'), (b'b', b'2')])

    def test_read_frames_ignores_partial_tail(self):
        complete = encode_frame(b'a', b'1')
        truncated = encode_frame(b'b', b'2')[:-3]

        frames = list(read_frames(BytesIO(complete + truncated), chunk_size=5))

        self.assertEqual(len(frames), 1)
        self.assertEqual((frames[0].key, frames[0].value), (b'a', b'1'))

    def test_read_frames_strict(self):
        truncated = encode_frame(b'b', b'2')[:-3]

        self.assertRaises(
            IncompleteFrameError,
            list, read_frames(BytesIO(truncated), strict=True))

    def test_read_frames_bad_chunk_size(self):
        self.assertRaises(
            ValueError, list, read_frames(BytesIO(b''), chunk_size=0))


class DecoderOffsetTestCase(BasicTestCase):

    def test_error_offset_is_absolute(self):
        decoder = FrameDecoder()
        decoder.feed(encode_frame(b'a', b'1'))

        bad = bytearray(encode_frame(b'b', b'2'))
        bad[-1] ^= 0xff

        try:
            decoder.feed(bytes(bad))
        except CorruptFrameError as error:
            self.assertEqual(
                error.offset, len(encode_frame(b'a', b'1')))
        else:
            self.fail('expected CorruptFrameError')


class MigratePartitionsTestCase(BasicTestCase):

    def test_partitions_have_independent_protocols(self):
        partitions = [
            (PartitionSpec(KEY_VALUE), b'user\talice\n'),
            (PartitionSpec(VALUE_ONLY), b'user\tabc\n'),
        ]

        results = migrate_partitions(partitions)

        first, first_trailing = results[0]
        second, second_trailing = results[1]

        self.assertEqual(first_trailing, b'')
        self.assertEqual(second_trailing, b'')

        self.assertEqual(
            first.items[0].pair, (b'user', b'alice'))
        self.assertEqual(
            second.items[0].pair, (None, b'user\tabc'))

    def test_bad_partition(self):
        self.assertRaises(TypeError, migrate_partitions, [b'not a pair'])


class ContradictoryCalibrationTestCase(BasicTestCase):

    def test_same_raw_different_pair(self):
        self.assertRaises(
            InvalidCalibrationError, PartitionSpec,
            protocols=(KEY_VALUE, VALUE_ONLY),
            calibrations=[Calibration(b'a\tb', b'a', b'b'),
                          Calibration(b'a\tb', None, b'a\tb')])


class FinalRecordCompleteTestCase(BasicTestCase):

    def test_final_record_complete(self):
        result, trailing = migrate_partition_stream(
            b'user\talice\nuser\tbob',
            PartitionSpec(KEY_VALUE),
            final_record_complete=True)

        self.assertEqual(trailing, b'')
        self.assertEqual(result.restore_confirmed(),
                         (b'user\talice', b'user\tbob'))

    def test_final_record_complete_does_not_apply_when_terminated(self):
        result, trailing = migrate_partition_stream(
            b'user\talice\n', PartitionSpec(KEY_VALUE),
            final_record_complete=True)

        self.assertEqual(trailing, b'')
        self.assertEqual(result.restore_confirmed(), (b'user\talice',))


class AmbiguousContentsTestCase(BasicTestCase):

    def test_ambiguous_contents(self):
        result = migrate_partition(
            [b'\tvalue'],
            PartitionSpec(protocols=(KEY_VALUE, VALUE_ONLY)))

        contents = result.ambiguous_contents()

        self.assertEqual(len(contents), 1)
        raw, candidates, reason = contents[0]

        self.assertEqual(raw, b'\tvalue')
        self.assertEqual(reason, 'empty key or no key')
        self.assertEqual(
            [c.pair for c in candidates],
            [(b'', b'value'), (None, b'\tvalue')])
