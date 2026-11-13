"""
pyexcel_io.reconcile
~~~~~~~~~~~~~~~~~~~~

Incremental reconciliation between two workbook deliveries.

A supplier may rename a sheet, reorder the sheets or split the data of one
region into two sheets between monthly deliveries.  Sheet names and sheet
positions are therefore not trustworthy identifiers.  This module lets the
caller declare the row identity and the coverage scope of each delivery and
compares the rows themselves instead of the sheet layout.

The comparison produces six verdicts:

* :data:`ROW_UNCHANGED`: the same row is still delivered from the same sheet.
* :data:`ROW_MIGRATED`: the row value is unchanged but it now comes from
  another sheet, for example after a rename or a split into two sheets.
* :data:`ROW_CHANGED`: the same identity is still covered but the row value
  changed, so the row has to be re-collected.
* :data:`ROW_NEW`: the identity did not exist in the previous delivery.
* :data:`ROW_MISSING`: the identity disappeared from a scope that the current
  delivery explicitly covers in full.
* :data:`ROW_CONFLICTED`: two sheets claim the same identity in the same
  scope but carry different values.  Every source is kept so the caller can
  trace each claim back to its sheet and row.

A missing sheet alone never deletes data: rows belonging to scopes that the
current delivery does not cover are reported as untouched without a verdict.

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

from pyexcel_io._compact import OrderedDict

ROW_UNCHANGED = "unchanged"
ROW_MIGRATED = "migrated"
ROW_CHANGED = "changed"
ROW_NEW = "new"
ROW_MISSING = "missing"
ROW_CONFLICTED = "conflicted"
ROW_UNTOUCHED = "untouched"


class RowSource(object):
    """Provenance of one observed row.

    :param sheet_name: name of the sheet the row was read from.
    :param row_index: zero based index of the row inside that sheet.
    :param row: the row value as a tuple of cells.
    :param scope: coverage scope the sheet belongs to.
    """

    def __init__(self, sheet_name, row_index, row, scope):
        self.sheet_name = sheet_name
        self.row_index = row_index
        self.row = tuple(row)
        self.scope = scope

    def __repr__(self):
        return "RowSource(sheet_name=%r, row_index=%r, scope=%r)" % (
            self.sheet_name,
            self.row_index,
            self.scope,
        )

    def __eq__(self, other):
        if not isinstance(other, RowSource):
            return NotImplemented
        return (
            self.sheet_name == other.sheet_name
            and self.row_index == other.row_index
            and self.row == other.row
            and self.scope == other.scope
        )

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result


class RowRecord(object):
    """One identity within one coverage scope.

    A record groups every source that claims the identity.  When more than
    one source carries a different value, :attr:`is_conflicted` is true and
    all sources remain available for trace back.
    """

    def __init__(self, identity, scope, sources=None, previous=None):
        self.identity = identity
        self.scope = scope
        self.sources = list(sources or [])
        self.previous = previous

    @property
    def row(self):
        """The row value of the first source, or ``None`` without sources."""
        if self.sources:
            return self.sources[0].row
        return None

    @property
    def is_conflicted(self):
        """True when the sources disagree on the row value."""
        if len(self.sources) < 2:
            return False
        first_value = _comparable_row(self.sources[0].row)
        for source in self.sources[1:]:
            if _comparable_row(source.row) != first_value:
                return True
        return False

    @property
    def sheet_names(self):
        """Ordered unique sheet names that provide the row."""
        names = []
        for source in self.sources:
            if source.sheet_name not in names:
                names.append(source.sheet_name)
        return tuple(names)

    def add_source(self, source):
        self.sources.append(source)

    def __repr__(self):
        return "RowRecord(identity=%r, scope=%r, sources=%d)" % (
            self.identity,
            self.scope,
            len(self.sources),
        )


class ReconcileResult(object):
    """The verdict of comparing two deliveries.

    Each bucket holds :class:`RowRecord` instances.  Records matched against
    a previous delivery keep that previous record in their ``previous``
    attribute so the migration history stays traceable.
    """

    def __init__(
        self,
        unchanged=None,
        migrated=None,
        changed=None,
        new=None,
        missing=None,
        conflicts=None,
        untouched=None,
        covered_scopes=None,
    ):
        self.unchanged = list(unchanged or [])
        self.migrated = list(migrated or [])
        self.changed = list(changed or [])
        self.new = list(new or [])
        self.missing = list(missing or [])
        self.conflicts = list(conflicts or [])
        self.untouched = list(untouched or [])
        self.covered_scopes = frozenset(covered_scopes or [])

    @property
    def needs_recollection(self):
        """Records that must be re-collected or manually resolved."""
        return self.changed + self.missing + self.conflicts

    def as_dict(self):
        """Return the buckets as a plain dictionary."""
        return {
            ROW_UNCHANGED: self.unchanged,
            ROW_MIGRATED: self.migrated,
            ROW_CHANGED: self.changed,
            ROW_NEW: self.new,
            ROW_MISSING: self.missing,
            ROW_CONFLICTED: self.conflicts,
            ROW_UNTOUCHED: self.untouched,
        }

    def __repr__(self):
        return (
            "ReconcileResult(unchanged=%d, migrated=%d, changed=%d, "
            "new=%d, missing=%d, conflicts=%d, untouched=%d)"
            % (
                len(self.unchanged),
                len(self.migrated),
                len(self.changed),
                len(self.new),
                len(self.missing),
                len(self.conflicts),
                len(self.untouched),
            )
        )


def default_scope_func(sheet_name, rows):
    """Scope function that treats each sheet name as its own scope."""
    return sheet_name


def _build_identity_func(identity_columns, identity_func):
    if identity_columns is not None and identity_func is not None:
        raise ValueError(
            "Provide either identity_columns or identity_func, not both"
        )
    if identity_func is not None:
        return identity_func
    if identity_columns is None:
        raise ValueError(
            "Row identity is required: pass identity_columns or "
            "identity_func"
        )
    if isinstance(identity_columns, int):
        columns = (identity_columns,)
    else:
        columns = tuple(identity_columns)
    if not columns:
        raise ValueError("identity_columns must not be empty")

    def identify(row):
        values = []
        for column in columns:
            if column < len(row):
                values.append(row[column])
            else:
                values.append(None)
        if len(columns) == 1:
            return values[0]
        return tuple(values)

    return identify


def _is_empty_row(row):
    for cell in row:
        if cell is not None and cell != "":
            return False
    return True


def _comparable_row(row):
    normalized = list(row)
    while normalized and (
        normalized[-1] is None or normalized[-1] == ""
    ):
        normalized.pop()
    return tuple(normalized)


def extract_rows(book, identity_func, scope_func=None):
    """Turn a workbook dictionary into identity keyed row records.

    :param book: the ``{sheet_name: rows}`` dictionary returned by
        :func:`pyexcel_io.get_data`, or a record dictionary produced by an
        earlier ``extract_rows`` call, which is then returned unchanged.
    :param identity_func: callable receiving a row tuple and returning its
        hashable identity.  Returning ``None`` skips the row, which is handy
        for ignoring header rows.
    :param scope_func: callable receiving ``(sheet_name, rows)`` and
        returning the coverage scope of the sheet.  It defaults to the
        sheet name.
    :returns: an :class:`~collections.OrderedDict` keyed by
        ``(scope, identity)`` holding :class:`RowRecord` instances.
    """
    if scope_func is None:
        scope_func = default_scope_func
    if _holds_records(book):
        return book

    records = OrderedDict()
    for sheet_name, rows in book.items():
        scope = scope_func(sheet_name, rows)
        for row_index, row in enumerate(rows):
            row = tuple(row)
            if _is_empty_row(row):
                continue
            identity = identity_func(row)
            if identity is None:
                continue
            key = (scope, identity)
            source = RowSource(sheet_name, row_index, row, scope)
            if key in records:
                records[key].add_source(source)
            else:
                records[key] = RowRecord(identity, scope, [source])
    return records


def _holds_records(book):
    if not isinstance(book, dict) or not book:
        return False
    first_value = next(iter(book.values()))
    return isinstance(first_value, RowRecord)


def _covered_scopes_of(book, scope_func):
    if _holds_records(book):
        return frozenset(record.scope for record in book.values())
    covered = set()
    for sheet_name, rows in book.items():
        covered.add(scope_func(sheet_name, rows))
    return frozenset(covered)


def reconcile_rows(
    previous,
    current,
    identity_columns=None,
    identity_func=None,
    scope_func=None,
    covered_scopes=None,
):
    """Compare two workbook deliveries row by row.

    :param previous: the previous delivery.  It is either a workbook
        dictionary from :func:`pyexcel_io.get_data` or a record dictionary
        stored from an earlier :func:`extract_rows` call.  Passing the
        latter preserves conflict provenance across deliveries.
    :param current: the latest delivery in the same shapes as ``previous``.
    :param identity_columns: one column index or a list of column indexes
        that uniquely identify a row.
    :param identity_func: alternative to ``identity_columns``: a callable
        mapping a row tuple to its hashable identity.  Returning ``None``
        skips the row.  Only one of the two identity parameters may be
        given.
    :param scope_func: callable receiving ``(sheet_name, rows)`` and
        returning the coverage scope of a sheet.  It defaults to the sheet
        name, so a renamed sheet is seen as a different scope only when the
        caller models explicit regions.
    :param covered_scopes: optional iterable of scopes that the current
        delivery promises to cover in full.  When omitted, the scopes are
        derived from the sheets actually present.  A previous row is only
        declared :data:`ROW_MISSING` when its scope is covered; otherwise
        it stays :data:`ROW_UNTOUCHED`.
    :returns: a :class:`ReconcileResult`.
    """
    identify = _build_identity_func(identity_columns, identity_func)
    if scope_func is None:
        scope_func = default_scope_func

    previous_records = extract_rows(previous, identify, scope_func)
    current_records = extract_rows(current, identify, scope_func)

    if covered_scopes is None:
        effective_covered = _covered_scopes_of(current, scope_func)
    else:
        effective_covered = frozenset(covered_scopes)

    result = ReconcileResult(covered_scopes=effective_covered)

    for key, current_record in current_records.items():
        previous_record = previous_records.get(key)
        current_record.previous = previous_record
        if current_record.is_conflicted:
            result.conflicts.append(current_record)
        elif previous_record is None:
            result.new.append(current_record)
        elif previous_record.is_conflicted:
            result.changed.append(current_record)
        else:
            same_value = _comparable_row(
                current_record.row
            ) == _comparable_row(previous_record.row)
            if not same_value:
                result.changed.append(current_record)
            elif (
                current_record.sheet_names == previous_record.sheet_names
            ):
                result.unchanged.append(current_record)
            else:
                result.migrated.append(current_record)

    for key, previous_record in previous_records.items():
        if key in current_records:
            continue
        if previous_record.scope in effective_covered:
            result.missing.append(previous_record)
        else:
            result.untouched.append(previous_record)

    return result
