# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

"""
Auditing of a wrapping (modular) cumulative odometer reading.

Some devices export the cumulative distance as a *short* unsigned counter that
wraps around (rolls over) once it reaches its maximum value. When two successive
absolute readings go from a large value back to a small one, two explanations
are therefore possible:

* the counter did one or more natural rollovers during the gap;
* the device was restarted (or its odometer reset) in between, so the new
  reading is an unrelated value.

This module does not try to guess. For each pair of adjacent absolute readings
it enumerates every physically plausible explanation:

* **continuous counter** - the odometer kept running and wrapped ``n`` times
  during the gap, so the true increment is ``delta + n * rollover`` where
  ``delta`` is the short modular increment. When the new reading is smaller
  than the previous one, continuity requires at least one wrap (``n >= 1``);
* **restart** - the device was restarted (or its odometer reset to zero) in
  between, making the new reading an unrelated, fresh counter. A reset counter
  still has to accumulate its new reading within the gap, so this explanation
  only holds when the new reading itself fits the maximum-speed budget.

Each gap is then classified as uniquely determined, ambiguous, or impossible.
The shortest possible increment is never assumed when several explanations
remain. An impossible gap points at the two observations that contradict each
other under the given maximum speed.

All the values handled here are integers expressed in the counter's own
**count** unit (i.e. the FIT raw value, before any scale/offset processing).
Working on raw integers keeps rollover arithmetic exact and avoids floating
point rounding.
"""

import math
from collections import namedtuple

from . import profile, records
from .reader import FitReader

__all__ = [
    'DEFAULT_ROLLOVER',
    'DISTANCE_FIELD_NUM',
    'RECORD_MESG_NUM',
    'OdometerAudit',
    'OdometerGap',
    'OdometerObservation',
    'audit_odometer',
    'iter_record_odometer',
]


#: Default rollover value of a 32-bit unsigned counter: a full turn counts
#: ``2**32`` steps (the maximum readable value is ``2**32 - 1``).
DEFAULT_ROLLOVER = 1 << 32

#: FIT global message number of a ``record`` message.
RECORD_MESG_NUM = 20

#: FIT definition number of the ``distance`` field of a ``record`` message.
DISTANCE_FIELD_NUM = 5


class OdometerObservation(namedtuple(
        'OdometerObservation',
        ('index', 'fit_file_index', 'frame_index', 'timestamp', 'reading'))):
    """
    A single absolute odometer reading extracted from a FIT stream.

    * ``index``: zero-based index of this observation among the readings that
      were collected, per FIT file (records without a distance field are
      skipped);
    * ``fit_file_index``: index of the chained FIT file this observation belongs
      to, as reported by :attr:`fitdecode.FitReader.fit_file_index`;
    * ``frame_index``: index of the source frame in the FIT stream, as reported
      by :attr:`fitdecode.FitChunk.index` (``None`` if raw chunks were not
      kept);
    * ``timestamp``: raw ``timestamp`` of the record in FIT seconds, or ``None``
      if the record has none;
    * ``reading``: the absolute odometer reading, in counter counts (raw value).
    """
    __slots__ = ()


class OdometerGap(namedtuple(
        'OdometerGap',
        ('prev', 'curr', 'elapsed', 'delta', 'wrapped', 'bounded',
         'min_increment', 'max_increment', 'rollovers', 'restart_possible',
         'status'))):
    """
    The result of auditing two successive :class:`OdometerObservation`.

    Two families of explanations are considered:

    * a **continuous** counter, which wrapped ``n`` times. Its true increment is
      ``delta + n * rollover``;
    * a **restart**, where the odometer was reset and the current reading was
      accumulated from zero after the reset (see :attr:`restart_possible`).

    Fields:

    * ``prev`` and ``curr``: the two observations being compared;
    * ``elapsed``: time in seconds separating the two observations, or ``None``
      when a timestamp is missing. A negative value means the observations are
      not ordered by time, so no speed bound can be applied;
    * ``delta``: the short (modular) increment from ``prev`` to ``curr``, in
      counts, i.e. a value in ``range(rollover)``. This is the value a naive
      "shortest delta" implementation would pick;
    * ``wrapped``: ``True`` when the new reading is smaller than the previous
      one, i.e. a natural rollover or a restart is required to keep the
      sequence continuous;
    * ``bounded``: ``True`` when a physical speed bound could be applied,
      ``False`` when the candidate rollover counts are not enumerable. A gap is
      not bounded when it has no usable timestamp, its timestamps go backwards,
      or the elapsed time is zero (the timestamp resolution is then too coarse
      to enforce a speed limit);
    * ``min_increment`` / ``max_increment``: inclusive bounds, in counts, of the
      physically plausible increments (``None`` when not bounded);
    * ``rollovers``: sorted tuple of the rollover counts ``n`` (each a
      non-negative ``int``) compatible with the maximum speed for a continuous
      counter. Empty when none survives, or when the gap is not bounded;
    * ``restart_possible``: ``True`` when a reset/restart cannot be ruled out.
      For a bounded gap it means the fresh reading fits the speed budget; for an
      unbounded gap it is always ``True`` since nothing can be excluded;
    * ``status``: one of :attr:`OdometerAudit.UNIQUE`,
      :attr:`OdometerAudit.AMBIGUOUS` or :attr:`OdometerAudit.IMPOSSIBLE`.
    """
    __slots__ = ()

    @property
    def is_unique(self):
        """``True`` if exactly one explanation survives the constraints."""
        return self.status == OdometerAudit.UNIQUE

    @property
    def is_ambiguous(self):
        """``True`` if more than one explanation survives."""
        return self.status == OdometerAudit.AMBIGUOUS

    @property
    def is_impossible(self):
        """``True`` if no explanation is physically plausible."""
        return self.status == OdometerAudit.IMPOSSIBLE

    @property
    def is_time_ordered(self):
        """
        ``False`` when the two timestamps are present but go backwards.

        Such a gap cannot be bounded by a speed, so it is reported as
        ambiguous rather than guessed.
        """
        return self.elapsed is None or self.elapsed >= 0

    @property
    def unique_explanation(self):
        """
        The single surviving explanation as a tuple.

        It is either ``('rollover', n)`` (continuous counter that wrapped ``n``
        times) or ``('restart',)`` (odometer reset in between).

        Raises `ValueError` unless the gap is :attr:`is_unique`.
        """
        if self.status != OdometerAudit.UNIQUE:
            raise ValueError('gap is not uniquely determined')
        if self.rollovers:
            return ('rollover', self.rollovers[0])
        return ('restart',)

    @property
    def explanation_count(self):
        """
        Number of distinct surviving explanations.

        Returns ``None`` when the gap is not bounded, since the continuous
        family then contains an unbounded number of rollover counts.
        """
        if not self.bounded:
            return None
        return len(self.rollovers) + (1 if self.restart_possible else 0)

    def increment_candidates(self, rollover):
        """
        Yield the plausible true increments (in counts) for a continuous
        counter, one per compatible rollover count, in ascending order.

        This only covers the continuous family; check
        :attr:`restart_possible` for the reset hypothesis. Raises `ValueError`
        if the gap is not bounded, since its candidate set is then infinite and
        cannot be enumerated.
        """
        if not self.bounded:
            raise ValueError(
                'gap is not bounded by a maximum speed and an elapsed time; '
                'its rollover candidates are not enumerable')
        for turns in self.rollovers:
            yield self.delta + turns * rollover


class OdometerAudit:
    """
    Collection of :class:`OdometerGap` objects resulting from an audit run.

    The object behaves as a read-only sequence of gaps.
    """

    #: Exactly one rollover count is plausible: the segment is fully determined.
    UNIQUE = 'unique'

    #: More than one rollover count is plausible: the gap admits several
    #: interpretations and the shortest increment must not be assumed.
    AMBIGUOUS = 'ambiguous'

    #: No rollover count is plausible: the two observations contradict each
    #: other under the given maximum speed.
    IMPOSSIBLE = 'impossible'

    def __init__(self, rollover, max_speed, scale, gaps):
        self.rollover = int(rollover)
        self.max_speed = max_speed
        self.scale = scale
        self._gaps = tuple(gaps)

    def __iter__(self):
        return iter(self._gaps)

    def __len__(self):
        return len(self._gaps)

    def __getitem__(self, index):
        return self._gaps[index]

    @property
    def observations(self):
        """Every observation involved in the audit, in stream order."""
        if not self._gaps:
            return ()
        result = [self._gaps[0].prev]
        result.extend(gap.curr for gap in self._gaps)
        return tuple(result)

    @property
    def unique_gaps(self):
        """Tuple of the gaps whose increment is uniquely determined."""
        return tuple(gap for gap in self._gaps if gap.is_unique)

    @property
    def ambiguous_gaps(self):
        """Tuple of the gaps that still admit several explanations."""
        return tuple(gap for gap in self._gaps if gap.is_ambiguous)

    @property
    def impossible_gaps(self):
        """Tuple of the gaps that no rollover count and no restart can explain."""
        return tuple(gap for gap in self._gaps if gap.is_impossible)

    @property
    def restart_gaps(self):
        """Tuple of the gaps for which a restart/reset stays a viable explanation."""
        return tuple(gap for gap in self._gaps if gap.restart_possible)

    @property
    def is_consistent(self):
        """
        ``True`` if every adjacent pair of observations admits at least one
        explanation.

        A ``False`` result means at least one pair of observations contradicts
        the maximum-speed hypothesis; see :attr:`impossible_gaps`.
        """
        return not any(gap.is_impossible for gap in self._gaps)


def iter_record_odometer(fileish, *, processor=None, check_crc=None,
                         error_handling=None):
    """
    Yield an :class:`OdometerObservation` for each ``record`` message of
    *fileish* that carries a ``distance`` field.

    The distance is read as a **raw value** so that rollover arithmetic stays on
    integer counts. Records whose distance is missing, invalid (``None``) or
    non-integer are skipped.

    *fileish* is anything accepted by :class:`fitdecode.FitReader`. The extra
    keyword arguments are forwarded to :class:`fitdecode.FitReader`. By default
    no unit processor is applied, so timestamps remain raw FIT integers.
    """
    reader_kwargs = {'keep_raw_chunks': True}
    if processor is not None:
        reader_kwargs['processor'] = processor
    if check_crc is not None:
        reader_kwargs['check_crc'] = check_crc
    if error_handling is not None:
        reader_kwargs['error_handling'] = error_handling

    obs_index_per_file = {}

    with FitReader(fileish, **reader_kwargs) as fit:
        for frame in fit:
            if not isinstance(frame, records.FitDataMessage):
                continue
            if frame.global_mesg_num != RECORD_MESG_NUM:
                continue

            reading = frame.get_value(
                DISTANCE_FIELD_NUM, fallback=None, raw_value=True)
            if reading is None or not isinstance(reading, int):
                continue

            timestamp = frame.get_value(
                profile.FIELD_NUM_TIMESTAMP, fallback=None, raw_value=True)

            fit_file_index = (
                fit.fit_file_index if fit.fit_file_index is not None else 0)
            obs_index = obs_index_per_file.get(fit_file_index, -1) + 1
            obs_index_per_file[fit_file_index] = obs_index

            frame_index = frame.chunk.index if frame.chunk is not None else None
            yield OdometerObservation(
                index=obs_index,
                fit_file_index=fit_file_index,
                frame_index=frame_index,
                timestamp=timestamp,
                reading=reading)


def audit_odometer(observations, *, rollover=DEFAULT_ROLLOVER,
                   max_speed=None, scale=None):
    """
    Audit a sequence of absolute odometer *observations*.

    * *rollover*: number of counts in a full turn of the counter
      (``2**32`` by default);
    * *max_speed*: known physical speed limit expressed in **meters per
      second**. Combined with *scale* and the elapsed time, it bounds the
      plausible increment (in counts) of each gap;
    * *scale*: number of counts per meter, i.e. the FIT ``scale`` of the
      distance field (``100`` for the standard ``record.distance``). Required
      when *max_speed* is provided, ignored otherwise.

    Returns an :class:`OdometerAudit`.

    When *max_speed* is omitted, a gap has no usable timestamp, its timestamps
    go backwards, or the elapsed time is zero, no physical bound can be
    applied: such a gap is reported as ambiguous with a non-enumerable
    candidate set instead of being guessed, and a restart can no longer be
    ruled out either.

    Raises `ValueError` on invalid arguments or out-of-range readings.
    """
    rollover = int(rollover)
    if rollover <= 0:
        raise ValueError('rollover must be a positive integer')

    if max_speed is not None:
        if max_speed < 0:
            raise ValueError('max_speed must be non-negative')
        if scale is None:
            raise ValueError(
                'scale is required to convert max_speed to counter counts')
        if scale <= 0:
            raise ValueError('scale must be positive')

    observations = tuple(observations)
    for obs in observations:
        if (not isinstance(obs.reading, int) or
                not (0 <= obs.reading < rollover)):
            raise ValueError(
                f'reading {obs.reading!r} is out of range for a counter '
                f'modulo {rollover}')

    gaps = [
        _audit_gap(
            observations[idx], observations[idx + 1],
            rollover, max_speed, scale)
        for idx in range(len(observations) - 1)]

    return OdometerAudit(
        rollover=rollover,
        max_speed=max_speed,
        scale=scale,
        gaps=gaps)


def _audit_gap(prev, curr, rollover, max_speed, scale):
    elapsed = None
    if prev.timestamp is not None and curr.timestamp is not None:
        elapsed = curr.timestamp - prev.timestamp

    # the new reading being smaller than the previous one means a continuous
    # counter must have wrapped at least once; a restart is the alternative
    wrapped = curr.reading < prev.reading

    # short, modular increment (the value a naive "shortest delta"
    # implementation would pick)
    delta = (curr.reading - prev.reading) % rollover

    # A speed bound is only meaningful over a strictly positive elapsed time.
    # An elapsed time of zero is a timestamp-resolution artifact (e.g. two
    # records within the same second), not a contradiction, so such a gap is
    # treated as unbounded rather than rejected.
    bounded = (
        max_speed is not None and
        elapsed is not None and elapsed > 0)
    min_increment = None
    max_increment = None
    rollover_candidates = ()
    restart_possible = False

    if bounded:
        # maximum plausible distance travelled during the gap, in counts:
        #   max speed (m/s) * scale (counts/m) * elapsed (s)
        max_increment = math.floor(max_speed * scale * elapsed)
        min_increment = 0

        # continuous counter: enumerate the rollover counts n such that
        #   delta + n * rollover <= max_increment
        # a decreased reading requires at least one wrap (n >= 1), otherwise the
        # same modular delta would describe an increasing, wrap-free segment
        n_min = 1 if wrapped else 0
        if max_increment >= delta + n_min * rollover:
            n_max = (max_increment - delta) // rollover
            rollover_candidates = tuple(range(n_min, n_max + 1))

        # restart hypothesis: after a reset the fresh counter accumulates the
        # current reading from zero, which has to fit the same speed budget
        if curr.reading <= max_increment:
            restart_possible = True

        explanation_count = len(rollover_candidates) + (
            1 if restart_possible else 0)
        if explanation_count == 0:
            status = OdometerAudit.IMPOSSIBLE
        elif explanation_count == 1:
            status = OdometerAudit.UNIQUE
        else:
            status = OdometerAudit.AMBIGUOUS
    else:
        # without a physical bound neither the number of wraps nor a possible
        # restart can be ruled out: nothing is uniquely determined
        restart_possible = True
        status = OdometerAudit.AMBIGUOUS

    return OdometerGap(
        prev=prev,
        curr=curr,
        elapsed=elapsed,
        delta=delta,
        wrapped=wrapped,
        bounded=bounded,
        min_increment=min_increment,
        max_increment=max_increment,
        rollovers=rollover_candidates,
        restart_possible=restart_possible,
        status=status)
