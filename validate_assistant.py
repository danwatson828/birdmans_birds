#!/usr/bin/env python3
"""Out-of-sample validation of the draft assistant. READ-ONLY.

WHY THIS EXISTS. The in-sample simulation in draft_assistant.py is CIRCULAR: both
strategies draft to maximise a summed z-score and the simulated season scores the
same summed z-score, so it can only measure which strategy accumulates more of
the quantity it was told to accumulate. It reported my team finishing 1st of 12
in 100% of runs with an 0.81 category win share -- a number no real league
produces, and the tell that the test was measuring itself.

This version breaks the circularity by drafting on estimates and scoring on
outcomes:
    draft   using only seasons T-3..T-1  (what you would have known)
    score   using season T's ACTUAL per-game production
Same walk-forward discipline as backtest_weights.py.

Substitutions forced by the data, both of which weaken the test:
  * No historical ESPN ranks exist, so the opponents' consensus order is the
    prior-window value rank plus noise. Real ESPN ranks correlate about 0.77 with
    the board, so this makes the field a little too similar to my own board.
  * ESPN position eligibility is current-only, so historical position fit uses
    players.position, a single string ("G", "F-C") rather than a real eligibility
    list. Position fit is therefore cruder here than in the live assistant.

    python validate_assistant.py --seasons 2021 2022 2023 2024 2025 --sims 30
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats as sps

import draft_assistant as DA

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
LIVE_DB = PROJECT_ROOT / "nba.duckdb"
WEIGHTS = {0: 0.60, 1: 0.30, 2: 0.10}          # offsets from T-1
POS_TO_SLOTS = {"G": ["PG", "SG", "G"], "F": ["SF", "PF", "F"], "C": ["C"],
                "G-F": ["SG", "SF", "G", "F"], "F-G": ["SG", "SF", "G", "F"],
                "F-C": ["PF", "C", "F"], "C-F": ["PF", "C", "F"]}


def season_totals(con) -> pd.DataFrame:
    return con.execute("""
        SELECT pg.player_id, any_value(pg.player_name) player_name, pg.season,
               any_value(p."position") AS pos_raw, count(*) gp,
               sum(pg.pts) pts, sum(pg.reb) reb, sum(pg.ast) ast, sum(pg.stl) stl,
               sum(pg.blk) blk, sum(pg.fg3m) fg3m, sum(pg.turnover) turnover,
               sum(pg.fgm) fgm, sum(pg.fga) fga, sum(pg.ftm) ftm, sum(pg.fta) fta
          FROM v_player_games pg JOIN players p ON p.id = pg.player_id
         WHERE pg.game_type='regular' AND NOT pg.is_dnp AND pg.season >= 1990
         GROUP BY pg.player_id, pg.season""").df()


def to_z(rates: pd.DataFrame, pool: pd.DataFrame) -> pd.DataFrame:
    """The board's nine z-scores. Identical construction on both sides."""
    z = pd.DataFrame(index=rates.index)
    for c in ["pts", "reb", "ast", "stl", "blk", "fg3m"]:
        sd = pool[c].std(ddof=0)
        z[f"z_{c}"] = 0.0 if not sd else (rates[c] - pool[c].mean()) / sd
    for k in ("fg", "ft"):
        lg = pool[f"{k}m"].sum() / pool[f"{k}a"].sum()
        pi = (pool[f"{k}m"] / pool[f"{k}a"].replace(0, np.nan) - lg) * pool[f"{k}a"]
        im = (rates[f"{k}m"] / rates[f"{k}a"].replace(0, np.nan) - lg) * rates[f"{k}a"]
        sd = pi.std(ddof=0)
        z[f"z_{k}pct"] = 0.0 if not sd else (im - pi.mean()) / sd
    sd = pool["turnover"].std(ddof=0)
    z["z_turnover"] = 0.0 if not sd else -((rates["turnover"] - pool["turnover"].mean()) / sd)
    return z.fillna(0.0)


def weighted_window(tot: pd.DataFrame, T: int) -> pd.DataFrame:
    win = tot[tot["season"].isin([T - 1, T - 2, T - 3])].copy()
    win["w"] = win["season"].map({T - 1 - k: v for k, v in WEIGHTS.items()}) * win["gp"]
    cols = ["pts", "reb", "ast", "stl", "blk", "fg3m", "turnover",
            "fgm", "fga", "ftm", "fta"]
    for c in cols:
        win[f"wx_{c}"] = win["w"] * win[c] / win["gp"]
    win["wx_gp"] = win["w"] * win["gp"]
    g = win.groupby("player_id", as_index=False).agg(
        player_name=("player_name", "first"), pos_raw=("pos_raw", "first"),
        w=("w", "sum"), wx_gp=("wx_gp", "sum"),
        **{f"wx_{c}": (f"wx_{c}", "sum") for c in cols})
    out = g[["player_id", "player_name", "pos_raw"]].copy()
    out["w_gp"] = g["wx_gp"] / g["w"]
    for c in cols:
        out[c] = g[f"wx_{c}"] / g["w"]
    return out[out["w_gp"] >= 20].reset_index(drop=True)


def prep(df: pd.DataFrame, pool_n: int = 168) -> pd.DataFrame:
    pool = df.nlargest(pool_n, "w_gp")
    z = to_z(df, pool)
    out = pd.concat([df.reset_index(drop=True), z.reset_index(drop=True)], axis=1)
    out["availability"] = (out["w_gp"] / 82).clip(upper=1.0)
    out["total_value"] = z.sum(axis=1).values * out["availability"]
    out["positions"] = out["pos_raw"].fillna("F").map(
        lambda p: POS_TO_SLOTS.get(p, ["UT"]))
    return out


def run_season(tot: pd.DataFrame, T: int, sims: int, seed: int,
               opp_noise: float) -> pd.DataFrame:
    pred = prep(weighted_window(tot, T))
    cur = tot[tot["season"] == T].set_index("player_id")
    if cur.empty or len(pred) < 200:
        return pd.DataFrame()
    rates = cur[["pts", "reb", "ast", "stl", "blk", "fg3m", "turnover",
                 "fgm", "fga", "ftm", "fta"]].div(cur["gp"], axis=0)
    real_pool = rates.loc[cur["gp"].nlargest(168).index]
    zr = to_z(rates, real_pool)
    realized = zr.mul((cur["gp"] / 82).clip(upper=1.0), axis=0)

    # consensus order stands in for ESPN rank (none exists historically)
    pred = pred.sort_values("total_value", ascending=False).reset_index(drop=True)
    pred["draft_rank"] = np.arange(1, len(pred) + 1)

    rows = []
    for s in range(sims):
        res = {"season": T, "sim": s}
        for strat in ("board", "assistant"):
            rng = np.random.default_rng(seed + s)
            rosters = DA.simulate_draft(pred, strat, rng, opp_noise)
            # score on REALIZED season-T production, not on the draft estimates
            share = {}
            for t, r in rosters.items():
                ids = [i for i in r["player_id"] if i in realized.index]
                share[t] = realized.loc[ids].sum() if ids else realized.iloc[0:0].sum()
            teams = sorted(share)
            rng2 = np.random.default_rng(seed + s + 99991)
            wins = {t: 0.0 for t in teams}
            n = {t: 0 for t in teams}
            for _ in range(20):
                for i, a in enumerate(teams):
                    for b in teams[i + 1:]:
                        for c in DA.CATS:
                            va = share[a][f"z_{c}"] + rng2.normal(0, 0.55)
                            vb = share[b][f"z_{c}"] + rng2.normal(0, 0.55)
                            wins[a] += 1 if va > vb else (0.5 if va == vb else 0)
                            wins[b] += 1 if vb > va else (0.5 if va == vb else 0)
                        n[a] += len(DA.CATS); n[b] += len(DA.CATS)
            res[strat] = wins[DA.MY_TEAM_ID] / n[DA.MY_TEAM_ID]
            res[f"{strat}_rank"] = 1 + sum(
                1 for t in teams if wins[t] / n[t] > res[strat])
        rows.append(res)
    return pd.DataFrame(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seasons", type=int, nargs="+",
                    default=[2021, 2022, 2023, 2024, 2025])
    ap.add_argument("--sims", type=int, default=20)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--opp-noise", type=float, default=12.0)
    args = ap.parse_args()
    if SNAPSHOT.resolve() == LIVE_DB.resolve():
        raise SystemExit("refusing to read the live database")
    con = duckdb.connect(str(SNAPSHOT), read_only=True)
    try:
        tot = season_totals(con)
    finally:
        con.close()

    allres = pd.concat([run_season(tot, T, args.sims, args.seed, args.opp_noise)
                        for T in args.seasons], ignore_index=True)
    if allres.empty:
        raise SystemExit("no usable seasons")

    print("=" * 80)
    print("OUT-OF-SAMPLE VALIDATION -- draft on T-3..T-1, score on season T actuals")
    print("=" * 80)
    print(f"  seasons: {args.seasons}   sims/season: {args.sims}   "
          f"opponent noise sd: {args.opp_noise}")
    print(f"\n  {'season':>7}{'board':>9}{'assistant':>11}{'diff':>9}"
          f"{'asst wins':>11}{'board fin':>11}{'asst fin':>10}")
    for T, g in allres.groupby("season"):
        d = g["assistant"] - g["board"]
        print(f"  {T:>7}{g['board'].mean():>9.4f}{g['assistant'].mean():>11.4f}"
              f"{d.mean():>+9.4f}{f'{int((d>0).sum())}/{len(d)}':>11}"
              f"{g['board_rank'].mean():>11.2f}{g['assistant_rank'].mean():>10.2f}")
    d = allres["assistant"] - allres["board"]
    t = sps.ttest_rel(allres["assistant"], allres["board"])
    print(f"\n  POOLED (n={len(allres)} paired drafts)")
    print(f"    board     : {allres['board'].mean():.4f} category win share "
          f"(finish {allres['board_rank'].mean():.2f} of 12)")
    print(f"    assistant : {allres['assistant'].mean():.4f} "
          f"(finish {allres['assistant_rank'].mean():.2f} of 12)")
    print(f"    difference: {d.mean():+.4f} "
          f"({100*d.mean()/allres['board'].mean():+.2f}% relative), "
          f"assistant wins {int((d>0).sum())}/{len(d)}, p={t.pvalue:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
