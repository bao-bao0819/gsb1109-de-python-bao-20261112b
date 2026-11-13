from decimal import Decimal
from textwrap import dedent

import agate
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

import agatesql
from agatesql.decimal_storage import DecimalStorageContract, DecimalStorageError


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


class TestDecimalStorageContract(agate.AgateTestCase):
    def setUp(self):
        self.rows = (
            (Decimal('0.00000000000001'), 'tiny'),
            (Decimal('99999999999999999999'), 'huge'),
            (Decimal('12345678901234567890'), 'large'),
            (Decimal('-1.5'), 'negative'),
            (Decimal('1.500'), 'alternate-spelling'),
            (Decimal('2'), 'integer'),
            (None, 'missing'),
        )
        self.column_names = ['measurement', 'label']
        self.column_types = [agate.Number(), agate.Text()]
        self.table = agate.Table(self.rows, self.column_names, self.column_types)
        self.contract = DecimalStorageContract(
            minimum='-100000000000000000000',
            maximum='100000000000000000000',
            decimal_places=20,
        )

        self.engine = create_engine('sqlite:///:memory:')
        self.connection = self.engine.connect()

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()

    def test_round_trip_is_exact(self):
        self.table.to_sql(self.connection, 'test', decimal_contracts={'measurement': self.contract})

        table = agate.Table.from_sql(self.connection, 'test')

        self.assertColumnTypes(table, [agate.Number, agate.Text])
        self.assertRows(table, self.rows)
        self.assertEqual(type(table.rows[0][0]), Decimal)
        self.assertEqual(table.rows[0][0].as_tuple(), Decimal('0.00000000000001').as_tuple())
        self.assertEqual(table.rows[1][0], Decimal('99999999999999999999'))
        self.assertEqual(table.rows[4][0], Decimal('1.500'))

    def test_storage_is_text_not_binary_float(self):
        self.table.to_sql(self.connection, 'test', decimal_contracts={'measurement': self.contract})

        stored = self.connection.exec_driver_sql(
            'SELECT measurement, typeof(measurement) FROM test WHERE label = ?', ('tiny',)
        ).fetchone()
        self.assertEqual(stored, ('0.00000000000001', 'text'))

        stored = self.connection.exec_driver_sql(
            'SELECT measurement, typeof(measurement) FROM test WHERE label = ?', ('huge',)
        ).fetchone()
        self.assertEqual(stored, ('99999999999999999999', 'text'))

    def test_order_by_is_numeric(self):
        self.table.to_sql(self.connection, 'test', decimal_contracts={'measurement': self.contract})

        values = self.connection.exec_driver_sql(
            'SELECT measurement FROM test WHERE measurement IS NOT NULL ORDER BY measurement'
        ).scalars().all()
        self.assertEqual(values, [
            '-1.5',
            '0.00000000000001',
            '1.500',
            '2',
            '12345678901234567890',
            '99999999999999999999',
        ])

    def test_range_filter_is_numeric(self):
        self.table.to_sql(self.connection, 'test', decimal_contracts={'measurement': self.contract})

        labels = self.connection.exec_driver_sql(
            "SELECT label FROM test WHERE measurement BETWEEN '-2' AND '3' ORDER BY measurement"
        ).scalars().all()
        self.assertEqual(labels, ['negative', 'tiny', 'alternate-spelling', 'integer'])

        labels = self.connection.exec_driver_sql(
            'SELECT label FROM test WHERE measurement > 10000000000000000000 ORDER BY measurement'
        ).scalars().all()
        self.assertEqual(labels, ['large', 'huge'])

    def test_equal_values_with_different_spellings_match(self):
        self.table.to_sql(self.connection, 'test', decimal_contracts={'measurement': self.contract})

        labels = self.connection.exec_driver_sql(
            "SELECT label FROM test WHERE measurement = '1.5'"
        ).scalars().all()
        self.assertEqual(labels, ['alternate-spelling'])

    def test_sql_query_can_use_contract(self):
        results = self.table.sql_query(
            "SELECT measurement FROM agate WHERE measurement BETWEEN '-2' AND '2' ORDER BY measurement",
            decimal_contract=self.contract,
        )

        self.assertColumnTypes(results, [agate.Number])
        self.assertRows(results, [
            (Decimal('-1.5'),),
            (Decimal('0.00000000000001'),),
            (Decimal('1.500'),),
            (Decimal('2'),),
        ])

    def test_create_statement_uses_exact_decimal_storage(self):
        statement = self.table.to_sql_create_statement(
            'test_table',
            decimal_contracts={'measurement': self.contract},
        )

        self.assertIn('measurement AGATE_DECIMAL_TEXT(20) COLLATE AGATE_DECIMAL', statement)

    def test_contract_default_applies_to_all_number_columns(self):
        table = agate.Table(
            ((Decimal('1.5'), Decimal('2.5')),),
            ['a', 'b'],
            [agate.Number(), agate.Number()],
        )
        table.to_sql(self.connection, 'test', decimal_contract=DecimalStorageContract(decimal_places=2))

        statement = self.connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE name = 'test'"
        ).scalar()
        self.assertEqual(statement.count('AGATE_DECIMAL_TEXT(2) COLLATE AGATE_DECIMAL'), 2)

    def test_decimal_places_violation_reports_original_row(self):
        table = agate.Table(
            (
                (Decimal('1.25'), 'ok'),
                (Decimal('-1.25'), 'ok'),
                (Decimal('1.2345'), 'too-precise'),
            ),
            ['measurement', 'label'],
            [agate.Number(), agate.Text()],
        )

        with self.assertRaises(DecimalStorageError) as context:
            table.to_sql(
                self.connection,
                'test',
                decimal_contracts={'measurement': DecimalStorageContract(decimal_places=3)},
            )

        error = context.exception
        self.assertEqual(error.column_name, 'measurement')
        self.assertEqual(error.row_index, 2)
        self.assertEqual(error.row_number, 3)
        self.assertEqual(error.value, Decimal('1.2345'))
        self.assertEqual(error.reason, 'decimal_places')
        self.assertIn('row 3', error.args[0])

        # Nothing was silently rounded, and no table was created.
        self.assertFalse(self.connection.dialect.has_table(self.connection, 'test'))

    def test_minimum_violation_reports_original_row(self):
        table = agate.Table(
            ((Decimal('0.5'),), (Decimal('-0.5'),)),
            ['measurement'],
            [agate.Number()],
        )

        with self.assertRaises(DecimalStorageError) as context:
            table.to_sql(
                self.connection,
                'test',
                decimal_contracts={'measurement': DecimalStorageContract(minimum='0')},
            )

        error = context.exception
        self.assertEqual(error.row_index, 1)
        self.assertEqual(error.row_number, 2)
        self.assertEqual(error.reason, 'minimum')
        self.assertFalse(self.connection.dialect.has_table(self.connection, 'test'))

    def test_maximum_violation(self):
        table = agate.Table(
            ((Decimal('10.0001'),),),
            ['measurement'],
            [agate.Number()],
        )

        with self.assertRaises(DecimalStorageError) as context:
            table.to_sql(
                self.connection,
                'test',
                decimal_contracts={'measurement': DecimalStorageContract(maximum='10')},
            )

        self.assertEqual(context.exception.reason, 'maximum')

    def test_trailing_zeroes_do_not_count_as_decimal_places(self):
        table = agate.Table(
            ((Decimal('1.2300'),),),
            ['measurement'],
            [agate.Number()],
        )
        table.to_sql(
            self.connection,
            'test',
            decimal_contracts={'measurement': DecimalStorageContract(decimal_places=2)},
        )

        self.assertRows(agate.Table.from_sql(self.connection, 'test'), [(Decimal('1.2300'),)])

    def test_chunked_insert_with_contract_is_exact(self):
        table = agate.Table(
            ((Decimal('0.00000000000001'),), (Decimal('99999999999999999999'),), (Decimal('-1.5'),)),
            ['measurement'],
            [agate.Number()],
        )
        table.to_sql(
            self.connection,
            'test',
            chunk_size=1,
            decimal_contracts={'measurement': self.contract},
        )

        self.assertRows(agate.Table.from_sql(self.connection, 'test'), table.rows)

    def test_contract_dict_form(self):
        table = agate.Table(((Decimal('1.5'),),), ['measurement'], [agate.Number()])
        table.to_sql(
            self.connection,
            'test',
            decimal_contracts={'measurement': {'minimum': '0', 'maximum': '10', 'decimal_places': 1}},
        )

        self.assertRows(agate.Table.from_sql(self.connection, 'test'), [(Decimal('1.5'),)])

    def test_contract_rejects_unknown_column(self):
        with self.assertRaises(ValueError):
            self.table.to_sql(self.connection, 'test', decimal_contracts={'missing': self.contract})

    def test_contract_rejects_non_number_column(self):
        with self.assertRaises(ValueError):
            self.table.to_sql(self.connection, 'test', decimal_contracts={'label': self.contract})

    def test_contract_rejects_inverted_range(self):
        with self.assertRaises(ValueError):
            DecimalStorageContract(minimum='10', maximum='0')
