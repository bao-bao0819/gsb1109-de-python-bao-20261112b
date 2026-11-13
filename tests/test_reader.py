#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import csv
import datetime
import glob
import hashlib
import os.path
import struct
import unittest

import fitdecode

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), 'files')
HASH_METHOD = 'sha1'


def _test_file(name):
    return os.path.join(TEST_FILES_DIR, name)


def _invalid_test_file(name):
    return os.path.join(TEST_FILES_DIR, 'invalid', name)


def _secs_to_dt(secs):
    return datetime.datetime.fromtimestamp(
        secs + fitdecode.FIT_UTC_REFERENCE,
        datetime.timezone.utc)


def _generate_messages(mesg_num, local_mesg_num, field_defs,
                       endian='<', data=None):
    mesgs = []
    base_type_list = []

    # definition message, local message num
    s = struct.pack('<B', 0x40 | local_mesg_num)

    # reserved byte and endian
    s += struct.pack('<xB', int(endian == '>'))

    # global message num, num fields
    s += struct.pack('%sHB' % endian, mesg_num, len(field_defs))

    for def_num, base_type in field_defs:
        base_type = [
            bt for bt in fitdecode.types.BASE_TYPES.values()
            if bt.name == base_type][0]
        base_type_list.append(base_type)
        s += struct.pack('<3B', def_num, base_type.size, base_type.identifier)

    mesgs.append(s)

    if data:
        for mesg_data in data:
            s = struct.pack('B', local_mesg_num)
            for value, base_type in zip(mesg_data, base_type_list):
                s += struct.pack("%s%s" % (endian, base_type.fmt), value)
            mesgs.append(s)

    return b''.join(mesgs)


def _dev_definition(local_mesg_num, global_mesg_num, field_defs=None,
                    dev_field_defs=None, endian='<'):
    header_byte = 0x40 | local_mesg_num
    if dev_field_defs:
        header_byte |= 0x20

    s = struct.pack('<B', header_byte)
    s += struct.pack('<xB', int(endian == '>'))
    s += struct.pack('%sHB' % endian, global_mesg_num, len(field_defs or []))
    for def_num, size, base_type_num in field_defs or []:
        s += struct.pack('<3B', def_num, size, base_type_num)

    if dev_field_defs:
        s += struct.pack('<B', len(dev_field_defs))
        for def_num, size, dev_data_index in dev_field_defs:
            s += struct.pack('<3B', def_num, size, dev_data_index)

    return s


def _dev_data(local_mesg_num, payload, is_dev=False):
    return struct.pack('B', (0x20 if is_dev else 0) | local_mesg_num) + payload


def _dev_string(value, size):
    encoded = value.encode('utf-8')
    return encoded + b'\x00' * (size - len(encoded))


def _dev_data_id(local_mesg_num, dev_data_index, application_id):
    return (
        _dev_definition(
            local_mesg_num, 207,
            field_defs=[(1, 16, 0x0d), (3, 1, 0x02), (4, 4, 0x86)]) +
        _dev_data(
            local_mesg_num,
            bytes(application_id) + struct.pack('<BI', dev_data_index, 1)))


def _field_desc(local_mesg_num, dev_data_index, field_def_num, base_type_num,
                name, units, scale=None, offset=None):
    field_defs = [
        (0, 1, 0x02), (1, 1, 0x02), (2, 1, 0x02), (3, 16, 0x07)]
    payload = struct.pack('<3B', dev_data_index, field_def_num, base_type_num)
    payload += _dev_string(name or '', 16)

    if scale is not None:
        field_defs.append((6, 1, 0x02))
        payload += struct.pack('<B', scale)
    if offset is not None:
        field_defs.append((7, 1, 0x01))
        payload += struct.pack('<b', offset)
    if units is not None:
        field_defs.append((8, 16, 0x07))
        payload += _dev_string(units, 16)

    return (
        _dev_definition(local_mesg_num, 206, field_defs=field_defs) +
        _dev_data(local_mesg_num, payload))


def _dev_record(local_mesg_num, timestamp, dev_payload, dev_data_index):
    return (
        _dev_definition(
            local_mesg_num, 20, field_defs=[(253, 4, 0x86)],
            dev_field_defs=[(3, len(dev_payload), dev_data_index)]) +
        _dev_data(
            local_mesg_num, struct.pack('<I', timestamp) + dev_payload,
            is_dev=True))


def _generate_fitfile(data=None, endian='<'):
    fit_data = (_generate_messages(
        # local mesg 0, global mesg 0 (file_id)
        mesg_num=0, local_mesg_num=0, endian=endian, field_defs=[
            # serial number, time_created, manufacturer
            (3, 'uint32z'), (4, 'uint32'), (1, 'uint16'),
            # product/garmin_product, number, type
            (2, 'uint16'), (5, 'uint16'), (0, 'enum')],
        # random serial number, random time, garmin, edge500, null, activity
        data=[[558069241, 723842606, 1, 1036, (2 ** 16) - 1, 4]]))

    if data:
        fit_data += data

    # Prototcol version 1.0, profile version 1.52
    header = struct.pack('<2BHI4s', 14, 16, 152, len(fit_data), b'.FIT')

    file_data = \
        header + \
        struct.pack('<H', fitdecode.utils.compute_crc(header)) + \
        fit_data

    return \
        file_data + \
        struct.pack('<H', fitdecode.utils.compute_crc(file_data))


class FitReaderTestCase(unittest.TestCase):

    def test_raw_chunk_parsing(self):
        """
        Test that FitReader parses correctly all our "valid" test files by
        building an in-memory clone of each source file, chunk by chunk, and
        then match file's and bytes object's checksums

        Files with developer types (at least)::

            developer-types-sample.fit
            20170518-191602-1740899583.fit
            DeveloperData.fit

        Chained files (at least)::

            activity-settings.fit
        """
        for src_file in glob.iglob(os.path.join(TEST_FILES_DIR, '*.fit')):
            raw_content = b''

            # read src_file chunk by chunk
            try:
                with fitdecode.FitReader(
                        src_file,
                        check_crc=fitdecode.CrcCheck.RAISE,
                        keep_raw_chunks=True) as fit:
                    for record in fit:
                        raw_content += record.chunk.bytes
            except Exception:
                print('ERROR while parsing:', src_file)
                raise

            # checksum of src_file
            h1 = hashlib.new(HASH_METHOD)
            with open(src_file, mode='rb') as fin:
                for buff in iter(lambda: fin.read(64 * 1024), b''):
                    h1.update(buff)

            # checksum of raw_content
            h2 = hashlib.new(HASH_METHOD)
            h2.update(raw_content)

            # compare checksums
            self.assertEqual(h1.digest(), h2.digest())

    def test_fitparse_invalid_crc(self):
        try:
            tuple(fitdecode.FitReader(
                _invalid_test_file('activity-filecrc.fit'),
                check_crc=fitdecode.CrcCheck.RAISE,
                keep_raw_chunks=True))
            self.fail('did not detect an invalid CRC')
        except fitdecode.FitCRCError:
            pass

    def test_fitparse_unexpected_eof(self):
        try:
            tuple(fitdecode.FitReader(
                _invalid_test_file('activity-unexpected-eof.fit'),
                check_crc=fitdecode.CrcCheck.RAISE,
                keep_raw_chunks=True))
            self.fail('did not detect an unexpected EOF')
        except fitdecode.FitEOFError:
            pass

    def test_fitparse_invalid_chained_files(self):
        """Detect errors when files are chained - concatenated - together"""
        try:
            tuple(fitdecode.FitReader(
                _invalid_test_file('activity-activity-filecrc.fit'),
                check_crc=fitdecode.CrcCheck.RAISE))
            self.fail("Didn't detect a CRC error in the chained file")
        except fitdecode.FitCRCError:
            pass

        try:
            tuple(fitdecode.FitReader(
                _invalid_test_file('activity-settings-corruptheader.fit'),
                check_crc=fitdecode.CrcCheck.RAISE))
            self.fail("Didn't detect a header error in the chained file")
        except fitdecode.FitHeaderError:
            pass

        try:
            tuple(fitdecode.FitReader(
                _invalid_test_file('activity-settings-nodata.fit'),
                check_crc=fitdecode.CrcCheck.RAISE))
            self.fail("Didn't detect an EOF error in the chaned file")
        except fitdecode.FitEOFError:
            pass

    def test_fitparse_elemnt_bolt_developer_data_id_without_application_id(self):
        """
        Test that a file without application id set inside developer_data_id is
        parsed (as seen on ELEMNT BOLT with firmware version WB09-1507)
        """
        tuple(fitdecode.FitReader(
            _invalid_test_file('elemnt-bolt-no-application-id-inside-developer-data-id.fit'),
            check_crc=fitdecode.CrcCheck.RAISE,
            keep_raw_chunks=True))

    def test_fitparse_basic_file_with_one_record(self, endian='<'):
        fit = tuple(fitdecode.FitReader(
            _generate_fitfile(endian=endian),
            check_crc=fitdecode.CrcCheck.RAISE,
            keep_raw_chunks=False))

        file_header = fit[0]
        file_id = fit[2]  # 1 is the definition message

        self.assertEqual(file_header.profile_ver, (1, 52))
        self.assertEqual(file_header.proto_ver, (1, 0))
        self.assertEqual(file_id.name, 'file_id')

        for field in ('type', 0):
            self.assertEqual(file_id.get_field(field).value, 'activity')
            self.assertEqual(file_id.get_field(field).raw_value, 4)

        for field in ('manufacturer', 1):
            self.assertEqual(file_id.get_field(field).value, 'garmin')
            self.assertEqual(file_id.get_field(field).raw_value, 1)

        for field in ('product', 'garmin_product', 2):
            self.assertEqual(file_id.get_field(field).value, 'edge500')
            self.assertEqual(file_id.get_field(field).raw_value, 1036)

        for field in ('serial_number', 3):
            self.assertEqual(file_id.get_field(field).value, 558069241)

        for field in ('time_created', 4):
            self.assertEqual(file_id.get_field(field).value, _secs_to_dt(723842606))
            self.assertEqual(file_id.get_field(field).raw_value, 723842606)

        for field in ('number', 5):
            self.assertEqual(file_id.get_field(field).value, None)

    def test_fitparse_basic_file_big_endian(self):
        self.test_fitparse_basic_file_with_one_record('>')

    def test_fitparse_component_field_accumulaters(self):
        csv_fp = open(
            _test_file('compressed-speed-distance-records.csv'),
            mode='rt')
        csv_file = csv.reader(csv_fp)
        next(csv_file)  # consume header

        # parse the whole content
        fit = tuple(fitdecode.FitReader(
            _test_file('compressed-speed-distance.fit'),
            check_crc=fitdecode.CrcCheck.RAISE,
            keep_raw_chunks=False))

        # make a generator of 'record' messages
        records = (
            r for r in fit
            if isinstance(r, fitdecode.FitDataMessage)
            and r.name == 'record')

        # skip empty record for now (sets timestamp via header)
        empty_record = next(records)

        # file's timestamp record is < 0x10000000, so field returns seconds
        self.assertEqual(empty_record.get_field('timestamp').value, 17217864)

        # TODO: update using local_timestamp as offset, since we have this value
        # as 2012 date

        for count, (record, (timestamp, heartrate, speed, distance, cadence)) in enumerate(zip(records, csv_file)):
            # no fancy datetime stuff, since timestamp record is < 0x10000000
            fit_ts = record.get_field('timestamp').value

            self.assertIsInstance(fit_ts, int)
            self.assertLess(fit_ts, 0x10000000)
            self.assertEqual(fit_ts, int(timestamp))

            self.assertEqual(record.get_field('heart_rate').value, int(heartrate))
            self.assertEqual(record.get_field('cadence').value, int(cadence) if cadence != 'null' else None)
            self.assertAlmostEqual(record.get_field('speed').value, float(speed))
            self.assertAlmostEqual(record.get_field('distance').value, float(distance))

        self.assertEqual(count, 753)  # TODO: confirm size(records) = size(csv)
        csv_fp.close()

    def test_fitparse_component_field_resolves_subfield(self):
        fit_data = _generate_fitfile(
            _generate_messages(
                # event (21), local message 1
                mesg_num=21, local_mesg_num=1, field_defs=[
                    # event, event_type, data16
                    (0, 'enum'), (1, 'enum'), (2, 'uint16')],
                data=[[0, 0, 2]]))

        # parse the whole content
        fit = tuple(fitdecode.FitReader(
            fit_data,
            check_crc=fitdecode.CrcCheck.RAISE,
            keep_raw_chunks=False))

        event = fit[4]
        self.assertEqual(event.name, 'event')

        for field in ('event', 0):
            self.assertEqual(event.get_field(field).value, 'timer')
            self.assertEqual(event.get_field(field).raw_value, 0)

        for field in ('event_type', 1):
            self.assertEqual(event.get_field(field).value, 'start')
            self.assertEqual(event.get_field(field).raw_value, 0)

        # should be able to reference by original field name, component field
        # name, subfield name, and then the field def_num of both the original
        # field and component field
        for field in ('timer_trigger', 'data', 3):
            self.assertEqual(event.get_field(field).value, 'fitness_equipment')
            self.assertEqual(event.get_field(field).raw_value, 2)

        # component field should be left as is
        for field in ('data16', 2):
            self.assertEqual(event.get_field(field).value, 2)

    def test_fitparse_subfield_components(self):
        # score = 123, opponent_score = 456, total = 29884539
        sport_point_value = 123 + (456 << 16)

        # rear_gear_num = 4, rear_gear, = 20, front_gear_num = 2, front_gear = 34
        gear_chance_value = 4 + (20 << 8) + (2 << 16) + (34 << 24)

        fit_data = _generate_fitfile(
            _generate_messages(
                # event (21), local message 1
                mesg_num=21, local_mesg_num=1, field_defs=[
                    # event, data
                    (0, 'enum'), (3, 'uint32')],
                data=[
                    # sport point
                    [33, sport_point_value],
                    # front gear change
                    [42, gear_chance_value]]))

        # parse the whole content
        fit = tuple(fitdecode.FitReader(
            fit_data,
            check_crc=fitdecode.CrcCheck.RAISE,
            keep_raw_chunks=False))

        sport_point = fit[4]
        self.assertEqual(sport_point.name, 'event')

        for field in ('event', 0):
            self.assertEqual(sport_point.get_field(field).value, 'sport_point')
            self.assertEqual(sport_point.get_field(field).raw_value, 33)

        for field in ('sport_point', 'data', 3):
            # verify raw numeric value
            self.assertEqual(sport_point.get_field(field).value, sport_point_value)

        for field in ('score', 7):
            self.assertEqual(sport_point.get_field(field).value, 123)

        for field in ('opponent_score', 8):
            self.assertEqual(sport_point.get_field(field).value, 456)

        gear_change = fit[5]
        self.assertEqual(gear_change.name, 'event')

        for field in ('event', 0):
            self.assertEqual(gear_change.get_field(field).value, 'front_gear_change')
            self.assertEqual(gear_change.get_field(field).raw_value, 42)

        for field in ('gear_change_data', 'data', 3):
            # verify raw numeric value
            self.assertEqual(gear_change.get_field(field).value, gear_chance_value)

        for field in ('front_gear_num', 9):
            self.assertEqual(gear_change.get_field(field).value, 2)

        for field in ('front_gear', 10):
            self.assertEqual(gear_change.get_field(field).value, 34)

        for field in ('rear_gear_num', 11):
            self.assertEqual(gear_change.get_field(field).value, 4)

        for field in ('rear_gear', 12):
            self.assertEqual(gear_change.get_field(field).value, 20)

    def test_fitparse_parsing_edge_500_fit_file(self):
        self._fitparse_csv_test_helper(
            'garmin-edge-500-activity.fit',
            'garmin-edge-500-activity-records.csv')

    def test_fitparse_parsing_fenix_5_bike_fit_file(self):
        self._fitparse_csv_test_helper(
            'garmin-fenix-5-bike.fit',
            'garmin-fenix-5-bike-records.csv')

    def test_fitparse_parsing_fenix_5_run_fit_file(self):
        self._fitparse_csv_test_helper(
            'garmin-fenix-5-run.fit',
            'garmin-fenix-5-run-records.csv')

    def test_fitparse_parsing_fenix_5_walk_fit_file(self):
        self._fitparse_csv_test_helper(
            'garmin-fenix-5-walk.fit',
            'garmin-fenix-5-walk-records.csv')

    def test_fitparse_parsing_edge_820_fit_file(self):
        self._fitparse_csv_test_helper(
            'garmin-edge-820-bike.fit',
            'garmin-edge-820-bike-records.csv')

    def _fitparse_csv_test_helper(self, fit_file, csv_file):
        csv_fp = open(_test_file(csv_file), 'r')
        csv_messages = csv.reader(csv_fp)
        field_names = next(csv_messages)  # consume header

        # parse the whole content
        fit = tuple(fitdecode.FitReader(
            _test_file(fit_file),
            check_crc=fitdecode.CrcCheck.RAISE,
            keep_raw_chunks=False))

        # make a generator of 'record' messages
        messages = (
            r for r in fit
            if isinstance(r, fitdecode.FitDataMessage)
            and r.name == 'record')

        # for fixups
        last_valid_lat, last_valid_long = None, None

        for message, csv_message in zip(messages, csv_messages):
            for csv_index, field_name in enumerate(field_names):
                try:
                    fit_value = message.get_field(field_name).value
                except KeyError:
                    fit_value = None

                csv_value = csv_message[csv_index]

                if field_name == 'timestamp':
                    # adjust GMT to PDT and format
                    fit_value = (fit_value - datetime.timedelta(hours=7)).strftime("%a %b %d %H:%M:%S PDT %Y")

                # track last valid lat/longs
                if field_name == 'position_lat':
                    if fit_value is not None:
                        last_valid_lat = fit_value
                if field_name == 'position_long':
                    if fit_value is not None:
                        last_valid_long = fit_value

                # ANT FIT SDK Dump tool does a bad job of logging invalids, so fix them
                if fit_value is None:
                    # ANT FIT SDK Dump tool cadence reports invalid as 0
                    if field_name == 'cadence' and csv_value == '0':
                        csv_value = None
                    # ANT FIT SDK Dump tool invalid lat/lng reports as last valid
                    if field_name == 'position_lat':
                        fit_value = last_valid_lat
                    if field_name == 'position_long':
                        fit_value = last_valid_long

                if isinstance(fit_value, int):
                    csv_value = int(fit_value)
                if csv_value == '':
                    csv_value = None

                if isinstance(fit_value, float):
                    # float comparison
                    self.assertAlmostEqual(fit_value, float(csv_value))
                else:
                    self.assertEqual(
                        fit_value, csv_value,
                        msg="For %s, FIT value '%s' did not match CSV value '%s'" % (field_name, fit_value, csv_value))

        try:
            next(messages)
            self.fail(".FIT file had more than csv file")
        except StopIteration:
            pass

        try:
            next(csv_messages)
            self.fail(".CSV file had more messages than .FIT file")
        except StopIteration:
            pass

        csv_fp.close()

    def test_fitparse_speed(self):
        fit = fitdecode.FitReader(
            _test_file('2019-02-17-062644-ELEMNT-297E-195-0.fit'),
            check_crc=fitdecode.CrcCheck.RAISE)

        # find the first 'session' data message
        msg = next(
            r for r in fit
            if isinstance(r, fitdecode.FitDataMessage)
            and r.name == 'session')

        self.assertEqual(msg.get_value('avg_speed', fit_type='uint16'), 5.86)

    def test_dev_field_description_after_values(self):
        """Developer values keep bytes until a later description arrives"""
        app_one = tuple(range(1, 17))

        body = b''
        body += _dev_data_id(0, 0, app_one)
        body += _dev_record(2, 10, struct.pack('<H', 100), 0)
        body += _dev_record(2, 11, struct.pack('<H', 101), 0)
        body += _field_desc(1, 0, 3, 0x84, 'late field', 'm', 10)
        body += _dev_record(2, 12, struct.pack('<H', 120), 0)

        frames = list(fitdecode.FitReader(
            _generate_fitfile(body), check_crc=fitdecode.CrcCheck.RAISE))
        messages = [f for f in frames if isinstance(f, fitdecode.FitDataMessage)]

        pending_fields = [
            f for m in messages if m.name == 'record' for f in m.fields
            if f.is_dev and f.field_def.def_num == 3]
        self.assertEqual(len(pending_fields), 3)

        # Values read before the description are filled in retroactively.
        first, second, third = pending_fields[:3]
        self.assertEqual(first.raw_value, 100)
        self.assertEqual(first.value, 10.0)
        self.assertEqual(first.units, 'm')
        self.assertEqual(first.raw_bytes, struct.pack('<H', 100))
        self.assertEqual(second.raw_value, 101)
        self.assertEqual(third.raw_value, 120)
        self.assertEqual(third.value, 12.0)

        for field in pending_fields:
            self.assertEqual(field.name, 'late field')
            self.assertEqual(field.dev_data_index, 0)
            self.assertEqual(field.application_id, app_one)
            self.assertFalse(field.is_unresolved_dev)

    def test_dev_field_reading_with_or_without_late_description(self):
        """Both reading orders must agree on fields and values"""
        app_one = tuple(range(1, 17))
        samples = [10, 11, 12]

        def _describe_values(description_first):
            body = _dev_data_id(0, 0, app_one)
            if description_first:
                body += _field_desc(1, 0, 3, 0x84, 'latched', 'm', 10)
            body += b''.join(
                _dev_record(2, ts, struct.pack('<H', ts * 10), 0)
                for ts in samples)
            if not description_first:
                body += _field_desc(1, 0, 3, 0x84, 'latched', 'm', 10)
            return body

        def _look(description_first):
            frames = tuple(fitdecode.FitReader(
                _generate_fitfile(_describe_values(description_first)),
                check_crc=fitdecode.CrcCheck.RAISE))
            return [
                (field.name, field.units, field.application_id,
                 field.raw_value, field.value)
                for message in frames
                if isinstance(message, fitdecode.FitDataMessage)
                and message.name == 'record'
                for field in message.fields if field.is_dev]

        after = _look(description_first=False)
        before = _look(description_first=True)
        self.assertEqual(len(after), len(samples))
        self.assertEqual(before, after)
        self.assertEqual(
            after,
            [('latched', 'm', app_one, ts * 10, float(ts))
             for ts in samples])

    def test_dev_field_same_number_two_applications(self):
        """Field 3 of one application must never resolve field 3 of another"""
        app_one = tuple(range(1, 17))
        app_two = tuple(range(101, 117))

        body = b''
        body += _dev_data_id(0, 0, app_one)
        body += _field_desc(1, 0, 3, 0x84, 'first app field', 'm', 10)
        body += _dev_record(2, 10, struct.pack('<H', 100), 0)
        body += _dev_data_id(3, 1, app_two)
        body += _field_desc(4, 1, 3, 0x84, 'second app field', 'cm', 2)
        body += _dev_record(5, 11, struct.pack('<H', 50), 1)

        frames = tuple(fitdecode.FitReader(
            _generate_fitfile(body), check_crc=fitdecode.CrcCheck.RAISE))
        records = [
            f for f in frames
            if isinstance(f, fitdecode.FitDataMessage) and f.name == 'record']

        first = [f for f in records[0].fields if f.is_dev][0]
        second = [f for f in records[1].fields if f.is_dev][0]
        for field in (first, second):
            self.assertNotEqual(field.name, 'unknown_dev_0_3')
            self.assertNotEqual(field.name, 'unknown_dev_1_3')
        self.assertEqual(first.name, 'first app field')
        self.assertEqual(first.units, 'm')
        self.assertEqual(first.value, 10.0)
        self.assertEqual(first.application_id, app_one)
        self.assertEqual(second.name, 'second app field')
        self.assertEqual(second.units, 'cm')
        self.assertEqual(second.value, 25.0)
        self.assertEqual(second.application_id, app_two)

    def test_dev_field_chained_file_reassigns_application_slot(self):
        """A chained file may reuse developer_data_index for another app"""
        app_one = tuple(range(1, 17))
        app_two = tuple(range(101, 117))

        first_file = _generate_fitfile(
            _dev_data_id(0, 0, app_one) +
            _field_desc(1, 0, 3, 0x84, 'first app field', 'm') +
            _dev_record(2, 10, struct.pack('<H', 100), 0))
        second_file = _generate_fitfile(
            _dev_data_id(0, 0, app_two) +
            _field_desc(1, 0, 3, 0x84, 'second app field', 'cm') +
            _dev_record(2, 10, struct.pack('<H', 50), 0))

        frames = tuple(fitdecode.FitReader(
            first_file + second_file, check_crc=fitdecode.CrcCheck.RAISE))
        records = [
            f for f in frames
            if isinstance(f, fitdecode.FitDataMessage) and f.name == 'record']

        first = [f for f in records[0].fields if f.is_dev][0]
        second = [f for f in records[1].fields if f.is_dev][0]
        self.assertEqual(first.name, 'first app field')
        self.assertEqual(second.name, 'second app field')
        self.assertEqual(first.application_id, app_one)
        self.assertEqual(second.application_id, app_two)

    def test_dev_field_without_description_keeps_raw_bytes(self):
        """No units/scale may be invented when no description was received"""
        body = _dev_record(2, 10, b'\x2a\x00\x00\x00', 7)

        frames = tuple(fitdecode.FitReader(
            _generate_fitfile(body), check_crc=fitdecode.CrcCheck.RAISE,
            error_handling=fitdecode.ErrorHandling.IGNORE))
        record = [
            f for f in frames
            if isinstance(f, fitdecode.FitDataMessage) and f.name == 'record'][0]
        field = [f for f in record.fields if f.is_dev][0]

        self.assertTrue(field.is_unresolved_dev)
        self.assertIsNone(field.units)
        self.assertEqual(field.raw_value, b'\x2a\x00\x00\x00')
        self.assertEqual(field.value, b'\x2a\x00\x00\x00')
        self.assertEqual(field.raw_bytes, b'\x2a\x00\x00\x00')
        self.assertEqual(field.dev_data_index, 7)
        self.assertIsNone(field.application_id)
        self.assertEqual(field.name, 'unknown_dev_7_3')

    def test_fitparse_units_processor(self):
        for x in ('2013-02-06-12-11-14.fit', '2015-10-13-08-43-15.fit',
                  'Activity.fit', 'Edge810-Vector-2013-08-16-15-35-10.fit',
                  'MonitoringFile.fit', 'Settings.fit', 'Settings2.fit',
                  'WeightScaleMultiUser.fit', 'WeightScaleSingleUser.fit',
                  'WorkoutCustomTargetValues.fit', 'WorkoutIndividualSteps.fit',
                  'WorkoutRepeatGreaterThanStep.fit', 'WorkoutRepeatSteps.fit',
                  'activity-large-fenxi2-multisport.fit', 'activity-small-fenix2-run.fit',
                  'antfs-dump.63.fit', 'sample-activity-indoor-trainer.fit',
                  'sample-activity.fit', 'garmin-fenix-5-bike.fit',
                  'garmin-fenix-5-run.fit', 'garmin-fenix-5-walk.fit',
                  'garmin-edge-820-bike.fit'):
            tuple(fitdecode.FitReader(
                _test_file(x),
                check_crc=fitdecode.CrcCheck.RAISE,
                processor=fitdecode.StandardUnitsDataProcessor()))

    def test_fitparse_int_long(self):
        """Test that ints are properly shifted and scaled"""
        fit = tuple(fitdecode.FitReader(
            _test_file('event_timestamp.fit'),
            check_crc=fitdecode.CrcCheck.RAISE))
        raw_value = fit[-2].get_value('event_timestamp', idx=0, raw_value=True)
        self.assertEqual(raw_value, 863.486328125)


if __name__ == '__main__':
    unittest.main()
