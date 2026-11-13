CSV column input contracts
================================================================================

The csv reader normally guesses cell types.  Automatic guessing is convenient
for general data, but it is not suitable for identifiers and business columns:
``0007`` can be a part number, while the same spelling in a quantity column
means the integer ``7``.  A column input contract makes the interpretation
explicit per column.

Selecting a column
--------------------------------------------------------------------------------

A contract can select columns in two ways.  Select by header path when the file
has one or two header rows::

    >>> data = get_data(
    ...     "orders.csv",
    ...     column_contract={
    ...         "料号": "text",
    ...         "数量": "int",
    ...         "金额": "amount",
    ...     },
    ... )

Select by the original zero-based column position when there is no usable
header::

    >>> data = get_data(
    ...     "orders.csv",
    ...     column_contract={0: "text", 1: "int", 2: "amount"},
    ... )

Header based contracts consume the header rows.  The number of header rows is
inferred from the selectors (one level by default, two levels for a selector
such as ``("订单", "数量")``).  It can also be set explicitly::

    >>> contract = ColumnContract(
    ...     {0: "text", 1: "int"},
    ...     header_rows=1,
    ... )

Position based contracts still see every source row, including the header row.
Pass ``header_rows`` as a reader keyword to skip it, or use
``start_row``/``start_column`` for output slicing.  Position numbers always
refer to the original csv row, not to the sliced output.

Merged cells in two-level headers are supported: an empty parent cell inherits
its value from the cell to the left, so ``订单,,金额`` matches
``("订单", "金额")``.

Supported rule types
--------------------------------------------------------------------------------

``text`` (also ``string``, ``raw``, ``code``, ``sku``)
    Keeps the original spelling, including leading zeros.

``int`` (also ``integer``)
    Converts grouped integers such as ``1,234``.  A leading zero is removed by
    ``int`` conversion, so use a text rule for identifiers.

``amount`` / ``number`` / ``float``
    Accepts local thousands separators.  The separator convention is guessed
    from grouping unless it is stated explicitly with ``thousands``,
    ``decimal`` or ``locale``.

``decimal``
    Same as a number rule but returns :class:`decimal.Decimal` without losing
    trailing precision.

``date`` / ``datetime`` / ``bool``
    Parse the corresponding values.

A callable can also be supplied as a rule.  A :class:`ColumnRule` allows
options such as ``thousands=","``, ``locale="de"``, ``required=False`` or
``allow_empty=True``::

    >>> contract = ColumnContract({
    ...     "金额": ColumnRule("amount", thousands=",", decimal="."),
    ...     "备注": ColumnRule("text", required=False, allow_empty=True),
    ... })

Empty values and missing columns
--------------------------------------------------------------------------------

Typed rules reject ``""`` and cells beyond the end of a short csv row by default.
Their reasons are reported separately as ``"empty"`` and ``"missing"``.  A
configured header that does not exist in the sheet is reported as
``"missing_column"``.  Use ``required=False`` or an ``empty`` policy when that
is legitimate::

    >>> ColumnRule("amount", required=False, empty="null")

Violations and rejection
--------------------------------------------------------------------------------

By default a rejected cell keeps its exact original text in the returned data
and is recorded on the contract, so a caller can reject the record::

    >>> contract = ColumnContract({"料号": "text", "数量": "int"})
    >>> data = get_data("orders.csv", column_contract=contract)
    >>> for violation in contract.violations:
    ...     print(violation.row, violation.column, violation.raw_value)

Each violation exposes:

* ``row``: zero-based data row, not counting the header rows
* ``source_row``: zero-based row in the original csv
* ``column``: zero-based original column position
* ``raw_value``: the exact rejected spelling
* ``reason``: ``invalid``, ``empty``, ``missing`` or ``missing_column``
* ``line_number``: one-based physical line where the row starts

Pass ``strict=True`` to raise :class:`CSVColumnContractError` on the first
violating row instead.  The exception carries the same fields as its first
violation in ``error.violation`` and all of them in ``error.violations``.

Both entry points behave the same
--------------------------------------------------------------------------------

The physical-file, content-bytes and memory-stream readers all use the same
contract implementation.  The same data therefore produces the same rows,
conversion errors and original row/column locations no matter which entry
point was used.
