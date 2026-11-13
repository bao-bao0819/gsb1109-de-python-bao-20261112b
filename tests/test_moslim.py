#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Tests for the byte-bounded slim MO builder (moslim)."""

import os
import sys
import tempfile
import unittest

sys.path.insert(1, os.path.abspath(os.path.join(os.path.dirname(__file__),
                                                '..')))

import polib

import moslim


HEADER = u'''msgid ""
msgstr ""
"Project-Id-Version: test\\n"
"Content-Type: text/plain; charset=UTF-8\\n"
"Plural-Forms: nplurals=2; plural=(n != 1);\\n"
'''


def po_from(source):
    return polib.pofile(HEADER + source)


SAMPLE_PO = u'''
msgid "alpha"
msgstr "ALPHA"

msgid "beta"
msgstr "BETA"

msgid "gamma"
msgid_plural "gammas"
msgstr[0] "GAMMA-0"
msgstr[1] "GAMMA-1"

msgctxt "menu"
msgid "alpha"
msgstr "CTX-ALPHA"

#, fuzzy
msgid "fuzzy-one"
msgstr "FUZZY"

msgid "untranslated-one"
msgstr ""

# long entry used to exercise the "skip oversize, keep going" path
msgid "long"
msgstr "XXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX"
'''


class MoslimTests(unittest.TestCase):

    def setUp(self):
        self.po = po_from(SAMPLE_PO)
        self.compilable = moslim._compilable_entries(self.po)
        # Five compilable entries: alpha, beta, gamma (plural),
        # contextual alpha, long.  Fuzzy and untranslated are excluded.
        self.assertEqual(len(self.compilable), 5)

    def test_size_accounting_matches_real_binary(self):
        for subset in (self.compilable[:1], self.compilable[:3],
                       self.compilable, []):
            counted = moslim.compile_size(self.po, subset)
            actual = len(moslim._clone_pofile(self.po, subset).to_binary())
            self.assertEqual(counted, actual)

    def test_full_size_report_matches_full_binary(self):
        _, report = moslim.build_slim_catalog(
            self.po, max_bytes=10 ** 9
        )
        self.assertEqual(
            report['full_size'],
            len(self.po.to_binary())
        )

    def test_usage_drives_selection(self):
        usage = {'beta': 50, 'alpha': 10}
        # Budget room for the metadata entry plus exactly two entries.
        two_entry_budget = (
            moslim.compile_size(self.po, [])
            + moslim._entry_delta_bytes(self.po.find('beta'), 'utf-8')
            + moslim._entry_delta_bytes(self.po.find('alpha'), 'utf-8')
        )
        entries, report = moslim.build_slim_catalog(
            self.po, two_entry_budget, usage=usage
        )
        keys = [moslim._entry_key(e) for e in entries]
        self.assertEqual(keys, ['beta', 'alpha'])
        self.assertEqual(report['selected_count'], 2)
        self.assertLessEqual(report['selected_size'], two_entry_budget)

    def test_ties_are_deterministic(self):
        entries_a, _ = moslim.build_slim_catalog(
            self.po, max_bytes=10 ** 9, usage={}
        )
        entries_b, _ = moslim.build_slim_catalog(
            self.po, max_bytes=10 ** 9, usage={}
        )
        self.assertEqual(
            [moslim._entry_key(e) for e in entries_a],
            [moslim._entry_key(e) for e in entries_b]
        )

    def test_required_closure_beats_usage(self):
        # "long" has zero usage and is the biggest entry; requiring it must
        # keep it regardless.
        entries, report = moslim.build_slim_catalog(
            self.po, max_bytes=10 ** 9,
            required_keys=['long'], usage={'alpha': 999}
        )
        keys = [moslim._entry_key(e) for e in entries]
        self.assertIn('long', keys)
        self.assertEqual(keys[0], 'long')
        self.assertEqual(report['required_keys'], ['long'])

    def test_contextual_required_key(self):
        contextual = 'menu\x04alpha'
        entries, _ = moslim.build_slim_catalog(
            self.po, max_bytes=10 ** 9, required_keys=[contextual]
        )
        keys = [moslim._entry_key(e) for e in entries]
        self.assertIn(contextual, keys)
        # The plain "alpha" must not be confused with the contextual one.
        self.assertIn('alpha', keys)
        self.assertEqual(len(set(keys)), len(keys))

    def test_missing_required_key_raises(self):
        with self.assertRaises(moslim.RequiredEntryNotFound):
            moslim.build_slim_catalog(
                self.po, max_bytes=10 ** 9, required_keys=['nope']
            )

    def test_duplicate_required_keys_are_deduped(self):
        entries, report = moslim.build_slim_catalog(
            self.po, max_bytes=10 ** 9,
            required_keys=['alpha', 'alpha', 'beta']
        )
        keys = [moslim._entry_key(e) for e in entries]
        self.assertEqual(keys.count('alpha'), 1)
        self.assertEqual(report['required_keys'], ['alpha', 'beta'])

    def test_plural_entry_is_atomic(self):
        entries, _ = moslim.build_slim_catalog(
            self.po, max_bytes=10 ** 9, required_keys=['gamma']
        )
        plural = [e for e in entries if e.msgid == 'gamma'][0]
        self.assertEqual(
            sorted(plural.msgstr_plural), [0, 1]
        )
        # And it survives a full binary round-trip with both branches.
        with tempfile.NamedTemporaryFile(suffix='.mo', delete=False) as fh:
            mo_path = fh.name
        try:
            slim = moslim._clone_pofile(self.po, entries)
            slim.save_as_mofile(mo_path)
            mo = polib.mofile(mo_path)
            loaded = mo.find('gamma')
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded.msgstr_plural[0], 'GAMMA-0')
            self.assertEqual(loaded.msgstr_plural[1], 'GAMMA-1')
        finally:
            os.unlink(mo_path)

    def test_greedy_skips_oversize_and_keeps_going(self):
        # Budget that admits small entries but never the "long" one.
        budget = moslim.compile_size(self.po, [
            self.po.find('alpha'), self.po.find('beta')
        ])
        entries, report = moslim.build_slim_catalog(
            self.po, max_bytes=budget, usage={'long': 999}
        )
        keys = [moslim._entry_key(e) for e in entries]
        self.assertNotIn('long', keys)
        self.assertIn('alpha', keys)
        self.assertIn('beta', keys)
        self.assertLessEqual(report['selected_size'], budget)

    def test_required_closure_overflow_refuses(self):
        long_entry = self.po.find('long')
        closure_size = moslim._actual_binary_size(self.po, [long_entry])
        entries, report = moslim.build_slim_catalog(
            self.po, max_bytes=closure_size - 1, required_keys=['long']
        )
        self.assertIsNone(entries)
        self.assertFalse(report['fits'])
        self.assertEqual(report['required_size'], closure_size)
        self.assertGreater(report['required_size'], report['limit'])

    def test_write_slim_mo_loadable_and_within_budget(self):
        with tempfile.NamedTemporaryFile(suffix='.mo', delete=False) as fh:
            mo_path = fh.name
        # The in-memory PO has no backing file, so exercise the writer via
        # a real serialized PO.
        with tempfile.NamedTemporaryFile(suffix='.po', delete=False) as fh:
            po_path = fh.name
            fh.write((HEADER + SAMPLE_PO).encode('utf-8'))
        try:
            budget = moslim.compile_size(self.po, [
                self.po.find('alpha'), self.po.find('gamma')
            ])
            report = moslim.write_slim_mo(
                po_path, mo_path, budget,
                required_keys=['gamma'], usage={'alpha': 5}
            )
            on_disk = os.path.getsize(mo_path)
            self.assertEqual(on_disk, report['selected_size'])
            self.assertLessEqual(on_disk, budget)
            mo = polib.mofile(mo_path)
            self.assertEqual(mo.find('alpha').msgstr, 'ALPHA')
            self.assertEqual(mo.find('gamma').msgstr_plural[1], 'GAMMA-1')
            self.assertIsNone(mo.find('beta'))
        finally:
            os.unlink(po_path)
            if os.path.exists(mo_path):
                os.unlink(mo_path)

    def test_write_slim_mo_raises_when_closure_overflows(self):
        long_entry = self.po.find('long')
        closure_size = moslim._actual_binary_size(self.po, [long_entry])
        with tempfile.NamedTemporaryFile(suffix='.po', delete=False) as fh:
            po_path = fh.name
            fh.write((HEADER + SAMPLE_PO).encode('utf-8'))
        mo_path = po_path + '.mo'
        try:
            with self.assertRaises(moslim.BudgetExceeded) as ctx:
                moslim.write_slim_mo(
                    po_path, mo_path, closure_size - 1,
                    required_keys=['long']
                )
            self.assertEqual(ctx.exception.report['required_size'],
                             closure_size)
            self.assertFalse(os.path.exists(mo_path))
        finally:
            os.unlink(po_path)

    def test_cli_success_and_refusal(self):
        with tempfile.NamedTemporaryFile(suffix='.po', delete=False) as fh:
            po_path = fh.name
            fh.write((HEADER + SAMPLE_PO).encode('utf-8'))
        mo_path = po_path + '.mo'
        try:
            rc = moslim.main([
                po_path, mo_path, '-b', '1000000',
                '-r', 'gamma', '-u', os.devnull
            ])
            self.assertEqual(rc, 0)
            self.assertTrue(os.path.exists(mo_path))
            os.unlink(mo_path)

            long_entry = self.po.find('long')
            closure_size = moslim._actual_binary_size(
                self.po, [long_entry]
            )
            rc = moslim.main([
                po_path, mo_path, '-b', str(closure_size - 1),
                '-r', 'long'
            ])
            self.assertEqual(rc, 2)
            self.assertFalse(os.path.exists(mo_path))
        finally:
            os.unlink(po_path)

    def test_usage_file_parsing(self):
        with tempfile.NamedTemporaryFile(
            mode='w', suffix='.txt', delete=False
        ) as fh:
            fh.write('# comment\n\nalpha = 7\nmenu\\x04alpha=3\nbare\n')
            usage_path = fh.name
        try:
            usage = moslim.load_usage(usage_path)
            self.assertEqual(usage['alpha'], 7)
            self.assertEqual(usage['menu\x04alpha'], 3)
            self.assertEqual(usage['bare'], 1)
        finally:
            os.unlink(usage_path)

    def test_real_catalog_plural_and_context(self):
        # Sanity check on the shipped fixture, which carries plurals and
        # msgctxt entries.
        po = polib.pofile(os.path.join(
            os.path.dirname(__file__), 'test_utf8.po'
        ))
        entries, report = moslim.build_slim_catalog(
            po, max_bytes=2000,
            required_keys=['year'],
            usage={'and': 100, 'or': 90}
        )
        self.assertIsNotNone(entries)
        keys = [moslim._entry_key(e) for e in entries]
        self.assertIn('year', keys)
        year = [e for e in entries if e.msgid == 'year'][0]
        self.assertEqual(sorted(year.msgstr_plural), [0, 1])
        self.assertLessEqual(report['selected_size'], 2000)


if __name__ == '__main__':
    unittest.main()
