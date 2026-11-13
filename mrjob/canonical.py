"""Canonical JSON keys for cross-language shuffle grouping.

The default :py:class:`~mrjob.protocol.JSONProtocol` just calls ``dumps()``,
so two mappers written in different languages can emit different bytes for
the *same* key:

* ``1`` (integer) versus ``1.0`` (floating point)
* the same object with its members in a different order
* different whitespace or number spellings (``100`` versus ``1e2``)

Hadoop groups mapper output by comparing raw bytes, so those keys land in
different reducer groups even though they mean the same thing.

This module implements a small, language-neutral *canonical key protocol*
(version ``canonical-json/1``). Both sides of a mixed-language job follow
the same rules and get the same bytes before the shuffle:

1. objects are unordered: members are sorted by Unicode code point, so
   member order never matters
2. arrays are ordered: two arrays with the same items in a different
   order stay in different groups
3. a missing field is simply absent; two objects with the same present
   fields canonicalize identically regardless of which fields the
   source data left out
4. an integer and a floating-point number with the same value merge
   (``1`` and ``1.0`` both become ``1``)
5. booleans are their own type and never merge with numbers
   (``true`` is not ``1``)
6. strings use JSON escaping (RFC 8259) encoded as UTF-8

The canonical bytes are themselves compact JSON, so any language with a
JSON library can read them; the normalization rules above are the whole
contract.

Legacy keys that cannot be trusted to group correctly -- objects with
duplicate members, or numbers that carry more precision than an IEEE-754
double can represent -- raise a :class:`CanonicalKeyError` instead of
being silently merged. Jobs route those records back to the source
partition with :class:`Quarantine` / :func:`canonical_shuffle_key`.
"""
import json
import math
from decimal import Decimal

from mrjob.py2 import integer_types
from mrjob.py2 import string_types

#: Wire-format revision. Bump this if the normalization rules change, so
#: mappers and reducers can check they speak the same dialect.
CANONICAL_KEY_PROTOCOL = 'canonical-json/1'


class CanonicalKeyError(ValueError):
    """Base class for keys that cannot be put through the canonical
    protocol without risking wrong shuffle groups."""

    def __init__(self, message, path=()):
        super(CanonicalKeyError, self).__init__(message)
        #: Location of the problem inside the key, a tuple of object keys
        #: and array indexes, outermost first.
        self.path = tuple(path)


class DuplicateMemberError(CanonicalKeyError):
    """Raised when an object contains the same member more than once.

    JSON parsers disagree about which duplicate wins, so the record must
    be resolved against the original data rather than guessed at.
    """

    def __init__(self, member, path=()):
        self.member = member
        super(DuplicateMemberError, self).__init__(
            'duplicate object member %r at %s' % (
                member, _format_path(path) or '<root>'),
            path)


class SourcePrecisionError(CanonicalKeyError):
    """Raised when a number in the source text carries more precision
    than an IEEE-754 double can preserve.

    The canonical form only has doubles to work with, so rounding here
    would make distinct source keys collide. The owning partition has to
    re-derive the key from the original data (for example by keeping the
    value as a string or using a fixed-point type).
    """

    def __init__(self, raw, path=()):
        self.raw = raw
        super(SourcePrecisionError, self).__init__(
            'number %r at %s exceeds double precision' % (
                raw, _format_path(path) or '<root>'),
            path)


#: Largest integer every JSON implementation can hold exactly in a
#: double (the 53-bit significand). Integers past this range are not
#: portable across languages, so the protocol refuses to shuffle them.
MAX_EXACT_INTEGER = 2 ** 53 - 1


class _JSONObject(list):
    """Internal: object members kept as an ordered ``(key, value)`` list
    so that duplicate members remain visible long enough to reject them.
    """


class _CheckedFloat(float):
    """Internal: a float that still remembers the exact text it was
    parsed from, so source precision can be checked during encoding."""

    __slots__ = ('raw',)

    def __new__(cls, raw):
        self = float.__new__(cls, raw)
        self.raw = raw
        return self


def _format_path(path):
    if not path:
        return ''
    out = []
    for part in path:
        if isinstance(part, integer_types):
            out.append('[%d]' % part)
        else:
            out.append('.%s' % part)
    return ''.join(out)


def _number_roundtrips(raw, value):
    """True iff the source text ``raw`` says exactly what the parsed
    double ``value`` stores, with no digits lost to rounding."""
    if math.isnan(value) or math.isinf(value):
        return False
    try:
        return Decimal(raw) == Decimal(repr(float(value)))
    except Exception:
        return False


def _normalize(value, path=()):
    """Convert a JSON-compatible Python value into a structure that
    :py:func:`json.dumps` renders deterministically."""
    if value is None:
        return None

    # bool is a subclass of int, so check it first; booleans stay their
    # own business category and never compare equal to 0 or 1.
    if isinstance(value, bool):
        return value

    if isinstance(value, integer_types):
        if abs(value) > MAX_EXACT_INTEGER:
            raise SourcePrecisionError('%d' % value, path)
        return value

    if isinstance(value, float):
        if isinstance(value, _CheckedFloat):
            if not _number_roundtrips(value.raw, float(value)):
                raise SourcePrecisionError(value.raw, path)

        if math.isnan(value) or math.isinf(value):
            raise CanonicalKeyError(
                '%r is not representable in JSON' % (value,), path)

        # 1 and 1.0 are the same key; render whole-valued floats as
        # integers so the two spellings merge.
        if value.is_integer():
            return int(value)
        return value

    if isinstance(value, string_types):
        return value

    if isinstance(value, bytes):
        raise CanonicalKeyError(
            'bytes are not JSON strings; decode them first: %r' % (value,),
            path)

    # _JSONObject is a list subclass, so check it before plain lists
    if isinstance(value, _JSONObject):
        members = value
    elif isinstance(value, (list, tuple)):
        return [
            _normalize(item, path + (index,))
            for index, item in enumerate(value)
        ]
    elif isinstance(value, dict):
        members = [(k, v) for k, v in value.items()]
    else:
        raise CanonicalKeyError(
            '%r is not a JSON-compatible key' % (type(value).__name__,),
            path)

    seen = set()
    normalized_members = []
    for member, item in members:
        if not isinstance(member, string_types):
            raise CanonicalKeyError(
                'object members must be strings, not %r' % (member,),
                path)
        if member in seen:
            raise DuplicateMemberError(member, path)
        seen.add(member)
        normalized_members.append(
            (member, _normalize(item, path + (member,))))

    # sort_keys=True makes the member order deterministic and sorts
    # by Unicode code point, which any other language can reproduce.
    return dict(sorted(normalized_members))


def canonicalize(value):
    """Return the canonical shuffle bytes for a JSON-compatible value.

    Accepts the usual ``json`` types: ``None``, ``bool``, ``int``,
    ``float``, strings, lists/tuples, and string-keyed dictionaries.

    :raises DuplicateMemberError: if an object repeats a member
    :raises SourcePrecisionError: if a source number lost digits to
        floating-point rounding
    :raises CanonicalKeyError: for any value the protocol cannot represent
    """
    normalized = _normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(',', ':'),
        sort_keys=True,
    ).encode('utf_8')


def _reject_constant(constant):
    raise CanonicalKeyError(
        '%r is not part of the JSON number system' % (constant,))


def loads_checked(data):
    """Parse JSON text without throwing away the legacy warning signs.

    Unlike the standard :py:func:`json.loads`, this keeps duplicate
    object members visible (so :func:`canonicalize` can raise
    :class:`DuplicateMemberError`) and remembers each number's exact
    source text (so :class:`SourcePrecisionError` can catch digits lost
    to double rounding).
    """
    if isinstance(data, bytes):
        data = data.decode('utf_8')

    return json.loads(
        data,
        object_pairs_hook=_JSONObject,
        parse_float=_CheckedFloat,
        parse_constant=_reject_constant,
    )


def canonicalize_json(data):
    """Canonicalize a key given as raw JSON bytes (or text).

    Unlike :func:`canonicalize`, this keeps the original number spellings
    and object members around long enough to apply the legacy checks:

    * duplicate object members raise :class:`DuplicateMemberError`
    * numbers beyond double precision raise :class:`SourcePrecisionError`
    * ``NaN``/``Infinity`` constants are rejected outright
    """
    return canonicalize(loads_checked(data))


def decanonicalize(data):
    """Parse canonical bytes back into a Python value.

    Note that normalization is one-way on purpose: the key ``1.0`` comes
    back as the integer ``1`` because the two are defined as the same
    key.
    """
    if isinstance(data, bytes):
        data = data.decode('utf_8')
    return json.loads(data)


def legacy_value(value):
    """Strip the checked wrappers to restore old, lenient JSON behavior.

    Duplicate members collapse last-wins and over-precise numbers round
    to plain doubles, exactly like the pre-protocol parsers did. Use
    this for batches that have not been cut over yet; never use it on a
    key you intend to canonicalize.
    """
    if isinstance(value, _JSONObject):
        return dict(
            (member, legacy_value(item)) for member, item in value)
    if isinstance(value, _CheckedFloat):
        return float(value)
    if isinstance(value, list):
        return [legacy_value(item) for item in value]
    return value


class Quarantine(object):
    """Records that could not share the canonical shuffle and must be
    resolved against their source partition.

    A quarantined record is never yielded to the reducer, so it cannot
    silently land in the wrong group. Each entry says which partition
    and record to go back to, and why.
    """

    #: machine-readable rejection reasons
    DUPLICATE_MEMBER = 'duplicate-member'
    SOURCE_PRECISION = 'source-precision'
    UNENCODABLE_KEY = 'unencodable-key'

    def __init__(self):
        self._entries = []

    def reject(self, partition, record_id, reason, detail=None):
        """Add a record to the quarantine."""
        self._entries.append({
            'partition': partition,
            'record_id': record_id,
            'reason': reason,
            'detail': detail,
        })

    def entries(self):
        """Return a copy of the quarantine entries as plain dicts."""
        return [dict(entry) for entry in self._entries]

    def partitions(self):
        """Return the sorted set of partitions that need re-processing."""
        return sorted(set(
            entry['partition']
            for entry in self._entries
            if entry['partition'] is not None))

    def __len__(self):
        return len(self._entries)

    def __iter__(self):
        return iter(self.entries())


def _reason_for(error):
    if isinstance(error, DuplicateMemberError):
        return Quarantine.DUPLICATE_MEMBER
    if isinstance(error, SourcePrecisionError):
        return Quarantine.SOURCE_PRECISION
    return Quarantine.UNENCODABLE_KEY


def canonical_shuffle_key(value, quarantine=None, partition=None,
                          record_id=None):
    """Canonicalize a mapper key, routing bad legacy keys to quarantine.

    Returns the canonical bytes, or ``None`` when the key was rejected
    (in which case an entry has been added to ``quarantine``). Without
    a quarantine, :class:`CanonicalKeyError` propagates as usual.
    """
    try:
        return canonicalize(value)
    except CanonicalKeyError as error:
        if quarantine is None:
            raise
        quarantine.reject(
            partition, record_id, _reason_for(error), str(error))
        return None
