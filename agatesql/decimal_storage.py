"""
Exact storage of :class:`decimal.Decimal` values in SQLite.

SQLite stores numbers as 64-bit integers or IEEE-754 doubles. Neither can
represent every value that appears in high-precision data, such as
``0.00000000000001`` or ``99999999999999999999``. A :class:`.Table` may
therefore declare an optional :class:`.DecimalStorageContract` for a column.
Contracted columns are stored as canonical decimal text and compared using the
``AGATE_DECIMAL`` collation, which compares the text values as arbitrary
precision :class:`decimal.Decimal` objects. This makes equality, range
predicates and ordering exact, including negatives and values with different
integer digit counts.
"""

import math
import sqlite3
from decimal import Decimal, InvalidOperation

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.types import UserDefinedType

#: The name of the SQLite collation used by contracted columns.
DECIMAL_COLLATION_NAME = 'AGATE_DECIMAL'

#: The declared SQLite type used to identify contracted columns on reflection.
DECIMAL_TYPE_NAME = 'AGATE_DECIMAL_TEXT'

_CONTRACT_KEYS = {
    'minimum', 'maximum', 'decimal_places', 'min', 'max', 'min_value', 'max_value', 'scale', 'places',
    'allowed_decimal_places', 'max_decimal_places', 'range', 'precision',
}


def _to_decimal(value):
    """
    Coerce a value to a finite :class:`decimal.Decimal`, without passing it
    through binary floating point.
    """
    if value is None:
        return None

    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, bool):
        raise ValueError('Boolean values are not valid decimal values.')
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError('Non-finite values are not valid decimal values.')
        result = Decimal(repr(value))
    elif isinstance(value, (str, bytes)):
        if isinstance(value, bytes):
            value = value.decode()
        text = value.strip()
        try:
            result = Decimal(text)
        except (InvalidOperation, ValueError):
            raise ValueError('Can not parse value "%s" as a decimal.' % value) from None
    else:
        try:
            result = Decimal(str(value))
        except (InvalidOperation, ValueError):
            raise ValueError('Can not parse value "%s" as a decimal.' % value) from None

    if not result.is_finite():
        raise ValueError('Non-finite values are not valid decimal values.')

    return result


def _format_decimal(value):
    """
    Format a decimal as plain text, without exponent notation or binary
    floating point conversion.
    """
    if value is None:
        return None

    if isinstance(value, str):
        try:
            value = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return value
    elif isinstance(value, bytes):
        return _format_decimal(value.decode())
    elif not isinstance(value, Decimal):
        value = _to_decimal(value)

    if not value.is_finite():
        raise ValueError('Non-finite values are not valid decimal values.')

    return format(value, 'f')


def _decimal_places_used(value):
    """
    Return the number of decimal places needed to represent the value exactly,
    ignoring trailing zeroes. For example, ``1.500`` uses one decimal place.
    """
    # Decimal.normalize() is context-sensitive and may round to the ambient
    # precision. Work from the tuple instead, stripping trailing zero digits.
    _sign, digits, exponent = value.as_tuple()

    if not any(digits):
        return 0

    digit_count = len(digits)
    while digit_count and digits[digit_count - 1] == 0:
        digit_count -= 1
        exponent += 1

    if exponent >= 0:
        return 0
    return -exponent


def _decimal_collation(left, right):
    """
    SQLite collation that compares decimal text by numeric value.
    """
    if isinstance(left, bytes):
        left = left.decode()
    if isinstance(right, bytes):
        right = right.decode()

    try:
        left_decimal = Decimal(left)
        right_decimal = Decimal(right)
    except (InvalidOperation, TypeError, ValueError):
        return (left > right) - (left < right)

    return (left_decimal > right_decimal) - (left_decimal < right_decimal)


_global_listener_installed = False


def _install_global_listener():
    """
    Register the decimal collation on every SQLite connection created through
    SQLAlchemy, so a database file can be reopened after it was written.
    """
    global _global_listener_installed

    if _global_listener_installed:
        return
    _global_listener_installed = True

    @event.listens_for(Engine, 'connect')
    def _register_decimal_collation(dbapi_connection, connection_record):
        if not isinstance(dbapi_connection, sqlite3.Connection):
            return

        dbapi_connection.create_collation(DECIMAL_COLLATION_NAME, _decimal_collation)

        engine = getattr(connection_record, 'engine', None)
        ischema_names = getattr(getattr(engine, 'dialect', None), 'ischema_names', None)
        if ischema_names is not None:
            ischema_names[DECIMAL_TYPE_NAME] = DecimalStorageType


def install_sqlite_decimal_support(connection=None, engine=None):
    """
    Register the decimal collation and reflected column type on a SQLAlchemy
    connection or engine, if it uses SQLite.

    The collation is also registered on every new SQLite connection created
    through SQLAlchemy, so databases can be reopened after they are written.
    """
    _install_global_listener()

    if connection is None and engine is None:
        return

    if engine is None:
        engine = connection.engine

    if engine is None or engine.dialect.name != 'sqlite':
        return

    ischema_names = getattr(engine.dialect, 'ischema_names', None)
    if ischema_names is not None:
        ischema_names[DECIMAL_TYPE_NAME] = DecimalStorageType

    # Existing pooled connections (including the one passed in) predate the
    # global listener.
    if connection is not None:
        raw_connection = getattr(connection, 'connection', None)
        dbapi_connection = getattr(raw_connection, 'dbapi_connection', None)
        if dbapi_connection is None:
            # SQLAlchemy 1.4 exposes the DBAPI connection as .connection.
            dbapi_connection = getattr(raw_connection, 'connection', raw_connection)
        if isinstance(dbapi_connection, sqlite3.Connection):
            dbapi_connection.create_collation(DECIMAL_COLLATION_NAME, _decimal_collation)


class DecimalStorageContract:
    """
    Limits declared by a publisher for a decimal column.

    Values outside ``minimum`` and ``maximum``, or using more than
    ``decimal_places`` significant decimal places, are rejected. They are
    never rounded.

    :param minimum:
        The inclusive minimum permitted value, or ``None`` for unbounded.
    :param maximum:
        The inclusive maximum permitted value, or ``None`` for unbounded.
    :param decimal_places:
        The maximum number of decimal places permitted, or ``None`` for
        unbounded.
    """
    def __init__(self, minimum=None, maximum=None, decimal_places=None, *, min=None, max=None,
                 min_value=None, max_value=None, scale=None, places=None, allowed_decimal_places=None,
                 max_decimal_places=None, range=None, precision=None, **kwargs):
        if kwargs:
            raise TypeError('Unexpected decimal storage contract options: %s' % ', '.join(sorted(kwargs)))

        if range is not None:
            if isinstance(range, (tuple, list)) and len(range) == 2:
                if minimum is None:
                    minimum = range[0]
                if maximum is None:
                    maximum = range[1]
            else:
                raise TypeError('range must be a (minimum, maximum) pair.')

        for candidate in (min, min_value):
            if candidate is not None:
                minimum = candidate
        for candidate in (max, max_value):
            if candidate is not None:
                maximum = candidate
        for candidate in (scale, places, allowed_decimal_places, max_decimal_places):
            if candidate is not None:
                decimal_places = candidate

        self.minimum = None if minimum is None else _to_decimal(minimum)
        self.maximum = None if maximum is None else _to_decimal(maximum)

        if decimal_places is not None:
            if isinstance(decimal_places, Decimal):
                if decimal_places != decimal_places.to_integral_value():
                    raise ValueError('decimal_places must be an integer.')
                decimal_places = int(decimal_places)
            elif isinstance(decimal_places, bool) or not isinstance(decimal_places, int):
                raise TypeError('decimal_places must be an integer.')
            if decimal_places < 0:
                raise ValueError('decimal_places must not be negative.')
        self.decimal_places = decimal_places

        #: Accepted for compatibility with callers that describe a SQL
        #: ``DECIMAL(precision, scale)`` type. It is advisory only; range and
        #: decimal_places define the actual storage contract.
        self.precision = precision

        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError('minimum must not be greater than maximum.')

    @property
    def min(self):
        return self.minimum

    @property
    def max(self):
        return self.maximum

    @property
    def min_value(self):
        return self.minimum

    @property
    def max_value(self):
        return self.maximum

    @property
    def scale(self):
        return self.decimal_places

    @property
    def places(self):
        return self.decimal_places

    @property
    def allowed_decimal_places(self):
        return self.decimal_places

    @property
    def max_decimal_places(self):
        return self.decimal_places

    def __eq__(self, other):
        if not isinstance(other, DecimalStorageContract):
            return NotImplemented
        return (self.minimum, self.maximum, self.decimal_places, self.precision) == (
            other.minimum, other.maximum, other.decimal_places, other.precision)

    def __repr__(self):
        return 'DecimalStorageContract(minimum=%r, maximum=%r, decimal_places=%r)' % (
            self.minimum, self.maximum, self.decimal_places)


#: Backwards-friendly aliases for the contract class.
DecimalContract = DecimalStorageContract
StorageContract = DecimalStorageContract
DecimalColumnContract = DecimalStorageContract


class DecimalStorageError(ValueError):
    """
    Raised when a value violates a :class:`.DecimalStorageContract`.

    The original row is reported through ``row_index`` (zero-based),
    ``row_number`` (one-based), ``column_name``, ``row`` and, when the table
    has row names, ``row_name``.
    """
    def __init__(self, message, *, column_name=None, row_index=None, row_number=None, value=None, contract=None,
                 reason=None, row=None, row_name=None):
        super().__init__(message)
        self.column_name = column_name
        self.row_index = row_index
        self.row_number = row_number
        self.value = value
        self.contract = contract
        self.reason = reason
        self.row = row
        self.row_name = row_name


DecimalContractError = DecimalStorageError
DecimalContractViolation = DecimalStorageError


def coerce_contract(spec):
    """
    Convert a contract described as a contract instance, mapping, sequence or
    single integer into a :class:`.DecimalStorageContract`.
    """
    if spec is None:
        return None
    if isinstance(spec, DecimalStorageContract):
        return spec

    if isinstance(spec, dict):
        unknown = set(spec) - _CONTRACT_KEYS
        if unknown:
            raise TypeError('Unexpected decimal storage contract options: %s' % ', '.join(sorted(unknown)))
        return DecimalStorageContract(**spec)

    if isinstance(spec, (tuple, list)):
        if len(spec) == 2:
            return DecimalStorageContract(spec[0], spec[1])
        if len(spec) == 3:
            return DecimalStorageContract(spec[0], spec[1], spec[2])
        raise TypeError('A decimal storage contract sequence must have 2 or 3 items.')

    if isinstance(spec, bool):
        raise TypeError('A decimal storage contract must not be a boolean.')

    if isinstance(spec, int):
        return DecimalStorageContract(decimal_places=spec)

    raise TypeError('Unsupported decimal storage contract: %r' % (spec,))


def resolve_decimal_contracts(table, decimal_contracts=None, default_contract=None):
    """
    Resolve the contracts for a table to a ``{column_name: contract}`` mapping.

    ``decimal_contracts`` may be a mapping of column names to contracts. A
    single contract may also be used as the default for all Number columns.
    """
    if decimal_contracts is None:
        decimal_contracts = {}
    elif isinstance(decimal_contracts, DecimalStorageContract):
        default_contract = decimal_contracts
        decimal_contracts = {}
    elif isinstance(decimal_contracts, dict):
        pass
    else:
        raise TypeError('decimal_contracts must be a mapping of column names to contracts.')

    contracts = {name: coerce_contract(spec) for name, spec in decimal_contracts.items()}

    if default_contract is not None:
        default_contract = coerce_contract(default_contract)
        for column_name, column in table.columns.items():
            if isinstance(column.data_type, agate_number_type()) and column_name not in contracts:
                contracts[column_name] = default_contract

    for column_name in contracts:
        if column_name not in table.column_names:
            raise ValueError('Decimal storage contract refers to unknown column: %s' % column_name)
        if not isinstance(table.columns[column_name].data_type, agate_number_type()):
            raise ValueError('Decimal storage contract can only be used with Number columns: %s' % column_name)

    return contracts


def agate_number_type():
    # Imported lazily to avoid a circular import.
    from agate.data_types import Number
    return Number


def validate_decimal_contract_value(contract, value, column_name=None, row_index=None, row=None, row_name=None):
    """
    Validate a single value against a contract.

    :param contract:
        A :class:`.DecimalStorageContract`.
    :param value:
        The value to validate. ``None`` is always permitted.
    :param column_name:
        The column name, used in error reporting.
    :param row_index:
        The zero-based row index, used in error reporting.
    :param row:
        The original row, used in error reporting.
    :param row_name:
        The original row name, used in error reporting.
    :raises DecimalStorageError:
        If the value violates the contract.
    """
    if contract is None or value is None:
        return

    decimal_value = _to_decimal(value)
    row_number = None if row_index is None else row_index + 1
    location = '%s in column %r at row %s (zero-based index %s)' % (
        _format_decimal(decimal_value), column_name, row_number, row_index)
    if row_name is not None:
        location += ' named %r' % row_name

    if contract.decimal_places is not None:
        used = _decimal_places_used(decimal_value)
        if used > contract.decimal_places:
            raise DecimalStorageError(
                'Value %s has %d decimal places, but the storage contract allows at most %d; '
                'values are never rounded.' % (location, used, contract.decimal_places),
                column_name=column_name, row_index=row_index, row_number=row_number,
                value=decimal_value, contract=contract, reason='decimal_places',
                row=row, row_name=row_name)

    if contract.minimum is not None and decimal_value < contract.minimum:
        raise DecimalStorageError(
            'Value %s is below the storage contract minimum %s.' % (
                location, _format_decimal(contract.minimum)),
            column_name=column_name, row_index=row_index, row_number=row_number,
            value=decimal_value, contract=contract, reason='minimum',
            row=row, row_name=row_name)

    if contract.maximum is not None and decimal_value > contract.maximum:
        raise DecimalStorageError(
            'Value %s is above the storage contract maximum %s.' % (
                location, _format_decimal(contract.maximum)),
            column_name=column_name, row_index=row_index, row_number=row_number,
            value=decimal_value, contract=contract, reason='maximum',
            row=row, row_name=row_name)


def validate_decimal_contracts(table, contracts):
    """
    Validate all rows of a table against the given contract mapping.
    """
    for column_name, contract in contracts.items():
        if contract is None:
            continue

        column = table.columns[column_name]
        row_names = list(table.row_names) if table.row_names is not None else [None] * len(table.rows)
        for row_index, value in enumerate(column.values()):
            validate_decimal_contract_value(contract, value, column_name=column_name, row_index=row_index,
                                            row=table.rows[row_index], row_name=row_names[row_index])


class DecimalStorageType(UserDefinedType):
    """
    SQLite type for exact decimal storage.

    The declared type contains ``TEXT`` so that SQLite gives the column TEXT
    affinity and stores the canonical decimal text exactly. The
    ``AGATE_DECIMAL`` collation gives text values numeric comparison semantics.
    """
    cache_ok = True

    def __init__(self, scale=None, minimum=None, maximum=None):
        if scale is not None:
            if isinstance(scale, Decimal):
                scale = int(scale)
            scale = int(scale)
            if scale < 0:
                raise ValueError('scale must not be negative.')
        self.scale = scale
        self.minimum = minimum
        self.maximum = maximum

    @property
    def decimal_places(self):
        return self.scale

    @property
    def python_type(self):
        return Decimal

    def get_col_spec(self, **kwargs):
        if self.scale is None:
            return '%s COLLATE %s' % (DECIMAL_TYPE_NAME, DECIMAL_COLLATION_NAME)
        return '%s(%d) COLLATE %s' % (DECIMAL_TYPE_NAME, self.scale, DECIMAL_COLLATION_NAME)

    def bind_processor(self, dialect):
        def process(value):
            if value is None:
                return None
            return _format_decimal(value)

        return process

    def result_processor(self, dialect, coltype):
        def process(value):
            if value is None:
                return None
            return _to_decimal(value)

        return process

    def coerce_compared_value(self, op, value):
        # Make comparisons against Python numbers bind as decimal text rather
        # than as SQLite REAL values, which would lose precision.
        return self

    def literal_processor(self, dialect):
        def process(value):
            if value is None:
                return 'NULL'
            return "'%s'" % _format_decimal(value).replace("'", "''")

        return process
