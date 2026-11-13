# -*- coding: utf-8 -*-
"""Grouped, paginated handover sheets (交接单) built on top of xlwt.

:func:`write_handover` consumes a *single-pass* stream of detail rows
ordered by cargo owner (货主) and shipment order (发运单), and renders it
as one or more worksheets:

* every page repeats the report title and the column captions;
* each shipment order is kept on a single page whenever it fits;
* an order larger than one page continues on the next page(s) -- the
  page break is marked with a 本页小计 (page subtotal) row while the last
  page of the order carries the 整单合计 (whole-order total), so the
  receiver can tell the two apart;
* each cargo owner is closed with a 货主合计 (owner total) row;
* an order whose details plus subtotal exactly fill the last page never
  produces a blank continuation sheet, and every physical piece is
  summed exactly once across all pages.

Only one shipment order is buffered at a time, so memory use stays
bounded regardless of the stream length.
"""
from __future__ import unicode_literals

from .Style import easyxf

__all__ = [
    'HandoverColumn',
    'write_handover',
    'PAGE_SUBTOTAL_LABEL',
    'ORDER_TOTAL_LABEL',
    'OWNER_TOTAL_LABEL',
]

#: Label suffix of a row that subtotals one page of a continued order.
PAGE_SUBTOTAL_LABEL = '本页小计'
#: Label suffix of the row that totals a whole shipment order.
ORDER_TOTAL_LABEL = '整单合计'
#: Label suffix of the row that totals a whole cargo owner.
OWNER_TOTAL_LABEL = '货主合计'


class HandoverColumn(object):
    """Definition of one column of a handover sheet.

    :param key: mapping key (or attribute name) used to pull the value
        out of a detail row.
    :param title: caption shown in the header row of every page.
    :param width: column width, in characters.
    :param total: when True, the column holds a numeric quantity (such
        as 件数) that is summed in subtotal and total rows.
    """

    def __init__(self, key, title, width=12, total=False):
        self.key = key
        self.title = title
        self.width = width
        self.total = total


#: Default column layout, used when the caller does not supply one.
DEFAULT_COLUMNS = (
    HandoverColumn('order', '发运单号', width=18),
    HandoverColumn('item', '货物名称', width=24),
    HandoverColumn('pieces', '件数', width=8, total=True),
    HandoverColumn('weight', '重量', width=10, total=True),
)


def _field(row, key):
    """Pull ``key`` out of a detail row (mapping or plain object)."""
    if hasattr(row, 'keys'):
        return row[key]
    return getattr(row, key)


def _iter_order_groups(rows, owner_key, order_key):
    """Yield ``(owner, order, details)`` groups from a single-pass stream.

    The stream must be ordered by (owner, order); only the current order
    is buffered.  A key that reappears after its group was closed raises
    :class:`ValueError`, because such a stream cannot be rendered
    correctly in a single pass.
    """
    seen_orders = set()
    seen_owners = set()
    current_key = None
    current = []
    for row in rows:
        key = (_field(row, owner_key), _field(row, order_key))
        if key != current_key:
            if current_key is not None:
                seen_orders.add(current_key)
                if key[0] != current_key[0]:
                    seen_owners.add(current_key[0])
                yield current_key[0], current_key[1], current
            if key in seen_orders:
                raise ValueError(
                    'detail stream is not grouped by shipment order: '
                    '%r reappeared' % (key,))
            if key[0] in seen_owners:
                raise ValueError(
                    'detail stream is not grouped by cargo owner: '
                    '%r reappeared' % (key[0],))
            current_key = key
            current = []
        current.append(row)
    if current_key is not None:
        yield current_key[0], current_key[1], current


class _HandoverWriter(object):
    """Stateful renderer behind :func:`write_handover`."""

    header_height = 2  # title row + column caption row

    def __init__(self, workbook, columns, row_limit, sheet_name, title):
        self.workbook = workbook
        self.columns = list(columns)
        self.sheet_name = sheet_name
        self.title = title
        if row_limit < self.header_height + 2:
            raise ValueError(
                'row_limit must leave room for the header plus at least '
                'one detail row and one subtotal row, got %r' % (row_limit,))
        self.capacity = row_limit - self.header_height
        self.sheets = []
        self._sheet = None
        self._used = 0
        self._title_style = easyxf(
            'font: bold on, height 280; align: horiz center, vert center;')
        self._header_style = easyxf(
            'font: bold on; align: horiz center, vert center; '
            'borders: left thin, right thin, top thin, bottom thin;')
        self._detail_style = easyxf(
            'borders: left thin, right thin, top thin, bottom thin;')
        self._total_style = easyxf(
            'font: bold on; '
            'borders: left thin, right thin, top thin, bottom thin; '
            'pattern: pattern solid, fore_colour gray25;')

    @property
    def _space(self):
        # Body rows still available on the current page.  When no page is
        # open yet the whole capacity is available, which keeps page
        # creation lazy: a fresh sheet is only started when something
        # actually has to be written on it.
        return self.capacity - self._used

    def _ensure_page(self):
        if self._sheet is None:
            self._open_page()

    def _open_page(self):
        page = len(self.sheets) + 1
        sheet = self.workbook.add_sheet('%s-%d' % (self.sheet_name, page))
        for i, column in enumerate(self.columns):
            sheet.col(i).width = 256 * max(int(column.width), 1)
        last_col = len(self.columns) - 1
        caption = '%s（第%d页）' % (self.title, page)
        if last_col:
            sheet.write_merge(0, 0, 0, last_col, caption, self._title_style)
        else:
            sheet.write(0, 0, caption, self._title_style)
        for i, column in enumerate(self.columns):
            sheet.write(1, i, column.title, self._header_style)
        self._sheet = sheet
        self._used = 0
        self.sheets.append(sheet)

    def _close_page(self):
        # Lazy pagination: the next write reopens a page on demand, so a
        # group that exactly fills a page never spawns a blank sheet.
        self._sheet = None
        self._used = 0

    def _write_row(self, values, style):
        self._ensure_page()
        rowx = self.header_height + self._used
        for colx, value in enumerate(values):
            self._sheet.write(rowx, colx, value, style)
        self._used += 1

    def _write_detail(self, detail):
        self._write_row([_field(detail, column.key) for column in self.columns],
                        self._detail_style)

    def _write_total_row(self, label, sums):
        values = []
        for i, column in enumerate(self.columns):
            if i == 0:
                values.append(label)
            elif column.total:
                values.append(sums.get(i, 0))
            else:
                values.append('')
        self._write_row(values, self._total_style)

    def _sum_columns(self, details):
        return dict(
            (i, sum((_field(detail, column.key) for detail in details), 0))
            for i, column in enumerate(self.columns) if column.total)

    def write_order(self, owner, order, details):
        """Write one shipment order and return its quantity sums.

        The order is placed whole on one page whenever it fits there;
        only an order taller than a full page is continued across pages.
        """
        sums = self._sum_columns(details)
        need = len(details) + 1  # details plus the 整单合计 row
        if need <= self.capacity:
            if need > self._space:
                self._close_page()
            for detail in details:
                self._write_detail(detail)
            self._write_total_row('%s %s' % (order, ORDER_TOTAL_LABEL), sums)
        else:
            self._write_oversized(order, details, sums)
        return sums

    def _write_oversized(self, order, details, sums):
        index = 0
        total = len(details)
        while index < total:
            self._ensure_page()
            space = self._space
            remaining = total - index
            if remaining + 1 <= space:
                # The rest of the order, including its 整单合计 row, fits
                # on this page.  An exact fit ends the order here and
                # must not spawn a blank continuation page.
                for detail in details[index:]:
                    self._write_detail(detail)
                self._write_total_row('%s %s' % (order, ORDER_TOTAL_LABEL), sums)
                index = total
            else:
                writable = space - 1  # reserve one row for 本页小计
                if writable <= 0:
                    # Not even one detail plus the page subtotal fits;
                    # move to a fresh page.  Cannot happen on an empty
                    # page because capacity >= 2.
                    self._close_page()
                    continue
                page_slice = details[index:index + writable]
                for detail in page_slice:
                    self._write_detail(detail)
                index += writable
                # The page subtotal covers only this page's slice, so
                # across pages every physical piece is counted once.
                self._write_total_row('%s %s' % (order, PAGE_SUBTOTAL_LABEL),
                                      self._sum_columns(page_slice))
                self._close_page()

    def write_owner_total(self, owner, sums):
        if self._space < 1:
            self._close_page()
        self._write_total_row('%s %s' % (owner, OWNER_TOTAL_LABEL), sums)


def write_handover(workbook, rows, row_limit,
                   owner_key='owner',
                   order_key='order',
                   columns=DEFAULT_COLUMNS,
                   sheet_name='交接单',
                   title='仓库交接单'):
    """Render a grouped, paginated handover sheet into ``workbook``.

    :param workbook: the :class:`xlwt.Workbook` to write into.
    :param rows: iterable of detail rows (mappings or objects), ordered
        by (``owner_key``, ``order_key``).  It is consumed in a single
        pass; only one shipment order is buffered at a time.
    :param row_limit: maximum number of rows per page, including the
        two header rows and any subtotal/total rows.
    :param owner_key: field holding the cargo owner (货主).
    :param order_key: field holding the shipment order number (发运单号).
    :param columns: sequence of :class:`HandoverColumn` describing the
        layout; columns marked ``total=True`` are summed in the
        本页小计 / 整单合计 / 货主合计 rows.
    :param sheet_name: base name of the created sheets; pages are named
        ``'<sheet_name>-1'``, ``'<sheet_name>-2'``, ...
    :param title: report title shown on top of every page, suffixed
        with the page number.
    :returns: the list of worksheets created, in page order.
    """
    writer = _HandoverWriter(workbook, columns, row_limit,
                             sheet_name, title)
    last_owner = None
    owner_sums = None
    for owner, order, details in _iter_order_groups(rows, owner_key, order_key):
        if last_owner is None:
            last_owner = owner
            owner_sums = dict((i, 0) for i, column in enumerate(writer.columns)
                              if column.total)
        elif owner != last_owner:
            writer.write_owner_total(last_owner, owner_sums)
            last_owner = owner
            owner_sums = dict((i, 0) for i, column in enumerate(writer.columns)
                              if column.total)
        order_sums = writer.write_order(owner, order, details)
        for i in owner_sums:
            owner_sums[i] += order_sums.get(i, 0)
    if last_owner is not None:
        writer.write_owner_total(last_owner, owner_sums)
    return writer.sheets
