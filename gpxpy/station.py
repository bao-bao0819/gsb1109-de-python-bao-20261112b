"""
Station dwell detection from GPX tracks.

Given a station location and two independently configured geofence radii, this
module turns a time ordered sequence of track points into station visits:

* arrival is the first observation inside the *entry* radius;
* departure is only confirmed once *consecutive* observations stay outside the
  *exit* radius for both a minimum number of points and a minimum duration, so
  isolated GPS jumps outside the fence while the vehicle is parked do not end a
  stop;
* time spans without trustworthy positions (a sampling gap longer than the
  configured maximum, or a switch between track segments) are reported as
  unknown intervals and split visits instead of being guessed over.

Points without a timestamp are ignored because they cannot be ordered in time.
"""

import datetime as mod_datetime

from typing import List, NamedTuple, Optional

from . import geo as mod_geo
from .gpx import GPX, GPXTrackPoint

# Reasons recorded on unknown intervals:
GAP = 'gap'
SEGMENT = 'segment'

# Reasons recorded on incomplete (unconfirmed) dwells:
UNKNOWN_GAP = 'unknown_gap'
UNKNOWN_SEGMENT = 'unknown_segment'
END_OF_DATA = 'end_of_data'

_INSIDE = 'inside'
_OUTSIDE = 'outside'


class StationConfig(NamedTuple):
    """
    Configuration for station dwell detection.

    Distances are in meters, durations in seconds.

    entry_radius:
        A point must be within this radius of the station to count as an
        arrival while the vehicle is considered outside.
    exit_radius:
        While considered inside, a point must be farther than this radius to
        count as an outside observation. Keeping this larger than
        ``entry_radius`` gives a hysteresis band that absorbs fence noise.
    min_outside_observations:
        Number of consecutive outside observations required, together with
        ``min_outside_seconds``, before a departure is confirmed.
    min_outside_seconds:
        Minimum time span covered by the consecutive outside observations
        before a departure is confirmed.
    max_gap_seconds:
        Consecutive observations farther apart than this are treated as an
        unobserved gap (unknown interval), even within one segment.
    min_dwell_seconds:
        Confirmed dwells shorter than this are discarded as noise.
    """

    latitude: float
    longitude: float
    entry_radius: float
    exit_radius: float
    min_outside_observations: int = 3
    min_outside_seconds: float = 30.0
    max_gap_seconds: float = 300.0
    min_dwell_seconds: float = 0.0


class UnknownInterval(NamedTuple):
    """
    A time span for which no trustworthy inside/outside classification exists.

    reason is one of ``gap`` (no sample within ``max_gap_seconds``) or
    ``segment`` (the track switched to another track/segment).
    """

    start_time: mod_datetime.datetime
    end_time: mod_datetime.datetime
    reason: str
    duration_seconds: float


class StationDwell(NamedTuple):
    """
    One station visit.

    arrival_time:
        Timestamp of the first observation classified inside.
    departure_time:
        Timestamp of the first observation of the confirmed outside run, or
        ``None`` when the departure was never confirmed.
    end_time:
        Departure time for complete dwells, otherwise the last observation
        seen for this visit.
    dwell_seconds:
        Confirmed time from arrival to departure, or ``None`` for incomplete
        visits.
    confirmed_inside_seconds:
        Time from arrival to the last observation known to be inside. This is
        the dwell duration that can be claimed without interpolation.
    complete:
        ``True`` when a departure was confirmed by sustained outside
        observations.
    incomplete_reason:
        ``None`` for complete dwells, otherwise one of ``unknown_gap``,
        ``unknown_segment`` or ``end_of_data``.
    """

    arrival_time: mod_datetime.datetime
    departure_time: Optional[mod_datetime.datetime]
    end_time: mod_datetime.datetime
    dwell_seconds: Optional[float]
    confirmed_inside_seconds: float
    complete: bool
    incomplete_reason: Optional[str]


class StationVisitResult(NamedTuple):
    dwells: List[StationDwell]
    unknown_intervals: List[UnknownInterval]


class _Observation(NamedTuple):
    point: GPXTrackPoint
    time: mod_datetime.datetime
    track_no: int
    segment_no: int


def _validate_config(config: StationConfig) -> None:
    if config.entry_radius <= 0:
        raise ValueError('entry_radius must be positive')
    if config.exit_radius <= 0:
        raise ValueError('exit_radius must be positive')
    if config.min_outside_observations < 1:
        raise ValueError('min_outside_observations must be at least 1')
    if config.min_outside_seconds < 0:
        raise ValueError('min_outside_seconds must not be negative')
    if config.max_gap_seconds <= 0:
        raise ValueError('max_gap_seconds must be positive')
    if config.min_dwell_seconds < 0:
        raise ValueError('min_dwell_seconds must not be negative')


def _iter_timed_observations(gpx: GPX) -> List[_Observation]:
    observations: List[_Observation] = []
    for track_no, track in enumerate(gpx.tracks if gpx.tracks else []):
        for segment_no, segment in enumerate(track.segments if track.segments else []):
            for point in segment.points if segment.points else []:
                if point.time is not None:
                    observations.append(_Observation(point, point.time, track_no, segment_no))
    return observations


def _boundary_reason(previous: _Observation, current: _Observation, max_gap_seconds: float) -> Optional[str]:
    if (previous.track_no, previous.segment_no) != (current.track_no, current.segment_no):
        return SEGMENT
    if (current.time - previous.time).total_seconds() > max_gap_seconds:
        return GAP
    return None


def _incomplete_dwell(arrival_time: mod_datetime.datetime,
                      last_inside_time: mod_datetime.datetime,
                      end_time: mod_datetime.datetime,
                      reason: str) -> StationDwell:
    return StationDwell(
        arrival_time=arrival_time,
        departure_time=None,
        end_time=end_time,
        dwell_seconds=None,
        confirmed_inside_seconds=(last_inside_time - arrival_time).total_seconds(),
        complete=False,
        incomplete_reason=reason,
    )


def detect_station_dwells(gpx: GPX, config: StationConfig) -> StationVisitResult:
    """
    Detect station arrivals, departures and confirmed dwell seconds.

    The track points are walked in file order across all tracks and segments.
    Inside/outside classification uses separate entry and exit radii with
    hysteresis; a departure needs a sustained run of outside observations.
    Sampling gaps and segment switches become unknown intervals and terminate
    the current visit as unconfirmed instead of bridging over missing data.
    """

    _validate_config(config)

    dwells: List[StationDwell] = []
    unknown_intervals: List[UnknownInterval] = []

    inside = False
    arrival_time: Optional[mod_datetime.datetime] = None
    last_inside_time: Optional[mod_datetime.datetime] = None
    outside_run_start: Optional[mod_datetime.datetime] = None
    outside_run_count = 0
    previous: Optional[_Observation] = None

    for observation in _iter_timed_observations(gpx):
        if previous is not None:
            boundary = _boundary_reason(previous, observation, config.max_gap_seconds)
            if boundary is not None:
                unknown_intervals.append(UnknownInterval(
                    start_time=previous.time,
                    end_time=observation.time,
                    reason=boundary,
                    duration_seconds=(observation.time - previous.time).total_seconds(),
                ))
                if inside:
                    assert arrival_time is not None
                    assert last_inside_time is not None
                    reason = UNKNOWN_SEGMENT if boundary == SEGMENT else UNKNOWN_GAP
                    dwells.append(_incomplete_dwell(arrival_time, last_inside_time, previous.time, reason))
                    inside = False
                    arrival_time = None
                    last_inside_time = None
                outside_run_start = None
                outside_run_count = 0

        distance = mod_geo.haversine_distance(
            config.latitude, config.longitude,
            observation.point.latitude, observation.point.longitude,
        )
        if inside:
            is_inside = distance <= config.exit_radius
        else:
            is_inside = distance <= config.entry_radius

        if is_inside:
            if not inside:
                inside = True
                arrival_time = observation.time
            last_inside_time = observation.time
            outside_run_start = None
            outside_run_count = 0
        elif inside:
            if outside_run_start is None:
                outside_run_start = observation.time
                outside_run_count = 1
            else:
                outside_run_count += 1

            assert outside_run_start is not None
            outside_span = (observation.time - outside_run_start).total_seconds()
            if (outside_run_count >= config.min_outside_observations
                    and outside_span >= config.min_outside_seconds):
                assert arrival_time is not None
                assert last_inside_time is not None
                dwell_seconds = (outside_run_start - arrival_time).total_seconds()
                if dwell_seconds >= config.min_dwell_seconds:
                    dwells.append(StationDwell(
                        arrival_time=arrival_time,
                        departure_time=outside_run_start,
                        end_time=outside_run_start,
                        dwell_seconds=dwell_seconds,
                        confirmed_inside_seconds=(last_inside_time - arrival_time).total_seconds(),
                        complete=True,
                        incomplete_reason=None,
                    ))
                inside = False
                arrival_time = None
                last_inside_time = None
                outside_run_start = None
                outside_run_count = 0

        previous = observation

    if inside:
        assert arrival_time is not None
        assert last_inside_time is not None
        assert previous is not None
        dwells.append(_incomplete_dwell(arrival_time, last_inside_time, previous.time, END_OF_DATA))

    return StationVisitResult(dwells=dwells, unknown_intervals=unknown_intervals)
