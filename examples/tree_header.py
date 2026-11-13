#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Multi-level header generated from a tree-shaped field list.

Run me and open tree_header.xls.  The two branches that both contain an
"其他" node stay in separate columns because their parent paths differ.
"""
from __future__ import unicode_literals

from xlwt import *

field_tree = (
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
    ('汇总', {}),  # shallow branch, extended vertically to the deepest row
)

header_style = easyxf(
    'font: bold on, colour white;'
    'pattern: pattern solid, fore_colour gray50;'
    'align: vert center, horz center;'
    'borders: left thin, right thin, top thin, bottom thin;')
leaf_style = easyxf(
    'font: bold on;'
    'align: vert center, horz center;'
    'borders: left thin, right thin, top thin, bottom thin;')

wb = Workbook()
ws = wb.add_sheet('字段表头')
ws.write_tree_header(field_tree, header_style=header_style,
                     leaf_style=leaf_style)

# An old template may already reserve a merge range.  When a generated
# range has exactly the same geometry, the template merge is reused.
ws2 = wb.add_sheet('保留区')
ws2.write_merge(0, 2, 5, 5, '汇总', header_style)
ws2.write_tree_header(field_tree, header_style=header_style,
                      leaf_style=leaf_style)

wb.save('tree_header.xls')
print('saved tree_header.xls with %d merged ranges' % len(ws.merged_ranges))
