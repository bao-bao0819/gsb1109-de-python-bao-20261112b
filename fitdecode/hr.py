# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

"""
Heart-rate (``hr`` message) per-beat indexing helpers.

A device may pack several fractional-second heartbeat events into a single
``hr`` message. After a link loss, it may also re-send a message that overlaps
the previous one. This module builds an index of the individual beat
observations out of a stream of :class:`fitdecode.FitDataMessage` objects, so
that each beat can be queried on its own while keeping track of every message
it appeared in.

Design properties:

* Beats are keyed by their raw ``event_timestamp`` value, i.e. an integer
  number of :data:`HR_EVENT_TIMESTAMP_SCALE` ticks since the FIT epoch. Device
  precision is therefore preserved (two beats within the same wall-clock
  second stay distinct).
* Re-transmitted events are *not* duplicated: the same observation gains an
  additional :class:`HrMessageRef` source instead.
* When the same event position carries two distinct ``filtered_bpm``
  measurements, both values are kept and the observation is flagged as
  conflicting. Values are never averaged nor dropped.
* Every source points back to the original ``hr`` message, so the packets that
  caused a disagreement can be retrieved verbatim.
"""

import datetime

from . import profile
from . import records
from .processors import FIT_UTC_REFERENCE, DefaultDataProcessor

__all__ = [
    'HR_EVENT_TIMESTAMP_SCALE',
    'HrMessageRef', 'HrBeatObservation', 'HrBeatIndex',
    'HrMessageIndexingProcessor', 'index_hr_stream']


#: Number of ``event_timestamp`` ticks per second, as defined by the FIT
#: profile (the ``event_timestamp`` field has ``scale=1024``).
HR_EVENT_TIMESTAMP_SCALE = 1024


def _tick_from_field_data(field_data):
    """
    Return the raw (integer, 1024-Hz) ``event_timestamp`` tick represented by
    a :class:`fitdecode.FieldData` object, regardless of the data processor in
    use.
    """
    # A "real" event_timestamp field (def_num 9) keeps its unscaled integer in
    # raw_value. Component-expanded fields (born from event_timestamp_12) carry
    # a scaled float in both value and raw_value, plus the anchor offset in
    # value.
    if not field_data.is_expanded and isinstance(field_data.raw_value, int):
        return field_data.raw_value

    value = field_data.value
    if isinstance(value, datetime.datetime):
        seconds = value.timestamp() - FIT_UTC_REFERENCE
    elif isinstance(value, (int, float)):
        seconds = value
    else:
        raise TypeError(
            f'cannot extract event_timestamp tick from value {value!r}')

    # tick spacing (~976us) is far larger than datetime microsecond rounding,
    # so rounding always recovers the original integer tick
    return int(round(seconds * HR_EVENT_TIMESTAMP_SCALE))


def _tick_to_datetime(tick):
    return datetime.datetime.fromtimestamp(
        FIT_UTC_REFERENCE + tick / HR_EVENT_TIMESTAMP_SCALE,
        datetime.timezone.utc)


def _flatten_bpm(value):
    if value is None:
        return []
    if isinstance(value, (tuple, list)):
        return list(value)
    return [value]


class HrMessageRef:
    """
    Reference to a single beat event within a specific ``hr`` message.

    Attributes:

    * ``fit_file_index``: zero-based index of the FIT file in the data stream
      (chained FIT files are counted separately).
    * ``message_index``: zero-based index of this ``hr`` message among the
      other ``hr`` messages of the same FIT file.
    * ``chunk_index``: zero-based index of the message frame in the stream, or
      `None` if raw chunks were not kept by the reader.
    * ``offset``: byte offset of the message frame in the stream, or `None` if
      raw chunks were not kept.
    * ``event_index``: zero-based index of the beat event inside the message
      (i.e. its position among the expanded ``event_timestamp`` values).
    """

    __slots__ = (
        'fit_file_index', 'message_index', 'chunk_index', 'offset',
        'event_index')

    def __init__(self, fit_file_index, message_index, chunk_index, offset,
                 event_index):
        self.fit_file_index = fit_file_index
        self.message_index = message_index
        self.chunk_index = chunk_index
        self.offset = offset
        self.event_index = event_index

    def __repr__(self):
        return (
            f'{type(self).__name__}('
            f'fit_file_index={self.fit_file_index!r}, '
            f'message_index={self.message_index!r}, '
            f'chunk_index={self.chunk_index!r}, '
            f'offset={self.offset!r}, '
            f'event_index={self.event_index!r})')

    def __eq__(self, other):
        if not isinstance(other, type(self)):
            return NotImplemented
        return (
            self.fit_file_index == other.fit_file_index and
            self.message_index == other.message_index and
            self.chunk_index == other.chunk_index and
            self.offset == other.offset and
            self.event_index == other.event_index)

    def __hash__(self):
        return hash((
            type(self).__name__, self.fit_file_index, self.message_index,
            self.chunk_index, self.offset, self.event_index))


class HrBeatObservation:
    """
    A single heartbeat observation, identified by its device timestamp.

    The same observation may be advertised by more than one ``hr`` message
    (typically overlapping re-transmissions after a reconnect), in which case
    :attr:`sources` contains several entries.

    Attributes:

    * ``tick``: raw device timestamp as an integer number of
      :data:`HR_EVENT_TIMESTAMP_SCALE` ticks since the FIT epoch.
    * ``filtered_bpm``: first measured ``filtered_bpm`` value seen for this
      beat (`None` if no measurement was provided). Check :attr:`conflict`
      before treating this value as authoritative.
    * ``sources``: tuple of :class:`HrMessageRef`, one per message occurrence.
    """

    __slots__ = ('tick', '_bpm_values', '_sources')

    def __init__(self, tick):
        self.tick = tick
        self._bpm_values = []
        self._sources = []

    def __repr__(self):
        return (
            f'{type(self).__name__}(tick={self.tick!r}, '
            f'filtered_bpm={self.filtered_bpm!r}, '
            f'conflict={self.conflict!r}, sources={len(self._sources)})')

    def __eq__(self, other):
        if not isinstance(other, type(self)):
            return NotImplemented
        return (
            self.tick == other.tick and
            self._bpm_values == other._bpm_values and
            self._sources == other._sources)

    @property
    def timestamp_seconds(self):
        """Device timestamp in seconds since the FIT epoch (`float`)."""
        return self.tick / HR_EVENT_TIMESTAMP_SCALE

    @property
    def timestamp_datetime(self):
        """Device timestamp as a timezone-aware UTC `datetime.datetime`."""
        return _tick_to_datetime(self.tick)

    @property
    def second(self):
        """Whole-second part of :attr:`timestamp_seconds` (`int`)."""
        return self.tick // HR_EVENT_TIMESTAMP_SCALE

    @property
    def filtered_bpm(self):
        return self._bpm_values[0] if self._bpm_values else None

    @property
    def bpm_values(self):
        """
        Distinct ``filtered_bpm`` measurements seen for this beat, in first-
        seen order (`tuple`). More than one entry means a conflict.
        """
        return tuple(self._bpm_values)

    @property
    def conflict(self):
        """`True` if two distinct measurements were seen for the same tick."""
        return len(self._bpm_values) > 1

    @property
    def sources(self):
        return tuple(self._sources)


class HrBeatIndex:
    """
    Accumulates per-beat :class:`HrBeatObservation` objects from ``hr``
    messages.

    Usage::

        index = HrBeatIndex()
        with fitdecode.FitReader('activity.fit') as fit:
            index.add_fit_stream(fit)

        for beat in index.observations():
            print(beat.tick, beat.filtered_bpm, beat.conflict, beat.sources)
    """

    def __init__(self):
        self._observations = {}  # tick -> HrBeatObservation
        self._messages = {}  # (fit_file_index, message_index) -> message
        self._message_refs = []  # ordered list of HrMessageRef
        self._message_counts = {}  # fit_file_index -> next message_index

    def add_message(self, data_message, *, fit_file_index=0,
                    message_index=None):
        """
        Index one ``hr`` :class:`fitdecode.FitDataMessage`.

        *fit_file_index* tells which FIT file of a chained stream this message
        belongs to. *message_index* may be provided to identify this message
        explicitly; otherwise a per-file counter is used.

        Returns the tuple of :class:`HrBeatObservation` objects that were
        created or updated by this call, in message order.
        """
        if (not isinstance(data_message, records.FitDataMessage) or
                data_message.global_mesg_num != profile.MESG_NUM_HR):
            raise TypeError('expected an hr FitDataMessage')

        if message_index is None:
            message_index = self._message_counts.get(fit_file_index, 0)
        self._message_counts[fit_file_index] = message_index + 1

        chunk = data_message.chunk
        chunk_index = chunk.index if chunk is not None else None
        offset = chunk.offset if chunk is not None else None

        timestamp_fields = list(data_message.get_fields(
            profile.FIELD_NUM_HR_EVENT_TIMESTAMP))
        ticks = [
            _tick_from_field_data(field_data)
            for field_data in timestamp_fields]

        bpm_values = []
        for field_data in data_message.get_fields('filtered_bpm'):
            bpm_values.extend(_flatten_bpm(field_data.value))

        self._messages[(fit_file_index, message_index)] = data_message

        touched = []
        for event_index, tick in enumerate(ticks):
            ref = HrMessageRef(
                fit_file_index, message_index, chunk_index, offset,
                event_index)
            self._message_refs.append(ref)

            try:
                observation = self._observations[tick]
            except KeyError:
                observation = HrBeatObservation(tick)
                self._observations[tick] = observation

            observation._sources.append(ref)

            if event_index < len(bpm_values):
                bpm = bpm_values[event_index]
                if bpm is not None and bpm not in observation._bpm_values:
                    observation._bpm_values.append(bpm)

            touched.append(observation)

        return tuple(touched)

    def add_fit_stream(self, frames):
        """
        Index every ``hr`` data message found in *frames*, an iterable of FIT
        frames (e.g. a :class:`fitdecode.FitReader`).

        Chained FIT files are counted automatically based on
        :class:`fitdecode.FitHeader` frames. Returns ``self``.
        """
        fit_file_index = -1
        for frame in frames:
            if frame.frame_type == records.FIT_FRAME_HEADER:
                fit_file_index += 1
            elif (frame.frame_type == records.FIT_FRAME_DATA and
                    frame.global_mesg_num == profile.MESG_NUM_HR):
                if fit_file_index < 0:
                    # malformed stream without header; assume file 0
                    fit_file_index = 0
                self.add_message(frame, fit_file_index=fit_file_index)
        return self

    def observations(self):
        """
        Return every :class:`HrBeatObservation`, sorted by device timestamp.
        """
        return [
            self._observations[tick]
            for tick in sorted(self._observations)]

    def get_observation(self, tick=None, *, seconds=None, moment=None):
        """
        Get one observation by its device timestamp.

        Lookup accepts exactly one of:

        * ``tick``: raw integer tick;
        * ``seconds``: timestamp in seconds since the FIT epoch (rounded to
          the nearest tick);
        * ``moment``: a `datetime.datetime` (timezone-aware or assumed UTC).

        Raises `KeyError` if no beat exists at this position.
        """
        args = (tick, seconds, moment)
        if sum(value is not None for value in args) != 1:
            raise ValueError(
                'exactly one of tick, seconds or moment must be specified')

        if tick is not None:
            target_tick = int(tick)
        elif seconds is not None:
            target_tick = int(round(seconds * HR_EVENT_TIMESTAMP_SCALE))
        else:
            if moment.tzinfo is None:
                moment = moment.replace(tzinfo=datetime.timezone.utc)
            target_tick = int(round(
                (moment.timestamp() - FIT_UTC_REFERENCE) *
                HR_EVENT_TIMESTAMP_SCALE))

        return self._observations[target_tick]

    def observations_at_second(self, second):
        """
        Yield the observations whose whole-second timestamp equals *second*
        (an integer number of seconds since the FIT epoch).

        Several beats may share the same second; they are returned as distinct
        observations and are never merged.
        """
        for tick in sorted(self._observations):
            if tick // HR_EVENT_TIMESTAMP_SCALE == second:
                yield self._observations[tick]

    def conflicts(self):
        """Yield every :class:`HrBeatObservation` flagged as conflicting."""
        for tick in sorted(self._observations):
            observation = self._observations[tick]
            if observation.conflict:
                yield observation

    def sources(self, tick):
        """
        Return the tuple of :class:`HrMessageRef` where the beat at *tick*
        appeared.
        """
        return self._observations[tick].sources

    def source_messages(self, tick):
        """
        Return the distinct original ``hr`` messages that carried the beat at
        *tick*, in first-seen order.

        This is the entry point to trace a conflict back to the exact packets
        that produced the divergent measurements.
        """
        messages = []
        seen = set()
        for ref in self.sources(tick):
            key = (ref.fit_file_index, ref.message_index)
            if key in seen:
                continue
            seen.add(key)
            messages.append(self._messages[key])
        return tuple(messages)

    def messages(self):
        """Return the ordered tuple of all indexed :class:`HrMessageRef`."""
        return tuple(self._message_refs)

    def get_message(self, ref):
        """
        Return the original :class:`fitdecode.FitDataMessage` pointed to by a
        :class:`HrMessageRef`.
        """
        return self._messages[(ref.fit_file_index, ref.message_index)]

    def __len__(self):
        return len(self._observations)

    def __iter__(self):
        return iter(self.observations())

    def __contains__(self, tick):
        return tick in self._observations


class HrMessageIndexingProcessor(DefaultDataProcessor):
    """
    Data processor that feeds every decoded ``hr`` message into an
    :class:`HrBeatIndex` while preserving the default value conversions.

    Usage::

        index = HrBeatIndex()
        processor = HrMessageIndexingProcessor(index)
        with fitdecode.FitReader('activity.fit', processor=processor) as fit:
            for frame in fit:
                pass
        index.conflicts()
    """

    def __init__(self, index=None):
        super().__init__()
        self.index = index if index is not None else HrBeatIndex()
        self._hr_message_count = 0

    def on_header(self, reader, fit_header):
        super().on_header(reader, fit_header)
        # chained FIT files start their own hr message sequence
        self._hr_message_count = 0

    def process_message_hr(self, reader, data_message):
        super().process_message_hr(reader, data_message)
        self.index.add_message(
            data_message,
            fit_file_index=reader.fit_file_index or 0,
            message_index=self._hr_message_count)
        self._hr_message_count += 1


def index_hr_stream(frames):
    """
    Convenience wrapper that creates an :class:`HrBeatIndex`, feeds it with
    *frames* (an iterable of FIT frames such as a `FitReader`) and returns it.
    """
    return HrBeatIndex().add_fit_stream(frames)
