import pytest

from pyexcel_io import iget_data_tee, save_data
from pyexcel_io.tee import TeeBook
from pyexcel_io._compact import StringIO, OrderedDict
from pyexcel_io.exceptions import BufferLimitExceeded

from .nose_tools import eq_, raises


def make_book(rows_by_sheet, max_buffered_rows=1000):
    sheets = OrderedDict()
    for name, rows in rows_by_sheet.items():
        sheets[name] = (row for row in rows)
    return TeeBook(sheets, max_buffered_rows=max_buffered_rows)


def test_interleaved_consumption_of_two_sheets():
    book = make_book(
        OrderedDict(
            [
                ("sheet1", [[1, 2], [3, 4], [5, 6]]),
                ("sheet2", [["a", "b"], ["c", "d"]]),
            ]
        )
    )
    reader1 = book.get_reader("sheet1")
    reader2 = book.get_reader("sheet2")

    eq_([1, 2], next(reader1))
    eq_(["a", "b"], next(reader2))
    eq_([3, 4], next(reader1))
    eq_(["c", "d"], next(reader2))
    eq_([5, 6], next(reader1))
    with pytest.raises(StopIteration):
        next(reader1)
    with pytest.raises(StopIteration):
        next(reader2)


def test_abandoning_a_sheet_early_keeps_the_other_intact():
    book = make_book(
        OrderedDict(
            [
                ("sheet1", [[1], [2], [3], [4]]),
                ("sheet2", [["a"], ["b"]]),
            ]
        )
    )
    reader1 = book.get_reader("sheet1")
    reader2 = book.get_reader("sheet2")

    eq_([1], next(reader1))
    reader1.close()
    assert reader1.closed

    eq_([["a"], ["b"]], list(reader2))


def test_abandoned_sheet_can_be_skipped_before_its_turn():
    book = make_book(
        OrderedDict(
            [
                ("sheet1", [[1], [2], [3]]),
                ("sheet2", [["a"]]),
            ]
        )
    )
    book.get_reader("sheet1").close()
    eq_([["a"]], list(book.get_reader("sheet2")))


def test_slow_reader_is_named_when_buffer_limit_is_hit():
    book = make_book(
        OrderedDict(
            [
                ("sheet1", [[1], [2], [3], [4]]),
                ("sheet2", [["a"]]),
            ]
        ),
        max_buffered_rows=2,
    )
    reader2 = book.get_reader("sheet2")

    with pytest.raises(BufferLimitExceeded) as exc_info:
        next(reader2)
    eq_("sheet1", exc_info.value.sheet_name)
    eq_(2, exc_info.value.limit)
    assert "sheet1" in str(exc_info.value)
    eq_(2, book.buffered_rows("sheet1"))


def test_reading_can_resume_after_buffer_limit_is_hit():
    book = make_book(
        OrderedDict(
            [
                ("sheet1", [[1], [2], [3]]),
                ("sheet2", [["a"]]),
            ]
        ),
        max_buffered_rows=2,
    )
    reader1 = book.get_reader("sheet1")
    reader2 = book.get_reader("sheet2")

    with pytest.raises(BufferLimitExceeded):
        next(reader2)

    eq_([1], next(reader1))
    eq_([2], next(reader1))
    eq_(["a"], next(reader2))
    eq_([[3]], list(reader1))


def test_abandoning_the_blocking_reader_unblocks_the_other():
    book = make_book(
        OrderedDict(
            [
                ("sheet1", [[1], [2], [3], [4]]),
                ("sheet2", [["a"]]),
            ]
        ),
        max_buffered_rows=2,
    )
    reader1 = book.get_reader("sheet1")
    reader2 = book.get_reader("sheet2")

    with pytest.raises(BufferLimitExceeded):
        next(reader2)

    reader1.close()
    eq_(["a"], next(reader2))


def test_reading_a_closed_reader_stops_iteration():
    book = make_book(OrderedDict([("sheet1", [[1], [2]])]))
    reader = book.get_reader("sheet1")
    eq_([1], next(reader))
    reader.close()
    reader.close()
    with pytest.raises(StopIteration):
        next(reader)


def test_reader_works_as_context_manager():
    book = make_book(
        OrderedDict([("sheet1", [[1], [2]]), ("sheet2", [["a"]])])
    )
    with book.get_reader("sheet1") as reader1:
        eq_([1], next(reader1))
    eq_([["a"]], list(book.get_reader("sheet2")))


@raises(KeyError)
def test_unknown_sheet_reader_is_rejected():
    make_book(OrderedDict([("sheet1", [[1]])])).get_reader("sheet2")


@raises(ValueError)
def test_negative_buffer_limit_is_rejected():
    make_book(OrderedDict([("sheet1", [[1]])]), max_buffered_rows=-1)


def test_closing_the_book_releases_everything():
    book = make_book(OrderedDict([("sheet1", [[1]]), ("sheet2", [[2]])]))
    book.get_reader("sheet1")
    book.close()
    assert book.get_reader("sheet1").closed
    assert book.get_reader("sheet2").closed
    with pytest.raises(StopIteration):
        next(book.get_reader("sheet1"))


class ForwardOnlyStream(object):
    """A text stream which cannot seek, like a network socket"""

    def __init__(self, text):
        self._lines = iter(text.splitlines(keepends=True))
        self.closed = False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self._lines)

    def readline(self):
        return next(self._lines, "")

    def read(self, *args):
        return "".join(self._lines)

    def close(self):
        self.closed = True


def test_iget_data_tee_reads_a_forward_only_stream():
    stream = ForwardOnlyStream("1,2\n3,4\n")
    book, reader = iget_data_tee(stream, file_type="csv")
    sheet = book.get_reader("csv")
    eq_([[1, 2], [3, 4]], list(sheet))
    reader.close()
    stream.close()


def test_iget_data_tee_interleaves_sheets_from_one_stream():
    stream = StringIO()
    save_data(
        stream,
        OrderedDict(
            [
                ("sheet1", [[1, 2], [3, 4], [5, 6]]),
                ("sheet2", [["a", "b"], ["c", "d"]]),
            ]
        ),
        file_type="csv",
        lineterminator="\n",
    )
    book, reader = iget_data_tee(
        stream, file_type="csv", multiple_sheets=True, lineterminator="\n"
    )
    eq_(["sheet1", "sheet2"], book.sheet_names)

    reader1 = book.get_reader("sheet1")
    reader2 = book.get_reader("sheet2")
    eq_([1, 2], next(reader1))
    eq_([["a", "b"], ["c", "d"]], list(reader2))
    eq_([[3, 4], [5, 6]], list(reader1))
    reader.close()
    stream.close()


def test_iget_data_tee_leaves_the_stream_to_the_caller():
    stream = StringIO()
    save_data(
        stream,
        OrderedDict([("sheet1", [[1], [2]]), ("sheet2", [[3], [4]])]),
        file_type="csv",
        lineterminator="\n",
    )
    book, reader = iget_data_tee(
        stream, file_type="csv", multiple_sheets=True, lineterminator="\n"
    )
    eq_([[1], [2]], list(book.get_reader("sheet1")))
    book.get_reader("sheet2").close()
    book.close()

    assert not stream.closed
    reader.close()
    stream.close()
    assert stream.closed
