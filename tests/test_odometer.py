#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import contextlib
import io
import json
import os
import tempfile
import unittest

from fitdecode.cmd import fitodo
from fitdecode.odometer import (
    OdometerAudit,
    OdometerObservation,
    audit_odometer,
    iter_record_odometer,
)

from .test_reader import _generate_fitfile, _generate_messages

ROLLOVER = 1000
SCALE = 100  # 100 counts per meter


def obs(index, reading, timestamp, fit_file_index=0, frame_index=None):
    return OdometerObservation(
        index=index,
        fit_file_index=fit_file_index,
        frame_index=frame_index,
        timestamp=timestamp,
        reading=reading)


def budget(max_speed, elapsed):
    """Maximum plausible increment in counts for the given speed and time."""
    return max_speed * SCALE * elapsed


class AuditGapTestCase(unittest.TestCase):

    def test_increasing_gap_unique_when_restart_ruled_out(self):
        # budget covers the 100-count increase (300 counts) but not a fresh
        # counter that would already read 600 after a reset
        prev = obs(0, 500, 0)
        curr = obs(1, 600, 10)
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER, max_speed=0.3, scale=SCALE)

        self.assertEqual(len(audit), 1)
        gap = audit[0]
        self.assertEqual(gap.status, OdometerAudit.UNIQUE)
        self.assertEqual(gap.unique_explanation, ('rollover', 0))
        self.assertFalse(gap.wrapped)
        self.assertFalse(gap.restart_possible)
        self.assertEqual(tuple(gap.increment_candidates(ROLLOVER)), (100,))

    def test_increasing_gap_ambiguous_when_restart_fits(self):
        prev = obs(0, 100, 0)
        curr = obs(1, 200, 10)
        # 4500-count budget: five wrap counts (n = 0..4) plus a restart
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER, max_speed=4.5, scale=SCALE)

        gap = audit[0]
        self.assertEqual(gap.status, OdometerAudit.AMBIGUOUS)
        self.assertTrue(gap.restart_possible)
        self.assertEqual(gap.explanation_count, 6)  # 5 wrap counts + restart
        self.assertEqual(
            tuple(gap.increment_candidates(ROLLOVER)),
            (100, 1100, 2100, 3100, 4100))

    def test_wrapped_gap_keeps_every_turn_not_just_the_shortest(self):
        # reading goes from 950 back to 350: modular delta is 400, and the
        # 4500-count budget leaves room for one to four turns
        prev = obs(0, 950, 0)
        curr = obs(1, 350, 10)
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER, max_speed=4.5, scale=SCALE)

        gap = audit[0]
        self.assertTrue(gap.wrapped)
        self.assertEqual(gap.delta, 400)
        self.assertEqual(gap.status, OdometerAudit.AMBIGUOUS)
        # n=0 is excluded: a decreased reading requires at least one wrap
        self.assertEqual(gap.rollovers, (1, 2, 3, 4))
        self.assertEqual(
            tuple(gap.increment_candidates(ROLLOVER)),
            (1400, 2400, 3400, 4400))
        # the restart hypothesis survives as well
        self.assertTrue(gap.restart_possible)
        self.assertEqual(gap.explanation_count, 5)

    def test_wrapped_gap_can_only_be_unique_as_a_restart(self):
        # one turn (1400 counts) does not fit the 1000-count budget, but the
        # fresh reading (350 counts) does
        prev = obs(0, 950, 0)
        curr = obs(1, 350, 10)
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER, max_speed=1.0, scale=SCALE)

        gap = audit[0]
        self.assertEqual(budget(1.0, 10), 1000)
        self.assertEqual(gap.status, OdometerAudit.UNIQUE)
        self.assertEqual(gap.unique_explanation, ('restart',))
        self.assertEqual(gap.rollovers, ())
        self.assertTrue(gap.restart_possible)

    def test_contradictory_pair_is_identified(self):
        # neither a wrap (1400 counts) nor a fresh start (350 counts) fits the
        # 300-count budget
        prev = obs(0, 950, 0)
        curr = obs(1, 350, 10)
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER, max_speed=0.3, scale=SCALE)

        gap = audit[0]
        self.assertEqual(budget(0.3, 10), 300)
        self.assertEqual(gap.status, OdometerAudit.IMPOSSIBLE)
        self.assertFalse(audit.is_consistent)
        self.assertEqual(audit.impossible_gaps, (gap,))

        # the two conflicting observations are pointed at explicitly
        self.assertIs(gap.prev, prev)
        self.assertIs(gap.curr, curr)
        self.assertEqual((gap.prev.index, gap.curr.index), (0, 1))
        self.assertFalse(gap.restart_possible)

    def test_contradiction_in_longer_series_names_the_right_observations(self):
        # 150-count budget per 10s gap: the 200 -> 180 decrease cannot be a
        # wrap (one turn costs 1980) nor a restart (fresh reading 180 > 150)
        observations = (
            obs(0, 100, 0),
            obs(1, 200, 10),
            obs(2, 180, 20),   # impossible jump with the tight budget
            obs(3, 190, 30))
        audit = audit_odometer(
            observations, rollover=ROLLOVER, max_speed=0.15, scale=SCALE)

        self.assertEqual(len(audit.impossible_gaps), 1)
        gap = audit.impossible_gaps[0]
        self.assertEqual((gap.prev.index, gap.curr.index), (1, 2))
        self.assertEqual((gap.prev.reading, gap.curr.reading), (200, 180))

    def test_unbounded_gap_is_not_guessed(self):
        prev = obs(0, 900, 0)
        curr = obs(1, 100, 10)

        # no max speed: nothing can be ruled out
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER)
        gap = audit[0]
        self.assertFalse(gap.bounded)
        self.assertEqual(gap.status, OdometerAudit.AMBIGUOUS)
        self.assertTrue(gap.restart_possible)
        self.assertIsNone(gap.explanation_count)
        with self.assertRaises(ValueError):
            list(gap.increment_candidates(ROLLOVER))

        # a missing timestamp has the same effect even with a max speed
        prev_no_ts = obs(0, 900, None)
        curr_no_ts = obs(1, 100, None)
        audit = audit_odometer(
            (prev_no_ts, curr_no_ts),
            rollover=ROLLOVER, max_speed=5.0, scale=SCALE)
        self.assertFalse(audit[0].bounded)
        self.assertEqual(audit[0].status, OdometerAudit.AMBIGUOUS)

    def test_zero_increment(self):
        prev = obs(0, 500, 0)
        curr = obs(1, 500, 10)
        # 500-count budget: a full extra turn (1000) does not fit, but a reset
        # reaching 500 does
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER, max_speed=0.5, scale=SCALE)

        gap = audit[0]
        self.assertEqual(gap.delta, 0)
        self.assertFalse(gap.wrapped)
        self.assertEqual(gap.status, OdometerAudit.AMBIGUOUS)
        self.assertEqual(gap.rollovers, (0,))
        self.assertTrue(gap.restart_possible)

    def test_invalid_arguments(self):
        with self.assertRaises(ValueError):
            audit_odometer(
                (obs(0, ROLLOVER, 0),), rollover=ROLLOVER)

        with self.assertRaises(ValueError):
            audit_odometer(
                (obs(0, -1, 0),), rollover=ROLLOVER)

        with self.assertRaises(ValueError):
            audit_odometer(
                (obs(0, 1, 0), obs(1, 2, 1)),
                rollover=ROLLOVER, max_speed=1.0)  # scale missing

    def test_backwards_timestamp_is_treated_as_unbounded(self):
        # a negative elapsed time cannot be bounded by a speed: the gap is
        # reported as undetermined instead of raising
        prev = obs(0, 200, 10)
        curr = obs(1, 100, 9)
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER, max_speed=1.0, scale=SCALE)

        gap = audit[0]
        self.assertEqual(gap.elapsed, -1)
        self.assertFalse(gap.is_time_ordered)
        self.assertFalse(gap.bounded)
        self.assertEqual(gap.status, OdometerAudit.AMBIGUOUS)
        self.assertTrue(gap.restart_possible)

    def test_zero_elapsed_time_is_treated_as_unbounded(self):
        # two records sharing a 1s timestamp cannot be bounded by a speed, even
        # though the reading moved within that second
        prev = obs(0, 100, 10)
        curr = obs(1, 180, 10)
        audit = audit_odometer(
            (prev, curr), rollover=ROLLOVER, max_speed=0.1, scale=SCALE)

        gap = audit[0]
        self.assertEqual(gap.elapsed, 0)
        self.assertTrue(gap.is_time_ordered)
        self.assertFalse(gap.bounded)
        self.assertEqual(gap.status, OdometerAudit.AMBIGUOUS)
        self.assertTrue(gap.restart_possible)

    def test_container_helpers(self):
        # 150-count budget per 10s gap
        observations = (
            obs(0, 100, 0),
            obs(1, 200, 10),    # unique (a reset reaching 200 does not fit)
            obs(2, 180, 20),    # impossible
            obs(3, 190, 30))    # unique
        audit = audit_odometer(
            observations, rollover=ROLLOVER, max_speed=0.15, scale=SCALE)

        self.assertEqual(len(audit), 3)
        self.assertEqual(len(audit.observations), 4)
        self.assertEqual(len(audit.unique_gaps), 2)
        self.assertEqual(len(audit.ambiguous_gaps), 0)
        self.assertEqual(len(audit.impossible_gaps), 1)
        self.assertFalse(audit.is_consistent)
        self.assertEqual([obs.index for obs in audit.observations], [0, 1, 2, 3])


class RecordExtractionTestCase(unittest.TestCase):

    def _build_fit(self):
        records = _generate_messages(
            mesg_num=20, local_mesg_num=0, field_defs=[
                # timestamp (253, uint32), distance (5, uint32)
                (253, 'uint32'), (5, 'uint32')],
            data=[
                [100, 5000],
                [101, 5100],
                [102, 0xFFFFFFFF],  # invalid distance: must be skipped
                [103, 5300],
                [104, 200]])        # reading decreased (wrap or restart)
        return _generate_fitfile(records)

    def test_iter_record_odometer(self):
        fit_data = self._build_fit()
        observations = tuple(iter_record_odometer(fit_data))

        self.assertEqual(
            [obs.reading for obs in observations],
            [5000, 5100, 5300, 200])
        self.assertEqual(
            [obs.timestamp for obs in observations],
            [100, 101, 103, 104])
        self.assertEqual([obs.index for obs in observations], [0, 1, 2, 3])
        self.assertEqual(
            {obs.fit_file_index for obs in observations}, {0})
        self.assertTrue(
            all(isinstance(obs.frame_index, int) for obs in observations))

    def test_end_to_end_audit(self):
        fit_data = self._build_fit()
        observations = tuple(iter_record_odometer(fit_data))
        # use a small rollover so a single 1s gap can actually contain a wrap,
        # keeping both the rollover and restart hypotheses alive
        audit = audit_odometer(
            observations,
            rollover=10000, max_speed=200.0, scale=100)

        # the last reading (200) is smaller than 5300, so the final gap is
        # wrapped; both one rollover and a restart fit the 2000-count budget
        last_gap = audit[-1]
        self.assertTrue(last_gap.wrapped)
        self.assertEqual(last_gap.status, OdometerAudit.AMBIGUOUS)
        self.assertTrue(last_gap.restart_possible)
        self.assertEqual(last_gap.rollovers, (1,))
        self.assertEqual(
            tuple(last_gap.increment_candidates(10000)), (14900,))


class CliTestCase(unittest.TestCase):

    def _write_fit(self, fit_data):
        tmp = tempfile.NamedTemporaryFile(
            suffix='.fit', delete=False)
        try:
            tmp.write(fit_data)
            tmp.flush()
            tmp.close()
            return tmp.name
        finally:
            tmp.close()

    def test_cli_reports_contradiction_exit_code(self):
        records = _generate_messages(
            mesg_num=20, local_mesg_num=0, field_defs=[
                (253, 'uint32'), (5, 'uint32')],
            data=[
                [100, 950],
                [110, 350]])  # 10s gap, impossible under a tight speed limit
        fit_path = self._write_fit(_generate_fitfile(records))

        try:
            exit_code = fitodo.main([
                fit_path, '--rollover', '1000', '--scale', '100',
                '--max-speed', '0.03'])
        finally:
            os.unlink(fit_path)

        self.assertEqual(exit_code, fitodo.EXIT_CONTRADICTION)

    def test_cli_json_report(self):
        records = _generate_messages(
            mesg_num=20, local_mesg_num=0, field_defs=[
                (253, 'uint32'), (5, 'uint32')],
            data=[
                [100, 500],
                [110, 600]])
        fit_path = self._write_fit(_generate_fitfile(records))

        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                exit_code = fitodo.main([
                    fit_path, '--json', '--rollover', '1000',
                    '--scale', '100', '--max-speed', '0.3'])
        finally:
            os.unlink(fit_path)

        self.assertEqual(exit_code, 0)
        report = json.loads(output.getvalue())
        file_report = report['fit_files'][0]
        self.assertTrue(file_report['consistent'])
        self.assertEqual(file_report['gaps'][0]['status'], 'unique')


if __name__ == '__main__':
    unittest.main()
