#!/usr/bin/env python3
"""Draft assistant for "The Bubble 2.0": marginal value to MY roster. READ-ONLY.

=============================================================================
MARGINAL-VALUE METHOD -- stated before the code, per the project's own rule
=============================================================================
draft_board.py scores a player in a vacuum: a sum of nine category z-scores.
That is the wrong quantity for a pick. In a 9-cat H2H league you win a category
by BEATING ONE OPPONENT in it, not by maximising it, so the value of more
production in a category depends on what I already have. Three adjustments:

1. CATEGORY BALANCE (diminishing returns).
   For each category c, my roster's strength is the sum of my players' z-scores
   in c. Dividing by roster size gives s(c), my average z per player in c -- a
   league-average roster sits at s(c) = 0 by construction, since the z-scores
   are normalised against the draftable pool.
       weight(c) = 1 / (1 + max(0, s(c)) / SATURATION)
   At or below average in c, the weight is 1.0 (full credit). At SATURATION
   above average, it is 0.5. It never reaches zero, because a category is never
   worthless -- a lead can still be lost. Being BELOW average is not rewarded
   with a weight above 1: that would chase lost causes, which is the opposite of
   what punting implies.

2. POSITION FIT against my remaining slots.
   A player who only fills a slot I have already filled is worth less than one
   who fills an empty starting slot. Multiplier: 1.0 for an empty starting slot,
   BENCH_MULT for bench/UT only, 0 once the roster is full.

3. URGENCY from the fixed snake order.
   I know exactly how many picks pass before my next turn. A player who will
   still be there is worth less NOW than an equal player who will not. Estimated
   survival is driven by ESPN rank, since that is how the other teams pick:
       P(survives) = P(fewer than `gap` teams take him first)
   approximated from his rank relative to the number of picks in between.
   Urgency = 1 - P(survives), applied as a tie-breaker weight, NOT as a
   multiplier on value -- a scarce mediocre player must not outrank a good one.

VALIDATION RESULT -- READ THIS BEFORE TRUSTING THE RANKING.
Out-of-sample (draft on T-3..T-1, score on season T actuals, 70 paired mock
drafts across 2021-2025) this assistant does NOT beat simply drafting straight
down draft_board.py's total_value: 0.604 category win share against 0.621,
finishing 3.3rd of 12 against 2.6th, winning 29 of 70 drafts, p = 0.105. That is
statistically indistinguishable from the simple board, with a point estimate
slightly WORSE.

Component ablation showed why: position fit is neutral (+0.002) and urgency is
exactly neutral (0.000); the category-balance weighting was doing all the damage
(-0.080 at its original setting). So use this as an ADVISORY view -- the category
weights and the marginal-value column are informative for a human deciding
between close picks -- and do not let it auto-pick over board order.

ASSUMPTIONS, all of which can be wrong:
  * Weekly category totals are approximated by summing per-game z-scores across
    the roster. This ignores games-per-week differences and the weekly schedule
    entirely, which Section 8 says the real strategy logic needs.
  * The opponent is assumed to be league-average (z = 0 in every category). No
    specific-opponent modelling, which is again what Section 8 describes.
  * The other 11 teams draft by ESPN rank plus noise. If they punt, trade, or
    reach for need, the urgency term is wrong.
  * Punting is EMERGENT, not chosen: the weights fall out of what I have, so the
    assistant will drift away from a category it is losing rather than commit to
    abandoning it deliberately.
  * Rookies with no model value cannot be valued at all, so the assistant will
    never recommend one even when ESPN ranks them highly.
  * Category z-scores are additive across a roster. Real category totals are
    not: FG% and FT% are ratios and do not sum, so their treatment here is an
    approximation that gets worse as a roster's attempt volume gets lopsided.
=============================================================================

    python draft_assistant.py --simulate 200
    python draft_assistant.py --picks picks.csv        # live/manual mode
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import numpy as np
import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent
BOARD_CSV = PROJECT_ROOT / "draft_board.csv"
KEEPERS = PROJECT_ROOT / "keepers.csv"

MY_TEAM_ID = 10
N_TEAMS = 12
N_ROUNDS = 14
ROUND1_ORDER = [10, 1, 7, 6, 5, 3, 4, 11, 12, 8, 2, 9]
STARTING_SLOTS = {"PG": 1, "SG": 1, "SF": 1, "PF": 1, "C": 1, "G": 1, "F": 1, "UT": 3}
BENCH = 4
CATS = ["pts", "reb", "ast", "stl", "blk", "fg3m", "fgpct", "ftpct", "turnover"]
ZCOL = {c: f"z_{c}" for c in CATS}

# Saturation is the average z-per-player above league average at which a
# category's weight halves. SET BY MEASUREMENT, NOT TASTE: the first value tried
# here was 0.40, which the out-of-sample validation showed costs 8 points of
# category win share (0.527 vs 0.609 for drafting straight down the board). The
# harm is monotone in aggressiveness -- 0.2 costs 17 points, 1.0 costs 5, 2.0
# costs 2 -- and only a near-neutral 4.0 breaks even or marginally helps. See
# validate_assistant.py; the sweep is in the report.
SATURATION = 4.0
BENCH_MULT = 0.85      # value retained when a player fits only bench/UT
URGENCY_W = 0.15       # how much urgency can shift the ranking


# =============================================================================
# Pure functions
# =============================================================================
def snake_order(round1: list[int], rounds: int) -> list[int]:
    """Full pick order. Verified against ESPN's own pick array: round 2 is the
    exact reverse of round 1."""
    out = []
    for r in range(rounds):
        out.extend(round1 if r % 2 == 0 else list(reversed(round1)))
    return out


def picks_until_next(order: list[int], team: int, next_index: int) -> int:
    """How many picks by other teams before `team` picks again."""
    for i in range(next_index, len(order)):
        if order[i] == team:
            return i - next_index
    return len(order) - next_index


def roster_strength(roster: pd.DataFrame) -> dict[str, float]:
    """Summed category z-scores for a roster."""
    return {c: float(roster[ZCOL[c]].sum()) if len(roster) else 0.0 for c in CATS}


def category_weights(strength: dict[str, float], n: int,
                     saturation: float = SATURATION) -> dict[str, float]:
    """Diminishing returns on categories I am already strong in.

    Never exceeds 1.0 and never reaches 0: chasing a category I am losing badly
    is as wrong as piling onto one I have already won.
    """
    if n <= 0:
        return {c: 1.0 for c in CATS}
    w = {}
    for c in CATS:
        s = strength[c] / n
        w[c] = 1.0 / (1.0 + max(0.0, s) / saturation)
    return w


def open_slots(roster: pd.DataFrame) -> dict[str, int]:
    """Which starting slots remain, filling greedily by scarcity.

    Approximate by design: true slot assignment is a bipartite matching problem
    and this is a greedy stand-in. It assigns each rostered player to the
    scarcest slot he is eligible for.
    """
    remaining = dict(STARTING_SLOTS)
    if not len(roster):
        return remaining
    order = ["C", "PG", "SG", "SF", "PF", "G", "F", "UT"]
    for _, p in roster.iterrows():
        elig = [s for s in order if s in p["positions"] and remaining.get(s, 0) > 0]
        if elig:
            remaining[elig[0]] -= 1
        elif remaining.get("UT", 0) > 0:
            remaining["UT"] -= 1
    return remaining


def slot_multiplier(positions: list[str], roster: pd.DataFrame) -> float:
    """1.0 if the player fills an empty starting slot, BENCH_MULT if only bench."""
    if len(roster) >= sum(STARTING_SLOTS.values()) + BENCH:
        return 0.0
    rem = open_slots(roster)
    if any(rem.get(s, 0) > 0 for s in positions):
        return 1.0
    if rem.get("UT", 0) > 0:
        return 1.0
    return BENCH_MULT


def survival_probability(espn_rank: float, gap: int, sigma: float = 12.0) -> float:
    """Chance a player is still available after `gap` opponent picks.

    The other teams pick by ESPN rank plus noise, so a player ranked well inside
    the next `gap` picks is very likely gone. Modelled as a logistic in the
    distance between his rank and the number of picks remaining.
    """
    if gap <= 0 or pd.isna(espn_rank):
        return 1.0
    return float(1.0 / (1.0 + np.exp(-(espn_rank - gap) / sigma)))


def marginal_value(row: pd.Series, weights: dict[str, float],
                   slot_mult: float) -> float:
    """Weighted category contribution x availability x position fit.

    Availability must be in here. Without it the weighted z-sum is a per-game
    quality score and a 46-game player outranks a healthy one -- Anthony Davis
    jumped from board #11 to #7 on the first version of this function for exactly
    that reason.
    """
    avail = row.get("availability", 1.0)
    if pd.isna(avail):
        avail = 1.0
    return (float(sum(row[ZCOL[c]] * weights[c] for c in CATS))
            * float(avail) * slot_mult)


def rank_available(available: pd.DataFrame, roster: pd.DataFrame,
                   gap: int, urgency_w: float = URGENCY_W) -> pd.DataFrame:
    """Rank what is left by marginal value to MY roster."""
    w = category_weights(roster_strength(roster), len(roster))
    out = available.copy()
    sm = [slot_multiplier(p, roster) for p in out["positions"]]
    out["slot_mult"] = sm
    out["marginal_value"] = [marginal_value(r, w, m)
                             for (_, r), m in zip(out.iterrows(), sm)]
    out["p_survives"] = [survival_probability(r, gap) for r in out["draft_rank"]]
    out["urgency"] = 1.0 - out["p_survives"]
    # Urgency shifts the ranking, it does not scale the value: a scarce weak
    # player must never outrank a strong one.
    out["pick_score"] = out["marginal_value"] + urgency_w * out["urgency"] * \
        out["marginal_value"].abs().mean()
    return out.sort_values("pick_score", ascending=False), w


# =============================================================================
# Draft simulation
# =============================================================================
def fetch_live_picks() -> tuple[pd.DataFrame, str]:
    """Read picks already made from ESPN's raw draft endpoint.

    espn_api exposes NOTHING usable for basketball drafts: League has no draft
    method (box_scores, free_agents, player_info, recent_activity, scoreboard,
    standings, transactions), and League.draft is an empty list even though the
    basketball League calls the base _fetch_draft(). The raw mDraftDetail view is
    the only working path.

    TRAP, verified: an unmade pick carries playerId = -1, not null and not
    absent. Testing truthiness counts all 168 slots as made, because -1 is
    truthy. The test must be playerId > 0.
    """
    import requests
    from dotenv import load_dotenv
    load_dotenv(PROJECT_ROOT / ".env")
    swid, s2 = os.environ.get("ESPN_SWID"), os.environ.get("ESPN_S2")
    if not (swid and s2):
        return pd.DataFrame(), "no ESPN credentials in .env"
    swid = swid if swid.startswith("{") else "{" + swid + "}"
    try:
        r = requests.get(
            "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons/2027"
            "/segments/0/leagues/1127755466",
            params={"view": "mDraftDetail"},
            cookies={"SWID": swid, "espn_s2": s2},
            headers={"User-Agent": "Mozilla/5.0"}, timeout=30)
        r.raise_for_status()
        detail = r.json().get("draftDetail") or {}
        picks = [p for p in (detail.get("picks") or []) if p.get("playerId", -1) > 0]
        df = pd.DataFrame([{"overall": p["overallPickNumber"], "round": p["roundId"],
                            "team_id": p["teamId"], "espn_player_id": p["playerId"],
                            "keeper": p.get("keeper", False)} for p in picks])
        return df, (f"ESPN live: drafted={detail.get('drafted')}, "
                    f"in_progress={detail.get('inProgress')}, "
                    f"{len(df)} pick(s) made")
    except Exception as exc:
        return pd.DataFrame(), f"live fetch failed ({type(exc).__name__}: {exc})"


def resolve_espn_ids(picks: pd.DataFrame, pool: pd.DataFrame) -> pd.DataFrame:
    """Map ESPN player ids on live picks to board rows via the committed ID map."""
    if picks.empty:
        return picks.assign(name=[], player_id=[])
    idm = pd.read_csv(PROJECT_ROOT / "espn_bdl_id_map.csv")
    picks = picks.merge(idm[["espn_id", "espn_name", "bdl_player_id"]],
                        left_on="espn_player_id", right_on="espn_id", how="left")
    picks = picks.merge(pool[["player_id", "name"]], left_on="bdl_player_id",
                        right_on="player_id", how="left")
    picks["name"] = picks["name"].fillna(picks["espn_name"])
    return picks


def load_pool() -> pd.DataFrame:
    b = pd.read_csv(BOARD_CSV)
    b = b[(b["value_source"] == "model") & b["total_value"].notna()].copy()
    if KEEPERS.exists():
        k = pd.read_csv(KEEPERS)
        if len(k):
            kept = set(k["player_name"].astype(str).str.lower())
            b = b[~b["name"].str.lower().isin(kept)]
    b["positions"] = b["pos_str"].fillna("").apply(
        lambda s: [x for x in s.split("/") if x])
    b["draft_rank"] = b["draft_rank"].fillna(999)
    return b.reset_index(drop=True)


def simulate_draft(pool: pd.DataFrame, my_strategy: str, rng: np.random.Generator,
                   opp_noise: float = 12.0) -> dict[int, pd.DataFrame]:
    order = snake_order(ROUND1_ORDER, N_ROUNDS)
    avail = pool.copy()
    # Opponent noise is drawn ONCE per simulation so both strategies face an
    # identical field -- otherwise the comparison measures luck, not strategy.
    avail["opp_key"] = avail["draft_rank"] + rng.normal(0, opp_noise, len(avail))
    rosters = {t: avail.iloc[0:0].copy() for t in set(order)}
    for i, team in enumerate(order):
        if avail.empty:
            break
        if team == MY_TEAM_ID:
            if my_strategy == "board":
                pick = avail.sort_values("total_value", ascending=False).index[0]
            else:
                gap = picks_until_next(order, team, i + 1)
                ranked, _ = rank_available(avail, rosters[team], gap)
                pick = ranked.index[0]
        else:
            pick = avail.sort_values("opp_key").index[0]
        rosters[team] = pd.concat([rosters[team], avail.loc[[pick]]])
        avail = avail.drop(index=pick)
    return rosters


def simulate_season(rosters: dict[int, pd.DataFrame], rng: np.random.Generator,
                    weeks: int = 20, week_noise: float = 0.55) -> dict[int, float]:
    """Round-robin H2H, categories compared with weekly noise.

    A deterministic comparison of summed z-scores would make the draft look more
    decisive than it is; real weeks are noisy and a category can be lost by a
    team that is better at it. week_noise is the sd of that per-category wobble.
    """
    teams = sorted(rosters)
    base = {t: roster_strength(rosters[t]) for t in teams}
    wins = {t: 0.0 for t in teams}
    played = {t: 0 for t in teams}
    for _ in range(weeks):
        for a_i, a in enumerate(teams):
            for b in teams[a_i + 1:]:
                for c in CATS:
                    va = base[a][c] + rng.normal(0, week_noise)
                    vb = base[b][c] + rng.normal(0, week_noise)
                    if va > vb:
                        wins[a] += 1
                    elif vb > va:
                        wins[b] += 1
                    else:
                        wins[a] += 0.5
                        wins[b] += 0.5
                played[a] += len(CATS)
                played[b] += len(CATS)
    return {t: wins[t] / played[t] for t in teams}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--simulate", type=int, default=0,
                    help="run N paired mock drafts + seasons")
    ap.add_argument("--picks", type=Path,
                    help="manual fallback: CSV of picks already made "
                         "(team_id, player_name)")
    ap.add_argument("--live", action="store_true",
                    help="read picks from ESPN's draft endpoint, falling back to "
                         "--picks if that returns nothing")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--saturation", type=float, default=SATURATION)
    ap.add_argument("--opp-noise", type=float, default=12.0)
    args = ap.parse_args()

    pool = load_pool()
    print(f"pool: {len(pool)} model-valued players "
          f"(keepers removed: {'yes' if KEEPERS.exists() else 'no file'})")
    order = snake_order(ROUND1_ORDER, N_ROUNDS)
    mine = [i + 1 for i, t in enumerate(order) if t == MY_TEAM_ID]
    print(f"my picks (team {MY_TEAM_ID}): {mine[:6]} ... total {len(mine)}")

    if args.simulate:
        rows = []
        for s in range(args.simulate):
            rng_seed = args.seed + s
            res = {}
            for strat in ("board", "assistant"):
                # identical seed per strategy => identical opponent field
                rng = np.random.default_rng(rng_seed)
                rosters = simulate_draft(pool, strat, rng, args.opp_noise)
                rng2 = np.random.default_rng(rng_seed + 100000)
                share = simulate_season(rosters, rng2)
                res[strat] = share[MY_TEAM_ID]
                res[f"{strat}_rank"] = (
                    1 + sum(1 for t, v in share.items() if v > share[MY_TEAM_ID]))
            rows.append(res)
        d = pd.DataFrame(rows)
        print("\n" + "=" * 78)
        print(f"PAIRED MOCK DRAFTS + SIMULATED SEASONS  (n={len(d)})")
        print("=" * 78)
        print(f"  category win share, straight down the board : "
              f"{d['board'].mean():.4f}  (sd {d['board'].std():.4f})")
        print(f"  category win share, assistant-guided       : "
              f"{d['assistant'].mean():.4f}  (sd {d['assistant'].std():.4f})")
        diff = d["assistant"] - d["board"]
        from scipy import stats as sps
        t = sps.ttest_rel(d["assistant"], d["board"])
        print(f"  difference: {diff.mean():+.4f} "
              f"({100*diff.mean()/d['board'].mean():+.2f}% relative), "
              f"assistant wins {int((diff>0).sum())}/{len(d)} drafts, p={t.pvalue:.4f}")
        print(f"  mean finish: board {d['board_rank'].mean():.2f} of {N_TEAMS}, "
              f"assistant {d['assistant_rank'].mean():.2f}")
        return 0

    # ---- advisory mode -----------------------------------------------------
    taken, roster = set(), pool.iloc[0:0].copy()
    live_used = False
    if args.live:
        lp, msg = fetch_live_picks()
        print(f"  {msg}")
        if len(lp):
            lp = resolve_espn_ids(lp, pool)
            unresolved = lp[lp["player_id"].isna()]
            for _, r in lp[lp["player_id"].notna()].iterrows():
                hit = pool[pool["player_id"] == r["player_id"]]
                if hit.empty:
                    continue
                taken.add(hit.index[0])
                if int(r["team_id"]) == MY_TEAM_ID:
                    roster = pd.concat([roster, hit])
            if len(unresolved):
                print(f"  WARNING: {len(unresolved)} live pick(s) could not be "
                      f"resolved to a board row (likely rookies with no model "
                      f"value): {unresolved['name'].tolist()[:5]}")
            live_used = True
        else:
            print("  no live picks available -- falling back to --picks")
    if not live_used and args.picks and args.picks.exists():
        pk = pd.read_csv(args.picks)
        for _, r in pk.iterrows():
            hit = pool[pool["name"].str.lower() == str(r["player_name"]).lower()]
            if hit.empty:
                print(f"  WARNING: pick {r['player_name']!r} not found in the pool")
                continue
            taken.add(hit.index[0])
            if int(r["team_id"]) == MY_TEAM_ID:
                roster = pd.concat([roster, hit])
    avail = pool.drop(index=[i for i in taken if i in pool.index])
    # Count from the pick AFTER mine: when I am on the clock the relevant gap is
    # how long until my *next* turn, not zero.
    gap = picks_until_next(order, MY_TEAM_ID, len(taken) + 1)
    ranked, w = rank_available(avail, roster, gap)
    print(f"\npicks made: {len(taken)}   my roster: {len(roster)}   "
          f"opponent picks before my next turn: {gap}")
    print("\ncategory weights from my roster (1.00 = full credit, lower = already strong):")
    print("  " + "  ".join(f"{c} {w[c]:.2f}" for c in CATS))
    if len(roster):
        print("  my roster:", ", ".join(roster["name"].tolist()))
    print(f"\n{'#':>3} {'player':<26}{'pos':<14}{'marg':>7}{'board':>7}"
          f"{'slot':>6}{'urg':>6}")
    for i, (_, r) in enumerate(ranked.head(15).iterrows(), 1):
        print(f"{i:>3} {r['name'][:25]:<26}{r['pos_str'][:13]:<14}"
              f"{r['marginal_value']:>7.2f}{r['total_value']:>7.2f}"
              f"{r['slot_mult']:>6.2f}{r['urgency']:>6.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
