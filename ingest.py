#!/usr/bin/env python3
"""Ingest BallDontLie NBA data into the local DuckDB database.

Two modes:

    python ingest.py --backfill              # every season, 1946 -> latest
    python ingest.py --backfill --season 2024
    python ingest.py --daily                 # last 2 days of games

/stats IS THE ONLY DATA ENDPOINT THIS SCRIPT PAGES.
Each /stats row carries a complete nested `game` object (with home_team_id and
visitor_team_id already flattened), a complete nested `player` object, and a
complete nested `team` object. So one pass over /stats populates player_stats,
games, players and teams together, and /games is never called. That was verified
during the probe work: the nested game object has the same 33 fields the /games
endpoint returns, and the nested player object agrees with /players (checked on
12 players, 12/12 match).

Facts this script is built on, all measured rather than assumed (see probe_api.py
and the notes in schema.sql):

  * per_page caps at 100. 200 and above return HTTP 400.
  * The real rate limit is 600/min. This script paces at 500/min for headroom.
    Observed throughput is ~216 req/min anyway -- the API's own latency is the
    binding constraint, not the limiter.
  * Season 2024 is ~46,150 rows over 462 pages. A full 80-season backfill is
    therefore on the order of 20,000 requests and a couple of hours, which is
    why progress is checkpointed rather than held in memory.
  * DuckDB is single-writer and locks the live file exclusively, so nothing can
    read it while ingestion runs. After a clean run this script publishes
    nba_snapshot.duckdb -- a consistent copy taken with COPY FROM DATABASE --
    and that is what the agent and any ad hoc query should read. The live file
    belongs to ingestion alone.
  * Publishing is gated on TWO conditions, both required: ingestion completed
    cleanly, AND the verification suite in tests/ passes. A run that satisfies
    only the first does not publish -- a pipeline can run perfectly and still
    produce wrong numbers, which is exactly what happened three times already.
    On failure the previous snapshot is left in place, so readers keep seeing
    verified data. The process exits non-zero so a cron job notices.
  * player_stats has TWO unique constraints (PRIMARY KEY id, and
    UNIQUE (game_id, player_id)), and DuckDB's ON CONFLICT can only name one of
    them. Upserting on the (game_id, player_id) grain is correct, but a
    colliding `id` still raises. See upsert_player_stats for how that is
    absorbed instead of killing a multi-hour run.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import time
import datetime as dt
from collections import deque
from pathlib import Path

import duckdb
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "nba.duckdb"
DEFAULT_SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
BASE_URL = "https://api.balldontlie.io/v1"

FIRST_SEASON = 1946           # earliest season with data in the API
MAX_PER_PAGE = 100            # verified: 200+ -> HTTP 400
RATE_LIMIT_PER_MIN = 500      # verified ceiling is 600; headroom is deliberate
CHECKPOINT_EVERY_PAGES = 10   # bounds re-work after a crash to <=10 pages
MAX_ATTEMPTS = 5


# =============================================================================
# Column definitions -- mirror schema.sql exactly
# =============================================================================
TEAM_COLS = ["id", "conference", "division", "city", "name", "full_name",
             "abbreviation"]

PLAYER_COLS = ["id", "first_name", "last_name", "position", "height", "weight",
               "jersey_number", "college", "country", "draft_year",
               "draft_round", "draft_number", "team_id"]

GAME_COLS = ["id", "date", "season", "status", "status_state", "period", "time",
             "postseason", "postponed", "home_team_id", "visitor_team_id",
             "home_team_score", "visitor_team_score", "datetime",
             "home_q1", "home_q2", "home_q3", "home_q4",
             "home_ot1", "home_ot2", "home_ot3",
             "home_timeouts_remaining", "home_in_bonus",
             "visitor_q1", "visitor_q2", "visitor_q3", "visitor_q4",
             "visitor_ot1", "visitor_ot2", "visitor_ot3",
             "visitor_timeouts_remaining", "visitor_in_bonus", "ist_stage"]

# The API field names for a stats row, minus the three ids lifted out of the
# nested objects. "min" is quoted everywhere because it collides with the
# aggregate function name.
STAT_API_COLS = ["id", "min", "fgm", "fga", "fg_pct", "fg3m", "fg3a", "fg3_pct",
                 "ftm", "fta", "ft_pct", "oreb", "dreb", "reb", "ast", "stl",
                 "blk", "turnover", "pf", "pts", "plus_minus"]
STAT_COLS = ["player_id", "game_id", "team_id"] + STAT_API_COLS


def upsert_sql(table: str, cols: list[str], conflict: list[str]) -> str:
    """Build an idempotent INSERT ... ON CONFLICT DO UPDATE for one table.

    Every identifier is quoted because several API field names collide with SQL
    keywords ("min", "date", "time", "position", "season").

    _ingested_at is refreshed on update so the column means "last seen from the
    API", not "first seen" -- useful when checking whether a daily run actually
    touched a row.
    """
    collist = ", ".join(f'"{c}"' for c in cols)
    placeholders = ", ".join("?" * len(cols))
    conflict_list = ", ".join(f'"{c}"' for c in conflict)
    updates = ", ".join(f'"{c}" = excluded."{c}"' for c in cols if c not in conflict)
    # CAST(now() AS TIMESTAMP), not current_timestamp: inside DO UPDATE SET,
    # DuckDB resolves a bare `current_timestamp` as a column name and fails with
    # "table does not have a column named current_timestamp".
    return (f"INSERT INTO {table} ({collist}) VALUES ({placeholders}) "
            f"ON CONFLICT ({conflict_list}) DO UPDATE SET {updates}, "
            f'"_ingested_at" = CAST(now() AS TIMESTAMP)')


SQL_TEAMS = upsert_sql("teams", TEAM_COLS, ["id"])
SQL_PLAYERS = upsert_sql("players", PLAYER_COLS, ["id"])
SQL_GAMES = upsert_sql("games", GAME_COLS, ["id"])
# Conflict target is the GRAIN, not the id: (game_id, player_id) is the real
# business key, so a box score line re-issued by the API under a new id updates
# in place rather than duplicating.
SQL_STATS = upsert_sql("player_stats", STAT_COLS, ["game_id", "player_id"])


# =============================================================================
# Rate limiting and HTTP
# =============================================================================
class RateLimiter:
    """Sliding-window limiter: at most `per_min` requests in any 60s window."""

    def __init__(self, per_min: int = RATE_LIMIT_PER_MIN):
        self.per_min = per_min
        self.hits: deque[float] = deque()

    def wait(self) -> None:
        now = time.monotonic()
        while self.hits and now - self.hits[0] >= 60.0:
            self.hits.popleft()
        if len(self.hits) >= self.per_min:
            sleep_for = 60.0 - (now - self.hits[0]) + 0.01
            if sleep_for > 0:
                time.sleep(sleep_for)
            return self.wait()
        self.hits.append(time.monotonic())


class Client:
    """Thin BallDontLie client: paced, retrying, and honest about failure."""

    def __init__(self, api_key: str, per_min: int = RATE_LIMIT_PER_MIN):
        self.session = requests.Session()
        self.session.headers["Authorization"] = api_key
        self.limiter = RateLimiter(per_min)
        self.request_count = 0

    def get(self, path: str, params) -> dict:
        last_error = None
        for attempt in range(1, MAX_ATTEMPTS + 1):
            self.limiter.wait()
            self.request_count += 1
            try:
                r = self.session.get(f"{BASE_URL}/{path}", params=params, timeout=60)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                time.sleep(min(2 ** attempt, 30))
                continue

            if r.status_code == 200:
                return r.json()

            # 429 means we misjudged the pace; obey Retry-After when offered.
            if r.status_code == 429:
                wait = float(r.headers.get("Retry-After") or min(2 ** attempt, 60))
                print(f"      rate limited, sleeping {wait:.0f}s", flush=True)
                time.sleep(wait)
                last_error = "HTTP 429"
                continue

            if r.status_code >= 500:
                last_error = f"HTTP {r.status_code}"
                time.sleep(min(2 ** attempt, 30))
                continue

            # 4xx other than 429 is a request bug, not a transient fault --
            # retrying identical params cannot help.
            raise RuntimeError(f"HTTP {r.status_code} for {path} "
                               f"params={params}: {r.text[:200]}")

        raise RuntimeError(f"gave up on {path} after {MAX_ATTEMPTS} attempts: "
                           f"{last_error}")


# =============================================================================
# Checkpointing, via the existing _ingest_log table
# =============================================================================
# schema.sql already provides _ingest_log for exactly this ("so a backfill can be
# resumed rather than restarted"), so no new table is introduced here.
#
# One row per unit of work. A unit is (season, postseason) for backfill and a
# single date for daily -- small enough that a crash loses little, large enough
# that the log stays readable. The resume cursor lives in the params JSON, which
# DuckDB can read back with json_extract_string.
# =============================================================================
def unit_key(kind: str, **parts) -> str:
    return kind + ":" + ":".join(f"{k}={parts[k]}" for k in sorted(parts))


def checkpoint_lookup(con, key: str) -> tuple[str | None, int | None, int]:
    """Return (status, cursor, rows_upserted) for the latest attempt at `key`."""
    row = con.execute(
        """SELECT json_extract_string(params, '$.status'),
                  json_extract_string(params, '$.cursor'),
                  coalesce(rows_upserted, 0)
             FROM _ingest_log
            WHERE json_extract_string(params, '$.unit') = ?
            ORDER BY id DESC LIMIT 1""", [key]).fetchone()
    if not row:
        return None, None, 0
    status, cursor, rows = row
    return status, (int(cursor) if cursor not in (None, "null") else None), int(rows or 0)


def checkpoint_start(con, endpoint: str, key: str, season: int | None,
                     extra: dict, resume_cursor: int | None, rows_so_far: int) -> int:
    log_id = con.execute("SELECT nextval('_ingest_log_seq')").fetchone()[0]
    params = dict(extra, unit=key, status="partial", cursor=resume_cursor)
    con.execute(
        """INSERT INTO _ingest_log (id, endpoint, params, season, rows_upserted,
                                    started_at)
           VALUES (?, ?, ?, ?, ?, current_timestamp)""",
        [log_id, endpoint, json.dumps(params), season, rows_so_far])
    return log_id


def checkpoint_update(con, log_id: int, cursor: int | None, rows: int) -> None:
    con.execute(
        """UPDATE _ingest_log
              SET params = json_merge_patch(params, ?), rows_upserted = ?
            WHERE id = ?""",
        [json.dumps({"cursor": cursor, "status": "partial"}), rows, log_id])


def checkpoint_finish(con, log_id: int, rows: int, http_status: int = 200,
                      error: str | None = None) -> None:
    # Clearing the cursor on success matters: it is only checkpointed every N
    # pages, so at completion params still holds a stale mid-season cursor. Left
    # in place, a later --force run would resume from the middle and silently
    # skip every page before it.
    done = error is None
    con.execute(
        """UPDATE _ingest_log
              SET params = json_merge_patch(params, ?), rows_upserted = ?,
                  http_status = ?, finished_at = current_timestamp, error = ?
            WHERE id = ?""",
        [json.dumps({"status": "complete", "cursor": None} if done
                    else {"status": "failed"}),
         rows, http_status, error, log_id])


# =============================================================================
# Writing
# =============================================================================
def upsert_player_stats(con, rows: list[list]) -> int:
    """Upsert box score lines, absorbing the two-unique-constraint problem.

    ON CONFLICT names (game_id, player_id). If an incoming row's `id` happens to
    belong to a DIFFERENT (game_id, player_id) already stored, the primary key
    still raises -- ON CONFLICT covers one constraint, not both. That never
    occurred across 3,000 verified rows, but a two-hour backfill should not die
    on an upstream oddity, so the batch degrades to row-by-row, then to clearing
    the squatting id, and only then gives up on that single row.

    Returns the number of rows actually written.
    """
    if not rows:
        return 0
    try:
        con.execute("BEGIN")
        con.executemany(SQL_STATS, rows)
        con.execute("COMMIT")
        return len(rows)
    except duckdb.ConstraintException:
        con.execute("ROLLBACK")

    gi, pi, ii = STAT_COLS.index("game_id"), STAT_COLS.index("player_id"), STAT_COLS.index("id")
    written = 0
    for row in rows:
        try:
            con.execute(SQL_STATS, row)
            written += 1
            continue
        except duckdb.ConstraintException:
            pass
        # The id is held by a different box score line. Drop that row and retry:
        # the grain is the authority on identity, the id is just a label.
        try:
            con.execute(
                """DELETE FROM player_stats
                    WHERE id = ? AND NOT (game_id = ? AND player_id = ?)""",
                [row[ii], row[gi], row[pi]])
            con.execute(SQL_STATS, row)
            written += 1
        except duckdb.ConstraintException as exc:
            print(f"      SKIPPED anomalous row id={row[ii]} "
                  f"game={row[gi]} player={row[pi]}: {exc}", flush=True)
            log_id = con.execute("SELECT nextval('_ingest_log_seq')").fetchone()[0]
            con.execute(
                """INSERT INTO _ingest_log (id, endpoint, params, started_at,
                                            finished_at, error)
                   VALUES (?, 'stats:anomaly', ?, current_timestamp,
                           current_timestamp, ?)""",
                [log_id, json.dumps({"stat_id": row[ii], "game_id": row[gi],
                                     "player_id": row[pi]}), str(exc)[:400]])
    return written


def write_page(con, stat_rows: list[dict]) -> int:
    """Write one /stats page to all four tables.

    Teams, players and games are deduplicated within the page before writing --
    a page of 100 box scores typically references only a handful of distinct
    games, so this cuts redundant upserts substantially.
    """
    teams, players, games, stats = {}, {}, {}, {}
    for r in stat_rows:
        team, player, game = r.get("team"), r.get("player"), r.get("game")
        if team:
            teams[team["id"]] = [team.get(c) for c in TEAM_COLS]
        if player:
            players[player["id"]] = [player.get(c) for c in PLAYER_COLS]
        if game:
            games[game["id"]] = [game.get(c) for c in GAME_COLS]
        if not (player and game):
            continue
        # Keyed by grain so a duplicate inside one page collapses deterministically
        # (last wins) rather than relying on DuckDB's within-batch behaviour.
        stats[(game["id"], player["id"])] = (
            [player["id"], game["id"], (team or {}).get("id")]
            + [r.get(c) for c in STAT_API_COLS])

    con.execute("BEGIN")
    try:
        if teams:
            con.executemany(SQL_TEAMS, list(teams.values()))
        if players:
            con.executemany(SQL_PLAYERS, list(players.values()))
        if games:
            con.executemany(SQL_GAMES, list(games.values()))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return upsert_player_stats(con, list(stats.values()))


# =============================================================================
# Paging one unit of work
# =============================================================================
def ingest_unit(con, client: Client, key: str, query: list[tuple],
                season: int | None, extra: dict, force: bool,
                max_pages: int | None = None) -> tuple[int, str]:
    """Page /stats for one unit, resuming from a checkpoint when there is one."""
    status, cursor, rows_so_far = checkpoint_lookup(con, key)
    if status == "complete":
        if not force:
            return rows_so_far, "skipped"
        # A forced re-ingest restarts the unit rather than resuming it, so the
        # whole season is re-read and the row tally does not double-count.
        cursor, rows_so_far = None, 0

    resumed = cursor is not None
    log_id = checkpoint_start(con, "stats", key, season, extra, cursor, rows_so_far)
    total = rows_so_far
    pages = 0
    label = f"resume@{cursor}" if resumed else "fresh"

    try:
        while True:
            params = list(query) + [("per_page", str(MAX_PER_PAGE))]
            if cursor is not None:
                params.append(("cursor", str(cursor)))
            payload = client.get("stats", params)
            batch = payload.get("data", [])
            if not batch:
                break
            total += write_page(con, batch)
            pages += 1
            cursor = payload.get("meta", {}).get("next_cursor")
            if pages % CHECKPOINT_EVERY_PAGES == 0:
                checkpoint_update(con, log_id, cursor, total)
                print(f"      {pages} pages, {total:,} rows", flush=True)
            if cursor is None:
                break
            if max_pages and pages >= max_pages:
                checkpoint_update(con, log_id, cursor, total)
                return total, f"stopped at --max-pages ({pages} pages, {label})"
    except Exception as exc:
        # Leave the checkpoint partial so the next run resumes here, and keep
        # going: one bad season must not abort an 80-season backfill.
        checkpoint_update(con, log_id, cursor, total)
        checkpoint_finish(con, log_id, total, http_status=0, error=str(exc)[:400])
        return total, f"FAILED ({exc})"

    checkpoint_finish(con, log_id, total)
    return total, f"complete ({pages} pages, {label})"


def ingest_teams(con, client: Client) -> int:
    """Refresh the full team list.

    /stats only ever mentions teams that appear in a box score, so this one extra
    call is what guarantees teams is complete -- including the defunct and
    international clubs the endpoint returns.
    """
    payload = client.get("teams", [])
    teams = payload.get("data", [])
    con.execute("BEGIN")
    try:
        con.executemany(SQL_TEAMS, [[t.get(c) for c in TEAM_COLS] for t in teams])
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return len(teams)


# =============================================================================
# Snapshot publishing
# =============================================================================
SNAPSHOT_TABLES = ("teams", "players", "games", "player_stats")


def _table_counts(con) -> dict:
    return {t: con.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in SNAPSHOT_TABLES}


def write_snapshot(db_path: Path, snapshot_path: Path) -> dict:
    """Publish a consistent copy of the database for readers to query.

    DuckDB is single-writer and holds an exclusive lock on the live file, so
    nothing -- not the agent, not an ad hoc query -- can read it while ingestion
    is writing. The contract is: ingestion owns the live file, everything else
    reads this snapshot.

    Uses COPY FROM DATABASE rather than shutil.copy. DuckDB keeps a separate .wal
    file next to the database (verified: non-empty mid-write, removed by
    CHECKPOINT), so copying just the main file can silently drop committed rows.
    COPY FROM DATABASE writes a fresh, compacted, logically complete database --
    tables, views and macros included.

    The copy goes to a temp path and is moved into place with os.replace, which
    is atomic: a concurrent reader sees either the previous snapshot or the new
    one, never a half-written file. A reader holding the old file open keeps
    reading it safely until it reopens.

    Row counts are compared afterwards, so a silently truncated snapshot fails
    loudly instead of being published.
    """
    tmp = snapshot_path.with_name(snapshot_path.name + ".tmp")
    for leftover in (tmp, Path(str(tmp) + ".wal")):
        leftover.unlink(missing_ok=True)

    with duckdb.connect(str(db_path)) as con:
        source = con.execute("SELECT current_database()").fetchone()[0]
        con.execute("CHECKPOINT")
        live = _table_counts(con)
        con.execute(f"ATTACH '{tmp}' AS __snapshot")
        con.execute(f"COPY FROM DATABASE {source} TO __snapshot")
        con.execute("DETACH __snapshot")

    stray_wal = Path(str(tmp) + ".wal")
    if stray_wal.exists() and stray_wal.stat().st_size > 0:
        raise RuntimeError(f"snapshot left a non-empty WAL at {stray_wal}; "
                           f"refusing to publish a possibly incomplete copy")
    stray_wal.unlink(missing_ok=True)

    os.replace(tmp, snapshot_path)

    with duckdb.connect(str(snapshot_path), read_only=True) as con:
        snap = _table_counts(con)
    if snap != live:
        raise RuntimeError(f"snapshot row counts differ from live: "
                           f"live={live} snapshot={snap}")
    return snap


# =============================================================================
# Modes
# =============================================================================
def latest_season(today: dt.date | None = None) -> int:
    """NBA seasons are labelled by start year; a season starting in October."""
    today = today or dt.date.today()
    return today.year if today.month >= 10 else today.year - 1


def run_backfill(con, client: Client, seasons: list[int], force: bool,
                 max_pages: int | None) -> int:
    """Returns the number of units that failed, so the caller can decide
    whether the run was clean enough to publish a snapshot."""
    print(f"backfill: {len(seasons)} season(s) "
          f"{seasons[0]}..{seasons[-1]}, paced at {client.limiter.per_min}/min")
    print(f"teams refreshed: {ingest_teams(con, client)} rows\n")

    grand = 0
    failed = 0
    for season in seasons:
        # Regular season and postseason are paged as separate units. Being
        # explicit removes any reliance on what the API does when `postseason` is
        # omitted, and halves the amount of re-work a crash costs.
        for postseason in (False, True):
            key = unit_key("backfill", season=season, postseason=postseason)
            query = [("seasons[]", str(season)),
                     ("postseason", "true" if postseason else "false")]
            tag = "post" if postseason else "reg "
            print(f"  season {season} {tag}", flush=True)
            rows, outcome = ingest_unit(
                con, client, key, query, season,
                {"kind": "backfill", "season": season, "postseason": postseason},
                force, max_pages)
            grand += rows if outcome != "skipped" else 0
            if outcome.startswith("FAILED"):
                failed += 1
            print(f"    -> {rows:,} rows, {outcome}", flush=True)
    print(f"\nbackfill wrote {grand:,} rows using {client.request_count} requests")
    if failed:
        print(f"WARNING: {failed} unit(s) failed -- re-run to resume them")
    return failed


def run_daily(con, client: Client, days: int,
              end_date: dt.date | None = None) -> int:
    """Pull the last `days` days, ending at `end_date` (default today).

    end_date exists for two reasons: the mode is untestable in the offseason
    without it, and after an outage it is the way to re-pull a specific window
    without running a whole backfill.
    """
    end = end_date or dt.date.today()
    dates = [(end - dt.timedelta(days=offset)).isoformat()
             for offset in range(days - 1, -1, -1)]
    print(f"daily: {dates[0]} .. {dates[-1]} ({days} days), "
          f"paced at {client.limiter.per_min}/min")
    print(f"teams refreshed: {ingest_teams(con, client)} rows\n")

    total = 0
    failed = 0
    for date in dates:
        # force is always True here: a day already marked complete must still be
        # re-pulled, because that is the entire point of overlapping the window --
        # late finals and score corrections land after the first run saw the date.
        key = unit_key("daily", date=date)
        print(f"  {date}", flush=True)
        rows, outcome = ingest_unit(con, client, key, [("dates[]", date)],
                                    None, {"kind": "daily", "date": date},
                                    force=True)
        total += rows
        if outcome.startswith("FAILED"):
            failed += 1
        print(f"    -> {rows:,} rows, {outcome}", flush=True)
    print(f"\ndaily wrote {total:,} rows using {client.request_count} requests")
    if failed:
        print(f"WARNING: {failed} day(s) failed -- re-run to resume them")
    return failed


def summarise(con) -> None:
    print("\ntable counts:")
    for table in ("teams", "players", "games", "player_stats"):
        n = con.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        print(f"  {table:<14} {n:>12,}")
    span = con.execute("SELECT min(season), max(season), min(date), max(date) "
                       "FROM games").fetchone()
    if span[0] is not None:
        print(f"  seasons {span[0]}..{span[1]}   dates {span[2]}..{span[3]}")
    # Count UNITS whose latest attempt is unfinished, not log rows: each resume
    # leaves its earlier partial row in the log, so counting rows overstates it.
    incomplete = con.execute(
        """SELECT count(*) FROM (
               SELECT max_by(json_extract_string(params, '$.status'), id) AS status
                 FROM _ingest_log
                WHERE endpoint = 'stats'
                GROUP BY json_extract_string(params, '$.unit'))
            WHERE status IS DISTINCT FROM 'complete'""").fetchone()[0]
    anomalies = con.execute(
        "SELECT count(*) FROM _ingest_log WHERE endpoint = 'stats:anomaly'").fetchone()[0]
    print(f"  unfinished units: {incomplete}   anomalous rows skipped: {anomalies}")
    if incomplete:
        print("  (re-run the same command to resume them)")


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--backfill", action="store_true",
                      help="pull full history across all available seasons")
    mode.add_argument("--daily", action="store_true",
                      help="pull the last --days days of games")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--season", type=int,
                    help="backfill a single season (test before the full run)")
    ap.add_argument("--from-season", type=int, default=FIRST_SEASON)
    ap.add_argument("--to-season", type=int, default=None)
    ap.add_argument("--order", choices=("newest", "oldest"), default="newest",
                    help="backfill direction (default newest: an interrupted "
                         "run then leaves the most useful seasons already loaded)")
    ap.add_argument("--days", type=int, default=2,
                    help="daily window size, ending at --end-date (default 2)")
    ap.add_argument("--end-date", type=dt.date.fromisoformat,
                    help="last day of the --daily window (default today); "
                         "use to re-pull a specific window after an outage")
    ap.add_argument("--rate-limit", type=int, default=RATE_LIMIT_PER_MIN,
                    help=f"requests/min (default {RATE_LIMIT_PER_MIN}; API ceiling is 600)")
    ap.add_argument("--force", action="store_true",
                    help="re-ingest units already marked complete")
    ap.add_argument("--max-pages", type=int,
                    help="stop each unit after N pages (for smoke tests)")
    ap.add_argument("--snapshot", type=Path, default=DEFAULT_SNAPSHOT,
                    help=f"where to publish the readable copy "
                         f"(default {DEFAULT_SNAPSHOT.name})")
    ap.add_argument("--no-snapshot", action="store_true",
                    help="skip publishing the snapshot after a successful run")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"error: {args.db} does not exist -- run init_db.py first",
              file=sys.stderr)
        return 1

    load_dotenv(PROJECT_ROOT / ".env")
    api_key = os.environ.get("BALLDONTLIE_API_KEY")
    if not api_key:
        print("error: BALLDONTLIE_API_KEY not set (expected in .env)",
              file=sys.stderr)
        return 1

    client = Client(api_key, args.rate_limit)
    started = time.monotonic()

    with duckdb.connect(str(args.db)) as con:
        if args.backfill:
            if args.season:
                seasons = [args.season]
            else:
                hi = args.to_season or latest_season()
                seasons = list(range(args.from_season, hi + 1))
                if args.order == "newest":
                    seasons.reverse()
            failed = run_backfill(con, client, seasons, args.force, args.max_pages)
        else:
            failed = run_daily(con, client, args.days, args.end_date)
        summarise(con)
        unfinished = con.execute(
            """SELECT count(*) FROM (
                   SELECT max_by(json_extract_string(params, '$.status'), id) AS status
                     FROM _ingest_log
                    WHERE endpoint = 'stats'
                    GROUP BY json_extract_string(params, '$.unit'))
                WHERE status IS DISTINCT FROM 'complete'""").fetchone()[0]

    # The connection is closed before snapshotting: the live file must not be
    # held open by this process while a consistent copy is taken.
    #
    # PUBLISHING REQUIRES TWO INDEPENDENT GATES, BOTH MANDATORY.
    #   gate 1  ingestion finished cleanly -- no failed units this run, nothing
    #           left partial in _ingest_log
    #   gate 2  the verification suite in tests/ passes against the live file
    #
    # Gate 1 alone only proves the pipeline ran; it says nothing about whether
    # the numbers are right. Every defect found so far -- is_dnp reading the
    # pre-1951 era as wholesale did-not-play, a sampled era table wrong on five
    # stats, plus_minus coverage measured over the wrong denominator -- was
    # invisible to gate 1 and produced a clean run with wrong answers. So the
    # suite is not advisory. There is deliberately no flag to bypass it: use
    # --no-snapshot to skip publishing entirely, then fix the failures.
    if args.no_snapshot:
        print("\nsnapshot skipped (--no-snapshot)")
    else:
        ingest_clean = not (failed or unfinished)
        print("\ngate 1/2  ingestion: "
              + ("clean" if ingest_clean
                 else f"NOT CLEAN -- {failed} failed unit(s), {unfinished} unfinished"))

        # Run the suite regardless of gate 1, so one run reports everything.
        print("gate 2/2  verification suite:")
        try:
            from tests.run import format_report, run_suite
        except ImportError as exc:
            tests_ok = False
            print(f"  CANNOT RUN -- tests/ is not importable ({exc}). "
                  f"Publishing is blocked: the suite is a required gate.")
        else:
            tests_ok, results = run_suite(args.db)
            # Summary when green, full detail when red -- a daily cron should be
            # quiet on success and explicit on failure.
            print(format_report(results, verbose=not tests_ok))

        if ingest_clean and tests_ok:
            print(f"\nboth gates passed -- publishing snapshot -> {args.snapshot}")
            counts = write_snapshot(args.db, args.snapshot)
            size_mb = args.snapshot.stat().st_size / 1e6
            print(f"  verified {counts['player_stats']:,} player_stats rows, "
                  f"{size_mb:.1f} MB")
            print(f"  readers should query {args.snapshot.name}, never {args.db.name}")
        else:
            blocked = []
            if not ingest_clean:
                blocked.append("ingestion incomplete (re-run to resume)")
            if not tests_ok:
                blocked.append("verification suite failed")
            print(f"\nsnapshot NOT PUBLISHED: {'; '.join(blocked)}.")
            print(f"  {args.snapshot.name} still holds the last good copy, so "
                  f"readers keep seeing verified data rather than this run's.")
            return 1

    print(f"\nelapsed {time.monotonic() - started:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
