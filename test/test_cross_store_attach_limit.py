"""Cross-store queries past SQLite's ATTACH limit.

The scenario: an archive split into one SQLite database per store, where one
query may touch more databases than SQLite allows to ATTACH simultaneously.
q handles the overflow by copying overflow tables into its private adhoc
database (attach -> CREATE TABLE AS SELECT -> detach), so the whole query still
runs in one connected database. These tests prove that this copy path returns
exactly the same rows as the direct-attach path, including:

* cross-store customers (the same key appearing in several stores),
* right tables with several matching rows (one-to-many LEFT JOIN),
* NULL join keys and unmatched left rows,
* same-schema empty databases added until the attach limit is exceeded.

Source databases are never written to: every test hashes the files before and
after the run.
"""

import glob
import hashlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
Q_EXECUTABLE = os.path.abspath(os.path.join(REPO_ROOT, "bin", "q.py"))
HELPER = os.path.abspath(os.path.join(REPO_ROOT, "tools", "cross_store_query.py"))

CUSTOMERS_DDL = "CREATE TABLE customers (cust_id INTEGER, name TEXT, region TEXT)"
ORDERS_DDL = (
    "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, "
    "cust_id INTEGER, amount REAL, store_note TEXT)"
)


def union(table_name, db_files):
    branches = [
        "select * from %s:::%s" % (db_file, table_name) for db_file in db_files
    ]
    return "( " + " UNION ALL ".join(branches) + " )"


def row_level_query(db_files):
    # Conditional LEFT JOIN: the amount filter sits in ON, so unmatched left
    # rows must survive with NULL right columns.
    return (
        "select c.cust_id, c.name, o.order_id, o.amount "
        "from %s c left join %s o "
        "on c.cust_id = o.cust_id and o.amount > 100 "
        "order by c.cust_id, c.name, o.order_id"
        % (union("customers", db_files), union("orders", db_files))
    )


def aggregate_query(db_files):
    # Cross-store summary: COUNT/COALESCE(SUM) over the one-to-many join.
    return (
        "select c.cust_id, c.name, count(o.order_id) as n_orders, "
        "coalesce(sum(o.amount), 0) as total_amount "
        "from %s c left join %s o "
        "on c.cust_id = o.cust_id and o.amount > 100 "
        "group by c.cust_id, c.name order by c.cust_id, c.name"
        % (union("customers", db_files), union("orders", db_files))
    )


def null_key_query(db_files):
    return (
        "select l.k, l.v, r.d from %s l left join %s r "
        "on l.k = r.k and r.d <> 'r2' order by l.v, r.d"
        % (union("l", db_files), union("r", db_files))
    )


def run_q(query, max_attached=None):
    command = [sys.executable, Q_EXECUTABLE, query]
    if max_attached is not None:
        command.append("--max-attached-sqlite-databases=%d" % max_attached)
    return subprocess.run(
        command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )


def file_hash(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        digest.update(handle.read())
    return digest.hexdigest()


class CrossStoreAttachLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="cross-store-")
        self.data_dbs = []
        self.empty_dbs = []

    def tearDown(self):
        for path in glob.glob(os.path.join(self.tmpdir, "*")):
            os.remove(path)
        os.rmdir(self.tmpdir)

    def db_path(self, name):
        return os.path.join(self.tmpdir, name)

    def create_data_store(self, index, customers, orders, null_tables=False):
        path = self.db_path("store_%02d.sqlite" % index)
        conn = sqlite3.connect(path)
        try:
            if null_tables:
                conn.execute("CREATE TABLE l (k TEXT, v TEXT)")
                conn.execute("CREATE TABLE r (k TEXT, d TEXT)")
                conn.executemany("INSERT INTO l VALUES (?, ?)", customers)
                conn.executemany("INSERT INTO r VALUES (?, ?)", orders)
            else:
                conn.execute(CUSTOMERS_DDL)
                conn.execute(ORDERS_DDL)
                conn.executemany(
                    "INSERT INTO customers VALUES (?, ?, ?)", customers
                )
                conn.executemany(
                    "INSERT INTO orders (order_id, cust_id, amount, store_note) "
                    "VALUES (?, ?, ?, ?)",
                    orders,
                )
            conn.commit()
        finally:
            conn.close()
        self.data_dbs.append(path)
        return path

    def create_empty_store(self, index, null_tables=False):
        path = self.db_path("empty_%02d.sqlite" % index)
        conn = sqlite3.connect(path)
        try:
            if null_tables:
                conn.execute("CREATE TABLE l (k TEXT, v TEXT)")
                conn.execute("CREATE TABLE r (k TEXT, d TEXT)")
            else:
                conn.execute(CUSTOMERS_DDL)
                conn.execute(ORDERS_DDL)
            conn.commit()
        finally:
            conn.close()
        self.empty_dbs.append(path)
        return path

    def assert_query_matches_control(self, query_builder, control_dbs, overflow_dbs):
        control = run_q(query_builder(control_dbs), max_attached=10)
        self.assertEqual(
            control.returncode, 0, control.stderr.decode("utf-8", "replace")
        )

        # Forced overflow: the attach ceiling is below the number of stores,
        # so q must copy every table past the first slots into the adhoc db.
        overflow = run_q(query_builder(overflow_dbs), max_attached=4)
        self.assertEqual(
            overflow.returncode, 0, overflow.stderr.decode("utf-8", "replace")
        )

        self.assertEqual(
            overflow.stdout,
            control.stdout,
            "row-level output differs between copy and direct-attach paths",
        )
        return control.stdout

    def test_copy_overflow_matches_direct_attach_with_empty_stores(self):
        # Customers 1..3 appear in every store (cross-store customer); customer
        # 3 only exists in the second store. Customer 1 has several matching
        # order rows spread across both stores (one-to-many).
        self.create_data_store(
            0,
            [(1, "alice", "N"), (2, "bob", "S")],
            [
                (101, 1, 150.0, "s0"),
                (102, 1, 50.0, "s0"),
                (103, 2, 200.0, "s0"),
            ],
        )
        self.create_data_store(
            1,
            [(1, "alice", "N"), (2, "bob", "S"), (3, "carol", "E")],
            [
                (201, 1, 120.0, "s1"),
                (202, 1, 120.0, "s1"),
                (203, 3, None, "s1"),
            ],
        )
        # Same-schema empty databases push the referenced database count well
        # past the effective attach ceiling (limit 4 leaves only two user
        # slots once q's internal databases count).
        for index in range(6):
            self.create_empty_store(index)

        all_dbs = self.data_dbs + self.empty_dbs
        before = {path: file_hash(path) for path in all_dbs}

        rows = self.assert_query_matches_control(
            row_level_query, self.data_dbs, all_dbs
        )
        self.assert_query_matches_control(
            aggregate_query, self.data_dbs, all_dbs
        )

        after = {path: file_hash(path) for path in all_dbs}
        self.assertEqual(before, after, "source databases must stay read-only")

        # Unmatched left rows survive the conditional LEFT JOIN with NULLs.
        self.assertIn(b"3 carol ", rows)
        # The amount<=100 order is filtered out by the ON condition, while the
        # customer row still appears via its other matches.
        self.assertNotIn(b"102", rows)
        # One-to-many across stores: alice has a customer row in each of the
        # two stores and three qualifying orders overall. Every customer row
        # matches every qualifying order (2 x 3 = 6 rows) - precisely why the
        # join has to happen in one connected database instead of computing a
        # number per store and concatenating the results.
        alice_rows = [line for line in rows.splitlines() if line.startswith(b"1 ")]
        self.assertEqual(len(alice_rows), 6)
        for order_id in (b"101", b"201", b"202"):
            self.assertEqual(
                sum(1 for line in alice_rows if order_id in line), 2
            )
        self.assertEqual(len(rows.splitlines()), 9)

    def test_null_keys_and_duplicate_rows_match_across_paths(self):
        self.create_data_store(
            0,
            [(None, "a-null"), ("x1", "a"), ("x1", "a-dup"), ("x2", "b")],
            [(None, "r-null"), ("x1", "r1"), ("x1", "r2")],
            null_tables=True,
        )
        self.create_data_store(
            1,
            [("x1", "cross-store"), ("x3", "d")],
            [("x1", "r3"), ("x3", "r4")],
            null_tables=True,
        )
        for index in range(4):
            self.create_empty_store(index, null_tables=True)

        all_dbs = self.data_dbs + self.empty_dbs
        before = {path: file_hash(path) for path in all_dbs}

        output = self.assert_query_matches_control(
            null_key_query, self.data_dbs, all_dbs
        )
        after = {path: file_hash(path) for path in all_dbs}
        self.assertEqual(before, after)

        lines = output.splitlines()
        # NULL-key left row and unmatched x2 row survive with empty right col.
        self.assertIn(b" a-null ", lines)
        self.assertIn(b"x2 b ", lines)
        # Duplicate left key x1 fans out against every matching right row in
        # both stores, including the duplicate left row a-dup.
        self.assertEqual(
            sorted(line for line in lines if b"x1" in line),
            [
                b"x1 a r1",
                b"x1 a r3",
                b"x1 a-dup r1",
                b"x1 a-dup r3",
                b"x1 cross-store r1",
                b"x1 cross-store r3",
            ],
        )

    def test_text_and_integer_join_keys_match_across_paths(self):
        # Join keys stored with different declared types/affinities: TEXT on
        # the left, INTEGER on the right. SQLite compares digit text with an
        # integer numerically, and CREATE TABLE AS SELECT (the copy path)
        # preserves column affinity on modern SQLite, so both paths must join
        # the same way.
        left_path = self.db_path("store_text.sqlite")
        conn = sqlite3.connect(left_path)
        conn.execute("CREATE TABLE customers (cust_id TEXT, name TEXT)")
        conn.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, "
            "cust_id INTEGER, amount REAL, store_note TEXT)"
        )
        conn.executemany(
            "INSERT INTO customers VALUES (?, ?)",
            [("1", "alice"), ("2", "bob"), ("3", "carol")],
        )
        conn.commit()
        conn.close()
        self.data_dbs.append(left_path)

        right_path = self.db_path("store_integer.sqlite")
        conn = sqlite3.connect(right_path)
        # Same schema as every other store; the customers table is empty and
        # all order rows live here with integer join keys.
        conn.execute("CREATE TABLE customers (cust_id TEXT, name TEXT)")
        conn.execute(
            "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, "
            "cust_id INTEGER, amount REAL, store_note TEXT)"
        )
        conn.executemany(
            "INSERT INTO orders VALUES (?, ?, ?, ?)",
            [(10, 1, 5.0, "a"), (11, 1, 7.0, "b"), (12, 2, 9.0, "c")],
        )
        conn.commit()
        conn.close()
        self.data_dbs.append(right_path)

        for index in range(6):
            path = self.db_path("empty_%02d.sqlite" % index)
            conn = sqlite3.connect(path)
            conn.execute("CREATE TABLE customers (cust_id TEXT, name TEXT)")
            conn.execute(
                "CREATE TABLE orders (order_id INTEGER PRIMARY KEY, "
                "cust_id INTEGER, amount REAL, store_note TEXT)"
            )
            conn.commit()
            conn.close()
            self.empty_dbs.append(path)

        def mixed_type_query(db_files):
            return (
                "select c.cust_id, c.name, o.order_id, o.amount "
                "from %s c left join %s o "
                "on c.cust_id = o.cust_id and o.amount > 0 "
                "order by c.cust_id, o.order_id"
                % (union("customers", db_files), union("orders", db_files))
            )

        all_dbs = self.data_dbs + self.empty_dbs
        before = {path: file_hash(path) for path in all_dbs}
        output = self.assert_query_matches_control(
            mixed_type_query, self.data_dbs, all_dbs
        )
        after = {path: file_hash(path) for path in all_dbs}
        self.assertEqual(before, after)

        lines = output.splitlines()
        # Digit text '1'/'2' matched integer keys 1/2 in both paths.
        self.assertEqual(
            lines[:3],
            [b"1 alice 10 5.0", b"1 alice 11 7.0", b"2 bob 12 9.0"],
        )
        # The unmatched text key survives the LEFT JOIN with NULL right cols.
        self.assertEqual(lines[3].split(), [b"3", b"carol"])

    def test_more_stores_than_default_attach_limit(self):
        # Twelve populated stores exceed the standard ten-database SQLite
        # ceiling even with q's default --max-attached-sqlite-databases=10.
        for index in range(12):
            # The shared customer row exists in one store only, while every
            # store carries an order for it: the global UNION ALL join still
            # reaches orders from stores that have no matching customer row.
            customers = (
                [(1, "shared", "N"), (100 + index, "local-%d" % index, "S")]
                if index == 0
                else [(100 + index, "local-%d" % index, "S")]
            )
            self.create_data_store(
                index,
                customers,
                [
                    (1000 + index * 10, 1, 250.0, "s%d" % index),
                    (1000 + index * 10 + 1, 100 + index, None, "s%d" % index),
                ],
            )

        before = {path: file_hash(path) for path in self.data_dbs}
        process = run_q(aggregate_query(self.data_dbs))
        self.assertEqual(
            process.returncode, 0, process.stderr.decode("utf-8", "replace")
        )
        after = {path: file_hash(path) for path in self.data_dbs}
        self.assertEqual(before, after)

        # The shared customer accumulates one qualifying order from every store.
        shared = [
            line for line in process.stdout.splitlines() if line.startswith(b"1 ")
        ]
        self.assertEqual(len(shared), 1)
        self.assertEqual(shared[0], b"1 shared 12 3000.0")

    def test_helper_end_to_end_matches_direct_attach(self):
        self.create_data_store(
            0,
            [(1, "alice", "N"), (2, "bob", "S")],
            [(101, 1, 150.0, "s0"), (103, 2, 200.0, "s0")],
        )
        self.create_data_store(
            1,
            [(1, "alice", "N"), (3, "carol", "E")],
            [(201, 1, 120.0, "s1"), (203, 3, None, "s1")],
        )
        for index in range(6):
            self.create_empty_store(index)

        all_dbs = self.data_dbs + self.empty_dbs
        before = {path: file_hash(path) for path in all_dbs}

        control = run_q(row_level_query(self.data_dbs), max_attached=10)
        self.assertEqual(control.returncode, 0)

        helper_command = [
            sys.executable,
            HELPER,
            "--db-glob",
            os.path.join(self.tmpdir, "*.sqlite"),
            "--table",
            "customers",
            "--table",
            "orders",
            "--max-attached-sqlite-databases=4",
            "--query",
            (
                "select c.cust_id, c.name, o.order_id, o.amount "
                "from customers c left join orders o "
                "on c.cust_id = o.cust_id and o.amount > 100 "
                "order by c.cust_id, c.name, o.order_id"
            ),
        ]
        helper = subprocess.run(
            helper_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        self.assertEqual(
            helper.returncode, 0, helper.stderr.decode("utf-8", "replace")
        )
        self.assertEqual(helper.stdout, control.stdout)

        after = {path: file_hash(path) for path in all_dbs}
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
