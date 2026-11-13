from decimal import Decimal
from textwrap import dedent

import agate
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError

import agatesql


def enable_foreign_keys(engine):
    @event.listens_for(engine, 'connect')
    def set_sqlite_pragma(dbapi_connection, connection_record):
        dbapi_connection.execute('PRAGMA foreign_keys = ON')



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


class TestTableSetToSQL(agate.AgateTestCase):
    def setUp(self):
        self.column_types = [agate.Number(), agate.Text(), agate.Number()]

        self.departments = agate.Table(
            (
                (1, 'Engineering', 10),
                (2, 'Sales', 20),
                (3, 'Empty', 30),
            ),
            ['id', 'name', 'manager_id'],
            self.column_types,
        )
        self.managers = agate.Table(
            (
                (10, 'Alice', 1),
                (20, 'Bob', 2),
                (30, 'Mallory', 99),
            ),
            ['id', 'name', 'department_id'],
            self.column_types,
        )
        self.stores = agate.Table(
            (
                (100, 'Downtown', None),
                (200, 'Airport', None),
            ),
            ['id', 'name', 'manager_id'],
            self.column_types,
        )

        self.foreign_keys = {
            'departments': [('manager_id', 'managers', 'id')],
            'managers': [('department_id', 'departments', 'id')],
        }
        self.unique_constraints = {
            'departments': ['id'],
            'managers': ['id'],
            'stores': ['id'],
        }

        self.connection_string = 'sqlite:///:memory:'

    def make_tables(self, order=('departments', 'managers', 'stores')):
        tables = {
            'departments': self.departments,
            'managers': self.managers,
            'stores': self.stores,
        }
        return {name: tables[name] for name in order}

    def load(self, connection, order=('departments', 'managers', 'stores')):
        return agatesql.table.to_sql_group(
            self.make_tables(order),
            connection,
            foreign_keys=self.foreign_keys,
            unique_constraints=self.unique_constraints,
        )

    def committed_ids(self, connection, table_name):
        rows = connection.exec_driver_sql(f'SELECT id FROM {table_name} ORDER BY id').fetchall()
        return [row[0] for row in rows]

    def test_circular_references_and_unresolvable_rows(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            rejected = self.load(connection)

            # The valid circular references are committed, as are the
            # independent stores.
            self.assertSequenceEqual(self.committed_ids(connection, 'departments'), [1, 2])
            self.assertSequenceEqual(self.committed_ids(connection, 'managers'), [10, 20])
            self.assertSequenceEqual(self.committed_ids(connection, 'stores'), [100, 200])

            # Manager 30 references missing department 99, and department 3
            # references manager 30, so both are rejected and reported.
            self.assertSequenceEqual(sorted(rejected), ['departments', 'managers'])
            self.assertColumnNames(rejected['departments'], self.departments.column_names)
            self.assertRows(rejected['departments'], [[3, 'Empty', 30]])
            self.assertRows(rejected['managers'], [[30, 'Mallory', 99]])
        finally:
            connection.close()
            engine.dispose()

    def test_table_order_does_not_change_committed_rows(self):
        orders = [
            ('departments', 'managers', 'stores'),
            ('stores', 'managers', 'departments'),
            ('managers', 'stores', 'departments'),
        ]

        results = []
        for order in orders:
            engine = create_engine(self.connection_string)
            connection = engine.connect()
            try:
                rejected = self.load(connection, order)
                committed = tuple(
                    self.committed_ids(connection, name)
                    for name in ('departments', 'managers', 'stores')
                )
                rejected_rows = tuple(
                    tuple(tuple(row) for row in rejected[name].rows)
                    for name in sorted(rejected)
                )
                results.append((committed, rejected_rows))
            finally:
                connection.close()
                engine.dispose()

        self.assertEqual(results[0], results[1])
        self.assertEqual(results[0], results[2])

    def test_existing_database_rows_can_be_referenced(self):
        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            # Department 99 already exists in the database, so manager 30's
            # reference to it is valid.
            existing = agate.Table(
                ((99, 'Archive', None),),
                ['id', 'name', 'manager_id'],
                self.column_types,
            )
            existing.to_sql(connection, 'departments', unique_constraint=['id'])

            managers_only = {'managers': self.managers}
            rejected = agatesql.table.to_sql_group(
                managers_only,
                connection,
                foreign_keys={'managers': [('department_id', 'departments', 'id')]},
                unique_constraints={'managers': ['id']},
            )

            # Only manager 30's reference (to existing department 99) is
            # valid; managers 10 and 20 reference departments that exist
            # neither in the batch nor in the database.
            self.assertSequenceEqual(self.committed_ids(connection, 'managers'), [30])
            self.assertSequenceEqual(sorted(rejected), ['managers'])
            self.assertRows(rejected['managers'], [[10, 'Alice', 1], [20, 'Bob', 2]])
        finally:
            connection.close()
            engine.dispose()

    def test_foreign_keys_remain_enforced(self):
        engine = create_engine(self.connection_string)
        enable_foreign_keys(engine)
        connection = engine.connect()

        try:
            self.load(connection)
            connection.commit()

            # Foreign key enforcement was not switched off to load the batch.
            self.assertEqual(connection.exec_driver_sql('PRAGMA foreign_keys').scalar(), 1)
            self.assertEqual(connection.exec_driver_sql('PRAGMA defer_foreign_keys').scalar(), 0)

            # The created tables really carry the constraints.
            self.assertNotEqual(
                connection.exec_driver_sql('PRAGMA foreign_key_list(departments)').fetchall(), [])

            connection.exec_driver_sql(
                'INSERT INTO managers (id, name, department_id) VALUES (40, \'Eve\', 42)')
            with self.assertRaises(IntegrityError):
                connection.commit()
            connection.rollback()
        finally:
            connection.close()
            engine.dispose()

    def test_connection_string_commits(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix='.db')
        os.close(fd)

        try:
            rejected = agatesql.table.to_sql_group(
                self.make_tables(),
                'sqlite:///' + path,
                foreign_keys=self.foreign_keys,
                unique_constraints=self.unique_constraints,
            )
            self.assertSequenceEqual(sorted(rejected), ['departments', 'managers'])

            engine = create_engine('sqlite:///' + path)
            connection = engine.connect()
            try:
                self.assertSequenceEqual(self.committed_ids(connection, 'departments'), [1, 2])
                self.assertSequenceEqual(self.committed_ids(connection, 'managers'), [10, 20])
                self.assertSequenceEqual(self.committed_ids(connection, 'stores'), [100, 200])
            finally:
                connection.close()
                engine.dispose()
        finally:
            os.remove(path)

    def test_all_rows_valid(self):
        managers = agate.Table(
            (
                (10, 'Alice', 1),
                (20, 'Bob', 2),
            ),
            ['id', 'name', 'department_id'],
            self.column_types,
        )
        departments = agate.Table(
            (
                (1, 'Engineering', 10),
                (2, 'Sales', 20),
            ),
            ['id', 'name', 'manager_id'],
            self.column_types,
        )

        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            rejected = agatesql.table.to_sql_group(
                {'departments': departments, 'managers': managers},
                connection,
                foreign_keys=self.foreign_keys,
                unique_constraints=self.unique_constraints,
            )

            self.assertEqual(rejected, {})
            self.assertSequenceEqual(self.committed_ids(connection, 'departments'), [1, 2])
            self.assertSequenceEqual(self.committed_ids(connection, 'managers'), [10, 20])
        finally:
            connection.close()
            engine.dispose()

    def test_table_set_to_sql(self):
        column_names = ['id', 'ref_id']
        column_types = [agate.Number(), agate.Number()]
        table_a = agate.Table(((1, 1), (2, 2)), column_names, column_types)
        table_b = agate.Table(((1, 2), (2, 1)), column_names, column_types)

        engine = create_engine(self.connection_string)
        connection = engine.connect()

        try:
            table_set = agate.TableSet([table_a, table_b], ['a', 'b'])
            rejected = table_set.to_sql(
                connection,
                foreign_keys={
                    'a': [('ref_id', 'b', 'id')],
                    'b': [('ref_id', 'a', 'id')],
                },
                unique_constraints={'a': ['id'], 'b': ['id']},
            )

            self.assertEqual(rejected, {})
            self.assertSequenceEqual(self.committed_ids(connection, 'a'), [1, 2])
            self.assertSequenceEqual(self.committed_ids(connection, 'b'), [1, 2])
        finally:
            connection.close()
            engine.dispose()
