# Copyright 2009-2020 Yelp and Contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Combinable metrics for MapReduce jobs.

A metric is a value that can be aggregated in two stages:

* :py:meth:`~StoreSales.add_line` folds one input row into a partial metric.
  This is cheap enough to do in the mapper, including in
  :py:meth:`~mrjob.job.MRJob.mapper_final` ("pre-aggregation").
* :py:meth:`~StoreSales.merge` combines two partial metrics. It is
  associative and commutative, so it doesn't matter how mappers, combiners,
  and reducers group the rows.

The important rule is that partial metrics are never rounded. Rounding is
only applied by :py:meth:`~StoreSales.settled`, which should be called in
the final reducer (or by a report that is building its output). A partial
metric that happens to be zero is still merged into the final metric; zero
is a valid result, not a signal to drop a store. A store with zero net
units is likewise kept.

For example, net sales per store is::

    from decimal import Decimal
    from mrjob.metrics import StoreSales

    metric = StoreSales('123')
    metric = metric.add_line(Decimal('10.005'), 3)
    metric = metric.add_line(Decimal('10.00'), 1, is_return=True)

    metric.net_sales == Decimal('0.005')
    metric.net_units == 2

    settled = metric.settled(places=2)
    settled.net_sales == Decimal('0.01')
    settled.units_per_item(2) == Decimal('0.01')

Pre-aggregation in a MRJob is simply keeping a metric per key in
``mapper_final()`` and yielding it, then merging metrics in
``combiner()`` and ``reducer()``. See
:py:mod:`mrjob.examples.mr_store_sales` for a complete job.

.. versionadded:: 0.7.5
"""
from decimal import Decimal
from decimal import InvalidOperation
from decimal import ROUND_HALF_UP
from decimal import localcontext


#: Default number of decimal places for monetary values.
DEFAULT_PLACES = 2

#: Working precision for addition/division. The decimal module's default
#: context rounds to 28 significant digits, which would silently round large
#: or high-precision partials before settlement. 100 digits is far beyond any
#: real sales figure while still bounding pathological input.
_WORKING_PRECISION = 100


def _to_decimal(value, name='value'):
    """Convert *value* to a :py:class:`~decimal.Decimal` without rounding.

    ``Decimal('1.50')`` and ``Decimal(1.5)`` compare equal but can have
    different exponents; we always keep the exact input value so that
    combining partial metrics never introduces an extra rounding step.

    NaN and infinities are rejected because they have no meaningful
    settled monetary value and would poison later comparisons.
    """
    if isinstance(value, Decimal):
        result = value
    elif isinstance(value, int):
        result = Decimal(value)
    elif isinstance(value, float):
        # str() rather than Decimal(float), which would expose binary noise
        result = Decimal(str(value))
    elif isinstance(value, str):
        result = Decimal(value)
    else:
        raise TypeError('%s must be a number or string, not %r' %
                        (name, value))

    if not result.is_finite():
        raise ValueError('%s must be finite, not %r' % (name, result))
    return result


def _to_int(value, name='value'):
    """Convert *value* to a whole number of items without rounding."""
    if isinstance(value, bool):
        raise TypeError('%s must be an integer, not bool' % name)
    elif isinstance(value, int):
        return value
    elif isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError('%s must be finite, not %r' % (name, value))
        if value != value.to_integral_value():
            raise ValueError('%s must be a whole number, not %r' %
                             (name, value))
        return int(value)
    elif isinstance(value, str):
        return _to_int(Decimal(value), name)
    else:
        raise TypeError('%s must be an integer, not %r' % (name, value))


def _decimal_str(value):
    """Serialize a Decimal without rounding, normalizing trailing zeros.

    ``Decimal('1.10')`` and ``Decimal('1.1')`` are equal. Their plain
    strings differ, so using ``str()`` would make serialized partial metrics
    depend on split order. :py:meth:`Decimal.normalize` removes trailing
    zeros without changing the value. For integral values we format
    without an exponent, so ``1000`` stays ``'1000'`` rather than ``'1E+3'``.
    """
    if value == value.to_integral_value():
        if value == 0:
            return '0'
        with localcontext() as ctx:
            ctx.prec = _WORKING_PRECISION
            return str(value.quantize(Decimal('1')))
    with localcontext() as ctx:
        ctx.prec = _WORKING_PRECISION
        return str(value.normalize())


def _add(left, right):
    """Add two Decimals without the default context's implicit rounding."""
    with localcontext() as ctx:
        ctx.prec = _WORKING_PRECISION
        return left + right


def _divide(left, right):
    """Divide two Decimals without the default context's implicit rounding."""
    with localcontext() as ctx:
        ctx.prec = _WORKING_PRECISION
        return left / right


def _quantize(value, places):
    """Round *value* to *places* decimal places, half up.

    0.01 gives 0.10 rather than 0.1: fixed-point reports should not change
    their precision just because a metric happens to be a round number.

    Uses the working precision rather than the caller's default Decimal
    context, so quantizing a large value to zero places doesn't raise
    ``InvalidOperation``.
    """
    exponent = Decimal('1').scaleb(-places)
    with localcontext() as ctx:
        ctx.prec = _WORKING_PRECISION
        return value.quantize(exponent, rounding=ROUND_HALF_UP)


class StoreSales(object):
    """Unrounded net sales and units for one store.

    The object is immutable; :py:meth:`merge` returns a new metric. It is
    also hashable, which makes it convenient to use in tests.

    Net sales and net units are the sums of sales and units over rows,
    with returns subtracted. A return may be encoded either as a negative
    ``sales``/``units`` value, or as positive values with ``is_return=True``
    (which subtracts them). Negative values are kept exactly as given.

    Zero units are allowed. A store with zero units still has a valid
    metric and must be included in reports; use
    :py:meth:`units_per_item` only when you know units are non-zero.
    """

    __slots__ = ('store', 'net_sales', 'net_units')

    def __init__(self, store, net_sales=0, net_units=0):
        self.store = store
        self.net_sales = _to_decimal(net_sales, 'net_sales')
        self.net_units = _to_int(net_units, 'net_units')

    @classmethod
    def from_line(cls, store, sales, units, is_return=False):
        """Build a metric from one input row.

        :param store: store key (usually a string, but any hashable value
                      works)
        :param sales: sales amount for this row; negative values are
                      returns (see *is_return*)
        :param units: number of items for this row; negative values are
                      returns (see *is_return*)
        :param is_return: if true, the row is a return and its positive
                          *sales*/*units* values are subtracted

        Either encode a return as a negative amount, or pass positive
        amounts with ``is_return=True``. Passing a negative amount *and*
        ``is_return=True`` is rejected, because it would be ambiguous
        whether the caller applied the return twice. Sales and units must
        agree in direction; a row can't be a sale in money and a return in
        items (or vice versa).
        """
        sales = _to_decimal(sales, 'sales')
        units = _to_int(units, 'units')

        if is_return:
            if sales < 0 or units < 0:
                raise ValueError(
                    'negative sales/units with is_return=True is ambiguous;'
                    ' use either negative amounts or is_return, not both')
            sales = -sales
            units = -units

        if (sales > 0 and units < 0) or (sales < 0 and units > 0):
            raise ValueError(
                'sales and units must have the same sign, got %r and %r' %
                (sales, units))

        return cls(store, sales, units)

    def add_line(self, sales, units, is_return=False):
        """Return a new metric with one more input row folded in."""
        return self.merge(self.from_line(
            self.store, sales, units, is_return=is_return))

    def merge(self, other):
        """Return a new metric combining this metric with *other*.

        :py:meth:`merge` is associative and commutative, so combining
        arbitrary map-side partials and then combining those partials again
        produces exactly the same unrounded totals.
        """
        if not isinstance(other, StoreSales):
            raise TypeError('can only merge StoreSales, not %r' % other)
        if self.store != other.store:
            raise ValueError('cannot merge metrics for different stores: '
                             '%r and %r' % (self.store, other.store))

        return StoreSales(
            self.store,
            _add(self.net_sales, other.net_sales),
            self.net_units + other.net_units)

    __add__ = merge

    @property
    def is_zero(self):
        """True if both net sales and net units are zero."""
        return self.net_sales == 0 and self.net_units == 0

    def settled(self, places=DEFAULT_PLACES):
        """Return a :py:class:`SettledStoreSales` rounded to *places*.

        Rounding is deliberately the last step: do this once per store, in
        one final place. ``places=0`` rounds to whole units of money; it
        does not round net units.
        """
        places = _to_int(places, 'places')
        if places < 0:
            raise ValueError('places must be non-negative, not %d' % places)

        return SettledStoreSales(
            self.store, _quantize(self.net_sales, places), places,
            net_units=self.net_units)

    def as_dict(self):
        """Protocol-friendly representation of the metric.

        :py:class:`~decimal.Decimal` is not JSON-encodable, so this stores
        the exact values as strings. Use :py:meth:`from_dict` to restore
        them. The representation is stable: equivalent decimals such as
        ``1.10`` and ``1.1`` serialize the same way, and the value itself
        is never rounded.
        """
        return dict(
            net_sales=_decimal_str(self.net_sales),
            net_units=self.net_units,
        )

    @classmethod
    def from_dict(cls, value):
        """Restore a metric produced by :py:meth:`as_dict`.

        The store ID is not stored in the value; the key that mrjob groups
        by is the authoritative store ID. Passing it explicitly keeps
        serialized metrics small and lets a combiner verify that it never
        combines two stores.
        """
        if not isinstance(value, dict):
            raise TypeError('expected dict, not %r' % value)
        return cls(
            None,
            _to_decimal(value['net_sales'], 'net_sales'),
            _to_int(value['net_units'], 'net_units'))

    def __eq__(self, other):
        if not isinstance(other, StoreSales):
            return NotImplemented
        return (self.store == other.store and
                self.net_sales == other.net_sales and
                self.net_units == other.net_units)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.store, self.net_sales, self.net_units))

    def __repr__(self):
        return '%s(store=%r, net_sales=%r, net_units=%r)' % (
            self.__class__.__name__, self.store,
            str(self.net_sales), self.net_units)


class SettledStoreSales(object):
    """A :py:class:`StoreSales` metric rounded for a report.

    This is a frozen snapshot; it has no :py:meth:`merge` because merging
    rounded metrics would change the total. Merge
    :py:class:`StoreSales` first, then settle once.

    ``net_units`` is carried along so that the settled result contains the
    complete per-store report, including the net item count. It defaults to
    ``None`` for callers who only want the monetary value; in that case
    :py:meth:`units_per_item` requires *net_units* as an argument.
    """

    __slots__ = ('store', 'net_sales', 'net_units', 'places')

    def __init__(self, store, net_sales, places=DEFAULT_PLACES,
                 net_units=None):
        self.store = store
        self.net_sales = _to_decimal(net_sales, 'net_sales')
        self.places = _to_int(places, 'places')
        if self.places < 0:
            raise ValueError(
                'places must be non-negative, not %d' % self.places)
        self.net_units = (
            None if net_units is None else _to_int(net_units, 'net_units'))

    def units_per_item(self, net_units=None):
        """Return net sales divided by net units, rounded to *places*.

        If *net_units* is not given, use the value carried by
        :py:meth:`StoreSales.settled`. The result is rounded with the same
        half-up rule as the report. Raises :py:class:`ZeroDivisionError`
        for zero units; callers should check this themselves and decide how
        to present a store with no items.
        """
        if net_units is None:
            net_units = self.net_units
        if net_units is None:
            raise TypeError('net_units is required')

        units = _to_int(net_units, 'net_units')
        if units == 0:
            raise ZeroDivisionError('cannot divide net sales by zero units')

        try:
            return _quantize(
                _divide(Decimal(self.net_sales), Decimal(units)),
                self.places)
        except InvalidOperation:
            # only possible for pathological Decimal contexts; keep the
            # error clear for callers
            raise ValueError('cannot compute net sales per item')

    def as_dict(self, net_units=None):
        """:py:meth:`StoreSales.as_dict` for the settled value.

        Unlike :py:meth:`StoreSales.as_dict`, this keeps the settled
        exponent, so a value settled to ``Decimal('1.20')`` is reported as
        ``'1.20'`` rather than ``'1.2'``.

        *net_units* defaults to the item count carried by :py:meth:`settled`.
        """
        if net_units is None:
            net_units = self.net_units
        if net_units is None:
            raise TypeError('net_units is required')

        return dict(
            net_sales=str(self.net_sales),
            net_units=_to_int(net_units, 'net_units'),
        )

    def report(self):
        """Return the complete per-store report as a dict.

        This is the most convenient form for a final reducer: it includes
        the settled net sales, the net item count, and the per-item value
        (``None`` when net units are zero). A store with zero net units is
        still present.
        """
        if self.net_units is None:
            raise TypeError('net_units is required')

        result = self.as_dict()
        if self.net_units == 0:
            result['net_sales_per_item'] = None
        else:
            result['net_sales_per_item'] = str(self.units_per_item())
        return result

    def __eq__(self, other):
        if not isinstance(other, SettledStoreSales):
            return NotImplemented
        # net_units is report metadata; equality is about the settled
        # monetary value and its precision
        return (self.store == other.store and
                self.net_sales == other.net_sales and
                self.places == other.places)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.store, self.net_sales, self.places))

    def __repr__(self):
        return '%s(store=%r, net_sales=%r, net_units=%r, places=%r)' % (
            self.__class__.__name__, self.store,
            str(self.net_sales), self.net_units, self.places)
