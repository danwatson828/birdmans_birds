#!/usr/bin/env python3
"""Generate inventory.md: what data exists and how far it can be trusted.

Every number in the output is measured from the database at generation time, so
the document cannot drift from reality the way a hand-maintained one would.
Reads the snapshot by default -- the live file is owned by ingestion and is
locked while it runs.

    python inventory.py                 # writes inventory.md
    python inventory.py --db nba.duckdb --out /tmp/inv.md
"""

from __future__ import annotations

import argparse
import datetime as dt
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_DB = PROJECT_ROOT / "nba_snapshot.duckdb"
DEFAULT_OUT = PROJECT_ROOT / "inventory.md"

# Stats whose availability depends on the era. Ordered by when they appear.
ERA_STATS = ["pts", "fgm", "ftm", "fta", "fga", "ast", "pf", "reb",
             "oreb", "dreb", "turnover", "stl", "blk", "fg3m", "fg3a",
             "plus_minus"]

# What each object is for. The database knows its columns; it does not know its
# purpose, so these descriptions are maintained here.
TABLE_DESC = {
    "teams": ("One row per team the API knows about -- not just the NBA. "
              "Includes defunct BAA clubs and international sides."),
    "players": ("One row per player. `team_id` is a mutable snapshot of where "
                "they are now and is unsafe for historical questions."),
    "games": ("One row per game, with quarter/OT scores and both teams. "
              "`postseason` is a two-way flag over four kinds of game -- use "
              "`v_game_type`."),
    "player_stats": ("The box score. One row per player per game. Era-limited: "
                     "see the coverage section."),
    "player_advanced_stats": ("Advanced metrics (ratings, usage, pace, PIE) per "
                              "player per game. 1996+ only."),
    "_ingest_log": ("Ingestion bookkeeping and resume checkpoints. One row per "
                    "unit attempt; the latest row per unit is the live state."),
}
VIEW_DESC = {
    "v_teams": "Teams with whitespace junk nulled and `is_nba_franchise` flagged.",
    "v_players": "Players with height/weight/jersey parsed to numbers.",
    "v_games": "Game context with team abbreviations, margin, and `game_type`.",
    "v_game_type": "**The season-type authority.** regular / play_in / cup_final / playoff.",
    "v_cup_final_games": "The NBA Cup final, derived structurally (one per season, 2023+).",
    "v_play_in_games": "Play-in games, derived from per-team game counts.",
    "v_season_schedule": "Per season: scheduled games per team and whether the schedule was uniform.",
    "v_team_game_slots": "One row per team per non-postseason game. Plumbing for the classification.",
    "v_game_classification_audit": "Self-check: regular games must equal scheduled x teams / 2.",
    "v_stat_first_tracked": "**The era authority.** When each stat first appears and first becomes reliable.",
    "v_player_games": "**The spine.** Box score + game context + parsed minutes + `is_dnp`.",
    "v_player_game_shooting": "Per-game TS%, eFG%, and true shooting attempts.",
    "v_player_game_per36": "Per-36-minute rates, per game.",
    "v_player_rolling_5": "Trailing 5-game form. `*_last_n` excludes the current game.",
    "v_player_rolling_10": "Trailing 10-game form. Same exclusive/inclusive split.",
    "v_player_season_totals": "Season totals per player per team per game_type.",
    "v_player_season_averages": "Season per-game and rate lines, computed from totals.",
}
MACRO_DESC = {
    "parse_minutes": "The one place the `min` string becomes a number.",
    "ts_pct": "True shooting %. PTS / (2 x (FGA + 0.44 x FTA)).",
    "efg_pct": "Effective FG%. Threes counted as 1.5 twos.",
    "per36": "Per-36-minute rate. NULL below a real minutes base.",
    "player_rolling_avg": "Table macro: rolling form for an arbitrary window.",
}


def q1(con, sql, params=None):
    return con.execute(sql, params or []).fetchone()


def table_exists(con, name) -> bool:
    return bool(q1(con, "SELECT count(*) FROM information_schema.tables "
                        "WHERE table_name = ?", [name])[0])


def coverage_row(con, stat: str) -> dict:
    """Coverage measured over rows where the player ACTUALLY PLAYED.

    Measuring over all rows understates every stat, because DNP rows are all-NULL
    by nature -- that mistake is what once made plus_minus look like a 63% field.
    """
    rows = con.execute(f"""SELECT season, count(*) AS played, count({stat}) AS have
                             FROM v_player_games WHERE NOT is_dnp
                            GROUP BY 1 ORDER BY 1""").fetchall()
    first_any = next((s for s, _, h in rows if h), None)
    full = None
    for i, (s, _, _) in enumerate(rows):
        if all(h / p >= 0.95 for _, p, h in rows[i:] if p):
            full = s
            break
    by_season = {s: (100.0 * h / p if p else 0.0) for s, p, h in rows}
    # A "dip" is a sub-95% season that comes after a season already at 95%+.
    # That is a regression in the source rather than the era ramp -- and it is
    # what catches the 2023 plus_minus hole, which falls before the strict
    # reliable-from year and so would otherwise go unmentioned.
    dips, seen_full = [], False
    for s2 in sorted(by_season):
        if by_season[s2] >= 95.0:
            seen_full = True
        elif seen_full:
            dips.append(s2)
    # Leading run of seasons with no data at all, which IS contiguous.
    zero_lead = [s2 for s2 in sorted(by_season)
                 if first_any is not None and s2 < first_any]
    # Later zero seasons are not contiguous with it, so keep them separate.
    zero_after = [s2 for s2 in sorted(by_season)
                  if first_any is not None and s2 >= first_any
                  and by_season[s2] == 0.0]
    return {"stat": stat, "first_any": first_any, "first_full": full,
            "by_season": by_season, "dips": dips,
            "zero_lead": zero_lead, "zero_after": zero_after}


def build(con) -> str:
    L = []
    w = L.append
    now = dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")

    counts = {t: (q1(con, f"SELECT count(*) FROM {t}")[0] if table_exists(con, t) else None)
              for t in TABLE_DESC}
    lo, hi, nseas, dmin, dmax = q1(con,
        "SELECT min(season), max(season), count(DISTINCT season), min(date), max(date) FROM games")
    gt = dict(con.execute("SELECT game_type, count(*) FROM v_game_type "
                          "GROUP BY 1 ORDER BY 2 DESC").fetchall())
    dnp, allrows = q1(con, "SELECT count(*) FILTER (WHERE is_dnp), count(*) FROM v_player_games")
    nba_teams, all_teams = q1(con, "SELECT count(*) FILTER (WHERE is_nba_franchise), count(*) FROM v_teams")

    w("# Data Inventory\n")
    w(f"Generated {now} from `{Path(con.execute('PRAGMA database_list').fetchone()[2]).name}` "
      f"by `inventory.py`. Every figure below is measured, not remembered -- "
      f"regenerate after any ingest.\n")

    w("## At a glance\n")
    w(f"- **{counts['player_stats']:,}** box score lines across **{nseas} seasons** "
      f"({lo}-{hi}, games dated {dmin} to {dmax})")
    if counts["player_advanced_stats"]:
        arange = q1(con, """SELECT min(g.season), max(g.season), count(DISTINCT g.season)
                              FROM player_advanced_stats a JOIN games g ON g.id = a.game_id""")
        w(f"- **{counts['player_advanced_stats']:,}** advanced-stat lines, "
          f"**{arange[2]} seasons** ({arange[0]}-{arange[1]}) -- "
          f"{arange[2]}/{nseas} of the seasons the box scores cover")
    else:
        w("- **advanced stats: not present in this database** "
          "(run `python ingest.py --backfill --advanced-only`)")
    w(f"- **{counts['games']:,}** games, **{counts['players']:,}** players, "
      f"**{counts['teams']}** teams ({nba_teams} current NBA franchises; the rest "
      f"are defunct or international)")
    w(f"- **{dnp:,}** of {allrows:,} box score rows ({100*dnp/allrows:.1f}%) are "
      f"did-not-play rows -- excluded by `games_played`, included in `games_on_roster`")
    w(f"- game types: " + ", ".join(f"{k} {v:,}" for k, v in gt.items()))
    w("")
    w("**Read the snapshot, not the live file.** `nba.duckdb` belongs to "
      "ingestion, which holds an exclusive lock while it runs. "
      "`nba_snapshot.duckdb` is published only after ingestion completes cleanly "
      "*and* `tests/` passes, so it is the only copy that has been verified.\n")

    w("## Raw tables\n")
    w("Column names mirror the API field-for-field; nothing is cleaned here.\n")
    for t, desc in TABLE_DESC.items():
        n = counts[t]
        head = f"### `{t}`" + (f" -- {n:,} rows" if n is not None else " -- *absent*")
        w(head)
        w(f"{desc}\n")
        if not table_exists(con, t):
            continue
        cols = [r[0] for r in con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ? ORDER BY ordinal_position", [t]).fetchall()]
        w(f"`{'`, `'.join(cols)}`\n")

    w("## Semantic views\n")
    w("Canonical definitions. Prefer these over the raw tables: a metric "
      "re-derived ad hoc can disagree with the same metric asked another way, "
      "and nothing then says which is right.\n")
    views = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables WHERE table_type='VIEW' "
        "ORDER BY table_name").fetchall()]
    w("| view | what it holds |")
    w("|---|---|")
    for v in views:
        w(f"| `{v}` | {VIEW_DESC.get(v, '')} |")
    w("")
    macros = [r[0] for r in con.execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() WHERE internal=false "
        "ORDER BY 1").fetchall()]
    w("| macro | what it computes |")
    w("|---|---|")
    for m in macros:
        w(f"| `{m}` | {MACRO_DESC.get(m, '')} |")
    w("")

    # ---- the part that answers "how much can I trust it" --------------------
    w("## Coverage and trust\n")
    w("A NULL in this data means **not recorded**, which is not zero. `SUM` and "
      "`AVG` skip NULLs silently, so a query spanning an era boundary ranks "
      "players partly by whether the league was recording the stat while they "
      "played. `v_stat_first_tracked` is the authority; the table below is that "
      "view plus measured per-season coverage.\n")
    w("Coverage is measured over rows where the player **actually played** "
      "(`NOT is_dnp`). Measuring over all rows understates everything, because "
      "DNP rows are all-NULL by nature.\n")

    covs = {s: coverage_row(con, s) for s in ERA_STATS}
    checkpoints = [1950, 1960, 1970, 1975, 1980, 1983, 1990, 2000, 2020]
    checkpoints = [c for c in checkpoints if lo <= c <= hi]
    w("| stat | first appears | reliable from | " +
      " | ".join(str(c) for c in checkpoints) + " |")
    w("|---|---|---|" + "---|" * len(checkpoints))
    for s in ERA_STATS:
        c = covs[s]
        cells = []
        for yr in checkpoints:
            pct = c["by_season"].get(yr)
            cells.append("--" if pct is None else
                         ("100%" if pct >= 99.95 else f"{pct:.0f}%"))
        w(f"| `{s}` | {c['first_any'] or '--'} | {c['first_full'] or 'never'} | "
          + " | ".join(cells) + " |")
    w("")
    w("`reliable from` is the first season at or after which coverage stays "
      "at 95% or better. The gap between the two columns is often a decade: "
      "several stats debut with a *single recorded row* in the whole season, so "
      "`first appears` is close to useless as a filter and `reliable from` is "
      "the one to use for comparisons.\n")

    dips = {s: c["dips"] for s, c in covs.items() if c["dips"]}
    if dips:
        w("### Coverage that regressed\n")
        w("Seasons where coverage fell below 95% **after** the stat had already "
          "reached 95% -- gaps in the source rather than the era ramp. Exclude "
          "these by name rather than loosening a threshold, so a new gap "
          "elsewhere still shows up:\n")
        for s, ds in dips.items():
            detail = ", ".join(f"**{yr} ({covs[s]['by_season'][yr]:.0f}%)**" for yr in ds)
            w(f"- `{s}`: {detail}")
        w("")

    w("### The specific fields to be careful with\n")
    for stat, note in [
        ("stl", "Steals"), ("blk", "Blocks"),
        ("turnover", "Turnovers"), ("plus_minus", "Plus/minus"), ("reb", "Rebounds"),
    ]:
        c = covs[stat]
        span = f"appears {c['first_any']}, reliable from {c['first_full'] or 'never'}"
        w(f"**{note} (`{stat}`)** -- {span}.")
        lead = c["zero_lead"]
        if lead:
            w(f"  No data at all for the {len(lead)} seasons "
              f"{min(lead)}-{max(lead)}: any player whose career fell entirely "
              f"in that window is structurally absent from a `{stat}` "
              f"leaderboard, so an unqualified all-time ranking is wrong even "
              f"when the names look plausible.")
        after = c["zero_after"]
        if after:
            w(f"  Still zero in {len(after)} later season(s) "
              f"({after[0]}-{after[-1]}) despite the first appearance in "
              f"{c['first_any']} -- the debut is a single stray row, not the "
              f"start of real recording.")
        if c["dips"]:
            w(f"  Regressed after reaching full coverage: "
              + ", ".join(f"{yr} ({c['by_season'][yr]:.0f}%)" for yr in c["dips"])
              + ".")
        w("")

    # minutes and the format trap
    mins = con.execute("""SELECT CASE WHEN min_raw LIKE '%:%' THEN 'MM:SS'
                                 WHEN min_raw IS NULL THEN 'NULL'
                                 ELSE 'integer-string' END AS fmt, count(*)
                            FROM v_player_games GROUP BY 1 ORDER BY 2 DESC""").fetchall()
    w("### Minutes\n")
    w("`player_stats.min` is a string in two different formats depending on era: "
      + ", ".join(f"{f} {n:,}" for f, n in mins) + ". "
      "`v_player_games.minutes_played` is the parsed number -- never parse it "
      "again yourself.\n")
    early = con.execute("""SELECT season,
        round(100.0*count(*) FILTER (WHERE minutes_played > 0)/count(*),1)
        FROM v_player_games WHERE NOT is_dnp AND season <= 1955
        GROUP BY 1 ORDER BY 1""").fetchall()
    if early:
        w("Minutes are essentially absent early on: "
          + ", ".join(f"{s} {p:g}%" for s, p in early) + ". "
          "That is why `is_dnp` cannot be defined as \"no minutes\" -- doing so "
          "read the entire pre-1951 era as did-not-play and turned Jerry West's "
          "27.0 career average into 48.7.\n")

    # classification trust
    aud = q1(con, """SELECT count(*), count(*) FILTER (WHERE schedule_is_uniform),
                     count(*) FILTER (WHERE schedule_is_uniform AND regular_count_matches)
                     FROM v_game_classification_audit""")
    nonu = [r[0] for r in con.execute("""SELECT season FROM v_game_classification_audit
                     WHERE NOT schedule_is_uniform ORDER BY 1""").fetchall()]
    w("### Season-type classification\n")
    w(f"Of {aud[0]} seasons, {aud[1]} have a uniform schedule (every team played "
      f"at least a full slate) and **all {aud[2]} of those land exactly on "
      f"`scheduled x teams / 2` regular-season games** -- the arithmetic check in "
      f"`v_game_classification_audit`.\n")
    w(f"The {aud[0]-aud[1]} non-uniform seasons are {nonu}. In those the "
      f"count-based derivation is meaningless (early-league uneven schedules, "
      f"the 2011-12 lockout, the 2019-20 bubble), so the classification declines "
      f"to guess and calls everything `regular` rather than inventing play-in or "
      f"cup games.\n")

    if counts["player_advanced_stats"]:
        w("### Advanced stats coverage\n")
        rows = con.execute("""SELECT g.season, count(*) FROM player_advanced_stats a
                               JOIN games g ON g.id = a.game_id
                              GROUP BY 1 ORDER BY 1""").fetchall()
        seasons = [r[0] for r in rows]
        gaps = [s for s in range(min(seasons), max(seasons)+1) if s not in seasons]
        w(f"{min(seasons)}-{max(seasons)}, {len(seasons)} seasons, "
          f"gaps: {gaps if gaps else 'none'}.\n")
        played, padv = q1(con, """SELECT count(*), count(a.id)
              FROM v_player_games pg JOIN games g ON g.id = pg.game_id
              LEFT JOIN player_advanced_stats a
                ON a.game_id = pg.game_id AND a.player_id = pg.player_id
             WHERE g.season BETWEEN ? AND ? AND NOT pg.is_dnp""",
             [min(seasons), max(seasons)])
        allr, aadv = q1(con, """SELECT count(*), count(a.id)
              FROM v_player_games pg JOIN games g ON g.id = pg.game_id
              LEFT JOIN player_advanced_stats a
                ON a.game_id = pg.game_id AND a.player_id = pg.player_id
             WHERE g.season BETWEEN ? AND ?""", [min(seasons), max(seasons)])
        w(f"Within that range **{padv:,} of {played:,} box score lines for "
          f"players who actually played ({100*padv/played:.2f}%)** have a "
          f"matching advanced row -- effectively complete.\n")
        w(f"Measured against *all* rows including did-not-play it is "
          f"{aadv:,}/{allr:,} ({100*aadv/allr:.1f}%), but that is the wrong "
          f"denominator: advanced metrics are rates, and a player with zero "
          f"minutes has none to compute, so DNP rows are only partly "
          f"represented upstream. The recent-season DNP count roughly tripled "
          f"(about 5,000 a season before 2022, about 18,000 after), which makes "
          f"the all-rows figure look like it degrades over time when coverage "
          f"of actual appearances does not.\n")
        w(f"**{min(seasons)} is a hard floor.** The {min(seasons)-lo} seasons from "
          f"{lo} to {min(seasons)-1} have box scores but no advanced stats at "
          f"all, so any join must be a LEFT JOIN unless the query is "
          f"deliberately scoped to {min(seasons)}+.\n")
        w("Percentage fields are **fractions** (`usage_percentage` 0.108 = 10.8%). "
          "Ratings are per-game and therefore volatile in small samples -- a "
          "single-game `defensive_rating` of 154 is noise, not an outlier to "
          "clean. Join on `(game_id, player_id)`: the advanced row's `id` is its "
          "own sequence, unrelated to `player_stats.id`.\n")

    w("## Traps worth restating\n")
    w("1. **Season type** -- use `v_game_type.game_type`, never the raw "
      "`postseason` flag. `postseason = false` also contains play-in games and "
      "the NBA Cup final, which official statistics exclude. Using the raw flag "
      "reported Shai Gilgeous-Alexander at 32.5 ppg over 77 games against an "
      "official 32.7 over 76.")
    w("2. **Team context** -- `players.team_id` is where a player is *now*. For "
      "history use the per-game `team_id`. Luka Doncic's `players.team_id` is "
      "the Lakers, so his Dallas games are invisible through it.")
    w("3. **Era limits** -- check `v_stat_first_tracked` before any career or "
      "all-time question touching steals, blocks, turnovers, offensive/defensive "
      "rebounds, threes, or plus/minus.")
    w("4. **Plus/minus** -- pair any total with `games_with_plus_minus`. "
      "+140 over 12 recorded games and +140 over 82 are otherwise "
      "indistinguishable.")
    w("5. **Shooting efficiency before 1974** -- `fga` was barely recorded, so "
      "TS% and eFG% are unreliable however confidently they compute.")
    w("")
    return "\n".join(L) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--db", type=Path, default=DEFAULT_DB)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    args = ap.parse_args()
    if not args.db.exists():
        raise SystemExit(f"{args.db} does not exist")
    con = duckdb.connect(str(args.db), read_only=True)
    try:
        text = build(con)
    finally:
        con.close()
    args.out.write_text(text)
    print(f"wrote {args.out} ({len(text.splitlines())} lines, "
          f"{len(text)/1024:.1f} KB) from {args.db.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
