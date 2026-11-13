# -*- coding: utf-8 -*-
"""
Per-column precision policy example.

Ordinary metering readings keep their numeric type and can still be summed
directly.  Readings with more significant digits than a binary64 double can
hold are written as reversible text, and the sheet reports which cells can no
longer take part in a sum.
"""
from __future__ import print_function

from decimal import Decimal

import xlwt

wb = xlwt.Workbook()
ws = wb.add_sheet('metering')

# Long, high-significance readings: reject nothing, keep them as text instead.
# A binary64 double only carries ~16 significant digits, so a tolerance of
# 1e-18 forces these 24-digit readings onto the reversible-text path.
ws.set_precision_policy(0, tolerance=1e-18, on_overflow=xlwt.TEXT)
# Ordinary readings stay numeric; fail loudly if one would lose too much.
ws.set_precision_policy(1, tolerance=1e-9, on_overflow=xlwt.REJECT)

rows = [
    (Decimal('1.234567890123456789012345'), Decimal('12.50')),
    (Decimal('2.718281828459045235360287'), Decimal('7.50')),
    (Decimal('0.25'), Decimal('3.00')),
]
for rowx, (long_reading, ordinary) in enumerate(rows):
    ws.write(rowx, 0, long_reading)
    ws.write(rowx, 1, ordinary)

wb.save('precision.xls')

print('cells downgraded to text (not directly summable):',
      ws.non_numeric_cells)
print('finance sum of the ordinary numeric column:',
      ws.sum_numeric(1))
