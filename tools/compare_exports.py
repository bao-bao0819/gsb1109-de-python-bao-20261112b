#!/usr/bin/env python3
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT
"""
Reconcile two unit-aware exports of the same FIT recording.

Given:

* the raw FIT file (the observation),
* two tabular exports (CSV) of the same message stream, each column carrying
  a unit, and each export carrying a known number of decimal places,

this tool decides, row by row and field by field, whether the two exports can
both derive from the same observation, and if not, why not:

* ``rounding`` -- the two exports disagree only because each was rounded to
  its own number of decimals after unit conversion.  Their rounding
  intervals overlap, so a single true value can produce both.
* ``quantization`` -- the exports' rounding intervals do not overlap, but
  both still intersect the quantization interval of the raw FIT field (the
  stored integer is only known to ``+- 0.5 / scale``).  The observation
  itself cannot tell the two exports apart.
* ``impossible`` -- one export's rounding interval does not intersect the
  raw field's quantization interval at all: that export cannot have been
  produced from this observation, whatever the rounding.

Quantities are matched by physical dimension, never by unit name alone: a
``speed`` column (e.g. km/h) is never reconciled against a ``distance``
field (e.g. km) merely because both can be expressed with kilometres.

The first incompatible row is located to its file and field, and the
acceptable range of exported values is printed.
"""

import argparse
import collections
import csv
import datetime
import os.path
import re
import sys
from decimal import Decimal, InvalidOperation

sys.path.insert(0, os.path.join(os.path.dirname(__file__), os.path.pardir))

import fitdecode  # noqa: E402,I202


#: classification results
ROUNDING = 'rounding'
QUANTIZATION = 'quantization'
IMPOSSIBLE_A = 'impossible_a'
IMPOSSIBLE_B = 'impossible_b'
IMPOSSIBLE_BOTH = 'impossible_both'

IMPOSSIBLE_RESULTS = (IMPOSSIBLE_A, IMPOSSIBLE_B, IMPOSSIBLE_BOTH)

#: conversion factors to each dimension's base unit (the FIT native unit)
_DIMENSIONS = collections.OrderedDict((
    ('length', {
        'm': 1.0, 'km': 1e3, 'cm': 1e-2, 'mm': 1e-3,
        'mi': 1609.344, 'mile': 1609.344, 'miles': 1609.344,
        'ft': 0.3048, 'feet': 0.3048, 'yd': 0.9144}),
    ('speed', {
        'm/s': 1.0, 'mps': 1.0,
        'km/h': 1.0 / 3.6, 'kph': 1.0 / 3.6, 'kmh': 1.0 / 3.6,
        'mph': 1609.344 / 3600.0, 'kn': 1852.0 / 3600.0}),
    ('angle', {
        'semicircles': 1.0,
        'deg': (2 ** 31) / 180.0,
        'degree': (2 ** 31) / 180.0,
        'degrees': (2 ** 31) / 180.0}),
    ('time', {'s': 1.0, 'ms': 1e-3, 'min': 60.0, 'h': 3600.0}),
    ('temperature', {'C': 1.0, 'degC': 1.0}),
    ('heart_rate', {'bpm': 1.0}),
    ('cadence', {'rpm': 1.0, 'spm': 1.0}),
    ('power', {'watts': 1.0, 'W': 1.0}),
    ('ratio', {'percent': 1.0, '%': 1.0}),
    ('unitless', {'': 1.0, '1': 1.0}),
))

_UNIT_TO_DIM = {}
for _dim, _table in _DIMENSIONS.items():
    for _unit in _table:
        _UNIT_TO_DIM[_unit] = _dim

#: relative slack used when testing interval intersection, to absorb binary
#: floating-point representation error only (kept far below any quantization
#: step, e.g. 0.5 semicircle for coordinates around 5e8)
_EPS = 1e-12

_HEADER_RE = re.compile(
    r'^\s*(?P<name>[^\[\(]+?)\s*(?:\[(?P<brack>[^\]]*)\]'
    r'|\((?P<paren>[^\)]*)\))?\s*$')

Interval = collections.namedtuple('Interval', ('lo', 'hi'))

Column = collections.namedtuple('Column', ('name', 'unit', 'decimals'))

Export = collections.namedtuple('Export', ('path', 'columns', 'rows'))

FitRecord = collections.namedtuple(
    'FitRecord', ('index', 'timestamp', 'fields'))


class UnitError(ValueError):
    """Raised when a unit is unknown or dimensionally incompatible."""


class ExportError(ValueError):
    """Raised when an export file cannot be understood."""


def unit_dimension(unit):
    """
    Return the physical dimension of *unit* (e.g. ``'speed'``), or `None`
    if the unit is not known.
    """
    return _UNIT_TO_DIM.get((unit or '').strip())


def conversion_factor(unit):
    """
    Return the factor converting a value expressed in *unit* to the base
    (FIT native) unit of its dimension.

    Raise `UnitError` if the unit is unknown.
    """
    dim = unit_dimension(unit)
    if dim is None:
        raise UnitError(f'unknown unit {unit!r}')
    return _DIMENSIONS[dim][(unit or '').strip()]


def check_same_dimension(export_unit, fit_unit, column_name, path):
    """
    Ensure an export column and the FIT field it is matched to measure the
    same physical quantity.

    This is what prevents a speed column from being reconciled against a
    distance field just because both can be expressed with kilometres.
    """
    exp_dim = unit_dimension(export_unit)
    fit_dim = unit_dimension(fit_unit)

    if exp_dim is None:
        raise UnitError(
            f'{path}: column {column_name!r}: unknown unit {export_unit!r}')
    if fit_dim is None:
        # the FIT field carries no known unit: only a unitless column can
        # be compared against it
        if exp_dim != 'unitless':
            raise UnitError(
                f'{path}: column {column_name!r}: unit {export_unit!r} '
                f'({exp_dim}) cannot be compared against a dimensionless '
                f'FIT field (units {fit_unit!r})')
        return
    if exp_dim != fit_dim:
        raise UnitError(
            f'{path}: column {column_name!r}: unit {export_unit!r} is a '
            f'{exp_dim} unit but the FIT field is expressed in '
            f'{fit_unit!r} ({fit_dim}); quantities of different dimensions '
            f'must not be reconciled even if their units share a name '
            f'(e.g. speed in km/h vs distance in km)')


def intersects(int_a, int_b):
    """Whether two `Interval` objects intersect, within float slack."""
    eps = _EPS * max(1.0, abs(int_a.lo), abs(int_a.hi),
                     abs(int_b.lo), abs(int_b.hi))
    return int_a.lo <= int_b.hi + eps and int_b.lo <= int_a.hi + eps


def classify(q_interval, exp_a, exp_b):
    """
    Classify the compatibility of two exports against one observation.

    *q_interval* is the raw field's quantization interval, *exp_a* and
    *exp_b* the two exports' rounding intervals, all expressed in the FIT
    field's native unit.
    """
    a_ok = intersects(exp_a, q_interval)
    b_ok = intersects(exp_b, q_interval)
    if not a_ok and not b_ok:
        return IMPOSSIBLE_BOTH
    if not a_ok:
        return IMPOSSIBLE_A
    if not b_ok:
        return IMPOSSIBLE_B
    if intersects(exp_a, exp_b):
        return ROUNDING
    return QUANTIZATION


def quantization_interval(field_data):
    """
    Interval of true values that could have produced the stored raw value
    of *field_data*, in the field's native unit.

    A field with ``scale`` stores ``round((true + offset) * scale)``, so the
    true value is known to ``+- 0.5 / scale``.  Unscaled fields are stored
    exactly.
    """
    value = field_data.value
    if value is None:
        return None
    field = field_data.field
    scale = getattr(field, 'scale', None) if field is not None else None
    half = 0.5 / float(scale) if scale else 0.0
    return Interval(float(value) - half, float(value) + half)


def rounding_interval(value, decimals):
    """
    Interval of true values that round to *value* (a `Decimal`) when
    rounded to *decimals* decimal places.
    """
    half = 0.5 * (10.0 ** -decimals)
    center = float(value)
    return Interval(center - half, center + half)


def infer_decimals(text):
    """Number of decimal places carried by the literal *text*."""
    try:
        exp = Decimal(text.strip()).as_tuple().exponent
    except InvalidOperation:
        return None
    return max(0, -exp) if isinstance(exp, int) else 0


def parse_key_value_spec(spec, value_parser, spec_name):
    """
    Parse a CLI spec of the form ``col1=val,col2=val`` into a dict, or a
    bare value applied to every column (returned as ``{'*': value}``).
    """
    if spec is None:
        return {}
    spec = spec.strip()
    if not spec:
        return {}
    if '=' not in spec:
        return {'*': value_parser(spec)}
    result = {}
    for item in spec.split(','):
        key, sep, val = item.partition('=')
        if not sep:
            raise ExportError(
                f'invalid {spec_name} spec item {item!r} (expected '
                f'name=value)')
        result[key.strip()] = value_parser(val.strip())
    return result


def parse_header_cell(cell):
    """Split a header cell like ``speed [km/h]`` into name and unit."""
    match = _HEADER_RE.match(cell)
    name = match.group('name').strip()
    unit = match.group('brack')
    if unit is None:
        unit = match.group('paren')
    return name, (unit.strip() if unit else None)


def read_export(path, units_spec=None, decimals_spec=None):
    """
    Read a CSV export.

    *units_spec* / *decimals_spec* are dicts as returned by
    `parse_key_value_spec`; column units may also be declared in the header
    (``name [unit]``) and decimals are inferred from the literals when not
    specified.
    """
    units_spec = units_spec or {}
    decimals_spec = decimals_spec or {}

    with open(path, 'r', newline='') as csv_file:
        rows = list(csv.reader(csv_file))
    if not rows:
        raise ExportError(f'{path}: empty file')

    header = rows[0]
    data_rows = [row for row in rows[1:] if row]
    for lineno, row in enumerate(data_rows, start=2):
        if len(row) != len(header):
            raise ExportError(
                f'{path}: line {lineno}: expected {len(header)} cells, '
                f'got {len(row)}')

    columns = []
    for idx, cell in enumerate(header):
        name, header_unit = parse_header_cell(cell)
        unit = units_spec.get(name, units_spec.get('*', header_unit))
        if unit is None:
            unit = ''
        decimals = decimals_spec.get(name, decimals_spec.get('*'))
        if decimals is None:
            inferred = [infer_decimals(row[idx]) for row in data_rows
                        if row[idx].strip()]
            inferred = [d for d in inferred if d is not None]
            decimals = max(inferred) if inferred else 0
        columns.append(Column(name=name, unit=unit, decimals=decimals))

    parsed_rows = []
    for row in data_rows:
        parsed = {}
        for idx, column in enumerate(columns):
            text = row[idx].strip()
            if not text:
                continue
            try:
                parsed[column.name] = Decimal(text)
            except InvalidOperation:
                # non-numeric cell (e.g. a timestamp string): kept as text
                parsed[column.name] = text
        parsed_rows.append(parsed)

    return Export(path=path, columns=columns, rows=parsed_rows)


def load_fit_records(fit_path, mesg_name='record'):
    """
    Load the raw records of *fit_path* without any unit conversion, so that
    values remain in FIT native units and ``raw_value``/``scale`` describe
    the original quantization.
    """
    records = []
    with fitdecode.FitReader(
            fit_path, processor=fitdecode.DataProcessorBase()) as fit:
        for index, frame in enumerate(fit):
            if not isinstance(frame, fitdecode.FitDataMessage):
                continue
            if frame.name != mesg_name:
                continue
            fields = {}
            for field_data in frame.fields:
                fields.setdefault(field_data.name, field_data)
            timestamp = None
            ts_data = fields.get('timestamp')
            if ts_data is not None and ts_data.value is not None:
                timestamp = datetime.datetime.fromtimestamp(
                    fitdecode.FIT_UTC_REFERENCE + ts_data.value,
                    datetime.timezone.utc)
            records.append(FitRecord(
                index=index, timestamp=timestamp, fields=fields))
    return records


def _resolve_fit_field(record_fields, column_name):
    field_data = record_fields.get(column_name)
    if field_data is not None:
        return field_data
    # an export may use the "enhanced" name for the same quantity or the
    # other way round; both share scale and units in the FIT profile
    for other in (f'enhanced_{column_name}',):
        field_data = record_fields.get(other)
        if field_data is not None:
            return field_data
    if column_name.startswith('enhanced_'):
        field_data = record_fields.get(column_name[len('enhanced_'):])
        if field_data is not None:
            return field_data
    return None


def _export_interval_native(value, column):
    factor = conversion_factor(column.unit)
    interval = rounding_interval(value, column.decimals)
    return Interval(interval.lo * factor, interval.hi * factor)


FieldVerdict = collections.namedtuple('FieldVerdict', (
    'row', 'column', 'result', 'bad_export', 'q_interval', 'exp_interval'))


def compare_records(fit_records, exports, mesg_name='record'):
    """
    Compare the exports row by row, field by field.

    Returns ``(verdicts, warnings)`` where *verdicts* is a list of
    `FieldVerdict` (only non-`ROUNDING` results and impossibilities are
    kept) and *warnings* a list of human-readable notes.
    """
    exp_a, exp_b = exports
    warnings = []

    fit_units = {}
    for record in fit_records:
        for name, field_data in record.fields.items():
            fit_units.setdefault(name, field_data.units)

    # validate dimensions once per column, before any comparison
    for export in exports:
        checked = set()
        for column in export.columns:
            if column.name in checked:
                continue
            checked.add(column.name)
            if column.name not in fit_units:
                continue  # unknown column: reported later as a warning
            if not any(isinstance(row.get(column.name), Decimal)
                       for row in export.rows):
                continue  # non-numeric column (e.g. timestamp text)
            check_same_dimension(
                column.unit, fit_units[column.name], column.name,
                export.path)

    n_rows = min(len(fit_records), len(exp_a.rows), len(exp_b.rows))
    for label, count in (('FIT records', len(fit_records)),
                         (f'rows of {exp_a.path}', len(exp_a.rows)),
                         (f'rows of {exp_b.path}', len(exp_b.rows))):
        if count != n_rows:
            warnings.append(
                f'row count mismatch: {label} has {count} rows; only the '
                f'first {n_rows} rows are compared')

    unknown_columns = set()
    verdicts = []
    for row_idx in range(n_rows):
        record = fit_records[row_idx]
        row_a = exp_a.rows[row_idx]
        row_b = exp_b.rows[row_idx]
        names = []
        for column in exp_a.columns + exp_b.columns:
            if column.name not in names:
                names.append(column.name)
        for name in names:
            field_data = _resolve_fit_field(record.fields, name)
            if field_data is None:
                if name not in unknown_columns:
                    unknown_columns.add(name)
                    if name != 'timestamp':
                        warnings.append(
                            f'column {name!r}: no matching field in FIT '
                            f'{mesg_name!r} messages; column skipped')
                continue
            value_a = row_a.get(name)
            value_b = row_b.get(name)
            if not isinstance(value_a, Decimal) and \
                    not isinstance(value_b, Decimal):
                continue
            q_interval = quantization_interval(field_data)
            if q_interval is None:
                continue
            col_a = _column_of(exp_a, name)
            col_b = _column_of(exp_b, name)
            int_a = (_export_interval_native(value_a, col_a)
                     if isinstance(value_a, Decimal) else None)
            int_b = (_export_interval_native(value_b, col_b)
                     if isinstance(value_b, Decimal) else None)
            if int_a is None:
                result = IMPOSSIBLE_B if not intersects(
                    int_b, q_interval) else ROUNDING
                if result in IMPOSSIBLE_RESULTS:
                    verdicts.append(FieldVerdict(
                        row=row_idx, column=name, result=result,
                        bad_export=1, q_interval=q_interval,
                        exp_interval=int_b))
                continue
            if int_b is None:
                result = IMPOSSIBLE_A if not intersects(
                    int_a, q_interval) else ROUNDING
                if result in IMPOSSIBLE_RESULTS:
                    verdicts.append(FieldVerdict(
                        row=row_idx, column=name, result=result,
                        bad_export=0, q_interval=q_interval,
                        exp_interval=int_a))
                continue
            result = classify(q_interval, int_a, int_b)
            if result == ROUNDING:
                continue
            bad = None
            exp_interval = None
            if result == IMPOSSIBLE_A:
                bad, exp_interval = 0, int_a
            elif result == IMPOSSIBLE_B:
                bad, exp_interval = 1, int_b
            verdicts.append(FieldVerdict(
                row=row_idx, column=name, result=result, bad_export=bad,
                q_interval=q_interval, exp_interval=exp_interval))

    return verdicts, warnings


def _column_of(export, name):
    for column in export.columns:
        if column.name == name:
            return column
    return None


def acceptable_range_text(q_interval, column):
    """
    Human-readable acceptable value range for one export column, given the
    observation's quantization interval *q_interval* (native units).
    """
    factor = conversion_factor(column.unit)
    lo = q_interval.lo / factor
    hi = q_interval.hi / factor
    half = 0.5 * (10.0 ** -column.decimals)
    unit = column.unit or '(unitless)'
    return (
        f'true value in [{lo:.10g}, {hi:.10g}] {unit}; '
        f'any exported number in [{lo - half:.10g}, {hi + half:.10g}] '
        f'{unit} (at {column.decimals} decimals) remains compatible')


def format_report(fit_records, exports, verdicts, warnings):
    """Build the human-readable comparison report."""
    exp_a, exp_b = exports
    lines = []
    for warning in warnings:
        lines.append(f'warning: {warning}')

    counts = collections.Counter(v.result for v in verdicts)
    lines.append(
        'summary: '
        f'{counts[QUANTIZATION]} field(s) indistinguishable at source '
        f'quantization, '
        f'{sum(counts[r] for r in IMPOSSIBLE_RESULTS)} impossible field(s)')

    impossible = [v for v in verdicts if v.result in IMPOSSIBLE_RESULTS]
    if impossible:
        first = impossible[0]
        record = fit_records[first.row]
        if first.result == IMPOSSIBLE_BOTH:
            bad_export, bad_idx = exp_a, 0
        else:
            bad_idx = first.bad_export
            bad_export = exports[bad_idx]
        column = _column_of(bad_export, first.column)
        cell = bad_export.rows[first.row].get(first.column)
        ts_text = (record.timestamp.isoformat()
                   if record.timestamp is not None else 'no timestamp')
        lines.append('')
        lines.append('first incompatible row:')
        lines.append(
            f'  file: {bad_export.path} (export {"AB"[bad_idx]}), '
            f'data row {first.row + 1}, field {first.column!r}')
        lines.append(
            f'  FIT record #{record.index}, timestamp {ts_text}')
        lines.append(f'  exported value: {cell} {column.unit or ""}'.rstrip())
        fit_units = record.fields[first.column].units \
            if first.column in record.fields else None
        lines.append(
            f'  quantization interval of the raw field '
            f'(native {fit_units or "unitless"}): '
            f'[{first.q_interval.lo:.10g}, {first.q_interval.hi:.10g}]')
        lines.append(
            f'  acceptable range: '
            f'{acceptable_range_text(first.q_interval, column)}')
    else:
        quant = [v for v in verdicts if v.result == QUANTIZATION]
        lines.append('')
        if quant:
            lines.append(
                'no impossible value found; the two exports can both '
                'derive from this observation')
            lines.append(
                f'  note: {len(quant)} field(s) differ by more than export '
                'rounding but remain inside the raw field quantization '
                '(the observation cannot tell them apart)')
        else:
            lines.append(
                'the two exports are fully consistent with the observation '
                '(differences explained by rounding after unit conversion)')

    return '\n'.join(lines)


def _decimals_parser(text):
    try:
        value = int(text)
    except ValueError:
        raise ExportError(f'invalid decimals count {text!r}') from None
    if value < 0:
        raise ExportError(f'invalid decimals count {text!r}')
    return value


def main(argv=None):
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('fit_file', help='the raw FIT recording')
    parser.add_argument('export_a', help='first CSV export')
    parser.add_argument('export_b', help='second CSV export')
    parser.add_argument(
        '--mesg', default='record',
        help="FIT message type to compare (default: 'record')")
    parser.add_argument(
        '--units-a', dest='units_a',
        help="units for export A: 'col=unit,...' or a single unit; "
             "default: parsed from 'name [unit]' headers, else unitless")
    parser.add_argument(
        '--units-b', dest='units_b', help='units for export B')
    parser.add_argument(
        '--decimals-a', dest='decimals_a',
        help="decimal places for export A: 'col=N,...' or a single N; "
             'default: inferred from the literals')
    parser.add_argument(
        '--decimals-b', dest='decimals_b', help='decimal places for export B')
    args = parser.parse_args(argv)

    try:
        units_a = parse_key_value_spec(args.units_a, str, 'units')
        units_b = parse_key_value_spec(args.units_b, str, 'units')
        decimals_a = parse_key_value_spec(
            args.decimals_a, _decimals_parser, 'decimals')
        decimals_b = parse_key_value_spec(
            args.decimals_b, _decimals_parser, 'decimals')
        exports = (
            read_export(args.export_a, units_a, decimals_a),
            read_export(args.export_b, units_b, decimals_b))
        fit_records = load_fit_records(args.fit_file, mesg_name=args.mesg)
        verdicts, warnings = compare_records(
            fit_records, exports, mesg_name=args.mesg)
    except (ExportError, UnitError, fitdecode.FitParseError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2

    print(format_report(fit_records, exports, verdicts, warnings))
    return (1 if any(v.result in IMPOSSIBLE_RESULTS for v in verdicts)
            else 0)


if __name__ == '__main__':
    sys.exit(main())
