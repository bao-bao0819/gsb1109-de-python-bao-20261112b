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

Incremental, resumable reads
============================

Paginating a changing SQLite table with ``WHERE updated_at > :last_seen`` is
not safe: rows that share a timestamp can be skipped, and a row that has not
been read yet can have its timestamp moved behind the watermark, so it is
silently never returned. :mod:`agatesql.incremental` defines an explicit,
re-runnable batch boundary instead.

Every batch is read from a point-in-time **snapshot** of the database created
with the SQLite online backup API. Concurrent writes to the live database,
including timestamp rewinds, cannot change what a batch contains. Ordering and
resume use a composite keyset of ``(timestamp_column, key_columns...)``, so
rows that share a timestamp are never split ambiguously across pages.

The returned :class:`agatesql.IncrementalCursor` is serializable and records
the snapshot file, a snapshot fingerprint, and the exact last delivered
keyset. Another process can resume from it or replay the exact batch::

    import agatesql
    from agatesql import IncrementalReader, IncrementalCursor

    with IncrementalReader(
        'sqlite:///live.db', 'events', key_columns=('id',),
        timestamp_column='updated_at', batch_size=1000,
        snapshot_path='/snapshots/run-1.sqlite',
    ) as reader:
        result = reader.read_batch()
        handoff = result.handoff_document()  # JSON-serializable envelope
        token = result.next_cursor.to_token()  # or store just this string

        result = reader.read_batch(IncrementalCursor.from_token(token))

The handoff envelope pins both the source state (``snapshot_path`` plus
``snapshot_fingerprint``) and the batch boundaries (``start`` and ``next``,
both exclusive cursors). A separate process resumes with
:meth:`agatesql.IncrementalReader.for_cursor` and reconstructs any previously
delivered batch with :func:`agatesql.replay_batch`. Use a persistent
``snapshot_path`` whenever a cursor crosses a process boundary; without one
the snapshot is a temporary file deleted when the reader closes, and resuming
raises :class:`agatesql.SnapshotMismatchError` rather than returning a
continuation point that could lose rows.

Before any cursor is handed out, the source must satisfy the keyset contract:

* the source must be SQLite (the snapshot mechanism is SQLite-specific);
* the key columns must be non-NULL and unique together;
* the timestamp column must be non-NULL and use one SQLite storage type.

Any violation raises an :class:`agatesql.IncrementalReadError` subclass
(:class:`agatesql.UnsupportedSourceError`, :class:`agatesql.CursorContractError`,
or :class:`agatesql.SnapshotMismatchError`) instead of a cursor that could
silently skip data. Within one snapshot no row is ever skipped or duplicated;
rows changed on the live database after the snapshot are picked up by the next
snapshot run, so delivery across runs is at-least-once.

===
API
===

.. autofunction:: agatesql.table.from_sql

.. autofunction:: agatesql.table.from_sql_query

.. autofunction:: agatesql.table.to_sql

.. autofunction:: agatesql.table.to_sql_create_statement

.. autofunction:: agatesql.table.sql_query

.. autoclass:: agatesql.incremental.IncrementalReader
    :members:

.. autoclass:: agatesql.incremental.IncrementalCursor
    :members:

.. autoclass:: agatesql.incremental.BatchResult
    :members:

.. autofunction:: agatesql.incremental.read_incremental_batch

.. autofunction:: agatesql.incremental.replay_batch

.. autofunction:: agatesql.incremental.open_snapshot

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
