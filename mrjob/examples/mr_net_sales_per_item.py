# -*- coding: utf-8 -*-
"""Compute per-store net sales per item, with exact settlement.

This is a demonstration of :py:class:`mrjob.metrics.RatioMetric`. Each mapper
pre-aggregates rows into a single metric per store; the combiner merges those
metrics; the reducer settles them (rounds) exactly once.

Notice that returns are represented as negative sales/items, and a store
whose net item count is zero is still emitted (with no ratio).
"""
import re

from mrjob.job import MRJob
from mrjob.metrics import RatioMetric
from mrjob.metrics import metric_from_dict
from mrjob.step import MRStep


class MRNetSalesPerItem(MRJob):
    """Input lines look like ``store,sales,items``."""

    def configure_args(self):
        super(MRNetSalesPerItem, self).configure_args()

        self.add_passthru_arg(
            '--decimal-places', type=int, default=2,
            help='decimal places to keep when settling (default: 2)')

    def steps(self):
        return [MRStep(
            mapper=self.mapper,
            combiner=self.combiner,
            reducer=self.reducer,
        )]

    def mapper(self, _, line):
        line = line.strip()
        if not line or line.startswith('#'):
            return

        m = re.match(r'^(?P<store>[^,]+),(?P<sales>-?\d+(?:\.\d+)?),'
                     r'(?P<items>-?\d+)$', line)
        if not m:
            self.increment_counter('errors', 'bad_lines')
            return

        metric = RatioMetric(m.group('store'), 'net_sales_per_item')
        metric.add(m.group('sales'), m.group('items'))

        # yielding a single pre-aggregated value per row is still useful:
        # the combiner can merge these without re-reading the input
        yield m.group('store'), metric.as_dict()

    def combiner(self, store, metric_dicts):
        metric = RatioMetric(store, 'net_sales_per_item')
        for metric_dict in metric_dicts:
            metric.merge(metric_from_dict(metric_dict))

        yield store, metric.as_dict()

    def reducer(self, store, metric_dicts):
        metric = RatioMetric(store, 'net_sales_per_item')
        for metric_dict in metric_dicts:
            metric.merge(metric_from_dict(metric_dict))

        yield store, dict(
            net_sales=str(metric.numerator),
            net_items=str(metric.denominator),
            net_sales_per_item=(
                str(metric.settle(self.options.decimal_places))
                if metric.denominator else None
            ),
        )


if __name__ == '__main__':
    MRNetSalesPerItem.run()
