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

Declaring per-table uniqueness conventions
------------------------------------------

Pass ``unique_indexes`` to :meth:`.to_sql` or :meth:`.to_sql_create_statement`
to declare unique indexes as part of the table schema, so the convention is
enforced by the database itself for every writer, including code that writes
to the SQLite file without agate or SQLAlchemy.

Each index spec is a dict with a ``columns`` list, an optional ``where``
predicate that creates a partial unique index, and an optional ``name``.
The predicate is expressed in raw SQL and must not include the ``WHERE``
keyword. Partial indexes are supported on SQLite, PostgreSQL and SQL Server;
on dialects without support, such as MySQL, the predicate is omitted and an
ordinary unique index is created.

Use SQL ``NULL`` as the only missing-batch marker. Because the marker is
``NULL`` rather than a sentinel string, real batch values such as the text
``"未知"`` and empty strings stay fully constrained and never collide with a
missing batch.

A receiving table that keeps one row per warehouse, sku and known batch, and
at most a single "batch unknown" placeholder per warehouse and sku, declares
two partial unique indexes:

.. code-block:: python

    receiving_indexes = [
        {'columns': ['warehouse', 'sku', 'batch'], 'where': 'batch IS NOT NULL'},
        {'columns': ['warehouse', 'sku'], 'where': 'batch IS NULL'},
    ]

    table.to_sql(
        'sqlite:///inventory.db', 'receiving',
        unique_indexes=receiving_indexes,
    )

This emits:

.. code-block:: sql

    CREATE UNIQUE INDEX ... ON receiving (warehouse, sku, batch) WHERE batch IS NOT NULL;
    CREATE UNIQUE INDEX ... ON receiving (warehouse, sku) WHERE batch IS NULL;

A transfer table that follows the same known-batch rule but permits several
unknown-batch rows omits the second, ``batch IS NULL`` index:

.. code-block:: python

    transfer_indexes = [
        {'columns': ['warehouse', 'sku', 'batch'], 'where': 'batch IS NOT NULL'},
    ]

    table.to_sql(
        'sqlite:///inventory.db', 'transfer',
        unique_indexes=transfer_indexes,
    )

===
API
===

.. autofunction:: agatesql.table.from_sql

.. autofunction:: agatesql.table.from_sql_query

.. autofunction:: agatesql.table.to_sql

.. autofunction:: agatesql.table.to_sql_create_statement

.. autofunction:: agatesql.table.sql_query

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
