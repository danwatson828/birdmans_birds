#!/usr/bin/env python3
"""Weekly matchup strategy, lineup optimisation and waiver radar. READ-ONLY.

ADVISORY, NOT BACKTESTED. The draft board's weights and the Phase 6 projections
were both validated walk-forward against a stated benchmark. Nothing in this
file has been: roster context, lineup legality, games-remaining and opponent
behaviour are judgement, and no held-out test says this advice wins matchups.
Every output here states that, because the two tools look alike and the
confidence behind them is not alike.

WHAT IS GROUNDED. Three things are verified rather than assumed:

  * the ESPN -> BallDontLie identity map resolves all 15 roster players;
  * ESPN scores only players in STARTING slots, bench excluded -- confirmed by
    reproducing the week of 2026-03-23 to the last decimal in all nine
    categories (FG% 0.47506562, FT% 0.78888889) from box scores alone;
  * that reproduction also dates the matchup, which ESPN's cache does not carry.

`verify_week()` re-runs that reconciliation, so a change to the identity map or
the scoring rule fails loudly here instead of silently producing worse advice.

    python matchup.py --status            # category state + what would flip it
    python matchup.py --optimise          # best legal lineup for the week
    python matchup.py --waivers           # free agents beating rostered bench
    python matchup.py --verify            # reconcile against the real week
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import duckdb
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
LIVE_DB = PROJECT_ROOT / "nba.duckdb"
CACHE_DIR = PROJECT_ROOT / "espn_cache"
ID_MAP = PROJECT_ROOT / "espn_bdl_id_map.csv"

# ESPN's nine categories, mapped to snapshot columns. TO and the two
# percentages need their own handling: turnovers are won by the LOWER total,
# and a percentage is made/attempted over the week, never an average of
# per-game percentages.
COUNTING = {"PTS": "pts", "REB": "reb", "AST": "ast", "STL": "stl",
            "BLK": "blk", "3PM": "fg3m"}
INVERTED = {"TO": "turnover"}
PCT = {"FG%": ("fgm", "fga"), "FT%": ("ftm", "fta")}
ALL_CATS = [*COUNTING, *PCT, *INVERTED]

BENCH_SLOTS = {"BE", "IR"}
# A full NBA season, used to turn a season-level projected value into a
# per-week share. Mirrors draft_board.FULL_SEASON rather than re-deriving it.
FULL_SEASON = 82


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------
def latest_cache(season: int | None = None) -> Path:
    """The league cache to read.

    Prefers an in-progress season over a not-yet-started one: the upcoming
    league's roster is pre-draft placeholder data and advising on it would be
    advising on a roster that does not exist.
    """
    files = sorted(CACHE_DIR.glob("league_*_*.json"))
    if not files:
        raise SystemExit(f"no league cache in {CACHE_DIR}; run league_sync.py")
    loaded = []
    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        if season and d.get("espn_season") != season:
            continue
        loaded.append((d.get("season_state") == "in_progress", d.get("espn_season"), f))
    if not loaded:
        raise SystemExit(f"no league cache for season {season}")
    loaded.sort(reverse=True)
    return loaded[0][2]


def load_league(path: Path) -> dict:
    d = json.loads(path.read_text())
    idmap = pd.read_csv(ID_MAP)
    to_bdl = {int(r.espn_id): int(r.bdl_player_id) for r in idmap.itertuples()
              if pd.notna(r.bdl_player_id)}
    roster = []
    for p in d["my_team"]["roster"]:
        roster.append({**p, "bdl_id": to_bdl.get(p["player_id"]),
                       "starting": p["lineup_slot"] not in BENCH_SLOTS})
    free = [{**p, "bdl_id": to_bdl.get(p["player_id"])}
            for p in (d.get("free_agents") or [])]
    return {"raw": d, "cache_file": path.name, "roster": roster,
            "free_agents": free, "matchup": d.get("matchup") or {},
            "rules": d.get("roster_rules") or {},
            "season_label": d.get("season_label"),
            "season_state": d.get("season_state"),
            "espn_season": d.get("espn_season"),
            "unmapped": [p["name"] for p in roster if p["bdl_id"] is None]}


# ---------------------------------------------------------------------------
# Actuals over a week
# ---------------------------------------------------------------------------
def week_totals(con, bdl_ids: list[int], start: str, days: int = 7) -> dict:
    """Category totals for a set of players over a date window, from box scores."""
    if not bdl_ids:
        return {}
    ids = ",".join(str(int(x)) for x in bdl_ids)
    row = con.execute(f"""
        SELECT sum(pts) pts, sum(reb) reb, sum(ast) ast, sum(stl) stl,
               sum(blk) blk, sum(fg3m) fg3m, sum(turnover) turnover,
               sum(fgm) fgm, sum(fga) fga, sum(ftm) ftm, sum(fta) fta,
               count(*) AS player_games
          FROM v_player_games
         WHERE game_type='regular' AND NOT is_dnp
           AND player_id IN ({ids})
           AND game_date >= DATE '{start}'
           AND game_date <  DATE '{start}' + INTERVAL {int(days)} DAY
    """).df()
    if row.empty:
        return {}
    r = row.iloc[0]
    out = {k: float(r[v] or 0) for k, v in {**COUNTING, **INVERTED}.items()}
    for cat, (made, att) in PCT.items():
        a = float(r[att] or 0)
        out[cat] = (float(r[made] or 0) / a) if a else 0.0
        out[f"_{made}"], out[f"_{att}"] = float(r[made] or 0), a
    out["_player_games"] = int(r["player_games"] or 0)
    return out


def games_in_window(con, bdl_ids: list[int], start: str, days: int = 7) -> dict:
    """Games each player actually has scheduled in the window.

    Read from the schedule rather than assumed: a player on a team with four
    games in a week is worth more that week than the same player with two, and
    that is the single biggest lever in a weekly league.
    """
    if not bdl_ids:
        return {}
    ids = ",".join(str(int(x)) for x in bdl_ids)
    # ONE team per player. A DISTINCT over (player_id, team_id) returns two
    # rows for anyone traded mid-season -- 87 players in 2025 -- and joining
    # that to the schedule counts BOTH clubs' fixtures, which showed a traded
    # guard with seven games in a seven-day week. The team is resolved as of
    # the window: the most recent club he appeared for at or before it starts,
    # falling back to his first if the window precedes his debut.
    rows = con.execute(f"""
        WITH apps AS (
          SELECT player_id, team_id, game_date,
                 row_number() OVER (
                   PARTITION BY player_id
                   ORDER BY (game_date <= DATE '{start}') DESC,
                            CASE WHEN game_date <= DATE '{start}'
                                 THEN game_date END DESC NULLS LAST,
                            game_date ASC) AS rn
            FROM v_player_games
           WHERE player_id IN ({ids}) AND game_type='regular'
             AND season = (SELECT max(season) FROM v_games
                            WHERE game_type='regular')
        ),
        roster AS (SELECT player_id, team_id FROM apps WHERE rn = 1)
        SELECT r.player_id, count(*) AS games
          FROM roster r
          JOIN v_games g
            ON (g.home_team_id = r.team_id OR g.visitor_team_id = r.team_id)
         WHERE g.game_type='regular'
           AND g.game_date >= DATE '{start}'
           AND g.game_date <  DATE '{start}' + INTERVAL {int(days)} DAY
         GROUP BY r.player_id
    """).df()
    return {int(r.player_id): int(r.games) for r in rows.itertuples()}


def find_week(con, league: dict) -> dict:
    """Recover the matchup's date window by reconciling against box scores.

    ESPN's cache carries a scoring period but no dates. Rather than guess at an
    epoch, this searches every 7-day window in the season for the one whose
    starters' totals reproduce the reported category values. An exact hit is
    strong evidence that the identity map, the starters-only rule and the
    window are all right at once; anything less is reported as a miss rather
    than quietly used.
    """
    mine = league["matchup"].get("my_categories") or {}
    if not mine:
        return {"found": False, "reason": "cache carries no scored categories"}
    starters = [p["bdl_id"] for p in league["roster"]
                if p["starting"] and p["bdl_id"]]
    season = con.execute(
        "SELECT max(season) FROM v_games WHERE game_type='regular'").fetchone()[0]
    dates = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT game_date FROM v_games WHERE game_type='regular' "
        f"AND season={season} ORDER BY game_date").fetchall()]
    target = {k: v["value"] for k, v in mine.items()}
    best = None
    for start in dates:
        got = week_totals(con, starters, start)
        if not got:
            continue
        err = 0.0
        for cat, want in target.items():
            if cat in PCT:
                err += 1000 * abs(got.get(cat, 0) - want)
            else:
                err += abs(got.get(cat, 0) - want)
        if best is None or err < best[0]:
            best = (err, start, got)
    if best is None:
        return {"found": False, "reason": "no window produced any totals"}
    err, start, got = best
    # Per-category tolerance, not a summed one: counting stats are integers so
    # anything under half a unit is exact, while a percentage carries float
    # noise that the x1000 weighting above turns into a few parts per million.
    exact = all(
        abs(got.get(cat, 0) - want) < (1e-6 if cat in PCT else 0.5)
        for cat, want in target.items())
    return {"found": exact, "start": start, "days": 7,
            "abs_error": err, "reproduced": got, "reported": target,
            "n_starters": len(starters)}


# ---------------------------------------------------------------------------
# Projected contribution
# ---------------------------------------------------------------------------
def projections(con) -> pd.DataFrame:
    """Phase 6 per-game projections, wide by player."""
    try:
        rows = con.execute("""
            SELECT player_id, stat, projected_value
              FROM player_projections
             WHERE season = (SELECT max(season) FROM player_projections)
        """).df()
    except Exception:
        return pd.DataFrame()
    if rows.empty:
        return pd.DataFrame()
    return rows.pivot_table(index="player_id", columns="stat",
                            values="projected_value", aggfunc="first")


def projected_week(proj: pd.DataFrame, bdl_ids: list[int],
                   games: dict[int, int]) -> dict:
    """Expected category totals for a set of players over a week.

    Per-game projected rate x games actually scheduled. The projection supplies
    the rate and the schedule supplies the count; conflating the two (using the
    projected season games figure for a single week) is the obvious wrong turn
    here and would make every player look identical regardless of fixture load.
    """
    out = {k: 0.0 for k in [*COUNTING, *INVERTED]}
    made_att = {m: 0.0 for pair in PCT.values() for m in pair}
    for pid in bdl_ids:
        if pid is None or pid not in proj.index:
            continue
        n = games.get(int(pid), 0)
        if not n:
            continue
        row = proj.loc[pid]
        for cat, col in {**COUNTING, **INVERTED}.items():
            out[cat] += float(row.get(col, 0) or 0) * n
        for made, att in PCT.values():
            made_att[made] += float(row.get(made, 0) or 0) * n
            made_att[att] += float(row.get(att, 0) or 0) * n
    for cat, (made, att) in PCT.items():
        out[cat] = made_att[made] / made_att[att] if made_att[att] else 0.0
        out[f"_{made}"], out[f"_{att}"] = made_att[made], made_att[att]
    return out


# ---------------------------------------------------------------------------
# Category state
# ---------------------------------------------------------------------------
def category_state(mine: dict, theirs: dict, mine_raw: dict | None = None) -> list[dict]:
    """Per-category standing, margin, and how close it is.

    "Close" is expressed relative to the two sides' own scale rather than as a
    fixed number, because a 3-block gap and a 3-point gap are not comparable
    quantities and treating them alike is how a strategy tool starts giving
    confident nonsense.
    """
    out = []
    for cat in ALL_CATS:
        a, b = mine.get(cat), theirs.get(cat)
        if a is None or b is None:
            continue
        lower_wins = cat in INVERTED
        winning = (a < b) if lower_wins else (a > b)
        tied = abs(a - b) < (1e-9 if cat in PCT else 0.5)
        scale = max(abs(a), abs(b), 1e-9)
        rel = abs(a - b) / scale
        out.append({
            "category": cat, "mine": a, "theirs": b,
            "margin": (b - a) if lower_wins else (a - b),
            "status": "tied" if tied else ("winning" if winning else "losing"),
            "relative_gap": rel,
            # Two bands only. A finer grading would imply a precision this has
            # no basis for.
            "closeness": "close" if rel < 0.08 else "clear",
            "lower_wins": lower_wins,
        })
        if cat in PCT:
            made, att = PCT[cat]
            src = mine_raw or {}
            out[-1]["_made"] = float(src.get(f"_{made}", 0) or 0)
            out[-1]["_att"] = float(src.get(f"_{att}", 0) or 0)
    return out


def summarise(state: list[dict]) -> dict:
    w = sum(1 for c in state if c["status"] == "winning")
    l = sum(1 for c in state if c["status"] == "losing")
    t = sum(1 for c in state if c["status"] == "tied")
    flippable = [c for c in state
                 if c["status"] == "losing" and c["closeness"] == "close"]
    at_risk = [c for c in state
               if c["status"] == "winning" and c["closeness"] == "close"]
    return {"winning": w, "losing": l, "tied": t,
            "flippable": [c["category"] for c in flippable],
            "at_risk": [c["category"] for c in at_risk]}


# ---------------------------------------------------------------------------
# What would flip a category
# ---------------------------------------------------------------------------
def flip_moves(con, league: dict, proj: pd.DataFrame, state: list[dict],
               start: str, days: int = 7) -> list[dict]:
    """For each losing category, the swap that most closes the gap.

    Only start/sit swaps are considered -- a bench player replacing a starter
    for the remainder of the window. Adds and drops are the waiver tool's job,
    and a tool that proposes both at once invites a roster move for a category
    that a lineup change already wins.

    The gap is measured against the OPPONENT'S CURRENT total, held fixed. That
    is wrong in the sense that the opponent also keeps playing; it is stated
    rather than silently corrected, because projecting the opponent's remaining
    games needs their roster's schedule and the cache does not carry it.
    """
    bench = [p for p in league["roster"]
             if not p["starting"] and p["bdl_id"] and p["lineup_slot"] != "IR"]
    starters = [p for p in league["roster"] if p["starting"] and p["bdl_id"]]
    if not bench or not starters:
        return []
    games = games_in_window(con, [p["bdl_id"] for p in league["roster"] if p["bdl_id"]],
                            start, days)
    per_player = {}
    for p in league["roster"]:
        pid = p["bdl_id"]
        if pid is None:
            continue
        per_player[pid] = projected_week(proj, [pid], games)

    out = []
    for cat in [c for c in state if c["status"] == "losing"]:
        key = cat["category"]
        need = abs(cat["margin"])
        best = None
        for b in bench:
            for s in starters:
                bp, sp = per_player.get(b["bdl_id"], {}), per_player.get(s["bdl_id"], {})
                if not bp or not sp:
                    continue
                if key in PCT:
                    # In percentage POINTS, the same unit as the gap. The
                    # marginal-impact form (extra makes above the current rate)
                    # is a different quantity, and comparing it to a gap
                    # measured in points made "closes it" mean nothing.
                    made, att = PCT[key]
                    cur_m = cat["_made"]; cur_a = cat["_att"]
                    new_m = cur_m + bp.get(f"_{made}", 0) - sp.get(f"_{made}", 0)
                    new_a = cur_a + bp.get(f"_{att}", 0) - sp.get(f"_{att}", 0)
                    gain = (new_m / new_a - cat["mine"]) if new_a > 0 else 0.0
                elif key in INVERTED:
                    gain = sp.get(key, 0) - bp.get(key, 0)
                else:
                    gain = bp.get(key, 0) - sp.get(key, 0)
                if gain <= 0:
                    continue
                # What the same swap costs elsewhere: a move that wins one
                # category and loses two is not an improvement, and showing it
                # without its cost would be the tool's worst failure mode.
                collateral = []
                for other in state:
                    ok = other["category"]
                    if ok == key:
                        continue
                    if ok in PCT:
                        m2, a2 = PCT[ok]
                        nm = other["_made"] + bp.get(f"_{m2}", 0) - sp.get(f"_{m2}", 0)
                        na = other["_att"] + bp.get(f"_{a2}", 0) - sp.get(f"_{a2}", 0)
                        d = (nm / na - other["mine"]) if na > 0 else 0.0
                    elif ok in INVERTED:
                        d = sp.get(ok, 0) - bp.get(ok, 0)
                    else:
                        d = bp.get(ok, 0) - sp.get(ok, 0)
                    if other["status"] == "winning" and d < 0 and abs(d) >= other["margin"]:
                        collateral.append(ok)
                cand = {"category": key, "need": need, "gain": gain,
                        "closes_gap": gain >= need,
                        "bench_in": b["name"], "starter_out": s["name"],
                        "bench_games": games.get(b["bdl_id"], 0),
                        "starter_games": games.get(s["bdl_id"], 0),
                        "would_lose": collateral}
                if best is None or (len(cand["would_lose"]), -cand["gain"]) < \
                                   (len(best["would_lose"]), -best["gain"]):
                    best = cand
        if best:
            out.append(best)
        else:
            out.append({"category": key, "need": need, "gain": 0.0,
                        "closes_gap": False, "bench_in": None,
                        "starter_out": None, "would_lose": [],
                        "note": "no bench swap improves this category"})
    return out


# ---------------------------------------------------------------------------
# Lineup optimiser
# ---------------------------------------------------------------------------
SLOT_ELIGIBILITY = {
    "PG": {"PG"}, "SG": {"SG"}, "SF": {"SF"}, "PF": {"PF"}, "C": {"C"},
    "G": {"PG", "SG"}, "F": {"SF", "PF"},
    "UT": {"PG", "SG", "SF", "PF", "C"},
}


def eligible_positions(p: dict) -> set[str]:
    """ESPN gives eligible_slots on free agents but only a primary position on
    roster entries, so the roster's position string is split on the separators
    ESPN uses. A player who is genuinely multi-position but listed with one
    will simply be considered for fewer slots -- conservative, not wrong."""
    slots = p.get("eligible_slots")
    if slots:
        return {s for s in slots if s in {"PG", "SG", "SF", "PF", "C"}}
    raw = (p.get("position") or "").replace("/", " ").replace("-", " ").split()
    return {s for s in raw if s in {"PG", "SG", "SF", "PF", "C"}} or {"UT"}


def _expected_cats_won(totals: dict, opponent: dict) -> float:
    """How many categories a projected total set would win.

    Ties count as half, and a category the opponent has not scored is skipped
    rather than assumed won.
    """
    s = 0.0
    for cat, theirs in opponent.items():
        if cat not in totals:
            continue
        mine = totals[cat]
        if cat in INVERTED:
            s += 1.0 if mine < theirs else (0.5 if mine == theirs else 0.0)
        else:
            s += 1.0 if mine > theirs else (0.5 if mine == theirs else 0.0)
    return s


def optimise_lineup_categories(con, league: dict, proj: pd.DataFrame,
                               opponent: dict, start: str, days: int = 7,
                               rounds: int = 60) -> dict:
    """Choose the lineup that wins the most CATEGORIES, not the most production.

    Built because the production objective failed its own sanity check: on the
    real week of 2026-03-23 it matched the lineup actually set at 5 of 9 and
    flipped nothing, gaining rebounds and assists in categories already won
    while losing threes in one that was close. A category league does not pay
    for surplus in a category you already have.

    Local search: start from the production-optimal lineup, then repeatedly try
    every legal single swap and keep the one that most improves expected
    categories won, stopping when nothing does. Small enough to be exhaustive
    per step, and the objective is stated so a disagreement with it is a
    disagreement about strategy rather than about arithmetic.

    MEASURED, AND IT DID NOT HELP. Across 25 weeks of season 2025, scored on
    real box scores with projections that could not see 2025: the production
    objective took 5.58 of 9 categories against the static lineup, winning 17
    of 25 weeks; this category objective managed 5.28 and 16. So it is kept,
    because the reasoning behind it is sound and a projection good enough to
    exploit it would change the answer, but it is NOT the default and must not
    be presented as an upgrade.
    """
    base = optimise_lineup(con, league, proj, start, days)
    avail = {p["bdl_id"]: p for p in league["roster"]
             if p["bdl_id"] and p["lineup_slot"] != "IR"
             and (p.get("injury_status") or "ACTIVE") != "OUT"}
    games = games_in_window(con, list(avail), start, days)

    def totals_for(ids):
        return projected_week(proj, list(ids), games)

    slots = [a["slot"] for a in base["slots"]]
    chosen = [a.get("bdl_id") for a in base["slots"]]
    best = _expected_cats_won(totals_for([c for c in chosen if c]), opponent)
    history = [{"round": 0, "expected_cats": best, "swap": None}]

    for rnd in range(1, rounds + 1):
        cand_best, cand = best, None
        bench_ids = [i for i in avail if i not in chosen]
        for pos, slot in enumerate(slots):
            allow = SLOT_ELIGIBILITY.get(slot, set())
            for bid in bench_ids:
                if not (eligible_positions(avail[bid]) & allow):
                    continue
                trial = list(chosen)
                trial[pos] = bid
                score = _expected_cats_won(
                    totals_for([c for c in trial if c]), opponent)
                if score > cand_best + 1e-12:
                    cand_best, cand = score, (pos, bid)
        if cand is None:
            break
        pos, bid = cand
        out_id = chosen[pos]
        chosen[pos] = bid
        best = cand_best
        history.append({
            "round": rnd, "expected_cats": best,
            "swap": {"slot": slots[pos],
                     "in": avail[bid]["name"],
                     "out": avail[out_id]["name"] if out_id else None}})

    assignment = []
    for slot, bid in zip(slots, chosen):
        p = avail.get(bid)
        assignment.append({
            "slot": slot, "player": p["name"] if p else None, "bdl_id": bid,
            "position": p.get("position") if p else None,
            "games": games.get(bid, 0) if bid else 0,
            "was_starting": p["starting"] if p else False})
    changes = [a for a in assignment if a.get("player") and not a["was_starting"]]
    return {"start": start, "days": days, "slots": assignment,
            "changes": changes, "n_changes": len(changes),
            "expected_cats_won": best,
            "expected_cats_won_start": history[0]["expected_cats"],
            "search": history,
            "bench": [{"player": avail[i]["name"], "games": games.get(i, 0)}
                      for i in avail if i not in chosen],
            "objective": "expected categories won against the opponent's "
                         "current totals (ties count half)"}


def optimise_lineup(con, league: dict, proj: pd.DataFrame, start: str,
                    days: int = 7) -> dict:
    """Fill the starting slots to maximise total projected category production.

    Objective: the sum of each player's projected z-like contribution over the
    window, where a counting category is scaled by the roster's own spread so
    that points do not swamp blocks purely because points are bigger numbers.
    Turnovers count negatively. This is a greedy assignment over a small slot
    set (10 slots, 15 players) solved exactly by trying every eligible player
    per slot in descending value -- the search is small enough not to need
    anything cleverer, and an exact answer to a stated objective is easier to
    argue with than a heuristic.

    The objective is a choice, not a fact: it maximises expected production,
    which is not the same as maximising the probability of winning the most
    categories. A category you are already winning by a mile absorbs value
    that would be better spent on a close one. The matchup view's flip
    analysis is where that trade-off is visible.
    """
    avail = [p for p in league["roster"]
             if p["bdl_id"] and p["lineup_slot"] != "IR"
             and (p.get("injury_status") or "ACTIVE") != "OUT"]
    games = games_in_window(con, [p["bdl_id"] for p in avail], start, days)
    per = {p["bdl_id"]: projected_week(proj, [p["bdl_id"]], games) for p in avail}

    # The two percentage categories cannot enter this sum as percentages: a
    # ratio is not additive, and a 100%-from-one-attempt shooter would outrank
    # a high-volume starter. They enter as volume-weighted IMPACT,
    # (player rate - roster rate) x attempts, which is additive and is the same
    # transformation the draft board uses -- so a player's shooting is worth
    # the same thing in both places.
    #
    # Found by a percentage-unit audit: the first version of this objective
    # summed COUNTING and INVERTED only, leaving two of nine categories at
    # zero weight while the page claimed to maximise category production.
    #
    # Including them is kept because it is what the objective says it does, not
    # because it measurably wins more: over 25 weeks of season 2025 the two
    # versions choose an IDENTICAL lineup in 20 weeks, and the seven-category
    # version is nominally ahead (5.56 of 9 against 5.44) on the strength of
    # four differing weeks. That is not evidence of anything in either
    # direction, and the honest tie-breaker is the principled form.
    lg = {}
    for cat, (made, att) in PCT.items():
        tm = sum(per[p["bdl_id"]].get(f"_{made}", 0) for p in avail)
        ta = sum(per[p["bdl_id"]].get(f"_{att}", 0) for p in avail)
        lg[cat] = (tm / ta) if ta > 0 else 0.0

    def impact(v, cat):
        made, att = PCT[cat]
        a = v.get(f"_{att}", 0)
        if a <= 0:
            return 0.0
        return (v.get(f"_{made}", 0) / a - lg[cat]) * a

    scales = {}
    for cat in [*COUNTING, *INVERTED]:
        vals = [per[p["bdl_id"]].get(cat, 0) for p in avail]
        sd = pd.Series(vals).std(ddof=0)
        scales[cat] = sd if sd and sd > 1e-9 else 1.0
    for cat in PCT:
        vals = [impact(per[p["bdl_id"]], cat) for p in avail]
        sd = pd.Series(vals).std(ddof=0)
        scales[cat] = sd if sd and sd > 1e-9 else 1.0

    def value(p):
        v = per.get(p["bdl_id"], {})
        s = sum(v.get(c, 0) / scales[c] for c in COUNTING)
        s -= sum(v.get(c, 0) / scales[c] for c in INVERTED)
        s += sum(impact(v, c) / scales[c] for c in PCT)
        return s

    slots = []
    for slot, n in (league["rules"].get("starting_slots") or {}).items():
        slots.extend([slot] * int(n))
    # Fill the most constrained slots first, otherwise a UT slot takes the
    # player a PG slot needed.
    slots.sort(key=lambda s: len(SLOT_ELIGIBILITY.get(s, {"PG"})))

    ranked = sorted(avail, key=value, reverse=True)
    used, assignment = set(), []
    for slot in slots:
        allow = SLOT_ELIGIBILITY.get(slot, set())
        pick = next((p for p in ranked
                     if p["bdl_id"] not in used
                     and (eligible_positions(p) & allow)), None)
        if pick is None:
            assignment.append({"slot": slot, "player": None})
            continue
        used.add(pick["bdl_id"])
        assignment.append({
            "slot": slot, "player": pick["name"], "bdl_id": pick["bdl_id"],
            "position": pick.get("position"),
            "games": games.get(pick["bdl_id"], 0),
            "value": round(value(pick), 3),
            "was_starting": pick["starting"]})
    benched = [{"player": p["name"], "games": games.get(p["bdl_id"], 0),
                "value": round(value(p), 3), "was_starting": p["starting"]}
               for p in ranked if p["bdl_id"] not in used]
    changes = [a for a in assignment if a.get("player") and not a.get("was_starting")]
    return {"start": start, "days": days, "slots": assignment,
            "bench": benched, "changes": changes,
            "n_changes": len(changes),
            "objective": "sum of spread-scaled projected production across all "
                         "nine categories: counting stats direct, turnovers "
                         "negative, FG%/FT% as volume-weighted impact"}


# ---------------------------------------------------------------------------
# Waiver radar
# ---------------------------------------------------------------------------
def waiver_radar(con, league: dict, proj: pd.DataFrame,
                 start: str | None = None, days: int = 7) -> dict:
    """Free agents whose PROJECTED season value beats a rostered bench player.

    Deliberately not a "trending" list. Recent-average risers are mostly noise
    plus a short hot streak; the comparison that matters is projected value
    against what is already held, which is the same quantity the draft board
    ranks on and the same model Phase 6 validated. A player is only surfaced
    if he clears someone you would actually have to drop.
    """
    if proj.empty or "value_9cat" not in proj.columns:
        return {"available": False,
                "reason": "player_projections has no value_9cat; run "
                          "`python projections.py --write`"}
    val = proj["value_9cat"]
    bench = [p for p in league["roster"]
             if not p["starting"] and p["bdl_id"] and p["lineup_slot"] != "IR"]
    droppable = [{"name": p["name"], "bdl_id": p["bdl_id"],
                  "value": float(val.get(p["bdl_id"], float("nan")))}
                 for p in bench]
    droppable = [d for d in droppable if d["value"] == d["value"]]
    if not droppable:
        return {"available": False,
                "reason": "no bench player has a projection to compare against"}
    worst = min(droppable, key=lambda d: d["value"])

    games = {}
    if start:
        games = games_in_window(
            con, [p["bdl_id"] for p in league["free_agents"] if p["bdl_id"]],
            start, days)

    rows = []
    for fa in league["free_agents"]:
        pid = fa["bdl_id"]
        if pid is None or pid not in val.index:
            continue
        v = float(val.loc[pid])
        if v <= worst["value"]:
            continue
        rows.append({"name": fa["name"], "pro_team": fa.get("pro_team"),
                     "position": fa.get("position"),
                     "injury_status": fa.get("injury_status"),
                     "projected_value": round(v, 3),
                     "beats": worst["name"],
                     "beats_by": round(v - worst["value"], 3),
                     "games_this_week": games.get(int(pid)) if games else None})
    rows.sort(key=lambda r: -r["projected_value"])
    return {"available": True, "candidates": rows,
            "weakest_bench": {"name": worst["name"],
                              "projected_value": round(worst["value"], 3)},
            "bench_considered": sorted(droppable, key=lambda d: d["value"]),
            "free_agents_seen": len(league["free_agents"]),
            "free_agents_with_projection":
                sum(1 for f in league["free_agents"]
                    if f["bdl_id"] is not None and f["bdl_id"] in val.index),
            "basis": "Phase 6 projected 9-cat season value, not recent form"}


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------
def build(con, season: int | None = None, week_start: str | None = None) -> dict:
    league = load_league(latest_cache(season))
    proj = projections(con)
    week = find_week(con, league)
    start = week_start or (week.get("start") if week.get("found") else None)

    mine_reported = {k: v["value"]
                     for k, v in (league["matchup"].get("my_categories") or {}).items()}
    theirs_reported = {k: v["value"]
                       for k, v in (league["matchup"].get("opponent_categories") or {}).items()}
    # ESPN reports a percentage but not the makes and attempts behind it, and
    # a swap cannot be re-expressed as a new percentage without them. The
    # recovered week supplies them -- and only because it reconciled exactly,
    # which is checked rather than assumed.
    raw = week.get("reproduced") if week.get("found") else None
    state = category_state(mine_reported, theirs_reported, raw)

    out = {
        "cache_file": league["cache_file"],
        "season_label": league["season_label"],
        "season_state": league["season_state"],
        "opponent": league["matchup"].get("opponent_name"),
        "scoring_period": league["matchup"].get("scoring_period"),
        "category_wins": {"me": league["matchup"].get("category_wins_me"),
                          "opponent": league["matchup"].get("category_wins_opponent")},
        "week": week,
        "state": state,
        "summary": summarise(state) if state else None,
        "unmapped_players": league["unmapped"],
        "roster": [{"name": p["name"], "slot": p["lineup_slot"],
                    "position": p.get("position"),
                    "injury_status": p.get("injury_status"),
                    "starting": p["starting"], "mapped": p["bdl_id"] is not None}
                   for p in league["roster"]],
        "advisory": (
            "Advisory only. The draft board's weights and the Phase 6 "
            "projections were validated walk-forward against a stated "
            "benchmark; none of the advice on this page has been. Opponent "
            "totals are held fixed at their current value because the cache "
            "does not carry the opponent's schedule, so a 'gap' understates "
            "what is actually needed."),
        "has_projections": not proj.empty,
    }
    if start and not proj.empty:
        out["flips"] = flip_moves(con, league, proj, state, start)
        out["lineup"] = optimise_lineup(con, league, proj, start)
    out["waivers"] = waiver_radar(con, league, proj, start)
    out["roster_strength"] = roster_strength(con, league, proj)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--optimise", action="store_true")
    ap.add_argument("--waivers", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--season", type=int, default=None)
    ap.add_argument("--week-start", default=None)
    args = ap.parse_args()
    if SNAPSHOT.resolve() == LIVE_DB.resolve():
        raise SystemExit("refusing to read the live database")
    con = duckdb.connect(str(SNAPSHOT), read_only=True)
    try:
        d = build(con, args.season, args.week_start)
    finally:
        con.close()

    print("=" * 84)
    print(f"MATCHUP — {d['season_label']} · {d['season_state']} · "
          f"vs {d['opponent']}")
    print("=" * 84)
    w = d["week"]
    if w.get("found"):
        print(f"week      {w['start']} .. +{w['days']}d  "
              f"(recovered by reconciling box scores, exact match)")
    else:
        print(f"week      NOT identified ({w.get('reason', 'no exact match')})")
    print(f"roster    {sum(1 for p in d['roster'] if p['mapped'])}/"
          f"{len(d['roster'])} players mapped to the snapshot")
    print(f"\n{d['advisory']}\n")

    if args.verify or not any((args.status, args.optimise, args.waivers)):
        print("-" * 84)
        print("RECONCILIATION — reported vs recomputed from box scores")
        print("-" * 84)
        rep, got = w.get("reported", {}), w.get("reproduced", {})
        for cat in ALL_CATS:
            if cat not in rep:
                continue
            a, b = rep[cat], got.get(cat, 0)
            mark = "ok" if abs(a - b) < (1e-6 if cat in PCT else 0.5) else "MISMATCH"
            fmt = "{:.8f}" if cat in PCT else "{:.0f}"
            print(f"  {cat:5s} reported {fmt.format(a):>12}   "
                  f"recomputed {fmt.format(b):>12}   {mark}")
        print(f"\n  total absolute error {w.get('abs_error', float('nan')):.8f}")

    if args.status or not any((args.optimise, args.waivers, args.verify)):
        print("\n" + "-" * 84)
        print("CATEGORIES")
        print("-" * 84)
        for c in d["state"]:
            arrow = {"winning": "WIN ", "losing": "LOSS", "tied": "TIE "}[c["status"]]
            fmt = "{:.4f}" if c["category"] in PCT else "{:.0f}"
            print(f"  {arrow} {c['category']:5s} "
                  f"{fmt.format(c['mine']):>10} v {fmt.format(c['theirs']):>10}  "
                  f"{c['closeness']}")
        s = d["summary"]
        print(f"\n  {s['winning']}-{s['losing']}-{s['tied']}   "
              f"close and losing: {', '.join(s['flippable']) or 'none'}   "
              f"close and winning: {', '.join(s['at_risk']) or 'none'}")
        for f in d.get("flips", []):
            if f.get("bench_in"):
                cost = (f"  (would give up {', '.join(f['would_lose'])})"
                        if f["would_lose"] else "")
                # A percentage gap of 0.0008 printed to one decimal is "0.0",
                # which reads as "no gap" when it is the whole question.
                if f["category"] in PCT:
                    g = f"+{f['gain'] * 100:.2f}pp, need {f['need'] * 100:.2f}pp"
                else:
                    g = f"+{f['gain']:.1f}, need {f['need']:.1f}"
                print(f"\n  {f['category']}: start {f['bench_in']} "
                      f"({f['bench_games']} games) over {f['starter_out']} "
                      f"({f['starter_games']}) -> {g}"
                      f"{' — CLOSES IT' if f['closes_gap'] else ''}{cost}")
            else:
                print(f"\n  {f['category']}: {f.get('note')}")

    if args.optimise:
        lu = d.get("lineup")
        print("\n" + "-" * 84)
        print("LINEUP")
        print("-" * 84)
        if not lu:
            print("  unavailable (no week identified or no projections)")
        else:
            for a in lu["slots"]:
                if a.get("player"):
                    flag = "" if a.get("was_starting") else "  <- CHANGE"
                    print(f"  {a['slot']:3s} {a['player']:<26} "
                          f"{a['games']}g  v={a['value']:+.2f}{flag}")
                else:
                    print(f"  {a['slot']:3s} (nobody eligible)")
            print(f"\n  bench: " + ", ".join(
                f"{b['player']} ({b['games']}g)" for b in lu["bench"]))
            print(f"  {lu['n_changes']} change(s) from the lineup as set")
            print(f"  objective: {lu['objective']}")

    if args.waivers:
        wv = d["waivers"]
        print("\n" + "-" * 84)
        print("WAIVER RADAR")
        print("-" * 84)
        if not wv.get("available"):
            print("  " + wv.get("reason", "unavailable"))
        else:
            print(f"  basis: {wv['basis']}")
            print(f"  weakest rostered bench: {wv['weakest_bench']['name']} "
                  f"({wv['weakest_bench']['projected_value']:+.2f})")
            print(f"  {wv['free_agents_with_projection']}/{wv['free_agents_seen']} "
                  f"free agents have a projection\n")
            if not wv["candidates"]:
                print("  no free agent projects above the weakest bench player")
            for c in wv["candidates"]:
                g = f"  {c['games_this_week']}g" if c["games_this_week"] else ""
                print(f"  {c['projected_value']:+6.2f}  {c['name']:<24} "
                      f"{c['pro_team'] or '':<4} {c['position'] or '':<6}"
                      f"beats {c['beats']} by {c['beats_by']:.2f}{g}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


# ---------------------------------------------------------------------------
# Season outlook
# ---------------------------------------------------------------------------
def season_outlook(league: dict, sims: int = 20000, seed: int = 0,
                   strength_from: dict | None = None) -> dict:
    """Playoff odds from the remaining schedule, or an honest statement of why not.

    WHAT THIS CANNOT DO WITH THIS CACHE. Simulating opponent-by-opponent needs
    three things the league sync does not store: the remaining matchup
    schedule, the other eleven rosters, and their lineups. Without them the
    only defensible model is at team level -- each team's demonstrated
    per-category win rate, played against a randomly drawn opponent for each
    remaining matchup. That assumption is stated in the output rather than
    buried, because a round-robin draw is not the real schedule and a team
    whose remaining fixtures are unusually hard or easy will be mis-estimated.

    WALK-FORWARD. Strength comes only from the record to date, which is
    exactly what is known at the point the projection is made. No player
    projection enters here: using Phase 6 to re-rate opponents would be using
    knowledge of players whose rosters this cache does not even contain.
    """
    import random

    lg = league["raw"].get("league") or {}
    total = int(lg.get("regular_season_matchups") or 0)
    current = int(lg.get("current_matchup_period") or 0)
    spots = int(lg.get("playoff_team_count") or 0)
    standings = league["raw"].get("standings") or []
    me = league["raw"]["my_team"]["team_id"]

    def pts(s):
        return (s.get("wins") or 0) + 0.5 * (s.get("ties") or 0)

    table = sorted(standings, key=lambda s: -pts(s))
    rank = next((i + 1 for i, s in enumerate(table) if s["team_id"] == me), None)
    remaining = max(0, total - current + 1)

    base = {"total_matchups": total, "current_matchup": current,
            "remaining_matchups": remaining, "playoff_spots": spots,
            "my_rank": rank, "teams": len(standings),
            "standings": [{"team_id": s["team_id"], "name": s["name"],
                           "wins": s.get("wins"), "losses": s.get("losses"),
                           "ties": s.get("ties"), "points": pts(s),
                           "is_me": s["team_id"] == me} for s in table]}

    if not standings or not total:
        return {**base, "playable": False,
                "reason": "the league cache carries no standings or matchup count"}
    if remaining <= 0:
        return {**base, "playable": False,
                "reason": (f"the regular season is complete — matchup period "
                           f"{current} of {total}. Final position {rank} of "
                           f"{len(standings)}, and the top {spots} make the "
                           f"playoffs, so there is nothing left to simulate."),
                "made_playoffs": (rank is not None and rank <= spots)}

    # Per-category win rate to date, shrunk toward even. Without shrinkage a
    # team that has played few matchups gets an extreme rate from a tiny
    # sample and the simulation inherits it as certainty.
    PRIOR = 30.0
    strength, basis = {}, "record to date, shrunk toward even"
    played = sum((s.get("wins") or 0) + (s.get("losses") or 0)
                 for s in standings)
    if strength_from and played < len(standings) * 9:
        # Before a season has produced much record, every team looks identical
        # and the simulation returns a coin flip that is technically correct
        # and completely uninformative. Projected roster value is the only
        # real signal available at that point. Mapped onto a win rate by
        # z-scoring across the league, so the scale of the values -- which is
        # arbitrary -- does not set how lopsided the league looks.
        vals = {t: v["total"] for t, v in strength_from.items()}
        if len(vals) >= 4:
            mu = sum(vals.values()) / len(vals)
            sd = (sum((v - mu) ** 2 for v in vals.values()) / len(vals)) ** 0.5
            if sd > 1e-9:
                for t, v in vals.items():
                    strength[t] = 0.5 + 0.08 * ((v - mu) / sd)
                basis = ("projected roster value, z-scored across the league "
                         "(too little record to use)")
    if not strength:
        for s in standings:
            w = (s.get("wins") or 0) + 0.5 * (s.get("ties") or 0)
            n = w + (s.get("losses") or 0) + 0.5 * (s.get("ties") or 0)
            strength[s["team_id"]] = (w + PRIOR / 2) / (n + PRIOR) if n else 0.5
    for s in standings:                     # anyone the basis missed
        strength.setdefault(s["team_id"], 0.5)

    rng = random.Random(seed)
    ids = [s["team_id"] for s in standings]
    cats = len(ALL_CATS)
    made = 0
    finishes = {t: 0 for t in ids}
    for _ in range(sims):
        pts_now = {s["team_id"]: pts(s) for s in standings}
        for _m in range(remaining):
            order = ids[:]
            rng.shuffle(order)
            for a, b in zip(order[0::2], order[1::2]):
                pa = strength[a] / (strength[a] + strength[b])
                wa = sum(1 for _ in range(cats) if rng.random() < pa)
                pts_now[a] += wa
                pts_now[b] += cats - wa
        final = sorted(ids, key=lambda t: -pts_now[t])
        pos = final.index(me) + 1
        finishes[me] = finishes[me]
        if pos <= spots:
            made += 1
        for i, t in enumerate(final):
            if t == me:
                finishes[t] = finishes[t] + i + 1
    return {**base, "playable": True,
            "simulations": sims,
            "playoff_odds": made / sims,
            "mean_finish": finishes[me] / sims,
            "strength_basis": basis,
            # A pre-season projection is only as real as the rosters behind
            # it, and a league that has not drafted yet has placeholder
            # rosters. Saying the odds without saying that is a fabrication
            # dressed as a forecast.
            "roster_caveat": (
                "These rosters are pre-draft placeholders — the league has "
                "not drafted, so the projected strength behind these odds is "
                "not the team anyone will actually field."
                if league.get("season_state") == "not_started"
                   and basis.startswith("projected roster")
                else None),
            "assumptions": [
                "Opponents for each remaining matchup are drawn at random: the "
                "cache does not carry the league schedule, so a team with an "
                "unusually hard or easy run-in is mis-estimated.",
                "Team strength is the per-category win rate to date, shrunk "
                "toward even with a 30-category prior.",
                "Categories within a matchup are independent draws, which "
                "understates how correlated a good week is across categories.",
            ]}


# ---------------------------------------------------------------------------
# Multi-week schedule outlook
# ---------------------------------------------------------------------------
def schedule_outlook(con, league: dict, start: str, weeks: int = 3,
                     proj: pd.DataFrame | None = None) -> dict:
    """Fixture load per roster player over the coming weeks.

    WHAT THIS IS NOT. A true opponent preview needs the league's matchup
    schedule and the other eleven rosters; the sync stores neither, so who you
    face in two weeks' time is not knowable from this cache and is not guessed
    at here. What IS knowable, and is the half that actually drives a trade or
    a waiver claim made in advance, is your own roster's fixture load: a
    four-game week and a two-game week are worth very different amounts from
    the same player, and that is visible three weeks out.

    Games are counted from the NBA schedule in the snapshot, so a week beyond
    the data's last fixture is reported as unknown rather than as zero games --
    those are different statements and conflating them would make the final
    weeks of a season look like a roster-wide injury.
    """
    ids = [p["bdl_id"] for p in league["roster"] if p["bdl_id"]]
    last = con.execute(
        "SELECT max(game_date) FROM v_games WHERE game_type='regular'").fetchone()[0]
    out_weeks = []
    for w in range(weeks):
        wk_start = (pd.Timestamp(start) + pd.Timedelta(days=7 * w)).date().isoformat()
        wk_end = (pd.Timestamp(wk_start) + pd.Timedelta(days=6)).date().isoformat()
        covered = str(last) >= wk_end
        games = games_in_window(con, ids, wk_start) if str(last) >= wk_start else {}
        rows = []
        for p in league["roster"]:
            if p["bdl_id"] is None:
                continue
            n = games.get(int(p["bdl_id"]), 0)
            row = {"name": p["name"], "slot": p["lineup_slot"],
                   "starting": p["starting"], "games": n}
            if proj is not None and not proj.empty and p["bdl_id"] in proj.index:
                # Value of the week = per-game projection x fixtures, so a
                # thin week for a good player is visible as a number.
                pv = projected_week(proj, [p["bdl_id"]], games)
                row["projected_pts"] = pv.get("PTS", 0.0)
                row["projected_value_week"] = n * float(
                    proj.loc[p["bdl_id"]].get("value_9cat", 0) or 0) / FULL_SEASON
            rows.append(row)
        starters = [r for r in rows if r["starting"]]
        out_weeks.append({
            "start": wk_start, "end": wk_end,
            "covered_by_schedule": covered,
            "total_starter_games": sum(r["games"] for r in starters),
            "thin": sorted([r for r in starters if r["games"] <= 2],
                           key=lambda r: r["games"]),
            "heavy": sorted([r for r in rows if r["games"] >= 4],
                            key=lambda r: -r["games"]),
            "players": sorted(rows, key=lambda r: (-r["games"], r["name"])),
        })
    return {"weeks": out_weeks, "schedule_last_date": str(last),
            "opponent_preview_available": False,
            "opponent_note": (
                "Who you face in later weeks is not in the league cache: it "
                "stores the current matchup only, with no schedule and no "
                "other rosters. Fixture load is shown instead, which is the "
                "part a trade or waiver claim can be planned around.")}


# ---------------------------------------------------------------------------
# Lineup retrospective
# ---------------------------------------------------------------------------
_RETRO_PROJ_CACHE: dict = {}


def honest_projections(con, season: int):
    """Projections for `season` fit only on target seasons before it.

    The shipped table projects the NEXT season and was built from features that
    include `season` itself. Using it to grade lineup decisions inside that
    season hands the optimiser foresight: it already knows who turned out to be
    good. Refitting is slow, so the result is cached per season for the life of
    the process -- correctness first, then don't pay for it twice.
    """
    if season in _RETRO_PROJ_CACHE:
        return _RETRO_PROJ_CACHE[season]
    import projections as P
    tot = P.load_totals(con)
    hist = P.component_frame(
        P.assemble(con, P.FIRST_INPUT_SEASON + 3, season - 1), tot)
    train = hist[P.eligible(hist)]
    feats = [c for c in P.feature_columns(hist) if not c.startswith("y_")]
    fut = P.build_features(tot, P.load_advanced(con), P.load_bio(con), season)
    for lab, w in (("base_recent", {season - 1: 1.0}),
                   ("base_shipped", {season - 1: .6, season - 2: .3,
                                     season - 3: .1})):
        fut[lab] = P.baseline_prediction(tot, season, w)
    fut = fut.reset_index()
    fut = fut[fut["base_shipped"].notna()]
    comp = P.fit_components(train, fut, feats)
    comp.index = fut["player_id"].to_numpy()
    _RETRO_PROJ_CACHE[season] = (comp, int(train["target_season"].nunique()),
                                 len(train))
    return _RETRO_PROJ_CACHE[season]


def retrospective(con, league: dict, proj: pd.DataFrame, season: int | None = None,
                  objective: str = "production") -> dict:
    """Week by week: the lineup that was set against the one the optimiser picks.

    ONE REAL LIMITATION, STATED UP FRONT. ESPN's cache stores the CURRENT
    lineup only -- there is no history of what was started in week three. So
    the comparison is against that one lineup held constant across every week,
    which measures "optimise weekly" against "set it once and leave it". That
    is a fair question and a common failure mode, but it is not a record of the
    decisions actually made, and part of any gain is schedule exploitation
    rather than better player evaluation.

    Scored on real box scores: each lineup's realised category totals are
    compared head to head, so the winner is what happened and not what was
    projected.
    """
    season = season or con.execute(
        "SELECT max(season) FROM v_games WHERE game_type='regular'").fetchone()[0]
    # Grade the season with a model that could not see it.
    proj, train_seasons, train_rows = honest_projections(con, int(season))
    dates = [str(r[0]) for r in con.execute(
        "SELECT DISTINCT game_date FROM v_games WHERE game_type='regular' "
        f"AND season={season} ORDER BY game_date").fetchall()]
    if not dates:
        return {"available": False, "reason": f"no {season} schedule in the snapshot"}
    mondays = sorted({(pd.Timestamp(d) - pd.Timedelta(days=pd.Timestamp(d).weekday()))
                      .date().isoformat() for d in dates})
    actual = [p["bdl_id"] for p in league["roster"] if p["starting"] and p["bdl_id"]]
    by_id = {p["bdl_id"]: p["name"] for p in league["roster"] if p["bdl_id"]}

    weeks = []
    for start in mondays:
        games = games_in_window(con, [p["bdl_id"] for p in league["roster"]
                                      if p["bdl_id"]], start)
        if not games:
            continue
        lu = (optimise_lineup(con, league, proj, start) if objective == "production"
              else optimise_lineup_categories(
                  con, league, proj,
                  projected_week(proj, actual, games), start))
        picked = [a.get("bdl_id") for a in lu["slots"] if a.get("bdl_id")]
        mine = week_totals(con, actual, start)
        theirs = week_totals(con, picked, start)
        if not mine or not theirs:
            continue
        won = 0.0
        per_cat = []
        for cat in ALL_CATS:
            a, b = mine.get(cat, 0), theirs.get(cat, 0)
            better = (b < a) if cat in INVERTED else (b > a)
            same = abs(a - b) < (1e-9 if cat in PCT else 0.5)
            won += 0.5 if same else (1.0 if better else 0.0)
            per_cat.append({"category": cat, "as_set": a, "optimised": b,
                            "optimiser_better": bool(better and not same)})
        changes = [{"in": by_id.get(a.get("bdl_id")), "slot": a["slot"]}
                   for a in lu["slots"]
                   if a.get("bdl_id") and a["bdl_id"] not in actual]
        weeks.append({
            "week": start,
            "optimiser_cats_won": won,
            "verdict": ("optimiser" if won > 4.5 else
                        "lineup as set" if won < 4.5 else "level"),
            "n_changes": len(changes), "changes": changes,
            "starter_games_as_set": sum(games.get(i, 0) for i in actual),
            "categories": per_cat,
        })
    if not weeks:
        return {"available": False, "reason": "no week produced totals for both lineups"}
    wins = sum(1 for w in weeks if w["verdict"] == "optimiser")
    losses = sum(1 for w in weeks if w["verdict"] == "lineup as set")
    mean = sum(w["optimiser_cats_won"] for w in weeks) / len(weeks)
    # Which categories the optimiser reliably improves, and which it sacrifices.
    by_cat = {}
    for w in weeks:
        for c in w["categories"]:
            by_cat.setdefault(c["category"], []).append(c["optimiser_better"])
    return {
        "available": True, "season": season, "objective": objective,
        "weeks": weeks, "n_weeks": len(weeks),
        "optimiser_won": wins, "optimiser_lost": losses,
        "level": len(weeks) - wins - losses,
        "mean_cats_won": mean,
        "by_category": [{"category": k,
                         "weeks_better": sum(v), "weeks": len(v),
                         "rate": sum(v) / len(v)}
                        for k, v in by_cat.items()],
        "projection_basis": (
            f"Refit for season {season} on {train_seasons} earlier target "
            f"seasons ({train_rows:,} player-seasons). The shipped projections "
            f"were built from features including {season} and would have given "
            "the optimiser foresight here."),
        "limitation": (
            "ESPN stores the current lineup only, so the comparison holds that "
            "one lineup constant across every week. This measures optimising "
            "weekly against setting a lineup once and leaving it — not against "
            "the decisions actually made week to week."),
    }


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------
INJURY_DIR = CACHE_DIR / "injuries.json"


def load_injuries() -> dict:
    """Live ESPN injury statuses, keyed by ESPN id, with their age.

    The age matters more than the content: a status pulled three days ago is
    not news, and presenting it as today's is how a digest starts lying
    quietly. Whatever is shown carries the timestamp it was fetched at.
    """
    if not INJURY_DIR.exists():
        return {"available": False, "reason": "no injuries.json in the cache"}
    d = json.loads(INJURY_DIR.read_text())
    return {"available": True, "fetched_at": d.get("fetched_at"),
            "by_espn_id": {int(p["espn_id"]): p for p in d.get("players", [])
                           if p.get("espn_id") is not None}}


def daily_digest(con, league: dict, proj: pd.DataFrame,
                 today: str | None = None) -> dict:
    """Today's lineup status, roster injuries, and one waiver opportunity.

    "Today" defaults to the last date the SCHEDULE covers, not the wall clock:
    this snapshot ends on a date in the past relative to when it is read, and a
    digest that reports "no games today" because the real date is outside the
    data would be technically true and completely useless.
    """
    last = str(con.execute(
        "SELECT max(game_date) FROM v_games WHERE game_type='regular'").fetchone()[0])
    day = today or last
    ids = [p["bdl_id"] for p in league["roster"] if p["bdl_id"]]
    playing = games_in_window(con, ids, day, days=1)

    inj = load_injuries()
    starters, bench, idle_starters = [], [], []
    for p in league["roster"]:
        n = playing.get(int(p["bdl_id"]), 0) if p["bdl_id"] else 0
        status = None
        if inj.get("available"):
            rec = inj["by_espn_id"].get(p["player_id"])
            status = (rec or {}).get("injury_status")
        row = {"name": p["name"], "slot": p["lineup_slot"],
               "starting": p["starting"], "plays_today": bool(n),
               "injury_status": status or p.get("injury_status")}
        (starters if p["starting"] else bench).append(row)
        if p["starting"] and not n:
            idle_starters.append(row)

    # The actionable pairing: a starter with no game today while a bench player
    # with one sits behind him.
    bench_playing = [b for b in bench
                     if b["slot"] != "IR" and b["plays_today"]
                     and (b["injury_status"] or "ACTIVE") not in ("OUT",)]
    swaps = []
    for s in idle_starters:
        for b in bench_playing:
            swaps.append({"sit": s["name"], "start": b["name"],
                          "reason": "starter has no game today; bench player does"})
            break

    flagged = [r for r in starters + bench
               if (r["injury_status"] or "ACTIVE") not in ("ACTIVE", None)]

    wv = waiver_radar(con, league, proj, day)
    top = (wv.get("candidates") or [None])[0] if wv.get("available") else None

    return {
        "day": day, "is_schedule_end": day == last,
        "schedule_last_date": last,
        "starters_playing": sum(1 for s in starters if s["plays_today"]),
        "starters_total": len(starters),
        "idle_starters": idle_starters,
        "bench_playing": bench_playing,
        "suggested_swaps": swaps,
        "injuries": {"fetched_at": inj.get("fetched_at"),
                     "available": inj.get("available", False),
                     "flagged": flagged},
        "waiver_highlight": top,
        "note": ("Lineup slots are not changed for you — this reports what is "
                 "actionable today and leaves the decision where it belongs."),
    }


def roster_strength(con, league: dict, proj: pd.DataFrame) -> dict:
    """Each team's summed projected 9-cat value, from its actual roster.

    Needs all twelve rosters, which league_sync only began caching once the
    trade finder required them. Before that the only available signal was the
    record to date -- useless before a season starts, which is exactly when a
    pre-season projection is wanted.

    Starters only: a 16-man roster with four bench players is not worth the
    sum of sixteen, and the slot rules say how many actually score.
    """
    teams = league["raw"].get("teams") or []
    if not teams or proj.empty or "value_9cat" not in proj.columns:
        return {}
    idmap = pd.read_csv(ID_MAP)
    to_bdl = {int(r.espn_id): int(r.bdl_player_id) for r in idmap.itertuples()
              if pd.notna(r.bdl_player_id)}
    starters = int((league["rules"].get("starters_total") or 10))
    val = proj["value_9cat"]
    out = {}
    for t in teams:
        vals = []
        for p in t.get("roster", []):
            bid = to_bdl.get(p.get("player_id"))
            if bid is not None and bid in val.index:
                vals.append(float(val.loc[bid]))
        if not vals:
            continue
        vals.sort(reverse=True)
        out[t["team_id"]] = {
            "total": sum(vals[:starters]),
            "matched": len(vals), "roster": len(t.get("roster", [])),
        }
    return out


# ---------------------------------------------------------------------------
# Season recap: what the lineup cost, and what the wire offered
# ---------------------------------------------------------------------------
def missed_waivers(con, league: dict, season: int | None = None,
                   top: int = 12) -> dict:
    """Players nobody rostered who outscored the bottom of my roster.

    AVAILABILITY IS APPROXIMATED, and the approximation is the weak point.
    ESPN's cache holds end-of-season rosters, not a history of who was free
    in week nine, so "available" here means "on no roster when the season
    ended". A player dropped in March shows as available all year and a
    waiver pickup shows as never available. It is directionally useful and it
    is not a transaction log; said plainly rather than presented as one.
    """
    season = season or con.execute(
        "SELECT max(season) FROM v_games WHERE game_type='regular'").fetchone()[0]
    teams = league["raw"].get("teams") or []
    if not teams:
        return {"available": False,
                "reason": "this league cache predates all-roster support"}
    idmap = pd.read_csv(ID_MAP)
    to_bdl = {int(r.espn_id): int(r.bdl_player_id) for r in idmap.itertuples()
              if pd.notna(r.bdl_player_id)}
    rostered, mine = set(), set()
    for t in teams:
        for p in t.get("roster", []):
            bid = to_bdl.get(p.get("player_id"))
            if bid is None:
                continue
            rostered.add(bid)
            if t.get("is_me"):
                mine.add(bid)
    if not mine:
        return {"available": False, "reason": "no team is marked as yours"}

    # Realised 9-cat value for the season, scored exactly as the board does.
    import backtest_weights as bw
    tot = con.execute(f"""
        SELECT player_id, any_value(player_name) AS player_name,
               count(*) AS gp, sum(pts) pts, sum(reb) reb, sum(ast) ast,
               sum(stl) stl, sum(blk) blk, sum(fg3m) fg3m,
               sum(turnover) turnover, sum(fgm) fgm, sum(fga) fga,
               sum(ftm) ftm, sum(fta) fta
          FROM v_player_games
         WHERE game_type='regular' AND NOT is_dnp AND season = {int(season)}
         GROUP BY player_id""").df()
    if tot.empty:
        return {"available": False, "reason": f"no {season} box scores"}
    tot = tot.set_index("player_id")
    rates = tot[bw.COUNTING + ["turnover", "fgm", "fga", "ftm", "fta"]].div(
        tot["gp"], axis=0)
    pool = rates.loc[tot["gp"].sort_values(ascending=False)
                     .head(bw.POOL_SIZE).index]
    value = (bw.nine_cat_value(rates, pool)
             * (tot["gp"] / bw.FULL_SEASON).clip(upper=1.0))

    held = value.reindex([p for p in mine if p in value.index]).dropna()
    if held.empty:
        return {"available": False, "reason": "none of my roster has a value"}
    bar = float(held.min())
    worst = tot.loc[held.idxmin(), "player_name"]

    free = value.drop(index=[p for p in rostered if p in value.index],
                      errors="ignore")
    free = free[free > bar].sort_values(ascending=False).head(top)
    rows = [{"player_id": int(pid), "name": tot.loc[pid, "player_name"],
             "games": int(tot.loc[pid, "gp"]), "value": round(float(v), 3),
             "beats_by": round(float(v) - bar, 3)}
            for pid, v in free.items()]
    return {"available": True, "season": int(season),
            "bar": round(bar, 3), "weakest_held": worst,
            "candidates": rows,
            "rostered_players": len(rostered),
            "caveat": ("'Unrostered' means on no roster at season end. This "
                       "cache has no transaction history, so a player dropped "
                       "late looks free all year and a mid-season pickup looks "
                       "never-available.")}
