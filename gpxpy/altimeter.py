"""
Reconciliation of multiple altimeter track segments recorded with
independent vertical datums.

Each segment is assumed to carry a constant vertical offset on top of the
true elevation profile::

    true_elevation = recorded_elevation + offset(segment)

The offsets are constrained by two kinds of field observations:

* ``Benchmark`` -- a point with a known elevation that one segment passes.
* ``OverlapObservation`` -- the same ground position walked by two segments,
  so their corrected elevations must agree there.

Observations are combined in a weighted least-squares adjustment (weights
inverse squared tolerance).  Segments linked through overlaps form
components.  A component can only get absolute offsets when it contains at
least one benchmark; components without any vertical datum stay floating:
their relative geometry is known but their absolute elevation is never
guessed.  For those only the in-segment climb (which a constant offset does
not change) is reported.
"""

import copy as mod_copy
from dataclasses import dataclass, field
from typing import Dict, List, NamedTuple, Optional, Sequence, Set, Tuple

from . import geo as mod_geo
from . import gpx as mod_gpx

DEFAULT_MATCH_DISTANCE: float = 25.0
DEFAULT_BENCHMARK_TOLERANCE: float = 5.0
DEFAULT_OVERLAP_TOLERANCE: float = 5.0
DEFAULT_MIN_OVERLAP_PAIRS: int = 3


class Climb(NamedTuple):
    """Total uphill/downhill of a segment, in meters (smoothed, as in gpxpy)."""

    uphill: float
    downhill: float


@dataclass
class Benchmark:
    """A ground point with a known elevation."""

    latitude: float
    longitude: float
    elevation: float
    name: Optional[str] = None
    tolerance: float = DEFAULT_BENCHMARK_TOLERANCE


@dataclass
class OverlapObservation:
    """The same position walked by two segments, where elevations must agree."""

    segment_a: int
    segment_b: int
    latitude: float
    longitude: float
    elevation_a: float
    elevation_b: float
    horizontal_distance: float
    tolerance: float = DEFAULT_OVERLAP_TOLERANCE
    name: Optional[str] = None


@dataclass
class Residual:
    """
    Observed-vs-predicted mismatch for one field observation.

    For benchmarks: ``observed_elevation`` is the raw recorded reading,
    ``reference_elevation`` the known elevation and ``predicted_elevation``
    the offset-corrected reading; ``residual`` is corrected minus known.

    For overlaps: ``observed_elevation`` is the corrected reading of the
    first segment, ``reference_elevation`` the corrected reading of the
    second and ``predicted_elevation`` equals the first; ``residual`` is
    their difference, which must be within ``tolerance``.
    """

    kind: str
    name: str
    segment_index: int
    other_segment_index: Optional[int]
    latitude: float
    longitude: float
    observed_elevation: float
    reference_elevation: float
    predicted_elevation: float
    residual: float
    tolerance: float
    is_conflict: bool


@dataclass
class SegmentAdjustment:
    """Result for one input segment."""

    index: int
    name: str
    component: int
    point_count: int
    uphill: float
    downhill: float
    offset: Optional[float] = None
    floating: bool = True


@dataclass
class AdjustmentReport:
    """Full outcome of the offset reconciliation."""

    adjustments: List[SegmentAdjustment]
    residuals: List[Residual]
    conflicts: List[Residual]
    floating_segments: List[int]
    compatible: bool

    def offset_for(self, segment_index: int) -> Optional[float]:
        """Offset in meters for the segment, or ``None`` when undetermined."""
        return self.adjustments[segment_index].offset

    def adjusted_segments(self) -> List[int]:
        """Indices of segments whose absolute offset was determined."""
        return [item.index for item in self.adjustments if not item.floating]


@dataclass
class _MatchedBenchmark:
    benchmark: Benchmark
    segment_index: int
    recorded_elevation: float
    horizontal_distance: float


@dataclass
class _UnionFind:
    parent: List[int] = field(default_factory=list)

    def add(self) -> int:
        self.parent.append(len(self.parent))
        return len(self.parent) - 1

    def find(self, item: int) -> int:
        root = item
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[item] != item:
            parent = self.parent[item]
            self.parent[item] = root
            item = parent
        return root

    def union(self, left: int, right: int) -> None:
        self.parent[self.find(left)] = self.find(right)


def segment_climb(segment: mod_gpx.GPXTrackSegment) -> Climb:
    """
    In-segment uphill/downhill.  A constant offset cancels out of the
    differences, so the climb is known even for floating segments.
    """
    uphill, downhill = mod_geo.calculate_uphill_downhill(
        [point.elevation for point in segment.points])
    return Climb(uphill, downhill)


def _nearest_point(
    segment: mod_gpx.GPXTrackSegment, latitude: float, longitude: float
) -> Tuple[Optional[mod_gpx.GPXTrackPoint], float]:
    nearest: Optional[mod_gpx.GPXTrackPoint] = None
    nearest_distance = float('inf')
    for point in segment.points:
        distance = mod_geo.haversine_distance(
            latitude, longitude, point.latitude, point.longitude)
        if distance < nearest_distance:
            nearest_distance = distance
            nearest = point
    return nearest, nearest_distance


def _solve_linear_system(
    matrix: List[List[float]], vector: List[float]
) -> List[float]:
    """Gauss-Jordan elimination with partial pivoting."""
    size = len(vector)
    augmented = [matrix[row][:] + [vector[row]] for row in range(size)]
    for column in range(size):
        pivot = max(
            range(column, size),
            key=lambda row: abs(augmented[row][column]))
        if abs(augmented[pivot][column]) < 1e-12:
            raise ValueError('Singular normal-equation matrix')
        augmented[column], augmented[pivot] = augmented[pivot], augmented[column]
        pivot_value = augmented[column][column]
        for row in range(size):
            if row == column:
                continue
            factor = augmented[row][column] / pivot_value
            for entry in range(column, size + 1):
                augmented[row][entry] -= factor * augmented[column][entry]
    return [augmented[row][size] / augmented[row][row] for row in range(size)]


class AltimeterAdjuster:
    """
    Builds and solves the constant-offset adjustment for a collection of
    altimeter track segments.

    Parameters
    ----------
    segments : sequence of GPXTrackSegment
        One element per independently zeroed altimeter recording.
    names : sequence of str, optional
        Human readable names, defaults to ``segment 0``, ``segment 1`` ...
    """

    def __init__(
        self,
        segments: Sequence[mod_gpx.GPXTrackSegment],
        names: Optional[Sequence[str]] = None,
    ) -> None:
        if not segments:
            raise ValueError('At least one track segment is required')
        if names is not None and len(names) != len(segments):
            raise ValueError('One name per segment is required')
        self._segments: List[mod_gpx.GPXTrackSegment] = list(segments)
        self._names: List[str] = (
            list(names) if names is not None
            else [f'segment {index}' for index in range(len(segments))])
        self._benchmarks: List[_MatchedBenchmark] = []
        self._overlaps: List[OverlapObservation] = []

    @property
    def segments(self) -> List[mod_gpx.GPXTrackSegment]:
        return self._segments

    @property
    def names(self) -> List[str]:
        return self._names

    def add_benchmark(
        self,
        benchmark: Benchmark,
        max_distance: float = DEFAULT_MATCH_DISTANCE,
    ) -> List[_MatchedBenchmark]:
        """
        Match the benchmark to the nearest point of every segment passing
        within ``max_distance``.  One observation is created per matching
        segment, since a known point can constrain several recordings.
        Raises ``ValueError`` when no segment passes it or every match lacks
        an elevation.
        """
        if benchmark.tolerance <= 0:
            raise ValueError('Benchmark tolerance must be positive')
        matched: List[_MatchedBenchmark] = []
        for index, segment in enumerate(self._segments):
            point, distance = _nearest_point(
                segment, benchmark.latitude, benchmark.longitude)
            if point is None or distance > max_distance:
                continue
            if point.elevation is None:
                continue
            observation = _MatchedBenchmark(
                benchmark=benchmark,
                segment_index=index,
                recorded_elevation=float(point.elevation),
                horizontal_distance=distance)
            self._benchmarks.append(observation)
            matched.append(observation)
        if not matched:
            raise ValueError(
                f'Benchmark {benchmark.name or "(unnamed)"} matches no '
                f'segment with elevation within {max_distance} m')
        return matched

    def add_waypoint_benchmarks(
        self,
        gpx: mod_gpx.GPX,
        max_distance: float = DEFAULT_MATCH_DISTANCE,
        tolerance: float = DEFAULT_BENCHMARK_TOLERANCE,
    ) -> List[_MatchedBenchmark]:
        """Use GPX waypoints that carry an elevation as benchmarks."""
        matched: List[_MatchedBenchmark] = []
        for waypoint in gpx.waypoints:
            if waypoint.elevation is None:
                continue
            benchmark = Benchmark(
                latitude=waypoint.latitude,
                longitude=waypoint.longitude,
                elevation=float(waypoint.elevation),
                name=waypoint.name,
                tolerance=tolerance)
            try:
                matched.extend(self.add_benchmark(benchmark, max_distance))
            except ValueError:
                continue
        return matched

    def add_overlap(self, observation: OverlapObservation) -> None:
        """Register one co-located pair of readings from two segments."""
        count = len(self._segments)
        if not (0 <= observation.segment_a < count
                and 0 <= observation.segment_b < count):
            raise ValueError('Overlap references an unknown segment')
        if observation.segment_a == observation.segment_b:
            raise ValueError('An overlap must connect two different segments')
        if observation.tolerance <= 0:
            raise ValueError('Overlap tolerance must be positive')
        self._overlaps.append(observation)

    def find_overlaps(
        self,
        max_distance: float = DEFAULT_MATCH_DISTANCE,
        tolerance: float = DEFAULT_OVERLAP_TOLERANCE,
        min_pairs: int = DEFAULT_MIN_OVERLAP_PAIRS,
    ) -> List[OverlapObservation]:
        """
        Detect ground positions walked by two segments.

        For every pair of segments each point of the first is paired with
        the nearest point of the second when their horizontal separation is
        at most ``max_distance``.  A segment pair is accepted only when at
        least ``min_pairs`` such pairs exist, and each accepted pair becomes
        an overlap observation with the given ``tolerance``.
        """
        found: List[OverlapObservation] = []
        count = len(self._segments)
        for index_a in range(count):
            for index_b in range(index_a + 1, count):
                candidates: List[OverlapObservation] = []
                for point_a in self._segments[index_a].points:
                    if point_a.elevation is None:
                        continue
                    point_b, distance = _nearest_point(
                        self._segments[index_b],
                        point_a.latitude,
                        point_a.longitude)
                    if (point_b is None
                            or distance > max_distance
                            or point_b.elevation is None):
                        continue
                    candidates.append(OverlapObservation(
                        segment_a=index_a,
                        segment_b=index_b,
                        latitude=point_a.latitude,
                        longitude=point_a.longitude,
                        elevation_a=float(point_a.elevation),
                        elevation_b=float(point_b.elevation),
                        horizontal_distance=distance,
                        tolerance=tolerance,
                        name=(f'{self._names[index_a]} / '
                              f'{self._names[index_b]} overlap')))
                if len(candidates) >= min_pairs:
                    for observation in candidates:
                        self.add_overlap(observation)
                        found.append(observation)
        return found

    def _build_components(self) -> Tuple[_UnionFind, List[bool]]:
        union_find = _UnionFind()
        for _ in self._segments:
            union_find.add()
        has_datum = [False] * len(self._segments)
        for matched in self._benchmarks:
            has_datum[matched.segment_index] = True
        for overlap in self._overlaps:
            union_find.union(overlap.segment_a, overlap.segment_b)
        return union_find, has_datum

    def solve(self) -> AdjustmentReport:
        """
        Solve the offset adjustment and validate every observation.

        Components containing a benchmark get absolute offsets via weighted
        least squares.  Benchmark- or overlap residuals beyond the declared
        tolerances are reported as conflicts.  Components without a benchmark
        stay floating: a provisional gauge (first segment pinned to zero
        offset) is used only to check overlap consistency, but no absolute
        offset is delivered -- only in-segment climbs, which a constant
        offset cannot change.
        """
        union_find, has_datum = self._build_components()
        roots = {union_find.find(index) for index in range(len(self._segments))}
        component_ids = {
            root: component_id
            for component_id, root in enumerate(sorted(roots))}
        component_of = [
            component_ids[union_find.find(index)]
            for index in range(len(self._segments))]
        segment_count = len(self._segments)

        component_members: List[List[int]] = [
            [] for _ in range(len(component_ids))]
        for index in range(segment_count):
            component_members[component_of[index]].append(index)
        component_floating = [
            not any(has_datum[index] for index in members)
            for members in component_members]

        # Provisional offsets used only for residual checking.  Floating
        # components are solved against an arbitrary gauge and never exposed.
        provisional_offsets: List[Optional[float]] = [None] * segment_count
        delivered_offsets: List[Optional[float]] = [None] * segment_count

        for component, members in enumerate(component_members):
            floating = component_floating[component]
            local_column = {
                segment_index: column
                for column, segment_index in enumerate(members)}
            size = len(members)
            matrix = [[0.0] * size for _ in range(size)]
            vector = [0.0] * size

            def accumulate(
                column_a: int,
                column_b: Optional[int],
                right_hand: float,
                tolerance: float,
            ) -> None:
                weight = 1.0 / (tolerance * tolerance)
                if column_b is None:
                    # weight * (offset_a - right_hand)^2
                    matrix[column_a][column_a] += weight
                    vector[column_a] += weight * right_hand
                else:
                    # weight * (offset_a - offset_b - right_hand)^2
                    matrix[column_a][column_a] += weight
                    matrix[column_b][column_b] += weight
                    matrix[column_a][column_b] -= weight
                    matrix[column_b][column_a] -= weight
                    vector[column_a] += weight * right_hand
                    vector[column_b] -= weight * right_hand

            for matched in self._benchmarks:
                if component_of[matched.segment_index] != component:
                    continue
                # offset = known_elevation - recorded_elevation
                accumulate(
                    local_column[matched.segment_index],
                    None,
                    matched.benchmark.elevation - matched.recorded_elevation,
                    matched.benchmark.tolerance)
            for overlap in self._overlaps:
                if component_of[overlap.segment_a] != component:
                    continue
                # offset_a - offset_b = elevation_b - elevation_a
                accumulate(
                    local_column[overlap.segment_a],
                    local_column[overlap.segment_b],
                    overlap.elevation_b - overlap.elevation_a,
                    overlap.tolerance)

            if floating:
                # Arbitrary gauge: pin the first member's offset to zero.
                # Tight enough to dominate, soft enough to stay invertible.
                gauge = members[0]
                tolerance = 1e-6
                weight = 1.0 / (tolerance * tolerance)
                matrix[local_column[gauge]][local_column[gauge]] += weight

            # Benchmarks (datum components) or the gauge (floating ones)
            # together with the connected overlap graph guarantee full rank.
            solved = _solve_linear_system(matrix, vector)
            for segment_index, column in local_column.items():
                provisional_offsets[segment_index] = solved[column]
                if not floating:
                    delivered_offsets[segment_index] = solved[column]

        residuals: List[Residual] = []
        conflicts: List[Residual] = []

        for matched in self._benchmarks:
            index = matched.segment_index
            offset = provisional_offsets[index]
            predicted = (matched.recorded_elevation + offset
                         if offset is not None else float('nan'))
            residual_value = (
                predicted - matched.benchmark.elevation
                if offset is not None else float('nan'))
            is_conflict = (
                offset is not None
                and abs(residual_value) > matched.benchmark.tolerance)
            residual = Residual(
                kind='benchmark',
                name=matched.benchmark.name or f'benchmark@{index}',
                segment_index=index,
                other_segment_index=None,
                latitude=matched.benchmark.latitude,
                longitude=matched.benchmark.longitude,
                observed_elevation=matched.recorded_elevation,
                reference_elevation=matched.benchmark.elevation,
                predicted_elevation=predicted,
                residual=residual_value,
                tolerance=matched.benchmark.tolerance,
                is_conflict=is_conflict)
            residuals.append(residual)
            if is_conflict:
                conflicts.append(residual)

        for overlap in self._overlaps:
            offset_a = provisional_offsets[overlap.segment_a]
            offset_b = provisional_offsets[overlap.segment_b]
            if offset_a is None or offset_b is None:
                continue
            predicted_a = overlap.elevation_a + offset_a
            predicted_b = overlap.elevation_b + offset_b
            residual_value = predicted_a - predicted_b
            is_conflict = abs(residual_value) > overlap.tolerance
            residual = Residual(
                kind='overlap',
                name=overlap.name or (
                    f'overlap {overlap.segment_a}/{overlap.segment_b}'),
                segment_index=overlap.segment_a,
                other_segment_index=overlap.segment_b,
                latitude=overlap.latitude,
                longitude=overlap.longitude,
                observed_elevation=predicted_a,
                reference_elevation=predicted_b,
                predicted_elevation=predicted_a,
                residual=residual_value,
                tolerance=overlap.tolerance,
                is_conflict=is_conflict)
            residuals.append(residual)
            if is_conflict:
                conflicts.append(residual)

        adjustments: List[SegmentAdjustment] = []
        floating_segments: List[int] = []
        for index, segment in enumerate(self._segments):
            component = component_of[index]
            climb = segment_climb(segment)
            floating = component_floating[component]
            if floating:
                floating_segments.append(index)
            adjustments.append(SegmentAdjustment(
                index=index,
                name=self._names[index],
                component=component,
                point_count=len(segment.points),
                uphill=climb.uphill,
                downhill=climb.downhill,
                offset=delivered_offsets[index],
                floating=floating))

        return AdjustmentReport(
            adjustments=adjustments,
            residuals=residuals,
            conflicts=conflicts,
            floating_segments=floating_segments,
            compatible=not conflicts)

    def apply_offsets(self, report: AdjustmentReport) -> None:
        """
        Add the solved offsets to segment elevations in place.  Floating
        segments (no vertical datum) are left untouched.
        """
        for adjustment in report.adjustments:
            if adjustment.floating or adjustment.offset is None:
                continue
            for point in self._segments[adjustment.index].points:
                if point.elevation is not None:
                    point.elevation = point.elevation + adjustment.offset

    def corrected_gpx(self, report: AdjustmentReport) -> mod_gpx.GPX:
        """
        Build a new GPX object with corrected elevations.

        Each connected component becomes one track holding its segments in
        input order.  Floating components keep their recorded (relative)
        elevations and are labelled as such instead of receiving a guessed
        absolute height.
        """
        gpx = mod_gpx.GPX()
        component_count = 1 + max(
            (item.component for item in report.adjustments), default=-1)
        for component in range(component_count):
            members = [
                item for item in report.adjustments
                if item.component == component]
            floating = members[0].floating
            track = mod_gpx.GPXTrack(
                name=(f'component {component} '
                      f'("floating: relative elevations only")'
                      if floating
                      else f'component {component} (offset-adjusted)'))
            for item in members:
                source = self._segments[item.index]
                points = []
                for point in source.points:
                    corrected = mod_gpx.GPXTrackPoint(
                        latitude=point.latitude,
                        longitude=point.longitude,
                        elevation=(
                            point.elevation
                            if (floating
                                or item.offset is None
                                or point.elevation is None)
                            else point.elevation + item.offset),
                        time=mod_copy.copy(point.time))
                    points.append(corrected)
                track.segments.append(mod_gpx.GPXTrackSegment(points))
            gpx.tracks.append(track)
        emitted_benchmarks: Set[int] = set()
        for matched in self._benchmarks:
            benchmark_id = id(matched.benchmark)
            if benchmark_id in emitted_benchmarks:
                continue
            emitted_benchmarks.add(benchmark_id)
            benchmark = matched.benchmark
            gpx.waypoints.append(mod_gpx.GPXWaypoint(
                latitude=benchmark.latitude,
                longitude=benchmark.longitude,
                elevation=benchmark.elevation,
                name=benchmark.name))
        return gpx


def reconcile(
    segments: Sequence[mod_gpx.GPXTrackSegment],
    benchmarks: Sequence[Benchmark] = (),
    overlaps: Sequence[OverlapObservation] = (),
    names: Optional[Sequence[str]] = None,
    max_match_distance: float = DEFAULT_MATCH_DISTANCE,
) -> AdjustmentReport:
    """Convenience wrapper: register observations and solve in one call."""
    adjuster = AltimeterAdjuster(segments, names=names)
    for benchmark in benchmarks:
        adjuster.add_benchmark(benchmark, max_match_distance)
    for overlap in overlaps:
        adjuster.add_overlap(overlap)
    return adjuster.solve()
