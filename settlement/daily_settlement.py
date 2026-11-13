# -*- coding: utf-8 -*-
"""日结作业：输出每家店各品类销量相对于上一份已公布日报的增减行。

输入输出格式与业务规则见 settlement/README.md。

用法：
    python settlement/daily_settlement.py -r inline 输入目录/ -o 输出目录/
"""

import json

from mrjob.job import MRJob
from mrjob.protocol import JSONValueProtocol
from mrjob.step import MRStep

_SALE_KEY_PREFIX = 'S:'
_PREV_KEY_PREFIX = 'P:'
_UNVERIFIED_KEY = '__UNVERIFIED__'

_REASON_UNKNOWN_SALE = 'unknown_sale'
_REASON_OVER_REFUND = 'over_refund'


def _group_key(biz_date, store, category):
    return json.dumps([biz_date, store, category],
                      ensure_ascii=False, sort_keys=True)


class MRDailySettlement(MRJob):
    """两步作业：

    第一步按销售单号归集销售/退款/更正事件，解出每张销售单的最终归属
    （业务日、门店、品类）与核减后的净销量，并把未核退款单独列出；
    第二步按（业务日、门店、品类）聚合净销量，与上一份已公布日报比对，
    输出增减行。
    """

    OUTPUT_PROTOCOL = JSONValueProtocol

    def configure_args(self):
        super(MRDailySettlement, self).configure_args()
        self.add_passthru_arg(
            '--emit-snapshot', action='store_true',
            help='额外输出全量新快照（record=snapshot），'
                 '可作为下一次运行的“已公布日报”输入')

    def steps(self):
        return [
            MRStep(mapper=self.mapper_events,
                   reducer=self.reducer_resolve_sales),
            MRStep(mapper=self.mapper_rekey,
                   reducer=self.reducer_diff),
        ]

    # ---- 第一步：按销售单号归集事件 ----

    def mapper_events(self, _, line):
        try:
            rec = json.loads(line)
        except ValueError:
            self.increment_counter('settlement', 'bad_json_lines', 1)
            return
        rectype = rec.get('type')
        if rectype in ('sale', 'correction', 'refund'):
            sale_id = rec.get('sale_id')
            if sale_id is None:
                self.increment_counter('settlement', 'missing_sale_id', 1)
                return
            yield _SALE_KEY_PREFIX + str(sale_id), (rectype, rec)
        elif rectype == 'prev':
            key = _group_key(rec.get('biz_date'), rec.get('store'),
                             rec.get('category'))
            yield _PREV_KEY_PREFIX + key, ('prev', rec)
        else:
            self.increment_counter('settlement', 'unknown_record_type', 1)

    def reducer_resolve_sales(self, key, values):
        if key.startswith(_PREV_KEY_PREFIX):
            for _, rec in values:
                yield None, {
                    'record': 'prev_row',
                    'biz_date': rec.get('biz_date'),
                    'store': rec.get('store'),
                    'category': rec.get('category'),
                    'qty': rec.get('qty', 0),
                    'sale_ids': list(rec.get('sale_ids') or []),
                }
            return

        sale_id = key[len(_SALE_KEY_PREFIX):]
        sales, corrections, refunds = [], [], []
        for kind, rec in values:
            if kind == 'sale':
                sales.append(rec)
            elif kind == 'correction':
                corrections.append(rec)
            else:
                refunds.append(rec)

        if not sales:
            # 退款引用的销售单从未到达：整笔保持未核
            for refund in self._dedupe_refunds(refunds):
                yield None, self._unverified(refund, refund.get('qty', 0),
                                             _REASON_UNKNOWN_SALE)
            if corrections:
                self.increment_counter(
                    'settlement', 'corrections_without_sale', 1)
            return

        # 同一销售单重复传送按内容去重；内容冲突时取排序后的第一条，
        # 保证结果确定
        uniq = sorted({(str(s.get('biz_date')), str(s.get('store')),
                        str(s.get('category')), s.get('qty', 0))
                       for s in sales})
        biz_date, store, category, qty = uniq[0]

        # 更正按 (ts, 内容) 排序后依次应用，后到的覆盖先到的
        corrections.sort(key=lambda c: (
            str(c.get('ts') or ''), str(c.get('store') or ''),
            str(c.get('category') or ''), str(c.get('biz_date') or '')))
        for corr in corrections:
            if corr.get('store') is not None:
                store = str(corr['store'])
            if corr.get('category') is not None:
                category = str(corr['category'])
            if corr.get('biz_date') is not None:
                biz_date = str(corr['biz_date'])

        # 退款按退款号去重后依次核销，超过原售数量的部分保持未核；
        # 核销结果记到销售最终归属的业务日/门店/品类上
        remaining = qty
        for refund in self._dedupe_refunds(refunds):
            rqty = refund.get('qty', 0)
            verified = min(rqty, remaining)
            remaining -= verified
            if rqty - verified > 0:
                yield None, self._unverified(refund, rqty - verified,
                                             _REASON_OVER_REFUND)

        yield None, {
            'record': 'contribution',
            'biz_date': biz_date,
            'store': store,
            'category': category,
            'qty': remaining,
            'sale_id': sale_id,
        }

    # ---- 第二步：按（业务日、门店、品类）聚合并与上一份日报比对 ----

    def mapper_rekey(self, _, rec):
        kind = rec.get('record')
        if kind == 'contribution':
            yield _group_key(rec['biz_date'], rec['store'], rec['category']), \
                ('new', rec['qty'], rec['sale_id'])
        elif kind == 'prev_row':
            yield _group_key(rec['biz_date'], rec['store'], rec['category']), \
                ('prev', rec['qty'], tuple(rec.get('sale_ids') or ()))
        elif kind == 'unverified':
            yield _UNVERIFIED_KEY, ('unverified', rec)

    def reducer_diff(self, key, values):
        if key == _UNVERIFIED_KEY:
            for _, rec in values:
                yield None, rec
            return

        biz_date, store, category = json.loads(key)
        new_qty = 0
        prev_qty = 0
        new_sale_ids = set()
        prev_sale_ids = set()
        for kind, qty, ids in values:
            if kind == 'new':
                new_qty += qty
                new_sale_ids.add(ids)
            else:
                prev_qty += qty
                prev_sale_ids.update(ids)

        delta = new_qty - prev_qty
        if delta:
            # 增减行带上新旧两侧的销售单号：改类/改店时，旧值的冲减行
            # 与新值的增加行都能追到同一张销售单
            yield None, {
                'record': 'delta',
                'biz_date': biz_date,
                'store': store,
                'category': category,
                'delta': delta,
                'sale_ids': sorted(new_sale_ids | prev_sale_ids),
            }
        if self.options.emit_snapshot and new_qty:
            yield None, {
                'record': 'snapshot',
                'biz_date': biz_date,
                'store': store,
                'category': category,
                'qty': new_qty,
                'sale_ids': sorted(new_sale_ids),
            }

    @staticmethod
    def _dedupe_refunds(refunds):
        """同一笔退款（按退款号）重复传送只认一次；数量冲突时取较小值，
        保证结果确定。返回按退款号排序的列表。"""
        by_id = {}
        for refund in refunds:
            rid = str(refund.get('refund_id'))
            if (rid not in by_id or
                    refund.get('qty', 0) < by_id[rid].get('qty', 0)):
                by_id[rid] = refund
        return [by_id[rid] for rid in sorted(by_id)]

    @staticmethod
    def _unverified(refund, qty, reason):
        return {
            'record': 'unverified',
            'refund_id': str(refund.get('refund_id')),
            'sale_id': str(refund.get('sale_id')),
            'qty': qty,
            'reason': reason,
        }


if __name__ == '__main__':
    MRDailySettlement.run()
