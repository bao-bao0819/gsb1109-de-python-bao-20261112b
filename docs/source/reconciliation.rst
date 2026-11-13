Incremental row reconciliation
================================================================================

A supplier may rename a sheet, reorder the sheets or split the data of one
region into two sheets between monthly deliveries.  Sheet names and sheet
positions are therefore not trustworthy identifiers.  The functions in this
page compare the rows themselves, so that layout changes do not look like
deleted data.

The caller declares two things:

#. The **row identity**: one column, a set of columns or a callable that
   uniquely identifies a row.
#. The **coverage scope** of each sheet, for example the region the sheet
   belongs to.  A scope is only considered delivered when a sheet mapping to
   it is present, or when the caller explicitly claims full coverage.

Read the two deliveries with :func:`get_data` first:

.. code-block:: python

   >>> from pyexcel_io import get_data, reconcile_rows
   >>> previous = get_data("last_month.xlsx")
   >>> current = get_data("this_month.xlsx")

The first column holds the article number, so it is used as the row
identity:

.. code-block:: python

   >>> result = reconcile_rows(previous, current, identity_columns=0)

Verdicts
--------------------------------------------------------------------------------

The returned :class:`~pyexcel_io.reconcile.ReconcileResult` groups every row
record into one of seven buckets:

=========================== ===================================================
Bucket                      Meaning
=========================== ===================================================
``unchanged``               Same row value, still delivered from the same
                            sheet.  The row can be reused as is.
``migrated``                Same row value, but the delivering sheet changed,
                            for example after a rename or a split.  Migrate the
                            stored provenance instead of re-collecting.
``changed``                 Same identity and scope, but the row value
                            changed.  The row has to be re-collected.
``new``                     Identity that did not exist in the previous
                            delivery.
``missing``                 Identity disappeared from a scope that the
                            current delivery explicitly covers in full.
``conflicts``               Two sheets claim the same identity in the same
                            scope with different values.  Every source is kept.
``untouched``               Previous rows of scopes that the current delivery
                            does not cover.  No verdict is made about them.
=========================== ===================================================

``changed``, ``missing`` and ``conflicts`` are also available through the
``needs_recollection`` property.

Coverage scopes and missing sheets
--------------------------------------------------------------------------------

By default each sheet name is its own scope.  When the supplier renames
``north-jan`` to ``north-feb``, pass a ``scope_func`` that maps both names to
the same region scope:

.. code-block:: python

   >>> def region_scope(sheet_name, rows):
   ...     return sheet_name.split("-")[0]
   >>> result = reconcile_rows(
   ...     previous,
   ...     current,
   ...     identity_columns=0,
   ...     scope_func=region_scope,
   ... )

A missing sheet alone never deletes data.  Rows of scopes absent from the
current delivery land in ``untouched``.  Only pass ``covered_scopes`` when
the delivery explicitly promises to contain the complete scope; rows that
then cannot be found are reported as ``missing``:

.. code-block:: python

   >>> result = reconcile_rows(
   ...     previous,
   ...     current,
   ...     identity_columns=0,
   ...     covered_scopes=["north", "south"],
   ... )

Conflicting sources
--------------------------------------------------------------------------------

When two sheets claim the same article number but carry different
quantities, the record is placed in ``conflicts`` instead of picking a
winner.  Each source keeps its sheet name, zero based row index, row value
and scope, so every claim can be traced back:

.. code-block:: python

   >>> for record in result.conflicts:
   ...     for source in record.sources:
   ...         print(source.sheet_name, source.row_index, source.row)

Two sheets carrying the same identity with the same value are not a
conflict; both sources are simply retained on the record.

Custom identity and reuse
--------------------------------------------------------------------------------

Use ``identity_func`` for composite or computed identities, for example to
skip a header row.  Returning ``None`` skips the row:

.. code-block:: python

   >>> def article_identity(row):
   ...     if not row or row[0] in (None, "", "article"):
   ...         return None
   ...     return row[0]

:func:`~pyexcel_io.reconcile.extract_rows` exposes the extraction step on
its own.  Storing its output instead of the raw workbook preserves conflict
provenance and lets the next monthly run reuse the records directly:

.. code-block:: python

   >>> from pyexcel_io import extract_rows
   >>> records = extract_rows(current, article_identity)
