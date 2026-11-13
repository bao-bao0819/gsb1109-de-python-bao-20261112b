#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import json
import os.path
import struct
import tempfile
import unittest

import fitdecode
import fitdecode.cmd.fitjson as fitjson

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), 'files')

# global message number of the standard "record" message
RECORD_MESG_NUM = 20

# field definition numbers used by the synthetic record messages
FIELD_NUM_TIMESTAMP = 253
FIELD_NUM_HEART_RATE = 3
FIELD_NUM_SPEED = 6


def _base_type(name):
    return next(
        base_type
        for base_type in fitdecode.types.BASE_TYPES.values()
        if base_type.name == name)


def _definition_message(local_mesg_num, mesg_num, field_specs, endian):
    """Build the raw bytes of a definition message."""
    data = struct.pack('<B', 0x40 | local_mesg_num)
    data += struct.pack('<xB', int(endian == '>'))
    data += struct.pack(f'{endian}HB', mesg_num, len(field_specs))
    for def_num, base_type_name in field_specs:
        base_type = _base_type(base_type_name)
        data += struct.pack(
            '<3B', def_num, base_type.size, base_type.identifier)
    return data


def _data_message(local_mesg_num, field_specs, values, endian):
    """Build the raw bytes of one data message."""
    data = struct.pack('B', local_mesg_num)
    for value, (_, base_type_name) in zip(values, field_specs):
        base_type = _base_type(base_type_name)
        data += struct.pack(f'{endian}{base_type.fmt}', value)
    return data


def _wrap_fit(body):
    header = struct.pack(
        '<2BHI4s', 14, 16, 152, len(body), b'.FIT')
    header += struct.pack('<H', fitdecode.utils.compute_crc(header))
    file_data = header + body
    return file_data + struct.pack(
        '<H', fitdecode.utils.compute_crc(file_data))


def _build_redefinition_file(endian_variants=('<', '<', '<')):
    """
    Build a FIT file in which local message 0 (global "record") is defined
    three times: the "speed" field (def_num 6) is encoded on 2 bytes, then on
    4 bytes, then back on 2 bytes.

    *endian_variants* allows changing the byte order of each definition to
    make sure endianness is part of the column compatibility signature.
    """
    # (def_num, base type name); only speed changes width along the stream
    field_specs_16 = [
        (FIELD_NUM_TIMESTAMP, 'uint32'),
        (FIELD_NUM_HEART_RATE, 'uint8'),
        (FIELD_NUM_SPEED, 'uint16')]
    field_specs_32 = [
        (FIELD_NUM_TIMESTAMP, 'uint32'),
        (FIELD_NUM_HEART_RATE, 'uint8'),
        (FIELD_NUM_SPEED, 'uint32')]

    body = b''
    rows = [
        # values follow the (timestamp, heart_rate, speed) ordering
        (1000, 110, 300),
        (1001, 111, 70000),
        (1002, 112, 302)]

    specs = (field_specs_16, field_specs_32, field_specs_16)
    for index, (endian, specs) in enumerate(zip(endian_variants, specs)):
        body += _definition_message(0, RECORD_MESG_NUM, specs, endian)
        body += _data_message(0, specs, rows[index], endian)

    return _wrap_fit(body), rows


class DataDictionaryTestCase(unittest.TestCase):

    def _parse_dictionary(self, fit_bytes):
        with fitdecode.FitReader(
                fit_bytes,
                check_crc=fitdecode.CrcCheck.RAISE,
                keep_raw_chunks=True) as fit:
            return fitdecode.datadict.build_data_dictionary(fit)

    def test_definitions_are_snapshotted_individually(self):
        fit_bytes, _ = _build_redefinition_file()
        dictionary = self._parse_dictionary(fit_bytes)

        # one snapshot per redefinition, even though local message number is
        # always 0
        self.assertEqual(len(dictionary.definitions), 3)
        for index, layout in enumerate(dictionary.definitions):
            self.assertEqual(layout.definition_id, index)
            self.assertEqual(layout.local_mesg_num, 0)
            self.assertEqual(layout.global_mesg_num, RECORD_MESG_NUM)
            self.assertEqual(layout.endian, '<')
            self.assertEqual(
                [field['def_num'] for field in layout.field_layouts],
                [FIELD_NUM_TIMESTAMP, FIELD_NUM_HEART_RATE, FIELD_NUM_SPEED])

        speed_layouts = [
            layout.field_layouts[2] for layout in dictionary.definitions]
        self.assertEqual(
            [layout['size'] for layout in speed_layouts], [2, 4, 2])
        self.assertEqual(
            [layout['element_size'] for layout in speed_layouts],
            [2, 4, 2])
        self.assertEqual(
            [layout['base_type'] for layout in speed_layouts],
            ['uint16', 'uint32', 'uint16'])
        for layout in speed_layouts:
            self.assertEqual(layout['endian'], 'little')
            self.assertEqual(layout['elements'], 1)

    def test_compatible_columns_are_shared_incompatible_ones_split(self):
        fit_bytes, _ = _build_redefinition_file()
        dictionary = self._parse_dictionary(fit_bytes)

        [table] = dictionary.tables.values()
        self.assertEqual(table.name, 'record')
        self.assertEqual(len(table.rows), 3)

        columns = {column.def_num: column for column in table.columns.values()}

        # unchanged fields keep a single, shared column variant
        heart_rate = columns[FIELD_NUM_HEART_RATE]
        self.assertTrue(heart_rate.is_shared)
        self.assertEqual(len(heart_rate.variants), 1)
        self.assertEqual(heart_rate.variants[0].layout['size'], 1)

        timestamp = columns[FIELD_NUM_TIMESTAMP]
        self.assertTrue(timestamp.is_shared)

        # speed was redefined with an incompatible width: a second variant is
        # created, the first one is left untouched
        speed = columns[FIELD_NUM_SPEED]
        self.assertFalse(speed.is_shared)
        self.assertEqual(len(speed.variants), 2)
        self.assertEqual(speed.variants[0].layout['size'], 2)
        self.assertEqual(speed.variants[0].layout['base_type'], 'uint16')
        self.assertEqual(speed.variants[1].layout['size'], 4)
        self.assertEqual(speed.variants[1].layout['base_type'], 'uint32')

        # rows keep pointing at their original, immutable variant
        speed_cells = [row.cells[speed.key] for row in table.rows]
        self.assertEqual(
            [cell.variant_index for cell in speed_cells], [0, 1, 0])

        # the first variant must still reference both 16-bit definitions,
        # proving a later redefinition never rewrites a previously used column
        self.assertEqual(
            speed.variants[0].definition_ids, [0, 2])
        self.assertEqual(speed.variants[1].definition_ids, [1])

    def test_every_row_traces_back_to_its_original_bytes(self):
        fit_bytes, rows = _build_redefinition_file()
        dictionary = self._parse_dictionary(fit_bytes)
        [table] = dictionary.tables.values()

        for row_index, (row, expected_values) in enumerate(
                zip(table.rows, rows)):
            layout = dictionary.definition_of(row)
            self.assertEqual(row.definition_id, layout.definition_id)

            # the frame chunk is the exact bytes the data message was made of
            self.assertEqual(
                row.frame.chunk.bytes,
                fit_bytes[
                    row.frame.chunk.offset:
                    row.frame.chunk.offset + len(row.frame.chunk.bytes)])

            for cell, expected_value in zip(
                    row.cells.values(), expected_values):
                field_layout = next(
                    field
                    for field in layout.field_layouts
                    if field['def_num'] == cell.field_def.def_num)

                # dictionary layout and cell agree on width and position
                self.assertEqual(cell.size, field_layout['size'])
                self.assertEqual(cell.offset, field_layout['offset'])

                original_bytes = cell.raw_bytes(row)
                self.assertEqual(len(original_bytes), cell.size)
                self.assertEqual(
                    original_bytes,
                    row.frame.chunk.bytes[
                        cell.offset:cell.offset + cell.size])

                # decoding the original bytes yields the stored raw value
                reencoded = cell.reencoded_bytes(layout.endian)
                self.assertEqual(reencoded, original_bytes)
                self.assertEqual(cell.field_data.raw_value, expected_value)

    def test_endianness_change_creates_a_new_variant(self):
        # definitions: little-endian, big-endian, little-endian, same widths
        fit_bytes, _ = _build_redefinition_file(
            endian_variants=('<', '>', '<'))
        dictionary = self._parse_dictionary(fit_bytes)

        [table] = dictionary.tables.values()
        columns = {column.def_num: column for column in table.columns.values()}

        # even a 1-byte heart_rate field gets a new variant because its
        # definition switched byte order
        heart_rate = columns[FIELD_NUM_HEART_RATE]
        self.assertFalse(heart_rate.is_shared)
        self.assertEqual(len(heart_rate.variants), 2)
        self.assertEqual(
            [variant.layout['endian'] for variant in heart_rate.variants],
            ['little', 'big'])

        speed = columns[FIELD_NUM_SPEED]
        # 16-bit LE (defs #0 and #2), then 32-bit BE (def #1): switching back
        # to the original encoding reuses the first variant instead of
        # creating another one
        self.assertEqual(len(speed.variants), 2)
        self.assertEqual(
            [(variant.layout['base_type'], variant.layout['endian'])
             for variant in speed.variants],
            [('uint16', 'little'), ('uint32', 'big')])
        self.assertEqual(speed.variants[0].definition_ids, [0, 2])
        self.assertEqual(speed.variants[1].definition_ids, [1])
        self.assertEqual(
            [row.cells[speed.key].variant_index for row in table.rows],
            [0, 1, 0])

        # big-endian row still re-encodes to the original bytes
        big_endian_row = table.rows[1]
        layout = dictionary.definition_of(big_endian_row)
        self.assertEqual(layout.endian, '>')
        for cell in big_endian_row.cells.values():
            self.assertEqual(
                cell.reencoded_bytes('>'), cell.raw_bytes(big_endian_row))

    def test_json_export_is_serializable_and_reverse_lookup_works(self):
        fit_bytes, _ = _build_redefinition_file()
        dictionary = self._parse_dictionary(fit_bytes)

        payload = json.dumps(
            dictionary.to_dict(include_raw_bytes=True),
            cls=fitdecode.cmd.fitjson.RecordJSONEncoder)
        exported = json.loads(payload)

        self.assertEqual(len(exported['definitions']), 3)
        [exported_table] = exported['tables']
        self.assertEqual(len(exported_table['rows']), 3)

        # any exported row can be traced back to the source bytes using the
        # definition it declares
        definitions = {
            definition['definition_id']: definition
            for definition in exported['definitions']}
        for exported_row in exported_table['rows']:
            layout = definitions[exported_row['definition_id']]
            frame_offset = exported_row['chunk_offset']
            for exported_cell in exported_row['cells']:
                field_layout = next(
                    field
                    for field in layout['fields']
                    if field['def_num'] ==
                    int(exported_cell['column'].split(':')[-1]))
                absolute_offset = frame_offset + exported_cell['offset']
                self.assertEqual(
                    exported_cell['raw_bytes'],
                    fit_bytes[
                        absolute_offset:
                        absolute_offset + exported_cell['size']].hex())
                self.assertEqual(exported_cell['size'], field_layout['size'])

    def test_works_without_raw_chunks(self):
        fit_bytes, _ = _build_redefinition_file()
        with fitdecode.FitReader(
                fit_bytes, check_crc=fitdecode.CrcCheck.RAISE,
                keep_raw_chunks=False) as fit:
            dictionary = fitdecode.datadict.build_data_dictionary(fit)

        # layouts (endianness, base types, widths) are known even without raw
        # chunks, but byte-level reverse lookup is intentionally unavailable
        [table] = dictionary.tables.values()
        exported = table.to_dict()
        for exported_row in exported['rows']:
            self.assertIsNone(exported_row['chunk_offset'])
            for exported_cell in exported_row['cells']:
                self.assertNotIn('raw_bytes', exported_cell)

        speed_column = next(
            column for column in table.columns.values()
            if column.def_num == FIELD_NUM_SPEED)
        self.assertEqual(
            [variant.layout['size'] for variant in speed_column.variants],
            [2, 4])

    def test_trace_row_and_table_lookup(self):
        fit_bytes, _ = _build_redefinition_file()
        dictionary = self._parse_dictionary(fit_bytes)

        table = dictionary.get_table('record')
        self.assertIs(table, dictionary.get_table(RECORD_MESG_NUM))
        with self.assertRaises(KeyError):
            dictionary.get_table('does_not_exist')

        # the middle row used the 4-byte definition; tracing it must expose
        # the matching layout and the exact original bytes
        row = table.rows[1]
        traced = dictionary.trace_row(row)
        speed_key = ('std', FIELD_NUM_SPEED)
        self.assertIn(speed_key, traced)
        entry = traced[speed_key]
        self.assertEqual(entry['layout']['size'], 4)
        self.assertEqual(entry['layout']['base_type'], 'uint32')
        self.assertEqual(entry['raw_value'], 70000)
        self.assertEqual(
            entry['raw_bytes'], row.frame.chunk.bytes[
                entry['layout']['offset']:
                entry['layout']['offset'] + 4])

    def test_developer_fields_keep_their_own_columns(self):
        path = os.path.join(TEST_FILES_DIR, 'developer-types-sample.fit')
        with fitdecode.FitReader(
                path, check_crc=fitdecode.CrcCheck.RAISE,
                keep_raw_chunks=True) as fit:
            dictionary = fitdecode.datadict.build_data_dictionary(fit)

        dev_columns = [
            column
            for table in dictionary.tables.values()
            for column in table.columns.values()
            if column.is_dev]
        self.assertTrue(dev_columns)
        for column in dev_columns:
            # the developer data index must be part of the key so two
            # developer fields sharing a def_num never collapse together
            self.assertEqual(column.key[0], 'dev')
            self.assertIsNotNone(column.dev_data_index)
            self.assertTrue(column.is_shared)

        # exported cell references carry the full, unambiguous column key
        any_table = next(iter(dictionary.tables.values()))
        exported = any_table.to_dict()
        for exported_row in exported['rows']:
            for exported_cell in exported_row['cells']:
                self.assertTrue(
                    exported_cell['column'].startswith(
                        ('std:', 'dev:')))

    def test_real_files_round_trip_to_original_bytes(self):
        # exercise the dictionary on genuine, heterogeneous FIT files and
        # verify every cell slices the exact bytes of its frame
        for name in ('sample-activity.fit', 'garmin-fenix-5-bike.fit',
                     'developer-types-sample.fit'):
            path = os.path.join(TEST_FILES_DIR, name)
            with fitdecode.FitReader(
                    path, check_crc=fitdecode.CrcCheck.RAISE,
                    keep_raw_chunks=True) as fit:
                dictionary = fitdecode.datadict.build_data_dictionary(fit)

            self.assertTrue(dictionary.definitions)
            self.assertTrue(dictionary.tables)
            for table in dictionary.tables.values():
                for row in table.rows:
                    layout = dictionary.definition_of(row)
                    for cell in row.cells.values():
                        self.assertEqual(
                            cell.raw_bytes(row),
                            row.frame.chunk.bytes[
                                cell.offset:cell.offset + cell.size])
                        field_layout = next(
                            field
                            for field in layout.field_layouts
                            if field['def_num'] == cell.field_def.def_num
                            and field['is_dev'] == cell.field_def.is_dev)
                        self.assertEqual(field_layout['size'], cell.size)

                        # numeric fields holding a regular value must
                        # re-encode byte-for-byte; strings, NaN floats and
                        # arrays with invalid elements cannot be rebuilt from
                        # their parsed value alone and return None instead
                        reencoded = cell.reencoded_bytes(layout.endian)
                        if reencoded is not None:
                            self.assertEqual(
                                reencoded, cell.raw_bytes(row))


class FitJsonDataDictionaryCliTestCase(unittest.TestCase):

    def test_cli_datadict_option(self):
        fit_bytes, _ = _build_redefinition_file()

        with tempfile.TemporaryDirectory() as tmp_dir:
            in_path = os.path.join(tmp_dir, 'redefinition.fit')
            out_path = os.path.join(tmp_dir, 'dictionary.json')
            with open(in_path, 'wb') as fout:
                fout.write(fit_bytes)

            result = fitjson.main([
                '--datadict', '--raw-bytes', '--pretty',
                '-o', out_path, in_path])

            self.assertEqual(result, 0)
            with open(out_path, encoding='utf-8') as fin:
                exported = json.load(fin)

        self.assertIn('definitions', exported)
        self.assertIn('tables', exported)
        self.assertEqual(
            [definition['endian']
             for definition in exported['definitions']],
            ['little', 'little', 'little'])

        [table] = exported['tables']
        speed_column = next(
            column for column in table['columns']
            if column['def_num'] == FIELD_NUM_SPEED)
        self.assertFalse(speed_column['shared'])
        self.assertEqual(
            [variant['size'] for variant in speed_column['variants']],
            [2, 4])

        # the raw bytes exported for every cell must match the source file
        definitions = {
            definition['definition_id']: definition
            for definition in exported['definitions']}
        for exported_row in table['rows']:
            frame_offset = exported_row['chunk_offset']
            for exported_cell in exported_row['cells']:
                absolute_offset = frame_offset + exported_cell['offset']
                self.assertEqual(
                    exported_cell['raw_bytes'],
                    fit_bytes[
                        absolute_offset:
                        absolute_offset + exported_cell['size']].hex())
            # every row references a definition that actually exists
            self.assertIn(
                exported_row['definition_id'], definitions)


if __name__ == '__main__':
    unittest.main()
