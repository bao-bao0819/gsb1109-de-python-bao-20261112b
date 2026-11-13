# -*- coding: utf-8 -*-
"""Integration tests for exact metric aggregation across shardings."""
from decimal import Decimal
from io import BytesIO

from mrjob.metrics import RatioMetric
from mrjob.metrics import metric_from_dict

from tests.mr_net_sales_metric import MRNetSalesMetricJob
from tests.py2 import patch
from tests.sandbox import SandboxedTestCase


# the same sales rows, in several different orders
SALES_ROWS = [
    ('store-a', '10.00', '3'),
    ('store-a', '-1.00', '1'),
    ('store-b', '0.00', '0'),
    ('store-c', '5.00', '2'),
    ('store-a', '2.50', '2'),
    ('store-c', '-0.50', '0'),
]


class NetSalesMetricTestCase(SandboxedTestCase):

    def _run(self, rows, use_combiner=False, num_cores=None, places=2):
        input_bytes = ''.join(
            '%s,%s,%s\n' % row for row in rows).encode('utf-8')

        args = ['-r', 'inline']
        if use_combiner:
            args.append('--use-combiner')
        args.extend(['--decimal-places', str(places)])

        job = MRNetSalesMetricJob(args)
        job.sandbox(stdin=BytesIO(input_bytes))

        with job.make_runner() as runner:
            runner.run()
            output = dict(
                (store, tuple(values))
                for store, values in job.parse_output(runner.cat_output())
            )

        return output

    def test_store_report(self):
        output = self._run(SALES_ROWS)

        # store-a: 11.50 / 6 = 1.9166... -> 1.92
        # store-b: 0 / 0 -> zero denominator
        # store-c: 4.50 / 2 = 2.25
        self.assertEqual(output['store-a'],
                         ('11.50', '6', '1.92'))
        self.assertEqual(output['store-b'],
                         ('0.00', '0', 'None'))
        self.assertEqual(output['store-c'],
                         ('4.50', '2', '2.25'))

    def test_zero_item_store_still_appears(self):
        output = self._run([('store-b', '0.00', '0')])

        self.assertIn('store-b', output)
        self.assertEqual(output['store-b'][2], 'None')

    def test_returns_are_negative(self):
        output = self._run([
            ('store-a', '5.00', '1'),
            ('store-a', '-3.00', '1'),
        ])

        self.assertEqual(output['store-a'], ('2.00', '2', '1.00'))

    def test_sharding_does_not_change_result(self):
        # force several mappers, with rows mixed up between them
        outputs = set()

        for rows in (
                SALES_ROWS,
                list(reversed(SALES_ROWS)),
                SALES_ROWS[3:] + SALES_ROWS[:3],
        ):
            output = self._run(rows, num_cores=1)
            outputs.add(tuple(sorted(output.items())))

        self.assertEqual(len(outputs), 1)

    def test_combiner_does_not_change_result(self):
        without_combiner = self._run(SALES_ROWS, use_combiner=False)
        with_combiner = self._run(SALES_ROWS, use_combiner=True)

        self.assertEqual(without_combiner, with_combiner)

    def test_result_independent_of_decimal_places(self):
        output_0 = self._run(SALES_ROWS, places=0)
        output_3 = self._run(SALES_ROWS, places=3)

        self.assertEqual(output_0['store-a'][2], '2')
        self.assertEqual(output_3['store-a'][2], '1.917')

    def test_metric_arithmetic_is_associative(self):
        # merge the same rows in several groupings; the settled value must
        # not depend on how the partial metrics were combined
        values = [RatioMetric('store-a', 'net_sales').add(s, i)
                  for s, i in [(10, 3), (-1, 1), (2.5, 2)]]

        expected = values[0] + values[1] + values[2]
        grouped = (values[0] + values[1]) + values[2]
        regrouped = values[0] + (values[1] + values[2])

        self.assertEqual(expected.settle(2), Decimal('1.92'))
        self.assertEqual(grouped.settle(2), expected.settle(2))
        self.assertEqual(regrouped.settle(2), expected.settle(2))

    def test_metric_round_trip_through_dict(self):
        metric = RatioMetric('store-a', 'net_sales')
        metric.add('10.00', 3).add('-1.00', 1).add('2.50', 2)

        self.assertEqual(
            RatioMetric.from_dict(metric.as_dict()),
            metric)


class MetricSubtotalsTestCase(SandboxedTestCase):

    def _run(self, rows, no_combiner=False):
        from tests.mr_metric_subtotals_job import MRMetricSubtotalsJob

        input_bytes = ''.join(
            '%s,%s,%s\n' % row for row in rows).encode('utf-8')

        args = ['-r', 'inline']
        if no_combiner:
            args.append('--no-combiner')

        job = MRMetricSubtotalsJob(args)
        job.sandbox(stdin=BytesIO(input_bytes))

        with job.make_runner() as runner:
            runner.run()
            return dict(
                (store, tuple(values))
                for store, values in job.parse_output(runner.cat_output())
            )

    def test_subtotals_merged_again(self):
        output = self._run(SALES_ROWS)

        self.assertEqual(output['store-a'], ('11.50', '6', '1.92'))
        self.assertEqual(output['store-b'], ('0.00', '0', 'None'))
        self.assertEqual(output['store-c'], ('4.50', '2', '2.25'))

    def test_combiner_does_not_change_subtotal_result(self):
        self.assertEqual(
            self._run(SALES_ROWS, no_combiner=False),
            self._run(SALES_ROWS, no_combiner=True))


class ZeroCancelingShardTestCase(SandboxedTestCase):

    def test_zero_canceling_shard_still_contributes(self):
        # store goes 1,10 -> 0,0 -> 2,2. The middle row cancels exactly;
        # it must not be dropped before settlement.
        rows = [
            ('store', '1.00', '1'),
            ('store', '0.00', '0'),
            ('store', '2.00', '2'),
        ]

        from tests.mr_metric_subtotals_job import MRMetricSubtotalsJob

        input_bytes = ''.join(
            '%s,%s,%s\n' % row for row in rows).encode('utf-8')

        job = MRMetricSubtotalsJob(['-r', 'inline'])
        job.sandbox(stdin=BytesIO(input_bytes))

        with job.make_runner() as runner:
            runner.run()
            output = dict(job.parse_output(runner.cat_output()))

        # 3.00 / 3 = 1.00, not 1.50 or 0.00 (which would happen if the
        # middle row were dropped or the local zero were treated as empty)
        self.assertEqual(output['store'], ['3.00', '3', '1.00'])

    def test_shard_local_zero_is_preserved_in_output(self):
        metric = RatioMetric('store', 'net_sales')
        metric.add('10.00', '2')
        metric.add('-10.00', '-0')
        metric.add('1.00', '1')

        self.assertEqual(metric.numerator, Decimal('1.00'))
        self.assertEqual(metric.denominator, Decimal('3'))
        self.assertEqual(metric.settle(2), Decimal('0.33'))
