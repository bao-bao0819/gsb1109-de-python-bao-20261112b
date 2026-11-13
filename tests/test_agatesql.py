from decimal import Decimal
from textwrap import dedent
import os
import sqlite3
import tempfile

import agate
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

import agatesql

# Per-table uniqueness conventions for the warehouse inventory tables.
# SQL NULL is the only missing-batch marker: the literal "未知" and an empty
# string are ordinary batch values and must stay fully constrained.
RECEIVING_UNIQUE_INDEXES = [
    {
        'name': 'uq_receiving_known_batch',
        'columns': ['warehouse', 'sku', 'batch'],
        'where': 'batch IS NOT NULL',
    },
    {
        'name': 'uq_receiving_unknown_batch',
        'columns': ['warehouse', 'sku'],
        'where': 'batch IS NULL',
    },
]

TRANSFER_UNIQUE_INDEXES = [
    {
        'name': 'uq_transfer_known_batch',
        'columns': ['warehouse', 'sku', 'batch'],
        'where': 'batch IS NOT NULL',
    },
]


class TestSQL(agate.AgateTestCase):
    def setUp(self):
        self.rows = (
            (1.123, 'a', True, '11/4/2015', '11/4/2015 12:22 PM'),
            (2, '👍', False, '11/5/2015', '11/4/2015 12:45 PM'),
            (2, 'c', False, '11/5/2015', '11/4/2015 12:45 PM'),
            (None, 'b', None, None, None),
        )

        self.column_names = [
            'number', 'textcol', 'boolean', 'date', 'datetime',
        ]

        self.column_types = [
            agate.Number(), agate.Text(), agate.Boolean(),
            agate.Date(), agate.DateTime(),
        ]

        self.table = agate.Table(self.rows, self.column_names, self.column_types)
        self.connection_string = 'sqlite:///:memory:'

    def test_back_and_forth(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        self.table.to_sql(connection, 'test')

        table = agate.Table.from_sql(connection, 'test')

        self.assertSequenceEqual(table.column_names, self.column_names)
        self.assertIsInstance(table.column_types[0], agate.Number)
        self.assertIsInstance(table.column_types[1], agate.Text)
        self.assertIsInstance(table.column_types[2], agate.Boolean)
        self.assertIsInstance(table.column_types[3], agate.Date)
        self.assertIsInstance(table.column_types[4], agate.DateTime)

        self.assertEqual(len(table.rows), len(self.table.rows))
        self.assertSequenceEqual(table.rows[0], self.table.rows[0])

    def test_create_if_not_exists(self):
        column_names = ['id', 'name']
        column_types = [agate.Number(), agate.Text()]
        rows1 = (
            (1, 'Jake'),
            (2, 'Howard'),
        )
        rows2 = (
            (3, 'Liz'),
            (4, 'Tim'),
        )

        table1 = agate.Table(rows1, column_names, column_types)
        table2 = agate.Table(rows2, column_names, column_types)

        engine = create_engine(self.connection_string)
        connection = engine.connect()

        # Write two agate tables into the same SQL table
        table1.to_sql(connection, 'create_if_not_exists_test', create=True, create_if_not_exists=True, insert=True)
        table2.to_sql(connection, 'create_if_not_exists_test', create=True, create_if_not_exists=True, insert=True)

        table = agate.Table.from_sql(connection, 'create_if_not_exists_test')
        self.assertSequenceEqual(table.column_names, column_names)
        self.assertIsInstance(table.column_types[0], agate.Number)
        self.assertIsInstance(table.column_types[1], agate.Text)
        self.assertEqual(len(table.rows), len(table1.rows) + len(table1.rows))
        self.assertSequenceEqual(table.rows[0], table1.rows[0])

    def test_unique_constraint(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        with self.assertRaises(IntegrityError):
            self.table.to_sql(connection, 'unique_constraint_test', unique_constraint=['number'])

    def test_prefixes(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        self.table.to_sql(connection, 'prefixes_test', prefixes=['OR REPLACE'],
                          unique_constraint=['number'])

        table = agate.Table.from_sql(connection, 'prefixes_test')

        self.assertSequenceEqual(table.column_names, self.column_names)
        self.assertIsInstance(table.column_types[0], agate.Number)
        self.assertIsInstance(table.column_types[1], agate.Text)
        self.assertIsInstance(table.column_types[2], agate.Boolean)
        self.assertIsInstance(table.column_types[3], agate.Date)
        self.assertIsInstance(table.column_types[4], agate.DateTime)

        self.assertEqual(len(table.rows), len(self.table.rows) - 1)
        self.assertSequenceEqual(table.rows[1], self.table.rows[2])

    def test_to_sql_create_statement(self):
        statement = self.table.to_sql_create_statement('test_table')

        self.assertEqual(statement.replace('\t', '  '), dedent('''\
            CREATE TABLE test_table (
              number DECIMAL, 
              textcol VARCHAR NOT NULL, 
              boolean BOOLEAN, 
              date DATE, 
              datetime TIMESTAMP
            );'''))  # noqa: W291

    def test_to_sql_create_statement_no_constraints(self):
        statement = self.table.to_sql_create_statement('test_table', constraints=False)

        self.assertEqual(statement.replace('\t', '  '), dedent('''\
            CREATE TABLE test_table (
              number DECIMAL, 
              textcol VARCHAR, 
              boolean BOOLEAN, 
              date DATE, 
              datetime TIMESTAMP
            );'''))  # noqa: W291

    def test_to_sql_create_statement_unique_constraint(self):
        statement = self.table.to_sql_create_statement('test_table', unique_constraint=['number', 'textcol'])

        self.assertEqual(statement.replace('\t', '  '), dedent('''\
            CREATE TABLE test_table (
              number DECIMAL, 
              textcol VARCHAR NOT NULL, 
              boolean BOOLEAN, 
              date DATE, 
              datetime TIMESTAMP, 
              UNIQUE (number, textcol)
            );'''))  # noqa: W291

    def test_to_sql_create_statement_with_schema(self):
        statement = self.table.to_sql_create_statement('test_table', db_schema='test_schema', dialect='mysql')

        # https://github.com/wireservice/agate-sql/issues/33#issuecomment-879267838
        if 'CHECK' in statement:
            expected = '''\
                CREATE TABLE test_schema.test_table (
                  number DECIMAL(38, 3), 
                  textcol VARCHAR(1) NOT NULL, 
                  boolean BOOL, 
                  date DATE, 
                  datetime TIMESTAMP NULL, 
                  CHECK (boolean IN (0, 1))
                );'''  # noqa: W291
        else:
            expected = '''\
                CREATE TABLE test_schema.test_table (
                  number DECIMAL(38, 3), 
                  textcol VARCHAR(1) NOT NULL, 
                  boolean BOOL, 
                  date DATE, 
                  datetime TIMESTAMP NULL
                );'''  # noqa: W291

        self.assertEqual(statement.replace('\t', '  '), dedent(expected))

    def test_to_sql_create_statement_with_dialects(self):
        # Can test 'ingres' if:
        # pip install git+https://github.com/clach04/ingres_sa_dialect.git
        for dialect in ('crate', 'mssql', 'mysql', 'oracle', 'postgresql', 'sqlite'):
            self.table.to_sql_create_statement('test_table', dialect=dialect)

    def test_to_sql_create_statement_zero_width(self):
        rows = ((1, ''), (2, ''))
        column_names = ['id', 'name']
        column_types = [agate.Number(), agate.Text()]
        table = agate.Table(rows, column_names, column_types)

        statement = table.to_sql_create_statement('test_table', db_schema='test_schema', dialect='mysql')

        self.assertEqual(statement.replace('\t', '  '), dedent('''\
            CREATE TABLE test_schema.test_table (
              id DECIMAL(38, 0) NOT NULL, 
              name VARCHAR(1)
            );'''))  # noqa: W291

    def test_to_sql_create_statement_wide_width(self):
        rows = ((1, 'x' * 21845), (2, ''))
        column_names = ['id', 'name']
        column_types = [agate.Number(), agate.Text()]
        table = agate.Table(rows, column_names, column_types)

        statement = table.to_sql_create_statement('test_table', db_schema='test_schema', dialect='mysql')

        self.assertEqual(statement.replace('\t', '  '), dedent('''\
            CREATE TABLE test_schema.test_table (
              id DECIMAL(38, 0) NOT NULL, 
              name TEXT
            );'''))  # noqa: W291

    def test_make_sql_table_col_len_multiplier(self):
        rows = ((1, 'x' * 10), (2, ''))
        column_names = ['id', 'name']
        column_types = [agate.Number(), agate.Text()]
        table = agate.Table(rows, column_names, column_types)

        sql_table = agatesql.table.make_sql_table(table, 'test_table', dialect='mysql', db_schema='test_schema',
                                                  constraints=True, col_len_multiplier=1.5)

        self.assertEqual(sql_table.columns.get('name').type.length, 15)

    def test_make_sql_table_min_col_len(self):
        rows = ((1, 'x' * 10), (2, ''))
        column_names = ['id', 'name']
        column_types = [agate.Number(), agate.Text()]
        table = agate.Table(rows, column_names, column_types)

        sql_table = agatesql.table.make_sql_table(table, 'test_table', dialect='mysql', db_schema='test_schema',
                                                  constraints=True, min_col_len=20)

        self.assertEqual(sql_table.columns.get('name').type.length, 20)

    def test_sql_query_simple(self):
        results = self.table.sql_query('select * from agate')

        self.assertColumnNames(results, self.table.column_names)
        self.assertRows(results, self.table.rows)

    def test_sql_query_limit(self):
        results = self.table.sql_query('select * from agate limit 2')

        self.assertColumnNames(results, self.table.column_names)
        self.assertRows(results, self.table.rows[:2])

    def test_sql_query_select(self):
        results = self.table.sql_query('select number, boolean from agate')

        self.assertColumnNames(results, ['number', 'boolean'])
        self.assertColumnTypes(results, [agate.Number, agate.Boolean])
        self.assertRows(results, [
            [Decimal('1.123'), True],
            [2, False],
            [2, False],
            [None, None],
        ])

    def test_sql_query_aggregate(self):
        results = self.table.sql_query('select sum(number) as total from agate')

        self.assertColumnNames(results, ['total'])
        self.assertColumnTypes(results, [agate.Number])
        self.assertRows(results, [[Decimal('5.123')]])

    def test_chunk_size(self):
        column_names = ['number']
        column_types = [agate.Number()]

        rows = []
        expected = 0
        for n in range(9999):
            rows.append((n,))
            expected += n

        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            table = agate.Table(rows, column_names, column_types)
            table.to_sql(connection, 'test_chunk_size', overwrite=True, chunk_size=100)

            table = agate.Table.from_sql(connection, 'test_chunk_size')
            actual = sum(r[0] for r in table.rows)
            self.assertEqual(len(table.rows), len(rows))
            self.assertEqual(expected, actual)
        finally:
            connection.close()
            engine.dispose()

    def _batch_table(self, rows):
        return agate.Table(
            rows,
            ['warehouse', 'sku', 'batch'],
            [agate.Text(), agate.Text(), agate.Text()],
        )

    def test_create_statement_partial_unique_indexes(self):
        table = self._batch_table((
            ('W1', 'S1', 'B1'),
            (None, None, None),
        ))

        statement = table.to_sql_create_statement(
            'receiving', dialect='sqlite', unique_indexes=RECEIVING_UNIQUE_INDEXES,
        )

        normalized = '\n'.join(
            line.rstrip() for line in statement.replace('\t', '  ').splitlines()
        )
        self.assertEqual(normalized, dedent('''\
            CREATE TABLE receiving (
              warehouse VARCHAR,
              sku VARCHAR,
              batch VARCHAR
            );
            CREATE UNIQUE INDEX uq_receiving_known_batch ON receiving (warehouse, sku, batch) WHERE batch IS NOT NULL;
            CREATE UNIQUE INDEX uq_receiving_unknown_batch ON receiving (warehouse, sku) WHERE batch IS NULL;'''))  # noqa: E501

    def test_receiving_table_enforces_single_unknown_batch(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            self._batch_table((('W1', 'S1', 'B1'), (None, None, None))).to_sql(
                connection, 'receiving', unique_indexes=RECEIVING_UNIQUE_INDEXES, insert=False,
            )

            def insert(warehouse, sku, batch):
                connection.execute(
                    text('INSERT INTO receiving (warehouse, sku, batch) VALUES (:w, :s, :b)'),
                    {'w': warehouse, 's': sku, 'b': batch},
                )

            insert('W1', 'S1', 'B1')
            # Same warehouse, sku and known batch: only one row.
            with self.assertRaises(IntegrityError):
                insert('W1', 'S1', 'B1')
            # "未知" is an ordinary batch value, not a missing marker.
            insert('W1', 'S2', '未知')
            with self.assertRaises(IntegrityError):
                insert('W1', 'S2', '未知')
            # An empty string is likewise an ordinary batch value.
            insert('W1', 'S3', '')
            with self.assertRaises(IntegrityError):
                insert('W1', 'S3', '')
            # NULL means "batch unknown": a single placeholder row per warehouse and sku.
            insert('W1', 'S4', None)
            with self.assertRaises(IntegrityError):
                insert('W1', 'S4', None)
            # Different sku may carry its own unknown-batch row.
            insert('W1', 'S5', None)
        finally:
            connection.close()
            engine.dispose()

    def test_transfer_table_allows_multiple_unknown_batches(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            self._batch_table((('W1', 'S1', 'B1'), (None, None, None))).to_sql(
                connection, 'transfer', unique_indexes=TRANSFER_UNIQUE_INDEXES, insert=False,
            )

            def insert(warehouse, sku, batch):
                connection.execute(
                    text('INSERT INTO transfer (warehouse, sku, batch) VALUES (:w, :s, :b)'),
                    {'w': warehouse, 's': sku, 'b': batch},
                )

            insert('W1', 'S1', 'B1')
            with self.assertRaises(IntegrityError):
                insert('W1', 'S1', 'B1')
            insert('W1', 'S2', '未知')
            with self.assertRaises(IntegrityError):
                insert('W1', 'S2', '未知')
            insert('W1', 'S3', '')
            with self.assertRaises(IntegrityError):
                insert('W1', 'S3', '')
            # The transfer table permits several unknown-batch rows per warehouse and sku.
            insert('W1', 'S4', None)
            insert('W1', 'S4', None)
        finally:
            connection.close()
            engine.dispose()

    def test_indexes_enforced_by_raw_sqlite3(self):
        # The convention must hold for writers that bypass agate/SQLAlchemy
        # entirely and talk to the SQLite file with the stdlib driver.
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, 'inventory.db')
            self._batch_table((('W1', 'S1', 'B1'), (None, None, None))).to_sql(
                'sqlite:///%s' % db_path, 'receiving',
                unique_indexes=RECEIVING_UNIQUE_INDEXES, insert=False,
            )

            raw = sqlite3.connect(db_path)
            try:
                index_sql = {
                    row[0]: row[1]
                    for row in raw.execute(
                        "SELECT name, sql FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
                    )
                }
                self.assertIn('uq_receiving_known_batch', index_sql)
                self.assertIn('WHERE batch IS NOT NULL', index_sql['uq_receiving_known_batch'])
                self.assertIn('WHERE batch IS NULL', index_sql['uq_receiving_unknown_batch'])

                raw.execute("INSERT INTO receiving VALUES ('W1', 'S1', 'B1')")
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute("INSERT INTO receiving VALUES ('W1', 'S1', 'B1')")
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute("INSERT INTO receiving VALUES ('W1', 'S2', '未知')")
                    raw.execute("INSERT INTO receiving VALUES ('W1', 'S2', '未知')")
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute("INSERT INTO receiving VALUES ('W1', 'S3', '')")
                    raw.execute("INSERT INTO receiving VALUES ('W1', 'S3', '')")
                raw.execute("INSERT INTO receiving VALUES ('W1', 'S4', NULL)")
                with self.assertRaises(sqlite3.IntegrityError):
                    raw.execute("INSERT INTO receiving VALUES ('W1', 'S4', NULL)")
            finally:
                raw.close()

    def test_partial_index_predicate_ignored_without_dialect_support(self):
        table = self._batch_table((('W1', 'S1', 'B1'),))

        statement = table.to_sql_create_statement(
            'receiving', dialect='mysql', unique_indexes=RECEIVING_UNIQUE_INDEXES,
        )

        self.assertIn('CREATE UNIQUE INDEX uq_receiving_known_batch ON receiving (warehouse, sku, batch);',
                      statement.replace('\t', '  '))
        self.assertNotIn('WHERE', statement)

    def test_make_sql_index_requires_columns(self):
        with self.assertRaises(ValueError):
            agatesql.table.make_sql_index('receiving', {'where': 'batch IS NULL'}, dialect='sqlite')

    def test_make_sql_index_name_is_deterministic(self):
        spec = {'columns': ['warehouse', 'sku', 'batch'], 'where': 'batch IS NOT NULL'}

        first = agatesql.table.make_sql_index('receiving', spec, dialect='sqlite')
        second = agatesql.table.make_sql_index('receiving', spec, dialect='sqlite')

        self.assertEqual(first.name, second.name)
        self.assertTrue(first.unique)
        self.assertTrue(first.name.startswith('uq_receiving_'))
