# -*- coding: utf-8 -*-
"""
Cost-centre row outline planner.

The BIFF ``ROW`` record stores the outline level in three bits, so a worksheet
can only represent outline levels 0..7, and a sheet outline is a single
nested-interval structure: every group is one contiguous row range and groups
are either disjoint or strictly nested.

Real cost-centre tables do not always fit that model:

* a centre may be managed by more than one department, so its detail rows are
  claimed by two subtotal ranges that partially overlap;
* the organisational depth may exceed the eight levels (sheet root plus seven
  outline levels) that BIFF can store.

This module never silently discards one side of such a conflict.  Ranges that
can be represented together are kept as one hierarchical outline; ranges that
cannot be represented together are returned as :class:`OutlineConflict`
objects listing the competing ranges, and the caller decides which group to
keep via :meth:`OutlinePlan.select`.

Every outline emitted by :meth:`OutlinePlan.apply` is balanced: at any
collapsed level a parent subtotal still equals its expanded direct children
plus its ungrouped detail rows, and every subtotal can be re-computed from the
original detail rows with :func:`verify_subtotals`.
"""

import six

from .Cell import NumberCell

#: BIFF ROW record stores the outline level in three bits.
MAX_OUTLINE_LEVEL = 7

#: Level 0 is the sheet itself, so the deepest representable nesting is 8.
MAX_OUTLINE_DEPTH = MAX_OUTLINE_LEVEL + 1


class CostCenter(object):
    """A cost-centre subtotal range.

    :param name: centre identifier, used in conflict reports and ``select``.
    :param first_row: first detail row (zero based, inclusive).
    :param last_row: last row of the range (zero based, inclusive).
    :param subtotal_row: row holding the centre's subtotal.  May be ``None``
        when the input does not identify one; it must lie inside the range.
    :param subtotal_value: the booked subtotal amount, if already known.
        Kept for :func:`verify_subtotals`; it is never used to fabricate
        detail amounts.
    """

    __slots__ = ("name", "first_row", "last_row", "subtotal_row",
                 "subtotal_value")

    def __init__(self, name, first_row, last_row, subtotal_row=None,
                 subtotal_value=None):
        self._check_row("first_row", first_row)
        self._check_row("last_row", last_row)
        if first_row > last_row:
            raise ValueError(
                "cost centre %r: first_row %d is after last_row %d"
                % (name, first_row, last_row))
        if subtotal_row is not None:
            self._check_row("subtotal_row", subtotal_row)
            if not (first_row <= subtotal_row <= last_row):
                raise ValueError(
                    "cost centre %r: subtotal_row %d outside range [%d, %d]"
                    % (name, subtotal_row, first_row, last_row))
        if subtotal_value is not None and not isinstance(
                subtotal_value, (six.integer_types, float)):
            raise ValueError(
                "cost centre %r: subtotal_value must be numeric, got %r"
                % (name, type(subtotal_value)))
        self.name = name
        self.first_row = first_row
        self.last_row = last_row
        self.subtotal_row = subtotal_row
        self.subtotal_value = subtotal_value

    @staticmethod
    def _check_row(label, value):
        if not isinstance(value, six.integer_types) \
                or isinstance(value, bool):
            raise ValueError("%s must be an int, got %r" % (label, value))
        if not 0 <= value <= 65535:
            raise ValueError(
                "%s %d outside the BIFF row range 0..65535" % (label, value))

    def __repr__(self):
        return "CostCenter(%r, %d, %d, subtotal_row=%r, subtotal_value=%r)" % (
            self.name, self.first_row, self.last_row, self.subtotal_row,
            self.subtotal_value)

    def __eq__(self, other):
        if not isinstance(other, CostCenter):
            return NotImplemented
        return all(getattr(self, attr) == getattr(other, attr)
                   for attr in self.__slots__)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.name, self.first_row, self.last_row,
                     self.subtotal_row, self.subtotal_value))


class OutlineNode(object):
    """A :class:`CostCenter` placed in the representable hierarchy."""

    __slots__ = ("center", "parent", "children", "depth", "row_level")

    def __init__(self, center):
        self.center = center
        self.parent = None
        self.children = []
        self.depth = 1
        # Outline level written into the BIFF ROW record (1..7); deep chains
        # are compressed to 7 and surfaced as depth conflicts.
        self.row_level = 1

    @property
    def name(self):
        return self.center.name

    @property
    def first_row(self):
        return self.center.first_row

    @property
    def last_row(self):
        return self.center.last_row

    @property
    def subtotal_row(self):
        return self.center.subtotal_row

    def __repr__(self):
        return "OutlineNode(%r, depth=%d, row_level=%d)" % (
            self.name, self.depth, self.row_level)


class OutlineConflict(object):
    """Ranges that cannot be represented together in one sheet outline.

    :attr kind: ``"overlap"`` for partially overlapping ranges (cross-managed
        centres) or ``"depth"`` for a nesting chain deeper than BIFF allows.
    :attr ranges: the competing :class:`CostCenter` objects.
    :attr row: first row where the conflict is visible.
    :attr message: human-readable explanation; the structure, not this text,
        is the stable API.
    """

    __slots__ = ("kind", "ranges", "row", "message")

    def __init__(self, kind, ranges, row, message):
        self.kind = kind
        self.ranges = list(ranges)
        self.row = row
        self.message = message

    def __repr__(self):
        return "OutlineConflict(%r, %r, row=%d)" % (
            self.kind, [r.name for r in self.ranges], self.row)

    def __eq__(self, other):
        if not isinstance(other, OutlineConflict):
            return NotImplemented
        return (self.kind == other.kind
                and self.ranges == other.ranges
                and self.row == other.row)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


class SubtotalReport(object):
    """Result of re-computing one centre's subtotal from raw detail rows.

    :attr ok: ``True`` when no booked value was supplied or it equals the
        recomputed amount and no detail row lacks a value.
    :attr missing_rows: detail rows for which no amount could be found.
    :attr difference: ``recomputed - expected``, or ``None`` when no booked
        value was available.
    """

    __slots__ = ("center", "detail_rows", "recomputed", "expected",
                 "difference", "missing_rows", "ok")

    def __init__(self, center, detail_rows, recomputed, expected,
                 difference, missing_rows, ok):
        self.center = center
        self.detail_rows = detail_rows
        self.recomputed = recomputed
        self.expected = expected
        self.difference = difference
        self.missing_rows = missing_rows
        self.ok = ok

    def __repr__(self):
        return "SubtotalReport(%r, recomputed=%r, expected=%r, ok=%r)" % (
            self.center.name, self.recomputed, self.expected, self.ok)


class OutlinePlan(object):
    """A resolved outline: representable hierarchy plus unresolved conflicts.

    Attributes:

    * ``nodes`` -- root :class:`OutlineNode` objects of the hierarchy built
      from the ranges that can coexist.
    * ``all_nodes`` -- every node in that hierarchy (depth-first order).
    * ``conflicts`` -- :class:`OutlineConflict` objects the caller must settle
      with :meth:`select`.
    * ``row_levels`` -- mapping of row index to the BIFF outline level
      (0..7) to write on that row.  Rows absent from the mapping stay at 0.
    * ``max_depth`` -- nesting depth required by the kept ranges (a single
      root centre has depth 1).
    * ``truncated`` -- ``True`` when a chain was compressed into level 7;
      the corresponding depth conflicts explain which centres lost visual
      separation.
    """

    def __init__(self, centers):
        centers = [_coerce_center(center) for center in centers]
        self._check_duplicate_names(centers)
        self.conflicts = []
        self._centers = centers
        self.rows_by_name = dict(
            (center.name, center) for center in centers)

        overlap_components = self._find_overlap_conflicts(centers)
        conflicted = set()
        for component in overlap_components:
            conflicted.update(id(center) for center in component)
            self.conflicts.append(OutlineConflict(
                "overlap", component,
                min(center.first_row for center in component),
                "ranges %s overlap without strict nesting; a sheet outline "
                "can keep only one side"
                % ", ".join(repr(center.name) for center in component)))

        kept = [center for center in centers
                if id(center) not in conflicted]
        self.nodes = self._build_forest(kept)
        self.all_nodes = []
        for root in self.nodes:
            self.all_nodes.extend(_walk(root))

        depths = [node.depth for node in self.all_nodes]
        self.max_depth = max(depths) if depths else 0
        self.truncated = self.max_depth > MAX_OUTLINE_DEPTH
        self._find_depth_conflicts()

        self.row_levels = self._build_row_levels()

    @staticmethod
    def _check_duplicate_names(centers):
        seen = set()
        for center in centers:
            if center.name in seen:
                raise ValueError(
                    "duplicate cost centre name %r" % (center.name,))
            seen.add(center.name)

    @staticmethod
    def _find_overlap_conflicts(centers):
        # Union-find over centres that participate in a partial overlap.
        parent = dict((id(center), id(center)) for center in centers)

        def find(token):
            while parent[token] != token:
                parent[token] = parent[parent[token]]
                token = parent[token]
            return token

        def union(left, right):
            parent[find(id(left))] = find(id(right))

        for i, left in enumerate(centers):
            for right in centers[i + 1:]:
                if left.first_row > right.last_row \
                        or right.first_row > left.last_row:
                    continue
                left_contains = (left.first_row <= right.first_row
                                 and right.last_row <= left.last_row
                                 and (left.first_row < right.first_row
                                      or right.last_row < left.last_row))
                right_contains = (right.first_row <= left.first_row
                                  and left.last_row <= right.last_row
                                  and (right.first_row < left.first_row
                                       or left.last_row < right.last_row))
                if not left_contains and not right_contains:
                    union(left, right)

        components = {}
        for center in centers:
            components.setdefault(find(id(center)), []).append(center)
        return [sorted(component, key=lambda center: center.first_row)
                for component in components.values()
                if len(component) >= 2]

    @staticmethod
    def _build_forest(centers):
        nodes = dict((id(center), OutlineNode(center)) for center in centers)
        roots = []
        for center in centers:
            node = nodes[id(center)]
            parent_center = None
            parent_span = None
            for candidate in centers:
                if candidate is center:
                    continue
                if candidate.first_row <= center.first_row \
                        and center.last_row <= candidate.last_row \
                        and (candidate.first_row < center.first_row
                             or center.last_row < candidate.last_row):
                    span = (candidate.last_row - candidate.first_row)
                    if parent_span is None or span < parent_span:
                        parent_center = candidate
                        parent_span = span
            if parent_center is None:
                roots.append(node)
            else:
                parent_node = nodes[id(parent_center)]
                node.parent = parent_node
                parent_node.children.append(node)

        for root in roots:
            for node in _walk(root):
                node.depth = 1 if node.parent is None \
                    else node.parent.depth + 1
                node.row_level = min(node.depth, MAX_OUTLINE_LEVEL)
                node.children.sort(
                    key=lambda child: child.first_row)
        roots.sort(key=lambda node: node.first_row)
        return roots

    def _find_depth_conflicts(self):
        reported = set()
        for node in self.all_nodes:
            if node.depth != MAX_OUTLINE_DEPTH + 1:
                continue
            # Anchor on the deepest still representable ancestor (depth 8) so
            # every overflowing branch under it is reported in one conflict.
            anchor = node.parent
            if anchor is None or id(anchor) in reported:
                continue
            reported.add(id(anchor))
            overflow = [part for part in _walk(anchor)
                        if part.depth > MAX_OUTLINE_DEPTH]
            deepest = max(part.depth for part in overflow)
            chain = " -> ".join(
                part.name for part in _walk_parents(anchor))
            self.conflicts.append(OutlineConflict(
                "depth",
                [part.center for part in overflow],
                overflow[0].first_row,
                "nesting under %s reaches depth %d but BIFF outlines hold at "
                "most %d levels; the centres below %r are flattened in this "
                "plan, use select() to keep a shallower subtree"
                % (chain, deepest, MAX_OUTLINE_DEPTH, anchor.name)))

    def _build_row_levels(self):
        levels = {}
        for node in self.all_nodes:
            for rowx in range(node.first_row, node.last_row + 1):
                levels[rowx] = max(levels.get(rowx, 0), node.row_level)
        # Summary rows sit one outline step above their own detail (at the
        # parent's row level) so they stay visible when the centre collapses.
        # Claims are combined with min: a row closing several groups must stay
        # at the shallowest summary step.
        summary_claims = {}
        for node in self.all_nodes:
            if node.subtotal_row is None:
                continue
            summary_level = node.parent.row_level if node.parent else 0
            previous = summary_claims.get(node.subtotal_row)
            if previous is None or summary_level < previous:
                summary_claims[node.subtotal_row] = summary_level
        for rowx, summary_level in summary_claims.items():
            levels[rowx] = summary_level
        return levels

    def select(self, keep):
        """Rebuild the plan keeping only the named centres.

        :param keep: centre names or :class:`CostCenter` objects to retain.
        :returns: a new, independently validated :class:`OutlinePlan`.
        """
        wanted = set()
        for item in keep:
            if isinstance(item, CostCenter):
                wanted.add(item.name)
            else:
                wanted.add(item)
        unknown = wanted.difference(self.rows_by_name)
        if unknown:
            raise KeyError(
                "unknown cost centre name(s): %s"
                % ", ".join(sorted(repr(name) for name in unknown)))
        return OutlinePlan(
            [center for center in self._centers
             if center.name in wanted])

    def node_for(self, name):
        """Return the :class:`OutlineNode` of a kept centre, else ``None``."""
        for node in self.all_nodes:
            if node.name == name:
                return node
        return None

    def detail_rows(self, node):
        """Raw detail rows of a node, excluding every in-hierarchy subtotal."""
        subtotal_rows = set()
        for candidate in self.all_nodes:
            if candidate.subtotal_row is not None \
                    and node.first_row <= candidate.subtotal_row \
                    <= node.last_row:
                subtotal_rows.add(candidate.subtotal_row)
        return [rowx for rowx in range(node.first_row, node.last_row + 1)
                if rowx not in subtotal_rows]

    def direct_children(self, node):
        """Centres grouped directly under ``node`` (no centre in between)."""
        return list(node.children)

    def ungrouped_detail_rows(self, node):
        """Node rows covered by no direct child, excluding subtotal rows.

        These are the standalone detail amounts that, together with the
        direct children's subtotals, must reconcile to the node's subtotal.
        """
        covered = set()
        for child in node.children:
            for rowx in range(child.first_row, child.last_row + 1):
                covered.add(rowx)
        subtotal_rows = set()
        for candidate in self.all_nodes:
            if candidate.subtotal_row is not None \
                    and node.first_row <= candidate.subtotal_row \
                    <= node.last_row:
                subtotal_rows.add(candidate.subtotal_row)
        return [rowx for rowx in range(node.first_row, node.last_row + 1)
                if rowx not in covered and rowx not in subtotal_rows]

    def apply(self, worksheet, visible_level=None):
        """Write outline levels onto the rows of an xlwt worksheet.

        Only rows already created on the worksheet are touched; rows absent
        from :attr:`row_levels` are reset to level 0.  When ``visible_level``
        is given, deeper rows are hidden and the affected subtotal rows are
        marked collapsed, matching Excel's outline buttons.

        Subtotal rows are expected below their detail, the usual finance
        layout.  Set ``worksheet.outline_below = 1`` yourself so Excel draws
        the outline brackets towards those summary rows; this method only
        changes per-row flags and never touches sheet-level settings.
        """
        if visible_level is not None and not (
                0 <= visible_level <= MAX_OUTLINE_LEVEL):
            raise ValueError(
                "visible_level must be in 0..%d" % MAX_OUTLINE_LEVEL)
        rows = worksheet.get_rows()
        for rowx, row in rows.items():
            row.level = self.row_levels.get(rowx, 0)
            row.hidden = 0
            row.collapse = 0
        if visible_level is None:
            return
        for rowx, level in self.row_levels.items():
            if level <= visible_level or rowx not in rows:
                continue
            rows[rowx].hidden = 1
        for node in self.all_nodes:
            if node.row_level <= visible_level \
                    or node.subtotal_row is None \
                    or node.subtotal_row not in rows:
                continue
            rows[node.subtotal_row].collapse = 1


def _coerce_center(center):
    if isinstance(center, CostCenter):
        return center
    if isinstance(center, dict):
        return CostCenter(**center)
    if isinstance(center, (tuple, list)):
        return CostCenter(*center)
    raise TypeError(
        "cost centre must be a CostCenter, tuple or dict, got %r"
        % type(center))


def _walk(node):
    yield node
    for child in node.children:
        for descendant in _walk(child):
            yield descendant


def _walk_parents(node):
    chain = []
    current = node
    while current is not None:
        chain.append(current)
        current = current.parent
    for ancestor in reversed(chain):
        yield ancestor


def build_outline_plan(centers):
    """Plan an outline from cost-centre subtotal ranges.

    :param centers: :class:`CostCenter` objects, ``(name, first_row,
        last_row[, subtotal_row[, subtotal_value]])`` tuples or equivalent
        dicts.
    :rtype: :class:`OutlinePlan`
    """
    return OutlinePlan(centers)


def _row_number(worksheet, rowx, value_col):
    rows = worksheet.get_rows()
    if rowx not in rows:
        return None
    cells = getattr(rows[rowx], "_Row__cells", {})
    cell = cells.get(value_col)
    if isinstance(cell, NumberCell):
        return cell.number
    return None


def verify_subtotals(plan, worksheet=None, values=None, value_col=0,
                     tolerance=1e-9):
    """Re-compute every kept subtotal from its original detail rows.

    Detail amounts come from the explicit ``values`` mapping (row index to
    amount) or, failing that, from numeric cells of ``worksheet`` at
    ``value_col``.  Ungrouped detail rows inside a centre are included; the
    subtotal rows of nested centres are never counted twice.

    :rtype: list of :class:`SubtotalReport`, one per node in depth-first
        order.  A report fails when a booked value disagrees with the
        recomputed amount or when a detail row has no readable amount.
    """
    if worksheet is None and not values:
        raise ValueError(
            "provide a worksheet or an explicit values mapping")
    values = values or {}

    def amount(rowx):
        if rowx in values:
            return values[rowx]
        if worksheet is not None:
            return _row_number(worksheet, rowx, value_col)
        return None

    reports = []
    for node in plan.all_nodes:
        detail_rows = plan.detail_rows(node)
        missing = []
        total = None
        for rowx in detail_rows:
            value = amount(rowx)
            if value is None:
                missing.append(rowx)
                continue
            total = value if total is None else total + value
        if total is None:
            total = 0.0
        expected = node.center.subtotal_value
        if expected is None and worksheet is not None \
                and node.subtotal_row is not None:
            expected = _row_number(
                worksheet, node.subtotal_row, value_col)
        difference = None if expected is None else total - expected
        ok = not missing and (
            expected is None or abs(difference) <= tolerance)
        reports.append(SubtotalReport(
            node.center, detail_rows, total, expected, difference,
            missing, ok))
    return reports
