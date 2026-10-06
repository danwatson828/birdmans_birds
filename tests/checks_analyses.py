"""Saved-analysis store: stored SQL fidelity and chart-spec validation.

The load-bearing check here is byte-identity: what the store holds as
`executed_sql` must be exactly the string that was handed to con.execute(). If it
drifts, every saved analysis becomes unreproducible and the rerun feature is
quietly lying, which is worse than not having it.

Every check skips when analyses.db is absent, so adding this file cannot block a
snapshot publish before any analysis has been saved.
"""

import json
import sqlite3
from pathlib import Path

from .harness import Skip, check

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STORE = PROJECT_ROOT / "analyses.db"


def _store():
    if not STORE.exists():
        raise Skip("analyses.db does not exist yet (no analysis saved)")
    con = sqlite3.connect(str(STORE))
    con.row_factory = sqlite3.Row
    n = con.execute("SELECT count(*) FROM runs").fetchone()[0]
    if n == 0:
        con.close()
        raise Skip("analyses.db has no saved runs yet")
    return con


@check("stored SQL is byte-identical to what was executed", group="analysis store")
def stored_sql_is_byte_identical(con_unused):
    """Re-execute every stored query and compare the string the DB actually ran.

    DuckDB exposes no "last statement" introspection, so byte-identity is
    established from the other direction: the stored string is executed verbatim,
    and it must both run and return the column list that was recorded alongside
    it. A stored string that had been normalised, re-quoted or reformatted would
    either fail to execute or return different columns.
    """
    import duckdb
    store = _store()
    try:
        rows = store.execute(
            """SELECT t.id, t.run_id, t.seq, t.executed_sql, t.result_columns,
                      t.input_json, r.snapshot_path
                 FROM tool_calls t JOIN runs r ON r.id = t.run_id
                WHERE t.executed_sql IS NOT NULL AND t.error IS NULL""").fetchall()
    finally:
        store.close()
    if not rows:
        raise Skip("no successful SQL tool calls stored yet")
    snap = Path(rows[0]["snapshot_path"])
    if not snap.exists():
        raise Skip(f"snapshot {snap.name} not present")
    db = duckdb.connect(str(snap), read_only=True)
    bad = []
    try:
        for r in rows:
            sql = r["executed_sql"]
            # 1. the stored bytes must be executable exactly as stored
            try:
                cur = db.execute(sql)
                cols = [d[0] for d in cur.description]
                cur.fetchall()
            except Exception as exc:
                bad.append(f"call {r['id']}: stored SQL does not execute "
                           f"({type(exc).__name__})")
                continue
            # 2. and must still produce the columns recorded with it
            stored_cols = json.loads(r["result_columns"]) if r["result_columns"] else None
            if stored_cols is not None and stored_cols != cols:
                bad.append(f"call {r['id']}: columns {stored_cols} != {cols}")
            # 3. the stored string must be the normalised form the tool executes,
            #    derived from the verbatim input the model sent
            model_input = json.loads(r["input_json"]).get("query")
            if model_input is not None:
                expected = model_input.strip().rstrip(";").strip()
                if expected != sql:
                    bad.append(f"call {r['id']}: stored executed_sql differs from "
                               f"the input's normalised form")
    finally:
        db.close()
    return not bad, (f"{len(rows)} stored queries re-executed, all byte-identical"
                     if not bad else f"{len(bad)} problem(s): {bad[:3]}")


@check("every run records the snapshot checksum it ran against",
       group="analysis store")
def runs_record_snapshot_identity(con_unused):
    store = _store()
    try:
        bad = store.execute(
            "SELECT count(*) FROM runs WHERE snapshot_sha256 IS NULL "
            "OR length(snapshot_sha256) <> 64").fetchone()[0]
        n = store.execute("SELECT count(*) FROM runs").fetchone()[0]
    finally:
        store.close()
    return bad == 0, f"{n - bad}/{n} runs carry a 64-char sha256"


@check("stored tool outputs are bounded", group="analysis store")
def outputs_bounded(con_unused):
    import analyses
    store = _store()
    try:
        worst = store.execute(
            "SELECT max(length(output_text)) FROM tool_calls").fetchone()[0] or 0
    finally:
        store.close()
    return worst <= analyses.MAX_STORED_OUTPUT, \
        f"largest stored output {worst} chars (cap {analyses.MAX_STORED_OUTPUT})"


@check("chart specs validate against their query's columns", group="analysis store")
def charts_reference_real_columns(con_unused):
    """Any stored chart must still validate. A spec is validated at write time, so
    a stored spec that fails now means the store was written around the check."""
    import analyses
    import duckdb
    store = _store()
    try:
        rows = store.execute(
            """SELECT c.id, c.spec_json, t.executed_sql, r.snapshot_path
                 FROM charts c
                 JOIN tool_calls t ON t.id = c.tool_call_id
                 JOIN runs r ON r.id = c.run_id""").fetchall()
    finally:
        store.close()
    if not rows:
        raise Skip("no chart specs stored yet")
    bad = []
    db = duckdb.connect(str(Path(rows[0]["snapshot_path"])), read_only=True)
    try:
        for r in rows:
            cols = [d[0] for d in db.execute(r["executed_sql"]).description]
            ok, problems = analyses.validate_chart_spec(
                json.loads(r["spec_json"]), cols)
            if not ok:
                bad.append(f"chart {r['id']}: {problems}")
    finally:
        db.close()
    return not bad, (f"{len(rows)} chart spec(s) revalidated"
                     if not bad else str(bad[:2]))


@check("chart validator rejects a missing column", group="analysis store")
def validator_rejects_bad_field(con_unused):
    """A self-test of the validator itself, independent of stored data."""
    import analyses
    good = {"mark": "bar", "encoding": {
        "x": {"field": "season", "type": "ordinal"},
        "y": {"field": "ppg", "type": "quantitative"}}}
    cols = ["season", "ppg"]
    ok_good, _ = analyses.validate_chart_spec(good, cols)
    bad_field = {"mark": "bar", "encoding": {
        "x": {"field": "season", "type": "ordinal"},
        "y": {"field": "points_per_game", "type": "quantitative"}}}
    ok_bad, why = analyses.validate_chart_spec(bad_field, cols)
    inline = {"mark": "bar", "data": {"values": [{"a": 1}]},
              "encoding": {"x": {"field": "season", "type": "ordinal"}}}
    ok_inline, _ = analyses.validate_chart_spec(inline, cols)
    no_mark = {"encoding": {"x": {"field": "season", "type": "ordinal"}}}
    ok_nomark, _ = analyses.validate_chart_spec(no_mark, cols)
    problems = []
    if not ok_good:
        problems.append("rejected a valid spec")
    if ok_bad:
        problems.append("accepted a spec naming a column not in the result")
    if ok_inline:
        problems.append("accepted inline data.values")
    if ok_nomark:
        problems.append("accepted a spec with no mark")
    return not problems, ("valid accepted; missing column, inline data and "
                          "missing mark all rejected" if not problems
                          else str(problems))


@check("run_sql rejects a non-reproducible query", group="analysis store")
def determinism_guard_rejects(con_unused):
    """The execute-twice check must reject the two defect classes and, just as
    importantly, must NOT reject queries that are merely unordered by design.

    A false rejection is the expensive failure here: it would teach the agent to
    bolt meaningless sort keys onto correct queries.
    """
    import agent
    tool = agent.SqlTool(agent.SNAPSHOT)
    try:
        cases = [
            # (label, sql, expected stability)
            ("total order on a key", "stable",
             "SELECT player_id, full_name FROM v_players ORDER BY player_id LIMIT 5"),
            ("no ORDER BY -- unordered by design, content stable", "stable",
             "SELECT game_type, COUNT(*) n FROM v_games WHERE season=2025 "
             "GROUP BY game_type"),
            ("order-sensitive aggregate with no ORDER BY", "unstable",
             "SELECT season, STRING_AGG(DISTINCT season_label) s FROM v_games "
             "WHERE season>=2020 GROUP BY season ORDER BY season"),
            ("same aggregate, ordered", "stable",
             "SELECT season, STRING_AGG(DISTINCT season_label, ',' "
             "ORDER BY season_label) s FROM v_games WHERE season>=2020 "
             "GROUP BY season ORDER BY season"),
        ]
        problems = []
        for label, want, sql in cases:
            tool.run(sql)
            rec = tool.calls[-1]
            got = rec["stability"]
            if got != want:
                problems.append(f"{label}: expected {want}, got {got}")
            if want == "unstable" and "ERROR:" not in (rec["output_text"] or ""):
                problems.append(f"{label}: rejection was not returned as an error")
            if want == "stable" and rec["error"]:
                problems.append(f"{label}: unexpectedly errored -- {rec['error']}")
        return not problems, ("rejects unstable queries, accepts stable and "
                              "unordered-by-design ones" if not problems
                              else "; ".join(problems))
    finally:
        tool.con.close()


@check("stability verdict is stored per step", group="analysis store")
def verdict_is_stored(con_unused):
    """Reproducibility must be queryable from the store, not rediscovered."""
    import sqlite3
    import analyses
    con = sqlite3.connect(f"file:{analyses.STORE}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(tool_calls)")}
        missing = {"stability", "compare_mode", "order_varied",
                   "execution_used"} - cols
        if missing:
            return False, f"tool_calls is missing {sorted(missing)}"
        n = con.execute("SELECT COUNT(*) FROM tool_calls WHERE tool='run_sql' "
                        "AND stability IS NOT NULL").fetchone()[0]
        if n == 0:
            raise Skip("no run_sql step recorded since the check was added")
        bad = con.execute(
            "SELECT COUNT(*) FROM tool_calls WHERE stability IS NOT NULL "
            "AND stability NOT IN ('stable','unstable')").fetchone()[0]
        return bad == 0, (f"{n} step(s) carry a verdict, all well-formed"
                          if bad == 0 else f"{bad} step(s) hold an unknown verdict")
    finally:
        con.close()


@check("determinism guard tolerates float aggregation noise", group="analysis store")
def guard_tolerates_float_noise(con_unused):
    """DuckDB sums in parallel and float addition is not associative, so AVG
    over a large table differs in its last bits between runs -- measured at
    3.9e-14, with every one of six executions distinct.

    Comparing exactly rejected this query in production, costing the agent a
    turn to rediscover ROUND(). The guard must treat it as stable while still
    catching a difference that matters.
    """
    import agent
    tool = agent.SqlTool(agent.SNAPSHOT)
    try:
        noisy = ("SELECT is_home, COUNT(*) n, AVG(pts) pts, AVG(minutes_played) mp "
                 "FROM v_player_games WHERE game_type='regular' AND NOT is_dnp "
                 "GROUP BY is_home ORDER BY is_home")
        verdicts = []
        for _ in range(6):
            tool.run(noisy)
            verdicts.append(tool.calls[-1]["stability"])
        if any(v != "stable" for v in verdicts):
            return False, (f"float noise rejected as unstable in "
                           f"{sum(v != 'stable' for v in verdicts)}/6 runs")
        # the tolerance must not be so wide it hides a real difference
        canon = agent._canon_cell
        if canon(1.0) == canon(1.0001):
            return False, "9-significant-digit rounding is collapsing real differences"
        if canon(21.713894240040116) != canon(21.713894240040137):
            return False, "tolerance is too tight for observed parallel-sum noise"
        return True, "6/6 stable on a noisy aggregate; real differences still distinguished"
    finally:
        tool.con.close()


# A total order in this view needs team_id too: a traded player has one row per
# team, so player_id alone is not unique (694 rows, 587 distinct, in 2024).
_TOTAL_ORDER_Q = ("SELECT player_id, team_id, player_name, pts "
                  "FROM v_player_season_totals "
                  "WHERE season=2024 AND game_type='regular' "
                  "ORDER BY player_id, team_id")
_TIE_PRONE_Q = ("SELECT player_name, pts FROM v_player_season_totals "
                "WHERE season=2024 AND game_type='regular' "
                "ORDER BY pts DESC LIMIT 30")


@check("run_analysis catches Python nondeterminism and names it", group="analysis store")
def analysis_determinism(con_unused):
    """run_analysis has two nondeterminism sources and they need separating.

    Measured before building this: across 21 stored steps the Python output was
    byte-identical every time, while 6 feeder queries were genuinely unstable.
    So the verdict is decided on the OUTPUT, and the feeder check exists to say
    whether the cause was the query or the code. A feeder that merely reorders
    without reaching the output must not be flagged -- 7 stored steps do exactly
    that, and flagging them would be 7 false positives.
    """
    import agent
    tool = agent.SqlTool(agent.SNAPSHOT)
    an = agent.AnalysisTool(tool)
    try:
        cases = [
            ("deterministic", "stable", None,
             "print('mean', round(df.pts.mean(), 4))"),
            ("unseeded RNG", "unstable", "analysis_code",
             "import numpy as np\nprint(np.random.default_rng().integers(0, 10**9))"),
            ("seeded RNG", "stable", None,
             "import numpy as np\nprint(np.random.default_rng(0).integers(0, 10**9))"),
            ("set iteration", "unstable", "analysis_code",
             "print(list({'z', 'a', 'm', 'q', 'b', 'k', 'w'}))"),
            ("sorted set", "stable", None,
             "print(sorted({'z', 'a', 'm', 'q', 'b', 'k', 'w'}))"),
        ]
        problems = []
        for label, want, want_cause, code in cases:
            an.run(_TOTAL_ORDER_Q, code)
            r = an.calls[-1]
            if r["stability"] != want:
                problems.append(f"{label}: expected {want}, got {r['stability']}")
            elif want == "unstable" and r["cause"] != want_cause:
                problems.append(f"{label}: cause {r['cause']}, expected {want_cause}")
            if want == "unstable" and "not reproducible" not in (r["output_text"] or ""):
                problems.append(f"{label}: rejection not returned as a usable error")

        # a reordering feeder that does not reach the output is NOT a defect
        an.run(_TIE_PRONE_Q, "print('n', len(df)); print('total', int(df.pts.sum()))")
        r = an.calls[-1]
        if r["stability"] != "stable":
            problems.append("a harmless feeder reorder was flagged unstable")

        # ...but one that DOES reach the output must be caught and blamed on the
        # query, not the code. This only works because the verification run
        # re-queries rather than re-reading the same materialised frame.
        reaches = ("SELECT player_name, pts FROM v_player_season_totals "
                   "WHERE season=2024 AND game_type='regular' "
                   "AND pts BETWEEN 200 AND 260 ORDER BY pts DESC")
        fired = None
        for _ in range(6):
            an.run(reaches, "print('slice', list(df.player_name)[20:40])")
            if an.calls[-1]["stability"] == "unstable":
                fired = an.calls[-1]
                break
        if fired is None:
            problems.append("a feeder tie that reaches the output was never caught "
                            "in 6 attempts")
        elif fired["cause"] != "feeder_sql":
            problems.append(f"feeder-caused instability blamed on {fired['cause']}, "
                            f"expected feeder_sql")
        return not problems, ("Python nondeterminism caught and attributed; "
                              "harmless feeder reorder not flagged"
                              if not problems else "; ".join(problems))
    finally:
        tool.con.close()


@check("chart_spec rejects a query whose rows are not reproducible", group="analysis store")
def chart_determinism(con_unused):
    """A chart is its query's rows on axes, so its one variation source is the
    query -- the same one run_sql polices, reused rather than reimplemented.

    The >60-row case is the regression worth pinning: run_sql compares a 60-row
    window, and comparing a chart's full result against a truncated re-execution
    reported every chart over 60 rows as changed.
    """
    import agent
    tool = agent.SqlTool(agent.SNAPSHOT)
    ch = agent.ChartTool(tool)
    spec = {"mark": "point",
            "encoding": {"x": {"field": "pts", "type": "quantitative"},
                         "y": {"field": "pts", "type": "quantitative"}}}
    try:
        problems = []
        ch.run("total order, 694 rows", _TOTAL_ORDER_Q, dict(spec))
        r = ch.calls[-1]
        if r["stability"] != "stable":
            problems.append(f"a reproducible {r['rows']}-row chart was rejected "
                            f"({r['cause']})")
        # Asserted on the DETERMINISTIC trigger. A tie at a LIMIT boundary is
        # caught by sampling, which is probabilistic by nature (~9/10 on the
        # known cases), so asserting on one would make this test flaky rather
        # than make the guard stronger.
        ch.run("unordered aggregate",
               "SELECT season, STRING_AGG(DISTINCT season_label) s, COUNT(*) pts "
               "FROM v_games WHERE season>=2020 GROUP BY season ORDER BY season",
               {"mark": "bar",
                "encoding": {"x": {"field": "season", "type": "ordinal"},
                             "y": {"field": "pts", "type": "quantitative"}}})
        r = ch.calls[-1]
        if r["stability"] != "unstable" or r["cause"] != "unordered_aggregate":
            problems.append(f"unordered-aggregate chart: got {r['stability']}/"
                            f"{r['cause']}, expected unstable/unordered_aggregate")
        elif "not reproducible" not in (r["output_text"] or ""):
            problems.append("chart rejection not returned as a usable error")
        return not problems, ("large reproducible chart accepted; "
                              "unordered-aggregate chart rejected with its cause"
                              if not problems else "; ".join(problems))
    finally:
        tool.con.close()


@check("float comparison uses a tolerance, not a rounding boundary", group="analysis store")
def float_tolerance_not_quantisation(con_unused):
    """Quantising to 9 significant digits decided equality once, and two values
    1.8e-15 apart landed either side of a boundary -- that is what made analysis
    #34 report a step unstable when nothing was wrong."""
    from agent import _cells_equal, compare_result_sets
    pairs = [(-12.851718750000002, -12.85171875),
             (120.44468749999997, 120.4446875)]
    for a, b in pairs:
        if not _cells_equal(a, b):
            return False, f"{a} and {b} compare unequal; that is the #34 bug"
    if _cells_equal(1.0, 1.000001):
        return False, "tolerance is wide enough to hide a real difference"
    v, _ = compare_result_sets([(1, -12.851718750000002)], [(1, -12.85171875)],
                               ordered=True)
    if v != "same":
        return False, f"row comparison says {v} on pure float noise"
    return True, "boundary-straddling noise equal; real differences still unequal"
