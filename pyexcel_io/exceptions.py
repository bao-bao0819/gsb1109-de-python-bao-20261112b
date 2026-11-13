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


class BufferLimitExceeded(Exception):
    """
    raised when a slow sheet reader lets the tee buffer reach its limit

    The sheet whose reader is blocking progress is carried in
    `sheet_name`, so the caller knows whom to speed up or abandon
    instead of buffering rows without bound.
    """

    def __init__(self, sheet_name, limit):
        self.sheet_name = sheet_name
        self.limit = limit
        message = (
            "Sheet reader '%s' is blocking progress: its buffer "
            "reached the agreed limit of %d rows" % (sheet_name, limit)
        )
        super(BufferLimitExceeded, self).__init__(message)


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
