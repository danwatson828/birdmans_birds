#!/usr/bin/env python3
"""Walk-forward backtest of the draft board's season weights. READ-ONLY.

Question: do the shipped weights (0.60 / 0.30 / 0.10 over the three prior
seasons) actually predict next season's realized 9-cat value better than the
alternatives? The first draft board verified its arithmetic, not its predictive
validity -- this is the missing half.

NO LEAKAGE. For each target season T the prediction uses ONLY seasons T-1, T-2
and T-3: the weighted rates, the availability discount, and the pool used to
normalise the z-scores are all built from pre-T data. Season T is touched only to
score the outcome.

The pool is identical across weight schemes (top 168 by unweighted games in the
prior window), so the comparison isolates the weighting and nothing else.

    python backtest_weights.py
    python backtest_weights.py --from-season 2010
"""

from __future__ import annotations

import argparse
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats as sps

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
LIVE_DB = PROJECT_ROOT / "nba.duckdb"

# All nine categories are >=95% recorded only from 1983 (v_stat_first_tracked:
# stl, blk and turnover all reach full coverage that year), so the earliest
# usable INPUT season is 1983 and the earliest target is 1986.
FIRST_INPUT_SEASON = 1983
POOL_SIZE = 168
MIN_GP = 20
FULL_SEASON = 82

SCHEMES = {
    "recent only  1.0/0/0": (1.00, 0.00, 0.00),
    "shipped      .6/.3/.1": (0.60, 0.30, 0.10),
    "flatter      .5/.3/.2": (0.50, 0.30, 0.20),
    "equal      1/3 each":  (1 / 3, 1 / 3, 1 / 3),
}
COUNTING = ["pts", "reb", "ast", "stl", "blk", "fg3m"]


def load_season_totals(con) -> pd.DataFrame:
    return con.execute(f"""
        SELECT player_id, any_value(player_name) AS player_name, season,
               count(*) AS gp,
               sum(pts) pts, sum(reb) reb, sum(ast) ast, sum(stl) stl,
               sum(blk) blk, sum(fg3m) fg3m, sum(turnover) turnover,
               sum(fgm) fgm, sum(fga) fga, sum(ftm) ftm, sum(fta) fta
          FROM v_player_games
         WHERE game_type = 'regular' AND NOT is_dnp
           AND season >= {FIRST_INPUT_SEASON}
         GROUP BY player_id, season
    """).df()


def nine_cat_value(rates: pd.DataFrame, pool: pd.DataFrame) -> pd.Series:
    """The board's scoring function, applied to whatever rates are handed in.

    Shared by the prediction and the outcome so the two are measured on the same
    scale -- if this function is wrong, it is wrong identically on both sides and
    the comparison between weight schemes still holds.
    """
    z = pd.DataFrame(index=rates.index)
    for c in COUNTING:
        sd = pool[c].std(ddof=0)
        z[c] = 0.0 if sd == 0 else (rates[c] - pool[c].mean()) / sd
    for k in ("fg", "ft"):
        lg = pool[f"{k}m"].sum() / pool[f"{k}a"].sum()
        pool_imp = (pool[f"{k}m"] / pool[f"{k}a"].replace(0, np.nan) - lg) * pool[f"{k}a"]
        imp = (rates[f"{k}m"] / rates[f"{k}a"].replace(0, np.nan) - lg) * rates[f"{k}a"]
        sd = pool_imp.std(ddof=0)
        z[f"{k}pct"] = 0.0 if sd == 0 else (imp - pool_imp.mean()) / sd
    sd = pool["turnover"].std(ddof=0)
    z["turnover"] = 0.0 if sd == 0 else -((rates["turnover"] - pool["turnover"].mean()) / sd)
    return z.fillna(0.0).sum(axis=1)


def weighted_rates(win: pd.DataFrame, weights: dict[int, float]) -> pd.DataFrame:
    """Per-game rates over a window, weighted by season weight x games played."""
    d = win.copy()
    d["w"] = d["season"].map(weights) * d["gp"]
    d = d[d["w"] > 0]
    cols = COUNTING + ["turnover", "fgm", "fga", "ftm", "fta"]
    for c in cols:
        d[f"wx_{c}"] = d["w"] * d[c] / d["gp"]
    d["wx_gp"] = d["w"] * d["gp"]
    g = d.groupby("player_id", as_index=False).agg(
        w_total=("w", "sum"), wx_gp=("wx_gp", "sum"),
        raw_gp=("gp", "sum"),
        **{f"wx_{c}": (f"wx_{c}", "sum") for c in cols})
    out = g[["player_id", "raw_gp"]].copy()
    out["w_gp"] = g["wx_gp"] / g["w_total"]
    for c in cols:
        out[c] = g[f"wx_{c}"] / g["w_total"]
    return out.set_index("player_id")


def run(con, first_target: int) -> tuple[pd.DataFrame, list[int]]:
    tot = load_season_totals(con)
    seasons = sorted(tot["season"].unique())
    targets = [t for t in seasons if t - 3 >= FIRST_INPUT_SEASON and t >= first_target]
    rows = []
    for T in targets:
        win = tot[tot["season"].isin([T - 1, T - 2, T - 3])]
        cur = tot[tot["season"] == T].set_index("player_id")
        if win.empty or cur.empty:
            continue

        # --- pool, scheme-independent and pre-T only -------------------------
        gp_prior = win.groupby("player_id")["gp"].sum().sort_values(ascending=False)
        pool_ids = gp_prior.head(POOL_SIZE).index
        # equal-weight rates purely to define pool statistics, identically for
        # every scheme being compared
        pool_rates = weighted_rates(win[win["player_id"].isin(pool_ids)],
                                    {T - 1: 1 / 3, T - 2: 1 / 3, T - 3: 1 / 3})

        # --- realized outcome in season T -----------------------------------
        cur_rates = cur[COUNTING + ["turnover", "fgm", "fga", "ftm", "fta"]].div(
            cur["gp"], axis=0)
        cur_rates = cur_rates.assign(gp=cur["gp"])
        cur_pool = cur_rates.loc[cur_rates.index.intersection(
            cur["gp"].sort_values(ascending=False).head(POOL_SIZE).index)]
        realized = (nine_cat_value(cur_rates, cur_pool)
                    * (cur["gp"] / FULL_SEASON).clip(upper=1.0))

        for label, (w1, w2, w3) in SCHEMES.items():
            wr = weighted_rates(win, {T - 1: w1, T - 2: w2, T - 3: w3})
            wr = wr[wr["w_gp"] >= MIN_GP]
            pred = (nine_cat_value(wr, pool_rates)
                    * (wr["w_gp"] / FULL_SEASON).clip(upper=1.0))
            # evaluation universe: predictable AND actually played in T
            idx = pred.index.intersection(realized.index)
            idx = idx[cur.loc[idx, "gp"] >= MIN_GP]
            if len(idx) < 50:
                continue
            p, r = pred.loc[idx], realized.loc[idx]
            rho = sps.spearmanr(p, r).statistic
            n_hit = min(100, len(idx))
            hit = len(set(p.nlargest(n_hit).index) & set(r.nlargest(n_hit).index)) / n_hit
            rows.append({"target_season": T, "scheme": label, "n": len(idx),
                         "spearman": rho, "top100_hit": hit})
    return pd.DataFrame(rows), targets


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-season", type=int, default=FIRST_INPUT_SEASON + 3)
    args = ap.parse_args()
    if SNAPSHOT.resolve() == LIVE_DB.resolve():
        raise SystemExit("refusing to read the live database")
    con = duckdb.connect(str(SNAPSHOT), read_only=True)
    try:
        res, targets = run(con, args.from_season)
    finally:
        con.close()
    if res.empty:
        raise SystemExit("no usable target seasons")

    print("=" * 84)
    print("WALK-FORWARD BACKTEST OF THE SEASON WEIGHTS")
    print("=" * 84)
    print(f"""
Design      For each target season T, predict using ONLY seasons T-1/T-2/T-3.
            Season T is used only to score the outcome. The pool that normalises
            the z-scores is built from pre-T games and is IDENTICAL across
            schemes, so the comparison isolates the weighting.
Target      the same 9-cat total the board produces, computed from season T's
            actual per-game rates x actual availability.
Metrics     Spearman rank correlation over the evaluation universe, and the
            share of the predicted top 100 that lands in the realized top 100.
Universe    players with >= {MIN_GP} weighted games before T AND >= {MIN_GP} games in T.
Sample      {len(res['target_season'].unique())} target seasons ({min(targets)}-{max(targets)}),
            median universe {int(res['n'].median())} players per season.
""")
    agg = res.groupby("scheme").agg(
        seasons=("target_season", "nunique"),
        spearman_mean=("spearman", "mean"), spearman_sd=("spearman", "std"),
        hit_mean=("top100_hit", "mean"), hit_sd=("top100_hit", "std")).reset_index()
    order = list(SCHEMES)
    agg["o"] = agg["scheme"].apply(order.index)
    agg = agg.sort_values("o").drop(columns="o")
    print(f"  {'scheme':<24}{'seasons':>8}{'Spearman':>11}{'(sd)':>8}"
          f"{'top100 hit':>12}{'(sd)':>8}")
    for _, r in agg.iterrows():
        print(f"  {r['scheme']:<24}{int(r['seasons']):>8}{r['spearman_mean']:>11.4f}"
              f"{r['spearman_sd']:>8.3f}{r['hit_mean']:>12.3f}{r['hit_sd']:>8.3f}")

    best_s = agg.loc[agg["spearman_mean"].idxmax(), "scheme"]
    best_h = agg.loc[agg["hit_mean"].idxmax(), "scheme"]
    ship = "shipped      .6/.3/.1"
    print(f"\n  best by Spearman : {best_s}")
    print(f"  best by hit rate : {best_h}")

    # Paired test: the schemes see identical seasons, so compare within season.
    piv = res.pivot(index="target_season", columns="scheme", values="spearman")
    print(f"\n  PAIRED comparison against the shipped weights (same seasons, "
          f"n={len(piv)}):")
    for sc in order:
        if sc == ship:
            continue
        d = (piv[sc] - piv[ship]).dropna()
        t = sps.ttest_rel(piv[sc].dropna(), piv[ship].dropna())
        print(f"    {sc:<24} mean diff {d.mean():+.4f}  wins {int((d>0).sum())}/"
              f"{len(d)} seasons  p={t.pvalue:.3f}")
    print(f"""
  How to read this: the differences are what matter, not the absolute level.
  A scheme that wins by a hair on the mean but loses most individual seasons is
  not better. Treat p > 0.05 on the paired test as "no evidence of a
  difference", which is a reason to leave the shipped weights alone.""")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
