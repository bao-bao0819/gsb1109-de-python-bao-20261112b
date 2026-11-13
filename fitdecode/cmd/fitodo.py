#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

"""
Command-line entry point auditing a wrapping cumulative odometer (the
``record.distance`` field of a FIT file).

See :mod:`fitdecode.odometer` for the meaning of the reported statuses.
"""

import argparse
import json
import sys
from collections import OrderedDict

import fitdecode
from fitdecode.odometer import DEFAULT_ROLLOVER, audit_odometer, iter_record_odometer

#: exit code returned when at least one pair of observations contradicts the
#: maximum-speed hypothesis. Distinct from argparse's own usage-error code (2)
#: so callers can tell a data contradiction apart from a bad command line.
EXIT_CONTRADICTION = 3


def parse_args(args=None):
    parser = argparse.ArgumentParser(
        description=(
            'Audit the wrapping cumulative odometer of a FIT file: list the '
            'distance segments that are uniquely determined, the intervals '
            'that still admit several explanations, and the contradictory '
            'observations'),
        epilog=(
            f'fitdecode version {fitdecode.__version__}; exit code '
            f'{EXIT_CONTRADICTION} when the input contains contradictory '
            'observations'),
        allow_abbrev=False)

    parser.add_argument(
        '--output', '-o', type=argparse.FileType(mode='wt', encoding='utf-8'),
        default='-',
        help='File to output data into (defaults to stdout)')

    parser.add_argument(
        '--json', dest='as_json', action='store_true',
        help='Emit a machine-readable JSON report instead of the text report')

    parser.add_argument(
        '--max-speed', type=float, default=None,
        help=(
            'Known maximum speed of the device. Required to bound plausible '
            'increments; without it every non-trivial gap stays ambiguous. '
            'Unit is selected with --speed-unit'))

    parser.add_argument(
        '--speed-unit', choices=('ms', 'kmh', 'mph'), default='ms',
        help=(
            'Unit of --max-speed: "ms" for m/s (default), "kmh" for km/h or '
            '"mph" for miles per hour'))

    parser.add_argument(
        '--rollover', type=int, default=DEFAULT_ROLLOVER,
        help=(
            'Number of counts in a full counter turn '
            f'(defaults to 2**32 = {DEFAULT_ROLLOVER})'))

    parser.add_argument(
        '--scale', type=float, default=100,
        help=(
            'Counts per meter of the distance field, i.e. its FIT scale '
            '(defaults to 100 for record.distance)'))

    parser.add_argument(
        '--nocrc', action='store_const',
        const=fitdecode.CrcCheck.DISABLED,
        default=fitdecode.CrcCheck.WARN,
        help="Some devices seem to write invalid CRC's, ignore these")

    parser.add_argument(
        'infile', metavar='FITFILE', type=argparse.FileType(mode='rb'),
        help='Input .FIT file (use - for stdin)')

    options = parser.parse_args(args)

    if options.max_speed is not None:
        if options.max_speed < 0:
            parser.error('--max-speed must be non-negative')
        if options.speed_unit == 'kmh':
            options.max_speed /= 3.6
        elif options.speed_unit == 'mph':
            options.max_speed *= 1609.344 / 3600.0
        # options.max_speed is now expressed in m/s
        if options.scale <= 0:
            parser.error('--scale must be positive')
    elif options.speed_unit != 'ms':
        parser.error('--speed-unit requires --max-speed')

    if options.rollover <= 0:
        parser.error('--rollover must be a positive integer')

    return options


def _audit_per_fit_file(observations, options):
    """Run one audit per chained FIT file and return a list of audits."""
    audits = []
    current = []
    current_file = None

    for obs in observations:
        if current_file is not None and obs.fit_file_index != current_file:
            audits.append(audit_odometer(
                current,
                rollover=options.rollover,
                max_speed=options.max_speed,
                scale=options.scale))
            current = []
        current_file = obs.fit_file_index
        current.append(obs)

    if current:
        audits.append(audit_odometer(
            current,
            rollover=options.rollover,
            max_speed=options.max_speed,
            scale=options.scale))

    return audits


def _meters(counts, scale):
    return counts / scale


def _fmt_counts_meters(counts, scale):
    return f'{counts} counts ({_meters(counts, scale):.2f} m)'


def _obs_label(obs):
    return (
        f'obs#{obs.index} (frame#{obs.frame_index}, '
        f't={obs.timestamp if obs.timestamp is not None else "n/a"}, '
        f'reading={obs.reading})')


def render_text_report(audits, options):
    lines = []
    lines.append('ODOMETER ROLLOVER AUDIT')
    lines.append(f'input: {options.infile.name}')
    lines.append(f'rollover: {options.rollover}')
    lines.append(f'scale: {options.scale:g} counts/m')
    if options.max_speed is None:
        lines.append('max speed: none (gaps cannot be bounded)')
    else:
        lines.append(f'max speed: {options.max_speed:g} m/s')
    lines.append('')

    for file_index, audit in enumerate(audits):
        lines.append(f'-- FIT file #{file_index} --')
        lines.append(f'observations: {len(audit.observations)}')
        if not audit.observations:
            lines.append('no record.distance reading found in this file')
            lines.append('')
            continue
        lines.append(
            f'gaps: {len(audit)} total, {len(audit.unique_gaps)} unique, '
            f'{len(audit.ambiguous_gaps)} ambiguous, '
            f'{len(audit.impossible_gaps)} impossible')
        lines.append('')

        _render_unique(lines, audit)
        _render_ambiguous(lines, audit)
        _render_impossible(lines, audit)

    if not audits:
        lines.append('no record.distance reading found in the input file')
        lines.append('')

    return '\n'.join(lines)


def _render_unique(lines, audit):
    gaps = audit.unique_gaps
    if not gaps:
        return

    lines.append('Uniquely determined segments:')
    for gap in gaps:
        explanation = gap.unique_explanation
        if explanation[0] == 'rollover':
            turns = explanation[1]
            increment = gap.delta + turns * audit.rollover
            detail = (
                f'continuous counter, {turns} wrap(s), true increment '
                f'{_fmt_counts_meters(increment, audit.scale)}')
        else:
            detail = (
                'restart/reset in between; visible post-reset distance '
                f'{_fmt_counts_meters(gap.curr.reading, audit.scale)}')

        lines.append(
            f'  {_obs_label(gap.prev)} -> {_obs_label(gap.curr)}')
        lines.append(f'      {detail}')
    lines.append('')


def _render_ambiguous(lines, audit):
    gaps = audit.ambiguous_gaps
    if not gaps:
        return

    lines.append('Ambiguous intervals (several explanations remain; '
                 'the shortest increment is NOT assumed):')
    for gap in gaps:
        lines.append(
            f'  {_obs_label(gap.prev)} -> {_obs_label(gap.curr)}')
        if not gap.bounded:
            if gap.elapsed is not None and gap.elapsed < 0:
                reason = (
                    'timestamp goes backwards '
                    f'({gap.elapsed}s): no speed bound can be applied; any '
                    'wrap count, including a restart, remains possible')
            elif gap.elapsed == 0:
                reason = (
                    'zero elapsed time (timestamp resolution too coarse for a '
                    'speed bound): any wrap count, including a restart, '
                    'remains possible')
            else:
                reason = (
                    'no speed bound and/or missing timestamp: any wrap '
                    'count, including a restart, remains possible')
            lines.append(f'      {reason}')
            continue

        increments = tuple(gap.increment_candidates(audit.rollover))
        rollover_text = ', '.join(str(n) for n in gap.rollovers)
        lines.append(f'      continuous wrap counts n: {rollover_text}')
        lines.append(
            '      continuous true increments: '
            + ', '.join(
                _fmt_counts_meters(value, audit.scale) for value in increments))
        if gap.restart_possible:
            lines.append(
                '      restart/reset: also possible (post-reset reading '
                f'{_fmt_counts_meters(gap.curr.reading, audit.scale)} fits)')
        lines.append(
            '      allowed increment range: '
            f'{_fmt_counts_meters(gap.min_increment, audit.scale)} to '
            f'{_fmt_counts_meters(gap.max_increment, audit.scale)}')
    lines.append('')


def _render_impossible(lines, audit):
    gaps = audit.impossible_gaps
    if not gaps:
        return

    lines.append('Contradictory observations (no wrap count and no restart '
                 'fits the maximum speed):')
    for gap in gaps:
        required_continuous = gap.delta + (
            audit.rollover if gap.wrapped else 0)
        lines.append(
            f'  conflicting pair: {_obs_label(gap.prev)} and '
            f'{_obs_label(gap.curr)}')
        lines.append(
            f'      elapsed: {gap.elapsed}s; maximum plausible increment '
            f'{_fmt_counts_meters(gap.max_increment, audit.scale)}')
        if gap.wrapped:
            lines.append(
                '      reading decreased: one wrap already requires '
                f'{_fmt_counts_meters(required_continuous, audit.scale)}')
        else:
            lines.append(
                '      reading increase requires '
                f'{_fmt_counts_meters(required_continuous, audit.scale)}')
        if not gap.restart_possible:
            lines.append(
                '      restart ruled out: the post-reset reading alone '
                f'({gap.curr.reading} counts) exceeds the speed budget')
    lines.append('')


def _gap_to_dict(gap, rollover, scale):
    data = OrderedDict((
        ('prev_index', gap.prev.index),
        ('curr_index', gap.curr.index),
        ('prev_frame_index', gap.prev.frame_index),
        ('curr_frame_index', gap.curr.frame_index),
        ('prev_timestamp', gap.prev.timestamp),
        ('curr_timestamp', gap.curr.timestamp),
        ('prev_reading', gap.prev.reading),
        ('curr_reading', gap.curr.reading),
        ('elapsed', gap.elapsed),
        ('time_ordered', gap.is_time_ordered),
        ('delta', gap.delta),
        ('wrapped', gap.wrapped),
        ('bounded', gap.bounded),
        ('min_increment', gap.min_increment),
        ('max_increment', gap.max_increment),
        ('rollover_counts', list(gap.rollovers)),
        ('restart_possible', gap.restart_possible),
        ('status', gap.status)))

    if gap.bounded:
        increments = list(gap.increment_candidates(rollover))
        data['continuous_increments_counts'] = increments
        data['continuous_increments_m'] = [
            value / scale for value in increments]

    if gap.is_unique:
        data['unique_explanation'] = list(gap.unique_explanation)

    return data


def render_json_report(audits, options):
    report = OrderedDict((
        ('input', options.infile.name),
        ('rollover', options.rollover),
        ('scale', options.scale),
        ('max_speed_ms', options.max_speed),
        ('fit_files', [])))

    for file_index, audit in enumerate(audits):
        file_report = OrderedDict((
            ('fit_file_index', file_index),
            ('observation_count', len(audit.observations)),
            ('consistent', audit.is_consistent),
            ('unique_gap_count', len(audit.unique_gaps)),
            ('ambiguous_gap_count', len(audit.ambiguous_gaps)),
            ('impossible_gap_count', len(audit.impossible_gaps)),
            ('observations', [
                OrderedDict((
                    ('index', obs.index),
                    ('frame_index', obs.frame_index),
                    ('timestamp', obs.timestamp),
                    ('reading', obs.reading)))
                for obs in audit.observations]),
            ('gaps', [
                _gap_to_dict(gap, audit.rollover, audit.scale)
                for gap in audit])))
        report['fit_files'].append(file_report)

    return json.dumps(report, indent='\t')


def main(args=None):
    options = parse_args(args)

    try:
        observations = tuple(iter_record_odometer(
            options.infile, check_crc=options.nocrc))
    except Exception:
        print(
            'ERROR: failed to parse FIT file for odometer audit',
            file=sys.stderr)
        raise

    audits = _audit_per_fit_file(observations, options)

    if options.as_json:
        print(render_json_report(audits, options), file=options.output)
    else:
        print(render_text_report(audits, options), file=options.output)

    if any(not audit.is_consistent for audit in audits):
        return EXIT_CONTRADICTION
    return 0


if __name__ == '__main__':
    sys.exit(main())
