"""
Cross-check separation between two time-stamped tracks.

Both tracks are expressed in the same declared local planar coordinate
system (x/y, in whatever unit the caller uses, usually meters).  Time is
the only thing used for synchronisation: positions between two consecutive
samples are linearly interpolated in time.  There is no geographic
projection and no interpolation in space which is not implied by time.

Only the intersection of the two valid time coverages contributes to the
reported duration.  A track may consist of several segments: each segment
has its own coverage, positions are never interpolated across a gap between
two segments, and segments must not overlap in time.

The result does not depend on which track is passed first and it is
invariant to redundant samples inserted on straight, constant-velocity
parts of a track.
"""

import bisect as mod_bisect
import datetime as mod_datetime
import math as mod_math

from typing import cast, Iterable, List, NamedTuple, Optional, Protocol, Tuple, Union, overload

# The public input accepts PlanarPoint instances, plain (time, x, y)
# triples, or any object exposing ``time``, ``x`` and ``y`` attributes.
class PlanarSample(Protocol):
    time: mod_datetime.datetime
    x: float
    y: float


PlanarPointLike = Union["PlanarPoint", Tuple[mod_datetime.datetime, float, float], PlanarSample]
# A track is a flat sequence of samples or a sequence of segments.
# Objects exposing ``segments`` (each with ``points``) or ``points`` are
# accepted as well.
class PlanarSegment(Protocol):
    points: Iterable[PlanarPointLike]


class SegmentedTrack(Protocol):
    segments: Iterable[PlanarSegment]


class PointTrack(Protocol):
    points: Iterable[PlanarPointLike]


TrackInput = Union[Iterable[PlanarPointLike], Iterable[PlanarSegment],
                   SegmentedTrack, PointTrack]


class PlanarPoint(NamedTuple):
    """A sample in a local planar coordinate system."""

    time: mod_datetime.datetime
    x: float
    y: float


class CoverageInterval(NamedTuple):
    """A closed interval of time for which a track has valid coverage."""

    start_time: mod_datetime.datetime
    end_time: mod_datetime.datetime


class SeparationReport(NamedTuple):
    """
    Result of a separation cross-check.

    Attributes
    ----------
    threshold : float
        Separation threshold, in planar coordinate units.
    coverage_intervals : tuple of CoverageInterval
        Intersection of the two tracks' valid time coverage.  Time outside
        these intervals never contributes to any duration below.
    coverage_seconds : float
        Total duration of ``coverage_intervals`` in seconds.
    exceedance_seconds : float
        Exact time (in seconds) during the shared coverage where the
        interpolated separation is strictly greater than the threshold.
    exceedance_intervals : tuple of CoverageInterval
        Time intervals during which the threshold is exceeded.
    max_separation : float
        Maximum interpolated separation observed on the shared coverage.
    """

    threshold: float
    coverage_intervals: Tuple[CoverageInterval, ...]
    coverage_seconds: float
    exceedance_seconds: float
    exceedance_intervals: Tuple[CoverageInterval, ...]
    max_separation: float


class _SegmentData(NamedTuple):
    times: List[float]
    xs: List[float]
    ys: List[float]


class _CoveredInterval(NamedTuple):
    start: float
    end: float
    segment_index: int


class _TrackData(NamedTuple):
    segments: List[_SegmentData]
    coverage: List[_CoveredInterval]


class _SharedInterval(NamedTuple):
    start: float
    end: float
    segment_a: int
    segment_b: int


# Admissible deviation (relative to the chord length) for a sample to be
# considered on the straight constant-velocity path between its neighbours.
# This is far below any physically meaningful GPS/planar sampling resolution,
# but safely above the microsecond quantisation of ``datetime``.
_COLLINEAR_RELATIVE_TOLERANCE = 1e-6


def calculate_separation(track_a: TrackInput,
                         track_b: TrackInput,
                         threshold: float,
                         max_gap: Optional[mod_datetime.timedelta]=None,
                         ) -> SeparationReport:
    """
    Cross-check the separation between two tracks over their shared coverage.

    Parameters
    ----------
    track_a, track_b :
        Either a flat sequence of samples, a sequence of segments, or an
        object exposing ``segments`` (each with ``points``) or ``points``.
        Samples may be PlanarPoint objects, (time, x, y) tuples, or objects
        with ``time``, ``x`` and ``y`` attributes.  Coordinates must be in
        the same declared local planar system.  Samples are sorted by time;
        duplicate timestamps are allowed only when their coordinates are
        identical, otherwise the position at that time is ambiguous and
        ValueError is raised.  Segments must not overlap in time.
    threshold : float
        Separation threshold in planar coordinate units.  The reported
        duration counts strictly greater separations only ("exceeds",
        not "reaches").
    max_gap : datetime.timedelta, optional
        If supplied, a gap between adjacent samples longer than this is
        treated as missing time coverage instead of a segment to
        interpolate across.  By default every adjacent pair of samples
        within a segment is covered, which is appropriate for tracks that
        merely use different sampling rates.

    Returns
    -------
    report : SeparationReport
        Shared coverage, exceedance duration, exceedance intervals and
        maximum separation.

    Raises
    ------
    ValueError
        For negative/non-finite thresholds, non-finite coordinates,
        duplicate timestamps with different positions, inconsistent
        timezone awareness, overlapping segments, or a non-positive
        ``max_gap``.
    """
    try:
        threshold_value = float(threshold)
    except (TypeError, ValueError) as exc:
        raise ValueError('threshold must be a finite, non-negative number') from exc
    if not mod_math.isfinite(threshold_value) or threshold_value < 0:
        raise ValueError('threshold must be a finite, non-negative number')
    if max_gap is not None and max_gap <= mod_datetime.timedelta(0):
        raise ValueError('max_gap must be positive')

    segments_a = _normalize_track(track_a, 'track_a', max_gap)
    segments_b = _normalize_track(track_b, 'track_b', max_gap)

    sample_times = [point.time for segment in segments_a + segments_b for point in segment]
    if not sample_times:
        return _empty_report(threshold_value)

    try:
        origin = min(sample_times)
    except TypeError as exc:
        raise ValueError('all sample times must use the same timezone awareness') from exc

    track_a_data = _prepare_track(segments_a, origin, max_gap, 'track_a')
    track_b_data = _prepare_track(segments_b, origin, max_gap, 'track_b')

    common = _intersect_coverage(track_a_data.coverage, track_b_data.coverage)
    if not common:
        return _empty_report(threshold_value)

    coverage_seconds = sum(interval.end - interval.start for interval in common)
    exceedance_seconds, exceedance, max_separation = _integrate_exceedance(
        track_a_data, track_b_data, common, threshold_value)

    return SeparationReport(
        threshold=threshold_value,
        coverage_intervals=_to_coverage_intervals(origin, [
            (interval.start, interval.end) for interval in common]),
        coverage_seconds=coverage_seconds,
        exceedance_seconds=exceedance_seconds,
        exceedance_intervals=_to_coverage_intervals(origin, exceedance),
        max_separation=max_separation,
    )


def separation_exceedance_seconds(track_a: TrackInput,
                                  track_b: TrackInput,
                                  threshold: float,
                                  max_gap: Optional[mod_datetime.timedelta]=None,
                                  ) -> float:
    """Convenience wrapper returning only the exceedance duration in seconds."""
    return calculate_separation(track_a, track_b, threshold, max_gap).exceedance_seconds


@overload
def _normalize_track(track: Iterable[PlanarPointLike], name: str,
                     max_gap: Optional[mod_datetime.timedelta]) -> List[List[PlanarPoint]]: ...
@overload
def _normalize_track(track: Iterable[PlanarSegment], name: str,
                     max_gap: Optional[mod_datetime.timedelta]) -> List[List[PlanarPoint]]: ...
@overload
def _normalize_track(track: SegmentedTrack, name: str,
                     max_gap: Optional[mod_datetime.timedelta]) -> List[List[PlanarPoint]]: ...
@overload
def _normalize_track(track: PointTrack, name: str,
                     max_gap: Optional[mod_datetime.timedelta]) -> List[List[PlanarPoint]]: ...
def _normalize_track(track: TrackInput, name: str,
                     max_gap: Optional[mod_datetime.timedelta]) -> List[List[PlanarPoint]]:
    if hasattr(track, 'segments'):
        raw_segments = [segment.points
                        for segment in cast(SegmentedTrack, track).segments]
    elif hasattr(track, 'points'):
        raw_segments = [cast(PointTrack, track).points]
    else:
        items = list(cast(Iterable[object], track))
        if items and _looks_like_sample(items[0]):
            raw_segments = [cast(Iterable[PlanarPointLike], items)]
        else:
            raw_segments = [cast(Iterable[PlanarPointLike], segment)
                            for segment in items]

    segments: List[List[PlanarPoint]] = []
    for raw_segment in raw_segments:
        normalized = _normalize_points(raw_segment, name)
        if not normalized:
            continue
        for run in _split_runs(normalized, max_gap):
            segments.append(_remove_collinear_points(run, max_gap))
    return segments


def _looks_like_sample(value: object) -> bool:
    if isinstance(value, PlanarPoint):
        return True
    if isinstance(value, tuple):
        return len(value) == 3 and isinstance(value[0], mod_datetime.datetime)
    return (hasattr(value, 'time') and hasattr(value, 'x') and hasattr(value, 'y'))


def _normalize_points(points: Iterable[PlanarPointLike],
                      name: str) -> List[PlanarPoint]:
    normalized: List[PlanarPoint] = []
    for point in points:
        if isinstance(point, PlanarPoint):
            time, x, y = point
        elif isinstance(point, tuple):
            if len(point) != 3:
                raise ValueError(f'{name}: expected a (time, x, y) triple')
            sample_time, sample_x, sample_y = point
            time, x, y = sample_time, sample_x, sample_y
        else:
            try:
                time, x, y = point.time, point.x, point.y
            except AttributeError as exc:
                raise ValueError(f'{name}: expected a PlanarPoint, an object with time/x/y, or a (time, x, y) triple') from exc

        if not isinstance(time, mod_datetime.datetime):
            raise ValueError(f'{name}: sample time must be a datetime')
        if not mod_math.isfinite(x) or not mod_math.isfinite(y):
            raise ValueError(f'{name}: sample coordinates must be finite')
        normalized.append(PlanarPoint(time, float(x), float(y)))

    try:
        normalized.sort(key=lambda point: point.time)
    except TypeError as exc:
        raise ValueError(f'{name}: sample times must all use the same timezone awareness') from exc

    deduplicated: List[PlanarPoint] = []
    for point in normalized:
        if not deduplicated:
            deduplicated.append(point)
            continue
        previous = deduplicated[-1]
        if point.time != previous.time:
            deduplicated.append(point)
            continue
        if point.x != previous.x or point.y != previous.y:
            raise ValueError(f'{name}: duplicate timestamp with conflicting coordinates: {point.time}')
    return deduplicated


def _split_runs(points: List[PlanarPoint],
                max_gap: Optional[mod_datetime.timedelta]) -> List[List[PlanarPoint]]:
    """Split at holes longer than max_gap, since no interpolation spans them."""
    if max_gap is None:
        return [points]

    runs: List[List[PlanarPoint]] = [[points[0]]]
    for previous, current in zip(points, points[1:]):
        if current.time - previous.time > max_gap:
            runs.append([])
        runs[-1].append(current)
    return runs


def _remove_collinear_points(points: List[PlanarPoint],
                             max_gap: Optional[mod_datetime.timedelta]) -> List[PlanarPoint]:
    """
    Drop samples which carry no information: a point on the linear
    interpolation between its neighbours (in time) defines the same straight
    constant-velocity path.  Removing it makes redundant samples literally
    invisible to the rest of the calculation, so sampling density cannot
    perturb the result through floating-point rounding.
    """
    if len(points) < 3:
        return points

    changed = True
    while changed and len(points) >= 3:
        changed = False
        kept = [points[0]]
        for index in range(1, len(points) - 1):
            previous = kept[-1]
            current = points[index]
            following = points[index + 1]

            if max_gap is not None and following.time - previous.time > max_gap:
                # Pruning this sample would invent a coverage hole.
                kept.append(current)
                continue

            previous_seconds = (previous.time - following.time).total_seconds()
            current_seconds = (current.time - following.time).total_seconds()
            ratio = (current_seconds - previous_seconds) / -previous_seconds

            previous_x = previous.x
            previous_y = previous.y
            following_x = following.x
            following_y = following.y
            span = mod_math.hypot(following_x - previous_x, following_y - previous_y)
            residual = mod_math.hypot(
                current.x - (previous_x + ratio * (following_x - previous_x)),
                current.y - (previous_y + ratio * (following_y - previous_y)))
            tolerance = _COLLINEAR_RELATIVE_TOLERANCE * max(1.0, span)

            if residual <= tolerance:
                changed = True
                continue
            kept.append(current)
        kept.append(points[-1])
        points = kept
    return points


def _prepare_track(segments: List[List[PlanarPoint]],
                   origin: mod_datetime.datetime,
                   max_gap: Optional[mod_datetime.timedelta],
                   name: str) -> _TrackData:
    prepared: List[_SegmentData] = []
    coverage: List[_CoveredInterval] = []
    previous_end: Optional[float] = None

    max_gap_seconds = None if max_gap is None else max_gap.total_seconds()

    for segment_index, segment in enumerate(segments):
        if len(segment) < 2:
            continue

        segment_times = [_seconds_since(point.time, origin, name) for point in segment]
        segment_end = segment_times[-1]

        if previous_end is not None and segment_times[0] < previous_end:
            raise ValueError(f'{name}: segments must not overlap in time')
        previous_end = segment_end

        segment_data = _SegmentData(
            times=segment_times,
            xs=[point.x for point in segment],
            ys=[point.y for point in segment],
        )
        prepared.append(segment_data)

        segment_index = len(prepared) - 1
        for start, end in zip(segment_times, segment_times[1:]):
            if max_gap_seconds is not None and end - start > max_gap_seconds + 1e-9:
                continue
            if coverage and coverage[-1].segment_index == segment_index \
                    and coverage[-1].end == start:
                coverage[-1] = _CoveredInterval(coverage[-1].start, end, segment_index)
            else:
                coverage.append(_CoveredInterval(start, end, segment_index))

    return _TrackData(segments=prepared, coverage=coverage)


def _intersect_coverage(left: List[_CoveredInterval],
                        right: List[_CoveredInterval]
                        ) -> List[_SharedInterval]:
    result: List[_SharedInterval] = []
    left_index = 0
    right_index = 0

    while left_index < len(left) and right_index < len(right):
        left_interval = left[left_index]
        right_interval = right[right_index]
        start = max(left_interval.start, right_interval.start)
        end = min(left_interval.end, right_interval.end)

        if end > start:
            if result and result[-1].end == start \
                    and result[-1].segment_a == left_interval.segment_index \
                    and result[-1].segment_b == right_interval.segment_index:
                result[-1] = _SharedInterval(result[-1].start, end,
                                             result[-1].segment_a, result[-1].segment_b)
            else:
                result.append(_SharedInterval(start, end,
                                              left_interval.segment_index,
                                              right_interval.segment_index))

        if left_interval.end < right_interval.end:
            left_index += 1
        else:
            right_index += 1

    return result


def _integrate_exceedance(track_a: _TrackData,
                          track_b: _TrackData,
                          common: List[_SharedInterval],
                          threshold: float,
                          ) -> Tuple[float, List[Tuple[float, float]], float]:
    exceedance_seconds = 0.0
    exceedance: List[Tuple[float, float]] = []
    max_separation = 0.0

    for shared in common:
        segment_a = track_a.segments[shared.segment_a]
        segment_b = track_b.segments[shared.segment_b]
        events = _event_times(segment_a.times, segment_b.times, shared.start, shared.end)
        for start, end in zip(events, events[1:]):
            duration = end - start
            if duration <= 0:
                continue

            ax_start, ay_start = _position_at(segment_a, start)
            ax_end, ay_end = _position_at(segment_a, end)
            bx_start, by_start = _position_at(segment_b, start)
            bx_end, by_end = _position_at(segment_b, end)

            dx_start = ax_start - bx_start
            dy_start = ay_start - by_start
            dx_end = ax_end - bx_end
            dy_end = ay_end - by_end

            max_separation = max(max_separation,
                                 mod_math.hypot(dx_start, dy_start),
                                 mod_math.hypot(dx_end, dy_end))

            for positive_start, positive_end in _exceedance_segments(
                    dx_start, dy_start, dx_end, dy_end, duration, threshold):
                interval_start = start + positive_start
                interval_end = start + positive_end
                exceedance_seconds += interval_end - interval_start
                _append_interval(exceedance, interval_start, interval_end)

    return exceedance_seconds, exceedance, max_separation


def _event_times(times_a: List[float],
                 times_b: List[float],
                 start: float,
                 end: float,
                 ) -> List[float]:
    events = [start, end]

    for times in (times_a, times_b):
        index = mod_bisect.bisect_right(times, start)
        while index < len(times) and times[index] < end:
            events.append(times[index])
            index += 1

    events.sort()
    unique: List[float] = []
    for event in events:
        if not unique or event != unique[-1]:
            unique.append(event)
    return unique


def _position_at(segment: _SegmentData, time: float) -> Tuple[float, float]:
    point_index = mod_bisect.bisect_right(segment.times, time) - 1
    point_index = max(0, min(point_index, len(segment.times) - 2))

    start_time = segment.times[point_index]
    end_time = segment.times[point_index + 1]
    ratio = (time - start_time) / (end_time - start_time)

    x = segment.xs[point_index] + ratio * (segment.xs[point_index + 1] - segment.xs[point_index])
    y = segment.ys[point_index] + ratio * (segment.ys[point_index + 1] - segment.ys[point_index])
    return x, y


def _exceedance_segments(dx_start: float,
                         dy_start: float,
                         dx_end: float,
                         dy_end: float,
                         duration: float,
                         threshold: float,
                         ) -> List[Tuple[float, float]]:
    """
    Return the sub-intervals of [0, duration] where the squared separation
    is strictly above threshold squared.

    On such a sub-interval the relative position is linear in time, so the
    squared separation is a convex quadratic.  Its crossing times are
    solved analytically; numerical stepping is never used.
    """
    velocity_x = (dx_end - dx_start) / duration
    velocity_y = (dy_end - dy_start) / duration

    quadratic = velocity_x ** 2 + velocity_y ** 2
    linear = 2 * (dx_start * velocity_x + dy_start * velocity_y)
    constant = dx_start ** 2 + dy_start ** 2 - threshold ** 2

    if quadratic == 0:
        return [(0.0, duration)] if constant > 0 else []

    discriminant = linear ** 2 - 4 * quadratic * constant
    if discriminant < 0:
        # Convex quadratic with no real roots: it is positive everywhere.
        return [(0.0, duration)]

    square_root = mod_math.sqrt(max(discriminant, 0))
    # Numerically stable quadratic roots:
    helper = -0.5 * (linear + mod_math.copysign(square_root, linear))
    if helper == 0:
        root_1 = root_2 = 0.0
    else:
        root_1 = helper / quadratic
        root_2 = constant / helper
    if root_1 > root_2:
        root_1, root_2 = root_2, root_1

    # The quadratic is positive outside [root_1, root_2].
    left_end = min(max(root_1, 0.0), duration)
    right_start = min(max(root_2, 0.0), duration)

    segments: List[Tuple[float, float]] = []
    if left_end > 0:
        segments.append((0.0, left_end))
    if right_start < duration:
        segments.append((right_start, duration))
    return segments


def _append_interval(intervals: List[Tuple[float, float]],
                     start: float,
                     end: float) -> None:
    if end <= start:
        return
    if intervals and start <= intervals[-1][1] + 1e-12:
        intervals[-1] = (intervals[-1][0], max(intervals[-1][1], end))
    else:
        intervals.append((start, end))


def _to_coverage_intervals(origin: mod_datetime.datetime,
                           intervals: List[Tuple[float, float]]
                           ) -> Tuple[CoverageInterval, ...]:
    merged: List[Tuple[float, float]] = []
    for start, end in intervals:
        _append_interval(merged, start, end)

    return tuple(
        CoverageInterval(
            start_time=origin + mod_datetime.timedelta(seconds=start),
            end_time=origin + mod_datetime.timedelta(seconds=end),
        )
        for start, end in merged
    )


def _seconds_since(time: mod_datetime.datetime,
                   origin: mod_datetime.datetime,
                   name: str) -> float:
    try:
        return (time - origin).total_seconds()
    except TypeError as exc:
        raise ValueError(f'{name}: all sample times must use the same timezone awareness') from exc


def _empty_report(threshold: float) -> SeparationReport:
    return SeparationReport(
        threshold=threshold,
        coverage_intervals=(),
        coverage_seconds=0.0,
        exceedance_seconds=0.0,
        exceedance_intervals=(),
        max_separation=0.0,
    )
