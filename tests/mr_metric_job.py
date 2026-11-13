# -*- coding: utf-8 -*-
"""Job that records task-local metrics while processing rows."""
from mrjob.job import MRJob
from mrjob.step import MRStep


class MRMetricJob(MRJob):
    """Emit (store, sales, items) rows and record exact metrics.

    Metrics are recorded in the mapper only, so the runner-level summary
    matches the settled per-store values regardless of combiner use.
    """

    def configure_args(self):
        super(MRMetricJob, self).configure_args()

        self.add_passthru_arg('--use-combiner', action='store_true')

    def steps(self):
        kwargs = dict(mapper=self.mapper)
        if self.options.use_combiner:
            kwargs['combiner'] = self.combiner

        return [MRStep(**kwargs)]

    def mapper(self, _, line):
        line = line.strip()
        if not line:
            return

        store, sales, items = line.split(',')
        self.add_metric('net_sales', store, sales, items)
        yield store, (sales, items)

    def combiner(self, store, values):
        sales = 0
        items = 0
        for value_sales, value_items in values:
            sales += int(float(value_sales) * 100)
            items += int(value_items)


        yield store, ('%.2f' % (sales / 100.0), str(items))


if __name__ == '__main__':
    MRMetricJob.run()
