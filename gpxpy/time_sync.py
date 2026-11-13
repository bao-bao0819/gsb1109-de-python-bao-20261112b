# Copyright 2011 Tomo Krajina
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

"""
Clock synchronisation between a reference track and a device track.

A typical use case is a handheld recorder and a car logger that both record
the same trip.  A handover list pairs a few button presses: the same press has
a time on the reference (handheld) clock and a time on the device (car) clock.
After every power-on the device clock may have a different offset, and it may
also run at a slightly different rate.  The handover list supplies only the
matching tolerance, not the offset or the rate.

This module:

* groups paired events per **session** (one power-on period, represented by
  one GPX track segment unless a custom selector is used; a segment that
  contains an explicit restart is split by the event sessions),
* computes the complete set of affine clock models that satisfy *all* paired
  events of that session within their tolerances,
* maps every calibratable device point onto the reference time axis,
* reports a calibrated time **range** for each point and keeps *both* minute
  candidates when the range crosses a minute boundary, and
* associates the resulting minutes with minute-level work orders without
  discarding an alternative.

A model is::

    reference = anchor_reference
                + offset
                + (1 + rate) * (device - anchor_device)

The feasible set of ``(offset, rate)`` is an interval (a convex polygon), not
a single least-squares line.  A session is fitted independently, so a slope
estimated before a device restart is never reused for points recorded after
the restart.  Points are assigned to a session only by their own device time
and only inside one segment.  A session whose clock rate cannot be identified
from its paired events is reported as skipped instead of being calibrated
with an invented slope.

The module does not modify the GPX object.  Use :func:`apply_calibrated_times`
when the nominal times should be written back.
"""

import datetime as mod_datetime
import math as mod_math

from dataclasses import dataclass, field, replace
from typing import (Any, Callable, Dict, Hashable, Iterable, List, Optional,
                    Sequence, Tuple, Union)

from . import gpx as mod_gpx

__all__ = [
    'ClockSyncError',
    'InconsistentSyncRecords',
    'UnidentifiableClockRate',
    'SyncEvent',
    'ClockModel',
    'CalibratedTimeRange',
    'ClockCalibration',
    'CalibratedPoint',
    'SkippedPoint',
    'GPXCalibration',
    'WorkOrder',
    'WorkOrderMatch',
    'WorkOrderCandidates',
    'calibrate_clock',
    'fit_clock',
    'calibrate_track_point',
    'calibrate_gpx',
    'apply_calibrated_times',
    'associate_minute_work_orders',
    'associate_work_orders',
    'group_work_order_candidates',
    'parse_sync_events',
    'parse_work_orders',
    'calibrate_sessions',
    'calibrate_handover_track',
    'match_work_orders',
]

SessionKey = Hashable
Point2 = Tuple[float, float]

# Generous search rectangle for the offset.  A real clock offset is many
# orders of magnitude smaller than a century.  The polygon stays finite while
# each tolerance band is intersected with it.
_MAX_OFFSET_SECONDS = 100.0 * 366.0 * 24.0 * 3600.0

# When the caller supplies no device-specific rate bound, the rate is limited
# only by the paired events themselves (plus these numerical guards).  For two
# or more distinct device times the tolerance bands already bound the rate.
_MIN_RATE_SCALE = 1e-6
_MAX_RATE_SCALE = 1e6

_NAIVE_EPOCH = mod_datetime.datetime(1970, 1, 1)


class ClockSyncError(Exception):
    """Base exception for clock-synchronisation problems."""


class InconsistentSyncRecords(ClockSyncError):
    """Raised when no clock model can satisfy every paired event."""


class UnidentifiableClockRate(ClockSyncError):
    """Raised when the paired events cannot identify the clock rate."""


def _absolute_seconds(value: mod_datetime.datetime) -> float:
    if value.tzinfo is None:
        return (value - _NAIVE_EPOCH).total_seconds()
    return value.timestamp()


def _datetime_from_absolute(seconds: float, reference: mod_datetime.datetime) -> mod_datetime.datetime:
    tz = reference.tzinfo
    if tz is None:
        return _NAIVE_EPOCH + mod_datetime.timedelta(seconds=seconds)
    return mod_datetime.datetime.fromtimestamp(seconds, mod_datetime.timezone.utc).astimezone(tz)


def _check_timestamp_kind(values: Sequence[mod_datetime.datetime], what: str) -> None:
    if not values:
        raise ValueError(f'at least one {what} is required')
    if len({value.tzinfo is not None for value in values}) > 1:
        raise ValueError(f'{what} must either all have a timezone or all be timezone-naive')


def _floor_minute_seconds(seconds: float) -> int:
    return int(mod_math.floor(seconds / 60.0)) * 60


def _minute_candidates(earliest: mod_datetime.datetime,
                       latest: mod_datetime.datetime) -> Tuple[mod_datetime.datetime, ...]:
    if latest < earliest:
        raise ValueError('calibrated time range has negative width')

    first_bucket = _floor_minute_seconds(_absolute_seconds(earliest))
    last_bucket = _floor_minute_seconds(_absolute_seconds(latest))
    buckets = range(first_bucket, last_bucket + 60, 60)
    return tuple(_datetime_from_absolute(float(bucket), earliest) for bucket in buckets)


def _feasible_vertices(constraints: Sequence[Tuple[float, float, float]]) -> List[Point2]:
    """Vertices of a bounded polygon given as ``a*x + b*y <= c`` constraints.

    Every vertex is the intersection of two boundary lines.  Enumerating the
    intersections and then filtering them against all constraints avoids the
    cumulative rounding drift of sequential polygon clipping and gives a very
    accurate representation of narrow feasible regions.
    """
    candidates: List[Point2] = []
    for index, (a1, b1, c1) in enumerate(constraints):
        for a2, b2, c2 in constraints[index + 1:]:
            determinant = a1 * b2 - a2 * b1
            if abs(determinant) < 1e-12:
                continue
            x = (c1 * b2 - c2 * b1) / determinant
            y = (a1 * c2 - a2 * c1) / determinant
            candidates.append((x, y))

    feasible: List[Point2] = []
    for x, y in candidates:
        if all(_satisfies(a, b, c, x, y) for a, b, c in constraints):
            feasible.append((x, y))
    return feasible


def _satisfies(a: float, b: float, c: float, x: float, y: float) -> bool:
    """Test one half-plane with a tolerance independent of epoch size."""
    residual = a * x + b * y - c
    scale = max(1.0, abs(a * x), abs(b * y), abs(c))
    return residual <= 1e-9 + 1e-13 * scale


def _deduplicate_vertices(polygon: Sequence[Point2]) -> List[Point2]:
    """Remove duplicates and return the vertices in cyclic (convex) order."""
    if not polygon:
        return []

    offset_scale = max(1.0, max(abs(vertex[0]) for vertex in polygon))
    rate_scale = max(1.0, max(abs(vertex[1]) for vertex in polygon))

    unique: List[Point2] = []
    for vertex in polygon:
        if not unique or all(
                abs(vertex[0] - existing[0]) > 1e-6
                or abs(vertex[1] - existing[1]) > 1e-12
                for existing in unique):
            unique.append(vertex)

    if len(unique) <= 2:
        return unique

    centre = (
        sum(vertex[0] for vertex in unique) / len(unique),
        sum(vertex[1] for vertex in unique) / len(unique),
    )
    return sorted(unique, key=lambda vertex: mod_math.atan2(
        (vertex[1] - centre[1]) / rate_scale,
        (vertex[0] - centre[0]) / offset_scale))


def _device_span(points: Sequence[mod_gpx.GPXTrackPoint]) -> Optional[Tuple[float, float]]:
    timed = [_absolute_seconds(point.time) for point in points if point.time is not None]
    if not timed:
        return None
    return min(timed), max(timed)


def _nearest_session(device_time: mod_datetime.datetime,
                     spans: Dict[SessionKey, Optional[Tuple[float, float]]],
                     event_id: str) -> SessionKey:
    device_seconds = _absolute_seconds(device_time)
    distances: List[Tuple[float, SessionKey]] = []
    for session, span in spans.items():
        if span is None:
            continue
        low, high = span
        if device_seconds < low:
            distance = low - device_seconds
        elif device_seconds > high:
            distance = device_seconds - high
        else:
            distance = 0.0
        distances.append((distance, session))

    if not distances:
        raise ValueError(f'sync event {event_id!r} cannot be assigned: no timed GPX segment')
    distances.sort(key=lambda item: item[0])
    if len(distances) > 1 and distances[0][0] == distances[1][0]:
        raise ValueError(
            f'sync event {event_id!r} lies equally close to sessions '
            f'{distances[0][1]!r} and {distances[1][1]!r}; set its session explicitly')
    return distances[0][1]


def _offset_extent_at_rate(polygon: Sequence[Point2], rate_scale: float) -> Tuple[float, float]:
    """Return the smallest and largest offset on the ``rate_scale`` line."""
    low: Optional[float] = None
    high: Optional[float] = None

    previous = polygon[-1]
    previous_side = previous[1] - rate_scale
    for current in polygon:
        current_side = current[1] - rate_scale
        if previous_side == 0:
            offsets = [previous[0]]
        elif current_side == 0:
            offsets = [current[0]]
        elif (previous_side < 0) != (current_side < 0):
            ratio = previous_side / (previous_side - current_side)
            offsets = [previous[0] + ratio * (current[0] - previous[0])]
        else:
            offsets = []

        for offset in offsets:
            low = offset if low is None else min(low, offset)
            high = offset if high is None else max(high, offset)

        previous = current
        previous_side = current_side

    if low is None or high is None:
        nearest = min(polygon, key=lambda vertex: abs(vertex[1] - rate_scale))
        return nearest[0], nearest[0]
    return low, high


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


@dataclass(frozen=True)
class SyncEvent:
    """One paired button press from the handover list.

    Parameters
    ----------
    event_id:
        Identifier of the pair in the handover list.  Must be unique within a
        session.
    reference_time:
        The press time on the reference (handheld) clock.
    device_time:
        The same press time on the device (car) clock.
    session:
        Power-on/recording session.  When omitted, :func:`calibrate_gpx` tries
        to assign the event from its device time; :func:`calibrate_clock`
        requires all its events to belong to the same session.
    tolerance_seconds:
        Tolerance for this pair.  When omitted, the default passed to the
        fitting function is used.  A handover list that provides one value
        per pair should populate this field.
    """

    event_id: str
    reference_time: mod_datetime.datetime
    device_time: mod_datetime.datetime
    session: Optional[SessionKey] = None
    tolerance_seconds: Optional[float] = None

    def __post_init__(self) -> None:
        if not self.event_id:
            raise ValueError('event_id cannot be empty')
        if self.tolerance_seconds is not None:
            if not mod_math.isfinite(self.tolerance_seconds) or self.tolerance_seconds < 0:
                raise ValueError('tolerance_seconds must be a finite non-negative number')


@dataclass(frozen=True)
class ClockModel:
    """An affine map from device time to the reference time axis."""

    anchor_reference: mod_datetime.datetime
    anchor_device: mod_datetime.datetime
    offset_seconds: float
    rate: float

    @property
    def offset(self) -> float:
        return self.offset_seconds

    @property
    def drift(self) -> float:
        return self.rate

    def apply(self, device_time: mod_datetime.datetime) -> mod_datetime.datetime:
        reference_seconds = _absolute_seconds(self.anchor_reference)
        device_seconds = _absolute_seconds(self.anchor_device)
        elapsed = _absolute_seconds(device_time) - device_seconds
        mapped = reference_seconds + self.offset_seconds + (1.0 + self.rate) * elapsed
        return _datetime_from_absolute(mapped, self.anchor_reference)


@dataclass(frozen=True)
class CalibratedTimeRange:
    """Every calibrated time permitted by all paired events and tolerances."""

    earliest: mod_datetime.datetime
    latest: mod_datetime.datetime

    @property
    def width_seconds(self) -> float:
        return _absolute_seconds(self.latest) - _absolute_seconds(self.earliest)

    @property
    def minutes(self) -> Tuple[mod_datetime.datetime, ...]:
        """Minute buckets touched by this range, in chronological order."""
        return _minute_candidates(self.earliest, self.latest)

    @property
    def minute_candidates(self) -> Tuple[mod_datetime.datetime, ...]:
        return self.minutes

    @property
    def ambiguous(self) -> bool:
        """True if the range spans more than one minute."""
        return len(self.minutes) > 1


@dataclass(frozen=True)
class ClockCalibration:
    """The complete feasible clock fit for one session.

    ``fit_region`` is a convex polygon in ``(offset, rate_scale)`` coordinates,
    where ``rate_scale == 1 + rate``.  All calculations use the whole region.
    """

    session: Optional[SessionKey]
    events: Tuple[SyncEvent, ...]
    default_tolerance_seconds: Optional[float]
    max_rate_deviation: Optional[float]
    anchor_reference: mod_datetime.datetime
    anchor_device: mod_datetime.datetime
    fit_region: Tuple[Point2, ...] = field(repr=False)
    constraints: Tuple[Tuple[float, float, float], ...] = field(repr=False)

    def tolerance_for(self, event: SyncEvent) -> float:
        if event.tolerance_seconds is not None:
            return event.tolerance_seconds
        if self.default_tolerance_seconds is not None:
            return self.default_tolerance_seconds
        raise ValueError(f'no tolerance for sync event {event.event_id!r}')

    @property
    def tolerance_seconds(self) -> Optional[float]:
        return self.default_tolerance_seconds

    @property
    def offset_range(self) -> Tuple[float, float]:
        offsets = [vertex[0] for vertex in self.fit_region]
        return min(offsets), max(offsets)

    @property
    def rate_range(self) -> Tuple[float, float]:
        scales = [vertex[1] - 1.0 for vertex in self.fit_region]
        return min(scales), max(scales)

    @property
    def nominal_model(self) -> ClockModel:
        """A feasible model with the least clock-rate deviation.

        This is a representative member of the feasible set, not the only
        correct model.  When several rates are possible, the closest to a
        steady clock is chosen and its offset is centred in the remaining
        allowance.
        """
        rate_scales = [vertex[1] for vertex in self.fit_region]
        rate_low = min(rate_scales)
        rate_high = max(rate_scales)
        selected_rate = _clamp(1.0, rate_low, rate_high)
        offset_low, offset_high = _offset_extent_at_rate(self.fit_region, selected_rate)
        return ClockModel(
            anchor_reference=self.anchor_reference,
            anchor_device=self.anchor_device,
            offset_seconds=(offset_low + offset_high) / 2.0,
            rate=selected_rate - 1.0,
        )

    def residuals(self, model: Optional[ClockModel] = None) -> List[Tuple[str, float]]:
        """Absolute residual (seconds) of each pair under a model."""
        selected = model or self.nominal_model
        anchor_reference = _absolute_seconds(self.anchor_reference)
        anchor_device = _absolute_seconds(self.anchor_device)
        values: List[Tuple[str, float]] = []
        for event in self.events:
            device_elapsed = _absolute_seconds(event.device_time) - anchor_device
            reference_elapsed = _absolute_seconds(event.reference_time) - anchor_reference
            mapped = selected.offset_seconds + (1.0 + selected.rate) * device_elapsed
            values.append((event.event_id, abs(mapped - reference_elapsed)))
        return values

    def calibrated_time_range(self, device_time: mod_datetime.datetime) -> CalibratedTimeRange:
        anchor_device_aware = self.anchor_device.tzinfo is not None
        if (device_time.tzinfo is not None) != anchor_device_aware:
            raise ValueError('device point time has a different timezone kind than the sync records')

        elapsed = _absolute_seconds(device_time) - _absolute_seconds(self.anchor_device)
        reference_seconds = _absolute_seconds(self.anchor_reference)
        values = [offset + elapsed * rate_scale for offset, rate_scale in self.fit_region]
        return CalibratedTimeRange(
            earliest=_datetime_from_absolute(reference_seconds + min(values), self.anchor_reference),
            latest=_datetime_from_absolute(reference_seconds + max(values), self.anchor_reference),
        )

    def calibrated_time(self, device_time: mod_datetime.datetime) -> mod_datetime.datetime:
        """Nominal calibrated time (a feasible centre of the region)."""
        return self.nominal_model.apply(device_time)

    def minute_candidates(self, device_time: mod_datetime.datetime) -> Tuple[mod_datetime.datetime, ...]:
        return self.calibrated_time_range(device_time).minutes

    def contains(self, offset_seconds: float, rate: float) -> bool:
        """Whether a concrete model satisfies every paired-event tolerance."""
        x, y = offset_seconds, 1.0 + rate
        return all(_satisfies(a, b, c, x, y) for a, b, c in self.constraints)


def calibrate_clock(events: Iterable[SyncEvent],
                    tolerance_seconds: Optional[float] = None,
                    max_rate_deviation: Optional[float] = None) -> ClockCalibration:
    """Fit every clock model consistent with all paired events.

    Parameters
    ----------
    events:
        Paired button events.  All must belong to the same session.
    tolerance_seconds:
        Default maximum absolute error per pair (inclusive).  Events may carry
        their own ``tolerance_seconds``; a per-event value takes precedence.
        At least one of the two must be provided for every event.
    max_rate_deviation:
        Optional explicit bound on the fractional clock-rate error, for
        example from a device specification.  When omitted, the paired events
        alone determine the rate.  It must not be invented merely to make a
        fit look tighter than the records allow.

    Raises
    ------
    InconsistentSyncRecords
        If the intersection of all tolerance bands is empty.
    UnidentifiableClockRate
        If fewer than two distinct device times are represented and no rate
        bound was supplied.  A single instant cannot identify a clock rate.
    ValueError
        For malformed input (mixed timezone awareness, duplicate ids,
        events from different sessions, invalid tolerances, etc.).
    """
    event_list = list(events)
    if not event_list:
        raise ValueError('at least one sync event is required')
    if tolerance_seconds is not None and (
            not mod_math.isfinite(tolerance_seconds) or tolerance_seconds < 0):
        raise ValueError('tolerance_seconds must be a finite non-negative number')
    if max_rate_deviation is not None and (
            not mod_math.isfinite(max_rate_deviation) or not 0 <= max_rate_deviation < 1):
        raise ValueError('max_rate_deviation must be finite and in [0, 1)')

    sessions = {event.session for event in event_list}
    if len(sessions) > 1:
        raise ValueError('calibrate_clock() accepts one session; group events by session first')

    event_ids = [event.event_id for event in event_list]
    if len(set(event_ids)) != len(event_ids):
        raise ValueError('sync event ids must be unique within a session')

    _check_timestamp_kind([event.reference_time for event in event_list], 'reference times')
    _check_timestamp_kind([event.device_time for event in event_list], 'device times')

    for event in event_list:
        if event.tolerance_seconds is None and tolerance_seconds is None:
            raise ValueError(f'no tolerance for sync event {event.event_id!r}')

    ordered = tuple(sorted(event_list,
                           key=lambda event: (_absolute_seconds(event.device_time), event.event_id)))
    anchor_event = ordered[0]
    anchor_reference_seconds = _absolute_seconds(anchor_event.reference_time)
    anchor_device_seconds = _absolute_seconds(anchor_event.device_time)

    constraints: List[Tuple[float, float, float]] = []
    event_constraints: List[Tuple[float, float, float]] = []
    for event in ordered:
        tolerance = event.tolerance_seconds if event.tolerance_seconds is not None else tolerance_seconds
        assert tolerance is not None
        device_elapsed = _absolute_seconds(event.device_time) - anchor_device_seconds
        reference_elapsed = _absolute_seconds(event.reference_time) - anchor_reference_seconds
        # |offset + rate_scale * device_elapsed - reference_elapsed| <= tolerance
        for constraint in ((1.0, device_elapsed, reference_elapsed + tolerance),
                           (-1.0, -device_elapsed, -reference_elapsed + tolerance)):
            constraints.append(constraint)
            event_constraints.append(constraint)

    distinct_device_times = {_absolute_seconds(event.device_time) for event in ordered}
    if len(distinct_device_times) < 2 and max_rate_deviation is None:
        raise UnidentifiableClockRate(
            f'[{ordered[0].event_id}] session {ordered[0].session!r} has no second distinct '
            'device time; its clock rate cannot be identified')

    if max_rate_deviation is None:
        rate_low, rate_high = _MIN_RATE_SCALE, _MAX_RATE_SCALE
    else:
        rate_low = 1.0 - max_rate_deviation
        rate_high = 1.0 + max_rate_deviation

    # Search guards, only used to keep the search finite.  For two or more
    # distinct device times the tolerance bands already bound both offset and
    # rate, so these guards do not alter the feasible region.
    constraints.append((1.0, 0.0, _MAX_OFFSET_SECONDS))
    constraints.append((-1.0, 0.0, _MAX_OFFSET_SECONDS))
    constraints.append((0.0, 1.0, rate_high))
    constraints.append((0.0, -1.0, -rate_low))

    polygon = _deduplicate_vertices(_feasible_vertices(constraints))
    if not polygon:
        ids = ', '.join(event.event_id for event in ordered)
        detail = 'within the given tolerances' if tolerance_seconds is not None else 'within their tolerances'
        raise InconsistentSyncRecords(
            f'no clock model satisfies all sync events {detail}: {ids}')

    return ClockCalibration(
        session=ordered[0].session,
        events=ordered,
        default_tolerance_seconds=tolerance_seconds,
        max_rate_deviation=max_rate_deviation,
        anchor_reference=anchor_event.reference_time,
        anchor_device=anchor_event.device_time,
        fit_region=tuple(polygon),
        constraints=tuple(event_constraints),
    )


fit_clock = calibrate_clock


@dataclass(frozen=True)
class CalibratedPoint:
    """One device point mapped onto the reference time axis."""

    point: mod_gpx.GPXTrackPoint
    session: Optional[SessionKey]
    track_no: int
    segment_no: int
    point_no: int
    nominal_time: mod_datetime.datetime
    time_range: CalibratedTimeRange

    @property
    def calibrated_time(self) -> mod_datetime.datetime:
        return self.nominal_time

    @property
    def minute_candidates(self) -> Tuple[mod_datetime.datetime, ...]:
        return self.time_range.minutes

    @property
    def ambiguous(self) -> bool:
        return self.time_range.ambiguous


def calibrate_track_point(point: mod_gpx.GPXTrackPoint, calibration: ClockCalibration,
                          track_no: int = -1, segment_no: int = -1,
                          point_no: int = -1) -> CalibratedPoint:
    """Calibrate one device point with a session calibration.

    The original point is not modified.  Its time must already be present;
    points without a device time are not guessed.
    """
    if point.time is None:
        raise ValueError('cannot calibrate a point without a device time')
    time_range = calibration.calibrated_time_range(point.time)
    return CalibratedPoint(
        point=point,
        session=calibration.session,
        track_no=track_no,
        segment_no=segment_no,
        point_no=point_no,
        nominal_time=calibration.calibrated_time(point.time),
        time_range=time_range,
    )


@dataclass(frozen=True)
class SkippedPoint:
    """A device point that was not calibrated, with the reason."""

    point: mod_gpx.GPXTrackPoint
    track_no: int
    segment_no: int
    point_no: int
    reason: str


@dataclass
class GPXCalibration:
    """Calibrated point values and skipped points for a GPX object."""

    calibrations: Dict[SessionKey, ClockCalibration]
    calibrated_points: List[CalibratedPoint]
    skipped_points: List[SkippedPoint]

    @property
    def uncalibrated_points(self) -> List[mod_gpx.GPXTrackPoint]:
        return [skipped.point for skipped in self.skipped_points]

    def calibration_for(self, point: mod_gpx.GPXTrackPoint) -> Optional[ClockCalibration]:
        for calibrated in self.calibrated_points:
            if calibrated.point is point:
                return self.calibrations.get(calibrated.session)
        return None

    def point_for(self, point: mod_gpx.GPXTrackPoint) -> Optional[CalibratedPoint]:
        for calibrated in self.calibrated_points:
            if calibrated.point is point:
                return calibrated
        return None

    def sorted_points(self) -> List[CalibratedPoint]:
        """Calibrated points in nominal-time order."""
        return sorted(self.calibrated_points,
                      key=lambda calibrated: _absolute_seconds(calibrated.nominal_time))


def _default_session_selector(track_no: int, segment_no: int) -> SessionKey:
    return (track_no, segment_no)


def _event_session_spans(events: Sequence[SyncEvent]) -> Dict[SessionKey, Tuple[float, float]]:
    grouped: Dict[SessionKey, List[SyncEvent]] = {}
    for event in events:
        if event.session is None:
            raise ValueError(f'sync event {event.event_id!r} has no session')
        grouped.setdefault(event.session, []).append(event)
    return {
        session: (min(_absolute_seconds(event.device_time) for event in session_events),
                  max(_absolute_seconds(event.device_time) for event in session_events))
        for session, session_events in grouped.items()
    }


def _nearest_event_session(device_seconds: float,
                           spans: Dict[SessionKey, Tuple[float, float]]) -> SessionKey:
    """Choose the session whose device-time span is nearest.

    Spans are ordered on the device timeline.  Two adjacent sessions meet at
    the midpoint between their spans, so every point has one deterministic
    nearest session and no session can absorb points on the far side of a
    restart.
    """
    ordered = sorted(spans.items(), key=lambda item: (item[1][0], repr(item[0])))
    for index, (session, (low, high)) in enumerate(ordered):
        if low <= device_seconds <= high:
            return session
        if device_seconds < low:
            if index == 0:
                return session
            previous_session, (_, previous_high) = ordered[index - 1]
            midpoint = (previous_high + low) / 2.0
            return previous_session if device_seconds < midpoint else session
    return ordered[-1][0]


def _assign_points_to_sessions(
        point_sessions: Dict[SessionKey, List[Tuple[int, int, int, mod_gpx.GPXTrackPoint]]],
        grouped: Dict[SessionKey, List[SyncEvent]]) -> Dict[SessionKey, List[Tuple[int, int, int, mod_gpx.GPXTrackPoint]]]:
    """Assign segment points to event-defined sessions.

    If the event keys already match the segment keys, the original grouping is
    returned untouched.  Otherwise each segment is treated as one physical
    recording: only event sessions whose spans overlap that segment are
    candidates, and a point goes to the nearest candidate by device time.
    This splits a segment that contains several power-on periods without
    letting any session jump across a segment boundary.
    """
    if set(point_sessions) == set(grouped):
        return point_sessions

    spans = _event_session_spans([event for events in grouped.values() for event in events])
    result: Dict[SessionKey, List[Tuple[int, int, int, mod_gpx.GPXTrackPoint]]] = {
        session: [] for session in point_sessions
    }
    session_segments: Dict[SessionKey, set[Tuple[int, int]]] = {}

    for segment_key, points in point_sessions.items():
        segment_span = _device_span([point for _, _, _, point in points])
        if segment_span is None:
            result[segment_key].extend(points)
            continue

        candidates = {
            session: span for session, span in spans.items()
            if span[0] <= segment_span[1] and span[1] >= segment_span[0]
        }
        if not candidates:
            # No paired session overlaps this segment: keep it as its own
            # session so it is reported as uncalibratable, never as an
            # extension of another session.
            result[segment_key].extend(points)
            continue

        for track_no, segment_no, point_no, point in points:
            if point.time is None:
                result[segment_key].append((track_no, segment_no, point_no, point))
                continue
            target = _nearest_event_session(_absolute_seconds(point.time), candidates)
            result.setdefault(target, []).append((track_no, segment_no, point_no, point))
            session_segments.setdefault(target, set()).add((track_no, segment_no))

    for session, used_segments in session_segments.items():
        if len(used_segments) > 1:
            raise ValueError(
                f'session {session!r} has points in segments {sorted(used_segments)}; '
                'a power-on session cannot cross a device restart')

    return {session: points for session, points in result.items() if points}


def calibrate_gpx(gpx: mod_gpx.GPX,
                  sync_events: Iterable[SyncEvent],
                  tolerance_seconds: Optional[float] = None,
                  session_selector: Optional[Callable[[int, int], SessionKey]] = None,
                  max_rate_deviation: Optional[float] = None,
                  on_inconsistent: str = 'raise') -> GPXCalibration:
    """Calibrate all calibratable timed points of a GPX object.

    By default one GPX track segment is one session (one power-on period).
    ``session_selector`` may map ``(track_no, segment_no)`` to a session key
    matching ``SyncEvent.session``.  Returning the same key for several
    segments explicitly declares that they belong to one continuous
    recording and may share one fit.

    If the sync events carry session keys that do not correspond to whole
    segments (for example two power-on periods were written into one segment),
    each segment's points are split between those sessions by device time.
    A session whose points span several segments is rejected: segment
    boundaries are device restarts and a previous slope must not bridge them.

    Events whose ``session`` is omitted are assigned as follows: with exactly
    one segment every event belongs to it; with several segments each event is
    assigned to the segment whose device-time span is nearest (and a tie is an
    error, requiring an explicit session).  Points are assigned only from
    their own segment's device-time span; fitting never crosses a segment
    boundary.

    A session is skipped as a whole (and its points are reported in
    ``skipped_points``) when its rate cannot be identified, when it has no
    paired events, or when points have no device time.  This is deliberate:
    the slope of another session is never reused.

    ``on_inconsistent`` controls whether contradictory session definitions
    are fatal: ``'raise'`` (default) raises an exception (a
    :class:`ClockSyncError` for contradictory paired events, or ``ValueError``
    for a session/segment conflict); ``'skip'`` records the affected session
    and points in ``skipped_points`` instead of fitting them.
    """
    if on_inconsistent not in ('raise', 'skip'):
        raise ValueError("on_inconsistent must be 'raise' or 'skip'")

    events = list(sync_events)
    selector = session_selector or _default_session_selector

    point_sessions: Dict[SessionKey, List[Tuple[int, int, int, mod_gpx.GPXTrackPoint]]] = {}
    for track_no, track in enumerate(gpx.tracks):
        for segment_no, segment in enumerate(track.segments):
            session = selector(track_no, segment_no)
            point_sessions.setdefault(session, []).extend(
                (track_no, segment_no, point_no, point)
                for point_no, point in enumerate(segment.points)
            )

    spans = {
        session: _device_span([point for _, _, _, point in points])
        for session, points in point_sessions.items()
    }

    assigned: List[SyncEvent] = []
    for event in events:
        if event.session is None:
            if len(point_sessions) == 1:
                session = next(iter(point_sessions))
            else:
                session = _nearest_session(event.device_time, spans, event.event_id)
            assigned.append(replace(event, session=session))
        else:
            assigned.append(event)

    grouped: Dict[SessionKey, List[SyncEvent]] = {}
    for event in assigned:
        if event.session is None:
            raise ValueError(f'sync event {event.event_id!r} has no session')
        grouped.setdefault(event.session, []).append(event)

    # Refine point groups when event session keys subdivide a segment.
    point_sessions = _assign_points_to_sessions(point_sessions, grouped)

    calibrations: Dict[SessionKey, ClockCalibration] = {}
    session_reasons: Dict[SessionKey, str] = {}

    for session, session_events in grouped.items():
        if session not in point_sessions:
            if not session_events:
                continue
            session_reasons[session] = 'sync events have no GPX points in any segment'

    for session, session_events in grouped.items():
        if session not in point_sessions:
            continue
        try:
            calibrations[session] = calibrate_clock(
                session_events,
                tolerance_seconds=tolerance_seconds,
                max_rate_deviation=max_rate_deviation,
            )
        except UnidentifiableClockRate as error:
            session_reasons[session] = str(error)
        except InconsistentSyncRecords as error:
            if on_inconsistent == 'raise':
                raise
            session_reasons[session] = str(error)

    for session, points in point_sessions.items():
        if session not in grouped:
            session_reasons.setdefault(
                session, 'no paired sync events for this session; its clock cannot be calibrated')

    calibrated_points: List[CalibratedPoint] = []
    skipped_points: List[SkippedPoint] = []

    for session, points in point_sessions.items():
        calibration = calibrations.get(session)
        reason = session_reasons.get(session)
        for track_no, segment_no, point_no, point in points:
            if calibration is None:
                skipped_points.append(SkippedPoint(
                    point=point,
                    track_no=track_no,
                    segment_no=segment_no,
                    point_no=point_no,
                    reason=reason or 'session cannot be calibrated',
                ))
            elif point.time is None:
                skipped_points.append(SkippedPoint(
                    point=point,
                    track_no=track_no,
                    segment_no=segment_no,
                    point_no=point_no,
                    reason='point has no device time',
                ))
            else:
                calibrated_points.append(calibrate_track_point(
                    point, calibration,
                    track_no=track_no, segment_no=segment_no, point_no=point_no))

    return GPXCalibration(
        calibrations=calibrations,
        calibrated_points=calibrated_points,
        skipped_points=skipped_points,
    )


def apply_calibrated_times(gpx: mod_gpx.GPX, calibration: GPXCalibration,
                           in_place: bool = True) -> mod_gpx.GPX:
    """Write each calibrated point's nominal time back into a GPX object.

    Ambiguous points retain both candidates in :class:`GPXCalibration`; a GPX
    timestamp can store only one.  ``nominal_time`` is the feasible model
    closest to a steady clock.  Set ``in_place=False`` to return a clone.
    """
    target = gpx if in_place else gpx.clone()
    for calibrated in calibration.calibrated_points:
        target.tracks[calibrated.track_no] \
            .segments[calibrated.segment_no] \
            .points[calibrated.point_no].time = calibrated.nominal_time
    return target


@dataclass(frozen=True)
class WorkOrder:
    """A minute-level work order.

    ``minute`` is any instant inside the order's minute; matching uses the
    minute bucket, so seconds in the input do not matter.
    """

    work_order_id: str
    minute: mod_datetime.datetime

    def __post_init__(self) -> None:
        if not self.work_order_id:
            raise ValueError('work_order_id cannot be empty')


@dataclass(frozen=True)
class WorkOrderMatch:
    """One possible point/work-order association."""

    calibrated_point: CalibratedPoint
    work_order: WorkOrder
    minute: mod_datetime.datetime

    @property
    def point(self) -> mod_gpx.GPXTrackPoint:
        return self.calibrated_point.point

    @property
    def work_order_id(self) -> str:
        return self.work_order.work_order_id


@dataclass
class WorkOrderCandidates:
    """Every possible work order for one point, one entry per minute."""

    calibrated_point: CalibratedPoint
    matches: List[WorkOrderMatch]

    @property
    def point(self) -> mod_gpx.GPXTrackPoint:
        return self.calibrated_point.point

    @property
    def minutes(self) -> Tuple[mod_datetime.datetime, ...]:
        return self.calibrated_point.minute_candidates

    @property
    def work_order_ids(self) -> Tuple[str, ...]:
        return tuple(dict.fromkeys(match.work_order_id for match in self.matches))

    @property
    def ambiguous(self) -> bool:
        return self.calibrated_point.ambiguous


def _minute_bucket(value: mod_datetime.datetime) -> int:
    return _floor_minute_seconds(_absolute_seconds(value))


def _coerce_calibrated_points(value: Union[Iterable[CalibratedPoint], GPXCalibration]) -> List[CalibratedPoint]:
    if isinstance(value, GPXCalibration):
        return list(value.calibrated_points)
    return list(value)


def associate_minute_work_orders(
        points: Union[Iterable[CalibratedPoint], GPXCalibration],
        work_orders: Iterable[WorkOrder]) -> List[WorkOrderMatch]:
    """Associate calibrated points with minute-level work orders.

    Every minute candidate of every point is considered.  If a point straddles
    a minute boundary, matches for both minutes are returned; there is no
    "best" choice.  Work orders whose minute has no candidate point are simply
    not matched.
    """
    point_list = _coerce_calibrated_points(points)
    order_list = list(work_orders)
    if not point_list or not order_list:
        return []

    point_aware = {point.time_range.earliest.tzinfo is not None for point in point_list}
    order_aware = {order.minute.tzinfo is not None for order in order_list}
    if len(point_aware | order_aware) > 1:
        raise ValueError('work-order minutes and calibrated times must both be timezone-aware or both be naive')

    orders_by_minute: Dict[int, List[WorkOrder]] = {}
    for order in order_list:
        orders_by_minute.setdefault(_minute_bucket(order.minute), []).append(order)

    matches: List[WorkOrderMatch] = []
    for point in point_list:
        for candidate_minute in point.minute_candidates:
            bucket = _minute_bucket(candidate_minute)
            for order in orders_by_minute.get(bucket, []):
                matches.append(WorkOrderMatch(
                    calibrated_point=point,
                    work_order=order,
                    minute=candidate_minute,
                ))
    return matches


associate_work_orders = associate_minute_work_orders


def group_work_order_candidates(
        points: Union[Iterable[CalibratedPoint], GPXCalibration],
        work_orders: Iterable[WorkOrder]) -> List[WorkOrderCandidates]:
    """Like :func:`associate_minute_work_orders`, but grouped per point.

    Keeping candidates grouped makes it explicit that several work orders
    returned for one point are alternatives, not several observations.
    """
    matches = associate_minute_work_orders(points, work_orders)
    grouped: Dict[int, WorkOrderCandidates] = {}
    for match in matches:
        key = id(match.calibrated_point)
        group = grouped.get(key)
        if group is None:
            group = WorkOrderCandidates(calibrated_point=match.calibrated_point, matches=[])
            grouped[key] = group
        group.matches.append(match)
    return list(grouped.values())


def _coerce_datetime(value: Any, field_name: str) -> mod_datetime.datetime:
    if isinstance(value, mod_datetime.datetime):
        return value
    if isinstance(value, str):
        from . import gpxfield as mod_gpxfield
        parsed = mod_gpxfield.parse_time(value.strip())
        if parsed is None:
            raise ValueError(f'{field_name} is empty')
        return parsed
    raise ValueError(f'{field_name} must be a datetime or an ISO-8601 string, '
                     f'got {type(value).__name__}')


def _record_get(record: Any, names: Sequence[str]) -> Any:
    if isinstance(record, dict):
        for name in names:
            if record.get(name) is not None:
                return record[name]
        return None
    for name in names:
        value = getattr(record, name, None)
        if value is not None:
            return value
    return None


def parse_sync_events(records: Iterable[Any],
                      session: Optional[SessionKey] = None) -> List[SyncEvent]:
    """Convert handover-list records into :class:`SyncEvent` objects.

    Supports :class:`SyncEvent` instances, dictionaries and
    ``(event_id, reference_time, device_time[, session])`` sequences.
    The handover list must already pair the presses; unpaired events are not
    matched by position.
    """
    events: List[SyncEvent] = []
    for index, record in enumerate(records):
        if isinstance(record, SyncEvent):
            events.append(record if session is None else replace(record, session=session))
            continue

        if isinstance(record, (tuple, list)):
            if len(record) not in (3, 4):
                raise ValueError('a handover sequence must be '
                                 '(event_id, reference_time, device_time[, session])')
            event_id = str(record[0])
            reference_time = _coerce_datetime(record[1], 'reference_time')
            device_time = _coerce_datetime(record[2], 'device_time')
            record_session = record[3] if len(record) == 4 else session
            tolerance = None
        else:
            raw_id = _record_get(record, ('event_id', 'id', 'button_event', 'button_id', 'event'))
            if raw_id is None:
                raise ValueError(f'handover record {index} has no event id')
            event_id = str(raw_id)
            raw_reference = _record_get(record, ('reference_time', 'reference', 'ref', 'reference_timestamp'))
            raw_device = _record_get(record, ('device_time', 'device', 'dev', 'device_timestamp'))
            if raw_reference is None or raw_device is None:
                raise ValueError(f'handover record {index} must contain both '
                                 'reference_time and device_time')
            reference_time = _coerce_datetime(raw_reference, 'reference_time')
            device_time = _coerce_datetime(raw_device, 'device_time')
            record_session = _record_get(record, ('session', 'session_id', 'power_on_session')) or session
            tolerance = _record_get(record, ('tolerance_seconds', 'tolerance', 'sync_tolerance'))

        events.append(SyncEvent(
            event_id=event_id,
            reference_time=reference_time,
            device_time=device_time,
            session=record_session,
            tolerance_seconds=float(tolerance) if tolerance is not None else None,
        ))
    return events


def parse_work_orders(records: Iterable[Any]) -> List[WorkOrder]:
    """Convert work-order records into :class:`WorkOrder` objects.

    Supports :class:`WorkOrder`, dictionaries and
    ``(work_order_id, minute)`` sequences.
    """
    orders: List[WorkOrder] = []
    for index, record in enumerate(records):
        if isinstance(record, WorkOrder):
            orders.append(record)
        elif isinstance(record, (tuple, list)):
            if len(record) != 2:
                raise ValueError('a work-order sequence must be (work_order_id, minute)')
            orders.append(WorkOrder(str(record[0]), _coerce_datetime(record[1], 'minute')))
        else:
            raw_id = _record_get(record, ('work_order_id', 'id', 'order_id', 'workorder', 'order'))
            raw_minute = _record_get(record, ('minute', 'time', 'start', 'start_time'))
            if raw_id is None or raw_minute is None:
                raise ValueError(f'work-order record {index} must contain an id and a minute')
            orders.append(WorkOrder(str(raw_id), _coerce_datetime(raw_minute, 'minute')))
    return orders


def calibrate_sessions(events: Iterable[Any], tolerance_seconds: Optional[float] = None,
                       max_rate_deviation: Optional[float] = None) -> Dict[SessionKey, ClockCalibration]:
    """Fit one independent clock model per power-on session."""
    typed: List[SyncEvent] = []
    for event in events:
        typed.append(event if isinstance(event, SyncEvent) else parse_sync_events([event])[0])

    grouped: Dict[SessionKey, List[SyncEvent]] = {}
    for event in typed:
        if event.session is None:
            raise ValueError(f'sync event {event.event_id!r} has no session; '
                             'calibrate_sessions needs session keys')
        grouped.setdefault(event.session, []).append(event)

    return {
        session: calibrate_clock(session_events,
                                 tolerance_seconds=tolerance_seconds,
                                 max_rate_deviation=max_rate_deviation)
        for session, session_events in grouped.items()
    }


def calibrate_handover_track(gpx: mod_gpx.GPX, handover_records: Iterable[Any],
                             tolerance_seconds: Optional[float] = None,
                             session_selector: Optional[Callable[[int, int], SessionKey]] = None,
                             max_rate_deviation: Optional[float] = None,
                             on_inconsistent: str = 'raise') -> GPXCalibration:
    """Parse a handover list and calibrate a GPX in one step."""
    events = parse_sync_events(handover_records)
    return calibrate_gpx(
        gpx,
        events,
        tolerance_seconds=tolerance_seconds,
        session_selector=session_selector,
        max_rate_deviation=max_rate_deviation,
        on_inconsistent=on_inconsistent,
    )


def match_work_orders(gpx_calibration: GPXCalibration,
                      work_order_records: Iterable[Any]) -> List[WorkOrderCandidates]:
    """Parse work-order records and group all possible associations per point."""
    return group_work_order_candidates(gpx_calibration, parse_work_orders(work_order_records))
