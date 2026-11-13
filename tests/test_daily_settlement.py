# -*- coding: utf-8 -*-
"""settlement.daily_settlement 的测试。"""

import json
from io import BytesIO
from unittest import TestCase

from settlement.daily_settlement import MRDailySettlement


def run_job(records, extra_args=()):
    job = MRDailySettlement(['-r', 'inline', '--no-conf'] + list(extra_args))
    data = ''.join(json.dumps(r, ensure_ascii=False) + '\n' for r in records)
    job.sandbox(stdin=BytesIO(data.encode('utf-8')))
    out = []
    with job.make_runner() as runner:
        runner.run()
        for _, value in job.parse_output(runner.cat_output()):
            out.append(value)
    return out


def deltas(records):
    return [r for r in records if r['record'] == 'delta']


def unverified(records):
    return [r for r in records if r['record'] == 'unverified']


def sale(sale_id, qty, store='ST01', category='饮料', biz_date='2026-11-12'):
    return {'type': 'sale', 'sale_id': sale_id, 'biz_date': biz_date,
            'store': store, 'category': category, 'qty': qty}


def refund(refund_id, sale_id, qty):
    return {'type': 'refund', 'refund_id': refund_id,
            'sale_id': sale_id, 'qty': qty}


def prev(qty, store='ST01', category='饮料', biz_date='2026-11-12',
         sale_ids=()):
    return {'type': 'prev', 'biz_date': biz_date, 'store': store,
            'category': category, 'qty': qty, 'sale_ids': list(sale_ids)}


class BasicDeltaTestCase(TestCase):

    def test_sales_aggregated_per_store_category_day(self):
        out = run_job([
            sale('S1', 3),
            sale('S2', 2),
            sale('S3', 5, category='零食'),
            sale('S4', 7, store='ST02'),
            sale('S5', 1, biz_date='2026-11-11'),
        ])
        got = {(d['biz_date'], d['store'], d['category']):
               (d['delta'], d['sale_ids']) for d in deltas(out)}
        self.assertEqual(got, {
            ('2026-11-12', 'ST01', '饮料'): (5, ['S1', 'S2']),
            ('2026-11-12', 'ST01', '零食'): (5, ['S3']),
            ('2026-11-12', 'ST02', '饮料'): (7, ['S4']),
            ('2026-11-11', 'ST01', '饮料'): (1, ['S5']),
        })

    def test_no_delta_when_matching_prev_report(self):
        out = run_job([
            prev(5, sale_ids=['S1', 'S2']),
            sale('S1', 3),
            sale('S2', 2),
        ])
        self.assertEqual(deltas(out), [])

    def test_delta_against_prev_report(self):
        out = run_job([
            prev(5, sale_ids=['S0']),
            prev(4, category='零食', sale_ids=['S9']),
            sale('S1', 8),
        ])
        got = {(d['store'], d['category']): d['delta'] for d in deltas(out)}
        self.assertEqual(got, {('ST01', '饮料'): 3, ('ST01', '零食'): -4})


class RefundTestCase(TestCase):

    def test_partial_refunds_accumulate(self):
        out = run_job([
            sale('S1', 5),
            refund('R1', 'S1', 2),
            refund('R2', 'S1', 1),
        ])
        self.assertEqual([d['delta'] for d in deltas(out)], [2])
        self.assertEqual(unverified(out), [])

    def test_refund_may_arrive_before_sale(self):
        out = run_job([
            refund('R1', 'S1', 2),
            refund('R2', 'S1', 1),
            sale('S1', 5),
        ])
        self.assertEqual([d['delta'] for d in deltas(out)], [2])

    def test_over_refund_part_stays_unverified(self):
        out = run_job([
            sale('S1', 3),
            refund('R1', 'S1', 2),
            refund('R2', 'S1', 3),
        ])
        self.assertEqual(deltas(out), [])
        self.assertEqual(unverified(out), [
            {'record': 'unverified', 'refund_id': 'R2', 'sale_id': 'S1',
             'qty': 2, 'reason': 'over_refund'},
        ])

    def test_duplicate_refund_counted_once(self):
        out = run_job([
            sale('S1', 3),
            refund('R1', 'S1', 2),
            refund('R1', 'S1', 2),
        ])
        self.assertEqual([d['delta'] for d in deltas(out)], [1])
        self.assertEqual(unverified(out), [])

    def test_refund_without_sale_stays_unverified(self):
        out = run_job([
            sale('S1', 3),
            refund('R9', 'S404', 2),
        ])
        self.assertEqual([d['delta'] for d in deltas(out)], [3])
        self.assertEqual(unverified(out), [
            {'record': 'unverified', 'refund_id': 'R9', 'sale_id': 'S404',
             'qty': 2, 'reason': 'unknown_sale'},
        ])


class CorrectionTestCase(TestCase):

    def test_category_change_traces_to_same_sale(self):
        out = run_job([
            prev(3, category='旧类', sale_ids=['S1']),
            sale('S1', 3, category='旧类'),
            {'type': 'correction', 'sale_id': 'S1', 'category': '新类',
             'ts': '2026-11-12T21:00:00'},
        ])
        got = {d['category']: (d['delta'], d['sale_ids'])
               for d in deltas(out)}
        self.assertEqual(got, {'旧类': (-3, ['S1']), '新类': (3, ['S1'])})

    def test_store_correction_and_refund_follows_final_attribution(self):
        out = run_job([
            prev(4, store='ST01', sale_ids=['S1']),
            sale('S1', 4, store='ST01'),
            {'type': 'correction', 'sale_id': 'S1', 'store': 'ST02',
             'ts': '2026-11-12T22:00:00'},
            refund('R1', 'S1', 1),
        ])
        got = {d['store']: (d['delta'], d['sale_ids']) for d in deltas(out)}
        self.assertEqual(got, {'ST01': (-4, ['S1']), 'ST02': (3, ['S1'])})

    def test_refund_booked_to_final_biz_date(self):
        out = run_job([
            sale('S1', 3, biz_date='2026-11-11'),
            {'type': 'correction', 'sale_id': 'S1',
             'biz_date': '2026-11-12', 'ts': '2026-11-13T01:00:00'},
            refund('R1', 'S1', 1),
        ])
        got = {(d['biz_date'], d['delta']) for d in deltas(out)}
        self.assertEqual(got, {('2026-11-12', 2)})

    def test_later_correction_wins(self):
        out = run_job([
            sale('S1', 2, category='旧类'),
            {'type': 'correction', 'sale_id': 'S1', 'category': '新类',
             'ts': '2026-11-12T20:00:00'},
            {'type': 'correction', 'sale_id': 'S1', 'category': '最终类',
             'ts': '2026-11-12T23:00:00'},
        ])
        got = {(d['category'], d['delta']) for d in deltas(out)}
        self.assertEqual(got, {('最终类', 2)})

    def test_correction_without_sale_is_ignored(self):
        out = run_job([
            sale('S1', 2),
            {'type': 'correction', 'sale_id': 'S404', 'category': '新类'},
        ])
        self.assertEqual([d['delta'] for d in deltas(out)], [2])


class SnapshotTestCase(TestCase):

    def test_emit_snapshot_roundtrip(self):
        events = [sale('S1', 3), refund('R1', 'S1', 1), sale('S2', 4)]
        out = run_job(events, extra_args=['--emit-snapshot'])
        snaps = [r for r in out if r['record'] == 'snapshot']
        self.assertEqual(
            {(s['qty'], tuple(s['sale_ids'])) for s in snaps},
            {(6, ('S1', 'S2'))})
        # 快照作为下一次运行的 prev 输入后，同样的事件不再产生增减
        prev_rows = [dict(s, type='prev') for s in snaps]
        for s in prev_rows:
            del s['record']
        out2 = run_job(prev_rows + events)
        self.assertEqual(deltas(out2), [])

    def test_no_snapshot_by_default(self):
        out = run_job([sale('S1', 3)])
        self.assertEqual([r for r in out if r['record'] == 'snapshot'], [])
