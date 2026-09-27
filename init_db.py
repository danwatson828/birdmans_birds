#!/usr/bin/env python3
"""Create a local DuckDB database and apply schema.sql + semantic.sql.

The raw schema and the semantic layer are applied as two steps because they have
different lifecycles: schema.sql creates tables and is written to be safe to
re-run (CREATE TABLE IF NOT EXISTS), while semantic.sql is all CREATE OR REPLACE
and is meant to be re-applied freely whenever a metric definition changes.

Usage:
    python init_db.py                    # create ./nba.duckdb, refuse if it exists
    python init_db.py --views-only       # re-apply the semantic layer in place
    python init_db.py --force            # drop and recreate from scratch
    python init_db.py --db /tmp/x.duckdb # somewhere else
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "nba.duckdb"
SCHEMA_SQL = PROJECT_ROOT / "schema.sql"
SEMANTIC_SQL = PROJECT_ROOT / "semantic.sql"


def apply_sql(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    """Execute a .sql file as a single script."""
    if not path.exists():
        raise FileNotFoundError(f"missing SQL file: {path}")
    print(f"  applying {path.name} ...", end=" ", flush=True)
    # DuckDB's execute() runs multiple statements from one string, which keeps
    # the whole file in a single transaction rather than splitting on ';' -- a
    # split would corrupt the macro bodies, which contain semicolon-free but
    # comment-heavy SQL that is easy to mangle by hand.
    con.execute(path.read_text())
    print("ok")


def summarise(con: duckdb.DuckDBPyConnection) -> None:
    """Print what now exists, so a run is self-verifying."""
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'BASE TABLE' "
        "ORDER BY table_name"
    ).fetchall()]
    views = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' AND table_type = 'VIEW' "
        "ORDER BY table_name"
    ).fetchall()]
    # internal = false is what separates our macros from DuckDB's ~1,500
    # built-ins, which also live in the main schema.
    macros = [(r[0], r[1]) for r in con.execute(
        "SELECT DISTINCT function_name, function_type FROM duckdb_functions() "
        "WHERE internal = false ORDER BY function_name"
    ).fetchall()]

    print(f"\ntables ({len(tables)}):")
    for t in tables:
        n = con.execute(f'SELECT count(*) FROM "{t}"').fetchone()[0]
        print(f"  {t:<24} {n:>10,} rows")
    print(f"\nviews ({len(views)}):")
    for v in views:
        print(f"  {v}")
    print(f"\nmacros ({len(macros)}):")
    for name, kind in macros:
        print(f"  {name:<24} ({kind})")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB,
                    help=f"database file (default: {DEFAULT_DB.name})")
    ap.add_argument("--force", action="store_true",
                    help="delete an existing database file first")
    ap.add_argument("--views-only", action="store_true",
                    help="re-apply semantic.sql only; leaves tables and data alone")
    args = ap.parse_args()

    db: Path = args.db

    if args.views_only:
        if not db.exists():
            print(f"error: {db} does not exist; run without --views-only first",
                  file=sys.stderr)
            return 1
        print(f"re-applying semantic layer to existing {db}")
        with duckdb.connect(str(db)) as con:
            apply_sql(con, SEMANTIC_SQL)
            summarise(con)
        return 0

    if db.exists():
        if not args.force:
            print(f"error: {db} already exists.\n"
                  f"  --views-only  re-apply metric definitions, keep the data\n"
                  f"  --force       delete it and start over",
                  file=sys.stderr)
            return 1
        # Deleting a database is worth saying out loud rather than doing quietly.
        print(f"--force: deleting existing {db}")
        db.unlink()

    print(f"creating {db}")
    with duckdb.connect(str(db)) as con:
        apply_sql(con, SCHEMA_SQL)
        apply_sql(con, SEMANTIC_SQL)
        summarise(con)

    print(f"\ndone. next step is ingestion -- the tables are empty by design.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
