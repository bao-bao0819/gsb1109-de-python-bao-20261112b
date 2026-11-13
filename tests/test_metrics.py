# Copyright 2020 Yelp
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
"""Tests for the combinable metric in :py:mod:`mrjob.metrics`."""
from decimal import Decimal

from mrjob.metrics import DEFAULT_PLACES
from mrjob.metrics import SettledStoreSales
from mrjob.metrics import StoreSales
from mrjob.metrics import _to_decimal
from mrjob.metrics import _to_int
from mrjob.metrics import _decimal_str

from tests.sandbox import BasicTestCase


class ToDecimalTestCase(BasicTestCase):

    def test_decimal_preserved_exactly(self):
        self.assertEqual(_to_decimal(Decimal('1.005')),
                         Decimal('1.005'))

    def test_int(self):
        self.assertEqual(_to_decimal(7), Decimal('7'))

    def test_float_uses_str(self):
        # Decimal(0.1) would be 0.1000000000000000055511151231257827...
        self.assertEqual(_to_decimal(0.1), Decimal('0.1'))

    def test_str(self):
        self.assertEqual(_to_decimal('1.50'), Decimal('1.50'))

    def test_bad_type(self):
        self.assertRaises(TypeError, _to_decimal, None)

    def test_non_finite_rejected(self):
        for value in ('NaN', 'Infinity', '-Infinity'):
            self.assertRaises(ValueError, _to_decimal, value)
            self.assertRaises(ValueError, _to_decimal, Decimal(value))


class ToIntTestCase(BasicTestCase):

    def test_int(self):
        self.assertEqual(_to_int(3), 3)

    def test_decimal_whole_number(self):
        self.assertEqual(_to_int(Decimal('3.000')), 3)

    def test_decimal_fractional(self):
        self.assertRaises(ValueError, _to_int, Decimal('3.5'))

    def test_bool_rejected(self):
        self.assertRaises(TypeError, _to_int, True)

    def test_bad_type(self):
        self.assertRaises(TypeError, _to_int, 1.5)


class DecimalStrTestCase(BasicTestCase):

    def test_trailing_zeros_normalized(self):
        self.assertEqual(_decimal_str(Decimal('1.10')), '1.1')
        self.assertEqual(_decimal_str(Decimal('1.1000')), '1.1')

    def test_integral_without_exponent(self):
        self.assertEqual(_decimal_str(Decimal('1000.00')), '1000')

    def test_negative_zero(self):
        self.assertEqual(_decimal_str(Decimal('-0.00')), '0')

    def test_large_value_not_rounded(self):
        value = Decimal('1' + '0' * 40)
        self.assertEqual(_decimal_str(value), '1' + '0' * 40)


class StoreSalesFromLineTestCase(BasicTestCase):

    def test_sale(self):
        metric = StoreSales.from_line('A', Decimal('10.005'), 3)

        self.assertEqual(metric.store, 'A')
        self.assertEqual(metric.net_sales, Decimal('10.005'))
        self.assertEqual(metric.net_units, 3)

    def test_return_subtracts(self):
        metric = StoreSales.from_line('A', Decimal('10'), 2, is_return=True)

        self.assertEqual(metric.net_sales, Decimal('-10'))
        self.assertEqual(metric.net_units, -2)

    def test_negative_sales_are_returns(self):
        # the prompt's input format may encode returns as negative values
        metric = StoreSales.from_line('A', Decimal('-10'), -2)

        self.assertEqual(metric.net_sales, Decimal('-10'))
        self.assertEqual(metric.net_units, -2)

    def test_negative_sales_with_is_return_rejected(self):
        # don't apply a return twice
        self.assertRaises(
            ValueError, StoreSales.from_line,
            'A', Decimal('-1'), -1, True)

    def test_mixed_signs_rejected(self):
        self.assertRaises(
            ValueError, StoreSales.from_line,
            'A', Decimal('1'), -1)
        self.assertRaises(
            ValueError, StoreSales.from_line,
            'A', Decimal('-1'), 1)

    def test_zero_units_allowed(self):
        metric = StoreSales.from_line('A', Decimal('5'), 0)

        self.assertEqual(metric.net_units, 0)


class StoreSalesAddLineTestCase(BasicTestCase):

    def test_add_sale(self):
        metric = StoreSales('A')
        metric = metric.add_line(Decimal('1.25'), 2)

        self.assertEqual(metric, StoreSales('A', Decimal('1.25'), 2))

    def test_add_return(self):
        metric = StoreSales('A', Decimal('4'), 2)
        metric = metric.add_line(Decimal('1.5'), 1, is_return=True)

        self.assertEqual(metric, StoreSales('A', Decimal('2.5'), 1))

    def test_does_not_mutate(self):
        original = StoreSales('A', Decimal('1'), 1)
        updated = original.add_line(Decimal('2'), 3)

        self.assertEqual(original, StoreSales('A', Decimal('1'), 1))
        self.assertEqual(updated, StoreSales('A', Decimal('3'), 4))


class StoreSalesMergeTestCase(BasicTestCase):

    def test_basic_merge(self):
        left = StoreSales('A', Decimal('1.25'), 2)
        right = StoreSales('A', Decimal('2.50'), 3)

        self.assertEqual(left.merge(right),
                         StoreSales('A', Decimal('3.75'), 5))

    def test_commutative(self):
        a = StoreSales('A', Decimal('1.25'), 2)
        b = StoreSales('A', Decimal('2.50'), 3)

        self.assertEqual(a.merge(b), b.merge(a))

    def test_associative(self):
        a = StoreSales('A', Decimal('0.1'), 1)
        b = StoreSales('A', Decimal('0.2'), 2)
        c = StoreSales('A', Decimal('0.3'), 3)

        self.assertEqual(
            a.merge(b).merge(c),
            a.merge(b.merge(c)))

    def test_zero_identity(self):
        metric = StoreSales('A', Decimal('4.20'), 3)

        self.assertEqual(StoreSales('A').merge(metric), metric)
        self.assertEqual(metric.merge(StoreSales('A')), metric)

    def test_zero_partial_is_kept(self):
        # a partial metric that cancels out to zero is still a metric
        sale = StoreSales.from_line('A', Decimal('5'), 1)
        ret = StoreSales.from_line('A', Decimal('5'), 1, is_return=True)

        merged = sale.merge(ret)
        self.assertTrue(merged.is_zero)
        self.assertEqual(merged.store, 'A')
        self.assertEqual(merged, StoreSales('A'))

    def test_different_stores_rejected(self):
        self.assertRaises(
            ValueError, StoreSales('A').merge, StoreSales('B'))

    def test_bad_type_rejected(self):
        self.assertRaises(TypeError, StoreSales('A').merge, 1)

    def test_add_operator(self):
        self.assertEqual(
            StoreSales('A', 1, 1) + StoreSales('A', 2, 2),
            StoreSales('A', 3, 3))


class StoreSalesSettleTestCase(BasicTestCase):

    def test_default_places(self):
        self.assertEqual(DEFAULT_PLACES, 2)

    def test_round_half_up(self):
        metric = StoreSales('A', Decimal('10.005'), 3)

        self.assertEqual(
            metric.settled(),
            SettledStoreSales('A', Decimal('10.01'), 2))

    def test_zero_is_settled_not_dropped(self):
        metric = StoreSales('A')

        self.assertEqual(
            metric.settled(),
            SettledStoreSales('A', Decimal('0.00'), 2))

    def test_returns_can_go_negative(self):
        metric = StoreSales('A', Decimal('-1.234'), 0)

        self.assertEqual(
            metric.settled(),
            SettledStoreSales('A', Decimal('-1.23'), 2))

    def test_zero_places(self):
        metric = StoreSales('A', Decimal('10.5'), 1)

        self.assertEqual(metric.settled(0).net_sales, Decimal('11'))

    def test_rounding_is_late(self):
        # rounding each line would give 0.01 + 0.01 = 0.02; settling the
        # exact sum gives 0.01, which is what finance expects
        metric = StoreSales('A')
        metric = metric.add_line(Decimal('0.004'), 1)
        metric = metric.add_line(Decimal('0.004'), 1)

        self.assertEqual(metric.settled().net_sales, Decimal('0.01'))
        self.assertEqual(metric.net_units, 2)

    def test_negative_places_rejected(self):
        self.assertRaises(ValueError, StoreSales('A').settled, -1)

    def test_result_has_no_merge(self):
        settled = StoreSales('A').settled()

        self.assertFalse(hasattr(settled, 'merge'))


class SettledStoreSalesTestCase(BasicTestCase):

    def test_units_per_item(self):
        settled = SettledStoreSales('A', Decimal('10.00'), 2)

        self.assertEqual(
            settled.units_per_item(3),
            Decimal('3.33'))

    def test_settled_carries_net_units(self):
        settled = StoreSales('A', Decimal('10.005'), 3).settled()

        self.assertEqual(settled.net_units, 3)
        self.assertEqual(
            settled.units_per_item(),
            Decimal('3.34'))

    def test_units_per_item_round_half_up(self):
        settled = SettledStoreSales('A', Decimal('10.00'), 1)

        self.assertEqual(
            settled.units_per_item(8),
            Decimal('1.3'))

    def test_zero_units(self):
        self.assertRaises(
            ZeroDivisionError,
            SettledStoreSales('A', Decimal('10.00'), 2).units_per_item, 0)

    def test_missing_units(self):
        self.assertRaises(
            TypeError, SettledStoreSales('A', Decimal('10.00')).units_per_item)

    def test_negative_units_allowed(self):
        # a store whose returns exceed its sales has negative net units;
        # reporting the per-item value is still meaningful
        settled = SettledStoreSales('A', Decimal('10.00'), 2)

        self.assertEqual(
            settled.units_per_item(-3),
            Decimal('-3.33'))

    def test_report(self):
        settled = StoreSales('A', Decimal('10.005'), 3).settled()

        self.assertEqual(
            settled.report(),
            dict(
                net_sales='10.01',
                net_units=3,
                net_sales_per_item='3.34',
            ))

    def test_report_with_zero_units(self):
        settled = StoreSales('A', Decimal('0.00'), 0).settled()

        self.assertEqual(
            settled.report(),
            dict(
                net_sales='0.00',
                net_units=0,
                net_sales_per_item=None,
            ))

    def test_report_without_units(self):
        self.assertRaises(
            TypeError, SettledStoreSales('A', Decimal('1.00')).report)


class StoreSalesSerializationTestCase(BasicTestCase):

    def test_round_trip(self):
        metric = StoreSales('A', Decimal('1.005'), 7)

        self.assertEqual(
            StoreSales.from_dict(metric.as_dict()),

            # store isn't part of the serialized value
            StoreSales(None, Decimal('1.005'), 7))

    def test_exact_decimal_survives(self):
        # JSON floats would turn 0.005 into binary noise; strings don't
        metric = StoreSales('A', Decimal('0.005'), 1)

        self.assertEqual(StoreSales.from_dict(metric.as_dict()).net_sales,
                         Decimal('0.005'))

    def test_stable_dict(self):
        a = StoreSales('A', Decimal('1.10'), 2)
        b = StoreSales('A', Decimal('1.1'), 2)

        self.assertEqual(a.as_dict(), b.as_dict())

    def test_integral_values_serialize_without_exponent(self):
        self.assertEqual(
            StoreSales('A', Decimal('1000.00'), 2).as_dict()['net_sales'],
            '1000')

    def test_merge_does_not_round_to_default_context(self):
        # default Decimal context has 28 significant digits; merging must
        # not silently round values before settlement
        big = StoreSales('A', Decimal('1' + '0' * 40), 1)
        one = StoreSales('A', Decimal('1'), 1)

        merged = big.merge(one)

        self.assertEqual(
            merged.net_sales,
            Decimal('1' + '0' * 39 + '1'))
        self.assertEqual(
            merged.settled(0).net_sales,
            Decimal('1' + '0' * 39 + '1'))

    def test_merge_is_exact_for_high_precision(self):
        left = StoreSales('A', Decimal('0.' + '1' * 40), 1)
        right = StoreSales('A', Decimal('0.' + '2' * 40), 1)

        self.assertEqual(
            left.merge(right).net_sales,
            Decimal('0.' + '3' * 40))

    def test_bad_dict(self):
        self.assertRaises(TypeError, StoreSales.from_dict, [])
        self.assertRaises(KeyError, StoreSales.from_dict, {'net_sales': '1'})

    def test_settled_dict(self):
        settled = SettledStoreSales('A', Decimal('1.20'), 2)

        self.assertEqual(
            settled.as_dict(5),
            dict(net_sales='1.20', net_units=5))
