# Copyright (c) Jean-Charles Lefebvre
# SPDX-License-Identifier: MIT

"""Optional cleaning of speed observations.

A single FIT data message may report speed directly (``record.speed`` /
``record.enhanced_speed``) and/or carry it packed into a combined field such
as ``record.compressed_speed_distance``. The decoder expands the packed value
into a dynamic ``speed`` field, so the same physical observation can show up
several times in ``FitDataMessage.fields``.

`clean_speed` treats one data message as **one observation** and keeps every
source field that contributed to it. Each source is checked against the
quantization step of the field it came from. Two sources are compatible when
their half-step intervals overlap::

    [value - step / 2, value + step / 2]

When all sources are compatible, the result exposes their common range
(``SpeedCleaning.common_range``). Otherwise every reported value and its
field provenance is preserved (``SpeedCleaning.values`` and
``SpeedCleaning.sources``).

Sources are grouped into *lineages* by the physical field they originate
from. Repeated expansions of the same parent field share one lineage and are
therefore not counted as independent sensors corroborating each other. The
number of independent lineages is exposed as
``SpeedCleaning.independent_source_count``.
"""

__all__ = [
    'SPEED_FIELD_NAMES', 'SPEED_UNIT_FACTORS',
    'SpeedSource', 'SpeedLineage', 'SpeedObservation', 'SpeedCleaning',
    'iter_speed_sources', 'reconcile_sources', 'reconcile_speed',
    'reconcile_message_speed', 'clean_speed', 'clean_speed_from_fields']


#: Field names considered as speed observations by default.
SPEED_FIELD_NAMES = frozenset(('speed', 'enhanced_speed'))

#: Conversion factors to ``m/s`` for supported units.
SPEED_UNIT_FACTORS = {
    'm/s': 1.0,
    'mps': 1.0,
    'km/h': 1000.0 / 3600.0,
    'kmh': 1000.0 / 3600.0,
    'mph': 1609.344 / 3600.0}

#: Tolerance used to absorb floating point noise when comparing intervals.
_EPSILON = 1e-9


def _round(value):
    if value is None:
        return None
    return round(value, 9)


def _interval(source):
    step = source.quantization_step_mps
    if step is None:
        return (source.value_mps, source.value_mps)
    half = step / 2.0
    return (source.value_mps - half, source.value_mps + half)


def _intersection(sources):
    """Return ``(low, high, compatible)`` over *sources*."""
    intervals = [_interval(source) for source in sources]
    low = max(interval[0] for interval in intervals)
    high = min(interval[1] for interval in intervals)
    return low, high, low <= high + _EPSILON


def _source_label(source):
    label = f'{source.field_name}#{source.field_num}={_round(source.value_mps)}'
    if source.expanded_from_name is None:
        return label
    return (
        f'{label} (expanded from {source.expanded_from_name}'
        f'#{source.expanded_from_def_num})')


class SpeedSource:
    """A single speed field occurrence and its provenance."""

    __slots__ = (
        'field_name', 'field_num', 'value', 'units', 'quantization_step',
        'value_mps', 'quantization_step_mps', 'is_expanded',
        'expanded_from_name', 'expanded_from_def_num', 'raw_value')

    def __init__(
            self, field_name, field_num, value, units, quantization_step,
            value_mps, quantization_step_mps, is_expanded,
            expanded_from_name=None, expanded_from_def_num=None,
            raw_value=None):
        self.field_name = field_name
        self.field_num = field_num
        self.value = value
        self.units = units
        self.quantization_step = quantization_step
        self.value_mps = value_mps
        self.quantization_step_mps = quantization_step_mps
        self.is_expanded = is_expanded
        self.expanded_from_name = expanded_from_name
        self.expanded_from_def_num = expanded_from_def_num
        self.raw_value = raw_value

    @property
    def source_key(self):
        """
        Identity of the physical field this occurrence originated from.

        It is ``(def_num, name)`` of the parent field for expanded fields, and
        of the field itself otherwise. Repeated expansions of the same parent
        field share this key, so they are not counted as separate sensors.
        """
        if self.is_expanded and self.expanded_from_def_num is not None:
            return (self.expanded_from_def_num, self.expanded_from_name)
        return (self.field_num, self.field_name)

    @property
    def quantization_range(self):
        """Representable interval of this source, in its own units."""
        if self.value is None:
            return None
        if self.quantization_step is None:
            return (self.value, self.value)
        half = self.quantization_step / 2.0
        return (self.value - half, self.value + half)

    def as_dict(self):
        return {
            'field_name': self.field_name,
            'field_num': self.field_num,
            'value': _round(self.value),
            'units': self.units,
            'quantization_step': _round(self.quantization_step),
            'is_expanded': self.is_expanded,
            'expanded_from': self.expanded_from_name,
            'expanded_from_def_num': self.expanded_from_def_num,
            'raw_value': self.raw_value}

    def __repr__(self):
        return f'<SpeedSource {_source_label(self)}>'


class SpeedLineage:
    """All occurrences of one physical speed field inside a message."""

    __slots__ = ('sources', 'compatible', 'low_mps', 'high_mps')

    def __init__(self, sources, compatible, low_mps, high_mps):
        self.sources = tuple(sources)
        self.compatible = compatible
        self.low_mps = low_mps
        self.high_mps = high_mps

    @property
    def source_key(self):
        return self.sources[0].source_key

    @property
    def range_mps(self):
        if not self.compatible:
            return None
        return (self.low_mps, self.high_mps)

    @property
    def source_count(self):
        return len(self.sources)

    @property
    def value_mps(self):
        best = min(
            self.sources,
            key=lambda source: (
                source.quantization_step_mps
                if source.quantization_step_mps is not None else 0.0))
        return best.value_mps

    def as_dict(self):
        return {
            'source_key': self.source_key,
            'value': _round(self.value_mps),
            'compatible': self.compatible,
            'range': None if not self.compatible else [
                _round(self.low_mps), _round(self.high_mps)],
            'source_count': self.source_count,
            'sources': [source.as_dict() for source in self.sources]}

    def __repr__(self):
        return (
            f'<SpeedLineage {self.source_key!r} '
            f'sources={self.source_count} compatible={self.compatible}>')


class SpeedObservation:
    """The single speed observation carried by one data message."""

    __slots__ = ('sources', 'lineages', 'compatible', 'low_mps', 'high_mps')

    def __init__(self, sources, lineages, compatible, low_mps, high_mps):
        self.sources = tuple(sources)
        self.lineages = tuple(lineages)
        self.compatible = compatible
        self.low_mps = low_mps
        self.high_mps = high_mps

    @property
    def range_mps(self):
        if not self.compatible:
            return None
        return (self.low_mps, self.high_mps)

    @property
    def source_count(self):
        return len(self.sources)

    @property
    def independent_source_count(self):
        """Number of distinct physical fields, ignoring repeated expansions."""
        return len(self.lineages)

    @property
    def lineage_count(self):
        """Alias of `independent_source_count`."""
        return len(self.lineages)

    @property
    def value_mps(self):
        best = min(
            self.sources,
            key=lambda source: (
                source.quantization_step_mps
                if source.quantization_step_mps is not None else 0.0))
        return best.value_mps

    def as_dict(self):
        return {
            'value': _round(self.value_mps),
            'compatible': self.compatible,
            'range': None if not self.compatible else [
                _round(self.low_mps), _round(self.high_mps)],
            'source_count': self.source_count,
            'independent_source_count': self.independent_source_count,
            'sources': [source.as_dict() for source in self.sources],
            'lineages': [lineage.as_dict() for lineage in self.lineages]}

    def __repr__(self):
        return (
            f'<SpeedObservation value={_round(self.value_mps)} '
            f'sources={self.source_count} '
            f'lineages={self.independent_source_count} '
            f'compatible={self.compatible}>')


class SpeedCleaning:
    """Result of reconciling every speed source of a data message."""

    __slots__ = (
        'observations', 'compatible', 'low_mps', 'high_mps', 'units', 'reason')

    def __init__(self, observations, compatible, low_mps, high_mps, units,
                 reason=None):
        self.observations = tuple(observations)
        self.compatible = compatible
        self.low_mps = low_mps
        self.high_mps = high_mps
        self.units = units
        self.reason = reason

    @property
    def common_range_mps(self):
        """Common range in ``m/s``, or `None` when sources are incompatible."""
        if not self.compatible:
            return None
        return (self.low_mps, self.high_mps)

    @property
    def common_range(self):
        """Common range expressed in `units`."""
        range_mps = self.common_range_mps
        if range_mps is None:
            return None
        factor = SPEED_UNIT_FACTORS.get(self.units.lower(), 1.0)
        return (range_mps[0] / factor, range_mps[1] / factor)

    @property
    def value_mps(self):
        """Single best estimate in ``m/s``: the highest precision source."""
        if not self.observations:
            return None
        return self.observations[0].value_mps

    @property
    def value(self):
        """Single best estimate expressed in `units`."""
        value_mps = self.value_mps
        if value_mps is None:
            return None
        factor = SPEED_UNIT_FACTORS.get(self.units.lower(), 1.0)
        return value_mps / factor

    @property
    def observation_count(self):
        """Number of observations.

        A data message carries at most one speed observation, no matter how
        many fields reported or expanded it.
        """
        return len(self.observations)

    @property
    def sources(self):
        """Every speed field occurrence, in message order."""
        return tuple(
            source
            for observation in self.observations
            for source in observation.sources)

    @property
    def source_count(self):
        """Number of speed field occurrences, expansions included."""
        return len(self.sources)

    @property
    def lineages(self):
        """Groups of sources sharing one physical field."""
        return tuple(
            lineage
            for observation in self.observations
            for lineage in observation.lineages)

    @property
    def independent_source_count(self):
        """Number of distinct physical fields backing the observation."""
        return len(self.lineages)

    @property
    def lineage_count(self):
        """Alias of `independent_source_count`."""
        return len(self.lineages)

    @property
    def values_mps(self):
        """Every distinct reported value, normalized to ``m/s``."""
        values = []
        for source in self.sources:
            if source.value_mps not in values:
                values.append(source.value_mps)
        return tuple(values)

    @property
    def values(self):
        """Every distinct reported value, expressed in `units`."""
        factor = SPEED_UNIT_FACTORS.get(self.units.lower(), 1.0)
        return tuple(value / factor for value in self.values_mps)

    def as_dict(self):
        return {
            'compatible': self.compatible,
            'units': self.units,
            'range': None if not self.compatible else [
                _round(value) for value in self.common_range],
            'range_mps': None if not self.compatible else [
                _round(self.low_mps), _round(self.high_mps)],
            'values': [_round(value) for value in self.values],
            'values_mps': [_round(value) for value in self.values_mps],
            'value': _round(self.value),
            'value_mps': _round(self.value_mps),
            'observation_count': self.observation_count,
            'source_count': self.source_count,
            'independent_source_count': self.independent_source_count,
            'reason': self.reason,
            'observations': [
                observation.as_dict()
                for observation in self.observations]}

    def __repr__(self):
        return (
            f'<SpeedCleaning compatible={self.compatible} '
            f'observations={self.observation_count} '
            f'sources={len(self.sources)} '
            f'range={self.common_range_mps}>')


def _source_from_field(field_data):
    value = field_data.value
    units = field_data.units or 'm/s'
    factor = SPEED_UNIT_FACTORS.get(units.lower())
    if factor is None:
        return None

    step = field_data.quantization_step
    step_units = getattr(field_data, 'quantization_units', None) or units
    step_factor = SPEED_UNIT_FACTORS.get(step_units.lower(), factor)
    step_mps = None if step is None else step * step_factor

    expanded_from_name = None
    expanded_from_def_num = None
    if field_data.expansion_parent is not None:
        expanded_from_name = field_data.expansion_parent.name
        expanded_from_def_num = field_data.expansion_parent_def_num

    return SpeedSource(
        field_name=field_data.name,
        field_num=field_data.def_num,
        value=value,
        units=units,
        quantization_step=step,
        value_mps=value * factor,
        quantization_step_mps=step_mps,
        is_expanded=field_data.is_expanded,
        expanded_from_name=expanded_from_name,
        expanded_from_def_num=expanded_from_def_num,
        raw_value=field_data.raw_value)


def iter_speed_sources(message, *, field_names=SPEED_FIELD_NAMES):
    """Yield a `SpeedSource` for every usable speed field of *message*.

    *message* can be a `FitDataMessage`, a single `FieldData`, or any iterable
    of `FieldData` objects. Fields without a numeric value, unknown units or a
    name outside *field_names* are skipped.
    """
    fields = getattr(message, 'fields', message)

    if hasattr(fields, 'value') and hasattr(fields, 'name'):
        # a single FieldData object
        fields = (fields, )

    for field_data in fields:
        if field_names is not None and field_data.name not in field_names:
            continue

        value = field_data.value
        if (isinstance(value, bool) or
                not isinstance(value, (int, float))):
            continue

        source = _source_from_field(field_data)
        if source is not None:
            yield source


def _build_lineages(sources):
    groups = {}
    order = []
    for source in sources:
        key = source.source_key
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(source)

    lineages = []
    for key in order:
        group = groups[key]
        low, high, compatible = _intersection(group)
        lineages.append(SpeedLineage(
            group,
            compatible,
            low if compatible else None,
            high if compatible else None))
    return lineages


def reconcile_sources(sources, *, units=None):
    """Reconcile a sequence of `SpeedSource` objects.

    The whole sequence is treated as one observation. Sources expanded from
    the same parent field share a lineage: they are retained individually but
    count as one source for corroboration.
    """
    sources = list(sources)

    if units is None:
        unit_values = {
            source.units.lower() for source in sources
            if source.units is not None}
        units = unit_values.pop() if len(unit_values) == 1 else 'm/s'

        # keep the spelling of the first source if it matches the selection
        for source in sources:
            if source.units is not None and \
                    source.units.lower() == units.lower():
                units = source.units
                break

    if not sources:
        return SpeedCleaning((), True, None, None, units, 'no speed sources')

    lineages = _build_lineages(sources)

    low = high = None
    compatible = all(lineage.compatible for lineage in lineages)
    if compatible:
        low, high, compatible = _intersection(sources)

    if compatible:
        observation = SpeedObservation(
            sources, lineages, True, low, high)
        reason = (
            f'1 observation from {len(sources)} source(s) in '
            f'{len(lineages)} independent lineage(s), '
            f'compatible within [{_round(low)}, {_round(high)}] m/s')
    else:
        observation = SpeedObservation(
            sources, lineages, False, None, None)
        labels = [_source_label(source) for source in sources]
        reason = 'incompatible speed sources: ' + ' | '.join(labels)

        broken_lineages = [
            lineage for lineage in lineages if not lineage.compatible]
        if broken_lineages:
            reason += (
                '; conflicting duplicate expansions of the same parent '
                'field are counted once, not as two sensors')

    return SpeedCleaning(
        (observation,), compatible, low, high, units, reason)


def clean_speed(message, *, field_names=SPEED_FIELD_NAMES):
    """Reconcile the speed fields of *message*.

    Return `None` when the message does not carry any usable speed value.
    """
    sources = list(iter_speed_sources(message, field_names=field_names))
    if not sources:
        return None
    return reconcile_sources(sources)


def clean_speed_from_fields(fields, *, field_names=SPEED_FIELD_NAMES):
    """Reconcile a plain sequence of `FieldData` objects."""
    return clean_speed(fields, field_names=field_names)


def reconcile_speed(message, *, field_names=SPEED_FIELD_NAMES):
    """Alias of `clean_speed`."""
    return clean_speed(message, field_names=field_names)


def reconcile_message_speed(message, *, field_names=SPEED_FIELD_NAMES):
    """Alias of `clean_speed` for `FitDataMessage` objects."""
    return clean_speed(message, field_names=field_names)
