# Copyright 2020 Yelp and Contributors
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

"""Tests for the cross-language canonical key protocol."""
from tests.sandbox import BasicTestCase

from mrjob.canonical import CANONICAL_KEY_PROTOCOL
from mrjob.canonical import CanonicalKeyError
from mrjob.canonical import DuplicateMemberError
from mrjob.canonical import Quarantine
from mrjob.canonical import SourcePrecisionError
from mrjob.canonical import canonical_shuffle_key
from mrjob.canonical import canonicalize
from mrjob.canonical import canonicalize_json
from mrjob.canonical import decanonicalize
from mrjob.canonical import legacy_value
from mrjob.canonical import loads_checked
from mrjob.protocol import CanonicalJSONProtocol
from mrjob.protocol import CanonicalJSONValueProtocol


class CanonicalizeTestCase(BasicTestCase):

    def test_integer_and_float_with_same_value_merge(self):
        self.assertEqual(canonicalize(1), canonicalize(1.0))
        self.assertEqual(canonicalize(1.0), b'1')
        self.assertEqual(canonicalize(1), b'1')

    def test_fractional_floats_keep_value(self):
        self.assertEqual(canonicalize(1.5), b'1.5')
        self.assertEqual(canonicalize(0.1), b'0.1')

    def test_booleans_are_their_own_category(self):
        self.assertNotEqual(canonicalize(True), canonicalize(1))
        self.assertNotEqual(canonicalize(False), canonicalize(0))
        self.assertEqual(canonicalize(True), b'true')
        self.assertEqual(canonicalize(False), b'false')

    def test_none_strings_and_numbers_stay_distinct(self):
        encodings = [canonicalize(None), canonicalize(0),
                     canonicalize(''), canonicalize(False),
                     canonicalize([]), canonicalize({})]
        self.assertEqual(len(set(encodings)), len(encodings))

    def test_object_member_order_is_ignored(self):
        left = canonicalize({'a': 1, 'b': {'x': 2, 'y': 3}})
        right = canonicalize({'b': {'y': 3, 'x': 2}, 'a': 1})
        self.assertEqual(left, right)
        self.assertEqual(left, b'{"a":1,"b":{"x":2,"y":3}}')

    def test_missing_fields_do_not_matter(self):
        full = canonicalize({'a': 1, 'b': 2, 'c': 3})
        sparse = canonicalize({'a': 1, 'c': 3})
        self.assertNotEqual(full, sparse)

        same_shape_a = canonicalize({'a': 1, 'b': None})
        same_shape_b = canonicalize({'b': None, 'a': 1})
        self.assertEqual(same_shape_a, same_shape_b)

    def test_array_order_is_significant(self):
        self.assertNotEqual(canonicalize([1, 2, 3]),
                            canonicalize([3, 2, 1]))
        self.assertEqual(canonicalize([1, [2, 3], {}]),
                         canonicalize([1, [2, 3], {}]))

    def test_nested_structures(self):
        key = {'user': {'id': 7}, 'tags': ['a', 'b'], 'ok': True}
        self.assertEqual(
            canonicalize(key),
            b'{"ok":true,"tags":["a","b"],"user":{"id":7}}')

    def test_tuples_encode_as_arrays(self):
        self.assertEqual(canonicalize((1, 2)), canonicalize([1, 2]))

    def test_unicode_strings(self):
        self.assertEqual(canonicalize(u'Qu\xe9bec'),
                         u'"Qu\xe9bec"'.encode('utf_8'))
        # quotes and control characters are JSON-escaped
        self.assertEqual(canonicalize('a"b\nc'), b'"a\\"b\\nc"')

    def test_members_sort_by_code_point(self):
        self.assertEqual(canonicalize({u'\xe9': 1, 'a': 2}),
                         b'{"a":2,"\xc3\xa9":1}')

    def test_bytes_and_sets_are_rejected(self):
        self.assertRaises(CanonicalKeyError, canonicalize, b'foo')
        self.assertRaises(CanonicalKeyError, canonicalize, {1, 2})
        self.assertRaises(CanonicalKeyError, canonicalize, object())

    def test_nan_and_infinity_are_rejected(self):
        self.assertRaises(CanonicalKeyError, canonicalize, float('nan'))
        self.assertRaises(CanonicalKeyError, canonicalize, float('inf'))

    def test_deterministic_across_calls(self):
        key = {'z': [1, 2], 'a': {'y': True, 'b': None}}
        self.assertEqual(canonicalize(key), canonicalize(dict(key)))


class CanonicalizeJSONTestCase(BasicTestCase):

    def test_raw_json_member_order_normalized(self):
        self.assertEqual(
            canonicalize_json(b'{"b":1,"a":2}'),
            canonicalize({'a': 2, 'b': 1}))

    def test_raw_number_spellings_merge(self):
        self.assertEqual(canonicalize_json(b'1'),
                         canonicalize_json(b'1.0'))
        self.assertEqual(canonicalize_json(b'100'),
                         canonicalize_json(b'1e2'))

    def test_duplicate_member_is_rejected(self):
        with self.assertRaises(DuplicateMemberError) as ctx:
            canonicalize_json(b'{"a":1,"a":2}')

        self.assertEqual(ctx.exception.member, 'a')
        self.assertEqual(ctx.exception.path, ())

    def test_duplicate_nested_member_reports_path(self):
        with self.assertRaises(DuplicateMemberError) as ctx:
            canonicalize_json(b'{"outer":[{"k":1},{"k":2,"k":3}]}')

        self.assertEqual(ctx.exception.member, 'k')
        self.assertEqual(ctx.exception.path, ('outer', 1))

    def test_over_precise_float_is_rejected(self):
        with self.assertRaises(SourcePrecisionError) as ctx:
            canonicalize_json(b'0.1234567890123456789012345678901')

        self.assertEqual(
            ctx.exception.raw, '0.1234567890123456789012345678901')

    def test_distinct_sources_that_would_round_to_same_double(self):
        # 1.0000000000000001 rounds to 1.0, so its source precision is
        # lost and it must not be grouped silently
        with self.assertRaises(SourcePrecisionError):
            canonicalize_json(b'1.0000000000000001')

        # ...002 parses to its own distinct double, so it is portable
        # and stays in its own group rather than merging with 1
        distinct = canonicalize_json(b'1.0000000000000002')
        self.assertNotEqual(distinct, canonicalize(1))

    def test_equivalent_number_spellings_still_merge(self):
        # different text, exact same double value -> same group
        self.assertEqual(canonicalize_json(b'100'),
                         canonicalize_json(b'1e2'))
        self.assertEqual(canonicalize_json(b'0.5'),
                         canonicalize_json(b'5e-1'))

    def test_safe_doubles_pass(self):
        self.assertEqual(canonicalize_json(b'3.14'),
                         canonicalize(3.14))

    def test_nan_constant_is_rejected(self):
        self.assertRaises(CanonicalKeyError,
                          canonicalize_json, b'NaN')
        self.assertRaises(CanonicalKeyError,
                          canonicalize_json, b'-Infinity')

    def test_integer_beyond_double_range_is_rejected(self):
        with self.assertRaises(SourcePrecisionError):
            canonicalize_json(b'9007199254740993')


class DecanonicalizeTestCase(BasicTestCase):

    def test_round_trip(self):
        for value in (None, True, 1, 1.5, 'foo', [1, {'a': 2}]):
            self.assertEqual(decanonicalize(canonicalize(value)),
                             decanonicalize(canonicalize(value)))

    def test_whole_float_comes_back_as_integer(self):
        # normalization is intentionally one-way on number type
        self.assertEqual(decanonicalize(canonicalize(1.0)), 1)
        self.assertIsInstance(decanonicalize(canonicalize(1.0)), int)


class LegacyValueTestCase(BasicTestCase):

    def test_duplicate_members_collapse_last_wins(self):
        value = loads_checked(b'{"a":1,"a":2}')
        self.assertEqual(legacy_value(value), {'a': 2})

    def test_checked_float_becomes_plain_float(self):
        value = loads_checked(b'{"x":1.5}')
        legacy = legacy_value(value)
        self.assertEqual(legacy, {'x': 1.5})
        self.assertIsInstance(legacy['x'], float)

    def test_nested_legacy_conversion(self):
        value = loads_checked(b'{"o":{"a":1,"a":2},"xs":[1.0,2.5]}')
        self.assertEqual(
            legacy_value(value),
            {'o': {'a': 2}, 'xs': [1.0, 2.5]})


class QuarantineTestCase(BasicTestCase):

    def test_bad_key_is_routed_to_quarantine(self):
        quarantine = Quarantine()

        raw_key = canonical_shuffle_key(
            loads_checked(b'{"a":1,"a":2}'),
            quarantine=quarantine,
            partition='p07',
            record_id='r42')

        self.assertIsNone(raw_key)
        self.assertEqual(len(quarantine), 1)

        entry = quarantine.entries()[0]
        self.assertEqual(entry['partition'], 'p07')
        self.assertEqual(entry['record_id'], 'r42')
        self.assertEqual(entry['reason'],
                         Quarantine.DUPLICATE_MEMBER)

    def test_precision_error_names_partition_to_reprocess(self):
        quarantine = Quarantine()

        canonical_shuffle_key(
            loads_checked(b'0.1234567890123456789012345678901'),
            quarantine=quarantine, partition='p09', record_id='r1')
        canonical_shuffle_key(
            loads_checked(b'{"a":1,"a":2}'),
            quarantine=quarantine, partition='p07', record_id='r2')
        canonical_shuffle_key(
            loads_checked(b'{"a":1,"a":2}'),
            quarantine=quarantine, partition='p09', record_id='r3')

        self.assertEqual(quarantine.partitions(), ['p07', 'p09'])
        self.assertEqual(
            [entry['reason'] for entry in quarantine],
            [Quarantine.SOURCE_PRECISION,
             Quarantine.DUPLICATE_MEMBER,
             Quarantine.DUPLICATE_MEMBER])

    def test_without_quarantine_error_propagates(self):
        self.assertRaises(
            DuplicateMemberError,
            canonical_shuffle_key,
            loads_checked(b'{"a":1,"a":2}'))

    def test_good_key_returns_bytes_and_leaves_quarantine_empty(self):
        quarantine = Quarantine()
        raw_key = canonical_shuffle_key(
            {'a': 1}, quarantine=quarantine,
            partition='p07', record_id='r4')

        self.assertEqual(raw_key, b'{"a":1}')
        self.assertEqual(len(quarantine), 0)

    def test_unencodable_key_gets_its_own_reason(self):
        quarantine = Quarantine()

        raw_key = canonical_shuffle_key(
            {1, 2}, quarantine=quarantine,
            partition='p03', record_id='r9')

        self.assertIsNone(raw_key)
        entry = list(quarantine)[0]
        self.assertEqual(entry['reason'],
                         Quarantine.UNENCODABLE_KEY)
        self.assertEqual(quarantine.partitions(), ['p03'])


class CanonicalJSONProtocolTestCase(BasicTestCase):

    def setUp(self):
        super(CanonicalJSONProtocolTestCase, self).setUp()
        self.protocol = CanonicalJSONProtocol()

    def test_round_trip(self):
        for key, value in (
                (1, 'v'),
                ({'a': 1, 'b': [True, None]}, [1, 2]),
                ('k\xe9', {'x': 1.5}),
                ([3, 2, 1], None)):
            self.assertEqual(
                self.protocol.read(self.protocol.write(key, value)),
                (decanonicalize(canonicalize(key)), value))

    def test_semantically_equal_keys_share_bytes(self):
        # simulate a Python mapper and a mapper that emits 1.0 /
        # reordered members
        py_line = self.protocol.write({'a': 1, 'b': 2}, 'v')
        other_line = (
            canonicalize({'b': 2, 'a': 1.0}) + b'\t' + b'"v"')

        self.assertEqual(
            py_line.split(b'\t', 1)[0],
            other_line.split(b'\t', 1)[0])

    def test_array_order_stays_separate(self):
        self.assertNotEqual(
            self.protocol.write([1, 2], None),
            self.protocol.write([2, 1], None))

    def test_boolean_and_number_stay_separate(self):
        self.assertNotEqual(
            self.protocol.write(True, None),
            self.protocol.write(1, None))

    def test_bad_key_raises_at_write_time(self):
        self.assertRaises(
            DuplicateMemberError,
            self.protocol.write,
            loads_checked(b'{"a":1,"a":2}'), None)

    def test_key_cache_does_not_cross_contaminate(self):
        line_a = self.protocol.write({'a': 1}, 'x')
        line_b = self.protocol.write({'a': 1}, 'y')
        line_c = self.protocol.write({'a': 2}, 'z')

        self.assertEqual(self.protocol.read(line_a), ({'a': 1}, 'x'))
        self.assertEqual(self.protocol.read(line_b), ({'a': 1}, 'y'))
        self.assertEqual(self.protocol.read(line_c), ({'a': 2}, 'z'))


class CanonicalJSONValueProtocolTestCase(BasicTestCase):

    def setUp(self):
        super(CanonicalJSONValueProtocolTestCase, self).setUp()
        self.protocol = CanonicalJSONValueProtocol()

    def test_round_trip(self):
        self.assertEqual(
            self.protocol.read(self.protocol.write(None, {'a': 1})),
            (None, {'a': 1}))

    def test_canonicalizes_value(self):
        self.assertEqual(
            self.protocol.write(None, {'b': 1, 'a': 2}),
            b'{"a":2,"b":1}')


class ProtocolVersionTestCase(BasicTestCase):

    def test_versioned_wire_name(self):
        # bumping the normalization rules must bump the wire name
        self.assertEqual(CANONICAL_KEY_PROTOCOL, 'canonical-json/1')
