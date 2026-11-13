#!/usr/bin/env python

"""
Demonstrate the SQLite migration preflight end to end.

Run with: python example_migration.py

Three small tables exercise the classifications a maintainer will see:

1. ``patients`` -- column order differs and a nullable column is new, so the
   change is display-only plus an in-place append.
2. ``visits`` -- a nullable column becomes NOT NULL while a live row contains
   NULL, so the apply is refused and the original table stays usable.
3. ``codes`` -- a column type changes, so the table must be rebuilt. The
   rebuild copies the existing data and keeps its constraints.

The database is written to ``migration_example.db`` in the current directory.
"""

import os

import agate
import sqlalchemy

import agatesql
from agatesql.migration import blocking_rows_json

CONNECTION_STRING = 'sqlite:///migration_example.db'


def show_plan(label, plan):
    print('\n== %s ==' % label)
    for change in plan.changes:
        print('  %-14s %-18s %s' % (change.name, change.label, change.detail))
    print('  requires rebuild: %s' % plan.requires_rebuild)
    if plan.blocking_rows:
        print('  blocking rows: %s' % blocking_rows_json(plan))
    if plan.sql:
        print(plan.sql)


def build_live_tables(connection):
    for statement in (
        'DROP TABLE IF EXISTS patients',
        'DROP TABLE IF EXISTS visits',
        'DROP TABLE IF EXISTS codes',
        '''
        CREATE TABLE patients (
            name VARCHAR(40) NOT NULL,
            patient_id INTEGER NOT NULL,
            ward VARCHAR(20) NOT NULL
        )
        ''',
        "INSERT INTO patients VALUES ('Ada', 1, 'A')",
        "INSERT INTO patients VALUES ('Grace', 2, 'B')",
        '''
        CREATE TABLE visits (
            visit_id INTEGER NOT NULL,
            patient_id INTEGER NOT NULL,
            age VARCHAR(10)
        )
        ''',
        "INSERT INTO visits VALUES (10, 1, '36')",
        "INSERT INTO visits VALUES (11, 2, NULL)",
        '''
        CREATE TABLE codes (
            code TEXT NOT NULL,
            label VARCHAR(20) NOT NULL,
            CHECK (length(label) >= 3)
        )
        ''',
        "INSERT INTO codes VALUES ('001', 'alpha')",
    ):
        connection.execute(sqlalchemy.text(statement))
    connection.commit()


def patients_scenario(connection):
    table = agate.Table(
        rows=[(1, 'Ada', 'A', None), (2, 'Grace', 'B', None)],
        column_names=['patient_id', 'name', 'ward', 'note'],
        column_types=[agate.Number(), agate.Text(), agate.Text(), agate.Text()],
    )
    plan = table.to_sql_migration_plan(connection, 'patients')
    show_plan('patients: display order + append', plan)
    table.to_sql_migration(connection, 'patients', plan=plan)
    print('  applied; columns: %s' % [
        row[1] for row in connection.execute(sqlalchemy.text('PRAGMA table_info(patients)'))
    ])


def visits_scenario(connection):
    table = agate.Table(
        rows=[(10, 1, 36), (11, 2, 37)],
        column_names=['visit_id', 'patient_id', 'age'],
        column_types=[agate.Number(), agate.Number(), agate.Number()],
    )
    plan = table.to_sql_migration_plan(connection, 'visits')
    show_plan('visits: nullable -> NOT NULL', plan)
    try:
        table.to_sql_migration(connection, 'visits', plan=plan)
    except ValueError as error:
        print('  refused: %s' % error)
    print('  original still available: %s' % connection.execute(
        sqlalchemy.text('SELECT * FROM visits')
    ).fetchall())


def codes_scenario(connection):
    table = agate.Table(
        rows=[(1, 'alpha')],
        column_names=['code', 'label'],
        column_types=[agate.Number(), agate.Text()],
    )
    plan = table.to_sql_migration_plan(connection, 'codes')
    show_plan('codes: type change requires rebuild', plan)
    table.to_sql_migration(connection, 'codes', plan=plan)
    print('  rebuilt rows: %s' % connection.execute(
        sqlalchemy.text('SELECT * FROM codes')
    ).fetchall())
    print('  schema: %s' % connection.execute(
        sqlalchemy.text("SELECT sql FROM sqlite_master WHERE name = 'codes'")
    ).scalar())


def main():
    if os.path.exists('migration_example.db'):
        os.remove('migration_example.db')

    engine = sqlalchemy.create_engine(CONNECTION_STRING)
    connection = engine.connect()
    build_live_tables(connection)

    patients_scenario(connection)
    visits_scenario(connection)
    codes_scenario(connection)

    connection.close()
    engine.dispose()


if __name__ == '__main__':
    main()
