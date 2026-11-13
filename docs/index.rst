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

Price-history tables
====================

A price-dimension table records which price applies to a product starting on a
given business date. Price changes arrive in change batches, and a batch may
contain a late correction for a business date that is already in the past.
:meth:`Table.to_sql_price_history` imports such a batch into an append-only
landing table (``<table_name>_revisions``), keeps every revision for audit,
arbitrates revisions by business date and revision number, merges consecutive
periods with the same price while retaining the revision provenance, and
materializes non-overlapping segments in the target table. Re-importing the
same batch is idempotent.

.. code-block:: python

    table = agate.Table.from_csv('price_changes.csv')
    table.to_sql_price_history('sqlite:///prices.db', 'product_price')

    # At most one effective price is returned for any product and date.
    agate.Table.from_sql_price('sqlite:///prices.db', 'product_price', 'SKU1',
                               datetime.date(2026, 10, 15))

The source table must contain product identifier, effective date, price,
revision number and source reference columns (the column names are
configurable). Use :meth:`Table.from_sql_price_history` to read the
adjudicated segments back as an agate table.

===
API
===

.. autofunction:: agatesql.table.from_sql

.. autofunction:: agatesql.table.from_sql_query

.. autofunction:: agatesql.table.to_sql

.. autofunction:: agatesql.table.to_sql_create_statement

.. autofunction:: agatesql.table.sql_query

.. autofunction:: agatesql.price.adjudicate_price_history

.. autofunction:: agatesql.price.to_sql_price_history

.. autofunction:: agatesql.price.from_sql_price

.. autofunction:: agatesql.price.from_sql_price_history

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
