"""Does the lineup optimiser help across many weeks, or was one week noise?

Head-to-head on real box scores: for every Monday-start week of season 2025,
pick a lineup three ways from projections that could not see 2025, then score
each pair against what those players actually did. Reports how often each
objective wins more categories than the other.
"""
import duckdb, pandas as pd, numpy as np, itertools, random
import projections as P, matchup as M

TARGET = 2025
con = duckdb.connect("nba_snapshot.duckdb", read_only=True)
league = M.load_league(M.latest_cache())

tot = P.load_totals(con); adv = P.load_advanced(con); bio = P.load_bio(con)
hist = P.component_frame(P.assemble(con, P.FIRST_INPUT_SEASON + 3, TARGET - 1), tot)
train = hist[P.eligible(hist)]
feats = [c for c in P.feature_columns(hist) if not c.startswith("y_")]
fut = P.build_features(tot, adv, bio, TARGET)
for lab, w in (("base_recent", {TARGET-1: 1.0}),
               ("base_shipped", {TARGET-1: .6, TARGET-2: .3, TARGET-3: .1})):
    fut[lab] = P.baseline_prediction(tot, TARGET, w)
fut = fut.reset_index(); fut = fut[fut["base_shipped"].notna()]
comp = P.fit_components(train, fut, feats); comp.index = fut["player_id"].to_numpy()
print(f"projections for {TARGET} fit on seasons <= {TARGET-1} ({len(train):,} rows)")

dates = [str(r[0]) for r in con.execute(
    f"SELECT DISTINCT game_date FROM v_games WHERE game_type='regular' "
    f"AND season={TARGET} ORDER BY game_date").fetchall()]
mondays = sorted({str(pd.Timestamp(d) - pd.Timedelta(days=pd.Timestamp(d).weekday()))[:10]
                  for d in dates})
name2bdl = {p["name"]: p["bdl_id"] for p in league["roster"]}
actual_ids = [p["bdl_id"] for p in league["roster"] if p["starting"]]

def cats_won(ids_a, ids_b, start):
    a = M.week_totals(con, [i for i in ids_a if i], start)
    b = M.week_totals(con, [i for i in ids_b if i], start)
    if not a or not b: return None
    wa = 0.0
    for cat in M.ALL_CATS:
        x, y = a.get(cat, 0), b.get(cat, 0)
        if cat in M.INVERTED: wa += 1.0 if x < y else (0.5 if x == y else 0.0)
        else:                 wa += 1.0 if x > y else (0.5 if x == y else 0.0)
    return wa

rows = []
for start in mondays:
    if not M.games_in_window(con, [i for i in actual_ids if i], start):
        continue
    # opponent proxy for the category objective: the roster's own projected
    # median week, so the objective has a target to beat without peeking.
    opp_proxy = M.projected_week(
        comp, [p["bdl_id"] for p in league["roster"] if p["bdl_id"]],
        {k: v for k, v in M.games_in_window(
            con, [p["bdl_id"] for p in league["roster"] if p["bdl_id"]], start).items()})
    opp_proxy = {k: v * 0.66 for k, v in opp_proxy.items() if k in M.ALL_CATS}
    prod = M.optimise_lineup(con, league, comp, start)
    cats = M.optimise_lineup_categories(con, league, comp, opp_proxy, start)
    pid = [a.get("bdl_id") for a in prod["slots"] if a.get("bdl_id")]
    cid = [a.get("bdl_id") for a in cats["slots"] if a.get("bdl_id")]
    r = {"week": start}
    r["prod_vs_actual"] = cats_won(pid, actual_ids, start)
    r["cats_vs_actual"] = cats_won(cid, actual_ids, start)
    r["cats_vs_prod"]   = cats_won(cid, pid, start)
    rows.append(r)

df = pd.DataFrame(rows).dropna()
print(f"\n{len(df)} weeks scored\n")
for col, lab in (("prod_vs_actual", "production objective vs lineup-as-set"),
                 ("cats_vs_actual", "category objective   vs lineup-as-set"),
                 ("cats_vs_prod",   "category objective   vs production obj")):
    v = df[col]
    print(f"  {lab}: mean {v.mean():.2f}/9 cats   "
          f"wins {(v>4.5).sum()}/{len(v)} weeks   ties {(v==4.5).sum()}")
df.to_csv("exports/lineup_multiweek.csv", index=False)
print("\n -> exports/lineup_multiweek.csv")
con.close()
