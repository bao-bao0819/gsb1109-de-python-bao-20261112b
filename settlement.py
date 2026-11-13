# -*- coding: utf-8 -*-
"""
Settle the work of two translation vendors that started from the same PO
catalog (the "common draft") and each delivered the whole catalog back.

Billing must not be based on how many entries a delivered file happens to
contain. Instead every billable unit is taken from the common draft and is
attributed by comparing what each vendor actually changed relative to that
draft:

* a translation unit is either the singular ``msgstr`` of an entry or one of
  its plural slots ``msgstr_plural[i]``;
* a comment unit is one comment field of an entry, namely the extracted
  comment (``#.`` / :attr:`polib.POEntry.comment`) or the translator comment
  (``#`` / :attr:`polib.POEntry.tcomment`).

Rules implemented here:

* a unit that neither vendor changed relative to the draft is not billable;
* a unit changed by only one vendor is settled on that vendor;
* a unit changed by both vendors to the **same** value is settled once but
  credited to both vendors (the change is paid a single time, both names stay
  on it);
* a translation slot that the two vendors translated **differently** becomes a
  pending arbitration item and is not settled; the same holds when both vendors
  rewrite the very same comment field to different text;
* a comment edit and a translation edit never collide: they are different
  units, so when one vendor only touches a comment while the other touches the
  translation, both can claim their own unit;
* the header entry (the empty-``msgid`` metadata entry) is not a translatable
  term and is ignored.

Because the set of examined units is taken solely from the common draft,
swapping the two vendor files never changes the settled or disputed counts; it
only swaps the vendor names attached to the results.
"""

import os

import polib

#: Unit kind for an entry singular ``msgstr``.
SINGULAR = 'singular'
#: Unit kind for an entry ``msgstr_plural[i]`` slot.
PLURAL = 'plural'
#: Unit kind for an extracted comment (``#.``).
COMMENT = 'comment'
#: Unit kind for a translator comment (``#``).
TCOMMENT = 'tcomment'

TRANSLATION_KINDS = (SINGULAR, PLURAL)
COMMENT_KINDS = (COMMENT, TCOMMENT)


class SettledUnit(object):
    """A billable unit that can be settled without arbitration."""

    def __init__(self, kind, msgid, value, vendors, base='', msgctxt=None,
                 plural_index=None):
        self.kind = kind
        self.msgid = msgid
        self.msgctxt = msgctxt
        self.plural_index = plural_index
        self.base = base
        self.value = value
        #: tuple of vendor names that delivered this identical change.
        self.vendors = tuple(vendors)

    def __repr__(self):
        return ('SettledUnit(kind=%r, msgid=%r, plural_index=%r, vendors=%r)'
                % (self.kind, self.msgid, self.plural_index, self.vendors))

    def __eq__(self, other):
        if not isinstance(other, SettledUnit):
            return NotImplemented
        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.kind, self.msgctxt, self.msgid, self.plural_index))


class DisputedUnit(object):
    """A translation slot the two vendors filled with different content."""

    def __init__(self, kind, msgid, values, base='', msgctxt=None,
                 plural_index=None):
        self.kind = kind
        self.msgid = msgid
        self.msgctxt = msgctxt
        self.plural_index = plural_index
        self.base = base
        #: mapping ``vendor name -> value`` for the conflicting edits.
        self.values = dict(values)

    def __repr__(self):
        return ('DisputedUnit(kind=%r, msgid=%r, plural_index=%r, values=%r)'
                % (self.kind, self.msgid, self.plural_index, self.values))

    def __eq__(self, other):
        if not isinstance(other, DisputedUnit):
            return NotImplemented
        return self.__dict__ == other.__dict__

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.kind, self.msgctxt, self.msgid, self.plural_index))


class SettlementResult(object):
    """Outcome of comparing two vendor deliveries against a common draft."""

    def __init__(self, vendor_a, vendor_b, settled=None, disputes=None):
        self.vendor_a = vendor_a
        self.vendor_b = vendor_b
        self.settled = list(settled or [])
        self.disputes = list(disputes or [])

    @property
    def settled_count(self):
        """Number of units that are billable exactly once."""
        return len(self.settled)

    @property
    def disputed_count(self):
        """Number of slots awaiting arbitration (not billable yet)."""
        return len(self.disputes)

    def credit_by_vendor(self):
        """Return ``{vendor name: [settled units]}`` keyed by vendor."""
        credit = {self.vendor_a: [], self.vendor_b: []}
        for unit in self.settled:
            for vendor in unit.vendors:
                credit.setdefault(vendor, []).append(unit)
        return credit

    def units_for(self, vendor):
        """Settled units on which ``vendor`` has authorship."""
        return [unit for unit in self.settled if vendor in unit.vendors]

    def to_dict(self):
        """Plain-data view, handy for reports or serialization."""
        return {
            'vendors': [self.vendor_a, self.vendor_b],
            'settled_count': self.settled_count,
            'disputed_count': self.disputed_count,
            'settled': [
                {
                    'kind': u.kind,
                    'msgid': u.msgid,
                    'msgctxt': u.msgctxt,
                    'plural_index': u.plural_index,
                    'base': u.base,
                    'value': u.value,
                    'vendors': list(u.vendors),
                } for u in self.settled
            ],
            'disputes': [
                {
                    'kind': d.kind,
                    'msgid': d.msgid,
                    'msgctxt': d.msgctxt,
                    'plural_index': d.plural_index,
                    'base': d.base,
                    'values': d.values,
                } for d in self.disputes
            ],
        }


def _entry_key(entry):
    return entry.msgctxt or '', entry.msgid


def _index_entries(po):
    indexed = {}
    for entry in po:
        if entry.msgid == '':
            # header / metadata entry, not a translatable term
            continue
        indexed.setdefault(_entry_key(entry), entry)
    return indexed


def _translation_slots(entry):
    """Yield ``(kind, plural_index, value)`` for an entry's translation."""
    if entry is not None and entry.msgstr_plural:
        for index in sorted(entry.msgstr_plural, key=lambda i: int(i)):
            yield PLURAL, int(index), entry.msgstr_plural[index]
    else:
        yield SINGULAR, None, getattr(entry, 'msgstr', '') if entry else ''


def _slot_value(entry, kind, plural_index):
    if entry is None:
        return ''
    if kind == PLURAL:
        return entry.msgstr_plural.get(plural_index, '')
    return entry.msgstr


def _comment_fields():
    return ((COMMENT, 'comment'), (TCOMMENT, 'tcomment'))


def settle(base_po, vendor_a_po, vendor_b_po, vendor_a='A', vendor_b='B'):
    """
    Compare two vendor deliveries against the common draft ``base_po``.

    The first three arguments are :class:`polib.POFile` instances. Vendor names
    are only used as labels. Returns a :class:`SettlementResult`.
    """
    entries_a = _index_entries(vendor_a_po)
    entries_b = _index_entries(vendor_b_po)

    settled = []
    disputes = []

    # Only the common draft defines the units under examination, so the result
    # is independent of the order in which the two vendors are passed.
    for base_entry in base_po:
        if base_entry.msgid == '':
            continue

        key = _entry_key(base_entry)
        msgctxt, msgid = key
        entry_a = entries_a.get(key)
        entry_b = entries_b.get(key)

        # Translation units: singular msgstr or each plural slot.
        for kind, plural_index, base_value in _translation_slots(base_entry):
            value_a = _slot_value(entry_a, kind, plural_index)
            value_b = _slot_value(entry_b, kind, plural_index)
            changed_a = value_a != base_value
            changed_b = value_b != base_value
            if not changed_a and not changed_b:
                continue

            common = dict(kind=kind, msgid=msgid, base=base_value,
                          msgctxt=msgctxt or None, plural_index=plural_index)
            if changed_a and changed_b:
                if value_a == value_b:
                    settled.append(SettledUnit(
                        value=value_a, vendors=(vendor_a, vendor_b), **common))
                else:
                    disputes.append(DisputedUnit(
                        values={vendor_a: value_a, vendor_b: value_b},
                        **common))
            elif changed_a:
                settled.append(SettledUnit(
                    value=value_a, vendors=(vendor_a,), **common))
            else:
                settled.append(SettledUnit(
                    value=value_b, vendors=(vendor_b,), **common))

        # Comment units: extracted comments and translator comments are tracked
        # separately from translations and from each other.
        for kind, field in _comment_fields():
            base_value = getattr(base_entry, field, '') or ''
            value_a = getattr(entry_a, field, '') or '' if entry_a else ''
            value_b = getattr(entry_b, field, '') or '' if entry_b else ''
            changed_a = value_a != base_value
            changed_b = value_b != base_value
            if not changed_a and not changed_b:
                continue

            common = dict(kind=kind, msgid=msgid, base=base_value,
                          msgctxt=msgctxt or None, plural_index=None)
            if changed_a and changed_b:
                if value_a == value_b:
                    settled.append(SettledUnit(
                        value=value_a, vendors=(vendor_a, vendor_b), **common))
                else:
                    disputes.append(DisputedUnit(
                        values={vendor_a: value_a, vendor_b: value_b},
                        **common))
            elif changed_a:
                settled.append(SettledUnit(
                    value=value_a, vendors=(vendor_a,), **common))
            else:
                settled.append(SettledUnit(
                    value=value_b, vendors=(vendor_b,), **common))

    return SettlementResult(vendor_a, vendor_b, settled, disputes)


def settle_paths(base_path, vendor_a_path, vendor_b_path,
                 vendor_a=None, vendor_b=None):
    """
    Path-based wrapper around :func:`settle`.

    Vendor names default to the file names (without extension) when not given.
    """
    base_po = polib.pofile(base_path)
    vendor_a_po = polib.pofile(vendor_a_path)
    vendor_b_po = polib.pofile(vendor_b_path)
    if vendor_a is None:
        vendor_a = os.path.splitext(os.path.basename(vendor_a_path))[0]
    if vendor_b is None:
        vendor_b = os.path.splitext(os.path.basename(vendor_b_path))[0]
    return settle(base_po, vendor_a_po, vendor_b_po, vendor_a, vendor_b)
