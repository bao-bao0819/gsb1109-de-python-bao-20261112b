# Copyright 2011 Tomo Krajina
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Typed extraction of GPX extension data.

Two device vendors can both put a ``temp`` element inside their GPX
extensions while one of them means the reefer (cold-chain) temperature and
the other one the engine temperature. XML prefixes are arbitrary and may be
re-bound in the middle of a document, so matching must be done on namespace
URIs and the full element hierarchy instead of literal prefixed tag names.

The :class:`ObservationMapping` describes business columns with:

* a namespace-aware, full hierarchical path to the extension leaf
  (either Clark notation ``{uri}local`` or prefixed names resolved through
  a ``prefix_map``),
* the target unit and the unit used in the source document,
* a business column name, value type and optional provenance metadata
  (vendor source / channel).

Extraction results in an :class:`ObservationTable` of typed
:class:`ObservationRow` objects. Leaves that no column maps to are kept as
:class:`UnknownExtension` records carrying the original XML fragment so the
mapping can be completed later. Reparsing the same document with a different
set of XML prefixes yields exactly the same business columns because every
path is canonicalized to Clark notation before matching.
"""

import xml.etree.ElementTree as mod_etree

from typing import Any, AnyStr, Callable, Dict, IO, Iterator, List, NamedTuple, Optional, Tuple, Union

from . import gpx as mod_gpx

# ----------------------------------------------------------------------------------------------------
# Unit conversion
# ----------------------------------------------------------------------------------------------------

# Source/target unit pairs. Units are short plain labels ("C", "F", "K", ...);
# new pairs can be registered through register_unit_converter().
_UNIT_CONVERTERS: Dict[Tuple[str, str], Callable[[float], float]] = {
    ('F', 'C'): lambda value: (value - 32.0) * 5.0 / 9.0,
    ('K', 'C'): lambda value: value - 273.15,
    ('C', 'F'): lambda value: value * 9.0 / 5.0 + 32.0,
    ('C', 'K'): lambda value: value + 273.15,
}


def register_unit_converter(source_unit: str, target_unit: str,
                            converter: Callable[[float], float]) -> None:
    """
    Register a converter converting ``source_unit`` values into
    ``target_unit`` values.
    """
    _UNIT_CONVERTERS[(source_unit, target_unit)] = converter


def convert_value(value: float, source_unit: Optional[str],
                  target_unit: Optional[str]) -> float:
    """
    Convert a numeric value between units.

    When no source unit is declared, or source and target units are equal,
    the value is returned unchanged.
    """
    if source_unit is None or target_unit is None or source_unit == target_unit:
        return value
    converter = _UNIT_CONVERTERS.get((source_unit, target_unit))
    if converter is None:
        raise ValueError(
            f'No unit converter from {source_unit!r} to {target_unit!r}; '
            'register one with register_unit_converter()')
    return converter(value)


# ----------------------------------------------------------------------------------------------------
# Path helpers (prefix independent, Clark notation based)
# ----------------------------------------------------------------------------------------------------

def _resolve_segment(segment: str, prefix_map: Dict[str, str]) -> str:
    # Already in Clark notation ({namespace-uri}local-name): URIs may
    # contain slashes, so it must be passed through untouched.
    if segment.startswith('{'):
        return segment
    prefix, separator, local_name = segment.partition(':')
    if not separator:
        return segment
    namespace = prefix_map.get(prefix)
    if namespace is None:
        raise ValueError(
            f'Unknown XML prefix {prefix!r} in path segment {segment!r}; '
            'pass it in the mapping prefix_map')
    return f'{{{namespace}}}{local_name}'


def normalize_path(path: str, prefix_map: Optional[Dict[str, str]]=None) -> str:
    """
    Canonicalize an extension path into Clark notation.

    A path is a ``/`` separated hierarchy of element names. Each segment is
    either already in Clark notation (``{namespace-uri}local-name``) or a
    prefixed name (``prefix:local-name``) resolved through *prefix_map*.
    Bare local names without a prefix stay namespace-less. Slashes inside
    the braces of a Clark namespace URI are not treated as separators.

    Matching on the canonicalized path makes results independent of the
    (possibly re-bound) XML prefixes used in a concrete document.
    """
    prefix_map = prefix_map or {}
    canonical_segments: List[str] = []
    segment_parts: List[str] = []
    in_braces = False
    for char in path.strip('/'):
        if char == '{':
            in_braces = True
            segment_parts.append(char)
        elif char == '}':
            in_braces = False
            segment_parts.append(char)
        elif char == '/' and not in_braces:
            segment = ''.join(segment_parts)
            if segment:
                canonical_segments.append(
                    _resolve_segment(segment, prefix_map))
            segment_parts = []
        else:
            segment_parts.append(char)
    segment = ''.join(segment_parts)
    if segment:
        canonical_segments.append(_resolve_segment(segment, prefix_map))
    return '/'.join(canonical_segments)


def _build_parent_map(root: Any) -> Dict[Any, Any]:
    return {child: parent for parent in root.iter() for child in parent}


def _clark_path(element: Any, parent_map: Dict[Any, Any], root: Any) -> str:
    """
    Full hierarchical path of *element* below the extension *root*, using
    the namespace URIs carried by the ElementTree tags (Clark notation).
    """
    chain: List[Any] = []
    current: Any = element
    while current is not None:
        chain.append(current)
        if current is root:
            break
        current = parent_map.get(current)
    chain.reverse()
    return '/'.join(node.tag for node in chain if isinstance(node.tag, str))


# ----------------------------------------------------------------------------------------------------
# Mapping specification
# ----------------------------------------------------------------------------------------------------

class ObservationColumn:
    """
    Declaration of one typed business column.

    Attributes:
        name: business column name used in the observation table.
        path: namespace-aware full path of the extension leaf, either in
            Clark notation (``{uri}a/{uri}b``) or using prefixes declared in
            the mapping ``prefix_map``.
        unit: normalized unit of the values stored in the column.
        source_unit: unit used by the vendor in the XML; values are converted
            from this unit to ``unit``. ``None`` means no conversion.
        value_type: ``float`` (default), ``int`` or ``str``.
        source: vendor/source identifier kept on every extracted row.
        channel: channel discriminator (e.g. ``fridge`` vs ``engine``) so
            two equally named ``temp`` leaves can coexist in one point.
    """

    __slots__ = ('name', 'path', 'unit', 'source_unit', 'value_type',
                 'source', 'channel', '_canonical_path')

    def __init__(self, name: str, path: str, unit: Optional[str]=None,
                 source_unit: Optional[str]=None, value_type: str='float',
                 source: Optional[str]=None, channel: Optional[str]=None,
                 prefix_map: Optional[Dict[str, str]]=None) -> None:
        self.name = name
        self.path = path
        self.unit = unit
        self.source_unit = source_unit
        if value_type not in ('float', 'int', 'str'):
            raise ValueError(f'Invalid value_type {value_type!r}')
        self.value_type = value_type
        self.source = source
        self.channel = channel
        self._canonical_path = normalize_path(path, prefix_map)

    @classmethod
    def from_dict(cls, data: Dict[str, Any],
                  prefix_map: Optional[Dict[str, str]]=None) -> 'ObservationColumn':
        """ Build a column from a plain dictionary (e.g. imported config). """
        return cls(
            name=data['name'],
            path=data['path'],
            unit=data.get('unit'),
            source_unit=data.get('source_unit'),
            value_type=data.get('value_type', 'float'),
            source=data.get('source'),
            channel=data.get('channel'),
            prefix_map=prefix_map,
        )

    @property
    def canonical_path(self) -> str:
        return self._canonical_path

    def __repr__(self) -> str:
        return (f'ObservationColumn(name={self.name!r}, path={self.path!r}, '
                f'unit={self.unit!r}, source_unit={self.source_unit!r}, '
                f'channel={self.channel!r})')


class ObservationMapping:
    """
    Collection of :class:`ObservationColumn` objects indexed by their
    canonical (prefix-independent) extension path.
    """

    __slots__ = ('columns', 'prefix_map', '_columns_by_path')

    def __init__(self, columns: List[Union[ObservationColumn, Dict[str, Any]]],
                 prefix_map: Optional[Dict[str, str]]=None) -> None:
        self.prefix_map: Dict[str, str] = dict(prefix_map or {})
        self.columns: List[ObservationColumn] = []
        self._columns_by_path: Dict[str, ObservationColumn] = {}
        for column in columns:
            if isinstance(column, dict):
                column = ObservationColumn.from_dict(column, self.prefix_map)
            self.columns.append(column)
            existing = self._columns_by_path.get(column.canonical_path)
            if existing is not None and existing.name != column.name:
                raise ValueError(
                    f'Path {column.canonical_path!r} mapped to both '
                    f'{existing.name!r} and {column.name!r}')
            self._columns_by_path[column.canonical_path] = column

    def column_for_path(self, canonical_path: str) -> Optional[ObservationColumn]:
        return self._columns_by_path.get(canonical_path)

    def __len__(self) -> int:
        return len(self.columns)


# ----------------------------------------------------------------------------------------------------
# Extraction results
# ----------------------------------------------------------------------------------------------------

class PointContext(NamedTuple):
    """ Location of an observation inside the GPX document. """
    kind: str  # 'wpt', 'rtept' or 'trkpt'
    point: Any
    track_no: Optional[int]
    segment_no: Optional[int]
    point_no: Optional[int]


class ObservationRow:
    """
    One typed observation extracted from a point extension leaf.

    The original (pre-conversion) text and the vendor source are always kept
    so values that remain contradictory after unit normalization stay
    traceable to their origin.
    """

    __slots__ = ('context', 'column', 'value', 'raw_value', 'source_path',
                 'source', 'channel', 'unit', 'source_unit')

    def __init__(self, context: PointContext, column: ObservationColumn,
                 value: Any, raw_value: str, source_path: str) -> None:
        self.context = context
        self.column = column
        self.value = value
        self.raw_value = raw_value
        self.source_path = source_path
        self.source = column.source
        self.channel = column.channel
        self.unit = column.unit
        self.source_unit = column.source_unit

    @property
    def point(self) -> Any:
        return self.context.point

    def as_dict(self) -> Dict[str, Any]:
        """ Flat, business-oriented representation of this observation. """
        point = self.context.point
        return {
            'kind': self.context.kind,
            'track_no': self.context.track_no,
            'segment_no': self.context.segment_no,
            'point_no': self.context.point_no,
            'time': point.time,
            'latitude': point.latitude,
            'longitude': point.longitude,
            'elevation': point.elevation,
            'column': self.column.name,
            'value': self.value,
            'unit': self.unit,
            'source_unit': self.source_unit,
            'source': self.source,
            'channel': self.channel,
            'source_path': self.source_path,
            'raw_value': self.raw_value,
        }

    def __repr__(self) -> str:
        return (f'ObservationRow(column={self.column.name!r}, value={self.value!r}, '
                f'unit={self.unit!r}, source={self.source!r}, '
                f'channel={self.channel!r})')


class UnknownExtension:
    """
    An extension subtree containing leaves that no column maps to.

    ``fragment`` is the original extension XML (serialized back from the
    parsed tree) and ``paths`` the canonical paths of the unmatched leaves,
    so the missing mapping can be added later without losing data.
    """

    __slots__ = ('context', 'paths', 'fragment')

    def __init__(self, context: PointContext, paths: List[str],
                 fragment: str) -> None:
        self.context = context
        self.paths = paths
        self.fragment = fragment

    def __repr__(self) -> str:
        return f'UnknownExtension(paths={self.paths!r})'


class ObservationTable:
    """ Typed observation table built from one GPX document. """

    __slots__ = ('columns', 'rows', 'unknown')

    def __init__(self, columns: List[ObservationColumn],
                 rows: List[ObservationRow],
                 unknown: List[UnknownExtension]) -> None:
        self.columns = columns
        self.rows = rows
        self.unknown = unknown

    def column_names(self) -> List[str]:
        """ Business column names in mapping order, without duplicates. """
        names: List[str] = []
        for column in self.columns:
            if column.name not in names:
                names.append(column.name)
        return names

    def rows_for(self, column_name: str) -> List[ObservationRow]:
        return [row for row in self.rows if row.column.name == column_name]

    def to_rows(self) -> List[Dict[str, Any]]:
        """ All observations as plain dictionaries (long format). """
        return [row.as_dict() for row in self.rows]

    def find_conflicts(self, tolerance: float=1e-9) -> List[List[ObservationRow]]:
        """
        Groups of rows targeting the same business column on the same point
        whose normalized values disagree. Every row keeps its own source and
        raw value; nothing is merged or discarded.
        """
        groups: Dict[Tuple[Any, str], List[ObservationRow]] = {}
        for row in self.rows:
            key = (id(row.context.point), row.column.name)
            groups.setdefault(key, []).append(row)
        conflicts: List[List[ObservationRow]] = []
        for group in groups.values():
            if len(group) < 2:
                continue
            values = [row.value for row in group]
            if all(isinstance(value, (int, float))
                   and not isinstance(value, bool) for value in values):
                disagreed = any(abs(float(value) - float(values[0])) > tolerance
                                for value in values[1:])
            else:
                disagreed = any(value != values[0] for value in values[1:])
            if disagreed:
                conflicts.append(group)
        return conflicts

    def to_dataframe(self) -> Any:
        """
        Return the observations as a pandas ``DataFrame``.

        Raises:
            ImportError: when pandas is not installed.
        """
        try:
            import pandas as mod_pandas  # type: ignore[import-untyped]
        except ImportError as e:
            raise ImportError(
                'pandas is required for ObservationTable.to_dataframe()') from e
        return mod_pandas.DataFrame(self.to_rows())


# ----------------------------------------------------------------------------------------------------
# Extraction
# ----------------------------------------------------------------------------------------------------

def _coerce_value(raw_value: str, value_type: str) -> Any:
    if value_type == 'float':
        return float(raw_value.strip())
    if value_type == 'int':
        return int(raw_value.strip())
    return raw_value


def _extract_from_point(context: PointContext, mapping: ObservationMapping,
                        rows: List[ObservationRow],
                        unknown: List[UnknownExtension]) -> None:
    point = context.point
    for extension_root in getattr(point, 'extensions', None) or []:
        if not isinstance(extension_root.tag, str):
            # Comments and other non-element nodes carry no observations.
            continue
        parent_map = _build_parent_map(extension_root)
        unmatched_paths: List[str] = []
        for element in extension_root.iter():
            if not isinstance(element.tag, str):
                continue
            if element.text is None or not element.text.strip():
                continue
            canonical_path = _clark_path(element, parent_map, extension_root)
            column = mapping.column_for_path(canonical_path)
            raw_value = element.text.strip()
            if column is None:
                unmatched_paths.append(canonical_path)
                continue
            value = _coerce_value(raw_value, column.value_type)
            if column.value_type in ('float', 'int'):
                value = convert_value(value, column.source_unit, column.unit)
            rows.append(ObservationRow(
                context=context, column=column, value=value,
                raw_value=raw_value, source_path=canonical_path))
        if unmatched_paths:
            fragment = mod_etree.tostring(extension_root, encoding='unicode')
            unknown.append(UnknownExtension(
                context=context, paths=unmatched_paths, fragment=fragment))


def _iter_point_contexts(gpx: mod_gpx.GPX) -> Iterator[PointContext]:
    for point, track_no, segment_no, point_no in gpx.walk():
        yield PointContext('trkpt', point, track_no, segment_no, point_no)
    for point_no, waypoint in enumerate(gpx.waypoints):
        yield PointContext('wpt', waypoint, None, None, point_no)
    for route_no, route in enumerate(gpx.routes):
        for point_no, point in enumerate(route.points):
            yield PointContext('rtept', point, route_no, None, point_no)


def extract_observations(gpx: mod_gpx.GPX,
                         columns: Union[ObservationMapping,
                                        List[Union[ObservationColumn, Dict[str, Any]]]],
                         prefix_map: Optional[Dict[str, str]]=None) -> ObservationTable:
    """
    Walk every point of a parsed GPX and extract typed extension observations.

    Matching uses the canonical Clark-notation paths of the extension
    elements, so the result does not depend on the XML prefixes chosen by the
    vendor document.
    """
    if isinstance(columns, ObservationMapping):
        mapping = columns
    else:
        mapping = ObservationMapping(columns, prefix_map)

    rows: List[ObservationRow] = []
    unknown: List[UnknownExtension] = []
    for context in _iter_point_contexts(gpx):
        _extract_from_point(context, mapping, rows, unknown)
    return ObservationTable(mapping.columns, rows, unknown)


def parse_observations(xml_or_file: Union[AnyStr, IO[str]],
                       columns: Union[ObservationMapping,
                                      List[Union[ObservationColumn, Dict[str, Any]]]],
                       prefix_map: Optional[Dict[str, str]]=None,
                       version: Optional[str]=None) -> ObservationTable:
    """
    Parse a GPX document and extract typed extension observations in one call.
    """
    from . import parser as mod_parser
    gpx = mod_parser.GPXParser(xml_or_file).parse(version=version)
    return extract_observations(gpx, columns, prefix_map=prefix_map)
