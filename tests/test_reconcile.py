from pyexcel_io import get_data
from pyexcel_io._compact import StringIO
from pyexcel_io.reconcile import (
    ROW_CONFLICTED,
    ROW_UNCHANGED,
    ROW_UNTOUCHED,
    extract_rows,
    reconcile_rows,
)

from .nose_tools import eq_, raises


def region_scope(sheet_name, rows):
    return sheet_name.split("-")[0]


def identity_without_header(row):
    sku = row[0]
    if sku in (None, "", "sku"):
        return None
    return sku


def test_unchanged_rows_ignore_sheet_order():
    previous = {
        "north": [["a", 1], ["b", 2]],
        "south": [["c", 3]],
    }
    current = {
        "south": [["c", 3]],
        "north": [["a", 1], ["b", 2]],
    }
    result = reconcile_rows(previous, current, identity_columns=0)
    eq_(len(result.unchanged), 3)
    eq_(result.migrated, [])
    eq_(result.as_dict()[ROW_UNCHANGED], result.unchanged)


def test_renamed_sheet_is_migration_under_shared_scope():
    previous = {"north-jan": [["a", 1], ["b", 2]]}
    current = {"north-feb": [["a", 1], ["b", 2]]}
    result = reconcile_rows(
        previous,
        current,
        identity_columns=0,
        scope_func=region_scope,
    )
    eq_(len(result.migrated), 2)
    record = result.migrated[0]
    eq_(record.identity, "a")
    eq_(record.scope, "north")
    eq_(record.sheet_names, ("north-feb",))
    eq_(record.previous.sheet_names, ("north-jan",))
    eq_(result.covered_scopes, frozenset(["north"]))


def test_split_into_two_sheets_keeps_identity_trackable():
    previous = {"north": [["a", 1], ["b", 2], ["c", 3]]}
    current = {
        "north-1": [["a", 1]],
        "north-2": [["b", 2], ["c", 3]],
    }
    result = reconcile_rows(
        previous,
        current,
        identity_columns=0,
        scope_func=region_scope,
    )
    eq_(len(result.migrated), 3)
    eq_(result.changed, [])
    eq_(result.missing, [])
    identities = sorted(record.identity for record in result.migrated)
    eq_(identities, ["a", "b", "c"])


def test_missing_sheet_does_not_delete_without_coverage_claim():
    previous = {
        "north": [["a", 1]],
        "south": [["b", 2]],
    }
    current = {"north": [["a", 1]]}
    result = reconcile_rows(previous, current, identity_columns=0)
    eq_(len(result.untouched), 1)
    eq_(result.untouched[0].identity, "b")
    eq_(result.missing, [])


def test_covered_scope_declares_rows_missing():
    previous = {
        "north": [["a", 1]],
        "south": [["b", 2]],
    }
    current = {"north": [["a", 1]]}
    result = reconcile_rows(
        previous,
        current,
        identity_columns=0,
        covered_scopes=["north", "south"],
    )
    eq_(len(result.missing), 1)
    missing = result.missing[0]
    eq_(missing.identity, "b")
    eq_(missing.scope, "south")
    eq_(result.untouched, [])


def test_partial_coverage_only_deletes_covered_scope():
    previous = {
        "north": [["a", 1]],
        "south": [["b", 2]],
    }
    current = {"north": [["a", 1]]}
    result = reconcile_rows(
        previous,
        current,
        identity_columns=0,
        covered_scopes=["north"],
    )
    eq_(result.missing, [])
    eq_(len(result.untouched), 1)
    eq_(result.untouched[0].scope, "south")


def test_conflicting_sources_are_both_retained():
    previous = {"north": [["a", 1]]}
    current = {
        "north-warehouse": [["a", 1]],
        "north-store": [["a", 9]],
    }
    result = reconcile_rows(
        previous,
        current,
        identity_columns=0,
        scope_func=region_scope,
    )
    eq_(len(result.conflicts), 1)
    conflict = result.conflicts[0]
    eq_(conflict.identity, "a")
    eq_(conflict.is_conflicted, True)
    eq_(conflict.sheet_names, ("north-warehouse", "north-store"))
    eq_(len(conflict.sources), 2)
    values = [source.row for source in conflict.sources]
    eq_(values, [("a", 1), ("a", 9)])
    eq_(
        [source.row_index for source in conflict.sources],
        [0, 0],
    )
    eq_(conflict in result.needs_recollection, True)
    eq_(result.new, [])


def test_duplicate_sources_with_same_value_are_not_conflicts():
    previous = {"north": [["a", 1]]}
    current = {
        "north-1": [["a", 1]],
        "north-2": [["a", 1]],
    }
    result = reconcile_rows(
        previous,
        current,
        identity_columns=0,
        scope_func=region_scope,
    )
    eq_(result.conflicts, [])
    eq_(len(result.migrated), 1)
    eq_(result.migrated[0].sheet_names, ("north-1", "north-2"))


def test_changed_value_requires_recollection():
    previous = {"north": [["a", 1], ["b", 2]]}
    current = {"north": [["a", 1], ["b", 5]]}
    result = reconcile_rows(previous, current, identity_columns=0)
    eq_(len(result.changed), 1)
    eq_(result.changed[0].identity, "b")
    eq_(result.changed[0].row, ("b", 5))
    eq_(result.changed[0].previous.row, ("b", 2))
    eq_(len(result.needs_recollection), 1)


def test_new_rows_are_reported():
    previous = {"north": [["a", 1]]}
    current = {"north": [["a", 1], ["d", 4]]}
    result = reconcile_rows(previous, current, identity_columns=0)
    eq_(len(result.new), 1)
    eq_(result.new[0].identity, "d")
    eq_(result.new[0].previous, None)


def test_identity_func_skips_header_and_empty_rows():
    previous = {
        "north": [["sku", "qty"], ["a", 1], [], ["", ""], ["b", 2]]
    }
    current = {
        "north": [["sku", "qty"], ["a", 1], ["b", 2], ["c", 3]]
    }
    result = reconcile_rows(
        previous,
        current,
        identity_func=identity_without_header,
    )
    eq_(len(result.unchanged), 2)
    eq_(len(result.new), 1)
    eq_(result.new[0].identity, "c")


def test_composite_identity_and_missing_cells():
    previous = {"north": [["a", "x", 1]]}
    current = {"north": [["a", "x", 1], ["a", None, 2]]}
    result = reconcile_rows(
        previous, current, identity_columns=[0, 1]
    )
    eq_(len(result.unchanged), 1)
    eq_(len(result.new), 1)
    eq_(result.new[0].identity, ("a", None))


def test_extract_rows_is_idempotent():
    book = {"north": [["a", 1]]}
    records = extract_rows(book, lambda row: row[0])
    again = extract_rows(records, lambda row: row[0])
    eq_(again, records)


def test_previous_records_preserve_conflict_provenance():
    previous_book = {
        "north-1": [["a", 1]],
        "north-2": [["a", 9]],
    }
    previous_records = extract_rows(
        previous_book,
        lambda row: row[0],
        scope_func=region_scope,
    )
    current = {"north-3": [["a", 1]]}
    result = reconcile_rows(
        previous_records,
        current,
        identity_func=lambda row: row[0],
        scope_func=region_scope,
    )
    eq_(len(result.changed), 1)
    changed = result.changed[0]
    eq_(len(changed.previous.sources), 2)
    eq_(changed.previous.is_conflicted, True)


def test_reconcile_with_get_data_csv_stream():
    previous_stream = StringIO("a,1\nb,2\n")
    current_stream = StringIO("a,1\nb,5\nc,3\n")
    previous = get_data(
        previous_stream, file_type="csv"
    )
    current = get_data(current_stream, file_type="csv")
    result = reconcile_rows(previous, current, identity_columns=0)
    eq_(len(result.unchanged), 1)
    eq_(len(result.changed), 1)
    eq_(len(result.new), 1)


def test_trailing_empty_cells_do_not_look_like_changes():
    previous = {"north": [["a", 1, None, ""]]}
    current = {"north": [["a", 1]]}
    result = reconcile_rows(previous, current, identity_columns=0)
    eq_(len(result.unchanged), 1)


def test_result_repr_lists_bucket_sizes():
    result = reconcile_rows(
        {"north": [["a", 1]]},
        {"north": [["a", 1]]},
        identity_columns=0,
    )
    text = repr(result)
    eq_("unchanged=1" in text, True)
    eq_(ROW_CONFLICTED, "conflicted")
    eq_(ROW_UNTOUCHED, "untouched")


@raises(ValueError)
def test_both_identity_options_are_rejected():
    reconcile_rows(
        {"n": [["a"]]},
        {"n": [["a"]]},
        identity_columns=0,
        identity_func=lambda row: row[0],
    )


@raises(ValueError)
def test_missing_identity_option_is_rejected():
    reconcile_rows({"n": [["a"]]}, {"n": [["a"]]})


@raises(ValueError)
def test_empty_identity_columns_are_rejected():
    reconcile_rows(
        {"n": [["a"]]},
        {"n": [["a"]]},
        identity_columns=[],
    )
