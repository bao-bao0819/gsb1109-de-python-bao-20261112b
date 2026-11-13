#!/usr/bin/env python3
"""
Cross-check a separation report between two tracks.

Two devices on the same vehicle sample at different frequencies.  Both are
recorded in the same local planar system (x/y in meters).  This example uses
small, hand-checkable inputs and prints the durations implied by the shared
time coverage.

Run with:
    python3 examples/separation/separation_example.py
"""

import datetime as mod_datetime

from gpxpy.separation import PlanarPoint, calculate_separation


BASE = mod_datetime.datetime(2026, 1, 1, 8, 0, 0)


def point(seconds: float, x: float, y: float=0.0) -> PlanarPoint:
    return PlanarPoint(BASE + mod_datetime.timedelta(seconds=seconds), x, y)


def show(title: str, track_a, track_b, threshold: float,
         expected: str) -> None:
    report = calculate_separation(track_a, track_b, threshold)
    print(title)
    print(f'  shared coverage : {report.coverage_seconds:g}s {report.coverage_intervals}')
    print(f'  exceeds {threshold:g}   : {report.exceedance_seconds:g}s {report.exceedance_intervals}')
    print(f'  max separation  : {report.max_separation:g}')
    print(f'  hand calculation: {expected}')
    print()


def main() -> None:
    # 1) Static A, B at 5 m/s. Separation reaches 100 m at t=20s and never
    #    falls back, so exceedance is 100-20 = 80s. There is no sample of
    #    either device at t=20s.
    show('1. Different rates, crossing between samples',
         [point(0, 0), point(100, 0)],
         [point(0, 0), point(40, 200), point(80, 400), point(100, 500)],
         100, '100 - 20 = 80s')

    # 2) Same geometry with the slow sampler first / other side. Swapping the
    #    arguments must give exactly the same answer.
    show('2. Same input, tracks swapped (must equal case 1)',
         [point(0, 0), point(40, 200), point(80, 400), point(100, 500)],
         [point(0, 0), point(100, 0)],
         100, '100 - 20 = 80s')

    # 3) Redundant samples on A's straight constant-velocity line. A runs
    #    (0,0) -> (100,500) in x; inserting (25,125), (50,250), (75,375)
    #    changes nothing. B is the same line shifted 60 in y, and only
    #    sampled from t=5s to t=110s, so shared coverage is [5,100] = 95s
    #    and every second of it exceeds 50.
    show('3. Redundant samples on a straight constant-velocity track',
         [point(0, 0), point(25, 125), point(50, 250), point(75, 375), point(100, 500)],
         [point(5, 25, 60), point(55, 275, 60), point(110, 550, 60)],
         50, '95s shared coverage, all exceeded')

    # 4) Two crossings, neither at a sample: relative x = -300 + 10t over
    #    [0,60]. |x| > 100 on (0,20) and (40,60): 20 + 20 = 40s.
    show('4. Two crossings, both between samples',
         [point(0, 0), point(60, 0)],
         [point(0, -300), point(60, 300)],
         100, '20 + 20 = 40s')

    # 5) Coverage is only the intersection. A covers [0,100], B covers
    #    [30,150], so shared coverage is 70s; B's y = 10t-600 crosses
    #    |y|=100 at t=50 and t=70: 20 + 30 = 50s.
    show('5. Partial overlap, only the intersection counts',
         [point(0, 0), point(100, 0)],
         [point(30, -300), point(100, 400), point(150, 900)],
         100, '70s coverage; 20 + 30 = 50s exceeded')

    # 6) Threshold is strict: separation exactly 100 for the whole time
    #    is not an exceedance.
    show('6. Exactly at the threshold is not exceeded',
         [point(0, 0), point(60, 0)],
         [point(0, 100), point(60, 100)],
         100, '0s exceeded, max = 100')

    # 7) No shared coverage at all.
    show('7. No shared coverage',
         [point(0, 0), point(40, 0)],
         [point(50, 1000), point(90, 1000)],
         1, '0s coverage, 0s exceeded')


if __name__ == '__main__':
    main()
