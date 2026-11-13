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

Exact decimal storage in SQLite
===============================

SQLite stores numbers as 64-bit integers or IEEE-754 doubles, so values such
as ``0.00000000000001`` and ``99999999999999999999`` can lose precision when
written to a ``FLOAT`` column. If a publisher declares the legal range and the
allowed number of decimal places for a column, pass a
:class:`.DecimalStorageContract` to :meth:`.to_sql`:

.. code-block:: python

    from agatesql import DecimalStorageContract

    contract = DecimalStorageContract(
        minimum='-100000000000000000000',
        maximum='100000000000000000000',
        decimal_places=20,
    )
    table.to_sql('sqlite:///measurements.db', 'samples', decimal_contracts={'reading': contract})

Contracted columns are stored as canonical decimal text, so values round-trip
exactly. They also use a decimal collation, so ``ORDER BY``, ``=``, ``<``,
``BETWEEN`` and other range predicates compare by numeric value, including
negative values and values with different numbers of integer digits:

.. code-block:: sql

    SELECT reading FROM samples WHERE reading BETWEEN -1 AND 1 ORDER BY reading

Use ``decimal_contract`` instead of ``decimal_contracts`` to apply the same
contract to every Number column. A value outside the contract raises
:class:`.DecimalStorageError`; it is never rounded. The exception identifies
the original column and row through ``column_name``, ``row_index`` (zero-based),
``row_number`` (one-based), ``row`` and, when available, ``row_name``.

.. autoclass:: agatesql.decimal_storage.DecimalStorageContract

.. autoexception:: agatesql.decimal_storage.DecimalStorageError

.. autoclass:: agatesql.decimal_storage.DecimalStorageType

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
