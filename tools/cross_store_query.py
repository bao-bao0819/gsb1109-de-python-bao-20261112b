#!/usr/bin/env python3
"""Run one SQL query across many per-store SQLite files with q.

The archive is split into one small SQLite database per store. A single query
can reference more stores than SQLite allows to ATTACH at once (the standard
compile-time limit is 10, and q keeps two slots for itself). q already knows how
to deal with this: databases up to the limit are ATTACHed directly, and every
further table is brought in with an attach -> ``CREATE TABLE AS SELECT`` ->
detach cycle into q's private in-memory adhoc database. Copied tables do not
consume an ATTACH slot, so there is no hard ceiling on the number of stores.

This helper enumerates the store databases and expands every referenced table
into a ``UNION ALL`` of ``<db>:::<table>`` branches. The final statement still
runs inside one connected database, which is what makes cross-store summaries
and conditional LEFT JOINs correct: the same customer appearing in several
stores is not "computed per store and concatenated afterwards", and right
tables with several matching rows keep producing several output rows.

Source databases are only ever opened read-only (q itself uses an
``immutable=1`` URI; the pre-flight check below uses ``mode=ro``).
"""

from __future__ import print_function

import argparse
import glob
import os
import sqlite3
import subprocess
import sys


def expand_table(table_name, db_files):
    branches = [
        "select * from %s:::%s" % (db_file, table_name) for db_file in db_files
    ]
    if not branches:
        raise ValueError("no database files provided for table %s" % table_name)
    return "( " + " UNION ALL ".join(branches) + " )"


def expand_query(query, table_to_dbs):
    """Replace each FROM/JOIN <table> token with its UNION ALL expansion.

    This deliberately mirrors q's own whitespace-based SQL splitting: only the
    bare token right after FROM or JOIN is a table reference. Parenthesised
    subqueries are left untouched, and table aliases following the reference
    are preserved.
    """
    parts = query.split()
    for idx, part in enumerate(parts):
        if part.upper() not in ("FROM", "JOIN"):
            continue
        if idx == len(parts) - 1:
            raise ValueError("FROM/JOIN is missing a table name after it")
        table_name = parts[idx + 1]
        if table_name.startswith("(") or table_name not in table_to_dbs:
            continue
        parts[idx + 1] = expand_table(table_name, table_to_dbs[table_name])
    return " ".join(parts)


def validate_tables(db_files, table_names):
    """Fail early with a readable error if a store lacks an expected table.

    Opens every database with a read-only URI so the check itself cannot
    modify the archives.
    """
    for db_file in db_files:
        uri = "file:%s?mode=ro" % os.path.abspath(db_file)
        conn = sqlite3.connect(uri, uri=True)
        try:
            existing = {
                row[0]
                for row in conn.execute(
                    "select name from sqlite_master where type='table'"
                )
            }
        finally:
            conn.close()
        missing = [name for name in table_names if name not in existing]
        if missing:
            raise SystemExit(
                "table(s) %s not found in %s (available: %s)"
                % (",".join(sorted(missing)), db_file, ",".join(sorted(existing)))
            )


def resolve_q_path(q_path):
    if q_path:
        return q_path
    repo_q = os.path.abspath(
        os.path.join(os.path.dirname(__file__), os.pardir, "bin", "q.py")
    )
    return repo_q if os.path.exists(repo_q) else "q"


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Run a cross-store SQLite query past the ATTACH limit via q."
    )
    parser.add_argument(
        "--db-glob",
        action="append",
        default=[],
        metavar="PATTERN",
        help="glob of store database files; repeatable (e.g. stores/*.sqlite)",
    )
    parser.add_argument(
        "--db-file",
        action="append",
        default=[],
        metavar="FILE",
        help="individual store database file; repeatable",
    )
    parser.add_argument(
        "--table",
        action="append",
        default=[],
        metavar="NAME",
        help="table name referenced by the query; repeatable",
    )
    parser.add_argument(
        "--query",
        required=True,
        help="SQL query using the bare table names given in --table",
    )
    parser.add_argument(
        "--max-attached-sqlite-databases",
        type=int,
        default=None,
        help="forwarded to q; lower it to force the copy path during validation",
    )
    parser.add_argument(
        "--q-path", default=None, help="path to q (defaults to ./bin/q.py or q on PATH)"
    )
    parser.add_argument(
        "--q-arg",
        action="append",
        default=[],
        help="extra argument forwarded verbatim to q; repeatable",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the expanded q query and exit without running it",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)

    db_files = []
    for pattern in args.db_glob:
        db_files.extend(glob.glob(pattern))
    db_files.extend(args.db_file)
    db_files = sorted(set(os.path.abspath(path) for path in db_files))
    if not db_files:
        raise SystemExit("no database files matched --db-glob/--db-file")
    if not args.table:
        raise SystemExit("provide at least one --table")

    table_names = list(dict.fromkeys(args.table))
    validate_tables(db_files, table_names)
    table_to_dbs = {name: db_files for name in table_names}

    expanded_query = expand_query(args.query, table_to_dbs)

    if args.dry_run:
        print(expanded_query)
        return 0

    command = [resolve_q_path(args.q_path), expanded_query]
    if args.max_attached_sqlite_databases is not None:
        command.append(
            "--max-attached-sqlite-databases=%d" % args.max_attached_sqlite_databases
        )
    command.extend(args.q_arg)

    process = subprocess.Popen(command)
    return process.wait()


if __name__ == "__main__":
    sys.exit(main())
