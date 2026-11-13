"""
Tests for clock synchronisation between a reference track and a device track.
Run with:
    python -m unittest test_time_sync
"""

import datetime as mod_datetime
import unittest as mod_unittest

import gpxpy.gpx as mod_gpx
import gpxpy.time_sync as mod_time_sync

from typing import Any, List, Optional

UTC = mod_datetime.timezone.utc


def dt(minute: int = 0, second: float = 0, day: int = 1) -> mod_datetime.datetime:
    return mod_datetime.datetime(2026, 1, day, 8, 0, tzinfo=UTC) + mod_datetime.timedelta(minutes=minute, seconds=second)


class TimeSyncTests(mod_unittest.TestCase):

    def make_gpx(self, segments: List[List[mod_datetime.datetime]]) -> mod_gpx.GPX:
        gpx = mod_gpx.GPX()
        track = mod_gpx.GPXTrack()
        gpx.tracks.append(track)
        for times in segments:
            segment = mod_gpx.GPXTrackSegment()
            for index, time in enumerate(times):
                segment.points.append(mod_gpx.GPXTrackPoint(45.0, 13.0 + index * 1e-5, time=time))
            track.segments.append(segment)
        return gpx

    def test_single_offset_is_recovered(self) -> None:
        event = mod_time_sync.SyncEvent('press-1', dt(second=100), dt(second=100 - 35))
        calibration = mod_time_sync.calibrate_clock(
            [event, mod_time_sync.SyncEvent('press-2', dt(5, 200), dt(5, 200 - 35))],
            tolerance_seconds=0.5,
        )
        model = calibration.nominal_model
        self.assertAlmostEqual(model.offset_seconds, 0.0, places=6)
        self.assertAlmostEqual(model.rate, 0.0, places=9)

        result = calibration.calibrated_time_range(dt(2))
        self.assertAlmostEqual((result.earliest - dt(2)).total_seconds(), 35.0, delta=0.51)
        self.assertAlmostEqual((result.latest - dt(2)).total_seconds(), 35.0, delta=0.51)
        self.assertEqual((dt(2),), result.minutes)

    def test_rate_is_fitted_from_two_events(self) -> None:
        # Device runs 2% fast: after 30 minutes it is 36 seconds ahead.
        calibration = mod_time_sync.calibrate_clock([
            mod_time_sync.SyncEvent('a', dt(0), dt(0)),
            mod_time_sync.SyncEvent('b', dt(30), dt(30, 36)),
        ], tolerance_seconds=0.01)

        low, high = calibration.rate_range
        self.assertLess(low, high)
        self.assertLessEqual(low, -0.0196)
        self.assertGreaterEqual(high, -0.0196)
        self.assertAlmostEqual(calibration.nominal_model.rate, -0.0196, delta=0.001)

    def test_tolerance_is_inclusive(self) -> None:
        events = [
            mod_time_sync.SyncEvent('a', dt(0), dt(0)),
            mod_time_sync.SyncEvent('b', dt(10), dt(10, 2)),
        ]
        # Reading differs by 2 s; a tolerance of exactly 1 s cannot absorb it
        # at both ends unless the rate is free, but it must still bound the fit.
        calibration = mod_time_sync.calibrate_clock(events, tolerance_seconds=1.0)
        for event_id, residual in calibration.residuals():
            self.assertLessEqual(residual, 1.0 + 1e-6, event_id)

        with self.assertRaises(mod_time_sync.InconsistentSyncRecords):
            # Two events with the same device time but reference times
            # 10 s apart cannot both be matched within 1 s.
            mod_time_sync.calibrate_clock([
                mod_time_sync.SyncEvent('c', dt(0, 0), dt(4)),
                mod_time_sync.SyncEvent('d', dt(0, 10), dt(4)),
            ], tolerance_seconds=1.0, max_rate_deviation=0.05)

    def test_per_event_tolerance_is_used(self) -> None:
        calibration = mod_time_sync.calibrate_clock([
            mod_time_sync.SyncEvent('a', dt(0), dt(0), tolerance_seconds=10),
            mod_time_sync.SyncEvent('b', dt(10), dt(10), tolerance_seconds=1),
        ])
        self.assertEqual(10, calibration.tolerance_for(calibration.events[0]))
        self.assertEqual(1, calibration.tolerance_for(calibration.events[1]))
        for event_id, residual in calibration.residuals():
            self.assertLessEqual(residual, 10.0 + 1e-6, event_id)

    def test_single_event_time_cannot_identify_rate(self) -> None:
        with self.assertRaises(mod_time_sync.UnidentifiableClockRate):
            mod_time_sync.calibrate_clock([
                mod_time_sync.SyncEvent('a', dt(0, 5), dt(1, 5)),
                mod_time_sync.SyncEvent('b', dt(0, 50), dt(1, 5)),
            ], tolerance_seconds=5)

    def test_explicit_rate_bound_allows_single_instant(self) -> None:
        calibration = mod_time_sync.calibrate_clock(
            [mod_time_sync.SyncEvent('a', dt(1), dt(30))],
            tolerance_seconds=2,
            max_rate_deviation=0.01,
        )
        low, high = calibration.rate_range
        self.assertAlmostEqual(-0.01, low, places=9)
        self.assertAlmostEqual(0.01, high, places=9)

    def test_ambiguous_minute_keeps_both_candidates(self) -> None:
        calibration = mod_time_sync.calibrate_clock(
            [mod_time_sync.SyncEvent('only', dt(0), dt(0), tolerance_seconds=20)],
            max_rate_deviation=0.01,
        )
        result = calibration.calibrated_time_range(dt(0, 20))
        self.assertEqual({dt(-1, 0), dt(0)}, set(result.minutes))
        self.assertTrue(result.ambiguous)

    def test_unambiguous_minute_has_one_candidate(self) -> None:
        calibration = mod_time_sync.calibrate_clock(
            [mod_time_sync.SyncEvent('only', dt(0), dt(0), tolerance_seconds=10)],
            max_rate_deviation=0.01,
        )
        result = calibration.calibrated_time_range(dt(0, 20))
        self.assertEqual((dt(0),), result.minutes)
        self.assertFalse(result.ambiguous)

    def test_restart_does_not_reuse_previous_slope(self) -> None:
        # Session 0: device 10 s ahead of the reference (rate 0).
        first_times = [dt(0, 0), dt(5, 0), dt(10, 0)]
        # Session 1 after restart: device 300 s ahead at its start and 2% fast.
        second_times = [dt(10, 0), dt(15, 0), dt(20, 0)]
        gpx = self.make_gpx([first_times, second_times])

        def session_one_reference(device_seconds: float) -> mod_datetime.datetime:
            return dt(0) + mod_datetime.timedelta(
                seconds=device_seconds + 300.0 + 0.02 * (device_seconds - 600.0))

        events = [
            mod_time_sync.SyncEvent('s0-a', dt(0, 10), dt(0, 0), session=(0, 0)),
            mod_time_sync.SyncEvent('s0-b', dt(10, 10), dt(10, 0), session=(0, 0)),
            mod_time_sync.SyncEvent('s1-a', session_one_reference(600), dt(10, 0), session=(0, 1)),
            mod_time_sync.SyncEvent('s1-b', session_one_reference(1200), dt(20, 0), session=(0, 1)),
        ]
        result = mod_time_sync.calibrate_gpx(gpx, events, tolerance_seconds=1)

        self.assertEqual(2, len(result.calibrations))
        self.assertEqual(6, len(result.calibrated_points))

        first = result.calibrations[(0, 0)]
        second = result.calibrations[(0, 1)]
        self.assertAlmostEqual(first.nominal_model.rate, 0.0, delta=0.005)
        self.assertAlmostEqual(second.nominal_model.rate, 0.02, delta=0.005)

        second_point = result.point_for(gpx.tracks[0].segments[1].points[0])
        assert second_point is not None
        self.assertEqual((0, 1), second_point.session)
        self.assertAlmostEqual((second_point.nominal_time - dt(10, 0)).total_seconds(),
                               300.0, delta=1.0)

        # Reusing the first session's model would put this point at the wrong
        # offset: the calibrated answer does not lie inside the first fit.
        self.assertFalse(first.contains(second.nominal_model.offset_seconds,
                                        second.nominal_model.rate))

    def test_restart_inside_one_segment_is_split(self) -> None:
        times = [dt(0), dt(5), dt(10), dt(20), dt(25), dt(30)]
        gpx = self.make_gpx([times])
        events = [
            mod_time_sync.SyncEvent('p1', dt(0, 10), dt(0), session='s1'),
            mod_time_sync.SyncEvent('p2', dt(10, 10), dt(10), session='s1'),
            mod_time_sync.SyncEvent('p3', dt(20, 700), dt(20), session='s2'),
            mod_time_sync.SyncEvent('p4', dt(30, 704), dt(30), session='s2'),
        ]
        result = mod_time_sync.calibrate_gpx(gpx, events, tolerance_seconds=1)
        self.assertEqual({'s1', 's2'}, set(result.calibrations))
        self.assertEqual(6, len(result.calibrated_points))

        point_results = [result.point_for(point) for point in gpx.tracks[0].segments[0].points]
        self.assertTrue(all(item is not None for item in point_results))
        sessions = [item.session for item in point_results if item is not None]
        self.assertEqual(['s1', 's1', 's1', 's2', 's2', 's2'], sessions)
        second_session = result.calibrations['s2']
        # The session's anchor is its first paired press, so its clock offset
        # is relative to that press (about 0), not to the trip's first press.
        self.assertAlmostEqual(
            second_session.calibrated_time(dt(20)).timestamp(),
            dt(20, 700).timestamp(), delta=1.0)
        # Small drift is still feasible; both end presses are within 1 s of
        # their stated readings, so a nominal residual of up to ~1 s is fine.
        self.assertLessEqual(
            abs((second_session.calibrated_time(dt(30)) - dt(30)).total_seconds() - 704.0),
            1.5)

    def test_segment_with_no_events_is_not_extrapolated(self) -> None:
        gpx = self.make_gpx([[dt(0, 0), dt(0, 30)], [dt(40, 0), dt(40, 30)]])
        events = [
            mod_time_sync.SyncEvent('only-1', dt(0), dt(0, 5), session=(0, 0)),
            mod_time_sync.SyncEvent('only-2', dt(0, 20), dt(0, 25), session=(0, 0)),
        ]
        result = mod_time_sync.calibrate_gpx(gpx, events, tolerance_seconds=0.5)

        self.assertEqual(2, len(result.calibrated_points))
        self.assertEqual(2, len(result.skipped_points))
        for skipped in result.skipped_points:
            self.assertEqual(1, skipped.segment_no)
            self.assertIn('no paired sync events', skipped.reason)

    def test_inconsistent_session_can_be_skipped(self) -> None:
        gpx = self.make_gpx([[dt(0, 0), dt(0, 30)]])
        events = [
            mod_time_sync.SyncEvent('a', dt(0), dt(0)),
            mod_time_sync.SyncEvent('b', dt(0, 10), dt(0)),
        ]
        result = mod_time_sync.calibrate_gpx(gpx, events, tolerance_seconds=1,
                                             on_inconsistent='skip')
        self.assertEqual(0, len(result.calibrated_points))
        self.assertEqual(2, len(result.skipped_points))
        self.assertEqual(0, len(result.calibrations))

    def test_points_without_device_time_are_skipped(self) -> None:
        gpx = self.make_gpx([[dt(0, 0), dt(0, 30)]])
        gpx.tracks[0].segments[0].points[1].time = None
        events = [
            mod_time_sync.SyncEvent('a', dt(0), dt(0)),
            mod_time_sync.SyncEvent('b', dt(0, 30), dt(0, 30)),
        ]
        result = mod_time_sync.calibrate_gpx(gpx, events, tolerance_seconds=1)
        self.assertEqual(1, len(result.calibrated_points))
        self.assertEqual(1, len(result.skipped_points))
        self.assertIn('no device time', result.skipped_points[0].reason)

    def test_apply_calibrated_times_does_not_change_source_when_cloning(self) -> None:
        gpx = self.make_gpx([[dt(0, 0), dt(0, 30)]])
        events = [
            mod_time_sync.SyncEvent('a', dt(0), dt(0)),
            mod_time_sync.SyncEvent('b', dt(0, 30), dt(0, 30)),
        ]
        result = mod_time_sync.calibrate_gpx(gpx, events, tolerance_seconds=0.1)
        copy = mod_time_sync.apply_calibrated_times(gpx, result, in_place=False)
        self.assertEqual(dt(0, 0), copy.tracks[0].segments[0].points[0].time)

        mod_time_sync.apply_calibrated_times(gpx, result, in_place=True)
        self.assertEqual(dt(0, 0), gpx.tracks[0].segments[0].points[0].time)

    def test_work_order_candidates_keep_both_minutes(self) -> None:
        gpx = self.make_gpx([[dt(0, 0)]])
        events = [mod_time_sync.SyncEvent('only', dt(0), dt(0), tolerance_seconds=30)]
        result = mod_time_sync.calibrate_gpx(
            gpx, events, max_rate_deviation=0.01)

        orders = [
            mod_time_sync.WorkOrder('wo-7-59', dt(-1)),
            mod_time_sync.WorkOrder('wo-8-00', dt(0)),
            mod_time_sync.WorkOrder('wo-8-05', dt(5)),
        ]
        candidates = mod_time_sync.match_work_orders(result, orders)
        self.assertEqual(1, len(candidates))
        self.assertEqual(('wo-7-59', 'wo-8-00'), candidates[0].work_order_ids)
        self.assertTrue(candidates[0].ambiguous)

    def test_work_orders_far_from_points_are_not_matched(self) -> None:
        gpx = self.make_gpx([[dt(0, 0), dt(3, 0)]])
        events = [
            mod_time_sync.SyncEvent('a', dt(0), dt(0), tolerance_seconds=0.5),
            mod_time_sync.SyncEvent('b', dt(3), dt(3), tolerance_seconds=0.5),
        ]
        result = mod_time_sync.calibrate_gpx(gpx, events)
        orders = [mod_time_sync.WorkOrder('wo-late', dt(30))]
        self.assertEqual([], mod_time_sync.match_work_orders(result, orders))

    def test_parse_handover_records(self) -> None:
        records = [
            {'id': 'b1', 'reference': '2026-01-01T08:00:00Z', 'device': '2026-01-01T07:59:50Z', 'tolerance': 2},
            ('b2', '2026-01-01T08:10:00Z', '2026-01-01T08:09:50Z'),
        ]
        events = mod_time_sync.parse_sync_events(records, session='trip-1')
        self.assertEqual(2, len(events))
        self.assertEqual('b1', events[0].event_id)
        self.assertEqual(2, events[0].tolerance_seconds)
        self.assertEqual('trip-1', events[0].session)
        self.assertIsNone(events[1].tolerance_seconds)

    def test_parse_work_order_records(self) -> None:
        orders = mod_time_sync.parse_work_orders([
            {'id': 'wo-1', 'minute': '2026-01-01T08:05:00Z'},
            ('wo-2', mod_datetime.datetime(2026, 1, 1, 8, 7, tzinfo=UTC)),
        ])
        self.assertEqual(['wo-1', 'wo-2'], [order.work_order_id for order in orders])

    def test_naive_datetimes_are_supported(self) -> None:
        calibration = mod_time_sync.calibrate_clock([
            mod_time_sync.SyncEvent('a',
                                    mod_datetime.datetime(2026, 1, 1, 8, 0),
                                    mod_datetime.datetime(2026, 1, 1, 7, 59, 50)),
            mod_time_sync.SyncEvent('b',
                                    mod_datetime.datetime(2026, 1, 1, 8, 10),
                                    mod_datetime.datetime(2026, 1, 1, 8, 9, 50)),
        ], tolerance_seconds=0.5)
        self.assertIsNone(calibration.calibrated_time_range(
            mod_datetime.datetime(2026, 1, 1, 8, 5)).earliest.tzinfo)

    def test_mixed_timezone_kinds_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            mod_time_sync.calibrate_clock([
                mod_time_sync.SyncEvent('a', dt(0), mod_datetime.datetime(2026, 1, 1, 8, 0)),
                mod_time_sync.SyncEvent('b', dt(10), dt(10)),
            ], tolerance_seconds=1)

    def test_events_from_different_sessions_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            mod_time_sync.calibrate_clock([
                mod_time_sync.SyncEvent('a', dt(0), dt(0), session='s0'),
                mod_time_sync.SyncEvent('b', dt(10), dt(10), session='s1'),
            ], tolerance_seconds=1)

    def test_unkeyed_events_are_assigned_to_nearest_segment(self) -> None:
        gpx = self.make_gpx([[dt(0, 0), dt(0, 30)], [dt(60, 0), dt(60, 30)]])
        events = [
            mod_time_sync.SyncEvent('a', dt(0), dt(0, 5)),
            mod_time_sync.SyncEvent('b', dt(0, 30), dt(0, 35)),
            mod_time_sync.SyncEvent('c', dt(60), dt(60, 5)),
            mod_time_sync.SyncEvent('d', dt(60, 30), dt(60, 35)),
        ]
        result = mod_time_sync.calibrate_gpx(gpx, events, tolerance_seconds=0.5)
        self.assertEqual(4, len(result.calibrated_points))
        self.assertEqual({(0, 0), (0, 1)}, set(result.calibrations))

        first_two = [result.point_for(point) for point in gpx.tracks[0].segments[0].points]
        self.assertTrue(all(item is not None and item.session == (0, 0) for item in first_two))


if __name__ == '__main__':
    mod_unittest.main()
