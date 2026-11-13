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

Query result columns
====================

Queries can return the same label more than once and can also return a column
whose label already looks like a generated suffix. For example, a query that
joins two tables that both have an ``id`` and also aliases a third expression
to ``id_2`` returns labels ``id``, ``id``, and ``id_2``. agate requires unique
column names, so agate-sql renames duplicates without taking a label that
belongs to another result column. In this example the columns are named
``id``, ``id_3``, and ``id_2``.

Every table produced by :func:`.from_sql_query` or :func:`.sql_query` records
the original label and position of each column:

.. code-block:: python

    table = agate.Table.from_sql_query(query)

    table.column_names          # ('id', 'id_3', 'id_2')
    table.column_labels         # ('id', 'id', 'id_2')
    table.column_positions      # {'id': (0, 1), 'id_2': (2,)}
    table.columns_by_label      # {'id': (column 0, column 1), 'id_2': (column 2,)}
    table.column_sources        # ('contracts', 'payments', 'payments')

    for origin in table.column_origins:
        print(origin.index, origin.label, origin.table, origin.column)

``origin.table`` is ``None`` when an expression cannot be traced to exactly one
physical table, for example ``a.id + b.id`` or a literal. The provenance is
derived from the SELECT list and the database schema, never from the values in
the result rows, so a left join that doesn't match still has the same column
ownership as a matching row.

===
API
===

.. autofunction:: agatesql.table.from_sql

.. autofunction:: agatesql.table.from_sql_query

.. autofunction:: agatesql.table.column_origins

.. autofunction:: agatesql.table.column_labels

.. autofunction:: agatesql.table.column_positions

.. autofunction:: agatesql.table.columns_by_label

.. autofunction:: agatesql.table.column_sources

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
