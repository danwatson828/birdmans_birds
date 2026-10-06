#!/usr/bin/env python3
"""Pull and cache ESPN fantasy league metadata. READ-ONLY.

Design spec section 2 / build-order step 5. This touches no roster moves, no
lineup changes, no adds or drops -- it only reads. Every espn_api call used here
is a getter.

What it caches (JSON, not DuckDB -- this is league metadata, small and
frequently re-fetched, not the core stats data):
  * scoring categories and roster rules
  * the current (or next scheduled) matchup, mine vs my opponent
  * my roster
  * available free agents

Credentials come from .env: ESPN_SWID and ESPN_S2, the two cookies ESPN sets
after you log in. League id and season are read from ESPN_LEAGUE_ID /
ESPN_SEASON if present, otherwise discovered from your own ESPN fan profile, so
there is nothing to look up by hand.

Usage:
    python league_sync.py                    # discover, pull, cache, summarise
    python league_sync.py --season 2026       # a specific ESPN season year
    python league_sync.py --free-agents 100
    python league_sync.py --json              # print the cache instead of prose
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import datetime as dt
from pathlib import Path

import requests
from dotenv import load_dotenv

try:
    from espn_api.basketball import League
    from espn_api.basketball.constant import NINE_CAT_STATS, POSITION_MAP, STATS_MAP
except ImportError:
    sys.exit("pip install espn_api")

PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_DIR = PROJECT_ROOT / "espn_cache"
FAN_API = "https://fan.api.espn.com/apis/v2/fans"
LM_API = "https://lm-api-reads.fantasy.espn.com/apis/v3/games/fba/seasons"
UA = {"User-Agent": "Mozilla/5.0"}

# ESPN scores a category league on a subset of stats; the rest of the stat line
# comes back alongside with result=None. These are the ones we treat as scored.
BENCH_SLOTS = {"BE", "IR"}


def braced(swid: str) -> str:
    """ESPN stores SWID wrapped in braces; espn_api expects that form."""
    return swid if swid.startswith("{") else "{" + swid + "}"


# =============================================================================
# Discovery
# =============================================================================
def discover_leagues(swid: str, s2: str) -> list[dict]:
    """Ask ESPN which fantasy leagues this user belongs to.

    Saves having to dig a league id out of a browser URL. FBA = fantasy
    basketball; the same profile also lists football (FFL) and other sports,
    which are filtered out.
    """
    r = requests.get(f"{FAN_API}/{braced(swid)}", headers=UA,
                     cookies={"SWID": braced(swid), "espn_s2": s2},
                     params={"context": "fantasy", "showAirings": "false"},
                     timeout=30)
    r.raise_for_status()
    found = []
    for pref in r.json().get("preferences", []) or []:
        entry = (pref.get("metaData") or {}).get("entry") or {}
        if not entry:
            continue
        for group in entry.get("groups", []) or []:
            found.append({
                "sport": entry.get("abbrev"),
                "league_id": group.get("groupId"),
                "league_name": group.get("groupName"),
                "season": entry.get("seasonId"),
                "team_id": entry.get("entryId"),
            })
    return [f for f in found if f.get("sport") == "FBA"]


def fetch_raw_settings(league_id: int, season: int, swid: str, s2: str) -> dict:
    """Raw mSettings payload.

    Needed because espn_api 0.46.0's Settings object exposes neither the scoring
    categories nor the lineup slot counts -- settings.scoring_format does not
    exist and settings.roster_slots is None. The categories and roster rules are
    the two things most worth verifying, so they are parsed from source here
    rather than trusted to the wrapper.
    """
    r = requests.get(f"{LM_API}/{season}/segments/0/leagues/{league_id}",
                     headers=UA, cookies={"SWID": braced(swid), "espn_s2": s2},
                     params={"view": ["mSettings", "mDraftDetail"]}, timeout=30)
    r.raise_for_status()
    return r.json()


# =============================================================================
# Extraction
# =============================================================================
def extract_categories(raw: dict) -> list[dict]:
    items = ((raw.get("settings") or {}).get("scoringSettings") or {}).get("scoringItems") or []
    cats = []
    for it in items:
        sid = str(it.get("statId"))
        cats.append({
            "stat_id": sid,
            "name": STATS_MAP.get(sid, f"stat_{sid}"),
            # Turnovers are a category you want to LOSE; ESPN marks that here.
            "lower_is_better": bool(it.get("isReverseItem")),
        })
    return cats


def extract_roster_rules(raw: dict) -> dict:
    rs = ((raw.get("settings") or {}).get("rosterSettings") or {})
    counts = rs.get("lineupSlotCounts") or {}
    slots = {}
    for slot, n in counts.items():
        if n:
            slots[POSITION_MAP.get(int(slot), f"slot_{slot}")] = n
    starters = {k: v for k, v in slots.items() if k not in BENCH_SLOTS}
    return {
        "slots": slots,
        "starting_slots": starters,
        "starters_total": sum(starters.values()),
        "bench": slots.get("BE", 0),
        "injured_reserve": slots.get("IR", 0),
        "roster_total": sum(slots.values()),
    }


def extract_draft_settings(raw: dict) -> dict:
    """Draft format, keeper rule and pick order, from the raw payload.

    espn_api exposes none of this, and the original version of this script did
    not capture it -- so the draft format had to be re-fetched by hand when the
    draft board needed it.

    Two caveats recorded alongside the data rather than in someone's head:
    `auctionBudget` is populated even for a SNAKE league and means nothing there;
    and because ESPN echoes prior-season data before a draft (section 2), the
    pick order should be confirmed in the app before anything depends on it.
    """
    ds = (raw.get("settings") or {}).get("draftSettings") or {}
    detail = raw.get("draftDetail") or {}
    date_ms = ds.get("date")
    return {
        "type": ds.get("type"),
        "is_snake": ds.get("type") == "SNAKE",
        "date_epoch_ms": date_ms,
        "date_local": (dt.datetime.fromtimestamp(date_ms / 1000).astimezone()
                       .isoformat(timespec="minutes") if date_ms else None),
        "date_configured": bool(date_ms),
        "keeper_count": ds.get("keeperCount"),
        "keeper_count_future": ds.get("keeperCountFuture"),
        "keeper_order_type": ds.get("keeperOrderType"),
        "order_type": ds.get("orderType"),
        "pick_order_team_ids": ds.get("pickOrder"),
        "time_per_selection_s": ds.get("timePerSelection"),
        "trading_enabled_during_draft": ds.get("isTradingEnabled"),
        # Populated even for snake leagues, where it is meaningless.
        "auction_budget_vestigial_if_snake": ds.get("auctionBudget"),
        "total_picks": len(detail.get("picks") or []) or None,
        "drafted": detail.get("drafted"),
        "in_progress": detail.get("inProgress"),
        "pick_order_caveat": ("ESPN echoes prior-season data before a draft; "
                              "confirm this order in the app before relying on it"),
    }


def extract_league_meta(raw: dict, lg: League) -> dict:
    st = raw.get("settings") or {}
    sched = st.get("scheduleSettings") or {}
    acq = st.get("acquisitionSettings") or {}
    status = raw.get("status") or {}
    return {
        "name": st.get("name"),
        "size": st.get("size"),
        "scoring_type": (st.get("scoringSettings") or {}).get("scoringType"),
        "regular_season_matchups": sched.get("matchupPeriodCount"),
        "matchup_period_length_weeks": sched.get("matchupPeriodLength"),
        "playoff_team_count": sched.get("playoffTeamCount"),
        "keeper_count": getattr(lg.settings, "keeper_count", None),
        "acquisition_type": acq.get("acquisitionType"),
        "uses_faab": bool(getattr(lg.settings, "faab", False)),
        "acquisition_budget": acq.get("acquisitionBudget"),
        "waiver_hours": acq.get("waiverHours"),
        "trade_deadline": getattr(lg.settings, "trade_deadline", None),
        "current_matchup_period": status.get("currentMatchupPeriod"),
        "latest_scoring_period": status.get("latestScoringPeriod"),
        "is_active": status.get("isActive"),
    }


def season_state(meta: dict) -> str:
    """Has this ESPN season actually started?

    ESPN keeps a finished season flagged active until the next one rolls over,
    and pre-populates the upcoming season's settings and rosters well before its
    first game -- so neither isActive nor the presence of a roster tells you
    whether games have been played. latestScoringPeriod does: it is 0 until the
    season starts.
    """
    if not meta.get("latest_scoring_period"):
        return "not_started"
    return "in_progress" if meta.get("is_active") else "complete"


def player_dict(p) -> dict:
    return {
        "player_id": getattr(p, "playerId", None),
        "name": p.name,
        "pro_team": getattr(p, "proTeam", None),
        "position": getattr(p, "position", None),
        "eligible_slots": [s for s in (getattr(p, "eligibleSlots", []) or [])
                           if s not in BENCH_SLOTS],
        "lineup_slot": getattr(p, "lineupSlot", None),
        "injured": bool(getattr(p, "injured", False)),
        "injury_status": getattr(p, "injuryStatus", None),
        "pos_rank": getattr(p, "posRank", None),
        "acquisition_type": getattr(p, "acquisitionType", None),
        # nine_cat_averages is exactly the shape a 9-cat league needs.
        "nine_cat_averages": getattr(p, "nine_cat_averages", None),
    }


def extract_matchup(lg: League, my_team_id: int, state: str) -> dict | None:
    """The current matchup if one is being scored, else the next scheduled one.

    box_scores() carries the category-by-category detail that section 8's
    strategy logic needs, but it raises before the season starts (there is no
    scoring data to parse). scoreboard() still returns the schedule, so a
    not-yet-started season yields opponent and fixture without any stats.
    """
    if state != "not_started":
        try:
            for b in lg.box_scores():
                if my_team_id in (b.home_team.team_id, b.away_team.team_id):
                    i_am_home = b.home_team.team_id == my_team_id
                    mine, theirs = ((b.home_stats, b.away_stats) if i_am_home
                                    else (b.away_stats, b.home_stats))
                    my_w, opp_w = ((b.home_wins, b.away_wins) if i_am_home
                                   else (b.away_wins, b.home_wins))
                    opp = b.away_team if i_am_home else b.home_team
                    return {
                        "source": "box_scores",
                        "scoring_period": getattr(b, "scoring_period", None),
                        "i_am_home": i_am_home,
                        "opponent_team_id": opp.team_id,
                        "opponent_name": opp.team_name,
                        "category_wins_me": my_w,
                        "category_wins_opponent": opp_w,
                        "ties": getattr(b, "home_ties", None),
                        # Only categories ESPN actually scored carry a result.
                        "my_categories": {k: v for k, v in mine.items()
                                          if v.get("result") is not None},
                        "opponent_categories": {k: v for k, v in theirs.items()
                                                if v.get("result") is not None},
                        "unscored_components": sorted(k for k, v in mine.items()
                                                      if v.get("result") is None),
                    }
        except Exception as exc:
            # Fall through to the schedule rather than failing the whole sync.
            print(f"  note: box_scores() unavailable ({type(exc).__name__}); "
                  f"falling back to the schedule", file=sys.stderr)

    try:
        for m in lg.scoreboard():
            h, a = getattr(m, "home_team", None), getattr(m, "away_team", None)
            if not h or not a:
                continue
            if my_team_id in (getattr(h, "team_id", None), getattr(a, "team_id", None)):
                i_am_home = getattr(h, "team_id", None) == my_team_id
                opp = a if i_am_home else h
                return {
                    "source": "scoreboard",
                    "scoring_period": None,
                    "i_am_home": i_am_home,
                    "opponent_team_id": getattr(opp, "team_id", None),
                    "opponent_name": getattr(opp, "team_name", None),
                    "category_wins_me": None,
                    "category_wins_opponent": None,
                    "my_categories": {},
                    "opponent_categories": {},
                    "note": "scheduled fixture only; no stats yet",
                }
    except Exception as exc:
        print(f"  note: scoreboard() failed ({type(exc).__name__}: {exc})",
              file=sys.stderr)
    return None


# =============================================================================
# Verification -- does the pulled config match a real league, or did the call
# merely succeed?
# =============================================================================
def verify(cache: dict) -> list[dict]:
    checks = []

    def add(name, ok, detail):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    cats = cache["scoring"]["categories"]
    names = {c["name"] for c in cats}
    meta = cache["league"]

    add("scoring type is category-based (H2H_CATEGORY)",
        meta["scoring_type"] == "H2H_CATEGORY", meta["scoring_type"])

    add("category set is the canonical 9-cat",
        names == set(NINE_CAT_STATS),
        f"{len(cats)} categories: {', '.join(sorted(names))}"
        + ("" if names == set(NINE_CAT_STATS)
           else f" | canonical-only: {sorted(set(NINE_CAT_STATS)-names)}"
                f" extra: {sorted(names-set(NINE_CAT_STATS))}"))

    rev = [c["name"] for c in cats if c["lower_is_better"]]
    add("turnovers are the only inverted category", rev == ["TO"], f"inverted: {rev}")

    # Independent confirmation: the settings blob says which categories are
    # scored, and a real scored matchup says which ones ESPN actually graded.
    # Agreement between the two is the difference between reading config and
    # confirming behaviour.
    mu = cache.get("matchup")
    if mu and mu.get("my_categories"):
        graded = set(mu["my_categories"])
        add("categories ESPN actually graded == categories in settings",
            graded == names,
            f"graded {len(graded)}: {', '.join(sorted(graded))}"
            + ("" if graded == names else f" | mismatch vs settings {sorted(names ^ graded)}"))
    else:
        add("categories ESPN actually graded == categories in settings", True,
            "SKIPPED - no scored matchup yet this season (cannot cross-check "
            "settings against real grading until games are played)")

    # Behavioural confirmation of the inversion, not just the config flag: in a
    # real graded matchup the side with FEWER turnovers must be the one credited
    # with the win. If ESPN ever stopped treating TO as a negative category, the
    # settings flag alone would not reveal it.
    if mu and mu.get("my_categories"):
        problems = []
        for c in cats:
            if not c["lower_is_better"]:
                continue
            name = c["name"]
            mine = (mu["my_categories"].get(name) or {})
            theirs = (mu["opponent_categories"].get(name) or {})
            if mine.get("result") is None or theirs.get("value") is None:
                continue
            lower_won = ((mine["value"] < theirs["value"] and mine["result"] == "WIN")
                         or (mine["value"] > theirs["value"] and mine["result"] == "LOSS")
                         or mine["value"] == theirs["value"])
            if not lower_won:
                problems.append(f"{name}: mine={mine['value']} theirs={theirs['value']} "
                                f"-> {mine['result']}")
        detail = "; ".join(
            f"{c['name']}: {mu['my_categories'][c['name']]['value']:g} vs "
            f"{mu['opponent_categories'][c['name']]['value']:g} -> "
            f"{mu['my_categories'][c['name']]['result']}"
            for c in cats if c["lower_is_better"] and c["name"] in mu["my_categories"])
        add("inverted category graded so that fewer is better",
            not problems, detail or "no inverted category in this matchup")
    else:
        add("inverted category graded so that fewer is better", True,
            "SKIPPED - no scored matchup yet this season")

    # The category wins reported for the matchup should account for every scored
    # category: wins + losses + ties == the number of categories.
    if mu and mu.get("category_wins_me") is not None:
        total = (mu["category_wins_me"] + mu["category_wins_opponent"]
                 + (mu.get("ties") or 0))
        add("matchup category wins + losses + ties == category count",
            total == len(cats),
            f"{mu['category_wins_me']}-{mu['category_wins_opponent']}"
            f"-{mu.get('ties') or 0} = {total} of {len(cats)}")
    else:
        add("matchup category wins + losses + ties == category count", True,
            "SKIPPED - no scored matchup yet this season")

    d = cache.get("draft") or {}
    add("draft format is present and is snake or auction",
        d.get("type") in ("SNAKE", "AUCTION"), str(d.get("type")))
    if d.get("total_picks") and meta.get("size"):
        add("total draft picks divide evenly by league size",
            d["total_picks"] % meta["size"] == 0,
            f"{d['total_picks']} picks / {meta['size']} teams = "
            f"{d['total_picks'] / meta['size']:.2f} rounds")
    else:
        add("total draft picks divide evenly by league size", True,
            "SKIPPED - no pick list in the payload")
    po = d.get("pick_order_team_ids") or []
    if po:
        add("pick order lists every team exactly once",
            sorted(po) == sorted({t["team_id"] for t in cache["standings"]}),
            f"{len(po)} entries, {len(set(po))} distinct")
    else:
        add("pick order lists every team exactly once", True,
            "SKIPPED - no pick order configured")

    rr = cache["roster_rules"]
    add("starting slots + bench + IR == total roster slots",
        rr["starters_total"] + rr["bench"] + rr["injured_reserve"] == rr["roster_total"],
        f"{rr['starters_total']} starters + {rr['bench']} bench + "
        f"{rr['injured_reserve']} IR = {rr['roster_total']}")

    n_roster = len(cache["my_team"]["roster"])
    active_capacity = rr["starters_total"] + rr["bench"]
    add("my roster fits the configured roster size",
        n_roster <= rr["roster_total"],
        f"{n_roster} players vs {rr['roster_total']} slots "
        f"({active_capacity} active + {rr['injured_reserve']} IR)")

    # Every rostered player must be legal somewhere in the configured lineup.
    slots = set(rr["starting_slots"])
    illegal = [p["name"] for p in cache["my_team"]["roster"]
               if p["eligible_slots"] and not (set(p["eligible_slots"]) & slots)]
    add("every rostered player is eligible for a configured slot",
        not illegal, f"{len(illegal)} ineligible" + (f": {illegal[:5]}" if illegal else ""))

    add("league size matches the number of teams returned",
        meta["size"] == cache["league"]["team_count_returned"],
        f"settings say {meta['size']}, API returned "
        f"{cache['league']['team_count_returned']}")

    return checks


# =============================================================================
# Sync
# =============================================================================
def sync(league_id: int, season: int, swid: str, s2: str, my_team_id: int | None,
         n_free_agents: int) -> dict:
    lg = League(league_id=league_id, year=season, swid=braced(swid), espn_s2=s2)
    raw = fetch_raw_settings(league_id, season, swid, s2)

    meta = extract_league_meta(raw, lg)
    meta["team_count_returned"] = len(lg.teams)
    state = season_state(meta)

    if my_team_id is None:
        raise ValueError("could not determine which team is yours")
    me = next((t for t in lg.teams if t.team_id == my_team_id), None)
    if me is None:
        raise ValueError(f"team_id {my_team_id} not in league "
                         f"{[t.team_id for t in lg.teams]}")

    return {
        "fetched_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "espn_season": season,
        "season_label": f"{season-1}-{str(season)[2:]}",
        "season_state": state,
        "league_id": league_id,
        "league": meta,
        "scoring": {"categories": extract_categories(raw)},
        "draft": extract_draft_settings(raw),
        "roster_rules": extract_roster_rules(raw),
        "my_team": {
            "team_id": me.team_id,
            "name": me.team_name,
            "wins": me.wins, "losses": me.losses, "ties": getattr(me, "ties", None),
            "roster": [player_dict(p) for p in me.roster],
        },
        "matchup": extract_matchup(lg, my_team_id, state),
        "standings": [{"team_id": t.team_id, "name": t.team_name,
                       "wins": t.wins, "losses": t.losses,
                       "ties": getattr(t, "ties", None)} for t in lg.teams],
        # Every roster, not just the user's. A trade finder, a keeper
        # comparison and an opponent preview all need to know who else holds
        # whom, and none of them can be built from standings alone. Same
        # player_dict shape as my_team.roster so a consumer does not need to
        # know which list a player came from.
        "teams": [{"team_id": t.team_id, "name": t.team_name,
                   "wins": t.wins, "losses": t.losses,
                   "ties": getattr(t, "ties", None),
                   "is_me": t.team_id == my_team_id,
                   "roster": [player_dict(p) for p in t.roster]}
                  for t in lg.teams],
        "free_agents": [player_dict(p) for p in lg.free_agents(size=n_free_agents)],
    }


# =============================================================================
# Output
# =============================================================================
def summarise(cache: dict, checks: list[dict]) -> None:
    L, rr, mu = cache["league"], cache["roster_rules"], cache["matchup"]
    STATE = {"not_started": "has NOT started yet",
             "in_progress": "in progress",
             "complete": "complete"}
    print("=" * 74)
    print(f"{L['name']}  (league {cache['league_id']}, ESPN season "
          f"{cache['espn_season']} = {cache['season_label']} NBA season)")
    print("=" * 74)
    print(f"  {L['size']}-team {L['scoring_type']}   season {STATE[cache['season_state']]}")
    print(f"  {L['regular_season_matchups']} regular-season matchups, "
          f"{L['matchup_period_length_weeks']}-week periods, "
          f"{L['playoff_team_count']} make playoffs")
    print(f"  keepers: {L['keeper_count']}   acquisitions: {L['acquisition_type']}"
          f" (FAAB {'on' if L['uses_faab'] else 'off'}, waivers "
          f"{L['waiver_hours']}h)")
    print(f"  currentMatchupPeriod={L['current_matchup_period']} "
          f"latestScoringPeriod={L['latest_scoring_period']}")

    d = cache.get("draft") or {}
    print(f"\nDRAFT")
    print(f"  format: {d.get('type')}"
          + ("  (auctionBudget is present but vestigial for a snake league)"
             if d.get("is_snake") and d.get("auction_budget_vestigial_if_snake")
             else ""))
    print(f"  date: {d.get('date_local') or 'NOT CONFIGURED IN ESPN'}")
    print(f"  keepers: {d.get('keeper_count')} per team "
          f"({d.get('keeper_order_type')} order)")
    print(f"  total picks: {d.get('total_picks')}"
          + (f" = {cache['league']['size']} teams x "
             f"{d['total_picks'] // cache['league']['size']} rounds"
             if d.get("total_picks") and cache["league"].get("size") else ""))
    print(f"  {d.get('time_per_selection_s')}s per pick, trading during draft: "
          f"{d.get('trading_enabled_during_draft')}")
    print(f"  drafted: {d.get('drafted')}   in progress: {d.get('in_progress')}")
    po = d.get("pick_order_team_ids") or []
    if po:
        mine = cache["my_team"]["team_id"]
        pos = po.index(mine) + 1 if mine in po else None
        print(f"  pick order (team ids): {po}")
        if pos:
            print(f"  -> your team ({mine}) picks {pos} of {len(po)} in round 1")
        print(f"  CAVEAT: {d.get('pick_order_caveat')}")

    cats = cache["scoring"]["categories"]
    print(f"\nSCORING CATEGORIES ({len(cats)})")
    for c in cats:
        arrow = "lower is better" if c["lower_is_better"] else ""
        print(f"  {c['name']:<5} (statId {c['stat_id']:>2}) {arrow}")

    print(f"\nROSTER RULES  ({rr['roster_total']} slots total)")
    print("  starters: " + ", ".join(f"{k}x{v}" for k, v in rr["starting_slots"].items())
          + f"  = {rr['starters_total']}")
    print(f"  bench: {rr['bench']}    IR: {rr['injured_reserve']}")

    me = cache["my_team"]
    print(f"\nMY TEAM: {me['name']}  (team {me['team_id']})   "
          f"record {me['wins']}-{me['losses']}"
          + (f"-{me['ties']}" if me['ties'] else ""))
    print(f"  roster ({len(me['roster'])}):")
    for p in me["roster"]:
        flag = ""
        if p["injured"] or (p["injury_status"] or "ACTIVE") != "ACTIVE":
            flag = f"  [{p['injury_status']}]"
        print(f"    {p['lineup_slot'] or '?':<3} {p['name']:<26} "
              f"{p['pro_team'] or '--':<4} {p['position'] or '':<3}{flag}")

    print("\nCURRENT MATCHUP")
    if not mu:
        print("  none found")
    else:
        side = "home" if mu["i_am_home"] else "away"
        print(f"  {me['name']} ({side}) vs {mu['opponent_name']}")
        if mu.get("note"):
            print(f"  {mu['note']}")
        if mu.get("my_categories"):
            print(f"  category score: me {mu['category_wins_me']} - "
                  f"{mu['category_wins_opponent']} opponent"
                  + (f", {mu['ties']} tied" if mu.get("ties") else ""))
            print(f"  {'cat':<6}{'mine':>12}{'theirs':>12}   result")
            for cat in [c["name"] for c in cats]:
                m = mu["my_categories"].get(cat, {})
                o = mu["opponent_categories"].get(cat, {})
                fmt = (lambda v: f"{v:.3f}" if isinstance(v, float) and v < 2
                       else f"{v:g}")
                print(f"  {cat:<6}{fmt(m.get('value',0)):>12}"
                      f"{fmt(o.get('value',0)):>12}   {m.get('result','')}")

    fa = cache["free_agents"]
    print(f"\nFREE AGENTS (top {len(fa)} by ESPN ranking)")
    for p in fa[:12]:
        nc = p.get("nine_cat_averages") or {}
        line = "  ".join(f"{k} {nc[k]:.1f}" for k in ("PTS", "REB", "AST")
                         if isinstance(nc.get(k), (int, float)))
        print(f"    {p['name']:<26} {p['pro_team'] or '--':<4} "
              f"{p['position'] or '':<3} {line}")
    if len(fa) > 12:
        print(f"    ... and {len(fa)-12} more in the cache")

    print("\nVERIFICATION")
    for c in checks:
        mark = "PASS" if c["ok"] else "FAIL"
        if c["detail"].startswith("SKIPPED"):
            mark = "SKIP"
        print(f"  {mark}  {c['check']}")
        print(f"        {c['detail']}")
    bad = [c for c in checks if not c["ok"]]
    print(f"\n  {len(checks)-len(bad)}/{len(checks)} checks passed"
          + ("" if not bad else f" -- {len(bad)} FAILED"))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--league-id", type=int)
    ap.add_argument("--season", type=int, help="ESPN season year (2027 = 2026-27)")
    ap.add_argument("--team-id", type=int)
    ap.add_argument("--free-agents", type=int, default=50)
    ap.add_argument("--cache-dir", type=Path, default=CACHE_DIR)
    ap.add_argument("--json", action="store_true", help="print the cache as JSON")
    args = ap.parse_args()

    load_dotenv(PROJECT_ROOT / ".env")
    swid, s2 = os.environ.get("ESPN_SWID"), os.environ.get("ESPN_S2")
    if not swid or not s2:
        sys.exit("ESPN_SWID and ESPN_S2 must be set in .env (browser cookies "
                 "after logging in to ESPN)")

    league_id = args.league_id or (int(os.environ["ESPN_LEAGUE_ID"])
                                  if os.environ.get("ESPN_LEAGUE_ID") else None)
    season = args.season or (int(os.environ["ESPN_SEASON"])
                             if os.environ.get("ESPN_SEASON") else None)
    team_id = args.team_id or (int(os.environ["ESPN_TEAM_ID"])
                              if os.environ.get("ESPN_TEAM_ID") else None)

    if not league_id or not season or not team_id:
        print("discovering your basketball leagues from your ESPN profile ...")
        leagues = discover_leagues(swid, s2)
        if not leagues:
            sys.exit("no fantasy basketball (FBA) league found on this ESPN "
                     "profile; pass --league-id / --season / --team-id")
        if len(leagues) > 1 and not league_id:
            print("  multiple basketball leagues found:")
            for f in leagues:
                print(f"    league_id={f['league_id']} season={f['season']} "
                      f"{f['league_name']!r}")
            sys.exit("pass --league-id (and --season) to pick one")
        f = leagues[0]
        league_id = league_id or f["league_id"]
        season = season or f["season"]
        team_id = team_id or f["team_id"]
        print(f"  using league_id={league_id} season={season} team_id={team_id} "
              f"({f['league_name']!r})")

    t0 = time.monotonic()
    cache = sync(league_id, season, swid, s2, team_id, args.free_agents)
    checks = verify(cache)
    cache["verification"] = checks

    args.cache_dir.mkdir(parents=True, exist_ok=True)
    out = args.cache_dir / f"league_{league_id}_{season}.json"
    out.write_text(json.dumps(cache, indent=2, default=str))

    if args.json:
        print(json.dumps(cache, indent=2, default=str))
    else:
        summarise(cache, checks)
        print(f"\ncached -> {out.relative_to(PROJECT_ROOT)} "
              f"({out.stat().st_size/1024:.0f} KB, {time.monotonic()-t0:.1f}s)")

    return 0 if all(c["ok"] for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
