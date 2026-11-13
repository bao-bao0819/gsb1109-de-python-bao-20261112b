#!/usr/bin/env python3

"""
Engineering stationing with a broken chain (road re-alignment).

The old line ends at ``K12+800`` and the re-alignment joins the new line
at ``K12+300``. Station markers are recorded in pass order; stationing
between markers is interpolated by measured path length and the result is
written back into the GPX as ``stn:stationing`` extensions.
"""

import gpxpy.gpx
from gpxpy.stationing import StationingModel

gpx = gpxpy.gpx.GPX()
track = gpxpy.gpx.GPXTrack()
segment = gpxpy.gpx.GPXTrackSegment()
track.segments.append(segment)
gpx.tracks.append(track)

# Twelve surveyed points in pass order, ~100 m apart along a meridian.
for index in range(12):
    segment.points.append(gpxpy.gpx.GPXTrackPoint(45.0 + index * 0.0009, 15.0))

# Station markers recorded by the survey vehicle (in pass order).
for index, name in ((0, 'K12+000'), (8, 'K12+800'), (9, 'K12+300'), (11, 'K13+000')):
    point = segment.points[index]
    gpx.waypoints.append(gpxpy.gpx.GPXWaypoint(point.latitude, point.longitude, name=name))

model = StationingModel.from_gpx(gpx)

for break_point in model.break_points():
    print(f'Broken chain: incoming {break_point.incoming_station} '
          f'-> outgoing {break_point.outgoing_station} '
          f'({break_point.connector_length_m:.1f} m connector)')

for result in model.all_stationing():
    label = result.station if result.station else f'local {result.local_mileage_m:.1f} m'
    side = f' [{result.side}]' if result.side else ''
    print(f'point {result.index:2d}: {label}{side}')

# Reverse lookup: K12+500 exists on both chains and on the connector span.
for hit in model.find('K12+500'):
    print(f'K12+500 compatible with point {hit.index} (chain {hit.chain_id})')

model.apply_to_gpx(gpx)
print('GPX with stationing extensions:')
print(gpx.to_xml())
