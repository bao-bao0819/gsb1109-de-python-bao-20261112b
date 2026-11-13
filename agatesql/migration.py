"""
Structural comparison between an agate table and an existing SQL table.

``agate-sql`` can already generate ``CREATE TABLE`` SQL for an agate table. This
module handles the next deployment question: the target database may already
contain a table with the same name and live data. A preflight classifies the
differences so a maintainer can decide whether the change is a presentation-only
difference, an in-place column addition, or a rebuild that must preserve the
existing constraints and data.
"""

import datetime
import decimal
import json
import re
from collections import OrderedDict
from dataclasses import dataclass

import agate
from sqlalchemy import (
    CheckConstraint, Column, ForeignKeyConstraint, Index, Integer, MetaData,
    PrimaryKeyConstraint, Table, UniqueConstraint, dialects, inspect, text,
)
from sqlalchemy.schema import CreateColumn, CreateIndex, CreateTable, DefaultClause

from agatesql.table import get_engine_and_connection, make_sql_table

DISPLAY_ORDER = 'display_order'
ADDITIVE = 'append_column'
REBUILD = 'rebuild_required'

ACTION_LABELS = OrderedDict([
    (DISPLAY_ORDER, 'display_order_only'),
    (ADDITIVE, 'append_column'),
    (REBUILD, 'rebuild_required'),
])

_AGATE_PROBES = (
    (agate.Boolean, True),
    (agate.Number, decimal.Decimal('1')),
    (agate.Date, datetime.date(2000, 1, 1)),
    (agate.DateTime, datetime.datetime(2000, 1, 1)),
    (agate.TimeDelta, datetime.timedelta(seconds=1)),
    (agate.Text, 'a'),
)

_TYPE_FAMILIES = (
    {'DECIMAL', 'NUMERIC', 'FLOAT', 'REAL', 'DOUBLE', 'DOUBLE PRECISION',
     'INTEGER', 'INT', 'BIGINT', 'SMALLINT', 'TINYINT'},
    {'VARCHAR', 'TEXT', 'CHAR', 'CHARACTER', 'CHARACTER VARYING', 'NVARCHAR', 'NCHAR', 'STRING'},
    {'TIMESTAMP', 'DATETIME'},
)


@dataclass(frozen=True)
class ColumnChange:
    """One difference between the live table and the agate table."""

    name: str
    action: str
    existing_type: str = None
    desired_type: str = None
    existing_nullable: bool = None
    desired_nullable: bool = None
    existing_default: str = None
    desired_default: str = None
    detail: str = ''
    existing_order: tuple = None
    desired_order: tuple = None

    @property
    def label(self):
        return ACTION_LABELS[self.action]

    @property
    def requires_rebuild(self):
        return self.action == REBUILD

    def _asdict(self):
        return {
            'name': self.name,
            'action': self.action,
            'label': self.label,
            'existing_type': self.existing_type,
            'desired_type': self.desired_type,
            'existing_nullable': self.existing_nullable,
            'desired_nullable': self.desired_nullable,
            'existing_default': self.existing_default,
            'desired_default': self.desired_default,
            'detail': self.detail,
            'existing_order': list(self.existing_order) if self.existing_order else None,
            'desired_order': list(self.desired_order) if self.desired_order else None,
        }


@dataclass(frozen=True)
class BlockingRow:
    """A live row that prevents a NOT NULL or UNIQUE change from being applied."""

    row_number: int
    row_id: object
    values: dict
    reason: str

    def _asdict(self):
        return {
            'row_number': self.row_number,
            'row_id': self.row_id,
            'values': self.values,
            'reason': self.reason,
        }


@dataclass(frozen=True)
class MigrationPlan:
    """Result of comparing an agate table with a live SQL table."""

    table_name: str
    dialect: str
    table_exists: bool
    changes: tuple = ()
    blocking_rows: tuple = ()
    rebuild_reasons: tuple = ()
    statements: tuple = ()
    create_statement: str = None
    fingerprint: tuple = ()
    preserved_indexes: tuple = ()
    preserved_checks: tuple = ()
    preserved_foreign_keys: tuple = ()
    preserved_primary_key: tuple = ()
    preserved_uniques: tuple = ()
    options: tuple = ()

    @property
    def display_only(self):
        return (
            bool(self.changes)
            and all(change.action == DISPLAY_ORDER for change in self.changes)
            and not self.rebuild_reasons
            and not self.blocking_rows
        )

    @property
    def additive_columns(self):
        return tuple(change.name for change in self.changes if change.action == ADDITIVE)

    @property
    def rebuild_columns(self):
        return tuple(change.name for change in self.changes if change.action == REBUILD)

    @property
    def requires_rebuild(self):
        return bool(self.rebuild_reasons)

    @property
    def has_blocking_rows(self):
        return bool(self.blocking_rows)

    @property
    def safe_to_apply(self):
        """True when applying the plan cannot be blocked by existing data."""
        return not self.has_blocking_rows

    @property
    def sql(self):
        return '\n'.join(self.statements)

    @property
    def display_order_columns(self):
        for change in self.changes:
            if change.action == DISPLAY_ORDER:
                return change.desired_order or ()
        return ()

    def as_dict(self):
        return {
            'table_name': self.table_name,
            'dialect': self.dialect,
            'table_exists': self.table_exists,
            'display_only': self.display_only,
            'requires_rebuild': self.requires_rebuild,
            'has_blocking_rows': self.has_blocking_rows,
            'changes': [change._asdict() for change in self.changes],
            'blocking_rows': [row._asdict() for row in self.blocking_rows],
            'rebuild_reasons': list(self.rebuild_reasons),
            'statements': list(self.statements),
            'create_statement': self.create_statement,
            'preserved_indexes': list(self.preserved_indexes),
            'preserved_checks': list(self.preserved_checks),
            'preserved_foreign_keys': list(self.preserved_foreign_keys),
            'preserved_primary_key': list(self.preserved_primary_key),
            'preserved_uniques': [list(columns) for columns in self.preserved_uniques],
            'options': list(self.options),
        }

    def to_json(self):
        return json.dumps(self.as_dict(), ensure_ascii=False, default=str, indent=2)


def _dialect_name(connection, dialect):
    if dialect:
        return dialect
    if connection is not None:
        return connection.engine.dialect.name
    return 'sqlite'


def _sql_type_name(sql_type):
    return str(sql_type).upper()


def _type_agrees(existing_column, desired_column):
    existing = _sql_type_name(existing_column.type)
    desired = _sql_type_name(desired_column.type)
    if existing == desired:
        return True

    existing_base = existing.split('(')[0].strip()
    desired_base = desired.split('(')[0].strip()
    if existing_base == desired_base:
        return True

    for family in _TYPE_FAMILIES:
        if existing_base in family and desired_base in family:
            return True
    return False


def _raw_default(column):
    if column.server_default is None:
        return None
    default = column.server_default
    if hasattr(default, 'arg'):
        return str(default.arg)
    return str(default)


def _normalize_default(value):
    if value is None:
        return None
    normalized = value.strip()
    if len(normalized) >= 2 and normalized[0] == normalized[-1] and normalized[0] in ('"', "'"):
        normalized = normalized[1:-1]
    return normalized


def _quote(connection, name):
    return connection.dialect.identifier_preparer.quote(name)


def _constraint_columns(constraint):
    return tuple(column.name for column in constraint.columns)


def _unique_signatures(table):
    signatures = set()
    for constraint in table.constraints:
        if isinstance(constraint, UniqueConstraint):
            signatures.add(_constraint_columns(constraint))
    for index in table.indexes:
        if index.unique:
            signatures.add(tuple(column.name for column in index.columns))
    return signatures


def _table_fingerprint(connection, table_name, db_schema, existing_table):
    """Snapshot the live shape and row count so apply can detect drift."""
    if existing_table is None:
        return ('missing', table_name)
    inspector = inspect(connection)
    column_spec = tuple(
        (column['name'], str(column['type']), bool(column['nullable']), column.get('default'))
        for column in inspector.get_columns(table_name, schema=db_schema)
    )
    unique_spec = tuple(sorted(
        tuple(item['column_names'])
        for item in inspector.get_unique_constraints(table_name, schema=db_schema)
    ))
    index_spec = tuple(sorted(
        tuple(item['column_names'])
        for item in inspector.get_indexes(table_name, schema=db_schema)
    ))
    row_count = connection.execute(
        text('SELECT COUNT(*) FROM %s' % _quote(connection, table_name))
    ).scalar()
    return (table_name, column_spec, unique_spec, index_spec, int(row_count or 0))


def _desired_table(table, table_name, dialect, db_schema, constraints, unique_constraint,
                   min_col_len, col_len_multiplier, column_defaults=None):
    sql_table = make_sql_table(
        table, table_name, dialect=dialect, db_schema=db_schema, constraints=constraints,
        unique_constraint=unique_constraint, min_col_len=min_col_len,
        col_len_multiplier=col_len_multiplier,
    )
    if column_defaults:
        for name, default in column_defaults.items():
            if name not in sql_table.columns:
                raise ValueError('Unknown column in column_defaults: %s' % name)
            sql_table.columns[name].server_default = _default_clause(default)
    return sql_table


def _default_clause(value):
    if value is None:
        return None
    if isinstance(value, bool):
        return DefaultClause(text('1' if value else '0'))
    if isinstance(value, (int, float, decimal.Decimal)):
        return DefaultClause(text(str(value)))
    if isinstance(value, datetime.datetime):
        return DefaultClause(text("'%s'" % value.isoformat(sep=' ')))
    if isinstance(value, datetime.date):
        return DefaultClause(text("'%s'" % value.isoformat()))
    if isinstance(value, datetime.timedelta):
        return DefaultClause(text("'%s'" % value))
    return DefaultClause(text("'%s'" % str(value).replace("'", "''")))


def _compile_create(table, dialect):
    return str(CreateTable(table).compile(dialect=dialect)).strip() + ';'


def _reflected_constraints(connection, table_name, db_schema, columns):
    """Collect constraints agate cannot express so the rebuild keeps them."""
    inspector = inspect(connection)
    column_names = set(columns)

    checks = []
    for check in inspector.get_check_constraints(table_name, schema=db_schema):
        sqltext = check.get('sqltext')
        if sqltext:
            checks.append((check.get('name'), sqltext))

    foreign_keys = []
    for foreign_key in inspector.get_foreign_keys(table_name, schema=db_schema):
        constrained = tuple(foreign_key['constrained_columns'])
        if not all(name in column_names for name in constrained):
            continue
        foreign_keys.append((
            foreign_key.get('name'), constrained, foreign_key.get('referred_schema'),
            foreign_key['referred_table'], tuple(foreign_key['referred_columns']),
        ))

    primary_key = inspector.get_pk_constraint(table_name, schema=db_schema)
    pk_columns = tuple(primary_key.get('constrained_columns') or [])
    if not all(name in column_names for name in pk_columns):
        pk_columns = ()
    return tuple(checks), tuple(foreign_keys), pk_columns


def _attach_reflected_constraints(table, constraints, source_table_name, metadata):
    """
    Attach reflected CHECK, FK, and PK constraints to the rebuild table.

    ``table`` is the temporary table. A self-referential foreign key is pointed
    at the temporary table itself, so that after the final rename it points at
    the replacement table. Other foreign keys get a stub referenced table,
    because SQLAlchemy needs one to render the constraint.
    """
    checks, foreign_keys, primary_key = constraints

    pk_names = {column.name for column in table.primary_key.columns}
    if primary_key and not set(primary_key).issubset(pk_names):
        PrimaryKeyConstraint(*(table.columns[name] for name in primary_key))

    existing_checks = {
        str(constraint.sqltext) for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.sqltext is not None
    }
    for name, sqltext in checks:
        if sqltext in existing_checks:
            continue
        CheckConstraint(text(sqltext), name=name, table=table)

    for name, constrained, referred_schema, referred_table, referred_columns in foreign_keys:
        if any(isinstance(constraint, ForeignKeyConstraint)
               and tuple(column.name for column in constraint.columns) == constrained
               for constraint in table.constraints):
            continue

        if referred_table == source_table_name and not referred_schema:
            target_table = table
        else:
            key = '%s.%s' % (referred_schema, referred_table) if referred_schema else referred_table
            if key not in metadata.tables:
                Table(referred_table, metadata, *[
                    Column(referred_name, Integer)
                    for referred_name in referred_columns
                ], schema=referred_schema)
            target_table = metadata.tables[key]

        ForeignKeyConstraint(
            [table.columns[column_name] for column_name in constrained],
            [target_table.columns[column_name] for column_name in referred_columns],
            name=name,
            table=table,
        )


def _diagnose_columns(existing_columns, desired_columns, existing_count):
    changes = []

    for name in desired_columns:
        if name not in existing_columns:
            desired_column = desired_columns[name]
            has_default = _raw_default(desired_column) is not None
            if desired_column.nullable or has_default or existing_count == 0:
                changes.append(ColumnChange(
                    name, ADDITIVE, desired_type=_sql_type_name(desired_column.type),
                    desired_nullable=bool(desired_column.nullable),
                    desired_default=_raw_default(desired_column),
                    detail='new column can be appended',
                ))
            else:
                changes.append(ColumnChange(
                    name, REBUILD, desired_type=_sql_type_name(desired_column.type),
                    desired_nullable=False,
                    desired_default=None,
                    detail='new NOT NULL column has no default for existing rows',
                ))

    for name in existing_columns:
        if name not in desired_columns:
            changes.append(ColumnChange(
                name, REBUILD, existing_type=_sql_type_name(existing_columns[name].type),
                existing_nullable=bool(existing_columns[name].nullable),
                existing_default=_raw_default(existing_columns[name]),
                detail='column is not present in the agate table',
            ))

    for name in desired_columns:
        if name not in existing_columns:
            continue
        existing_column = existing_columns[name]
        desired_column = desired_columns[name]

        reasons = []
        if not _type_agrees(existing_column, desired_column):
            reasons.append('type %s -> %s' % (_sql_type_name(existing_column.type),
                                              _sql_type_name(desired_column.type)))
        if existing_column.nullable != desired_column.nullable:
            if existing_column.nullable and not desired_column.nullable:
                reasons.append('nullable -> NOT NULL')
            else:
                reasons.append('NOT NULL -> nullable')
        if _normalize_default(_raw_default(existing_column)) != _normalize_default(_raw_default(desired_column)):
            reasons.append('default %r -> %r' % (_raw_default(existing_column), _raw_default(desired_column)))

        if reasons:
            changes.append(ColumnChange(
                name, REBUILD, _sql_type_name(existing_column.type),
                _sql_type_name(desired_column.type), bool(existing_column.nullable),
                bool(desired_column.nullable), _raw_default(existing_column),
                _raw_default(desired_column), '; '.join(reasons),
            ))

    common_existing = [name for name in existing_columns if name in desired_columns]
    common_desired = [name for name in desired_columns if name in existing_columns]
    if common_existing and common_existing != common_desired:
        changes.append(ColumnChange(
            '<column order>', DISPLAY_ORDER,
            detail='same columns, different display order; no DDL needed',
            existing_order=tuple(common_existing), desired_order=tuple(common_desired),
        ))

    return changes


def _find_blockers(connection, table_name, existing_columns, desired_columns, desired_table,
                   max_rows=None):
    blockers = []

    def limit_clause():
        if max_rows is None:
            return ''
        return ' LIMIT %d' % int(max_rows)

    def fetch(where, reason):
        # rowid is not a declared column and only exists on ordinary SQLite
        # tables, so try it opportunistically and fall back to plain columns.
        for projection in ('rowid AS __agate_rowid, *', '*'):
            try:
                query = 'SELECT %s FROM %s WHERE %s%s' % (
                    projection, _quote(connection, table_name), where, limit_clause(),
                )
                rows = connection.execute(text(query)).mappings().all()
                break
            except Exception:
                rows = None
        if rows is None:
            return
        for order, row in enumerate(rows, start=1):
            values = {key: value for key, value in row.items() if key != '__agate_rowid'}
            blockers.append(BlockingRow(order, row.get('__agate_rowid'), values, reason))

    for name, desired_column in desired_columns.items():
        if name not in existing_columns:
            # A new NOT NULL column without a default is a structural decision,
            # not a bad row that can be corrected. Surface it as a rebuild
            # reason instead of listing every row in the table.
            continue
        existing_column = existing_columns[name]
        if existing_column.nullable and not desired_column.nullable:
            fetch('%s IS NULL' % _quote(connection, name),
                  'NULL in %s, which becomes NOT NULL' % name)

    seen_unique = set()
    for column_names in sorted(_unique_signatures(desired_table)):
        if not column_names or not all(name in existing_columns for name in column_names):
            continue
        if column_names in seen_unique:
            continue
        seen_unique.add(column_names)
        columns = ', '.join(_quote(connection, name) for name in column_names)
        group = columns
        having = 'COUNT(*) > 1'
        query = 'SELECT %s FROM %s GROUP BY %s HAVING %s%s' % (
            columns, _quote(connection, table_name), group, having, limit_clause(),
        )
        rows = connection.execute(text(query)).mappings().all()
        for order, row in enumerate(rows, start=1):
            blockers.append(BlockingRow(
                order, None, dict(row),
                'duplicate values for UNIQUE (%s)' % ', '.join(column_names),
            ))

    return blockers


def _build_plan(connection, table, table_name, dialect, db_schema, constraints, unique_constraint,
                min_col_len, col_len_multiplier, column_defaults, max_blocking_rows):
    inspector = inspect(connection)
    table_exists = inspector.has_table(table_name, schema=db_schema)
    sql_dialect = dialects.registry.load(dialect)() if dialect else connection.engine.dialect

    if not table_exists:
        desired_table = _desired_table(
            table, table_name, dialect, db_schema, constraints, unique_constraint,
            min_col_len, col_len_multiplier, column_defaults,
        )
        create_statement = _compile_create(desired_table, sql_dialect)
        return MigrationPlan(
            table_name=table_name,
            dialect=dialect,
            table_exists=False,
            statements=(create_statement,),
            create_statement=create_statement,
            fingerprint=('missing', table_name),
        )

    existing_table = Table(table_name, MetaData(), schema=db_schema, autoload_with=connection)
    desired_table = _desired_table(
        table, table_name, dialect, db_schema, constraints, unique_constraint,
        min_col_len, col_len_multiplier, column_defaults,
    )
    existing_columns = OrderedDict((column.name, column) for column in existing_table.columns)
    desired_columns = OrderedDict((column.name, column) for column in desired_table.columns)

    row_count = connection.execute(
        text('SELECT COUNT(*) FROM %s' % _quote(connection, table_name))
    ).scalar() or 0
    changes = _diagnose_columns(existing_columns, desired_columns, int(row_count))

    rebuild_reasons = [change.detail for change in changes if change.action == REBUILD]

    existing_unique = _unique_signatures(existing_table)
    desired_unique = _unique_signatures(desired_table)

    added_uniques = desired_unique - existing_unique
    if added_uniques:
        rebuild_reasons.append('new UNIQUE constraints require a rebuild: %s' % (
            [list(columns) for columns in sorted(added_uniques)],
        ))

    # A live UNIQUE constraint is a strict guarantee. If the agate table does
    # not restate it, keep it rather than silently relaxing the schema. It is
    # not itself a reason to rebuild; it only matters if another change does.
    preserved_uniques = tuple(sorted(
        columns for columns in existing_unique - desired_unique
        if all(name in desired_columns for name in columns)
    ))

    blocking_rows = _find_blockers(
        connection, table_name, existing_columns, desired_columns, desired_table,
        max_blocking_rows,
    )

    preserved_indexes = []
    for index in existing_table.indexes:
        columns = [column.name for column in index.columns]
        # UNIQUE indexes express constraints owned by the desired schema. Only
        # carry over ordinary indexes, which agate has no way to describe.
        if not index.unique and all(name in desired_columns for name in columns):
            preserved_indexes.append((index.name, tuple(columns), bool(index.unique)))

    preserved_checks, preserved_foreign_keys, preserved_primary_key = _reflected_constraints(
        connection, table_name, db_schema, existing_columns,
    )

    statements = _statements_for(
        existing_table, desired_table, changes, rebuild_reasons, sql_dialect,
        preserved_indexes=preserved_indexes,
        preserved_checks=preserved_checks,
        preserved_foreign_keys=preserved_foreign_keys,
        preserved_primary_key=preserved_primary_key,
        preserved_uniques=preserved_uniques,
    )

    return MigrationPlan(
        table_name=table_name,
        dialect=dialect,
        table_exists=True,
        changes=tuple(changes),
        blocking_rows=tuple(blocking_rows),
        rebuild_reasons=tuple(rebuild_reasons),
        statements=tuple(statements),
        fingerprint=_table_fingerprint(connection, table_name, db_schema, existing_table),
        preserved_indexes=tuple(preserved_indexes),
        preserved_checks=preserved_checks,
        preserved_foreign_keys=preserved_foreign_keys,
        preserved_primary_key=preserved_primary_key,
        preserved_uniques=preserved_uniques,
        options=_options_signature(
            constraints, unique_constraint, min_col_len, col_len_multiplier, column_defaults,
        ),
    )


def _options_signature(constraints, unique_constraint, min_col_len, col_len_multiplier,
                       column_defaults):
    defaults = tuple(sorted((str(name), str(value)) for name, value in (column_defaults or {}).items()))
    return (
        bool(constraints), tuple(unique_constraint or ()), min_col_len, col_len_multiplier, defaults,
    )


def _temp_name(table_name):
    return '__agate_migrate_%s' % table_name


def _rebuild_table_object(table_name, desired_table, preserved_checks, preserved_foreign_keys,
                          preserved_primary_key, preserved_uniques=(), temp_name=None):
    """Build the replacement table, including constraints agate cannot express."""
    name = temp_name or _temp_name(table_name)
    metadata = MetaData()
    table = desired_table.to_metadata(metadata, name=name)

    existing_uniques = _unique_signatures(table)
    for columns in preserved_uniques:
        if columns in existing_uniques or not all(name in table.columns for name in columns):
            continue
        table.append_constraint(UniqueConstraint(*(table.columns[name] for name in columns)))

    _attach_reflected_constraints(
        table, (preserved_checks, preserved_foreign_keys, preserved_primary_key),
        table_name, metadata,
    )
    return table


def _statements_for(existing_table, desired_table, changes, rebuild_reasons, sql_dialect,
                    preserved_indexes=(), preserved_checks=(), preserved_foreign_keys=(),
                    preserved_primary_key=(), preserved_uniques=()):
    statements = []
    if rebuild_reasons:
        quote = _quote_placeholder
        temp_name = _temp_name(existing_table.name)
        temp_table = _rebuild_table_object(
            existing_table.name, desired_table, preserved_checks, preserved_foreign_keys,
            preserved_primary_key, preserved_uniques, temp_name,
        )
        source_columns = {column.name for column in existing_table.columns}
        common_columns = [name for name in desired_table.columns.keys() if name in source_columns]

        statements.append('BEGIN IMMEDIATE;')
        statements.append('DROP TABLE IF EXISTS %s;' % quote(temp_name))
        statements.append(_compile_create(temp_table, sql_dialect))
        if common_columns:
            columns = ', '.join(quote(name) for name in common_columns)
            statements.append('INSERT INTO %s (%s) SELECT %s FROM %s;' % (
                quote(temp_name), columns, columns, quote(existing_table.name),
            ))
        statements.append('DROP TABLE %s;' % quote(existing_table.name))
        statements.append('ALTER TABLE %s RENAME TO %s;' % (quote(temp_name), quote(existing_table.name)))

        final_table = desired_table.to_metadata(MetaData(), name=existing_table.name)
        for index_name, column_names, unique in preserved_indexes:
            if not all(name in final_table.columns for name in column_names):
                continue
            index = Index(index_name, *(final_table.columns[name] for name in column_names), unique=unique)
            statements.append(str(CreateIndex(index).compile(dialect=sql_dialect)) + ';')
        statements.append('COMMIT;')
        return statements

    for change in changes:
        if change.action != ADDITIVE:
            continue
        column = desired_table.columns[change.name]
        statements.append('ALTER TABLE %s ADD COLUMN %s;' % (
            _quote_placeholder(existing_table.name),
            str(CreateColumn(column).compile(dialect=sql_dialect)),
        ))
    return statements


def _quote_placeholder(name):
    if re.match(r'^[A-Za-z_][A-Za-z0-9_$]*$', name):
        return name
    return '"%s"' % name.replace('"', '""')


def plan_migration(table, connection_or_string, table_name, dialect=None, db_schema=None,
                   constraints=True, unique_constraint=[], min_col_len=1, col_len_multiplier=1,
                   column_defaults=None, max_blocking_rows=None):
    """
    Compare an agate table with a live SQL table and return a migration plan.

    The plan classifies each difference as one of:

    ``display_order_only``
        The same column names appear in a different order. SQL tables have a
        physical column order, but it affects only ``SELECT *`` presentation.
        No DDL is generated; consumers that care about order should list columns
        explicitly.

    ``append_column``
        A new nullable column, or a new NOT NULL column with a default, can be
        added with ``ALTER TABLE ... ADD COLUMN`` without rebuilding.

    ``rebuild_required``
        Type, nullability, default, removed-column, or UNIQUE-constraint
        differences cannot be altered in place on SQLite while preserving the
        original constraints. The plan emits a transactional rebuild sequence
        that copies existing rows and recreates ordinary indexes. Primary keys,
        CHECK constraints, foreign keys, and any live UNIQUE constraints the
        agate table does not restate are carried over to the replacement table.

    ``blocking_rows`` contains data that must be fixed before a tightened
    NOT NULL or a new UNIQUE constraint can be applied. Each entry includes the
    rowid where available and the current values.

    A new NOT NULL column with no default cannot be populated for existing
    rows, so it is reported as ``rebuild_required`` rather than as blocking
    data. Supply ``column_defaults`` to make it additive instead.

    :param column_defaults:
        Optional mapping of column name to a default value for new or changed
        columns. This lets a new NOT NULL column be appended in place.
    :param max_blocking_rows:
        Optional limit for the number of live rows returned as evidence.
    """
    engine, connection = get_engine_and_connection(connection_or_string)
    try:
        dialect_name = _dialect_name(connection, dialect)
        return _build_plan(
            connection, table, table_name, dialect_name, db_schema, constraints,
            unique_constraint, min_col_len, col_len_multiplier, column_defaults,
            max_blocking_rows,
        )
    finally:
        if engine is not None:
            connection.close()
            engine.dispose()


def _apply_plan(connection, table, table_name, desired_plan, constraints, unique_constraint,
                db_schema, min_col_len, col_len_multiplier, column_defaults, dialect):
    desired_table = _desired_table(
        table, table_name, dialect, db_schema, constraints, unique_constraint,
        min_col_len, col_len_multiplier, column_defaults,
    )

    if desired_plan.requires_rebuild:
        _rebuild_table(connection, table_name, desired_table, desired_plan)
        return

    for change in desired_plan.changes:
        if change.action != ADDITIVE:
            continue
        column = desired_table.columns[change.name]
        connection.execute(text('ALTER TABLE %s ADD COLUMN %s' % (
            _quote(connection, table_name),
            str(CreateColumn(column).compile(dialect=connection.engine.dialect)),
        )))


def apply_migration(table, connection_or_string, table_name, plan=None, dialect=None, db_schema=None,
                    constraints=True, unique_constraint=[], min_col_len=1, col_len_multiplier=1,
                    column_defaults=None, max_blocking_rows=None, confirm=False):
    """
    Apply a migration plan, atomically.

    The preflight is re-run inside the transaction immediately before any DDL.
    If a plan was supplied, its fingerprint must still match the live table;
    otherwise the call raises and changes nothing. Blocking rows also raise
    before DDL. Any failure during the rebuild rolls back the transaction, so
    the original table and its data remain available.

    :param confirm:
        Apply even if the live fingerprint no longer matches the supplied plan.
        The fresh preflight and blocking-row checks still apply.
    :returns:
        The :class:`MigrationPlan` that was applied.
    """
    engine, connection = get_engine_and_connection(connection_or_string)
    dialect_name = _dialect_name(connection, dialect)
    try:
        with _atomic(connection):
            fresh_plan = _build_plan(
                connection, table, table_name, dialect_name, db_schema, constraints,
                unique_constraint, min_col_len, col_len_multiplier, column_defaults,
                max_blocking_rows,
            )

            if plan is not None and not confirm:
                if plan.options and plan.options != fresh_plan.options:
                    raise RuntimeError(
                        'migration options differ from the supplied plan; '
                        're-run plan_migration or pass confirm=True'
                    )
                if plan.fingerprint != fresh_plan.fingerprint:
                    raise RuntimeError(
                        'migration preflight changed since the plan was created; '
                        're-run plan_migration or pass confirm=True'
                    )

            if fresh_plan.has_blocking_rows:
                raise ValueError(
                    'migration blocked by %d row(s); fix the data first' % len(fresh_plan.blocking_rows)
                )

            if fresh_plan.table_exists:
                _apply_plan(
                    connection, table, table_name, fresh_plan, constraints,
                    unique_constraint, db_schema, min_col_len, col_len_multiplier,
                    column_defaults, dialect_name,
                )
            else:
                connection.execute(text(fresh_plan.create_statement))
                desired_table = _desired_table(
                    table, table_name, dialect_name, db_schema, constraints,
                    unique_constraint, min_col_len, col_len_multiplier, column_defaults,
                )
                rows = [dict(zip(table.column_names, row)) for row in table.rows]
                if rows:
                    connection.execute(desired_table.insert(), rows)
        return fresh_plan
    finally:
        if engine is not None:
            connection.close()
            engine.dispose()


def _atomic(connection):
    """
    Start a transaction that also covers DDL.

    SQLite's legacy transaction mode does not open a transaction for DDL. An
    explicit ``BEGIN IMMEDIATE`` is what makes a failed rebuild roll back.
    """
    if connection.in_transaction():
        return connection.begin_nested()
    if connection.engine.dialect.name == 'sqlite':
        connection.exec_driver_sql('BEGIN IMMEDIATE')
        return _ExplicitTransaction(connection)
    return connection.begin()


class _ExplicitTransaction:
    def __init__(self, connection):
        self.connection = connection

    def __enter__(self):
        return self.connection

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            self.connection.commit()
        else:
            self.connection.rollback()
        return False


def _rebuild_table(connection, table_name, desired_table, plan):
    quote = connection.dialect.identifier_preparer.quote
    temp_name = _temp_name(table_name)
    temp_table = _rebuild_table_object(
        table_name, desired_table, plan.preserved_checks, plan.preserved_foreign_keys,
        plan.preserved_primary_key, plan.preserved_uniques, temp_name,
    )

    connection.execute(text('DROP TABLE IF EXISTS %s' % quote(temp_name)))
    connection.execute(text(_compile_create(temp_table, connection.engine.dialect)))

    source_columns = inspect(connection).get_columns(table_name)
    source_names = {column['name'] for column in source_columns}
    common_columns = [name for name in temp_table.columns.keys() if name in source_names]
    if common_columns:
        columns = ', '.join(quote(name) for name in common_columns)
        connection.execute(text('INSERT INTO %s (%s) SELECT %s FROM %s' % (
            quote(temp_name), columns, columns, quote(table_name),
        )))
    else:
        raise RuntimeError('refusing to rebuild %s without any shared columns' % table_name)

    connection.execute(text('DROP TABLE %s' % quote(table_name)))
    connection.execute(text('ALTER TABLE %s RENAME TO %s' % (quote(temp_name), quote(table_name))))

    final_table = desired_table.to_metadata(MetaData(), name=table_name)
    for index_name, column_names, unique in plan.preserved_indexes:
        if not all(name in final_table.columns for name in column_names):
            continue
        index = Index(index_name, *(final_table.columns[name] for name in column_names), unique=unique)
        connection.execute(text(str(CreateIndex(index).compile(dialect=connection.engine.dialect))))


def to_sql_migration_plan(self, connection_or_string, table_name, **kwargs):
    """
    Monkey patched as :meth:`Table.to_sql_migration_plan`.

    See :func:`plan_migration`.
    """
    return plan_migration(self, connection_or_string, table_name, **kwargs)


def to_sql_migration(self, connection_or_string, table_name, **kwargs):
    """
    Monkey patched as :meth:`Table.to_sql_migration`.

    See :func:`apply_migration`.
    """
    return apply_migration(self, connection_or_string, table_name, **kwargs)


def blocking_rows_json(plan):
    """Serialize blocking rows for a deployment ticket or CLI output."""
    return json.dumps([row._asdict() for row in plan.blocking_rows],
                      ensure_ascii=False, default=str, indent=2)


agate.Table.to_sql_migration_plan = to_sql_migration_plan
agate.Table.to_sql_migration = to_sql_migration
