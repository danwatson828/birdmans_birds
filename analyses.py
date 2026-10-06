#!/usr/bin/env python3
"""Saved-analysis store: persistence, rerun, chart specs, report rendering.

Design spec section 14: every agent run becomes a saved object rather than a chat
answer, holding the question, the methodology as stated, the exact SQL and code as
executed, the results, the chart specs and the snapshot it ran against.

WHY SQLITE IN WAL MODE, IN ITS OWN FILE
---------------------------------------
Separate file, never inside the snapshot: `ingest.py` republishes the snapshot on
every clean run via an atomic replace, so anything written inside it is destroyed
on the next publish.

SQLite rather than DuckDB for this store, specifically because of concurrency.
DuckDB takes an exclusive lock on its file and that already bit this project once
-- during the first backfill the live database could not even be opened
read-only, which is the whole reason a separate published snapshot exists. This
store has two writers by design: the CLI agent today and the FastAPI backend
section 14 calls for. WAL mode gives concurrent readers alongside a single
writer, and readers never block the writer, which is exactly the access pattern
here. The data is small and relational, so DuckDB's columnar strengths buy
nothing.

    python analyses.py list
    python analyses.py show 3
    python analyses.py rerun 3
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
STORE = PROJECT_ROOT / "analyses.db"
SNAPSHOT = PROJECT_ROOT / "nba_snapshot.duckdb"
LIVE_DB = PROJECT_ROOT / "nba.duckdb"
MAX_STORED_OUTPUT = 20000        # bounded copy of each tool result

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    label                TEXT,
    question             TEXT NOT NULL,
    -- Prose the model produced BEFORE its first run_analysis call. Captured
    -- separately from the final answer because a methodology written after
    -- seeing the numbers is worthless as a check on them.
    methodology          TEXT,
    final_answer         TEXT,
    model                TEXT,
    snapshot_path        TEXT,
    snapshot_sha256      TEXT,
    turns                INTEGER,
    tokens_in            INTEGER,
    tokens_out           INTEGER,
    tokens_cache_read    INTEGER,
    tokens_cache_write   INTEGER,
    cost_usd             REAL,
    started_at           TEXT,
    finished_at          TEXT
);

CREATE TABLE IF NOT EXISTS notes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL DEFAULT '',
    -- A note may hang off a saved analysis, or stand alone.
    run_id      INTEGER REFERENCES runs(id) ON DELETE SET NULL,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

-- Tags are a structured index, not embeddings: the knowledge base is meant to be
-- queried exactly ("everything about Jokic", "everything tagged ts_pct in the
-- 2010s"), which a join answers precisely and a similarity search only
-- approximates. kind is one of player/team/stat/era/free.
CREATE TABLE IF NOT EXISTS tags (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    kind   TEXT NOT NULL,
    value  TEXT NOT NULL,
    UNIQUE (kind, value)
);

-- One join table for both kinds of item, so a tag query spans analyses and
-- notes in a single pass rather than two code paths that can disagree.
CREATE TABLE IF NOT EXISTS item_tags (
    tag_id    INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    item_type TEXT NOT NULL CHECK (item_type IN ('run', 'note')),
    item_id   INTEGER NOT NULL,
    PRIMARY KEY (tag_id, item_type, item_id)
);
CREATE INDEX IF NOT EXISTS idx_item_tags_item ON item_tags(item_type, item_id);
CREATE INDEX IF NOT EXISTS idx_notes_run ON notes(run_id);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id          INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    tool            TEXT NOT NULL,
    -- The tool input exactly as the model sent it, as JSON.
    input_json      TEXT NOT NULL,
    -- The exact string handed to con.execute(). NOT the same as the model's
    -- input: SqlTool strips whitespace and a trailing semicolon first, so
    -- storing the input alone would not let a rerun reproduce the execution.
    executed_sql    TEXT,
    executed_code   TEXT,
    output_text     TEXT,
    output_truncated INTEGER DEFAULT 0,
    result_columns  TEXT,
    rows_returned   INTEGER,
    error           TEXT,
    seconds         REAL,
    -- Reproducibility verdict from run_sql's execute-twice check, so whether a
    -- saved analysis re-executes to the same answer is a queryable fact rather
    -- than something you rediscover by rerunning it.
    --   stability     'stable' | 'unstable' | NULL (not a run_sql call)
    --   compare_mode  'sequence' when the query promised an ORDER BY,
    --                 'multiset' when it did not and only content was compared
    --   order_varied  1 when the two executions differed in row order at all,
    --                 including the benign unordered-by-design case
    --   execution_used which of the two executions produced the stored output
    --   cause         machine-readable reason a step is unstable:
    --                 'row_order' | 'row_content' | 'unordered_aggregate' |
    --                 'analysis_code' | 'feeder_sql' | 'execution_failed'.
    --                 A bare UNSTABLE tag is barely better than no tag; the
    --                 whole point is that the reader learns what to fix.
    --   feeder_stability  for run_analysis: whether its OWN query was stable,
    --                 which is what separates a SQL cause from a Python one
    stability       TEXT,
    compare_mode    TEXT,
    order_varied    INTEGER,
    cause           TEXT,
    feeder_stability TEXT,
    execution_used  INTEGER,
    UNIQUE (run_id, seq)
);

CREATE TABLE IF NOT EXISTS charts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    tool_call_id  INTEGER REFERENCES tool_calls(id) ON DELETE CASCADE,
    title         TEXT,
    spec_json     TEXT NOT NULL,
    validated_at  TEXT
);

CREATE INDEX IF NOT EXISTS idx_tool_calls_run ON tool_calls(run_id, seq);
CREATE INDEX IF NOT EXISTS idx_charts_run ON charts(run_id);
"""


def connect(path: Path = STORE) -> sqlite3.Connection:
    con = sqlite3.connect(str(path))
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    # SCHEMA only creates missing TABLES, so a store written before these
    # columns existed needs them added explicitly. Existing rows keep NULL,
    # which reads correctly as "recorded before the check existed".
    have = {r["name"] for r in con.execute("PRAGMA table_info(tool_calls)")}
    if "skills_loaded" not in {r["name"] for r in con.execute("PRAGMA table_info(runs)")}:
        con.execute("ALTER TABLE runs ADD COLUMN skills_loaded TEXT")
    for col, decl in (("stability", "TEXT"), ("compare_mode", "TEXT"),
                      ("order_varied", "INTEGER"), ("execution_used", "INTEGER"),
                      ("cause", "TEXT"), ("feeder_stability", "TEXT")):
        if col not in have:
            con.execute(f"ALTER TABLE tool_calls ADD COLUMN {col} {decl}")
    con.commit()
    return con


# =============================================================================
# Snapshot identity
# =============================================================================
def snapshot_sha256(path: Path = SNAPSHOT) -> str:
    """Full-file checksum, read from the publish-time sidecar when it is current.

    A logical fingerprint (row count + max date) is not enough: a corrective
    backfill can rewrite values in place without moving either, and this project
    has shipped exactly that kind of fix more than once. The checksum catches it.

    ingest.py writes `<snapshot>.sha256` at publish. If that sidecar is missing or
    older than the snapshot it is recomputed here (~0.3s for 174MB) and cached, so
    an existing snapshot does not need republishing to be identifiable.
    """
    side = Path(str(path) + ".sha256")
    if side.exists() and side.stat().st_mtime >= path.stat().st_mtime:
        return side.read_text().split()[0]
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(8 << 20), b""):
            digest.update(chunk)
    sha = digest.hexdigest()
    try:
        side.write_text(f"{sha}  {path.name}\n")
    except OSError:
        pass
    return sha


# =============================================================================
# Chart specs -- Vega-Lite
# =============================================================================
# WHY VEGA-LITE RATHER THAN PLOTLY
# Vega-Lite is a declarative grammar: `encoding` channels name columns as
# first-class `field` references, which makes "does this column exist in the
# query result" a mechanical walk of the spec rather than a guess. Data is
# referenced by name instead of embedded, so a spec stays tied to the saved query
# it was built from rather than duplicating its rows -- which is the property
# section 14 asks for. Plotly figures carry `x`/`y` value arrays inside traces
# instead of field names, so a spec would either inline the data (breaking the
# tie to the stored query) or need a separate binding step, and column validation
# stops being mechanical. Vega-Lite also renders in the browser via vega-embed
# with no Python runtime, which suits the React frontend section 14 describes.
ALLOWED_MARKS = {"bar", "line", "point", "circle", "square", "area", "tick",
                 "rect", "boxplot", "errorband", "errorbar",
                 # Added for the statistical chart vocabulary: `rule` draws the
                 # zero line a residual plot needs and the interval whiskers a
                 # coefficient/forest plot needs; `text` labels points directly
                 # instead of forcing the reader through a legend.
                 "rule", "text"}


def _collect_fields(node, out: list[str]) -> None:
    """Walk every encoding block in a spec, including layered/faceted ones."""
    if isinstance(node, dict):
        enc = node.get("encoding")
        if isinstance(enc, dict):
            for channel, defn in enc.items():
                if isinstance(defn, dict) and isinstance(defn.get("field"), str):
                    out.append(defn["field"])
        for key in ("layer", "hconcat", "vconcat", "concat"):
            for child in node.get(key, []) or []:
                _collect_fields(child, out)
        for key in ("spec", "facet", "repeat"):
            if isinstance(node.get(key), dict):
                _collect_fields(node[key], out)
        if isinstance(node.get("facet"), dict) and isinstance(
                node["facet"].get("field"), str):
            out.append(node["facet"]["field"])
    elif isinstance(node, list):
        for child in node:
            _collect_fields(child, out)


def _collect_marks(node, out: list[str]) -> None:
    """Every mark in a spec, including those nested in layer/concat/facet."""
    if isinstance(node, dict):
        mark = node.get("mark")
        if isinstance(mark, str):
            out.append(mark)
        elif isinstance(mark, dict) and isinstance(mark.get("type"), str):
            out.append(mark["type"])
        for key in ("layer", "hconcat", "vconcat", "concat", "spec", "facet"):
            child = node.get(key)
            if isinstance(child, list):
                for c in child:
                    _collect_marks(c, out)
            elif isinstance(child, dict):
                _collect_marks(child, out)
    elif isinstance(node, list):
        for c in node:
            _collect_marks(c, out)


def validate_chart_spec(spec: dict, columns: list[str]) -> tuple[bool, list[str]]:
    """Reject a spec before storing it. Returns (ok, problems).

    The point of validating at write time is that a spec referencing a column the
    query does not return is a silent failure at render time -- an empty or
    mislabelled chart rather than an error.
    """
    problems: list[str] = []
    if not isinstance(spec, dict):
        return False, ["spec must be a JSON object"]
    if "mark" not in spec and "layer" not in spec:
        problems.append("spec needs a `mark` (or a `layer` of marks)")
    # Marks are collected recursively. Checking only the top level let a layered
    # spec through unvalidated, which is exactly the shape the statistical charts
    # use -- a scatter with a fitted line, or points with error bars -- so the
    # new vocabulary would have arrived with its validation silently switched off.
    marks: list[str] = []
    _collect_marks(spec, marks)
    bad = sorted({m for m in marks if m not in ALLOWED_MARKS})
    if bad:
        problems.append(f"mark(s) {bad} not in {sorted(ALLOWED_MARKS)}")
    data = spec.get("data")
    if isinstance(data, dict) and "values" in data:
        problems.append("data.values is not allowed: a chart must reference the "
                        "saved query, not embed its own copy of the rows")
    fields: list[str] = []
    _collect_fields(spec, fields)
    if not fields:
        problems.append("no encoding field references found; nothing to validate")
    cols = set(columns)
    unknown = sorted({f for f in fields if f not in cols})
    if unknown:
        problems.append(f"encoding references column(s) not in the query result: "
                        f"{unknown}; available: {sorted(cols)}")
    return (not problems), problems


# =============================================================================
# Writing
# =============================================================================
def save_run(out: dict, question: str, label: str, model: str,
             started_at: str, finished_at: str, cost_usd: float,
             snapshot: Path = SNAPSHOT, store: Path = STORE) -> int:
    """Persist one agent run and everything it did."""
    con = connect(store)
    try:
        with con:
            cur = con.execute(
                """INSERT INTO runs (label, question, methodology, final_answer,
                       model, snapshot_path, snapshot_sha256, turns, tokens_in,
                       tokens_out, tokens_cache_read, tokens_cache_write,
                       cost_usd, started_at, finished_at, skills_loaded)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (label, question, out.get("methodology"), out.get("answer"),
                 model, str(snapshot), snapshot_sha256(snapshot),
                 out.get("turns"), out["usage"]["input"], out["usage"]["output"],
                 out["usage"]["cache_read"], out["usage"]["cache_write"],
                 cost_usd, started_at, finished_at,
                 json.dumps(out.get("skills_loaded") or [])))
            run_id = cur.lastrowid
            for seq, call in enumerate(out.get("tool_log", []), 1):
                text = call.get("output_text") or ""
                truncated = len(text) > MAX_STORED_OUTPUT
                con.execute(
                    """INSERT INTO tool_calls (run_id, seq, tool, input_json,
                           executed_sql, executed_code, output_text,
                           output_truncated, result_columns, rows_returned,
                           error, seconds, stability, compare_mode,
                           order_varied, execution_used, cause,
                           feeder_stability)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (run_id, seq, call["tool"], json.dumps(call.get("input", {}),
                     sort_keys=True), call.get("executed_sql"),
                     call.get("executed_code"),
                     text[:MAX_STORED_OUTPUT], int(truncated),
                     json.dumps(call.get("result_columns")) if call.get(
                         "result_columns") else None,
                     call.get("rows"), call.get("error"), call.get("seconds"),
                     call.get("stability"), call.get("compare_mode"),
                     None if call.get("order_varied") is None
                          else int(call["order_varied"]),
                     call.get("execution_used"), call.get("cause"),
                     call.get("feeder_stability")))
            for ch in out.get("charts", []):
                tc = con.execute(
                    "SELECT id FROM tool_calls WHERE run_id=? AND seq=?",
                    (run_id, ch.get("source_seq"))).fetchone()
                con.execute(
                    """INSERT INTO charts (run_id, tool_call_id, title, spec_json,
                           validated_at) VALUES (?,?,?,?,?)""",
                    (run_id, tc["id"] if tc else None, ch.get("title"),
                     json.dumps(ch["spec"], sort_keys=True),
                     dt.datetime.now().astimezone().isoformat(timespec="seconds")))
        return run_id
    finally:
        con.close()


# =============================================================================
# Reading and reporting
# =============================================================================
def load_run(run_id: int, store: Path = STORE) -> dict:
    con = connect(store)
    try:
        run = con.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if run is None:
            raise SystemExit(f"no analysis with id {run_id}")
        calls = con.execute(
            "SELECT * FROM tool_calls WHERE run_id=? ORDER BY seq", (run_id,)
        ).fetchall()
        charts = con.execute(
            "SELECT * FROM charts WHERE run_id=? ORDER BY id", (run_id,)).fetchall()
        return {"run": dict(run), "calls": [dict(c) for c in calls],
                "charts": [dict(c) for c in charts]}
    finally:
        con.close()


def render_report(rec: dict, show_output: bool = False) -> str:
    """The section 14 report structure.

    SQL and code are rendered VERBATIM from the stored tool calls. They are never
    regenerated by the model, and any plain-language gloss on a query would be
    labelled separately -- an LLM's account of what its own SQL does can be wrong.
    """
    r, calls, charts = rec["run"], rec["calls"], rec["charts"]
    L = [f"ANALYSIS #{r['id']}" + (f"  [{r['label']}]" if r["label"] else ""),
         "=" * 78,
         f"QUESTION\n  {r['question']}", ""]
    L.append("METHODOLOGY (as stated before any analysis ran)")
    L.append("  " + ("\n  ".join((r["methodology"] or "").splitlines())
                     if r["methodology"] else
                     "(none recorded -- this was a direct countable question)"))
    L.append("")
    L.append("FINDINGS")
    L.append("  " + "\n  ".join((r["final_answer"] or "").splitlines()))
    L.append("")
    L.append(f"PROVENANCE")
    L.append(f"  snapshot   {Path(r['snapshot_path']).name}")
    L.append(f"  checksum   {r['snapshot_sha256']}")
    L.append(f"  model      {r['model']}   turns {r['turns']}   "
             f"cost ${r['cost_usd']:.4f}")
    L.append(f"  ran        {r['started_at']} -> {r['finished_at']}")
    L.append("")
    L.append(f"QUERIES AND CODE AS EXECUTED ({len(calls)} tool call(s), verbatim)")
    for c in calls:
        head = f"  [{c['seq']}] {c['tool']}"
        if c["error"]:
            head += f"   ERROR: {c['error'][:80]}"
        elif c["rows_returned"] is not None:
            head += f"   {c['rows_returned']} row(s), {c['seconds']:.2f}s"
        if c["stability"]:
            # How the verdict was reached matters as much as the verdict: an
            # unordered query compared on content alone is reproducible in a
            # weaker sense than one whose exact sequence was checked.
            how = {"sequence": "exact row order reproduced",
                   "multiset": "same rows, order not promised",
                   "rerun_output": "same output on re-execution",
                   "unverified": "not verified -- the check itself timed out",
                   "static": "rejected before comparison"}.get(
                       c["compare_mode"], c["compare_mode"] or "")
            # An unstable step names its cause. A bare tag tells the reader
            # something is wrong without telling them what, which is the defect
            # the Ask view exposed.
            why = {"row_order": "ORDER BY is not a total order",
                   "row_content": "the rows themselves changed",
                   "unordered_aggregate": "order-sensitive aggregate with no "
                                          "internal ORDER BY",
                   "analysis_code": "nondeterminism in the Python, not the query",
                   "feeder_sql": "its query is unstable and that reaches the result",
                   "execution_failed": "a later execution raised"}.get(c["cause"])
            head += f"   [{c['stability']}: {why or how}]"
        L.append(head)
        if c["executed_sql"]:
            L += ["      " + ln for ln in c["executed_sql"].splitlines()]
        if c["executed_code"]:
            L.append("      --- python ---")
            L += ["      " + ln for ln in c["executed_code"].splitlines()]
        if not c["executed_sql"] and not c["executed_code"]:
            L.append(f"      input: {c['input_json']}")
        if show_output and c["output_text"]:
            L.append("      --- output ---")
            L += ["      " + ln for ln in c["output_text"].splitlines()[:12]]
    if charts:
        L.append("")
        L.append(f"CHART SPECS ({len(charts)}, Vega-Lite, validated at save time)")
        for ch in charts:
            L.append(f"  {ch['title'] or '(untitled)'}  "
                     f"[from tool call {ch['tool_call_id']}]")
            L += ["      " + ln for ln in
                  json.dumps(json.loads(ch["spec_json"]), indent=2).splitlines()]
    return "\n".join(L)


# =============================================================================
# Rerun
# =============================================================================
def rerun(run_id: int, store: Path = STORE, snapshot: Path = SNAPSHOT) -> dict:
    """Re-execute a saved analysis's stored SQL against the current snapshot.

    Only the SQL is re-executed. run_analysis code is reported but NOT re-run:
    re-executing arbitrary stored Python outside the sandbox would defeat the
    containment in agent.py, and the sandbox is deliberately left as it is.
    """
    import duckdb
    from agent import _promises_order, compare_result_sets
    if snapshot.resolve() == LIVE_DB.resolve():
        raise SystemExit("refusing to read the live database")
    rec = load_run(run_id, store)
    r = rec["run"]
    now_sha = snapshot_sha256(snapshot)
    same_snapshot = now_sha == r["snapshot_sha256"]
    con = duckdb.connect(str(snapshot), read_only=True)
    results = []
    try:
        for c in rec["calls"]:
            if not c["executed_sql"]:
                if c["executed_code"]:
                    results.append({"seq": c["seq"], "tool": c["tool"],
                                    "status": "skipped (python, not re-run "
                                              "outside the sandbox)"})
                continue
            try:
                # Execute THREE times. Two is not enough: an unstable query can
                # return the same rows twice by luck, and it then gets compared to
                # the stored result and mislabelled CHANGED -- the classification
                # itself was flaky before this. Three executions make that much
                # rarer without being expensive.
                runs3 = []
                cols = None
                for _ in range(3):
                    cur = con.execute(c["executed_sql"])
                    cols = [d[0] for d in cur.description]
                    runs3.append(cur.fetchall())

                # Judge these by exactly the rules run_sql applies at capture
                # time, imported rather than restated. Comparing raw tuples here
                # produced three false UNSTABLE verdicts on one reproducible
                # analysis: two feeder queries with no ORDER BY whose 12,652 rows
                # were identical in content and merely differently ordered, and
                # one whose only difference was 9e-16 of parallel-sum float noise.
                canon = [[tuple(_j(v) for v in row) for row in r] for r in runs3]
                ordered = _promises_order(con, c["executed_sql"]) is not False
                verdicts = [compare_result_sets(canon[0], o, ordered)
                            for o in canon[1:]]
                worst = next((v for v in verdicts if v[0] == "different"), None) \
                    or (next((v for v in verdicts if v[0] == "reordered"), None)
                        if ordered else None)
                if worst is not None:
                    results.append({
                        "seq": c["seq"], "tool": c["tool"], "status": "UNSTABLE",
                        "detail": f"same SQL, same snapshot: {worst[1]}",
                        "rows_then": c["rows_returned"], "rows_now": len(runs3[0])})
                    continue
                rows = runs3[0]

                changed = []
                stored_cols = json.loads(c["result_columns"]) if c["result_columns"] else None
                if stored_cols is not None and stored_cols != cols:
                    changed.append(f"columns {stored_cols} -> {cols}")
                if c["rows_returned"] is not None and c["rows_returned"] != len(rows):
                    changed.append(f"row count {c['rows_returned']} -> {len(rows)}")
                note = ""
                if c["tool"] == "run_sql" and not c["output_truncated"] and c["output_text"]:
                    stored = json.loads(c["output_text"]) if c["output_text"].startswith("{") else None
                    if stored is not None and "rows" in stored:
                        # Canonicalised on both sides for the same reason as
                        # above: an AVG that shifts in its last bits between runs
                        # is not a data change, and reporting it as one would make
                        # every aggregate analysis look broken.
                        now_rows = [tuple(_j(v) for v in row) for row in rows]
                        then_rows = [tuple(row) for row in stored["rows"]]
                        # Same tolerance as everywhere else: an AVG that shifts in
                        # its last bits is not a data change.
                        v, why = compare_result_sets(then_rows, now_rows,
                                                     ordered=False)
                        if v == "different":
                            changed.append(f"result values differ ({why})")
                        elif compare_result_sets(then_rows, now_rows,
                                                 ordered=True)[0] != "same":
                            note = "same rows, different order"
                results.append({"seq": c["seq"], "tool": c["tool"],
                                "status": "CHANGED" if changed else "identical",
                                "detail": "; ".join(changed) or note,
                                "rows_then": c["rows_returned"],
                                "rows_now": len(rows)})
            except Exception as exc:
                results.append({"seq": c["seq"], "tool": c["tool"],
                                "status": "ERROR",
                                "detail": f"{type(exc).__name__}: {exc}"})
    finally:
        con.close()
    return {"run": r, "same_snapshot": same_snapshot,
            "snapshot_then": r["snapshot_sha256"], "snapshot_now": now_sha,
            "results": results}


def _j(v):
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    import decimal
    if isinstance(v, decimal.Decimal):
        return float(v)
    return v


# =============================================================================
# CLI
# =============================================================================
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list").add_argument("--limit", type=int, default=25)
    p_show = sub.add_parser("show")
    p_show.add_argument("id", type=int)
    p_show.add_argument("--output", action="store_true",
                        help="include a sample of each tool's stored output")
    sub.add_parser("rerun").add_argument("id", type=int)
    args = ap.parse_args()

    if args.cmd == "list":
        con = connect()
        try:
            rows = con.execute(
                """SELECT r.id, r.label, r.question, r.started_at, r.cost_usd,
                          r.snapshot_sha256,
                          (SELECT count(*) FROM tool_calls t WHERE t.run_id=r.id) tc,
                          (SELECT count(*) FROM charts c WHERE c.run_id=r.id) ch
                     FROM runs r ORDER BY r.id DESC LIMIT ?""",
                (args.limit,)).fetchall()
        finally:
            con.close()
        if not rows:
            print("no saved analyses yet")
            return 0
        print(f"{'id':>4}  {'when':<20}{'calls':>6}{'charts':>7}{'cost':>9}  "
              f"{'snap':<9}question")
        for r in rows:
            print(f"{r['id']:>4}  {(r['started_at'] or '')[:19]:<20}{r['tc']:>6}"
                  f"{r['ch']:>7}{r['cost_usd'] or 0:>9.4f}  "
                  f"{(r['snapshot_sha256'] or '')[:8]:<9}{r['question'][:60]}")
        return 0

    if args.cmd == "show":
        print(render_report(load_run(args.id), show_output=args.output))
        return 0

    res = rerun(args.id)
    r = res["run"]
    print(f"RERUN of analysis #{r['id']}: {r['question'][:70]}")
    print(f"  snapshot then {res['snapshot_then'][:16]}...")
    print(f"  snapshot now  {res['snapshot_now'][:16]}...  "
          + ("SAME snapshot" if res["same_snapshot"]
             else "DIFFERENT snapshot -- differences below may be real data changes"))
    changed = [x for x in res["results"] if x["status"] == "CHANGED"]
    errored = [x for x in res["results"] if x["status"] == "ERROR"]
    unstable = [x for x in res["results"] if x["status"] == "UNSTABLE"]
    for x in res["results"]:
        line = f"  [{x['seq']}] {x['tool']:<15}{x['status']}"
        if x.get("detail"):
            line += f"  {x['detail']}"
        print(line)
    print(f"\n  {len(res['results'])} step(s): "
          f"{sum(1 for x in res['results'] if x['status']=='identical')} identical, "
          f"{len(changed)} changed, {len(unstable)} unstable, "
          f"{len(errored)} errored")
    if unstable:
        print("  NOTE: an unstable step is a defect in the SAVED QUERY, not a data"
              "\n  change -- add a tie-breaker to its ORDER BY to make it"
              " reproducible.")
    return 1 if (changed or errored or unstable) else 0


if __name__ == "__main__":
    sys.exit(main())
