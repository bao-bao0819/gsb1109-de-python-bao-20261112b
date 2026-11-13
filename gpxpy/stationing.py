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
Engineering stationing (``桩号``) for GPX tracks, including support for
broken chains (``断链``) caused by road re-alignment.

The survey vehicle records:

* a GPX track (points in pass order), and
* station markers recorded in pass order, e.g. ``K12+800``.

Between two consecutive markers the engineering stationing is interpolated
by *measured path length* (not by point count or straight-line distance).
When a re-alignment joins the old line ``K12+800`` onto the new line
``K12+300``, identical station numbers legitimately exist on both sides of
the broken chain. Reverse lookups therefore return every compatible track
section, and a query at the break point distinguishes the incoming
(``来向``) and outgoing (``去向``) stakes. Track sections without any
station marker keep their own zero-based local mileage instead of a
fabricated engineering station.
"""

import math as mod_math
import re as mod_re
import xml.etree.ElementTree as mod_etree

from bisect import bisect_right
from dataclasses import dataclass
from typing import Any, Iterable, List, Optional, Sequence, Set, Tuple, Union

from . import geo as mod_geo
from . import gpx as mod_gpx

STATIONING_NAMESPACE: str = 'http://gpxpy.github.io/stationing'
STATIONING_PREFIX: str = 'stn'

SIDE_INCOMING: str = '来向'
SIDE_OUTGOING: str = '去向'

LOCAL_CHAIN_ID: int = -1

_STATIONING_RE = mod_re.compile(r'^\s*K?\s*(\d+)\s*\+\s*(\d+(?:\.\d+)?)\s*$', mod_re.IGNORECASE)


def parse_stationing(value: Union[str, float, int]) -> float:
    """
    Parse an engineering stationing into meters.

    Accepts ``"K12+800"``, ``"12+300.5"`` (case-insensitive optional ``K``)
    or a plain number already expressed in meters (``12800``).
    """
    if isinstance(value, (int, float)):
        return float(value)
    match = _STATIONING_RE.match(value)
    if not match:
        raise ValueError(f'Invalid engineering stationing: {value!r}')
    return int(match.group(1)) * 1000 + float(match.group(2))


def format_stationing(meters: float, prefix: str = 'K', decimals: int = 3) -> str:
    """
    Format meters as an engineering stationing such as ``K12+345.678``.

    Trailing zeros of the meter part are removed, so ``12300`` renders as
    ``K12+300``.
    """
    if meters < 0:
        raise ValueError(f'Engineering stationing must not be negative: {meters}')
    rounded = round(meters, decimals) if decimals > 0 else meters
    kilometers = int(mod_math.floor(rounded / 1000))
    remainder = rounded - kilometers * 1000
    whole, _, fraction = f'{remainder:.{decimals}f}'.partition('.')
    rendered = whole.zfill(3)
    if decimals > 0 and fraction.strip('0'):
        rendered += '.' + fraction.rstrip('0')
    return f'{prefix}{kilometers}+{rendered}'


@dataclass(frozen=True)
class StationMarker:
    """A station marker bound to a track point (by flattened point index)."""

    index: int
    station_m: float
    name: Optional[str] = None
    is_break: bool = False
    """When True, the edge from this marker to the next marker is a broken-chain boundary."""

    @classmethod
    def parse(cls, index: int, station: Union[str, float, int],
              name: Optional[str] = None, is_break: bool = False) -> 'StationMarker':
        """Build a marker from a stationing string such as ``K12+800``."""
        station_m = parse_stationing(station)
        return cls(index=index, station_m=station_m,
                   name=name if name is not None else str(station), is_break=is_break)


@dataclass(frozen=True)
class StationingResult:
    """Engineering stationing of a single track point."""

    index: int
    distance_m: float
    station_m: Optional[float]
    station: Optional[str]
    chain_id: int
    span_id: int
    local: bool
    in_broken_span: bool
    side: Optional[str]
    local_mileage_m: float


@dataclass(frozen=True)
class ReverseHit:
    """One place where a queried stationing occurs along the track."""

    station_m: float
    chain_id: int
    span_id: int
    broken: bool
    start_index: int
    end_index: int
    index: int
    position: float
    side: Optional[str]
    distance_m: float


@dataclass(frozen=True)
class BreakPoint:
    """A broken-chain boundary with its incoming and outgoing stakes."""

    chain_before: int
    chain_after: int
    incoming_index: int
    incoming_station_m: float
    outgoing_index: int
    outgoing_station_m: float
    connector_length_m: float

    @property
    def incoming_station(self) -> str:
        return format_stationing(self.incoming_station_m)

    @property
    def outgoing_station(self) -> str:
        return format_stationing(self.outgoing_station_m)


@dataclass(frozen=True)
class _Span:
    span_id: int
    start_index: int
    end_index: int
    start_distance: float
    end_distance: float
    start_station: Optional[float]
    end_station: Optional[float]
    chain_id: int
    broken: bool
    local: bool

    @property
    def length_m(self) -> float:
        return self.end_distance - self.start_distance

    def contains(self, index: int) -> bool:
        return self.start_index <= index <= self.end_index


class StationingModel:
    """
    Stationing calibration built from a flattened track and ordered markers.

    ``points`` are track points in pass order (for example the output of
    ``gpx.walk(only_points=True)``). Distances are measured with the same
    2D distance logic as gpxpy; pass ``haversine=True`` to force the
    haversine formula on every segment.
    """

    def __init__(self, points: Sequence[mod_geo.Location],
                 markers: Sequence[StationMarker],
                 haversine: bool = False,
                 gaps: Optional[Iterable[int]] = None) -> None:
        """
        ``gaps`` lists flattened point indices where no measured path
        exists between the point and its predecessor (for example GPX
        segment boundaries). Those edges contribute zero length.
        """
        if not points:
            raise ValueError('At least one track point is required')
        self._points: List[mod_geo.Location] = list(points)
        self.haversine: bool = haversine
        self._gaps: Set[int] = set(gaps) if gaps else set()
        self._distances: List[float] = self._measure_distances(
            self._points, haversine, self._gaps)
        self._markers: List[StationMarker] = self._validate_markers(markers, len(self._points))
        self._marker_by_index: dict[int, StationMarker] = {
            marker.index: marker for marker in self._markers}
        self._spans: List[_Span] = self._build_spans(self._markers, self._distances, self._gaps)
        self._span_ends: List[int] = [span.end_index for span in self._spans]

    @staticmethod
    def _measure_distances(points: Sequence[mod_geo.Location], haversine: bool,
                           gaps: Set[int]) -> List[float]:
        distances = [0.0]
        for index, (previous, current) in enumerate(zip(points, points[1:]), start=1):
            if index in gaps:
                distances.append(distances[-1])
                continue
            segment = mod_geo.distance(
                previous.latitude, previous.longitude, None,
                current.latitude, current.longitude, None,
                haversine=haversine)
            distances.append(distances[-1] + segment)
        return distances

    @staticmethod
    def _validate_markers(markers: Sequence[StationMarker], points_no: int) -> List[StationMarker]:
        ordered = sorted(markers, key=lambda marker: marker.index)
        if any(marker.index < 0 or marker.index >= points_no for marker in ordered):
            raise ValueError('Marker index outside the track bounds')
        previous_index: Optional[int] = None
        for marker in ordered:
            if previous_index is not None and marker.index == previous_index:
                raise ValueError(f'Multiple markers on point index {marker.index}')
            if marker.station_m < 0:
                raise ValueError(f'Engineering stationing must not be negative: {marker.station_m}')
            previous_index = marker.index
        return ordered

    @staticmethod
    def _components(points_no: int, gaps: Set[int]) -> List[Tuple[int, int]]:
        """Connected index ranges; a gap breaks the measured path."""
        components: List[Tuple[int, int]] = []
        start = 0
        for index in range(1, points_no):
            if index in gaps:
                components.append((start, index - 1))
                start = index
        components.append((start, points_no - 1))
        return components

    @staticmethod
    def _build_spans(markers: List[StationMarker], distances: List[float],
                     gaps: Set[int]) -> List[_Span]:
        # Spans are built per connected component, so interpolation never
        # crosses a gap; only a marker sitting exactly on a gap is invalid.
        for marker in markers:
            if marker.index in gaps:
                raise ValueError('A station marker cannot sit on a track gap')

        spans: List[_Span] = []
        span_id = 0
        chain_id = 0
        local_chain_id = -1
        engineering_started = False

        def local_span(start_index: int, end_index: int) -> _Span:
            nonlocal local_chain_id, span_id
            span = _Span(
                span_id=span_id, start_index=start_index, end_index=end_index,
                start_distance=distances[start_index], end_distance=distances[end_index],
                start_station=None, end_station=None,
                chain_id=local_chain_id, broken=False, local=True)
            local_chain_id -= 1
            span_id += 1
            return span

        for component_start, component_end in StationingModel._components(len(distances), gaps):
            component_markers = [
                marker for marker in markers
                if component_start <= marker.index <= component_end]

            if not component_markers:
                # No engineering stake in this section: keep its own
                # zero-based local mileage.
                spans.append(local_span(component_start, component_end))
                continue

            # Each disconnected section starts a fresh engineering chain.
            if engineering_started:
                chain_id += 1
            engineering_started = True

            first_index = component_markers[0].index
            if first_index > component_start:
                spans.append(local_span(component_start, first_index))

            for left, right in zip(component_markers, component_markers[1:]):
                broken = StationingModel._is_break(left, right)
                spans.append(_Span(
                    span_id=span_id,
                    start_index=left.index, end_index=right.index,
                    start_distance=distances[left.index], end_distance=distances[right.index],
                    start_station=left.station_m, end_station=right.station_m,
                    chain_id=chain_id, broken=broken, local=False))
                span_id += 1
                if broken:
                    chain_id += 1

            last_index = component_markers[-1].index
            if last_index < component_end:
                spans.append(local_span(last_index, component_end))
        return spans

    @staticmethod
    def _is_break(left: StationMarker, right: StationMarker) -> bool:
        if left.is_break:
            return True
        # Within one continuous chain the stationing strictly increases in
        # pass order. A non-increasing step marks a (re-alignment) break.
        return right.station_m <= left.station_m

    @property
    def markers(self) -> Tuple[StationMarker, ...]:
        return tuple(self._markers)

    @property
    def total_length_m(self) -> float:
        return self._distances[-1]

    @property
    def points_no(self) -> int:
        return len(self._points)

    def break_points(self) -> List[BreakPoint]:
        """All broken-chain boundaries, in pass order."""
        result: List[BreakPoint] = []
        for span in self._spans:
            if not span.broken or span.start_station is None or span.end_station is None:
                continue
            result.append(BreakPoint(
                chain_before=span.chain_id,
                chain_after=span.chain_id + 1,
                incoming_index=span.start_index,
                incoming_station_m=span.start_station,
                outgoing_index=span.end_index,
                outgoing_station_m=span.end_station,
                connector_length_m=span.length_m))
        return result

    def _spans_for(self, index: int) -> List[_Span]:
        if index < 0 or index >= len(self._points):
            raise IndexError(f'Point index outside the track bounds: {index}')
        position = bisect_right(self._span_ends, index)
        candidates: List[_Span] = []
        if position < len(self._spans) and self._spans[position].contains(index):
            candidates.append(self._spans[position])
        if position > 0 and self._spans[position - 1].contains(index):
            candidates.append(self._spans[position - 1])
        return candidates

    def stationing_at(self, index: int) -> StationingResult:
        """
        Engineering stationing for one track point.

        Marker points return their recorded stationing exactly. At a
        broken-chain boundary the broken span wins so that the result
        carries the ``来向``/``去向`` side information.
        """
        spans = self._spans_for(index)
        # Prefer the broken span (side information), then an engineering
        # span over a neighboring local-only span.
        span = next((candidate for candidate in spans if candidate.broken), None)
        if span is None:
            span = next((candidate for candidate in spans if not candidate.local), spans[0])
        result = self._result_for(index, span)
        marker = self._marker_by_index.get(index)
        if marker is not None:
            result = StationingResult(
                index=index, distance_m=result.distance_m,
                station_m=marker.station_m, station=format_stationing(marker.station_m),
                chain_id=result.chain_id, span_id=result.span_id, local=False,
                in_broken_span=result.in_broken_span, side=result.side,
                local_mileage_m=result.local_mileage_m)
        return result

    def all_stationing(self) -> List[StationingResult]:
        """Stationing (or local mileage) for every track point, in order."""
        return [self.stationing_at(index) for index in range(len(self._points))]

    def _result_for(self, index: int, span: _Span) -> StationingResult:
        distance_m = self._distances[index]
        local_mileage = distance_m - span.start_distance
        side: Optional[str] = None
        if span.broken:
            if index == span.start_index:
                side = SIDE_INCOMING
            elif index == span.end_index:
                side = SIDE_OUTGOING
        # The outgoing stake of a broken chain is the first stake of the
        # next chain; connector interior points stay with the old chain.
        chain_id = span.chain_id + 1 if side == SIDE_OUTGOING else span.chain_id
        if span.local or span.start_station is None or span.end_station is None:
            return StationingResult(
                index=index, distance_m=distance_m, station_m=None, station=None,
                chain_id=span.chain_id, span_id=span.span_id, local=True,
                in_broken_span=False, side=None, local_mileage_m=local_mileage)
        if span.length_m <= 0:
            station_m = span.start_station
        else:
            ratio = (distance_m - span.start_distance) / span.length_m
            station_m = span.start_station + ratio * (span.end_station - span.start_station)
        return StationingResult(
            index=index, distance_m=distance_m, station_m=station_m,
            station=format_stationing(station_m),
            chain_id=chain_id, span_id=span.span_id, local=False,
            in_broken_span=span.broken, side=side, local_mileage_m=local_mileage)

    def find(self, station: Union[str, float, int]) -> List[ReverseHit]:
        """
        Reverse lookup: every track section compatible with ``station``.

        A stationing duplicated across a broken chain yields one hit per
        chain, ordered by pass order.
        """
        target = parse_stationing(station)
        # Keyed by (point index, chain): distinct chains sharing a boundary
        # point are different answers; within one chain the plain span and
        # the broken span describe the same stake, so merge and keep the
        # hit carrying incoming/outgoing side information.
        hits_by_key: dict[Tuple[int, int], ReverseHit] = {}
        for span in self._spans:
            if span.local or span.start_station is None or span.end_station is None:
                continue
            low = min(span.start_station, span.end_station)
            high = max(span.start_station, span.end_station)
            if not low - 1e-9 <= target <= high + 1e-9:
                continue
            if span.end_station == span.start_station:
                # Both boundary points carry the same stationing. For a
                # broken chain return both (incoming and outgoing).
                if abs(target - span.start_station) > 1e-9:
                    continue
                ratios: Tuple[float, ...] = (0.0, 1.0) if span.broken else (0.0,)
            else:
                ratio = (target - span.start_station) / (span.end_station - span.start_station)
                ratios = (min(1.0, max(0.0, ratio)),)
            for ratio in ratios:
                hit_distance = span.start_distance + ratio * span.length_m
                index = self._nearest_index(span, hit_distance)
                side: Optional[str] = None
                if span.broken:
                    if ratio <= 0.0:
                        side = SIDE_INCOMING
                    elif ratio >= 1.0:
                        side = SIDE_OUTGOING
                hit_chain = span.chain_id + 1 if side == SIDE_OUTGOING else span.chain_id
                hit = ReverseHit(
                    station_m=target, chain_id=hit_chain, span_id=span.span_id,
                    broken=span.broken, start_index=span.start_index, end_index=span.end_index,
                    index=index, position=ratio, side=side, distance_m=hit_distance)
                key = (index, hit_chain)
                previous = hits_by_key.get(key)
                if previous is None or (previous.side is None and hit.side is not None):
                    hits_by_key[key] = hit
        return [hit for _, hit in sorted(hits_by_key.items(), key=lambda item: item[0])]

    def _nearest_index(self, span: _Span, hit_distance: float) -> int:
        best_index = span.start_index
        best_delta = float('inf')
        for index in range(span.start_index, span.end_index + 1):
            delta = abs(self._distances[index] - hit_distance)
            if delta < best_delta:
                best_delta = delta
                best_index = index
        return best_index

    def apply_to_gpx(self, gpx: mod_gpx.GPX) -> None:
        """
        Write the computed stationing back onto the GPX track points as
        ``stn:stationing`` extension elements.

        The GPX must contain the same points, in the same order, used to
        build the model.
        """
        points = list(gpx.walk(only_points=True))
        if len(points) != len(self._points):
            raise ValueError('GPX track point count does not match the stationing model')
        gpx.nsmap[STATIONING_PREFIX] = STATIONING_NAMESPACE
        for result in self.all_stationing():
            point = points[result.index]
            point.extensions = [
                node for node in point.extensions
                if getattr(node, 'tag', '') != f'{{{STATIONING_NAMESPACE}}}stationing']
            attributes = {
                'chain': str(result.chain_id),
                'span': str(result.span_id),
                'local': 'true' if result.local else 'false',
                'broken': 'true' if result.in_broken_span else 'false',
                'side': result.side or '',
                'distance_m': f'{result.distance_m:.6f}',
                'local_mileage_m': f'{result.local_mileage_m:.6f}',
                'station': result.station or '',
            }
            node = mod_etree.Element(f'{{{STATIONING_NAMESPACE}}}stationing', attributes)
            point.extensions.append(node)

    @staticmethod
    def read_point_stationing(gpx: mod_gpx.GPX) -> List[StationingResult]:
        """Read ``stn:stationing`` extensions previously written to points."""
        results: List[StationingResult] = []
        for index, point in enumerate(gpx.walk(only_points=True)):
            node = StationingModel._find_extension(point.extensions, 'stationing')
            if node is None:
                continue
            station_text = node.attrib.get('station', '')
            side_text = node.attrib.get('side', '')
            results.append(StationingResult(
                index=index,
                distance_m=float(node.attrib.get('distance_m', '0')),
                station_m=parse_stationing(station_text) if station_text else None,
                station=station_text or None,
                chain_id=int(node.attrib.get('chain', str(LOCAL_CHAIN_ID))),
                span_id=int(node.attrib.get('span', '0')),
                local=node.attrib.get('local') == 'true',
                in_broken_span=node.attrib.get('broken') == 'true',
                side=side_text or None,
                local_mileage_m=float(node.attrib.get('local_mileage_m', '0'))))
        return results

    @staticmethod
    def _find_extension(extensions: List[Any], local_name: str) -> Optional[Any]:
        for node in extensions:
            tag = getattr(node, 'tag', '')
            if tag == f'{{{STATIONING_NAMESPACE}}}{local_name}' or tag == local_name:
                return node
        return None

    @classmethod
    def from_gpx(cls, gpx: mod_gpx.GPX,
                 markers: Optional[Sequence[StationMarker]] = None,
                 haversine: bool = False) -> 'StationingModel':
        """
        Build a model from a GPX object.

        If ``markers`` is omitted, markers are derived from GPX waypoints
        whose name parses as a stationing (``K12+800``). Each waypoint is
        snapped to its nearest track point and ordered by pass order. A
        waypoint carrying a ``stn:break`` extension element is treated as
        an explicit broken-chain boundary; otherwise breaks are
        auto-detected from non-increasing stationing steps.
        """
        points: List[mod_geo.Location] = list(gpx.walk(only_points=True))
        if markers is None:
            markers = cls._markers_from_waypoints(gpx, points, haversine)
        return cls(points, markers, haversine=haversine,
                   gaps=cls._segment_boundary_gaps(gpx))

    @staticmethod
    def _segment_boundary_gaps(gpx: mod_gpx.GPX) -> List[int]:
        """Flattened indices where a new GPX segment begins (no measured edge)."""
        gaps: List[int] = []
        offset = 0
        for track in gpx.tracks:
            for segment_no, segment in enumerate(track.segments):
                if segment_no > 0:
                    gaps.append(offset)
                offset += len(segment.points)
            # A new track also starts a disconnected section:
            gaps.append(offset)
        if gaps:
            gaps.pop()
        return gaps

    @staticmethod
    def _markers_from_waypoints(gpx: mod_gpx.GPX,
                                points: Sequence[mod_geo.Location],
                                haversine: bool) -> List[StationMarker]:
        markers: List[StationMarker] = []
        used_indices: set[int] = set()
        for waypoint in gpx.waypoints:
            name = waypoint.name or ''
            try:
                station_m = parse_stationing(name)
            except ValueError:
                continue
            nearest_index = StationingModel._nearest_point_index(waypoint, points, haversine)
            if nearest_index in used_indices:
                raise ValueError(
                    f'Waypoint {name!r} snaps to an already marked track point {nearest_index}')
            used_indices.add(nearest_index)
            is_break = StationingModel._waypoint_is_break(waypoint)
            markers.append(StationMarker(
                index=nearest_index, station_m=station_m, name=name, is_break=is_break))
        markers.sort(key=lambda marker: marker.index)
        return markers

    @staticmethod
    def _waypoint_is_break(waypoint: mod_gpx.GPXWaypoint) -> bool:
        for node in waypoint.extensions:
            if getattr(node, 'tag', '') == f'{{{STATIONING_NAMESPACE}}}break' \
                    and node.attrib.get('value', 'true') == 'true':
                return True
        return False

    @staticmethod
    def _nearest_point_index(location: mod_geo.Location,
                             points: Sequence[mod_geo.Location],
                             haversine: bool) -> int:
        best_index = 0
        best_distance = float('inf')
        for index, point in enumerate(points):
            distance = mod_geo.distance(
                location.latitude, location.longitude, None,
                point.latitude, point.longitude, None,
                haversine=haversine)
            if distance < best_distance:
                best_distance = distance
                best_index = index
        return best_index
