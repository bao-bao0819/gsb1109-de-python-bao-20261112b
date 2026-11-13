# -*- coding: utf-8 -*-
"""Tests for exact, aggregatable metrics."""
from decimal import Decimal

from mrjob.metrics import Averager
from mrjob.metrics import DecimalMetric
from mrjob.metrics import METRIC_LINE_PREFIX
from mrjob.metrics import RatioMetric
from mrjob.metrics import exact_ratio
from mrjob.metrics import merge_metric_summaries
from mrjob.metrics import metric_summary
from mrjob.metrics import metric_summary_line
from mrjob.metrics import parse_metric_summary_line
from mrjob.metrics import settle_metric_summary
from mrjob.metrics import settle_ratio

from tests.sandbox import BasicTestCase


class SettleRatioTestCase(BasicTestCase):

    def test_basic(self):
        self.assertEqual(settle_ratio(10, 3, 2), Decimal('3.33'))

    def test_round_half_up(self):
        self.assertEqual(
            settle_ratio(1, 8, decimal_places=2), Decimal('0.13'))

    def test_round_half_even(self):
        self.assertEqual(
            settle_ratio(1, 8, decimal_places=2,
                         rounding='ROUND_HALF_EVEN'),
            Decimal('0.12'))

    def test_zero_denominator(self):
        self.assertEqual(settle_ratio(0, 0), None)

    def test_zero_denominator_with_value(self):
        self.assertEqual(
            settle_ratio(5, 0, zero_denominator=0), Decimal('0'))

    def test_negative_sales(self):
        self.assertEqual(settle_ratio(-10, 4, 2), Decimal('-2.50'))

    def test_bad_decimal_places(self):
        self.assertRaises(ValueError, settle_ratio, 1, 2, -1)
        self.assertRaises(ValueError, settle_ratio, 1, 2, '2')

    def test_bad_rounding(self):
        self.assertRaises(
            ValueError, settle_ratio, 1, 2, 2, 'ROUND_SIDEWAYS')

    def test_bad_amount(self):
        self.assertRaises(TypeError, settle_ratio, None, 1)
        self.assertRaises(ValueError, settle_ratio, 'foo', 1)


class ExactRatioTestCase(BasicTestCase):

    def test_no_rounding(self):
        self.assertEqual(
            exact_ratio(1, 3).quantize(Decimal('0.0001')),
            Decimal('0.3333'))

    def test_zero_denominator(self):
        self.assertEqual(exact_ratio(1, 0), None)


class RatioMetricTestCase(BasicTestCase):

    def test_add_and_settle(self):
        metric = RatioMetric('sales', 'store')
        metric.add(10, 3).add(5, 2)

        self.assertEqual(metric.numerator, Decimal(15))
        self.assertEqual(metric.denominator, Decimal(5))
        self.assertEqual(metric.settle(2), Decimal('3.00'))

    def test_returns_are_negative(self):
        metric = RatioMetric('sales', 'store')
        metric.add(10, 3).add(-1, 1)

        self.assertEqual(metric.numerator, Decimal(9))
        self.assertEqual(metric.denominator, Decimal(4))
        self.assertEqual(metric.settle(2), Decimal('2.25'))

    def test_zero_denominator_is_none(self):
        metric = RatioMetric('sales', 'store')
        metric.add(0, 0)

        self.assertEqual(metric.value, None)
        self.assertEqual(metric.settle(2), None)
        self.assertEqual(
            metric.settle(2, zero_denominator=0), Decimal('0'))

    def test_partial_shard_cancels_to_zero(self):
        # a shard that cancels to zero must still contribute its (zero)
        # numerator and denominator before settlement
        metric = RatioMetric('sales', 'store')
        metric.add(10, 1).add(-10, 0).add(1, 0)

        self.assertEqual(metric.settle(2), Decimal('1.00'))

    def test_add_does_not_round(self):
        metric = RatioMetric('sales', 'store')
        for _ in range(10):
            metric.add(Decimal('1.00'), 1)

        self.assertEqual(metric.numerator, Decimal('10.00'))
        self.assertEqual(metric.denominator, Decimal('10'))
        self.assertEqual(metric.settle(2), Decimal('1.00'))

    def test_merge(self):
        a = RatioMetric('sales', 'store').add(10, 3)
        b = RatioMetric('sales', 'store').add(-1, 1)

        a.merge(b)
        self.assertEqual(a.numerator, Decimal(9))
        self.assertEqual(a.denominator, Decimal(4))

    def test_add_operator(self):
        a = RatioMetric('sales', 'store').add(10, 3)
        b = RatioMetric('sales', 'store').add(-1, 1)

        c = a + b
        self.assertEqual(c.numerator, Decimal(9))
        self.assertEqual(c.denominator, Decimal(4))

        # sum() starts with 0
        self.assertEqual(sum([a, b]), c)

    def test_merge_rejects_other_metric(self):
        a = RatioMetric('sales', 'store')
        b = RatioMetric('sales', 'other')

        self.assertRaises(ValueError, a.merge, b)

    def test_float_input(self):
        metric = RatioMetric('sales', 'store')
        metric.add(0.1, 1).add(0.2, 1).add(0.3, 1).add(0.4, 1)

        self.assertEqual(metric.numerator, Decimal('1.0'))
        self.assertEqual(metric.settle(2), Decimal('0.25'))

    def test_as_dict_round_trip(self):
        metric = RatioMetric('sales', 'store').add('1.10', 2)
        d = metric.as_dict()

        recreated = RatioMetric.from_dict(d)
        self.assertEqual(recreated, metric)

    def test_settle_with_more_places(self):
        metric = RatioMetric('sales', 'store').add(1, 3)
        self.assertEqual(metric.settle(4), Decimal('0.3333'))


class DecimalMetricTestCase(BasicTestCase):

    def test_add_and_settle(self):
        metric = DecimalMetric('money', 'refunds')
        metric.add('1.001').add('2.002')

        self.assertEqual(metric.value, Decimal('3.003'))
        self.assertEqual(metric.settle(2), Decimal('3.00'))

    def test_merge(self):
        a = DecimalMetric('money', 'refunds').add('1.10')
        b = DecimalMetric('money', 'refunds').add('2.20')

        a.merge(b)
        self.assertEqual(a.value, Decimal('3.30'))

    def test_add_operator(self):
        a = DecimalMetric('money', 'refunds').add('1.10')
        b = DecimalMetric('money', 'refunds').add('2.20')

        self.assertEqual((a + b).value, Decimal('3.30'))

    def test_round_trip(self):
        metric = DecimalMetric('money', 'refunds').add('1.10')
        self.assertEqual(
            DecimalMetric.from_dict(metric.as_dict()), metric)


class AveragerTestCase(BasicTestCase):

    def test_average(self):
        avg = Averager()
        avg.add(10).add(20)

        self.assertEqual(avg.denominator, Decimal(2))
        self.assertEqual(avg.settle(2), Decimal('15.00'))


class MetricSummaryTestCase(BasicTestCase):

    def test_merge_is_order_independent(self):
        a = {'sales': {'store': RatioMetric('sales', 'store').add(10, 3)}}
        b = {'sales': {'store': RatioMetric('sales', 'store').add(-1, 1)}}
        c = {'sales': {'store': RatioMetric('sales', 'store').add(2, 0)}}

        summary1 = merge_metric_summaries(a, b, c)
        summary2 = merge_metric_summaries(c, b, a)

        self.assertEqual(
            summary1['sales']['store']['numerator'],
            summary2['sales']['store']['numerator'])
        self.assertEqual(
            summary1['sales']['store']['denominator'],
            summary2['sales']['store']['denominator'])

        settled = settle_metric_summary(summary1, decimal_places=2)
        self.assertEqual(settled['sales']['store'], Decimal('2.75'))

    def test_zero_denominator_survives(self):
        metric = RatioMetric('sales', 'empty-store').add(0, 0)
        summary = metric_summary(metric)

        settled = settle_metric_summary(summary, decimal_places=2)
        self.assertIn('empty-store', settled['sales'])
        self.assertEqual(settled['sales']['empty-store'], None)

    def test_line_round_trip(self):
        metric = RatioMetric('sales', 'store').add(10, 3)
        line = metric_summary_line([metric])

        self.assertTrue(line.startswith(METRIC_LINE_PREFIX))
        parsed = parse_metric_summary_line(line)
        self.assertEqual(parsed, metric_summary(metric))

    def test_parse_other_line(self):
        self.assertEqual(parse_metric_summary_line(b'reporter:status:foo'),
                         None)

    def test_merge_keeps_separate_groups(self):
        a = metric_summary(RatioMetric('sales', 'store').add(1, 1))
        b = metric_summary(RatioMetric('sales', 'other').add(2, 2))

        merged = merge_metric_summaries(a, b)
        self.assertEqual(sorted(merged['sales']), ['other', 'store'])




class MetricHelpersTestCase(BasicTestCase):

    def test_merge_metrics(self):
        from mrjob.metrics import merge_metrics

        merged = merge_metrics([
            RatioMetric('sales', 'store').add(10, 3),
            RatioMetric('sales', 'store').add(-1, 1),
        ])

        self.assertEqual(merged.numerator, Decimal(9))
        self.assertEqual(merged.denominator, Decimal(4))

    def test_clone_is_independent(self):
        metric = RatioMetric('sales', 'store').add(1, 1)
        clone = metric.clone()

        clone.add(1, 1)
        self.assertNotEqual(metric, clone)
        self.assertEqual(metric.numerator, Decimal(1))

    def test_equality_uses_exact_state(self):
        self.assertEqual(
            RatioMetric('sales', 'store').add(1, 2),
            RatioMetric('sales', 'store').add(1, 2))

        self.assertNotEqual(
            RatioMetric('sales', 'store').add(1, 2),
            RatioMetric('sales', 'store').add(1, 3))
