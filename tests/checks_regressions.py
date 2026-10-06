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


# ---------------------------------------------------------------------------
# The flagship ground-truth figure. Every build round since Phase 8 has recorded
# "654/600/54 unchanged" in the spec as prose, re-derived by hand each time. It
# belongs here: it is one query, it touches is_dnp, game classification and the
# home/away flag at once, and a silent change in any of those three moves it.
# ---------------------------------------------------------------------------
@check("the 40+/12+ road count still splits 654/600/54", group="regression: ground truth")
def road_forty_twelve(con):
    rows = {r[0]: r[1] for r in con.execute(
        """SELECT game_type, count(*) FROM v_player_games
            WHERE pts >= 40 AND reb >= 12 AND NOT is_home
            GROUP BY ALL""").fetchall()}
    total = sum(rows.values())
    got = (total, rows.get("regular", 0), rows.get("playoff", 0))
    if got != (654, 600, 54):
        return False, (f"expected 654/600/54, got {got[0]}/{got[1]}/{got[2]} "
                       f"(all game types: {rows})")
    return True, f"654 total = 600 regular + 54 playoff, and nothing else"


# ---------------------------------------------------------------------------
# A stored zero is not always an observation. minutes_played is non-null in
# 100% of played rows back to 1946 but is ZERO for most players before the
# mid-1970s, so counting non-nulls reported it as fully covered and the app
# showed Bill Russell at 31.3 minutes a game in 1961-62 (really 44.9 over the
# 53 of his 76 appearances that carry a figure). Third instance of this
# shape in the project, after the two is_dnp bugs.
# ---------------------------------------------------------------------------
@check("minutes coverage counts values, not nulls", group="regression: zero-is-missing")
def minutes_coverage_counts_values(con):
    import agent
    if "minutes_played" not in agent.ZERO_MEANS_MISSING:
        return False, "minutes_played is no longer treated as zero-means-missing"
    cov = agent.CoverageTool(con)
    import json as _json
    d = _json.loads(cov.run(["minutes", "pts"]))
    m, p = d["minutes"], d["pts"]
    if m.get("first_season_reliable_95pct") is None:
        return False, "minutes has no reliable season at all"
    if m["first_season_reliable_95pct"] < 1965:
        return False, (f"minutes reported reliable from "
                       f"{m['first_season_reliable_95pct']}; the null-counting "
                       "bug is back")
    # The rule must stay narrow: a zero is a real observation for counting
    # stats, and applying it to points would understate every one of them.
    if p.get("first_season_reliable_95pct") != 1946:
        return False, (f"points reliable from {p.get('first_season_reliable_95pct')}, "
                       "expected 1946 — the zero rule has leaked onto a "
                       "counting stat")
    if "value > 0" not in (m.get("counted_as_present") or ""):
        return False, "minutes does not report that it counts values"
    if "not null" not in (p.get("counted_as_present") or ""):
        return False, "points does not report that it counts nulls"
    return True, (f"minutes reliable from {m['first_season_reliable_95pct']} "
                  f"(was 1946 under null-counting); points unchanged at 1946")


@check("per-36 never divides by an unrecorded zero", group="regression: zero-is-missing")
def per36_nullifs_zero(con):
    """The per36 macro guards with NULLIF, so a zero-minute row yields NULL
    rather than a fabricated rate. Verified on the worst era."""
    bad = con.execute("""
        SELECT count(*) FROM v_player_games g
        JOIN v_player_game_per36 p USING (player_id, game_id)
        WHERE NOT g.is_dnp AND g.minutes_played = 0
          AND p.pts_per36 IS NOT NULL""").fetchone()[0]
    if bad:
        return False, f"{bad} rows have a per-36 figure despite zero minutes"
    sampled = con.execute("""
        SELECT count(*) FROM v_player_games
        WHERE NOT is_dnp AND minutes_played = 0 AND season < 1970""").fetchone()[0]
    return True, (f"no per-36 value is produced from a zero-minute row "
                  f"({sampled:,} such rows before 1970)")
