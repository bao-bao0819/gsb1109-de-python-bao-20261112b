# -*- coding: utf-8 -*-
"""Job used to test exact metric aggregation across shardings."""
from mrjob.metrics import RatioMetric
from mrjob.metrics import metric_from_dict
from mrjob.job import MRJob
from mrjob.step import MRStep


class MRNetSalesMetricJob(MRJob):
    """Aggregate (sales, items) rows per store with exact metric values.

    Input lines look like ``store,sales,items``. Blank lines are ignored.
    The mapper yields a ``RatioMetric`` value for each store, so the
    combiner and reducer merge pre-aggregated values instead of recomputing
    them from rows.
    """

    def configure_args(self):
        super(MRNetSalesMetricJob, self).configure_args()

        self.add_passthru_arg('--use-combiner', action='store_true')
        self.add_passthru_arg('--decimal-places', type=int, default=2)

    def steps(self):
        kwargs = dict(mapper=self.mapper, reducer=self.reducer)
        if self.options.use_combiner:
            kwargs['combiner'] = self.combiner
        return [MRStep(**kwargs)]

    def mapper(self, _, line):
        line = line.strip()
        if not line:
            return

        fields = line.split(',')
        if len(fields) != 3:
            return

        store, sales, items = fields
        metric = RatioMetric(store, 'net_sales')
        metric.add(sales, items)
        yield store, metric.as_dict()

    def combiner(self, store, metric_dicts):
        metric = RatioMetric(store, 'net_sales')
        for metric_dict in metric_dicts:
            metric.merge(metric_from_dict(metric_dict))

        yield store, metric.as_dict()

    def reducer(self, store, metric_dicts):
        metric = RatioMetric(store, 'net_sales')
        for metric_dict in metric_dicts:
            metric.merge(metric_from_dict(metric_dict))

        yield store, (
            str(metric.numerator),
            str(metric.denominator),
            str(metric.settle(decimal_places=self.options.decimal_places)),
        )
