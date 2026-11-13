# -*- coding: utf-8 -*-
"""
TreeHeader
==========

Turn a tree-shaped field list into the merged cell ranges of a multi-level
spreadsheet header.

A field tree is a nested mapping ``{label: subtree}``; an empty mapping (or
an empty sequence) marks a leaf, and each leaf becomes one detail column.
An ordered sequence of ``(label, subtree)`` pairs may be used instead of a
mapping, which makes the column order explicit (mapping iteration order is
only guaranteed on Python 3.7+).

Only header cells that share the *same parent*, occupy *consecutive* leaf
columns and carry the *same label* are merged horizontally.  Two branches
that happen to use an identical label (for example two unrelated "Other"
nodes) are never merged, because their parent paths differ.  Branches of
uneven depth are extended downwards with vertical merges, so every detail
column still keeps exactly one path back to the root.
"""


class HeaderConflict(Exception):
    """Raised when a generated header range clashes with a merge range that
    was already present on the sheet (a reserved region of an old template).
    """


def _children(node):
    if isinstance(node, dict):
        return list(node.items())
    return [(label, subtree) for label, subtree in node]


def flatten_field_tree(tree):
    """Return one tuple of labels per leaf column, in column order.

    Each returned tuple describes the unique field path from the root to a
    single detail column.
    """
    if tree is None or not _children(tree):
        raise ValueError("the field tree contains no leaf columns")

    paths = []

    def walk(node, prefix):
        if node is None:
            paths.append(tuple(prefix))
            return
        children = _children(node)
        if not children:
            paths.append(tuple(prefix))
            return
        for label, subtree in children:
            walk(subtree, prefix + (label,))

    walk(tree, ())
    return paths


def plan_tree_header(tree, origin_row=0, origin_col=0):
    """Compute every label cell and merged range of a multi-level header.

    :param tree: the field tree (nested mapping or ordered ``(label, child)``
        pairs).
    :param origin_row: zero-based row of the first header row.
    :param origin_col: zero-based column of the first detail column.
    :returns: a mapping with the keys ``depth``, ``width``, ``paths``,
        ``labels`` and ``merges``.  ``labels`` lists ``(row, col, label)``
        cells and ``merges`` lists ``(r1, r2, c1, c2, label)`` ranges; a
        ``None`` label marks a vertical tail that only carries the style.
    """
    paths = flatten_field_tree(tree)
    depth = max(len(path) for path in paths)
    width = len(paths)

    # grid[level][col] holds the label at that level, or None when the
    # column already ended at a shallower level (vertical-merge territory).
    grid = [
        [path[level] if level < len(path) else None for path in paths]
        for level in range(depth)
    ]

    merges = []
    horizontally_merged = set()

    # Horizontal runs: same parent path, consecutive columns, same label.
    for level in range(depth):
        col = 0
        while col < width:
            if grid[level][col] is None:
                col += 1
                continue
            start = col
            label = grid[level][col]
            parent = paths[col][:level]
            col += 1
            while (col < width
                   and grid[level][col] == label
                   and paths[col][:level] == parent):
                col += 1
            row = origin_row + level
            c1 = origin_col + start
            c2 = origin_col + col - 1
            if col - start > 1:
                merges.append((row, row, c1, c2, label))
                for covered in range(start, col):
                    horizontally_merged.add((level, covered))

    # Shallow branches: carry the terminal label straight down.  When the
    # terminal cell already belongs to a horizontal merge, only the blank
    # tail underneath it is merged so that ranges never intersect.
    for col, path in enumerate(paths):
        if len(path) >= depth:
            continue
        top_level = len(path) - 1
        r1 = origin_row + top_level
        r2 = origin_row + depth - 1
        c = origin_col + col
        if (top_level, col) in horizontally_merged:
            if r2 > r1:
                merges.append((r1 + 1, r2, c, c, None))
        else:
            merges.append((r1, r2, c, c, path[-1]))

    occupied = set()
    top_left_labels = {}
    for r1, r2, c1, c2, label in merges:
        top_left_labels[(r1, c1)] = label
        for row in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                occupied.add((row, c))

    labels = []
    for level in range(depth):
        for col in range(width):
            label = grid[level][col]
            if label is None:
                continue
            coord = (origin_row + level, origin_col + col)
            if coord in occupied:
                if coord in top_left_labels and top_left_labels[coord] is not None:
                    labels.append((coord[0], coord[1], top_left_labels[coord]))
            else:
                labels.append((coord[0], coord[1], label))

    return {
        'depth': depth,
        'width': width,
        'paths': paths,
        'labels': labels,
        'merges': merges,
    }


def _intersects(first, second):
    fr1, fr2, fc1, fc2 = first[:4]
    sr1, sr2, sc1, sc2 = second[:4]
    return not (fr2 < sr1 or sr2 < fr1 or fc2 < sc1 or sc2 < fc1)


def _same_box(first, second):
    fr1, fr2, fc1, fc2 = first[:4]
    sr1, sr2, sc1, sc2 = second[:4]
    return fr1 == sr1 and fr2 == sr2 and fc1 == sc1 and fc2 == sc2


def format_range(rng):
    """Return a human-readable description of a row/col range tuple."""
    r1, r2, c1, c2 = rng[:4]
    return "rows %d..%d, cols %d..%d" % (r1, r2, c1, c2)


def check_reserved_ranges(plan, reserved_ranges):
    """Compare generated header regions with ranges already on the sheet.

    :returns: ``(conflicts, reusable)``.  Generated regions are the merge
        ranges plus every standalone label cell (expressed as a one-cell
        range).  A region is only ``reusable`` when its geometry is identical
        to a reserved range, so the template merge can be kept untouched.
        Every other intersection -- partial overlap, a reserved range
        wrapping a generated one or a reserved range nested inside one -- is
        returned in ``conflicts`` as a ``(generated, reserved)`` pair,
        because silently choosing either side would produce a half-written
        or wrongly merged header.
    """
    merge_toplefts = {(r1, c1) for r1, r2, c1, c2, label in plan['merges']}
    regions = list(plan['merges'])
    for r, c, label in plan['labels']:
        if (r, c) not in merge_toplefts:
            regions.append((r, r, c, c, label))

    conflicts = []
    reusable = []
    for merged in regions:
        for reserved in reserved_ranges:
            if not _intersects(merged, reserved):
                continue
            if _same_box(merged, reserved):
                reusable.append((merged, reserved))
            else:
                conflicts.append((merged, reserved))
            break
    return conflicts, reusable


def describe_conflicts(conflicts):
    """Build the explanatory message used by :class:`HeaderConflict`."""
    lines = ["refusing to write a partial header; "
             "generated ranges clash with reserved merge ranges:"]
    for merged, reserved in conflicts:
        label = merged[4]
        lines.append(
            "  - new %s %r vs reserved %s"
            % (format_range(merged), label, format_range(reserved)))
    return "\n".join(lines)
