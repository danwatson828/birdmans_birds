"""The MCP server: registration, dispatch, framing, and the single writer.

Exercised at the dispatch layer the stdio transport calls into, so a tool that
is registered but broken, or whose description has lost the methodology
standard, fails here rather than in Claude Desktop.
"""

from __future__ import annotations

import asyncio

from .harness import Skip, check

EXPECTED = {"run_sql", "run_analysis", "check_coverage",
            "investigate_relationship", "save_analysis",
            "refresh_nba_data", "refresh_espn_data"}


def _server():
    try:
        import mcp_server
    except Exception as exc:
        raise Skip(f"mcp_server unavailable: {exc}")
    return mcp_server.build_server()


def _text(res):
    return "\n".join(getattr(p, "text", "")
                     for p in (getattr(res, "content", []) or []))


@check("every MCP tool is registered with a schema", group="mcp")
def mcp_tools_registered(con_unused):
    mcp = _server()
    tools = asyncio.run(mcp.list_tools())
    names = {t.name for t in tools}
    if names != EXPECTED:
        return False, (f"missing {sorted(EXPECTED - names)}, "
                       f"unexpected {sorted(names - EXPECTED)}")
    for t in tools:
        if not (t.description or "").strip():
            return False, f"{t.name} has no description"
        if not t.input_schema:
            return False, f"{t.name} has no input schema"
    return True, f"{len(names)} tools, each with a description and a schema"


@check("the methodology standard is in the tool descriptions", group="mcp")
def mcp_methodology_in_descriptions(con_unused):
    """An MCP server has no system prompt. The host model reads tool
    descriptions and nothing else this server writes, so the standard that was
    measured as load-bearing has to live there or it does not reach it."""
    mcp = _server()
    tools = {t.name: (t.description or "") for t in asyncio.run(mcp.list_tools())}
    problems = []
    for name in ("run_analysis", "investigate_relationship"):
        d = tools[name]
        for needed in ("SAMPLE", "COMPARISON", "CONFOUNDS", "NOT HANDLED",
                       "confidence interval"):
            if needed not in d:
                problems.append(f"{name} omits {needed!r}")
    # Each data tool must warn about era coverage somehow -- by naming a
    # boundary year or by pointing at check_coverage. The exact wording is not
    # the point; a tool that gives neither is.
    for name in ("run_sql", "run_analysis", "check_coverage"):
        d = tools[name]
        if not any(k in d for k in ("1973", "1979", "check_coverage",
                                    "era coverage", "eras")):
            problems.append(f"{name} gives no era-coverage warning")
    if problems:
        return False, "; ".join(problems[:5])
    return True, ("the four-part methodology and the reporting standard appear "
                  "in the descriptions of the tools they govern")


@check("MCP run_sql reaches the real data and the real guard", group="mcp")
def mcp_run_sql(con_unused):
    """Same SqlTool as the web app, so the determinism guard applies here too.
    The 654 is the project's ground-truth figure; getting it through MCP proves
    the tool is wired to the snapshot and not to a stub."""
    mcp = _server()
    body = _text(asyncio.run(mcp.call_tool("run_sql", {
        "query": "SELECT count(*) AS n FROM v_player_games "
                 "WHERE pts >= 40 AND reb >= 12 AND NOT is_home"})))
    if "654" not in body:
        return False, f"expected 654, got {body[:160]}"
    # The guard must still fire. STRING_AGG without its own ORDER BY is the
    # DETERMINISTIC half of the guard -- a static check on the query text --
    # so it is the right thing to assert on. Row-order detection is sampled
    # and documented as probabilistic, and an unordered LIMIT that happens to
    # return the same rows twice would make this check flaky rather than
    # wrong, which is worse.
    bad = _text(asyncio.run(mcp.call_tool("run_sql", {
        "query": "SELECT season, string_agg(DISTINCT player_name, ', ') AS who "
                 "FROM v_player_games WHERE season = 2025 "
                 "GROUP BY season ORDER BY season"})))
    if "not reproducible" not in bad.lower():
        return False, ("an order-sensitive STRING_AGG was accepted through "
                       "MCP; the determinism guard is not applying")
    return True, ("654 returned through the tool, and an order-sensitive "
                  "STRING_AGG is still rejected by the shared guard")


@check("the investigate protocol keeps rejection available", group="mcp")
def mcp_investigate_framing(con_unused):
    """The wording decides the outcome. This is the framing the removed web
    button carried, and it has to survive the move."""
    mcp = _server()
    body = _text(asyncio.run(mcp.call_tool("investigate_relationship", {
        "x": "Jersey number", "y": "Free throw %",
        "season_from": 2000, "season_to": 2025})))
    problems = []
    for needed, why in [
            ("held-out", "does not require a held-out era"),
            ("NOT CONFIRMED", "does not offer rejection as a verdict"),
            ("do not strain", "does not discourage rescuing the finding"),
            ("check_coverage", "does not require an era-coverage check"),
            ("pseudo-replication", "does not raise pseudo-replication"),
            ("SAMPLE", "does not carry the methodology standard")]:
        if needed.lower() not in body.lower():
            problems.append(why)
    low = body.lower()
    if "not confirmed" in low and "confirmed," in low:
        if low.index("not confirmed") > low.index("confirmed,"):
            problems.append("CONFIRMED is offered before NOT CONFIRMED")
    if problems:
        return False, "; ".join(problems)
    return True, (f"{len(body)} chars demanding a held-out era, an era-coverage "
                  "check and an explicit verdict, naming NOT CONFIRMED first")


@check("MCP save_analysis uses the one writer and records its steps", group="mcp")
def mcp_save_analysis(con_unused):
    """A second writer into analyses.db would be a second schema to keep in
    step. This must go through analyses.save_run and persist the tool calls --
    the first version silently saved runs with zero steps."""
    import analyses
    mcp = _server()
    con = analyses.connect()
    before = con.execute("SELECT count(*) FROM runs").fetchone()[0]
    con.close()

    async def go():
        await mcp.call_tool("run_sql", {"query": "SELECT 1 AS probe"})
        return _text(await mcp.call_tool("save_analysis", {
            "question": "suite probe: MCP writer",
            "answer": "# FINDINGS\n\nVerdict: INCONCLUSIVE — suite probe.",
            "methodology": "SAMPLE none. COMPARISON none.",
            "label": "suite-probe"}))

    msg = asyncio.run(go())
    con = analyses.connect()
    try:
        after = con.execute("SELECT count(*) FROM runs").fetchone()[0]
        rid = con.execute("SELECT max(id) FROM runs").fetchone()[0]
        row = con.execute(
            "SELECT label, model, methodology FROM runs WHERE id = ?",
            [rid]).fetchone()
        steps = con.execute(
            "SELECT count(*) FROM tool_calls WHERE run_id = ?", [rid]).fetchone()[0]
        problems = []
        if after != before + 1:
            problems.append(f"run count went {before} -> {after}")
        if row[0] != "suite-probe":
            problems.append(f"label is {row[0]!r}")
        if not steps:
            problems.append("the run was saved with zero tool calls")
        if not row[2]:
            problems.append("the methodology was not persisted")
    finally:
        # Never leave a probe in the store: it would appear in the knowledge
        # base and on the findings page as if it were real work.
        con.execute("DELETE FROM tool_calls WHERE run_id = ?", [rid])
        con.execute("DELETE FROM runs WHERE id = ?", [rid])
        con.commit()
        final = con.execute("SELECT count(*) FROM runs").fetchone()[0]
        con.close()
    if final != before:
        return False, f"cleanup failed: {before} -> {final}"
    if problems:
        return False, "; ".join(problems)
    return True, (f"saved through analyses.save_run with {steps} step(s) and "
                  f"its methodology, then removed ({msg.split('.')[0]})")


@check("each saved analysis records only its own steps", group="mcp")
def mcp_save_scopes_steps(con_unused):
    """Claude Desktop keeps ONE server process across every request, so the
    agent's tool objects accumulate .calls for the whole session. Reading them
    at save time attached the entire history to whatever was saved next:
    analysis #86 was stored claiming to have executed #82's queries, and one
    check_coverage call was replayed into five different runs. A provenance log
    that reports work the analysis did not do reads exactly like a real one,
    which is what makes it worth a check rather than a comment.

    Two saves in one process, as Desktop would do them. The second must carry
    its own query and NOT the first's, and a third save with no work in between
    must be empty rather than a duplicate."""
    import analyses
    mcp = _server()
    con = analyses.connect()
    before = con.execute("SELECT count(*) FROM runs").fetchone()[0]
    con.close()

    def _save(n):
        return mcp.call_tool("save_analysis", {
            "question": f"suite probe: step scoping {n}",
            "answer": "# FINDINGS\n\nVerdict: INCONCLUSIVE — suite probe.",
            "methodology": "SAMPLE none. COMPARISON none.",
            "label": "suite-probe-scope"})

    async def go():
        await mcp.call_tool("run_sql", {"query": "SELECT 101 AS first_probe"})
        await _save(1)
        await mcp.call_tool("run_sql", {"query": "SELECT 202 AS second_probe"})
        await mcp.call_tool("run_sql", {"query": "SELECT 303 AS third_probe"})
        await _save(2)
        return _text(await _save(3))

    empty_msg = asyncio.run(go())
    con = analyses.connect()
    ids = [r[0] for r in con.execute(
        "SELECT id FROM runs WHERE label = 'suite-probe-scope' ORDER BY id"
    ).fetchall()]
    try:
        problems = []
        if len(ids) != 3:
            problems.append(f"{len(ids)} probe runs saved, expected 3")
        else:
            sql = [[r[0] or "" for r in con.execute(
                "SELECT executed_sql FROM tool_calls WHERE run_id = ? "
                "ORDER BY seq", [i]).fetchall()] for i in ids]
            if len(sql[0]) != 1 or "first_probe" not in sql[0][0]:
                problems.append(f"run 1 recorded {len(sql[0])} step(s), "
                                "expected only its own")
            if len(sql[1]) != 2:
                problems.append(f"run 2 recorded {len(sql[1])} step(s), "
                                "expected 2")
            if any("first_probe" in q for q in sql[1]):
                problems.append("run 2 inherited run 1's query")
            if not all(any(k in q for q in sql[1])
                       for k in ("second_probe", "third_probe")):
                problems.append("run 2 is missing its own queries")
            if sql[2]:
                problems.append(f"run 3 recorded {len(sql[2])} step(s) with no "
                                "work since the previous save")
        # The empty save must say so rather than look like a normal one.
        if "no SQL or analysis steps" not in empty_msg:
            problems.append("an empty save did not warn that it has no "
                            f"provenance: {empty_msg[:80]!r}")
    finally:
        for i in ids:
            con.execute("DELETE FROM tool_calls WHERE run_id = ?", [i])
            con.execute("DELETE FROM runs WHERE id = ?", [i])
        con.commit()
        final = con.execute("SELECT count(*) FROM runs").fetchone()[0]
        con.close()
    if final != before:
        return False, f"cleanup failed: {before} -> {final}"
    if problems:
        return False, "; ".join(problems)
    return True, ("three saves in one process recorded 1, 2 and 0 steps with "
                  "no cross-contamination")


@check("the refresh tools do not run by accident", group="mcp")
def mcp_refresh_is_opt_in(con_unused):
    """One of these republishes the snapshot, which changes the fingerprint
    every saved analysis recorded. A bare call must describe, not do."""
    mcp = _server()
    for tool in ("refresh_nba_data", "refresh_espn_data"):
        body = _text(asyncio.run(mcp.call_tool(tool, {})))
        if "DRY RUN" not in body:
            return False, f"{tool} executed on a bare call"
        if "dry_run=false" not in body:
            return False, f"{tool} does not say how to actually run it"
    return True, "both refresh tools describe themselves and require dry_run=false"


@check("the extension bundle is well-formed and launchable", group="mcp")
def extension_bundle(con_unused):
    """The bundle is the durable registration. Its manifest has to be valid
    and its launcher has to actually start the server from a stripped
    environment -- a GUI app does not inherit a shell's PATH or cwd."""
    import json
    import subprocess
    import sys
    import zipfile
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent
    try:
        import build_extension as be
    except Exception as exc:
        raise Skip(f"build_extension unavailable: {exc}")

    m = be.manifest()
    for key in ("manifest_version", "name", "version", "server", "tools"):
        if key not in m:
            return False, f"manifest is missing {key!r}"
    srv = m["server"]["mcp_config"]
    cmd = srv.get("command")
    if not cmd or not Path(cmd).exists():
        return False, f"manifest command does not exist: {cmd!r}"
    # The venv trap: a resolved .venv/bin/python is the SYSTEM interpreter.
    probe = subprocess.run([cmd, "-c", "import mcp, duckdb"],
                           capture_output=True, text=True, timeout=120)
    if probe.returncode != 0:
        return False, ("the manifest's interpreter cannot import this "
                       "project's packages; it is probably a resolved symlink "
                       "pointing outside the venv")
    if len(m["tools"]) != len(EXPECTED):
        return False, (f"manifest declares {len(m['tools'])} tools, server "
                       f"exposes {len(EXPECTED)}")
    declared = {t["name"] for t in m["tools"]}
    if declared != EXPECTED:
        return False, f"manifest tool names differ: {sorted(declared ^ EXPECTED)}"

    bundle = root / "dist" / "nba-analytics.mcpb"
    if not bundle.exists():
        raise Skip("bundle not built — run `python build_extension.py`")
    with zipfile.ZipFile(bundle) as z:
        names = set(z.namelist())
        bad = z.testzip()
    if bad:
        return False, f"corrupt entry in the bundle: {bad}"
    for need in ("manifest.json", "launcher.py", "project.json"):
        if need not in names:
            return False, f"bundle is missing {need}"

    launcher = root / "dist" / "extension" / "launcher.py"
    if not launcher.is_file():
        raise Skip("unpacked bundle not present")
    failures = _stdio_probe([cmd, str(launcher)])
    if failures:
        return False, "; ".join(failures)
    return True, (f"manifest v{m['manifest_version']} declaring {len(declared)} "
                  f"tools, {bundle.stat().st_size:,}-byte bundle, launcher "
                  "speaks MCP from a stripped environment")


def _stdio_probe(cmd):
    """Speak MCP to a command the way a GUI app would launch it."""
    import json
    import os
    import subprocess

    failures = []
    env = {"HOME": os.environ.get("HOME", ""), "PATH": "/usr/bin:/bin",
           "PYTHONUNBUFFERED": "1"}
    p = subprocess.Popen(cmd, cwd="/", env=env, stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, bufsize=1)
    try:
        for msg in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize",
             "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                        "clientInfo": {"name": "suite", "version": "0"}}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        ):
            p.stdin.write(json.dumps(msg) + "\n")
            p.stdin.flush()
        seen = {}
        for _ in range(2):
            line = p.stdout.readline()
            if not line.strip():
                break
            m = json.loads(line)
            if m.get("id"):
                seen[m["id"]] = m
        if (((seen.get(1) or {}).get("result") or {})
                .get("serverInfo", {}).get("name")) != "nba-analytics":
            failures.append("handshake did not identify as nba-analytics")
        tools = (((seen.get(2) or {}).get("result") or {}).get("tools") or [])
        if {t["name"] for t in tools} != EXPECTED:
            failures.append(f"tools/list returned {len(tools)} tools")
    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
        err = (p.stderr.read() or "")[-400:]
        if err.strip():
            failures.append(f"stderr: {err.strip()}")
    finally:
        p.terminate()
        try:
            p.wait(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
    return failures
