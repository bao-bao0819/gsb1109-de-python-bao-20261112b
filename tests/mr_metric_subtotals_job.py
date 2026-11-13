# -*- coding: utf-8 -*-
"""Job whose reducer emits exact metric subtotals for a second step."""
from mrjob.job import MRJob
from mrjob.metrics import RatioMetric
from mrjob.metrics import metric_from_dict
from mrjob.step import MRStep


class MRMetricSubtotalsJob(MRJob):
    """Two-step job: aggregate rows, then merge subtotals again.

    Step 1 yields one exact metric dict per store. Step 2's mapper passes
    that subtotal through as-is, its combiner merges subtotals, and its
    reducer settles once.
    """

    def configure_args(self):
        super(MRMetricSubtotalsJob, self).configure_args()

        self.add_passthru_arg('--no-combiner', dest='no_combiner',
                              action='store_true')
        self.add_passthru_arg('--decimal-places', type=int, default=2)

    def steps(self):
        step1_kwargs = dict(mapper=self.mapper1, reducer=self.reducer1)
        step2_kwargs = dict(mapper=self.mapper2, reducer=self.reducer2)
        if not self.options.no_combiner:
            step1_kwargs['combiner'] = self.combiner1
            step2_kwargs['combiner'] = self.combiner2

        return [MRStep(**step1_kwargs), MRStep(**step2_kwargs)]

    def mapper1(self, _, line):
        line = line.strip()
        if not line:
            return

        store, sales, items = line.split(',')
        metric = RatioMetric(store, 'net_sales')
        metric.add(sales, items)
        yield store, metric.as_dict()

    def combiner1(self, store, metric_dicts):
        yield store, self._merge(metric_dicts, store).as_dict()

    def reducer1(self, store, metric_dicts):
        # emit the subtotal itself, not a settled ratio
        yield store, self._merge(metric_dicts, store).as_dict()

    def mapper2(self, store, metric_dict):
        yield store, metric_dict

    def combiner2(self, store, metric_dicts):
        yield store, self._merge(metric_dicts, store).as_dict()

    def reducer2(self, store, metric_dicts):
        metric = self._merge(metric_dicts, store)
        yield store, (
            str(metric.numerator),
            str(metric.denominator),
            str(metric.settle(self.options.decimal_places)),
        )

    def _merge(self, metric_dicts, store):
        metric = RatioMetric(store, 'net_sales')
        for metric_dict in metric_dicts:
            metric.merge(metric_from_dict(metric_dict))
        return metric
