import pytest
import agate
from sqlalchemy import create_engine, event

import agatesql  # noqa: F401
from agatesql.migration import (
    ADDITIVE,
    DISPLAY_ORDER,
    REBUILD,
    blocking_rows_json,
    plan_migration,
)


@pytest.fixture
def connection():
    engine = create_engine('sqlite:///:memory:')
    conn = engine.connect()
    yield conn
    conn.close()
    engine.dispose()


def make_table(column_names, column_types, rows):
    return agate.Table(rows, column_names, column_types)


def live_table(connection, ddl, rows=()):
    connection.exec_driver_sql(ddl)
    for row in rows:
        placeholders = ', '.join('?' for _ in row)
        connection.exec_driver_sql('INSERT INTO patients VALUES (%s)' % placeholders, row)
    connection.commit()


def test_display_order_only_has_no_ddl(connection):
    live_table(connection, 'CREATE TABLE patients (name TEXT NOT NULL, patient_id FLOAT NOT NULL)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])

    plan = plan_migration(table, connection, 'patients')

    assert plan.table_exists
    assert plan.display_only
    assert not plan.requires_rebuild
    assert plan.changes[0].action == DISPLAY_ORDER
    assert plan.statements == ()
    assert plan.blocking_rows == ()


def test_new_nullable_column_is_additive(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a')])
    table = make_table(
        ['patient_id', 'name', 'note'],
        [agate.Number(), agate.Text(), agate.Text()],
        [(1, 'a', None)],
    )

    plan = plan_migration(table, connection, 'patients')

    assert plan.additive_columns == ('note',)
    assert not plan.requires_rebuild
    assert plan.statements == ('ALTER TABLE patients ADD COLUMN note VARCHAR;',)


def test_new_not_null_column_with_default_is_additive(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL)', [(1,)])
    table = make_table(['patient_id', 'status'], [agate.Number(), agate.Text()], [(1, 'new')])

    plan = plan_migration(table, connection, 'patients', column_defaults={'status': 'new'})

    assert plan.additive_columns == ('status',)
    assert not plan.requires_rebuild
    assert 'DEFAULT' in plan.sql


def test_new_not_null_column_without_default_requires_rebuild(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL)', [(1,)])
    table = make_table(['patient_id', 'status'], [agate.Number(), agate.Text()], [(1, 'new')])

    plan = plan_migration(table, connection, 'patients')

    assert plan.requires_rebuild
    assert plan.changes[0].action == REBUILD
    assert 'no default' in plan.changes[0].detail
    assert plan.blocking_rows == ()


def test_new_not_null_column_on_empty_table_is_additive(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL)')
    table = make_table(['patient_id', 'status'], [agate.Number(), agate.Text()], [(1, 'new')])

    plan = plan_migration(table, connection, 'patients')

    assert plan.additive_columns == ('status',)
    assert not plan.requires_rebuild

    table.to_sql_migration(connection, 'patients')

    columns = connection.exec_driver_sql('PRAGMA table_info(patients)').fetchall()
    assert columns[1][1:4] == ('status', 'VARCHAR', 1)


def test_nullable_to_not_null_reports_blocking_rows(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, age TEXT)',
               [(1, None), (2, '41')])
    table = make_table(['patient_id', 'age'], [agate.Number(), agate.Text()], [(1, 1), (2, 2)])

    plan = plan_migration(table, connection, 'patients')

    assert plan.requires_rebuild
    assert len(plan.blocking_rows) == 1
    blocker = plan.blocking_rows[0]
    assert blocker.row_id == 1
    assert blocker.values == {'patient_id': 1.0, 'age': None}
    assert 'NULL in age' in blocker.reason

    payload = blocking_rows_json(plan)
    assert '"row_id": 1' in payload
    assert '"age": null' in payload


def test_blocking_rows_are_limited(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, age TEXT)',
               [(1, None), (2, None), (3, None)])
    table = make_table(['patient_id', 'age'], [agate.Number(), agate.Text()], [(1, 1)])

    plan = plan_migration(table, connection, 'patients', max_blocking_rows=2)

    assert len(plan.blocking_rows) == 2


def test_unique_constraint_duplicates_are_blocking(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a'), (1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])

    plan = plan_migration(table, connection, 'patients', unique_constraint=['patient_id', 'name'])

    assert plan.requires_rebuild
    assert any('duplicate values' in row.reason for row in plan.blocking_rows)
    assert not plan.safe_to_apply


def test_removed_column_requires_rebuild(connection):
    live_table(connection,
               'CREATE TABLE patients (patient_id FLOAT NOT NULL, legacy TEXT NOT NULL)',
               [(1, 'x')])
    table = make_table(['patient_id'], [agate.Number()], [(1,)])

    plan = plan_migration(table, connection, 'patients')

    assert plan.requires_rebuild
    assert plan.rebuild_columns == ('legacy',)
    assert 'not present' in plan.changes[0].detail


def test_type_change_requires_rebuild_and_preserves_rows(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id TEXT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])

    plan = plan_migration(table, connection, 'patients')
    assert plan.requires_rebuild
    assert 'type TEXT -> FLOAT' in plan.changes[0].detail

    table.to_sql_migration(connection, 'patients', plan=plan)

    rows = connection.exec_driver_sql('SELECT patient_id, name FROM patients').fetchall()
    assert rows == [(1.0, 'a')]


def test_rebuild_preserves_ordinary_indexes(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id TEXT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a')])
    connection.exec_driver_sql('CREATE INDEX ix_patients_name ON patients (name)')
    connection.commit()

    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])
    plan = plan_migration(table, connection, 'patients')
    assert plan.preserved_indexes == (('ix_patients_name', ('name',), False),)

    table.to_sql_migration(connection, 'patients', plan=plan)

    indexes = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'patients'"
    ).fetchall()
    assert indexes == [('ix_patients_name',)]


def test_rebuild_preserves_primary_key_check_and_foreign_key(connection):
    connection.exec_driver_sql('CREATE TABLE parent (id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE)')
    connection.exec_driver_sql('''
        CREATE TABLE patients (
            patient_id INTEGER NOT NULL,
            parent_id INTEGER NOT NULL,
            age INTEGER CHECK (age > 0),
            PRIMARY KEY (patient_id),
            CONSTRAINT ck_age CHECK (age < 200),
            FOREIGN KEY (parent_id) REFERENCES parent(id)
        )
    ''')
    connection.exec_driver_sql("INSERT INTO parent VALUES (1, 'p')")
    connection.exec_driver_sql('INSERT INTO patients VALUES (1, 1, 30)')
    connection.exec_driver_sql('ALTER TABLE patients ADD COLUMN legacy TEXT')
    connection.commit()

    table = make_table(['patient_id', 'parent_id', 'age'],
                       [agate.Number(), agate.Number(), agate.Number()], [(1, 1, 30)])
    plan = plan_migration(table, connection, 'patients')
    assert plan.requires_rebuild
    assert plan.preserved_primary_key == ('patient_id',)
    assert ('ck_age', 'age < 200') in plan.preserved_checks
    assert plan.preserved_foreign_keys == ((None, ('parent_id',), None, 'parent', ('id',)),)

    table.to_sql_migration(connection, 'patients', plan=plan)

    inspector = __import__('sqlalchemy').inspect(connection)
    assert inspector.get_pk_constraint('patients')['constrained_columns'] == ['patient_id']
    checks = {item['sqltext'] for item in inspector.get_check_constraints('patients')}
    assert checks == {'age > 0', 'age < 200'}
    foreign_keys = inspector.get_foreign_keys('patients')
    assert foreign_keys[0]['referred_table'] == 'parent'
    assert connection.exec_driver_sql('SELECT * FROM patients').fetchall() == [(1.0, 1.0, 30.0)]


def test_rebuild_preserves_self_referential_foreign_key(connection):
    connection.exec_driver_sql('''
        CREATE TABLE node (
            id INTEGER NOT NULL,
            parent_id INTEGER,
            PRIMARY KEY (id),
            FOREIGN KEY (parent_id) REFERENCES node(id)
        )
    ''')
    connection.exec_driver_sql('INSERT INTO node VALUES (1, NULL)')
    connection.exec_driver_sql('INSERT INTO node VALUES (2, 1)')
    connection.exec_driver_sql('ALTER TABLE node ADD COLUMN legacy TEXT')
    connection.commit()

    table = make_table(['id', 'parent_id'], [agate.Number(), agate.Number()], [(1, None), (2, 1)])
    table.to_sql_migration(connection, 'node')

    inspector = __import__('sqlalchemy').inspect(connection)
    foreign_key = inspector.get_foreign_keys('node')[0]
    assert foreign_key['referred_table'] == 'node'
    assert foreign_key['constrained_columns'] == ['parent_id']
    assert connection.exec_driver_sql('SELECT * FROM node').fetchall() == [(1.0, None), (2.0, 1.0)]


def test_existing_unique_constraint_is_preserved_across_rebuild(connection):
    live_table(connection,
               'CREATE TABLE patients (patient_id TEXT NOT NULL, name TEXT NOT NULL, UNIQUE (patient_id))',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])

    plan = plan_migration(table, connection, 'patients')
    assert plan.requires_rebuild
    assert plan.preserved_uniques == (('patient_id',),)

    table.to_sql_migration(connection, 'patients', plan=plan)

    inspector = __import__('sqlalchemy').inspect(connection)
    assert inspector.get_unique_constraints('patients') == [
        {'name': None, 'column_names': ['patient_id']}
    ]


def test_options_drift_is_rejected(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])
    plan = plan_migration(table, connection, 'patients')

    with pytest.raises(RuntimeError, match='options differ'):
        table.to_sql_migration(connection, 'patients', plan=plan, unique_constraint=['patient_id'])


def test_rebuild_sql_is_executable_as_shown(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id TEXT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])
    plan = plan_migration(table, connection, 'patients')
    assert plan.requires_rebuild

    for statement in plan.statements:
        connection.exec_driver_sql(statement)

    assert connection.exec_driver_sql('SELECT * FROM patients').fetchall() == [(1.0, 'a')]


def test_missing_table_plans_create_statement(connection):
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])

    plan = plan_migration(table, connection, 'patients')

    assert not plan.table_exists
    assert plan.create_statement.startswith('CREATE TABLE patients')
    assert plan.statements == (plan.create_statement,)


def test_apply_refuses_stale_plan(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, name TEXT)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])
    plan = plan_migration(table, connection, 'patients')

    connection.exec_driver_sql("INSERT INTO patients VALUES (2, 'b')")
    connection.commit()

    with pytest.raises(RuntimeError, match='preflight changed'):
        table.to_sql_migration(connection, 'patients', plan=plan)

    assert connection.exec_driver_sql('SELECT COUNT(*) FROM patients').scalar() == 2


def test_apply_allows_drift_with_confirm(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, name TEXT)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])
    plan = plan_migration(table, connection, 'patients')

    connection.exec_driver_sql("INSERT INTO patients VALUES (2, 'b')")
    connection.commit()

    table.to_sql_migration(connection, 'patients', plan=plan, confirm=True)

    assert connection.exec_driver_sql('SELECT COUNT(*) FROM patients').scalar() == 2
    columns = connection.exec_driver_sql('PRAGMA table_info(patients)').fetchall()
    assert [row[1] for row in columns] == ['patient_id', 'name']
    assert columns[1][3] == 1


def test_apply_blocks_on_bad_data_and_keeps_table(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, age TEXT)', [(1, None)])
    table = make_table(['patient_id', 'age'], [agate.Number(), agate.Text()], [(1, 1)])

    with pytest.raises(ValueError, match='blocked'):
        table.to_sql_migration(connection, 'patients')

    assert connection.exec_driver_sql('SELECT * FROM patients').fetchall() == [(1.0, None)]
    sql = connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'patients'"
    ).scalar()
    assert 'age TEXT' in sql
    assert 'age TEXT NOT NULL' not in sql


def test_failed_rebuild_rolls_back_and_leaves_original_available(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id TEXT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])
    plan = plan_migration(table, connection, 'patients')
    assert plan.requires_rebuild

    # Fail after the original table has been dropped and the replacement has
    # been renamed. This is the riskiest point in the rebuild: without a
    # transaction the original would already be gone.
    def fail_on_rename(conn, cursor, statement, parameters, context, executemany):
        if statement.strip().startswith('ALTER TABLE') and 'RENAME TO' in statement:
            raise RuntimeError('simulated failure during rebuild')

    event.listen(connection.engine, 'before_cursor_execute', fail_on_rename)
    try:
        with pytest.raises(RuntimeError, match='simulated failure'):
            table.to_sql_migration(connection, 'patients', plan=plan)
    finally:
        event.remove(connection.engine, 'before_cursor_execute', fail_on_rename)

    tables = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
    ).fetchall()
    assert ('patients',) in tables
    assert connection.exec_driver_sql('SELECT * FROM patients').fetchall() == [('1', 'a')]
    columns = connection.exec_driver_sql('PRAGMA table_info(patients)').fetchall()
    assert columns[0][2] == 'TEXT'


def test_apply_with_existing_transaction_uses_savepoint(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id TEXT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])

    connection.exec_driver_sql('BEGIN IMMEDIATE')
    table.to_sql_migration(connection, 'patients')
    connection.rollback()

    # The caller owns the outer transaction, so the whole migration is rolled
    # back with it, leaving the original table untouched.
    rows = connection.exec_driver_sql('SELECT * FROM patients').fetchall()
    assert rows == [('1', 'a')]
    columns = connection.exec_driver_sql('PRAGMA table_info(patients)').fetchall()
    assert columns[0][2] == 'TEXT'


def test_additive_apply_is_atomic(connection):
    live_table(connection, 'CREATE TABLE patients (patient_id FLOAT NOT NULL, name TEXT NOT NULL)',
               [(1, 'a')])
    table = make_table(['patient_id', 'name', 'note'], [agate.Number(), agate.Text(), agate.Text()],
                       [(1, 'a', None)])

    table.to_sql_migration(connection, 'patients')

    columns = [row[1] for row in connection.exec_driver_sql('PRAGMA table_info(patients)').fetchall()]
    assert columns == ['patient_id', 'name', 'note']
    assert connection.exec_driver_sql('SELECT * FROM patients').fetchall() == [(1.0, 'a', None)]


def test_apply_creates_missing_table(connection):
    table = make_table(['patient_id', 'name'], [agate.Number(), agate.Text()], [(1, 'a')])

    table.to_sql_migration(connection, 'patients')

    assert connection.exec_driver_sql('SELECT * FROM patients').fetchall() == [(1.0, 'a')]
