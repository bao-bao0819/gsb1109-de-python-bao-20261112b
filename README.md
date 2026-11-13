[![Run Tests](https://github.com/tkrajina/gpxpy/actions/workflows/test.yml/badge.svg)](https://github.com/tkrajina/gpxpy/actions/workflows/test.yml)

# gpxpy -- GPX file parser

This is a simple Python library for parsing and manipulating GPX files. GPX is an XML based format for GPS tracks.

You can see it in action on [my online GPS track editor and organizer](http://www.trackprofiler.com).

There is also a Golang port of gpxpy: [gpxgo](http://github.com/tkrajina/gpxgo).

See also [srtm.py](https://github.com/tkrajina/srtm.py) if your track lacks elevation data.

## Usage

```python
import gpxpy
import gpxpy.gpx

# Parsing an existing file:
# -------------------------

gpx_file = open('test_files/cerknicko-jezero.gpx', 'r')

gpx = gpxpy.parse(gpx_file)

for track in gpx.tracks:
    for segment in track.segments:
        for point in segment.points:
            print(f'Point at ({point.latitude},{point.longitude}) -> {point.elevation}')

for waypoint in gpx.waypoints:
    print(f'waypoint {waypoint.name} -> ({waypoint.latitude},{waypoint.longitude})')

for route in gpx.routes:
    print('Route:')
    for point in route.points:
        print(f'Point at ({point.latitude},{point.longitude}) -> {point.elevation}')

# There are many more utility methods and functions:
# You can manipulate/add/remove tracks, segments, points, waypoints and routes and
# get the GPX XML file from the resulting object:

print('GPX:', gpx.to_xml())

# Creating a new file:
# --------------------

gpx = gpxpy.gpx.GPX()

# Create first track in our GPX:
gpx_track = gpxpy.gpx.GPXTrack()
gpx.tracks.append(gpx_track)

# Create first segment in our GPX track:
gpx_segment = gpxpy.gpx.GPXTrackSegment()
gpx_track.segments.append(gpx_segment)

# Create points:
gpx_segment.points.append(gpxpy.gpx.GPXTrackPoint(2.1234, 5.1234, elevation=1234))
gpx_segment.points.append(gpxpy.gpx.GPXTrackPoint(2.1235, 5.1235, elevation=1235))
gpx_segment.points.append(gpxpy.gpx.GPXTrackPoint(2.1236, 5.1236, elevation=1236))

# You can add routes and waypoints, too...

print('Created GPX:', gpx.to_xml())
```

## GPX version

gpx.py can parse and generate GPX 1.0 and 1.1 files. The generated file will always be a valid XML document, but it may not be (strictly speaking) a valid GPX document. For example, if you set gpx.email to "my.email AT mail.com" the generated GPX tag won't confirm to the regex pattern. And the file won't be valid. Most applications will ignore such errors, but... Be aware of this!

Be aware that the gpxpy object model *is not 100% equivalent* with the underlying GPX XML file schema. That's because the library object model works with both GPX 1.0 and 1.1.

For example, GPX 1.0 specified a `speed` attribute for every track point, but that was removed in GPX 1.1. If you parse GPX 1.0 and serialize back with `gpx.to_xml()` everything will work fine. But if you have a GPX 1.1 object, changes in the `speed` attribute will be lost after `gpx.to_xml()`. If you want to force using 1.0, you can `gpx.to_xml(version="1.0")`. Another possibility is to use `extensions` to save the speed in GPX 1.1.

## GPX extensions

gpx.py preserves GPX extensions. They are stored as [ElementTree](https://docs.python.org/2/library/xml.etree.elementtree.html#module-xml.etree.ElementTree) DOM objects. Extensions are part of GPX 1.1, and will be ignored when serializing a GPX object in a GPX 1.0 file.

## XML parsing

The GPX version is automatically determined when parsing by reading the version attribute in the gpx node. If this attribute is not present then the version is assumed to be 1.0. A specific version can be forced by setting the `version` parameter in the parse function. Possible values for the 'version' parameter are `1.0`, `1.1` and `None`.

## GPX max speed

Gpxpy is a GPX parser and by using it you have access to all the data from the original GPX file. The additional methods to calculate stats have some additional heuristics to remove common GPS errors. For example, to calculate `max_speed` it removes the top `5%` of speeds and points with nonstandard distance (those are usually GPS errors).

"Raw" max speed can be calculated with:

    moving_data = gpx.get_moving_data(raw=True)

## Clock synchronisation

`gpxpy.time_sync` maps a device track (for example a car logger) onto a
reference time axis using a handover list of paired button presses. The
handover list supplies only a matching tolerance; the clock offset and the
clock rate are fitted per power-on session (one track segment by default, or
explicit session keys when a segment contains a restart).

The fit keeps every model allowed by all paired events, so a calibrated time
is reported as a range. If the range crosses a minute boundary, both minute
candidates are kept rather than an arbitrary one being chosen. A session
whose clock rate cannot be identified (for example only one paired instant)
is reported as skipped; points that recorded no device time are skipped too.
A restart is never bridged by extending a previous session's slope.

```python
import gpxpy.time_sync as time_sync

handover = [
    {'id': 'press-1', 'reference': '2026-01-01T08:00:00Z', 'device': '2026-01-01T07:59:35Z'},
    {'id': 'press-2', 'reference': '2026-01-01T08:20:00Z', 'device': '2026-01-01T08:19:35Z'},
]
events = time_sync.parse_sync_events(handover)

calibration = time_sync.calibrate_gpx(gpx, events, tolerance_seconds=2)
work_orders = time_sync.parse_work_orders([
    {'id': 'wo-1', 'minute': '2026-01-01T08:05:00Z'},
])
candidates = time_sync.match_work_orders(calibration, work_orders)

# Write the nominal calibrated times back when a single timestamp is needed:
time_sync.apply_calibrated_times(gpx, calibration)
```

`calibration.skipped_points` lists every point that was not calibrated and
why. `WorkOrderCandidates.work_order_ids` lists alternatives; an ambiguous
point intentionally keeps more than one.

## Pull requests

Branches:

* `master` contains the code of the latest release
* `dev` branch is where code for the next release should go.

Send your pull request against `dev`, not `master`!

Before sending a pull request -- check that all tests are OK.  Run all the static typing checks and unit tests with:

    $ make mypy-and-tests

Run a single test with:

    $ python3 -m unittest test.GPXTests.test_haversine_and_nonhaversine

Gpxpy runs only with Python 3.10+. The code must have type hints and must pass all the mypy checks.

## GPX tools

Additional command-line tools for GPX files can be downloaded here <https://github.com/tkrajina/gpx-cmd-tools> or installed with:

```
pip install gpx-cmd-tools
```

## License

GPX.py is licensed under the [Apache License, Version 2.0](http://www.apache.org/licenses/LICENSE-2.0)

