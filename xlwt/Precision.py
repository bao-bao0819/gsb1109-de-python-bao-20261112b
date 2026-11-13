# -*- coding: utf-8 -*-
"""
Per-column precision policies for values that must survive export.

The BIFF8 .xls format stores every numeric cell as an IEEE-754 binary64
(double) value.  Most metering readings are ordinary numbers that are happy
with that representation and keep participating in spreadsheet sums, but some
readings carry more significant digits than a double can hold and must be
preserved verbatim.

A :class:`PrecisionPolicy` is attached to a column and decides, for every
numeric value written into that column, whether the nearest representable
double stays within an allowed error.  Values that do not fit either cause the
write to be rejected or are written as reversible text, in which case the
affected cell can no longer take part directly in a spreadsheet sum.
"""

from decimal import Decimal, localcontext

#: Reject values whose binary64 representation exceeds the tolerance.
REJECT = 'reject'
#: Write out-of-tolerance values as text that can be parsed back losslessly.
TEXT = 'text'

#: Keep the legacy behaviour: always store a binary64 number, even if the
#: rounding error exceeds the tolerance.
FORCE_NUMERIC = 'force_numeric'

_OVERFLOW_ACTIONS = (REJECT, TEXT, FORCE_NUMERIC)


def is_finite(value):
    """Return ``True`` for finite floats (works on Python 2.7 as well)."""
    return value == value and value not in (float('inf'), float('-inf'))


class PrecisionError(ValueError):
    """Raised when a value cannot be stored within the column precision policy."""


def _as_decimal(value):
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


class PrecisionPolicy(object):
    """
    Precision convention for one column.

    :param tolerance:
        Maximum allowed *relative* rounding error, i.e.
        ``abs(stored - original) / abs(original)``, when the original value is
        converted to the binary64 number used by the .xls format.  Defaults to
        ``1e-9``.
    :param abs_tolerance:
        Maximum allowed *absolute* rounding error.  A value passes when it is
        inside either the relative or the absolute tolerance, which keeps
        readings close to zero usable.  Defaults to ``tolerance``.
    :param on_overflow:
        What to do when the error exceeds the tolerance:

        * :data:`REJECT` (default): raise :class:`PrecisionError`;
        * :data:`TEXT`: write the value as reversible text instead of a number;
        * :data:`FORCE_NUMERIC`: store the binary64 number anyway.
    :param text_formatter:
        Optional callable ``(original_value, stored_float) -> str`` used by the
        :data:`TEXT` action.  The default keeps :class:`~decimal.Decimal`
        digits verbatim and uses the shortest round-tripping representation of
        any other number, so the text can always be parsed back.
    """

    def __init__(self, tolerance=1e-9, abs_tolerance=None,
                 on_overflow=REJECT, text_formatter=None):
        tolerance = float(tolerance)
        if not (tolerance >= 0.0 and is_finite(tolerance)):
            raise ValueError("tolerance must be a finite, non-negative number")
        if abs_tolerance is None:
            abs_tolerance = tolerance
        abs_tolerance = float(abs_tolerance)
        if not (abs_tolerance >= 0.0 and is_finite(abs_tolerance)):
            raise ValueError("abs_tolerance must be a finite, non-negative number")
        if on_overflow not in _OVERFLOW_ACTIONS:
            raise ValueError(
                "on_overflow must be one of %r, got %r"
                % (_OVERFLOW_ACTIONS, on_overflow))
        if text_formatter is not None and not callable(text_formatter):
            raise ValueError("text_formatter must be callable")
        self.tolerance = tolerance
        self.abs_tolerance = abs_tolerance
        self.on_overflow = on_overflow
        self.text_formatter = text_formatter

    def rounding_error(self, value, stored=None):
        """
        Return ``(absolute_error, relative_error)`` between *value* and the
        binary64 number that would actually be stored.

        Both errors are :class:`~decimal.Decimal` instances so that the long
        significant digits of the original reading are not themselves blurred
        while measuring the error.  The calculation depends on magnitude only,
        so a value and its negation always report identical errors.
        """
        if stored is None:
            stored = float(value)
        original = _as_decimal(value)
        represented = Decimal(repr(stored))
        # A high-precision local context keeps the subtraction exact enough
        # that negating the input cannot flip the rounding of the last digit;
        # without this the default 28-digit context rounds the two signs
        # differently for very long readings.
        with localcontext() as context:
            context.prec = max(80, len(original.as_tuple().digits) * 2)
            absolute_error = abs(represented - original)
            if original == 0:
                relative_error = absolute_error
            else:
                relative_error = absolute_error / abs(original)
        return absolute_error, relative_error

    def fits(self, value, stored=None):
        """
        Return ``True`` when *value* can be stored as a binary64 number within
        this policy's tolerance.

        Non-finite numbers (``NaN``, infinities) never fit because the .xls
        number records cannot represent them meaningfully.
        """
        if stored is None:
            stored = float(value)
        if not is_finite(stored):
            return False
        absolute_error, relative_error = self.rounding_error(value, stored)
        within_absolute = absolute_error <= Decimal(repr(self.abs_tolerance))
        within_relative = relative_error <= Decimal(repr(self.tolerance))
        return within_absolute or within_relative

    def format_text(self, value, stored=None):
        """
        Return the reversible text representation used by the :data:`TEXT`
        overflow action.

        Integers and :class:`~decimal.Decimal` instances keep every digit of
        the original value; floats use their shortest round-tripping
        representation.  In every case ``float(text)`` (or
        :meth:`decimal.Decimal`) recovers the intended reading.
        """
        if self.text_formatter is not None:
            if stored is None:
                stored = float(value)
            return self.text_formatter(value, stored)
        if isinstance(value, Decimal):
            return format(value, 'f')
        if isinstance(value, int):
            return str(value)
        if stored is None:
            stored = float(value)
        return repr(stored)
