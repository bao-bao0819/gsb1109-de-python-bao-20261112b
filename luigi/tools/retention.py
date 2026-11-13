# -*- coding: utf-8 -*-
"""
View-only retention planner for quarterly audits.

Given a manifest describing Luigi local targets (size, expiry and dependency
alternatives) plus the reports an audit must be able to recompute, this tool
picks a set of files to *keep* so that every designated report retains at
least one complete rebuild path within a given space budget.

Properties of the planner:

* Shared source files are only counted (and listed) once.
* A dependency cycle with no original input at its base is not rebuildable
  evidence; plans that would rely on it are discarded.
* Expired targets (TTL before the audit date) cannot serve as evidence.
* The output is a view-only retention list with per-report rebuild
  explanations.  When the budget cannot cover every report the tool says so
  and deliberately offers no deletion suggestions.

Manifest format (JSON)::

    {
      "audit_date": "2026-11-13",          # optional, defaults to today
      "budget": 500,                        # optional, --budget overrides
      "targets": {
        "raw_events":   {"size": 120, "expires": "2027-03-31"},
        "daily_rollup": {"size": 40, "depends_on": [["raw_events"]]},
        "q_report":     {"size": 10,
                         "depends_on": [["daily_rollup"], ["raw_events"]]}
      },
      "reports": ["q_report"]
    }

``depends_on`` is a list of equivalent alternatives (OR); each alternative is
a list of targets that are all required together (AND).  A target without
``depends_on`` is an original input.  Every target in a rebuild path is kept,
including the report file itself.

CLI::

    python -m luigi.tools.retention manifest.json [--budget BYTES] [--json]
"""

import itertools
import json
import sys
from dataclasses import dataclass, field
from datetime import date

# Cap on rebuild plans enumerated per target, keeps pathological OR-graphs
# tractable.  Only the cheapest plans are retained.
PLANS_CAP = 256

# Cap on plan combinations explored while selecting one plan per report.
COMBINATION_CAP = 250000


@dataclass(frozen=True)
class Target:
    name: str
    size: int
    expires: date | None = None
    depends_on: tuple = ()  # tuple of alternatives; each alternative a tuple of names

    @property
    def is_source(self):
        return not self.depends_on


@dataclass
class Manifest:
    targets: dict
    reports: list
    budget: int | None = None
    audit_date: date = field(default_factory=date.today)


def _parse_date(value, what):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise ValueError(f"invalid ISO date for {what}: {value!r}")


def load_manifest(path):
    with open(path, encoding="utf-8") as handle:
        return parse_manifest(json.load(handle))


def parse_manifest(data):
    audit_date = date.today()
    if data.get("audit_date"):
        audit_date = _parse_date(data["audit_date"], "audit_date")

    raw_targets = data.get("targets")
    if not isinstance(raw_targets, dict) or not raw_targets:
        raise ValueError("manifest must define a non-empty 'targets' object")

    targets = {}
    for name, spec in raw_targets.items():
        size = spec.get("size")
        if not isinstance(size, int) or size < 0:
            raise ValueError(f"target {name!r} needs a non-negative integer 'size'")
        expires = spec.get("expires")
        alternatives = []
        for alternative in spec.get("depends_on", []):
            if not alternative:
                raise ValueError(f"target {name!r} has an empty dependency alternative")
            for dep in alternative:
                if dep not in raw_targets:
                    raise ValueError(f"target {name!r} depends on unknown target {dep!r}")
            alternatives.append(tuple(alternative))
        targets[name] = Target(
            name=name,
            size=size,
            expires=_parse_date(expires, f"expires of {name!r}") if expires else None,
            depends_on=tuple(alternatives),
        )

    reports = data.get("reports")
    if not isinstance(reports, list) or not reports:
        raise ValueError("manifest must list at least one designated report")
    for report in reports:
        if report not in targets:
            raise ValueError(f"designated report {report!r} is not a known target")

    budget = data.get("budget")
    if budget is not None and (not isinstance(budget, int) or budget < 0):
        raise ValueError("budget must be a non-negative integer")

    return Manifest(targets=targets, reports=list(reports), budget=budget, audit_date=audit_date)


def _usable(target, audit_date):
    return target.expires is None or target.expires >= audit_date


def _keep_cheapest(plans, targets):
    ordered = sorted(plans, key=lambda p: (sum(targets[n].size for n in p), len(p), sorted(p)))
    return ordered[:PLANS_CAP]


def rebuild_plans(manifest, name, _stack=()):
    """All valid rebuild plans for ``name`` as a list of frozensets of target names.

    A plan is the set of files that must exist to recompute ``name``: the
    target itself plus, recursively, one chosen dependency alternative per
    non-source target.  Plans that loop through a dependency cycle without
    reaching an original input, or that need an expired file, are discarded.
    """
    target = manifest.targets[name]
    if not _usable(target, manifest.audit_date):
        return []
    if name in _stack:
        return []
    if target.is_source:
        return [frozenset({name})]

    plans = []
    stack = _stack + (name,)
    for alternative in target.depends_on:
        combined = [frozenset({name})]
        for dep in alternative:
            sub_plans = rebuild_plans(manifest, dep, stack)
            if not sub_plans:
                combined = []
                break
            combined = [a | b for a in combined for b in sub_plans]
        plans.extend(combined)
        plans = _keep_cheapest(plans, manifest.targets)
    return plans


def _plan_cost(plan, targets):
    return sum(targets[name].size for name in plan)


@dataclass
class ReportPlan:
    report: str
    files: frozenset
    steps: list  # ordered (name, rebuilt_from) pairs, sources first


@dataclass
class RetentionPlan:
    feasible: bool
    budget: int | None
    audit_date: date
    kept: list = field(default_factory=list)  # Target objects, sorted by name
    total_size: int = 0
    minimum_size: int = 0
    reports: list = field(default_factory=list)  # ReportPlan, designated order
    uncoverable: list = field(default_factory=list)  # reports with no valid plan


def _rebuild_steps(manifest, report, chosen):
    """Order a chosen plan into rebuild steps, original inputs first."""
    steps = []
    visited = set()

    def visit(name):
        if name in visited:
            return
        visited.add(name)
        target = manifest.targets[name]
        rebuilt_from = ()
        if not target.is_source:
            for alternative in target.depends_on:
                if all(dep in chosen for dep in alternative):
                    rebuilt_from = alternative
                    break
        for dep in rebuilt_from:
            visit(dep)
        steps.append((name, rebuilt_from))

    visit(report)
    return steps


def plan_retention(manifest):
    """Pick one rebuild plan per report, minimizing total bytes kept."""
    per_report = []
    uncoverable = []
    for report in manifest.reports:
        plans = rebuild_plans(manifest, report)
        if not plans:
            uncoverable.append(report)
        else:
            per_report.append((report, plans))

    result = RetentionPlan(
        feasible=False,
        budget=manifest.budget,
        audit_date=manifest.audit_date,
        uncoverable=uncoverable,
    )
    if uncoverable:
        return result

    combinations = 1
    for _, plans in per_report:
        combinations *= len(plans)
    if combinations <= COMBINATION_CAP:
        candidates = itertools.product(*(plans for _, plans in per_report))
    else:
        # Greedy fallback for very large search spaces: cheapest plan per
        # report (plans are enumerated cheapest-first), no further search.
        candidates = [tuple(plans[0] for _, plans in per_report)]

    best_combo = None
    best_union = None
    for combo in candidates:
        union = frozenset().union(*combo)
        if best_union is None or _plan_cost(union, manifest.targets) < _plan_cost(best_union, manifest.targets):
            best_combo = combo
            best_union = union

    result.minimum_size = _plan_cost(best_union, manifest.targets)
    if manifest.budget is not None and result.minimum_size > manifest.budget:
        return result

    result.feasible = True
    result.total_size = result.minimum_size
    result.kept = sorted(
        (manifest.targets[name] for name in best_union),
        key=lambda t: t.name,
    )
    for (report, _), files in zip(per_report, best_combo):
        result.reports.append(
            ReportPlan(
                report=report,
                files=files,
                steps=_rebuild_steps(manifest, report, files),
            )
        )
    return result


def render(plan):
    """Human-readable, view-only retention list."""
    lines = []
    budget = "unlimited" if plan.budget is None else f"{plan.budget} B"
    lines.append(f"Retention plan as of {plan.audit_date.isoformat()} (view-only: no files are deleted)")
    lines.append(f"Space budget: {budget}")

    if plan.uncoverable:
        lines.append("INFEASIBLE: no complete rebuild path exists for: " + ", ".join(plan.uncoverable))
        lines.append("No retention list is produced and no deletion suggestions are given.")
        return "\n".join(lines)

    if not plan.feasible:
        lines.append(f"INFEASIBLE: the cheapest complete coverage needs {plan.minimum_size} B, which exceeds the budget.")
        lines.append("No retention list is produced and no deletion suggestions are given.")
        return "\n".join(lines)

    lines.append(f"Keep {len(plan.kept)} file(s), {plan.total_size} B in total:")
    for target in plan.kept:
        expires = target.expires.isoformat() if target.expires else "no expiry"
        lines.append(f"  {target.name}: {target.size} B (expires: {expires})")

    lines.append("")
    lines.append("Rebuild instructions per report:")
    for report_plan in plan.reports:
        lines.append(f"  {report_plan.report}:")
        for name, rebuilt_from in report_plan.steps:
            if rebuilt_from:
                lines.append(f"    recompute {name} from {', '.join(rebuilt_from)}")
            else:
                lines.append(f"    keep original input {name}")
    return "\n".join(lines)


def to_dict(plan):
    """Machine-readable view of the retention plan."""
    data = {
        "audit_date": plan.audit_date.isoformat(),
        "budget": plan.budget,
        "feasible": plan.feasible,
        "view_only": True,
    }
    if plan.uncoverable:
        data["uncoverable_reports"] = plan.uncoverable
        return data
    data["minimum_size"] = plan.minimum_size
    if not plan.feasible:
        return data
    data["total_size"] = plan.total_size
    data["keep"] = [
        {
            "name": t.name,
            "size": t.size,
            "expires": t.expires.isoformat() if t.expires else None,
        }
        for t in plan.kept
    ]
    data["reports"] = {
        rp.report: [{"recompute": name, "from": list(rebuilt_from)} if rebuilt_from else {"original_input": name} for name, rebuilt_from in rp.steps]
        for rp in plan.reports
    }
    return data


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="python -m luigi.tools.retention",
        description="View-only audit retention planner for Luigi local targets.",
    )
    parser.add_argument("manifest", help="path to the JSON retention manifest")
    parser.add_argument("--budget", type=int, default=None, help="space budget in bytes (overrides the manifest)")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args(argv)

    manifest = load_manifest(args.manifest)
    if args.budget is not None:
        if args.budget < 0:
            parser.error("--budget must be non-negative")
        manifest.budget = args.budget

    plan = plan_retention(manifest)
    if args.json:
        print(json.dumps(to_dict(plan), indent=2))
    else:
        print(render(plan))
    return 0 if plan.feasible else 1


if __name__ == "__main__":
    sys.exit(main())
