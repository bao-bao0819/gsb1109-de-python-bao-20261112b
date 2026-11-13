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
"""Compute net sales and net units per store, plus net sales per item.

Input lines are tab-separated::

    store_id<TAB>sales<TAB>units
    store_id<TAB>sales<TAB>units<TAB>is_return

Returns are normally encoded as negative ``sales``/``units`` values. You
may also write positive values with ``is_return`` set to ``true`` (or
``1``); negative amounts plus ``is_return=true`` is rejected as ambiguous.

The mapper pre-aggregates every store it sees into a single
:py:class:`~mrjob.metrics.StoreSales` metric, so a store whose partial
metrics happen to cancel out to zero still yields a zero metric. The
combiner merges those metrics; Hadoop may run it zero or more times,
including more than once on already-combined output. The reducer does the
same merge and is the *only* place that rounds monetary values, so the
result is independent of input splits and combiner behavior.

Output is JSON, keyed by store::

    <store_id>\t{"net_sales": "10.00", "net_units": 3,
                 "net_sales_per_item": "3.33"}

``net_sales`` is the settled (rounded) monetary value and ``net_units`` is
the exact item count. ``net_sales_per_item`` is ``net_sales`` divided by
``net_units`` and rounded the same way; it is ``null`` when net units are
zero, but ``net_sales`` and ``net_units`` are still emitted so a store with
no net items is never dropped from the report.

All monetary fields are strings so the exact settled value survives
serialization. ``net_sales_per_item`` is computed from the *settled*
``net_sales``, matching how finance reconciles the report.

Run it locally with::

    python mr_store_sales.py -r inline input.txt

or, to change the number of decimal places::

    python mr_store_sales.py --places 3 -r inline input.txt
"""
from decimal import Decimal

from mrjob.job import MRJob
from mrjob.metrics import DEFAULT_PLACES
from mrjob.metrics import StoreSales
from mrjob.protocol import JSONProtocol
from mrjob.protocol import RawValueProtocol
from mrjob.step import MRStep


def parse_store_sales_line(line):
    """Parse one input line into ``(store, sales, units, is_return)``.

    Returns can be expressed either as negative sales/units (the most
    common file format), or as positive values with ``is_return`` set to
    ``true``. The direction is not applied here; :py:meth:`StoreSales.add_line`
    handles it. ``is_return`` may be omitted entirely (3 fields), in which
    case it defaults to ``False``.

    The input protocol is RawValueProtocol (one line per record), so this
    helper is also useful in tests.
    """
    fields = line.split('\t')
    if len(fields) == 3:
        store, sales, units = fields
        is_return = False
    elif len(fields) == 4:
        store, sales, units, is_return = fields
    else:
        raise ValueError(
            'expected 3 or 4 tab-separated fields, got %d: %r' %
            (len(fields), line))

    if not store:
        raise ValueError('store ID must not be empty')

    if isinstance(is_return, str):
        is_return = is_return.strip().lower()
        if is_return in ('true', '1'):
            is_return = True
        elif is_return in ('false', '0', ''):
            is_return = False
        else:
            raise ValueError('is_return must be true/false or 1/0, not %r' %
                             is_return)

    sales = Decimal(sales)
    units = int(units)
    return store, sales, units, is_return


class MRStoreSales(MRJob):
    """Net sales and net units per store, with mapper-side pre-aggregation."""

    INPUT_PROTOCOL = RawValueProtocol
    OUTPUT_PROTOCOL = JSONProtocol

    def steps(self):
        return [MRStep(mapper=self.mapper,
                       mapper_init=self.mapper_init,
                       mapper_final=self.mapper_final,
                       combiner=self.combiner,
                       reducer=self.reducer)]

    def configure_args(self):
        super(MRStoreSales, self).configure_args()

        self.add_passthru_arg(
            '--places', type=int, default=DEFAULT_PLACES,
            help='decimal places to round settled monetary values to '
                 '(default: %(default)s)')

    def mapper_init(self):
        # one metric per store for this mapper task; flushed in mapper_final()
        self._store_metrics = {}

    def mapper(self, _, line):
        if not line.strip():
            # tolerate blank lines (common at the end of hand-edited files)
            return

        store, sales, units, is_return = parse_store_sales_line(line)

        metric = self._store_metrics.get(store)
        if metric is None:
            metric = StoreSales(store)

        self._store_metrics[store] = metric.add_line(
            sales, units, is_return=is_return)

    def mapper_final(self):
        for store in sorted(self._store_metrics):
            yield store, self._store_metrics[store].as_dict()

    def combiner(self, store, values):
        yield store, _merge_values(store, values).as_dict()

    def reducer(self, store, values):
        metric = _merge_values(store, values)
        settled = metric.settled(self.options.places)

        # report() includes net_sales, net_units, and net_sales_per_item;
        # a store with zero net units still appears in the report
        yield store, settled.report()


def _merge_values(store, values):
    """Merge serialized :py:class:`StoreSales` values for *store*."""
    total = StoreSales(store)
    for value in values:
        if isinstance(value, StoreSales):
            metric = value
        else:
            metric = StoreSales.from_dict(value)

        # as_dict() doesn't include the store ID; the key is authoritative.
        # Rebind it so a malicious/buggy value can't silently cross stores.
        if metric.store is None:
            metric = StoreSales(store, metric.net_sales, metric.net_units)

        if metric.store != store:
            raise ValueError(
                'metric for store %r reached reducer for store %r' %
                (metric.store, store))
        total = total.merge(metric)
    return total


if __name__ == '__main__':
    MRStoreSales.run()
