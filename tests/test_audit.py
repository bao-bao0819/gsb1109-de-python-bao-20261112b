import decimal
import unittest

from sqlalchemy import create_engine

import agatesql  # noqa: F401
from agatesql.audit import (
    INVALID,
    NULL,
    RECOVERABLE,
    UNCONFIRMABLE,
    audit_amount_column,
    classify_amount,
)


class TestClassifyAmount(unittest.TestCase):
    def setUp(self):
        self.quantum = decimal.Decimal('0.01')

    def classify(self, value):
        return classify_amount(value, self.quantum)

    def test_null(self):
        category, recovered, note = self.classify(None)

        self.assertEqual(category, NULL)
        self.assertIsNone(recovered)

    def test_integer(self):
        category, recovered, note = self.classify(5)

        self.assertEqual(category, RECOVERABLE)
        self.assertEqual(recovered, decimal.Decimal('0.01') * 500)
        self.assertEqual(note, 'integer')

    def test_text_zero(self):
        category, recovered, note = self.classify('0.00')

        self.assertEqual(category, RECOVERABLE)
        self.assertEqual(recovered, decimal.Decimal('0.00'))
        self.assertEqual(note, 'text')

    def test_text_with_extra_zero(self):
        category, recovered, note = self.classify('1.230')

        self.assertEqual(category, RECOVERABLE)
        self.assertEqual(recovered, decimal.Decimal('1.23'))

    def test_text_too_many_places(self):
        category, recovered, note = self.classify('1.234')

        self.assertEqual(category, INVALID)
        self.assertIsNone(recovered)

    def test_text_non_numeric(self):
        category, recovered, note = self.classify('abc')

        self.assertEqual(category, INVALID)

    def test_text_empty(self):
        category, recovered, note = self.classify('')

        self.assertEqual(category, INVALID)

    def test_text_non_finite(self):
        category, recovered, note = self.classify('NaN')

        self.assertEqual(category, INVALID)

    def test_decimal_exact(self):
        category, recovered, note = self.classify(decimal.Decimal('12.34'))

        self.assertEqual(category, RECOVERABLE)
        self.assertEqual(recovered, decimal.Decimal('12.34'))
        self.assertEqual(note, 'decimal')

    def test_decimal_too_many_places(self):
        category, recovered, note = self.classify(decimal.Decimal('12.345'))

        self.assertEqual(category, INVALID)

    def test_float_near_grid(self):
        category, recovered, note = self.classify(0.1)

        self.assertEqual(category, RECOVERABLE)
        self.assertEqual(recovered, decimal.Decimal('0.10'))
        self.assertEqual(note, 'float')

    def test_float_exactly_representable(self):
        category, recovered, note = self.classify(100.25)

        self.assertEqual(category, RECOVERABLE)
        self.assertEqual(recovered, decimal.Decimal('100.25'))

    def test_float_off_grid(self):
        # 1.234375 is exactly representable as a float, so float storage
        # cannot explain why it is not a valid scale-2 amount.
        category, recovered, note = self.classify(1.234375)

        self.assertEqual(category, INVALID)

    def test_float_large_unconfirmable(self):
        # ulp(1e18) is 128, far larger than 0.01: the database already
        # discarded the tail digits, so this is not an importer loss.
        category, recovered, note = self.classify(1e18)

        self.assertEqual(category, UNCONFIRMABLE)
        self.assertIsNone(recovered)
        self.assertIn('not the importer', note)

    def test_float_non_finite(self):
        category, recovered, note = self.classify(float('nan'))

        self.assertEqual(category, INVALID)


class TestAuditAmountColumn(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine('sqlite:///:memory:')
        self.connection = self.engine.connect()
        self.connection.exec_driver_sql('CREATE TABLE ledger (id INTEGER, amount NUMERIC)')

        rows = [
            (1, 5),                   # integer: recoverable as 5.00
            (2, 0.1),                 # float near the grid: recoverable as 0.10
            (3, 100.25),              # exactly representable float: recoverable
            (4, 1.234375),            # off-grid float: invalid
            (5, 1234567890123456.8),  # large float, ulp 0.25: unconfirmable
            (6, 1e19),                # huge float: unconfirmable
            (7, '1,234.56'),          # non-numeric text: invalid
            (8, 'abc'),               # non-numeric text: invalid
            (9, None),                # null
        ]

        for row in rows:
            self.connection.exec_driver_sql('INSERT INTO ledger VALUES (?, ?)', row)

    def tearDown(self):
        self.connection.close()
        self.engine.dispose()

    def test_audit(self):
        report = audit_amount_column(self.connection, 'ledger', 'amount', scale=2, key_column='id')

        self.assertEqual(report.table_name, 'ledger')
        self.assertEqual(report.column_name, 'amount')
        self.assertEqual(report.scale, 2)
        self.assertEqual(report.quantum, decimal.Decimal('0.01'))
        self.assertEqual(report.total_rows, 9)

        self.assertEqual(
            [(entry.key, entry.value) for entry in report.recoverable],
            [
                (1, decimal.Decimal('5.00')),
                (2, decimal.Decimal('0.10')),
                (3, decimal.Decimal('100.25')),
            ],
        )
        self.assertEqual([entry.note for entry in report.recoverable], ['integer', 'float', 'float'])

        self.assertEqual([entry.key for entry in report.unconfirmable], [5, 6])
        for entry in report.unconfirmable:
            self.assertIn('not the importer', entry.note)

        self.assertEqual([entry.key for entry in report.invalid], [4, 7, 8])
        self.assertEqual(report.invalid[0].value, 1.234375)
        self.assertEqual(report.invalid[1].value, '1,234.56')

        self.assertEqual([entry.key for entry in report.nulls], [9])

        self.assertEqual(report.summary(), {
            'recoverable': 3,
            'unconfirmable': 2,
            'invalid': 3,
            'nulls': 1,
            'total': 9,
        })

    def test_audit_without_key_column(self):
        report = audit_amount_column(self.connection, 'ledger', 'amount', scale=2)

        self.assertEqual([entry.row for entry in report.recoverable], [1, 2, 3])
        self.assertTrue(all(entry.key is None for entry in report.recoverable))

    def test_audit_is_read_only(self):
        before = self.connection.exec_driver_sql('SELECT COUNT(*) FROM ledger').scalar()

        audit_amount_column(self.connection, 'ledger', 'amount', scale=2)
        audit_amount_column(self.connection, 'ledger', 'amount', scale=2)

        after = self.connection.exec_driver_sql('SELECT COUNT(*) FROM ledger').scalar()
        self.assertEqual(before, after)

    def test_audit_with_connection_string(self):
        import os
        import tempfile

        fd, path = tempfile.mkstemp(suffix='.db')

        try:
            engine = create_engine('sqlite:///%s' % path)
            connection = engine.connect()
            connection.exec_driver_sql('CREATE TABLE ledger (amount NUMERIC)')
            connection.exec_driver_sql('INSERT INTO ledger VALUES (?)', ('0.00',))
            connection.commit()
            connection.close()
            engine.dispose()

            report = audit_amount_column('sqlite:///%s' % path, 'ledger', 'amount', scale=2)

            self.assertEqual(len(report.recoverable), 1)
            self.assertEqual(report.recoverable[0].value, decimal.Decimal('0.00'))
        finally:
            os.close(fd)
            os.remove(path)

    def test_invalid_scale(self):
        with self.assertRaises(ValueError):
            audit_amount_column(self.connection, 'ledger', 'amount', scale=-1)
