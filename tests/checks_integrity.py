"""Structural integrity of the raw tables and the ingestion bookkeeping."""

from .harness import Skip, check, one, scalar, season_span


@check("database is not empty", group="integrity")
def database_has_data(con):
    """A floor, not a real assertion.

    Most integrity checks are vacuously true on an empty database (an empty table
    has no duplicates), and every data-dependent check skips. Without this, a
    freshly initialised database would pass the suite and could be published as a
    snapshot -- replacing good data with nothing. This makes emptiness a failure
    rather than a clean sweep.
    """
    stats = scalar(con, "SELECT count(*) FROM player_stats")
    games = scalar(con, "SELECT count(*) FROM games")
    return stats > 0 and games > 0, f"{stats:,} stat rows, {games:,} games"


@check("no duplicate (game_id, player_id)", group="integrity")
def grain_is_unique(con):
    tot, dis = one(con, "SELECT count(*), count(DISTINCT (game_id, player_id)) "
                        "FROM player_stats")
    if tot == 0:
        raise Skip("player_stats is empty")
    return tot == dis, f"{tot:,} rows / {dis:,} distinct"


@check("no duplicate stat id", group="integrity")
def stat_id_is_unique(con):
    n = scalar(con, "SELECT count(*) FROM (SELECT id FROM player_stats "
                    "GROUP BY id HAVING count(*) > 1)")
    return n == 0, f"{n} duplicated ids"


@check("every stat row resolves to a game", group="integrity")
def no_orphan_games(con):
    n = scalar(con, "SELECT count(*) FROM player_stats ps "
                    "LEFT JOIN games g ON g.id = ps.game_id WHERE g.id IS NULL")
    return n == 0, f"{n} orphans"


@check("every stat row resolves to a player", group="integrity")
def no_orphan_players(con):
    n = scalar(con, "SELECT count(*) FROM player_stats ps "
                    "LEFT JOIN players p ON p.id = ps.player_id WHERE p.id IS NULL")
    return n == 0, f"{n} orphans"


@check("every game has a season", group="integrity")
def games_have_season(con):
    return scalar(con, "SELECT count(*) FROM games WHERE season IS NULL") == 0


@check("semantic spine covers every stat row", group="integrity")
def spine_is_complete(con):
    raw = scalar(con, "SELECT count(*) FROM player_stats")
    if raw == 0:
        raise Skip("player_stats is empty")
    spine = scalar(con, "SELECT count(*) FROM v_player_games")
    return spine == raw, f"{spine:,} vs {raw:,}"


@check("no gap in the ingested season range", group="integrity")
def seasons_contiguous(con):
    lo, hi = season_span(con)
    if lo is None:
        raise Skip("games table is empty")
    missing = [r[0] for r in con.execute(
        """WITH s AS (SELECT DISTINCT season FROM games)
           SELECT season + 1 FROM s
            WHERE season + 1 NOT IN (SELECT season FROM s)
              AND season < (SELECT max(season) FROM s) ORDER BY 1""").fetchall()]
    return not missing, f"{lo}-{hi}, missing {missing}" if missing else f"{lo}-{hi}"


@check("zero unfinished ingest units", group="integrity")
def no_unfinished_units(con):
    # Counts UNITS by latest status, not log rows: every resume leaves its
    # earlier partial row behind, so counting rows overstates this.
    n = scalar(con, """SELECT count(*) FROM (
        SELECT max_by(json_extract_string(params, '$.status'), id) AS status
          FROM _ingest_log WHERE endpoint = 'stats'
         GROUP BY json_extract_string(params, '$.unit'))
        WHERE status IS DISTINCT FROM 'complete'""")
    return n == 0, f"{n} unfinished"


@check("zero anomalous rows skipped during ingest", group="integrity")
def no_ingest_anomalies(con):
    n = scalar(con, "SELECT count(*) FROM _ingest_log WHERE endpoint = 'stats:anomaly'")
    return n == 0, f"{n} skipped"
