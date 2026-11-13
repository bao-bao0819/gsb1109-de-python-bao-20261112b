"""
pyexcel_io.readers.csv_sheet
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

csv file reader

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

import csv

import pyexcel_io.service as service
import pyexcel_io._compact as compact
import pyexcel_io.constants as constants
from pyexcel_io.column_contract import (
    CSVColumnContractError,
    ColumnContract,
    ColumnRule,
    normalise_header_rows,
    normalise_headers,
)
from pyexcel_io.plugin_api import ISheet

DEFAULT_SEPARATOR = "__"
DEFAULT_SHEET_SEPARATOR_FORMATTER = "---%s---" % constants.DEFAULT_NAME + "%s"
SEPARATOR_MATCHER = "---%s:(.*)---" % constants.DEFAULT_NAME
DEFAULT_CSV_STREAM_FILE_FORMATTER = (
    "---%s:" % constants.DEFAULT_NAME + "%s---%s"
)
DEFAULT_NEWLINE = "\r\n"
BOM_LITTLE_ENDIAN = b"\xff\xfe"
BOM_BIG_ENDIAN = b"\xfe\ff"
LITTLE_ENDIAN = 0
BIG_ENDIAN = 1

CONTRACT_KEYWORDS = (
    "column_contract",
    "input_contract",
    "column_rules",
    "column_types",
    "columns",
    "contract",
)
HEADER_KEYWORDS = (
    "header_rows",
    "header_row_count",
    "headers",
    "header",
)


class CSVMemoryMapIterator(object):
    """
    Wrapper class for mmap object

    mmap object does not handle encoding at all. This class
    provide the necessary transcoding for utf-8, utf-16 and utf-32
    """

    def __init__(self, mmap_obj, encoding):
        self.__mmap_obj = mmap_obj
        self.__encoding = encoding
        self.__count = 0
        self.__endian = LITTLE_ENDIAN
        if encoding == "utf-8":
            # ..\r\x00\n
            # \x00\x..
            self.__zeros_left_in_2_row = 0
        elif encoding == "utf-16":
            # ..\r\x00\n
            # \x00\x..
            self.__zeros_left_in_2_row = 1
        elif encoding == "utf-32":
            # \r\x00\x00\x00\n
            # \x00\x00\x00\x..
            self.__zeros_left_in_2_row = 3
        elif encoding in ["utf-32-be", "utf-16-be"]:
            self.__zeros_left_in_2_row = 0
            self.__endian = BIG_ENDIAN
        elif encoding == "utf-32-le":
            self.__zeros_left_in_2_row = 3
            self.__endian = LITTLE_ENDIAN
        elif encoding == "utf-16-le":
            self.__zeros_left_in_2_row = 1
            self.__endian = LITTLE_ENDIAN
        else:
            raise Exception("Encoding %s is not supported" % encoding)

    def __iter__(self):
        return self

    def __next__(self):
        line = self.__mmap_obj.readline()
        if self.__count == 0:
            utf_16_32 = (
                self.__encoding == "utf-16" or self.__encoding == "utf-32"
            )
            if utf_16_32:
                bom_header = line[:2]
                if bom_header == BOM_BIG_ENDIAN:
                    self.__endian = BIG_ENDIAN
        elif self.__endian == LITTLE_ENDIAN:
            line = line[self.__zeros_left_in_2_row :]  # noqa: E203
        if self.__endian == LITTLE_ENDIAN:
            line = line.rstrip()
        line = line.decode(self.__encoding)
        self.__count += 1
        if line == "":
            raise StopIteration

        return line

    def close(self):
        pass


class _ContractRow(list):
    """A row whose contracted cells must not be auto-detected again."""

    def __init__(self, values, converted_positions, source_row, data_row):
        super(_ContractRow, self).__init__(values)
        self.converted_positions = set(converted_positions)
        self.source_row = source_row
        self.data_row = data_row


class CSVSheetReader(ISheet):
    """generic csv file reader"""

    def __init__(
        self,
        sheet,
        encoding="utf-8",
        auto_detect_float=True,
        ignore_infinity=True,
        auto_detect_int=True,
        auto_detect_datetime=True,
        pep_0515_off=True,
        ignore_nan_text=False,
        default_float_nan=None,
        **keywords
    ):
        self._native_sheet = sheet
        self._encoding = encoding
        self.__auto_detect_int = auto_detect_int
        self.__auto_detect_float = auto_detect_float
        self.__ignore_infinity = ignore_infinity
        self.__auto_detect_datetime = auto_detect_datetime
        self.__file_handle = None
        self.__pep_0515_off = pep_0515_off
        self.__ignore_nan_text = ignore_nan_text
        self.__default_float_nan = default_float_nan
        self.__contract = self.__extract_contract(keywords)
        self.__explicit_header_rows = "header_rows" in keywords
        self.__header_rows = self.__extract_header_rows(keywords)
        self.__headers = self.__extract_headers(keywords)
        self.__header_values = []
        self.__bound_contract = None
        self.__rows_consumed = 0
        self.__data_row = 0
        self.__row_iterator = None
        self.__read_violations = []
        self.__read_number = 0
        self._keywords = keywords

    @staticmethod
    def __extract_contract(keywords):
        found = []
        for keyword in CONTRACT_KEYWORDS:
            if keyword not in keywords:
                continue
            value = keywords.pop(keyword)
            if value is None:
                continue
            found.append(value)
        if len(found) > 1:
            raise TypeError(
                "Specify the csv column contract only once"
            )
        if not found:
            return None
        return ensure_contract(found[0])

    @staticmethod
    def __extract_header_rows(keywords):
        value = keywords.pop("header_rows", None)
        value_from_alias = keywords.pop("header_row_count", None)
        if value is None:
            value = value_from_alias
        elif value_from_alias is not None:
            raise TypeError(
                "Specify either header_rows or header_row_count, not both"
            )
        if value is None:
            return None
        return normalise_header_rows(value)

    @staticmethod
    def __extract_headers(keywords):
        value = keywords.pop("headers", None)
        alias = keywords.pop("header", None)
        if value is None:
            value = alias
        elif alias is not None:
            raise TypeError("Specify either headers or header, not both")
        if value is None:
            return None
        return normalise_headers(value)

    @property
    def column_contract(self):
        return self.__contract

    @property
    def input_contract(self):
        return self.__contract

    @property
    def contract_violations(self):
        if self.__contract is None:
            return []
        return self.__read_violations

    @property
    def errors(self):
        return self.contract_violations

    def get_file_handle(self):
        """return me unicode reader for csv"""
        raise NotImplementedError("Please implement get_file_handle()")

    def row_iterator(self):
        self.__file_handle = self.get_file_handle()
        self.__read_number += 1
        if self.__read_number > 1:
            self.__header_values = []
            self.__bound_contract = None
            self.__rows_consumed = 0
            self.__data_row = 0
        if self.__contract is not None:
            self.__contract.begin_read()
        csv_keywords = {
            key: value
            for key, value in self._keywords.items()
            if key not in CONTRACT_KEYWORDS
            and key not in HEADER_KEYWORDS
        }
        self.__row_iterator = csv.reader(
            self.__file_handle, **csv_keywords
        )
        self.__read_violations = []
        if self.__contract is not None:
            self.__prepare_contract()
        return self.__iter_rows()

    def __prepare_contract(self):
        if self.__headers is not None:
            self.__header_values = [list(row) for row in self.__headers]
            self.__bound_contract = self.__contract.bind(
                self.__header_values
            )
            if self.__explicit_header_rows and self.__header_rows:
                self.__consume_rows(self.__header_rows)
            return
        if self.__explicit_header_rows:
            count = self.__header_rows or 0
            if count:
                self.__prepare_headers(count)
            else:
                self.__bound_contract = self.__contract.bind(
                    self.__header_values
                )
            return
        contract = self.__contract
        if contract.headers:
            self.__header_values = [list(row) for row in contract.headers]
            self.__bound_contract = contract.bind(self.__header_values)
            return
        if contract.header_rows and contract.consume_header_rows:
            self.__prepare_headers(contract.header_rows)
            return
        self.__bound_contract = contract.bind(self.__header_values)

    def __consume_rows(self, count):
        for _ in range(count):
            try:
                next(self.__row_iterator)
            except StopIteration:
                break
            self.__rows_consumed += 1

    def __prepare_headers(self, count):
        self.__header_rows = count
        for position in range(count):
            try:
                header = next(self.__row_iterator)
            except StopIteration:
                break
            self.__rows_consumed += 1
            self.__header_values.append(list(header))
            if position == count - 1:
                self.__bound_contract = self.__contract.bind(
                    self.__header_values
                )
        if self.__bound_contract is None:
            self.__bound_contract = self.__contract.bind(
                self.__header_values
            )

    def __iter_rows(self):
        for row in self.__row_iterator:
            row = self.__apply_contract(row)
            yield row

    def __apply_contract(self, row):
        if self.__contract is None:
            return row
        if self.__bound_contract is None:
            self.__bound_contract = self.__contract.bind(
                self.__header_values
            )
        source_row = self.__rows_consumed
        line_number = source_row + 1
        converted, violations = self.__bound_contract.convert_row(
            row,
            data_row=self.__data_row,
            source_row=source_row,
            line_number=line_number,
        )
        if violations:
            self.__read_violations.extend(violations)
            if self.__contract.strict:
                self.close()
                raise CSVColumnContractError(violations)
        self.__data_row += 1
        self.__rows_consumed += 1
        return _ContractRow(
            converted,
            self.__bound_contract.position_rules.keys(),
            source_row,
            self.__data_row - 1,
        )

    def column_iterator(self, row):
        converted_positions = getattr(row, "converted_positions", ())
        for column, element in enumerate(row):
            if column in converted_positions:
                yield element
            elif element is not None and element != "":
                yield self.__convert_cell(element)
            else:
                yield element

    def __convert_cell(self, csv_cell_text):
        ret = None
        if self.__auto_detect_int:
            ret = service.detect_int_value(csv_cell_text, self.__pep_0515_off)
        if ret is None and self.__auto_detect_float:
            ret = service.detect_float_value(
                csv_cell_text,
                self.__pep_0515_off,
                ignore_nan_text=self.__ignore_nan_text,
                default_float_nan=self.__default_float_nan,
            )
            shall_we_ignore_the_conversion = (
                ret in [float("inf"), float("-inf")]
            ) and self.__ignore_infinity
            if shall_we_ignore_the_conversion:
                ret = None
        if ret is None and self.__auto_detect_datetime:
            ret = service.detect_date_value(csv_cell_text)
        if ret is None:
            ret = csv_cell_text
        return ret

    def close(self):
        if self.__file_handle:
            self.__file_handle.close()


def ensure_contract(value):
    """Turn a contract-like value into a :class:`ColumnContract`."""
    if isinstance(value, ColumnContract):
        return value
    if isinstance(value, ColumnRule):
        return ColumnContract(rules=[value])
    if isinstance(value, dict):
        known = set(
            (
                "rules",
                "header_rows",
                "headers",
                "strict",
                "on_error",
                "missing_column",
                "consume_header_rows",
            )
        )
        if known.intersection(value):
            return ColumnContract(**value)
        return ColumnContract(rules=value)
    if isinstance(value, (list, tuple)):
        return ColumnContract(rules=value)
    raise TypeError(
        "Unsupported csv column contract %r" % (value,)
    )


csv_column_contract = ensure_contract


# else: means the generator has been run
# yes, no run, no file open.


class CSVFileReader(CSVSheetReader):
    """read csv from physical file"""

    def get_file_handle(self):
        unicode_reader = open(
            self._native_sheet.payload, "r", encoding=self._encoding
        )
        return unicode_reader


class CSVinMemoryReader(CSVSheetReader):
    """read csv file from memory"""

    def get_file_handle(self):
        if isinstance(self._native_sheet.payload, compact.BytesIO):
            # please note that
            # if the end developer feed us bytesio in python3
            # we will do the conversion to StriongIO but that
            # comes at a cost.
            content = self._native_sheet.payload.read()
            unicode_reader = compact.StringIO(content.decode(self._encoding))
        else:
            unicode_reader = self._native_sheet.payload

        return unicode_reader
