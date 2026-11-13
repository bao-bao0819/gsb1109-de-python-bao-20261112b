# Cross-store queries past the SQLite ATTACH limit

## The problem

The archive is split into one small SQLite database per store. A single query
can reference more store databases than SQLite allows to ATTACH at the same
time (the standard compile-time limit is 10 attached databases; q reserves two
of the slots for its own query-level and adhoc databases, leaving eight user
databases before any copy is needed).

Two tempting shortcuts are wrong:

* **Compute a number per store and concatenate the numbers.** A customer can
  appear in several stores, and the right table may contain several matching
  rows per join key. Per-store aggregation happens *before* the join, so
  cross-store matches, duplicate fan-out and conditional `LEFT JOIN` NULLs are
  all lost.
* **Merge the archives into one big database.** The source databases are
  read-only; merging also means copying data and keeping the copy in sync.

## How q handles the limit

Every table reference runs inside one connected in-memory query database.

1. While the number of attached databases is below
   `--max-attached-sqlite-databases`, q ATTACHes the store database directly
   and queries it from disk.
2. Once the limit is reached, q switches per table to an
   attach -> `CREATE TABLE ... AS SELECT *` -> detach cycle into its private
   adhoc database (`attach_and_copy_table` in `bin/q.py`). The copied table
   lives in the adhoc database, which is already attached, so it consumes no
   additional ATTACH slot.

Because the copied tables and the directly attached tables are all visible in
the same connected database, joins, `GROUP BY`, `WHERE`/`HAVING` and window
functions see the union of all stores at once. There is no per-store
aggregation step. On modern SQLite (3.31+) `CREATE TABLE AS SELECT` preserves
the source column type names and affinity, so joins on keys stored as TEXT in
one store and INTEGER in another keep the same comparison semantics as the
direct-attach path.

Store databases are opened with an `immutable=1` URI and are only ever attached
or briefly attached for copying; they are never written. The regression tests
hash every source file before and after each run to enforce this.

## Query shape

Turn each logical table into one `UNION ALL` of the per-store tables, then
write the join against the two unions. Put right-side filters in the `ON`
clause so unmatched left rows survive as NULL rows.

```bash
q "select c.cust_id, c.name, count(o.order_id) as n_orders,
          coalesce(sum(o.amount), 0) as total_amount
   from (
     select * from stores/store_00.sqlite:::customers
     UNION ALL
     select * from stores/store_01.sqlite:::customers
   ) c
   left join (
     select * from stores/store_00.sqlite:::orders
     UNION ALL
     select * from stores/store_01.sqlite:::orders
   ) o
   on c.cust_id = o.cust_id and o.amount > 100
   group by c.cust_id, c.name
   order by c.cust_id, c.name"
```

Notes:

* SQLite files are not glob-expanded by q, so every `:::<table>` branch must be
  listed explicitly. `tools/cross_store_query.py` generates the expansion from
  `--db-glob`/`--db-file` and `--table` arguments.
* Same-schema empty databases are valid branches: `UNION ALL` over an empty
  table contributes no rows and changes nothing. They are the standard way to
  push a query over the attach limit during validation.
* CTEs (`WITH ...`) are not supported by q's tokenizer; use parenthesised
  subqueries in `FROM` as above.

### Helper

```bash
python tools/cross_store_query.py \
  --db-glob 'stores/*.sqlite' \
  --table customers --table orders \
  --query "select c.cust_id, count(o.order_id)
           from customers c
           left join orders o
             on c.cust_id = o.cust_id and o.amount > 100
           group by c.cust_id order by c.cust_id"
```

`--max-attached-sqlite-databases=4` forces the copy path (the value must be
larger than 3), and `--dry-run` prints the expanded query without executing it.

## Validation: control batch versus over-limit run

The regression test `test/test_cross_store_attach_limit.py` implements the
required equivalence check:

1. Build a batch of populated stores that fits within the attach limit and run
   the query directly (limit 10). This is the control.
2. Add same-schema empty stores until the referenced database count exceeds the
   limit, then rerun the same query with a low limit so the copy path is
   guaranteed to be used.
3. Compare stdout byte-for-byte (including row order) and hash every source
   database before and after the run.

Covered cases:

* a customer row in several stores matching several right rows across stores
  (full cross-store fan-out, not per-store counts),
* conditional `LEFT JOIN` with the filter in `ON`, so unmatched left rows stay
  NULL,
* NULL join keys and duplicate keys on both sides,
* join keys declared TEXT on one side and INTEGER on the other,
* twelve populated stores, beyond SQLite's default ten-database ceiling,
* an end-to-end run through `tools/cross_store_query.py`.

Run them with:

```bash
.venv/bin/pytest test/test_cross_store_attach_limit.py -v
```
