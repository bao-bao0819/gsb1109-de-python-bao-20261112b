# Sheet name allocation for stable supplier IDs.
#
# Suppliers are identified by a stable ID. Their display names may contain
# characters Excel forbids in sheet names (``[]:\?/*``), control characters,
# emoji, or far more than 31 characters, so names cannot be used as sheet
# tabs directly. This module assigns each supplier ID a valid, unique sheet
# name and can persist the registry to JSON so that:
#
# * a supplier keeps the same sheet name across exports (months),
# * a new supplier whose display name collides with an existing one gets a
#   suffixed name and never displaces the older supplier,
# * a deactivated supplier keeps its name, so reactivation reuses it,
# * conflicts are detected case-insensitively, like Excel does,
# * assignments never depend on the traversal order of the input file:
#   new suppliers are allocated in sorted supplier-ID order.
from __future__ import unicode_literals

import io
import json
import os

import six

from .Utils import valid_sheet_name

# Excel limits sheet names to 31 UTF-16 code units; a non-BMP character
# (e.g. an emoji) counts as two.
MAX_SHEET_NAME_UNITS = 31

INVALID_SHEET_CHARS = frozenset('[]:\\?/*')
REPLACEMENT_CHAR = '_'
FALLBACK_BASE_NAME = 'Supplier'
DEFAULT_MAPPING_SHEET_NAME = '名称对照表'

_MAPPING_HEADER = ('供货商编号', '页签名', '原始名称')

# Owner marker for names reserved for internal sheets (e.g. the mapping
# sheet) so they can never be handed out to a supplier.
_RESERVED = object()

_MISSING = object()


def _utf16_units(text):
    return len(text.encode('utf-16-le')) // 2


def _truncate_to_units(text, max_units):
    out = []
    units = 0
    for ch in text:
        width = _utf16_units(ch)
        if units + width > max_units:
            break
        out.append(ch)
        units += width
    return ''.join(out)


def _id_sort_key(supplier_id):
    # Deterministic ordering that does not depend on input traversal order:
    # numbers sort numerically before everything else, other IDs by text.
    if isinstance(supplier_id, six.integer_types + (float,)):
        return (0, supplier_id, '')
    return (1, 0, six.text_type(supplier_id))


def sanitize_sheet_name(raw_name, max_units=MAX_SHEET_NAME_UNITS):
    """Turn an arbitrary display name into a valid Excel sheet name base.

    Forbidden characters are replaced with ``_``, control characters are
    replaced as well, a leading apostrophe is dropped, and the result is
    truncated to 31 UTF-16 code units without splitting surrogate pairs.
    Returns ``''`` if nothing usable remains.
    """
    if raw_name is None:
        return ''
    text = six.text_type(raw_name)
    out = []
    units = 0
    for ch in text:
        if ch in INVALID_SHEET_CHARS or ch < ' ':
            ch = REPLACEMENT_CHAR
        width = _utf16_units(ch)
        if units + width > max_units:
            break
        out.append(ch)
        units += width
    return ''.join(out).lstrip("'")


class SheetNameAllocator(object):
    """Assigns valid, unique, persistent sheet names to supplier IDs."""

    def __init__(self, assignments=None):
        self._assignments = {}  # supplier_id -> sheet name
        self._display = {}      # supplier_id -> last seen display name
        self._used = {}         # sheet name (lower) -> supplier_id or _RESERVED
        self._reserved = {}     # requested base (lower) -> reserved sheet name
        if assignments:
            items = getattr(assignments, 'items', None)
            if items is None:
                pairs = assignments
            else:
                pairs = items()
            for supplier_id, sheet_name in pairs:
                self._register(supplier_id, sheet_name)

    @property
    def assignments(self):
        """A copy of the current ``{supplier_id: sheet_name}`` mapping."""
        return dict(self._assignments)

    def _register(self, supplier_id, sheet_name, display_name=None):
        if not valid_sheet_name(sheet_name):
            raise ValueError('invalid sheet name %r' % (sheet_name,))
        owner = self._used.get(sheet_name.lower(), _MISSING)
        if owner is _RESERVED:
            raise ValueError('sheet name %r is reserved' % (sheet_name,))
        if owner is not _MISSING and owner != supplier_id:
            raise ValueError(
                'sheet name %r already assigned to supplier %r'
                % (sheet_name, owner))
        existing = self._assignments.get(supplier_id)
        if existing is not None and existing != sheet_name:
            raise ValueError(
                'supplier %r already has sheet name %r'
                % (supplier_id, existing))
        self._assignments[supplier_id] = sheet_name
        self._used[sheet_name.lower()] = supplier_id
        if display_name is not None:
            self._display[supplier_id] = display_name

    def _unique_name(self, base):
        if base.lower() not in self._used:
            return base
        counter = 2
        while True:
            suffix = ' (%d)' % counter
            stem = _truncate_to_units(
                base, MAX_SHEET_NAME_UNITS - _utf16_units(suffix))
            candidate = stem + suffix
            if candidate.lower() not in self._used:
                return candidate
            counter += 1

    def assign(self, suppliers):
        """Assign a sheet name to every ``(supplier_id, display_name)`` pair.

        Suppliers already known to the allocator keep their existing name.
        New suppliers are allocated in sorted supplier-ID order, so the
        result never depends on the order of the input. Returns a
        ``{supplier_id: sheet_name}`` dict covering exactly the given
        suppliers.
        """
        suppliers = list(suppliers)
        seen_ids = set()
        for supplier_id, _display_name in suppliers:
            if supplier_id in seen_ids:
                raise ValueError(
                    'duplicate supplier id %r in input' % (supplier_id,))
            seen_ids.add(supplier_id)
        new_suppliers = [
            (supplier_id, display_name)
            for supplier_id, display_name in suppliers
            if supplier_id not in self._assignments
        ]
        new_suppliers.sort(key=lambda item: _id_sort_key(item[0]))
        for supplier_id, display_name in new_suppliers:
            base = sanitize_sheet_name(display_name) or FALLBACK_BASE_NAME
            self._register(supplier_id, self._unique_name(base), display_name)
        for supplier_id, display_name in suppliers:
            if display_name is not None:
                self._display[supplier_id] = display_name
        return {
            supplier_id: self._assignments[supplier_id]
            for supplier_id, _display_name in suppliers
        }

    def reserve_name(self, raw_name):
        """Reserve a valid sheet name for a non-supplier sheet.

        Returns the reserved name; reserving the same base name again
        returns the existing reservation.
        """
        base = sanitize_sheet_name(raw_name) or FALLBACK_BASE_NAME
        key = base.lower()
        if key in self._reserved:
            return self._reserved[key]
        name = self._unique_name(base)
        self._reserved[key] = name
        self._used[name.lower()] = _RESERVED
        return name

    def mapping_rows(self, suppliers=None):
        """Rows ``(supplier_id, sheet_name, display_name)`` for the mapping
        sheet, sorted by supplier ID.

        With ``suppliers=None`` every known supplier is listed, including
        deactivated ones, using the last seen display name.
        """
        if suppliers is None:
            ids = sorted(self._assignments, key=_id_sort_key)
            return [
                (supplier_id, self._assignments[supplier_id],
                 self._display.get(supplier_id))
                for supplier_id in ids
            ]
        rows = []
        for supplier_id, display_name in suppliers:
            if supplier_id not in self._assignments:
                raise ValueError(
                    'unknown supplier id %r; call assign() first'
                    % (supplier_id,))
            rows.append(
                (supplier_id, self._assignments[supplier_id], display_name))
        rows.sort(key=lambda row: _id_sort_key(row[0]))
        return rows

    def write_mapping_sheet(self, workbook, suppliers=None,
                            sheet_name=DEFAULT_MAPPING_SHEET_NAME,
                            cell_overwrite_ok=False):
        """Add the name-mapping sheet to ``workbook`` and return it."""
        worksheet = workbook.add_sheet(
            self.reserve_name(sheet_name),
            cell_overwrite_ok=cell_overwrite_ok)
        for col, text in enumerate(_MAPPING_HEADER):
            worksheet.write(0, col, text)
        for row, (supplier_id, name, display_name) in enumerate(
                self.mapping_rows(suppliers), start=1):
            worksheet.write(row, 0, supplier_id)
            worksheet.write(row, 1, name)
            worksheet.write(row, 2, '' if display_name is None else display_name)
        return worksheet

    @classmethod
    def load(cls, path):
        """Load a registry previously written by :meth:`save`."""
        with io.open(path, encoding='utf-8') as f:
            data = json.load(f)
        allocator = cls()
        for entry in data.get('assignments', []):
            supplier_id, sheet_name = entry[0], entry[1]
            display_name = entry[2] if len(entry) > 2 else None
            allocator._register(supplier_id, sheet_name, display_name)
        for base, name in data.get('reserved', []):
            allocator._reserved[base] = name
            allocator._used[name.lower()] = _RESERVED
        return allocator

    @classmethod
    def load_or_create(cls, path):
        """Load the registry at ``path`` if it exists, else start empty."""
        if os.path.exists(path):
            return cls.load(path)
        return cls()

    def save(self, path):
        """Persist the registry (including deactivated suppliers and
        reserved names) to ``path`` as JSON, atomically."""
        assignments = [
            [supplier_id, self._assignments[supplier_id],
             self._display.get(supplier_id)]
            for supplier_id in sorted(self._assignments, key=_id_sort_key)
        ]
        payload = {
            'version': 1,
            'assignments': assignments,
            'reserved': sorted(self._reserved.items()),
        }
        tmp_path = path + '.tmp'
        with io.open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(payload, f, ensure_ascii=False, indent=2,
                      sort_keys=True)
            f.write('\n')
        if hasattr(os, 'replace'):
            os.replace(tmp_path, path)
        else:
            os.rename(tmp_path, path)
