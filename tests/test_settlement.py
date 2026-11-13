#!/usr/bin/env python
# -*- coding: utf-8 -*-
import os
import sys
import tempfile
import unittest

sys.path.insert(1, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                '..')))

import polib
import settlement
from settlement import (
    SINGULAR, PLURAL, COMMENT, TCOMMENT, SettledUnit, DisputedUnit, settle,
    settle_paths,
)


def make_po(entries):
    po = polib.POFile()
    po.metadata = {'Content-Type': 'text/plain; charset=utf-8'}
    for kwargs in entries:
        po.append(polib.POEntry(**kwargs))
    return po


def base_catalog():
    return make_po([
        {'msgid': 'greeting', 'msgstr': ''},
        {'msgid': 'fruit', 'msgid_plural': 'fruits',
         'msgstr_plural': {0: '', 1: '', 2: ''}},
        {'msgid': 'note', 'msgstr': 'orig',
         'comment': 'extracted', 'tcomment': 'translator'},
        {'msgid': 'same', 'msgstr': ''},
        {'msgid': 'conflict', 'msgstr': ''},
        {'msgid': 'ctx', 'msgctxt': 'menu', 'msgstr': ''},
    ])


class SettlementTests(unittest.TestCase):

    def test_unchanged_unit_is_not_billable(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 0)
        self.assertEqual(result.disputed_count, 0)

    def test_single_vendor_singular_change(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('greeting').msgstr = 'hallo'

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 1)
        unit = result.settled[0]
        self.assertEqual(unit.kind, SINGULAR)
        self.assertEqual(unit.msgid, 'greeting')
        self.assertEqual(unit.value, 'hallo')
        self.assertEqual(unit.vendors, ('A',))

    def test_identical_change_billed_once_but_credited_to_both(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('same').msgstr = 'identisch'
        po_b.find('same').msgstr = 'identisch'

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 1)
        self.assertEqual(result.disputed_count, 0)
        unit = result.settled[0]
        self.assertEqual(unit.vendors, ('A', 'B'))
        # both names stay on the change, yet it is a single billable unit
        self.assertEqual(len(result.units_for('A')), 1)
        self.assertEqual(len(result.units_for('B')), 1)

    def test_different_translations_become_dispute(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('conflict').msgstr = 'streit-a'
        po_b.find('conflict').msgstr = 'streit-b'

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 0)
        self.assertEqual(result.disputed_count, 1)
        dispute = result.disputes[0]
        self.assertIsInstance(dispute, DisputedUnit)
        self.assertEqual(dispute.kind, SINGULAR)
        self.assertEqual(dispute.values,
                         {'A': 'streit-a', 'B': 'streit-b'})

    def test_comment_and_translation_are_separate_claims(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        # vendor A only edits the translator comment
        po_a.find('note').tcomment = 'translator-A'
        # vendor B only edits the translation
        po_b.find('note').msgstr = 'note-B'

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 2)
        by_kind = {u.kind: u for u in result.settled}
        self.assertEqual(by_kind[TCOMMENT].vendors, ('A',))
        self.assertEqual(by_kind[TCOMMENT].value, 'translator-A')
        self.assertEqual(by_kind[SINGULAR].vendors, ('B',))
        self.assertEqual(by_kind[SINGULAR].value, 'note-B')

    def test_extracted_and_translator_comments_are_distinct_units(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('note').comment = 'extracted-A'
        po_b.find('note').tcomment = 'translator-B'

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 2)
        by_kind = {u.kind: u for u in result.settled}
        self.assertEqual(by_kind[COMMENT].vendors, ('A',))
        self.assertEqual(by_kind[TCOMMENT].vendors, ('B',))

    def test_divergent_edits_to_same_comment_field_are_disputed(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('note').tcomment = 'translator-A'
        po_b.find('note').tcomment = 'translator-B'

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 0)
        self.assertEqual(result.disputed_count, 1)
        dispute = result.disputes[0]
        self.assertEqual(dispute.kind, TCOMMENT)
        self.assertEqual(dispute.values,
                         {'A': 'translator-A', 'B': 'translator-B'})

    def test_plural_slots_are_settled_independently(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        # slot 0: both vendors deliver the same value
        po_a.find('fruit').msgstr_plural[0] = 'obst-a0'
        po_b.find('fruit').msgstr_plural[0] = 'obst-a0'
        # slot 1: only vendor A edits
        po_a.find('fruit').msgstr_plural[1] = 'obst-a1'
        # slot 2: vendors disagree
        po_a.find('fruit').msgstr_plural[2] = 'obst-a2'
        po_b.find('fruit').msgstr_plural[2] = 'obst-b2'

        result = settle(base, po_a, po_b, 'A', 'B')
        # slot0 shared (1) + slot1 single (1) = 2 settled, slot2 disputed
        self.assertEqual(result.settled_count, 2)
        self.assertEqual(result.disputed_count, 1)

        plural_settled = {u.plural_index: u for u in result.settled
                          if u.kind == PLURAL}
        self.assertEqual(set(plural_settled), {0, 1})
        self.assertEqual(plural_settled[0].vendors, ('A', 'B'))
        self.assertEqual(plural_settled[1].vendors, ('A',))

        dispute = result.disputes[0]
        self.assertEqual(dispute.kind, PLURAL)
        self.assertEqual(dispute.plural_index, 2)
        self.assertEqual(dispute.values,
                         {'A': 'obst-a2', 'B': 'obst-b2'})

    def test_context_distinguishes_entries(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('ctx', msgctxt='menu').msgstr = 'kontext'

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 1)
        unit = result.settled[0]
        self.assertEqual(unit.msgctxt, 'menu')
        self.assertEqual(unit.msgid, 'ctx')

    def test_header_entry_is_ignored(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.metadata['Last-Translator'] = 'Alice'
        po_b.metadata['Last-Translator'] = 'Bob'

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 0)
        self.assertEqual(result.disputed_count, 0)

    def test_missing_entry_in_a_vendor_is_treated_as_blank(self):
        base = make_po([{'msgid': 'only-here', 'msgstr': ''}])
        po_a = make_po([{'msgid': 'only-here', 'msgstr': 'gefuellt'}])
        po_b = make_po([])  # vendor B dropped the entry entirely

        result = settle(base, po_a, po_b, 'A', 'B')
        self.assertEqual(result.settled_count, 1)
        self.assertEqual(result.settled[0].vendors, ('A',))

    def test_credit_by_vendor_counts_shared_for_both(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('greeting').msgstr = 'hallo'          # only A
        po_a.find('same').msgstr = 'identisch'          # shared
        po_b.find('same').msgstr = 'identisch'          # shared
        po_b.find('note').msgstr = 'note-B'             # only B

        result = settle(base, po_a, po_b, 'A', 'B')
        credit = result.credit_by_vendor()
        self.assertEqual({u.msgid for u in credit['A']},
                         {'greeting', 'same'})
        self.assertEqual({u.msgid for u in credit['B']},
                         {'same', 'note'})
        # the shared change is paid once overall though credited to both
        self.assertEqual(result.settled_count, 3)

    def test_to_dict_round_trip_shape(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('conflict').msgstr = 'x'
        po_b.find('conflict').msgstr = 'y'

        data = settle(base, po_a, po_b, 'A', 'B').to_dict()
        self.assertEqual(data['vendors'], ['A', 'B'])
        self.assertEqual(data['settled_count'], 0)
        self.assertEqual(data['disputed_count'], 1)
        self.assertEqual(data['disputes'][0]['values'],
                         {'A': 'x', 'B': 'y'})

    def _populate_mixed_case(self, po_a, po_b):
        po_a.find('greeting').msgstr = 'hallo'           # only vendor A
        po_b.find('note').msgstr = 'note-B'              # only vendor B
        po_a.find('same').msgstr = 'identisch'           # shared, identical
        po_b.find('same').msgstr = 'identisch'
        po_a.find('conflict').msgstr = 'a-text'          # dispute
        po_b.find('conflict').msgstr = 'b-text'

    def test_swap_input_order_keeps_full_result_when_labels_follow_files(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        self._populate_mixed_case(po_a, po_b)

        # reversing the two file inputs while each label stays attached to its
        # own file must leave the whole result (not just the counts) unchanged
        forward = settle(base, po_a, po_b, 'A', 'B')
        swapped = settle(base, po_b, po_a, 'B', 'A')

        self.assertEqual(forward.settled_count, swapped.settled_count)
        self.assertEqual(forward.disputed_count, swapped.disputed_count)

        def settled_index(result):
            return {(u.kind, u.msgctxt, u.msgid, u.plural_index):
                    set(u.vendors) for u in result.settled}

        self.assertEqual(settled_index(forward), settled_index(swapped))

        fwd_disputes = {(d.kind, d.msgid, d.plural_index): d.values
                        for d in forward.disputes}
        swp_disputes = {(d.kind, d.msgid, d.plural_index): d.values
                        for d in swapped.disputes}
        self.assertEqual(fwd_disputes, swp_disputes)

    def test_swap_files_with_fixed_positional_labels_only_swaps_owners(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        self._populate_mixed_case(po_a, po_b)

        forward = settle(base, po_a, po_b, 'A', 'B')
        # same positional labels, but the underlying files are exchanged
        swapped = settle(base, po_b, po_a, 'A', 'B')

        # billable / disputed counts never move
        self.assertEqual(forward.settled_count, swapped.settled_count)
        self.assertEqual(forward.disputed_count, swapped.disputed_count)

        def settled_index(result):
            return {(u.kind, u.msgctxt, u.msgid, u.plural_index):
                    set(u.vendors) for u in result.settled}

        swap = {'A': 'B', 'B': 'A'}
        for key, vendors in settled_index(forward).items():
            expected = {swap[name] for name in vendors}
            self.assertEqual(settled_index(swapped)[key], expected)

        fwd_disputes = {(d.kind, d.msgid, d.plural_index): d.values
                        for d in forward.disputes}
        swp_disputes = {(d.kind, d.msgid, d.plural_index): d.values
                        for d in swapped.disputes}
        self.assertEqual(set(fwd_disputes), set(swp_disputes))
        for key, values in fwd_disputes.items():
            expected = {swap[name]: value for name, value in values.items()}
            self.assertEqual(swp_disputes[key], expected)


class SettlePathsTests(unittest.TestCase):

    def _write(self, po):
        handle = tempfile.NamedTemporaryFile(suffix='.po', delete=False)
        handle.close()
        po.save(handle.name)
        return handle.name

    def test_settle_paths_uses_filenames_as_vendor_labels(self):
        base = base_catalog()
        po_a = base_catalog()
        po_b = base_catalog()
        po_a.find('greeting').msgstr = 'hallo'

        paths = [self._write(base), self._write(po_a), self._write(po_b)]
        try:
            result = settle_paths(paths[0], paths[1], paths[2])
        finally:
            for path in paths:
                os.unlink(path)

        self.assertEqual(result.vendor_a, os.path.splitext(
            os.path.basename(paths[1]))[0])
        self.assertEqual(result.settled_count, 1)
        self.assertEqual(result.settled[0].vendors, (result.vendor_a,))


if __name__ == '__main__':
    unittest.main()
