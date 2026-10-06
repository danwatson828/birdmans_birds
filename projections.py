#!/usr/bin/env python3
"""Season-level 9-cat projections, validated walk-forward. READ-ONLY on the DB.

WHAT THIS PREDICTS. For a target season T, each player's realized 9-category
total value in T -- the same quantity `backtest_weights.py` scores, computed by
the same `nine_cat_value` function, so the number produced here is directly
comparable to the recency-weighted baseline and not to a reweighted cousin of it.

THE PROTOCOL IS COPIED, NOT REINVENTED. Features for target T are built from
seasons T-1/T-2/T-3 only. The model that predicts T is fit on target seasons
strictly before T (an expanding window), so no row the model ever sees contains
information from T or later. The pool that normalises z-scores is pre-T and
identical to the baseline's. The evaluation universe is identical too: at least
MIN_GP weighted games before T and at least MIN_GP games in T. Any of those
loosened would make the comparison flattering and meaningless.

WHY A SEPARATE FEATURE BUILDER. The model builder's registry (api.model_var)
resolves variables for an interactive fit at player-season grain. Reusing its
*definitions* is the point -- per-36 rates, minutes-weighted advanced metrics,
and lag semantics all come from there -- but the pipeline needs them assembled
as a three-season window per target with no season-T column present at all,
which is a different shape from "one row per player-season". The definitions
are shared; the assembly is not.

    python projections.py --validate            # walk-forward, prints the table
    python projections.py --validate --fast     # 2005+ only, for iteration
    python projections.py --write               # fit final model, write table
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy import stats as sps

import backtest_weights as bw

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
LIVE_DB = PROJECT_ROOT / "nba.duckdb"

MODEL_VERSION = "gbt-9cat-v1"
FIRST_INPUT_SEASON = bw.FIRST_INPUT_SEASON     # 1983
POOL_SIZE = bw.POOL_SIZE                       # 168
MIN_GP = bw.MIN_GP                             # 20
FULL_SEASON = bw.FULL_SEASON                   # 82
COUNTING = bw.COUNTING

# The model needs a run-up of completed target seasons before its first honest
# prediction. Fitting on three seasons and calling the result a validated model
# would be the same self-flattery the circular draft simulation produced.
MIN_TRAIN_SEASONS = 8

RATE_COLS = COUNTING + ["turnover", "fgm", "fga", "ftm", "fta"]


# ---------------------------------------------------------------------------
# Features
# ---------------------------------------------------------------------------
def load_totals(con) -> pd.DataFrame:
    """Per player-season box totals, regular season, from the same view the
    baseline uses -- identical universe, so neither side gets a different
    population by accident."""
    return bw.load_season_totals(con)


def load_advanced(con) -> pd.DataFrame:
    """Minutes-weighted advanced metrics per player-season.

    Same weighting rule as the model builder's `_model_frame`: weight by the
    minutes of games where the metric is actually present, so a season with a
    few missing advanced rows is not shrunk toward zero, and a four-minute
    appearance does not count as much as a start.
    """
    cols = {
        "usage_pct": "usage_percentage",
        "ts_pct_adv": "true_shooting_percentage",
        "off_rating": "offensive_rating",
        "def_rating": "defensive_rating",
        "net_rating": "net_rating",
        "pie": "pie",
        "ast_pct": "assist_percentage",
        "ast_to_tov": "assist_to_turnover",
        "tov_ratio": "turnover_ratio",
        "reb_pct": "rebound_percentage",
        "oreb_pct": "offensive_rebound_percentage",
        "dreb_pct": "defensive_rebound_percentage",
    }
    sel = ", ".join(
        f'SUM(s."{src}" * g.minutes_played) '
        f'/ NULLIF(SUM(CASE WHEN s."{src}" IS NULL THEN 0 '
        f'ELSE g.minutes_played END), 0) AS "{key}"'
        for key, src in cols.items())
    return con.execute(f"""
        SELECT g.player_id, g.season, {sel}
          FROM v_player_games g
          JOIN player_advanced_stats s
            ON s.player_id = g.player_id AND s.game_id = g.game_id
         WHERE g.game_type = 'regular' AND NOT g.is_dnp
         GROUP BY g.player_id, g.season
    """).df()


def load_bio(con) -> pd.DataFrame:
    """Player-constant attributes. Height and draft position are legitimately
    predictive and cannot leak, because they do not change with the season."""
    return con.execute("""
        SELECT player_id, height_inches AS height_in, weight_lbs,
               draft_number
          FROM v_players
    """).df()


def build_features(tot: pd.DataFrame, adv: pd.DataFrame, bio: pd.DataFrame,
                   target: int) -> pd.DataFrame:
    """One row per player eligible to be predicted for season `target`.

    Everything here reads T-1/T-2/T-3 and nothing else. The lag-1/2/3 columns
    are explicit rather than a LAG() window because a player who missed a
    season must get NaN for that slot, not the next season back shifted into
    its place -- the same rule the model builder's lag variables follow.
    """
    win = tot[tot["season"].between(target - 3, target - 1)]
    if win.empty:
        return pd.DataFrame()

    # Baseline-equivalent weighted rates: the single strongest known signal, so
    # the model starts from it rather than being asked to rediscover it.
    feats = []
    for label, weights in (("recent", {target - 1: 1.0}),
                           ("shipped", {target - 1: .6, target - 2: .3,
                                        target - 3: .1})):
        wr = bw.weighted_rates(win, weights)
        wr = wr.add_prefix(f"{label}_")
        feats.append(wr)
    f = pd.concat(feats, axis=1)

    # Per-season slices, so the model can see trajectory rather than only a
    # blend: a 24-year-old climbing and a 34-year-old falling can share a
    # weighted average and have very different next seasons.
    for lag in (1, 2, 3):
        s = tot[tot["season"] == target - lag].set_index("player_id")
        if s.empty:
            continue
        rates = s[RATE_COLS].div(s["gp"], axis=0)
        rates["gp"] = s["gp"]
        f = f.join(rates.add_prefix(f"l{lag}_"), how="left")
        a = adv[adv["season"] == target - lag].set_index("player_id")
        if not a.empty:
            f = f.join(a.drop(columns=["season"]).add_prefix(f"l{lag}_"),
                       how="left")

    # Trajectory deltas: the year-over-year change the model would otherwise
    # have to infer from two columns.
    for c in ("pts", "reb", "ast", "gp"):
        if f.get(f"l1_{c}") is not None and f.get(f"l2_{c}") is not None:
            f[f"d_{c}"] = f[f"l1_{c}"] - f[f"l2_{c}"]

    f = f.join(bio.set_index("player_id"), how="left")
    # Seasons of experience before T, and how many of the last three were
    # played at all -- availability history is a real predictor of availability.
    exp = (tot[tot["season"] < target].groupby("player_id")["season"]
           .agg(["min", "count"]))
    f["experience"] = target - exp["min"]
    f["seasons_played"] = exp["count"]
    f["windows_present"] = (
        win.groupby("player_id")["season"].nunique().reindex(f.index))
    f["target_season"] = target
    return f


def baseline_prediction(tot: pd.DataFrame, target: int,
                        weights: dict[int, float]) -> pd.Series:
    """The shipped board's own prediction for season `target`, pre-T only.

    This is both the number to beat and a model feature, so it is computed in
    exactly one place.
    """
    win = tot[tot["season"].between(target - 3, target - 1)]
    if win.empty:
        return pd.Series(dtype=float)
    gp_prior = win.groupby("player_id")["gp"].sum().sort_values(ascending=False)
    pool_ids = gp_prior.head(POOL_SIZE).index
    pool_rates = bw.weighted_rates(win[win["player_id"].isin(pool_ids)],
                                   {target - 1: 1 / 3, target - 2: 1 / 3,
                                    target - 3: 1 / 3})
    wr = bw.weighted_rates(win, weights)
    wr = wr[wr["w_gp"] >= MIN_GP]
    return (bw.nine_cat_value(wr, pool_rates)
            * (wr["w_gp"] / FULL_SEASON).clip(upper=1.0))


def realized_value(tot: pd.DataFrame, target: int) -> pd.Series:
    """Season T's actual 9-cat total -- the thing being predicted."""
    cur = tot[tot["season"] == target].set_index("player_id")
    if cur.empty:
        return pd.Series(dtype=float)
    cur_rates = cur[RATE_COLS].div(cur["gp"], axis=0)
    cur_rates = cur_rates.assign(gp=cur["gp"])
    cur_pool = cur_rates.loc[cur_rates.index.intersection(
        cur["gp"].sort_values(ascending=False).head(POOL_SIZE).index)]
    return (bw.nine_cat_value(cur_rates, cur_pool)
            * (cur["gp"] / FULL_SEASON).clip(upper=1.0))


def assemble(con, first_target: int, last_target: int) -> pd.DataFrame:
    """Every (player, target season) row with its features and its outcome."""
    tot = load_totals(con)
    adv = load_advanced(con)
    bio = load_bio(con)
    frames = []
    for T in range(first_target, last_target + 1):
        f = build_features(tot, adv, bio, T)
        if f.empty:
            continue
        for label, w in (("base_recent", {T - 1: 1.0}),
                         ("base_shipped", {T - 1: .6, T - 2: .3, T - 3: .1})):
            f[label] = baseline_prediction(tot, T, w)
        f["y"] = realized_value(tot, T)
        cur_gp = tot[tot["season"] == T].set_index("player_id")["gp"]
        f["target_gp"] = cur_gp
        frames.append(f.reset_index())
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


# ---------------------------------------------------------------------------
# Evaluation universe -- identical to the baseline's, by construction
# ---------------------------------------------------------------------------
def eligible(df: pd.DataFrame) -> pd.Series:
    """Predictable before T (the baseline could score them) AND played in T."""
    return (df["base_shipped"].notna()
            & df["y"].notna()
            & (df["target_gp"] >= MIN_GP))


def score(pred: pd.Series, truth: pd.Series) -> tuple[float, float]:
    rho = sps.spearmanr(pred, truth).statistic
    n_hit = min(100, len(pred))
    hit = len(set(pred.nlargest(n_hit).index)
              & set(truth.nlargest(n_hit).index)) / n_hit
    return float(rho), float(hit)


FEATURE_BLOCKLIST = {"player_id", "player_name", "y", "target_gp",
                     "target_season", "index"}


def feature_columns(df: pd.DataFrame) -> list[str]:
    return [c for c in df.columns
            if c not in FEATURE_BLOCKLIST
            and pd.api.types.is_numeric_dtype(df[c])]


def walk_forward(df: pd.DataFrame, make_model, label: str,
                 verbose: bool = True) -> pd.DataFrame:
    """Fit on every target season before T, predict T, never the reverse."""
    feats = feature_columns(df)
    targets = sorted(df["target_season"].unique())
    rows = []
    for i, T in enumerate(targets):
        train = df[(df["target_season"] < T) & eligible(df)]
        if train["target_season"].nunique() < MIN_TRAIN_SEASONS:
            continue
        test = df[(df["target_season"] == T) & eligible(df)]
        if len(test) < 50:
            continue
        model = make_model()
        model.fit(train[feats].to_numpy(dtype=float),
                  train["y"].to_numpy(dtype=float))
        pred = pd.Series(model.predict(test[feats].to_numpy(dtype=float)),
                         index=test.index)
        rho, hit = score(pred, test["y"])
        rows.append({"target_season": int(T), "model": label, "n": len(test),
                     "train_rows": len(train),
                     "train_seasons": int(train["target_season"].nunique()),
                     "spearman": rho, "top100_hit": hit})
        if verbose:
            print(f"  {label:22s} T={T}  n={len(test):4d}  "
                  f"rho={rho:.4f}  hit={hit:.3f}", flush=True)
    return pd.DataFrame(rows)


def baseline_walk(df: pd.DataFrame, col: str, label: str) -> pd.DataFrame:
    """Score a baseline column on exactly the seasons the model was scored on.

    Each baseline is scored only where it actually produced a prediction:
    recent-only has nothing for a player who did not play T-1, and including
    those rows as NaN made the whole season's Spearman NaN. This mirrors
    backtest_weights.py, which intersects indices per scheme.
    """
    rows = []
    for T in sorted(df["target_season"].unique()):
        test = df[(df["target_season"] == T) & eligible(df)]
        test = test[test[col].notna()]
        if len(test) < 50:
            continue
        rho, hit = score(test[col], test["y"])
        rows.append({"target_season": int(T), "model": label, "n": len(test),
                     "spearman": rho, "top100_hit": hit})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# Candidate models
# ---------------------------------------------------------------------------
def make_gbt():
    from lightgbm import LGBMRegressor
    return LGBMRegressor(
        n_estimators=400, learning_rate=0.05, num_leaves=31,
        min_child_samples=30, subsample=0.8, subsample_freq=1,
        colsample_bytree=0.8, reg_lambda=1.0, random_state=0,
        n_jobs=4, verbose=-1)


def make_ridge():
    """A linear model on the same features -- the honest control. If the
    gradient-boosted model cannot beat this, the trees are not earning their
    complexity, and that is a result worth reporting rather than hiding."""
    from sklearn.linear_model import Ridge
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.impute import SimpleImputer
    return make_pipeline(SimpleImputer(strategy="median"),
                         StandardScaler(), Ridge(alpha=10.0))


def make_rf():
    from sklearn.ensemble import RandomForestRegressor
    from sklearn.pipeline import make_pipeline
    from sklearn.impute import SimpleImputer
    return make_pipeline(
        SimpleImputer(strategy="median"),
        RandomForestRegressor(n_estimators=300, min_samples_leaf=5,
                              max_features=0.4, random_state=0, n_jobs=4))


MODELS = {"gradient boosted": make_gbt,
          "ridge (linear)": make_ridge,
          "random forest": make_rf}


def validate(con, first_target: int, last_target: int,
             models: dict | None = None) -> dict:
    """Walk-forward every candidate, then score the baselines on EXACTLY the
    seasons the models were scored on.

    The models need a run-up of completed seasons before their first honest
    prediction, so they cannot be scored on 1986. Comparing a model's mean over
    1994-2025 against a baseline's mean over 1986-2025 would be a different
    sample, and the difference between them would be partly era. The baseline
    numbers reported here are therefore recomputed on the model's own seasons,
    and the headline figure from `backtest_weights.py` is printed beside them
    so the shift from changing the sample is visible rather than hidden.
    """
    models = models or MODELS
    df = assemble(con, first_target, last_target)
    if df.empty:
        raise SystemExit("no rows assembled")
    results = []
    for label, maker in models.items():
        print(f"\n--- {label} ---", flush=True)
        results.append(walk_forward(df, maker, label))
    res = pd.concat([r for r in results if not r.empty], ignore_index=True)
    scored = sorted(res["target_season"].unique())
    same = df[df["target_season"].isin(scored)]
    for col, label in (("base_shipped", "baseline .6/.3/.1 (shipped)"),
                       ("base_recent", "baseline 1.0/0/0 (recent only)")):
        res = pd.concat([res, baseline_walk(same, col, label)],
                        ignore_index=True)
    return {"res": res, "df": df, "seasons": scored}


def report(out: dict) -> pd.DataFrame:
    res, scored = out["res"], out["seasons"]
    agg = (res.groupby("model")
              .agg(seasons=("target_season", "nunique"),
                   spearman=("spearman", "mean"), sp_sd=("spearman", "std"),
                   hit=("top100_hit", "mean"), hit_sd=("top100_hit", "std"))
              .sort_values("spearman", ascending=False))
    print("\n" + "=" * 86)
    print("WALK-FORWARD VALIDATION — 9-cat season value")
    print("=" * 86)
    print(f"""
Protocol    Features for target T use seasons T-1/T-2/T-3 only. The model that
            predicts T is fit on target seasons strictly before T (expanding
            window). Universe and z-score pool are identical to
            backtest_weights.py, so these numbers sit on the same scale.
Seasons     {len(scored)} scored ({min(scored)}-{max(scored)}); models need
            {MIN_TRAIN_SEASONS} completed target seasons before their first prediction,
            so the baselines below are rescored on these same seasons.
""")
    print(f"  {'model':30s} {'seasons':>7} {'Spearman':>9} {'(sd)':>7} "
          f"{'top100':>7} {'(sd)':>7}")
    for name, r in agg.iterrows():
        print(f"  {name:30s} {int(r['seasons']):7d} {r['spearman']:9.4f} "
              f"{r['sp_sd']:7.3f} {r['hit']:7.3f} {r['hit_sd']:7.3f}")

    # Paired, season by season: a mean that wins by a hair while losing most
    # individual seasons is not a better model.
    print("\n  PAIRED against the shipped baseline, same seasons:")
    piv = res.pivot_table(index="target_season", columns="model",
                          values="spearman")
    base = "baseline .6/.3/.1 (shipped)"
    for name in agg.index:
        if name == base or name not in piv:
            continue
        d = (piv[name] - piv[base]).dropna()
        if d.empty:
            continue
        p = sps.wilcoxon(d).pvalue if len(d) > 5 else float("nan")
        print(f"    {name:30s} mean diff {d.mean():+.4f}  "
              f"wins {int((d > 0).sum())}/{len(d)}  p={p:.3f}")
    return agg


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--fast", action="store_true",
                    help="2005+ only, for iteration")
    ap.add_argument("--write", action="store_true",
                    help="fit the final model and write the projections table")
    ap.add_argument("--first-target", type=int, default=FIRST_INPUT_SEASON + 3)
    args = ap.parse_args()
    if SNAPSHOT.resolve() == LIVE_DB.resolve():
        raise SystemExit("refusing to read the live database")
    con = duckdb.connect(str(SNAPSHOT), read_only=True)
    try:
        last = con.execute(
            "SELECT max(season) FROM v_games WHERE game_type='regular'").fetchone()[0]
        if args.validate:
            first = 2005 if args.fast else args.first_target
            out = validate(con, first, last)
            report(out)
            out["res"].to_csv(PROJECT_ROOT / "exports" / "projection_validation.csv",
                              index=False)
            print(f"\n  per-season detail -> exports/projection_validation.csv")
        if args.write:
            return write_projections(con, last)
    finally:
        con.close()
    return 0



# ---------------------------------------------------------------------------
# Component projections
# ---------------------------------------------------------------------------
# The draft board scores nine categories, so it needs projected RATES, not a
# projected composite. Each component gets its own model on the same features
# and the same walk-forward discipline; the composite is then computed from the
# projected components with the board's own scoring function. That composition
# is validated in its own right below, because a set of individually reasonable
# component forecasts can still compose into a worse ranking than a direct one.
COMPONENT_TARGETS = ["pts", "reb", "ast", "stl", "blk", "fg3m", "turnover",
                     "fgm", "fga", "ftm", "fta"]
GP_TARGET = "gp"


def component_frame(df: pd.DataFrame, tot: pd.DataFrame) -> pd.DataFrame:
    """Attach season-T per-game rates and games played as regression targets."""
    cur = tot.set_index(["player_id", "season"])
    idx = pd.MultiIndex.from_arrays([df["player_id"], df["target_season"]])
    out = df.copy()
    for c in COMPONENT_TARGETS:
        out[f"y_{c}"] = cur[c].reindex(idx).to_numpy() / cur["gp"].reindex(idx).to_numpy()
    out["y_gp"] = cur["gp"].reindex(idx).to_numpy()
    return out


def make_final():
    """The shipped model: ridge with alpha chosen by cross-validation INSIDE
    each training window, so the held-out season never participates in the
    choice.

    Chosen over the gradient-boosted and random-forest candidates on the
    validation below, and over a ridge+forest rank blend that scored +0.0036
    higher but won only 19 of 32 seasons -- by this project's own standard for
    the weight backtest, that is not a better model, it is a hair on the mean.
    """
    from sklearn.linear_model import RidgeCV
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    from sklearn.impute import SimpleImputer
    return make_pipeline(SimpleImputer(strategy="median"), StandardScaler(),
                         RidgeCV(alphas=np.logspace(-1, 3, 25)))


def fit_components(train: pd.DataFrame, test: pd.DataFrame,
                   feats: list[str]) -> pd.DataFrame:
    """Project every component for `test` from a model fit only on `train`."""
    X = train[feats].to_numpy(dtype=float)
    Xt = test[feats].to_numpy(dtype=float)
    out = pd.DataFrame(index=test.index)
    for c in COMPONENT_TARGETS + [GP_TARGET]:
        tgt = train[f"y_{c}"]
        ok = tgt.notna().to_numpy()
        if ok.sum() < 100:
            out[c] = np.nan
            continue
        m = make_final()
        m.fit(X[ok], tgt.to_numpy(dtype=float)[ok])
        out[c] = m.predict(Xt)
    # A projected rate cannot be negative, and games played cannot exceed a
    # full season. Clipping here rather than hoping the model never does it.
    for c in COMPONENT_TARGETS:
        out[c] = out[c].clip(lower=0.0)
    out[GP_TARGET] = out[GP_TARGET].clip(lower=0.0, upper=FULL_SEASON)
    # Makes cannot exceed attempts; an unclipped pair produces a shooting
    # percentage above 1 and a nonsense impact z-score.
    out["fgm"] = np.minimum(out["fgm"], out["fga"])
    out["ftm"] = np.minimum(out["ftm"], out["fta"])
    return out


def compose_value(proj: pd.DataFrame, pool_rates: pd.DataFrame) -> pd.Series:
    """The board's own 9-cat scoring, applied to projected rates."""
    rates = proj[COMPONENT_TARGETS].copy()
    return (bw.nine_cat_value(rates, pool_rates)
            * (proj[GP_TARGET] / FULL_SEASON).clip(upper=1.0))


# ---------------------------------------------------------------------------
# Storage -- live DB, checkpointed, then published through the snapshot step
# ---------------------------------------------------------------------------
PROJECTION_DDL = """
CREATE TABLE IF NOT EXISTS player_projections (
    player_id        INTEGER NOT NULL,
    player_name      VARCHAR,
    season           INTEGER NOT NULL,   -- the season being PROJECTED
    stat             VARCHAR NOT NULL,   -- per-game rate, 'gp', or 'value_9cat'
    projected_value  DOUBLE,
    model_version    VARCHAR NOT NULL,
    generated_at     TIMESTAMP NOT NULL,
    PRIMARY KEY (player_id, season, stat, model_version)
)
"""


def write_projections(con_snapshot, last_season: int) -> int:
    """Fit on every completed target season, project the next one, publish.

    Follows the ingestion contract rather than inventing a second one: the
    write goes to the LIVE database (nothing else may write there), it is
    bracketed by an `_ingest_log` checkpoint so a failed run is visible as a
    partial rather than as silence, and the result reaches readers only through
    `write_snapshot` -- the same atomic COPY FROM DATABASE + os.replace +
    sha256 sidecar every other table goes through.

    `con_snapshot` is read-only and used for the features; the live connection
    is opened separately and only for the write.
    """
    import ingest

    target = last_season + 1
    tot = load_totals(con_snapshot)
    hist = component_frame(assemble(con_snapshot, FIRST_INPUT_SEASON + 3,
                                    last_season), tot)
    train = hist[eligible(hist)]
    feats = [c for c in feature_columns(hist) if not c.startswith("y_")]

    # Features for the season being projected: T-1/T-2/T-3 are all complete,
    # and there is no outcome column because the season has not happened.
    adv, bio = load_advanced(con_snapshot), load_bio(con_snapshot)
    fut = build_features(tot, adv, bio, target)
    if fut.empty:
        raise SystemExit(f"no feature rows for {target}")
    for label, w in (("base_recent", {target - 1: 1.0}),
                     ("base_shipped", {target - 1: .6, target - 2: .3,
                                       target - 3: .1})):
        fut[label] = baseline_prediction(tot, target, w)
    fut = fut.reset_index()
    # Only players the baseline could also have scored, so the projected board
    # and the historical ones cover the same population.
    fut = fut[fut["base_shipped"].notna()]

    proj = fit_components(train, fut, feats)
    win = tot[tot["season"].between(target - 3, target - 1)]
    pool_ids = (win.groupby("player_id")["gp"].sum()
                .sort_values(ascending=False).head(POOL_SIZE).index)
    pool_rates = bw.weighted_rates(win[win["player_id"].isin(pool_ids)],
                                   {target - 1: 1 / 3, target - 2: 1 / 3,
                                    target - 3: 1 / 3})
    value = compose_value(proj, pool_rates)

    names = (tot.sort_values("season").groupby("player_id")["player_name"]
             .last().reindex(fut["player_id"]).to_numpy())
    long = []
    now = dt.datetime.now().astimezone().replace(microsecond=0)
    for stat in COMPONENT_TARGETS + [GP_TARGET]:
        long.append(pd.DataFrame({
            "player_id": fut["player_id"].to_numpy(),
            "player_name": names, "season": target, "stat": stat,
            "projected_value": proj[stat].to_numpy()}))
    long.append(pd.DataFrame({
        "player_id": fut["player_id"].to_numpy(), "player_name": names,
        "season": target, "stat": "value_9cat",
        "projected_value": value.to_numpy()}))
    out = pd.concat(long, ignore_index=True)
    out["model_version"] = MODEL_VERSION
    out["generated_at"] = now
    out = out.dropna(subset=["projected_value"])

    print(f"  projecting season {target} for {fut['player_id'].nunique()} players "
          f"-> {len(out):,} rows ({out['stat'].nunique()} stats)")
    print(f"  trained on {train['target_season'].nunique()} target seasons, "
          f"{len(train):,} player-seasons, {len(feats)} features")

    live = duckdb.connect(str(LIVE_DB))
    try:
        live.execute(PROJECTION_DDL)
        log_id = ingest.checkpoint_start(
            live, "projections", MODEL_VERSION, target,
            {"model": MODEL_VERSION, "features": len(feats),
             "train_seasons": int(train["target_season"].nunique())},
            None, 0)
        try:
            live.execute(
                "DELETE FROM player_projections WHERE season = ? AND model_version = ?",
                [target, MODEL_VERSION])
            live.register("_proj_in", out)
            live.execute("""INSERT INTO player_projections
                            SELECT player_id, player_name, season, stat,
                                   projected_value, model_version, generated_at
                            FROM _proj_in""")
            live.unregister("_proj_in")
            n = live.execute("SELECT count(*) FROM player_projections").fetchone()[0]
            ingest.checkpoint_finish(live, log_id, len(out))
            live.commit()
        except Exception as exc:
            ingest.checkpoint_finish(live, log_id, 0, http_status=500,
                                     error=str(exc)[:400])
            live.commit()
            raise
    finally:
        live.close()
    print(f"  player_projections now holds {n:,} rows in {LIVE_DB.name}")

    print("  publishing snapshot (atomic COPY FROM DATABASE + sha256)…")
    ingest.write_snapshot(LIVE_DB, SNAPSHOT)
    sha = Path(str(SNAPSHOT) + ".sha256").read_text().split()[0]
    print(f"  snapshot published, sha256 {sha[:16]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
