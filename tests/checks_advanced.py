"""Advanced stats (/stats/advanced), ingested into player_advanced_stats.

Every check skips on an empty table, so adding this file cannot block the
snapshot gate before the advanced backfill has run.
"""

from .harness import Skip, check, one, scalar, season_span

ADVANCED_FIRST_SEASON = 1996   # measured: 1946-1995 return zero rows


def _require_rows(con):
    n = scalar(con, "SELECT count(*) FROM player_advanced_stats")
    if n == 0:
        raise Skip("player_advanced_stats is empty (advanced backfill not run)")
    return n


@check("no duplicate (game_id, player_id)", group="advanced stats")
def grain_unique(con):
    _require_rows(con)
    tot, dis = one(con, "SELECT count(*), count(DISTINCT (game_id, player_id)) "
                        "FROM player_advanced_stats")
    return tot == dis, f"{tot:,} rows / {dis:,} distinct"


@check("no duplicate id", group="advanced stats")
def id_unique(con):
    _require_rows(con)
    n = scalar(con, "SELECT count(*) FROM (SELECT id FROM player_advanced_stats "
                    "GROUP BY id HAVING count(*) > 1)")
    return n == 0, f"{n} duplicated"


@check("every row resolves to a game and a player", group="advanced stats")
def no_orphans(con):
    _require_rows(con)
    g = scalar(con, "SELECT count(*) FROM player_advanced_stats a "
                    "LEFT JOIN games x ON x.id = a.game_id WHERE x.id IS NULL")
    p = scalar(con, "SELECT count(*) FROM player_advanced_stats a "
                    "LEFT JOIN players x ON x.id = a.player_id WHERE x.id IS NULL")
    return g == 0 and p == 0, f"{g} orphan games, {p} orphan players"


@check("no advanced row predates 1996", group="advanced stats")
def era_floor(con):
    _require_rows(con)
    earliest = scalar(con, """SELECT min(g.season) FROM player_advanced_stats a
                              JOIN games g ON g.id = a.game_id""")
    return earliest >= ADVANCED_FIRST_SEASON, f"earliest season {earliest}"


@check("no gap in the ingested advanced season range", group="advanced stats")
def seasons_contiguous(con):
    _require_rows(con)
    seasons = [r[0] for r in con.execute(
        """SELECT DISTINCT g.season FROM player_advanced_stats a
             JOIN games g ON g.id = a.game_id ORDER BY 1""").fetchall()]
    gaps = [s for s in range(min(seasons), max(seasons) + 1) if s not in seasons]
    return not gaps, f"{min(seasons)}-{max(seasons)}, gaps {gaps}" if gaps \
        else f"{min(seasons)}-{max(seasons)} ({len(seasons)} seasons)"


@check("advanced rows cover the played box scores", group="advanced stats")
def coverage_vs_box_scores(con):
    """Within 1996+, nearly every box score line for a player who ACTUALLY
    PLAYED should have a matching advanced row.

    The denominator is played rows, not all rows. Advanced metrics are rates --
    usage, ratings, percentages -- and a player with zero minutes has none to
    compute, so did-not-play rows are only partially represented upstream
    (~37% of them in recent seasons). Measuring against all rows reported 95.6%
    and looked like an ingestion gap; against played rows it is 99.98%.

    This is the same error as measuring plus_minus coverage over DNP-padded
    rows, which is why the denominator is stated explicitly here.
    """
    _require_rows(con)
    lo = scalar(con, """SELECT min(g.season) FROM player_advanced_stats a
                         JOIN games g ON g.id = a.game_id""")
    hi = scalar(con, """SELECT max(g.season) FROM player_advanced_stats a
                         JOIN games g ON g.id = a.game_id""")
    played, adv = one(con, """SELECT count(*), count(a.id)
          FROM v_player_games pg
          JOIN games g ON g.id = pg.game_id
          LEFT JOIN player_advanced_stats a
            ON a.game_id = pg.game_id AND a.player_id = pg.player_id
         WHERE g.season BETWEEN ? AND ? AND NOT pg.is_dnp""", [lo, hi])
    if played == 0:
        raise Skip("no played box score rows in the advanced season range")
    pct = 100.0 * adv / played
    thin = con.execute("""SELECT g.season,
             round(100.0 * count(a.id) / count(*), 1) AS pct
          FROM v_player_games pg
          JOIN games g ON g.id = pg.game_id
          LEFT JOIN player_advanced_stats a
            ON a.game_id = pg.game_id AND a.player_id = pg.player_id
         WHERE g.season BETWEEN ? AND ? AND NOT pg.is_dnp
         GROUP BY g.season HAVING count(a.id) < 0.99 * count(*)
         ORDER BY pct LIMIT 5""", [lo, hi]).fetchall()
    return not thin, (f"{adv:,}/{played:,} played rows = {pct:.2f}% over {lo}-{hi}"
                      + (f"; thin seasons {thin}" if thin else ""))


@check("percentage fields are fractions, not percents", group="advanced stats")
def percentages_are_fractions(con):
    """usage_percentage 0.108 means 10.8%.

    If ESPN-style percent values (10.8) ever appeared instead, every downstream
    rate would be off by 100x, so the range is asserted rather than assumed.
    """
    _require_rows(con)
    bad = []
    for col in ("assist_percentage", "defensive_rebound_percentage",
                "offensive_rebound_percentage", "rebound_percentage",
                "usage_percentage", "true_shooting_percentage",
                "effective_field_goal_percentage"):
        n = scalar(con, f"SELECT count(*) FROM player_advanced_stats "
                        f"WHERE {col} IS NOT NULL AND ({col} < 0 OR {col} > 2)")
        if n:
            bad.append((col, n))
    return not bad, "7 fraction fields in range" if not bad else str(bad)


@check("net_rating equals offensive minus defensive rating", group="advanced stats")
def net_rating_consistent(con):
    """An internal consistency check on the provider's own arithmetic.

    Rounding means this is approximate, so the tolerance is 0.2.
    """
    _require_rows(con)
    n = scalar(con, """SELECT count(*) FROM player_advanced_stats
                        WHERE net_rating IS NOT NULL
                          AND offensive_rating IS NOT NULL
                          AND defensive_rating IS NOT NULL
                          AND abs(net_rating - (offensive_rating - defensive_rating)) > 0.2""")
    tot = scalar(con, "SELECT count(*) FROM player_advanced_stats "
                      "WHERE net_rating IS NOT NULL")
    return n == 0, f"{n:,} of {tot:,} rows inconsistent"
