import pytest

from pyexcel_io._compact import OrderedDict
from pyexcel_io.database.querysets import (
    QuerysetsReader,
    UnalignedListCombinationError,
)
from pyexcel_io.database.exporters.queryset import QueryReader


def read_all(reader):
    result = OrderedDict()
    for index, sheet in enumerate(reader.content_array):
        result.update({sheet.name: reader.read_sheet(index).to_array()})
    return result


class Scan:
    def __init__(self, time):
        self.time = time


class Parcel:
    def __init__(self, number, scans):
        self.number = number
        self.scans = scans


class Note:
    def __init__(self, text):
        self.text = text


class Order:
    def __init__(self, order_no, parcels, notes=None):
        self.order_no = order_no
        self.parcels = parcels
        self.notes = notes or []


def make_orders():
    return [
        Order(
            "O1",
            [
                Parcel("P1", [Scan("10:01"), Scan("10:02")]),
                Parcel("P2", [Scan("10:05")]),
            ],
            [Note("fragile"), Note("leave at door")],
        ),
        Order("O2", [Parcel("P3", [])]),
    ]


COLUMNS = ["order_no", "parcels__number", "parcels__scans__time"]


def test_detail_path_aligns_parcels_with_their_own_scans():
    reader = QuerysetsReader(
        make_orders(), COLUMNS, detail_path="parcels__scans"
    )
    assert list(reader.to_array()) == [
        COLUMNS,
        ["O1", "P1", "10:01"],
        ["O1", "P1", "10:02"],
        ["O1", "P2", "10:05"],
    ]


def test_parcels_without_scans_are_dropped_by_default():
    reader = QuerysetsReader(
        make_orders(), COLUMNS, detail_path="parcels__scans"
    )
    rows = list(reader.to_array())
    assert all(row[1] != "P3" for row in rows[1:])


def test_keep_empty_retains_parcels_without_scans():
    reader = QuerysetsReader(
        make_orders(), COLUMNS, detail_path="parcels__scans", keep_empty=True
    )
    assert list(reader.to_array()) == [
        COLUMNS,
        ["O1", "P1", "10:01"],
        ["O1", "P1", "10:02"],
        ["O1", "P2", "10:05"],
        ["O2", "P3", None],
    ]


def test_independent_lists_require_explicit_confirmation():
    columns = COLUMNS + ["notes__text"]
    reader = QuerysetsReader(
        make_orders(), columns, detail_path="parcels__scans"
    )
    with pytest.raises(UnalignedListCombinationError):
        list(reader.to_array())


def test_allow_cartesian_combines_independent_lists_explicitly():
    columns = COLUMNS + ["notes__text"]
    orders = [
        Order("O1", [Parcel("P1", [Scan("10:01")])], [Note("a"), Note("b")])
    ]
    reader = QuerysetsReader(
        orders,
        columns,
        detail_path="parcels__scans",
        allow_cartesian=True,
    )
    assert list(reader.to_array()) == [
        columns,
        ["O1", "P1", "10:01", "a"],
        ["O1", "P1", "10:01", "b"],
    ]


def test_empty_independent_list_does_not_multiply_rows():
    columns = COLUMNS + ["notes__text"]
    orders = [Order("O1", [Parcel("P1", [Scan("10:01")])])]
    reader = QuerysetsReader(orders, columns, detail_path="parcels__scans")
    assert list(reader.to_array()) == [
        columns,
        ["O1", "P1", "10:01", None],
    ]


def test_detail_path_options_flow_through_query_reader():
    query_reader = QueryReader(
        make_orders(),
        None,
        column_names=COLUMNS,
        detail_path="parcels__scans",
        keep_empty=True,
    )
    result = read_all(query_reader)
    rows = list(result["pyexcel_sheet1"])
    assert rows == [
        COLUMNS,
        ["O1", "P1", "10:01"],
        ["O1", "P1", "10:02"],
        ["O1", "P2", "10:05"],
        ["O2", "P3", None],
    ]
    query_reader.close()


def test_legacy_behavior_without_detail_path_is_unchanged():
    orders = make_orders()
    reader = QuerysetsReader(orders, ["order_no"])
    assert list(reader.to_array()) == [["order_no"], ["O1"], ["O2"]]
