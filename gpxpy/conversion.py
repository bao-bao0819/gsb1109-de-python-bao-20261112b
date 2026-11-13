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
GPX 1.1 -> 1.0 down-conversion with a point-by-point round-trip ledger.

GPX 1.0 terminals cannot represent every GPX 1.1 construct. The three
relevant differences handled here are:

* ``speed``/``course`` are native GPX 1.0 track-point elements but do not
  exist in GPX 1.1, where speed is usually carried in a vendor extension.
* Links are ``<url>``/``<urlname>`` text elements in GPX 1.0 but
  ``<link href="...">`` elements (with an optional ``<type>``) in 1.1.
* Vendor ``<extensions>`` exist only in GPX 1.1 and are dropped entirely
  by a GPX 1.0 terminal.

Each reconciled field ends up in exactly one of three provenance classes:

* ``recoverable`` -- the 1.0 reply itself carries the value and it matches
  the original batch;
* ``mapped`` -- the 1.0 format cannot hold the value, so it is restored
  from the batch manifest travelling with the conversion;
* ``modified`` -- the 1.0 reply carries a different value, i.e. the
  terminal actually edited it, and the reply wins.

Terminals may delete points and reorder same-named waypoints. Points are
therefore matched on unique coordinates within their container, never by
list position or name alone. Extensions whose owner point cannot be
identified uniquely are suspended instead of being attached to a
different observation.
"""

import copy as mod_copy
import json as mod_json
from typing import Any, AnyStr, Dict, IO, List, Optional, Set, Tuple, cast

try:
    import xml.etree.cElementTree as mod_etree
except ImportError:
    import xml.etree.ElementTree as mod_etree  # type: ignore

from . import gpx as mod_gpx

MANIFEST_VERSION = 1
TARGET_VERSION = '1.0'
SOURCE_VERSION = '1.1'

# Field provenance classes
FIELD_RECOVERABLE = 'recoverable'
FIELD_MAPPED = 'mapped'
FIELD_MODIFIED = 'modified'

# Point reconciliation statuses
POINT_UNCHANGED = 'unchanged'
POINT_RESTORED = 'restored'
POINT_MODIFIED = 'modified'
POINT_LOST = 'lost'
POINT_NEW = 'new'
POINT_AMBIGUOUS = 'ambiguous'

# Fields every point type carries in both GPX versions and that therefore
# survive a 1.0 round trip on their own (link/link_text map to url/urlname):
COMMON_POINT_FIELDS: List[str] = [
    'latitude', 'longitude', 'elevation', 'time',
    'magnetic_variation', 'geoid_height', 'name', 'comment',
    'description', 'source', 'link', 'link_text', 'symbol', 'type',
    'type_of_gpx_fix', 'satellites', 'horizontal_dilution',
    'vertical_dilution', 'position_dilution', 'age_of_dgps_data',
    'dgps_id',
]

# GPX 1.0 track points additionally carry speed/course natively:
NATIVE_10_TRACK_FIELDS: List[str] = ['speed', 'course']

# No GPX 1.0 element exists for these; they only survive via the manifest:
MANIFEST_ONLY_POINT_FIELDS: List[str] = ['link_type']


# ----------------------------------------------------------------------------------------------------
# Stable point keys and container partitions
# ----------------------------------------------------------------------------------------------------

def waypoint_key(point_no: int) -> str:
    return f'wpt:{point_no}'


def route_point_key(route_no: int, point_no: int) -> str:
    return f'rte:{route_no}:{point_no}'


def track_point_key(track_no: int, segment_no: int, point_no: int) -> str:
    return f'trk:{track_no}:{segment_no}:{point_no}'


def _partition_of(key: str) -> Tuple[str, ...]:
    parts = key.split(':')
    kind = parts[0]
    if kind == 'wpt':
        return ('wpt',)
    if kind == 'rte':
        return ('rte', parts[1])
    return ('trk', parts[1], parts[2])


def _iter_point_keys(gpx: mod_gpx.GPX) -> List[Tuple[str, Any]]:
    """All observation points in document order with their stable key."""
    result: List[Tuple[str, Any]] = []
    for point_no, waypoint in enumerate(gpx.waypoints):
        result.append((waypoint_key(point_no), waypoint))
    for route_no, route in enumerate(gpx.routes):
        for point_no, point in enumerate(route.points):
            result.append((route_point_key(route_no, point_no), point))
    for track_no, track in enumerate(gpx.tracks):
        for segment_no, segment in enumerate(track.segments):
            for point_no, point in enumerate(segment.points):
                result.append((track_point_key(track_no, segment_no, point_no), point))
    return result


def _find_point(gpx: mod_gpx.GPX, key: str) -> Optional[Any]:
    parts = key.split(':')
    kind = parts[0]
    indices = [int(part) for part in parts[1:]]
    try:
        if kind == 'wpt':
            return gpx.waypoints[indices[0]]
        if kind == 'rte':
            return gpx.routes[indices[0]].points[indices[1]]
        if kind == 'trk':
            return gpx.tracks[indices[0]].segments[indices[1]].points[indices[2]]
    except IndexError:
        return None
    return None


# ----------------------------------------------------------------------------------------------------
# ElementTree extension elements <-> JSON-safe data
# ----------------------------------------------------------------------------------------------------

def _element_to_json(element: Any) -> Dict[str, Any]:
    return {
        'tag': element.tag,
        'attrib': dict(element.attrib),
        'text': element.text,
        'tail': element.tail,
        'children': [_element_to_json(child) for child in element],
    }


def _element_from_json(data: Dict[str, Any]) -> Any:
    element = mod_etree.Element(cast(str, data['tag']), attrib=dict(data.get('attrib') or {}))
    element.text = data.get('text')
    element.tail = data.get('tail')
    for child_data in data.get('children') or []:
        element.append(_element_from_json(child_data))
    return element


def _elements_to_json(elements: List[Any]) -> List[Dict[str, Any]]:
    return [_element_to_json(element) for element in elements]


def _elements_from_json(data: Optional[List[Dict[str, Any]]]) -> List[Any]:
    if not data:
        return []
    return [_element_from_json(element_data) for element_data in data]


# ----------------------------------------------------------------------------------------------------
# Manifest (batch sidecar)
# ----------------------------------------------------------------------------------------------------

def _point_manifest_entry(point: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {}
    for field_name in MANIFEST_ONLY_POINT_FIELDS:
        value = getattr(point, field_name, None)
        if value is not None:
            entry[field_name] = value
    if point.extensions:
        entry['extensions'] = _elements_to_json(point.extensions)
    return entry


def build_manifest(gpx: mod_gpx.GPX) -> Dict[str, Any]:
    """
    Collect every value that cannot survive a GPX 1.0 round trip.

    The returned object is JSON-serialisable and must travel with the
    batch (typically alongside the generated 1.0 file). It is the only
    source from which 1.1-only data can be restored later. Native GPX 1.0
    fields such as track-point ``speed``/``course`` or ``url``/``urlname``
    links are deliberately not stored because the reply carries them.
    """
    points: Dict[str, Any] = {}
    for key, point in _iter_point_keys(gpx):
        entry = _point_manifest_entry(point)
        if entry:
            points[key] = entry

    route_extensions: Dict[str, Any] = {}
    for route_no, route in enumerate(gpx.routes):
        if route.extensions:
            route_extensions[str(route_no)] = _elements_to_json(route.extensions)

    track_extensions: Dict[str, Any] = {}
    segment_extensions: Dict[str, Any] = {}
    for track_no, track in enumerate(gpx.tracks):
        if track.extensions:
            track_extensions[str(track_no)] = _elements_to_json(track.extensions)
        for segment_no, segment in enumerate(track.segments):
            if segment.extensions:
                segment_extensions[f'{track_no}:{segment_no}'] = \
                    _elements_to_json(segment.extensions)

    manifest: Dict[str, Any] = {
        'gpxpy_manifest_version': MANIFEST_VERSION,
        'source_gpx_version': SOURCE_VERSION,
        'target_gpx_version': TARGET_VERSION,
        'nsmap': dict(gpx.nsmap),
        'points': points,
    }
    if route_extensions:
        manifest['route_extensions'] = route_extensions
    if track_extensions:
        manifest['track_extensions'] = track_extensions
    if segment_extensions:
        manifest['segment_extensions'] = segment_extensions
    if gpx.metadata_extensions:
        manifest['metadata_extensions'] = _elements_to_json(gpx.metadata_extensions)
    if gpx.extensions:
        manifest['root_extensions'] = _elements_to_json(gpx.extensions)
    return manifest


def manifest_to_json(manifest: Dict[str, Any], indent: Optional[int] = 2) -> str:
    return mod_json.dumps(manifest, indent=indent, ensure_ascii=False)


def manifest_from_json(json_or_file: AnyStr | IO[str]) -> Dict[str, Any]:
    text = json_or_file.read() if hasattr(json_or_file, 'read') else json_or_file  # type: ignore
    if isinstance(text, bytes):
        text = text.decode('utf-8')
    manifest = mod_json.loads(cast(str, text))
    if manifest.get('gpxpy_manifest_version') != MANIFEST_VERSION:
        raise mod_gpx.GPXException(
            f"Unsupported manifest version: {manifest.get('gpxpy_manifest_version')}")
    return manifest


def downconvert_to_1_0(gpx: mod_gpx.GPX) -> Tuple[str, Dict[str, Any]]:
    """
    Emit the read-only GPX 1.0 payload and its companion manifest.

    The GPX object is not modified (``to_xml`` would otherwise rewrite its
    version/namespace state); link types and vendor extensions are captured
    in the manifest rather than silently dropped.
    """
    manifest = build_manifest(gpx)
    xml_1_0 = gpx.clone().to_xml(version=TARGET_VERSION)
    return xml_1_0, manifest


# ----------------------------------------------------------------------------------------------------
# Point matching under deletions, reordering and duplicate coordinates
# ----------------------------------------------------------------------------------------------------

def _coordinate_index(items: List[Tuple[str, Any]]
                      ) -> Tuple[Dict[Tuple[float, float], int], Set[Tuple[float, float]]]:
    """
    Map unique coordinates to their position inside one container.

    A coordinate occurring more than once inside the same stream is
    ambiguous and excluded, so collocated/same-named points can never be
    confused with each other.
    """
    positions: Dict[Tuple[float, float], int] = {}
    duplicates: Set[Tuple[float, float]] = set()
    for index, (_, point) in enumerate(items):
        coordinate = (float(point.latitude), float(point.longitude))
        if coordinate in positions:
            duplicates.add(coordinate)
        else:
            positions[coordinate] = index
    for coordinate in duplicates:
        positions.pop(coordinate, None)
    return positions, duplicates


def _match_partitions(original_items: List[Tuple[str, Any]],
                      echo_items: List[Tuple[str, Any]]
                      ) -> Tuple[Dict[str, str], Set[str], Set[str]]:
    """
    Match original point keys to echo point keys within each container.

    Returns the matches plus the original/echo point keys whose coordinate
    collides inside their container and that therefore cannot be matched
    without guessing.
    """
    original_by_partition: Dict[Tuple[str, ...], List[Tuple[str, Any]]] = {}
    echo_by_partition: Dict[Tuple[str, ...], List[Tuple[str, Any]]] = {}

    for key, point in original_items:
        original_by_partition.setdefault(_partition_of(key), []).append((key, point))
    for key, point in echo_items:
        echo_by_partition.setdefault(_partition_of(key), []).append((key, point))

    matches: Dict[str, str] = {}
    ambiguous_original_keys: Set[str] = set()
    ambiguous_echo_keys: Set[str] = set()

    for partition, original_partition_items in original_by_partition.items():
        echo_partition_items = echo_by_partition.get(partition, [])
        original_index, original_ambiguous = _coordinate_index(original_partition_items)
        echo_index, echo_ambiguous = _coordinate_index(echo_partition_items)
        ambiguous = original_ambiguous | echo_ambiguous

        for key, point in original_partition_items:
            if (float(point.latitude), float(point.longitude)) in ambiguous:
                ambiguous_original_keys.add(key)
        for key, point in echo_partition_items:
            if (float(point.latitude), float(point.longitude)) in ambiguous:
                ambiguous_echo_keys.add(key)

        for coordinate, original_position in original_index.items():
            if coordinate in ambiguous:
                continue
            echo_position = echo_index.get(coordinate)
            if echo_position is None:
                continue
            original_key = original_partition_items[original_position][0]
            echo_key = echo_partition_items[echo_position][0]
            matches[original_key] = echo_key

        # Fallback for moved/edited points: when coordinate matching leaves
        # exactly one unpaired, non-ambiguous point on each side of the same
        # container, the pairing is still unique and safe. With several
        # candidates on either side, or an unresolved collision in the
        # container, identity cannot be established, so nothing is guessed.
        if not ambiguous:
            unmatched_original = [key for key, _ in original_partition_items
                                  if key not in matches]
            matched_echo = set(matches.values())
            unmatched_echo = [key for key, _ in echo_partition_items
                              if key not in matched_echo]
            if len(unmatched_original) == 1 and len(unmatched_echo) == 1:
                matches[unmatched_original[0]] = unmatched_echo[0]

    return matches, ambiguous_original_keys, ambiguous_echo_keys


def _values_equal(left: Any, right: Any) -> bool:
    if left is None and right is None:
        return True
    if left is None or right is None:
        return False
    return bool(left == right)


# ----------------------------------------------------------------------------------------------------
# Reconciliation result types
# ----------------------------------------------------------------------------------------------------

class PointReconciliation:
    __slots__ = ('point_key', 'echo_key', 'status', 'field_statuses',
                 'restored_fields', 'modified_fields')

    def __init__(self, point_key: str, echo_key: Optional[str], status: str,
                 field_statuses: Optional[Dict[str, str]] = None,
                 restored_fields: Optional[List[str]] = None,
                 modified_fields: Optional[List[str]] = None) -> None:
        self.point_key = point_key
        self.echo_key = echo_key
        self.status = status
        self.field_statuses: Dict[str, str] = field_statuses or {}
        self.restored_fields: List[str] = restored_fields or []
        self.modified_fields: List[str] = modified_fields or []

    def __repr__(self) -> str:
        return (f'PointReconciliation({self.point_key!r}, echo={self.echo_key!r}, '
                f'status={self.status!r}, modified={self.modified_fields!r}, '
                f'restored={self.restored_fields!r})')


class SuspendedExtension:
    """An extension that could not be returned to its original owner."""

    __slots__ = ('owner_key', 'element', 'reason')

    def __init__(self, owner_key: str, element: Any, reason: str) -> None:
        self.owner_key = owner_key
        self.element = element
        self.reason = reason

    def __repr__(self) -> str:
        return f'SuspendedExtension(owner={self.owner_key!r}, tag={self.element.tag!r}, reason={self.reason!r})'


class ReconciliationResult:
    __slots__ = ('point_reconciliations', 'suspended_extensions',
                 'suspended_container_owners', 'original_gpx', 'echo_gpx',
                 'manifest')

    def __init__(self,
                 point_reconciliations: List[PointReconciliation],
                 suspended_extensions: List[SuspendedExtension],
                 suspended_container_owners: List[str],
                 original_gpx: mod_gpx.GPX,
                 echo_gpx: mod_gpx.GPX,
                 manifest: Dict[str, Any]) -> None:
        self.point_reconciliations = point_reconciliations
        self.suspended_extensions = suspended_extensions
        self.suspended_container_owners = suspended_container_owners
        self.original_gpx = original_gpx
        self.echo_gpx = echo_gpx
        self.manifest = manifest

    def by_status(self, status: str) -> List[PointReconciliation]:
        return [item for item in self.point_reconciliations if item.status == status]

    def field_status(self, point_key: str, field_name: str) -> Optional[str]:
        for item in self.point_reconciliations:
            if item.point_key == point_key:
                return item.field_statuses.get(field_name)
        return None

    def summary(self) -> Dict[str, int]:
        result: Dict[str, int] = {
            POINT_UNCHANGED: 0, POINT_RESTORED: 0, POINT_MODIFIED: 0,
            POINT_LOST: 0, POINT_NEW: 0, POINT_AMBIGUOUS: 0,
            FIELD_RECOVERABLE: 0, FIELD_MAPPED: 0, FIELD_MODIFIED: 0,
            'suspended_extensions': len(self.suspended_extensions),
        }
        for item in self.point_reconciliations:
            result[item.status] += 1
            for status in item.field_statuses.values():
                result[status] += 1
        return result

    def apply(self, restore_lost_points: bool = False) -> mod_gpx.GPX:
        """
        Build the updated GPX 1.1 ledger from the terminal reply.

        Terminal edits (``modified`` fields), deletions and reorderings
        come from the 1.0 reply and win. Manifest-only values (link types
        and vendor extensions) are restored onto uniquely recognised
        points only. Suspended extensions are never attached
        automatically.

        When ``restore_lost_points`` is true, points the terminal deleted
        are appended back to their original container; their extensions
        remain suspended. By default deletions are honoured.
        """
        merged = self.echo_gpx.clone()
        merged.version = SOURCE_VERSION
        merged.nsmap = dict(self.manifest.get('nsmap') or {})

        echo_match: Dict[str, PointReconciliation] = {}
        for item in self.point_reconciliations:
            if item.echo_key is not None and item.status != POINT_AMBIGUOUS:
                echo_match[item.echo_key] = item

        suspended_owners = {entry.owner_key for entry in self.suspended_extensions}

        def restore_point(echo_key: str, point: Any) -> None:
            item = echo_match.get(echo_key)
            if item is None:
                return
            manifest_entry = self.manifest.get('points', {}).get(item.point_key)
            if not manifest_entry:
                return
            for field_name in MANIFEST_ONLY_POINT_FIELDS:
                if item.field_statuses.get(field_name) == FIELD_MAPPED:
                    setattr(point, field_name, manifest_entry[field_name])
            if item.field_statuses.get('extensions') == FIELD_MAPPED and \
                    item.point_key not in suspended_owners:
                point.extensions = _elements_from_json(manifest_entry.get('extensions'))

        for point_no, waypoint in enumerate(merged.waypoints):
            restore_point(waypoint_key(point_no), waypoint)
        for route_no, route in enumerate(merged.routes):
            self._restore_container_extensions(
                route, f'rte:{route_no}',
                self.manifest.get('route_extensions', {}).get(str(route_no)))
            for point_no, point in enumerate(route.points):
                restore_point(route_point_key(route_no, point_no), point)
        for track_no, track in enumerate(merged.tracks):
            self._restore_container_extensions(
                track, f'trk:{track_no}',
                self.manifest.get('track_extensions', {}).get(str(track_no)))
            for segment_no, segment in enumerate(track.segments):
                self._restore_container_extensions(
                    segment, f'trk:{track_no}:{segment_no}',
                    self.manifest.get('segment_extensions', {}).get(f'{track_no}:{segment_no}'))
                for point_no, point in enumerate(segment.points):
                    restore_point(track_point_key(track_no, segment_no, point_no), point)

        merged.metadata_extensions = _elements_from_json(
            self.manifest.get('metadata_extensions'))
        merged.extensions = _elements_from_json(self.manifest.get('root_extensions'))

        if restore_lost_points:
            for item in self.by_status(POINT_LOST):
                original_point = _find_point(self.original_gpx, item.point_key)
                if original_point is None:
                    continue
                kept_point = mod_copy.deepcopy(original_point)
                if item.point_key in suspended_owners:
                    kept_point.extensions = []
                self._append_lost_point(merged, item.point_key, kept_point)

        return merged

    def _restore_container_extensions(self, container: Any, owner_key: str,
                                      raw_extensions: Optional[List[Dict[str, Any]]]) -> None:
        if owner_key in self.suspended_container_owners:
            return
        container.extensions = _elements_from_json(raw_extensions)

    def _append_lost_point(self, merged: mod_gpx.GPX, key: str, point: Any) -> None:
        parts = key.split(':')
        kind = parts[0]
        indices = [int(part) for part in parts[1:]]
        try:
            if kind == 'wpt':
                merged.waypoints.append(point)
            elif kind == 'rte':
                merged.routes[indices[0]].points.append(point)
            elif kind == 'trk':
                merged.tracks[indices[0]].segments[indices[1]].points.append(point)
        except IndexError:
            # Container itself disappeared; the point stays reported as lost.
            pass


# ----------------------------------------------------------------------------------------------------
# Reconciliation
# ----------------------------------------------------------------------------------------------------

def _native_10_fields(point: Any) -> List[str]:
    fields = list(COMMON_POINT_FIELDS)
    if isinstance(point, mod_gpx.GPXTrackPoint):
        fields += NATIVE_10_TRACK_FIELDS
    return fields


def _reconcile_matched_point(original_key: str, echo_key: str,
                             original_point: Any, echo_point: Any,
                             manifest_entry: Optional[Dict[str, Any]]) -> PointReconciliation:
    field_statuses: Dict[str, str] = {}
    modified_fields: List[str] = []

    # Values the 1.0 reply itself can carry: the reply is authoritative.
    for field_name in _native_10_fields(original_point):
        original_value = getattr(original_point, field_name, None)
        echo_value = getattr(echo_point, field_name, None)
        if _values_equal(original_value, echo_value):
            field_statuses[field_name] = FIELD_RECOVERABLE
        else:
            field_statuses[field_name] = FIELD_MODIFIED
            modified_fields.append(field_name)

    # Values no GPX 1.0 element can represent: only the manifest knows them.
    restored_fields: List[str] = []
    if manifest_entry:
        for field_name in MANIFEST_ONLY_POINT_FIELDS:
            if field_name in manifest_entry:
                field_statuses[field_name] = FIELD_MAPPED
                restored_fields.append(field_name)
        if manifest_entry.get('extensions'):
            field_statuses['extensions'] = FIELD_MAPPED
            restored_fields.append('extensions')

    if modified_fields:
        status = POINT_MODIFIED
    elif restored_fields:
        status = POINT_RESTORED
    else:
        status = POINT_UNCHANGED
    return PointReconciliation(original_key, echo_key, status,
                               field_statuses, restored_fields, modified_fields)


def reconcile_round_trip(original_gpx: mod_gpx.GPX,
                         echo_gpx: mod_gpx.GPX,
                         manifest: Dict[str, Any]) -> ReconciliationResult:
    """
    Compare the original GPX 1.1 batch with the GPX 1.0 reply.

    Classifies every observation as unchanged/restored/modified/lost/new,
    attributes every field to its provenance class, and suspends
    extensions whose owner point (or container) cannot be uniquely
    recognised.
    """
    original_items = _iter_point_keys(original_gpx)
    echo_items = _iter_point_keys(echo_gpx)
    matches, ambiguous_original_keys, ambiguous_echo_keys = _match_partitions(
        original_items, echo_items)

    point_manifest = manifest.get('points', {})
    suspended: List[SuspendedExtension] = []
    reconciliations: List[PointReconciliation] = []

    # Original points: uniquely matched, ambiguous, or genuinely lost. Only
    # the first two cases have a recognisable owner in the reply.
    for original_key, original_point in original_items:
        if original_key in ambiguous_original_keys:
            entry = point_manifest.get(original_key)
            if entry and entry.get('extensions'):
                for element_data in entry['extensions']:
                    suspended.append(SuspendedExtension(
                        original_key, _element_from_json(element_data),
                        'coordinate collides with another point in the same container; '
                        'owner cannot be uniquely identified'))
            reconciliations.append(PointReconciliation(
                original_key, None, POINT_AMBIGUOUS))
            continue

        echo_key = matches.get(original_key)
        if echo_key is None:
            entry = point_manifest.get(original_key)
            if entry and entry.get('extensions'):
                for element_data in entry['extensions']:
                    suspended.append(SuspendedExtension(
                        original_key, _element_from_json(element_data),
                        'owner point cannot be uniquely identified in the 1.0 reply'))
            reconciliations.append(PointReconciliation(
                original_key, None, POINT_LOST))
            continue

        echo_point = _find_point(echo_gpx, echo_key)
        reconciliations.append(_reconcile_matched_point(
            original_key, echo_key, original_point, echo_point,
            point_manifest.get(original_key)))

    # Reply points without an original counterpart: terminal additions, apart
    # from ambiguous points which belong to an unresolved collision group.
    matched_echo_keys = set(matches.values())
    for echo_key, echo_point in echo_items:
        if echo_key in ambiguous_echo_keys:
            reconciliations.append(PointReconciliation(
                echo_key, echo_key, POINT_AMBIGUOUS))
        elif echo_key not in matched_echo_keys:
            reconciliations.append(PointReconciliation(
                echo_key, echo_key, POINT_NEW))

    suspended_container_owners = _suspend_container_extensions(
        echo_gpx, manifest, suspended)

    return ReconciliationResult(
        reconciliations, suspended, suspended_container_owners,
        original_gpx, echo_gpx, manifest)


def _suspend_container_extensions(echo_gpx: mod_gpx.GPX,
                                  manifest: Dict[str, Any],
                                  suspended: List[SuspendedExtension]) -> List[str]:
    """Container-level extensions are only safe when the container survives."""
    suspended_owners: List[str] = []

    for route_no_raw, raw_elements in manifest.get('route_extensions', {}).items():
        owner_key = f'rte:{route_no_raw}'
        if int(route_no_raw) >= len(echo_gpx.routes):
            suspended_owners.append(owner_key)
            for element_data in raw_elements:
                suspended.append(SuspendedExtension(
                    owner_key, _element_from_json(element_data),
                    'route missing from the 1.0 reply'))

    for track_no_raw, raw_elements in manifest.get('track_extensions', {}).items():
        owner_key = f'trk:{track_no_raw}'
        if int(track_no_raw) >= len(echo_gpx.tracks):
            suspended_owners.append(owner_key)
            for element_data in raw_elements:
                suspended.append(SuspendedExtension(
                    owner_key, _element_from_json(element_data),
                    'track missing from the 1.0 reply'))

    for segment_key, raw_elements in manifest.get('segment_extensions', {}).items():
        track_no_raw, segment_no_raw = segment_key.split(':')
        owner_key = f'trk:{segment_key}'
        track_no, segment_no = int(track_no_raw), int(segment_no_raw)
        if track_no >= len(echo_gpx.tracks) or \
                segment_no >= len(echo_gpx.tracks[track_no].segments):
            suspended_owners.append(owner_key)
            for element_data in raw_elements:
                suspended.append(SuspendedExtension(
                    owner_key, _element_from_json(element_data),
                    'track segment missing from the 1.0 reply'))

    return suspended_owners
