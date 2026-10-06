#!/usr/bin/env python3
"""MCP server exposing this project's verified tools to Claude Desktop.

WHY THIS EXISTS. The web app's Investigate button was removed on the claim
that Claude Desktop already covered it over MCP. No MCP server existed, so
investigation had no path at all. This is that path, built rather than
asserted.

WHAT IS AND IS NOT NEW HERE. Nothing about the tools is new. `run_sql`,
`run_analysis` and `check_coverage` call `agent.SqlTool`, `agent.AnalysisTool`
and `agent.CoverageTool` -- the same classes the web app and the CLI use, so
the determinism guard (execute-3x-and-compare plus the static AST check on
order-sensitive aggregates) applies here unchanged. `save_analysis` writes
through `analyses.save_run`, the one writer. If any of that is wrong here it
is wrong everywhere, which is the point.

THE METHODOLOGY INSTRUCTION LIVES IN THE TOOL DESCRIPTIONS. An MCP server has
no system prompt of its own -- the host's model reads tool descriptions and
nothing else this server writes. So the standard that was validated as
load-bearing in the Phase 8 A/B test is embedded in the descriptions of the
tools it governs, where the model will actually encounter it, rather than in
a system prompt that does not exist on this side of the protocol.

    python mcp_server.py                 # stdio, for Claude Desktop
    python mcp_server.py --selftest      # exercise every tool, no host needed
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT))

import agent                      # noqa: E402  (path set above)
import analyses                   # noqa: E402

SNAPSHOT = analyses.SNAPSHOT

# ---------------------------------------------------------------------------
# The standard, stated once and pasted into the descriptions that need it.
# ---------------------------------------------------------------------------
METHODOLOGY = """
BEFORE running this on any question about whether two things are related,
whether a difference is real, whether one group outperforms another, or what
predicts what, you MUST state your methodology first, in this order:

  SAMPLE       exactly which rows -- season range, game_type, and any minimum
               games or minutes threshold, and why that threshold not another
  COMPARISON   what is compared against what. If the question is purely
               descriptive with no comparison group, say so explicitly
  CONFOUNDS    what could produce this result other than the effect claimed,
               and which of those you are handling
  NOT HANDLED  the confounds you are NOT addressing. This list is almost never
               empty and writing "none" is nearly always wrong

Then run the analysis. Then report. Any claim resting on a model or a
correlation reports, every time: the effect size in the units of the question,
its 95% confidence interval, R-squared, the p-value, the sample size, and a
residual or robustness check. A bare coefficient is not a result.
""".strip()

ERA_WARNING = """
Stats are not recorded uniformly across this database's 1946-2025 range.
Steals, blocks and turnovers begin in 1973-74; three-pointers in 1979-80;
minutes are absent before 1951. Call check_coverage BEFORE any query whose
span could reach into an era where the stat was not tracked -- a leaderboard
or a trend that silently averages over untracked seasons is wrong in a way
that looks completely normal.
""".strip()


SAVE_OBLIGATION = """
AFTER you finish a substantive analysis using this tool, you MUST call
save_analysis. Not optional, and not only when the result seems interesting:
a rejected hypothesis and a null result are exactly the records this store
exists to keep. An analysis that is not saved did not happen as far as the
knowledge base and the Ask view are concerned. (The Validated Findings
page is narrower: it lists only investigate_relationship runs that
declared a held-out era and committed to a verdict.)

Substantive means any question that needed a real query or a real
calculation. A one-line lookup does not need saving; anything you would write
more than a sentence about does.
""".strip()


def _tools():
    """One connection and one tool set per process, built lazily.

    The agent's own tools hold a DuckDB connection; opening one per call would
    reopen a 174MB snapshot on every request.
    """
    if not hasattr(_tools, "_cache"):
        sql = agent.SqlTool(SNAPSHOT)
        _tools._cache = {
            "sql": sql,
            "analysis": agent.AnalysisTool(sql),
            "coverage": agent.CoverageTool(sql.con),
        }
    return _tools._cache


# The steps of the analysis that has not been saved yet, in the order they ran.
#
# WHY THIS EXISTS. The agent's tool objects accumulate `.calls` for the life of
# the process, and Claude Desktop keeps one server process -- so one tool set --
# across every request it makes. Reading `.calls` at save time therefore
# attached the whole session's history to whatever was saved next: analysis #86
# was saved claiming to have executed #82's queries, and the single
# check_coverage call of the session was replayed into five separate runs. A
# provenance log that reports work the analysis did not do is worse than no log,
# because it reads exactly like a real one.
#
# Draining on save is the deliberate semantics: a step belongs to exactly one
# analysis. Saving twice with no work in between gives the second run zero
# steps, which is the honest answer rather than a duplicate of the first.
_PENDING: list[dict] = []
_SEEN: dict[str, int] = {}


def _step(tool_name: str, rec: dict) -> dict:
    """One tool_log entry, in the shape agent.ask builds.

    save_run persists out["tool_log"], not out["calls"] -- passing the latter
    saved the run with zero steps, which is exactly the kind of silent partial
    write this project treats as a defect. Matching agent.ask's shape is what
    makes an MCP-saved analysis and a CLI-saved one indistinguishable in the
    store.
    """
    return {
        "tool": tool_name,
        "input": {k: rec.get(k) for k in ("query", "code", "stats")
                  if rec.get(k) is not None},
        **{k: rec.get(k) for k in (
            "executed_sql", "executed_code", "output_text", "result_columns",
            "rows", "error", "seconds", "stability", "compare_mode",
            "order_varied", "execution_used", "cause", "feeder_stability")},
    }


def _capture(tool_name: str, key: str) -> None:
    """Move whatever the tool just appended to .calls onto the pending log.

    Called after every tool invocation rather than once at save time, so the
    pending log preserves the real interleaving of SQL, analysis and coverage
    calls. The previous code concatenated all SQL, then all analysis, then all
    coverage, which reordered the steps of even a correctly-scoped run.
    """
    calls = _tools()[key].calls
    for rec in calls[_SEEN.get(key, 0):]:
        _PENDING.append(_step(tool_name, rec))
    _SEEN[key] = len(calls)


def build_server():
    from mcp.server.mcpserver import MCPServer

    mcp = MCPServer(
        name="nba-analytics",
        instructions=(
            "Read-only analytics over 80 seasons of NBA box scores "
            "(1946-47 to 2025-26), plus this project's saved-analysis store. "
            "Every query runs against a published snapshot, never the live "
            "database. " + ERA_WARNING),
    )

    @mcp.tool(
        name="run_sql",
        description=(
            "Run one read-only SELECT against the NBA snapshot and return the "
            "rows.\n\n"
            "Every statement is executed three times across different DuckDB "
            "thread counts and the results compared, so a query whose row "
            "order is not fully determined by its ORDER BY is REJECTED rather "
            "than returned. If you get that error, add enough columns to the "
            "ORDER BY to make the order unique -- it is a real defect in the "
            "query, not a flaky tool.\n\n"
            "Any ORDER BY ... LIMIT must include enough tie-breaking columns "
            "to guarantee a unique order.\n\n" + ERA_WARNING
            + "\n\n" + SAVE_OBLIGATION),
    )
    def run_sql(query: str) -> str:
        """Execute one SELECT against the snapshot."""
        out = _tools()["sql"].run(query)
        _capture("run_sql", "sql")
        return out

    @mcp.tool(
        name="run_analysis",
        description=(
            "Run Python statistics over a dataframe drawn from the snapshot. "
            "`query` supplies the rows as a pandas DataFrame named `df`; "
            "`code` is Python whose stdout is the result. numpy, pandas, "
            "scipy and statsmodels are available. The sandbox is "
            "write-blocked and network-blocked.\n\n"
            + METHODOLOGY + "\n\n" + ERA_WARNING
            + "\n\n" + SAVE_OBLIGATION),
    )
    def run_analysis(query: str, code: str) -> str:
        """Statistics over a dataframe from the snapshot."""
        out = _tools()["analysis"].run(query, code)
        _capture("run_analysis", "analysis")
        return out

    @mcp.tool(
        name="check_coverage",
        description=(
            "Report, per stat, the first season it was tracked at all and the "
            "first season it was reliably recorded, computed live from the "
            "database rather than read from a document that can go stale.\n\n"
            "Call this BEFORE any query spanning eras. A career leaderboard "
            "for steals that reaches back past 1973-74 is not wrong-looking, "
            "it is just wrong."),
    )
    def check_coverage(stats: list[str]) -> str:
        """Per-stat era coverage, computed live."""
        out = _tools()["coverage"].run(stats)
        _capture("check_coverage", "coverage")
        return out

    @mcp.tool(
        name="investigate_relationship",
        description=(
            "Compose the skeptical framing for testing a suspected "
            "relationship, then follow it yourself using run_analysis.\n\n"
            "This tool does NOT run the investigation. It returns the "
            "protocol, because the discipline is the thing that matters and "
            "it has to be visible in your reasoning rather than hidden "
            "inside a tool call.\n\n"
            "The framing is deliberate. A prompt that asks to confirm a "
            "relationship gets a confirmation. Most exploratory correlations "
            "do not survive contact with a held-out era, and NOT CONFIRMED is "
            "a perfectly good answer -- do not strain to rescue a finding.\n\n"
            + METHODOLOGY),
    )
    def investigate_relationship(x: str, y: str, season_from: int = 2000,
                                 season_to: int = 2025, min_games: int = 41,
                                 game_type: str = "regular",
                                 observed: str = "") -> str:
        """Return the investigation protocol for an x-y relationship."""
        obs = f"\n\nWhat was observed: {observed}" if observed else ""
        return (
            f"INVESTIGATE: a suspected relationship between {x} and {y}, at "
            f"player-season grain, seasons {season_from}-{season_to}, "
            f"game_type='{game_type}', minimum {min_games} games played.{obs}\n\n"
            "This is a hypothesis, very possibly a chance pattern or an "
            "artefact. Test it properly and tell the user whether it "
            "survives.\n\n"
            "1. BEFORE running anything, nominate a discovery era and a "
            "held-out era by season, and say which is which. Naming them "
            "afterwards is not a held-out test.\n"
            "2. Call check_coverage for every stat involved. If either is not "
            "reliably tracked across the window, say so and narrow the window "
            "rather than computing over incomplete data.\n"
            "3. Estimate the effect in the discovery era.\n"
            "4. Test it on the held-out era you named.\n"
            "5. Consider what else could produce it: a shared era trend, a "
            "position or role confound, selection from the games threshold, "
            "pseudo-replication if either variable is constant within a "
            "player across seasons, or a mechanical link between the two "
            "measures. Handle each one you can and say which you could not.\n"
            "6. State a verdict, in these words: NOT CONFIRMED, CONFIRMED, or "
            "INCONCLUSIVE, with the reason.\n\n"
            "Most exploratory correlations do not survive, and that is a "
            "perfectly good answer -- do not strain to rescue this one. "
            "NOT CONFIRMED does not need softening.\n\n"
            + METHODOLOGY)

    @mcp.tool(
        name="save_analysis",
        description=(
            "CALL THIS after completing any substantive analysis. It is a "
            "required final step, not an optional extra.\n\n"
            "Saving puts the run in this project's store, where it appears in "
            "the web app's Ask list and knowledge base alongside runs made "
            "from the CLI. The web app "
            "saves every run unconditionally; over MCP it is a call you have "
            "to make, so forgetting it silently loses the work.\n\n"
            "Save the WHOLE argument, not a summary: the methodology you "
            "stated, the question as asked, and the verdict including its "
            "caveats. An entry whose reasoning cannot be inspected is worse "
            "than no entry. Save null and negative results too — a rejected "
            "hypothesis is a finding.\n\n"
            "The SQL and analysis steps are attached automatically from what "
            "you ran this session; you do not need to repeat them."),
    )
    def save_analysis(question: str, answer: str, methodology: str = "",
                      label: str = "mcp") -> str:
        """Persist an analysis through the project's single writer."""
        now = dt.datetime.now().astimezone()
        started = now.isoformat(timespec="seconds")
        # Drain, not read: these steps belong to this analysis and to no other.
        tool_log = list(_PENDING)
        _PENDING.clear()
        sql_calls = [s for s in tool_log if s["tool"] == "run_sql"]
        out = {
            "answer": answer,
            "methodology": methodology or None,
            "calls": sql_calls,
            "analysis_calls": [s for s in tool_log
                               if s["tool"] == "run_analysis"],
            "coverage_calls": [s for s in tool_log
                               if s["tool"] == "check_coverage"],
            "tool_log": tool_log,
            "charts": [],
            "turns": 0,
            "order": [],
            "skills_loaded": [],
            "usage": {"input": 0, "output": 0,
                      "cache_read": 0, "cache_write": 0},
        }
        rid = analyses.save_run(
            out, question=question, label=label, model="mcp-host",
            started_at=started,
            finished_at=dt.datetime.now().astimezone().isoformat(
                timespec="seconds"),
            cost_usd=0.0, snapshot=SNAPSHOT)
        note = "" if tool_log else (
            " NOTE: no SQL or analysis steps had run since the last save, so "
            "this entry has no provenance log.")
        return (f"Saved as analysis #{rid} with {len(tool_log)} recorded "
                f"step(s).{note} "
                f"View: python analyses.py show {rid}")

    @mcp.tool(
        name="refresh_nba_data",
        description=(
            "Manually trigger NBA ingestion and publish a new snapshot. This "
            "runs ALONGSIDE the scheduled daily job and does not replace it; "
            "use it when you need data fresher than the last scheduled run.\n\n"
            "Slow -- minutes, not seconds. It publishes a snapshot only if "
            "ingestion completes cleanly, so a failed run leaves the current "
            "snapshot in place."),
    )
    def refresh_nba_data(season: int | None = None, dry_run: bool = True) -> str:
        """Run ingest.py. Defaults to a dry run because it writes."""
        cmd = [sys.executable, str(PROJECT_ROOT / "ingest.py"), "--daily"]
        if season:
            cmd += ["--season", str(season)]
        if dry_run:
            return ("DRY RUN. This would run:\n  " + " ".join(cmd) +
                    "\n\nIt writes to the live database and republishes the "
                    "snapshot, which changes the fingerprint every saved "
                    "analysis recorded. Call again with dry_run=false to "
                    "actually run it.")
        return _run_subprocess(cmd, timeout=3600)

    @mcp.tool(
        name="refresh_espn_data",
        description=(
            "Manually trigger the read-only ESPN fantasy league sync (roster, "
            "matchup, standings, free agents). Runs alongside the scheduled "
            "job rather than replacing it.\n\n"
            "Read-only against ESPN; it writes only this project's local "
            "cache."),
    )
    def refresh_espn_data(dry_run: bool = True) -> str:
        """Run league_sync.py."""
        cmd = [sys.executable, str(PROJECT_ROOT / "league_sync.py")]
        if dry_run:
            return ("DRY RUN. This would run:\n  " + " ".join(cmd) +
                    "\n\nIt is read-only against ESPN and refreshes the local "
                    "cache. Call again with dry_run=false to actually run it.")
        return _run_subprocess(cmd, timeout=900)

    return mcp


def _run_subprocess(cmd: list[str], timeout: int) -> str:
    """Run a project script and return a bounded transcript.

    Bounded because an ingestion log is thousands of lines and the host will
    put all of it in the model's context otherwise.
    """
    try:
        p = subprocess.run(cmd, cwd=PROJECT_ROOT, capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return f"TIMED OUT after {timeout}s: {' '.join(cmd)}"
    tail = (p.stdout or "")[-3000:]
    err = (p.stderr or "")[-1500:]
    return (f"exit {p.returncode}\n\n--- stdout (tail) ---\n{tail}"
            + (f"\n\n--- stderr (tail) ---\n{err}" if err.strip() else ""))


# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------
# Two levels, because they prove different things and only one of them proves
# Claude Desktop can actually talk to this:
#
#   in-process  -- list_tools() and call_tool() on the server object. Proves
#                  registration, JSON schemas, dispatch and that each tool
#                  reaches the real agent code and returns real data.
#   stdio       -- spawn this file as a subprocess and speak newline-delimited
#                  JSON-RPC at it, which is exactly the transport Claude
#                  Desktop uses. Proves the handshake and the wire format.
#
# Neither is the same as a human driving Claude Desktop, and the report says so.
EXPECTED_TOOLS = {"run_sql", "run_analysis", "check_coverage",
                  "investigate_relationship", "save_analysis",
                  "refresh_nba_data", "refresh_espn_data"}


def _selftest_inprocess() -> int:
    import asyncio

    mcp = build_server()
    failures = []

    async def go():
        tools = await mcp.list_tools()
        names = {t.name for t in tools}
        print(f"  tools registered: {len(names)}")
        missing = EXPECTED_TOOLS - names
        extra = names - EXPECTED_TOOLS
        if missing:
            failures.append(f"missing tools: {sorted(missing)}")
        if extra:
            failures.append(f"unexpected tools: {sorted(extra)}")

        # The methodology standard must be reachable by the host model, which
        # only ever reads these descriptions.
        for t in tools:
            if t.name in ("run_analysis", "investigate_relationship"):
                if "SAMPLE" not in (t.description or ""):
                    failures.append(f"{t.name}: no methodology in description")
            if t.name in ("run_sql", "run_analysis", "check_coverage"):
                if not (t.description or "").strip():
                    failures.append(f"{t.name}: empty description")
            if not t.input_schema:
                failures.append(f"{t.name}: no input schema")

        def text(res):
            parts = getattr(res, "content", []) or []
            return "\n".join(getattr(p, "text", "") for p in parts)

        r = await mcp.call_tool("run_sql", {
            "query": "SELECT count(*) AS n FROM v_player_games "
                     "WHERE pts >= 40 AND reb >= 12 AND NOT is_home"})
        body = text(r)
        print(f"  run_sql           -> {body.strip()[:70]}")
        if "654" not in body:
            failures.append(f"run_sql did not return the known 654: {body[:120]}")

        r = await mcp.call_tool("check_coverage", {"stats": ["fg3m", "stl"]})
        body = text(r)
        print(f"  check_coverage    -> {body.strip().splitlines()[0][:70]}")
        if "1979" not in body:
            failures.append("check_coverage did not report the 1979 three-point era")

        r = await mcp.call_tool("run_analysis", {
            # ORDER BY with a unique tie-break: a bare LIMIT has no defined
            # row order and the determinism guard rejects it -- which it did,
            # on the second run of this very self-test.
            "query": "SELECT pts FROM v_player_games WHERE season = 2025 "
                     "AND NOT is_dnp ORDER BY game_id, player_id LIMIT 500",
            "code": "print('mean', round(df.pts.mean(), 4))"})
        body = text(r)
        print(f"  run_analysis      -> {body.strip()[:70]}")
        if "mean" not in body:
            failures.append(f"run_analysis produced no output: {body[:120]}")

        r = await mcp.call_tool("investigate_relationship", {
            "x": "Height", "y": "Blocks per game", "season_from": 2000,
            "season_to": 2025})
        body = text(r)
        print(f"  investigate       -> {len(body)} chars of protocol")
        for needed in ("held-out", "NOT CONFIRMED", "do not strain",
                       "check_coverage", "SAMPLE"):
            if needed.lower() not in body.lower():
                failures.append(f"investigate protocol omits {needed!r}")
        if body.lower().index("not confirmed") > body.lower().index("confirmed,"):
            failures.append("CONFIRMED is offered before NOT CONFIRMED")

        for tool in ("refresh_nba_data", "refresh_espn_data"):
            r = await mcp.call_tool(tool, {})
            body = text(r)
            print(f"  {tool:17s} -> {body.strip().splitlines()[0][:60]}")
            if "DRY RUN" not in body:
                failures.append(f"{tool} is not dry-run by default — it would "
                                "have actually run on a bare call")
        return failures

    asyncio.run(go())
    return failures


def _selftest_stdio() -> list[str]:
    """Spawn this file and speak JSON-RPC at it, as Claude Desktop would."""
    return _selftest_stdio_cmd([sys.executable, str(Path(__file__).resolve())])


def _selftest_stdio_cmd(cmd: list[str], strip_env: bool = False) -> list[str]:
    """Speak MCP to an arbitrary command. `strip_env` imitates a GUI launch,
    where PATH and the shell's environment are not what a terminal has."""
    import os

    failures = []
    env = ({"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin"}
           if strip_env else None)
    proc = subprocess.Popen(
        cmd,
        cwd="/" if strip_env else PROJECT_ROOT,
        env=env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, bufsize=1)

    def send(obj):
        proc.stdin.write(json.dumps(obj) + "\n")
        proc.stdin.flush()

    def read():
        line = proc.stdout.readline()
        return json.loads(line) if line.strip() else None

    try:
        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18",
                         "capabilities": {},
                         "clientInfo": {"name": "selftest", "version": "0"}}})
        init = read()
        server_name = (((init or {}).get("result") or {})
                       .get("serverInfo") or {}).get("name")
        print(f"  initialize        -> serverInfo.name = {server_name!r}")
        if server_name != "nba-analytics":
            failures.append(f"handshake returned {server_name!r}")
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
        listed = read()
        names = {t["name"] for t in
                 (((listed or {}).get("result") or {}).get("tools") or [])}
        print(f"  tools/list        -> {len(names)} tools over the wire")
        if names != EXPECTED_TOOLS:
            failures.append(f"tools/list over stdio returned {sorted(names)}")

        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "run_sql",
                         "arguments": {"query": "SELECT 654 AS n"}}})
        called = read()
        content = (((called or {}).get("result") or {}).get("content") or [])
        body = "\n".join(c.get("text", "") for c in content)
        print(f"  tools/call        -> {body.strip()[:60]}")
        if "654" not in body:
            failures.append(f"tools/call over stdio returned {body[:120]!r}")
    except Exception as exc:
        failures.append(f"stdio transport raised {type(exc).__name__}: {exc}")
        err = proc.stderr.read()[-800:] if proc.stderr else ""
        if err:
            failures.append(f"server stderr: {err}")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    return failures


def selftest() -> int:
    print("=" * 74)
    print("MCP SELF-TEST — in-process dispatch")
    print("=" * 74)
    f1 = _selftest_inprocess()
    print()
    print("=" * 74)
    print("MCP SELF-TEST — real stdio JSON-RPC, the transport Claude Desktop uses")
    print("=" * 74)
    f2 = _selftest_stdio()
    failures = list(f1) + list(f2)
    print()
    if failures:
        print(f"FAILED ({len(failures)}):")
        for f in failures:
            print("  -", f)
        return 1
    print("ALL PASS — tools registered, dispatched, and reached over stdio.")
    print("NOT verified here: a human actually driving Claude Desktop against "
          "this server. That needs the config installed and the app restarted.")
    return 0


CLAUDE_DIR = Path.home() / "Library" / "Application Support" / "Claude"
DESKTOP_CONFIG = CLAUDE_DIR / "claude_desktop_config.json"
EXT_DIR = CLAUDE_DIR / "Claude Extensions"
EXT_REGISTRY = CLAUDE_DIR / "extensions-installations.json"
EXT_SETTINGS = CLAUDE_DIR / "Claude Extensions Settings"
EXT_ID = "local.nba-analytics"


def _extension_entry() -> dict | None:
    """The extension's registry record, if it is installed and on disk."""
    if not EXT_REGISTRY.exists():
        return None
    try:
        reg = json.loads(EXT_REGISTRY.read_text())
    except json.JSONDecodeError:
        return None
    e = (reg.get("extensions") or {}).get(EXT_ID)
    if not e:
        return None
    if not (EXT_DIR / EXT_ID / "manifest.json").is_file():
        return None
    flag = EXT_SETTINGS / f"{EXT_ID}.json"
    enabled = True
    if flag.is_file():
        try:
            enabled = bool(json.loads(flag.read_text()).get("isEnabled", True))
        except json.JSONDecodeError:
            pass
    return {**e, "enabled": enabled}


def check_config() -> int:
    """Validate the Claude Desktop registration end to end.

    Worth having as a command rather than a one-off because Claude Desktop
    OWNS this file and rewrites it from its own model whenever preferences
    change -- which is often. An `mcpServers` entry added from outside the app
    survived about thirteen hours before being dropped, so "is it still
    registered" is a question that needs re-asking, not a thing established
    once.

    Checks, in order of how they actually fail:
      * the file parses at all
      * the entry exists
      * the command is an executable file -- the trap here is `.venv/bin/python`
        being a symlink whose resolved target is the SYSTEM interpreter, which
        has none of this project's packages
      * that interpreter can import mcp and duckdb
      * the script exists
      * the whole thing actually speaks MCP when launched the way the app
        launches it: from `/`, with a stripped environment
    """
    import os

    problems: list[str] = []
    print(f"config: {DESKTOP_CONFIG}")
    if not DESKTOP_CONFIG.exists():
        print("  MISSING — Claude Desktop has not created it yet")
        return 1
    try:
        cfg = json.loads(DESKTOP_CONFIG.read_text())
    except json.JSONDecodeError as exc:
        print(f"  MALFORMED JSON: {exc}")
        return 1
    print(f"  parses, {DESKTOP_CONFIG.stat().st_size} bytes, "
          f"top-level keys: {sorted(cfg)}")

    # Two registration mechanisms, and they fail differently. The extension
    # registry is the durable one -- measured: changing one unrelated Desktop
    # setting rewrote claude_desktop_config.json and dropped the mcpServers
    # key while leaving the extension untouched. Report whichever is live.
    entry = (cfg.get("mcpServers") or {}).get("nba-analytics")
    ext = _extension_entry()
    print(f"extension registry: "
          + (f"REGISTERED ({ext['version']}, {ext['source']})" if ext
             else "not registered"))
    if ext and not ext.get("enabled", True):
        problems.append("the extension is registered but disabled in Settings")
    if not entry and not ext:
        print("  NOT REGISTERED by either mechanism.")
        print("    Claude Desktop rewrites claude_desktop_config.json from "
              "its own state and drops keys it did not write, so a raw "
              "mcpServers entry disappears on the app's next save. The "
              "durable registration is the extension:")
        print("      python build_extension.py --install")
        return 1
    if entry and not ext:
        print("  WARNING: only the raw mcpServers entry is present. It will "
              "be dropped the next time Claude Desktop saves its "
              "preferences. Install the extension instead:")
        print("      python build_extension.py --install")

    if ext:
        mc = (ext.get("manifest") or {}).get("server", {}).get("mcp_config", {})
        cmd = mc.get("command")
        raw_args = mc.get("args") or []
        ext_dir = EXT_DIR / EXT_ID
        args = [a.replace("${__dirname}", str(ext_dir)) for a in raw_args]
        print(f"  extension command: {cmd} {' '.join(args)}")
    else:
        cmd, args = entry.get("command"), entry.get("args") or []
        print(f"  mcpServers command: {cmd} {' '.join(args)}")

    if not cmd or not os.access(cmd, os.X_OK):
        problems.append(f"command is not an executable file: {cmd!r}")
    else:
        real = os.path.realpath(cmd)
        if real != cmd:
            print(f"    note: resolves to {real}")
        probe = subprocess.run(
            [cmd, "-c", "import mcp, duckdb, agent; print('deps ok')"],
            capture_output=True, text=True, cwd=PROJECT_ROOT, timeout=120)
        if probe.returncode != 0:
            problems.append(
                "that interpreter cannot import this project's packages: "
                + (probe.stderr or "").strip().splitlines()[-1:][0]
                if probe.stderr else "unknown import error")
        else:
            print(f"    interpreter has the dependencies")
    if not args or not Path(args[0]).is_file():
        problems.append(f"script not found: {args[0] if args else '(none)'}")

    if not problems:
        print("  launching it the way Claude Desktop does "
              "(cwd=/, stripped environment)...")
        failures = _selftest_stdio_cmd([cmd] + list(args))
        problems.extend(failures)

    print()
    if problems:
        print(f"FAILED ({len(problems)}):")
        for p_ in problems:
            print("  -", p_)
        return 1
    print("OK — registered, the interpreter has the packages, and the server "
          "answers MCP when launched as the app launches it.")
    if ext:
        print("Registered as a Claude Extension, which survives Claude "
              "Desktop rewriting its preferences — measured, not assumed. "
              "Restart Desktop to pick up a change to the tools.")
    else:
        print("Registered only via mcpServers, which Claude Desktop WILL drop "
              "the next time it saves preferences. Install the extension: "
              "python build_extension.py --install")
    return 0


def install_config() -> int:
    """Write the mcpServers entry, backing up whatever is there first."""
    import shutil
    import time

    if not DESKTOP_CONFIG.exists():
        print(f"{DESKTOP_CONFIG} does not exist; open Claude Desktop once.")
        return 1
    shutil.copy2(DESKTOP_CONFIG, f"{DESKTOP_CONFIG}.bak-{int(time.time())}")
    cfg = json.loads(DESKTOP_CONFIG.read_text())
    cfg.setdefault("mcpServers", {})["nba-analytics"] = {
        # NOT os.path.realpath'd: .venv/bin/python is a symlink to python3
        # whose target is the system interpreter, and a venv only works when
        # entered through its own bin/.
        "command": str(PROJECT_ROOT / ".venv" / "bin" / "python"),
        "args": [str(Path(__file__).resolve())],
    }
    DESKTOP_CONFIG.write_text(json.dumps(cfg, indent=2) + "\n")
    # Re-read, because printing the dict just mutated in memory proves the
    # mutation and nothing about the file. That mistake is why the previous
    # attempt was reported as installed when it was not.
    back = json.loads(DESKTOP_CONFIG.read_text())
    ok = "nba-analytics" in (back.get("mcpServers") or {})
    print(f"written and re-read from disk: registered={ok}")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--check-config", action="store_true",
                    help="validate the Claude Desktop registration end to end")
    ap.add_argument("--install", action="store_true",
                    help="write the mcpServers entry into the Desktop config")
    ap.add_argument("--print-config", action="store_true",
                    help="emit the claude_desktop_config.json entry")
    args = ap.parse_args()
    if args.install:
        return install_config()
    if args.check_config:
        return check_config()
    if args.selftest:
        return selftest()
    if args.print_config:
        print(json.dumps({"mcpServers": {"nba-analytics": {
            "command": str(PROJECT_ROOT / ".venv" / "bin" / "python"),
            "args": [str(Path(__file__).resolve())],
        }}}, indent=2))
        return 0
    build_server().run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
