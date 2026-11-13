"""
Incremental hand-off between two consecutive partial GPX exports.

A recording terminal typically exports only the most recent slice of a
track, and the tail of the previous export may still be corrected by the
next one.  This module reconciles two such consecutive exports into a
single ordered point sequence.

The caller marks, for each export, the time window that export
*guarantees* to be complete.  The intersection of both windows is the
*overlap window*: the only region where both exports can be held against
each other.

Semantics:

* Points are matched by value (time, latitude, longitude, elevation) and
  by *order*.  Several points may share the same second, and a point
  value is not a unique id, so alignment is sequence-based, never a
  value lookup.
* Inside the overlap window, and only with sufficient evidence (an
  unambiguous alignment), observations are merged, corrected or
  retracted.  The later export is authoritative there.
* Previous-export points outside the overlap window are always kept,
  even when they vanish from the later export.
* When a repeated traversal of the same segment (or duplicated
  same-second points) allows several equally good alignments, no merge
  or retraction is performed in that range; the range is reported in
  ``Reconciliation.undecided`` for the caller to resolve.
* Re-importing the same later export is idempotent: rows already
  reconciled do not change and the row count stays the same.
"""

import copy as mod_copy
import datetime as mod_datetime

from collections import Counter as mod_Counter
from dataclasses import dataclass, field
from functools import lru_cache
from typing import List, Optional, Sequence, Tuple

from . import gpx as mod_gpx

__all__ = ['reconcile', 'reconcile_gpx', 'Reconciliation', 'Correction',
           'UndecidedRange']

# A guaranteed-complete window: (start, end); either bound may be None
# for "unbounded".
Window = Tuple[Optional[mod_datetime.datetime], Optional[mod_datetime.datetime]]

_HEAD = 'head'
_OVERLAP = 'overlap'
_TAIL = 'tail'

# Default budget (in dynamic-programming cells) for the exact uniqueness
# check of a gap alignment.  Gaps are normally tiny (a corrected tail);
# larger ambiguous gaps are reported as undecided instead of guessed.
_DEFAULT_MAX_EXACT_CELLS = 62_500


@dataclass
class Correction:
    """A decided revision inside the overlap window: the previous
    export's points were replaced by the current export's points."""
    previous_points: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)
    current_points: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)


@dataclass
class UndecidedRange:
    """A range where several equally good alignments exist, so no merge
    or retraction was performed.  The previous export's points were kept
    verbatim; the current export's points are reported here for the
    caller to resolve."""
    previous_points: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)
    current_points: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)
    region: str = _OVERLAP
    start_time: Optional[mod_datetime.datetime] = None
    end_time: Optional[mod_datetime.datetime] = None


@dataclass
class Reconciliation:
    """Result of reconciling two consecutive exports."""
    points: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)
    confirmed: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)
    inserted: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)
    retracted: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)
    kept: List[mod_gpx.GPXTrackPoint] = field(default_factory=list)
    corrections: List[Correction] = field(default_factory=list)
    undecided: List[UndecidedRange] = field(default_factory=list)
    overlap_window: Window = (None, None)

    @property
    def row_count(self) -> int:
        return len(self.points)

    @property
    def changed(self) -> bool:
        return bool(self.inserted or self.retracted)

    def to_gpx(self) -> mod_gpx.GPX:
        """Build a single-track, single-segment GPX with (deep copies of)
        the reconciled points, in reconciled order."""
        gpx = mod_gpx.GPX()
        track = mod_gpx.GPXTrack()
        segment = mod_gpx.GPXTrackSegment()
        segment.points = [mod_copy.deepcopy(point) for point in self.points]
        track.segments.append(segment)
        gpx.tracks.append(track)
        return gpx


def _key(point: mod_gpx.GPXTrackPoint) -> Tuple:
    """Observation identity used for alignment.  Not a unique id: several
    points may share it (same second, same place)."""
    return (point.time, point.latitude, point.longitude, point.elevation)


def _within(time: mod_datetime.datetime, lo: Optional[mod_datetime.datetime],
            hi: Optional[mod_datetime.datetime]) -> bool:
    if lo is not None and time < lo:
        return False
    if hi is not None and time > hi:
        return False
    return True


def _region_of(time: mod_datetime.datetime, overlap_lo: Optional[mod_datetime.datetime],
               overlap_hi: Optional[mod_datetime.datetime]) -> str:
    if overlap_lo is not None and time < overlap_lo:
        return _HEAD
    if overlap_hi is not None and time > overlap_hi:
        return _TAIL
    return _OVERLAP


def _window_start(a: Optional[mod_datetime.datetime],
                  b: Optional[mod_datetime.datetime]) -> Optional[mod_datetime.datetime]:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _window_end(a: Optional[mod_datetime.datetime],
                b: Optional[mod_datetime.datetime]) -> Optional[mod_datetime.datetime]:
    if a is None:
        return b
    if b is None:
        return a
    return min(a, b)


def _anchors(a_keys: List, b_keys: List) -> Optional[List[Tuple[int, int]]]:
    """Candidate anchors: values occurring exactly once in each sequence,
    paired by value.  Returns the (i, j) pairs in sequence order if they
    are non-crossing, or None when the pairs contradict each other's
    order (contested alignment)."""
    a_counts = mod_Counter(a_keys)
    b_counts = mod_Counter(b_keys)
    a_index = {}
    for i, key in enumerate(a_keys):
        if a_counts[key] == 1 and b_counts[key] == 1:
            a_index[key] = i
    pairs = []
    for j, key in enumerate(b_keys):
        i = a_index.get(key)
        if i is not None:
            pairs.append((i, j))
    pairs.sort()
    js = [j for _, j in pairs]
    for k in range(len(js) - 1):
        if js[k] >= js[k + 1]:
            return None
    return pairs


def _unique_alignment(a_keys: List, b_keys: List,
                      max_exact_cells: int) -> Optional[List[Tuple[int, int]]]:
    """The matched (i, j) pairs of the optimal order-preserving alignment
    of the two key sequences, or None when several optimal alignments
    exist (or the exact check exceeds max_exact_cells cells)."""
    n, m = len(a_keys), len(b_keys)
    if n == 0 or m == 0:
        return []
    if a_keys == b_keys:
        return [(i, i) for i in range(n)]

    # Fast path: with all-distinct values on both sides the pairing is
    # forced; it is unambiguous exactly when the common values appear in
    # the same relative order.
    if len(set(a_keys)) == n and len(set(b_keys)) == m:
        b_index = {key: j for j, key in enumerate(b_keys)}
        a_set = set(a_keys)
        common_a = [key for key in a_keys if key in b_index]
        common_b = [key for key in b_keys if key in a_set]
        if common_a == common_b:
            return [(i, b_index[key]) for i, key in enumerate(a_keys) if key in b_index]

    if n * m > max_exact_cells:
        return None

    # LCS lengths: L[i][j] = LCS length of a_keys[i:], b_keys[j:].
    L = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        a_i = a_keys[i]
        row = L[i]
        next_row = L[i + 1]
        for j in range(m - 1, -1, -1):
            if a_i == b_keys[j]:
                row[j] = next_row[j + 1] + 1
            else:
                value = next_row[j]
                if row[j + 1] > value:
                    value = row[j + 1]
                row[j] = value

    # Count distinct optimal match-sets (capped at 2) by decomposing on
    # the first matched pair.  A pair (i2, j2) can be the first match of
    # an optimal alignment of a_keys[i:], b_keys[j:] iff skipping to it
    # loses nothing (L[i2][j2] == L[i][j]) and matching it is optimal.
    @lru_cache(maxsize=None)
    def _count(i: int, j: int) -> int:
        lij = L[i][j]
        if lij == 0:
            return 1
        total = 0
        for i2 in range(i, n):
            a_i2 = a_keys[i2]
            row_i2 = L[i2]
            next_row_i2 = L[i2 + 1]
            for j2 in range(j, m):
                if b_keys[j2] == a_i2 and row_i2[j2] == lij \
                        and row_i2[j2] == next_row_i2[j2 + 1] + 1:
                    total += _count(i2 + 1, j2 + 1)
                    if total >= 2:
                        return 2
        return total

    if _count(0, 0) != 1:
        return None

    alignment = []
    i = j = 0
    while i < n and j < m and L[i][j] > 0:
        if a_keys[i] == b_keys[j] and L[i][j] == L[i + 1][j + 1] + 1:
            alignment.append((i, j))
            i += 1
            j += 1
        elif L[i][j] == L[i + 1][j]:
            i += 1
        else:
            j += 1
    return alignment


class _RegionReport:
    def __init__(self) -> None:
        self.emitted: List[mod_gpx.GPXTrackPoint] = []
        self.confirmed: List[mod_gpx.GPXTrackPoint] = []
        self.inserted: List[mod_gpx.GPXTrackPoint] = []
        self.retracted: List[mod_gpx.GPXTrackPoint] = []
        self.kept: List[mod_gpx.GPXTrackPoint] = []
        self.corrections: List[Correction] = []
        self.undecided: List[UndecidedRange] = []


def _time_bounds(a: Sequence[mod_gpx.GPXTrackPoint],
                 b: Sequence[mod_gpx.GPXTrackPoint]) -> Window:
    times = [p.time for p in list(a) + list(b) if p.time is not None]
    if not times:
        return (None, None)
    return (min(times), max(times))


def _resolve_gap(a_gap: List[mod_gpx.GPXTrackPoint],
                 b_gap: List[mod_gpx.GPXTrackPoint],
                 authoritative: bool, region: str,
                 max_exact_cells: int, report: _RegionReport) -> None:
    """Resolve one bracketed gap between two anchors (or sequence ends).
    In authoritative regions (the overlap window) the current export
    wins; elsewhere the previous export is kept and only demonstrably
    new current points are added."""
    if not a_gap and not b_gap:
        return
    a_keys = [_key(p) for p in a_gap]
    b_keys = [_key(p) for p in b_gap]
    if a_keys == b_keys:
        report.confirmed.extend(a_gap)
        report.emitted.extend(a_gap)
        return
    if not a_gap:
        report.inserted.extend(b_gap)
        report.emitted.extend(b_gap)
        return
    if not b_gap:
        if authoritative:
            # Both exports guarantee this window: vanishing here is a
            # retraction by the later export.
            report.retracted.extend(a_gap)
        else:
            # Outside the overlap window vanished points are kept.
            report.kept.extend(a_gap)
            report.emitted.extend(a_gap)
        return

    alignment = _unique_alignment(a_keys, b_keys, max_exact_cells)
    if alignment is None:
        # Several equally good alignments: not enough evidence to merge
        # or retract.  Keep the previous points, report the range.
        start_time, end_time = _time_bounds(a_gap, b_gap)
        report.undecided.append(UndecidedRange(
            previous_points=list(a_gap), current_points=list(b_gap),
            region=region, start_time=start_time, end_time=end_time))
        report.kept.extend(a_gap)
        report.emitted.extend(a_gap)
        return

    if authoritative:
        report.corrections.append(Correction(
            previous_points=list(a_gap), current_points=list(b_gap)))
        report.retracted.extend(a_gap)
        report.inserted.extend(b_gap)
        report.emitted.extend(b_gap)
        return

    matched_a = {i for i, _ in alignment}
    matched_b = {j for _, j in alignment}
    for i, point in enumerate(a_gap):
        if i in matched_a:
            report.confirmed.append(point)
        else:
            report.kept.append(point)
        report.emitted.append(point)
    for j, point in enumerate(b_gap):
        if j not in matched_b:
            report.inserted.append(point)
            report.emitted.append(point)


def _reconcile_region(p_points: List[mod_gpx.GPXTrackPoint],
                      c_points: List[mod_gpx.GPXTrackPoint],
                      authoritative: bool, region: str,
                      max_exact_cells: int) -> _RegionReport:
    report = _RegionReport()
    a_keys = [_key(p) for p in p_points]
    b_keys = [_key(p) for p in c_points]
    if a_keys == b_keys:
        report.confirmed.extend(p_points)
        report.emitted.extend(p_points)
        return report

    anchors = _anchors(a_keys, b_keys)
    if anchors is None:
        # Unique-value anchors contradict each other's order: the whole
        # region is one contested gap.
        _resolve_gap(p_points, c_points, authoritative, region,
                     max_exact_cells, report)
        return report

    chain = [(-1, -1)] + anchors + [(len(p_points), len(c_points))]
    for (i0, j0), (i1, j1) in zip(chain, chain[1:]):
        if i0 >= 0:
            report.emitted.append(p_points[i0])
            report.confirmed.append(p_points[i0])
        _resolve_gap(p_points[i0 + 1:i1], c_points[j0 + 1:j1],
                     authoritative, region, max_exact_cells, report)
    return report


def reconcile(previous: Sequence[mod_gpx.GPXTrackPoint],
              current: Sequence[mod_gpx.GPXTrackPoint],
              previous_window: Window,
              current_window: Window,
              max_exact_cells: int = _DEFAULT_MAX_EXACT_CELLS) -> Reconciliation:
    """Reconcile two consecutive partial exports into one ordered point
    sequence.

    previous / current: ordered track points of each export (order is
        evidence; it is preserved, never sorted away).
    previous_window / current_window: (start, end) timestamps the caller
        guarantees each export to be complete for; a None bound means
        unbounded.

    Returns a Reconciliation with the merged points and full reporting
    (confirmed / inserted / retracted / kept / corrections / undecided).
    """
    p_lo, p_hi = previous_window
    c_lo, c_hi = current_window
    overlap_lo = _window_start(p_lo, c_lo)
    overlap_hi = _window_end(p_hi, c_hi)

    previous = list(previous)
    current = list(current)

    p_timed = [p for p in previous if p.time is not None]
    p_untimed = [p for p in previous if p.time is None]
    # Only the guaranteed-complete part of the current export is
    # evidence; its unguaranteed head/tail is ignored.
    c_timed = [p for p in current
               if p.time is not None and _within(p.time, c_lo, c_hi)]
    c_untimed = [p for p in current if p.time is None]

    result = Reconciliation(overlap_window=(overlap_lo, overlap_hi))

    has_overlap = not (overlap_lo is not None and overlap_hi is not None
                       and overlap_lo > overlap_hi)

    if has_overlap:
        merged_timed: List[mod_gpx.GPXTrackPoint] = []
        for region in (_HEAD, _OVERLAP, _TAIL):
            p_region = [p for p in p_timed
                        if _region_of(p.time, overlap_lo, overlap_hi) == region]
            c_region = [p for p in c_timed
                        if _region_of(p.time, overlap_lo, overlap_hi) == region]
            report = _reconcile_region(p_region, c_region,
                                       authoritative=(region == _OVERLAP),
                                       region=region,
                                       max_exact_cells=max_exact_cells)
            merged_timed.extend(report.emitted)
            result.confirmed.extend(report.confirmed)
            result.inserted.extend(report.inserted)
            result.retracted.extend(report.retracted)
            result.kept.extend(report.kept)
            result.corrections.extend(report.corrections)
            result.undecided.extend(report.undecided)
    else:
        # No shared guaranteed window: keep the previous export as-is
        # and append the guaranteed part of the current one.  If that
        # part is already the suffix of the previous sequence (the same
        # export imported twice), nothing is added.
        result.kept.extend(p_timed)
        merged_timed = list(p_timed)
        c_keys = [_key(p) for p in c_timed]
        p_keys = [_key(p) for p in p_timed]
        if c_keys and p_keys[-len(c_keys):] != c_keys:
            merged_timed.extend(c_timed)
            result.inserted.extend(c_timed)

    # Untimed previous points are kept, glued after the timed point they
    # followed in the original export (or at the head).
    emitted_ids = {id(p) for p in merged_timed}
    followers = {}
    lead: List[mod_gpx.GPXTrackPoint] = []
    last_alive: Optional[int] = None
    for point in previous:
        if point.time is None:
            if last_alive is None:
                lead.append(point)
            else:
                followers.setdefault(last_alive, []).append(point)
        elif id(point) in emitted_ids:
            last_alive = id(point)
    merged: List[mod_gpx.GPXTrackPoint] = list(lead)
    for point in merged_timed:
        merged.append(point)
        merged.extend(followers.get(id(point), ()))
    result.kept.extend(p_untimed)

    # Untimed current points are appended unless an identical untimed
    # point is already present (keeps re-import idempotent).
    known_untimed = {(p.latitude, p.longitude, p.elevation) for p in p_untimed}
    for point in c_untimed:
        key = (point.latitude, point.longitude, point.elevation)
        if key not in known_untimed:
            merged.append(point)
            result.inserted.append(point)
            known_untimed.add(key)

    result.points = merged
    return result


def reconcile_gpx(previous_gpx: mod_gpx.GPX, current_gpx: mod_gpx.GPX,
                  previous_window: Window, current_window: Window,
                  max_exact_cells: int = _DEFAULT_MAX_EXACT_CELLS) -> Reconciliation:
    """Reconcile two GPX documents (all track points in walk order).
    Use Reconciliation.to_gpx() on the result to obtain a merged GPX."""
    previous = list(previous_gpx.walk(only_points=True))
    current = list(current_gpx.walk(only_points=True))
    return reconcile(previous, current, previous_window, current_window,
                     max_exact_cells=max_exact_cells)
