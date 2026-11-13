# -*- coding: utf-8 -*-
"""Column contract for quote-collection XLS exports.

The contract separates three kinds of columns:

* ``TEXT`` -- remark text.  Values are stored verbatim, even when they
  look like a formula (``"=1+1"`` stays the string ``"=1+1"``).
* ``INPUT`` -- business inputs registered for the quote.  Values are
  stored as data and are never reinterpreted as expressions.
* ``COMPUTED`` -- calculated columns registered by the business.  Only
  the agreed expression template is accepted, and it may only
  reference the declared inputs of the same row.  References are bound
  by business field name at export time, so inserting a column never
  changes which field a reference points to.

Cached results supplied by upstream may only be used as a cross-check;
they are never silently written as a recomputable formula.  Cells that
violate the contract are reported through :class:`Violation` while the
remaining independent rows are exported as usual.
"""

import ast
import operator
import re

import xlwt
from xlwt.Utils import rowcol_to_cell

TEXT = "text"
INPUT = "input"
COMPUTED = "computed"

_KINDS = (TEXT, INPUT, COMPUTED)

_REF_RE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")

# Violation codes.
EXPRESSION_REJECTED = "expression_rejected"
CACHED_MISMATCH = "cached_mismatch"
UNEXPECTED_FIELD = "unexpected_field"
ROW_ERROR = "row_error"


class ContractError(ValueError):
    """Raised when a column contract itself is inconsistent."""


class Violation(object):
    """A single non-compliant cell (or row) found during export."""

    def __init__(self, row, col, field, code, message):
        self.row = row
        self.col = col
        self.field = field
        self.code = code
        self.message = message

    @property
    def cell(self):
        """Sheet coordinates ``(row, col)`` of the offending cell."""
        return (self.row, self.col)

    @property
    def address(self):
        if self.col is None:
            return None
        return rowcol_to_cell(self.row, self.col)

    def __repr__(self):
        return "Violation(row=%r, col=%r, field=%r, code=%r)" % (
            self.row, self.col, self.field, self.code)


class Column(object):
    """One column of the contract.

    ``expression`` is a template for COMPUTED columns such as
    ``"{unit_price}*{quantity}"``; ``{field}`` placeholders reference
    other business fields of the same row.  ``allowed_refs`` restricts
    which fields the expression may reference (defaults to exactly the
    fields referenced by the template).
    """

    def __init__(self, name, kind=INPUT, header=None, expression=None,
                 allowed_refs=None):
        if not name or not isinstance(name, str):
            raise ContractError("column name must be a non-empty string")
        if kind not in _KINDS:
            raise ContractError("unknown column kind %r" % (kind,))
        if kind == COMPUTED:
            if not expression or not isinstance(expression, str):
                raise ContractError(
                    "computed column %r needs an expression template" % name)
        elif expression is not None:
            raise ContractError(
                "column %r of kind %r must not carry an expression"
                % (name, kind))
        self.name = name
        self.kind = kind
        self.header = header if header is not None else name
        self.expression = expression
        self.allowed_refs = frozenset(allowed_refs) if allowed_refs else None

    @property
    def refs(self):
        if self.expression is None:
            return frozenset()
        return frozenset(_REF_RE.findall(self.expression))

    def __repr__(self):
        return "Column(%r, kind=%r)" % (self.name, self.kind)


class ColumnContract(object):
    """An ordered, validated set of columns for the export."""

    def __init__(self, columns):
        self._columns = []
        for column in columns:
            self.insert_column(column)

    def insert_column(self, column, before=None):
        """Add ``column`` before the field/position ``before``.

        Reference attribution is checked by business field name, so
        inserting a column does not disturb existing computed columns.
        """
        if not isinstance(column, Column):
            raise ContractError("expected a Column, got %r" % (column,))
        if column.name in self.names():
            raise ContractError("duplicate column name %r" % column.name)
        if before is None:
            index = len(self._columns)
        elif isinstance(before, int):
            index = before
        else:
            try:
                index = self.names().index(before)
            except ValueError:
                raise ContractError("unknown field %r" % (before,))
        self._columns.insert(index, column)
        try:
            self._validate()
        except Exception:
            del self._columns[index]
            raise
        return column

    def _validate(self):
        names = self.names()
        for column in self._columns:
            if column.kind != COMPUTED:
                continue
            refs = column.refs
            if column.allowed_refs is not None and \
                    not refs <= column.allowed_refs:
                raise ContractError(
                    "column %r references %s outside its allowed inputs %s"
                    % (column.name,
                       sorted(refs - column.allowed_refs),
                       sorted(column.allowed_refs)))
            unknown = refs - set(names)
            if unknown:
                raise ContractError(
                    "column %r references unknown fields %s"
                    % (column.name, sorted(unknown)))
            if column.name in refs:
                raise ContractError(
                    "column %r references itself" % column.name)

    def names(self):
        return [c.name for c in self._columns]

    def index(self, name):
        return self.names().index(name)

    def column(self, name):
        return self._columns[self.index(name)]

    def columns(self):
        return list(self._columns)

    def __len__(self):
        return len(self._columns)


class ExportResult(object):
    """Outcome of an export run."""

    def __init__(self, violations, rows_exported):
        self.violations = violations
        self.rows_exported = rows_exported

    @property
    def ok(self):
        return not self.violations

    def cells(self):
        """Sheet coordinates of all flagged cells."""
        return [v.cell for v in self.violations if v.col is not None]

    def violations_for(self, field=None, code=None):
        return [v for v in self.violations
                if (field is None or v.field == field)
                and (code is None or v.code == code)]


_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
}
_UNARY_OPS = {ast.UAdd: operator.pos, ast.USub: operator.neg}


class _NotEvaluable(Exception):
    pass


def _evaluate_node(node, values):
    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body, values)
    if isinstance(node, ast.BinOp) and type(node.op) in _BIN_OPS:
        return _BIN_OPS[type(node.op)](_evaluate_node(node.left, values),
                                       _evaluate_node(node.right, values))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _UNARY_OPS:
        return _UNARY_OPS[type(node.op)](_evaluate_node(node.operand, values))
    if isinstance(node, ast.Name):
        try:
            raw = values[node.id]
        except KeyError:
            raise _NotEvaluable("unknown name %r" % node.id)
        try:
            return float(raw)
        except (TypeError, ValueError):
            raise _NotEvaluable("non-numeric value for %r" % node.id)
    _num = getattr(ast, "Num", None)  # Python < 3.8
    if _num is not None and isinstance(node, _num):
        return float(node.n)
    if isinstance(node, ast.Constant) and \
            isinstance(node.value, (int, float)):
        return float(node.value)
    raise _NotEvaluable("unsupported expression element %r"
                        % type(node).__name__)


def evaluate_expression(template, values):
    """Evaluate a template against row values; ``None`` if not evaluable."""
    expr = _REF_RE.sub(lambda m: m.group(1), template)
    try:
        tree = ast.parse(expr, mode="eval")
        return _evaluate_node(tree, values)
    except (_NotEvaluable, SyntaxError, ZeroDivisionError):
        return None


def render_expression(contract, template, sheet_row):
    """Bind ``{field}`` placeholders to A1 references of ``sheet_row``."""
    return _REF_RE.sub(
        lambda m: rowcol_to_cell(sheet_row, contract.index(m.group(1))),
        template)


def _normalize_expression(text):
    text = text.strip()
    if text.startswith("="):
        text = text[1:]
    return re.sub(r"\s+", "", text)


class ContractExporter(object):
    """Writes records to a worksheet under a :class:`ColumnContract`."""

    def __init__(self, contract, header_style=xlwt.Style.default_style,
                 tolerance=1e-9):
        if not isinstance(contract, ColumnContract):
            raise ContractError("expected a ColumnContract")
        self.contract = contract
        self.header_style = header_style
        self.tolerance = tolerance

    def export(self, records, sheet, cached=None, start_row=0):
        """Export ``records`` to ``sheet``; returns an :class:`ExportResult`.

        ``cached`` may map a record index to ``{field: value}`` with
        upstream cached results; those are only cross-checked, never
        written as formulas.  A failing record never blocks the export
        of the remaining independent rows.
        """
        violations = []
        rows_exported = 0
        for colx, column in enumerate(self.contract.columns()):
            sheet.write(start_row, colx, column.header, self.header_style)
        for record_index, record in enumerate(records):
            sheet_row = start_row + 1 + record_index
            cached_row = {}
            if cached is not None:
                cached_row = cached.get(record_index, {}) or {}
            try:
                self._export_record(record, cached_row, sheet, sheet_row,
                                    violations)
                rows_exported += 1
            except Exception as exc:
                violations.append(Violation(
                    sheet_row, None, None, ROW_ERROR,
                    "row %d failed: %s" % (record_index, exc)))
        return ExportResult(violations, rows_exported)

    def _export_record(self, record, cached_row, sheet, sheet_row,
                       violations):
        if not hasattr(record, "get") or not hasattr(record, "keys"):
            raise TypeError("record must be a mapping, got %r"
                            % type(record).__name__)
        known = set(self.contract.names())
        for key in record.keys():
            if key not in known and not key.startswith("_"):
                violations.append(Violation(
                    sheet_row, None, key, UNEXPECTED_FIELD,
                    "field %r is not part of the contract" % (key,)))
        for colx, column in enumerate(self.contract.columns()):
            value = record.get(column.name)
            if column.kind == COMPUTED:
                self._export_computed(column, value, cached_row, sheet,
                                      sheet_row, colx, record, violations)
            elif column.kind == TEXT:
                # Remark text is stored verbatim, even when it looks
                # like a formula.
                sheet.write(sheet_row, colx,
                            "" if value is None else str(value))
            else:
                # Inputs are data; they never become expressions.
                sheet.write(sheet_row, colx,
                            "" if value is None else value)

    def _export_computed(self, column, value, cached_row, sheet, sheet_row,
                         colx, record, violations):
        template = column.expression
        cached_value = cached_row.get(column.name)
        if value is not None:
            if isinstance(value, str):
                if _normalize_expression(value) != \
                        _normalize_expression(template):
                    try:
                        cached_value = float(value)
                    except ValueError:
                        violations.append(Violation(
                            sheet_row, colx, column.name,
                            EXPRESSION_REJECTED,
                            "only the agreed expression %r is accepted, "
                            "got %r" % (template, value)))
            else:
                cached_value = value
        if cached_value is not None:
            self._cross_check(column, cached_value, record, sheet_row, colx,
                              violations)
        # The contracted formula is the only thing ever written here;
        # cached results are never turned into a recomputable formula.
        formula_text = render_expression(self.contract, template, sheet_row)
        sheet.write(sheet_row, colx, xlwt.Formula(formula_text))

    def _cross_check(self, column, cached_value, record, sheet_row, colx,
                     violations):
        try:
            cached_number = float(cached_value)
        except (TypeError, ValueError):
            return
        values = {ref: record.get(ref) for ref in column.refs}
        expected = evaluate_expression(column.expression, values)
        if expected is None:
            return
        if abs(expected - cached_number) > self.tolerance:
            violations.append(Violation(
                sheet_row, colx, column.name, CACHED_MISMATCH,
                "cached result %r disagrees with contracted expression "
                "(expected %r)" % (cached_value, expected)))
