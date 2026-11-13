import os
import shutil
import sqlite3
import tempfile

import agate
import pytest
from sqlalchemy import create_engine

import agatesql
from agatesql import (
    CursorContractError,
    IncrementalCursor,
    IncrementalReader,
    SnapshotMismatchError,
    UnsupportedSourceError,
    read_incremental_batch,
    replay_batch,
)


def make_database(path, rows=None, *, timestamp_kind='integer', table='events'):
    connection = sqlite3.connect(path)
    connection.execute(
        'CREATE TABLE %s (id INTEGER, updated_at %s, payload TEXT)'
        % (table, 'INTEGER' if timestamp_kind == 'integer' else 'TEXT'))
    if rows:
        connection.executemany(
            'INSERT INTO %s (id, updated_at, payload) VALUES (?, ?, ?)' % table, rows)
    connection.commit()
    connection.close()


def drain(reader, batch_size):
    batches = []
    cursor = None
    while True:
        result = reader.read_batch(cursor)
        batches.append(result)
        cursor = result.next_cursor
        if result.is_final:
            break
    return batches


class TestIncrementalRead:
    def setup_method(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, 'live.sqlite')
        self.snapshot_path = os.path.join(self.tmp.name, 'snapshot.sqlite')
        rows = [(identifier, 1000 + identifier, 'row-%d' % identifier)
                for identifier in range(1, 26)]
        make_database(self.db_path, rows)
        self.engine = create_engine('sqlite:///%s' % self.db_path)

    def teardown_method(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_pagination_covers_every_row_once_in_order(self):
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=10, snapshot_path=self.snapshot_path) as reader:
            batches = drain(reader, 10)

        assert [batch.rows_returned for batch in batches] == [10, 10, 5]
        delivered = [row for batch in batches for row in batch.table.rows]
        assert [row['id'] for row in delivered] == list(range(1, 26))
        keysets = [(row['updated_at'], row['id']) for row in delivered]
        assert keysets == sorted(keysets)
        assert batches[-1].next_cursor.exhausted is True

        with IncrementalReader.for_cursor(batches[-1].next_cursor) as reader:
            again = reader.read_batch(batches[-1].next_cursor)
        assert again.rows_returned == 0
        assert again.is_final is True

    def test_tied_timestamps_are_never_split_or_lost(self):
        rows = [(identifier, 5000, 'tie') for identifier in range(1, 41)]
        make_database(os.path.join(self.tmp.name, 'ties.sqlite'), rows)
        engine = create_engine('sqlite:///%s' % os.path.join(self.tmp.name, 'ties.sqlite'))
        snapshot = os.path.join(self.tmp.name, 'ties-snapshot.sqlite')
        try:
            with IncrementalReader(engine, 'events', 'id', 'updated_at',
                                   batch_size=7, snapshot_path=snapshot) as reader:
                batches = drain(reader, 7)
        finally:
            engine.dispose()

        delivered_ids = [row['id'] for batch in batches for row in batch.table.rows]
        assert sorted(delivered_ids) == list(range(1, 41))
        assert len(set(delivered_ids)) == 40
        assert [batch.rows_returned for batch in batches] == [7, 7, 7, 7, 7, 5]

    def test_rewind_after_snapshot_cannot_create_a_gap(self):
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=8, snapshot_path=self.snapshot_path) as reader:
            first = reader.read_batch()
            first_ids = [row['id'] for row in first.table.rows]
            assert len(first_ids) == 8

            # The live database changes while the snapshot is being read: rows
            # not yet delivered are rewound to before the first page, some are
            # deleted, and a brand new row appears.
            writer = sqlite3.connect(self.db_path)
            writer.execute('UPDATE events SET updated_at = 1 WHERE id > 8')
            writer.execute('DELETE FROM events WHERE id = 25')
            writer.execute('INSERT INTO events (id, updated_at, payload) VALUES (99, 9999, ?)',
                           ('late',))
            writer.commit()
            writer.close()

            rest = []
            cursor = first.next_cursor
            while True:
                result = reader.read_batch(cursor)
                rest.extend(row['id'] for row in result.table.rows)
                cursor = result.next_cursor
                if result.is_final:
                    break

        snapshot_ids = first_ids + rest
        assert sorted(snapshot_ids) == list(range(1, 26))

        # A fresh run over the new source state must still surface every
        # surviving row, including the ones that were rewound.
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=100,
                               snapshot_path=os.path.join(self.tmp.name, 'snapshot-2.sqlite')) as reader:
            fresh = drain(reader, 100)
        fresh_ids = sorted(row['id'] for batch in fresh for row in batch.table.rows)
        assert fresh_ids == sorted((set(range(1, 25)) | {99}))

    def test_cursor_roundtrip_and_handoff_to_another_reader(self):
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=10, snapshot_path=self.snapshot_path) as reader:
            first = reader.read_batch()
            token = first.next_cursor.to_token()
            document = first.handoff_document()

        assert document['snapshot_path'] == self.snapshot_path
        assert document['next'] == token
        assert document['rows_returned'] == 10
        assert document['exhausted'] is False

        resumed_cursor = IncrementalCursor.from_token(token)
        with IncrementalReader.for_cursor(resumed_cursor, batch_size=10) as reader:
            second = reader.read_batch(resumed_cursor)
        assert [int(row['id']) for row in second.table.rows] == list(range(11, 21))

        dict_cursor = IncrementalCursor.from_dict(resumed_cursor.to_dict())
        replayed = replay_batch(self.snapshot_path, first.start_cursor, first.next_cursor)
        assert [int(row['id']) for row in replayed.rows] == list(range(1, 11))
        assert dict_cursor == resumed_cursor

    def test_replay_reconstructs_each_delivered_batch(self):
        boundaries = []
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=10, snapshot_path=self.snapshot_path) as reader:
            cursor = None
            while True:
                result = reader.read_batch(cursor)
                boundaries.append((result.start_cursor, result.next_cursor,
                                   [row['id'] for row in result.table.rows]))
                cursor = result.next_cursor
                if result.is_final:
                    break

        with IncrementalReader.for_cursor(boundaries[-1][1]) as reader:
            for start, end, expected_ids in boundaries:
                replayed = reader.replay_batch(start, end)
                assert [row['id'] for row in replayed.rows] == expected_ids

    def test_composite_key(self):
        path = os.path.join(self.tmp.name, 'composite.sqlite')
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE events (tenant INTEGER, seq INTEGER, updated_at INTEGER)')
        connection.executemany('INSERT INTO events VALUES (?, ?, ?)',
                               [(tenant, seq, 100 + seq) for tenant in (1, 2) for seq in range(1, 6)])
        connection.commit()
        connection.close()
        engine = create_engine('sqlite:///%s' % path)
        try:
            with IncrementalReader(engine, 'events', ('tenant', 'seq'), 'updated_at',
                                   batch_size=4,
                                   snapshot_path=os.path.join(self.tmp.name, 'composite-snap.sqlite')) as reader:
                batches = drain(reader, 4)
        finally:
            engine.dispose()
        delivered = [(row['tenant'], row['seq']) for batch in batches for row in batch.table.rows]
        assert len(delivered) == 10
        assert len(set(delivered)) == 10

    def test_empty_table(self):
        path = os.path.join(self.tmp.name, 'empty.sqlite')
        make_database(path, [])
        result = read_incremental_batch('sqlite:///%s' % path, 'events', 'id', 'updated_at',
                                        snapshot_path=os.path.join(self.tmp.name, 'empty-snap.sqlite'))
        assert result.rows_returned == 0
        assert result.is_final is True
        assert result.next_cursor.exhausted is True

    def test_convenience_wrapper_resumes_from_cursor_snapshot(self):
        first = read_incremental_batch(self.db_path, 'events', 'id', 'updated_at',
                                       batch_size=10, snapshot_path=self.snapshot_path)
        assert [int(row['id']) for row in first.table.rows] == list(range(1, 11))

        second = read_incremental_batch(None, 'events', 'id', 'updated_at',
                                        batch_size=10, cursor=first.next_cursor)
        assert [int(row['id']) for row in second.table.rows] == list(range(11, 21))

        third = read_incremental_batch(self.snapshot_path, 'events', 'id', 'updated_at',
                                       batch_size=10, cursor=second.next_cursor)
        assert [int(row['id']) for row in third.table.rows] == list(range(21, 26))
        assert third.is_final is True

    def test_convenience_wrapper_rejects_resume_against_live_database(self):
        first = read_incremental_batch(self.db_path, 'events', 'id', 'updated_at',
                                       batch_size=10, snapshot_path=self.snapshot_path)
        with pytest.raises(SnapshotMismatchError, match='cursor snapshot'):
            read_incremental_batch(self.db_path, 'events', 'id', 'updated_at',
                                   cursor=first.next_cursor)
        with pytest.raises(SnapshotMismatchError, match='does not match'):
            read_incremental_batch(None, 'events', 'id', 'updated_at',
                                   cursor=first.next_cursor,
                                   snapshot_path=os.path.join(self.tmp.name, 'other.sqlite'))
        with pytest.raises(CursorContractError, match='for_cursor'):
            read_incremental_batch(self.engine, 'events', 'id', 'updated_at',
                                   cursor=first.next_cursor)

    def test_text_timestamps_order_consistently(self):
        path = os.path.join(self.tmp.name, 'text-ts.sqlite')
        rows = [(identifier, '2026-01-%02dT00:00:00' % ((identifier % 9) + 1), 'x')
                for identifier in range(1, 13)]
        make_database(path, rows, timestamp_kind='text')
        engine = create_engine('sqlite:///%s' % path)
        try:
            with IncrementalReader(engine, 'events', 'id', 'updated_at', batch_size=5,
                                   snapshot_path=os.path.join(self.tmp.name, 'text-snap.sqlite')) as reader:
                batches = drain(reader, 5)
        finally:
            engine.dispose()
        timestamps = [row['updated_at'] for batch in batches for row in batch.table.rows]
        assert timestamps == sorted(timestamps)
        assert len(timestamps) == 12

    def test_duplicate_key_is_rejected_before_cursor(self):
        path = os.path.join(self.tmp.name, 'dup.sqlite')
        make_database(path, [(1, 100, 'a'), (1, 101, 'b')])
        with pytest.raises(CursorContractError, match='not unique'):
            with IncrementalReader(create_engine('sqlite:///%s' % path), 'events', 'id',
                                   'updated_at'):
                pass

    def test_null_key_is_rejected(self):
        path = os.path.join(self.tmp.name, 'null-key.sqlite')
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE events (id INTEGER, updated_at INTEGER)')
        connection.execute('INSERT INTO events VALUES (NULL, 1)')
        connection.commit()
        connection.close()
        with pytest.raises(CursorContractError, match='NULL'):
            with IncrementalReader(create_engine('sqlite:///%s' % path), 'events', 'id',
                                   'updated_at'):
                pass

    def test_null_timestamp_is_rejected(self):
        path = os.path.join(self.tmp.name, 'null-ts.sqlite')
        make_database(path, [(1, None, 'a')])
        with pytest.raises(CursorContractError, match='Timestamp column'):
            with IncrementalReader(create_engine('sqlite:///%s' % path), 'events', 'id',
                                   'updated_at'):
                pass

    def test_mixed_timestamp_storage_types_is_rejected(self):
        path = os.path.join(self.tmp.name, 'mixed.sqlite')
        connection = sqlite3.connect(path)
        connection.execute('CREATE TABLE events (id INTEGER PRIMARY KEY, updated_at)')
        connection.execute('INSERT INTO events VALUES (1, 100)')
        connection.execute("INSERT INTO events VALUES (2, '2026-01-01')")
        connection.commit()
        connection.close()
        with pytest.raises(CursorContractError, match='storage types'):
            with IncrementalReader(create_engine('sqlite:///%s' % path), 'events', 'id',
                                   'updated_at'):
                pass

    def test_non_sqlite_source_is_rejected(self):
        with pytest.raises(UnsupportedSourceError, match='SQLite'):
            with IncrementalReader('postgresql://user:pass@localhost/none', 'events', 'id',
                                   'updated_at'):
                pass

    def test_non_sqlite_engine_like_object_is_rejected(self):
        class ForeignDialect:
            name = 'postgresql'

        class ForeignEngine:
            dialect = ForeignDialect()

        with pytest.raises(UnsupportedSourceError, match='SQLite'):
            with IncrementalReader(ForeignEngine(), 'events', 'id', 'updated_at'):
                pass

    def test_snapshot_file_must_differ_from_live_database(self):
        with pytest.raises(UnsupportedSourceError, match='separate file'):
            with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                                   snapshot_path=self.db_path):
                pass

    def test_missing_snapshot_raises_mismatch(self):
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=10, snapshot_path=self.snapshot_path) as reader:
            cursor = reader.read_batch().next_cursor
        os.unlink(self.snapshot_path)
        with pytest.raises(SnapshotMismatchError, match='no longer exists'):
            IncrementalReader.for_cursor(cursor)

    def test_tampered_snapshot_raises_mismatch(self):
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=10, snapshot_path=self.snapshot_path) as reader:
            cursor = reader.read_batch().next_cursor

        other = os.path.join(self.tmp.name, 'other.sqlite')
        make_database(other, [(1, 1, 'different')])
        shutil.copyfile(other, self.snapshot_path)
        with pytest.raises(SnapshotMismatchError, match='fingerprint'):
            IncrementalReader.for_cursor(cursor)

    def test_cursor_from_other_table_is_rejected(self):
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=10, snapshot_path=self.snapshot_path) as reader:
            cursor = reader.read_batch().next_cursor
        foreign = IncrementalCursor(
            'other_table', cursor.key_columns, cursor.timestamp_column,
            cursor.snapshot_path, cursor.snapshot_fingerprint)
        with IncrementalReader.for_cursor(cursor) as reader:
            with pytest.raises(CursorContractError, match='configured for table'):
                reader.read_batch(foreign)

    def test_malformed_cursor_token_is_rejected(self):
        with pytest.raises(CursorContractError):
            IncrementalCursor.from_token('not-a-token')

    def test_temporary_snapshot_is_disposed(self):
        result = read_incremental_batch(self.engine, 'events', 'id', 'updated_at',
                                        batch_size=5)
        assert result.rows_returned == 5
        assert not os.path.exists(result.snapshot_path)
        with pytest.raises(SnapshotMismatchError):
            IncrementalReader.for_cursor(result.next_cursor)

    def test_connection_object_is_accepted_as_source(self):
        connection = self.engine.connect()
        try:
            with IncrementalReader(connection, 'events', 'id', 'updated_at', batch_size=100,
                                   snapshot_path=os.path.join(self.tmp.name, 'conn-snap.sqlite')) as reader:
                batches = drain(reader, 100)
        finally:
            connection.close()
        assert sum(batch.rows_returned for batch in batches) == 25

    def test_in_memory_engine_is_supported_via_backup(self):
        engine = create_engine('sqlite:///:memory:')
        with engine.connect() as connection:
            connection.exec_driver_sql('CREATE TABLE events (id INTEGER, updated_at INTEGER, payload TEXT)')
            connection.exec_driver_sql(
                "INSERT INTO events VALUES (1, 100, 'a'), (2, 101, 'b')")
            connection.commit()
        with IncrementalReader(engine, 'events', 'id', 'updated_at', batch_size=10,
                               snapshot_path=os.path.join(self.tmp.name, 'memory-snap.sqlite')) as reader:
            batches = drain(reader, 10)
        assert [int(row['id']) for batch in batches for row in batch.table.rows] == [1, 2]

    def test_replay_rejects_wrong_snapshot_path(self):
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=10, snapshot_path=self.snapshot_path) as reader:
            result = reader.read_batch()
        with pytest.raises(SnapshotMismatchError, match='does not match'):
            replay_batch(os.path.join(self.tmp.name, 'elsewhere.sqlite'),
                         result.start_cursor, result.next_cursor)

    def test_returns_agate_table_with_declared_types(self):
        with IncrementalReader(self.engine, 'events', 'id', 'updated_at',
                               batch_size=10, snapshot_path=self.snapshot_path) as reader:
            result = reader.read_batch()
        assert isinstance(result.table, agate.Table)
        assert tuple(result.table.column_names) == ('id', 'updated_at', 'payload')
        assert isinstance(result.table.column_types[0], agate.Number)
        assert isinstance(result.table.column_types[2], agate.Text)
