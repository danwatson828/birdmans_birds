"""Runner for the verification suite.

Importable as run_suite(db_path) -> (ok, results) so ingest.py can gate the
snapshot publish on it, and runnable as `python -m tests.run` for humans.

A skipped check never fails the suite. Skips exist so the same suite is
meaningful against a one-season test database and against the full 1946-2025
history: a check that needs the 2024 season declares that and steps aside when
it is absent, rather than reporting a false failure.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import duckdb

from .harness import Result, registered, run_one

# Importing these modules is what populates the registry.
from . import checks_integrity      # noqa: F401
from . import checks_classification # noqa: F401
from . import checks_official       # noqa: F401
from . import checks_regressions    # noqa: F401
from . import checks_advanced       # noqa: F401
from . import checks_analyses      # noqa: F401
from . import checks_mcp          # noqa: F401
from . import checks_api           # noqa: F401
from . import checks_design        # noqa: F401
from . import checks_draft         # noqa: F401

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB = PROJECT_ROOT / "nba.duckdb"


def run_suite(db_path: Path | str) -> tuple[bool, list[Result]]:
    """Run every registered check against db_path. Returns (ok, results)."""
    db_path = Path(db_path)
    if not db_path.exists():
        return False, [Result("suite", "database exists", "fail", f"missing {db_path}")]
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except Exception as exc:
        return False, [Result("suite", "database opens read-only", "fail",
                              f"{type(exc).__name__}: {exc}")]
    try:
        results = [run_one(g, n, f, con) for g, n, f in registered()]
    finally:
        con.close()
    return not any(r.failed for r in results), results


def format_report(results: list[Result], verbose: bool = True) -> str:
    lines: list[str] = []
    if verbose:
        group = None
        for r in results:
            if r.group != group:
                group = r.group
                lines.append(f"\n{group}")
            mark = {"pass": "PASS", "fail": "FAIL", "skip": "SKIP"}[r.status]
            detail = f"  {r.detail}" if r.detail else ""
            lines.append(f"  {mark}  {r.name}{detail}")
    p = sum(r.status == "pass" for r in results)
    f = sum(r.status == "fail" for r in results)
    s = sum(r.status == "skip" for r in results)
    lines.append(f"\n{p} passed, {f} failed, {s} skipped")
    if f:
        lines.append("FAILURES:")
        lines += [f"  - [{r.group}] {r.name}: {r.detail}" for r in results if r.failed]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="Verify the NBA analytics database.")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--quiet", action="store_true", help="summary only")
    args = ap.parse_args()

    print(f"verifying {args.db}")
    ok, results = run_suite(args.db)
    print(format_report(results, verbose=not args.quiet))
    print("\nSUITE PASSED" if ok else "\nSUITE FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
