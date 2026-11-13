# Copyright 2009-2013 Yelp and Contributors
# Copyright 2015-2017 Yelp
# Copyright 2019 Yelp
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Length-delimited frames, and migration from tab-delimited legacy data.

The frame format in this module is meant for byte streams whose records are
not safely expressible as lines.  A frame stores the exact length of its key
and value, so tabs, NUL bytes, newlines, and other delimiters inside either
field have no special meaning.

The format also distinguishes "there is no key" from "the key is empty", and
"there is no value" from "the value is empty"::

    encode_frame(None, b'value') -> no key, b'value' value
    encode_frame(b'', b'value')  -> empty key, b'value' value

Records produced by the old protocols can be migrated with
:py:func:`migrate_partition`.  A record is only migrated if its meaning is
unique given the partition's possible protocols, its known business keys, and
its calibration records.  Ambiguous records are left byte-for-byte intact,
together with every candidate interpretation.  Confirmed records can always
be encoded back to the exact bytes they came from, using
:py:meth:`MigrationItem.restore` or
:py:meth:`MigrationResult.restore_confirmed`.

Old protocols
-------------

``key_value``
    The first tab is the boundary between key and value.  A record with no
    tab is read as ``(record, None)``, matching
    :py:class:`mrjob.protocol.BytesProtocol`.

``value_only``
    The whole record is the value; the key is ``None``.  This matches
    :py:class:`mrjob.protocol.BytesValueProtocol`.

Both protocols are inverted by :py:func:`encode_legacy_record`, so any
confirmed migration is lossless.
"""
import struct
import zlib
from collections import namedtuple

from mrjob.py2 import PY2
from mrjob.py2 import string_types as _STRING_TYPES


#: Magic bytes at the start of every frame
FRAME_MAGIC = b'MRF'

#: Current frame format version
FRAME_VERSION = 1

#: Flag bit meaning the key is present (it may still be empty)
FRAME_HAS_KEY = 0x01

#: Flag bit meaning the value is present (it may still be empty)
FRAME_HAS_VALUE = 0x02

#: All flag bits understood by this version
_FRAME_FLAGS = (FRAME_HAS_KEY | FRAME_HAS_VALUE)

# magic, version, flags, key length, value length
_HEADER_WITHOUT_CRC = struct.Struct('>3sBBII')
# magic, version, flags, key length, value length, CRC-32
_FRAME_HEADER = struct.Struct('>3sBBIII')

#: Number of bytes in a frame header
FRAME_HEADER_SIZE = _FRAME_HEADER.size

#: Refuse to decode frames larger than this by default.  A guard keeps a
#: corrupt length field from allocating (or waiting for) gigabytes of data.
MAX_FRAME_SIZE = 1 << 30


class FrameError(Exception):
    """Base class for frame encoding/decoding errors."""


class IncompleteFrameError(FrameError):
    """Not enough bytes to finish a frame.

    :ivar data: the bytes that were available for this frame
    :ivar offset: where this frame starts in the original input
    :ivar needed: total number of bytes this frame needs, if known
    """

    def __init__(self, data=b'', offset=0, needed=None):
        self.data = bytes(data)
        self.offset = offset
        self.needed = needed

        if needed is None:
            message = 'incomplete frame at offset %d' % offset
        else:
            message = (
                'incomplete frame at offset %d (got %d bytes, need %d)' % (
                    offset, len(self.data), needed))

        super(IncompleteFrameError, self).__init__(message)


class CorruptFrameError(FrameError):
    """A complete frame header or payload is not valid.

    :ivar detail: the specific problem, without the offset prefix
    """

    def __init__(self, message, offset=0):
        self.detail = message
        self.offset = offset
        super(CorruptFrameError, self).__init__(
            'corrupt frame at offset %d: %s' % (offset, message))


class MigrationError(ValueError):
    """A legacy partition cannot be migrated as specified."""


class InvalidCalibrationError(MigrationError):
    """Calibration records contradict every candidate protocol."""


#: A decoded frame.  *raw* is the exact bytes of the frame, including its
#: header.  *key* and *value* are ``None`` when absent.
Frame = namedtuple('Frame', ['key', 'value', 'raw'])


def _as_bytes(value, name='value'):
    """Accept bytes and (unicode) strings, but reject other types."""
    if isinstance(value, bytes):
        return value
    elif isinstance(value, bytearray):
        return bytes(value)
    elif isinstance(value, str) and not PY2:
        return value.encode('utf_8')
    elif PY2 and isinstance(value, unicode):  # noqa: F821
        return value.encode('utf_8')
    else:
        raise TypeError('%s should be bytes or None, not %r' % (
            name, type(value)))


def _normalize_key_or_value(value, name):
    if value is None:
        return None
    return _as_bytes(value, name)


def encode_frame(key, value):
    """Encode *key* and *value* as a single length-delimited frame.

    Either argument may be ``None`` to mean "absent".  Empty bytestrings
    are distinct from ``None``: an absent field has its flag turned off,
    while an empty field has its flag turned on and a length of zero.

    :param key: key bytes, empty bytes, or ``None``
    :param value: value bytes, empty bytes, or ``None``
    :rtype: bytes
    """
    key = _normalize_key_or_value(key, 'key')
    value = _normalize_key_or_value(value, 'value')

    flags = 0
    key_length = 0
    value_length = 0

    if key is not None:
        flags |= FRAME_HAS_KEY
        key_length = len(key)
    if value is not None:
        flags |= FRAME_HAS_VALUE
        value_length = len(value)

    header_without_crc = _HEADER_WITHOUT_CRC.pack(
        FRAME_MAGIC, FRAME_VERSION, flags, key_length, value_length)

    checksum = _checksum(header_without_crc, key, value)

    return _FRAME_HEADER.pack(
        FRAME_MAGIC, FRAME_VERSION, flags, key_length, value_length,
        checksum) + (key or b'') + (value or b'')


def _checksum(header_without_crc, key, value):
    checksum = zlib.crc32(header_without_crc)
    if key:
        checksum = zlib.crc32(key, checksum)
    if value:
        checksum = zlib.crc32(value, checksum)
    return checksum & 0xffffffff


def frame_length(key, value):
    """Return the number of bytes :py:func:`encode_frame` would produce."""
    key = _normalize_key_or_value(key, 'key')
    value = _normalize_key_or_value(value, 'value')
    return (FRAME_HEADER_SIZE +
            (len(key) if key is not None else 0) +
            (len(value) if value is not None else 0))


def decode_frame(data, offset=0, max_frame_size=MAX_FRAME_SIZE):
    """Decode one frame at *offset*.

    :return: ``(frame, next_offset)``, where *next_offset* points just
    past the frame.
    :raises IncompleteFrameError: if *data* ends in the middle of a frame
    :raises CorruptFrameError: if a complete header or payload is invalid
    """
    data = _as_bytes(data, 'data')

    if not 0 <= offset <= len(data):
        raise ValueError('offset out of range: %r' % offset)

    available = len(data) - offset

    if available < FRAME_HEADER_SIZE:
        raise IncompleteFrameError(
            data[offset:], offset=offset, needed=FRAME_HEADER_SIZE)

    (magic, version, flags, key_length,
     value_length, checksum) = _FRAME_HEADER.unpack_from(data, offset)

    if magic != FRAME_MAGIC:
        raise CorruptFrameError('bad magic %r' % magic, offset=offset)
    if version != FRAME_VERSION:
        raise CorruptFrameError('unknown version %d' % version,
                                offset=offset)
    if flags & ~_FRAME_FLAGS:
        raise CorruptFrameError('unknown flags 0x%02x' % flags,
                                offset=offset)
    if not flags & FRAME_HAS_KEY and key_length:
        raise CorruptFrameError(
            'key length is nonzero but key is absent', offset=offset)
    if not flags & FRAME_HAS_VALUE and value_length:
        raise CorruptFrameError(
            'value length is nonzero but value is absent', offset=offset)

    total_length = FRAME_HEADER_SIZE + key_length + value_length

    if total_length > max_frame_size:
        raise CorruptFrameError(
            'frame length %d exceeds limit %d' % (
                total_length, max_frame_size),
            offset=offset)

    if available < total_length:
        raise IncompleteFrameError(
            data[offset:], offset=offset, needed=total_length)

    raw = data[offset:offset + total_length]
    header_without_crc = raw[:_HEADER_WITHOUT_CRC.size]
    payload_start = FRAME_HEADER_SIZE
    key = raw[payload_start:payload_start + key_length]
    value = raw[payload_start + key_length:total_length]

    expected_checksum = _checksum(
        header_without_crc,
        key if flags & FRAME_HAS_KEY else None,
        value if flags & FRAME_HAS_VALUE else None)

    if checksum != expected_checksum:
        raise CorruptFrameError(
            'checksum mismatch (expected 0x%08x, got 0x%08x)' % (
                expected_checksum, checksum),
            offset=offset)

    return (
        Frame(
            key=key if flags & FRAME_HAS_KEY else None,
            value=value if flags & FRAME_HAS_VALUE else None,
            raw=raw),
        offset + total_length)


def split_frames(data, max_frame_size=MAX_FRAME_SIZE):
    """Decode all complete frames in *data*.

    Returns ``(frames, trailing)``.  *trailing* is the bytes at the end of
    *data* that are not yet a complete frame.  It is never merged into the
    preceding frame, so a truncated tail cannot destroy a complete record.

    A complete but invalid frame raises :py:class:`CorruptFrameError`.
    """
    data = _as_bytes(data, 'data')
    frames = []
    offset = 0

    while offset < len(data):
        try:
            frame, offset = decode_frame(
                data, offset, max_frame_size=max_frame_size)
        except IncompleteFrameError:
            return frames, data[offset:]

        frames.append(frame)

    return frames, b''


def iter_frames(data, max_frame_size=MAX_FRAME_SIZE):
    """Yield every complete frame in *data*.

    A truncated final frame is ignored (use :py:func:`split_frames` or
    :py:class:`FrameDecoder` if you need to inspect it).  Frames are decoded
    from the beginning, so records before the truncation are still yielded.
    """
    data = _as_bytes(data, 'data')
    offset = 0

    while offset < len(data):
        try:
            frame, offset = decode_frame(
                data, offset, max_frame_size=max_frame_size)
        except IncompleteFrameError:
            return

        yield frame


def read_frames(fileobj, chunk_size=65536, max_frame_size=MAX_FRAME_SIZE,
                strict=False):
    """Yield complete frames from a binary file object.

    Reads incrementally, so memory use is bounded by *chunk_size* plus the
    largest frame.  At end of stream a partial frame is ignored by default;
    set *strict* to raise :py:class:`IncompleteFrameError` instead.  Frames
    before the partial tail are always yielded.
    """
    if chunk_size <= 0:
        raise ValueError('chunk_size must be positive')

    decoder = FrameDecoder(max_frame_size=max_frame_size)

    while True:
        chunk = fileobj.read(chunk_size)
        if not chunk:
            break

        for frame in decoder.feed(chunk):
            yield frame

    decoder.finish(strict=strict)


class FrameDecoder(object):
    """Incremental length-delimited frame decoder.

    Feed arbitrary chunks with :py:meth:`feed`; complete frames are returned
    immediately.  Call :py:meth:`finish` at end of stream to get any
    incomplete tail.  Frames returned before a truncated tail are unaffected
    by it.
    """

    def __init__(self, max_frame_size=MAX_FRAME_SIZE):
        self._buffer = b''
        self._offset = 0
        self._max_frame_size = max_frame_size

    def feed(self, data):
        """Add bytes to the decoder, and return a list of complete frames.

        :raises CorruptFrameError: if a complete frame is invalid
        """
        data = _as_bytes(data, 'data')
        if data:
            self._buffer += data

        frames = []

        while self._buffer:
            try:
                frame, next_offset = decode_frame(
                    self._buffer, 0, max_frame_size=self._max_frame_size)
            except IncompleteFrameError:
                break
            except CorruptFrameError as error:
                raise CorruptFrameError(
                    error.detail, offset=self._offset + error.offset)

            frames.append(frame)
            self._offset += next_offset
            self._buffer = self._buffer[next_offset:]

        return frames

    @property
    def buffered_bytes(self):
        """Number of bytes held back waiting for the rest of a frame."""
        return len(self._buffer)

    @property
    def offset(self):
        """Offset in the overall stream where the buffered bytes start."""
        return self._offset

    def finish(self, strict=False):
        """End the stream.

        Returns the incomplete trailing bytes (or ``b''``).  If *strict* is
        true, raise :py:class:`IncompleteFrameError` instead when a
        partial frame remains.
        """
        trailing = self._buffer
        self._buffer = b''
        self._offset += len(trailing)

        if trailing and strict:
            raise IncompleteFrameError(trailing)

        return trailing


# --- legacy protocols ---

#: First tab separates key and value
KEY_VALUE = 'key_value'

#: Whole record is the value, with no key
VALUE_ONLY = 'value_only'

#: The legacy protocols understood by this module
LEGACY_PROTOCOLS = (KEY_VALUE, VALUE_ONLY)

_PROTOCOL_ALIASES = {
    'key_value': KEY_VALUE,
    'key-value': KEY_VALUE,
    'keyvalue': KEY_VALUE,
    'kv': KEY_VALUE,
    'tab': KEY_VALUE,
    'bytes': KEY_VALUE,
    'value_only': VALUE_ONLY,
    'value-only': VALUE_ONLY,
    'valueonly': VALUE_ONLY,
    'value': VALUE_ONLY,
    'raw': VALUE_ONLY,
    'bytes_value': VALUE_ONLY,
}


def normalize_legacy_protocol(protocol):
    """Return :py:data:`KEY_VALUE` or :py:data:`VALUE_ONLY` for *protocol*.

    Accepts the constants themselves and a few common aliases.
    """
    if protocol in (KEY_VALUE, VALUE_ONLY):
        return protocol
    elif isinstance(protocol, bytes):
        # e.g. an accidental b'key_value'
        try:
            protocol = protocol.decode('ascii')
        except UnicodeDecodeError:
            pass
    if isinstance(protocol, str):
        try:
            return _PROTOCOL_ALIASES[protocol.lower()]
        except KeyError:
            pass

    raise ValueError('unknown legacy protocol: %r' % (protocol,))


def decode_legacy_record(raw, protocol):
    """Decode a legacy record into ``(key, value)``.

    For ``key_value``, split on the first tab; a record with no tab has a
    value of ``None``.  For ``value_only``, the entire record is the value
    and the key is ``None``.
    """
    raw = _as_bytes(raw, 'raw')
    protocol = normalize_legacy_protocol(protocol)

    if protocol == VALUE_ONLY:
        return None, raw

    key, separator, value = raw.partition(b'\t')
    if not separator:
        return key, None
    else:
        return key, value


def encode_legacy_record(protocol, key, value):
    """Encode a ``(key, value)`` pair the way the legacy protocol did.

    This is the inverse of :py:func:`decode_legacy_record` for every
    confirmed migration, and is used to prove that migration is lossless.
    """
    protocol = normalize_legacy_protocol(protocol)
    key = _normalize_key_or_value(key, 'key')
    value = _normalize_key_or_value(value, 'value')

    if protocol == VALUE_ONLY:
        if value is None:
            raise ValueError('value_only record has no value')
        return value

    if key is None:
        return value if value is not None else b''
    elif value is None:
        return key
    else:
        return key + b'\t' + value


class LegacyCandidate(object):
    """One possible interpretation of an ambiguous legacy record.

    :ivar protocol: :py:data:`KEY_VALUE` or :py:data:`VALUE_ONLY`
    :ivar key: key bytes, or ``None``
    :ivar value: value bytes, or ``None``
    """

    __slots__ = ('protocol', 'key', 'value')

    def __init__(self, protocol, key, value):
        self.protocol = normalize_legacy_protocol(protocol)
        self.key = _normalize_key_or_value(key, 'key')
        self.value = _normalize_key_or_value(value, 'value')

    @property
    def pair(self):
        return (self.key, self.value)

    def __eq__(self, other):
        return (
            isinstance(other, LegacyCandidate) and
            self.protocol == other.protocol and
            self.pair == other.pair)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.protocol, self.pair))

    def __repr__(self):
        return 'LegacyCandidate(%r, key=%r, value=%r)' % (
            self.protocol, self.key, self.value)


class Calibration(object):
    """A legacy record whose expected interpretation is known.

    Calibrations serve two purposes: a calibration record is itself a
    confirmed input, and a set of calibrations can pin down which old
    protocol a partition used.

    :ivar raw: exact legacy bytes
    :ivar key: expected key (``None`` when the record has no key)
    :ivar value: expected value (``None`` when the record has no value)
    """

    __slots__ = ('raw', 'key', 'value')

    def __init__(self, raw, key, value):
        self.raw = _as_bytes(raw, 'raw')
        self.key = _normalize_key_or_value(key, 'key')
        self.value = _normalize_key_or_value(value, 'value')

    @property
    def pair(self):
        return (self.key, self.value)

    def __eq__(self, other):
        return (
            isinstance(other, Calibration) and
            self.raw == other.raw and
            self.pair == other.pair)

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash((self.raw, self.pair))

    def __repr__(self):
        return 'Calibration(%r, key=%r, value=%r)' % (
            self.raw, self.key, self.value)


def _normalize_calibration(calibration):
    if isinstance(calibration, Calibration):
        return calibration
    elif isinstance(calibration, (tuple, list)):
        if len(calibration) == 3:
            return Calibration(*calibration)
        elif len(calibration) == 2:
            expected = calibration[1]
            if (isinstance(expected, (tuple, list)) and
                    len(expected) == 2):
                return Calibration(
                    calibration[0], expected[0], expected[1])

    raise TypeError(
        'calibration should be Calibration or (raw, key, value), not %r' % (
            calibration,))


class PartitionSpec(object):
    """What is known about the old protocol used by one partition.

    :param protocols: sequence of possible legacy protocols.  For example,
    ``(KEY_VALUE, VALUE_ONLY)`` if you genuinely do not know which one a
    partition used.  Must not be empty.
    :param known_keys: business keys that may appear as the key in this
    partition.  Empty keys are ignored (an empty key cannot disambiguate
    "empty key" from "no key").
    :param calibrations: a few records with known interpretations.  If
    exactly one candidate protocol is consistent with all calibrations,
    that protocol is pinned for the partition.

    :ivar protocols: normalized tuple of protocols
    :ivar known_keys: frozenset of nonempty key bytes
    :ivar calibrations: tuple of :py:class:`Calibration`
    :ivar resolved_protocol: pinned protocol, or ``None`` if still ambiguous
    """

    def __init__(self, protocols=(KEY_VALUE,), known_keys=(),
                 calibrations=()):
        if isinstance(protocols, (bytes,) + _STRING_TYPES):
            protocols = (protocols,)
        else:
            try:
                protocols = tuple(protocols)
            except TypeError:
                protocols = (protocols,)

        normalized = []
        for protocol in protocols:
            protocol = normalize_legacy_protocol(protocol)
            if protocol not in normalized:
                normalized.append(protocol)

        if not normalized:
            raise ValueError('PartitionSpec needs at least one protocol')

        self.protocols = tuple(normalized)

        keys = set()
        for key in known_keys or ():
            key = _normalize_key_or_value(key, 'known key')
            if key:  # ignore empty keys, and ignore None above
                keys.add(key)
        self.known_keys = frozenset(keys)

        self.calibrations = tuple(
            _normalize_calibration(c) for c in calibrations or ())

        by_raw = {}
        for calibration in self.calibrations:
            previous = by_raw.get(calibration.raw)
            if previous is not None and previous.pair != calibration.pair:
                raise InvalidCalibrationError(
                    'contradictory calibrations for %r: %r and %r' % (
                        calibration.raw, previous, calibration))
            by_raw[calibration.raw] = calibration

        self.resolved_protocol = self._resolve_protocol(
            self.protocols, self.calibrations)

    @staticmethod
    def _resolve_protocol(protocols, calibrations):
        if not calibrations:
            return protocols[0] if len(protocols) == 1 else None

        matching = []
        for protocol in protocols:
            if all(decode_legacy_record(c.raw, protocol) == c.pair
                   for c in calibrations):
                matching.append(protocol)

        if not matching:
            raise InvalidCalibrationError(
                'no candidate protocol is consistent with all calibrations: '
                '%r' % (calibrations,))
        elif len(matching) == 1:
            return matching[0]
        else:
            return None

    @property
    def is_resolved(self):
        return self.resolved_protocol is not None

    def __repr__(self):
        return (
            'PartitionSpec(protocols=%r, known_keys=%r, calibrations=%r)' % (
                self.protocols, sorted(self.known_keys), self.calibrations))


# --- migration ---

CONFIRMED = 'confirmed'
AMBIGUOUS = 'ambiguous'


class MigrationItem(object):
    """The result of migrating one legacy record.

    :ivar raw: the original record bytes, unchanged
    :ivar status: :py:data:`CONFIRMED` or :py:data:`AMBIGUOUS`
    :ivar protocol: original protocol for a confirmed item (``None`` if it
    could not be pinned)
    :ivar key: decoded key for a confirmed item
    :ivar value: decoded value for a confirmed item
    :ivar candidates: tuple of :py:class:`LegacyCandidate` for every
    interpretation that is consistent with the record
    :ivar reason: why the record is ambiguous (``None`` when confirmed)
    """

    __slots__ = ('raw', 'status', 'protocol', 'key', 'value',
                 'candidates', 'reason')

    def __init__(self, raw, status, protocol=None, key=None, value=None,
                 candidates=(), reason=None):
        self.raw = _as_bytes(raw, 'raw')
        self.status = status
        self.protocol = protocol
        self.key = _normalize_key_or_value(key, 'key')
        self.value = _normalize_key_or_value(value, 'value')
        self.candidates = tuple(candidates)
        self.reason = reason

    @property
    def is_confirmed(self):
        return self.status == CONFIRMED

    @property
    def is_ambiguous(self):
        return self.status == AMBIGUOUS

    @property
    def pair(self):
        """``(key, value)`` for a confirmed item."""
        if not self.is_confirmed:
            raise ValueError(
                'ambiguous record has no single (key, value) pair: %r' % (
                    self,))
        return (self.key, self.value)

    def encode_frame(self):
        """Encode this confirmed record as a new-style frame.

        :raises ValueError: if this item is ambiguous
        """
        if not self.is_confirmed:
            raise ValueError('cannot frame an ambiguous record: %r' % (self,))

        return encode_frame(self.key, self.value)

    def restore(self):
        """Re-encode this confirmed record exactly as the old protocol did.

        :raises ValueError: if this item is ambiguous
        :return: original bytes (identical to :py:attr:`raw`)
        """
        if not self.is_confirmed:
            raise ValueError(
                'cannot restore an ambiguous record: %r' % (self,))

        return encode_legacy_record(self.protocol, self.key, self.value)

    def __repr__(self):
        if self.is_confirmed:
            return 'MigrationItem(confirmed, key=%r, value=%r)' % (
                self.key, self.value)
        else:
            return 'MigrationItem(ambiguous, raw=%r, candidates=%r)' % (
                self.raw, self.candidates)


class MigrationResult(object):
    """The result of :py:func:`migrate_partition`.

    :ivar items: ordered tuple of :py:class:`MigrationItem`
    :ivar spec: the :py:class:`PartitionSpec` used
    """

    __slots__ = ('items', 'spec')

    def __init__(self, items, spec):
        self.items = tuple(items)
        self.spec = spec

    @property
    def confirmed(self):
        """Confirmed items, in input order."""
        return tuple(item for item in self.items if item.is_confirmed)

    @property
    def ambiguous(self):
        """Ambiguous items, in input order."""
        return tuple(item for item in self.items if item.is_ambiguous)

    @property
    def confirmed_records(self):
        return tuple(item.raw for item in self.confirmed)

    @property
    def ambiguous_records(self):
        return tuple(item.raw for item in self.ambiguous)

    def frames(self):
        """Return the new framed bytes for all confirmed records, in order."""
        return b''.join(item.encode_frame() for item in self.confirmed)

    def restore_confirmed(self):
        """Return original bytes for every confirmed record, in order.

        Each value is the exact input record, encoded with the protocol
        recorded on its :py:class:`MigrationItem`.
        """
        return tuple(item.restore() for item in self.confirmed)

    def restore_all(self):
        """Return original bytes for every record, in input order.

        Confirmed records are re-encoded with their recorded protocol;
        ambiguous records are returned as the exact bytes they arrived as.
        """
        return tuple(
            item.restore() if item.is_confirmed else item.raw
            for item in self.items)

    def verify_restorable(self):
        """Return ``True`` if every confirmed record restores to its input.

        The migration code already enforces this; this method lets callers
        assert it as an end-to-end check.
        """
        return all(item.restore() == item.raw for item in self.confirmed)

    def ambiguous_contents(self):
        """Tuple of ``(raw, candidates, reason)`` for ambiguous records."""
        return tuple(
            (item.raw, item.candidates, item.reason)
            for item in self.ambiguous)

    def summary(self):
        return {
            'total': len(self.items),
            'confirmed': len(self.confirmed),
            'ambiguous': len(self.ambiguous),
            'resolved_protocol': self.spec.resolved_protocol,
        }


def split_legacy_records(data, newline=b'\n'):
    """Split line-oriented legacy partition bytes into records.

    Returns ``(records, trailing)``.  If the input does not end in *newline*,
    the final segment is returned as *trailing* rather than as a complete
    record.  This keeps a half-written last record from being mistaken for,
    or merged with, the record before it.

    The returned record bytes do not include the terminator.
    """
    data = _as_bytes(data, 'data')
    newline = _as_bytes(newline, 'newline')

    if not newline:
        raise ValueError('newline must not be empty')

    if not data:
        return [], b''

    segments = data.split(newline)

    if data.endswith(newline):
        # split() produces a final b'', which is an artifact of the terminator
        return segments[:-1], b''
    else:
        return segments[:-1], segments[-1]


def migrate_partition_stream(data, spec=None, newline=b'\n',
                             final_record_complete=False):
    """Migrate complete records in line-oriented legacy partition bytes.

    The incomplete final segment (if any) is returned separately and is
    neither migrated nor attached to the preceding record.

    :param final_record_complete: set to ``True`` only when the caller has
    independent evidence that the partition ended cleanly (for example a
    stored byte count that includes the last record).  Then a final segment
    without a terminator is treated as complete.  Do not use this for data
    that may have been cut mid-write.
    :return: ``(MigrationResult, trailing_bytes)``
    """
    records, trailing = split_legacy_records(data, newline=newline)

    if trailing and final_record_complete:
        records.append(trailing)
        trailing = b''

    return migrate_partition(records, spec=spec), trailing


def _candidate_for_calibration(calibration, protocols):
    for protocol in protocols:
        if decode_legacy_record(calibration.raw, protocol) == calibration.pair:
            return LegacyCandidate(
                protocol, calibration.key, calibration.value)
    return None


def _distinct_pairs(candidates):
    pairs = []
    for candidate in candidates:
        if candidate.pair not in pairs:
            pairs.append(candidate.pair)
    return pairs


def _ambiguity_reason(spec, candidates):
    has_empty_key = any(c.key == b'' for c in candidates)
    has_missing_key = any(c.key is None for c in candidates)

    if has_empty_key and has_missing_key:
        return 'empty key or no key'
    elif len(_distinct_pairs(candidates)) > 1:
        return 'possible protocols disagree'
    else:
        return 'known business keys do not identify the record'


def _select_confirmed_candidate(raw, spec, candidates):
    """Return ``(candidate_or_None, reason)``.

    A candidate is unique if there is exactly one semantic ``(key, value)``
    pair among all possible protocols, or if the known business keys single
    out exactly one pair.  In either case the chosen protocol must encode
    back to *raw* exactly.
    """
    pairs = _distinct_pairs(candidates)

    if len(pairs) == 1:
        selected_pairs = pairs
    else:
        known_keys = spec.known_keys
        known_candidates = [
            c for c in candidates
            if c.key is not None and c.key in known_keys
        ]
        known_pairs = _distinct_pairs(known_candidates)

        if len(known_pairs) == 1:
            selected_pairs = known_pairs
        else:
            return None, _ambiguity_reason(spec, candidates)

    for candidate in candidates:
        if candidate.pair in selected_pairs:
            try:
                restored = encode_legacy_record(
                    candidate.protocol, candidate.key, candidate.value)
            except (TypeError, ValueError):
                continue

            if restored == raw:
                return candidate, None

    return None, 'encoding does not round-trip'


def migrate_partition(records, spec=None):
    """Migrate legacy records from one partition.

    *records* is an iterable of exact legacy record bytes (without line
    terminators).  *spec* is a :py:class:`PartitionSpec`; by default it
    assumes key/value records only.

    Confirmed records get a new-style frame; ambiguous records are returned
    unchanged, with all their candidate interpretations.  A record is never
    guessed merely because it looks plausible.

    :return: :py:class:`MigrationResult`
    """
    if spec is None:
        spec = PartitionSpec()

    elif not isinstance(spec, PartitionSpec):
        raise TypeError('spec should be a PartitionSpec, not %r' % (spec,))

    calibration_by_raw = dict((c.raw, c) for c in spec.calibrations)

    items = []

    for raw in records:
        raw = _as_bytes(raw, 'record')

        if spec.resolved_protocol is not None:
            candidates = [
                LegacyCandidate(
                    spec.resolved_protocol,
                    *decode_legacy_record(raw, spec.resolved_protocol))
            ]
        else:
            candidates = [
                LegacyCandidate(protocol, *decode_legacy_record(raw, protocol))
                for protocol in spec.protocols
            ]

        calibration = calibration_by_raw.get(raw)
        if calibration is not None:
            calibration_candidate = _candidate_for_calibration(
                calibration, spec.protocols)

            if calibration_candidate is not None:
                selected, reason = _select_confirmed_candidate(
                    raw, spec, [calibration_candidate])
            else:
                selected, reason = (None, 'calibration cannot be decoded')
        else:
            selected, reason = _select_confirmed_candidate(
                raw, spec, candidates)

        if selected is None:
            items.append(MigrationItem(
                raw,
                AMBIGUOUS,
                candidates=candidates,
                reason=reason))
        else:
            items.append(MigrationItem(
                raw,
                CONFIRMED,
                protocol=selected.protocol,
                key=selected.key,
                value=selected.value,
                candidates=candidates))

    return MigrationResult(items, spec)


def migrate_partitions(partitions, newline=b'\n'):
    """Migrate several partitions independently.

    *partitions* is a sequence of ``(spec, data)`` pairs, where *data* is
    line-oriented legacy bytes for that partition.  Each partition may have
    its own protocol and its own calibrations.

    :return: a list of ``(MigrationResult, trailing_bytes)`` in input order.
    """
    results = []

    for partition in partitions:
        try:
            spec, data = partition
        except (TypeError, ValueError):
            raise TypeError(
                'partition should be a (spec, data) pair, not %r' % (
                    partition,))

        results.append(migrate_partition_stream(
            data, spec=spec, newline=newline))

    return results
