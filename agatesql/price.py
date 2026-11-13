"""
Bitemporal price-history support for :class:`agate.Table <agate.table.Table>`.

A price-dimension table records which price applies to a product starting on a
given business date. Price changes arrive in change batches: a batch may
contain a revision for a date that is already in the past, and several
revisions may exist for the same business date.

The functions in this module treat the imported agate table as an append-only
revision stream. Revisions are arbitrated by business date (``effective_date``)
and ``revision`` number:

* The revision with the highest ``revision`` number wins for a given
  ``product_id`` and ``effective_date``. Every revision remains stored in the
  landing table, so the raw history is never rewritten.
* A late-arriving revision opens or splits an interval; it cannot change
  periods that ended before its effective date, and it cannot remove a later
  price change that was already published.
* Consecutive intervals carrying the same price are merged into one segment,
  while the provenance (revision numbers and source references) of every
  merged revision is retained on the segment.

The adjudicated result is materialized as non-overlapping, half-open segments
(``[effective_date, end_date)``) with a ``UNIQUE (product_id,
effective_date)`` constraint, so a point-in-time lookup for any product and
date hits at most one effective price. Re-importing the same batch is
idempotent: rows that already exist in the landing table are skipped and the
adjudicated table is rebuilt deterministically.
"""

import datetime
import decimal
import json
from collections import defaultdict

import agate
from sqlalchemy import Column, MetaData, Table, UniqueConstraint, and_, func, or_, select
from sqlalchemy.types import DATE, DECIMAL, INTEGER, TEXT, TIMESTAMP, VARCHAR, TypeDecorator

from agatesql.table import get_engine_and_connection

PRODUCT_ID = 'product_id'
EFFECTIVE_DATE = 'effective_date'
END_DATE = 'end_date'
PRICE = 'price'
REVISION = 'revision'
REVISIONS = 'revisions'
SOURCE_REF = 'source_ref'
SOURCE_REFS = 'source_refs'
LOADED_AT = 'loaded_at'

LANDING_SUFFIX = '_revisions'

DECIMAL_PRECISION = 38
DECIMAL_SCALE = 18


class ExactDecimal(TypeDecorator):
    """
    Decimal type that avoids floating-point storage on backends (notably
    SQLite) whose native numeric affinity rounds decimals through floats.
    """

    impl = DECIMAL
    cache_ok = True

    def load_dialect_impl(self, dialect):
        if dialect.name == 'sqlite':
            return dialect.type_descriptor(VARCHAR(64))
        return dialect.type_descriptor(DECIMAL(DECIMAL_PRECISION, DECIMAL_SCALE))

    def process_bind_param(self, value, dialect):
        if value is None:
            return None
        if dialect.name == 'sqlite':
            return str(value)
        return value

    def process_result_value(self, value, dialect):
        if value is None or isinstance(value, decimal.Decimal):
            return value
        return decimal.Decimal(str(value))


def adjudicate_price_history(records):
    """
    Arbitrate an iterable of price revisions into non-overlapping segments.

    Each record is a tuple of ``(product_id, effective_date, price, revision,
    source_ref)``. For every product:

    * revisions sharing an ``effective_date`` are reduced to the one with the
      highest ``revision`` number;
    * the remaining revisions are ordered by business date and turned into
      half-open intervals ``[effective_date, next_effective_date)``;
    * adjacent intervals with an equal price are merged, keeping the revision
      numbers and source references of every merged revision.

    Returns a dict mapping each ``product_id`` to an ordered list of segment
    tuples ``(start_date, end_date, price, revisions, source_refs)`` where
    ``end_date`` is ``None`` for the open-ended current segment and
    ``revisions`` / ``source_refs`` are lists in effective-date order.
    """
    grouped = defaultdict(list)
    for product_id, effective_date, price, revision, source_ref in records:
        grouped[product_id].append((effective_date, price, revision, source_ref))

    history = {}
    for product_id, revisions in grouped.items():
        winners = {}
        for effective_date, price, revision, source_ref in revisions:
            incumbent = winners.get(effective_date)
            if incumbent is None or revision > incumbent[1]:
                winners[effective_date] = (price, revision, source_ref)

        segments = []
        for effective_date, (price, revision, source_ref) in sorted(winners.items()):
            if segments and segments[-1][2] == price:
                start, _, same_price, revision_list, source_ref_list = segments[-1]
                segments[-1] = [
                    start, None, same_price,
                    revision_list + [revision], source_ref_list + [source_ref],
                ]
            else:
                segments.append([effective_date, None, price, [revision], [source_ref]])

        for index, segment in enumerate(segments):
            if index + 1 < len(segments):
                segment[1] = segments[index + 1][0]

        history[product_id] = [tuple(segment) for segment in segments]

    return history


def _make_tables(table_name, metadata):
    landing = Table(
        table_name + LANDING_SUFFIX, metadata,
        Column(PRODUCT_ID, VARCHAR(255), nullable=False),
        Column(EFFECTIVE_DATE, DATE, nullable=False),
        Column(PRICE, ExactDecimal(), nullable=False),
        Column(REVISION, INTEGER, nullable=False),
        Column(SOURCE_REF, VARCHAR(512), nullable=False),
        Column(LOADED_AT, TIMESTAMP, server_default=func.current_timestamp()),
        UniqueConstraint(PRODUCT_ID, EFFECTIVE_DATE, REVISION),
    )

    current = Table(
        table_name, metadata,
        Column(PRODUCT_ID, VARCHAR(255), nullable=False),
        Column(EFFECTIVE_DATE, DATE, nullable=False),
        Column(END_DATE, DATE),
        Column(PRICE, ExactDecimal(), nullable=False),
        Column(REVISIONS, TEXT, nullable=False),
        Column(SOURCE_REFS, TEXT, nullable=False),
        UniqueConstraint(PRODUCT_ID, EFFECTIVE_DATE),
    )

    return landing, current


def _column_indexes(table, product_column, date_column, price_column, revision_column, source_column):
    column_map = {
        PRODUCT_ID: product_column,
        EFFECTIVE_DATE: date_column,
        PRICE: price_column,
        REVISION: revision_column,
        SOURCE_REF: source_column,
    }
    missing = [name for name in column_map.values() if name not in table.column_names]
    if missing:
        raise ValueError('Missing required column(s): %s' % ', '.join(sorted(missing)))

    return {key: table.column_names.index(name) for key, name in column_map.items()}


def _coerce_records(table, indexes):
    records = []
    seen = {}
    for row in table.rows:
        product_id = row[indexes[PRODUCT_ID]]
        effective_date = row[indexes[EFFECTIVE_DATE]]
        price = row[indexes[PRICE]]
        revision = row[indexes[REVISION]]
        source_ref = row[indexes[SOURCE_REF]]

        if any(value is None for value in (product_id, effective_date, price, revision, source_ref)):
            raise ValueError('Price history rows cannot contain null values in the key, price or source columns')
        if not isinstance(product_id, str):
            raise ValueError('The product column must contain text values, got %r' % type(product_id))
        if isinstance(effective_date, datetime.datetime):
            effective_date = effective_date.date()
        if not isinstance(effective_date, datetime.date):
            raise ValueError('The effective date column must contain date values, got %r' % type(effective_date))
        if not isinstance(price, decimal.Decimal):
            raise ValueError('The price column must contain number values, got %r' % type(price))
        if not isinstance(revision, decimal.Decimal) or int(revision) != revision or revision < 0:
            raise ValueError('The revision column must contain non-negative whole numbers, got %r' % revision)
        if not isinstance(source_ref, str):
            raise ValueError('The source reference column must contain text values, got %r' % type(source_ref))

        revision = int(revision)
        key = (product_id, effective_date, revision)
        payload = (price, source_ref)
        if key in seen:
            if seen[key] != payload:
                raise ValueError('Conflicting revisions for key %s: %r and %r' % (key, seen[key], payload))
            continue
        seen[key] = payload
        records.append((product_id, effective_date, price, revision, source_ref))

    return records


def _select_landing(landing):
    return select(
        landing.c[PRODUCT_ID], landing.c[EFFECTIVE_DATE], landing.c[PRICE],
        landing.c[REVISION], landing.c[SOURCE_REF],
    )


def to_sql_price_history(self, connection_or_string, table_name,
                         product_column='product_id', date_column='effective_date',
                         price_column='price', revision_column='revision',
                         source_column='source_ref'):
    """
    Import this agate table as a price-history change batch and adjudicate it.

    The source table must contain (at least) a product identifier, an
    effective business date, a price, a revision number and a source reference
    column. Column names may be overridden through the keyword arguments.

    Rows are appended to an immutable landing table named
    ``<table_name>_revisions``, keyed by ``UNIQUE (product_id,
    effective_date, revision)``. Re-importing the same batch skips rows that
    are already present; importing the same key with different data raises
    :class:`ValueError`. The adjudicated, non-overlapping segments are then
    rebuilt in ``<table_name>`` inside a savepoint.

    When a connection string is supplied, the transaction is committed by this
    method; when an existing connection is supplied, the caller remains
    responsible for committing.

    Monkey patched as instance method :meth:`Table.to_sql_price_history`.

    :param connection_or_string:
        An existing sqlalchemy connection or connection string.
    :param table_name:
        The name of the adjudicated price table. The landing table is named
        ``<table_name>_revisions``.
    :param product_column:
        Name of the product identifier column.
    :param date_column:
        Name of the effective business date column.
    :param price_column:
        Name of the price column.
    :param revision_column:
        Name of the revision number column.
    :param source_column:
        Name of the source reference column.
    """
    indexes = _column_indexes(self, product_column, date_column, price_column, revision_column, source_column)
    records = _coerce_records(self, indexes)

    engine, connection = get_engine_and_connection(connection_or_string)
    owns_connection = engine is not None
    metadata = MetaData()
    try:
        landing, current = _make_tables(table_name, metadata)
        landing.create(bind=connection, checkfirst=True)
        current.create(bind=connection, checkfirst=True)

        existing = {
            (row[0], row[1], row[3]): (row[2], row[4])
            for row in connection.execute(_select_landing(landing)).all()
        }

        new_records = []
        for product_id, effective_date, price, revision, source_ref in records:
            key = (product_id, effective_date, revision)
            payload = (price, source_ref)
            if key in existing:
                if existing[key] != payload:
                    raise ValueError(
                        'Revision %s already exists with different data: %r vs %r'
                        % (key, existing[key], payload))
                continue
            new_records.append({
                PRODUCT_ID: product_id,
                EFFECTIVE_DATE: effective_date,
                PRICE: price,
                REVISION: revision,
                SOURCE_REF: source_ref,
            })

        if new_records:
            connection.execute(landing.insert(), new_records)

        history = adjudicate_price_history(
            (row[0], row[1], row[2], row[3], row[4])
            for row in connection.execute(_select_landing(landing)).all()
        )

        segment_rows = []
        for product_id, segments in history.items():
            for start_date, end_date, price, revision_list, source_ref_list in segments:
                segment_rows.append({
                    PRODUCT_ID: product_id,
                    EFFECTIVE_DATE: start_date,
                    END_DATE: end_date,
                    PRICE: price,
                    REVISIONS: json.dumps(revision_list),
                    SOURCE_REFS: json.dumps(source_ref_list),
                })

        savepoint = connection.begin_nested()
        try:
            connection.execute(current.delete())
            if segment_rows:
                connection.execute(current.insert(), segment_rows)
            savepoint.commit()
        except Exception:
            savepoint.rollback()
            raise

        if owns_connection:
            connection.commit()

        return current
    except Exception:
        if owns_connection:
            connection.rollback()
        raise
    finally:
        if owns_connection:
            connection.close()
            engine.dispose()


def from_sql_price(cls, connection_or_string, table_name, product_id, on_date):
    """
    Look up the single effective price segment for a product on a date.

    The segment is the unique row of the adjudicated table whose
    ``effective_date`` is on or before ``on_date`` and whose ``end_date`` is
    open or after ``on_date``. Returns a tuple of
    ``(product_id, effective_date, end_date, price, revisions, source_refs)``
    with ``revisions`` and ``source_refs`` decoded into lists, or ``None``
    when no price had taken effect by the requested date.

    Monkey patched as class method :meth:`Table.from_sql_price`.

    :param connection_or_string:
        An existing sqlalchemy connection or connection string.
    :param table_name:
        The name of the adjudicated price table.
    :param product_id:
        The product identifier to look up.
    :param on_date:
        The business date for which the price is requested.
    """
    if isinstance(on_date, datetime.datetime):
        on_date = on_date.date()
    if not isinstance(on_date, datetime.date):
        raise ValueError('on_date must be a date, got %r' % type(on_date))

    engine, connection = get_engine_and_connection(connection_or_string)
    try:
        metadata = MetaData()
        current = Table(table_name, metadata, autoload_with=connection)
        statement = select(current).where(and_(
            current.c[PRODUCT_ID] == product_id,
            current.c[EFFECTIVE_DATE] <= on_date,
            or_(current.c[END_DATE].is_(None), current.c[END_DATE] > on_date),
        ))
        row = connection.execute(statement).first()
        if row is None:
            return None
        price = row._mapping[PRICE]
        if not isinstance(price, decimal.Decimal):
            price = decimal.Decimal(str(price))
        return (
            row._mapping[PRODUCT_ID],
            row._mapping[EFFECTIVE_DATE],
            row._mapping[END_DATE],
            price,
            json.loads(row._mapping[REVISIONS]),
            json.loads(row._mapping[SOURCE_REFS]),
        )
    finally:
        if engine is not None:
            connection.close()
            engine.dispose()


def from_sql_price_history(cls, connection_or_string, table_name):
    """
    Read the adjudicated price segments back as an :class:`agate.Table`.

    Unlike :meth:`Table.from_sql`, the price column is always read with exact
    decimal semantics (SQLite stores it as text), and the provenance columns
    remain JSON-encoded text.

    Monkey patched as class method :meth:`Table.from_sql_price_history`.
    """
    engine, connection = get_engine_and_connection(connection_or_string)
    try:
        metadata = MetaData()
        _, current = _make_tables(table_name, metadata)
        rows = connection.execute(select(current)).all()
        column_types = [
            agate.Text(), agate.Date(), agate.Date(), agate.Number(), agate.Text(), agate.Text(),
        ]
        return agate.Table(
            [tuple(row) for row in rows],
            [PRODUCT_ID, EFFECTIVE_DATE, END_DATE, PRICE, REVISIONS, SOURCE_REFS],
            column_types,
        )
    finally:
        if engine is not None:
            connection.close()
            engine.dispose()


agate.Table.to_sql_price_history = to_sql_price_history
agate.Table.from_sql_price = classmethod(from_sql_price)
agate.Table.from_sql_price_history = classmethod(from_sql_price_history)
