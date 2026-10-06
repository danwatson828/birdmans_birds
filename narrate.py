#!/usr/bin/env python3
"""Turn the matchup engine's numbers into written advice. READ-ONLY.

STATUS: BUILT, NOT VERIFIED LIVE. The API credit balance has been exhausted
for this and the previous session, so `compose()` and the whole brief-building
path are exercised by tests against a stub client, and `narrate()` has never
run against the real model. That is a real gap, not a formality: wording,
length and whether the model respects the constraints below are exactly the
things a stub cannot tell you. Nothing here fabricates a placeholder answer --
with no credits it raises, and the caller reports that.

WHY A BRIEF RATHER THAN A TOOL LOOP. Every other agent path in this project
hands the model tools and lets it query. That is right when the question is
open-ended. Here it is not: the numbers are already computed, checked and in
some cases reconciled against ESPN to the last decimal, and letting the model
recompute them invites it to disagree with the page it is captioning. So the
brief carries the facts and the model's only job is to say what they mean --
which also means it cannot invent a number that is not in front of it.

The methodology skill is loaded for the same reason it is loaded elsewhere:
it is what makes the model state its basis and refuse to overclaim, and this
advice is explicitly not backtested.
"""

from __future__ import annotations

import json
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
SKILL = PROJECT_ROOT / ".claude" / "skills" / "statistical-methodology" / "SKILL.md"

MAX_BRIEF_CHARS = 9000

SYSTEM = """You write a short briefing for one fantasy basketball team \
manager, from numbers that have already been computed and checked.

Rules, in order of importance:

1. Use ONLY the numbers in the brief. Do not estimate, extrapolate or recall \
any figure that is not there. If something needed to answer is missing, say \
it is missing.
2. This advice is NOT backtested. The draft board's weights and the season \
projections were validated walk-forward against a stated benchmark; the \
weekly advice built on them was not. Say so once, plainly, and do not hedge \
every sentence afterwards.
3. Opponent totals are frozen at their current value because the league cache \
carries no opponent schedule. Every gap you quote is therefore a MINIMUM. \
State this where it affects a recommendation.
4. Lead with what to do. A manager reading this wants the decision first and \
the reasoning second.
5. No invented certainty. "Closes the gap on the projection" is honest; \
"this wins you the category" is not.
6. Be brief: at most 250 words, no headings, no bullet lists longer than four \
items."""

# One narration path, four briefs. The task line is the ONLY thing that
# varies: writing four prompts would mean four places for the honesty rules
# above to drift, and the rules are the whole point of routing this through a
# single function.
KINDS = {
    "matchup": (
        "This brief is this week's head-to-head matchup. Lead with the "
        "start/sit decision that matters most, then what is at risk."),
    "trade": (
        "This brief is a proposed trade. Say which side it favours and why, "
        "in categories. You are NOT required to pick a winner: if the "
        "category gains and losses roughly offset, say it is close and say "
        "what would decide it. Do not manufacture a verdict the numbers do "
        "not support."),
    "outlook": (
        "This brief is the season standing and outlook. Say where the team "
        "actually stands and what, if anything, is still reachable. If the "
        "season is decided, say so plainly rather than finding a silver "
        "lining."),
    "recap": (
        "This brief is a whole-season retrospective. Write it as a short "
        "post-mortem: what the lineup decisions cost across the season, what "
        "the waiver wire offered that went unused, and the one or two things "
        "that would plausibly have changed the year. Be direct about a bad "
        "season if it was one — this team finished last and the recap should "
        "not read like a consolation note."),
    "retrospective": (
        "This brief is a season-long comparison between the lineup as set and "
        "what an optimiser would have chosen. Describe the PATTERN, not the "
        "individual weeks: which categories it reliably gained, which it "
        "reliably gave up, and what that says about the objective it "
        "maximises."),
}


class NoCredits(RuntimeError):
    """Raised when the model cannot be reached for billing reasons.

    Separated from a generic failure so the caller can say "not verified
    pending credits" rather than "narration is broken", which are different
    facts about the system.
    """


def _fmt(v, pct: bool = False) -> str:
    if v is None:
        return "n/a"
    if pct:
        return f"{v * 100:.1f}%"
    return f"{v:,.1f}" if isinstance(v, float) else str(v)


def compose_matchup(matchup: dict, digest: dict | None = None,
            outlook: dict | None = None, schedule: dict | None = None) -> str:
    """Build the brief. Pure: no model call, no I/O, fully testable.

    Every section is omitted when its input is absent rather than rendered
    empty, so the model is never handed a heading with nothing under it and
    invited to fill the gap.
    """
    L = []
    L.append(f"MATCHUP: {matchup.get('season_label')} vs "
             f"{matchup.get('opponent')}, scoring period "
             f"{matchup.get('scoring_period')}.")
    wins = matchup.get("category_wins") or {}
    if wins.get("me") is not None:
        L.append(f"Category score: {wins.get('me')}-{wins.get('opponent')}.")
    week = matchup.get("week") or {}
    if week.get("found"):
        L.append(f"Week of {week['start']}, verified against ESPN's own totals "
                 f"in all nine categories.")

    L.append("\nCATEGORIES (mine vs theirs, state, how close):")
    for c in matchup.get("state", []):
        is_pct = c["category"] in ("FG%", "FT%")
        L.append(f"  {c['category']}: {_fmt(c['mine'], is_pct)} vs "
                 f"{_fmt(c['theirs'], is_pct)} — {c['status']}, {c['closeness']}"
                 + (" (lower wins)" if c.get("lower_wins") else ""))

    flips = matchup.get("flips") or []
    if flips:
        L.append("\nSTART/SIT MOVES THAT WOULD HELP A LOSING CATEGORY:")
        for f in flips:
            if not f.get("bench_in"):
                L.append(f"  {f['category']}: no bench swap improves this.")
                continue
            is_pct = f["category"] in ("FG%", "FT%")
            gain = (f"{f['gain'] * 100:.2f} percentage points" if is_pct
                    else f"{f['gain']:.1f}")
            need = (f"{f['need'] * 100:.2f} percentage points" if is_pct
                    else f"{f['need']:.1f}")
            L.append(f"  {f['category']}: start {f['bench_in']} "
                     f"({f['bench_games']} games) over {f['starter_out']} "
                     f"({f['starter_games']} games) — gains {gain} against a "
                     f"gap of {need}"
                     + ("; closes it" if f["closes_gap"] else "; not enough alone")
                     + (f"; costs {', '.join(f['would_lose'])}"
                        if f.get("would_lose") else ""))

    lu = matchup.get("lineup")
    if lu and lu.get("changes"):
        L.append(f"\nLINEUP: {lu['n_changes']} change(s) from the lineup as set — "
                 + "; ".join(f"{c['player']} into {c['slot']}"
                             for c in lu["changes"]))

    wv = matchup.get("waivers") or {}
    if wv.get("available") and wv.get("candidates"):
        L.append("\nWAIVER CANDIDATES (projected season value, vs the weakest "
                 "bench player):")
        for c in wv["candidates"][:4]:
            L.append(f"  {c['name']} ({c.get('pro_team')}) "
                     f"{c['projected_value']:+.2f}, beats {c['beats']} by "
                     f"{c['beats_by']:.2f}")

    if digest:
        idle = [r["name"] for r in digest.get("idle_starters", [])]
        L.append(f"\nTODAY ({digest.get('day')}): "
                 f"{digest.get('starters_playing')}/{digest.get('starters_total')} "
                 f"starters have a game."
                 + (f" Idle starters: {', '.join(idle)}." if idle else ""))
        flagged = digest.get("injuries", {}).get("flagged") or []
        if flagged:
            L.append("  Injury flags: " + ", ".join(
                f"{r['name']} {r['injury_status']}" for r in flagged))

    if schedule and schedule.get("weeks"):
        L.append("\nFIXTURE LOAD AHEAD:")
        for w in schedule["weeks"]:
            thin = ", ".join(f"{r['name']} ({r['games']})" for r in w["thin"])
            L.append(f"  week of {w['start']}: "
                     f"{w['total_starter_games']} starter games"
                     + (f"; thin: {thin}" if thin else ""))
        if not schedule.get("opponent_preview_available"):
            L.append("  Opponents beyond this week are NOT known — the league "
                     "cache carries no schedule.")

    if outlook:
        if outlook.get("playable"):
            L.append(f"\nSEASON: {outlook['remaining_matchups']} matchups left, "
                     f"currently {outlook['my_rank']} of {outlook['teams']}, "
                     f"top {outlook['playoff_spots']} qualify. Simulated "
                     f"playoff odds {outlook['playoff_odds'] * 100:.0f}%.")
        else:
            L.append(f"\nSEASON: {outlook.get('reason')}")

    L.append("\nCONSTRAINTS: this advice is not backtested; opponent totals are "
             "frozen so every gap is a minimum.")
    brief = "\n".join(L)
    return brief[:MAX_BRIEF_CHARS]


def narrate(brief: str, kind: str = "matchup", client=None,
            model: str = "claude-opus-5", max_tokens: int = 900) -> dict:
    """Send a brief to the model. Shared by every commentary button.

    `kind` selects a one-paragraph task line and nothing else; the honesty
    rules, the methodology standard and the failure handling are identical for
    all of them by construction. `client` is injectable so tests never call
    out. Raises NoCredits on a billing failure so the caller can distinguish
    "not verified yet" from "broken".
    """
    import anthropic

    if kind not in KINDS:
        raise ValueError(f"unknown narration kind {kind!r}; "
                         f"expected one of {sorted(KINDS)}")
    skill_text = ""
    if SKILL.exists():
        skill_text = SKILL.read_text()[:6000]
    system = SYSTEM + "\n\n" + KINDS[kind]
    if skill_text:
        system += ("\n\nThe project's statistical methodology standard, which "
                   "applies to how you qualify claims:\n\n" + skill_text)

    client = client or anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=model, max_tokens=max_tokens, system=system,
            messages=[{"role": "user", "content": brief}])
    except Exception as exc:
        msg = str(exc)
        if "credit balance is too low" in msg or "billing" in msg.lower():
            raise NoCredits(msg) from exc
        raise
    text = "".join(getattr(b, "text", "") for b in resp.content)
    usage = getattr(resp, "usage", None)
    return {
        "text": text.strip(),
        "kind": kind,
        "model": model,
        "brief_chars": len(brief),
        "input_tokens": getattr(usage, "input_tokens", None),
        "output_tokens": getattr(usage, "output_tokens", None),
        "verified_live": True,
    }


# ---------------------------------------------------------------------------
# The other three briefs
# ---------------------------------------------------------------------------
def compose_trade(trade: dict) -> str:
    """A proposed trade, by category.

    The brief deliberately carries the UNROUNDED net per category and the
    count of players each side sends, because those are the two things that
    decide whether "close" is the honest answer. It also carries the advisory
    verbatim so the model cannot present this as draft-board-grade.
    """
    a, b = trade.get("side_a", {}), trade.get("side_b", {})
    L = [f"TRADE: {a.get('team', 'A')} sends "
         f"{', '.join(p['name'] for p in a.get('players', [])) or 'nobody'}; "
         f"{b.get('team', 'B')} sends "
         f"{', '.join(p['name'] for p in b.get('players', [])) or 'nobody'}.",
         f"Player counts: {len(a.get('players', []))} for "
         f"{len(b.get('players', []))}.",
         f"Summed draft-board value: {a.get('team', 'A')} "
         f"{a.get('total_value', 0):+.2f} out, {b.get('team', 'B')} "
         f"{b.get('total_value', 0):+.2f} out; net to "
         f"{a.get('team', 'A')} {trade.get('value_delta_a', 0):+.2f}."]
    if trade.get("counts_note"):
        L.append(trade["counts_note"])
    L.append("\nCATEGORY NET to " + str(a.get("team", "A"))
             + " (positive means they gain it):")
    for c in trade.get("category_deltas", []):
        L.append(f"  {c['label']}: {c['a_net']:+.3f} "
                 f"(sends {c['a_gives']:+.2f}, receives {c['b_gives']:+.2f})")
    gained = trade.get("gained_by_a") or []
    lost = trade.get("lost_by_a") or []
    L.append(f"\nCategories gained by {a.get('team', 'A')}: "
             f"{', '.join(gained) or 'none above the 0.5 threshold'}.")

    none_msg = "none above the 0.5 threshold"
    L.append(f"Categories given up: {', '.join(lost) or none_msg}.")
    missing = (a.get("missing") or []) + (b.get("missing") or [])
    if missing:
        L.append(f"NOT VALUED, because they are not on the draft board: "
                 f"{missing}. Any verdict is incomplete.")
    L.append("\nCONSTRAINTS: " + str(trade.get("advisory", "")))
    L.append("These are z-scores against the draft pool, not points. A net "
             "under about 0.5 in a category is noise at this resolution.")
    return "\n".join(L)[:MAX_BRIEF_CHARS]


def compose_outlook(payload: dict) -> str:
    """Standing, remaining schedule and today's state."""
    o = payload.get("outlook") or {}
    dg = payload.get("digest") or {}
    L = [f"SEASON OUTLOOK: {payload.get('season_label')}."]
    if o.get("playable"):
        L.append(f"{o['remaining_matchups']} matchups remain of "
                 f"{o['total_matchups']}. Currently {o['my_rank']} of "
                 f"{o['teams']}; the top {o['playoff_spots']} qualify.")
        L.append(f"Simulated playoff odds {o['playoff_odds'] * 100:.0f}%, "
                 f"mean finish {o.get('mean_finish', 0):.1f}.")
        for a in o.get("assumptions", []):
            L.append(f"  assumption: {a}")
    else:
        L.append(f"Not simulable: {o.get('reason')}")
        L.append(f"Final position {o.get('my_rank')} of {o.get('teams')}; "
                 f"top {o.get('playoff_spots')} qualify.")
    standings = o.get("standings") or []
    if standings:
        L.append("\nSTANDINGS (category wins, ties count half):")
        for i, t in enumerate(standings, 1):
            mark = "  <- this team" if t.get("is_me") else ""
            L.append(f"  {i:>2}. {t['name']}: {t['wins']}-{t['losses']}-"
                     f"{t['ties']} ({t['points']:.1f}){mark}")
    if dg:
        L.append(f"\nTODAY ({dg.get('day')}): "
                 f"{dg.get('starters_playing')}/{dg.get('starters_total')} "
                 f"starters have a game.")
        flagged = (dg.get("injuries") or {}).get("flagged") or []
        if flagged:
            L.append("  injury flags: " + ", ".join(
                f"{r['name']} {r['injury_status']}" for r in flagged))
    sched = payload.get("schedule") or {}
    for w in (sched.get("weeks") or []):
        thin = ", ".join(f"{r['name']} ({r['games']})" for r in w.get("thin", []))
        L.append(f"  week of {w['start']}: {w['total_starter_games']} starter "
                 f"games" + (f"; thin: {thin}" if thin else ""))
    L.append("\nCONSTRAINTS: the simulation draws opponents at random because "
             "the league cache carries no schedule; categories are treated as "
             "independent, which understates how correlated a good week is.")
    return "\n".join(L)[:MAX_BRIEF_CHARS]


def compose_retrospective(retro: dict) -> str:
    """The season-long pattern, which is the point -- not the week list."""
    if not retro.get("available"):
        return f"RETROSPECTIVE unavailable: {retro.get('reason')}"
    L = [f"LINEUP RETROSPECTIVE, season {retro.get('season')}, "
         f"{retro.get('n_weeks')} weeks, objective '{retro.get('objective')}'.",
         f"The optimiser's lineup took more categories in "
         f"{retro.get('optimiser_won')} weeks, fewer in "
         f"{retro.get('optimiser_lost')}, level in {retro.get('level')}; "
         f"mean {retro.get('mean_cats_won', 0):.2f} of 9."]
    L.append("\nPER CATEGORY — how often the optimiser's lineup was better:")
    for c in sorted(retro.get("by_category", []), key=lambda x: -x["rate"]):
        L.append(f"  {c['category']}: {c['weeks_better']}/{c['weeks']} weeks "
                 f"({c['rate'] * 100:.0f}%)")
    L.append("\nThe objective maximises spread-scaled production across all "
             "nine categories, with turnovers subtracted and the two "
             "percentages entering as volume-weighted impact.")
    if retro.get("projection_basis"):
        L.append(retro["projection_basis"])
    L.append("CONSTRAINTS: " + str(retro.get("limitation", "")))
    return "\n".join(L)[:MAX_BRIEF_CHARS]


def compose_recap(retro: dict, waivers: dict, outlook: dict) -> str:
    """The season in one brief: lineup cost, missed wire, final standing."""
    L = [f"SEASON RECAP."]
    if outlook:
        L.append(f"Finished {outlook.get('my_rank')} of {outlook.get('teams')}; "
                 f"top {outlook.get('playoff_spots')} made the playoffs.")
        for t in (outlook.get("standings") or [])[:3]:
            L.append(f"  top of the table: {t['name']} ({t['points']:.1f})")
        me = next((t for t in (outlook.get("standings") or [])
                   if t.get("is_me")), None)
        if me:
            L.append(f"  this team: {me['wins']}-{me['losses']}-{me['ties']} "
                     f"({me['points']:.1f} category points)")
    L.append("\n" + compose_retrospective(retro))
    if waivers and waivers.get("available"):
        L.append(f"\nWAIVER WIRE, IN HINDSIGHT. The weakest player held all "
                 f"season was {waivers['weakest_held']} at "
                 f"{waivers['bar']:+.2f}. Unrostered players who beat that:")
        for c in waivers.get("candidates", [])[:8]:
            L.append(f"  {c['name']}: {c['value']:+.2f} over {c['games']} "
                     f"games, {c['beats_by']:+.2f} better")
        L.append(f"  CAVEAT: {waivers.get('caveat')}")
    elif waivers:
        L.append(f"\nWAIVER WIRE: not available — {waivers.get('reason')}")
    L.append("\nCONSTRAINTS: the lineup comparison holds one lineup constant "
             "across every week, and waiver availability is approximated from "
             "end-of-season rosters. Neither is a transaction log.")
    return "\n".join(L)[:MAX_BRIEF_CHARS]
