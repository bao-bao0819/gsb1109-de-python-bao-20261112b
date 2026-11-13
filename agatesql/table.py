"""
This module contains the agatesql extensions to
:class:`Table <agate.table.Table>`.
"""

import datetime
import decimal
from urllib.parse import urlsplit

import agate
from sqlalchemy import Column, ForeignKeyConstraint, MetaData, Table, UniqueConstraint, column, create_engine, dialects
from sqlalchemy.dialects.mssql import BIT
from sqlalchemy.dialects.oracle import INTERVAL as ORACLE_INTERVAL
from sqlalchemy.dialects.postgresql import INTERVAL as POSTGRES_INTERVAL
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql import select
from sqlalchemy.types import BOOLEAN, DATE, DATETIME, DECIMAL, FLOAT, TEXT, TIMESTAMP, VARCHAR, Interval

SQL_TYPE_MAP = {
    agate.Boolean: None,  # See below
    agate.Number: None,  # See below
    agate.Date: DATE,
    agate.DateTime: None,  # See below
    agate.TimeDelta: None,  # See below
    agate.Text: VARCHAR,
}

DATETIME_MAP = {
    'mssql': DATETIME,
}

BOOLEAN_MAP = {
    'mssql': BIT,
}

NUMBER_MAP = {
    'crate': FLOAT,
    'sqlite': FLOAT,
}

INTERVAL_MAP = {
    'postgresql': POSTGRES_INTERVAL,
    'oracle': ORACLE_INTERVAL,
}


def get_engine_and_connection(connection_or_string=None):
    """
    Gets a connection to a specific SQL alchemy backend. If an existing
    connection is provided, it will be passed through. If no connection
    string is provided, then in in-memory SQLite database will be created.
    """
    if connection_or_string is None:
        engine = create_engine('sqlite:///:memory:')
        connection = engine.connect()
        return None, connection
    if isinstance(connection_or_string, Connection):
        connection = connection_or_string
        return None, connection

    kwargs = {}
    if urlsplit(connection_or_string).scheme == 'mssql+pyodbc':
        kwargs = {'fast_executemany': True}

    engine = create_engine(connection_or_string, **kwargs)
    connection = engine.connect()
    return engine, connection


def from_sql(cls, connection_or_string, table_name):
    """
    Create a new :class:`agate.Table` from a given SQL table. Types will be
    inferred from the database schema.

    Monkey patched as class method :meth:`Table.from_sql`.

    :param connection_or_string:
        An existing sqlalchemy connection or connection string.
    :param table_name:
        The name of a table in the referenced database.
    """
    engine, connection = get_engine_and_connection(connection_or_string)

    metadata = MetaData()
    sql_table = Table(table_name, metadata, autoload_with=connection)

    column_names = []
    column_types = []

    for sql_column in sql_table.columns:
        column_names.append(sql_column.name)

        if type(sql_column.type) in INTERVAL_MAP.values():
            py_type = datetime.timedelta
        else:
            py_type = sql_column.type.python_type

        if py_type in [int, float, decimal.Decimal]:
            if py_type is float:
                sql_column.type.asdecimal = True
            column_types.append(agate.Number())
        elif py_type is bool:
            column_types.append(agate.Boolean())
        elif issubclass(py_type, str):
            column_types.append(agate.Text())
        elif py_type is datetime.date:
            column_types.append(agate.Date())
        elif py_type is datetime.datetime:
            column_types.append(agate.DateTime())
        elif py_type is datetime.timedelta:
            column_types.append(agate.TimeDelta())
        else:
            raise ValueError('Unsupported sqlalchemy column type: %s' % type(sql_column.type))

    s = select(sql_table)

    rows = connection.execute(s)

    try:
        return agate.Table(rows, column_names, column_types)
    finally:
        if engine is not None:
            connection.close()
            engine.dispose()


def from_sql_query(self, query):
    """
    Create an agate table from the results of a SQL query. Note that column
    data types will be inferred from the returned data, not the column types
    declared in SQL (if any). This is more flexible than :func:`.from_sql` but
    could result in unexpected typing issues.

    :param query:
        A SQL query to execute.
    """
    _, connection = get_engine_and_connection()

    # Must escape '%'.
    # @see https://github.com/wireservice/csvkit/issues/440
    # @see https://bitbucket.org/zzzeek/sqlalchemy/commits/5bc1f17cb53248e7cea609693a3b2a9bb702545b
    rows = connection.execute(query.replace('%', '%%'))

    table = agate.Table(list(rows), column_names=rows._metadata.keys)

    return table


def make_sql_column(column_name, column, sql_type_kwargs=None, sql_column_kwargs=None, sql_column_type=None):
    """
    Creates a sqlalchemy column from agate column data.

    :param column_name:
        The name of the column.
    :param column:
        The agate column.
    :param sql_type_kwargs:
        Additional kwargs to passed through to the type constructor, such as ``length``.
    :param sql_column_kwargs:
        Additional kwargs to passed through to the Column constructor, such as ``nullable``.
    :param sql_column_type:
        The type of the column (optional).
    """
    if not sql_column_type:
        for agate_type, sql_type in SQL_TYPE_MAP.items():
            if isinstance(column.data_type, agate_type):
                sql_column_type = sql_type
                break

    if sql_column_type is None:
        raise ValueError('Unsupported column type: %s' % column.data_type)

    sql_type_kwargs = sql_type_kwargs or {}
    sql_column_kwargs = sql_column_kwargs or {}

    return Column(column_name, sql_column_type(**sql_type_kwargs), **sql_column_kwargs)


def make_sql_table(table, table_name, dialect=None, db_schema=None, constraints=True, unique_constraint=[],
                   connection=None, min_col_len=1, col_len_multiplier=1):
    """
    Generates a SQL alchemy table from an agate table.
    """
    metadata = MetaData()
    sql_table = Table(table_name, metadata, schema=db_schema)

    SQL_TYPE_MAP[agate.Boolean] = BOOLEAN_MAP.get(dialect, BOOLEAN)
    SQL_TYPE_MAP[agate.DateTime] = DATETIME_MAP.get(dialect, TIMESTAMP)
    SQL_TYPE_MAP[agate.Number] = NUMBER_MAP.get(dialect, DECIMAL)
    SQL_TYPE_MAP[agate.TimeDelta] = INTERVAL_MAP.get(dialect, Interval)

    for column_name, column in table.columns.items():
        sql_column_type = None
        sql_type_kwargs = {}
        sql_column_kwargs = {}

        if constraints:
            if isinstance(column.data_type, agate.Text) and dialect in ('ingres', 'mysql'):
                length = table.aggregate(agate.MaxLength(column_name)) * decimal.Decimal(col_len_multiplier)
                if (
                    # https://dev.mysql.com/doc/refman/8.2/en/string-type-syntax.html
                    dialect == 'mysql' and length > 21844  # 65,535 bytes divided by 3
                    # https://docs.actian.com/ingres/11.2/index.html#page/SQLRef/Character_Data_Types.htm
                    or dialect == 'ingres' and length > 10666  # 32,000 bytes divided by 3
                ):
                    sql_column_type = TEXT
                # If length is zero, SQLAlchemy may raise "VARCHAR requires a length on dialect mysql".
                else:
                    sql_type_kwargs['length'] = length if length >= min_col_len else min_col_len

            # PostgreSQL and SQLite don't have scale default 0.
            # @see https://www.postgresql.org/docs/9.2/static/datatype-numeric.html
            # @see https://www.sqlite.org/datatype3.html
            if isinstance(column.data_type, agate.Number) and dialect in ('ingres', 'mssql', 'mysql', 'oracle'):
                # Ingres has precision range 1-39 and default 5, scale default 0.
                # @see https://docs.actian.com/ingres/11.2/index.html#page/SQLRef/Storage_Formats_of_Data_Types.htm
                # MySQL has precision range 1-65 and default 10, scale default 0.
                # @see https://dev.mysql.com/doc/refman/8.2/en/fixed-point-types.html
                # Oracle has precision range 1-38 and default 38, scale default 0.
                # @see https://docs.oracle.com/cd/B28359_01/server.111/b28318/datatype.htm#CNCPT1832
                # SQL Server has range 1-38 and default 18, scale default 0.
                # @see https://docs.microsoft.com/en-us/sql/t-sql/data-types/decimal-and-numeric-transact-sql
                sql_type_kwargs['precision'] = 38
                sql_type_kwargs['scale'] = table.aggregate(agate.MaxPrecision(column_name))

            # Avoid errors due to NO_ZERO_DATE.
            # @see https://dev.mysql.com/doc/refman/8.2/en/sql-mode.html#sqlmode_no_zero_date
            if not isinstance(column.data_type, agate.DateTime):
                sql_column_kwargs['nullable'] = table.aggregate(agate.HasNulls(column_name))

        sql_table.append_column(make_sql_column(column_name, column,
                                sql_type_kwargs, sql_column_kwargs, sql_column_type))

    if unique_constraint:
        sql_table.append_constraint(UniqueConstraint(*unique_constraint))

    return sql_table


def to_sql(self, connection_or_string, table_name, overwrite=False,
           create=True, create_if_not_exists=False, insert=True, prefixes=[],
           db_schema=None, constraints=True, unique_constraint=[], chunk_size=None,
           min_col_len=1, col_len_multiplier=1):
    """
    Write this table to the given SQL database.

    Monkey patched as instance method :meth:`Table.to_sql`.

    :param connection_or_string:
        An existing sqlalchemy connection or a connection string.
    :param table_name:
        The name of the SQL table to create.
    :param overwrite:
        Drop any existing table with the same name before creating.
    :param create:
        Create the table.
    :param create_if_not_exists:
        When creating the table, don't fail if the table already exists.
    :param insert:
        Insert table data.
    :param prefixes:
        Add prefixes to the insert query.
    :param db_schema:
        Create table in the specified database schema.
    :param constraints:
        Generate constraints such as ``nullable`` for table columns.
    :param unique_constraint:
        The names of the columns to include in a UNIQUE constraint.
    :param chunk_size:
        Write rows in batches of this size. If not set, rows will be written at once.
    :param col_min_len:
        The minimum length of text columns.
    :param col_len_multiplier:
        Multiply the maximum column length by this multiplier to accomodate larger values in later runs.
    """
    engine, connection = get_engine_and_connection(connection_or_string)

    dialect = connection.engine.dialect.name
    sql_table = make_sql_table(self, table_name, dialect=dialect, db_schema=db_schema, constraints=constraints,
                               unique_constraint=unique_constraint, connection=connection,
                               min_col_len=min_col_len, col_len_multiplier=col_len_multiplier)

    if create:
        if overwrite:
            sql_table.drop(bind=connection, checkfirst=True)

        sql_table.create(bind=connection, checkfirst=create_if_not_exists)

    if insert:
        insert = sql_table.insert()
        for prefix in prefixes:
            insert = insert.prefix_with(prefix)
        if chunk_size is None:
            connection.execute(insert, [dict(zip(self.column_names, row)) for row in self.rows])
        else:
            number_of_rows = len(self.rows)
            for index in range((number_of_rows - 1) // chunk_size + 1):
                end_index = (index + 1) * chunk_size
                if end_index > number_of_rows:
                    end_index = number_of_rows
                connection.execute(insert, [dict(zip(self.column_names, row)) for row in
                                            self.rows[index * chunk_size:end_index]])

    try:
        return sql_table
    finally:
        if engine is not None:
            connection.close()
            engine.dispose()


def to_sql_create_statement(self, table_name, dialect=None, db_schema=None, constraints=True, unique_constraint=[]):
    """
    Generates a CREATE TABLE statement for this SQL table, but does not execute
    it.

    :param table_name:
        The name of the SQL table to create.
    :param dialect:
        The dialect of SQL to use for the table statement.
    :param db_schema:
        Create table in the specified database schema.
    :param constraints:
        Generate constraints such as ``nullable`` for table columns.
    :param unique_constraint:
        The names of the columns to include in a UNIQUE constraint.
    """
    sql_table = make_sql_table(self, table_name, dialect=dialect, db_schema=db_schema, constraints=constraints,
                               unique_constraint=unique_constraint)

    if dialect:
        sql_dialect = dialects.registry.load(dialect)()
    else:
        sql_dialect = None

    return str(CreateTable(sql_table).compile(dialect=sql_dialect)).strip() + ';'


def sql_query(self, query, table_name='agate'):
    """
    Convert this agate table into an intermediate, in-memory sqlite table,
    run a query against it, and then return the results as a new agate table.

    Multiple queries may be separated with semicolons.

    :param query:
        One SQL query, or multiple queries to be run consecutively separated
        with semicolons.
    :param table_name:
        The name to use for the table in the queries, defaults to ``agate``.
    """
    _, connection = get_engine_and_connection()

    # Execute the specified SQL queries
    queries = query.split(';')
    rows = None

    self.to_sql(connection, table_name)

    for q in queries:
        if q:
            rows = connection.exec_driver_sql(q)

    table = agate.Table(list(rows), column_names=rows._metadata.keys)

    return table


def _normalize_column_list(columns):
    if isinstance(columns, str):
        return [columns]
    return list(columns)


def _normalize_unique_constraint(columns_or_constraints):
    if not columns_or_constraints:
        return []
    if all(isinstance(c, str) for c in columns_or_constraints):
        return list(columns_or_constraints)
    # A list of column lists describes several constraints; only the first
    # one is used, matching Table.to_sql's single UNIQUE constraint support.
    return list(columns_or_constraints[0])


def _insert_group_rows(connection, sql_table, table, chunk_size):
    """
    Insert an agate table's rows and return a mapping of the SQLite ``rowid``
    of each inserted row to its index in the agate table.
    """
    insert = sql_table.insert().returning(column('rowid'))
    parameters = [dict(zip(table.column_names, row)) for row in table.rows]
    number_of_rows = len(parameters)
    step = chunk_size or number_of_rows or 1

    rowids = {}
    for start in range(0, number_of_rows, step):
        result = connection.execute(insert, parameters[start:start + step])
        for offset, (rowid,) in enumerate(result):
            rowids[rowid] = start + offset

    return rowids


def to_sql_group(tables, connection_or_string, foreign_keys=None, unique_constraints=None,
                 overwrite=False, constraints=True, chunk_size=None,
                 min_col_len=1, col_len_multiplier=1):
    """
    Write a group of agate tables to a SQLite database as a single batch.

    Tables are created if needed and all rows are inserted inside one
    transaction with foreign key enforcement deferred until the batch has
    been validated, so rows may reference rows in any other table of the
    batch — including circular references — as well as rows already present
    in the database. The order of the tables does not affect which rows are
    committed.

    Rows whose foreign keys cannot be resolved — directly or because the
    rows they referenced were themselves rejected — are removed before
    committing and reported to the caller. Tables without foreign keys are
    loaded unconditionally. Foreign key enforcement is never disabled: it
    is enabled on the connection if it was not already, and all checks are
    performed with ``PRAGMA foreign_key_check`` before the transaction
    commits.

    SQLite only.

    :param tables:
        A mapping of SQL table names to :class:`agate.Table` instances, or
        an :class:`agate.TableSet`.
    :param connection_or_string:
        An existing sqlalchemy connection or a connection string. If a
        connection string is given, the transaction is committed and the
        connection closed before returning. If a connection is given, the
        caller remains responsible for committing.
    :param foreign_keys:
        A mapping of table name to a list of foreign key declarations, each
        a ``(columns, referred_table, referred_columns)`` tuple. ``columns``
        and ``referred_columns`` may be a single column name or a list of
        names. The referred columns must be the primary key of, or have a
        unique constraint on, the referred table (see
        ``unique_constraints``), as SQLite requires. Referred tables that
        are not part of the batch must already exist in the database.
    :param unique_constraints:
        A mapping of table name to the names of the columns to include in a
        UNIQUE constraint when creating that table (or a list of such column
        lists for several constraints).
    :param overwrite:
        Drop any existing batch tables with the same names before creating.
    :param constraints:
        Generate constraints such as ``nullable`` for table columns.
    :param chunk_size:
        Write rows in batches of this size. If not set, rows will be written
        at once.
    :param col_min_len:
        The minimum length of text columns.
    :param col_len_multiplier:
        Multiply the maximum column length by this multiplier to accomodate
        larger values in later runs.
    :returns:
        A dictionary mapping table names to :class:`agate.Table` instances
        containing the rows that could not be committed because of
        unresolvable foreign key references. Empty if every row was
        committed.
    """
    engine, connection = get_engine_and_connection(connection_or_string)

    try:
        if connection.engine.dialect.name != 'sqlite':
            raise ValueError('to_sql_group is only supported for SQLite databases.')

        foreign_keys = foreign_keys or {}
        unique_constraints = unique_constraints or {}
        tables = dict(tables.items())

        # Enforce foreign keys (never disable them) and defer their checks
        # until commit, for this transaction only, so that circular
        # references can be inserted in any order.
        connection.exec_driver_sql('PRAGMA foreign_keys = ON')
        connection.exec_driver_sql('PRAGMA defer_foreign_keys = ON')

        metadata = MetaData()
        sql_tables = {}
        for name, table in tables.items():
            sql_table = make_sql_table(table, name, dialect='sqlite', constraints=constraints,
                                       unique_constraint=_normalize_unique_constraint(
                                           unique_constraints.get(name)),
                                       min_col_len=min_col_len, col_len_multiplier=col_len_multiplier)
            sql_tables[name] = sql_table.to_metadata(metadata)

        normalized_foreign_keys = {}
        for name, declarations in foreign_keys.items():
            if name not in sql_tables:
                raise ValueError('Foreign keys declared for table not in this batch: %s' % name)
            normalized = []
            for columns, referred_table, referred_columns in declarations:
                if referred_table not in sql_tables and referred_table not in metadata.tables:
                    # Referred tables outside the batch must already exist.
                    Table(referred_table, metadata, autoload_with=connection)
                normalized.append((_normalize_column_list(columns), referred_table,
                                   _normalize_column_list(referred_columns)))
            normalized_foreign_keys[name] = normalized

        for name, declarations in normalized_foreign_keys.items():
            for columns, referred_table, referred_columns in declarations:
                sql_tables[name].append_constraint(ForeignKeyConstraint(
                    columns,
                    ['%s.%s' % (referred_table, referred_column) for referred_column in referred_columns],
                    deferrable=True, initially='DEFERRED'))

        for sql_table in sql_tables.values():
            if overwrite:
                sql_table.drop(bind=connection, checkfirst=True)
            sql_table.create(bind=connection, checkfirst=True)

        # Violations that existed before this batch are not ours to fix.
        preexisting_violations = {
            (table_name, rowid)
            for table_name, rowid, _, _ in connection.exec_driver_sql('PRAGMA foreign_key_check')
        }

        rowids = {}
        for name, table in tables.items():
            rowids[name] = _insert_group_rows(connection, sql_tables[name], table, chunk_size)

        # Repeatedly remove batch rows with unresolvable references until
        # only valid rows remain. Removing a row can orphan rows that
        # reference it, so this runs to a fixed point, which is independent
        # of the order the tables were given in.
        identifier_preparer = connection.engine.dialect.identifier_preparer
        rejected = {name: [] for name in tables}

        while True:
            unresolvable = [
                (table_name, rowid)
                for table_name, rowid, _, _ in connection.exec_driver_sql('PRAGMA foreign_key_check')
                if table_name in rowids
                and (table_name, rowid) not in preexisting_violations
                and rowid in rowids[table_name]
            ]
            if not unresolvable:
                break
            for table_name, rowid in unresolvable:
                index = rowids[table_name].pop(rowid)
                rejected[table_name].append(tables[table_name].rows[index])
                connection.exec_driver_sql(
                    'DELETE FROM %s WHERE rowid = ?' % identifier_preparer.quote(table_name), (rowid,))

        if engine is not None:
            connection.commit()

        return {
            name: agate.Table(rows, tables[name].column_names, tables[name].column_types)
            for name, rows in rejected.items()
            if rows
        }
    finally:
        if engine is not None:
            connection.close()
            engine.dispose()


def tableset_to_sql(self, connection_or_string, **kwargs):
    """
    Write this table set to a SQLite database as a single batch. See
    :func:`.to_sql_group` for details. Note that an
    :class:`agate.TableSet` requires all tables to share the same columns;
    use :func:`.to_sql_group` directly with a dictionary for heterogeneous
    tables.

    Monkey patched as instance method :meth:`TableSet.to_sql`.
    """
    return to_sql_group(self, connection_or_string, **kwargs)


agate.Table.from_sql = classmethod(from_sql)
agate.Table.from_sql_query = classmethod(from_sql_query)
agate.Table.to_sql = to_sql
agate.Table.to_sql_create_statement = to_sql_create_statement
agate.Table.sql_query = sql_query
agate.TableSet.to_sql = tableset_to_sql
