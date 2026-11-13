#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Plan a collapsible cost-centre outline for a finance worksheet.

The input gives detail-row ranges plus a subtotal row for every cost centre.
Two real-world cases do not fit a single Excel outline:

* a centre managed by two departments produces ranges that cross;
* an organisation can be nested deeper than the eight BIFF outline levels.

The planner keeps everything that is representable, lists the rest as
conflicts, and lets the caller choose which group survives. Subtotals are
then re-computed from the original detail rows so collapsed centres can be
audited.
"""
from __future__ import print_function

import xlwt
from xlwt import CostCenter, build_outline_plan, verify_subtotals

# name, first detail row, last row, subtotal row, booked subtotal amount
centers = [
    CostCenter("全厂", 0, 23, 23, 1000.0),
    CostCenter("生产部", 0, 14, 14, 600.0),
    CostCenter("一车间", 0, 7, 7, 300.0),
    CostCenter("二车间", 8, 14, 14, 300.0),
    # Cross-managed centre: its range crosses 生产部, so the two cannot be
    # shown as one nested outline.
    CostCenter("质量部", 5, 19, 19, 500.0),
]

plan = build_outline_plan(centers)
for conflict in plan.conflicts:
    print("conflict (%s): %s"
          % (conflict.kind, ", ".join(c.name for c in conflict.ranges)))

# The caller decides: keep the production hierarchy, drop the crossing one.
plan = plan.select(["全厂", "生产部", "一车间", "二车间"])

# Raw detail amounts. Subtotal rows (7, 14, 23) hold the booked totals and are
# never counted twice by verify_subtotals().
values = {}
for rowx in range(0, 7):
    values[rowx] = 300.0 / 7.0
for rowx in range(8, 14):
    values[rowx] = 300.0 / 6.0
for rowx in range(15, 23):
    values[rowx] = 400.0 / 8.0
values[7] = 300.0
values[14] = 600.0
values[23] = 1000.0

workbook = xlwt.Workbook()
worksheet = workbook.add_sheet("成本中心")
# Subtotal rows sit below their detail: draw outline brackets towards them.
worksheet.outline_below = 1
for rowx in range(24):
    worksheet.write(rowx, 0, values[rowx])
    worksheet.write(rowx, 1, "row %d" % rowx)

# Outline level 1 visible: every centre's detail collapses onto its subtotal.
plan.apply(worksheet, visible_level=1)

for report in verify_subtotals(plan, worksheet, value_col=0):
    print("%s recomputed=%r expected=%r ok=%s"
          % (report.center.name, report.recomputed,
             report.expected, report.ok))

workbook.save("cost_center_outline.xls")
print("saved cost_center_outline.xls")
