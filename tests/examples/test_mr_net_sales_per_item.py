# -*- coding: utf-8 -*-
"""Tests for the net-sales-per-item example."""
from mrjob.examples.mr_net_sales_per_item import MRNetSalesPerItem

from tests.job import run_job
from tests.sandbox import BasicTestCase


class MRNetSalesPerItemTestCase(BasicTestCase):

    def test_empty(self):
        self.assertEqual(run_job(MRNetSalesPerItem([])), {})

    def test_store_report(self):
        input = (
            b'a,10.00,3\n'
            b'a,-1.00,1\n'
            b'a,2.50,2\n'
            b'b,0,0\n'
        )

        output = run_job(MRNetSalesPerItem([]), input)

        self.assertEqual(output['a'], dict(
            net_sales='11.50',
            net_items='6',
            net_sales_per_item='1.92',
        ))

        # a store with zero items is still present, but has no ratio
        self.assertEqual(output['b'], dict(
            net_sales='0',
            net_items='0',
            net_sales_per_item=None,
        ))

    def test_returns_reduce_sales(self):
        input = b'a,5.00,1\na,-3.00,1\n'

        output = run_job(MRNetSalesPerItem([]), input)
        self.assertEqual(output['a'], dict(
            net_sales='2.00',
            net_items='2',
            net_sales_per_item='1.00',
        ))

    def test_settlement_decimal_places(self):
        input = b'a,10,3\n'

        output = run_job(
            MRNetSalesPerItem(['--decimal-places', '3']), input)

        self.assertEqual(output['a']['net_sales_per_item'], '3.333')
