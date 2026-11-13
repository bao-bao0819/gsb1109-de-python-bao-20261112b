"""
Roadbook (route book) checking for GPX tracks.

A roadbook is an ordered list of stations that a track must visit. Each
station is a coordinate with an allowed radius in meters. This module
checks a whole GPX track against the roadbook and reports how many of
the required stations were visited, in the given order.

Matching rules:

* Visits are matched in the order given by the roadbook and the matched
  timestamps must strictly increase.
* A visit is confirmed while the track is inside the station radius.
* Repeated stations with the same coordinates (e.g. A, B, A) require at
  least one exit and re-entry of the station radius between two visits --
  staying inside the radius the whole time does not count as two visits.

This is deliberately different from looking up the nearest track point
per station: a track that circles around a station and comes back later
would otherwise merge two separate visits into one.
"""

import datetime as mod_datetime

from typing import Any, Dict, Iterable, List, NamedTuple, Optional, Tuple, Union

from . import geo as mod_geo


class RoadbookStation(NamedTuple):
    """ One required stop of a roadbook. Radius is in meters. """
    latitude: float
    longitude: float
    radius: float
    name: Optional[str] = None


class RoadbookVisit(NamedTuple):
    """
    One confirmed station visit.

    time is the matched track point time (strictly increasing between
    consecutive visits). enter_time/exit_time delimit the presence
    window: the period the track stayed inside the station radius during
    this visit.
    """
    station_index: int
    station: RoadbookStation
    time: mod_datetime.datetime
    enter_time: mod_datetime.datetime
    exit_time: mod_datetime.datetime


class RoadbookResult(NamedTuple):
    """
    Outcome of checking a track against a roadbook.

    stations_completed is how many of the required stations were
    confirmed in order. last_station_index is the last confirmable
    station (None if none). next_station_index is the first station
    that could not be confirmed (None when the roadbook is completed).
    visits holds every confirmed visit with its time window.
    """
    completed: bool
    stations_completed: int
    stations_total: int
    last_station_index: Optional[int]
    next_station_index: Optional[int]
    visits: List[RoadbookVisit]


class _PresenceRun(NamedTuple):
    """ A maximal run of consecutive track points inside a station radius. """
    start_seq: int
    end_seq: int
    times: List[mod_datetime.datetime]


def _normalize_station(station: Union[RoadbookStation, Tuple[float, ...], List[float]]) -> RoadbookStation:
    if isinstance(station, RoadbookStation):
        return station
    if isinstance(station, (tuple, list)):
        if len(station) == 3:
            return RoadbookStation(float(station[0]), float(station[1]), float(station[2]))
        if len(station) == 4:
            return RoadbookStation(float(station[0]), float(station[1]), float(station[2]), station[3])  # type: ignore
    raise ValueError(f'Invalid roadbook station: {station!r}. '
                     'Expected RoadbookStation or (latitude, longitude, radius[, name]).')


def _presence_runs(points: List[Any], station: RoadbookStation) -> List[_PresenceRun]:
    runs: List[_PresenceRun] = []
    start_seq: Optional[int] = None
    times: List[mod_datetime.datetime] = []
    for seq, point in enumerate(points):
        distance = mod_geo.haversine_distance(station.latitude, station.longitude,
                                              point.latitude, point.longitude)
        if distance <= station.radius:
            if start_seq is None:
                start_seq = seq
                times = []
            if point.time is not None:
                times.append(point.time)
            end_seq = seq
        else:
            if start_seq is not None:
                runs.append(_PresenceRun(start_seq, end_seq, times))
                start_seq = None
    if start_seq is not None:
        runs.append(_PresenceRun(start_seq, end_seq, times))
    return runs


def check_roadbook(points: Iterable[Any], roadbook: Iterable[Union[RoadbookStation, Tuple[float, ...], List[float]]]) -> RoadbookResult:
    """
    Check an ordered sequence of track points against a roadbook.

    Parameters
    ----------
    points: iterable
        Track points in track order. Each point needs latitude,
        longitude and time (points without time cannot confirm a visit
        but still count for exit/re-entry detection).
    roadbook: iterable
        Ordered stations, each a RoadbookStation or a
        (latitude, longitude, radius[, name]) tuple. Radius in meters.

    Returns
    ----------
    result: RoadbookResult
        How many stations were completed, the last confirmable station
        and the time windows of all confirmed visits.
    """
    stations = [_normalize_station(station) for station in roadbook]
    point_list = list(points)
    runs_per_station = [_presence_runs(point_list, station) for station in stations]

    visits: List[RoadbookVisit] = []
    cursor: Optional[mod_datetime.datetime] = None
    # For stations with the same coordinates, the sequence index where
    # the previously matched presence run ended. The next visit to the
    # same coordinates must use a later run, i.e. the track must have
    # left the radius and entered it again.
    last_run_end_by_coordinates: Dict[Tuple[float, float], int] = {}

    for station_index, station in enumerate(stations):
        coordinates = (station.latitude, station.longitude)
        min_start_seq = last_run_end_by_coordinates.get(coordinates, -1)

        matched_run: Optional[_PresenceRun] = None
        matched_time: Optional[mod_datetime.datetime] = None
        for run in runs_per_station[station_index]:
            if run.start_seq <= min_start_seq:
                continue
            for time in run.times:
                if cursor is None or time > cursor:
                    matched_run = run
                    matched_time = time
                    break
            if matched_run is not None:
                break

        if matched_run is None or matched_time is None:
            break

        visits.append(RoadbookVisit(
            station_index=station_index,
            station=station,
            time=matched_time,
            enter_time=matched_run.times[0],
            exit_time=matched_run.times[-1],
        ))
        cursor = matched_time
        last_run_end_by_coordinates[coordinates] = matched_run.end_seq

    completed = len(visits) == len(stations)
    return RoadbookResult(
        completed=completed,
        stations_completed=len(visits),
        stations_total=len(stations),
        last_station_index=visits[-1].station_index if visits else None,
        next_station_index=None if completed else len(visits),
        visits=visits,
    )
