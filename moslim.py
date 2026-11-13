#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
moslim
======

Build a size-bounded, actually loadable gettext ``.mo`` catalog from an
existing ``.po`` file with :mod:`polib`.

Selection rules (in order):

1. The *required* closure always wins.  Every required entry key pulls its
   whole PO entry, including every plural branch (a plural message is
   atomic -- it can only be shipped with all of its ``msgstr[n]``).
2. The remaining entries are picked by offline usage count, highest first,
   as long as the resulting binary stays within the byte budget.  Entries
   that are never compiled (obsolete, fuzzy, untranslated) are ignored,
   exactly like ``polib``'s own serializer.
3. If even the required closure does not fit, no catalog is written and the
   size of the closure compiled on its own is reported instead: the device
   rejects over-limit files based on the final byte count, so guessing is
   not an option.

Every size decision is taken against the real output of
:meth:`polib._BaseFile.to_binary`, so the reported byte count is the exact
size of the ``.mo`` that gets written.
"""

from __future__ import absolute_import, print_function

import argparse
import sys

import polib


# Fixed cost of one MO entry: one (length, offset) pair in the key index
# plus one in the value index, i.e. four 32-bit integers.
_INDEX_COST_PER_ENTRY = 4 * 4
# Seven 32-bit integers in the MO header.
_HEADER_SIZE = 7 * 4


class RequiredEntryNotFound(KeyError):
    """Raised when a required entry key does not exist in the PO file."""


def _entry_key(entry):
    """Canonical key of a compilable entry.

    Mirrors the key space of the binary catalog: the ``msgctxt`` is folded
    in with an ``\\x04`` separator when present, so a plain msgid and a
    contextual one never collide.
    """
    return entry.msgid_with_context


def _compilable_entries(po):
    """Entries that polib would actually serialize into the MO."""
    return [entry for entry in po.translated_entries()
            if not entry.obsolete]


def _entry_payload_sizes(entry, encoding):
    """Byte lengths of the entry's key and value inside the string tables.

    Follows ``polib._BaseFile.to_binary`` one to one: contextual keys carry
    ``context + \\x04``, plural keys are ``msgid + \\0 + msgid_plural`` and
    plural values are the NUL-joined ``msgstr`` branches.
    """
    msgid = b''
    if entry.msgctxt:
        msgid += entry.msgctxt.encode(encoding) + b'\x04'
    if entry.msgid_plural:
        msgid += entry.msgid.encode(encoding) + b'\x00'
        msgid += entry.msgid_plural.encode(encoding)
        value = b'\x00'.join(
            entry.msgstr_plural[index].encode(encoding)
            for index in sorted(entry.msgstr_plural)
        )
    else:
        msgid += entry.msgid.encode(encoding)
        value = entry.msgstr.encode(encoding)
    # The trailing NUL terminator of each table string is stored too.
    return len(msgid) + 1, len(value) + 1


def compile_size(po, entries):
    """Exact byte length of the MO produced for ``entries``.

    The layout is fully additive: fixed header, 16 bytes of index per entry
    and the concatenated NUL-terminated key/value strings.  The result is
    cross-checked against ``to_binary`` in the test-suite rather than
    trusted blindly.
    """
    encoding = po.encoding
    string_bytes = 0
    for entry in entries:
        key_len, value_len = _entry_payload_sizes(entry, encoding)
        string_bytes += key_len + value_len
    # The metadata header entry is always the first compiled entry.
    total_entries = len(entries) + 1
    return (_HEADER_SIZE
            + total_entries * _INDEX_COST_PER_ENTRY
            + string_bytes
            + _metadata_string_bytes(po))


def _metadata_string_bytes(po):
    """Key/value string-table cost of the synthetic metadata entry.

    Its key is the empty string (one NUL) and its value is the rendered
    header block plus its terminating NUL.
    """
    mentry = po.metadata_as_entry()
    _, value_len = _entry_payload_sizes(mentry, po.encoding)
    return 1 + value_len


def _entry_delta_bytes(entry, encoding):
    """Bytes added to the MO by appending one entry.

    Once the header and metadata entry are in place, every extra entry
    costs its 16-byte index slot plus its NUL-terminated key and value
    strings.  The math is identical to :func:`compile_size` expressed as a
    delta, so the greedy loop stays linear even on large catalogs.
    """
    key_len, value_len = _entry_payload_sizes(entry, encoding)
    return _INDEX_COST_PER_ENTRY + key_len + value_len


def _clone_pofile(po, entries):
    """A fresh POFile holding only ``entries`` plus the original header."""
    slim = polib.POFile(encoding=po.encoding, wrapwidth=po.wrapwidth)
    slim.metadata = dict(po.metadata)
    slim.metadata_is_fuzzy = po.metadata_is_fuzzy
    slim.header = po.header
    for entry in entries:
        slim.append(entry)
    return slim


def _actual_binary_size(po, entries):
    """Ground truth: compile and measure."""
    return len(_clone_pofile(po, entries).to_binary())


def _resolve_required(po, required_keys):
    """Expand the required key set into concrete PO entries (the closure)."""
    by_key = dict((_entry_key(entry), entry)
                  for entry in _compilable_entries(po))
    # De-duplicate while preserving order so a repeated CLI flag cannot
    # produce duplicate keys inside the compiled catalog.
    unique_keys = list(dict((key, None) for key in required_keys))
    missing = [key for key in unique_keys if key not in by_key]
    if missing:
        raise RequiredEntryNotFound(
            'required entry not found in the PO file: %s'
            % ', '.join(repr(key) for key in sorted(missing))
        )
    return [by_key[key] for key in unique_keys]


def _sorted_optional(po, required_set, usage):
    """Compilable, non-required entries ordered for greedy admission.

    Higher usage count first; ties are broken deterministically by key so
    repeated runs emit byte-identical catalogs.  Entries without any usage
    data count as zero.
    """
    optional = [entry for entry in _compilable_entries(po)
                if _entry_key(entry) not in required_set]

    def sort_key(entry):
        key = _entry_key(entry)
        return (-usage.get(key, 0), key)

    return sorted(optional, key=sort_key)


def build_slim_catalog(po, max_bytes, required_keys=None, usage=None):
    """Select entries for a catalog that fits in ``max_bytes``.

    Returns a ``(entries, report)`` tuple.  ``entries`` is the ordered list
    of selected :class:`polib.POEntry` objects (required closure first, then
    usage-ranked extras).  ``report`` is a dict carrying every number a
    caller needs to audit the decision::

        {
            'limit': int,                 # the byte budget
            'full_size': int,             # size with every compilable entry
            'required_keys': [str, ...],
            'required_size': int,         # closure compiled on its own
            'fits': bool,                 # whether the closure fits alone
            'selected_size': int|None,    # size of the chosen catalog
            'selected_count': int,        # entries in the chosen catalog
            'skipped_count': int,         # compilable entries left out
            'usage': {key: int},          # normalized usage view
        }

    When the closure does not fit, ``entries`` is ``None`` and
    ``selected_size``/``selected_count`` are ``None``.
    """
    required_keys = list(required_keys or [])
    usage = dict(usage or {})

    all_entries = _compilable_entries(po)
    full_size = _actual_binary_size(po, all_entries)

    required_entries = _resolve_required(po, required_keys)
    required_keys = [_entry_key(entry) for entry in required_entries]
    required_set = set(required_keys)
    required_size = _actual_binary_size(po, required_entries)

    report = {
        'limit': max_bytes,
        'full_size': full_size,
        'required_keys': list(required_keys),
        'required_size': required_size,
        'fits': required_size <= max_bytes,
        'selected_size': None,
        'selected_count': None,
        'skipped_count': 0,
        'usage': dict(usage),
    }

    if required_size > max_bytes:
        report['skipped_count'] = len(all_entries) - len(required_entries)
        return None, report

    selected = list(required_entries)
    current_size = required_size
    for entry in _sorted_optional(po, required_set, usage):
        candidate_size = current_size + _entry_delta_bytes(
            entry, po.encoding
        )
        if candidate_size > max_bytes:
            continue
        selected.append(entry)
        current_size = candidate_size

    # Ground-truth check: the additive accounting must match the real
    # serializer byte for byte.
    verified_size = _actual_binary_size(po, selected)
    if verified_size != current_size:
        raise AssertionError(
            'internal size mismatch: counted %d but to_binary produced %d'
            % (current_size, verified_size)
        )

    report['selected_size'] = current_size
    report['selected_count'] = len(selected)
    report['skipped_count'] = len(all_entries) - len(selected)
    return selected, report


def write_slim_mo(po_path, mo_path, max_bytes, required_keys=None,
                  usage=None, po_encoding=None):
    """End-to-end helper: read a PO, slim it, write a loadable MO.

    Returns the selection report.  Raises
    :class:`RequiredEntryNotFound` if a required key is absent and
    :class:`BudgetExceeded` (see below) when the required closure itself is
    larger than the budget; in that case nothing is written and the
    exception carries the closure's compiled size.
    """
    po = polib.pofile(po_path, encoding=po_encoding) \
        if po_encoding else polib.pofile(po_path)
    entries, report = build_slim_catalog(
        po, max_bytes, required_keys=required_keys, usage=usage
    )
    if entries is None:
        raise BudgetExceeded(report)
    slim = _clone_pofile(po, entries)
    slim.save_as_mofile(mo_path)
    return report


class BudgetExceeded(Exception):
    """The required closure alone is bigger than the device budget."""

    def __init__(self, report):
        self.report = report
        super(BudgetExceeded, self).__init__(
            'required closure needs %d bytes but the limit is %d'
            % (report['required_size'], report['limit'])
        )


def load_usage(path):
    """Load offline usage counts from a ``key = count`` text file.

    Blank lines and lines starting with ``#`` are ignored.  A bare line is
    treated as a count of one.  Contextual keys may be written with their
    literal ``\\x04`` separator (the four characters ``\\\\x04`` in the
    file are decoded to the real separator).
    """
    usage = {}
    with open(path, 'r') as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' in line:
                key, count = line.split('=', 1)
                key = key.strip().replace('\\x04', '\x04')
                usage[key] = int(count.strip())
            else:
                usage[line.replace('\\x04', '\x04')] = 1
    return usage


def _parse_required(values):
    required = []
    for value in values or []:
        for piece in value.split(','):
            piece = piece.strip()
            if piece:
                required.append(piece.replace('\\x04', '\x04'))
    return required


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Build a byte-bounded slim .mo catalog from a .po file.'
    )
    parser.add_argument('po_path', help='source .po file')
    parser.add_argument('mo_path', help='destination .mo file')
    parser.add_argument(
        '-b', '--max-bytes', type=int, required=True,
        help='hard byte limit for the written .mo file'
    )
    parser.add_argument(
        '-r', '--required', action='append', default=[],
        help='msgid (or context\\x04msgid) that must be kept; repeat or use '
             'comma-separated lists'
    )
    parser.add_argument(
        '-u', '--usage',
        help='file of offline usage counts ("key = count" per line)'
    )
    args = parser.parse_args(argv)

    usage = load_usage(args.usage) if args.usage else {}
    try:
        report = write_slim_mo(
            args.po_path, args.mo_path, args.max_bytes,
            required_keys=_parse_required(args.required), usage=usage
        )
    except BudgetExceeded as exc:
        info = exc.report
        print(
            'REFUSED: required closure alone compiles to %d bytes, '
            'limit is %d (no .mo written)'
            % (info['required_size'], info['limit']),
            file=sys.stderr
        )
        return 2
    except RequiredEntryNotFound as exc:
        print('ERROR: %s' % exc, file=sys.stderr)
        return 3

    print('wrote %s' % args.mo_path)
    print('full catalog : %d bytes' % report['full_size'])
    print('slim catalog : %d bytes (limit %d)'
          % (report['selected_size'], report['limit']))
    print('entries kept : %d (skipped %d)'
          % (report['selected_count'], report['skipped_count']))
    return 0


if __name__ == '__main__':
    sys.exit(main())
