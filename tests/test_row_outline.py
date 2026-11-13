# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import struct
import unittest
from io import BytesIO

import xlwt
from xlwt import (
    MAX_OUTLINE_DEPTH,
    MAX_OUTLINE_LEVEL,
    CostCenter,
    build_outline_plan,
    verify_subtotals,
)


def make_chain(depth, last_row=100):
    """Strictly nested centres: depth 1 spans [0, last_row], then shrinks."""
    centers = []
    for index in range(depth):
        first = index
        last = last_row - index
        subtotal = last
        centers.append(CostCenter(
            "L%d" % index, first, last,
            subtotal_row=subtotal, subtotal_value=index))
    return centers


class TestForestBuilding(unittest.TestCase):

    def test_disjoint_centres_become_roots(self):
        plan = build_outline_plan([
            ("A", 0, 9, 9),
            ("B", 10, 19, 19),
        ])
        self.assertEqual([node.name for node in plan.nodes], ["A", "B"])
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(plan.max_depth, 1)
        self.assertFalse(plan.truncated)

    def test_nested_centres_keep_their_hierarchy(self):
        plan = build_outline_plan([
            ("ROOT", 0, 20, 20),
            ("CHILD", 5, 15, 15),
            ("GRAND", 7, 10, 10),
        ])
        self.assertEqual(plan.conflicts, [])
        root = plan.node_for("ROOT")
        self.assertEqual([c.name for c in root.children], ["CHILD"])
        child = plan.node_for("CHILD")
        self.assertEqual(child.parent.name, "ROOT")
        self.assertEqual(
            [node.name for node in child.children], ["GRAND"])
        depths = dict((node.name, node.depth) for node in plan.all_nodes)
        self.assertEqual(depths, {"ROOT": 1, "CHILD": 2, "GRAND": 3})
        levels = dict((node.name, node.row_level) for node in plan.all_nodes)
        self.assertEqual(levels, {"ROOT": 1, "CHILD": 2, "GRAND": 3})

    def test_row_levels_cover_detail_and_keep_summary_at_parent_level(self):
        plan = build_outline_plan([
            ("ROOT", 0, 9, 9),
            ("CHILD", 3, 6, 6),
        ])
        levels = plan.row_levels
        # Root detail (rows 0..8) is at level 1; the child's detail is
        # deeper.  Summary rows close their group at the parent level.
        self.assertEqual(levels[0], 1)
        self.assertEqual(levels[2], 1)
        self.assertEqual(levels[3], 2)
        self.assertEqual(levels[5], 2)
        self.assertEqual(levels[6], 1)   # child subtotal -> root level
        self.assertEqual(levels[7], 1)
        self.assertEqual(levels[9], 0)   # root subtotal -> sheet level
        self.assertFalse(any(level > MAX_OUTLINE_LEVEL
                             for level in levels.values()))

    def test_inputs_accept_tuples_and_dicts(self):
        plan = build_outline_plan([
            {"name": "A", "first_row": 0, "last_row": 4},
            ("B", 5, 9),
        ])
        self.assertEqual(
            sorted(node.name for node in plan.all_nodes), ["A", "B"])
        self.assertIsInstance(plan.all_nodes[0].center, CostCenter)


class TestValidation(unittest.TestCase):

    def test_reversed_range_is_rejected(self):
        with self.assertRaises(ValueError):
            CostCenter("A", 9, 0)

    def test_subtotal_outside_range_is_rejected(self):
        with self.assertRaises(ValueError):
            CostCenter("A", 0, 9, subtotal_row=10)

    def test_duplicate_names_are_rejected(self):
        with self.assertRaises(ValueError):
            build_outline_plan([("A", 0, 4), ("A", 5, 9)])

    def test_bool_is_not_a_row_index(self):
        with self.assertRaises(ValueError):
            CostCenter("A", True, 4)


class TestOverlapConflicts(unittest.TestCase):

    def test_partial_overlap_is_reported_not_silently_dropped(self):
        plan = build_outline_plan([
            ("SALES", 0, 9, 9),
            ("SHARED", 5, 14, 14),
        ])
        self.assertEqual(len(plan.conflicts), 1)
        conflict = plan.conflicts[0]
        self.assertEqual(conflict.kind, "overlap")
        self.assertEqual(
            sorted(center.name for center in conflict.ranges),
            ["SALES", "SHARED"])
        self.assertEqual(conflict.row, 0)
        # Neither side is represented until the caller chooses.
        self.assertEqual(plan.all_nodes, [])
        self.assertEqual(plan.row_levels, {})

    def test_identical_ranges_conflict(self):
        plan = build_outline_plan([
            ("A", 0, 9, 9),
            ("B", 0, 9, 9),
        ])
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(plan.conflicts[0].kind, "overlap")

    def test_shared_boundary_nesting_is_representable(self):
        # Shared start and shared end are valid nested outlines; only
        # crossing ranges (each has rows outside the other) conflict.
        plan = build_outline_plan([
            ("OUTER", 0, 9, 9),
            ("SAME_START", 0, 4, 4),
            ("SAME_END", 6, 9, 8),
        ])
        self.assertEqual(plan.conflicts, [])
        outer = plan.node_for("OUTER")
        self.assertEqual(
            sorted(child.name for child in outer.children),
            ["SAME_END", "SAME_START"])
        levels = plan.row_levels
        self.assertEqual(levels[0], 2)   # SAME_START detail, shared start
        self.assertEqual(levels[4], 1)   # SAME_START summary at outer level
        self.assertEqual(levels[6], 2)   # SAME_END detail
        self.assertEqual(levels[9], 0)   # shared end row is outer summary

    def test_overlap_components_group_transitive_centres(self):
        plan = build_outline_plan([
            ("A", 0, 9),
            ("B", 5, 14),
            ("C", 12, 20),
            ("D", 30, 40),
        ])
        self.assertEqual(len(plan.conflicts), 1)
        self.assertEqual(
            sorted(center.name for center in plan.conflicts[0].ranges),
            ["A", "B", "C"])
        self.assertEqual([node.name for node in plan.nodes], ["D"])

    def test_select_keeps_caller_chosen_side(self):
        plan = build_outline_plan([
            ("SALES", 0, 9, 9),
            ("SHARED", 5, 14, 14),
            ("OTHER", 20, 24, 24),
        ])
        chosen = plan.select(["SALES", "OTHER"])
        self.assertEqual(chosen.conflicts, [])
        self.assertEqual(
            sorted(node.name for node in chosen.all_nodes),
            ["OTHER", "SALES"])
        # The original plan is untouched.
        self.assertEqual(len(plan.conflicts), 1)

    def test_select_unknown_name_raises(self):
        plan = build_outline_plan([("A", 0, 9)])
        with self.assertRaises(KeyError):
            plan.select(["NOPE"])


class TestDepthConflicts(unittest.TestCase):

    def test_exact_limit_is_representable(self):
        plan = build_outline_plan(make_chain(MAX_OUTLINE_DEPTH))
        self.assertEqual(plan.conflicts, [])
        self.assertFalse(plan.truncated)
        deepest = plan.node_for("L7")
        self.assertEqual(deepest.row_level, MAX_OUTLINE_LEVEL)

    def test_chain_deeper_than_biff_is_compressed_and_reported(self):
        plan = build_outline_plan(make_chain(10))
        self.assertTrue(plan.truncated)
        depth_conflicts = [c for c in plan.conflicts if c.kind == "depth"]
        self.assertEqual(len(depth_conflicts), 1)
        conflict = depth_conflicts[0]
        self.assertEqual(
            [center.name for center in conflict.ranges], ["L8", "L9"])
        self.assertEqual(conflict.row, 8)
        self.assertEqual(plan.max_depth, 10)
        # Every emitted level still fits the three-bit BIFF field.
        self.assertTrue(
            all(level <= MAX_OUTLINE_LEVEL
                for level in plan.row_levels.values()))
        flattened = [node for node in plan.all_nodes
                     if node.name in ("L7", "L8", "L9")]
        self.assertEqual(
            dict((node.name, node.row_level) for node in flattened),
            {"L7": 7, "L8": 7, "L9": 7})

    def test_selecting_shallower_subtree_resolves_depth_conflict(self):
        plan = build_outline_plan(make_chain(10))
        chosen = plan.select(["L%d" % index for index in range(8)])
        self.assertEqual(chosen.conflicts, [])
        self.assertFalse(chosen.truncated)
        self.assertEqual(chosen.max_depth, MAX_OUTLINE_DEPTH)

    def test_multiple_overflow_branches_share_one_conflict(self):
        # P1..P8 form a chain exactly at the representable limit; two
        # disjoint branches under P8 overflow together.
        centers = [CostCenter("P%d" % i, i, 100 - i, 100 - i)
                   for i in range(1, MAX_OUTLINE_DEPTH + 1)]
        centers.extend([
            ("B1", 10, 20, 20),
            ("B2", 30, 40, 40),
        ])
        plan = build_outline_plan(centers)
        depth_conflicts = [c for c in plan.conflicts if c.kind == "depth"]
        self.assertEqual(len(depth_conflicts), 1)
        self.assertEqual(
            sorted(center.name for center in depth_conflicts[0].ranges),
            ["B1", "B2"])
        self.assertEqual(plan.node_for("B1").depth, MAX_OUTLINE_DEPTH + 1)
        self.assertEqual(plan.node_for("B1").row_level, MAX_OUTLINE_LEVEL)


class TestApply(unittest.TestCase):

    def _sheet_with_rows(self, count):
        workbook = xlwt.Workbook()
        worksheet = workbook.add_sheet("cc")
        for rowx in range(count):
            worksheet.write(rowx, 0, rowx)
        return workbook, worksheet

    def test_apply_sets_outline_levels_on_rows(self):
        plan = build_outline_plan([
            ("ROOT", 0, 9, 9),
            ("CHILD", 3, 6, 6),
        ])
        _, worksheet = self._sheet_with_rows(10)
        plan.apply(worksheet)
        self.assertEqual(worksheet.row(0).level, 1)
        self.assertEqual(worksheet.row(3).level, 2)
        self.assertEqual(worksheet.row(6).level, 1)
        self.assertEqual(worksheet.row(9).level, 0)
        self.assertEqual(worksheet.row(0).hidden, 0)

    def test_apply_with_visible_level_hides_detail_and_collapses(self):
        plan = build_outline_plan([
            ("ROOT", 0, 9, 9),
            ("CHILD", 3, 6, 6),
        ])
        _, worksheet = self._sheet_with_rows(10)
        plan.apply(worksheet, visible_level=1)
        # Level-2 detail is hidden; both subtotal rows stay visible so the
        # collapsed centre can still be reconciled.
        self.assertEqual(worksheet.row(3).hidden, 1)
        self.assertEqual(worksheet.row(6).hidden, 0)
        self.assertEqual(worksheet.row(9).hidden, 0)
        self.assertEqual(worksheet.row(6).collapse, 1)

    def test_apply_then_save_produces_biff_file(self):
        plan = build_outline_plan(make_chain(10))
        workbook, worksheet = self._sheet_with_rows(101)
        plan.apply(worksheet)
        buffer = BytesIO()
        workbook.save(buffer)
        self.assertGreater(len(buffer.getvalue()), 0)

    def test_biff_row_records_carry_planned_outline_bits(self):
        plan = build_outline_plan([
            ("ROOT", 0, 9, 9),
            ("CHILD", 3, 6, 6),
        ])
        workbook, worksheet = self._sheet_with_rows(10)
        plan.apply(worksheet, visible_level=1)
        row_records, guts_row_levels = self._parse_outline(
            worksheet.get_biff_data())
        for rowx, level in plan.row_levels.items():
            self.assertEqual(row_records[rowx]["level"], level)
        self.assertEqual(row_records[3]["hidden"], 1)
        self.assertEqual(row_records[6]["collapse"], 1)
        self.assertEqual(row_records[6]["hidden"], 0)
        # GUTS exposes the deepest used outline step plus the sheet level.
        self.assertEqual(guts_row_levels, 3)

    def test_biff_levels_never_exceed_three_bit_field(self):
        plan = build_outline_plan(make_chain(10))
        _, worksheet = self._sheet_with_rows(101)
        plan.apply(worksheet)
        row_records, _ = self._parse_outline(worksheet.get_biff_data())
        self.assertTrue(
            all(record["level"] <= MAX_OUTLINE_LEVEL
                for record in row_records.values()))

    @staticmethod
    def _parse_outline(stream):
        row_records = {}
        guts_row_levels = None
        offset = 0
        while offset < len(stream):
            record_id, size = struct.unpack_from("<HH", stream, offset)
            offset += 4
            data = stream[offset:offset + size]
            offset += size
            if record_id == 0x0208:  # ROW
                row_index = struct.unpack_from("<H", data, 0)[0]
                options = struct.unpack_from("<I", data, 12)[0]
                row_records[row_index] = {
                    "level": options & 0x07,
                    "collapse": (options >> 4) & 0x01,
                    "hidden": (options >> 5) & 0x01,
                }
            elif record_id == 0x0080:  # GUTS
                guts_row_levels = struct.unpack_from("<H", data, 4)[0]
        return row_records, guts_row_levels

    def test_invalid_visible_level_is_rejected(self):
        plan = build_outline_plan([("A", 0, 4)])
        _, worksheet = self._sheet_with_rows(5)
        with self.assertRaises(ValueError):
            plan.apply(worksheet, visible_level=8)


class TestVerifySubtotals(unittest.TestCase):

    def setUp(self):
        # ROOT [0..9] subtotal row 9; CHILD [3..6] subtotal row 6.
        self.plan = build_outline_plan([
            CostCenter("ROOT", 0, 9, 9, subtotal_value=100.0),
            CostCenter("CHILD", 3, 6, 6, subtotal_value=40.0),
        ])
        # Raw detail amounts.  Rows 6 and 9 are subtotals and must not be
        # counted as detail for either centre.
        self.values = {
            0: 10.0, 1: 10.0, 2: 10.0,
            3: 10.0, 4: 15.0, 5: 15.0, 6: 40.0,
            7: 20.0, 8: 10.0, 9: 100.0,
        }

    def test_detail_rows_exclude_nested_subtotals_keep_ungrouped(self):
        child = self.plan.node_for("CHILD")
        root = self.plan.node_for("ROOT")
        self.assertEqual(self.plan.detail_rows(child), [3, 4, 5])
        self.assertEqual(
            self.plan.detail_rows(root),
            [0, 1, 2, 3, 4, 5, 7, 8])

    def test_parent_subtotal_equals_direct_children_plus_ungrouped(self):
        root = self.plan.node_for("ROOT")
        child = self.plan.node_for("CHILD")
        self.assertEqual(
            [node.name for node in self.plan.direct_children(root)],
            ["CHILD"])
        self.assertEqual(
            self.plan.ungrouped_detail_rows(root), [0, 1, 2, 7, 8])
        child_subtotal = self.values[child.subtotal_row]
        ungrouped_total = sum(
            self.values[rowx]
            for rowx in self.plan.ungrouped_detail_rows(root))
        self.assertEqual(
            child_subtotal + ungrouped_total,
            self.values[root.subtotal_row])
        # The same identity must hold when the centre is collapsed.
        workbook = xlwt.Workbook()
        worksheet = workbook.add_sheet("cc")
        for rowx in range(10):
            worksheet.write(rowx, 0, rowx)
        self.plan.apply(worksheet, visible_level=0)
        reports = verify_subtotals(self.plan, values=self.values)
        root_report = [r for r in reports
                       if r.center.name == "ROOT"][0]
        self.assertTrue(root_report.ok)
        self.assertEqual(
            root_report.recomputed, self.values[root.subtotal_row])

    def test_recompute_from_original_detail_rows(self):
        reports = verify_subtotals(self.plan, values=self.values)
        by_name = dict(
            (report.center.name, report) for report in reports)
        child = by_name["CHILD"]
        self.assertEqual(child.recomputed, 40.0)
        self.assertEqual(child.difference, 0.0)
        self.assertTrue(child.ok)
        root = by_name["ROOT"]
        # Ungrouped rows 0,1,2,7,8 plus child detail 3,4,5; subtotal rows
        # excluded, so nothing is double counted.
        self.assertEqual(root.recomputed, 100.0)
        self.assertTrue(root.ok)

    def test_mismatched_booked_subtotal_fails_report(self):
        self.values[8] = 25.0
        reports = verify_subtotals(self.plan, values=self.values)
        root = [r for r in reports if r.center.name == "ROOT"][0]
        self.assertFalse(root.ok)
        self.assertEqual(root.difference, 15.0)

    def test_missing_detail_amount_fails_report(self):
        del self.values[4]
        reports = verify_subtotals(self.plan, values=self.values)
        child = [r for r in reports if r.center.name == "CHILD"][0]
        self.assertFalse(child.ok)
        self.assertEqual(child.missing_rows, [4])

    def test_reads_amounts_from_worksheet_cells(self):
        workbook = xlwt.Workbook()
        worksheet = workbook.add_sheet("cc")
        for rowx, amount in self.values.items():
            worksheet.write(rowx, 0, amount)
        reports = verify_subtotals(self.plan, worksheet, value_col=0)
        self.assertTrue(all(report.ok for report in reports))


if __name__ == "__main__":
    unittest.main()
