#!/usr/bin/env python
"""Accept revisioned inventory rows, flush in batches, save one workbook.

Rows are keyed by SKU.  The newest accepted revision wins, but each SKU
keeps the position where it was first seen.  A row carrying the same
revision but different values is reported as a conflict.
"""

from __future__ import print_function

import xlwt


def main():
    book = xlwt.Workbook()
    sheet = book.add_sheet('Stock')
    for col, title in enumerate(('SKU', 'Revision', 'Name', 'Qty')):
        sheet.write(0, col, title)

    stream = sheet.get_revision_stream(key='sku', revision='revision',
                                       start_row=1)

    # First batch.
    stream.write({'sku': 'A-100', 'revision': 1, 'name': 'widget', 'qty': 4})
    stream.write({'sku': 'B-200', 'revision': 2, 'name': 'bolt', 'qty': 9})
    sheet.flush_row_data()

    # A higher revision for an already-flushed SKU, plus a conflict.
    stream.write({'sku': 'A-100', 'revision': 3, 'name': 'widget v3',
                  'qty': 7})
    stream.write({'sku': 'B-200', 'revision': 2, 'name': 'bolt', 'qty': 11})

    # Even a lower revision for a new key would be accepted at its position,
    # but a lower revision for an existing key is ignored.
    stream.write({'sku': 'C-300', 'revision': 1, 'name': 'nut', 'qty': 20})

    book.save('revision_rows.xls')
    print('rows:', stream.row_count)
    print('conflicts:', [c.key for c in sheet.revision_conflicts])


if __name__ == '__main__':
    main()
