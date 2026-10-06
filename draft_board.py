#!/usr/bin/env python3
"""9-cat draft board for "The Bubble 2.0" (Phase 9, design spec section 10).

READ-ONLY. Reads nba_snapshot.duckdb (never nba.duckdb, which ingestion owns) and
the ESPN player universe. Creates no tables, alters no views, touches no tests.

This is a TRANSPARENT BASELINE, deliberately not the Phase 6 projection model:
recency-weighted historical per-game rates turned into category z-scores against
the draftable pool. Every number here is reproducible with arithmetic you can
check by hand, which is the point -- it is a starting board to argue with, not a
forecast.

    python draft_board.py                  # build, verify, write CSV, summarise
    python draft_board.py --top 40
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
LIVE_DB = PROJECT_ROOT / "nba.duckdb"
ESPN_PLAYERS = PROJECT_ROOT / "espn_cache" / "espn_players.json"
ID_MAP = PROJECT_ROOT / "espn_bdl_id_map.csv"
KEEPERS = PROJECT_ROOT / "keepers.csv"
INJURY_CACHE = PROJECT_ROOT / "espn_cache" / "injuries.json"
LEAGUE_ID, ESPN_SEASON = 1127755466, 2027
# Statuses that take a player out of "best available" by default. A DAY_TO_DAY
# player is draftable at a discount; an OUT player is a different decision.
EXCLUDE_STATUSES = {"OUT", "SUSPENSION"}
OUT_CSV = PROJECT_ROOT / "draft_board.csv"

# ---------------------------------------------------------------------------
# Methodology constants -- all stated in the printed output as well
# ---------------------------------------------------------------------------
SEASONS = (2023, 2024, 2025)          # the three completed seasons in the snapshot
SEASON_WEIGHTS = {2025: 0.60, 2024: 0.30, 2023: 0.10}

# Alternate windows are the SAME weighting math over fewer seasons, with the
# shipped weights renormalised to sum to one -- not a different model and not a
# forecast. A shorter window trades sample size for recency; that is the whole
# of the difference, and it is the user's call which they want to look at.
WINDOWS = {
    "blend": (2023, 2024, 2025),
    "last2": (2024, 2025),
    "last1": (2025,),
}

# "projected" is the one window that is NOT a reweighting of completed seasons.
# It reads player_projections -- the walk-forward-validated model from
# projections.py -- and feeds those projected per-game rates through this
# script's unchanged scoring: the same pool, the same volume-weighted
# percentage impact, the same availability discount. Only the rates differ,
# which is what makes it comparable to the other three rather than a separate
# board that happens to sit beside them.
PROJECTED_WINDOW = "projected"


def apply_window(name: str) -> None:
    """Restrict SEASONS/SEASON_WEIGHTS in place, renormalising the weights."""
    global SEASONS, SEASON_WEIGHTS
    if name == PROJECTED_WINDOW:
        return                      # rates come from the projections table
    seasons = WINDOWS[name]
    total = sum(SEASON_WEIGHTS[s] for s in seasons)
    SEASON_WEIGHTS = {s: SEASON_WEIGHTS[s] / total for s in seasons}
    SEASONS = seasons
POOL_SIZE = 168                        # ESPN's own draftDetail.picks: 12 teams x 14
MIN_WEIGHTED_GP = 20                   # below this, the baseline is too thin to trust
FULL_SEASON = 82

COUNTING = ["pts", "reb", "ast", "stl", "blk", "fg3m"]
INVERTED = ["turnover"]
PCT = ["fg", "ft"]                     # handled as volume-weighted impact
CATS = COUNTING + PCT + INVERTED
LEAGUE_SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F"]
BASE_POS = ["PG", "SG", "SF", "PF", "C"]


def fetch_injuries() -> tuple[pd.DataFrame, str, str]:
    """Re-pull injury status from ESPN at run time.

    Injury status is the fastest-moving input on the board and the one most
    likely to be stale in a cached file, so it is fetched fresh on every run.
    On failure the last cached pull is used and its age is reported loudly --
    silently serving week-old injury data is worse than saying the fetch failed.
    """
    load_dotenv(PROJECT_ROOT / ".env")
    swid, s2 = os.environ.get("ESPN_SWID"), os.environ.get("ESPN_S2")
    now = dt.datetime.now().astimezone()
    if swid and s2:
        swid = swid if swid.startswith("{") else "{" + swid + "}"
        try:
            filt = {"players": {"limit": 400,
                    "sortDraftRanks": {"sortPriority": 1, "sortAsc": True,
                                       "value": "STANDARD"}}}
            r = requests.get(
                f"https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/"
                f"{ESPN_SEASON}/segments/0/leagues/{LEAGUE_ID}",
                params={"view": "kona_player_info"},
                cookies={"SWID": swid, "espn_s2": s2},
                headers={"User-Agent": "Mozilla/5.0",
                         "x-fantasy-filter": json.dumps(filt)}, timeout=30)
            r.raise_for_status()
            rows = [{"espn_id": (e.get("player") or {}).get("id"),
                     "injury_status": (e.get("player") or {}).get("injuryStatus"),
                     "pro_team_live": (e.get("player") or {}).get("proTeamId")}
                    for e in r.json().get("players", [])]
            rows = [x for x in rows if x["espn_id"]]
            INJURY_CACHE.parent.mkdir(parents=True, exist_ok=True)
            INJURY_CACHE.write_text(json.dumps(
                {"fetched_at": now.isoformat(timespec="seconds"), "players": rows}))
            return pd.DataFrame(rows), now.isoformat(timespec="seconds"), "live"
        except Exception as exc:
            print(f"  WARNING: live injury fetch failed ({type(exc).__name__}: "
                  f"{str(exc)[:80]}); falling back to cache")
    if INJURY_CACHE.exists():
        blob = json.loads(INJURY_CACHE.read_text())
        return pd.DataFrame(blob["players"]), blob["fetched_at"], "cache"
    return pd.DataFrame(columns=["espn_id", "injury_status"]), "never", "none"


def describe_age(stamp: str) -> str:
    if stamp == "never":
        return "no injury data at all"
    try:
        then = dt.datetime.fromisoformat(stamp)
    except ValueError:
        return f"unparseable timestamp {stamp!r}"
    age = dt.datetime.now().astimezone() - then
    mins = age.total_seconds() / 60
    if mins < 2:
        return f"{stamp} (just now)"
    if mins < 90:
        return f"{stamp} ({mins:.0f} minutes old)"
    hrs = mins / 60
    if hrs < 48:
        return f"{stamp} ({hrs:.1f} hours old)"
    return f"{stamp} ({hrs/24:.1f} DAYS OLD -- likely stale)"


def load_espn() -> tuple[pd.DataFrame, str, str]:
    """ESPN player universe joined through the COMMITTED ID map.

    No name matching happens here any more. The map (espn_bdl_id_map.csv, built
    by build_id_map.py) carries the ESPN id -> BallDontLie player_id resolution
    including hand-entered overrides, so a nickname mismatch is fixed once in a
    reviewable file rather than re-guessed on every run (design spec section 4).
    """
    if not ID_MAP.exists():
        raise SystemExit(f"{ID_MAP.name} is missing -- run build_id_map.py first")
    rows = json.loads(ESPN_PLAYERS.read_text())
    df = pd.DataFrame(rows)
    df["positions"] = df["eligible"].apply(
        lambda ss: sorted({s for s in ss if s in LEAGUE_SLOTS}))
    df["pos_str"] = df["positions"].apply("/".join)

    id_map = pd.read_csv(ID_MAP)
    resolved = id_map[id_map["bdl_player_id"].notna()][["espn_id", "bdl_player_id",
                                                        "match_method"]]
    df = df.merge(resolved, on="espn_id", how="left")
    df["player_id"] = df["bdl_player_id"]

    inj, stamp, source = fetch_injuries()
    if len(inj):
        df = df.drop(columns=["injury_status"]).merge(
            inj[["espn_id", "injury_status"]], on="espn_id", how="left")
    return (df[["espn_id", "name", "pro_team", "positions", "pos_str",
                "injury_status", "draft_rank", "player_id", "match_method"]],
            stamp, source)
def load_baseline(con) -> pd.DataFrame:
    """Recency-weighted per-game rates from the snapshot.

    Weighting is season_weight x games_played, so recency and sample size are
    handled by one mechanism: a 12-game season contributes far less than a
    70-game one even within the same season weight. DNP rows are excluded from
    every denominator -- counting them understates every rate, which is a mistake
    this project has already made twice (section 5).
    """
    per_season = con.execute(f"""
        SELECT player_id, any_value(player_name) AS player_name, season,
               count(*)                       AS gp,
               sum(pts) AS pts, sum(reb) AS reb, sum(ast) AS ast,
               sum(stl) AS stl, sum(blk) AS blk, sum(fg3m) AS fg3m,
               sum(turnover) AS turnover,
               sum(fgm) AS fgm, sum(fga) AS fga,
               sum(ftm) AS ftm, sum(fta) AS fta
          FROM v_player_games
         WHERE game_type = 'regular' AND NOT is_dnp
           AND season IN ({", ".join(str(x) for x in SEASONS)})
         GROUP BY player_id, season
    """).df()
    per_season["w"] = per_season["season"].map(SEASON_WEIGHTS) * per_season["gp"]

    # Weighted per-game rate for stat X is sum(w * X/gp) / sum(w). Computing the
    # numerators as columns and summing them avoids a groupby.apply, which was
    # silently producing a duplicate player_name column.
    rate_cols = COUNTING + INVERTED + ["fgm", "fga", "ftm", "fta"]
    for c in rate_cols:
        per_season[f"wx_{c}"] = per_season["w"] * per_season[c] / per_season["gp"]
    per_season["wx_gp"] = per_season["w"] * per_season["gp"]

    g = per_season.groupby("player_id", as_index=False).agg(
        player_name=("player_name", "first"),
        w_total=("w", "sum"),
        wx_gp=("wx_gp", "sum"),
        seasons_used=("season", "nunique"),
        raw_gp=("gp", "sum"),
        **{f"wx_{c}": (f"wx_{c}", "sum") for c in rate_cols},
    )
    out = g[["player_id", "player_name", "seasons_used", "raw_gp"]].copy()
    out["w_gp"] = g["wx_gp"] / g["w_total"]          # weighted games per season
    for c in rate_cols:
        out[c] = g[f"wx_{c}"] / g["w_total"]         # weighted per-game rate
    return out


def load_projected(con) -> pd.DataFrame:
    """Projected per-game rates, in the exact shape `load_baseline` returns.

    Everything downstream -- the pool, the z-scores, the percentage impact, the
    availability discount -- is untouched and cannot tell the difference. That
    is deliberate: if the projected board ranked differently for any reason
    other than the rates, the comparison between windows would be measuring the
    scoring change rather than the projection.
    """
    rows = con.execute("""
        SELECT player_id, any_value(player_name) AS player_name,
               max(season) AS season, stat, projected_value
          FROM player_projections
         WHERE season = (SELECT max(season) FROM player_projections)
         GROUP BY player_id, stat, projected_value
    """).df()
    if rows.empty:
        raise SystemExit(
            "player_projections is empty — run `python projections.py --write` "
            "first (it fits the model and republishes the snapshot)")
    wide = rows.pivot_table(index="player_id", columns="stat",
                            values="projected_value", aggfunc="first")
    names = rows.groupby("player_id")["player_name"].first()
    rate_cols = COUNTING + INVERTED + ["fgm", "fga", "ftm", "fta"]
    missing = [c for c in rate_cols + ["gp"] if c not in wide.columns]
    if missing:
        raise SystemExit(f"projections table is missing {missing}")
    out = pd.DataFrame({"player_id": wide.index,
                        "player_name": names.reindex(wide.index).to_numpy()})
    # The projection already covers a whole season, so there is no window to
    # count and no weighting to apply: "weighted games" IS projected games.
    out["seasons_used"] = len(WINDOWS["blend"])
    out["raw_gp"] = wide["gp"].to_numpy()
    out["w_gp"] = wide["gp"].to_numpy()
    for c in rate_cols:
        out[c] = wide[c].to_numpy()
    return out.reset_index(drop=True)


def load_keepers() -> pd.DataFrame:
    """Players kept by each team, read from keepers.csv (team_id, player_name).

    A kept player never enters the draft, so leaving them in the pool inflates
    what is actually available. The file is hand-maintained because there is no
    API for it before the draft -- ESPN does not expose other teams' keeper
    choices, and the roster field is prior-season placeholder data (section 2).
    """
    if not KEEPERS.exists():
        return pd.DataFrame(columns=["team_id", "player_name"])
    k = pd.read_csv(KEEPERS)
    k["player_name"] = k["player_name"].astype(str).str.strip()
    return k[k["player_name"].ne("") & k["player_name"].ne("nan")]


def build_board(espn: pd.DataFrame, base: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    # Join on the resolved BallDontLie id from the committed map, never on name.
    base = base.rename(columns={"player_id": "player_id"})
    df = espn.merge(base, on="player_id", how="left", suffixes=("", "_bdl"))
    unmatched = df[df["player_id"].isna()][["name", "draft_rank", "pro_team"]]
    no_stats = df[df["player_id"].notna() & df["w_gp"].isna()][
        ["name", "draft_rank", "pro_team"]]
    unmatched = pd.concat([unmatched, no_stats], ignore_index=True)
    # Rookies and no-history players get a row with NO model value rather than
    # being dropped: invisible on a draft board is worse than visibly unpriced,
    # because they are real picks other teams will make.
    rookies = df[df["player_id"].isna() | df["w_gp"].isna()].copy()
    rookies["value_source"] = "espn_rank_only"
    df = df[df["player_id"].notna() & df["w_gp"].notna()].copy()
    df["value_source"] = "model"
    thin = df[df["w_gp"] < MIN_WEIGHTED_GP][["name", "w_gp"]]
    df = df[df["w_gp"] >= MIN_WEIGHTED_GP].copy()

    # ---- the pool: ESPN's top 168 by draft rank, among players with stats.
    # Taking the pool from ESPN's ranking rather than from the value being
    # computed avoids defining the pool circularly out of its own output.
    df = df.sort_values("draft_rank")
    pool_mask = df["draft_rank"].rank(method="first") <= POOL_SIZE
    pool = df[pool_mask]

    # ---- percentage categories: volume-weighted impact, not raw percentage.
    # A 95% free-throw shooter taking one attempt a game moves nothing; impact is
    # (player rate - pool rate) x the player's own attempts.
    league = {}
    for k in PCT:
        league[k] = pool[f"{k}m"].sum() / pool[f"{k}a"].sum()
        df[f"{k}_pct"] = np.where(df[f"{k}a"] > 0, df[f"{k}m"] / df[f"{k}a"], np.nan)
        df[f"{k}_impact"] = (df[f"{k}_pct"] - league[k]) * df[f"{k}a"]

    # ---- z-scores against the pool
    zcols = []
    for cat in COUNTING:
        mu, sd = pool[cat].mean(), pool[cat].std(ddof=0)
        df[f"z_{cat}"] = (df[cat] - mu) / sd
        zcols.append(f"z_{cat}")
    for k in PCT:
        pool_imp = ((pool[f"{k}m"] / pool[f"{k}a"]) - league[k]) * pool[f"{k}a"]
        mu, sd = pool_imp.mean(), pool_imp.std(ddof=0)
        df[f"z_{k}pct"] = (df[f"{k}_impact"] - mu) / sd
        zcols.append(f"z_{k}pct")
    for cat in INVERTED:
        mu, sd = pool[cat].mean(), pool[cat].std(ddof=0)
        # Negated: fewer turnovers is better, so a low value must score positive.
        df[f"z_{cat}"] = -((df[cat] - mu) / sd)
        zcols.append(f"z_{cat}")

    df["per_game_value"] = df[zcols].sum(axis=1)
    # Availability: a full linear discount. A player available for half a season
    # delivers half the category volume, which in a weekly H2H format is close to
    # literally true. Stated so it can be argued with.
    df["availability"] = (df["w_gp"] / FULL_SEASON).clip(upper=1.0)
    df["total_value"] = df["per_game_value"] * df["availability"]
    df = df.sort_values("total_value", ascending=False).reset_index(drop=True)
    df["board_rank"] = np.arange(1, len(df) + 1)
    df["in_pool"] = df["board_rank"] <= POOL_SIZE
    meta = {"league_fg": league["fg"], "league_ft": league["ft"],
            "pool_n": int(pool_mask.sum()), "unmatched": unmatched, "thin": thin,
            "rookies": rookies.sort_values("draft_rank")}
    return df, meta


def scarcity(df: pd.DataFrame) -> pd.DataFrame:
    """How fast does value fall off at each position?

    Twelve teams start one of each base position, so the 12th-best eligible
    player is roughly the last starter and the 24th-best is roughly the last
    startable body. A big 12-to-24 gap means the position runs dry early.
    """
    rows = []
    for pos in BASE_POS:
        el = df[df["positions"].apply(lambda ps: pos in ps)]
        el = el.sort_values("total_value", ascending=False)
        v = el["total_value"].tolist()
        def at(n):
            return round(v[n - 1], 2) if len(v) >= n else None
        rows.append({"position": pos, "eligible_in_pool": int(el["in_pool"].sum()),
                     "best": at(1), "v12": at(12), "v24": at(24),
                     "gap_12_to_24": (round(at(12) - at(24), 2)
                                      if at(12) is not None and at(24) is not None
                                      else None)})
    return pd.DataFrame(rows).sort_values("gap_12_to_24", ascending=False)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--include-out", action="store_true",
                    help="keep OUT/SUSPENSION players in the best-available list")
    ap.add_argument("--available", type=int, default=15,
                    help="how many best-available players to print")
    ap.add_argument("--out", type=Path, default=OUT_CSV)
    ap.add_argument("--window",
                    choices=sorted([*WINDOWS, PROJECTED_WINDOW]), default="blend",
                    help="which seasons feed the weighted baseline (same "
                         "weighting, renormalised over the window), or "
                         "'projected' to score the model's projections instead")
    args = ap.parse_args()
    apply_window(args.window)

    if SNAPSHOT.resolve() == LIVE_DB.resolve():
        raise SystemExit("refusing to read the live database")
    con = duckdb.connect(str(SNAPSHOT), read_only=True)
    try:
        espn, inj_stamp, inj_source = load_espn()
        base = (load_projected(con) if args.window == PROJECTED_WINDOW
                else load_baseline(con))
    finally:
        con.close()

    df, meta = build_board(espn, base)

    keep = load_keepers()
    kept_names = set(keep["player_name"].apply(str.lower))
    df["kept"] = df["name"].str.lower().isin(kept_names)
    meta["rookies"]["kept"] = meta["rookies"]["name"].str.lower().isin(kept_names)
    matched_keepers = sorted(set(df.loc[df["kept"], "name"])
                             | set(meta["rookies"].loc[meta["rookies"]["kept"], "name"]))
    missing_keepers = sorted(kept_names - {n.lower() for n in matched_keepers})
    rk = meta["rookies"]
    rk = rk[~rk["kept"]] if "kept" in rk.columns else rk

    w = ", ".join(f"{s}-{str(s+1)[2:]} {SEASON_WEIGHTS[s]:.2f}" for s in sorted(SEASON_WEIGHTS, reverse=True))
    print("=" * 96)
    print(f"INJURY DATA: {inj_source} pull, {describe_age(inj_stamp)}")
    if inj_source != "live":
        print("  NOT a live pull -- treat every status below as possibly out of date.")
    print("=" * 96)
    print("METHODOLOGY (stated before the numbers)")
    print("=" * 96)
    print(f"""
SAMPLE      ESPN's top {len(espn)} players by STANDARD draft rank, joined to the
            snapshot by normalised name, keeping those with >= {MIN_WEIGHTED_GP} weighted
            games. Regular season only (game_type='regular'); DNP rows excluded
            from every denominator.
BASELINE    Recency-weighted per-game rates over {SEASONS[0]}-{str(SEASONS[0]+1)[2:]}..{SEASONS[-1]}-{str(SEASONS[-1]+1)[2:]}.
            Season weights: {w}.
            Each season's weight is multiplied by its games played, so recency
            and sample size are handled by one mechanism.
POOL        {POOL_SIZE} players = 12 teams x 14 rounds, taken from ESPN's own
            draftDetail.picks rather than inferred. Pool membership for the
            means and standard deviations comes from ESPN's top {POOL_SIZE} draft
            ranks, so the pool is not defined out of the value it produces.
SCORING     Six counting categories z-scored against the pool. FG% and FT% as
            volume-weighted impact: (player rate - pool rate) x attempts/game,
            then z-scored. Turnovers z-scored and NEGATED. Total = sum of 9 z's.
            Pool FG% = {meta['league_fg']:.4f}, pool FT% = {meta['league_ft']:.4f}.
AVAILABILITY  total_value = per_game_value x (weighted games / {FULL_SEASON}), capped at 1.

NOT HANDLED -- these are real gaps, not hedging:
  * Not the Phase 6 projection model. No walk-forward validation, no aging
    curve, no opponent or pace adjustment, no regression to the mean.
  * OFFSEASON MOVES AND ROLE CHANGES ARE INVISIBLE. Every rate here comes from
    the player's previous team and role. ESPN's team column is current, the
    stats are not -- Giannis Antetokounmpo reads MIA and LeBron James reads PHL
    on this board while their numbers are entirely from Milwaukee and Los
    Angeles. Treat any player who moved as unpriced.
  * Rookies and near-rookies are absent or thin: no NBA history means no row.
    {len(meta['unmatched'])} ESPN players had no match in the snapshot at all and
    {len(meta['thin'])} more fell under the games floor.
  * Standalone values, not roster-aware. No punt strategy, no positional
    weighting inside the total, no consideration of what you have already drafted.
  * The one-keeper rule is reported but NOT modelled -- which player each of the
    other 11 teams keeps is unknowable from here, and it will remove roughly 12
    names from the pool.
  * All nine categories weighted equally. That mirrors the scoring but is not
    necessarily the best strategy.
  * TURNOVERS ARE PARTLY A USAGE PENALTY, not a ball-security measure. Inverting
    the z-score rewards players who never touch the ball: across the pool,
    corr(TO z-score, points/game) = -0.77, and the best TO scores on the whole
    board belong to players averaging under 4 points. Measured containment: zero
    players inside the top 50 have a TO z above +1.0 while scoring under 12 a
    game, so the artifact lives in the deep bench rather than the draftable range
    -- but it is a real distortion, and a per-possession turnover rate would be
    the honest fix.
  * INJURY STATUS IS FLAGGED, NOT PRICED. Availability discounts past games
    missed; it knows nothing about a current injury. Eight of the top 50 carry a
    non-ACTIVE ESPN status, and the sharpest case is Jimmy Butler III at board
    #26 against ESPN's #207 -- ESPN is pricing an OUT designation this board only
    labels. Read every flagged player as a question, not a recommendation.
  * The board systematically FADES young improvers, because historical rates
    cannot see a role or leap that has not happened yet: Paolo Banchero, Derik
    Queen, Matas Buzelis, Deni Avdija and Brandin Podziemski all sit 45+ places
    below their ESPN rank. That is the same blind spot as the offseason-moves one
    and it is structural, not a tuning problem.
""")

    show = ["board_rank", "name", "pro_team", "pos_str", "match_method",
            "value_source", "kept", "w_gp", "availability",
            "z_pts", "z_reb", "z_ast", "z_stl", "z_blk", "z_fg3m",
            "z_fgpct", "z_ftpct", "z_turnover",
            "per_game_value", "total_value", "injury_status", "draft_rank"]
    out = df[show + ["player_id", "espn_id", "pts", "reb", "ast", "stl", "blk",
                     "fg3m", "turnover", "fg_pct", "ft_pct", "fga", "fta",
                     "in_pool", "seasons_used", "raw_gp"]].copy()
    for c in out.columns:
        if out[c].dtype.kind == "f":
            out[c] = out[c].round(4)
    rcols = ["name", "pro_team", "pos_str", "draft_rank", "injury_status",
             "value_source", "kept", "espn_id"]
    out = pd.concat([out, rk[[c for c in rcols if c in rk.columns]]],
                    ignore_index=True)
    out.to_csv(args.out, index=False)

    hdr = (f"{'#':>3} {'player':<25}{'tm':<4}{'pos':<12}{'GP':>5}{'PTS':>6}{'REB':>6}"
           f"{'AST':>6}{'STL':>6}{'BLK':>6}{'3PM':>6}{'FG%':>6}{'FT%':>6}{'TO':>6}"
           f"{'/gm':>7}{'TOTAL':>7}  flag")
    print("=" * 96)
    print(f"TOP {args.top} OF {len(df)} RANKED PLAYERS  (pool = top {POOL_SIZE})")
    print("=" * 96)
    print(hdr)
    for _, r in df.head(args.top).iterrows():
        flag = "" if r["injury_status"] in (None, "ACTIVE") else r["injury_status"]
        print(f"{int(r['board_rank']):>3} {r['name'][:24]:<25}{r['pro_team']:<4}"
              f"{r['pos_str'][:11]:<12}{r['w_gp']:>5.0f}"
              f"{r['z_pts']:>6.2f}{r['z_reb']:>6.2f}{r['z_ast']:>6.2f}{r['z_stl']:>6.2f}"
              f"{r['z_blk']:>6.2f}{r['z_fg3m']:>6.2f}{r['z_fgpct']:>6.2f}"
              f"{r['z_ftpct']:>6.2f}{r['z_turnover']:>6.2f}"
              f"{r['per_game_value']:>7.2f}{r['total_value']:>7.2f}  {flag}")

    print("=" * 96)
    if len(keep):
        print(f"KEEPERS: {len(keep)} entered, {len(matched_keepers)} matched and "
              f"removed from the available pool")
        for _, r in keep.iterrows():
            print(f"  team {r['team_id']}: {r['player_name']}")
        if missing_keepers:
            print(f"  WARNING: {len(missing_keepers)} keeper name(s) matched no "
                  f"player on the board -- check spelling: {missing_keepers}")
    else:
        print("KEEPERS: keepers.csv is empty. Each of the 12 teams keeps 1 player,")
        print("  so roughly 12 names should leave the pool. Until it is filled in,")
        print("  availability below is OVERSTATED.")

    excl = df["injury_status"].fillna("ACTIVE").isin(EXCLUDE_STATUSES) | df["kept"]
    print("\n" + "=" * 96)
    if args.include_out:
        print(f"BEST AVAILABLE (top {args.available}) -- OUT players INCLUDED (--include-out)")
        avail = df
    else:
        print(f"BEST AVAILABLE (top {args.available}) -- excluding "
              f"{int(df['kept'].sum())} keeper(s) and "
              f"{int((~df['kept'] & df['injury_status'].fillna('ACTIVE').isin(EXCLUDE_STATUSES)).sum())} "
              f"{'/'.join(sorted(EXCLUDE_STATUSES))} player(s)")
        avail = df[~excl]
    print("=" * 96)
    for _, r in avail.head(args.available).iterrows():
        flag = "" if r["injury_status"] in (None, "ACTIVE") or pd.isna(r["injury_status"]) \
               else f"  [{r['injury_status']}]"
        print(f"  {int(r['board_rank']):>3}. {r['name']:<26}{r['pro_team']:<5}"
              f"{r['pos_str'][:14]:<15}total {r['total_value']:>6.2f}"
              f"  avail {r['availability']:.2f}{flag}")
    if not args.include_out and excl.any():
        print(f"\n  excluded ({'/'.join(sorted(EXCLUDE_STATUSES))}), shown for awareness:")
        for _, r in df[excl].head(8).iterrows():
            print(f"    board #{int(r['board_rank']):<4} {r['name']:<26}"
                  f"{r['injury_status']:<12} total {r['total_value']:>6.2f}"
                  f"  ESPN #{int(r['draft_rank']) if not pd.isna(r['draft_rank']) else '?'}")

    print("\n" + "=" * 96)
    print("POSITION SCARCITY  (12 teams start one of each, so v12 is the last starter)")
    print("=" * 96)
    sc = scarcity(df)
    print(sc.to_string(index=False))
    print("\n  Read the gap column: a large 12-to-24 drop means the position empties")
    print("  fast and is worth reaching for; a small one means you can wait.")

    if len(rk):
        print("\n" + "=" * 96)
        print(f"NO MODEL VALUE -- ESPN RANK ONLY ({len(rk)} players)")
        print("=" * 96)
        print("  These have no NBA history in the snapshot, so the 9-cat model")
        print("  cannot price them at all. They are listed because other teams")
        print("  WILL draft them; the ESPN rank is the only signal here.")
        for _, r in rk.head(20).iterrows():
            rank = "?" if pd.isna(r["draft_rank"]) else int(r["draft_rank"])
            print(f"    ESPN #{str(rank):<5} {r['name']:<26}{r['pro_team']:<5}"
                  f"{r['pos_str'][:14]:<15}[no model value]")

    if len(meta["unmatched"]):
        print(f"\n  {len(meta['unmatched'])} ESPN players with no snapshot history "
              f"(rookies / never played). Top by ESPN rank:")
        for _, r in meta["unmatched"].sort_values("draft_rank").head(8).iterrows():
            print(f"    ESPN #{r['draft_rank']:<5} {r['name']:<26} {r['pro_team']}")
    if len(meta["thin"]):
        print(f"\n  {len(meta['thin'])} players under the {MIN_WEIGHTED_GP}-game floor, excluded:")
        for _, r in meta["thin"].head(6).iterrows():
            print(f"    {r['name']:<26} weighted GP {r['w_gp']:.1f}")

    mm = df["match_method"].value_counts().to_dict()
    print(f"\n  ID resolution via {ID_MAP.name}: "
          + ", ".join(f"{k} {v}" for k, v in mm.items()))
    print(f"wrote {args.out.name}: {len(out)} rows x {len(out.columns)} columns")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
