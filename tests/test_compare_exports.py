#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import os.path
import sys
import unittest

sys.path.insert(0, os.path.join(
    os.path.dirname(__file__), os.path.pardir, 'tools'))

import fitdecode  # noqa: E402

import compare_exports as ce  # noqa: E402

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), 'files')
BIKE_FIT = os.path.join(TEST_FILES_DIR, 'garmin-fenix-5-bike.fit')


def _record_frames(fit_path, processor):
    frames = []
    with fitdecode.FitReader(fit_path, processor=processor) as fit:
        for frame in fit:
            if isinstance(frame, fitdecode.FitDataMessage) and \
                    frame.name == 'record':
                frames.append(frame)
    return frames


def _fmt(value, decimals):
    if value is None:
        return ''
    return f'{value:.{decimals}f}'


def _write_export(path, frames, units, decimals):
    fields = ('heart_rate', 'speed', 'distance', 'altitude')
    with open(path, 'w') as fp:
        header = ['timestamp']
        header += [f'{name} [{units[name]}]' for name in fields]
        fp.write(','.join(header) + '\n')
        for frame in frames:
            cells = [frame.get_value('timestamp').isoformat()]
            for name in fields:
                cells.append(_fmt(frame.get_value(name), decimals[name]))
            fp.write(','.join(cells) + '\n')


def _make_exports(tmpdir, corrupt=None):
    """
    Write two exports of the same FIT recording: export A in FIT native
    units (default processor), export B in standard units (km, km/h).

    *corrupt* is an optional ``(row, field, delta)`` tuple applied to
    export B.
    """
    units_a = {'heart_rate': 'bpm', 'speed': 'm/s', 'distance': 'm',
               'altitude': 'm'}
    units_b = {'heart_rate': 'bpm', 'speed': 'km/h', 'distance': 'km',
               'altitude': 'm'}
    decimals_a = {'heart_rate': 0, 'speed': 3, 'distance': 3, 'altitude': 3}
    decimals_b = {'heart_rate': 0, 'speed': 5, 'distance': 5, 'altitude': 3}

    frames_a = _record_frames(BIKE_FIT, fitdecode.DefaultDataProcessor())
    frames_b = _record_frames(
        BIKE_FIT, fitdecode.StandardUnitsDataProcessor())

    path_a = os.path.join(tmpdir, 'export_a.csv')
    path_b = os.path.join(tmpdir, 'export_b.csv')
    _write_export(path_a, frames_a, units_a, decimals_a)
    _write_export(path_b, frames_b, units_b, decimals_b)

    if corrupt is not None:
        row, field, delta = corrupt
        with open(path_b) as fp:
            lines = fp.readlines()
        header = lines[0].rstrip('\n').split(',')
        idx = next(i for i, cell in enumerate(header)
                   if cell.startswith(field + ' '))
        cells = lines[row + 1].rstrip('\n').split(',')
        cells[idx] = _fmt(float(cells[idx]) + delta, decimals_b[field])
        lines[row + 1] = ','.join(cells) + '\n'
        with open(path_b, 'w') as fp:
            fp.writelines(lines)

    return path_a, path_b


class ClassifyTestCase(unittest.TestCase):
    def test_rounding(self):
        # both rounding intervals overlap inside the quantization interval
        q = ce.Interval(8.2575, 8.2585)  # raw 8258 at scale 1000
        a = ce.Interval(8.2575, 8.2585)  # "8.258" at 3 decimals
        b = ce.Interval(8.2579, 8.2581)
        self.assertEqual(ce.classify(q, a, b), ce.ROUNDING)

    def test_quantization(self):
        # disjoint rounding intervals, both inside quantization: the raw
        # record cannot tell the two exports apart
        q = ce.Interval(8.2575, 8.2585)
        a = ce.Interval(8.2575, 8.2577)
        b = ce.Interval(8.2583, 8.2585)
        self.assertEqual(ce.classify(q, a, b), ce.QUANTIZATION)

    def test_impossible(self):
        q = ce.Interval(8.2575, 8.2585)
        a = ce.Interval(8.2575, 8.2585)
        b = ce.Interval(8.2590, 8.2600)
        self.assertEqual(ce.classify(q, a, b), ce.IMPOSSIBLE_B)
        self.assertEqual(ce.classify(q, b, a), ce.IMPOSSIBLE_A)
        self.assertEqual(ce.classify(q, b, b), ce.IMPOSSIBLE_BOTH)


class ExportParsingTestCase(unittest.TestCase):
    def test_infer_decimals(self):
        self.assertEqual(ce.infer_decimals('16.15'), 2)
        self.assertEqual(ce.infer_decimals('0.0'), 1)
        self.assertEqual(ce.infer_decimals('77'), 0)
        self.assertEqual(ce.infer_decimals('-1.8000000000000114'), 16)
        self.assertIsNone(ce.infer_decimals('not a number'))

    def test_header_and_specs(self):
        export = ce.read_export(
            os.path.join(TEST_FILES_DIR, 'garmin-fenix-5-bike-records.csv'),
            units_spec={'speed': 'm/s', 'distance': 'm'},
            decimals_spec={'speed': 3})
        columns = {col.name: col for col in export.columns}
        self.assertEqual(columns['speed'].unit, 'm/s')
        self.assertEqual(columns['speed'].decimals, 3)
        self.assertEqual(columns['distance'].unit, 'm')
        # decimals inferred from the literals when not specified
        self.assertEqual(columns['heart_rate'].decimals, 0)
        self.assertGreaterEqual(columns['distance'].decimals, 1)

    def test_dimension_guard(self):
        # a speed column must not be reconciled against a distance field
        # (or vice versa) just because both units involve kilometres
        with self.assertRaises(ce.UnitError):
            ce.check_same_dimension('km', 'm/s', 'speed', 'x.csv')
        with self.assertRaises(ce.UnitError):
            ce.check_same_dimension('km/h', 'm', 'distance', 'x.csv')
        # same dimension, different units: allowed
        ce.check_same_dimension('km/h', 'm/s', 'speed', 'x.csv')
        ce.check_same_dimension('km', 'm', 'distance', 'x.csv')


class EndToEndTestCase(unittest.TestCase):
    def test_consistent_exports(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a, path_b = _make_exports(tmpdir)
            exports = (ce.read_export(path_a), ce.read_export(path_b))
            records = ce.load_fit_records(BIKE_FIT)
            verdicts, warnings = ce.compare_records(records, exports)
            self.assertEqual(warnings, [])
            impossible = [v for v in verdicts
                          if v.result in ce.IMPOSSIBLE_RESULTS]
            self.assertEqual(impossible, [])
            report = ce.format_report(records, exports, verdicts, warnings)
            self.assertIn('consistent', report)

    def test_first_impossible_row(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            # corrupt export B: row 10 (0-based), speed off by +1 km/h
            path_a, path_b = _make_exports(
                tmpdir, corrupt=(10, 'speed', 1.0))
            exports = (ce.read_export(path_a), ce.read_export(path_b))
            records = ce.load_fit_records(BIKE_FIT)
            verdicts, warnings = ce.compare_records(records, exports)

            impossible = [v for v in verdicts
                          if v.result in ce.IMPOSSIBLE_RESULTS]
            self.assertTrue(impossible)
            first = impossible[0]
            self.assertEqual(first.result, ce.IMPOSSIBLE_B)
            self.assertEqual(first.row, 10)
            self.assertEqual(first.column, 'speed')
            self.assertEqual(first.bad_export, 1)

            # the acceptable range must be stated in the export's own unit
            # and must contain the uncorrupted value
            column = ce._column_of(exports[1], 'speed')
            text = ce.acceptable_range_text(first.q_interval, column)
            self.assertIn('km/h', text)
            frame = _record_frames(
                BIKE_FIT, fitdecode.StandardUnitsDataProcessor())[10]
            good_value = frame.get_value('speed')
            factor = ce.conversion_factor('km/h')
            self.assertLessEqual(
                first.q_interval.lo / factor, good_value)
            self.assertGreaterEqual(
                first.q_interval.hi / factor, good_value)

            report = ce.format_report(records, exports, verdicts, warnings)
            self.assertIn('first incompatible row', report)
            self.assertIn(path_b, report)
            self.assertIn("'speed'", report)
            self.assertIn('acceptable range', report)

    def test_main_return_codes(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a, path_b = _make_exports(tmpdir)
            self.assertEqual(ce.main([BIKE_FIT, path_a, path_b]), 0)
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a, path_b = _make_exports(
                tmpdir, corrupt=(3, 'distance', 0.5))
            self.assertEqual(ce.main([BIKE_FIT, path_a, path_b]), 1)

    def test_main_rejects_mixed_dimensions(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            path_a, path_b = _make_exports(tmpdir)
            # relabel export B's speed column with a length unit
            with open(path_b) as fp:
                content = fp.read()
            content = content.replace(
                'speed [km/h]', 'speed [km]', 1)
            with open(path_b, 'w') as fp:
                fp.write(content)
            self.assertEqual(ce.main([BIKE_FIT, path_a, path_b]), 2)


if __name__ == '__main__':
    unittest.main()
