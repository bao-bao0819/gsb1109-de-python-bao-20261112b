# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import unittest

import xlwt
from xlwt.Cell import StrCell
from xlwt.TreeHeader import (
    HeaderConflict,
    check_reserved_ranges,
    describe_conflicts,
    flatten_field_tree,
    plan_tree_header,
)

from .utils import in_tst_output_dir


# Two unrelated branches both call a node "其他"; they must stay apart.
FIELD_TREE = (
    ('基本信息', (
        ('姓名', {}),
        ('其他', (
            ('备注', {}),
        )),
    )),
    ('联系方式', (
        ('电话', {}),
        ('其他', (
            ('邮箱', {}),
            ('微信', {}),
        )),
    )),
    # Shallow branch: ends two levels before the deepest path.
    ('汇总', {}),
)


def rebuild_paths(plan):
    """Walk every detail column upwards, mimicking a reader of the sheet."""
    top_left = {}
    for r1, r2, c1, c2, label in plan['merges']:
        for r in range(r1, r2 + 1):
            for c in range(c1, c2 + 1):
                top_left[(r, c)] = (r1, label)

    plain_labels = {(r, c): label for r, c, label in plan['labels']}
    rebuilt = []
    for col in range(plan['width']):
        path = []
        for level in range(plan['depth']):
            r = level
            if (r, col) in top_left:
                anchor_row, label = top_left[(r, col)]
                # A merged cell contributes its label once, at its top row;
                # blank vertical tails contribute nothing.
                if r == anchor_row and label is not None:
                    path.append(label)
            else:
                path.append(plain_labels[(r, col)])
        rebuilt.append(tuple(path))
    return rebuilt


class TreeHeaderPlanTests(unittest.TestCase):
    def test_flatten_paths_are_unique_and_in_order(self):
        paths = flatten_field_tree(FIELD_TREE)
        self.assertEqual(paths, [
            ('基本信息', '姓名'),
            ('基本信息', '其他', '备注'),
            ('联系方式', '电话'),
            ('联系方式', '其他', '邮箱'),
            ('联系方式', '其他', '微信'),
            ('汇总',),
        ])
        self.assertEqual(len(set(paths)), len(paths))

    def test_every_detail_column_recovers_its_unique_path(self):
        plan = plan_tree_header(FIELD_TREE)
        self.assertEqual(plan['depth'], 3)
        self.assertEqual(plan['width'], 6)
        self.assertEqual(rebuild_paths(plan), plan['paths'])

    def test_same_label_in_different_branches_is_not_merged(self):
        plan = plan_tree_header(FIELD_TREE)
        # The only "其他" merge is the two-column one under 联系方式;
        # the single-column "其他" under 基本信息 stays its own cell.
        other_merges = [m for m in plan['merges'] if m[4] == '其他']
        self.assertEqual(other_merges, [(1, 1, 3, 4, '其他')])
        other_cells = sorted((r, c) for r, c, label in plan['labels']
                             if label == '其他')
        self.assertEqual(other_cells, [(1, 1), (1, 3)])

    def test_merge_requires_same_parent_adjacency_and_label(self):
        tree = (
            ('组', (
                ('甲', {}),
                ('乙', {}),
                ('甲', {}),  # same label, but not adjacent -> no merge
            )),
        )
        plan = plan_tree_header(tree)
        self.assertNotIn((1, 1, 0, 2, '甲'), plan['merges'])
        self.assertEqual(plan['paths'], [
            ('组', '甲'), ('组', '乙'), ('组', '甲')])

    def test_uneven_depth_gets_vertical_merge(self):
        plan = plan_tree_header(FIELD_TREE)
        # "汇总" (last column) spans all three header rows.
        self.assertIn((0, 2, 5, 5, '汇总'), plan['merges'])
        # Shallow leaves "姓名"/"电话" extend down without a blank-tail
        # split because their terminal cell is not horizontally merged.
        self.assertIn((1, 2, 0, 0, '姓名'), plan['merges'])
        self.assertIn((1, 2, 2, 2, '电话'), plan['merges'])

    def test_blank_tail_when_terminal_cell_is_horizontally_merged(self):
        tree = (
            ('组', (
                ('明细', {}),
                ('明细', {}),
            )),
            ('组', (
                ('深', (
                    ('叶子', {}),
                    ('叶子', {}),
                )),
            )),
        )
        plan = plan_tree_header(tree)
        # Horizontal merge of the two shallow "明细" cells at level 1,
        # then one blank tail per column, never intersecting the merge.
        self.assertIn((1, 1, 0, 1, '明细'), plan['merges'])
        self.assertIn((2, 2, 0, 0, None), plan['merges'])
        self.assertIn((2, 2, 1, 1, None), plan['merges'])
        self.assertEqual(rebuild_paths(plan), plan['paths'])

    def test_origin_offsets_are_applied(self):
        plan = plan_tree_header(FIELD_TREE, origin_row=2, origin_col=3)
        self.assertEqual(plan['merges'][0], (2, 2, 3, 4, '基本信息'))
        self.assertEqual(plan['labels'][0], (2, 3, '基本信息'))

    def test_empty_tree_is_rejected(self):
        self.assertRaises(ValueError, plan_tree_header, {})


class ReservedRangeTests(unittest.TestCase):
    def setUp(self):
        self.plan = plan_tree_header(FIELD_TREE)

    def test_partial_overlap_is_reported_with_both_ranges(self):
        reserved = [(0, 0, 1, 3)]
        conflicts, reusable = check_reserved_ranges(self.plan, reserved)
        self.assertEqual(reusable, [])
        merged_ranges, reserved_ranges = zip(*conflicts)
        self.assertIn((0, 0, 2, 4, '联系方式'), merged_ranges)
        self.assertIn((0, 0, 1, 3), reserved_ranges)
        message = str(HeaderConflict(describe_conflicts(conflicts)))
        self.assertIn('rows 0..0, cols 2..4', message)
        self.assertIn('rows 0..0, cols 1..3', message)

    def test_matching_reserved_range_is_reused(self):
        # Exactly the same geometry as the vertical "汇总" merge.
        reserved = [(0, 2, 5, 5)]
        conflicts, reusable = check_reserved_ranges(self.plan, reserved)
        self.assertEqual(conflicts, [])
        self.assertEqual(len(reusable), 1)
        self.assertEqual(reusable[0][0], (0, 2, 5, 5, '汇总'))
        self.assertEqual(reusable[0][1], (0, 2, 5, 5))

    def test_reserved_range_wrapping_a_generated_one_is_a_conflict(self):
        # Reserved extends beyond the generated merge: keeping it would
        # merge extra cells, rewriting it would clobber the template, so
        # both ranges must be reported instead of producing a half header.
        reserved = [(0, 0, 0, 2)]
        conflicts, _ = check_reserved_ranges(self.plan, reserved)
        self.assertTrue(conflicts)

    def test_standalone_label_cell_is_checked_against_reserved_ranges(self):
        # The single-column "其他" at (row 1, col 1) is not a merge range;
        # a reserved range swallowing it must still be reported.
        reserved = [(1, 1, 1, 2)]
        conflicts, reusable = check_reserved_ranges(self.plan, reserved)
        self.assertEqual(reusable, [])
        generated = [pair[0] for pair in conflicts]
        self.assertIn((1, 1, 1, 1, '其他'), generated)

    def test_single_cell_reserved_range_matching_a_label_is_reusable(self):
        reserved = [(1, 1, 1, 1)]
        conflicts, reusable = check_reserved_ranges(self.plan, reserved)
        self.assertEqual(conflicts, [])
        self.assertEqual(reusable[0][0], (1, 1, 1, 1, '其他'))


def cell_text(book, sheet, rowx, colx):
    rows = sheet._Worksheet__rows
    cell = rows[rowx]._Row__cells[colx]
    if not isinstance(cell, StrCell):
        return None
    sst = book._Workbook__sst._str_indexes
    for value, index in sst.items():
        if index == cell.sst_idx:
            return value
    return None


class WorksheetWriteTests(unittest.TestCase):
    def test_write_tree_header_cells_merges_and_save(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('header')
        plan = sheet.write_tree_header(FIELD_TREE, row=1, col=1)

        expected_ranges = {
            (r1, r2, c1, c2)
            for r1, r2, c1, c2, label in plan['merges']
        }
        self.assertEqual(set(sheet.merged_ranges), expected_ranges)

        self.assertEqual(cell_text(book, sheet, 1, 1), '基本信息')
        self.assertEqual(cell_text(book, sheet, 2, 2), '其他')
        self.assertEqual(cell_text(book, sheet, 2, 4), '其他')
        self.assertEqual(cell_text(book, sheet, 1, 6), '汇总')
        self.assertEqual(cell_text(book, sheet, 3, 2), '备注')
        self.assertEqual(cell_text(book, sheet, 3, 4), '邮箱')

        book.save(in_tst_output_dir('tree_header.xls'))

    def test_partial_overlap_raises_before_writing_anything(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('header')
        sheet.write_merge(1, 1, 2, 4, '旧保留区')
        before = list(sheet.merged_ranges)
        self.assertRaises(HeaderConflict, sheet.write_tree_header,
                          FIELD_TREE, 1, 1)
        self.assertEqual(sheet.merged_ranges, before)

    def test_matching_reserved_range_is_kept_from_template(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('header')
        sheet.write_merge(1, 3, 6, 6, '旧汇总')
        sheet.write_tree_header(FIELD_TREE, row=1, col=1)
        # Reserved range appears once, not duplicated.
        self.assertEqual(sheet.merged_ranges.count((1, 3, 6, 6)), 1)
        self.assertEqual(cell_text(book, sheet, 1, 6), '旧汇总')
        book.save(in_tst_output_dir('tree_header_reuse.xls'))

    def test_overwrite_reserved_forces_full_header(self):
        book = xlwt.Workbook()
        sheet = book.add_sheet('header', cell_overwrite_ok=True)
        sheet.write_merge(1, 1, 2, 4, '旧保留区')
        plan = sheet.write_tree_header(
            FIELD_TREE, row=1, col=1, overwrite_reserved=True)
        self.assertEqual(
            len(sheet.merged_ranges), len(plan['merges']) + 1)
        book.save(in_tst_output_dir('tree_header_forced.xls'))


if __name__ == '__main__':
    unittest.main()
