#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import datetime
import importlib.util
import os.path

import pytest

import fitdecode  # noqa: F401  (ensures the package is importable)

TOOLS_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'tools')
TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), 'files')

_spec = importlib.util.spec_from_file_location(
    'lap_timer_stats', os.path.join(TOOLS_DIR, 'lap_timer_stats.py'))
lap_timer_stats = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(lap_timer_stats)

EPOCH = datetime.datetime(2020, 1, 1, tzinfo=datetime.timezone.utc)


def _ts(seconds):
    return EPOCH + datetime.timedelta(seconds=seconds)


def _events(*kinds_with_ts):
    return [
        lap_timer_stats.TimerEvent(_ts(ts), kind)
        for kind, ts in kinds_with_ts]


def _lap(index, start, end):
    return lap_timer_stats.Lap(index, _ts(start), _ts(end))


def test_duplicate_stops_do_not_double_deduct():
    events = _events(
        ('start', 10), ('stop', 20), ('stop', 20), ('stop', 25),
        ('start', 30), ('stop', 40), ('stop', 41))
    intervals, head, unclosed = lap_timer_stats.build_running_intervals(
        events)
    assert intervals == [(_ts(10), _ts(20)), (_ts(30), _ts(40))]
    assert head is None
    assert unclosed is None


def test_duplicate_starts_are_ignored():
    events = _events(('start', 10), ('start', 15), ('stop', 20))
    intervals, _, _ = lap_timer_stats.build_running_intervals(events)
    assert intervals == [(_ts(10), _ts(20))]


def test_same_second_start_stop_yields_no_interval():
    events = _events(('start', 10), ('stop', 10), ('start', 20), ('stop', 30))
    intervals, _, _ = lap_timer_stats.build_running_intervals(events)
    assert intervals == [(_ts(20), _ts(30))]


def test_head_running_without_start_is_not_back_filled():
    events = _events(('stop', 100), ('start', 200), ('stop', 300))
    intervals, head, unclosed = lap_timer_stats.build_running_intervals(
        events)
    assert intervals == [(_ts(200), _ts(300))]
    assert head == _ts(100)
    assert unclosed is None


def test_unclosed_interval_at_end_of_data():
    events = _events(('start', 10), ('stop', 20), ('start', 30))
    intervals, _, unclosed = lap_timer_stats.build_running_intervals(events)
    assert intervals == [(_ts(10), _ts(20))]
    assert unclosed == _ts(30)


def test_pause_spanning_lap_boundary_is_split():
    events = _events(
        ('start', 0), ('stop', 100), ('start', 200), ('stop', 300))
    laps = [_lap(1, 0, 150), _lap(2, 150, 300)]
    analysis = lap_timer_stats.analyze(events, laps)
    assert analysis.lap_intervals[1] == [(_ts(0), _ts(100))]
    assert analysis.lap_intervals[2] == [(_ts(200), _ts(300))]
    assert analysis.lap_running_seconds(laps[0]) == 100.0
    assert analysis.lap_running_seconds(laps[1]) == 100.0
    assert analysis.total_lap_running_seconds() == \
        analysis.covered_running_seconds()


def test_running_time_outside_lap_coverage_is_unassignable():
    events = _events(('start', 0), ('stop', 100))
    laps = [_lap(1, 10, 50), _lap(2, 60, 90)]
    analysis = lap_timer_stats.analyze(events, laps)
    assert analysis.uncovered_intervals == [
        (_ts(0), _ts(10)), (_ts(50), _ts(60)), (_ts(90), _ts(100))]
    assert analysis.coverage_gaps == [(_ts(50), _ts(60))]
    assert analysis.total_lap_running_seconds() == \
        analysis.covered_running_seconds() == 70.0


@pytest.mark.parametrize('filename', [
    'sample-activity.fit',
    'garmin-edge-500-activity.fit',
    'activity-small-fenix2-run.fit',
])
def test_real_files_are_self_consistent(filename):
    analysis = lap_timer_stats.analyze_file(
        os.path.join(TEST_FILES_DIR, filename))
    assert analysis.laps
    assert analysis.total_lap_running_seconds() == pytest.approx(
        analysis.covered_running_seconds())


def test_edge500_pause_spans_laps_3_and_4():
    analysis = lap_timer_stats.analyze_file(
        os.path.join(TEST_FILES_DIR, 'garmin-edge-500-activity.fit'))
    lap3, lap4 = analysis.laps[2], analysis.laps[3]
    # paused from 14:38:40 (inside lap 3) to 14:56:11 (inside lap 4):
    # the pause must be split at the lap boundary, not lost or duplicated
    assert lap3.end == lap4.start
    last3 = analysis.lap_intervals[lap3.index][-1]
    first4 = analysis.lap_intervals[lap4.index][0]
    assert (lap3.end - last3[1]).total_seconds() == 32.0
    assert (first4[0] - lap4.start).total_seconds() == 1019.0
    # duplicate stops at 16:31:50/16:31:54 must not double-deduct
    assert analysis.total_lap_running_seconds() == 10641.0
