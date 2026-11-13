from decimal import Decimal
from textwrap import dedent

import agate
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError

import agatesql


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


class TestVersionedMerge(agate.AgateTestCase):
    def setUp(self):
        self.column_names = ['order_no', 'version', 'action', 'price']
        self.column_types = [agate.Text(), agate.Number(), agate.Text(), agate.Number()]
        self.connection_string = 'sqlite:///:memory:'

    def _table(self, rows):
        return agate.Table(rows, self.column_names, self.column_types)

    def _merge(self, connection, rows, **kwargs):
        self._table(rows).to_sql(
            connection, 'orders', overwrite=True,
            merge_by_version=True, key_columns=['order_no'], **kwargs)

    def _business_rows(self, connection):
        return connection.execute(
            text('SELECT order_no, version, action, price FROM orders ORDER BY order_no')
        ).all()

    def _conflict_rows(self, connection):
        return connection.execute(
            text('SELECT order_no, version, action, price, conflict_reason '
                 'FROM orders_conflicts ORDER BY order_no, price')
        ).all()

    def test_exact_resend_is_stored_once(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            self._merge(connection, (
                ('A', 1, 'insert', 10),
                ('A', 1, 'insert', 10),
                ('B', 2, 'insert', 20),
            ))

            self.assertEqual(self._business_rows(connection), [
                ('A', 1, 'insert', 10),
                ('B', 2, 'insert', 20),
            ])
            self.assertEqual(self._conflict_rows(connection), [])
        finally:
            connection.close()
            engine.dispose()

    def test_two_prices_for_same_version_are_quarantined(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            # The two prices arrive in either order; neither may win by position.
            self._merge(connection, (
                ('A', 1, 'insert', 10),
                ('A', 1, 'insert', 11),
            ))

            self.assertEqual(self._business_rows(connection), [])
            self.assertEqual([row[:4] for row in self._conflict_rows(connection)], [
                ('A', 1, 'insert', 10),
                ('A', 1, 'insert', 11),
            ])

            self._merge(connection, (
                ('A', 1, 'insert', 11),
                ('A', 1, 'insert', 10),
            ))

            self.assertEqual(self._business_rows(connection), [])
            self.assertEqual([row[:4] for row in self._conflict_rows(connection)], [
                ('A', 1, 'insert', 10),
                ('A', 1, 'insert', 11),
            ])
        finally:
            connection.close()
            engine.dispose()

    def test_conflict_on_old_version_does_not_block_newer_version(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            self._merge(connection, (
                ('A', 1, 'insert', 10),
                ('A', 1, 'insert', 11),
                ('A', 2, 'insert', 30),
            ))

            self.assertEqual(self._business_rows(connection), [
                ('A', 2, 'insert', 30),
            ])
            self.assertEqual([row[:4] for row in self._conflict_rows(connection)], [
                ('A', 1, 'insert', 10),
                ('A', 1, 'insert', 11),
            ])
        finally:
            connection.close()
            engine.dispose()

    def test_cancel_before_earlier_versions_keeps_order_out_of_table(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            # Cancellation of the newest version is received before the older
            # orders it cancels.
            self._merge(connection, (
                ('A', 3, 'cancel', None),
                ('A', 1, 'insert', 10),
                ('A', 2, 'insert', 12),
            ))

            self.assertEqual(self._business_rows(connection), [])
            self.assertEqual(self._conflict_rows(connection), [])
        finally:
            connection.close()
            engine.dispose()

    def test_merge_result_is_independent_of_chunking_and_row_order(self):
        rows = (
            ('A', 1, 'insert', 10),
            ('A', 1, 'insert', 11),
            ('B', 1, 'insert', 5),
            ('B', 1, 'insert', 6),
            ('C', 1, 'insert', 9),
        )

        results = []
        for chunk_size in (None, 1, 2):
            engine = create_engine(self.connection_string)
            connection = engine.connect()
            try:
                self._table(rows).to_sql(
                    connection, 'orders', overwrite=True, chunk_size=chunk_size,
                    merge_by_version=True, key_columns=['order_no'])
                results.append((self._business_rows(connection), self._conflict_rows(connection)))
            finally:
                connection.close()
                engine.dispose()

        engine = create_engine(self.connection_string)
        connection = engine.connect()
        try:
            self._table(tuple(reversed(rows))).to_sql(
                connection, 'orders', overwrite=True, chunk_size=2,
                merge_by_version=True, key_columns=['order_no'])
            results.append((self._business_rows(connection), self._conflict_rows(connection)))
        finally:
            connection.close()
            engine.dispose()

        for result in results[1:]:
            self.assertEqual(result, results[0])

        business_rows, conflict_rows = results[0]
        self.assertEqual(business_rows, [('C', 1, 'insert', 9)])
        self.assertEqual([row[:4] for row in conflict_rows], [
            ('A', 1, 'insert', 10),
            ('A', 1, 'insert', 11),
            ('B', 1, 'insert', 5),
            ('B', 1, 'insert', 6),
        ])

    def test_newer_version_updates_and_older_version_cannot_downgrade(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            self._table((('A', 1, 'insert', 10),)).to_sql(
                connection, 'orders',
                merge_by_version=True, key_columns=['order_no'])
            self._table((('A', 2, 'insert', 15),)).to_sql(
                connection, 'orders', create=False,
                merge_by_version=True, key_columns=['order_no'])
            self._table((('A', 1, 'insert', 99),)).to_sql(
                connection, 'orders', create=False,
                merge_by_version=True, key_columns=['order_no'])

            self.assertEqual(
                connection.execute(text('SELECT version, price FROM orders')).all(),
                [(2, 15)])
        finally:
            connection.close()
            engine.dispose()

    def test_target_table_has_unique_constraint_on_key(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            self._merge(connection, (('A', 1, 'insert', 10),))

            with self.assertRaises(IntegrityError):
                connection.execute(text(
                    "INSERT INTO orders (order_no, version, action, price) "
                    "VALUES ('A', 9, 'insert', 1)"))
        finally:
            connection.close()
            engine.dispose()

    def test_key_columns_are_required(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            with self.assertRaises(ValueError):
                self._table((('A', 1, 'insert', 10),)).to_sql(
                    connection, 'orders', merge_by_version=True)
        finally:
            connection.close()
            engine.dispose()

    def test_invalid_action_is_rejected(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            with self.assertRaises(ValueError):
                self._table((('A', 1, 'unknown', 10),)).to_sql(
                    connection, 'orders', merge_by_version=True,
                    key_columns=['order_no'])
        finally:
            connection.close()
            engine.dispose()
