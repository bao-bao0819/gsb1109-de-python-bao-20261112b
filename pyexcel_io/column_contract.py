"""
pyexcel_io.column_contract
~~~~~~~~~~~~~~~~~~~~~~~~~~

Column level input contracts for the csv readers.

A contract assigns an interpretation to individual columns.  A column can be
selected by a header path (one or two header rows) or by its original
zero-based position in the csv row.  Text rules never run automatic number
detection, so identifiers such as ``0007`` keep their spelling.  A typed rule
converts values such as ``0007`` to the integer ``7``.

A value that cannot be converted is left exactly as it appeared in the source
and recorded as a :class:`ColumnContractViolation`.  This lets a caller reject
a record without losing the original spelling.  Empty cells, short rows and
configured-but-absent header columns have separate reasons.

All csv entry points (physical file, file content and memory stream) use the
same implementation through :class:`pyexcel_io.readers.csv_sheet.
CSVSheetReader`.
"""

import datetime
import re
from decimal import Decimal, InvalidOperation

import pyexcel_io.service as service
from pyexcel_io.exceptions import (
    ColumnContractConfigError,
    ColumnContractViolation,
    CSVColumnContractError,
)

EMPTY = "empty"
INVALID = "invalid"
MISSING = "missing"
MISSING_COLUMN = "missing_column"

TEXT_TYPES = frozenset(
    ("string", "str", "text", "raw", "literal", "code", "sku", "文本")
)
INTEGER_TYPES = frozenset(("int", "integer", "long", "whole", "整数"))
NUMBER_TYPES = frozenset(
    ("number", "float", "double", "amount", "numeric", "数量", "金额", "数值")
)
DECIMAL_TYPES = frozenset(("decimal", "money", "currency"))
DATE_TYPES = frozenset(("date", "日期"))
DATETIME_TYPES = frozenset(("datetime", "timestamp", "日期时间"))
BOOL_TYPES = frozenset(("bool", "boolean", "布尔"))

TRUE_TEXT = ("true", "yes", "y", "1", "on", "是", "真")
FALSE_TEXT = ("false", "no", "n", "0", "off", "否", "假")
CURRENCY_SYMBOLS = "$￥¥€£₤₹"
SPACE_GROUPING = (" ", " ", " ", " ", "'")

_INTEGER_TEXT = re.compile(r"^[+-]?[0-9]+$")
_NUMBER_TEXT = re.compile(
    r"^[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$"
)


class HeaderSelector(object):
    """Select a column by a one or two level header path."""

    def __init__(self, *path):
        if len(path) == 1 and isinstance(path[0], (list, tuple)):
            path = tuple(path[0])
        self.path = tuple(_header_text(part) for part in path)
        if not self.path or any(part == "" for part in self.path):
            raise ColumnContractConfigError(
                "A header selector needs at least one non-empty header name"
            )

    def __hash__(self):
        return hash(self.path)

    def __eq__(self, other):
        if isinstance(other, HeaderSelector):
            return self.path == other.path
        if isinstance(other, (list, tuple)):
            return self.path == tuple(other)
        return False

    def __repr__(self):
        return "HeaderSelector%r" % (self.path,)


class PositionSelector(object):
    """Select a column by its zero-based position in the source row."""

    def __init__(self, position):
        if isinstance(position, bool) or not isinstance(position, int):
            raise ColumnContractConfigError(
                "A position selector must be an integer, got %r"
                % (position,)
            )
        if position < 0:
            raise ColumnContractConfigError(
                "A position selector must not be negative"
            )
        self.position = position

    def __hash__(self):
        return hash(self.position)

    def __eq__(self, other):
        if isinstance(other, PositionSelector):
            return self.position == other.position
        if isinstance(other, int) and not isinstance(other, bool):
            return self.position == other
        return False

    def __repr__(self):
        return "PositionSelector(%r)" % (self.position,)


ByName = HeaderSelector
ByPosition = PositionSelector
ColumnPosition = PositionSelector


class ColumnRule(object):
    """Describe the input contract of one column.

    :param type: a supported type name or a callable converter
    :param header: a header name or a one/two level header path
    :param position: zero-based original column position
    :param required: whether a missing cell or column is a violation
    :param allow_empty: whether ``""`` is accepted.  Defaults to true for
                        text rules and false for typed rules.
    :param thousands: an explicit thousands separator
    :param decimal: an explicit decimal separator
    :param strip: strip surrounding whitespace before conversion
    """

    def __init__(
        self,
        type="text",
        header=None,
        position=None,
        required=True,
        allow_empty=None,
        empty=None,
        thousands=None,
        decimal=None,
        strip=False,
        converter=None,
        selector=None,
        **options
    ):
        if converter is not None:
            if type not in (None, "text", "string"):
                raise ColumnContractConfigError(
                    "Specify either converter or type, not both"
                )
            type = converter
        if type is None:
            type = "text"
        if selector is not None:
            if header is not None or position is not None:
                raise ColumnContractConfigError(
                    "selector cannot be combined with header or position"
                )
            selector = normalise_selector(selector)
            if isinstance(selector, PositionSelector):
                position = selector.position
            else:
                header = selector.path
        self.type = type
        self.header = _normalise_header_path(header)
        self.position = _normalise_position(position)
        if self.header is not None and self.position is not None:
            raise ColumnContractConfigError(
                "A rule cannot select both a header and a position"
            )
        self.required = required
        self.thousands = thousands
        self.decimal = decimal
        self.strip = strip
        self.options = options
        self.empty_policy = _empty_policy(empty, allow_empty, type)
        self.empty_value = (
            empty
            if empty not in (None, "allow", "error", "null", "default")
            else None
        )

    @classmethod
    def for_header(cls, header, type="text", **options):
        return cls(type=type, header=header, **options)

    @classmethod
    def at(cls, position, type="text", **options):
        return cls(type=type, position=position, **options)

    @property
    def selector(self):
        if self.position is not None:
            return PositionSelector(self.position)
        if self.header is not None:
            return HeaderSelector(self.header)
        return None

    def __repr__(self):
        return "ColumnRule(type=%r, selector=%r)" % (
            self.type,
            self.selector,
        )


ColumnSpec = ColumnRule
CellRule = ColumnRule


class ColumnContract(object):
    """A set of column-level input rules for one csv sheet.

    ``rules`` may be a mapping or a sequence of :class:`ColumnRule` objects.
    Mapping keys select columns:

    * a string, e.g. ``{"料号": "text"}``
    * a tuple/list for a two-level header, e.g.
      ``{("订单", "数量"): "int"}``
    * an integer for the original zero-based column position

    A nested mapping is shorthand for a multi-level header::

        {"订单": {"料号": "text", "数量": "int"}}

    ``header_rows`` states how many leading rows are header rows.  It defaults
    to the deepest header selector, and header rows are consumed before data
    rows are returned.  Pass ``headers=[...]`` to supply header values instead.

    Violations are collected by default.  ``strict=True`` raises
    :class:`CSVColumnContractError` on the first violating row.
    """

    def __init__(
        self,
        rules=None,
        header_rows=None,
        headers=None,
        strict=False,
        on_error=None,
        consume_header_rows=None,
        columns=None,
        schema=None,
        header_row_count=None,
        **options
    ):
        if columns is not None:
            if rules is not None:
                raise ColumnContractConfigError(
                    "Specify either rules or columns, not both"
                )
            rules = columns
        if schema is not None:
            if rules is not None:
                raise ColumnContractConfigError(
                    "Specify either rules or schema, not both"
                )
            rules = schema
        if header_row_count is not None:
            if header_rows is not None:
                raise ColumnContractConfigError(
                    "Specify header_rows or header_row_count, not both"
                )
            header_rows = header_row_count
        if isinstance(rules, ColumnContract):
            self.rules = list(rules.rules)
            if header_rows is None:
                header_rows = rules.header_rows
            if headers is None:
                headers = rules.headers
        else:
            self.rules = _normalise_rules(rules)
        self.headers = normalise_headers(headers)
        self.header_rows = normalise_header_rows(header_rows)
        if self.header_rows is None:
            self.header_rows = _infer_header_rows(self.rules, self.headers)
        if consume_header_rows is None:
            consume_header_rows = bool(self.header_rows)
        self.consume_header_rows = bool(consume_header_rows)
        if on_error is not None:
            strict = str(on_error).lower() in ("raise", "strict", "error")
        self.strict = bool(strict)
        self.options = options
        self._bound = []
        self._validate_duplicates()

    @classmethod
    def from_mapping(cls, mapping, **options):
        return cls(rules=mapping, **options)

    @classmethod
    def from_rules(cls, rules, **options):
        return cls(rules=rules, **options)

    def add(self, selector, rule=None, **options):
        if isinstance(selector, ColumnRule):
            self.rules.append(selector)
        else:
            if rule is None:
                rule = "text"
            self.rules.append(make_rule(selector, rule, options))
        self._validate_duplicates()
        return self

    def bind(self, headers=None):
        bound = BoundColumnContract(self, headers)
        self._bound = [bound]
        return bound

    def begin_read(self):
        """Start a fresh violation collection for a new read."""
        self._bound = []

    @property
    def violations(self):
        """Violations from the most recent read through this contract."""
        if self._bound:
            return list(self._bound[-1].violations)
        return []

    @property
    def errors(self):
        return self.violations

    @property
    def violations_by_row(self):
        grouped = {}
        for violation in self.violations:
            grouped.setdefault(violation.row, []).append(violation)
        return grouped

    def reset(self):
        self._bound = []

    def take_violations(self):
        violations = self.violations
        self.reset()
        return violations

    def _validate_duplicates(self):
        targets = set()
        for rule in self.rules:
            target = ("position", rule.position) if (
                rule.position is not None
            ) else ("header", rule.header)
            if target in targets:
                raise ColumnContractConfigError(
                    "More than one contract rule targets %r" % (target,)
                )
            targets.add(target)

    def __repr__(self):
        return "ColumnContract(rules=%r, header_rows=%r)" % (
            self.rules,
            self.header_rows,
        )


class BoundColumnContract(object):
    """A contract resolved against the header rows of one sheet."""

    def __init__(self, contract, headers=None):
        self.contract = contract
        if headers is None:
            headers = contract.headers
        self.headers = normalise_headers(headers)
        self.position_rules = {}
        self.unmatched_rules = []
        self.violations = []
        self._resolve()

    def _resolve(self):
        for rule in self.contract.rules:
            if rule.position is not None:
                self._register(rule.position, rule)
                continue
            index = find_header_index(self.headers, rule.header)
            if index is None:
                self.unmatched_rules.append(rule)
            else:
                self._register(index, rule)

    def _register(self, index, rule):
        if index in self.position_rules:
            raise ColumnContractConfigError(
                "More than one contract rule targets original column %s"
                % (index,)
            )
        self.position_rules[index] = rule

    def header_name(self, column):
        path = header_path_at(self.headers, column)
        if path:
            return "/".join(path)
        rule = self.position_rules.get(column)
        if rule is not None:
            return rule.header and "/".join(rule.header)
        return None

    def convert_row(self, row, data_row=0, source_row=None, line_number=None):
        """Convert one row and return ``(converted_row, violations)``."""
        row = list(row)
        converted = list(row)
        violations = []
        for column, rule in self.position_rules.items():
            if column >= len(row):
                if rule.required:
                    violations.append(
                        self._violation(
                            data_row,
                            column,
                            None,
                            MISSING,
                            rule,
                            source_row,
                            line_number,
                        )
                    )
                continue
            value, violation = self.convert_cell(
                row[column],
                rule,
                column,
                data_row,
                source_row,
                line_number,
            )
            if violation is not None:
                violations.append(violation)
            else:
                converted[column] = value

        for rule in self.unmatched_rules:
            if rule.required:
                violations.append(
                    self._violation(
                        data_row,
                        None,
                        None,
                        MISSING_COLUMN,
                        rule,
                        source_row,
                        line_number,
                    )
                )
        self.violations.extend(violations)
        return converted, violations

    def convert_cell(
        self,
        raw_value,
        rule,
        column,
        data_row=0,
        source_row=None,
        line_number=None,
    ):
        if raw_value is None or raw_value == "":
            if raw_value is None and not rule.required:
                return raw_value, None
            if rule.empty_policy == "allow":
                return raw_value, None
            if rule.empty_policy == "null":
                return None, None
            if rule.empty_policy == "default":
                if rule.empty_value is not None:
                    return rule.empty_value, None
            if not rule.required and raw_value == "":
                return raw_value, None
            reason = MISSING if raw_value is None else EMPTY
            return raw_value, self._violation(
                data_row,
                column,
                raw_value,
                reason,
                rule,
                source_row,
                line_number,
            )

        value, error = convert_value(raw_value, rule)
        if error is not None:
            return raw_value, self._violation(
                data_row,
                column,
                raw_value,
                INVALID,
                rule,
                source_row,
                line_number,
                error,
            )
        return value, None

    def _violation(
        self,
        row,
        column,
        raw_value,
        reason,
        rule,
        source_row=None,
        line_number=None,
        detail=None,
    ):
        name = self.header_name(column) if column is not None else None
        if name is None and rule.header:
            name = "/".join(rule.header)
        return ColumnContractViolation(
            row=row,
            column=column,
            raw_value=raw_value,
            reason=reason,
            column_name=name,
            header=name,
            rule=rule,
            source_row=source_row,
            line_number=line_number,
            message=_violation_message(
                row, column, raw_value, reason, name, rule, detail
            ),
        )


ColumnInputContract = ColumnContract
InputContract = ColumnContract
CSVColumnContract = ColumnContract
ColumnContractError = CSVColumnContractError
ColumnContractViolationError = CSVColumnContractError
ContractViolationError = CSVColumnContractError


def make_contract(rules=None, **options):
    return ColumnContract(rules=rules, **options)


column_contract = make_contract


def normalise_selector(selector):
    if isinstance(selector, (HeaderSelector, PositionSelector)):
        return selector
    if isinstance(selector, bool):
        raise ColumnContractConfigError(
            "Boolean values are not valid column selectors"
        )
    if isinstance(selector, int):
        return PositionSelector(selector)
    if isinstance(selector, (list, tuple)):
        return HeaderSelector(selector)
    if isinstance(selector, str):
        return HeaderSelector(selector)
    raise ColumnContractConfigError(
        "Unsupported column selector %r" % (selector,)
    )


def normalise_header_rows(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ColumnContractConfigError(
            "header_rows must be a non-negative integer"
        )
    if value < 0:
        raise ColumnContractConfigError(
            "header_rows must be a non-negative integer"
        )
    return value


def normalise_headers(headers):
    if headers is None:
        return []
    if isinstance(headers, str):
        return [[headers]]
    if not isinstance(headers, (list, tuple)):
        raise ColumnContractConfigError(
            "headers must be a list of header rows"
        )
    if not headers:
        return []
    if isinstance(headers[0], (list, tuple)):
        return [[_header_text(cell) for cell in row] for row in headers]
    return [[_header_text(cell) for cell in headers]]


def _infer_header_rows(rules, headers):
    if headers:
        return 0
    deepest = 0
    for rule in rules:
        if rule.header is not None:
            deepest = max(deepest, len(rule.header))
    return deepest


def _normalise_header_path(header):
    if header is None:
        return None
    if isinstance(header, HeaderSelector):
        return header.path
    if isinstance(header, (list, tuple)):
        path = tuple(_header_text(part) for part in header)
    else:
        path = (_header_text(header),)
    if not path or any(part == "" for part in path):
        raise ColumnContractConfigError(
            "A header selector needs at least one non-empty header name"
        )
    return path


def _normalise_position(position):
    if position is None:
        return None
    if isinstance(position, PositionSelector):
        position = position.position
    if isinstance(position, bool) or not isinstance(position, int):
        raise ColumnContractConfigError(
            "Column position must be an integer, got %r" % (position,)
        )
    if position < 0:
        raise ColumnContractConfigError(
            "Column position must not be negative"
        )
    return position


def _normalise_rules(rules):
    if rules is None:
        return []
    if isinstance(rules, ColumnContract):
        return list(rules.rules)
    if isinstance(rules, ColumnRule):
        return [rules]
    if isinstance(rules, dict):
        return _rules_from_mapping(rules, ())
    if isinstance(rules, (list, tuple)):
        collected = []
        for item in rules:
            if isinstance(item, ColumnRule):
                collected.append(item)
            elif isinstance(item, dict):
                if _looks_like_rule(item):
                    collected.append(rule_from_dict(item))
                else:
                    collected.extend(_rules_from_mapping(item, ()))
            else:
                raise ColumnContractConfigError(
                    "Cannot interpret column rule %r" % (item,)
                )
        return collected
    raise ColumnContractConfigError(
        "Cannot interpret column rules %r" % (rules,)
    )


def _rules_from_mapping(mapping, prefix):
    collected = []
    for key, definition in mapping.items():
        if isinstance(key, (list, tuple)):
            path = prefix + tuple(_header_text(part) for part in key)
        elif isinstance(key, int) and not isinstance(key, bool):
            path = None
        else:
            path = prefix + (_header_text(key),)

        if isinstance(definition, dict) and not _looks_like_rule(definition):
            collected.extend(
                _rules_from_mapping(definition, path if path else prefix)
            )
        elif isinstance(definition, dict):
            definition = dict(definition)
            if path is None:
                definition.setdefault("position", key)
            else:
                definition.setdefault("header", path)
            collected.append(rule_from_dict(definition))
        elif isinstance(definition, ColumnRule):
            collected.append(
                rebind_rule(
                    definition,
                    header=None if path is None else path,
                    position=key if path is None else None,
                )
            )
        else:
            target = key if path is None else path
            collected.append(make_rule(target, definition, {}))
    return collected


def make_rule(selector, definition, options):
    if isinstance(definition, ColumnRule):
        target = _selector_target(selector)
        return rebind_rule(
            definition,
            header=target.get("header"),
            position=target.get("position"),
        )
    if isinstance(definition, dict):
        definition = dict(definition)
        definition.setdefault("type", "text")
        target = _selector_target(selector)
        definition.update(target)
        definition.update(options)
        return rule_from_dict(definition)
    target = _selector_target(selector)
    target["type"] = definition
    return ColumnRule(**target, **options)


def _selector_target(selector):
    selector = normalise_selector(selector)
    if isinstance(selector, PositionSelector):
        return {"position": selector.position}
    return {"header": selector.path}


def rebind_rule(rule, header=None, position=None):
    options = {
        "type": rule.type,
        "required": rule.required,
        "thousands": rule.thousands,
        "decimal": rule.decimal,
        "strip": rule.strip,
    }
    if rule.empty_policy == "allow":
        options["allow_empty"] = True
    elif rule.empty_policy == "null":
        options["empty"] = "null"
    elif rule.empty_value is not None:
        options["empty"] = rule.empty_value
    options.update(rule.options)
    if position is not None:
        return ColumnRule(position=position, **options)
    return ColumnRule(header=header, **options)


_RULE_KEYS = frozenset(
    (
        "type",
        "converter",
        "header",
        "position",
        "selector",
        "required",
        "allow_empty",
        "empty",
        "thousands",
        "decimal",
        "strip",
    )
)


def _looks_like_rule(definition):
    return bool(_RULE_KEYS.intersection(definition))


def rule_from_dict(definition):
    definition = dict(definition)
    if "type" not in definition and "converter" not in definition:
        raise ColumnContractConfigError(
            "A rule dictionary needs a type: %r" % (definition,)
        )
    return ColumnRule(**definition)


def _empty_policy(empty, allow_empty, type_name):
    if empty is not None and empty not in (
        "allow",
        "error",
        "null",
        "default",
    ):
        return "default"
    if allow_empty is not None:
        return "allow" if allow_empty else "error"
    if empty is not None:
        return empty
    if _type_name(type_name) in TEXT_TYPES:
        return "allow"
    return "error"


def _type_name(type_name):
    if callable(type_name):
        return "converter"
    return str(type_name).lower().replace("_", "-").strip()


def _header_text(value):
    if value is None:
        return ""
    return str(value).replace("﻿", "").strip()


def header_path_at(headers, column):
    return tuple(
        cell
        for cell in (
            _cell_at(row, column)
            for row in headers
        )
        if cell
    )


def _cell_at(row, column):
    if column < len(row) and row[column] is not None:
        return _header_text(row[column])
    return ""


def find_header_index(headers, selector):
    if not headers or selector is None:
        return None
    selector = tuple(selector)
    best_score = 0
    best = []
    for column in range(max(len(row) for row in headers)):
        score = _header_match_score(headers, column, selector)
        if score > best_score:
            best_score = score
            best = [column]
        elif score == best_score and score:
            best.append(column)
    if not best:
        return None
    if len(best) > 1:
        raise ColumnContractConfigError(
            "Header selector %r is ambiguous: it matches columns %s"
            % ("/".join(selector), best)
        )
    return best[0]


def _header_match_score(headers, column, selector):
    path = header_path_at(headers, column)
    filled = _filled_header_path(headers, column)
    if not path:
        return 0
    if selector == path:
        return 5000 + len(path) * 10
    if selector == filled:
        return 4900 + len(path) * 10
    if len(selector) == 2:
        if len(path) >= 2 and selector == path[-2:]:
            return 4000 + len(path) * 10
        if len(filled) >= 2 and selector == filled[-2:]:
            return 3900 + len(path) * 10
        return 0
    name = selector[0]
    if len(path) == 1 and name == path[0]:
        return 3000
    if name == path[-1]:
        return 2500 + len(path) * 10
    if name in path:
        return 2000 + len(path) * 10
    if name in (
        "/".join(path),
        "__".join(path),
        "-".join(path),
        " ".join(path),
    ):
        return 1500 + len(path) * 10
    return 0


def _filled_header_path(headers, column):
    cells = []
    for row in headers:
        cell = _cell_at(row, column)
        if not cell:
            cell = _previous_non_empty(row, column)
        if cell:
            cells.append(cell)
    return tuple(cells)


def _previous_non_empty(row, column):
    for index in range(column - 1, -1, -1):
        cell = _cell_at(row, index)
        if cell:
            return cell
    return ""


def convert_value(raw_value, rule):
    """Convert a non-empty value.  Return ``(value, error_message)``."""
    type_name = _type_name(rule.type)
    text = str(raw_value)
    if rule.strip:
        text = text.strip()
    try:
        if callable(rule.type):
            return rule.type(raw_value), None
        if type_name in TEXT_TYPES:
            return text, None
        if type_name in INTEGER_TYPES:
            return _parse_integer(text, rule), None
        if type_name in NUMBER_TYPES:
            return _parse_float(text, rule), None
        if type_name in DECIMAL_TYPES:
            return _parse_decimal(text, rule), None
        if type_name in DATE_TYPES:
            value = service.detect_date_value(text)
            if value is None:
                raise ValueError("%r is not a date" % (text,))
            if isinstance(value, datetime.datetime):
                return value.date(), None
            return value, None
        if type_name in DATETIME_TYPES:
            return _parse_datetime(text), None
        if type_name in BOOL_TYPES:
            return _parse_boolean(text, rule), None
        if type_name in ("auto", "automatic", "detect"):
            return default_convert(raw_value), None
    except (ValueError, TypeError, InvalidOperation) as exception:
        return None, str(exception)
    return None, "unsupported column type %r" % (rule.type,)


def default_convert(raw_value):
    """Apply the reader's usual automatic conversions to one cell."""
    text = str(raw_value)
    value = service.detect_int_value(text)
    if value is None:
        value = service.detect_float_value(text)
    if value is None:
        value = service.detect_date_value(text)
    if value is None:
        value = text
    return value


def _parse_integer(text, rule):
    cleaned = _strip_currency(text, rule)
    cleaned = _strip_grouping(cleaned, rule)
    if _INTEGER_TEXT.match(cleaned) is None:
        raise ValueError("%r is not an integer" % (text,))
    return int(cleaned)


def _strip_grouping(text, rule):
    thousands, _ = _separators(rule, text)
    if thousands is None:
        if "," in text:
            thousands = ","
        else:
            for separator in SPACE_GROUPING:
                if separator in text:
                    thousands = separator
                    break
    if thousands:
        _validate_grouping(text, thousands)
        text = text.replace(thousands, "")
    return text


def _parse_float(text, rule):
    normalised = _normalise_number(text, rule)
    if _NUMBER_TEXT.match(normalised) is None:
        raise ValueError("%r is not a number" % (text,))
    return float(normalised)


def _parse_decimal(text, rule):
    normalised = _normalise_number(text, rule)
    if _NUMBER_TEXT.match(normalised) is None:
        raise ValueError("%r is not a decimal" % (text,))
    return Decimal(normalised)


def _normalise_number(text, rule):
    cleaned = _strip_currency(text, rule)
    cleaned = "".join(cleaned.split())
    thousands, decimal = _separators(rule, cleaned)
    if rule.thousands is not None or rule.decimal is not None or (
        rule.options.get("locale") is not None
    ):
        if thousands:
            _validate_grouping(cleaned, thousands, decimal)
            cleaned = cleaned.replace(thousands, "")
        if decimal and decimal != ".":
            cleaned = cleaned.replace(decimal, ".")
        return cleaned
    return _guess_number(cleaned)


def _guess_number(text):
    for separator in SPACE_GROUPING:
        if separator in text:
            _validate_grouping(text, separator)
            text = text.replace(separator, "")
    has_dot = "." in text
    has_comma = "," in text
    if has_dot and has_comma:
        if text.rfind(",") > text.rfind("."):
            _validate_grouping(text, ".", ",")
            return text.replace(".", "").replace(",", ".")
        _validate_grouping(text, ",", ".")
        return text.replace(",", "")
    if has_comma:
        groups = text.split(",")
        if len(groups) > 2:
            _validate_grouping(text, ",")
            return text.replace(",", "")
        if len(groups) == 2 and len(groups[1]) == 3 and len(groups[0]) <= 3:
            return text.replace(",", "")
        return groups[0] + "." + groups[1]
    if has_dot:
        groups = text.split(".")
        if len(groups) > 2:
            if all(len(group) == 3 for group in groups[1:]):
                return text.replace(".", "")
            return ".".join(groups[:-1]) + "." + groups[-1]
    return text


def _separators(rule, text=""):
    locale_separators = _locale_separators(rule.options.get("locale"))
    if locale_separators is not None:
        thousands, decimal = locale_separators
        if rule.thousands is not None:
            thousands = rule.thousands
        if rule.decimal is not None:
            decimal = rule.decimal
        return thousands, decimal
    decimal = rule.decimal if rule.decimal is not None else "."
    thousands = rule.thousands
    if rule.decimal is None and thousands is not None:
        if thousands == ".":
            decimal = ","
    if thousands is None and rule.decimal is not None:
        if rule.decimal == "." and "," in text:
            thousands = ","
        elif rule.decimal == "," and "." in text:
            thousands = "."
    return thousands, decimal


def _locale_separators(locale):
    if locale is None:
        return None
    lowered = str(locale).lower()
    if lowered in ("de", "de-de", "de-at", "de-ch"):
        return (".", ",")
    if lowered in ("fr", "fr-fr", "fr-be"):
        return (" ", ",")
    if lowered in ("en", "en-us", "en-gb", "us", "uk", "zh", "zh-cn", "cn"):
        return (",", ".")
    raise ColumnContractConfigError(
        "Unknown locale %r for a numeric column" % (locale,)
    )


def _validate_grouping(text, thousands, decimal=None):
    if thousands not in text:
        return
    integer_part = text.split(decimal)[0] if decimal else text
    if integer_part[:1] in ("+", "-"):
        integer_part = integer_part[1:]
    groups = integer_part.split(thousands)
    if len(groups) < 2:
        return
    if not groups[0].isdigit() or not 1 <= len(groups[0]) <= 3:
        raise ValueError("%r has invalid thousands grouping" % (text,))
    for group in groups[1:]:
        if len(group) != 3 or not group.isdigit():
            raise ValueError("%r has invalid thousands grouping" % (text,))


def _strip_currency(text, rule):
    currency = rule.options.get("currency")
    if currency is False:
        return text.strip()
    if isinstance(currency, (list, tuple, set)):
        for symbol in currency:
            text = text.replace(str(symbol), "")
        return text.strip()
    if currency is not None:
        return text.replace(str(currency), "").strip()
    return text.strip().strip(CURRENCY_SYMBOLS).strip()


def _parse_boolean(text, rule):
    lowered = text.strip().lower()
    true_values = rule.options.get("true_values") or TRUE_TEXT
    false_values = rule.options.get("false_values") or FALSE_TEXT
    if lowered in [str(value).lower() for value in true_values]:
        return True
    if lowered in [str(value).lower() for value in false_values]:
        return False
    raise ValueError("%r is not a boolean" % (text,))


def _parse_datetime(text):
    formats = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y/%m/%d %H:%M:%S",
    )
    for fmt in formats:
        try:
            return datetime.datetime.strptime(text, fmt)
        except ValueError:
            pass
    raise ValueError("%r is not a datetime" % (text,))


def _violation_message(
    row, column, raw_value, reason, name, rule, detail=None
):
    location = "row %s" % (row,) if column is None else (
        "row %s, column %s" % (row, column)
    )
    if name:
        location += " (%s)" % (name,)
    if reason == MISSING:
        return "%s: cell is missing" % (location,)
    if reason == MISSING_COLUMN:
        return "%s: column %r is missing" % (location, name)
    if reason == EMPTY:
        return "%s: empty value is not allowed" % (location,)
    message = "%s: %r is not a valid %s" % (
        location,
        raw_value,
        rule.type,
    )
    if detail:
        message += " (%s)" % (detail,)
    return message
