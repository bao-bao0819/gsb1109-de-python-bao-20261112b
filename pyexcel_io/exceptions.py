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


class CustomerMappingError(Exception):
    """base class for customer number cross reference errors"""

    pass


class DuplicateCustomerMappingError(CustomerMappingError):
    """raised when one customer number is used by more than one pair"""

    def __init__(self, number, pairs):
        self.number = number
        self.pairs = pairs
        custom_message = (
            "customer number %s appears in more than one mapping "
            "pair: %s. Chained rows such as A->B and B->C must stay "
            "independent pairs and are never collapsed into A->C"
            % (number, [pair.as_tuple() for pair in pairs])
        )
        super(DuplicateCustomerMappingError, self).__init__(
            custom_message
        )


class CustomerNumberOccupiedError(CustomerMappingError):
    """raised when a mapping target is held by an outside customer"""

    def __init__(self, plan):
        self.plan = plan
        blocked = [
            group.describe() for group in plan.blocked_groups
        ]
        custom_message = (
            "mapping targets are occupied by customers outside the "
            "import: %s. The independent groups in the same import "
            "can still be committed separately" % blocked
        )
        super(CustomerNumberOccupiedError, self).__init__(
            custom_message
        )
