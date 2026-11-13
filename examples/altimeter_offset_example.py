#!/usr/bin/env python3

"""
Reconciling altimeter segments with independent vertical zero points.

Two altimeter recordings meet at a mountain pass.  The second instrument
started its elevation scale from zero, so concatenating the raw tracks
would invent a vertical jump at the pass.  Each recording gets one constant
offset.  Known benchmarks and the section walked by both instruments are
used to check whether a single set of offsets can satisfy everything.
"""

from typing import List

import gpxpy
import gpxpy.altimeter as altimeter
import gpxpy.gpx


def segment(
    latitudes: List[float], elevations: List[float]
) -> gpxpy.gpx.GPXTrackSegment:
    return gpxpy.gpx.GPXTrackSegment([
        gpxpy.gpx.GPXTrackPoint(latitude, 14.0, elevation)
        for latitude, elevation in zip(latitudes, elevations)])


# Recording 1: true elevations 1000 -> 1200, instrument zeroed at 1000 m.
first = segment([46.00, 46.01, 46.02], [0.0, 100.0, 200.0])
# Recording 2: continues over the pass, instrument zeroed at the pass (1200 m).
second = segment([46.02, 46.03, 46.04], [0.0, 80.0, 150.0])

adjuster = altimeter.AltimeterAdjuster(
    [first, second], names=['approach', 'descent'])

# Benchmarks with known elevations, each carrying an error tolerance (m).
adjuster.add_benchmark(altimeter.Benchmark(
    46.00, 14.0, 1000.0, name='trailhead', tolerance=2.0))
adjuster.add_benchmark(altimeter.Benchmark(
    46.04, 14.0, 1350.0, name='ridge', tolerance=2.0))

# The pass was recorded by both instruments.
adjuster.add_overlap(altimeter.OverlapObservation(
    0, 1, 46.02, 14.0, 200.0, 0.0, 0.0, tolerance=2.0))

# Co-located points can also be detected automatically:
# adjuster.find_overlaps(max_distance=25, min_pairs=3)
# Elevated GPX waypoints can serve as benchmarks:
# adjuster.add_waypoint_benchmarks(gpx, tolerance=2.0)

report = adjuster.solve()

for adjustment in report.adjustments:
    if adjustment.floating:
        print(f'{adjustment.name}: no vertical datum, offset undetermined; '
              f'in-segment climb {adjustment.uphill:.1f} m '
              f'(descent {adjustment.downhill:.1f} m)')
    else:
        print(f'{adjustment.name}: offset {adjustment.offset:+.2f} m, '
              f'climb {adjustment.uphill:.1f} m')

if report.compatible:
    print('All observations compatible within their tolerances.')
else:
    print('Incompatible observations:')
    for conflict in report.conflicts:
        print(f'  {conflict.kind} {conflict.name} at '
              f'({conflict.latitude:.4f}, {conflict.longitude:.4f}): '
              f'residual {conflict.residual:+.2f} m '
              f'exceeds tolerance {conflict.tolerance:.2f} m')

# Only datum-connected segments receive corrected absolute elevations.
corrected = adjuster.corrected_gpx(report)
print()
print(corrected.to_xml())
