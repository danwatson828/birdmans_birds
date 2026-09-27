#!/usr/bin/env python3
"""Ad hoc NBA stat question answering: a hand-rolled Claude tool-use loop.

This is the learning-exercise harness from design spec section 6 -- a manual
`while stop_reason == "tool_use"` loop rather than the SDK tool runner or the
Claude Agent SDK, so the mechanism is visible: system prompt + tool definitions
+ question in, Claude decides whether to call a tool, the tool runs, the result
goes back, Claude decides again.

One tool: run_sql. It reads nba_snapshot.duckdb and never the live nba.duckdb --
ingestion owns the live file and holds an exclusive lock on it, and the snapshot
is only published after ingestion completes cleanly AND tests/ passes, so the
snapshot is the only database that has been verified.

Usage:
    python agent.py "how many triple doubles did Jokic have in 2024-25?"
    python agent.py --battery            # run the regression question set
    python agent.py --show-sql "..."     # echo each query as it runs
"""

from __future__ import annotations

import argparse
import json
import sys
import textwrap
import time
from pathlib import Path

import duckdb
from dotenv import load_dotenv

try:
    import anthropic
except ImportError:
    sys.exit("pip install anthropic")

PROJECT_ROOT = Path(__file__).resolve().parent
SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
LIVE_DB = PROJECT_ROOT / "nba.duckdb"

MODEL = "claude-opus-5"
MAX_TOKENS = 16000
MAX_TURNS = 12          # loop guard: a runaway agent should stop, not spin
MAX_RESULT_ROWS = 60    # keep one tool result from swamping the context
MAX_RESULT_CHARS = 12000


# =============================================================================
# The tool
# =============================================================================
class SqlTool:
    """run_sql against the verified snapshot, read-only.

    The connection is opened read_only=True, so DuckDB itself rejects writes --
    the statement-prefix check below is defence in depth and a clearer error
    message, not the actual guarantee.
    """

    def __init__(self, db_path: Path):
        if not db_path.exists():
            raise FileNotFoundError(
                f"{db_path} does not exist. Run `python ingest.py --daily` to "
                f"publish a snapshot (it publishes only when ingestion is clean "
                f"and tests/ passes)."
            )
        self.db_path = db_path
        self.con = duckdb.connect(str(db_path), read_only=True)
        self.calls: list[dict] = []

    # Anything that is not a single read is refused before it reaches DuckDB.
    FORBIDDEN = ("insert", "update", "delete", "drop", "create", "alter",
                 "attach", "detach", "copy", "install", "load", "pragma",
                 "export", "import", "call")

    def run(self, query: str) -> str:
        started = time.monotonic()
        record = {"query": query, "error": None, "rows": 0}
        try:
            stripped = query.strip().rstrip(";").strip()
            lowered = stripped.lower()
            if not (lowered.startswith("select") or lowered.startswith("with")):
                raise ValueError("only SELECT / WITH queries are allowed")
            if ";" in stripped:
                raise ValueError("one statement per call; remove the ';'")
            first = lowered.split(None, 1)[0]
            if first in self.FORBIDDEN:
                raise ValueError(f"'{first}' is not allowed; this tool is read-only")

            cur = self.con.execute(stripped)
            columns = [d[0] for d in cur.description]
            rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
            truncated = len(rows) > MAX_RESULT_ROWS
            rows = rows[:MAX_RESULT_ROWS]
            record["rows"] = len(rows)

            body = json.dumps(
                {"columns": columns,
                 "rows": [[_jsonable(v) for v in row] for row in rows]},
                default=str)
            if truncated:
                body += (f"\n\n[truncated at {MAX_RESULT_ROWS} rows -- add "
                         f"LIMIT/aggregation if you need more]")
            if len(body) > MAX_RESULT_CHARS:
                body = body[:MAX_RESULT_CHARS] + "\n\n[result truncated by size]"
            return body
        except Exception as exc:
            # Errors go back to the model as text, not as exceptions: a bad query
            # is information the agent can act on, and it usually self-corrects.
            record["error"] = f"{type(exc).__name__}: {exc}"
            return f"ERROR: {record['error']}"
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            self.calls.append(record)


def _jsonable(v):
    import datetime as _dt
    import decimal as _dec
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, _dec.Decimal):
        return float(v)
    return v


TOOL_DEF = {
    "name": "run_sql",
    "description": textwrap.dedent("""\
        Run one read-only SQL query (DuckDB dialect) against the NBA database and
        get back columns plus rows as JSON.

        One statement per call, SELECT or WITH only. Results are capped at 60
        rows, so aggregate or LIMIT rather than selecting raw detail. If the
        query errors, the error text comes back to you -- read it and fix the
        query. Call this tool as many times as you need, including to explore
        the data before answering."""),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "A single DuckDB SELECT or WITH statement."}
        },
        "required": ["query"],
        "additionalProperties": False,
    },
}


# =============================================================================
# Schema description, generated from the database rather than hardcoded
# =============================================================================
def describe_schema(con) -> str:
    """Introspect the live schema so the prompt can never drift from reality."""
    out = []
    for kind, label in (("BASE TABLE", "RAW TABLES"), ("VIEW", "SEMANTIC VIEWS")):
        names = [r[0] for r in con.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema='main' AND table_type=? "
            "AND table_name NOT LIKE '\\_%' ESCAPE '\\' ORDER BY table_name",
            [kind]).fetchall()]
        out.append(f"\n{label}")
        for name in names:
            cols = [r[0] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema='main' AND table_name=? ORDER BY ordinal_position",
                [name]).fetchall()]
            out.append(f"  {name}({', '.join(cols)})")
    macros = [r[0] for r in con.execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() "
        "WHERE internal=false ORDER BY 1").fetchall()]
    out.append(f"\nMACROS\n  {', '.join(macros)}")
    return "\n".join(out)


SYSTEM_PROMPT = """\
You answer questions about NBA statistics by querying a DuckDB database with the
run_sql tool, then giving a direct answer in plain language.

The database covers every NBA season from 1946-47 through 2025-26: 1,685,039
player-game box score lines, 73,174 games, 5,147 players.

## Use the semantic views, not the raw tables

The views below the raw tables are the canonical definitions of every derived
metric. Prefer them whenever one already covers what is being asked, instead of
re-deriving the logic in your own SQL. This is not a style preference: if you
compute true shooting percentage or a per-36 rate by hand, your answer can
disagree with the same question asked another way, and nobody can tell which is
right. v_player_games is the spine for anything per-player-per-game.

Reach for the raw tables only when no view covers what you need, and say so in
your answer when you do.

## Landmines. These are real defects that produced wrong answers here before.

1. SEASON TYPE -- use game_type, never the raw `postseason` flag.
   `postseason = false` is NOT "regular season". It also contains play-in games
   and the NBA Cup final, neither of which counts toward official regular-season
   statistics. game_type splits them properly: 'regular', 'play_in',
   'cup_final', 'playoff'. Using the raw flag reported Shai Gilgeous-Alexander
   at 32.5 ppg over 77 games when the official figure is 32.7 over 76. For
   anything compared against published statistics, filter game_type='regular'.

2. TEAM CONTEXT -- never use players.team_id for a historical question.
   players.team_id is a snapshot of where a player is NOW, and it is not even
   reliably current. For "how did X do for team Y", use the per-game team_id on
   v_player_games / v_player_season_totals, which is the team the player
   actually played for in that game and therefore survives mid-season trades.
   Luka Doncic's players.team_id is the Lakers; asking about his Dallas games
   through that column returns nothing. In a 3,000-row sample, 56% of rows had a
   per-game team_id that disagreed with players.team_id.

3. STAT ERAS -- check v_stat_first_tracked before any career-spanning or
   all-time question about steals, blocks, turnovers, offensive/defensive
   rebounds, three-pointers, or plus_minus.
   These stats did not exist for most of league history. Steals and blocks begin
   in the 1970s, turnovers in 1963, three-pointers in 1979 -- and several debut
   with a single recorded row before becoming dependable a decade later.
   v_stat_first_tracked gives first_tracked_season (first appearance anywhere)
   and first_full_season (first season reliably >=95% complete; use this one for
   comparisons). A NULL means "not recorded", not "did not happen", and SUM and
   AVG skip NULLs silently -- so an "all-time steals leaders" query ranks players
   partly by whether the league was recording steals while they played. Query
   v_stat_first_tracked, scope the query, and SAY SO in your answer. An
   unqualified all-time leaderboard for these stats is wrong even when the names
   happen to look plausible.

4. PLUS_MINUS IS NEVER FULLY POPULATED -- check games_with_plus_minus.
   It starts in 2019 and one season (2023-24) is only ~46% complete even for
   players who played. v_player_season_totals carries games_with_plus_minus
   alongside the plus_minus sum: a +140 over 12 recorded games and a +140 over 82
   are indistinguishable without it. Never present a plus_minus total or average
   without checking how many games it actually covers, and never compare two
   players' plus_minus without confirming both have comparable coverage.

## Also worth knowing

- season is the START year: season=2024 is the 2024-25 season.
- is_dnp marks a row for a player who did not play. games_played in the season
  views already excludes them; games_on_roster does not.
- minutes are a string in the raw table ("30" or "24:33"); minutes_played on
  v_player_games is the parsed number. Never parse it yourself.
- true shooting percentage and eFG% are unreliable before 1974 because field
  goal attempts were barely recorded. Check v_stat_first_tracked for fga.
- is_home on v_player_games is from the player's perspective, so the road team
  is `NOT is_home`.

## How to answer

Explore with as many queries as you need. When a question is ambiguous in a way
that changes the answer -- an undefined threshold, an unstated season range, a
metric that could mean several things -- do not silently pick one. Either ask,
or state the interpretation you used and give the number for it; if two readings
give materially different answers, give both.

Answer in prose, with the number first. Name the filters that produced it
(season range, game_type, any minimum). Flag data limitations that affect the
answer rather than burying them. Do not show SQL unless asked.
"""


# =============================================================================
# The loop
# =============================================================================
def ask(client, tool: SqlTool, question: str, schema: str,
        show_sql: bool = False, show_thinking: bool = False) -> dict:
    system = [{"type": "text",
               "text": SYSTEM_PROMPT + "\n## Schema\n" + schema,
               # The system prompt and schema are identical across questions, so
               # cache them: with a battery of questions this is the bulk of the
               # input tokens.
               "cache_control": {"type": "ephemeral"}}]
    messages = [{"role": "user", "content": question}]
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    turns = 0

    while True:
        turns += 1
        if turns > MAX_TURNS:
            return {"answer": f"[stopped: exceeded {MAX_TURNS} turns]",
                    "turns": turns, "usage": usage, "calls": tool.calls}

        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=[TOOL_DEF],
            thinking={"type": "adaptive",
                      "display": "summarized" if show_thinking else "omitted"},
            messages=messages,
        )
        u = response.usage
        usage["input"] += u.input_tokens
        usage["output"] += u.output_tokens
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            return {"answer": "[refused]", "turns": turns, "usage": usage,
                    "calls": tool.calls}

        if show_thinking:
            for b in response.content:
                if b.type == "thinking" and b.thinking:
                    print(f"    [thinking] {b.thinking[:400]}", flush=True)

        if response.stop_reason != "tool_use":
            answer = "\n".join(b.text for b in response.content if b.type == "text")
            return {"answer": answer.strip(), "turns": turns, "usage": usage,
                    "calls": tool.calls}

        messages.append({"role": "assistant", "content": response.content})

        # All tool_result blocks for one assistant turn go back in ONE user
        # message -- splitting them teaches the model to stop batching calls.
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            query = block.input.get("query", "")
            if show_sql:
                print(f"    [sql] {' '.join(query.split())[:300]}", flush=True)
            results.append({"type": "tool_result",
                            "tool_use_id": block.id,
                            "content": tool.run(query)})
        messages.append({"role": "user", "content": results})


BATTERY = [
    ("flagship / direct countable",
     "How many times has a player scored 40+ points with 12+ rebounds in a game "
     "as the road team?"),
    ("career leaderboard needing the era guard",
     "Who are the career steals leaders?"),
    ("trade-split logic",
     "How many points did Luka Doncic score for Dallas in 2024-25, and how does "
     "that compare to his total for the whole season?"),
    ("deliberately ambiguous",
     "Who's been the most efficient scorer lately?"),
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="*", help="the question to answer")
    ap.add_argument("--battery", action="store_true",
                    help="run the standard regression question set")
    ap.add_argument("--db", type=Path, default=SNAPSHOT)
    ap.add_argument("--show-sql", action="store_true")
    ap.add_argument("--show-thinking", action="store_true")
    args = ap.parse_args()

    if args.db.resolve() == LIVE_DB.resolve():
        sys.exit("refusing to query the live database; the agent reads the "
                 "snapshot, which is the only verified copy")

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        sys.exit(f"could not construct the Anthropic client: {exc}")

    questions = ([(label, q) for label, q in BATTERY] if args.battery
                 else [("ad hoc", " ".join(args.question))])
    if not args.battery and not questions[0][1]:
        ap.error("give a question, or --battery")

    probe = duckdb.connect(str(args.db), read_only=True)
    schema = describe_schema(probe)
    probe.close()

    grand = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for label, question in questions:
        tool = SqlTool(args.db)          # fresh call log per question
        print("=" * 78)
        print(f"[{label}]")
        print(f"Q: {question}")
        print("=" * 78, flush=True)
        t0 = time.monotonic()
        try:
            out = ask(client, tool, question, schema,
                      show_sql=args.show_sql, show_thinking=args.show_thinking)
        except anthropic.APIStatusError as exc:
            print(f"API error {exc.status_code}: {exc.message}")
            return 1
        except anthropic.APIConnectionError as exc:
            print(f"connection error: {exc}")
            return 1
        print(f"\n{out['answer']}\n")
        for k in grand:
            grand[k] += out["usage"][k]
        errs = sum(1 for c in out["calls"] if c["error"])
        print(f"-- {out['turns']} model turns, {len(out['calls'])} queries "
              f"({errs} errored), {time.monotonic()-t0:.1f}s, "
              f"in={out['usage']['input']} out={out['usage']['output']} "
              f"cache_read={out['usage']['cache_read']}")
        tool.con.close()

    # Opus 5: $5/MTok in, $25/MTok out; cache reads bill at ~0.1x input.
    cost = (grand["input"] * 5 + grand["cache_write"] * 6.25
            + grand["cache_read"] * 0.5 + grand["output"] * 25) / 1e6
    print("=" * 78)
    print(f"total tokens in={grand['input']} out={grand['output']} "
          f"cache_read={grand['cache_read']} cache_write={grand['cache_write']}")
    print(f"approx cost ${cost:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
