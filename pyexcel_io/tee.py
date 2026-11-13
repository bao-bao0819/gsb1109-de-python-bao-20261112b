"""
pyexcel_io.tee
~~~~~~~~~~~~~~~~~~~

Bounded multi-sheet consumption over a forward-only stream

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

from collections import deque

from pyexcel_io.exceptions import BufferLimitExceeded

DEFAULT_MAX_BUFFERED_ROWS = 1000

_ACTIVE = "active"
_EXHAUSTED = "exhausted"
_ABANDONED = "abandoned"


class TeeSheetReader(object):
    """One consumer's view onto a single sheet of a :class:`TeeBook`

    It is an iterator over the rows of one sheet. Call `close()` to
    abandon the sheet early: its buffered rows are dropped and its
    remaining rows are discarded as the stream advances, without
    affecting any other sheet's reader.
    """

    def __init__(self, book, sheet_name):
        self._book = book
        self.sheet_name = sheet_name

    def __iter__(self):
        return self

    def __next__(self):
        return self._book._next_row(self.sheet_name)

    @property
    def closed(self):
        """True once the sheet has been abandoned via `close()`"""
        return self._book._is_abandoned(self.sheet_name)

    def close(self):
        """Abandon this sheet; other sheets keep all of their rows"""
        self._book.abandon(self.sheet_name)

    def __enter__(self):
        return self

    def __exit__(self, a_type, value, traceback):
        self.close()


class TeeBook(object):
    """Fan out sequentially ordered sheet iterators to many readers

    The sheets of a workbook arrive one after another on a forward-only
    stream. This class lets one reader per sheet consume them in any
    interleaving: rows that arrive while another sheet is being read
    are buffered, but only up to `max_buffered_rows` per sheet. When a
    slow reader lets its buffer reach that limit,
    :class:`pyexcel_io.exceptions.BufferLimitExceeded` is raised and it
    names the blocking sheet, so rows are never hoarded without bound.

    The book never closes the underlying stream or reader: they belong
    to the caller, who closes them after the last sheet reader exits.

    :param sheets: an ordered mapping of sheet name to row iterator,
                   as returned by :func:`pyexcel_io.iget_data`
    :param max_buffered_rows: the agreed per-sheet buffer limit
    """

    def __init__(self, sheets, max_buffered_rows=DEFAULT_MAX_BUFFERED_ROWS):
        if max_buffered_rows < 0:
            raise ValueError("max_buffered_rows must not be negative")
        self._max_buffered_rows = max_buffered_rows
        self._sheet_names = list(sheets.keys())
        self._iterators = {
            name: iter(sheets[name]) for name in self._sheet_names
        }
        self._buffers = {name: deque() for name in self._sheet_names}
        self._states = {name: _ACTIVE for name in self._sheet_names}
        self._readers = {}
        self._current = 0

    @property
    def sheet_names(self):
        """the sheet names in their arrival order on the stream"""
        return list(self._sheet_names)

    @property
    def max_buffered_rows(self):
        """the agreed per-sheet buffer limit"""
        return self._max_buffered_rows

    def get_reader(self, sheet_name):
        """Return the :class:`TeeSheetReader` for the named sheet"""
        self._check_known(sheet_name)
        if sheet_name not in self._readers:
            self._readers[sheet_name] = TeeSheetReader(self, sheet_name)
        return self._readers[sheet_name]

    def buffered_rows(self, sheet_name):
        """the number of rows currently buffered for the named sheet"""
        self._check_known(sheet_name)
        return len(self._buffers[sheet_name])

    def abandon(self, sheet_name):
        """Abandon a sheet early, dropping its buffered rows

        Rows of the other sheets are not affected. The abandoned
        sheet's remaining rows are discarded as the stream advances.
        """
        self._check_known(sheet_name)
        self._states[sheet_name] = _ABANDONED
        self._buffers[sheet_name].clear()

    def close(self):
        """Abandon every sheet and release all buffered rows

        The underlying stream is left open; it belongs to the caller.
        """
        for sheet_name in self._sheet_names:
            self._states[sheet_name] = _ABANDONED
            self._buffers[sheet_name].clear()
        self._iterators = {}

    def _check_known(self, sheet_name):
        if sheet_name not in self._states:
            raise KeyError("Unknown sheet: %s" % sheet_name)

    def _is_abandoned(self, sheet_name):
        return self._states[sheet_name] == _ABANDONED

    def _next_row(self, sheet_name):
        state = self._states[sheet_name]
        if state == _ABANDONED:
            raise StopIteration
        buffer = self._buffers[sheet_name]
        if buffer:
            return buffer.popleft()
        if state == _EXHAUSTED:
            raise StopIteration
        while self._current < len(self._sheet_names):
            if self._states[sheet_name] == _EXHAUSTED:
                raise StopIteration
            current_name = self._sheet_names[self._current]
            if current_name != sheet_name:
                if self._states[current_name] == _ABANDONED:
                    current_buffer = None
                else:
                    current_buffer = self._buffers[current_name]
                    if len(current_buffer) >= self._max_buffered_rows:
                        raise BufferLimitExceeded(
                            current_name, self._max_buffered_rows
                        )
            try:
                row = next(self._iterators[current_name])
            except StopIteration:
                self._states[current_name] = _EXHAUSTED
                self._current += 1
                continue
            if current_name == sheet_name:
                return row
            if current_buffer is None:
                continue
            current_buffer.append(row)
        raise StopIteration
