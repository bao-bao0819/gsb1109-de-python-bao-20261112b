# -*- coding: utf-8 -*-
#
# Copyright 2026 Spotify AB.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and limitations
# under the License.
#
"""
Replenishment planning for corridor imagery coverage.

Imagery catalogues are collected per orbit segment (a *voyage*), while the
business report checks coverage against mileage intervals along a corridor.
A voyage covers one nominal mileage interval minus its cloud-obscured gaps,
carries a procurement price and may be archived (sealed voyages can no longer
be ordered).

Given the mileage segments that are still missing and the candidate voyages,
:func:`plan_replenishment` produces a replayable plan:

* each candidate voyage is annotated with the mileage intervals it can
  actually cover (cloud gaps excluded), its price and its archive state;
* one image may fill several disjoint gaps, so a single voyage is ordered at
  most once even when it covers multiple missing segments;
* among the feasible combinations the one with the lowest total price is
  chosen;
* mileage that no orderable voyage can cover is reported as explicit
  ``unreachable_segments`` endpoints instead of spawning a task that can
  never complete.

The :class:`ImageryReplenishmentTask` wraps the planner as a Luigi task whose
JSON output is deterministic and therefore safe to replay.

Example::

    required = [(0.0, 100.0)]
    candidates = [
        {"voyage_id": "V1", "start": 0.0, "end": 60.0,
         "price": 100.0, "archived": False, "cloud_gaps": [(40.0, 50.0)]},
        {"voyage_id": "V2", "start": 40.0, "end": 100.0,
         "price": 80.0, "archived": False, "cloud_gaps": []},
    ]
    plan = plan_replenishment(required, candidates)
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import luigi

logger = logging.getLogger("luigi-interface")


@dataclass(frozen=True)
class CandidateVoyage:
    """One orderable orbit segment and its usable mileage footprint."""

    voyage_id: str
    start: float
    end: float
    price: float
    archived: bool = False
    cloud_gaps: tuple[tuple[float, float], ...] = ()

    @property
    def nominal_interval(self) -> tuple[float, float]:
        return (self.start, self.end)


@dataclass(frozen=True)
class VoyageOrder:
    """A voyage selected for procurement, with the mileage it contributes."""

    voyage_id: str
    price: float
    archived: bool
    covered_segments: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class CandidateAnnotation:
    """Every input voyage annotated with its usable mileage footprint."""

    voyage_id: str
    price: float
    archived: bool
    coverage_segments: tuple[tuple[float, float], ...]


@dataclass(frozen=True)
class ReplenishmentPlan:
    """The full, replayable outcome of a replenishment run."""

    required_segments: tuple[tuple[float, float], ...]
    candidates: tuple[CandidateAnnotation, ...]
    orders: tuple[VoyageOrder, ...]
    total_price: float
    unreachable_segments: tuple[tuple[float, float], ...]
    excluded_archived: tuple[str, ...]

    @property
    def feasible(self) -> bool:
        return not self.unreachable_segments

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_segments": [list(segment) for segment in self.required_segments],
            "candidates": [
                {
                    "voyage_id": annotation.voyage_id,
                    "price": annotation.price,
                    "archived": annotation.archived,
                    "coverage_segments": [list(segment) for segment in annotation.coverage_segments],
                }
                for annotation in self.candidates
            ],
            "orders": [
                {
                    "voyage_id": order.voyage_id,
                    "price": order.price,
                    "archived": order.archived,
                    "covered_segments": [list(segment) for segment in order.covered_segments],
                }
                for order in self.orders
            ],
            "total_price": self.total_price,
            "unreachable_segments": [list(segment) for segment in self.unreachable_segments],
            "excluded_archived": list(self.excluded_archived),
            "feasible": self.feasible,
        }


def coerce_segments(raw) -> tuple[tuple[float, float], ...]:
    return tuple((float(start), float(end)) for start, end in raw)


def coerce_candidates(raw) -> list[CandidateVoyage]:
    """Accept :class:`CandidateVoyage` objects or plain mapping records."""
    voyages: list[CandidateVoyage] = []
    for entry in raw:
        if isinstance(entry, CandidateVoyage):
            voyages.append(entry)
            continue
        if not isinstance(entry, Mapping):
            raise TypeError(f"candidate voyage must be a mapping or CandidateVoyage, got {type(entry)!r}")
        data = dict(entry)
        gaps = tuple(sorted((float(start), float(end)) for start, end in (data.get("cloud_gaps") or ())))
        voyages.append(
            CandidateVoyage(
                voyage_id=str(data["voyage_id"]),
                start=float(data["start"]),
                end=float(data["end"]),
                price=float(data["price"]),
                archived=bool(data.get("archived", False)),
                cloud_gaps=gaps,
            )
        )
    return voyages


def _merge(intervals, tolerance: float) -> list[tuple[float, float]]:
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if end - start <= tolerance:
            continue
        if not merged or start > merged[-1][1] + tolerance:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    return [(start, end) for start, end in merged]


def _subtract(interval, gaps, tolerance: float) -> list[tuple[float, float]]:
    start, end = interval
    pieces = [[start, end]]
    for gap_start, gap_end in sorted(gaps):
        gap_start = max(gap_start, start)
        gap_end = min(gap_end, end)
        if gap_end - gap_start <= tolerance:
            continue
        remaining: list[list[float]] = []
        for piece_start, piece_end in pieces:
            if gap_end <= piece_start or gap_start >= piece_end:
                remaining.append([piece_start, piece_end])
            else:
                if gap_start > piece_start:
                    remaining.append([piece_start, gap_start])
                if gap_end < piece_end:
                    remaining.append([gap_end, piece_end])
        pieces = remaining
    return [(piece_start, piece_end) for piece_start, piece_end in pieces if piece_end - piece_start > tolerance]


def _uncovered(required, covered, tolerance: float) -> list[tuple[float, float]]:
    uncovered: list[tuple[float, float]] = []
    for required_start, required_end in required:
        remaining = [[required_start, required_end]]
        for covered_start, covered_end in covered:
            leftover: list[list[float]] = []
            for piece_start, piece_end in remaining:
                if covered_end <= piece_start or covered_start >= piece_end:
                    leftover.append([piece_start, piece_end])
                else:
                    if covered_start > piece_start:
                        leftover.append([piece_start, covered_start])
                    if covered_end < piece_end:
                        leftover.append([covered_end, piece_end])
            remaining = leftover
        uncovered.extend((piece_start, piece_end) for piece_start, piece_end in remaining if piece_end - piece_start > tolerance)
    return _merge(uncovered, tolerance)


def _intersect(intervals, clipping_segments, tolerance: float) -> list[tuple[float, float]]:
    intersections: list[tuple[float, float]] = []
    for start, end in intervals:
        for clip_start, clip_end in clipping_segments:
            overlap_start = max(start, clip_start)
            overlap_end = min(end, clip_end)
            if overlap_end - overlap_start > tolerance:
                intersections.append((overlap_start, overlap_end))
    return _merge(intersections, tolerance)


def _validate(voyages: list[CandidateVoyage], tolerance: float) -> None:
    seen_ids: set[str] = set()
    for voyage in voyages:
        if voyage.voyage_id in seen_ids:
            raise ValueError(f"duplicate voyage_id: {voyage.voyage_id!r}")
        seen_ids.add(voyage.voyage_id)
        if voyage.end - voyage.start <= tolerance:
            raise ValueError(f"voyage {voyage.voyage_id!r} has an empty footprint: {voyage.start} -> {voyage.end}")
        if voyage.price < 0:
            raise ValueError(f"voyage {voyage.voyage_id!r} has a negative price: {voyage.price}")
        for gap_start, gap_end in voyage.cloud_gaps:
            if gap_end - gap_start <= tolerance:
                raise ValueError(f"voyage {voyage.voyage_id!r} has an empty cloud gap: {gap_start} -> {gap_end}")
            if gap_start < voyage.start - tolerance or gap_end > voyage.end + tolerance:
                raise ValueError(f"voyage {voyage.voyage_id!r} cloud gap {gap_start} -> {gap_end} falls outside footprint {voyage.start} -> {voyage.end}")


def _drop_dominated(items, tolerance: float):
    """Remove a voyage when another one is no pricier and covers at least as much."""
    kept = []
    for voyage_id, price, pieces in items:
        coverage = _merge(pieces, tolerance)
        for other_id, other_price, other_pieces in items:
            if voyage_id == other_id:
                continue
            strictly_cheaper = other_price < price - tolerance
            equally_cheap_but_ordered = abs(other_price - price) <= tolerance and other_id < voyage_id
            if not (strictly_cheaper or equally_cheap_but_ordered):
                continue
            if not _uncovered(coverage, _merge(other_pieces, tolerance), tolerance):
                break
        else:
            kept.append((voyage_id, price, pieces))
    return kept


def _cheapest_cover(target, items, tolerance: float) -> tuple[str, ...]:
    if not target:
        return ()

    best_price: float | None = None
    best_selection: tuple[str, ...] = ()
    seen_states: set[tuple[int, tuple[tuple[float, float], ...], float]] = set()

    def search(index: int, covered, selected: list[str], price: float) -> None:
        nonlocal best_price, best_selection
        if not _uncovered(target, covered, tolerance):
            if best_price is None or price < best_price - tolerance:
                best_price = price
                best_selection = tuple(selected)
            return
        if index >= len(items):
            return
        state = (index, tuple(covered), price)
        if state in seen_states:
            return
        seen_states.add(state)
        if best_price is not None and price >= best_price - tolerance:
            return

        voyage_id, voyage_price, pieces = items[index]
        extended = _merge(covered + list(pieces), tolerance)
        if extended != covered:
            selected.append(voyage_id)
            search(index + 1, extended, selected, price + voyage_price)
            selected.pop()
        search(index + 1, covered, selected, price)

    search(0, [], [], 0.0)
    if best_price is None:
        raise RuntimeError("no combination covers the feasible target; unreachable segments were not detected")
    return best_selection


def plan_replenishment(required_segments, candidates, tolerance: float = 1e-9) -> ReplenishmentPlan:
    """Build the cheapest feasible replenishment plan for missing mileage.

    :param required_segments: iterable of ``(start, end)`` mileage intervals
        that still lack coverage.
    :param candidates: iterable of :class:`CandidateVoyage` objects or mapping
        records carrying ``voyage_id``, ``start``, ``end``, ``price``,
        ``archived`` and ``cloud_gaps``.
    :param tolerance: mileage tolerance used when merging and comparing
        intervals.
    """
    required = _merge(coerce_segments(required_segments), tolerance)
    voyages = coerce_candidates(candidates)
    _validate(voyages, tolerance)

    excluded_archived = tuple(sorted(voyage.voyage_id for voyage in voyages if voyage.archived))
    available = [voyage for voyage in voyages if not voyage.archived]

    # Effective coverage (footprint minus cloud gaps) is computed for every
    # candidate so each one can be annotated, including archived voyages.
    effective_coverage: dict[str, list[tuple[float, float]]] = {
        voyage.voyage_id: _subtract(voyage.nominal_interval, voyage.cloud_gaps, tolerance) for voyage in voyages
    }
    annotations = tuple(
        CandidateAnnotation(
            voyage_id=voyage.voyage_id,
            price=voyage.price,
            archived=voyage.archived,
            coverage_segments=tuple(effective_coverage[voyage.voyage_id]),
        )
        for voyage in voyages
    )
    union_coverage = _merge(
        [segment for voyage in available for segment in effective_coverage[voyage.voyage_id]],
        tolerance,
    )
    unreachable = tuple(_uncovered(required, union_coverage, tolerance))
    feasible_target = _uncovered(required, list(unreachable), tolerance)

    items = []
    for voyage in available:
        within_target = _intersect(effective_coverage[voyage.voyage_id], feasible_target, tolerance)
        if within_target:
            items.append((voyage.voyage_id, voyage.price, tuple(within_target)))
    items = _drop_dominated(items, tolerance)
    items.sort(key=lambda item: (item[1], item[0]))

    selected = set(_cheapest_cover(feasible_target, items, tolerance))

    orders: list[VoyageOrder] = []
    for voyage in available:
        if voyage.voyage_id not in selected:
            continue
        covered = _intersect(effective_coverage[voyage.voyage_id], required, tolerance)
        orders.append(
            VoyageOrder(
                voyage_id=voyage.voyage_id,
                price=voyage.price,
                archived=voyage.archived,
                covered_segments=tuple(covered),
            )
        )
    orders.sort(key=lambda order: (order.covered_segments[0], order.voyage_id))

    return ReplenishmentPlan(
        required_segments=tuple(required),
        candidates=annotations,
        orders=tuple(orders),
        total_price=sum(order.price for order in orders),
        unreachable_segments=unreachable,
        excluded_archived=excluded_archived,
    )


class ImageryReplenishmentTask(luigi.Task):
    """Luigi task that writes a deterministic, replayable replenishment plan.

    The inputs are plain JSON-compatible Luigi list parameters so the task
    can be re-run identically from the command line or a scheduler. The output
    target contains one sorted JSON document describing every annotated
    candidate, the procurement orders and any unreachable mileage endpoints.
    """

    required_segments = luigi.ListParameter(
        description="Missing mileage intervals as [[start, end], ...].",
    )
    candidates = luigi.ListParameter(
        description=("Candidate voyages as mappings with voyage_id, start, end, price, archived and cloud_gaps."),
    )
    output_path = luigi.PathParameter(description="Destination JSON file for the plan.")
    tolerance = luigi.FloatParameter(default=1e-9, significant=False)

    def output(self):
        return luigi.LocalTarget(str(self.output_path))

    def build_plan(self) -> ReplenishmentPlan:
        return plan_replenishment(
            coerce_segments(self.required_segments),
            coerce_candidates(self.candidates),
            tolerance=float(self.tolerance),
        )

    def run(self):
        plan = self.build_plan()
        if not plan.feasible:
            logger.warning(
                "ImageryReplenishmentTask %s leaves unreachable mileage: %s",
                self.task_id,
                plan.unreachable_segments,
            )
        with self.output().open("w") as handle:
            json.dump(plan.to_dict(), handle, indent=2, sort_keys=True)
            handle.write("\n")
