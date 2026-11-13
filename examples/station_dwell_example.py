#!/usr/bin/env python3

"""
Example: detect a station dwell from an in-memory track.

The track contains one GPS jump outside the geofence while the vehicle is
parked. The jump does not form a sustained outside run, so it is ignored and
the visit is reported as a single dwell.
"""

import datetime

import gpxpy.geo
import gpxpy.gpx
import gpxpy.station

start = datetime.datetime(2026, 11, 13, 8, 0, 0)
one_degree_meters = gpxpy.geo.ONE_DEGREE


def track_point(meters_from_station: float, seconds: float) -> gpxpy.gpx.GPXTrackPoint:
    return gpxpy.gpx.GPXTrackPoint(
        latitude=meters_from_station / one_degree_meters,
        longitude=0.0,
        time=start + datetime.timedelta(seconds=seconds),
    )


gpx = gpxpy.gpx.GPX()
track = gpxpy.gpx.GPXTrack()
gpx.tracks.append(track)
segment = gpxpy.gpx.GPXTrackSegment()
track.segments.append(segment)

# approach -> parked inside -> one out-of-fence jump -> parked -> sustained departure
for distance, seconds in [
    (300, 0),
    (20, 10), (20, 20), (20, 30),
    (500, 40),
    (20, 50), (20, 60),
    (300, 70), (300, 80), (300, 90), (300, 100),
]:
    segment.points.append(track_point(distance, seconds))

config = gpxpy.station.StationConfig(
    latitude=0.0,
    longitude=0.0,
    entry_radius=100.0,
    exit_radius=150.0,
    min_outside_observations=3,
    min_outside_seconds=30.0,
)

result = gpxpy.station.detect_station_dwells(gpx, config)

for dwell in result.dwells:
    print(f'arrival={dwell.arrival_time} departure={dwell.departure_time} '
          f'dwell_seconds={dwell.dwell_seconds} complete={dwell.complete}')
