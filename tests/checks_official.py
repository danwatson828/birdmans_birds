"""Agreement with published NBA statistics.

These are the checks that actually prove the semantic layer is right, because
they compare against numbers nobody in this project computed. The 2024-25 trio
is chosen to exercise both game-type exclusions: two NBA Cup finalists and one
play-in team.
"""

from .harness import Skip, check, one, require_seasons

# name, official ppg, official games played, what it exercises
OFFICIAL_2024 = [
    ("Shai Gilgeous-Alexander", 32.7, 76, "Cup finalist (OKC)"),
    ("Giannis Antetokounmpo",   30.4, 67, "Cup finalist (MIL)"),
    ("Trae Young",              24.2, 76, "play-in team (ATL)"),
]

# name, first season, last season, career regular-season games, career ppg.
# Career figures are through 2024-25, so the query is capped at season <= 2024.
CAREERS = [
    ("Jerry West",          1960, 1973,  932, 27.0),
    ("Oscar Robertson",     1960, 1973, 1040, 25.7),
    ("Kareem Abdul-Jabbar", 1969, 1988, 1560, 24.6),
    ("Michael Jordan",      1984, 2002, 1072, 30.1),
    ("LeBron James",        2003, 2024, 1562, 27.0),
]


def _regular_line(con, name, max_season=None):
    sql = ("SELECT sum(games_played), sum(pts) FROM v_player_season_totals "
           "WHERE player_name = ? AND game_type = 'regular'")
    params = [name]
    if max_season is not None:
        sql += " AND season <= ?"
        params.append(max_season)
    return one(con, sql, params)


def _make_season_check(name, ppg, games, why):
    @check(f"{name} 2024-25 == official ({games}g, {ppg} ppg)", group="official averages")
    def _fn(con, name=name, ppg=ppg, games=games):
        require_seasons(con, 2024, 2024, f"{name} 2024-25")
        row = one(con, """SELECT sum(games_played), sum(pts)
                            FROM v_player_season_totals
                           WHERE player_name = ? AND season = 2024
                             AND game_type = 'regular'""", [name])
        if not row or row[0] is None:
            raise Skip(f"{name} has no 2024 rows")
        gp, pts = row
        return (gp == games and abs(pts / gp - ppg) < 0.05,
                f"{gp}g {pts/gp:.2f} ppg")
    return _fn


for _n, _p, _g, _w in OFFICIAL_2024:
    _make_season_check(_n, _p, _g, _w)


def _make_career_check(name, first, last, games, ppg):
    @check(f"{name} career == actual ({games}g, {ppg} ppg)", group="official careers")
    def _fn(con, name=name, first=first, last=last, games=games, ppg=ppg):
        require_seasons(con, first, last, f"{name}'s career span")
        row = _regular_line(con, name, max_season=2024)
        if not row or row[0] is None:
            raise Skip(f"{name} has no rows")
        gp, pts = row
        # Tolerances are deliberately tight: 5 games and 0.4 ppg. Wide enough to
        # absorb small provider discrepancies, narrow enough that the is_dnp bug
        # (which read Jerry West as 517 games / 48.7 ppg) can never slip through.
        return (abs(gp - games) <= 5 and abs(pts / gp - ppg) < 0.4,
                f"{gp}g {pts/gp:.2f} ppg")
    return _fn


for _args in CAREERS:
    _make_career_check(*_args)
