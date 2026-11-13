# -*- coding: utf-8 -*-
"""Exact, aggregatable metrics for mrjob.

Hadoop counters only hold integers. That makes them unsuitable for values
such as money, where intermediate results must not be rounded. This module
provides exact, additive metrics that tasks can pre-aggregate before the
runner combines their summaries.

The central type is :py:class:`RatioMetric`, which accumulates a numerator
(e.g. net sales) and a denominator (e.g. net item count). Values are kept as
:py:class:`~decimal.Decimal`, so combining shards is always just addition. No
division or rounding happens until :py:meth:`RatioMetric.settle` is
explicitly called.

Because addition is associative and commutative, the same input rows always
produce the same settlement, regardless of how they were split into tasks or
whether a combiner ran. In particular, a shard whose total happens to be zero
still contributes its exact amounts (zero) to the totals; it is not dropped
before the final settlement.

Metrics can be used in two ways:

* As ordinary job values. ``RatioMetric`` objects support ``+`` and
  ``sum()``, so they can be yielded by a mapper, merged by a combiner, and
  settled by a reducer.
* As task-local metrics collected by :py:meth:`mrjob.job.MRJob.add_metric`
  and read from the runner after the job finishes.

Both forms use the same exact arithmetic and the same settlement rules.
"""
import json
from decimal import Decimal
from decimal import InvalidOperation
from decimal import localcontext

from mrjob.py2 import integer_types
from mrjob.py2 import string_types


#: Prefix of stderr lines tasks use to send metric summaries to the runner.
METRIC_LINE_PREFIX = 'mrjob:metric:'

#: Rounding modes accepted by :func:`settle_ratio` and friends.
ROUNDING_MODES = (
    'ROUND_CEILING',
    'ROUND_DOWN',
    'ROUND_FLOOR',
    'ROUND_HALF_DOWN',
    'ROUND_HALF_EVEN',
    'ROUND_HALF_UP',
    'ROUND_UP',
    'ROUND_05UP',
)

DEFAULT_DECIMAL_PLACES = 2
DEFAULT_ROUNDING = 'ROUND_HALF_UP'


def _as_decimal(amount):
    """Convert an int, float, string, or Decimal into a Decimal.

    Floats are converted through :py:func:`repr`, which gives the shortest
    decimal string that round-trips to the same float. This treats values
    read from JSON (e.g. ``12.34``) as the human-readable decimal they
    appear to be. If exact handling of arbitrary binary floats matters, pass
    strings or :py:class:`~decimal.Decimal` values instead.
    """
    if isinstance(amount, Decimal):
        return amount
    elif isinstance(amount, bool):
        raise TypeError('amount must be a number, not %r' % (amount,))
    elif isinstance(amount, integer_types):
        return Decimal(amount)
    elif isinstance(amount, float):
        return Decimal(repr(amount))
    elif isinstance(amount, string_types):
        try:
            return Decimal(amount)
        except InvalidOperation:
            raise ValueError('invalid decimal value: %r' % (amount,))
    else:
        raise TypeError('amount must be a number, not %r' % (amount,))


def _check_decimal_places(decimal_places):
    if (not isinstance(decimal_places, integer_types) or
            isinstance(decimal_places, bool) or decimal_places < 0):
        raise ValueError('decimal_places must be a non-negative integer')


def _check_rounding(rounding):
    if rounding not in ROUNDING_MODES:
        raise ValueError(
            'rounding must be one of %s, not %r' % (
                ', '.join(ROUNDING_MODES), rounding))


def settle_ratio(numerator, denominator,
                 decimal_places=DEFAULT_DECIMAL_PLACES,
                 rounding=DEFAULT_ROUNDING, zero_denominator=None):
    """Divide two exact values and round only at settlement.

    This is the "financial settlement" operation: division and rounding
    happen exactly once, after all shards have been combined. The result is
    a :py:class:`~decimal.Decimal` quantized to *decimal_places*.

    By default, a zero denominator produces ``None``. A zero denominator is
    a meaningful report row (e.g. a store with no net items), but it has no
    finite unit value. Pass ``zero_denominator=0`` (or any other value) to
    return that value instead; dotted strings such as ``'NaN'`` are converted
    to Decimals too.

    :param numerator: exact total (e.g. net sales)
    :param denominator: exact total (e.g. net item count)
    :param int decimal_places: number of decimal places to keep
    :param str rounding: a :py:mod:`decimal` rounding mode name, defaulting
                         to financial half-up rounding
    :param zero_denominator: value to return when *denominator* is zero

    >>> settle_ratio(10, 3)
    Decimal('3.33')
    >>> settle_ratio(5, 0) is None
    True
    >>> settle_ratio(5, 0, zero_denominator=0)
    Decimal('0')
    """
    numerator = _as_decimal(numerator)
    denominator = _as_decimal(denominator)
    _check_decimal_places(decimal_places)
    _check_rounding(rounding)

    if denominator == 0:
        if zero_denominator is None:
            return None
        return _as_decimal(zero_denominator)

    quantizer = Decimal(1).scaleb(-decimal_places)

    with localcontext() as ctx:
        # ensure a very large ratio does not exceed context precision before
        # quantize() gets a chance to round it
        ctx.prec = max(
            ctx.prec,
            len(numerator.as_tuple().digits) +
            len(denominator.as_tuple().digits) + 20)
        return (numerator / denominator).quantize(
            quantizer, rounding=rounding)


def settle_value(value, decimal_places=DEFAULT_DECIMAL_PLACES,
                 rounding=DEFAULT_ROUNDING):
    """Round a single exact value at settlement time."""
    value = _as_decimal(value)
    _check_decimal_places(decimal_places)
    _check_rounding(rounding)

    quantizer = Decimal(1).scaleb(-decimal_places)
    return value.quantize(quantizer, rounding=rounding)


def exact_ratio(numerator, denominator):
    """Return ``numerator / denominator`` without rounding, or ``None``.

    The result uses a high-precision decimal context, so very large or very
    small values don't silently overflow the default 28-digit context.
    """
    numerator = _as_decimal(numerator)
    denominator = _as_decimal(denominator)

    if denominator == 0:
        return None

    with localcontext() as ctx:
        ctx.prec = max(
            ctx.prec,
            len(numerator.as_tuple().digits) +
            len(denominator.as_tuple().digits) + 20)
        return numerator / denominator


class MetricBase(object):
    """Shared helpers for exact metrics."""

    def clone(self):
        """Return a new metric with the same exact state."""
        result = self.__class__(self.group, self.name)
        result._copy_state_from(self)
        return result

    def copy(self):
        """Alias for :py:meth:`clone`."""
        return self.clone()

    def __eq__(self, other):
        return (
            isinstance(other, self.__class__) and
            self.group == other.group and
            self.name == other.name and
            self.as_dict() == other.as_dict()
        )

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.__class__.__name__, self.group, self.name))


class RatioMetric(MetricBase):
    """An exact, additive ratio metric (e.g. net sales / net items).

    Both numerator and denominator are kept as exact Decimals. Use
    :py:meth:`add` in a mapper to pre-aggregate rows, and :py:meth:`merge`
    (or ``+``) to combine metric values from other tasks.

    :param str group: optional metric group (for task-local metrics)
    :param str name: optional metric name (for task-local metrics)
    """

    _TYPE = 'ratio'
    _DEFAULT_GROUP = 'metric'
    _DEFAULT_NAME = 'value'

    def __init__(self, group=None, name=None):
        self.group = _to_label(
            self._DEFAULT_GROUP if group is None else group, 'group')
        self.name = _to_label(
            self._DEFAULT_NAME if name is None else name, 'name')
        self._numerator = Decimal(0)
        self._denominator = Decimal(0)

    @property
    def numerator(self):
        """Exact accumulated numerator (e.g. net sales)."""
        return self._numerator

    @property
    def denominator(self):
        """Exact accumulated denominator (e.g. net items)."""
        return self._denominator

    @property
    def total(self):
        """Alias for :py:attr:`numerator`."""
        return self._numerator

    @property
    def sum(self):
        """Alias for :py:attr:`numerator`."""
        return self._numerator

    @property
    def count(self):
        """Alias for :py:attr:`denominator`."""
        return self._denominator

    @property
    def value(self):
        """Exact, unrounded ratio, or ``None`` for a zero denominator.

        Use :py:meth:`settle` to round according to the settlement policy.
        """
        return exact_ratio(self._numerator, self._denominator)

    @property
    def exact_value(self):
        """Alias for :py:attr:`value`."""
        return self.value

    @property
    def average(self):
        """Alias for :py:attr:`value`."""
        return self.value

    @property
    def mean(self):
        """Alias for :py:attr:`value`."""
        return self.value

    @property
    def ratio(self):
        """Alias for :py:attr:`value`."""
        return self.value

    @property
    def num(self):
        """Alias for :py:attr:`numerator`."""
        return self._numerator

    @property
    def den(self):
        """Alias for :py:attr:`denominator`."""
        return self._denominator

    @property
    def total_count(self):
        """Alias for :py:attr:`denominator`."""
        return self._denominator

    @property
    def type(self):
        return self._TYPE

    def __repr__(self):
        return '%s(%r, %r, %r, %r)' % (
            self.__class__.__name__, self.group, self.name,
            self._numerator, self._denominator)

    def _copy_state_from(self, other):
        self._numerator = other._numerator
        self._denominator = other._denominator

    def __add__(self, other):
        result = self.clone()
        result.merge(other)
        return result

    def __radd__(self, other):
        # makes sum() work: sum() starts with the integer 0
        if other == 0:
            return self
        return self.__add__(other)

    def __iadd__(self, other):
        return self.merge(other)

    def add(self, numerator, denominator=1):
        """Add exact numerator and denominator amounts.

        Use ``metric.add(sales, items)`` for one sale row. Returns ``self``.
        """
        self._numerator += _as_decimal(numerator)
        self._denominator += _as_decimal(denominator)
        return self

    # common alternative names
    def increment(self, numerator, denominator=1):
        """Alias for :py:meth:`add`."""
        return self.add(numerator, denominator)

    def update(self, numerator, denominator=1):
        """Alias for :py:meth:`add`."""
        return self.add(numerator, denominator)

    def merge(self, other):
        """Merge another metric value or dict into this one."""
        other = _to_metric_like(self, other)
        self._numerator += other.numerator
        self._denominator += other.denominator
        return self

    def combine(self, other):
        """Alias for :py:meth:`merge`."""
        return self.merge(other)

    def settle(self, decimal_places=DEFAULT_DECIMAL_PLACES,
               rounding=DEFAULT_ROUNDING, zero_denominator=None):
        """Settle the ratio, or return *zero_denominator* if it is undefined."""
        return settle_ratio(
            self._numerator, self._denominator,
            decimal_places=decimal_places, rounding=rounding,
            zero_denominator=zero_denominator)

    def settled_value(self, decimal_places=DEFAULT_DECIMAL_PLACES,
                      rounding=DEFAULT_ROUNDING, zero_denominator=None):
        """Alias for :py:meth:`settle`."""
        return self.settle(
            decimal_places=decimal_places, rounding=rounding,
            zero_denominator=zero_denominator)

    def as_dict(self):
        """Return exact state as a JSON-safe dictionary."""
        return {
            'type': self._TYPE,
            'group': self.group,
            'name': self.name,
            'numerator': str(self._numerator),
            'denominator': str(self._denominator),
        }

    @classmethod
    def from_dict(cls, d):
        """Recreate a metric from :py:meth:`as_dict` output."""
        metric = cls(d.get('group'), d.get('name'))
        metric._numerator = _as_decimal(d['numerator'])
        metric._denominator = _as_decimal(d['denominator'])
        return metric


class DecimalMetric(MetricBase):
    """An exact, additive decimal metric.

    Use this for simple totals (rather than ratios) where counters' integer
    restriction is the problem.

    :param str group: optional metric group (for task-local metrics)
    :param str name: optional metric name (for task-local metrics)
    """

    _TYPE = 'decimal'
    _DEFAULT_GROUP = 'metric'
    _DEFAULT_NAME = 'value'

    def __init__(self, group=None, name=None):
        self.group = _to_label(
            self._DEFAULT_GROUP if group is None else group, 'group')
        self.name = _to_label(
            self._DEFAULT_NAME if name is None else name, 'name')
        self._value = Decimal(0)

    @property
    def value(self):
        """Exact accumulated value, as a Decimal."""
        return self._value

    @property
    def exact_value(self):
        """Alias for :py:attr:`value`."""
        return self._value

    @property
    def sum(self):
        """Alias for :py:attr:`value`."""
        return self._value

    @property
    def type(self):
        return self._TYPE

    def __repr__(self):
        return '%s(%r, %r, %r)' % (
            self.__class__.__name__, self.group, self.name, self._value)

    def _copy_state_from(self, other):
        self._value = other._value

    def __add__(self, other):
        result = self.clone()
        result.merge(other)
        return result

    def __radd__(self, other):
        if other == 0:
            return self
        return self.__add__(other)

    def __iadd__(self, other):
        return self.merge(other)

    def add(self, amount):
        """Add an exact amount to this metric."""
        self._value += _as_decimal(amount)
        return self

    def increment(self, amount=1):
        """Alias for :py:meth:`add`."""
        return self.add(amount)

    def update(self, amount):
        """Alias for :py:meth:`add`."""
        return self.add(amount)

    def merge(self, other):
        """Merge another metric value or dict into this one."""
        other = _to_metric_like(self, other)
        self._value += other.value
        return self

    def combine(self, other):
        """Alias for :py:meth:`merge`."""
        return self.merge(other)

    def settle(self, decimal_places=DEFAULT_DECIMAL_PLACES,
               rounding=DEFAULT_ROUNDING):
        """Round this value at settlement time."""
        return settle_value(
            self._value, decimal_places=decimal_places, rounding=rounding)

    def settled_value(self, decimal_places=DEFAULT_DECIMAL_PLACES,
                      rounding=DEFAULT_ROUNDING):
        """Alias for :py:meth:`settle`."""
        return self.settle(
            decimal_places=decimal_places, rounding=rounding)

    def as_dict(self):
        """Return exact state as a JSON-safe dictionary."""
        return {
            'type': self._TYPE,
            'group': self.group,
            'name': self.name,
            'value': str(self._value),
        }

    @classmethod
    def from_dict(cls, d):
        """Recreate a metric from :py:meth:`as_dict` output."""
        metric = cls(d.get('group'), d.get('name'))
        metric._value = _as_decimal(d['value'])
        return metric


class Averager(RatioMetric):
    """A stand-alone average.

    This is a convenience subclass of :class:`RatioMetric`; ``add(value)``
    uses a denominator of 1.

    >>> avg = Averager()
    >>> avg.add(10).add(20) is avg
    True
    >>> avg.settle(2)
    Decimal('15.00')
    """

    _DEFAULT_GROUP = 'average'
    _DEFAULT_NAME = 'value'


class Average(RatioMetric):
    """Alias for :class:`Averager`."""

    _DEFAULT_GROUP = 'average'
    _DEFAULT_NAME = 'value'


class AverageMetric(RatioMetric):
    """Alias for :class:`Averager`."""

    _DEFAULT_GROUP = 'average'
    _DEFAULT_NAME = 'value'


class Metric(RatioMetric):
    """Alias for :class:`RatioMetric`."""


# additional common names
SumMetric = DecimalMetric
DecimalCounter = DecimalMetric
Aggregator = RatioMetric
AggregatableMetric = RatioMetric


def quantize_ratio(numerator, denominator,
                   decimal_places=DEFAULT_DECIMAL_PLACES,
                   rounding=DEFAULT_ROUNDING, zero_denominator=None):
    """Alias for :func:`settle_ratio`."""
    return settle_ratio(
        numerator, denominator, decimal_places=decimal_places,
        rounding=rounding, zero_denominator=zero_denominator)


def round_ratio(numerator, denominator,
                decimal_places=DEFAULT_DECIMAL_PLACES,
                rounding=DEFAULT_ROUNDING, zero_denominator=None):
    """Alias for :func:`settle_ratio`."""
    return settle_ratio(
        numerator, denominator, decimal_places=decimal_places,
        rounding=rounding, zero_denominator=zero_denominator)


def metric_from_dict(d):
    """Create the right metric type from a dictionary produced by as_dict()."""
    if d.get('type') == DecimalMetric._TYPE:
        return DecimalMetric.from_dict(d)
    elif d.get('type') == RatioMetric._TYPE:
        return RatioMetric.from_dict(d)
    else:
        raise ValueError('unknown metric type: %r' % (d.get('type'),))


def merge_metrics(metrics):
    """Merge an iterable of metric values of the same type.

    All metrics must have the same group and name. Returns a new metric.
    This is order-independent because merging is addition.
    """
    metrics = list(metrics)

    if not metrics:
        raise ValueError('nothing to merge')

    first = _to_metric(metrics[0])
    result = first.__class__(first.group, first.name)

    for other in metrics:
        result.merge(other)

    return result


def merge_metric_summaries(*summaries):
    """Merge per-task metric summaries.

    Each summary is a map from group to name to a metric dict (the format
    returned by :py:meth:`RatioMetric.as_dict`). For convenience, a summary
    may also be wrapped in ``{'metrics': ...}``; empty summaries are
    ignored.

    Merging is pure addition of the exact numerator and denominator (or
    decimal value), so it is associative and commutative. Subtotals can be
    merged again at any time without changing the result.
    """
    result = {}

    for summary in summaries:
        if not summary:
            continue

        if 'metrics' in summary:
            summary = summary['metrics']

        for group, group_metrics in summary.items():
            for name, metric_value in group_metrics.items():
                metric_dict = _to_metric(metric_value).as_dict()
                existing = result.get(group, {}).get(name)

                if existing is None:
                    result.setdefault(group, {})[name] = metric_dict
                else:
                    existing_metric = metric_from_dict(existing)
                    existing_metric.merge(metric_dict)
                    result[group][name] = existing_metric.as_dict()

    return result


def settle_metric_summary(summary, decimal_places=DEFAULT_DECIMAL_PLACES,
                          rounding=DEFAULT_ROUNDING,
                          zero_denominator=None):
    """Settle all metrics in a merged summary.

    Returns ``{group: {name: settled_value}}``. Zero denominators are
    returned as *zero_denominator* (``None`` by default), so their rows are
    still present in the result.
    """
    if 'metrics' in summary:
        summary = summary['metrics']

    result = {}

    for group, group_metrics in summary.items():
        result[group] = {}
        for name, metric_dict in group_metrics.items():
            metric = metric_from_dict(metric_dict)
            if isinstance(metric, RatioMetric):
                result[group][name] = metric.settle(
                    decimal_places=decimal_places, rounding=rounding,
                    zero_denominator=zero_denominator)
            else:
                result[group][name] = metric.settle(
                    decimal_places=decimal_places, rounding=rounding)

    return result


def metric_summary(*metrics):
    """Serialize metric values into a per-task summary dictionary."""
    result = {}

    for metric in metrics:
        metric = _to_metric(metric)
        result.setdefault(metric.group, {})[metric.name] = metric.as_dict()

    return result


def metric_summary_line(metrics):
    """Encode a metric summary as a stderr line (without trailing newline)."""
    return METRIC_LINE_PREFIX + json.dumps(
        metric_summary(*metrics), sort_keys=True)


def parse_metric_summary_line(line):
    """Parse a metric summary line, returning a summary dict or ``None``."""
    if isinstance(line, bytes):
        line = line.decode('utf_8')

    if not line.startswith(METRIC_LINE_PREFIX):
        return None

    payload = line[len(METRIC_LINE_PREFIX):].strip()
    if not payload:
        return None

    return json.loads(payload)


def metric_summary_to_json(summary):
    """Serialize a summary dictionary to a JSON string."""
    return json.dumps(summary, sort_keys=True)


def metric_summary_from_json(s):
    """Deserialize a JSON string created by :func:`metric_summary_to_json`."""
    if isinstance(s, bytes):
        s = s.decode('utf_8')
    return json.loads(s)


def _to_label(value, name):
    if not isinstance(value, string_types):
        value = str(value)
    if not value:
        raise ValueError('%s must not be empty' % name)
    return value


def _to_metric(value):
    if isinstance(value, (RatioMetric, DecimalMetric)):
        return value
    elif isinstance(value, dict):
        return metric_from_dict(value)
    else:
        raise TypeError('expected a metric or metric dict, not %r' % (value,))


def _to_metric_like(target, other):
    other = _to_metric(other)

    if target.group != other.group or target.name != other.name:
        raise ValueError('cannot merge %r into %r' % (other, target))

    if target.type != other.type:
        raise ValueError('cannot merge %s metric into %s metric' % (
            other.type, target.type))

    return other
