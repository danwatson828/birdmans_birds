"""Game classification: regular / play_in / cup_final / playoff.

The load-bearing invariant is arithmetic: in a season where every team played at
least a full slate, a correct classification must leave exactly
scheduled_games * teams / 2 regular-season games. Nothing here depends on a
hand-maintained list of dates.
"""

from .harness import Skip, check, one, scalar, require_seasons

KNOWN_CUP_FINALS = {2023: "2023-12-09", 2024: "2024-12-17", 2025: "2025-12-16"}


@check("every uniform season hits scheduled x teams / 2", group="classification")
def uniform_seasons_match_arithmetic(con):
    total = scalar(con, "SELECT count(*) FROM v_game_classification_audit "
                        "WHERE schedule_is_uniform")
    if total == 0:
        raise Skip("no schedule-uniform season in this database")
    bad = con.execute(
        """SELECT season, scheduled_games, regular_games, expected_regular_games
             FROM v_game_classification_audit
            WHERE schedule_is_uniform AND NOT regular_count_matches
            ORDER BY season""").fetchall()
    return not bad, (f"{total} uniform seasons, all match" if not bad
                     else f"mismatches: {bad}")


@check("every game carries a game_type", group="classification")
def game_type_is_total(con):
    n = scalar(con, "SELECT count(*) FROM v_game_type WHERE game_type IS NULL")
    return n == 0, f"{n} unclassified"


@check("cup finals are exactly the known games", group="classification")
def cup_finals_match_truth(con):
    lo, hi = one(con, "SELECT min(season), max(season) FROM games")
    if lo is None:
        raise Skip("games table is empty")
    expected = {s: d for s, d in KNOWN_CUP_FINALS.items() if lo <= s <= hi}
    if not expected:
        raise Skip(f"no NBA Cup season in range {lo}-{hi}")
    got = {s: str(d) for s, d in con.execute(
        "SELECT season, date FROM v_cup_final_games ORDER BY season").fetchall()}
    return got == expected, f"got {got}, expected {expected}"


@check("six play-in games in each play-in season", group="classification")
def play_in_counts(con):
    lo, hi = one(con, "SELECT min(season), max(season) FROM games")
    if lo is None:
        raise Skip("games table is empty")
    counts = dict(con.execute("SELECT season, count(*) FROM v_game_type "
                              "WHERE game_type = 'play_in' GROUP BY 1").fetchall())
    # 2022-2025 are the seasons where the API flags play-in games as
    # postseason=false, so they are the ones this layer has to classify.
    seasons = [s for s in (2022, 2023, 2024, 2025) if lo <= s <= hi]
    if not seasons:
        raise Skip(f"no 2022-2025 season in range {lo}-{hi}")
    wrong = {s: counts.get(s) for s in seasons if counts.get(s) != 6}
    return not wrong, f"checked {seasons}" if not wrong else f"wrong: {wrong}"


@check("non-uniform seasons invent nothing", group="classification")
def non_uniform_seasons_decline(con):
    # The count-based derivation is invalid when teams played different numbers
    # of games by design (the COVID bubble, early-league schedules). Those
    # seasons must fall back to 'regular', never guess a play-in or cup final.
    bad = con.execute(
        """SELECT season, play_in_games, cup_final_games
             FROM v_game_classification_audit
            WHERE NOT schedule_is_uniform
              AND (play_in_games > 0 OR cup_final_games > 0)""").fetchall()
    n = scalar(con, "SELECT count(*) FROM v_game_classification_audit "
                    "WHERE NOT schedule_is_uniform")
    if n == 0:
        raise Skip("no non-uniform season in this database")
    return not bad, f"{n} non-uniform seasons, none guessed" if not bad else str(bad)


@check("traded player keeps one id and splits by team", group="classification")
def trade_split(con):
    # Luka Doncic, 2024-25: 22 games for Dallas then 28 for the Lakers under one
    # player_id. Design spec section 4 -- the provider's id must survive a trade.
    require_seasons(con, 2024, 2024, "Luka trade case")
    rows = con.execute(
        """SELECT team_id, games_played FROM v_player_season_totals
            WHERE player_id = 132 AND season = 2024 AND game_type = 'regular'
            ORDER BY team_id""").fetchall()
    if not rows:
        raise Skip("player 132 has no 2024 rows in this database")
    ids = scalar(con, "SELECT count(DISTINCT player_id) FROM v_player_games "
                      "WHERE player_id = 132")
    ok = len(rows) == 2 and sum(r[1] for r in rows) == 50 and ids == 1
    return ok, f"{rows}, distinct player_ids={ids}"
