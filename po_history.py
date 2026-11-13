# -*- coding: utf-8 -*-
"""
po_history
==========

Reconstruct the active life of translation entries from a time-ordered
sequence of gettext PO snapshots and tell genuinely new copy apart from
translations that are merely re-used.

The module answers one product question: *which translations were really
new in a given month?*

Rules implemented here:

* An entry is only "active" when it is not obsolete.  Obsolete (``#~``)
  lines stay in the file for history but do not mean the message is still
  in use.
* When one publication carries both an obsolete old meaning and an active
  new meaning for the same key, both observations are kept as evidence;
  only the active one opens a new interval.
* Consecutive snapshots where the same key keeps the exact same
  translation form one active interval.  If the translation changes while
  the key stays active, the old interval is closed and a new one opens at
  that publication date.
* A translation that comes back verbatim after the key went offline opens
  a new interval classified as ``reused``, never as new copy.
* Dates are never invented: when the snapshot that would show a
  transition is missing, the boundary is reported as the open interval
  between the two surrounding publication dates.

Dates are plain ``YYYY-MM-DD`` strings, so chronological order is plain
lexicographic order.
"""

import os
import re
from collections import namedtuple

import polib


# Classification of an interval that starts inside the reported month.
NEW = 'new'
REWRITTEN = 'rewritten'
REUSED = 'reused'

# Why an interval was no longer active at the next delivered snapshot.
DEACT_REPLACED = 'replaced'
DEACT_OBSOLETE = 'obsolete'
DEACT_ABSENT = 'absent'

DATE_RE = re.compile(r'(\d{4}-\d{2}-\d{2}|\d{8})')


def _make_key(entry):
    """Identity of an entry: context plus msgid (the gettext lookup key)."""
    return entry.msgctxt or '', entry.msgid


def _translation_signature(entry):
    """Full translation payload, so verbatim re-use is detected exactly."""
    if entry.msgstr_plural:
        plural = tuple(
            (idx, entry.msgstr_plural[idx])
            for idx in sorted(entry.msgstr_plural)
        )
    else:
        plural = ()
    return entry.msgid_plural, entry.msgstr, plural


def _is_blank_signature(signature):
    _msgid_plural, msgstr, plural = signature
    if plural:
        return not any(value for _idx, value in plural)
    return msgstr == ''


class Snapshot(namedtuple('Snapshot', 'date path po active obsolete')):
    """One delivered PO file at one publication date.

    ``active`` maps a key to its single active entry.  ``obsolete`` maps a
    key to the list of obsolete entries kept in the same file.
    """

    def observations_for(self, key):
        found = []
        entry = self.active.get(key)
        if entry is not None:
            found.append(Observation.from_entry(self.date, self.path, entry))
        for entry in self.obsolete.get(key, ()):
            found.append(Observation.from_entry(self.date, self.path, entry))
        return found


class Observation(namedtuple(
        'Observation',
        'date source obsolete fuzzy msgid msgctxt msgid_plural signature')):
    """What one snapshot says about one key, including obsolete lines."""

    @classmethod
    def from_entry(cls, date, source, entry):
        return cls(
            date=date,
            source=source,
            obsolete=entry.obsolete,
            fuzzy=entry.fuzzy,
            msgid=entry.msgid,
            msgctxt=entry.msgctxt,
            msgid_plural=entry.msgid_plural,
            signature=_translation_signature(entry),
        )

    @property
    def key(self):
        return self.msgctxt or '', self.msgid

    @property
    def is_absent(self):
        return self.signature is None


def _absent_observation(date, key):
    msgctxt, msgid = key
    return Observation(
        date=date, source=None, obsolete=False, fuzzy=False,
        msgid=msgid, msgctxt=msgctxt, msgid_plural='', signature=None,
    )


class Interval(namedtuple(
        'Interval',
        'key start last_seen end_bound deactivation signature evidence')):
    """A maximal run of publications in which one translation is active.

    ``start`` is the first publication date that carries this translation
    actively.  ``last_seen`` is the last such date.  ``end_bound`` is the
    first later publication that no longer carries it (``None`` while it is
    still active in the newest snapshot); the deactivation happened somewhere
    between ``last_seen`` and ``end_bound``, never on a fabricated date.
    """

    @property
    def is_open(self):
        return self.end_bound is None

    @property
    def msgid(self):
        return self.key[1]

    @property
    def msgctxt(self):
        return self.key[0]

    @property
    def translation_text(self):
        _msgid_plural, msgstr, plural = self.signature
        if plural:
            return ' | '.join(value for _idx, value in plural)
        return msgstr


class KeyHistory(object):
    """Everything the snapshots tell us about one key."""

    def __init__(self, key):
        self.key = key
        self.observations = []
        self.intervals = []

    # -- ingestion -------------------------------------------------------

    def add(self, observation):
        self.observations.append(observation)

    # -- interval reconstruction ----------------------------------------

    def build_intervals(self):
        """Turn the raw observations into active intervals.

        Observations of the same date are processed together: an active
        entry keeps the key alive even when an obsolete entry of the same
        key exists in the same publication.  The obsolete entry stays
        available as evidence via :meth:`observations_on`.
        """
        self.intervals = []
        current = None
        grouped = {}
        dates = []
        for obs in self.observations:
            if obs.date not in grouped:
                grouped[obs.date] = []
                dates.append(obs.date)
            grouped[obs.date].append(obs)

        for date in dates:
            group = grouped[date]
            active_obs = next(
                (obs for obs in group
                 if not obs.obsolete and not obs.is_absent),
                None,
            )
            if active_obs is None:
                # No active translation at this publication.  An obsolete
                # line is evidence of retirement; an absent marker means the
                # key simply vanished from the delivered file.
                reason = DEACT_OBSOLETE if any(
                    not obs.is_absent for obs in group) else DEACT_ABSENT
                if current is not None:
                    self.intervals.append(
                        self._materialize(current, date, reason))
                    current = None
                continue
            if current is None:
                current = self._open(active_obs)
            elif active_obs.signature != current['signature']:
                self.intervals.append(
                    self._materialize(current, date, DEACT_REPLACED))
                current = self._open(active_obs)
            else:
                current['last_seen'].append(date)
                current['evidence'].append(active_obs)
        if current is not None:
            self.intervals.append(self._materialize(current, None, None))
        return self.intervals

    def _open(self, obs):
        return {
            'signature': obs.signature,
            'start': obs.date,
            'last_seen': [obs.date],
            'evidence': [obs],
        }

    def _materialize(self, current, end_bound, deactivation):
        last_seen = current['last_seen'][-1]
        return Interval(
            key=self.key,
            start=current['start'],
            last_seen=last_seen,
            end_bound=end_bound,
            deactivation=deactivation,
            signature=current['signature'],
            evidence=list(current['evidence']),
        )

    # -- queries ---------------------------------------------------------

    def observations_on(self, date):
        """All evidence (active and obsolete) for the key at one publish."""
        return [obs for obs in self.observations if obs.date == date]

    def active_on(self, date):
        """Return the active interval covering ``date``, if any."""
        for interval in self.intervals:
            if interval.start <= date <= interval.last_seen:
                return interval
        return None

    def classify(self, interval):
        """Classify an interval against earlier evidence of the same key.

        Earlier active intervals decide the common cases.  When no active
        interval exists before it, obsolete (``#~``) lines still prove the
        key existed before: the exact same translation returning is a
        re-use, while a different translation is a rewrite.
        """
        earlier = [iv for iv in self.intervals if iv.start < interval.start]
        if earlier:
            if any(iv.signature == interval.signature for iv in earlier):
                return REUSED
            return REWRITTEN
        prior_evidence = [
            obs for obs in self.observations
            if not obs.is_absent and obs.date < interval.start
        ]
        if any(obs.signature == interval.signature for obs in prior_evidence):
            return REUSED
        if prior_evidence:
            return REWRITTEN
        return NEW


class MonthlyReport(namedtuple(
        'MonthlyReport',
        'month new rewritten reused deactivations histories')):
    """Result for one calendar month."""

    @property
    def genuinely_new(self):
        """New copy: brand-new keys plus real rewrites, no re-uses."""
        return self.new + self.rewritten


def load_snapshot(date, source):
    """Read one PO snapshot (path or PO content) into a :class:`Snapshot`."""
    po = polib.pofile(source)
    active = {}
    obsolete = {}
    for entry in po:
        if entry.msgid == '':
            # PO metadata entry, not a translatable message.
            continue
        key = _make_key(entry)
        if entry.obsolete:
            obsolete.setdefault(key, []).append(entry)
        else:
            # A well-formed PO has one active entry per key; if a broken file
            # duplicates one, the last occurrence is the effective state.
            active[key] = entry
    return Snapshot(date=date, path=source if isinstance(source, str)
                    and os.path.exists(source) else None,
                    po=po, active=active, obsolete=obsolete)


def load_snapshots(pairs):
    """Load ``(date, source)`` pairs and return them in publication order.

    A ``ValueError`` is raised for two snapshots carrying the same date,
    since the order of same-day publications cannot be decided afterwards.
    """
    snapshots = [load_snapshot(date, source) for date, source in pairs]
    dates = [snap.date for snap in snapshots]
    if len(set(dates)) != len(dates):
        duplicates = sorted({d for d in dates if dates.count(d) > 1})
        raise ValueError('duplicate snapshot date(s): %s'
                         % ', '.join(duplicates))
    snapshots.sort(key=lambda snap: snap.date)
    return snapshots


def discover_snapshots(directory):
    """Find date-stamped ``.po`` files in ``directory``.

    The publication date is taken from a ``YYYY-MM-DD`` or ``YYYYMMDD``
    substring of the file name.  Files without a date are ignored.
    """
    pairs = []
    for name in sorted(os.listdir(directory)):
        if not name.endswith('.po'):
            continue
        match = DATE_RE.search(name)
        if not match:
            continue
        date = match.group(1)
        if len(date) == 8:
            date = '%s-%s-%s' % (date[:4], date[4:6], date[6:])
        pairs.append((date, os.path.join(directory, name)))
    return load_snapshots(pairs)


def reconstruct(snapshots):
    """Build a ``{key: KeyHistory}`` map from ordered snapshots."""
    snapshots = sorted(snapshots, key=lambda snap: snap.date)
    histories = {}
    for index, snap in enumerate(snapshots):
        seen = set()
        for key, entry in snap.active.items():
            history = histories.setdefault(key, KeyHistory(key))
            history.add(Observation.from_entry(snap.date, snap.path, entry))
            seen.add(key)
        for key, entries in snap.obsolete.items():
            history = histories.setdefault(key, KeyHistory(key))
            for entry in entries:
                history.add(
                    Observation.from_entry(snap.date, snap.path, entry))
            seen.add(key)
        if index == 0:
            continue
        # Keys known from earlier publications but missing from this file:
        # record the gap explicitly instead of guessing what happened.
        for key, history in histories.items():
            if key not in seen:
                history.add(_absent_observation(snap.date, key))
    for history in histories.values():
        history.observations.sort(key=lambda obs: obs.date)
        history.build_intervals()
    return histories


def monthly_report(histories, month, require_translation=True):
    """Classify intervals that start in ``month`` (``YYYY-MM``).

    With ``require_translation`` (the default), active entries whose
    translation is blank are not counted as copy.
    """
    prefix = month + '-'
    new, rewritten, reused, deactivations = [], [], [], []
    for history in histories.values():
        for interval in history.intervals:
            if require_translation and _is_blank_signature(interval.signature):
                continue
            if interval.start.startswith(prefix):
                status = history.classify(interval)
                if status == NEW:
                    new.append(interval)
                elif status == REWRITTEN:
                    rewritten.append(interval)
                else:
                    reused.append(interval)
            if interval.end_bound is not None \
                    and interval.end_bound.startswith(prefix):
                deactivations.append((interval, history))
    order = lambda iv: (iv.start, iv.msgctxt, iv.msgid)
    new.sort(key=order)
    rewritten.sort(key=order)
    reused.sort(key=order)
    deactivations.sort(key=lambda item: order(item[0]))
    return MonthlyReport(
        month=month, new=new, rewritten=rewritten, reused=reused,
        deactivations=deactivations, histories=histories,
    )


def analyze_month(pairs, month, require_translation=True):
    """One-call convenience: snapshots in, :class:`MonthlyReport` out."""
    return monthly_report(
        reconstruct(load_snapshots(pairs)), month,
        require_translation=require_translation,
    )


def _preview(text, width=60):
    text = text.replace('\n', '\\n')
    return text if len(text) <= width else text[:width - 1] + '...'


def render_report(report):
    """Plain-text rendering of a :class:`MonthlyReport`."""
    lines = ['Monthly copy report for %s' % report.month]
    buckets = (
        (NEW, report.new),
        (REWRITTEN, report.rewritten),
        (REUSED, report.reused),
    )
    for label, intervals in buckets:
        lines.append('')
        lines.append('%s (%d)' % (label, len(intervals)))
        for interval in intervals:
            history = report.histories[interval.key]
            status = history.classify(interval)
            qualifier = ''
            if status == REUSED:
                previous = [iv for iv in history.intervals
                            if iv.signature == interval.signature
                            and iv.start < interval.start]
                if previous:
                    earlier = previous[-1]
                    qualifier = (' [same translation previously active %s..%s]'
                                 % (earlier.start, earlier.last_seen))
            ctx = ('%s\x04' % interval.msgctxt) if interval.msgctxt else ''
            lines.append('  - %s%s' % (ctx, interval.msgid))
            lines.append('    translation: %s%s'
                         % (_preview(interval.translation_text), qualifier))
            lines.append('    active from %s' % interval.start)
            dual = [obs for obs in history.observations_on(interval.start)
                    if obs.obsolete]
            for obs in dual:
                lines.append('    evidence: obsolete old meaning kept in the '
                             'same publication (%s)'
                             % (obs.source or '<string content>'))
    lines.append('')
    lines.append('deactivations (%d)' % len(report.deactivations))
    for interval, history in report.deactivations:
        if interval.deactivation == DEACT_REPLACED:
            note = 'replaced by a new active translation'
        elif interval.deactivation == DEACT_OBSOLETE:
            note = 'only an obsolete (#~) entry remains'
        else:
            note = 'key absent from the delivered file'
        lines.append('  - %s: active until %s, gone by %s (%s); the change '
                     'happened between those two publications'
                     % (interval.msgid, interval.last_seen,
                        interval.end_bound, note))
    return '\n'.join(lines)


def main(argv=None):
    """Command line: ``python po_history.py YYYY-MM date=file.po ...``."""
    import argparse

    parser = argparse.ArgumentParser(
        description='Reconstruct active intervals of PO keys from dated '
                    'snapshots and report genuinely new copy for a month.')
    parser.add_argument('month', help='reporting month as YYYY-MM')
    parser.add_argument('snapshots', nargs='+',
                        help='snapshots as date=path-to-po, e.g. '
                             '2026-11-02=snapshots/week1.po')
    parser.add_argument('--include-untranslated', action='store_true',
                        help='count active entries whose msgstr is blank')
    args = parser.parse_args(argv)

    pairs = []
    for item in args.snapshots:
        if '=' not in item:
            parser.error('snapshot must be date=path, got %r' % item)
        date, path = item.split('=', 1)
        pairs.append((date, path))
    report = analyze_month(
        pairs, args.month,
        require_translation=not args.include_untranslated,
    )
    print(render_report(report))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
