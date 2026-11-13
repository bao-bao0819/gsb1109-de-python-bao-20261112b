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

## Reconciling independently zeroed altimeter segments

When several altimeter recordings meet (for example two instruments handed over at a mountain pass, the second one starting its scale at zero), concatenating the raw tracks invents a vertical jump at the junction. The `gpxpy.altimeter` module gives each segment one constant offset and checks whether a single set of offsets can satisfy:

* **Benchmarks** -- points with a known elevation (`Benchmark`), each with an error tolerance.
* **Overlaps** -- the same ground position walked by two segments (`OverlapObservation`), where the corrected elevations must agree.

Observations are combined in a weighted least-squares adjustment (weights inverse squared tolerance). Segments linked through overlaps form components. A component only gets absolute offsets when it contains a benchmark. Components without any vertical datum stay *floating*: their relative geometry is still checked for internal contradictions, but no absolute elevation is guessed -- only the in-segment climb (which a constant offset cannot change) is delivered.

```python
import gpxpy.altimeter as altimeter

adjuster = altimeter.AltimeterAdjuster(segments, names=['approach', 'descent'])

adjuster.add_benchmark(altimeter.Benchmark(
    46.00, 14.0, 1000.0, name='trailhead', tolerance=2.0))
# Elevated GPX waypoints can be used directly:
# adjuster.add_waypoint_benchmarks(gpx, tolerance=2.0)

adjuster.add_overlap(altimeter.OverlapObservation(
    segment_a=0, segment_b=1, latitude=46.02, longitude=14.0,
    elevation_a=200.0, elevation_b=0.0,
    horizontal_distance=0.0, tolerance=2.0))
# Co-located points can also be detected automatically:
# adjuster.find_overlaps(max_distance=25, min_pairs=3)

report = adjuster.solve()

if not report.compatible:
    for conflict in report.conflicts:
        print(conflict.name, conflict.latitude, conflict.longitude,
              conflict.residual, conflict.tolerance)

for item in report.adjustments:
    if item.floating:
        print(item.name, 'no datum; climb', item.uphill, 'm only')
    else:
        print(item.name, 'offset', item.offset)

# GPX with corrected elevations; floating components keep relative values.
corrected_gpx = adjuster.corrected_gpx(report)
# Or apply the offsets to the input segments in place:
adjuster.apply_offsets(report)
```

See `examples/altimeter_offset_example.py` for a complete runnable example.

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

