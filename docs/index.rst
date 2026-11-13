===================
agate-sql |release|
===================

.. include:: ../README.rst

Install
=======

To install:

.. code-block:: bash

    pip install agate-sql

For details on development or supported platforms see the `agate documentation <https://agate.readthedocs.org>`_.

.. warning::

    You'll need to have the correct `SQLAlchemy drivers <https://docs.sqlalchemy.org/en/20/dialects/index.html>`_ installed for whatever database you plan to access. For instance, in order to read/write tables in a PostgreSQL database, you'll also need to ``pip install psycopg2``.

    agate-sql supports all `included dialects <https://docs.sqlalchemy.org/en/20/dialects/index.html#included-dialects>`__. It is known to work with these `external dialects <https://docs.sqlalchemy.org/en/20/dialects/index.html#external-dialects>`__: CrateDB, Ingres.

Usage
=====

agate-sql uses a monkey patching pattern to add SQL support to all :class:`agate.Table <agate.table.Table>` instances.

.. code-block:: python

    import agate
    import agatesql

Importing :mod:`.agatesql` attaches new methods to :class:`agate.Table <agate.table.Table>`. For example, to import a table named :code:`doctors` from a local PostgreSQL database named :code:`hospitals` you will use :meth:`.from_sql`:

.. code-block:: python

    new_table = agate.Table.from_sql('postgresql:///hospitals', 'doctors')

To save this table back to the database:

.. code-block:: python

    new_table.to_sql('postgresql:///hospitals', 'doctors')

The first argument to either function can be any valid `sqlalchemy connection string <https://docs.sqlalchemy.org/en/rel_1_0/core/engines.html>`_. The second argument must be a database name. (Arbitrary SQL queries are not supported.)

That's all there is to it.

Migrating an existing table
===========================

``CREATE TABLE`` SQL is only safe when the target name is unused. When the
database already has a table with the same name and live data, use the migration
preflight instead of dropping or blindly re-creating it:

.. code-block:: python

    plan = new_table.to_sql_migration_plan('sqlite:///local.db', 'doctors')

    if plan.safe_to_apply:
        new_table.to_sql_migration('sqlite:///local.db', 'doctors', plan=plan)
    else:
        print(plan.to_json())

The plan classifies every difference:

* ``display_order_only`` -- the same columns exist in a different order. This
  only changes how ``SELECT *`` presents the table. No DDL is generated.
* ``append_column`` -- new nullable columns, or new ``NOT NULL`` columns with a
  default, can be added with ``ALTER TABLE ... ADD COLUMN``.
* ``rebuild_required`` -- a type, nullability, default, removed column, or
  constraint change cannot be altered in place on SQLite. The plan emits a
  transactional create/copy/drop/rename sequence that preserves existing rows,
  ordinary indexes, primary keys, CHECK constraints, and foreign keys.

Before tightening a nullable column to ``NOT NULL``, or adding a ``UNIQUE``
constraint, the plan queries the live data and returns ``blocking_rows`` with
the rowid and current values. Fix those rows and re-run the preflight. A new
``NOT NULL`` column without a default has no value for existing rows, so it is
reported as a rebuild reason; supply ``column_defaults={'column': value}`` to
make it appendable instead.

``to_sql_migration`` re-runs the preflight inside an explicit transaction
immediately before changing anything. If the table or row count changed since
the plan was produced, it refuses to run unless ``confirm=True``. Any failure
during the rebuild rolls the transaction back, leaving the original table
available. When the caller already owns a transaction, the migration uses a
savepoint instead.

For a runnable walkthrough against a local SQLite file, see
``example_migration.py``.

===
API
===

.. autofunction:: agatesql.table.from_sql

.. autofunction:: agatesql.table.from_sql_query

.. autofunction:: agatesql.table.to_sql

.. autofunction:: agatesql.table.to_sql_create_statement

.. autofunction:: agatesql.table.sql_query

.. autofunction:: agatesql.migration.plan_migration

.. autofunction:: agatesql.migration.apply_migration

.. autoclass:: agatesql.migration.MigrationPlan
   :members:

.. autoclass:: agatesql.migration.ColumnChange
   :members:

.. autoclass:: agatesql.migration.BlockingRow
   :members:

.. autofunction:: agatesql.migration.blocking_rows_json

Authors
=======

.. include:: ../AUTHORS.rst

Changelog
=========

.. include:: ../CHANGELOG.rst

License
=======

.. include:: ../COPYING

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`
