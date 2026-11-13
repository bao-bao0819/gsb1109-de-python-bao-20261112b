from unittest import TestCase

from pyexcel_io import select_plugin
from pyexcel_io._compact import BytesIO, StringIO
from pyexcel_io.exceptions import (
    NoSupportingPluginFound,
    InsufficientInformationError,
)
from pyexcel_io.plugins import (
    NEW_READERS,
    NEW_WRITERS,
    OLD_READERS,
    OLD_WRITERS,
)
from pyexcel_io.selector import READ, WRITE

from .nose_tools import eq_, raises


class SinglePassTextStream:
    """A text stream that cannot be rewound and records every read"""

    def __init__(self, text):
        self._buffer = text
        self.read_sizes = []

    def read(self, size=-1):
        self.read_sizes.append(size)
        if size == 0:
            return ""
        if size is None or size < 0:
            chunk, self._buffer = self._buffer, ""
        else:
            chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk

    def readline(self):
        line, separator, rest = self._buffer.partition("\n")
        self._buffer = rest
        return line + separator


class SinglePassBinaryStream:
    """A binary stream that cannot be rewound"""

    def __init__(self, content):
        self._buffer = content

    def read(self, size=-1):
        if size == 0:
            return b""
        if size is None or size < 0:
            chunk, self._buffer = self._buffer, b""
        else:
            chunk, self._buffer = self._buffer[:size], self._buffer[size:]
        return chunk


def candidate_names(selection):
    return [candidate.name for candidate in selection.candidates]


def skipped_reasons(selection, name_part):
    for candidate in selection.skipped:
        if name_part in candidate.name:
            return candidate.reasons
    return []


class TestReadSelection(TestCase):
    def test_file_input_picks_file_reader(self):
        selection = select_plugin(file_name="data.csv", operation=READ)
        eq_(selection.chosen.name, "pyexcel_io.readers.csv_in_file.FileReader")
        eq_(selection.file_type, "csv")

    def test_file_input_explains_skipped_readers(self):
        selection = select_plugin(file_name="data.csv", operation=READ)
        reasons = skipped_reasons(selection, "csv_in_memory.MemoryReader")
        assert any("not for a file" in reason for reason in reasons)
        reasons = skipped_reasons(selection, "csv_content.ContentReader")
        assert any("not for a file" in reason for reason in reasons)

    def test_content_input_picks_content_reader(self):
        selection = select_plugin(file_content="a,b\n1,2\n", file_type="csv")
        eq_(
            selection.chosen.name,
            "pyexcel_io.readers.csv_content.ContentReader",
        )

    def test_single_pass_stream_picks_memory_reader(self):
        stream = SinglePassTextStream("a,b\n1,2\n")
        selection = select_plugin(file_stream=stream, file_type="csv")
        eq_(
            selection.chosen.name,
            "pyexcel_io.readers.csv_in_memory.MemoryReader",
        )

    def test_read_only_plugins_are_skipped_for_write(self):
        selection = select_plugin(
            file_stream=StringIO(), file_type="csv", operation=WRITE
        )
        eq_(
            selection.chosen.name,
            "pyexcel_io.writers.csv_in_memory.CsvMemoryWriter",
        )
        reasons = skipped_reasons(selection, "csv_in_memory.MemoryReader")
        assert any("supports 'read' only" in reason for reason in reasons)

    def test_write_to_file_picks_file_writer(self):
        selection = select_plugin(file_name="out.csv", operation=WRITE)
        eq_(
            selection.chosen.name,
            "pyexcel_io.writers.csv_in_file.CsvFileWriter",
        )


class TestSeekability(TestCase):
    def test_single_pass_stream_rules_out_zip_reader(self):
        stream = SinglePassBinaryStream(b"PK\x03\x04")
        selection = select_plugin(file_stream=stream, file_type="csvz")
        eq_(selection.chosen, None)
        reasons = skipped_reasons(selection, "csvz.FileReader")
        assert any("seekable" in reason for reason in reasons)

    def test_seekable_stream_keeps_zip_reader(self):
        selection = select_plugin(
            file_stream=BytesIO(b"PK\x03\x04"), file_type="csvz"
        )
        eq_(selection.chosen.name, "pyexcel_io.readers.csvz.FileReader")

    def test_no_candidate_raises_with_explanation(self):
        stream = SinglePassBinaryStream(b"PK\x03\x04")
        selection = select_plugin(file_stream=stream, file_type="csvz")
        try:
            selection.get()
            raise AssertionError("should have raised")
        except NoSupportingPluginFound as error:
            assert "seekable" in str(error)


class TestLosslessProbing(TestCase):
    def test_probing_does_not_consume_first_line(self):
        stream = SinglePassTextStream("first,line\nsecond,line\n")
        select_plugin(file_stream=stream, file_type="csv")
        assert all(size == 0 for size in stream.read_sizes)
        eq_(stream.readline(), "first,line\n")

    def test_probing_does_not_move_seekable_stream(self):
        stream = StringIO("a,b\n1,2\n")
        stream.seek(3)
        select_plugin(file_stream=stream, file_type="csv")
        eq_(stream.tell(), 3)

    @raises(InsufficientInformationError)
    def test_missing_file_type_for_stream(self):
        select_plugin(file_stream=SinglePassTextStream("a,b\n"))

    def test_missing_file_type_message_names_the_maker(self):
        stream = SinglePassTextStream("a,b\n")
        try:
            select_plugin(file_stream=stream)
            raise AssertionError("should have raised")
        except InsufficientInformationError as error:
            assert "file_type" in str(error)
        # and nothing was consumed while asking
        eq_(stream.read_sizes, [])
        eq_(stream.readline(), "a,b\n")

    @raises(InsufficientInformationError)
    def test_missing_input(self):
        select_plugin(file_type="csv")


class TestDeterminism(TestCase):
    def test_registration_order_does_not_change_the_choice(self):
        managers = (NEW_READERS, NEW_WRITERS, OLD_READERS, OLD_WRITERS)
        first = select_plugin(file_name="data.csv", operation=READ)
        reversed_tags = []
        for manager in managers:
            for tag, plugin_infos in manager.registry.items():
                plugin_infos.reverse()
                reversed_tags.append((manager, tag))
        try:
            second = select_plugin(file_name="data.csv", operation=READ)
        finally:
            for manager, tag in reversed_tags:
                manager.registry[tag].reverse()
        eq_(first.chosen.name, second.chosen.name)
        eq_(candidate_names(first), candidate_names(second))


class TestExplanation(TestCase):
    def test_explain_reports_choice_and_skips(self):
        selection = select_plugin(
            file_stream=StringIO(), file_type="csv", operation=WRITE
        )
        report = selection.explain()
        assert "chosen: pyexcel_io.writers.csv_in_memory.CsvMemoryWriter" \
            in report
        assert "skipped: pyexcel_io.readers.csv_in_memory.MemoryReader" \
            in report
        assert "supports 'read' only" in report

    def test_not_installed_packages_are_explained(self):
        selection = select_plugin(file_name="a.pdf", operation=READ)
        eq_(selection.chosen, None)
        reasons = skipped_reasons(selection, "pyexcel-pdfr")
        eq_(reasons, ["package 'pyexcel-pdfr' is not installed"])

    def test_library_filter_is_explained(self):
        selection = select_plugin(
            file_name="data.csv", operation=READ, library="pyexcel-xls"
        )
        eq_(selection.chosen, None)
        reasons = skipped_reasons(selection, "csv_in_file.FileReader")
        assert any("pyexcel-xls" in reason for reason in reasons)
