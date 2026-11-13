# -*- coding: utf-8 -*-
"""Tests for MRJob's task-local exact metrics."""
from decimal import Decimal
from io import BytesIO

from mrjob.metrics import DecimalMetric
from mrjob.metrics import RatioMetric
from mrjob.metrics import metric_summary_line
from mrjob.metrics import parse_metric_summary_line

from tests.mr_metric_job import MRMetricJob
from tests.sandbox import BasicTestCase
from tests.sandbox import SandboxedTestCase


class AddMetricTestCase(BasicTestCase):

    def test_ratio_metric(self):
        job = MRMetricJob([]).sandbox()

        job.add_metric('sales', 'store', '10.00', 3)
        job.add_metric('sales', 'store', '-1.00', 1)

        metric = job._metrics[('ratio', 'sales', 'store')]
        self.assertEqual(metric.numerator, Decimal('9.00'))
        self.assertEqual(metric.denominator, Decimal('4'))
        self.assertEqual(metric.settle(2), Decimal('2.25'))

    def test_decimal_metric(self):
        job = MRMetricJob([]).sandbox()

        job.add_metric('money', 'refunds', '1.10')
        job.add_metric('money', 'refunds', '2.20')

        metric = job._metrics[('decimal', 'money', 'refunds')]
        self.assertEqual(metric.value, Decimal('3.30'))
        self.assertEqual(metric.settle(2), Decimal('3.30'))

    def test_no_rounding_at_add_time(self):
        job = MRMetricJob([]).sandbox()

        for _ in range(3):
            job.add_metric('sales', 'store', '0.01', 1)

        metric = job._metrics[('ratio', 'sales', 'store')]
        self.assertEqual(metric.numerator, Decimal('0.03'))
        self.assertEqual(metric.denominator, Decimal('3'))

    def test_aliases(self):
        job = MRMetricJob([]).sandbox()

        job.add_ratio_metric('sales', 'store', '10.00', 3)
        job.add_decimal_metric('money', 'refunds', '1.10')
        job.merge_metric('sales', 'store', '1.00', 1)

        self.assertEqual(
            job._metrics[('ratio', 'sales', 'store')].numerator,
            Decimal('11.00'))

    def test_flush_writes_summary_line(self):
        job = MRMetricJob([]).sandbox()

        job.add_metric('sales', 'store', '10.00', 3)
        job._flush_metrics()

        line = job.stderr.getvalue().splitlines()[0]
        summary = parse_metric_summary_line(line)

        self.assertEqual(
            summary['sales']['store']['numerator'], '10.00')
        self.assertEqual(
            summary['sales']['store']['denominator'], '3')

    def test_flush_without_metrics_does_nothing(self):
        job = MRMetricJob([]).sandbox()
        job._flush_metrics()

        self.assertEqual(job.stderr.getvalue(), b'')

    def test_metric_kinds_cannot_collide(self):
        job = MRMetricJob([]).sandbox()

        job.add_metric('m', 'x', '1.00')
        job.add_metric('m', 'x', '1.00', 1)

        self.assertIn(('decimal', 'm', 'x'), job._metrics)
        self.assertIn(('ratio', 'm', 'x'), job._metrics)


class RunnerMetricTestCase(SandboxedTestCase):

    INPUT = (
        b'store-a,10.00,3\n'
        b'store-b,0.00,0\n'
        b'store-a,-1.00,1\n'
        b'store-c,5.00,2\n'
    )

    def _run(self, use_combiner=False, num_cores=None):
        args = ['-r', 'inline']
        if use_combiner:
            args.append('--use-combiner')
        if num_cores is not None:
            args.extend(['--num-cores', str(num_cores)])

        job = MRMetricJob(args)
        job.sandbox(stdin=BytesIO(self.INPUT))

        with job.make_runner() as runner:
            runner.run()
            output = dict(job.parse_output(runner.cat_output()))

        return job, runner, output

    def test_runner_metric_summary(self):
        job, runner, output = self._run()

        summary = runner.metric_summary()
        self.assertEqual(
            summary['net_sales']['store-a']['numerator'], '9.00')
        self.assertEqual(
            summary['net_sales']['store-a']['denominator'], '4')
        self.assertEqual(
            summary['net_sales']['store-b']['numerator'], '0.00')
        self.assertEqual(
            summary['net_sales']['store-b']['denominator'], '0')

    def test_settle_metrics(self):
        job, runner, output = self._run()

        self.assertEqual(
            runner.settle_metrics('net_sales', 'store-a'),
            Decimal('2.25'))
        self.assertEqual(
            runner.settle_metrics('net_sales', 'store-b'),
            None)
        self.assertEqual(
            runner.settle_metrics('net_sales', 'store-c'),
            Decimal('2.50'))

    def test_settle_group(self):
        job, runner, output = self._run()

        self.assertEqual(
            runner.settle_metrics('net_sales'),
            {
                'store-a': Decimal('2.25'),
                'store-b': None,
                'store-c': Decimal('2.50'),
            })

    def test_traditional_counters_still_work(self):
        job, runner, output = self._run()

        self.assertEqual(runner.counters()[0], {})

    def test_metrics_are_read_from_job_object(self):
        job, runner, output = self._run()

        self.assertEqual(
            job.settle_metrics('net_sales', 'store-a'),
            Decimal('2.25'))

    def test_combiner_does_not_change_summary(self):
        job1, runner1, output1 = self._run(use_combiner=False)
        job2, runner2, output2 = self._run(use_combiner=True)

        self.assertEqual(
            runner1.settle_metrics('net_sales'),
            runner2.settle_metrics('net_sales'))
        self.assertEqual(output1, output2)

    def test_num_cores_does_not_change_summary(self):
        job1, runner1, output1 = self._run(num_cores=1)
        job2, runner2, output2 = self._run(num_cores=3)

        self.assertEqual(
            runner1.settle_metrics('net_sales'),
            runner2.settle_metrics('net_sales'))

    def test_settle_multiple_decimal_places(self):
        job, runner, output = self._run()

        self.assertEqual(
            runner.settle_metrics(
                'net_sales', 'store-a', decimal_places=3),
            Decimal('2.250'))

    def test_settle_no_metrics(self):
        job = MRMetricJob(['-r', 'inline']).sandbox(stdin=BytesIO(b''))
        with job.make_runner() as runner:
            runner.run()

        self.assertEqual(runner.metric_summaries(), [{}])
        self.assertRaises(
            ValueError, runner.settle_metrics, 'net_sales')


class FlexibleMetricArgumentTestCase(BasicTestCase):

    def test_numerator_and_value_keywords(self):
        job = MRMetricJob([]).sandbox()

        job.add_metric('sales', 'store', numerator='1.20', denominator=2)
        job.add_metric('money', 'refunds', value='3.40')

        self.assertEqual(
            job.metrics[('ratio', 'sales', 'store')].numerator,
            Decimal('1.20'))
        self.assertEqual(
            job.metrics[('decimal', 'money', 'refunds')].value,
            Decimal('3.40'))

    def test_metric_object_as_amount(self):
        job = MRMetricJob([]).sandbox()

        source = RatioMetric('sales', 'store').add('1.20', 2)
        job.add_metric('sales', 'store', source)

        self.assertEqual(
            job.metrics[('ratio', 'sales', 'store')],
            source)

    def test_metric_dict_as_amount(self):
        job = MRMetricJob([]).sandbox()

        source = RatioMetric('sales', 'store').add('1.20', 2)
        job.add_metric('sales', 'store', source.as_dict())

        self.assertEqual(
            job.metrics[('ratio', 'sales', 'store')], source)

    def test_increment_metric_defaults_to_one(self):
        job = MRMetricJob([]).sandbox()

        job.increment_metric('money', 'refunds')

        self.assertEqual(
            job.metrics[('decimal', 'money', 'refunds')].value,
            Decimal(1))


class SettledAliasesTestCase(SandboxedTestCase):

    def test_job_settled_metrics_all_groups(self):
        job = MRMetricJob(['-r', 'inline'])
        job.sandbox(stdin=BytesIO(b'a,10,3\nb,0,0\n'))

        with job.make_runner() as runner:
            runner.run()

        self.assertEqual(
            job.settled_metrics(),
            {'net_sales': {'a': Decimal('3.33'), 'b': None}})

    def test_runner_settled_metrics_alias(self):
        job = MRMetricJob(['-r', 'inline'])
        job.sandbox(stdin=BytesIO(b'a,10,3\n'))

        with job.make_runner() as runner:
            runner.run()

            self.assertEqual(
                runner.settled_metrics('net_sales', 'a'),
                Decimal('3.33'))
            self.assertEqual(
                runner.metrics(),
                runner.metric_summary())
