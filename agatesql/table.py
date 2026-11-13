"""
This module contains the agatesql extensions to
:class:`Table <agate.table.Table>`.
"""

import datetime
import decimal
from urllib.parse import urlsplit

import agate
from sqlalchemy import Column, MetaData, Table, UniqueConstraint, create_engine, dialects
from sqlalchemy.dialects.mssql import BIT
from sqlalchemy.dialects.oracle import INTERVAL as ORACLE_INTERVAL
from sqlalchemy.dialects.postgresql import INTERVAL as POSTGRES_INTERVAL
from sqlalchemy.engine import Connection
from sqlalchemy.schema import CreateTable
from sqlalchemy.sql import and_, func, select, tuple_
from sqlalchemy.types import BOOLEAN, DATE, DATETIME, DECIMAL, FLOAT, TEXT, TIMESTAMP, VARCHAR, Interval

MERGE_CONFLICT_REASON_COLUMN = 'conflict_reason'
MERGE_CONFLICT_REASON = 'different content was published for the same version'

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


def _validate_versioned_merge(table, dialect, version_column, key_columns, action_column,
                              insert_action, cancel_action):
    """
    Validates the arguments and source data for a version-based merge.
    """
    if dialect != 'sqlite':
        raise ValueError('The merge_by_version option is only supported on SQLite.')
    if not key_columns:
        raise ValueError('key_columns must name at least one column.')

    column_names = table.column_names

    if version_column not in column_names:
        raise ValueError('version_column "%s" is not a column of the table.' % version_column)
    if action_column not in column_names:
        raise ValueError('action_column "%s" is not a column of the table.' % action_column)

    for key_column in key_columns:
        if key_column not in column_names:
            raise ValueError('Key column "%s" is not a column of the table.' % key_column)
        if key_column in (version_column, action_column):
            raise ValueError('Key columns must not include the version or action columns.')

    if len(set(key_columns)) != len(key_columns):
        raise ValueError('key_columns must not contain duplicates.')

    if any(version is None for version in table.columns[version_column].values()):
        raise ValueError('The version column must not contain null values.')

    for key_column in key_columns:
        if any(value is None for value in table.columns[key_column].values()):
            raise ValueError('Key column "%s" must not contain null values.' % key_column)

    valid_actions = {insert_action, cancel_action}
    if any(action not in valid_actions for action in table.columns[action_column].values()):
        raise ValueError(
            'The action column must only contain %r or %r.' % (insert_action, cancel_action))


def _make_auxiliary_table(connection, table_name, sql_table, extra_columns=None):
    """
    Creates (or recreates) a table mirroring the target table's columns.
    """
    metadata = MetaData()
    auxiliary = Table(table_name, metadata)
    for column in sql_table.columns:
        auxiliary.append_column(Column(column.name, column.type))
    for column in extra_columns or []:
        auxiliary.append_column(column)
    auxiliary.drop(bind=connection, checkfirst=True)
    auxiliary.create(bind=connection)
    return auxiliary


def _versioned_merge(connection, table, sql_table, key_columns, version_column,
                     action_column, insert_action, cancel_action, conflict_table_name,
                     chunk_size):
    """
    Merges the source rows into the target table with set-based SQL, so the
    result does not depend on the order of the source rows or of the import
    chunks.

    Exact re-sends (same key, version and full content) collapse into one row.
    When the same key and version carry different content, none of those rows
    is applied and every distinct variant is written to the conflict table. A
    cancellation at the latest version of a key removes the row, regardless of
    where the cancellation or earlier versions appear in the source data.
    """
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    from sqlalchemy.sql import exists, literal

    column_names = list(table.column_names)
    staging_name = '%s__agatesql_staging' % sql_table.name
    staging = _make_auxiliary_table(connection, staging_name, sql_table)

    conflict_table = _make_auxiliary_table(
        connection, conflict_table_name, sql_table,
        extra_columns=[Column(MERGE_CONFLICT_REASON_COLUMN, TEXT)])

    try:
        # Load the whole batch first; chunking only affects this staging load,
        # never the merge outcome.
        staging_insert = staging.insert()
        staging_rows = [dict(zip(column_names, row)) for row in table.rows]
        if chunk_size is None:
            connection.execute(staging_insert, staging_rows)
        else:
            for index in range(0, len(staging_rows), chunk_size):
                connection.execute(staging_insert, staging_rows[index:index + chunk_size])

        # Exact re-sends are indistinguishable rows, so DISTINCT collapses them.
        dedupe = select(staging).distinct().subquery('dedupe')

        # Same key and version with more than one distinct content: conflict.
        conflict_groups = select(
            *[dedupe.c[name] for name in key_columns],
            dedupe.c[version_column].label('_conflict_version'),
        ).group_by(
            *[dedupe.c[name] for name in key_columns],
            dedupe.c[version_column],
        ).having(func.count() > 1).subquery('conflict_groups')

        conflict_match = and_(
            *[dedupe.c[name] == conflict_groups.c[name] for name in key_columns],
            dedupe.c[version_column] == conflict_groups.c._conflict_version,
        )
        conflict_select = select(
            *[dedupe.c[name] for name in column_names],
            literal(MERGE_CONFLICT_REASON).label(MERGE_CONFLICT_REASON_COLUMN),
        ).select_from(dedupe.join(conflict_groups, conflict_match))
        connection.execute(
            conflict_table.insert().from_select(
                column_names + [MERGE_CONFLICT_REASON_COLUMN], conflict_select))

        # Latest published version of each key within this batch.
        latest = select(
            *[dedupe.c[name] for name in key_columns],
            func.max(dedupe.c[version_column]).label('_max_version'),
        ).group_by(*[dedupe.c[name] for name in key_columns]).subquery('latest')

        latest_match = and_(
            *[dedupe.c[name] == latest.c[name] for name in key_columns],
            dedupe.c[version_column] == latest.c._max_version,
        )
        latest_rows = select(dedupe).select_from(
            dedupe.join(latest, latest_match)).subquery('latest_rows')

        # A cancellation at the latest version wins over every earlier version.
        cancel_keys = select(
            *[latest_rows.c[name] for name in key_columns]
        ).where(latest_rows.c[action_column] == cancel_action)
        connection.execute(
            sql_table.delete().where(
                tuple_(*[sql_table.c[name] for name in key_columns]).in_(cancel_keys)))

        # Apply the latest version unless it was cancelled or is conflicting.
        not_conflicted = ~exists().where(and_(
            *[conflict_groups.c[name] == latest_rows.c[name] for name in key_columns],
            conflict_groups.c._conflict_version == latest_rows.c[version_column],
        ))
        winner_select = select(
            *[latest_rows.c[name] for name in column_names]
        ).where(
            latest_rows.c[action_column] == insert_action
        ).where(not_conflicted)

        upsert = sqlite_insert(sql_table).from_select(column_names, winner_select)
        update_columns = [name for name in column_names if name not in key_columns]
        connection.execute(
            upsert.on_conflict_do_update(
                index_elements=key_columns,
                set_={name: upsert.excluded[name] for name in update_columns},
                # Never let an older version overwrite a newer one already stored.
                where=upsert.excluded[version_column] >= sql_table.c[version_column],
            ))
    finally:
        staging.drop(bind=connection, checkfirst=True)

    return conflict_table


def to_sql(self, connection_or_string, table_name, overwrite=False,
           create=True, create_if_not_exists=False, insert=True, prefixes=[],
           db_schema=None, constraints=True, unique_constraint=[], chunk_size=None,
           min_col_len=1, col_len_multiplier=1, merge_by_version=False,
           version_column='version', key_columns=None, action_column='action',
           insert_action='insert', cancel_action='cancel', conflict_table_name=None):
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
    :param merge_by_version:
        Merge the rows into the table by source version instead of blindly
        inserting them. Only supported on SQLite. Rows sharing the key columns
        are reconciled on ``version_column``: exact re-sends (identical rows)
        are stored once, different content published under the same version is
        quarantined in a conflict table instead of being applied, and a
        cancellation at the latest version removes the row. The merge is built
        from set-based SQL, so it is unaffected by row order or chunk
        boundaries. The target table carries a UNIQUE constraint on
        ``key_columns``.
    :param version_column:
        Name of the column holding the source version of each row.
    :param key_columns:
        Columns identifying one order/entity, e.g. the order number. Required
        when ``merge_by_version`` is enabled.
    :param action_column:
        Name of the column holding the write/cancel action of each row.
    :param insert_action:
        Value of the action column that writes (or re-publishes) a row.
    :param cancel_action:
        Value of the action column that cancels a row.
    :param conflict_table_name:
        Name of the table receiving conflicting rows. Defaults to
        ``"<table_name>_conflicts"``. The table is recreated on every merge.
    """
    engine, connection = get_engine_and_connection(connection_or_string)

    dialect = connection.engine.dialect.name

    if merge_by_version:
        if key_columns is None:
            raise ValueError('key_columns is required when merge_by_version is enabled.')
        key_columns = list(key_columns)
        _validate_versioned_merge(
            self, dialect, version_column, key_columns, action_column,
            insert_action, cancel_action)
        # ON CONFLICT needs a matching unique constraint on the key columns.
        unique_constraint = list(key_columns)
        if conflict_table_name is None:
            conflict_table_name = '%s_conflicts' % table_name

    sql_table = make_sql_table(self, table_name, dialect=dialect, db_schema=db_schema, constraints=constraints,
                               unique_constraint=unique_constraint, connection=connection,
                               min_col_len=min_col_len, col_len_multiplier=col_len_multiplier)

    if create:
        if overwrite:
            sql_table.drop(bind=connection, checkfirst=True)

        sql_table.create(bind=connection, checkfirst=create_if_not_exists)

    if insert:
        if merge_by_version:
            _versioned_merge(
                connection, self, sql_table, key_columns, version_column,
                action_column, insert_action, cancel_action, conflict_table_name,
                chunk_size)
        else:
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


agate.Table.from_sql = classmethod(from_sql)
agate.Table.from_sql_query = classmethod(from_sql_query)
agate.Table.to_sql = to_sql
agate.Table.to_sql_create_statement = to_sql_create_statement
agate.Table.sql_query = sql_query
