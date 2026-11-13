# -*- coding: utf-8 -*-
"""Tests for po_history: active-interval reconstruction and monthly copy
classification from dated PO snapshots."""

import os
import sys
import unittest

sys.path.insert(1, os.path.abspath('.'))

import po_history
from po_history import (
    DEACT_ABSENT,
    DEACT_OBSOLETE,
    DEACT_REPLACED,
    NEW,
    REUSED,
    REWRITTEN,
    analyze_month,
    discover_snapshots,
    load_snapshots,
    reconstruct,
    render_report,
)

HEADER = (
    'msgid ""\n'
    'msgstr ""\n'
    '"Content-Type: text/plain; charset=UTF-8\\n"\n\n'
)


def q(text):
    return '"%s"' % text


def build_po(items):
    """Build PO content from dicts: msgid/msgstr/obsolete/msgctxt/plural."""
    lines = [HEADER]
    for item in items:
        prefix = '#~ ' if item.get('obsolete') else ''
        if item.get('msgctxt') is not None:
            lines.append(prefix + 'msgctxt ' + q(item['msgctxt']))
        lines.append(prefix + 'msgid ' + q(item['msgid']))
        if item.get('plural'):
            lines.append(prefix + 'msgid_plural ' + q(item['msgid'] + 's'))
            for index, value in enumerate(item['plural']):
                lines.append(prefix + 'msgstr[%d] ' % index + q(value))
        else:
            lines.append(prefix + 'msgstr ' + q(item.get('msgstr', '')))
        lines.append('')
    return '\n'.join(lines)


def entry(msgid, msgstr, **kwargs):
    kwargs.update(msgid=msgid, msgstr=msgstr)
    return kwargs


class ReconstructionTests(unittest.TestCase):

    def test_brand_new_key_is_new(self):
        pairs = [
            ('2026-10-26', build_po([entry('old', 'alt')])),
            ('2026-11-02', build_po([
                entry('old', 'alt'),
                entry('greeting', 'Hello'),
            ])),
        ]
        report = analyze_month(pairs, '2026-11')
        self.assertEqual([iv.msgid for iv in report.new], ['greeting'])
        self.assertEqual(report.rewritten, [])
        self.assertEqual(report.reused, [])
        self.assertEqual(len(report.genuinely_new), 1)

    def test_consecutive_identical_snapshots_form_one_interval(self):
        pairs = [
            ('2026-11-02', build_po([entry('greeting', 'Hello')])),
            ('2026-11-09', build_po([entry('greeting', 'Hello')])),
            ('2026-11-16', build_po([entry('greeting', 'Hello')])),
        ]
        histories = reconstruct(load_snapshots(pairs))
        history = histories[('', 'greeting')]
        self.assertEqual(len(history.intervals), 1)
        interval = history.intervals[0]
        self.assertEqual(interval.start, '2026-11-02')
        self.assertEqual(interval.last_seen, '2026-11-16')
        self.assertTrue(interval.is_open)

    def test_obsolete_line_does_not_keep_key_active(self):
        pairs = [
            ('2026-11-02', build_po([entry('greeting', 'Hello')])),
            ('2026-11-09', build_po([
                entry('greeting', 'Hello', obsolete=True),
            ])),
        ]
        histories = reconstruct(load_snapshots(pairs))
        history = histories[('', 'greeting')]
        self.assertEqual(len(history.intervals), 1)
        interval = history.intervals[0]
        self.assertFalse(interval.is_open)
        self.assertEqual(interval.last_seen, '2026-11-02')
        self.assertEqual(interval.end_bound, '2026-11-09')
        self.assertEqual(interval.deactivation, DEACT_OBSOLETE)
        # The retired line is still retained as an observation.
        self.assertTrue(
            all(obs.obsolete for obs in history.observations_on('2026-11-09'))
        )

    def test_verbatim_return_after_retirement_is_reused_not_new(self):
        pairs = [
            ('2026-10-19', build_po([entry('greeting', 'Hello')])),
            ('2026-10-26', build_po([
                entry('greeting', 'Hello', obsolete=True),
            ])),
            ('2026-11-02', build_po([
                entry('greeting', 'Hello', obsolete=True),
            ])),
            ('2026-11-09', build_po([entry('greeting', 'Hello')])),
        ]
        report = analyze_month(pairs, '2026-11')
        self.assertEqual(report.new, [])
        self.assertEqual(report.rewritten, [])
        self.assertEqual([iv.msgid for iv in report.reused], ['greeting'])
        # The product count of really new copy must exclude it.
        self.assertEqual(report.genuinely_new, [])

    def test_rewritten_translation_is_separate_interval_and_rewritten(self):
        pairs = [
            ('2026-10-26', build_po([entry('greeting', 'Hello')])),
            ('2026-11-02', build_po([entry('greeting', 'Hi there')])),
        ]
        histories = reconstruct(load_snapshots(pairs))
        history = histories[('', 'greeting')]
        self.assertEqual(len(history.intervals), 2)
        old, new = history.intervals
        self.assertEqual(old.deactivation, DEACT_REPLACED)
        self.assertEqual(old.end_bound, '2026-11-02')
        self.assertEqual(new.start, '2026-11-02')
        self.assertEqual(history.classify(new), REWRITTEN)

        report = analyze_month(pairs, '2026-11')
        self.assertEqual([iv.msgid for iv in report.rewritten], ['greeting'])
        self.assertEqual([iv.msgid for iv in report.genuinely_new],
                         ['greeting'])

    def test_original_wording_returning_later_is_reused(self):
        pairs = [
            ('2026-10-05', build_po([entry('greeting', 'Hello')])),
            ('2026-10-19', build_po([entry('greeting', 'Hi')])),
            ('2026-11-09', build_po([entry('greeting', 'Hello')])),
        ]
        report = analyze_month(pairs, '2026-11')
        self.assertEqual([iv.msgid for iv in report.reused], ['greeting'])
        self.assertEqual(report.new, [])
        self.assertEqual(report.rewritten, [])

    def test_active_and_obsolete_same_key_same_publication_keeps_both(self):
        dual_po = build_po([
            entry('greeting', 'Old meaning', obsolete=True),
            entry('greeting', 'Brand new meaning'),
        ])
        pairs = [
            ('2026-10-26', build_po([entry('greeting', 'Old meaning')])),
            ('2026-11-02', dual_po),
        ]
        histories = reconstruct(load_snapshots(pairs))
        history = histories[('', 'greeting')]

        evidence = history.observations_on('2026-11-02')
        self.assertEqual(len(evidence), 2)
        self.assertEqual(
            sorted(obs.obsolete for obs in evidence), [False, True])

        self.assertEqual(len(history.intervals), 2)
        old, new = history.intervals
        self.assertEqual(old.deactivation, DEACT_REPLACED)
        self.assertEqual(new.start, '2026-11-02')
        self.assertEqual(history.classify(new), REWRITTEN)

        report = analyze_month(pairs, '2026-11')
        text = render_report(report)
        self.assertIn('obsolete old meaning', text)

    def test_missing_snapshot_gives_bounded_not_exact_deactivation(self):
        # Alive Monday 2026-11-02; the next delivered file is Monday
        # 2026-11-16, in which the key is simply gone.
        pairs = [
            ('2026-11-02', build_po([entry('greeting', 'Hello')])),
            ('2026-11-16', build_po([entry('other', 'elsewhere')])),
        ]
        histories = reconstruct(load_snapshots(pairs))
        history = histories[('', 'greeting')]
        interval = history.intervals[0]
        self.assertEqual(interval.last_seen, '2026-11-02')
        self.assertEqual(interval.end_bound, '2026-11-16')
        self.assertEqual(interval.deactivation, DEACT_ABSENT)

        absent = [obs for obs in history.observations if obs.is_absent]
        self.assertEqual([obs.date for obs in absent], ['2026-11-16'])

        report = analyze_month(pairs, '2026-11')
        self.assertEqual(len(report.deactivations), 1)
        text = render_report(report)
        self.assertIn('between those two publications', text)

    def test_interval_starting_before_month_not_counted(self):
        pairs = [
            ('2026-10-26', build_po([entry('greeting', 'Hello')])),
            ('2026-11-30', build_po([entry('greeting', 'Hello')])),
        ]
        report = analyze_month(pairs, '2026-11')
        self.assertEqual(report.new, [])
        self.assertEqual(report.rewritten, [])
        self.assertEqual(report.reused, [])

    def test_blank_msgstr_excluded_then_included(self):
        pairs = [
            ('2026-11-02', build_po([entry('empty', '')])),
        ]
        self.assertEqual(analyze_month(pairs, '2026-11').new, [])
        report = analyze_month(
            pairs, '2026-11', require_translation=False)
        self.assertEqual([iv.msgid for iv in report.new], ['empty'])

    def test_msgctxt_makes_distinct_keys(self):
        pairs = [
            ('2026-11-02', build_po([
                entry('read', 'Lesen', msgctxt='action'),
                entry('read', 'Gelesen', msgctxt='state'),
            ])),
        ]
        report = analyze_month(pairs, '2026-11')
        keys = sorted((iv.msgctxt, iv.msgid) for iv in report.new)
        self.assertEqual(keys, [('action', 'read'), ('state', 'read')])

    def test_plural_forms_part_of_signature(self):
        plural_po = build_po([
            entry('file', '', plural=['Datei', 'Dateien']),
        ])
        changed_po = build_po([
            entry('file', '', plural=['Datei', 'Datei-Dateien']),
        ])
        same_po = build_po([
            entry('file', '', plural=['Datei', 'Dateien']),
        ])
        pairs = [
            ('2026-10-26', plural_po),
            ('2026-11-02', changed_po),
            ('2026-11-09', same_po),
        ]
        report = analyze_month(pairs, '2026-11')
        self.assertEqual([iv.msgid for iv in report.rewritten], ['file'])
        self.assertEqual([iv.msgid for iv in report.reused], ['file'])

    def test_fuzzy_entry_is_still_an_active_observation(self):
        fuzzy_po = HEADER + (
            '#, fuzzy\nmsgid "maybe"\nmsgstr "vielleicht"\n'
        )
        snapshots = load_snapshots([('2026-11-02', fuzzy_po)])
        history = reconstruct(snapshots)[('', 'maybe')]
        self.assertEqual(len(history.intervals), 1)
        self.assertTrue(history.observations[0].fuzzy)

    def test_obsolete_only_history_verbatim_return_is_reused(self):
        pairs = [
            ('2026-10-26', build_po([
                entry('greeting', 'Hello', obsolete=True),
            ])),
            ('2026-11-02', build_po([entry('greeting', 'Hello')])),
        ]
        report = analyze_month(pairs, '2026-11')
        self.assertEqual([iv.msgid for iv in report.reused], ['greeting'])
        self.assertEqual(report.new, [])
        self.assertEqual(report.rewritten, [])

    def test_obsolete_only_history_changed_wording_is_rewritten(self):
        pairs = [
            ('2026-10-26', build_po([
                entry('greeting', 'Hello', obsolete=True),
            ])),
            ('2026-11-02', build_po([entry('greeting', 'Hi there')])),
        ]
        report = analyze_month(pairs, '2026-11')
        self.assertEqual([iv.msgid for iv in report.rewritten], ['greeting'])
        self.assertEqual(report.new, [])
        self.assertEqual(report.reused, [])


class SnapshotLoadingTests(unittest.TestCase):

    def test_duplicate_date_rejected(self):
        pairs = [
            ('2026-11-02', build_po([entry('a', 'x')])),
            ('2026-11-02', build_po([entry('b', 'y')])),
        ]
        with self.assertRaises(ValueError):
            load_snapshots(pairs)

    def test_snapshots_sorted_by_date_regardless_of_input_order(self):
        pairs = [
            ('2026-11-16', build_po([entry('a', 'x')])),
            ('2026-11-02', build_po([entry('a', 'x')])),
        ]
        snapshots = load_snapshots(pairs)
        self.assertEqual([s.date for s in snapshots],
                         ['2026-11-02', '2026-11-16'])

    def test_discover_snapshots_from_directory(self):
        import tempfile

        tmp = tempfile.mkdtemp()
        for name, content in (
                ('app-2026-11-02.po', build_po([entry('a', 'x')])),
                ('app-20261109.po', build_po([entry('a', 'x')])),
                ('notes.txt', 'ignore me'),
        ):
            with open(os.path.join(tmp, name), 'w', encoding='utf-8') as fh:
                fh.write(content)
        snapshots = discover_snapshots(tmp)
        self.assertEqual([s.date for s in snapshots],
                         ['2026-11-02', '2026-11-09'])
        self.assertTrue(all(os.path.exists(s.path) for s in snapshots))

    def test_render_report_contains_all_buckets(self):
        pairs = [
            ('2026-10-19', build_po([
                entry('comeback', 'back'),
                entry('stable', 'same'),
            ])),
            ('2026-10-26', build_po([
                entry('comeback', 'back', obsolete=True),
                entry('stable', 'same'),
            ])),
            ('2026-11-02', build_po([
                entry('comeback', 'back'),
                entry('stable', 'changed'),
                entry('fresh', 'brand new'),
            ])),
        ]
        text = render_report(analyze_month(pairs, '2026-11'))
        self.assertIn('new (1)', text)
        self.assertIn('rewritten (1)', text)
        self.assertIn('reused (1)', text)
        self.assertIn('fresh', text)
        self.assertIn('stable', text)
        self.assertIn('comeback', text)

    def test_cli_main(self):
        import tempfile

        tmp = tempfile.mkdtemp()
        path = os.path.join(tmp, 'snap-2026-11-02.po')
        with open(path, 'w', encoding='utf-8') as fh:
            fh.write(build_po([entry('cli', 'kommandozeile')]))
        rc = po_history.main(['2026-11', '2026-11-02=' + path])
        self.assertEqual(rc, 0)


if __name__ == '__main__':
    unittest.main()
