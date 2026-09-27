"""Regression guards for defects already found and fixed.

Each check names the bug it prevents and the symptom it produced, so a future
failure here is self-explaining. All three were invisible on a single modern
season and only surfaced against the full 1946-2025 history.
"""

from .harness import Skip, check, one, scalar, season_span

# ---------------------------------------------------------------------------
# BUG 1 -- is_dnp treated "minutes not recorded" as "did not play".
# Minutes are absent before 1951 and patchy through the 1950s, so every early
# row read as a did-not-play. 116,856 rows league-wide were flagged DNP while
# having scored points, and Jerry West came out at 517 games / 48.74 ppg instead
# of 932 / 27.0.
# ---------------------------------------------------------------------------
@check("no row is flagged DNP while showing production", group="regression: is_dnp")
def dnp_never_contradicts_production(con):
    n = scalar(con, """SELECT count(*) FROM v_player_games
                        WHERE is_dnp
                          AND coalesce(pts, 0) + coalesce(fga, 0) + coalesce(fta, 0)
                            + coalesce(reb, 0) + coalesce(ast, 0) + coalesce(pf, 0) > 0""")
    return n == 0, f"{n} contradictory rows (was 116,856 with the bug)"


@check("pre-minutes era is not wholesale DNP", group="regression: is_dnp")
def early_era_dnp_rate_is_sane(con):
    lo, _ = season_span(con)
    if lo is None or lo > 1950:
        raise Skip("database does not reach the pre-1951 era")
    row = one(con, """SELECT count(*), count(*) FILTER (WHERE is_dnp)
                        FROM v_player_games WHERE season <= 1950""")
    total, dnp = row
    if total == 0:
        raise Skip("no pre-1951 rows")
    share = dnp / total
    # With the bug this was ~100%. Genuine DNPs in that era are a small minority.
    return share < 0.25, f"{dnp:,}/{total:,} = {share*100:.1f}% DNP"


@check("games_played tracks roster rows when minutes are unrecorded",
       group="regression: is_dnp")
def minutes_absent_does_not_drop_games(con):
    # In the pre-minutes era the API only emits rows for players who appeared, so
    # games_played should track games_on_roster closely. The bug made them
    # diverge by ~45%.
    lo, _ = season_span(con)
    if lo is None or lo > 1950:
        raise Skip("database does not reach the pre-1951 era")
    row = one(con, """SELECT sum(games_on_roster), sum(games_played)
                        FROM v_player_season_totals WHERE season <= 1950""")
    roster, played = row
    if not roster:
        raise Skip("no pre-1951 rows")
    ratio = played / roster
    return ratio > 0.80, f"played/roster = {ratio:.3f} (was ~0.55 with the bug)"


# ---------------------------------------------------------------------------
# BUG 2 -- v_stat_first_tracked was built from a 100-row-per-season sample and
# was wrong on five of sixteen stats: steals declared 1973 when one 1970 row has
# them, turnovers 1977 when one 1963 row does, oreb/dreb 1973 when 1962 has one.
# Sampling finds the era, not the edge. This check re-derives the answer from the
# full data every run, so a re-sampled table cannot pass.
# ---------------------------------------------------------------------------
@check("declared onset season matches the data", group="regression: era table")
def era_onsets_match_data(con):
    lo, hi = season_span(con)
    if lo is None:
        raise Skip("games table is empty")
    declared = con.execute("SELECT stat, first_tracked_season FROM v_stat_first_tracked "
                           "ORDER BY stat").fetchall()
    wrong, checked = [], 0
    for stat, first in declared:
        # Only meaningful when the database actually spans the declared onset.
        if lo > first:
            continue
        actual = scalar(con, f"SELECT min(season) FROM v_player_games "
                             f"WHERE {stat} IS NOT NULL")
        if actual is None:
            continue
        checked += 1
        if actual != first:
            wrong.append((stat, f"declared {first}, data {actual}"))
    if checked == 0:
        raise Skip(f"no declared onset falls inside {lo}-{hi}")
    return not wrong, f"{checked} stats verified" if not wrong else str(wrong)


@check("first_full_season really is >=95% complete onward", group="regression: era table")
def first_full_season_holds(con):
    # The contract of first_full_season. Coverage is measured over rows where the
    # player actually played -- measuring over all rows is exactly the mistake
    # that made plus_minus look like a 63% field.
    lo, hi = season_span(con)
    if lo is None:
        raise Skip("games table is empty")
    rows = con.execute("SELECT stat, first_full_season FROM v_stat_first_tracked "
                       "WHERE first_full_season IS NOT NULL ORDER BY stat").fetchall()
    wrong, checked = [], 0
    for stat, first_full in rows:
        if lo > first_full:
            continue
        bad = con.execute(f"""SELECT season,
                     round(100.0 * count({stat}) / count(*), 1) AS pct
                FROM v_player_games
               WHERE NOT is_dnp AND season >= ?
               GROUP BY season
              HAVING count({stat}) < 0.95 * count(*)
               ORDER BY season""", [first_full]).fetchall()
        checked += 1
        if bad:
            wrong.append((stat, first_full, bad[:3]))
    if checked == 0:
        raise Skip(f"no first_full_season falls inside {lo}-{hi}")
    return not wrong, f"{checked} stats verified" if not wrong else str(wrong)


# ---------------------------------------------------------------------------
# BUG 3 -- plus_minus was documented as "never complete, ~63%". That figure was
# measured over ALL rows, including did-not-play rows which are all-NULL by
# nature. Among rows where the player actually played it is ~99.5% complete from
# 2019. Season 2023 is the one genuine hole (45.7%).
# ---------------------------------------------------------------------------
@check("plus_minus is near-complete for played rows from 2019",
       group="regression: plus_minus")
def plus_minus_coverage_over_played_rows(con):
    lo, hi = season_span(con)
    if lo is None or hi < 2019:
        raise Skip(f"database has no 2019+ season (range {lo}-{hi})")
    rows = con.execute("""SELECT season,
                 round(100.0 * count(plus_minus) / count(*), 1) AS pct
            FROM v_player_games
           WHERE NOT is_dnp AND season >= 2019
           GROUP BY season ORDER BY season""").fetchall()
    # 2023 is a known gap in the source and is excluded by name rather than by a
    # loosened threshold, so a NEW gap in another season still fails.
    low = [(s, p) for s, p in rows if s != 2023 and p < 95.0]
    return not low, (f"{len(rows)} seasons checked, 2023 excluded as a known hole"
                     if not low else f"unexpectedly low: {low}")
