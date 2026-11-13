#!/usr/bin/env python3

"""
Typed extraction of multi-vendor GPX extension observations.

Two vendors both write a ``temp`` element into their point extensions:
one means the reefer temperature, the other the engine temperature. The
mapping below disambiguates them by namespace URI and the full element
hierarchy, normalizes units to Celsius and keeps the source on every row.

The same business columns come out regardless of the XML prefixes used in
the concrete document.
"""

import gpxpy

COLD_NS = 'https://vendor-cold.example.com/telemetry/1'
ENGINE_NS = 'https://vendor-engine.example.com/telemetry/1'

# Prefix -> namespace URI. Prefixes are only used to resolve the paths in
# this configuration; matching itself happens on the URIs.
prefix_map = {
    'a': COLD_NS,
    'b': ENGINE_NS,
    'c': COLD_NS,  # the cold namespace may be re-bound to another prefix
}

# Column mapping: namespace-aware full hierarchy, type and units.
columns = [
    {
        'name': 'fridge_temperature_c',
        'path': 'a:telemetry/a:temp',
        'unit': 'C',
        'source_unit': 'C',
        'value_type': 'float',
        'source': 'vendor-cold',
        'channel': 'fridge',
    },
    {
        'name': 'engine_temperature_c',
        'path': 'b:engine/b:temp',
        'unit': 'C',
        'source_unit': 'F',  # the engine vendor reports Fahrenheit
        'value_type': 'float',
        'source': 'vendor-engine',
        'channel': 'engine',
    },
]

for path in ('test_files/multivendor_telemetry.gpx',
             'test_files/multivendor_telemetry_reprefixed.gpx'):
    with open(path) as gpx_file:
        table = gpxpy.parse_observations(gpx_file, columns,
                                         prefix_map=prefix_map)

    print(f'--- {path} ---')
    print('business columns:', table.column_names())
    for row in table.to_rows():
        print(f"  {row['kind']} {row['column']}={row['value']} {row['unit']} "
              f"(raw {row['raw_value']} {row['source_unit']} from "
              f"{row['source']}/{row['channel']})")

    # Extension leaves without a mapping are kept as raw XML fragments so
    # the mapping can be completed later.
    for unknown in table.unknown:
        print('  unmapped leaves:', unknown.paths)
