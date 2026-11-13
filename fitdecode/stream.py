# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

"""
Incremental segmentation of a stream made of one or more concatenated FIT
files (a.k.a. *chained FIT files*).

Unlike `FitReader`, which decodes the frames of a single already-complete
stream, this module does not interpret FIT messages. Its sole responsibility is
to locate the boundaries of the FIT files carried by an arbitrary byte stream
that arrives piece by piece, and to tell apart three situations for every
candidate *segment*:

* `SegmentState.COMPLETE`: a whole FIT file (header + body + CRC footer) is
  available and its CRC matches, so it can be handed over to a `FitReader` or
  persisted as-is;
* `SegmentState.INCOMPLETE`: a FIT header was recognized but the declared body
  and/or CRC footer has not been received yet, so more bytes are needed before
  anything can be committed;
* `SegmentState.CORRUPT`: a candidate span was fully received but failed its
  CRC check, so it must not be treated as a valid FIT file. Recovery simply
  resumes from the next byte, which means a byte sequence that merely *looks*
  like a FIT header (e.g. the ``.FIT`` magic appearing inside corrupt data) can
  never be announced as a valid file on its own: the CRC of the whole span has
  to match first.

Every reported segment carries the absolute source offset at which it starts
(and, by way of consequence, its ending boundary), so each commit can be
audited against the original stream. Already-validated bytes are consumed once
and never emitted again, therefore the receiver does not have to re-receive the
segments it already acknowledged.
"""

import enum
import struct

from . import utils
from .records import FitHeader

__all__ = [
    'FIT_HEADER_MAGIC', 'FIT_FIXED_HEADER_SIZE', 'FIT_EXTENDED_HEADER_SIZE',
    'FIT_CRC_SIZE', 'SegmentState', 'FitSegment', 'FitSegmentResult',
    'FitStreamSegmenter']

#: The 4-byte magic signature found at offset 8 of every FIT header.
FIT_HEADER_MAGIC = b'.FIT'

#: Size of the fixed part of a FIT header (i.e. without the optional CRC field).
FIT_FIXED_HEADER_SIZE = 12

#: Size of the optional, CRC-bearing extension of a FIT header.
FIT_EXTENDED_HEADER_SIZE = 2

#: Size of the CRC footer that terminates every FIT file.
FIT_CRC_SIZE = 2

#: The only header sizes allowed by the FIT specification.
_LEGAL_HEADER_SIZES = frozenset((
    FIT_FIXED_HEADER_SIZE,
    FIT_FIXED_HEADER_SIZE + FIT_EXTENDED_HEADER_SIZE))

#: Number of trailing bytes to keep while hunting for a header so that a header
#: straddling two feeds is never split incorrectly.
_HEADER_LOOKBEHIND = FIT_FIXED_HEADER_SIZE - 1

_STRUCT_FIXED_HEADER = struct.Struct('<2BHI4s')
_STRUCT_UINT16 = struct.Struct('<H')


class SegmentState(enum.Enum):
    """
    The state of a `FitSegment` discovered by `FitStreamSegmenter`.
    """

    #: A complete, CRC-validated FIT file is available.
    COMPLETE = 'complete'

    #: A FIT header was found but the declared body and/or CRC footer is still
    #: missing. More bytes are required before this segment can be committed.
    INCOMPLETE = 'incomplete'

    #: The candidate span was received in full but its CRC did not match. The
    #: bytes it covers are not a valid FIT file.
    CORRUPT = 'corrupt'


class FitSegment:
    """
    Description of a FIT file candidate located inside a byte stream.

    A segment is identified by its absolute source boundaries:
    :attr:`offset` (included) and :attr:`end_offset` (excluded). It also keeps
    the parsed `FitHeader` (when one could be read) together with the CRC
    information, so callers can audit why a segment was accepted or rejected.
    """

    __slots__ = (
        'state', 'offset', 'header_size', 'body_size', 'total_size',
        'header', 'crc_read', 'crc_matched')

    def __init__(
            self, state, offset, header_size, body_size, total_size,
            header=None, crc_read=None, crc_matched=None):
        #: current `SegmentState` of this segment
        self.state = state
        #: absolute source offset at which this segment starts (included)
        self.offset = offset
        #: size of the FIT header in bytes (0 for a header-less corrupt gap)
        self.header_size = header_size
        #: declared size of the FIT body in bytes (excludes header and CRC)
        self.body_size = body_size
        #: total number of bytes this segment spans once complete
        #: (header + body + CRC footer)
        self.total_size = total_size
        #: parsed `FitHeader` object, or `None` for a header-less corrupt gap
        self.header = header
        #: CRC value read from the footer, or `None` if not received yet
        self.crc_read = crc_read
        #: whether the footer CRC matched the computed one (`None` if unknown)
        self.crc_matched = crc_matched

    @property
    def end_offset(self):
        """Absolute source offset at which this segment ends (excluded)."""
        return self.offset + self.total_size

    def __repr__(self):
        return (
            f'{self.__class__.__name__}('
            f'state={self.state.value!r}, offset={self.offset}, '
            f'end_offset={self.end_offset}, total_size={self.total_size})')


class FitSegmentResult:
    """
    A `FitSegment` together with the raw bytes that were available for it at the
    time it was reported.

    ``data`` is a `bytes` object only when the segmenter was created with
    ``keep_raw_chunks=True``; otherwise it is `None` and only the boundaries
    are reported. Note that for an `~SegmentState.INCOMPLETE` segment, ``data``
    (when present) only contains the bytes received so far, which may be fewer
    than :attr:`FitSegment.total_size`.
    """

    __slots__ = ('segment', 'data')

    def __init__(self, segment, data=None):
        #: the `FitSegment` describing source offset and boundaries
        self.segment = segment
        #: raw bytes of the segment (`bytes`), or `None`
        self.data = data

    @property
    def state(self):
        """Shortcut for ``self.segment.state``."""
        return self.segment.state

    @property
    def offset(self):
        """Shortcut for ``self.segment.offset``."""
        return self.segment.offset

    @property
    def end_offset(self):
        """Shortcut for ``self.segment.end_offset``."""
        return self.segment.end_offset

    def __repr__(self):
        return (
            f'{self.__class__.__name__}('
            f'state={self.state.value!r}, offset={self.offset}, '
            f'end_offset={self.end_offset})')


class FitStreamSegmenter:
    """
    Incrementally split a stream of chained FIT files into validated segments.

    Feed incoming bytes with `consume()` each time a chunk of the stream
    arrives. That method returns the `FitSegmentResult` objects that became
    finalized during the call (either `~SegmentState.COMPLETE` or
    `~SegmentState.CORRUPT`), in source order. A segment whose header was read
    but whose body and/or CRC footer are still pending is *not* returned: it is
    retained internally until enough bytes are available. Call `flush()` after
    feeding everything you currently have to inspect such a pending segment
    (reported as `~SegmentState.INCOMPLETE`).

    Already-consumed bytes are retired from the internal buffer as soon as they
    are accounted for, so a previously acknowledged segment is never emitted a
    second time: the receiver only has to send the missing tail of the stream
    to resume.

    *keep_raw_chunks* enables the ``data`` attribute of the returned
    `FitSegmentResult` objects. It is disabled by default to save memory since
    source offsets and boundaries are always provided regardless.

    A `FitStreamSegmenter` is independent of any `FitReader`: once a complete
    segment has been reported, its raw bytes (or the corresponding slice of the
    original storage) can be passed to a regular `FitReader` for decoding.
    """

    def __init__(self, *, keep_raw_chunks=False):
        self._keep_raw = bool(keep_raw_chunks)

        # bytes not accounted for yet; self._abs_offset is the absolute stream
        # position of self._buffer[0]
        self._buffer = b''
        self._abs_offset = 0

        # the segment currently being assembled, or None when hunting a header
        self._segment = None

        # bookkeeping of an ongoing run of bytes that cannot belong to any valid
        # FIT file (a failed-CRC candidate and/or plain garbage). Corrupt bytes
        # are reported as non-overlapping increments: *_reported_end is the
        # absolute offset up to which results have already been emitted, so the
        # same byte is never announced twice.
        self._corrupt_start = None
        self._corrupt_reported_end = None
        self._corrupt_data = None

        # terminal flag set by close(); once true no more byte may be fed
        self._closed = False

    # ------------------------------------------------------------------ public

    @property
    def offset(self):
        """
        Absolute stream offset of the next byte that has not been accounted for
        yet (i.e. the position right after the last retired byte).
        """
        return self._abs_offset

    @property
    def buffered_size(self):
        """Number of bytes currently held while waiting for more input."""
        return len(self._buffer)

    @property
    def pending_segment(self):
        """
        The `FitSegment` that is currently waiting for more bytes, or `None`.

        A returned segment is always in `SegmentState.INCOMPLETE` state.
        """
        return self._segment

    def consume(self, data):
        """
        Feed a new chunk of the source stream and return the segments that got
        finalized as a list of `FitSegmentResult` objects.

        *data* may be any bytes-like object. An empty input returns an empty
        list. The pending, not-yet-complete segment (if any) is never returned
        here; use `flush()` to inspect it.

        Raises `ValueError` if this segmenter has been `close()`-d.
        """
        if self._closed:
            raise ValueError('cannot consume() after close()')
        if not data:
            return []

        self._buffer += bytes(data)
        results = []

        while self._buffer:
            if self._segment is None:
                # A fixed header is required at minimum to recognize a candidate.
                if len(self._buffer) < FIT_FIXED_HEADER_SIZE:
                    break

                position = self._find_header_candidate()
                if position is None:
                    # No candidate in the fully-available region. Retire the
                    # leading bytes as a corrupt gap but keep a small lookbehind
                    # so a header straddling the feed boundary is not missed.
                    # The run is only reported once it ends (or via flush/close).
                    self._begin_corrupt_run()
                    retire_count = len(self._buffer) - min(
                        _HEADER_LOOKBEHIND, len(self._buffer))
                    if not retire_count:
                        break
                    self._retire(retire_count)
                    continue

                if position > 0:
                    # Bytes before the candidate cannot start a valid FIT file.
                    self._begin_corrupt_run()
                    self._retire(position)
                    continue

                # A candidate header is sitting at the front of the buffer.
                # The fixed 12-byte part is available, which is enough to know
                # the declared body size and therefore the segment boundaries.
                # The optional 2-byte header CRC may still be in transit.
                #
                # Any ongoing corrupt run is intentionally left open here: it is
                # only closed once a candidate actually passes its CRC check, so
                # a candidate that later fails merges seamlessly back into the
                # same corrupt run instead of splitting it in two.
                self._segment = self._build_segment()

            segment = self._segment

            # A 14-byte header may have been only partially available when the
            # segment was created. Refresh its header CRC as soon as the two
            # extension bytes arrive.
            if (segment.header_size == FIT_FIXED_HEADER_SIZE
                    + FIT_EXTENDED_HEADER_SIZE
                    and len(self._buffer) >= segment.header_size):
                self._refresh_header_crc(segment)

            # A present-but-mismatching header CRC proves the header itself is
            # corrupt (a null header CRC means "absent", so it is ignored).
            # Reject such a candidate right away instead of waiting for a
            # potentially huge declared body, and resume from the next byte.
            if segment.header.crc_matched is False:
                self._segment = None
                self._begin_corrupt_run()
                self._retire(1)
                continue

            if len(self._buffer) < segment.total_size:
                # Header is known but the body and/or CRC footer is missing.
                segment.state = SegmentState.INCOMPLETE
                break

            self._finalize_candidate(results)

        return results

    def flush(self):
        """
        Return the finalized results that have not been returned yet, without
        consuming or reclassifying any buffered byte.

        This emits the latest corrupt increments and, when a segment is still
        waiting for more bytes, reports it as an
        `~SegmentState.INCOMPLETE` `FitSegmentResult` carrying the bytes
        available up to now. A short trailing run of bytes may still be held
        back (see the lookbehind logic) because it could still be the beginning
        of a header straddling two feeds; such bytes are only declared corrupt
        by `close()`. The internal state is left untouched, so feeding more
        bytes afterwards transparently resumes the pending segment.
        """
        if self._closed:
            return []

        results = []
        self._emit_corrupt(results, self._abs_offset)

        if self._segment is not None:
            # Return a detached snapshot: the live segment keeps evolving as
            # more bytes arrive, so referencing it directly would make a result
            # previously reported as INCOMPLETE silently change state later.
            segment = self._snapshot_pending_segment()
            available = min(len(self._buffer), segment.total_size)
            data = self._buffer[:available] if self._keep_raw else None
            results.append(FitSegmentResult(segment, data))

        return results

    def close(self):
        """
        Declare the stream finished and return the trailing results.

        After this call no more byte can be fed through `consume()`. Any
        pending segment is reported one last time as
        `~SegmentState.INCOMPLETE`, and any residual bytes that were held back
        while looking for a header are now definitely corrupt and are reported
        as such. Calling `close()` more than once returns an empty list.
        """
        if self._closed:
            return []

        results = []
        if self._segment is not None:
            self._emit_corrupt(results, self._abs_offset)

            segment = self._snapshot_pending_segment()
            available = min(len(self._buffer), segment.total_size)
            data = self._buffer[:available] if self._keep_raw else None
            results.append(FitSegmentResult(segment, data))
        else:
            if self._buffer:
                self._begin_corrupt_run()
                self._retire(len(self._buffer))
            self._emit_corrupt(results, self._abs_offset)

        self._closed = True
        return results

    @classmethod
    def segment(cls, data, *, keep_raw_chunks=False):
        """
        Convenience helper to segment a complete, already-buffered bytes-like
        object in one go and return every `FitSegmentResult` it contains.

        Since the whole input is known upfront, the stream is closed at the end
        so the trailing bytes are fully classified (including a final incomplete
        segment or a residual corrupt run).
        """
        segmenter = cls(keep_raw_chunks=keep_raw_chunks)
        results = segmenter.consume(data)
        results.extend(segmenter.close())
        return results

    # ----------------------------------------------------------------- private

    def _find_header_candidate(self):
        """
        Return the offset of the first plausible FIT header in the buffer, or
        `None` if none can be confirmed with the bytes available.

        Only the fixed (12-byte) part of the header is required here, so a
        14-byte header whose two extension bytes are still pending is still
        detected: the caller then simply waits for the missing bytes.
        """
        last_start = len(self._buffer) - FIT_FIXED_HEADER_SIZE
        for position in range(last_start + 1):
            if self._buffer[
                    position + 8:position + 12] != FIT_HEADER_MAGIC:
                continue
            if self._buffer[position] not in _LEGAL_HEADER_SIZES:
                continue
            return position
        return None

    def _build_segment(self):
        header_size, proto_ver_raw, profile_ver_raw, body_size, magic = \
            _STRUCT_FIXED_HEADER.unpack_from(self._buffer, 0)
        assert magic == FIT_HEADER_MAGIC

        header_crc = None
        header_crc_matched = None
        # For an extended header, the two CRC bytes may still be in transit; in
        # that case the header CRC simply stays unknown until the next feed.
        if (header_size == FIT_FIXED_HEADER_SIZE
                + FIT_EXTENDED_HEADER_SIZE
                and len(self._buffer) >= header_size):
            (header_crc, ) = _STRUCT_UINT16.unpack_from(
                self._buffer, FIT_FIXED_HEADER_SIZE)
            if header_crc:
                header_crc_matched = (
                    utils.compute_crc(
                        self._buffer[:FIT_FIXED_HEADER_SIZE])
                    == header_crc)
            else:
                header_crc = None

        header = FitHeader(
            header_size=header_size,
            proto_ver=(proto_ver_raw >> 4, proto_ver_raw & 0x0F),
            profile_ver=(
                int(profile_ver_raw / 100), int(profile_ver_raw % 100)),
            body_size=body_size,
            crc=header_crc,
            crc_matched=header_crc_matched,
            chunk=None)

        total_size = header_size + body_size + FIT_CRC_SIZE

        return FitSegment(
            state=SegmentState.INCOMPLETE,
            offset=self._abs_offset,
            header_size=header_size,
            body_size=body_size,
            total_size=total_size,
            header=header,
            crc_read=None,
            crc_matched=None)

    def _snapshot_pending_segment(self):
        live = self._segment
        header = live.header
        if header is not None:
            # Copy the header too, since its CRC fields may still be updated as
            # more bytes arrive.
            header = FitHeader(
                header_size=header.header_size,
                proto_ver=header.proto_ver,
                profile_ver=header.profile_ver,
                body_size=header.body_size,
                crc=header.crc,
                crc_matched=header.crc_matched,
                chunk=None)

        return FitSegment(
            state=SegmentState.INCOMPLETE,
            offset=live.offset,
            header_size=live.header_size,
            body_size=live.body_size,
            total_size=live.total_size,
            header=header,
            crc_read=live.crc_read,
            crc_matched=live.crc_matched)

    def _refresh_header_crc(self, segment):
        if segment.header.crc_matched is not None:
            return
        (header_crc, ) = _STRUCT_UINT16.unpack_from(
            self._buffer, FIT_FIXED_HEADER_SIZE)
        if not header_crc:
            segment.header.crc = None
            segment.header.crc_matched = None
            return
        segment.header.crc = header_crc
        segment.header.crc_matched = (
            utils.compute_crc(
                self._buffer[:FIT_FIXED_HEADER_SIZE]) == header_crc)

    def _finalize_candidate(self, results):
        segment = self._segment
        total_size = segment.total_size

        (crc_read, ) = _STRUCT_UINT16.unpack_from(
            self._buffer, total_size - FIT_CRC_SIZE)
        crc_computed = utils.compute_crc(
            memoryview(self._buffer)[:total_size - FIT_CRC_SIZE])
        crc_matched = crc_read == crc_computed

        segment.crc_read = crc_read
        segment.crc_matched = crc_matched

        if crc_matched:
            segment.state = SegmentState.COMPLETE

            # The candidate is genuinely valid: the corrupt run that may have
            # preceded it ends right where this segment starts.
            self._end_corrupt_run(results)

            data = self._buffer[:total_size] if self._keep_raw else None
            results.append(FitSegmentResult(segment, data))

            self._segment = None
            self._retire(total_size)
            return

        # The full span is available but the CRC does not match: this is a
        # corrupt segment, not an incomplete one. Only its first byte is retired
        # and hunting resumes, so a genuine FIT header located further on is
        # still discovered while any header-looking bytes inside the corrupt
        # span have to pass their own CRC check to be considered valid.
        segment.state = SegmentState.CORRUPT
        self._segment = None
        self._begin_corrupt_run()
        self._retire(1)

    def _begin_corrupt_run(self):
        if self._corrupt_start is None:
            self._corrupt_start = self._abs_offset
            self._corrupt_reported_end = self._abs_offset
            self._corrupt_data = bytearray() if self._keep_raw else None

    def _end_corrupt_run(self, results):
        # A run ends as soon as a valid FIT header candidate is found, so it
        # covers the bytes strictly before that header.
        self._emit_corrupt(results, self._abs_offset)
        self._corrupt_start = None
        self._corrupt_reported_end = None
        self._corrupt_data = None

    def _emit_corrupt(self, results, end_offset):
        """
        Emit the not-yet-reported prefix (up to *end_offset*) of the ongoing
        corrupt run, if any. Increments never overlap, which keeps every source
        offset attributable to exactly one reported result.
        """
        if self._corrupt_start is None:
            return

        if end_offset <= self._corrupt_reported_end:
            return

        start = self._corrupt_reported_end
        size = end_offset - start
        segment = FitSegment(
            state=SegmentState.CORRUPT,
            offset=start,
            header_size=0,
            body_size=0,
            total_size=size,
            header=None,
            crc_read=None,
            crc_matched=False)

        data = None
        if self._keep_raw:
            data_start = start - self._corrupt_start
            data = bytes(self._corrupt_data[data_start:data_start + size])

        results.append(FitSegmentResult(segment, data))
        self._corrupt_reported_end = end_offset

    def _retire(self, count):
        if count <= 0:
            return
        if self._corrupt_start is not None and self._keep_raw:
            self._corrupt_data += self._buffer[:count]
        self._buffer = self._buffer[count:]
        self._abs_offset += count
