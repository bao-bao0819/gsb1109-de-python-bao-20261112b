# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

"""
Data dictionary export.

A FIT file may re-use the same *local message number* several times, each time
with a different :class:`~fitdecode.records.FitDefinitionMessage` (different
byte order, base types and/or field widths). This module snapshots every
definition message into an immutable *data dictionary* and merges the data
messages that share the same global message number into *business tables*.

Columns of a business table are merged only while their on-disk encoding stays
compatible. As soon as a field is redefined with an incompatible byte order,
base type or width, a new column variant is created and the previously
recorded rows keep pointing at their original (immutable) variant. This makes
it possible to trace any row, and any of its cells, back to the exact bytes it
was decoded from.
"""

import struct
from collections import OrderedDict

from . import records
from . import types

__all__ = [
    'DataDictionary', 'MessageTable', 'Column', 'ColumnVariant', 'Row', 'Cell',
    'DefinitionLayout', 'build_data_dictionary', 'encode_field_value',
    'field_byte_layout']

_ENDIAN_NAMES = {'<': 'little', '>': 'big'}

# FIT SDK "invalid" sentinel per base type name
_INVALID_INT_VALUES = {
    'enum': 0xFF,
    'sint8': 0x7F,
    'uint8': 0xFF,
    'sint16': 0x7FFF,
    'uint16': 0xFFFF,
    'sint32': 0x7FFFFFFF,
    'uint32': 0xFFFFFFFF,
    'sint64': 0x7FFFFFFFFFFFFFFF,
    'uint64': 0xFFFFFFFFFFFFFFFF}
_ZERO_INVALID_TYPES = ('uint8z', 'uint16z', 'uint32z', 'uint64z')


def _field_struct_format(field_def, endian):
    base_type = field_def.base_type
    count = field_def.size // base_type.size
    return f'{endian}{count if count > 1 else ""}{base_type.fmt}'


def field_byte_layout(field_def, endian):
    """
    Return a plain, JSON-serializable ``dict`` describing the on-disk layout of
    a field definition: byte order, FIT base type, element width, number of
    elements and total field width in bytes.
    """
    base_type = field_def.base_type
    count = field_def.size // base_type.size
    layout = {
        'def_num': field_def.def_num,
        'name': field_def.name,
        'base_type': base_type.name,
        'base_type_num': base_type.identifier,
        'endian': _ENDIAN_NAMES[endian],
        'element_size': base_type.size,
        'elements': count,
        'size': field_def.size,
        'struct_format': _field_struct_format(field_def, endian),
        'is_dev': field_def.is_dev}
    if field_def.is_dev:
        layout['dev_data_index'] = field_def.dev_data_index
    return layout


def _invalid_sentinel_bytes(base_type, size):
    name = base_type.name
    if name == 'string':
        return None
    if name in _ZERO_INVALID_TYPES:
        return b'\x00' * size
    if name in ('float32', 'float64'):
        # NaN payloads are not unique, so the original bytes cannot be rebuilt
        # from the parsed (None) value alone
        return None
    if name == 'byte':
        return b'\xff' * size
    sentinel = _INVALID_INT_VALUES[name]
    count = size // base_type.size
    return struct.pack(
        f'{count if count > 1 else ""}{base_type.fmt}',
        *([sentinel] * count))


def encode_field_value(field_def, endian, raw_value):
    """
    Re-encode a field's ``raw_value`` back to its original bytes, according to
    the given field definition and byte order.

    The result is guaranteed to match the source bytes for numeric, float and
    ``byte`` fields holding a regular value. `None` is returned when the parsed
    value alone is not enough to rebuild the exact original bytes (a ``string``
    with arbitrary null padding, or a NaN float whose payload is unknown); use
    `Cell.raw_bytes` in that case.
    """
    base_type = field_def.base_type
    size = field_def.size

    if raw_value is None:
        return _invalid_sentinel_bytes(base_type, size)

    if base_type.identifier == types.BASE_TYPE_BYTE.identifier:
        return bytes(raw_value)

    if base_type.name == 'string':
        return None

    count = size // base_type.size
    if isinstance(raw_value, tuple):
        values = raw_value
        # an array holding invalid elements loses their exact encoding once
        # parsed into None values, so defer to the caller's raw bytes
        if any(value is None for value in values):
            return None
    else:
        values = (raw_value,)

    return struct.pack(
        f'{endian}{count if count > 1 else ""}{base_type.fmt}', *values)


class DefinitionLayout:
    """
    Immutable snapshot of one :class:`FitDefinitionMessage` as it was seen at a
    given point of the stream.
    """

    __slots__ = (
        'definition_id', 'def_mesg', 'local_mesg_num', 'global_mesg_num',
        'name', 'endian', 'message_size', 'field_layouts', 'chunk_offsets')

    def __init__(self, definition_id, def_mesg):
        self.definition_id = definition_id
        self.def_mesg = def_mesg
        self.local_mesg_num = def_mesg.local_mesg_num
        self.global_mesg_num = def_mesg.global_mesg_num
        self.name = def_mesg.name
        self.endian = def_mesg.endian
        self.message_size = sum(f.size for f in def_mesg.all_field_defs)

        self.field_layouts = []
        # offsets are relative to the start of the frame, so the one-byte
        # record header is accounted for and the first field starts at 1
        offset = 1
        for field_def in def_mesg.all_field_defs:
            layout = field_byte_layout(field_def, def_mesg.endian)
            layout['offset'] = offset
            self.field_layouts.append(layout)
            offset += field_def.size

        #: offsets of the FitChunk objects of every data message using this
        #: definition (i.e. every row bound to this dictionary entry)
        self.chunk_offsets = []

    def to_dict(self):
        return OrderedDict((
            ('definition_id', self.definition_id),
            ('name', self.name),
            ('global_mesg_num', self.global_mesg_num),
            ('local_mesg_num', self.local_mesg_num),
            ('endian', _ENDIAN_NAMES[self.endian]),
            ('message_size', self.message_size),
            ('fields', list(self.field_layouts)),
            ('used_by_chunks', list(self.chunk_offsets))))


class ColumnVariant:
    """
    One physical encoding of a column.

    A variant is created the first time a given field signature is seen and is
    never mutated afterwards, so rows recorded before an incompatible
    redefinition keep their original byte order and field width.
    """

    __slots__ = ('variant_index', 'signature', 'layout', 'definition_ids')

    def __init__(self, variant_index, signature, layout, definition_id):
        self.variant_index = variant_index
        self.signature = signature
        self.layout = layout
        self.definition_ids = [definition_id]

    def to_dict(self):
        data = OrderedDict(self.layout)
        data['variant_index'] = self.variant_index
        data['definition_ids'] = list(self.definition_ids)
        return data


class Column:
    """
    A logical column of a business table, identified by its field definition
    number (and its developer data index for developer fields).

    It may hold several `ColumnVariant` objects if the field was redefined with
    an incompatible encoding along the stream.
    """

    __slots__ = ('key', 'name', 'def_num', 'is_dev', 'dev_data_index',
                 'variants', '_signatures')

    def __init__(self, field_def):
        self.key = Column.column_key(field_def)
        self.name = field_def.name
        self.def_num = field_def.def_num
        self.is_dev = field_def.is_dev
        self.dev_data_index = (
            field_def.dev_data_index if field_def.is_dev else None)
        self.variants = []
        self._signatures = {}

    @staticmethod
    def column_key(field_def):
        if field_def.is_dev:
            return ('dev', field_def.dev_data_index, field_def.def_num)
        return ('std', field_def.def_num)

    @staticmethod
    def field_signature(field_def, endian):
        base_type = field_def.base_type
        return (
            endian,
            base_type.identifier,
            base_type.fmt,
            base_type.size,
            field_def.size // base_type.size,
            field_def.size)

    def get_or_add_variant(self, field_def, endian, definition_id):
        signature = self.field_signature(field_def, endian)
        try:
            variant = self._signatures[signature]
        except KeyError:
            layout = field_byte_layout(field_def, endian)
            variant = ColumnVariant(
                len(self.variants), signature, layout, definition_id)
            self.variants.append(variant)
            self._signatures[signature] = variant

        if definition_id not in variant.definition_ids:
            variant.definition_ids.append(definition_id)

        return variant

    @property
    def is_shared(self):
        """True if all the rows of this column share a single encoding."""
        return len(self.variants) == 1

    @property
    def logical_names(self):
        if self.is_shared:
            return [self.name]
        return [
            f'{self.name}__v{variant.variant_index}'
            for variant in self.variants]

    def to_dict(self):
        return OrderedDict((
            ('name', self.name),
            ('def_num', self.def_num),
            ('is_dev', self.is_dev),
            ('dev_data_index', self.dev_data_index),
            ('shared', self.is_shared),
            ('logical_names', list(self.logical_names)),
            ('variants', [variant.to_dict() for variant in self.variants])))


class Cell:
    """One cell of a `Row`: a field value plus its exact byte location."""

    __slots__ = (
        'column_key', 'variant_index', 'field_data', 'field_def',
        'frame_offset', 'offset', 'size')

    def __init__(self, column_key, variant_index, field_data, field_def,
                 frame_offset, offset, size):
        self.column_key = column_key
        self.variant_index = variant_index
        self.field_data = field_data
        self.field_def = field_def
        #: absolute offset of the owning frame in the FIT stream
        self.frame_offset = frame_offset
        #: offset of this field relative to the start of its frame (including
        #: the one-byte record header)
        self.offset = offset
        self.size = size

    def raw_bytes(self, row):
        """Return the exact original bytes of this cell."""
        return row.frame.chunk.bytes[self.offset:self.offset + self.size]

    def reencoded_bytes(self, endian):
        return encode_field_value(
            self.field_def, endian, self.field_data.raw_value)

    def to_dict(self, row, *, include_raw_bytes=False):
        data = OrderedDict((
            ('column', ':'.join(str(part) for part in self.column_key)),
            ('variant_index', self.variant_index),
            ('offset', self.offset),
            ('size', self.size),
            ('value', self.field_data.value),
            ('raw_value', self.field_data.raw_value)))
        if include_raw_bytes and row.frame.chunk is not None:
            data['raw_bytes'] = self.raw_bytes(row).hex()
        return data


class Row:
    """One data message merged into a `MessageTable`."""

    __slots__ = ('index', 'frame', 'definition_id', 'cells')

    def __init__(self, index, frame, definition_id):
        self.index = index
        self.frame = frame
        self.definition_id = definition_id
        #: ordered dict: column key -> Cell
        self.cells = OrderedDict()

    def to_dict(self, *, include_raw_bytes=False):
        return OrderedDict((
            ('row_index', self.index),
            ('definition_id', self.definition_id),
            ('chunk_offset',
             self.frame.chunk.offset if self.frame.chunk is not None else None),
            ('cells', [
                cell.to_dict(self, include_raw_bytes=include_raw_bytes)
                for cell in self.cells.values()])))


class MessageTable:
    """
    Business table gathering every data message sharing the same global
    message number, regardless of the local message number they were carried
    on and of the successive redefinitions of that local message.
    """

    __slots__ = ('global_mesg_num', 'name', 'columns', 'rows', '_column_order')

    def __init__(self, global_mesg_num, name):
        self.global_mesg_num = global_mesg_num
        self.name = name
        self.columns = OrderedDict()
        self.rows = []
        self._column_order = []

    def get_or_create_column(self, field_def):
        key = Column.column_key(field_def)
        try:
            return self.columns[key]
        except KeyError:
            column = Column(field_def)
            self.columns[key] = column
            self._column_order.append(key)
            return column

    def to_dict(self, *, include_raw_bytes=False):
        return OrderedDict((
            ('name', self.name),
            ('global_mesg_num', self.global_mesg_num),
            ('columns', [
                self.columns[key].to_dict() for key in self._column_order]),
            ('rows', [
                row.to_dict(include_raw_bytes=include_raw_bytes)
                for row in self.rows])))


class DataDictionary:
    """
    Build and hold the data dictionary of a FIT stream.

    Feed it with the frames yielded by `FitReader` (constructed with
    ``keep_raw_chunks=True`` if you intend to trace rows back to their original
    bytes), either one by one through `add_frame` or all at once through
    `build_data_dictionary`.
    """

    def __init__(self):
        self.definitions = []
        self.tables = OrderedDict()
        self._definition_ids = {}

    def add_frame(self, frame):
        if isinstance(frame, records.FitDefinitionMessage):
            self._register_definition(frame)
        elif isinstance(frame, records.FitDataMessage):
            self._register_data_message(frame)
        # other frame types (header, CRC) do not contribute to the dictionary

    def _register_definition(self, def_mesg):
        # A redefinition always produces a new FitDefinitionMessage object, so
        # object identity is a safe key here.
        def_id = id(def_mesg)
        if def_id in self._definition_ids:
            return self._definition_ids[def_id]

        layout = DefinitionLayout(len(self.definitions), def_mesg)
        self.definitions.append(layout)
        self._definition_ids[def_id] = layout
        return layout

    def _register_data_message(self, frame):
        def_mesg = frame.def_mesg
        layout = self._register_definition(def_mesg)

        table_key = (def_mesg.global_mesg_num, def_mesg.name)
        try:
            table = self.tables[table_key]
        except KeyError:
            table = MessageTable(*table_key)
            self.tables[table_key] = table

        if frame.chunk is not None:
            layout.chunk_offsets.append(frame.chunk.offset)

        row = Row(len(table.rows), frame, layout.definition_id)

        # cumulative offset of each field inside the data message payload;
        # the one-byte record header precedes it
        all_field_defs = list(def_mesg.all_field_defs)
        payload_offsets = {}
        relative_offset = 1
        for field_def in all_field_defs:
            payload_offsets[id(field_def)] = relative_offset
            relative_offset += field_def.size

        for field_data in frame.fields:
            # fields generated through component expansion or compressed
            # timestamps have no field definition and no bytes of their own
            if field_data.field_def is None:
                continue

            field_def = field_data.field_def
            column = table.get_or_create_column(field_def)
            variant = column.get_or_add_variant(
                field_def, def_mesg.endian, layout.definition_id)

            try:
                offset = payload_offsets[id(field_def)]
            except KeyError:
                continue

            row.cells[column.key] = Cell(
                column.key, variant.variant_index, field_data, field_def,
                frame.chunk.offset if frame.chunk is not None else None,
                offset, field_def.size)

        table.rows.append(row)

    def definition_of(self, row):
        """Return the `DefinitionLayout` a given `Row` was decoded with."""
        return self.definitions[row.definition_id]

    def get_table(self, name_or_num):
        """
        Return the `MessageTable` registered under the given global message
        number (`int`) or message name (`str`). Raise `KeyError` if not found.
        """
        for (mesg_num, name), table in self.tables.items():
            if name_or_num in (mesg_num, name):
                return table
        raise KeyError(f'no table for message "{name_or_num}"')

    def trace_row(self, row):
        """
        Reverse-lookup helper: return an `OrderedDict` mapping each column key
        of *row* to a dict containing the field layout that was in effect, the
        exact original bytes and the decoded/raw values.
        """
        layout = self.definition_of(row)
        layouts = {
            (field['is_dev'],
             field.get('dev_data_index'), field['def_num']): field
            for field in layout.field_layouts}
        result = OrderedDict()
        for key, cell in row.cells.items():
            is_dev = key[0] == 'dev'
            dev_data_index = key[1] if is_dev else None
            field_layout = layouts[(is_dev, dev_data_index, key[-1])]
            result[key] = OrderedDict((
                ('layout', field_layout),
                ('raw_bytes', cell.raw_bytes(row)),
                ('value', cell.field_data.value),
                ('raw_value', cell.field_data.raw_value)))
        return result

    def to_dict(self, *, include_raw_bytes=False):
        """Plain, JSON-serializable representation of this dictionary."""
        return OrderedDict((
            ('definitions', [
                definition.to_dict() for definition in self.definitions]),
            ('tables', [
                table.to_dict(include_raw_bytes=include_raw_bytes)
                for table in self.tables.values()])))


def build_data_dictionary(frames):
    """
    Convenience helper that consumes an iterable of FIT frames and returns a
    ready-to-use `DataDictionary`.
    """
    dictionary = DataDictionary()
    for frame in frames:
        dictionary.add_frame(frame)
    return dictionary
