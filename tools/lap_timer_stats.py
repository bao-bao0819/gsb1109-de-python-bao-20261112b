#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT
"""
Per-lap effective timer-running time, reconstructed from FIT timer events.

The goal is *not* ``lap.timestamp - lap.start_time`` (wall clock) but the
number of seconds during which the timer was actually running inside each
lap, according to the ``timer`` start/stop events recorded in the file.

Rules implemented here:

* ``start`` and ``stop``/``stop_all``/``stop_disable_all`` timer events drive
  a state machine.  Duplicate stops (several stops in a row, possibly within
  the same second) close the running interval only once; duplicate starts
  while already running are ignored as well.
* Events, lap messages and pauses may share the same second, and a pause may
  span a lap boundary.  Running intervals are therefore computed globally first,
  then clipped to each lap's coverage ``[start_time, timestamp)`` so every
  second is attributed to exactly one lap.
* If the file begins while the timer is already running (first timer event
  is a stop), the running time before that stop has no known origin: it is
  reported as unassignable and is *not* back-filled from the first record.
* If the timer is still running at end of data, the open interval is
  reported as unclosed and is not counted as confirmed running time.
* Consistency check: the sum of the confirmed per-lap running seconds must
  equal the known running duration within the union of the laps' coverage.

Usage::

    python tools/lap_timer_stats.py activity.fit [--json]
"""

import argparse
import datetime
import json
import sys

import fitdecode

__all__ = [
    'TimerEvent', 'Lap', 'Analysis', 'analyze', 'analyze_file',
    'format_report']

TIMER_START_TYPES = frozenset(('start',))
TIMER_STOP_TYPES = frozenset(('stop', 'stop_all', 'stop_disable_all'))


class TimerEvent:
    """A normalized timer event: ``kind`` is either ``'start'`` or ``'stop'``."""

    __slots__ = ('timestamp', 'kind')

    def __init__(self, timestamp, kind):
        assert kind in ('start', 'stop')
        self.timestamp = timestamp
        self.kind = kind

    def __repr__(self):
        return f'TimerEvent({self.timestamp!r}, {self.kind!r})'


class Lap:
    """A lap's time coverage, as a half-open interval ``[start, end)``."""

    __slots__ = ('index', 'start', 'end')

    def __init__(self, index, start, end):
        self.index = index
        self.start = start
        self.end = end

    @property
    def duration(self):
        return (self.end - self.start).total_seconds()


class Analysis:
    """Result of :func:`analyze`.  All durations are in seconds."""

    def __init__(self):
        # confirmed running intervals, as (start, end) datetime pairs
        self.intervals = []
        # timestamp of the first stop if the file started already running
        # without any start evidence, else None
        self.head_stop_without_start = None
        # start of a still-open running interval at end of data, else None
        self.unclosed_start = None
        # laps, in file order
        self.laps = []
        # per-lap confirmed running sub-intervals: {lap_index: [(start, end)]}
        self.lap_intervals = {}
        # confirmed running intervals (or parts) outside any lap coverage
        self.uncovered_intervals = []
        # wall-clock gaps between lap coverages: [(start, end)]
        self.coverage_gaps = []

    def lap_running_seconds(self, lap):
        return sum(
            (end - start).total_seconds()
            for start, end in self.lap_intervals.get(lap.index, ()))

    def lap_paused_seconds(self, lap):
        return lap.duration - self.lap_running_seconds(lap)

    def total_lap_running_seconds(self):
        return sum(self.lap_running_seconds(lap) for lap in self.laps)

    def covered_running_seconds(self):
        """Known running duration within the union of lap coverages."""
        total = 0.0
        for start, end in _merge_ranges(
                (lap.start, lap.end) for lap in self.laps):
            total += _overlap_seconds(self.intervals, start, end)
        return total


def _overlap_seconds(intervals, start, end):
    """Total seconds of ``intervals`` falling within ``[start, end)``."""
    total = 0.0
    for int_start, int_end in intervals:
        lo = max(int_start, start)
        hi = min(int_end, end)
        if hi > lo:
            total += (hi - lo).total_seconds()
    return total


def _merge_ranges(ranges):
    """Merge sorted-or-not half-open ``(start, end)`` ranges."""
    sorted_ranges = sorted(ranges)
    merged = []
    for start, end in sorted_ranges:
        if merged and start <= merged[-1][1]:
            if end > merged[-1][1]:
                merged[-1] = (merged[-1][0], end)
        else:
            merged.append((start, end))
    return merged


def build_running_intervals(events):
    """
    Apply the start/stop state machine to ``events`` (in file order).

    Returns ``(intervals, head_stop_without_start, unclosed_start)``:

    * ``intervals``: confirmed running ``(start, end)`` datetime pairs.
    * ``head_stop_without_start``: timestamp of the first stop event if it
      occurred before any start (file began while already running).
    * ``unclosed_start``: start of a running interval still open at the end
      of the event stream.
    """
    intervals = []
    head_stop_without_start = None
    running_since = None
    saw_any_event = False

    for event in events:
        if event.kind == 'start':
            if running_since is None:
                running_since = event.timestamp
            # else: duplicate start while already running, ignore it
        else:  # stop
            if running_since is not None:
                if event.timestamp > running_since:
                    intervals.append((running_since, event.timestamp))
                # a stop at the very same second as the start yields a
                # zero-length interval: nothing to record
                running_since = None
            elif not saw_any_event:
                # the file begins while the timer is already running but
                # there is no start evidence: do not back-fill the gap
                head_stop_without_start = event.timestamp
            # else: duplicate stop, ignore it (no double deduction)
        saw_any_event = True

    return intervals, head_stop_without_start, running_since


def _attribute_to_laps(intervals, laps):
    """Clip running intervals to each lap's coverage."""
    lap_intervals = {lap.index: [] for lap in laps}
    for int_start, int_end in intervals:
        for lap in laps:
            lo = max(int_start, lap.start)
            hi = min(int_end, lap.end)
            if hi > lo:
                lap_intervals[lap.index].append((lo, hi))
    return lap_intervals


def _uncovered_parts(intervals, laps):
    """Parts of running intervals not covered by any lap."""
    uncovered = []
    for int_start, int_end in intervals:
        cursor = int_start
        for cov_start, cov_end in _merge_ranges(
                (lap.start, lap.end) for lap in laps):
            if cov_end <= cursor:
                continue
            if cov_start >= int_end:
                break
            if cov_start > cursor:
                uncovered.append((cursor, min(cov_start, int_end)))
            cursor = max(cursor, cov_end)
        if cursor < int_end:
            uncovered.append((cursor, int_end))
    return uncovered


def _coverage_gaps(laps):
    """Wall-clock gaps between consecutive lap coverage blocks."""
    merged = _merge_ranges((lap.start, lap.end) for lap in laps)
    gaps = []
    for (_, prev_end), (next_start, _) in zip(merged, merged[1:]):
        if next_start > prev_end:
            gaps.append((prev_end, next_start))
    return gaps


def analyze(events, laps):
    """Run the full analysis from normalized events and laps."""
    result = Analysis()
    result.laps = list(laps)
    (result.intervals,
     result.head_stop_without_start,
     result.unclosed_start) = build_running_intervals(events)
    result.lap_intervals = _attribute_to_laps(result.intervals, result.laps)
    result.uncovered_intervals = _uncovered_parts(result.intervals, result.laps)
    result.coverage_gaps = _coverage_gaps(result.laps)
    return result


def _read_fit(path):
    """Extract timer events and lap coverages from a FIT file."""
    events = []
    laps = []
    lap_index = 0

    with fitdecode.FitReader(path) as reader:
        for frame in reader:
            if frame.frame_type != fitdecode.FIT_FRAME_DATA:
                continue

            if frame.name == 'event':
                timestamp = frame.get_value('timestamp', fallback=None)
                event = frame.get_value('event', fallback=None)
                event_type = frame.get_value('event_type', fallback=None)
                if timestamp is None:
                    continue
                if event == 'timer':
                    if event_type in TIMER_START_TYPES:
                        events.append(TimerEvent(timestamp, 'start'))
                    elif event_type in TIMER_STOP_TYPES:
                        events.append(TimerEvent(timestamp, 'stop'))
                elif event == 'session' and event_type in TIMER_STOP_TYPES:
                    # a session stop also stops the timer
                    events.append(TimerEvent(timestamp, 'stop'))

            elif frame.name == 'lap':
                lap_index += 1
                end = frame.get_value('timestamp', fallback=None)
                start = frame.get_value('start_time', fallback=None)
                if start is None and end is not None:
                    elapsed = frame.get_value(
                        'total_elapsed_time', fallback=None)
                    if elapsed is not None:
                        start = end - datetime.timedelta(seconds=elapsed)
                if start is None or end is None or end <= start:
                    print(
                        f'warning: lap #{lap_index} has unusable time '
                        'bounds, skipped', file=sys.stderr)
                    continue
                laps.append(Lap(lap_index, start, end))

    return events, laps


def analyze_file(path):
    """Analyze a FIT file and return an :class:`Analysis`."""
    events, laps = _read_fit(path)
    return analyze(events, laps)


def _fmt_dt(value):
    return value.strftime('%Y-%m-%d %H:%M:%S')


def _fmt_secs(seconds):
    return f'{seconds:.1f}s'


def format_report(analysis):
    """Render a human-readable report of an :class:`Analysis`."""
    lines = []
    lines.append('=== 每圈有效运行时间(按计时器事件,非圈尾减圈头)===')

    for lap in analysis.laps:
        running = analysis.lap_running_seconds(lap)
        paused = analysis.lap_paused_seconds(lap)
        lines.append(
            f'圈 {lap.index}  覆盖 [{_fmt_dt(lap.start)}, '
            f'{_fmt_dt(lap.end)})  墙钟 {_fmt_secs(lap.duration)}')
        lines.append(
            f'  有效运行 {_fmt_secs(running)}  暂停 {_fmt_secs(paused)}')
        sub_intervals = analysis.lap_intervals.get(lap.index, ())
        if sub_intervals:
            lines.append('  运行区间:')
            for start, end in sub_intervals:
                lines.append(
                    f'    [{_fmt_dt(start)}, {_fmt_dt(end)})  '
                    f'{_fmt_secs((end - start).total_seconds())}')
        else:
            lines.append('  运行区间: (无)')

    lines.append('')
    lines.append('=== 无法归属的时间 ===')
    unassigned = False
    if analysis.head_stop_without_start is not None:
        unassigned = True
        lines.append(
            f'  文件开头已在运行: 首个停止事件 '
            f'{_fmt_dt(analysis.head_stop_without_start)} 之前缺少开始依据,'
            '起点未知,不计入任何圈')
    if analysis.unclosed_start is not None:
        unassigned = True
        lines.append(
            f'  未闭合的运行区间: 自 {_fmt_dt(analysis.unclosed_start)} '
            '起运行,数据结束时仍未停止,终点未知,不计入任何圈')
    for start, end in analysis.uncovered_intervals:
        unassigned = True
        lines.append(
            f'  圈覆盖范围外的已知运行: [{_fmt_dt(start)}, {_fmt_dt(end)})  '
            f'{_fmt_secs((end - start).total_seconds())}')
    for start, end in analysis.coverage_gaps:
        unassigned = True
        lines.append(
            f'  圈间空隙(无圈覆盖的墙钟时间): [{_fmt_dt(start)}, '
            f'{_fmt_dt(end)})  {_fmt_secs((end - start).total_seconds())}')
    if not unassigned:
        lines.append('  (无)')

    lines.append('')
    total = analysis.total_lap_running_seconds()
    covered = analysis.covered_running_seconds()
    ok = abs(total - covered) < 1e-6
    lines.append(
        f'校验: 各圈有效运行合计 {_fmt_secs(total)} '
        f'{"==" if ok else "!="} 圈覆盖范围内已知运行时长 '
        f'{_fmt_secs(covered)}  {"OK" if ok else "不一致!"}')
    return '\n'.join(lines)


def _to_json_dict(analysis):
    def interval_list(intervals):
        return [
            {'start': _fmt_dt(start), 'end': _fmt_dt(end),
             'seconds': (end - start).total_seconds()}
            for start, end in intervals]

    return {
        'laps': [
            {
                'index': lap.index,
                'coverage_start': _fmt_dt(lap.start),
                'coverage_end': _fmt_dt(lap.end),
                'wall_clock_seconds': lap.duration,
                'running_seconds': analysis.lap_running_seconds(lap),
                'paused_seconds': analysis.lap_paused_seconds(lap),
                'running_intervals': interval_list(
                    analysis.lap_intervals.get(lap.index, ())),
            }
            for lap in analysis.laps],
        'unassignable': {
            'head_stop_without_start': (
                _fmt_dt(analysis.head_stop_without_start)
                if analysis.head_stop_without_start else None),
            'unclosed_start': (
                _fmt_dt(analysis.unclosed_start)
                if analysis.unclosed_start else None),
            'uncovered_running_intervals': interval_list(
                analysis.uncovered_intervals),
            'coverage_gaps': interval_list(analysis.coverage_gaps),
        },
        'check': {
            'sum_lap_running_seconds': analysis.total_lap_running_seconds(),
            'covered_running_seconds': analysis.covered_running_seconds(),
            'consistent': abs(
                analysis.total_lap_running_seconds()
                - analysis.covered_running_seconds()) < 1e-6,
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Per-lap effective timer-running time from FIT timer '
                    'events.')
    parser.add_argument('fit_file', help='path to the FIT activity file')
    parser.add_argument(
        '--json', action='store_true',
        help='emit machine-readable JSON instead of a text report')
    args = parser.parse_args(argv)

    analysis = analyze_file(args.fit_file)
    if args.json:
        print(json.dumps(_to_json_dict(analysis), indent=2))
    else:
        print(format_report(analysis))

    consistent = abs(
        analysis.total_lap_running_seconds()
        - analysis.covered_running_seconds()) < 1e-6
    return 0 if consistent else 1


if __name__ == '__main__':
    sys.exit(main())
