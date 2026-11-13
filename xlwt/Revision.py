# -*- coding: utf-8 -*-
"""Revision-aware streaming rows for :mod:`xlwt`.

Inventory feeds often arrive as rows identified by a business key (for
example a SKU) and carrying a revision number.  The same key can be seen
again after its row has already been flushed, possibly with a newer
revision.  This module provides a streaming buffer which:

* keeps the first-seen position of every key;
* accepts a row only when its revision is at least as new as the accepted
  one (equal revisions must carry equal values);
* reports equal-revision/different-value rows as conflicts;
* stores encoded row payloads in a temporary SQLite database instead of
  keeping all cell values in memory; and
* emits only the accepted rows, in first-seen order, when the worksheet is
  serialised.

Accepted rows are encoded when they are received, before the workbook
writes its global font/style/shared-string records.  Revision rows are
therefore not inserted into the worksheet's normal row cache and are not
affected by :meth:`xlwt.Worksheet.Worksheet.flush_row_data`; a correction
received before or after such a flush produces the same cells and order.
"""

from __future__ import unicode_literals

import datetime
import hashlib
import os
import pickle
import sqlite3
import tempfile
from decimal import Decimal

try:
    from collections.abc import Mapping
except ImportError:  # pragma: no cover - Python 2
    from collections import Mapping

import six

from . import Style
from .Row import Row


class RevisionConflict(Exception):
    """An equal revision was seen twice with different row values."""

    def __init__(self, key, revision, accepted_values, incoming_values,
                 accepted_ordinal=None, incoming_ordinal=None):
        Exception.__init__(self)
        self.key = key
        self.revision = revision
        self.accepted_values = accepted_values
        self.incoming_values = incoming_values
        self.accepted_ordinal = accepted_ordinal
        self.incoming_ordinal = incoming_ordinal

    def __str__(self):
        return (
            "conflicting values for key %r at revision %r "
            "(accepted %r, incoming %r)"
            % (self.key, self.revision, self.accepted_values,
               self.incoming_values)
        )

    def __repr__(self):
        return (
            "RevisionConflict(key=%r, revision=%r, accepted=%r, incoming=%r)"
            % (self.key, self.revision, self.accepted_values,
               self.incoming_values)
        )


RevisionConflictError = RevisionConflict


class RevisionWriteResult(object):
    """Outcome returned by :meth:`RevisionRowStream.write`.

    ``status`` is one of ``new``, ``updated``, ``duplicate``, ``stale`` or
    ``conflict``.  ``accepted`` is true for ``new`` and ``updated``.
    """

    __slots__ = (
        "key", "revision", "status", "ordinal", "previous_revision",
        "conflict",
    )

    def __init__(self, key, revision, status, ordinal=None,
                 previous_revision=None, conflict=None):
        self.key = key
        self.revision = revision
        self.status = status
        self.ordinal = ordinal
        self.previous_revision = previous_revision
        self.conflict = conflict

    @property
    def accepted(self):
        return self.status in ("new", "updated")

    @property
    def superseded(self):
        return self.status == "stale"

    def __repr__(self):
        return (
            "RevisionWriteResult(key=%r, revision=%r, status=%r, ordinal=%r)"
            % (self.key, self.revision, self.status, self.ordinal)
        )


def _is_mapping(obj):
    return isinstance(obj, Mapping)


def _as_tuple(values):
    if _is_mapping(values):
        return tuple(values.items())
    if isinstance(values, (list, tuple)):
        return tuple(values)
    return (values,)


def _revision_cmp(left, right):
    """Return -1, 0 or 1 comparing two revisions."""
    if left == right:
        return 0
    try:
        return -1 if left < right else 1
    except TypeError:
        raise TypeError(
            "revision values must be mutually comparable: %r and %r"
            % (left, right)
        )


class RevisionRowStream(object):
    """A disk-backed, revision-aware row buffer for one worksheet.

    :param worksheet:
        The :class:`xlwt.Worksheet.Worksheet` receiving the accepted rows.

    :param key:
        How to obtain the business key from a row.  It may be a callable,
        a mapping key, or (for sequence rows) a column index.  ``None`` is
        allowed when the key is always supplied explicitly to
        :meth:`write`.

    :param revision:
        How to obtain the revision from a row; same forms as ``key``.

    :param columns:
        Optional ordered column description.  For mapping rows it is the
        list of keys to read; for sequence rows it may be a list of integer
        indices.  When omitted, a mapping row's first-seen keys define the
        schema and sequence rows are written as-is.

    :param start_row:
        First worksheet row used by the stream.  When omitted, it is the
        first row after existing worksheet cells (zero for an empty sheet).

    :param start_col:
        First worksheet column used by the stream.

    :param on_conflict:
        ``"record"`` (default) records an equal-revision conflict and keeps
        the first accepted row.  ``"raise"`` records it and raises
        :class:`RevisionConflict` immediately.

    :param style:
        Default style for values.  It may also be a callable taking the
        incoming row and returning a style.
    """

    _STATUS_NEW = "new"
    _STATUS_UPDATED = "updated"
    _STATUS_DUPLICATE = "duplicate"
    _STATUS_STALE = "stale"
    _STATUS_CONFLICT = "conflict"

    def __init__(self, worksheet, key=None, revision=None, columns=None,
                 start_row=None, start_col=0, key_column=None,
                 revision_column=None, on_conflict="record",
                 style=Style.default_style, **kwargs):
        if key is None and key_column is not None:
            key = key_column
        if revision is None and revision_column is not None:
            revision = revision_column
        if on_conflict not in ("record", "raise"):
            raise ValueError("on_conflict must be 'record' or 'raise'")
        if not isinstance(start_col, six.integer_types) or start_col < 0:
            raise ValueError("start_col must be a non-negative integer")

        self.worksheet = worksheet
        self.key = key
        self.revision = revision
        self.columns = list(columns) if columns is not None else None
        self.start_col = int(start_col)
        self.on_conflict = on_conflict
        self.style = style
        self._columns_snapshot = None
        self._on_conflict_hook = kwargs.pop("conflict_handler", None)
        if kwargs:
            raise TypeError("unexpected keyword argument(s): %s"
                            % ", ".join(sorted(kwargs)))

        if start_row is None:
            if worksheet.first_used_row > worksheet.last_used_row:
                start_row = 0
            else:
                start_row = worksheet.last_used_row + 1
        if not isinstance(start_row, six.integer_types) or start_row < 0:
            raise ValueError("start_row must be a non-negative integer")
        if start_row > 65535:
            raise ValueError("start_row is outside the .xls row range")
        self.start_row = int(start_row)

        # A named file is required by sqlite.  The connection uses a small
        # page cache and no journal, so encoded rows stay on disk rather
        # than growing the Python heap with every cell value.
        handle, self._path = tempfile.mkstemp(
            prefix="xlwt-revision-", suffix=".db")
        os.close(handle)
        self._conn = sqlite3.connect(self._path)
        self._conn.execute("PRAGMA journal_mode=OFF")
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute("PRAGMA temp_store=FILE")
        self._conn.execute("PRAGMA cache_size=-2048")
        self._conn.execute(
            "CREATE TABLE rows ("
            "key BLOB PRIMARY KEY, "
            "ordinal INTEGER NOT NULL UNIQUE, "
            "revision BLOB NOT NULL, "
            "digest BLOB NOT NULL, "
            "row_values BLOB NOT NULL, "
            "record BLOB NOT NULL, "
            "sst_ids BLOB NOT NULL)"
        )
        self._conn.execute(
            "CREATE TABLE conflicts ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "key BLOB NOT NULL, "
            "revision BLOB NOT NULL, "
            "incoming BLOB NOT NULL, "
            "accepted_ordinal INTEGER, "
            "incoming_ordinal INTEGER)"
        )
        self._next_ordinal = 0
        self._max_width = 0
        self._pending = 0
        self._closed = False
        self._registered = False

        register = getattr(worksheet, "_register_revision_stream", None)
        if register is not None:
            register(self)
            self._registered = True

    @property
    def end_row_exclusive(self):
        """One past the last worksheet row this stream can occupy."""
        return self.start_row + self._next_ordinal

    @property
    def max_col(self):
        """The highest worksheet column used by this stream."""
        if not self._max_width:
            return self.start_col
        return self.start_col + self._max_width - 1

    # -- lifecycle ----------------------------------------------------

    def flush(self):
        """Persist pending rows to the temporary database."""
        self._check_open()
        self._conn.commit()

    def close(self):
        """Stop accepting writes.

        Accepted rows remain available to the worksheet and are still
        written by the next save.  Use :meth:`discard` to release the
        temporary database immediately instead.
        """
        if self._closed:
            return
        self._closed = True
        if self._conn is not None:
            self._conn.commit()

    def discard(self):
        """Close and remove the temporary database.

        Rows are no longer available for serialisation after this call.
        Shared-string entries registered for those rows are released so a
        later save does not carry orphaned text.
        """
        if self._registered:
            unregister = getattr(
                self.worksheet, "_unregister_revision_stream", None)
            if unregister is not None:
                unregister(self)
            self._registered = False
        self._closed = True
        if self._conn is not None:
            try:
                self._conn.commit()
                cursor = self._conn.execute("SELECT sst_ids FROM rows")
                for (sst_blob,) in cursor.fetchall():
                    if sst_blob:
                        self._release_sst_ids(sst_blob)
                self._conn.commit()
            finally:
                self._conn.close()
                self._conn = None
        try:
            os.unlink(self._path)
        except OSError:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()
        return False

    def __del__(self):
        try:
            self.discard()
        except Exception:
            pass

    def _check_open(self):
        if self._closed:
            raise ValueError("revision row stream is closed")

    def _check_available(self):
        if self._conn is None:
            raise ValueError("revision row stream has been discarded")

    # -- row interpretation -------------------------------------------

    def _extract(self, spec, row, what):
        if spec is None:
            raise ValueError(
                "%s was not supplied and no %s extractor was configured"
                % (what, what)
            )
        if callable(spec):
            return spec(row)
        if _is_mapping(row):
            return row[spec]
        if isinstance(spec, six.integer_types):
            try:
                return row[spec]
            except (IndexError, TypeError):
                pass
        raise ValueError("cannot obtain %s from row %r" % (what, row))

    def _resolve_columns(self, row):
        if self.columns is not None:
            return self.columns
        if _is_mapping(row):
            if self._columns_snapshot is None:
                self._columns_snapshot = list(row.keys())
            return self._columns_snapshot
        return None

    def _values_from_row(self, row):
        columns = self._resolve_columns(row)
        if columns is None:
            return tuple(row)
        if _is_mapping(row):
            missing = [name for name in columns if name not in row]
            if missing:
                raise ValueError(
                    "row is missing stream column(s) %r" % (missing,))
            return tuple(row[name] for name in columns)
        if all(isinstance(index, six.integer_types) for index in columns):
            return tuple(row[index] for index in columns)
        return tuple(row)

    def _style_for(self, row, style):
        if style is not None:
            return style
        if callable(self.style):
            return self.style(row)
        if self.style is None:
            return Style.default_style
        return self.style

    # -- public write API ---------------------------------------------

    def write(self, row=None, revision=None, values=None,
              style=None, key=None):
        """Accept one inventory row.

        Supported call forms::

            stream.write(row)
            stream.write(row, style=style)
            stream.write(key, revision, values, style=style)
            stream.write(values, key=key, revision=revision)
            stream.write(row=row, key=key, revision=revision)

        The first form requires ``key`` and ``revision`` extractors to have
        been configured.  Every form returns a
        :class:`RevisionWriteResult` with ``status`` set to ``new``,
        ``updated``, ``duplicate``, ``stale`` or ``conflict``.

        A stale row (lower revision than the accepted one) is ignored.  An
        equal revision with different values is recorded in
        :attr:`conflicts`; with ``on_conflict="raise"`` it also raises
        :class:`RevisionConflict`.  The first accepted row always keeps its
        position, even when a later revision replaces its contents.
        """
        self._check_open()

        # ``write(key, revision, values)`` is distinguishable because a key
        # is normally a scalar, not a mapping or sequence row.
        positional_key = all((
            values is not None,
            revision is not None,
            key is None,
            row is not None,
            not _is_mapping(row),
            not isinstance(row, (list, tuple, dict)),
        ))
        if positional_key:
            key = row
            row = None

        if values is None:
            if row is None:
                raise ValueError("either row or values must be supplied")
            values = self._values_from_row(row)
        else:
            values = _as_tuple(values)

        if key is None:
            if row is None:
                raise ValueError("key must be supplied")
            key = self._extract(self.key, row, "key")
        if revision is None:
            if row is None:
                raise ValueError("revision must be supplied")
            revision = self._extract(self.revision, row, "revision")

        style = self._style_for(row, style)
        return self._accept(key, revision, values, style)

    add = write
    append = write
    upsert = write

    # -- storage ------------------------------------------------------

    def _accept(self, key, revision, values, style):
        key_blob = pickle.dumps(key, protocol=2)
        revision_blob = pickle.dumps(revision, protocol=2)
        values_blob = pickle.dumps(values, protocol=2)

        current = self._conn.execute(
            "SELECT revision, digest, ordinal, record, sst_ids FROM rows "
            "WHERE key = ?",
            (sqlite3.Binary(key_blob),)
        ).fetchone()

        if current is None:
            ordinal = self._next_ordinal
            self._next_ordinal += 1
            record, sst_ids = self._encode_row(ordinal, values, style)
            digest = hashlib.sha256(self._signature(values)).digest()
            self._conn.execute(
                "INSERT INTO rows "
                "(key, ordinal, revision, digest, row_values, record, sst_ids) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (sqlite3.Binary(key_blob), ordinal,
                 sqlite3.Binary(revision_blob), sqlite3.Binary(digest),
                 sqlite3.Binary(values_blob), sqlite3.Binary(record),
                 sqlite3.Binary(pickle.dumps(sst_ids, protocol=2)))
            )
            self._max_width = max(self._max_width, len(values))
            self._notify_extent(ordinal)
            self._maybe_commit()
            return RevisionWriteResult(
                key, revision, self._STATUS_NEW, ordinal=ordinal)

        current_revision, current_digest, ordinal, old_record, old_sst = current
        current_revision = pickle.loads(current_revision)
        current_digest = bytes(current_digest)

        cmp = _revision_cmp(revision, current_revision)
        if cmp < 0:
            return RevisionWriteResult(
                key, revision, self._STATUS_STALE, ordinal=ordinal,
                previous_revision=current_revision)

        if cmp == 0:
            digest = hashlib.sha256(self._signature(values)).digest()
            if digest == current_digest:
                return RevisionWriteResult(
                    key, revision, self._STATUS_DUPLICATE, ordinal=ordinal,
                    previous_revision=current_revision)
            accepted_values = self._load_values(key_blob)
            conflict = RevisionConflict(
                key, revision, accepted_values, values,
                accepted_ordinal=ordinal, incoming_ordinal=ordinal)
            self._conn.execute(
                "INSERT INTO conflicts "
                "(key, revision, incoming, accepted_ordinal, incoming_ordinal) "
                "VALUES (?, ?, ?, ?, ?)",
                (sqlite3.Binary(key_blob), sqlite3.Binary(revision_blob),
                 sqlite3.Binary(values_blob), ordinal, ordinal)
            )
            if self.on_conflict == "raise":
                raise conflict
            if self._on_conflict_hook is not None:
                self._on_conflict_hook(conflict)
            self._maybe_commit()
            return RevisionWriteResult(
                key, revision, self._STATUS_CONFLICT, ordinal=ordinal,
                previous_revision=current_revision, conflict=conflict)

        record, sst_ids = self._encode_row(ordinal, values, style)
        digest = hashlib.sha256(self._signature(values)).digest()
        self._conn.execute(
            "UPDATE rows SET revision = ?, digest = ?, row_values = ?, "
            "record = ?, sst_ids = ? WHERE key = ?",
            (sqlite3.Binary(revision_blob), sqlite3.Binary(digest),
             sqlite3.Binary(values_blob), sqlite3.Binary(record),
             sqlite3.Binary(pickle.dumps(sst_ids, protocol=2)),
             sqlite3.Binary(key_blob))
        )
        self._release_sst_ids(old_sst)
        self._max_width = max(self._max_width, len(values))
        self._notify_extent(ordinal)
        self._maybe_commit()
        return RevisionWriteResult(
            key, revision, self._STATUS_UPDATED, ordinal=ordinal,
            previous_revision=current_revision)

    def _maybe_commit(self):
        self._pending += 1
        if self._pending >= 64:
            self._conn.commit()
            self._pending = 0

    def _encode_row(self, ordinal, values, style):
        rowx = self.start_row + ordinal
        if rowx > 65535:
            raise ValueError("revision row exceeds the .xls row limit")
        if self.start_col + len(values) > 256:
            raise ValueError("revision row exceeds the .xls column limit")
        if not values:
            raise ValueError("revision row has no values")
        row = Row(rowx, self.worksheet)
        for offset, value in enumerate(values):
            row.write(self.start_col + offset, value, style)
        record = row.get_row_biff_data() + row.get_cells_biff_data()
        sst_ids = tuple(
            cell.sst_idx for cell in row.get_cell_objects()
            if getattr(cell, "sst_idx", None) is not None)
        return record, sst_ids

    def _signature(self, values):
        # Compare what the caller supplied, normalised the way the writer
        # normalises it.  Strings that encode identically are duplicates;
        # equal revisions with different displayed values are conflicts.
        pieces = []
        for value in values:
            if isinstance(value, bool):
                pieces.append(("b", value))
            elif isinstance(value, six.integer_types + (float,)):
                pieces.append(("n", float(value)))
            elif isinstance(value, Decimal):
                pieces.append(("n", float(value)))
            elif isinstance(value, (datetime.datetime, datetime.date,
                                    datetime.time)):
                pieces.append(("d", value))
            elif value is None:
                pieces.append(("blank",))
            elif isinstance(value, six.string_types):
                pieces.append(("s", value))
            else:
                pieces.append((type(value).__name__,
                               getattr(value, "text", lambda: value)()))
        return pickle.dumps(tuple(pieces), protocol=2)

    def _release_sst_ids(self, old_sst_blob):
        if not old_sst_blob:
            return
        workbook = self.worksheet.get_parent()
        for sst_idx in pickle.loads(old_sst_blob):
            workbook.del_str(sst_idx)

    def _notify_extent(self, ordinal):
        note = getattr(self.worksheet, "_note_revision_extent", None)
        if note is not None:
            note(self.start_row + ordinal, self.start_col, self._max_width)

    def _load_values(self, key_blob):
        row = self._conn.execute(
            "SELECT row_values FROM rows WHERE key = ?",
            (sqlite3.Binary(key_blob),)
        ).fetchone()
        return pickle.loads(row[0]) if row else None

    def _load_rows(self):
        self._conn.commit()
        cursor = self._conn.execute(
            "SELECT ordinal, record FROM rows ORDER BY ordinal")
        while True:
            chunk = cursor.fetchmany(64)
            if not chunk:
                break
            for ordinal, record in chunk:
                yield ordinal, bytes(record)

    def _load_conflicts(self, include_values=True):
        self._conn.commit()
        cursor = self._conn.execute(
            "SELECT key, revision, incoming, accepted_ordinal, "
            "incoming_ordinal FROM conflicts ORDER BY id")
        result = []
        while True:
            chunk = cursor.fetchmany(64)
            if not chunk:
                break
            for key_blob, revision_blob, incoming_blob, \
                    accepted_ordinal, incoming_ordinal in chunk:
                key = pickle.loads(key_blob)
                revision = pickle.loads(revision_blob)
                incoming_values = pickle.loads(incoming_blob)
                accepted_values = (self._load_values(key_blob)
                                   if include_values else None)
                result.append(RevisionConflict(
                    key, revision, accepted_values, incoming_values,
                    accepted_ordinal=accepted_ordinal,
                    incoming_ordinal=incoming_ordinal))
        return result

    # -- inspection ---------------------------------------------------

    @property
    def conflicts(self):
        """All equal-revision conflicts, in arrival order."""
        return self._load_conflicts(include_values=True)

    def get_conflicts(self, include_values=True):
        return self._load_conflicts(include_values=include_values)

    @property
    def conflict_count(self):
        self._conn.commit()
        return self._conn.execute(
            "SELECT COUNT(*) FROM conflicts").fetchone()[0]

    @property
    def row_count(self):
        self._conn.commit()
        return self._conn.execute("SELECT COUNT(*) FROM rows").fetchone()[0]

    # -- finalisation -------------------------------------------------

    def iter_row_biff_data(self):
        """Yield ``(rowx, biff_data)`` for accepted rows, first-seen first.

        Rows are read one chunk at a time, so only a small number of
        encoded records are alive at any moment.
        """
        self._check_available()
        for ordinal, record in self._load_rows():
            yield self.start_row + ordinal, record


# Descriptive aliases; RevisionRowStream is the primary public class.
InventoryRowStream = RevisionRowStream
RevisionedRowStream = RevisionRowStream
