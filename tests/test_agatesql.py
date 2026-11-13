from decimal import Decimal
from textwrap import dedent

import agate
from sqlalchemy import create_engine
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


class TestSQLColumnOrigins(agate.AgateTestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        self.connection = self.engine.connect()
        self.connection.exec_driver_sql('create table contracts (id integer, name text)')
        self.connection.exec_driver_sql(
            'create table payments (id integer, contract_id integer, amount integer)'
        )
        self.connection.exec_driver_sql("insert into contracts values (1, 'c1'), (2, 'c2')")
        self.connection.exec_driver_sql('insert into payments values (10, 1, 100)')

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()

    def query(self, sql):
        return agate.Table.from_sql_query(sql, self.connection)

    def test_duplicate_label_does_not_steal_alias(self):
        table = self.query(
            'select c.id, p.id, p.amount as id_2 '
            'from contracts c left join payments p on c.id = p.contract_id '
            'order by c.id'
        )

        # ``id`` is duplicated, but ``id_2`` belongs to the third expression.
        self.assertSequenceEqual(table.column_names, ['id', 'id_3', 'id_2'])
        self.assertSequenceEqual(table.column_origins.labels, ['id', 'id', 'id_2'])
        self.assertEqual(table.column_origins.positions['id'][0], 0)
        self.assertEqual(table.column_origins.positions['id'][1], 1)
        self.assertEqual(table.column_origins.positions['id_2'], (2,))

        # A dictionary lookup of the alias returns the third column only.
        self.assertEqual(dict(table.rows[0])['id_2'], Decimal('100'))
        self.assertEqual(dict(table.rows[1])['id_2'], None)
        self.assertEqual(table.columns_by_label['id_2'][0].name, 'id_2')
    def test_left_join_null_row_keeps_column_ownership(self):
        table = self.query(
            'select c.id, p.id, p.amount as id_2 '
            'from contracts c left join payments p on c.id = p.contract_id '
            'order by c.id'
        )

        null_row = table.rows[1]
        self.assertEqual(tuple(null_row), (Decimal('2'), None, None))

        # Ownership comes from the SELECT list, not from the row values.
        self.assertEqual(
            [(origin.table, origin.column) for origin in table.column_origins],
            [('contracts', 'id'), ('payments', 'id'), ('payments', 'amount')],
        )
        self.assertEqual(table.column_sources, ('contracts', 'payments', 'payments'))
        self.assertEqual(table.column_positions['id'], (0, 1))

    def test_empty_result_keeps_same_origins(self):
        populated = self.query(
            'select c.id, p.id, p.amount as id_2 '
            'from contracts c left join payments p on c.id = p.contract_id '
        )
        empty = self.query(
            'select c.id, p.id, p.amount as id_2 '
            'from contracts c left join payments p on c.id = p.contract_id '
            'where c.id = 999'
        )

        self.assertSequenceEqual(empty.column_names, populated.column_names)
        self.assertSequenceEqual(empty.column_origins.labels, populated.column_origins.labels)
        self.assertSequenceEqual(
            [(origin.table, origin.column) for origin in empty.column_origins],
            [(origin.table, origin.column) for origin in populated.column_origins],
        )

    def test_expression_without_single_physical_table_is_unknown(self):
        table = self.query(
            'select c.id, p.id, c.id + p.id as id_2 '
            'from contracts c left join payments p on c.id = p.contract_id '
            'order by c.id'
        )

        self.assertSequenceEqual(table.column_names, ['id', 'id_3', 'id_2'])
        self.assertEqual(table.column_origins[2].label, 'id_2')
        self.assertIsNone(table.column_origins[2].table)
        self.assertIsNone(table.column_origins[2].column)
        self.assertTrue(table.column_origins[2].unknown)

    def test_literal_alias_is_unknown(self):
        table = self.query(
            'select c.id, p.id, 7 as id_2 '
            'from contracts c left join payments p on c.id = p.contract_id '
        )

        self.assertSequenceEqual(table.column_names, ['id', 'id_3', 'id_2'])
        self.assertIsNone(table.column_origins[2].table)
        self.assertEqual(dict(table.rows[0])['id_2'], Decimal('7'))

    def test_star_expansion_keeps_per_table_labels(self):
        table = self.query(
            'select c.*, p.* '
            'from contracts c left join payments p on c.id = p.contract_id '
            'order by c.id'
        )

        self.assertSequenceEqual(
            table.column_names, ['id', 'name', 'id_2', 'contract_id', 'amount']
        )
        self.assertSequenceEqual(table.column_labels, ['id', 'name', 'id', 'contract_id', 'amount'])
        self.assertSequenceEqual(table.column_positions['id'], (0, 2))
        self.assertSequenceEqual(
            [(origin.table, origin.column) for origin in table.column_origins[:3]],
            [('contracts', 'id'), ('contracts', 'name'), ('payments', 'id')],
        )

    def test_reserved_labels_are_never_used_for_renames(self):
        table = agate.Table.from_sql_query('select 1 as id, 2 as id, 3 as id_2')

        self.assertSequenceEqual(table.column_names, ['id', 'id_3', 'id_2'])
        self.assertSequenceEqual(table.column_labels, ['id', 'id', 'id_2'])

        table = agate.Table.from_sql_query('select 1 as id, 2 as id, 3 as id, 4 as id_2, 5 as id_3')
        self.assertSequenceEqual(table.column_names, ['id', 'id_4', 'id_5', 'id_2', 'id_3'])

    def test_sql_query_reports_origins(self):
        source = agate.Table([(1, 'c1'), (2, 'c2')], ['id', 'name'])

        table = source.sql_query('''
            create table payments (id integer, contract_id integer, amount integer);
            insert into payments values (10, 1, 100);
            select c.id, p.id, p.amount as id_2
            from agate c left join payments p on c.id = p.contract_id
            order by c.id
        ''')

        self.assertSequenceEqual(table.column_names, ['id', 'id_3', 'id_2'])
        self.assertSequenceEqual(table.column_labels, ['id', 'id', 'id_2'])
        self.assertSequenceEqual(table.column_positions['id'], (0, 1))
        self.assertSequenceEqual(
            [origin.table for origin in table.column_origins], ['agate', 'payments', 'payments']
        )
