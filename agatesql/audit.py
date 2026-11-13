"""
Read-only auditing of legacy amount columns with mixed storage types.

Legacy databases sometimes declare a column ``NUMERIC`` while physically
storing a mixture of integers, floats and text (SQLite's type affinity is the
classic example). When such a column is read into an :class:`agate.Table`,
some amounts no longer match the original ledger.

:func:`audit_amount_column` is a read-only checker: it never writes to the
database. It fetches the raw stored values and classifies each row against a
caller-declared amount precision (scale) into:

* recoverable rows, whose amount can be exactly reconstructed at the declared
  precision;
* unconfirmable rows, where the database itself already discarded tail digits
  (for example a large amount stored as a float whose spacing exceeds the
  declared precision), so the original amount can no longer be confirmed —
  these losses must not be attributed to the importer;
* invalid rows, whose stored value cannot be a legitimate amount at the
  declared precision at all (non-numeric text, non-finite values, or exact
  values with too many decimal places).

Nulls are reported separately and are not counted as problems.
"""

import decimal
import math
from collections import namedtuple
from dataclasses import dataclass, field

from agatesql.table import get_engine_and_connection

#: The amount can be exactly reconstructed at the declared precision.
RECOVERABLE = 'recoverable'
#: The database already discarded tail digits; the original amount can no
#: longer be confirmed. Not an importer loss.
UNCONFIRMABLE = 'unconfirmable'
#: The stored value cannot be a legitimate amount at the declared precision.
INVALID = 'invalid'
#: The stored value is NULL.
NULL = 'null'

#: One audited row. ``row`` is the 1-based position of the row in the table,
#: ``key`` is the value of ``key_column`` if one was given (else ``None``).
#: For recoverable rows ``value`` is the reconstructed :class:`decimal.Decimal`
#: amount and ``note`` names the storage class the value was recovered from.
#: For unconfirmable and invalid rows ``value`` is the raw retrieved value and
#: ``note`` explains the classification.
AuditEntry = namedtuple('AuditEntry', ['row', 'key', 'value', 'note'])

#: A row whose amount column is NULL.
NullEntry = namedtuple('NullEntry', ['row', 'key'])


@dataclass
class AmountAuditReport:
    """
    The result of :func:`audit_amount_column`. Rows are grouped into
    :attr:`recoverable`, :attr:`unconfirmable` and :attr:`invalid` lists of
    :class:`AuditEntry`, plus a :attr:`nulls` list of :class:`NullEntry`.
    """

    table_name: str
    column_name: str
    scale: int
    quantum: decimal.Decimal
    recoverable: list = field(default_factory=list)
    unconfirmable: list = field(default_factory=list)
    invalid: list = field(default_factory=list)
    nulls: list = field(default_factory=list)

    @property
    def total_rows(self):
        return len(self.recoverable) + len(self.unconfirmable) + len(self.invalid) + len(self.nulls)

    def summary(self):
        """
        Return a dict with the number of rows in each category.
        """
        return {
            'recoverable': len(self.recoverable),
            'unconfirmable': len(self.unconfirmable),
            'invalid': len(self.invalid),
            'nulls': len(self.nulls),
            'total': self.total_rows,
        }


def _quote_identifier(name):
    return '"%s"' % name.replace('"', '""')


def _exact_fit(value, quantum):
    """
    Return ``value`` quantized to ``quantum`` if it is an exact multiple of
    the quantum, otherwise ``None``.
    """
    scale = -quantum.as_tuple().exponent
    digits = len(value.as_tuple().digits) + abs(value.as_tuple().exponent)

    with decimal.localcontext() as context:
        context.prec = max(28, digits + scale + 4)
        rounded = value.quantize(quantum)

    if rounded == value:
        return rounded

    return None


def _classify_float(value, quantum):
    if not math.isfinite(value):
        return INVALID, None, 'non-finite float: %r' % value

    ulp = decimal.Decimal.from_float(math.ulp(value))

    if ulp >= quantum:
        return UNCONFIRMABLE, None, (
            'stored as a float whose spacing (%s) is not smaller than the declared precision (%s); '
            'tail digits were discarded by the database, not the importer'
        ) % (ulp, quantum)

    exact = decimal.Decimal.from_float(value)

    with decimal.localcontext() as context:
        context.prec = max(28, len(exact.as_tuple().digits) + 4)
        nearest = exact.quantize(quantum, rounding=decimal.ROUND_HALF_EVEN)

    # A declared-precision amount stored as a float lands within half a ulp of
    # its original value. Since ulp < quantum, at most one declared-precision
    # amount can be that close, so the original is uniquely determined.
    if abs(exact - nearest) <= ulp / 2:
        return RECOVERABLE, nearest, 'float'

    return INVALID, None, (
        'float value %r is not a valid amount at the declared precision (%s) and is too far '
        'from any valid amount to be explained by float storage'
    ) % (value, quantum)


def _classify_text(value, quantum):
    text = value.strip()

    if not text:
        return INVALID, None, 'empty text'

    try:
        parsed = decimal.Decimal(text)
    except decimal.InvalidOperation:
        return INVALID, None, 'non-numeric text: %r' % value

    if not parsed.is_finite():
        return INVALID, None, 'non-finite text: %r' % value

    fitted = _exact_fit(parsed, quantum)

    if fitted is None:
        return INVALID, None, 'text value %r does not conform to the declared precision (%s)' % (value, quantum)

    return RECOVERABLE, fitted, 'text'


def classify_amount(value, quantum):
    """
    Classify a single raw value retrieved from an amount column against a
    quantum (the smallest declared-precision unit, e.g. ``Decimal('0.01')``).

    Returns a ``(category, recovered, note)`` tuple. ``category`` is one of
    :data:`RECOVERABLE`, :data:`UNCONFIRMABLE`, :data:`INVALID` or
    :data:`NULL`. ``recovered`` is the reconstructed :class:`decimal.Decimal`
    amount for recoverable values, else ``None``. ``note`` names the storage
    class for recoverable values and gives the reason otherwise.
    """
    if value is None:
        return NULL, None, None

    if isinstance(value, bool):
        return INVALID, None, 'boolean value in amount column'

    if isinstance(value, int):
        return RECOVERABLE, _exact_fit(decimal.Decimal(value), quantum), 'integer'

    if isinstance(value, float):
        return _classify_float(value, quantum)

    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            return INVALID, None, 'non-finite decimal: %r' % value

        fitted = _exact_fit(value, quantum)

        if fitted is None:
            return INVALID, None, 'value %r does not conform to the declared precision (%s)' % (value, quantum)

        return RECOVERABLE, fitted, 'decimal'

    if isinstance(value, str):
        return _classify_text(value, quantum)

    return INVALID, None, 'unsupported value type: %s' % type(value).__name__


def audit_amount_column(connection_or_string, table_name, column_name, scale=2, key_column=None):
    """
    Read-only audit of a legacy amount column whose declared SQL type is
    numeric but whose stored values are a mixture of integers, floats and
    text. Only a ``SELECT`` is ever executed; the database is never modified.

    Each row is classified against the caller-declared amount precision into
    recoverable, unconfirmable and invalid rows (see :mod:`agatesql.audit`).
    Tail digits the database itself already discarded (for example by storing
    a large amount as a float) are reported as unconfirmable, never as
    importer losses.

    :param connection_or_string:
        An existing sqlalchemy connection or a connection string.
    :param table_name:
        The name of the table to audit.
    :param column_name:
        The name of the amount column to audit.
    :param scale:
        The declared number of fractional decimal digits for amounts, e.g.
        ``2`` for cents.
    :param key_column:
        Optionally, the name of a column whose value identifies each row in
        the report (for example a primary key).
    :returns:
        An :class:`AmountAuditReport`.
    """
    if not isinstance(scale, int) or isinstance(scale, bool) or scale < 0:
        raise ValueError('scale must be a non-negative integer')

    quantum = decimal.Decimal(1).scaleb(-scale)

    engine, connection = get_engine_and_connection(connection_or_string)

    try:
        select_columns = [column_name] if key_column is None else [key_column, column_name]
        statement = 'SELECT %s FROM %s' % (
            ', '.join(_quote_identifier(name) for name in select_columns),
            _quote_identifier(table_name),
        )

        # exec_driver_sql bypasses sqlalchemy's type processors so the raw
        # storage classes (int, float, str, None) reach the classifier.
        rows = connection.exec_driver_sql(statement).fetchall()
    finally:
        if engine is not None:
            connection.close()
            engine.dispose()

    report = AmountAuditReport(table_name=table_name, column_name=column_name, scale=scale, quantum=quantum)

    for row_number, row in enumerate(rows, start=1):
        if key_column is None:
            key = None
            value = row[0]
        else:
            key, value = row[0], row[1]

        category, recovered, note = classify_amount(value, quantum)

        if category == RECOVERABLE:
            report.recoverable.append(AuditEntry(row_number, key, recovered, note))
        elif category == UNCONFIRMABLE:
            report.unconfirmable.append(AuditEntry(row_number, key, value, note))
        elif category == INVALID:
            report.invalid.append(AuditEntry(row_number, key, value, note))
        else:
            report.nulls.append(NullEntry(row_number, key))

    return report
