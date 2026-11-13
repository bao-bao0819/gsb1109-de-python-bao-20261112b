"""
Repeatable, resumable incremental reads from SQLite into agate.

A plain ``WHERE updated_at > :last_seen`` pagination has two failure modes
against a source that keeps changing while it is being read:

1. Rows that share a timestamp with the last delivered row are skipped or
   re-delivered depending on how the bound is written.
2. A row that has not been read yet can have its timestamp moved *behind* the
   watermark, so the next batch never sees it. The reader reports success
   while silently dropping data.

This module removes both failure modes with an explicit, hand-off-able batch
boundary:

* Every batch is read from a point-in-time **snapshot** of the SQLite database
  (an online backup, not a long-running read transaction). Concurrent writes
  to the live database, including timestamp rewinds, cannot change what a
  batch contains.
* Ordering and resume use a composite keyset, ``(timestamp_column,
  key_columns...)``, so rows that share a timestamp are never split
  ambiguously across pages.
* The :class:`IncrementalCursor` is fully serializable and records which
  snapshot file it belongs to, the snapshot fingerprint, and the exact last
  delivered keyset position. Another process can resume from it or replay the
  exact batch.

The contract is enforced *before* any cursor is handed out. If the source
cannot sustain these semantics (non-SQLite backend, duplicate or NULL keys,
NULL timestamps, mixed timestamp storage types, cursor/snapshot mismatch) an
:class:`IncrementalReadError` subclass is raised rather than returning a
continuation point that could silently lose rows.

Semantics: no data is ever skipped within a snapshot; a row changed on the
live database after a snapshot was taken is re-read on a later snapshot run,
so delivery across runs is at-least-once.
"""

import base64
import datetime
import decimal
import hashlib
import json
import os
import sqlite3
import tempfile
from dataclasses import dataclass

import agate
from sqlalchemy import Engine, MetaData, Table, and_, create_engine, func, or_, select
from sqlalchemy.engine import Connection, make_url

_CURSOR_VERSION = 1


class IncrementalReadError(Exception):
    """Base class for failures that prevent a safe incremental cursor."""


class UnsupportedSourceError(IncrementalReadError):
    """The source cannot provide the required point-in-time snapshot semantics."""


class CursorContractError(IncrementalReadError):
    """The table or cursor does not satisfy the keyset pagination contract."""


class SnapshotMismatchError(IncrementalReadError):
    """A cursor was presented against a snapshot it was not created from."""


def _normalize_key_columns(key_columns):
    if isinstance(key_columns, str):
        return (key_columns,)
    columns = tuple(key_columns)
    if not columns:
        raise CursorContractError('At least one key column is required.')
    return columns


def _check_sqlite_engine_or_connection(engine_or_connection):
    if isinstance(engine_or_connection, Connection):
        if engine_or_connection.engine.dialect.name != 'sqlite':
            raise UnsupportedSourceError(
                'Incremental reads require a SQLite source; got dialect %r.'
                % engine_or_connection.engine.dialect.name)
        return
    if isinstance(engine_or_connection, Engine):
        if engine_or_connection.dialect.name != 'sqlite':
            raise UnsupportedSourceError(
                'Incremental reads require a SQLite source; got dialect %r.'
                % engine_or_connection.dialect.name)
        return
    dialect_name = getattr(getattr(engine_or_connection, 'dialect', None), 'name', None)
    if dialect_name is not None:
        raise UnsupportedSourceError(
            'Incremental reads require a SQLite source; got dialect %r.' % dialect_name)
    raise UnsupportedSourceError('Unsupported source object: %r' % type(engine_or_connection))


def _backup_to(source, snapshot_path):
    if isinstance(source, Engine):
        sa_connection = source.connect()
        try:
            raw_source = sa_connection.connection.dbapi_connection
            _guard_distinct_database(raw_source, snapshot_path)
            destination = sqlite3.connect(snapshot_path)
            try:
                raw_source.backup(destination)
            finally:
                destination.close()
        finally:
            sa_connection.close()
        return
    raw_source = source.connection.dbapi_connection
    _guard_distinct_database(raw_source, snapshot_path)
    destination = sqlite3.connect(snapshot_path)
    try:
        raw_source.backup(destination)
    finally:
        destination.close()


def _guard_distinct_database(raw_source, snapshot_path):
    source_database = raw_source.execute('PRAGMA database_list').fetchall()
    source_files = {os.path.abspath(row[2]) for row in source_database if row[2] and row[2] != ''}
    if os.path.abspath(snapshot_path) in source_files:
        raise UnsupportedSourceError(
            'The snapshot path must be a separate file from the live database; '
            '%r is the live database itself.' % snapshot_path)


@dataclass(frozen=True)
class Snapshot:
    """A point-in-time SQLite database file and a connection engine to it."""

    engine: Engine
    path: str
    owns_file: bool
    file_token: str

    def dispose(self):
        self.engine.dispose()
        if self.owns_file:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def open_snapshot(source, snapshot_path=None, reuse_existing=False):
    """
    Create a point-in-time SQLite snapshot using the online backup API.

    The backup is a consistent database image taken at one instant; readers of
    the snapshot are completely isolated from later writes (including
    timestamp rewinds) on the live database.

    :param source:
        A SQLAlchemy sqlite ``Engine``/``Connection``, a ``sqlite://``
        connection string, or a filesystem path to a SQLite database.
    :param snapshot_path:
        Where to write the snapshot. When omitted, a temporary file is created
        and deleted by :meth:`Snapshot.dispose`. Provide a persistent path when
        the cursor must be handed to another process.
    :param reuse_existing:
        When ``source`` and ``snapshot_path`` name the same existing file, open
        it directly instead of copying. Used when reopening a snapshot
        referenced by an existing cursor; never use it against the live
        database.
    """
    owns_file = False
    if snapshot_path is None:
        fd, snapshot_path = tempfile.mkstemp(prefix='agatesql-snapshot-', suffix='.sqlite')
        os.close(fd)
        owns_file = True

    reuse_in_place = (
        reuse_existing
        and isinstance(source, str) and '://' not in source
        and os.path.abspath(source) == os.path.abspath(snapshot_path)
        and os.path.exists(snapshot_path))

    if not reuse_in_place and isinstance(source, (Engine, Connection)):
        _check_sqlite_engine_or_connection(source)
        _backup_to(source, snapshot_path)
    elif not reuse_in_place and isinstance(source, str):
        parsed = make_url(source) if '://' in source else None
        scheme = parsed.get_backend_name() if parsed is not None else None
        if scheme is not None and scheme != 'sqlite':
            raise UnsupportedSourceError(
                'Incremental reads require a SQLite source; got connection string %r.' % source)
        database = parsed.database if parsed is not None else source
        if not database or database == ':memory:':
            raise UnsupportedSourceError(
                'An in-memory SQLite database cannot be reopened from a URL. '
                'Pass its SQLAlchemy Engine or Connection instead so its contents '
                'can be backed up to a snapshot file.')
        if os.path.abspath(database) == os.path.abspath(snapshot_path):
            raise UnsupportedSourceError(
                'The snapshot path must be a separate file from the live database; '
                '%r is the live database itself.' % snapshot_path)
        raw_source = sqlite3.connect(database)
        try:
            destination = sqlite3.connect(snapshot_path)
            try:
                raw_source.backup(destination)
            finally:
                destination.close()
        finally:
            raw_source.close()
    elif not reuse_in_place:
        _check_sqlite_engine_or_connection(source)

    absolute_path = os.path.abspath(snapshot_path)
    stat = os.stat(absolute_path)
    file_token = hashlib.sha256(
        ('%s:%d:%d' % (absolute_path, stat.st_size, stat.st_mtime_ns)).encode('utf-8')
    ).hexdigest()[:16]
    engine = create_engine('sqlite:///%s' % absolute_path)
    return Snapshot(engine=engine, path=absolute_path, owns_file=owns_file, file_token=file_token)


@dataclass(frozen=True)
class IncrementalCursor:
    """
    An immutable, serializable continuation point.

    The cursor positions the reader at one specific row of one specific
    snapshot. It is exclusive: resuming from it returns rows strictly after
    ``(last_timestamp, last_key)``.
    """

    table_name: str
    key_columns: tuple
    timestamp_column: str
    snapshot_path: str
    snapshot_fingerprint: str
    last_timestamp: object = None
    last_key: tuple = None
    exhausted: bool = False
    version: int = _CURSOR_VERSION

    def to_dict(self):
        return {
            'version': self.version,
            'table_name': self.table_name,
            'key_columns': list(self.key_columns),
            'timestamp_column': self.timestamp_column,
            'snapshot_path': self.snapshot_path,
            'snapshot_fingerprint': self.snapshot_fingerprint,
            'last_timestamp': self.last_timestamp,
            'last_key': list(self.last_key) if self.last_key is not None else None,
            'exhausted': self.exhausted,
        }

    def to_token(self):
        payload = json.dumps(self.to_dict(), separators=(',', ':'), sort_keys=True).encode('utf-8')
        return base64.urlsafe_b64encode(payload).decode('ascii')

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or data.get('version') != _CURSOR_VERSION:
            raise CursorContractError('Unrecognized or incompatible cursor payload.')
        try:
            return cls(
                table_name=data['table_name'],
                key_columns=tuple(data['key_columns']),
                timestamp_column=data['timestamp_column'],
                snapshot_path=data['snapshot_path'],
                snapshot_fingerprint=data['snapshot_fingerprint'],
                last_timestamp=data['last_timestamp'],
                last_key=tuple(data['last_key']) if data['last_key'] is not None else None,
                exhausted=bool(data['exhausted']),
            )
        except (KeyError, TypeError) as exc:
            raise CursorContractError('Malformed cursor payload: %s' % exc)

    @classmethod
    def from_token(cls, token):
        try:
            payload = base64.urlsafe_b64decode(token.encode('ascii'))
            return cls.from_dict(json.loads(payload.decode('utf-8')))
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CursorContractError('Malformed cursor token: %s' % exc)


@dataclass(frozen=True)
class BatchResult:
    """The agate table of one batch plus the boundaries needed to hand it off."""

    table: agate.Table
    start_cursor: object
    next_cursor: IncrementalCursor
    snapshot_path: str
    snapshot_fingerprint: str
    is_final: bool

    @property
    def rows_returned(self):
        return len(self.table.rows)

    def handoff_document(self):
        """
        A JSON-serializable envelope another process can use.

        It pins the source state (snapshot file plus fingerprint), the exact
        batch boundaries (``start`` exclusive, ``next`` exclusive), and the
        keyset contract.
        """
        return {
            'snapshot_path': self.snapshot_path,
            'snapshot_fingerprint': self.snapshot_fingerprint,
            'table_name': self.next_cursor.table_name,
            'key_columns': list(self.next_cursor.key_columns),
            'timestamp_column': self.next_cursor.timestamp_column,
            'start': self.start_cursor.to_token() if self.start_cursor is not None else None,
            'next': self.next_cursor.to_token(),
            'exhausted': self.is_final,
            'rows_returned': self.rows_returned,
        }


def _agate_type_for(sql_column):
    python_type = sql_column.type.python_type
    if python_type in (int, float, decimal.Decimal):
        return agate.Number()
    if python_type is bool:
        return agate.Boolean()
    if issubclass(python_type, str):
        return agate.Text()
    if python_type is datetime.datetime:
        return agate.DateTime()
    if python_type is datetime.date:
        return agate.Date()
    if python_type is datetime.timedelta:
        return agate.TimeDelta()
    raise CursorContractError('Unsupported sqlalchemy column type: %s' % type(sql_column.type))


def _row_greater_than(columns, values):
    condition = columns[-1] > values[-1]
    for index in range(len(columns) - 2, -1, -1):
        condition = or_(columns[index] > values[index],
                        and_(columns[index] == values[index], condition))
    return condition


def _row_less_than_or_equal(columns, values):
    condition = columns[-1] <= values[-1]
    for index in range(len(columns) - 2, -1, -1):
        condition = or_(columns[index] < values[index],
                        and_(columns[index] == values[index], condition))
    return condition


class IncrementalReader:
    """
    Reads a SQLite table in resumable batches against an immutable snapshot.

    Use as a context manager so the snapshot (and any temporary file backing
    it) is disposed afterwards::

        with IncrementalReader(engine, 'events', ('id',), 'updated_at',
                               snapshot_path='/snap/run-1.sqlite') as reader:
            result = reader.read_batch()
            token = result.next_cursor.to_token()
            result = reader.read_batch(IncrementalCursor.from_token(token))
    """

    def __init__(self, source, table_name, key_columns, timestamp_column, *,
                 batch_size=1000, snapshot_path=None):
        if not isinstance(table_name, str) or not table_name:
            raise CursorContractError('A table name is required.')
        self.source = source
        self.table_name = table_name
        self.key_columns = _normalize_key_columns(key_columns)
        self.timestamp_column = timestamp_column
        if not isinstance(timestamp_column, str) or not timestamp_column:
            raise CursorContractError('A timestamp column is required.')
        if not isinstance(batch_size, int) or batch_size < 1:
            raise CursorContractError('batch_size must be a positive integer.')
        self.batch_size = batch_size
        self.requested_snapshot_path = snapshot_path
        self.reuse_existing_snapshot = False
        self.snapshot = None
        self.sql_table = None
        self.column_names = None
        self.column_types = None
        self.fingerprint = None

    def __enter__(self):
        if self.snapshot is not None:
            return self
        self.snapshot = open_snapshot(
            self.source, self.requested_snapshot_path,
            reuse_existing=self.reuse_existing_snapshot)
        connection = self.snapshot.engine.connect()
        try:
            metadata = MetaData()
            self.sql_table = Table(self.table_name, metadata, autoload_with=connection)
            self._validate_contract(connection)
            self.fingerprint = self._fingerprint(connection)
        except Exception:
            connection.close()
            self.snapshot.dispose()
            raise
        else:
            connection.close()
        self.column_names = [column.name for column in self.sql_table.columns]
        self.column_types = [_agate_type_for(column) for column in self.sql_table.columns]
        return self

    def __exit__(self, exc_type, exc, traceback):
        if self.snapshot is not None:
            self.snapshot.dispose()
            self.snapshot = None
        return False

    @classmethod
    def for_cursor(cls, cursor, *, batch_size=1000):
        """
        Open the snapshot recorded in a cursor in a new process/reader.

        Raises :class:`SnapshotMismatchError` if the snapshot file is missing
        or its fingerprint no longer matches the cursor.
        """
        if not isinstance(cursor, IncrementalCursor):
            raise CursorContractError('for_cursor expects an IncrementalCursor.')
        if not os.path.exists(cursor.snapshot_path):
            raise SnapshotMismatchError(
                'Snapshot file %r referenced by the cursor no longer exists.' % cursor.snapshot_path)
        reader = cls(cursor.snapshot_path, cursor.table_name, cursor.key_columns,
                     cursor.timestamp_column, batch_size=batch_size,
                     snapshot_path=cursor.snapshot_path)
        reader.reuse_existing_snapshot = True
        reader.__enter__()
        if reader.fingerprint != cursor.snapshot_fingerprint:
            reader.__exit__(None, None, None)
            raise SnapshotMismatchError(
                'Snapshot fingerprint %r does not match cursor fingerprint %r. '
                'The snapshot file is not the image this cursor was created from.'
                % (reader.fingerprint, cursor.snapshot_fingerprint))
        return reader

    def _column(self, name):
        try:
            return self.sql_table.c[name]
        except KeyError:
            raise CursorContractError(
                'Column %r does not exist on table %r.' % (name, self.table_name))

    def _validate_contract(self, connection):
        timestamp_column = self._column(self.timestamp_column)
        key_columns = [self._column(name) for name in self.key_columns]

        null_timestamp = connection.execute(
            select(func.count()).select_from(self.sql_table).where(timestamp_column.is_(None))
        ).scalar()
        if null_timestamp:
            raise CursorContractError(
                'Timestamp column %r contains %d NULL value(s). Keyset pagination '
                'requires deterministic, non-NULL timestamps; backfill or coalesce '
                'the NULLs before requesting an incremental cursor.'
                % (self.timestamp_column, null_timestamp))

        for column in key_columns:
            null_keys = connection.execute(
                select(func.count()).select_from(self.sql_table).where(column.is_(None))
            ).scalar()
            if null_keys:
                raise CursorContractError(
                    'Key column %r contains %d NULL value(s); a resume key must be '
                    'uniquely and fully populated.' % (column.name, null_keys))

        storage_types = [
            row[0] for row in connection.execute(
                select(func.typeof(timestamp_column).label('storage_type'))
                .select_from(self.sql_table).distinct())
        ]
        if len(storage_types) > 1:
            raise CursorContractError(
                'Timestamp column %r mixes SQLite storage types %r; chronological '
                'ordering across mixed types is not defined. Normalize the column '
                'to one storage type first.' % (self.timestamp_column, storage_types))

        duplicate = connection.execute(
            select(*key_columns)
            .group_by(*key_columns)
            .having(func.count() > 1)
            .limit(1)
        ).first()
        if duplicate is not None:
            raise CursorContractError(
                'Key column(s) %s are not unique on table %r; first duplicate key: %r. '
                'Add columns until the key is unique, otherwise a resume position '
                'cannot be made unambiguous.' % (list(self.key_columns), self.table_name,
                                                 tuple(duplicate)))

    def _fingerprint(self, connection):
        timestamp_column = self._column(self.timestamp_column)
        count, minimum, maximum = connection.execute(
            select(func.count(), func.min(timestamp_column), func.max(timestamp_column))
            .select_from(self.sql_table)
        ).one()
        schema_version = connection.exec_driver_sql('PRAGMA schema_version').scalar()
        digest = hashlib.sha256(
            ('%s|%s|%s|%s|%s' % (self.snapshot.file_token, schema_version,
                                 count, minimum, maximum)).encode('utf-8')
        ).hexdigest()[:16]
        return '%s:%s' % (self.snapshot.file_token, digest)

    def _check_cursor(self, cursor):
        if cursor is None:
            return
        if (cursor.table_name != self.table_name
                or cursor.key_columns != self.key_columns
                or cursor.timestamp_column != self.timestamp_column):
            raise CursorContractError(
                'Cursor was created for table %r with keyset (%s, %s) but this reader '
                'is configured for table %r with keyset (%s, %s).'
                % (cursor.table_name, cursor.timestamp_column, list(cursor.key_columns),
                   self.table_name, self.timestamp_column, list(self.key_columns)))
        if cursor.snapshot_path != self.snapshot.path:
            raise SnapshotMismatchError(
                'Cursor belongs to snapshot %r but the reader opened %r.'
                % (cursor.snapshot_path, self.snapshot.path))
        if cursor.snapshot_fingerprint != self.fingerprint:
            raise SnapshotMismatchError(
                'Snapshot fingerprint mismatch: cursor %r vs reader %r.'
                % (cursor.snapshot_fingerprint, self.fingerprint))

    def _build_select(self, start_cursor, end_cursor, limit):
        statement = select(self.sql_table)
        conditions = []
        ordering_columns = [self._column(self.timestamp_column)] + [
            self._column(name) for name in self.key_columns]

        if start_cursor is not None and start_cursor.last_timestamp is not None:
            conditions.append(or_(
                ordering_columns[0] > start_cursor.last_timestamp,
                and_(ordering_columns[0] == start_cursor.last_timestamp,
                     _row_greater_than(ordering_columns[1:], list(start_cursor.last_key)))))
        if end_cursor is not None and end_cursor.last_timestamp is not None:
            conditions.append(or_(
                ordering_columns[0] < end_cursor.last_timestamp,
                and_(ordering_columns[0] == end_cursor.last_timestamp,
                     _row_less_than_or_equal(ordering_columns[1:], list(end_cursor.last_key)))))
        if conditions:
            statement = statement.where(and_(*conditions))
        statement = statement.order_by(*[column.asc() for column in ordering_columns])
        if limit is not None:
            statement = statement.limit(limit)
        return statement

    def read_batch(self, cursor=None):
        """
        Return the next :class:`BatchResult`, optionally resuming from ``cursor``.

        The returned table holds at most ``batch_size`` rows from the immutable
        snapshot and ``next_cursor`` is the exclusive continuation point.
        Reading an exhausted cursor is idempotent and returns an empty batch.
        """
        self._check_cursor(cursor)
        if cursor is not None and cursor.exhausted:
            empty = agate.Table([], self.column_names, self.column_types)
            return BatchResult(empty, cursor, cursor, self.snapshot.path,
                               self.fingerprint, True)

        connection = self.snapshot.engine.connect()
        try:
            statement = self._build_select(cursor, None, self.batch_size + 1)
            fetched = [tuple(row) for row in connection.execute(statement).all()]
        finally:
            connection.close()

        has_more = len(fetched) > self.batch_size
        page_rows = fetched[:self.batch_size]
        table = agate.Table(page_rows, self.column_names, self.column_types)

        if not page_rows:
            next_cursor = IncrementalCursor(
                self.table_name, self.key_columns, self.timestamp_column,
                self.snapshot.path, self.fingerprint,
                last_timestamp=None if cursor is None else cursor.last_timestamp,
                last_key=None if cursor is None else cursor.last_key,
                exhausted=True)
            return BatchResult(table, cursor, next_cursor, self.snapshot.path,
                               self.fingerprint, True)

        timestamp_index = self.column_names.index(self.timestamp_column)
        key_indexes = [self.column_names.index(name) for name in self.key_columns]
        last_row = page_rows[-1]
        next_cursor = IncrementalCursor(
            self.table_name, self.key_columns, self.timestamp_column,
            self.snapshot.path, self.fingerprint,
            last_timestamp=last_row[timestamp_index],
            last_key=tuple(last_row[index] for index in key_indexes),
            exhausted=not has_more)
        return BatchResult(table, cursor, next_cursor, self.snapshot.path,
                           self.fingerprint, not has_more)

    def replay_batch(self, start_cursor, end_cursor):
        """
        Reconstruct the exact rows of a previously delivered batch.

        Boundary cursors come from :meth:`BatchResult.handoff_document`; the
        start cursor is exclusive and the end cursor is inclusive.
        """
        self._check_cursor(start_cursor)
        self._check_cursor(end_cursor)
        connection = self.snapshot.engine.connect()
        try:
            statement = self._build_select(start_cursor, end_cursor, None)
            rows = [tuple(row) for row in connection.execute(statement).all()]
        finally:
            connection.close()
        return agate.Table(rows, self.column_names, self.column_types)


def read_incremental_batch(source, table_name, key_columns, timestamp_column, *,
                           cursor=None, batch_size=1000, snapshot_path=None):
    """
    One-shot convenience wrapper around :class:`IncrementalReader`.

    Without a cursor, a fresh point-in-time snapshot of ``source`` is opened and
    the first batch is returned. Pass a persistent ``snapshot_path`` whenever
    the returned cursor may be used by another process; otherwise the snapshot
    is a temporary file that is deleted when this call returns.

    With a cursor, the call resumes against the snapshot recorded in that
    cursor. ``source`` may be ``None`` or identify that same snapshot file;
    pointing it at the live database raises :class:`SnapshotMismatchError`,
    because a continuation only has meaning against its original source state.
    """
    if cursor is not None:
        if snapshot_path is not None and os.path.abspath(snapshot_path) != cursor.snapshot_path:
            raise SnapshotMismatchError(
                'snapshot_path %r does not match the cursor snapshot %r.'
                % (snapshot_path, cursor.snapshot_path))
        if source is not None:
            if isinstance(source, (Engine, Connection)):
                raise CursorContractError(
                    'Resuming from a cursor via this helper requires the snapshot '
                    'path (or None); use IncrementalReader.for_cursor with an engine.')
            if '://' in source:
                parsed = make_url(source)
                if parsed.get_backend_name() != 'sqlite':
                    raise UnsupportedSourceError(
                        'Incremental reads require a SQLite source; got %r.' % source)
                source_path = os.path.abspath(parsed.database)
            else:
                source_path = os.path.abspath(source)
            if source_path != cursor.snapshot_path:
                raise SnapshotMismatchError(
                    'Resume source %r is not the cursor snapshot %r. Point the call '
                    'at the handed-off snapshot, not the live database.'
                    % (source, cursor.snapshot_path))
        with IncrementalReader.for_cursor(cursor, batch_size=batch_size) as reader:
            return reader.read_batch(cursor)

    with IncrementalReader(source, table_name, key_columns, timestamp_column,
                           batch_size=batch_size, snapshot_path=snapshot_path) as reader:
        return reader.read_batch()


def replay_batch(snapshot_path, start_cursor, end_cursor):
    """
    Replay a batch in a separate process from its snapshot and boundary cursors.

    ``snapshot_path`` must match the file recorded in both cursors; it is
    accepted explicitly so callers can verify the handoff path before opening
    anything.
    """
    if snapshot_path != end_cursor.snapshot_path:
        raise SnapshotMismatchError(
            'Requested snapshot %r does not match the end cursor snapshot %r.'
            % (snapshot_path, end_cursor.snapshot_path))
    with IncrementalReader.for_cursor(end_cursor) as reader:
        return reader.replay_batch(start_cursor, end_cursor)
