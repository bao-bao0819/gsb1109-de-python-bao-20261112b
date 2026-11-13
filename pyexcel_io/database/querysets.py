"""
pyexcel_io.database.querysets
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The lower level handler for querysets

:copyright: (c) 2014-2026 by C Wang
:license: New BSD License, see LICENSE for more details
"""

import datetime
from itertools import chain, product

from pyexcel_io.constants import DEFAULT_SHEET_NAME
from pyexcel_io.plugin_api.abstract_sheet import ISheet


class UnalignedListCombinationError(Exception):
    """
    Raised when the selected columns traverse independent lists
    which cannot be aligned with the chosen detail path, e.g. an
    order's parcels and the same order's notes. Combining them
    would multiply rows silently, so an explicit confirmation via
    allow_cartesian=True is required.
    """


class QuerysetsReader(ISheet):
    """turn querysets into an array"""

    def __init__(
        self,
        query_sets,
        column_names,
        detail_path=None,
        keep_empty=False,
        allow_cartesian=False,
        **keywords
    ):
        self.name = DEFAULT_SHEET_NAME
        self.__column_names = column_names
        self.__query_sets = query_sets
        self.__detail_path = detail_path
        self.__keep_empty = keep_empty
        self.__allow_cartesian = allow_cartesian

    def row_iterator(self):
        if self.__detail_path:
            return self.__detail_row_iterator()
        return chain([self.__column_names], self.__query_sets)

    def to_array(self):
        """
        Convert query sets into an array
        """
        if len(self.__query_sets) == 0:
            yield []

        for row in self.row_iterator():
            row_values = []
            for value in self.column_iterator(row):
                row_values.append(value)
            yield row_values

    def column_iterator(self, row):
        if self.__column_names is None:
            return

        if isinstance(row, list):
            for element in row:
                yield element

        elif isinstance(row, _ExpandedRow):
            for value in row.values:
                yield value

        else:
            for column in self.__column_names:
                if "__" in column:
                    value = get_complex_attribute(row, column)
                else:
                    value = get_simple_attribute(row, column)
                yield value

    def __detail_row_iterator(self):
        yield self.__column_names
        for item in self.__query_sets:
            for context in self.__walk_detail_path(item):
                values_per_column = []
                unaligned_columns = []
                for column in self.__column_names:
                    values, aligned = resolve_column(
                        context, column, self.__detail_path
                    )
                    values_per_column.append(values)
                    if not aligned:
                        unaligned_columns.append(column)
                if unaligned_columns and not self.__allow_cartesian:
                    raise UnalignedListCombinationError(
                        f"Columns {unaligned_columns} traverse lists "
                        f"that cannot be aligned with detail_path "
                        f"'{self.__detail_path}'. Align them by extending "
                        f"detail_path, or confirm the combination with "
                        f"allow_cartesian=True."
                    )
                for combination in product(*values_per_column):
                    yield _ExpandedRow(list(combination))

    def __walk_detail_path(self, item):
        segments = self.__detail_path.split("__")
        contexts = [{"": item}]
        prefix = ""
        for segment in segments:
            parent_prefix = prefix
            prefix = segment if not prefix else prefix + "__" + segment
            next_contexts = []
            for context in contexts:
                children = to_children(
                    safe_get_attribute(context[parent_prefix], segment)
                )
                if children:
                    for child in children:
                        branch = dict(context)
                        branch[prefix] = child
                        next_contexts.append(branch)
                elif self.__keep_empty:
                    next_contexts.append(context)
            contexts = next_contexts
            if not contexts:
                break
        return contexts


class _ExpandedRow:
    """a fully resolved row ready for column iteration"""

    def __init__(self, values):
        self.values = values


def resolve_column(context, column, detail_path):
    """
    Resolve a column against a detail context.

    Returns a tuple (values, aligned): values holds one value per
    matching leaf, aligned is False when the column traverses a list
    which is not part of the detail path.
    """
    parts = column.split("__")
    value = None
    rest = parts
    prefix = ""
    for index in range(len(parts), -1, -1):
        candidate = "__".join(parts[:index])
        if candidate in context:
            value = context[candidate]
            rest = parts[index:]
            prefix = candidate
            break
    values = [value]
    aligned = True
    for attribute in rest:
        prefix = attribute if not prefix else prefix + "__" + attribute
        next_values = []
        for item in values:
            child = safe_get_attribute(item, attribute)
            if is_list_like(child):
                children = to_children(child)
                if children and not is_on_detail_path(detail_path, prefix):
                    aligned = False
                next_values.extend(children if children else [None])
            else:
                next_values.append(child)
        values = next_values
    return values, aligned


def is_on_detail_path(detail_path, prefix):
    """is the prefix a segment of the detail path?"""
    return detail_path == prefix or detail_path.startswith(prefix + "__")


def is_list_like(value):
    """is the value a related list rather than a scalar?"""
    if isinstance(value, (list, tuple)):
        return True
    if isinstance(value, (str, bytes, dict)):
        return False
    return callable(getattr(value, "all", None))


def to_children(value):
    """turn a related attribute into a list of child objects"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    all_method = getattr(value, "all", None)
    if callable(all_method):
        return list(all_method())
    return [value]


def safe_get_attribute(row, attribute):
    """get an attribute, None when it does not exist"""
    if row is None:
        return None
    try:
        return get_simple_attribute(row, attribute)
    except AttributeError:
        return None


def get_complex_attribute(row, attribute):
    """recursively get an attribute"""
    attributes = attribute.split("__")
    value = row
    try:
        for attributee in attributes:
            value = get_simple_attribute(value, attributee)
    except AttributeError:
        value = None
    return value


def get_simple_attribute(row, attribute):
    """get dotted attribute"""
    value = getattr(row, attribute)
    if isinstance(value, (datetime.date, datetime.time)):
        value = value.isoformat()
    return value
