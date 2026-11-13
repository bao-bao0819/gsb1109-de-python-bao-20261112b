"""
pyexcel_io.exceptions
~~~~~~~~~~~~~~~~~~~~~~

all possible exceptions

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""


class NoSupportingPluginFound(Exception):
    """raised when an unknown file extension is seen"""

    pass


class SupportingPluginAvailableButNotInstalled(Exception):
    """raised when a known plugin is not installed"""

    pass


class ColumnContractConfigError(ValueError):
    """Raised when a column input contract cannot be interpreted."""

    pass


class ColumnContractViolation(object):
    """Describe one cell that a column contract could not convert.

    ``row`` is the zero-based data row (header rows are not counted),
    ``column`` is the zero-based column position in the original CSV row.
    ``raw_value`` keeps the original spelling so a caller can reject the
    record without losing leading zeros or thousands separators.
    """

    def __init__(
        self,
        row,
        column,
        raw_value,
        reason,
        column_name=None,
        header=None,
        rule=None,
        source_row=None,
        line_number=None,
        message=None,
    ):
        self.row = row
        self.data_row = row
        self.row_index = row
        self.column = column
        self.column_index = column
        self.original_column = column
        self.raw_value = raw_value
        self.value = raw_value
        self.reason = reason
        self.outcome = reason
        self.column_name = column_name
        self.header = header
        self.rule = rule
        self.source_row = source_row
        self.line_number = line_number
        self.message = message or self._default_message()

    def _default_message(self):
        location = "row %s, column %s" % (self.row, self.column)
        if self.column_name:
            location += " (%s)" % (self.column_name,)
        if self.reason == "missing":
            return "%s: column is missing" % location
        if self.reason == "empty":
            return "%s: cell is empty" % location
        return "%s: %r does not satisfy the column contract" % (
            location,
            self.raw_value,
        )

    def as_tuple(self):
        """Return the stable identity used for cross-source comparisons."""
        return (self.row, self.column, self.reason, self.raw_value)

    def __eq__(self, other):
        if not isinstance(other, ColumnContractViolation):
            return NotImplemented
        return self.as_tuple() == other.as_tuple()

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __repr__(self):
        return (
            "ColumnContractViolation(row=%r, column=%r, reason=%r, "
            "raw_value=%r, column_name=%r)"
            % (
                self.row,
                self.column,
                self.reason,
                self.raw_value,
                self.column_name,
            )
        )


class CSVColumnContractError(ValueError):
    """Raised in strict mode when one or more cells violate a contract."""

    def __init__(self, violations, message=None):
        if isinstance(violations, ColumnContractViolation):
            violations = [violations]
        self.violations = list(violations)
        self.violation = self.violations[0] if self.violations else None
        if self.violation is not None:
            self.row = self.violation.row
            self.data_row = self.violation.data_row
            self.row_index = self.violation.row_index
            self.column = self.violation.column
            self.column_index = self.violation.column_index
            self.original_column = self.violation.original_column
            self.column_name = self.violation.column_name
            self.header = self.violation.header
            self.raw_value = self.violation.raw_value
            self.value = self.violation.value
            self.reason = self.violation.reason
            self.source_row = self.violation.source_row
            self.line_number = self.violation.line_number
            self.message = self.violation.message
        if message is None:
            if self.violation is None:
                message = "CSV column contract violation"
            else:
                message = self.violation.message
                if len(self.violations) > 1:
                    message += " (and %s more)" % (
                        len(self.violations) - 1,
                    )
        super(CSVColumnContractError, self).__init__(message)


class ColumnContractError(CSVColumnContractError):
    """Backward-friendly alias for :class:`CSVColumnContractError`."""

    pass


class IntegerAccuracyLossError(Exception):
    """
    When an integer is greater than 999999999999999, ods loses its accuracy.

    from pyexcel import Sheet, get_sheet
    s = Sheet()
    s[0,0] = 999999999999999  # 15 '9's
    print(s)
    s.save_as('abc.ods')
    b=get_sheet(file_name='abc.ods')
    b[0,0] == s[0,0]

    s = Sheet()
    s[0,0] = 9999999999999999 # 16 '9's
    print(s)
    s.save_as('abc.ods')
    b=get_sheet(file_name='abc.ods')
    b[0,0] != s[0,0]
    """

    def __init__(self, message):
        custom_message = (
            message
            + "\n"
            + "In order to keep its accuracy, please save as string. Then "
            + "convert to int, long or float after the value will be read back"
        )

        super(IntegerAccuracyLossError, self).__init__(custom_message)
