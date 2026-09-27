"""Minimal zero-dependency check registry.

pytest is deliberately NOT used here. This suite runs inside ingest.py's publish
gate, so it has to work with nothing installed beyond what ingestion already
needs (duckdb, requests, python-dotenv). A short registry is cheaper than making
pytest a runtime dependency of the data pipeline.

A check is a function taking a DuckDB connection and returning either:
    True / False
    (bool, "detail string")
or raising Skip("reason") when the database does not contain the data the check
needs. Skip matters because the same suite runs against the full 80-season
database and against small single-season databases -- an assertion about the
2022-2025 play-in tournament must not fail merely because those seasons are
absent.
"""

from __future__ import annotations

from dataclasses import dataclass


class Skip(Exception):
    """Raised by a check when the database lacks the data it needs."""


@dataclass
class Result:
    group: str
    name: str
    status: str          # "pass" | "fail" | "skip"
    detail: str = ""

    @property
    def failed(self) -> bool:
        return self.status == "fail"


_REGISTRY: list[tuple[str, str, object]] = []


def check(name: str, group: str = "general"):
    """Register a check under a group heading."""
    def decorate(fn):
        _REGISTRY.append((group, name, fn))
        return fn
    return decorate


def registered() -> list[tuple[str, str, object]]:
    return list(_REGISTRY)


def run_one(group: str, name: str, fn, con) -> Result:
    try:
        outcome = fn(con)
    except Skip as exc:
        return Result(group, name, "skip", str(exc))
    except Exception as exc:                     # a broken check is a failure
        return Result(group, name, "fail", f"{type(exc).__name__}: {exc}")
    detail = ""
    if isinstance(outcome, tuple):
        outcome, detail = outcome
    return Result(group, name, "pass" if outcome else "fail", detail)


# ---------------------------------------------------------------------------
# helpers shared by the check modules
# ---------------------------------------------------------------------------
def one(con, sql: str, params: list | None = None):
    return con.execute(sql, params or []).fetchone()


def scalar(con, sql: str, params: list | None = None):
    return one(con, sql, params)[0]


def season_span(con) -> tuple[int | None, int | None]:
    return one(con, "SELECT min(season), max(season) FROM games")


def require_seasons(con, first: int, last: int, why: str = "") -> None:
    """Skip unless the database covers [first, last] inclusive."""
    lo, hi = season_span(con)
    if lo is None:
        raise Skip("games table is empty")
    if lo > first or hi < last:
        raise Skip(f"needs seasons {first}-{last}, database has {lo}-{hi}"
                   + (f" ({why})" if why else ""))
