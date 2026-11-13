import datetime
import json
from decimal import Decimal

import agate
import pytest
from sqlalchemy import MetaData, create_engine, inspect, select

import agatesql
from agatesql.price import (
    EFFECTIVE_DATE,
    PRICE,
    REVISIONS,
    SOURCE_REFS,
    _make_tables,
    adjudicate_price_history,
)

DATE = datetime.date


def make_table(rows, column_names=None, column_types=None):
    if column_names is None:
        column_names = ['product_id', 'effective_date', 'price', 'revision', 'source_ref']
    if column_types is None:
        column_types = [agate.Text(), agate.Date(), agate.Number(), agate.Number(), agate.Text()]
    return agate.Table(rows, column_names, column_types)


def landing_table(connection, name):
    return _make_tables(name, MetaData())[0]


def current_table(connection, name):
    return _make_tables(name, MetaData())[1]


class TestAdjudication:
    def test_highest_revision_wins_per_business_date(self):
        records = [
            ('A', DATE(2026, 10, 1), Decimal('100'), 1, 'batch-1'),
            ('A', DATE(2026, 10, 1), Decimal('105'), 2, 'batch-2'),
            ('A', DATE(2026, 10, 1), Decimal('99'), 1, 'dup-key-different-batch'),
        ]

        history = adjudicate_price_history(records)

        assert history['A'] == [
            (DATE(2026, 10, 1), None, Decimal('105'), [2], ['batch-2']),
        ]

    def test_late_revision_splits_interval_without_touching_neighbors(self):
        records = [
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'sep-filing'),
            ('A', DATE(2026, 10, 1), Decimal('120'), 2, 'oct-filing'),
            ('A', DATE(2026, 9, 15), Decimal('110'), 3, 'late-sep-correction'),
        ]

        history = adjudicate_price_history(records)

        assert history['A'] == [
            (DATE(2026, 9, 1), DATE(2026, 9, 15), Decimal('100'), [1], ['sep-filing']),
            (DATE(2026, 9, 15), DATE(2026, 10, 1), Decimal('110'), [3], ['late-sep-correction']),
            (DATE(2026, 10, 1), None, Decimal('120'), [2], ['oct-filing']),
        ]

    def test_equal_adjacent_periods_merge_but_keep_all_provenance(self):
        records = [
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'b1'),
            ('A', DATE(2026, 9, 15), Decimal('100'), 2, 'b2'),
            ('A', DATE(2026, 10, 1), Decimal('120'), 3, 'b3'),
        ]

        history = adjudicate_price_history(records)

        assert history['A'] == [
            (DATE(2026, 9, 1), DATE(2026, 10, 1), Decimal('100'), [1, 2], ['b1', 'b2']),
            (DATE(2026, 10, 1), None, Decimal('120'), [3], ['b3']),
        ]

    def test_products_are_adjudicated_independently(self):
        records = [
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'b1'),
            ('B', DATE(2026, 9, 1), Decimal('50'), 1, 'b1'),
        ]

        history = adjudicate_price_history(records)

        assert sorted(history) == ['A', 'B']
        assert history['B'][0][2] == Decimal('50')


class TestPriceHistoryImport:
    @pytest.fixture()
    def connection(self):
        engine = create_engine('sqlite:///:memory:')
        link = engine.connect()
        yield link
        link.close()
        engine.dispose()

    @property
    def table_name(self):
        return 'product_price'

    def test_import_creates_landing_and_adjudicated_tables_with_constraints(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'batch-1'),
        ])

        table.to_sql_price_history(connection, self.table_name)

        inspector = inspect(connection)
        landing_constraints = {
            tuple(constraint['column_names'])
            for constraint in inspector.get_unique_constraints(self.table_name + '_revisions')
        }
        current_constraints = {
            tuple(constraint['column_names'])
            for constraint in inspector.get_unique_constraints(self.table_name)
        }

        assert ('product_id', 'effective_date', 'revision') in landing_constraints
        assert ('product_id', 'effective_date') in current_constraints

    def test_late_batch_preserves_prior_and_later_prices(self, connection):
        first_batch = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'sep-filing'),
            ('A', DATE(2026, 10, 1), Decimal('120'), 2, 'oct-filing'),
        ])
        first_batch.to_sql_price_history(connection, self.table_name)

        late_batch = make_table([
            ('A', DATE(2026, 9, 15), Decimal('110'), 3, 'late-sep-correction'),
        ])
        late_batch.to_sql_price_history(connection, self.table_name)

        current = current_table(connection, self.table_name)
        rows = connection.execute(select(current).order_by(EFFECTIVE_DATE)).all()

        assert [tuple(row) for row in rows] == [
            ('A', DATE(2026, 9, 1), DATE(2026, 9, 15), Decimal('100'), '[1]', '["sep-filing"]'),
            ('A', DATE(2026, 9, 15), DATE(2026, 10, 1), Decimal('110'), '[3]', '["late-sep-correction"]'),
            ('A', DATE(2026, 10, 1), None, Decimal('120'), '[2]', '["oct-filing"]'),
        ]

    def test_landing_table_keeps_every_revision_including_superseded(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'first'),
            ('A', DATE(2026, 9, 1), Decimal('105'), 2, 'correction'),
        ])
        table.to_sql_price_history(connection, self.table_name)

        landing = landing_table(connection, self.table_name)
        landing_rows = connection.execute(select(landing)).all()

        assert len(landing_rows) == 2
        assert {row.revision for row in landing_rows} == {1, 2}

        current = current_table(connection, self.table_name)
        current_rows = connection.execute(select(current)).all()
        assert len(current_rows) == 1
        assert current_rows[0].price == Decimal('105')

    def test_reimporting_same_batch_is_idempotent(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'batch-1'),
            ('A', DATE(2026, 10, 1), Decimal('120'), 2, 'batch-2'),
        ])
        table.to_sql_price_history(connection, self.table_name)
        table.to_sql_price_history(connection, self.table_name)

        landing = landing_table(connection, self.table_name)
        current = current_table(connection, self.table_name)

        assert len(connection.execute(select(landing)).all()) == 2
        first_snapshot = connection.execute(select(current).order_by(EFFECTIVE_DATE)).all()
        second_snapshot = connection.execute(select(current).order_by(EFFECTIVE_DATE)).all()
        assert [tuple(row) for row in first_snapshot] == [tuple(row) for row in second_snapshot]

    def test_reimporting_empty_batch_leaves_history_intact(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'batch-1'),
        ])
        table.to_sql_price_history(connection, self.table_name)

        make_table([]).to_sql_price_history(connection, self.table_name)

        current = current_table(connection, self.table_name)
        rows = connection.execute(select(current)).all()
        assert len(rows) == 1

    def test_same_key_with_different_payload_is_rejected(self, connection):
        make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'batch-1'),
        ]).to_sql_price_history(connection, self.table_name)

        conflicting = make_table([
            ('A', DATE(2026, 9, 1), Decimal('101'), 1, 'batch-1-tampered'),
        ])

        with pytest.raises(ValueError):
            conflicting.to_sql_price_history(connection, self.table_name)

        landing = landing_table(connection, self.table_name)
        rows = connection.execute(select(landing)).all()
        assert len(rows) == 1
        assert rows[0].price == Decimal('100')

    def test_duplicate_rows_within_one_batch_are_deduplicated(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'batch-1'),
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'batch-1'),
        ])
        table.to_sql_price_history(connection, self.table_name)

        landing = landing_table(connection, self.table_name)
        assert len(connection.execute(select(landing)).all()) == 1

    def test_merged_segment_records_each_revision_source(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'b1'),
            ('A', DATE(2026, 9, 15), Decimal('100'), 2, 'b2'),
            ('A', DATE(2026, 10, 1), Decimal('120'), 3, 'b3'),
        ])
        table.to_sql_price_history(connection, self.table_name)

        current = current_table(connection, self.table_name)
        rows = connection.execute(select(current).order_by(EFFECTIVE_DATE)).all()

        assert len(rows) == 2
        assert json.loads(rows[0].revisions) == [1, 2]
        assert json.loads(rows[0].source_refs) == ['b1', 'b2']

    def test_point_lookup_hits_at_most_one_effective_price(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'b1'),
            ('A', DATE(2026, 9, 15), Decimal('110'), 2, 'b2'),
            ('A', DATE(2026, 10, 1), Decimal('120'), 3, 'b3'),
        ])
        table.to_sql_price_history(connection, self.table_name)

        expected = {
            DATE(2026, 8, 31): None,
            DATE(2026, 9, 1): Decimal('100'),
            DATE(2026, 9, 14): Decimal('100'),
            DATE(2026, 9, 15): Decimal('110'),
            DATE(2026, 9, 30): Decimal('110'),
            DATE(2026, 10, 1): Decimal('120'),
            DATE(2026, 11, 13): Decimal('120'),
        }

        for day, price in expected.items():
            result = agate.Table.from_sql_price(connection, self.table_name, 'A', day)
            if price is None:
                assert result is None
            else:
                assert result is not None
                assert result[3] == price

    def test_no_overlapping_segments_in_adjudicated_table(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'b1'),
            ('A', DATE(2026, 9, 15), Decimal('110'), 3, 'late'),
            ('A', DATE(2026, 10, 1), Decimal('120'), 2, 'b3'),
            ('B', DATE(2026, 9, 1), Decimal('50'), 1, 'b1'),
        ])
        table.to_sql_price_history(connection, self.table_name)

        current = current_table(connection, self.table_name)
        left = current.alias('left_segment')
        right = current.alias('right_segment')
        statement = select(left, right).where(
            left.c.product_id == right.c.product_id,
            left.c.effective_date < right.c.effective_date,
            left.c.end_date.is_not(None),
            left.c.end_date > right.c.effective_date,
        )

        assert connection.execute(statement).all() == []

    def test_price_decimal_precision_is_preserved(self, connection):
        exact_price = Decimal('11.123456789012345678')
        table = make_table([
            ('A', DATE(2026, 9, 1), exact_price, 1, 'b1'),
        ])
        table.to_sql_price_history(connection, self.table_name)

        result = agate.Table.from_sql_price(connection, self.table_name, 'A', DATE(2026, 9, 1))
        assert result[3] == exact_price

        history = agate.Table.from_sql_price_history(connection, self.table_name)
        assert history.rows[0][PRICE] == exact_price
        assert isinstance(history.rows[0][PRICE], Decimal)

    def test_point_lookup_decodes_provenance_lists(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'b1'),
            ('A', DATE(2026, 9, 15), Decimal('100'), 2, 'b2'),
        ])
        table.to_sql_price_history(connection, self.table_name)

        result = agate.Table.from_sql_price(connection, self.table_name, 'A', DATE(2026, 9, 20))

        assert result[4] == [1, 2]
        assert result[5] == ['b1', 'b2']

    def test_custom_column_names(self, connection):
        table = agate.Table(
            [('SKU1', DATE(2026, 9, 1), Decimal('10'), 1, 'file.csv')],
            ['sku', 'biz_date', 'unit_price', 'rev_no', 'src'],
            [agate.Text(), agate.Date(), agate.Number(), agate.Number(), agate.Text()],
        )

        table.to_sql_price_history(
            connection, self.table_name,
            product_column='sku', date_column='biz_date', price_column='unit_price',
            revision_column='rev_no', source_column='src',
        )

        result = agate.Table.from_sql_price(connection, self.table_name, 'SKU1', DATE(2026, 9, 1))
        assert result[3] == Decimal('10')

    def test_missing_column_raises(self, connection):
        table = agate.Table(
            [('A', DATE(2026, 9, 1), Decimal('100'), 1)],
            ['product_id', 'effective_date', 'price', 'revision'],
            [agate.Text(), agate.Date(), agate.Number(), agate.Number()],
        )

        with pytest.raises(ValueError):
            table.to_sql_price_history(connection, self.table_name)

    def test_null_values_are_rejected(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), None, 1, 'b1'),
        ])

        with pytest.raises(ValueError):
            table.to_sql_price_history(connection, self.table_name)

    def test_fractional_revision_is_rejected(self, connection):
        table = make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), Decimal('1.5'), 'b1'),
        ])

        with pytest.raises(ValueError):
            table.to_sql_price_history(connection, self.table_name)

    def test_connection_string_path_commits_across_connections(self, tmp_path):
        db_path = tmp_path / 'prices.db'
        connection_string = 'sqlite:///%s' % db_path

        make_table([
            ('A', DATE(2026, 9, 1), Decimal('100'), 1, 'b1'),
        ]).to_sql_price_history(connection_string, self.table_name)

        result = agate.Table.from_sql_price(connection_string, self.table_name, 'A', DATE(2026, 9, 1))
        assert result is not None
        assert result[3] == Decimal('100')
