#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import os.path
import unittest

import fitdecode
from fitdecode import cleaning
from fitdecode.types import BASE_TYPES, Field, FieldData

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), 'files')
_UNSET = object()


def _test_file(name):
    return os.path.join(TEST_FILES_DIR, name)


def _make_speed_field(name='speed', def_num=6, scale=1000, units='m/s'):
    return Field(
        name=name, type=BASE_TYPES[0x84], def_num=def_num, scale=scale,
        units=units)


def _make_field_data(
        value, raw_value=None, *, name='speed', def_num=6, scale=1000,
        units='m/s', expansion_parent=None, expansion_parent_def_num=None,
        quantization_step=_UNSET):
    kwargs = {}
    if quantization_step is not _UNSET:
        kwargs['quantization_step'] = quantization_step
    return FieldData(
        None,
        _make_speed_field(
            name=name, def_num=def_num, scale=scale, units=units),
        None,
        value,
        value if raw_value is None else raw_value,
        expansion_parent=expansion_parent,
        expansion_parent_def_num=expansion_parent_def_num,
        **kwargs)


def _expanded_speed(value, raw_value=None, *, scale=100, unit_num=8,
                    parent_name='compressed_speed_distance',
                    quantization_step=_UNSET):
    parent = Field(
        name=parent_name, type=BASE_TYPES[0x0d], def_num=unit_num)
    return _make_field_data(
        value, raw_value, scale=scale,
        expansion_parent=parent, expansion_parent_def_num=unit_num,
        quantization_step=quantization_step)


def _first_record(path, **kwargs):
    with fitdecode.FitReader(
            path, processor=None,
            check_crc=fitdecode.CrcCheck.DISABLED, **kwargs) as fit:
        for frame in fit:
            if (isinstance(frame, fitdecode.FitDataMessage) and
                    frame.name == 'record' and
                    frame.has_field('speed')):
                return frame
    return None


class SpeedCleaningTestCase(unittest.TestCase):

    def test_direct_and_enhanced_speed_are_one_source_lineage(self):
        record = _first_record(_test_file('2015-10-13-08-43-15.fit'))

        result = record.speed_cleaning
        self.assertIsNotNone(result)
        self.assertEqual(result.observation_count, 1)
        self.assertEqual(result.independent_source_count, 1)
        self.assertEqual(len(result.sources), 2)
        self.assertTrue(result.compatible)
        self.assertEqual(len(result.values), 1)

        speed = record.get_field('speed')
        enhanced = record.get_field('enhanced_speed')
        self.assertTrue(enhanced.is_expanded)
        self.assertIs(enhanced.expansion_parent, speed.field)
        self.assertEqual(
            enhanced.expansion_parent_def_num, speed.field_def.def_num)

    def test_repeated_expansions_of_same_parent_are_one_lineage(self):
        first = _expanded_speed(3.55, 355, quantization_step=0.01)
        second = _expanded_speed(3.55, 355, quantization_step=0.01)

        result = cleaning.clean_speed([first, second])

        self.assertEqual(result.observation_count, 1)
        self.assertEqual(len(result.sources), 2)
        self.assertEqual(result.independent_source_count, 1)
        self.assertTrue(result.compatible)

    def test_conflicting_duplicates_of_same_parent_stay_one_lineage(self):
        first = _expanded_speed(3.56, 356, quantization_step=0.01)
        second = _expanded_speed(3.54, 354, quantization_step=0.01)

        result = cleaning.clean_speed([first, second])

        self.assertEqual(result.observation_count, 1)
        self.assertEqual(result.independent_source_count, 1)
        self.assertEqual(result.values, (3.56, 3.54))
        # Two quantization bins apart: they cannot come from one true value.
        self.assertFalse(result.compatible)
        self.assertIn('counted once', result.reason)

    def test_adjacent_quantization_bins_are_compatible(self):
        first = _expanded_speed(3.55, 355, quantization_step=0.01)
        second = _expanded_speed(3.54, 354, quantization_step=0.01)

        result = cleaning.clean_speed([first, second])

        self.assertEqual(result.observation_count, 1)
        self.assertTrue(result.compatible)
        self.assertAlmostEqual(result.common_range_mps[0], 3.545)
        self.assertAlmostEqual(result.common_range_mps[1], 3.545)

    def test_direct_and_expanded_duplicate_are_two_lineages(self):
        expanded = _expanded_speed(3.55, 355, quantization_step=0.01)
        direct = _make_field_data(3.546, 3546, scale=1000)

        result = cleaning.clean_speed([expanded, direct])

        self.assertEqual(result.observation_count, 1)
        self.assertEqual(result.independent_source_count, 2)
        self.assertEqual(len(result.sources), 2)
        self.assertTrue(result.compatible)
        self.assertAlmostEqual(result.common_range_mps[0], 3.5455)
        self.assertAlmostEqual(result.common_range_mps[1], 3.5465)

    def test_quantization_ranges_intersect(self):
        low_precision = _make_field_data(
            3.55, 355, scale=100, quantization_step=0.01)
        high_precision = _make_field_data(3.545, 3545, scale=1000)

        result = cleaning.clean_speed([low_precision, high_precision])

        self.assertTrue(result.compatible)
        self.assertAlmostEqual(result.common_range_mps[0], 3.545)
        self.assertAlmostEqual(result.common_range_mps[1], 3.5455)

    def test_incompatible_sources_keep_values_and_provenance(self):
        direct = _make_field_data(3.55, 3550, scale=1000)
        expanded = _expanded_speed(3.60, 3600)

        result = cleaning.clean_speed([direct, expanded])

        self.assertFalse(result.compatible)
        self.assertIsNone(result.common_range)
        self.assertIsNone(result.common_range_mps)
        self.assertEqual(result.values, (3.55, 3.6))

        sources = result.sources
        self.assertEqual(sources[0].field_name, 'speed')
        self.assertEqual(sources[0].field_num, 6)
        self.assertEqual(sources[0].raw_value, 3550)
        self.assertEqual(sources[1].expanded_from_name,
                         'compressed_speed_distance')
        self.assertEqual(sources[1].expanded_from_def_num, 8)
        self.assertIn('incompatible', result.reason)

    def test_unknown_units_source_is_ignored(self):
        good = _make_field_data(3.5, 3500)
        weird = _make_field_data(3.5, 3500, units='furlongs/week')

        result = cleaning.clean_speed([good, weird])

        self.assertEqual(len(result.sources), 1)
        self.assertEqual(result.values, (3.5,))

    def test_clean_speed_is_none_without_speed_values(self):
        self.assertIsNone(cleaning.clean_speed([]))

    def test_units_are_converted_consistently(self):
        kmh_a = _make_field_data(
            12.6, 12600, units='km/h', scale=1000)
        kmh_b = _make_field_data(
            12.6, 12600, units='km/h', scale=1000)

        result = cleaning.clean_speed([kmh_a, kmh_b])

        self.assertAlmostEqual(result.common_range[0], 12.6 - 0.0005)
        self.assertAlmostEqual(
            result.common_range_mps[0],
            (12.6 - 0.0005) * 1000.0 / 3600.0)
        self.assertEqual(result.units, 'km/h')

    def test_reader_clean_speed_option_matches_lazy_accessor(self):
        path = _test_file('null_compressed_speed_dist.fit')
        eager = _first_record(path, clean_speed=True)

        lazy = _first_record(path)

        self.assertEqual(
            eager.cleaning.as_dict(), lazy.speed_cleaning.as_dict())
        self.assertIs(lazy.speed_cleaning, lazy.speed_cleaning)
        self.assertGreaterEqual(lazy.speed_cleaning.observation_count, 1)

    def test_compressed_speed_distance_step_comes_from_component_scale(self):
        record = _first_record(_test_file('compressed-speed-distance.fit'))

        expanded = next(iter(record.get_fields('speed')))
        self.assertTrue(expanded.is_expanded)
        self.assertEqual(expanded.expansion_parent_def_num, 8)
        self.assertAlmostEqual(expanded.quantization_step, 0.01)

    def test_direct_speed_step_comes_from_field_scale(self):
        record = _first_record(_test_file('null_compressed_speed_dist.fit'))

        direct = next(
            field for field in record.get_fields('speed')
            if not field.is_expanded)
        self.assertAlmostEqual(direct.quantization_step, 0.001)


if __name__ == '__main__':
    unittest.main()
