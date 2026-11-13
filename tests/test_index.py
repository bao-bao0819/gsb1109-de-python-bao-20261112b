#!/usr/bin/env python
# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

import os
import os.path
import random
import tempfile
import unittest

import fitdecode

TEST_FILES_DIR = os.path.join(os.path.dirname(__file__), 'files')

# files that, together, cover message redefinitions, developer fields,
# compressed timestamp headers and chained FIT files
INDEXABLE_FILES = (
    'compressed-speed-distance.fit',
    'DeveloperData.fit',
    'activity-small-fenix2-run.fit',
    'garmin-fenix-5-bike.fit',
    'activity-settings.fit')


def _test_file(name):
    return os.path.join(TEST_FILES_DIR, name)


def _frame_signature(frame):
    """
    Signature of a frame, made of its name, its (timestamp) values and its
    raw position in the file
    """
    if isinstance(frame, fitdecode.FitDataMessage):
        return (
            frame.frame_type, frame.name,
            tuple((f.name, f.value, f.raw_value) for f in frame.fields),
            frame.chunk.index, frame.chunk.offset, frame.chunk.bytes)
    elif isinstance(frame, fitdecode.FitDefinitionMessage):
        return (
            frame.frame_type, frame.name,
            frame.chunk.index, frame.chunk.offset, frame.chunk.bytes)
    elif isinstance(frame, fitdecode.FitHeader):
        return (
            frame.frame_type, frame.body_size,
            frame.chunk.index, frame.chunk.offset, frame.chunk.bytes)
    else:
        assert isinstance(frame, fitdecode.FitCRC)
        return (
            frame.frame_type, frame.crc,
            frame.chunk.index, frame.chunk.offset, frame.chunk.bytes)


def _read_sequential(path, index_interval):
    with fitdecode.FitReader(
            path,
            check_crc=fitdecode.CrcCheck.RAISE,
            keep_raw_chunks=True,
            index_interval=index_interval) as fit:
        frames = list(fit)
        index = fit.index
    return frames, index


class NonSeekableWrapper:
    def __init__(self, fd):
        self._fd = fd

    def read(self, size=-1):
        return self._fd.read(size)

    def seekable(self):
        return False


class FitIndexTestCase(unittest.TestCase):

    def test_resume_matches_sequential_read(self):
        """
        Randomly sample index entries (which lands on both sides of message
        redefinitions) and check that resuming from them yields the exact
        same names, values and raw offsets as a plain sequential read
        """
        rng = random.Random(20261112)

        for file_name in INDEXABLE_FILES:
            path = _test_file(file_name)
            frames, index = _read_sequential(path, index_interval=7)

            self.assertTrue(index.complete)
            self.assertGreater(len(index.entries), 1)

            # entries must be ordered and point at record boundaries
            offsets = [entry.offset for entry in index.entries]
            self.assertEqual(offsets, sorted(offsets))
            self.assertEqual(
                set(offsets),
                set(offsets) & {f.chunk.offset for f in frames})

            # locate the sequential frame that follows each entry
            frame_index_by_offset = {
                frame.chunk.offset: idx for idx, frame in enumerate(frames)}

            samples = rng.sample(
                index.entries, min(12, len(index.entries)))

            for entry in samples:
                resumed_frames = []
                with fitdecode.FitReader(
                        path,
                        check_crc=fitdecode.CrcCheck.RAISE,
                        keep_raw_chunks=True,
                        resume_from=entry) as fit:
                    for idx, frame in enumerate(fit):
                        if idx >= 300:
                            break
                        resumed_frames.append(frame)

                start = frame_index_by_offset[entry.offset]
                expected = frames[start:start + len(resumed_frames)]

                self.assertEqual(len(resumed_frames), len(expected))
                for resumed_frame, expected_frame in zip(
                        resumed_frames, expected):
                    self.assertEqual(
                        _frame_signature(resumed_frame),
                        _frame_signature(expected_frame))

    def test_resume_to_eof_checks_crc(self):
        """Resuming must restore the running CRC context as well"""
        path = _test_file('garmin-fenix-5-bike.fit')
        frames, index = _read_sequential(path, index_interval=50)

        entry = index.entries[len(index.entries) // 2]
        with fitdecode.FitReader(
                path,
                check_crc=fitdecode.CrcCheck.RAISE,
                resume_from=entry) as fit:
            resumed_frames = list(fit)

        # the last frame must be a valid CRC footer
        self.assertIsInstance(resumed_frames[-1], fitdecode.FitCRC)
        self.assertTrue(resumed_frames[-1].matched)

    def test_entry_before(self):
        path = _test_file('activity-small-fenix2-run.fit')
        frames, index = _read_sequential(path, index_interval=10)

        self.assertIsNone(index.entry_before(index.entries[0].offset - 1))

        for entry in index.entries:
            self.assertIs(index.entry_before(entry.offset), entry)
            self.assertIs(index.entry_before(entry.offset + 1), entry)

        self.assertIs(index.entry_before(10 ** 12), index.entries[-1])

    def test_resume_from_index_object(self):
        """A FitIndex can be passed directly: its last entry is used"""
        path = _test_file('activity-small-fenix2-run.fit')
        frames, index = _read_sequential(path, index_interval=20)

        with fitdecode.FitReader(
                path, keep_raw_chunks=True, resume_from=index) as fit:
            resumed_frames = list(fit)

        self.assertEqual(
            resumed_frames[0].chunk.offset, index.entries[-1].offset)

    def test_stale_index_is_rejected(self):
        """
        If the tail of the file is replaced (even with content of the same
        length), the old index must not silently match the new data
        """
        path = _test_file('activity-small-fenix2-run.fit')
        frames, index = _read_sequential(path, index_interval=10)
        entry = index.entries[len(index.entries) // 2]

        with open(path, 'rb') as fin:
            data = bytearray(fin.read())

        # replace the second half of the file, keeping the very same size
        mid = len(data) // 2
        data[mid:] = bytes((byte + 1) % 256 for byte in data[mid:])

        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.fit')
        try:
            with os.fdopen(tmp_fd, 'wb') as fout:
                fout.write(data)

            with self.assertRaises(fitdecode.FitIndexError):
                fitdecode.FitReader(tmp_path, resume_from=entry)
        finally:
            os.unlink(tmp_path)

    def test_truncated_stream_is_rejected(self):
        path = _test_file('activity-small-fenix2-run.fit')
        frames, index = _read_sequential(path, index_interval=10)

        with open(path, 'rb') as fin:
            data = fin.read()

        tmp_fd, tmp_path = tempfile.mkstemp(suffix='.fit')
        try:
            with os.fdopen(tmp_fd, 'wb') as fout:
                fout.write(data[:len(data) - 100])

            with self.assertRaises(fitdecode.FitIndexError):
                fitdecode.FitReader(tmp_path, resume_from=index.entries[-1])
        finally:
            os.unlink(tmp_path)

    def test_incomplete_index_is_rejected(self):
        path = _test_file('activity-small-fenix2-run.fit')

        fit = fitdecode.FitReader(
            path,
            check_crc=fitdecode.CrcCheck.RAISE,
            index_interval=5)
        try:
            for count, frame in enumerate(fit):
                if count > 20:
                    break

            self.assertGreater(len(fit.index.entries), 0)
            self.assertFalse(fit.index.complete)

            with self.assertRaises(fitdecode.FitIndexError):
                fitdecode.FitReader(path, resume_from=fit.index.entries[-1])
        finally:
            fit.close()

    def test_non_seekable_input_is_rejected(self):
        path = _test_file('activity-small-fenix2-run.fit')
        frames, index = _read_sequential(path, index_interval=10)

        with open(path, 'rb') as fin:
            with self.assertRaises(fitdecode.FitIndexError):
                fitdecode.FitReader(
                    NonSeekableWrapper(fin), index_interval=10)

        with open(path, 'rb') as fin:
            with self.assertRaises(fitdecode.FitIndexError):
                fitdecode.FitReader(
                    NonSeekableWrapper(fin),
                    resume_from=index.entries[0])

    def test_invalid_options(self):
        path = _test_file('activity-small-fenix2-run.fit')
        frames, index = _read_sequential(path, index_interval=10)

        with self.assertRaises(ValueError):
            fitdecode.FitReader(path, index_interval=0)

        with self.assertRaises(ValueError):
            fitdecode.FitReader(
                path, index_interval=10, resume_from=index.entries[0])

        with self.assertRaises(ValueError):
            fitdecode.FitReader(path, resume_from=object())


if __name__ == '__main__':
    unittest.main()
