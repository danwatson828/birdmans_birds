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
    python agent.py                      # REPL; follow-ups keep the conversation
    python agent.py --ask "how many triple doubles did Jokic have in 2024-25?"
    python agent.py "same thing, positionally"
    python agent.py --battery            # run the regression question set
    python agent.py --show-sql ...       # echo each tool call as it runs

All four share one tool-use loop, one system prompt and one set of tools. The
REPL keeps a single conversation, so "what about just road games?" resolves
against the previous answer; /reset clears it, blank line or Ctrl-D exits.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import math
from collections import Counter
import os
import shutil
import subprocess
import sys
import tempfile
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
# Loop guard: a runaway agent should stop, not spin. 12 proved too tight -- a
# genuine multi-part analytical question (rank a leaderboard, then model its
# effect on team wins) spent 12 turns on productive exploration and was cut off
# before answering. The guard is for loops, not for thorough work.
MAX_TURNS = 30
# How many times run_sql executes a query before trusting its result. Two is
# enough to catch a coin-flip defect only half the time; measured detection on
# the three known non-deterministic queries in this project rose from 78/48/52%
# at two executions to 100/60/77% at three, for one extra ~72ms local query and
# no API spend. analyses.py rerun already uses three, so the two agree.
EXECUTIONS_PER_QUERY = 3

# Thread counts to cycle through across those executions. Re-running a query
# identically re-runs the same parallel plan, so a latent tie can stay hidden;
# changing the worker count repartitions the work and surfaces it. Measured over
# 10 trials on the known-bad queries, detection went 8/10 -> 10/10, 8/10 -> 8/10
# and 8/10 -> 10/10, and the sweep was FASTER (25.3s vs 28.8s) because the
# default of 4 is not the quickest setting here. 1 is excluded deliberately: it
# detected no better and ran 3x slower.
EXECUTION_THREADS = (4, 8, 6)

MAX_RESULT_ROWS = 60    # keep one tool result from swamping the context
MAX_RESULT_CHARS = 12000

# run_analysis containment. Every value below was chosen against what this
# machine actually enforces -- see the CONTAINMENT note on AnalysisTool.
ANALYSIS_TIMEOUT_S = 60        # wall clock; SIGKILL on expiry
ANALYSIS_CPU_S = 45            # RLIMIT_CPU backstop (verified enforced on darwin)
MAX_ANALYSIS_ROWS = 250_000    # bounds the frame handed to the sandbox
MAX_ANALYSIS_OUTPUT = 12000


# =============================================================================
# The tool
# =============================================================================
# Aggregates whose output depends on the order their input arrives in. DuckDB
# gives them no implicit order, so without an ORDER BY of their own the VALUE
# varies between runs even when every row is in a fixed position.
# Only the aggregates that COMBINE every value in the group. Their result
# depends on input order whenever a group holds more than one row, which is the
# normal reason to call them, so a static flag is nearly always right.
#
# ANY_VALUE, FIRST, LAST, ARG_MAX and ARG_MIN are deliberately NOT here. They
# pick one value, so whether that is ambiguous depends on whether the group holds
# more than one DISTINCT value -- a fact about the data, not the query. Flagging
# them produced a real false positive: ANY_VALUE(season_label) GROUPed BY season
# is idiomatic and perfectly deterministic, because season determines
# season_label, and rejecting it cost the agent a turn. That is the same lesson
# already learned on row order: static analysis is only trustworthy when the
# property lives inside the text being analysed.
ORDER_SENSITIVE_AGGS = {"string_agg", "list", "array_agg", "group_concat"}


def _unordered_aggregates(con, sql: str) -> list[str]:
    """Order-sensitive aggregate calls that carry no ORDER BY of their own.

    Sampling cannot police this class. The value collides with itself often
    enough to hide: on the STRING_AGG query that motivated this, two executions
    caught it in 48-68% of repeated trials, and even five executions only
    reached 90%. Static detection is exact here, and -- unlike deciding whether
    an ORDER BY is a total order -- it needs no knowledge of view keys or
    functional dependencies. It is a property of the call itself, which is why
    this one construct is worth checking statically and row order is not.
    """
    try:
        ast = json.loads(
            con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
        if ast.get("error"):
            return []
    except Exception:
        return []

    found: list[str] = []

    def walk(node):
        if isinstance(node, dict):
            if (node.get("class") == "FUNCTION"
                    and (node.get("function_name") or "").lower()
                        in ORDER_SENSITIVE_AGGS
                    and not ((node.get("order_bys") or {}).get("orders"))):
                found.append(node["function_name"].lower())
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(ast["statements"][0])
    return sorted(set(found))


FLOAT_REL_TOL = 1e-9      # ~9 significant digits, far above observed noise
FLOAT_ABS_TOL = 1e-12     # so values straddling zero compare sanely


def _canon_cell(v):
    """Bucketing key for a result cell -- NOT a verdict.

    Rounding to 9 significant digits is used only to sort and group rows so two
    result sets can be aligned. It must never decide equality on its own:
    rounding is quantisation, not a tolerance, and two values 1.8e-15 apart can
    land on opposite sides of a boundary. That exact case shipped -- analysis #34
    reported an unstable step whose real disagreement was
    -12.851718750000002 vs -12.85171875, quantised to -12.8517188 vs -12.8517187.
    Equality is decided by _cells_equal below, which uses a real tolerance.
    """
    if isinstance(v, float):
        if v != v or v in (float("inf"), float("-inf")):
            return repr(v)
        return float(f"{v:.9g}")
    return v


def _cells_equal(a, b) -> bool:
    """Cell equality with a relative tolerance on floats, no boundary effects."""
    if isinstance(a, float) and isinstance(b, float):
        if a != a and b != b:          # NaN == NaN, for this purpose
            return True
        return math.isclose(a, b, rel_tol=FLOAT_REL_TOL, abs_tol=FLOAT_ABS_TOL)
    return a == b


def _rows_equal(r1, r2) -> bool:
    return len(r1) == len(r2) and all(_cells_equal(a, b) for a, b in zip(r1, r2))


def compare_result_sets(a: list, b: list, ordered: bool) -> tuple[str, str]:
    """Compare two executions of the same query.

    Returns (verdict, detail) with verdict one of:
      "same"       -- equivalent, and in the sense the query promised
      "reordered"  -- same rows, different sequence (a defect only if an
                      ORDER BY promised a sequence)
      "different"  -- the rows themselves changed

    Rows are aligned by the quantised key and then compared with a real float
    tolerance, so parallel-sum noise never reads as a change while a genuine
    difference still does.
    """
    if len(a) != len(b):
        return "different", f"row count changed, {len(a)} -> {len(b)}"

    if all(_rows_equal(x, y) for x, y in zip(a, b)):
        return "same", ""

    key = lambda row: repr(tuple(_canon_cell(v) for v in row))
    sa, sb = sorted(a, key=key), sorted(b, key=key)
    if all(_rows_equal(x, y) for x, y in zip(sa, sb)):
        first = next((i for i, (x, y) in enumerate(zip(a, b))
                      if not _rows_equal(x, y)), 0)
        return "reordered", (f"the same rows came back in a different order, "
                             f"first differing at position {first + 1}")

    n_diff = sum(1 for x, y in zip(sa, sb) if not _rows_equal(x, y))
    return "different", (f"{n_diff} row(s) differ beyond floating-point "
                         f"tolerance between executions")


_NUM = re.compile(r"[-+]?(?:\d+\.\d+(?:[eE][-+]?\d+)?|\d+(?:[eE][-+]?\d+)?)")


def text_equivalent(a: str, b: str) -> tuple[bool, str]:
    """Compare two analysis outputs, tolerant of float noise in printed numbers.

    run_analysis returns stdout, not rows, so the row comparison does not apply.
    The same principle does: a number printed as 0.4413 one run and 0.4414 the
    next is noise from a reduction order, not a different finding, and failing on
    it would reject correct analyses. Non-numeric text must match exactly -- that
    is where a genuinely different result shows up (a different player name, a
    different n, a flipped conclusion).
    """
    if a == b:
        return True, ""
    sa, sb = _NUM.split(a), _NUM.split(b)
    if sa != sb:
        for i, (x, y) in enumerate(zip(sa, sb)):
            if x != y:
                return False, (f"the non-numeric text differs: "
                               f"{x.strip()[:60]!r} vs {y.strip()[:60]!r}")
        return False, "the output has a different shape between runs"
    na, nb = _NUM.findall(a), _NUM.findall(b)
    if len(na) != len(nb):
        return False, "a different count of numbers was printed"
    for x, y in zip(na, nb):
        try:
            fx, fy = float(x), float(y)
        except ValueError:
            if x != y:
                return False, f"{x} vs {y}"
            continue
        if not _cells_equal(fx, fy):
            # The tolerance only forgives noise below the printed precision, e.g.
            # an unrounded 0.44134567890123 against ...124. A change visible at
            # the precision the analysis chose to print is a change the reader
            # would see in the saved report, so it counts.
            return False, f"a printed number changed: {x} vs {y}"
    return True, ""


class Unstable(Exception):
    """A query returned different results on two identical executions."""


def _promises_order(con, sql: str) -> bool | None:
    """Does the OUTERMOST select carry an ORDER BY? None when unparseable.

    Uses DuckDB's own parser, so the dialect always matches what executed. An
    ORDER BY inside a CTE or subquery does not count -- only the top-level one
    determines the order the caller sees.
    """
    try:
        ast = json.loads(
            con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
        if ast.get("error"):
            return None
        mods = ast["statements"][0]["node"].get("modifiers") or []
        return any(m.get("type") == "ORDER_MODIFIER" for m in mods)
    except Exception:
        return None


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

    def _compare_repeats(self, sql: str, rows: list, truncated: bool,
                         limit: int | None = None) -> dict:
        """Re-execute and compare, order-sensitively only when order was promised.

        `limit` must match how many rows the CALLER kept: run_sql keeps a
        60-row window, while chart_spec and run_analysis consume the whole
        result. Comparing a full baseline against a truncated re-execution
        reports every result over 60 rows as changed.

        A query with no top-level ORDER BY never promised a row order, so
        comparing sequences would flag every GROUP BY as broken. For those the
        comparison is over multisets: only a change in CONTENT is a defect.
        A query that does carry an ORDER BY promised a reproducible sequence, so
        there the sequence itself must match -- that is what catches a tie at a
        LIMIT boundary, where membership changes and not just position.
        """
        bad_aggs = _unordered_aggregates(self.con, sql)
        if bad_aggs:
            names = ", ".join(a.upper() + "()" for a in bad_aggs)
            return {"compare_mode": "static", "order_varied": None,
                    "execution_used": None, "stability": "unstable",
                    "cause": "unordered_aggregate",
                    "detail": (f"{names} was called without an ORDER BY of its "
                               f"own, so what it concatenates is in arbitrary "
                               f"order and the value changes between runs")}

        ordered = _promises_order(self.con, sql)
        mode = "multiset" if ordered is False else "sequence"
        out = {"compare_mode": mode, "order_varied": False, "cause": None,
               "execution_used": 1, "stability": "stable", "detail": ""}

        def canon(rs):
            return [tuple(_canon_cell(_jsonable(v)) for v in r) for r in rs]

        seq1 = canon(rows)
        others = []
        for i in range(EXECUTIONS_PER_QUERY - 1):
            try:
                threads = EXECUTION_THREADS[(i + 1) % len(EXECUTION_THREADS)]
                try:
                    self.con.execute(f"SET threads={threads}")
                except Exception:
                    pass          # not fatal; the comparison still runs
                cur_n = self.con.execute(sql)
                cap = MAX_RESULT_ROWS if limit is None else limit
                others.append(canon(cur_n.fetchmany(cap + 1)[:cap]))
            except Exception as exc:
                out.update(stability="unstable", cause="execution_failed",
                           detail=(f"execution {i + 2} raised "
                                   f"{type(exc).__name__}: {exc}"))
                return out

        # One shared comparison, so run_sql, rerun and the new coverage below
        # all decide equivalence the same way.
        verdicts = [compare_result_sets(seq1, o, ordered is not False)
                    for o in others]
        try:
            self.con.execute(f"SET threads={EXECUTION_THREADS[0]}")
        except Exception:
            pass
        out["order_varied"] = any(v != "same" for v, _ in verdicts)

        bad = next((v for v in verdicts if v[0] == "different"), None)
        if bad is not None:
            out["stability"] = "unstable"
            out["cause"] = "row_content"
            out["detail"] = (("the set of rows itself changed" if not truncated
                              else "the set of rows changed within the returned "
                                   "window") + f": {bad[1]}")
            return out

        if mode == "sequence":
            reordered = next((v for v in verdicts if v[0] == "reordered"), None)
            if reordered is not None:
                out["stability"] = "unstable"
                out["cause"] = "row_order"
                out["detail"] = reordered[1]
                return out

        # Either identical throughout, or unordered by design: the rows are
        # reproducible and their sequence was never promised. Recorded, not an error.
        return out

    def run(self, query: str) -> str:
        started = time.monotonic()
        # executed_sql is recorded separately from the model's input: the input
        # is stripped of whitespace and a trailing semicolon before execution, so
        # storing only the input would not let a rerun reproduce the execution
        # byte-for-byte.
        record = {"query": query, "error": None, "rows": 0,
                  "executed_sql": None, "output_text": None,
                  "result_columns": None, "stability": None,
                  "compare_mode": None, "order_varied": None,
                  "cause": None, "execution_used": None}
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

            record["executed_sql"] = stripped
            cur = self.con.execute(stripped)
            columns = [d[0] for d in cur.description]
            record["result_columns"] = columns
            rows = cur.fetchmany(MAX_RESULT_ROWS + 1)
            truncated = len(rows) > MAX_RESULT_ROWS
            rows = rows[:MAX_RESULT_ROWS]
            record["rows"] = len(rows)

            # Two instruments, because the two defect classes need different
            # ones. Row order: execute twice and compare, because whether an
            # ORDER BY is a total order depends on view keys, functional
            # dependencies and filter-pinned constants that are not in the SQL
            # text -- parsing for it over-rejects correct queries (measured: 4
            # false positives for every real gap). Order-sensitive aggregates:
            # a static AST check, because that one IS decidable from the call
            # alone, and sampling misses it a third of the time.
            # Cost of the extra execution: ~72ms, no API spend.
            verdict = self._compare_repeats(stripped, rows, truncated)
            record.update(verdict)
            if verdict["stability"] == "unstable":
                raise Unstable(verdict["detail"])

            body = json.dumps(
                {"columns": columns,
                 "rows": [[_jsonable(v) for v in row] for row in rows]},
                default=str)
            if truncated:
                body += (f"\n\n[truncated at {MAX_RESULT_ROWS} rows -- add "
                         f"LIMIT/aggregation if you need more]")
            if len(body) > MAX_RESULT_CHARS:
                body = body[:MAX_RESULT_CHARS] + "\n\n[result truncated by size]"
            record["output_text"] = body
            return body
        except Unstable as exc:
            # Not a bad query -- a non-reproducible one. Same self-correctable
            # channel as any other error, but the remedy is specific.
            record["error"] = f"Unstable: {exc}"
            record["output_text"] = (
                f"ERROR: this query is not reproducible. Executed twice against "
                f"the unchanged snapshot, {exc}.\n\n"
                f"Every saved answer must re-execute to the same result, so this "
                f"result was not returned. Fix the query and call again:\n"
                f"  - ORDER BY ... LIMIT: append the grain's primary key as the "
                f"final sort key (player_id, game_id, team_id) so ties cannot "
                f"change which rows make the cut.\n"
                f"  - ORDER BY on a non-unique column: extend it until no two "
                f"rows can tie on the full key.\n"
                f"  - STRING_AGG / LIST / ARRAY_AGG / GROUP_CONCAT / FIRST / "
                f"LAST: give the aggregate its own ORDER BY inside the call, "
                f"e.g. STRING_AGG(x, '/' ORDER BY x). A total row order does "
                f"not fix this one.")
            return record["output_text"]
        except Exception as exc:
            # Errors go back to the model as text, not as exceptions: a bad query
            # is information the agent can act on, and it usually self-corrects.
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["output_text"] = f"ERROR: {record['error']}"
            return record["output_text"]
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


# =============================================================================
# The analysis tool
# =============================================================================
CHILD_PREAMBLE = """\
import sys, resource, socket

# Layer 3: neutralise outbound connections before any analysis code runs. Not
# airtight against ctypes, but it stops the accidental and casual network cases,
# and it is the only network control available here (see CONTAINMENT).
#
# The CLASS must stay a class: replacing socket.socket with a function breaks
# `class SSLSocket(socket)` inside the stdlib ssl module, which statsmodels
# imports transitively via statsmodels.datasets -> urllib.request -> http.client
# -> ssl. That failed with "TypeError: function() argument 'code' must be code,
# not str" and made every run_analysis call error out. So the connecting methods
# are replaced, and the type is left alone.
def _no_network(*a, **k):
    raise PermissionError("network access is disabled inside run_analysis")
socket.socket.connect = _no_network
socket.socket.connect_ex = _no_network
socket.create_connection = _no_network

# Layer 4: CPU ceiling. Verified enforced on this platform (SIGXCPU). Set
# soft-only so the process dies rather than the call failing.
try:
    resource.setrlimit(resource.RLIMIT_CPU, ({cpu}, resource.RLIM_INFINITY))
except (ValueError, OSError):
    pass

import pandas as pd
import numpy as np
import scipy.stats as stats
try:
    import statsmodels.api as sm
    import statsmodels.formula.api as smf
except ImportError:
    sm = smf = None

df = pd.read_parquet({frame!r})
pd.set_option("display.width", 120)
pd.set_option("display.max_columns", 50)

# ---- analysis code follows ----
"""


class AnalysisTool:
    """run_analysis: Python statistics over a dataframe drawn from the snapshot.

    CONTAINMENT -- what this is and is not
    -------------------------------------
    This is a much larger surface than run_sql: arbitrary Python rather than a
    single SELECT. The threat model is honest about who writes that Python. It
    is generated by Claude in response to the user's own question, so it is not
    adversarial third-party input; the realistic failures are runaway loops,
    memory blowups, an accidental write that damages hours of ingestion work,
    and an accidental network call. A determined escape is NOT what this defends
    against, and nothing here should be mistaken for a multi-tenant sandbox.

    Six layers, each chosen against what this machine actually enforces:

    1. NO DATABASE HANDLE IN THE SANDBOX. The biggest win, and a design choice
       rather than a restriction. The parent runs the SQL itself, through the
       same verified read-only path run_sql uses, and hands the child a Parquet
       file. The analysis code never sees a connection or a database path, so it
       cannot reach the live nba.duckdb, cannot take a lock that would break
       ingestion, and cannot read tables outside the declared sample. It also
       forces the agent to state its sample as SQL, which is exactly what the
       methodology step needs anyway.

    2. SEPARATE PROCESS UNDER macOS SEATBELT (`sandbox-exec -n no-write`).
       Kernel-enforced: every filesystem write is denied, verified against the
       project directory. A subprocess also means a hang or a crash kills the
       analysis, not the agent.

       Why `no-write` and not `no-network`: the two built-in profiles cannot be
       combined -- not by repeating the flag (usage error) and not by nesting
       (`sandbox_apply: Operation not permitted`) -- and a hand-written profile
       SIGABRTs the interpreter during startup. Forced to choose one, writes are
       the greater risk: the databases represent hours of ingestion and are the
       irreplaceable asset here, while the data itself is public NBA statistics.
       Network then gets covered at the Python layer (layer 3), for which there
       is no filesystem equivalent.

    3. SOCKET NEUTRALISATION in the child preamble, since the kernel-level
       network block was unavailable. Casual and accidental egress only.

    4. RLIMIT_CPU, verified enforced here (the process dies with SIGXCPU).

    5. WALL-CLOCK TIMEOUT with SIGKILL, covering the sleeping or blocking cases
       that CPU time does not.

    6. BOUNDED INPUT. The parent caps the materialised frame, so the sandbox
       cannot start from an unbounded amount of data.

    KNOWN GAPS, stated rather than papered over:
      * No hard memory cap. RLIMIT_AS and RLIMIT_DATA cannot be set on this
        platform at all (EINVAL), so a fast large allocation can still pressure
        the machine. The row cap and the timeout are what stand in for it.
      * The network block is process-level, not kernel-level, so ctypes could
        defeat it.
      * Reads are unrestricted -- the child could read any file the user can.
        With writes and network blocked this is low-consequence, but it is not
        zero.
    """

    def __init__(self, sql_tool: "SqlTool"):
        self.sql = sql_tool
        self.calls: list[dict] = []

    def run(self, query: str, code: str) -> str:
        started = time.monotonic()
        record = {"query": query, "code": code, "error": None, "rows": 0,
                  "executed_sql": None, "executed_code": code,
                  "output_text": None, "result_columns": None,
                  "stability": None, "compare_mode": None, "order_varied": None,
                  "cause": None, "execution_used": None,
                  "feeder_stability": None, "feeder_cause": None}
        workdir = None
        try:
            # --- materialise the sample in the PARENT, using the same guards ---
            stripped = query.strip().rstrip(";").strip()
            low = stripped.lower()
            if not (low.startswith("select") or low.startswith("with")):
                raise ValueError("query must be a single SELECT or WITH statement")
            if ";" in stripped:
                raise ValueError("one statement per call; remove the ';'")
            record["executed_sql"] = stripped
            cur = self.sql.con.execute(stripped)
            record["result_columns"] = [d[0] for d in cur.description]
            frame = cur.df()
            record["rows"] = len(frame)
            # Feeder state is recorded but NOT the verdict. Measured across 31
            # stored steps: 7 feeders came back reordered with no ORDER BY, and
            # the analysis output was byte-identical every time -- a correlation
            # does not care what order its rows arrive in. Flagging those would
            # be 7 false positives. What the feeder check is for is ATTRIBUTION:
            # when the output does move, it says whether the cause was the query
            # or the code.
            # The baseline must come from the same path as the comparison runs.
            # Passing the pandas frame's rows here compared numpy dtypes against
            # DuckDB tuples and reported every feeder as content-unstable, which
            # then misattributed a genuine Python defect to the SQL.
            _base = self.sql.con.execute(stripped).fetchall()
            feeder = self.sql._compare_repeats(stripped, _base, truncated=False,
                                               limit=len(_base))
            record["feeder_stability"] = feeder["stability"]
            record["feeder_cause"] = feeder["cause"]
            if len(frame) > MAX_ANALYSIS_ROWS:
                raise ValueError(
                    f"query returned {len(frame):,} rows, over the "
                    f"{MAX_ANALYSIS_ROWS:,} cap. Aggregate or filter in SQL first.")
            if frame.empty:
                return ("ERROR: the query returned no rows, so there is nothing to "
                        "analyse. Check the filters -- a common cause is an era "
                        "restriction (see check_coverage).")

            workdir = Path(tempfile.mkdtemp(prefix="nba-analysis-"))
            frame_path = workdir / "frame.parquet"
            frame.to_parquet(frame_path)
            script = workdir / "analysis.py"
            script.write_text(
                CHILD_PREAMBLE.format(cpu=ANALYSIS_CPU_S, frame=str(frame_path))
                + code)

            env = {"PATH": "/usr/bin:/bin", "HOME": str(workdir),
                   "PYTHONDONTWRITEBYTECODE": "1", "TMPDIR": str(workdir),
                   "OPENBLAS_NUM_THREADS": "2", "OMP_NUM_THREADS": "2"}
            argv = ["/usr/bin/sandbox-exec", "-n", "no-write",
                    sys.executable, str(script)]
            def _execute(script_path=script):
                try:
                    return subprocess.run(
                        [*argv[:-1], str(script_path)], capture_output=True,
                        text=True, timeout=ANALYSIS_TIMEOUT_S, env=env,
                        cwd=str(workdir))
                except subprocess.TimeoutExpired:
                    raise TimeoutError(
                        f"analysis exceeded {ANALYSIS_TIMEOUT_S}s and was killed. "
                        f"Reduce the sample or simplify the computation.")

            def _rematerialise():
                """A second script over a FRESHLY queried frame.

                The verification run must re-query, not re-read the same Parquet.
                Pointing both executions at one materialised frame tests only the
                Python, so an unstable feeder could never reach the comparison --
                it made the feeder_sql verdict unreachable, which a tie that
                genuinely changed the sample proved by never firing in 8 trials.
                Reproducibility of a saved analysis means the WHOLE step repeats.
                """
                f2 = workdir / "frame2.parquet"
                self.sql.con.execute(stripped).df().to_parquet(f2)
                s2 = workdir / "analysis2.py"
                s2.write_text(
                    CHILD_PREAMBLE.format(cpu=ANALYSIS_CPU_S, frame=str(f2))
                    + code)
                return s2

            proc = _execute()

            out = (proc.stdout or "").strip()
            err = (proc.stderr or "").strip()
            if proc.returncode != 0:
                # Send the traceback back as text so the model can fix its code,
                # exactly as run_sql does with SQL errors.
                record["error"] = f"exit {proc.returncode}"
                tail = err[-4000:] if err else "(no stderr)"
                extra = ""
                if proc.returncode in (-9, 137):
                    extra = " (killed -- likely memory or time)"
                elif proc.returncode in (-24, 152):
                    extra = " (SIGXCPU -- CPU limit reached)"
                return (f"ERROR: analysis exited {proc.returncode}{extra}\n"
                        f"stdout:\n{out[-2000:]}\n\nstderr:\n{tail}")
            if not out:
                return ("The analysis ran but printed nothing. Results only come "
                        "back through print() -- print the statistics you need.")
            # The output the agent consumed is what must be reproducible, so the
            # verdict is decided on stdout -- not on the feeder, and not by
            # scanning the code for risky constructs. That scan flagged
            # groupby/sort_values/dict-iteration on 13 of 21 stored steps whose
            # output never moved once: the constructs are not predictive, only
            # running it is. A second execution costs ~1.5s median.
            record.update(stability="stable", compare_mode="rerun_output",
                          cause=None, execution_used=1)
            try:
                proc2 = _execute(_rematerialise())
            except TimeoutError:
                # The first execution succeeded. Discarding a good result because
                # the verification run was slow would be worse than saying so:
                # record it as unverified and hand the result back.
                record.update(stability=None, compare_mode="unverified",
                              cause="verification_timed_out")
                body = (f"[sample: {len(frame):,} rows, {len(frame.columns)} "
                        f"columns]\n[not verified: the re-execution used to check "
                        f"reproducibility exceeded {ANALYSIS_TIMEOUT_S}s]\n{out}")
                record["output_text"] = body      # the record must hold what was returned
                return body

            if proc2.returncode != 0:
                # Succeeded once, crashed once, on identical input. That is
                # nondeterminism of the starkest kind, and worth its own cause
                # rather than being described as a changed number.
                record.update(stability="unstable", cause="analysis_code")
                raise Unstable(
                    f"the same code succeeded on one execution and exited "
                    f"{proc2.returncode} on the next. First difference: a crash "
                    f"that depends on something other than the input")

            same, why = text_equivalent(out, (proc2.stdout or "").strip())
            if not same:
                if record.get("feeder_stability") == "unstable":
                    cause, blame = ("feeder_sql",
                        f"its own query is not reproducible "
                        f"({record.get('feeder_cause')}), and that reaches the "
                        f"result")
                else:
                    cause, blame = ("analysis_code",
                        "the query is reproducible, so the variation is in the "
                        "Python -- an unseeded random draw, iteration over a set "
                        "or dict, or a tie broken arbitrarily")
                record.update(stability="unstable", cause=cause)
                raise Unstable(f"{blame}. First difference: {why}")

            body = f"[sample: {len(frame):,} rows, {len(frame.columns)} columns]\n{out}"
            if len(body) > MAX_ANALYSIS_OUTPUT:
                body = body[:MAX_ANALYSIS_OUTPUT] + "\n[output truncated]"
            record["output_text"] = body
            return body
        except Unstable as exc:
            record["error"] = f"Unstable: {exc}"
            record["output_text"] = (
                f"ERROR: this analysis is not reproducible. Run twice on the "
                f"unchanged snapshot, {exc}\n\n"
                f"Every saved analysis must re-execute to the same result, so "
                f"this one was not returned. Fix it and call again:\n"
                f"  - seed any random draw: rng = np.random.default_rng(0), or "
                f"random_state=0 on a bootstrap, resample or permutation test.\n"
                f"  - sort before any head/tail or top-N, with enough keys that "
                f"no two rows can tie.\n"
                f"  - do not iterate a set, or a dict built from unordered keys; "
                f"sort the keys first.\n"
                f"  - if the query is the problem, give it a total ORDER BY.")
            return record["output_text"]
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["output_text"] = f"ERROR: {record['error']}"
            return record["output_text"]
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            self.calls.append(record)
            if workdir and workdir.exists():
                shutil.rmtree(workdir, ignore_errors=True)


class ChartTool:
    """chart_spec: a declarative Vega-Lite spec tied to a query that actually ran.

    No images. The analysis sandbox is write- and network-blocked, so a chart is a
    spec the frontend renders, which also keeps every chart traceable to the rows
    it came from (design spec section 14).

    The query is EXECUTED here before the spec is accepted, so validation runs
    against the real result columns rather than against what the model believed
    they would be. A spec naming a column the query does not return is rejected,
    because at render time it fails silently -- an empty or mislabelled chart, not
    an error.
    """

    def __init__(self, sql_tool: "SqlTool"):
        self.sql = sql_tool
        self.calls: list[dict] = []
        self.charts: list[dict] = []

    def run(self, title: str, query: str, spec: dict) -> str:
        import analyses
        started = time.monotonic()
        record = {"tool": "chart_spec", "title": title, "query": query,
                  "error": None, "executed_sql": None, "output_text": None,
                  "result_columns": None, "rows": 0, "spec": spec,
                  "stability": None, "compare_mode": None,
                  "order_varied": None, "cause": None, "execution_used": None}
        try:
            stripped = (query or "").strip().rstrip(";").strip()
            low = stripped.lower()
            if not (low.startswith("select") or low.startswith("with")):
                raise ValueError("query must be a single SELECT or WITH statement")
            record["executed_sql"] = stripped
            cur = self.sql.con.execute(stripped)
            cols = [d[0] for d in cur.description]
            rows = cur.fetchall()
            record["result_columns"] = cols
            record["rows"] = len(rows)

            # A chart is its query's rows drawn on axes; the spec itself is fixed
            # JSON once emitted. So chart_spec has exactly ONE variation source --
            # the query -- and it is the same one run_sql already polices. Reuse
            # that guard rather than writing a second: an unstable query here
            # means the rendered chart changes shape between viewings.
            verdict = self.sql._compare_repeats(stripped, rows, truncated=False,
                                                limit=len(rows))
            record.update({k: verdict[k] for k in
                           ("stability", "compare_mode", "order_varied",
                            "cause", "execution_used")})
            if verdict["stability"] == "unstable":
                raise Unstable(verdict["detail"])

            if isinstance(spec, str):
                spec = json.loads(spec)
            ok, problems = analyses.validate_chart_spec(spec, cols)
            if not ok:
                raise ValueError("; ".join(problems))
            record["spec"] = spec
            self.charts.append({"title": title, "spec": spec, "query": stripped})
            record["output_text"] = (
                f"chart accepted: {title!r}, {len(rows)} row(s), "
                f"columns {cols}")
            return record["output_text"]
        except Unstable as exc:
            record["error"] = f"Unstable: {exc}"
            record["output_text"] = (
                f"ERROR: this chart's query is not reproducible -- {exc}. A chart "
                f"is its query's rows, so the rendered chart would change between "
                f"viewings. Give the query a total order (append the grain's "
                f"primary key to ORDER BY), or give any STRING_AGG/LIST/FIRST/LAST "
                f"its own internal ORDER BY, then call chart_spec again.")
            return record["output_text"]
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["output_text"] = (
                f"ERROR: chart spec rejected -- {record['error']}. "
                f"Fix the spec so every encoding field names a column the query "
                f"returns, then call chart_spec again.")
            return record["output_text"]
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            self.calls.append(record)


class SkillTool:
    """load_skill: model-invoked expertise, read from .claude/skills/*/SKILL.md.

    These are real Claude Code skills -- the same files, same frontmatter -- so
    they load automatically when the user works in Claude Code. agent.py is a
    hand-rolled Messages API loop, not Claude Code, so it reaches them through
    this tool instead: the model decides when a skill is relevant and asks for it
    by name, which is what "model-invoked" means and is verifiable from the
    transcript afterwards.

    NOT the Anthropic API's Skills product. That loads skills into a server-side
    execution container, which would move analysis off this machine and discard
    the containment the sandbox was built for -- no database handle in the child,
    a Parquet handoff, seatbelt, CPU and wall-clock limits. One source of truth
    for the text, the right execution model for the work.
    """

    def __init__(self, root: Path | None = None):
        self.root = root or (PROJECT_ROOT / ".claude" / "skills")
        self.calls: list[dict] = []

    def available(self) -> list[tuple[str, str]]:
        """(name, description) for every skill on disk, from its frontmatter."""
        out = []
        for d in sorted(self.root.glob("*/SKILL.md")):
            text = d.read_text()
            name = desc = ""
            if text.startswith("---"):
                fm = text.split("---", 2)[1]
                for line in fm.splitlines():
                    if line.startswith("name:"):
                        name = line.split(":", 1)[1].strip()
                    elif line.startswith("description:"):
                        desc = line.split(":", 1)[1].strip()
            out.append((name or d.parent.name, desc))
        return out

    def run(self, name: str) -> str:
        started = time.monotonic()
        record = {"tool": "load_skill", "name": name, "error": None,
                  "output_text": None, "rows": 0}
        try:
            safe = "".join(c for c in (name or "") if c.isalnum() or c in "-_")
            path = self.root / safe / "SKILL.md"
            if not path.is_file():
                have = ", ".join(n for n, _ in self.available()) or "none"
                raise FileNotFoundError(f"no skill {name!r}; available: {have}")
            body = path.read_text()
            if body.startswith("---"):          # drop the frontmatter, keep the body
                body = body.split("---", 2)[2].lstrip()
            record["output_text"] = body
            return body
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
            record["output_text"] = f"ERROR: {record['error']}"
            return record["output_text"]
        finally:
            record["seconds"] = round(time.monotonic() - started, 3)
            self.calls.append(record)


# Columns where a stored zero means "not recorded", not "zero of them".
# Deliberately a short, argued list rather than a blanket rule: for points,
# rebounds or steals a zero is a real observation and treating it as missing
# would understate coverage for every counting stat in the database.
ZERO_MEANS_MISSING = {"minutes_played", "min", "minutes"}


class CoverageTool:
    """check_coverage: live per-stat era coverage.

    Computed from the database on every call rather than read out of
    inventory.md. inventory.md is generated from this same source, so a tool that
    recomputes cannot go stale relative to the data the agent is about to query,
    and it reports coverage for whatever stat is asked about rather than only the
    ones a human happened to write up. The `dips` field is what surfaces findings
    that appear in no prompt anywhere -- the 1966 personal-fouls hole, the
    1971-73 field-goal-attempt and rebound holes, the 2023 plus/minus hole.
    """

    def __init__(self, con):
        self.con = con
        self.calls: list[dict] = []

    # Names callers reach for that live under a different name in the views the
    # coverage query actually reads. `min` is the raw box-score column; in
    # v_player_games it is min_raw (the string) and minutes_played (parsed).
    ALIASES = {"min": "minutes_played", "minutes": "minutes_played",
               "turnovers": "turnover", "to": "turnover",
               "three_pointers": "fg3m", "fg3": "fg3m"}

    def _columns(self, table: str) -> set[str]:
        return {r[0] for r in self.con.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = ?", [table]).fetchall()}

    def run(self, stats: list[str]) -> str:
        """Never raises. A bug in here must not take down the agent -- it returns
        an error the model can read and correct, exactly as run_sql does."""
        self.calls.append({"stats": stats})
        try:
            return self._run(stats)
        except Exception as exc:
            return json.dumps({"error": f"{type(exc).__name__}: {exc}"}, indent=1)

    def _run(self, stats: list[str]) -> str:
        box_cols = self._columns("v_player_games")
        adv_cols = self._columns("player_advanced_stats") - {
            "player_id", "game_id", "team_id", "id", "_ingested_at"}
        report = {}
        for raw in stats:
            stat = self.ALIASES.get(raw.strip().lower(), raw.strip())
            in_box, in_adv = stat in box_cols, stat in adv_cols
            if not (in_box or in_adv):
                report[raw] = {"error": f"unknown column {stat!r}",
                               "box_score_columns": sorted(box_cols),
                               "advanced_columns": sorted(adv_cols)}
                continue
            col = f'"{stat}"'          # quoted: several stat names are keywords
            # Zero means "not recorded" for some columns and is a perfectly
            # ordinary observation for others. A player can genuinely score
            # zero points; a player who APPEARED in a game did not play zero
            # minutes, so a zero there is a missing value wearing a number.
            # Counting non-nulls reported minutes as 100% covered back to
            # 1946 and let the app show Bill Russell at 31.3 minutes a game in
            # 1961-62 when only 53 of his 76 appearances carry a figure --
            # his real average was 44.9. Same failure class as the is_dnp bug.
            have_expr = (f"sum(CASE WHEN {col} > 0 THEN 1 ELSE 0 END)"
                         if stat in ZERO_MEANS_MISSING else f"count({col})")
            if in_adv and not in_box:
                adv_have = (have_expr.replace(f"{col}", f"a.{col}")
                            if stat in ZERO_MEANS_MISSING else f"count(a.{col})")
                rows = self.con.execute(f"""
                    SELECT g.season, count(*) AS n, {adv_have} AS have
                      FROM player_advanced_stats a JOIN games g ON g.id = a.game_id
                     GROUP BY 1 ORDER BY 1""").fetchall()
                measured = "advanced-stat rows (1996+ only)"
            else:
                rows = self.con.execute(f"""
                    SELECT season, count(*) AS n, {have_expr} AS have
                      FROM v_player_games WHERE NOT is_dnp
                     GROUP BY 1 ORDER BY 1""").fetchall()
                measured = "box score rows where the player actually played"
            if not rows:
                report[raw] = {"error": "no rows"}
                continue
            pct = {sn: (100.0 * h / n if n else 0.0) for sn, n, h in rows}
            first_any = next((sn for sn, _, h in rows if h), None)
            full = None
            for i, (sn, _, _) in enumerate(rows):
                if all(h / n >= 0.95 for _, n, h in rows[i:] if n):
                    full = sn
                    break
            dips, seen = [], False
            for sn in sorted(pct):
                if pct[sn] >= 95.0:
                    seen = True
                elif seen:
                    dips.append({"season": sn, "pct": round(pct[sn], 1)})
            declared = self.con.execute(
                "SELECT first_tracked_season, first_full_season "
                "FROM v_stat_first_tracked WHERE stat = ?", [stat]).fetchone()
            entry = {
                "resolved_column": stat,
                "counted_as_present": ("value > 0 (a zero here means the "
                                       "figure was not recorded)"
                                       if stat in ZERO_MEANS_MISSING
                                       else "value is not null"),
                "first_season_with_any_data": first_any,
                "first_season_reliable_95pct": full,
                "seasons_with_zero_coverage": sum(1 for v in pct.values() if v == 0),
                "coverage_regressions_after_becoming_reliable": dips or None,
                "measured_over": measured,
            }
            if stat != raw:
                entry["note"] = f"{raw!r} resolved to {stat!r}"
            if declared:
                entry["declared_in_v_stat_first_tracked"] = {
                    "first_tracked": declared[0], "first_full": declared[1]}
            report[raw] = entry
        return json.dumps(report, indent=1, default=str)


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


ANALYSIS_TOOL_DEF = {
    "name": "run_analysis",
    "description": textwrap.dedent("""\
        Run Python statistics over a sample drawn from the database. Use this for
        anything SQL cannot answer: correlation, regression, significance tests,
        distribution comparisons, effect sizes.

        You supply two things:
          query -- one SELECT/WITH that defines your SAMPLE. It is run for you and
                   the result arrives as a pandas DataFrame already bound to `df`.
                   You do not get a database connection inside the analysis.
          code  -- Python operating on `df`.

        Already imported: pandas as pd, numpy as np, scipy.stats as stats,
        statsmodels.api as sm, statsmodels.formula.api as smf.

        RESULTS COME BACK ONLY THROUGH print(). A returned value is discarded.
        Print the statistic, the sample size, and the p-value or interval -- a
        coefficient with no uncertainty is not a result.

        The sample is capped at 250,000 rows, so aggregate in SQL first if you
        would exceed that. There is no filesystem or network access, and the
        analysis is killed after 60 seconds."""),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {"type": "string",
                      "description": "SELECT/WITH defining the sample; becomes `df`."},
            "code": {"type": "string",
                     "description": "Python over `df`. print() your results."},
        },
        "required": ["query", "code"],
        "additionalProperties": False,
    },
}

SKILL_TOOL_DEF = {
    "name": "load_skill",
    "description": textwrap.dedent("""\
        Load a project skill: packaged expertise on how work of a particular
        kind must be done here.

        Load a skill BEFORE doing the work it governs, not after. Loading
        `statistical-methodology` after you have already reported a coefficient
        is too late to change how you reported it.

        Available skills:
        {skills}"""),
    "input_schema": {
        "type": "object",
        "properties": {
            "name": {"type": "string",
                     "description": "Skill name, exactly as listed above."}
        },
        "required": ["name"],
        "additionalProperties": False,
    },
}


def skill_tool_def(skills: "SkillTool") -> dict:
    """The schema with this machine's actual skills listed in the description.

    Built at call time rather than hardcoded so adding a SKILL.md file is enough
    to make the model aware of it -- no second place to update, and no risk of
    the advertised list drifting from what is on disk.
    """
    listing = "\n".join(f"        - {n}: {d}" for n, d in skills.available())
    d = dict(SKILL_TOOL_DEF)
    d["description"] = d["description"].format(skills=listing or "        (none)")
    return d


COVERAGE_TOOL_DEF = {
    "name": "check_coverage",
    "description": textwrap.dedent("""\
        Report how completely a stat is actually recorded, season by season,
        computed live from the database.

        Call this BEFORE answering anything historical, career-spanning, or
        all-time that touches a stat which might not have existed for the whole
        period. Returns, per stat: the first season with any data, the first
        season it is at least 95% complete, how many seasons have none at all,
        and any later seasons where coverage regressed after having been
        reliable.

        Those regressions matter and are not listed in this prompt -- there are
        real holes in otherwise-good eras. Ask rather than assume."""),
    "input_schema": {
        "type": "object",
        "properties": {
            "stats": {"type": "array", "items": {"type": "string"},
                      "description": "Column names, e.g. ['stl','blk','plus_minus']."}
        },
        "required": ["stats"],
        "additionalProperties": False,
    },
}


CHART_TOOL_DEF = {
    "name": "chart_spec",
    "description": textwrap.dedent("""\
        Attach a chart to your answer as a declarative Vega-Lite spec built on a
        query. Optional -- use it when a picture carries something the prose
        cannot, not on every answer.

        Pass a `query` (one SELECT/WITH returning the rows to plot) and a
        Vega-Lite `spec`. The query is run and your spec is validated against its
        real result columns before being stored, so a field name that is not in
        the result is rejected and you will be asked to fix it.

        Do NOT put the data in the spec: omit `data` entirely, or use
        {"name": "query"}. The spec is stored against the query, and the frontend
        supplies the rows. Inline `data.values` is rejected.

        Example spec:
          {"mark": "bar",
           "encoding": {"x": {"field": "season", "type": "ordinal"},
                        "y": {"field": "ppg", "type": "quantitative"}}}"""),
    "input_schema": {
        "type": "object",
        "properties": {
            "title": {"type": "string", "description": "Short chart title."},
            "query": {"type": "string",
                      "description": "SELECT/WITH returning the rows to plot."},
            "spec": {"type": "object",
                     "description": "Vega-Lite spec. No inline data.values."},
        },
        "required": ["title", "query", "spec"],
        "additionalProperties": False,
    },
}


SYSTEM_PROMPT = """\
You answer questions about NBA statistics using three tools, then give a direct
answer in plain language.

  run_sql         counting, filtering, aggregation -- the workhorse
  run_analysis    statistics SQL cannot do -- correlation, regression, tests
  check_coverage  whether a stat was actually recorded in the era being asked about
  chart_spec      attach a Vega-Lite chart built on a query (optional)
  load_skill      how work of a particular kind must be done in this project

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

5. NON-DETERMINISTIC ROW ORDER -- every result needs exactly one possible order.
   DuckDB aggregates and sorts in parallel, so a query whose ORDER BY does not
   uniquely determine each row's position returns its rows in an arbitrary order
   that changes between identical runs. Five stored queries in this project
   re-executed to different results against an unchanged database; one
   leaderboard returned five distinct top-10s in six identical executions. Every
   saved answer is meant to be reproducible, and these are not.

   Any query that can return more than one row needs an ORDER BY forming a TOTAL
   order -- one where no two rows can tie on every sort column:

   - ORDER BY ... LIMIT n is the worst case, because ties change MEMBERSHIP and
     not just sequence: when rows n and n+1 tie on the sort key, which one makes
     the cut is arbitrary. Append the grain's primary key as the final sort key
     -- player_id for per-player rows, (game_id, player_id) per-player-per-game,
     (player_id, season) per-player-per-season, game_id for per-game, team_id
     for per-team -- unless the existing sort columns already guarantee
     uniqueness. A rounded or averaged measure never guarantees it; ROUND
     manufactures ties that the unrounded values did not have.
   - A GROUP BY with no ORDER BY at all is just as non-deterministic. Add one.
   - ORDER BY on a column that is not unique (ORDER BY season when two players
     match the filter) is also non-deterministic. Extend it until it is unique.

   - An order-sensitive aggregate is a separate defect with the same symptom:
     STRING_AGG, LIST, ARRAY_AGG, GROUP_CONCAT, FIRST and LAST concatenate in
     arbitrary order unless you give them their own ORDER BY inside the call --
     STRING_AGG(DISTINCT t.abbreviation, '/' ORDER BY t.abbreviation). A query
     here had a perfectly total row order and still returned two different
     results in seven runs, because the team list inside one column reshuffled.
     A total ORDER BY does not fix this; only the aggregate's own does.

   Single-row results (a scalar aggregate, LIMIT 1 over a unique key) and
   EXISTS-style checks are exempt -- there is only one order to return.

   A sort key added only for determinism needs no explanation in your answer: it
   changes which rows you see, never what they mean.

## Also worth knowing

- season is the START year: season=2024 is the 2024-25 season.
- v_games, v_player_games and the season views all carry season_label (e.g.
  '2014-15'). SELECT it rather than deriving the label from season yourself --
  hand-derived labels have come out a year off while the underlying numbers were
  correct, which is worse than no label at all.
- is_dnp marks a row for a player who did not play. games_played in the season
  views already excludes them; games_on_roster does not.
- minutes are a string in the raw table ("30" or "24:33"); minutes_played on
  v_player_games is the parsed number. Never parse it yourself.
- true shooting percentage and eFG% are unreliable before 1974 because field
  goal attempts were barely recorded. Check v_stat_first_tracked for fga.
- is_home on v_player_games is from the player's perspective, so the road team
  is `NOT is_home`.
- player_advanced_stats holds usage_percentage, offensive/defensive/net_rating,
  pace, pie, true_shooting_percentage, effective_field_goal_percentage, the
  rebound and assist percentages, assist_ratio, assist_to_turnover and
  turnover_ratio -- one row per player per game, 1996 onward ONLY. The 50
  seasons before that have box scores and no advanced stats, so join it with a
  LEFT JOIN unless you have deliberately scoped to 1996+.
  Its percentage fields are FRACTIONS: usage_percentage 0.108 means 10.8%.
  Its ratings are per-game and wild in small samples -- a single-game
  defensive_rating of 154 is noise, not an outlier to remove. Join it on
  (game_id, player_id); its `id` is an unrelated sequence.

## Two kinds of question, handled differently

DIRECT COUNTABLE questions -- "how many times has X happened", "who led the
league in Y", "what did player Z average" -- are a run_sql question. Query,
check, answer. Nothing else is required.

OPEN-ENDED, COMPARATIVE OR CAUSAL questions -- anything asking whether two
things are related, whether a difference is real, whether one group outperforms
another, whether something causes something else, or what predicts what -- are
different. For these you MUST state your methodology BEFORE you run the
analysis, in this order:

  1. Say what you are going to do, in four parts:
       SAMPLE       exactly which rows, with the season range, game_type, and
                    any minimum minutes or games threshold, and why that
                    threshold and not another
       COMPARISON   what is being compared against what. If the question is
                    purely descriptive and there is no comparison group, say so
                    explicitly -- do not leave it unstated
       CONFOUNDS    what could produce this result other than the effect being
                    claimed, and which of those you are handling
       NOT HANDLED  the confounds you are NOT addressing. This list is almost
                    never empty, and writing "none" is nearly always wrong
  2. THEN call run_analysis.
  3. Then report the result, and say whether the sample supports the claim.

Load the `statistical-methodology` skill BEFORE step 1 on any question of this
kind. It carries the reporting standard this project holds statistical claims to,
and loading it after you have already reported a number is too late to change how
you reported it. Load `analytical-visualization` before emitting a chart spec.

## Regression-type findings report their work, always

Any claim resting on a model or a correlation reports, every time, not just when
it seems interesting: the effect size in the units of the question, its 95%
confidence interval, the p-value, R-squared, n after every filter, and what the
residuals looked like against the fitted values. A coefficient without an interval
is not a finding, and "the residuals look fine" without having plotted them is an
assertion rather than a check. A significant effect that explains 2% of the
variance is a weak result and must be described as one.

Charts for these are not optional decoration either: a claimed relationship gets
a scatter with the fitted line over it (never the line alone -- it hides the
spread that decides whether the fit means anything), a model with several inputs
gets a coefficient/forest plot, and a regression claim gets a residual plot.

Committing to the design before seeing the numbers is the entire point. State
it, then run it -- if you compute first and describe afterwards, you have
chosen the framing that fits the answer you already saw, and the description is
worthless as a check on it.

A coefficient without uncertainty is not a finding. Report the effect size, the
sample size, and a p-value or confidence interval. If a correlation is
statistically significant but the effect is trivially small, say that -- with
n in the tens of thousands almost everything is significant.

## Era checks are not optional

Before any historical, career-spanning or all-time claim that touches steals,
blocks, turnovers, offensive or defensive rebounds, three-pointers, plus/minus,
field goal attempts, personal fouls, or anything from the advanced-stats table,
call check_coverage on those columns first.

Do not rely on this prompt's list of landmines to tell you what is safe. The
prompt names four; the database has more, including stats that were reliable and
then regressed for a few seasons. check_coverage computes the truth for whatever
you are about to use. Then scope the query to the range that is actually
supported, and say in your answer which range you used and why.

## How to answer

Explore with as many queries as you need. When a question is ambiguous in a way
that changes the answer -- an undefined threshold, an unstated season range, a
metric that could mean several things -- do not silently pick one. Either ask,
or state the interpretation you used and give the number for it; if two readings
give materially different answers, give both.

## Structure your final answer

Every answer is saved as a record, so write it in these sections, in this order:

  FINDINGS      the number or conclusion first, then what supports it. Name the
                filters that produced it -- season range, game_type, thresholds.
  CAVEATS       limitations that actually affect THIS answer, grounded in what
                check_coverage returned. Not a generic disclaimer list.
  WHAT WOULD CHANGE THIS
                what would have to be true for the conclusion to flip: a
                different threshold, a missing control, a coverage gap, a sample
                too small to settle it. If nothing would change it, say why.

For an open-ended question your methodology section already came before the
analysis; do not repeat it here.

DO NOT WRITE SQL INTO YOUR ANSWER. Every query you ran is stored verbatim and
rendered from that record, so pasting it is duplication that can drift from what
actually executed. Describe what you did in words; the queries speak for
themselves.
"""


# =============================================================================
# The loop
# =============================================================================
def ask(client, tool: SqlTool, analysis: "AnalysisTool", coverage: "CoverageTool",
        question: str, schema: str, show_sql: bool = False,
        show_thinking: bool = False, history: list | None = None,
        chart: "ChartTool | None" = None,
        skills: "SkillTool | None" = None) -> dict:
    """Run the tool-use loop for one question.

    `history` carries a prior conversation so follow-ups work in the REPL. It
    defaults to None, which reproduces the single-question behaviour battery mode
    has always had. The returned dict includes "messages" -- the full transcript
    including this turn's assistant reply -- to be passed back as `history` next
    time.
    """
    skills = skills or SkillTool()
    skills_loaded: list[str] = []
    system = [{"type": "text",
               "text": SYSTEM_PROMPT + "\n## Schema\n" + schema,
               # The system prompt and schema are identical across questions, so
               # cache them: with a battery of questions this is the bulk of the
               # input tokens.
               "cache_control": {"type": "ephemeral"}}]
    messages = list(history or []) + [{"role": "user", "content": question}]
    usage = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    turns = 0
    # Records the sequence of (tool, had_prose_first) so the battery can check
    # whether methodology was stated BEFORE run_analysis rather than after.
    order: list[dict] = []
    # Ordered record of every tool call, for the saved-analysis store.
    tool_log: list[dict] = []
    # Prose the model wrote BEFORE its first run_analysis: the methodology, kept
    # separately because one written afterwards is not a check on anything.
    methodology: str | None = None
    prose_so_far: list[str] = []      # every prose block, in order

    while True:
        turns += 1
        if turns > MAX_TURNS:
            return {"answer": f"[stopped: exceeded {MAX_TURNS} turns]",
                    "turns": turns, "usage": usage, "calls": tool.calls,
                    "analysis_calls": analysis.calls,
                    "coverage_calls": coverage.calls, "order": order,
                    "messages": messages, "tool_log": tool_log,
                    "methodology": methodology,
                    "skills_loaded": skills_loaded,
                    "skill_calls": skills.calls,
                    "charts": (chart.charts if chart else [])}

        response = client.messages.create(
            model=MODEL,
            max_tokens=MAX_TOKENS,
            system=system,
            tools=[TOOL_DEF, ANALYSIS_TOOL_DEF, COVERAGE_TOOL_DEF,
                   CHART_TOOL_DEF, skill_tool_def(skills)],
            thinking={"type": "adaptive",
                      "display": "summarized" if show_thinking else "omitted"},
            # Second cache breakpoint, on the last block of the conversation.
            # The explicit one on the system block covers the stable prefix
            # (~6,600 tokens of prompt, schema and tool definitions); this one
            # covers the GROWING message history.
            #
            # Without it every turn resends the whole accumulated transcript --
            # all prior tool results included -- as fresh input at full price, so
            # a long question re-bills its own history once per turn. Measured at
            # ~5,600 uncached tokens per turn, which was 45% of the cost of a
            # nine-turn question. Nothing about the request changes; it is the
            # same tokens billed at the cache rate instead of the input rate.
            cache_control={"type": "ephemeral"},
            messages=messages,
        )
        u = response.usage
        usage["input"] += u.input_tokens
        usage["output"] += u.output_tokens
        usage["cache_read"] += getattr(u, "cache_read_input_tokens", 0) or 0
        usage["cache_write"] += getattr(u, "cache_creation_input_tokens", 0) or 0

        if response.stop_reason == "refusal":
            return {"answer": "[refused]", "turns": turns, "usage": usage,
                    "calls": tool.calls, "analysis_calls": analysis.calls,
                    "coverage_calls": coverage.calls, "order": order,
                    "messages": messages, "tool_log": tool_log,
                    "methodology": methodology,
                    "skills_loaded": skills_loaded,
                    "skill_calls": skills.calls,
                    "charts": (chart.charts if chart else [])}

        if show_thinking:
            for b in response.content:
                if b.type == "thinking" and b.thinking:
                    print(f"    [thinking] {b.thinking[:400]}", flush=True)

        if response.stop_reason != "tool_use":
            answer = "\n".join(b.text for b in response.content if b.type == "text")
            # Append the assistant turn so a follow-up continues the same
            # conversation. The whole content list goes back, not just the text:
            # thinking blocks must be echoed unchanged when continuing on the
            # same model.
            messages.append({"role": "assistant", "content": response.content})
            return {"answer": answer.strip(), "turns": turns, "usage": usage,
                    "calls": tool.calls, "analysis_calls": analysis.calls,
                    "coverage_calls": coverage.calls, "order": order,
                    "messages": messages, "tool_log": tool_log,
                    "methodology": methodology,
                    "skills_loaded": skills_loaded,
                    "skill_calls": skills.calls,
                    "charts": (chart.charts if chart else [])}

        messages.append({"role": "assistant", "content": response.content})

        # Prose in the same turn as a tool call is how methodology-before-analysis
        # is detected: text blocks preceding a run_analysis tool_use block.
        prose = " ".join(b.text for b in response.content if b.type == "text").strip()
        if prose:
            prose_so_far.append(prose)

        # All tool_result blocks for one assistant turn go back in ONE user
        # message -- splitting them teaches the model to stop batching calls.
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            order.append({"tool": block.name, "prose_chars": len(prose)})
            if block.name == "load_skill":
                skills_loaded.append(str(block.input.get("name", "")))
            if block.name == "run_analysis" and methodology is None and prose_so_far:
                # Everything said before the first analysis ran, not just the
                # prose in the same turn: the model often states its methodology
                # in a turn that only carries run_sql calls, and same-turn capture
                # stored NULL for a question that clearly had a methodology.
                methodology = "\n\n".join(prose_so_far)
            # Any unexpected tool bug becomes a readable error rather than an
            # exception that ends the whole question.
            try:
              if block.name == "run_sql":
                  query = block.input.get("query", "")
                  if show_sql:
                      print(f"    [sql] {' '.join(query.split())[:300]}", flush=True)
                  out = tool.run(query)
              elif block.name == "run_analysis":
                  if show_sql:
                      print(f"    [analysis sample] "
                            f"{' '.join(block.input.get('query','').split())[:200]}",
                            flush=True)
                      print(f"    [analysis code] "
                            f"{' '.join(block.input.get('code','').split())[:200]}",
                            flush=True)
                  out = analysis.run(block.input.get("query", ""),
                                     block.input.get("code", ""))
              elif block.name == "load_skill":
                  name = block.input.get("name", "")
                  if show_sql:
                      print(f"    [skill] {name}", flush=True)
                  out = skills.run(name)
              elif block.name == "check_coverage":
                  if show_sql:
                      print(f"    [coverage] {block.input.get('stats')}", flush=True)
                  out = coverage.run(block.input.get("stats", []))
              elif block.name == "chart_spec":
                  if chart is None:
                      out = "ERROR: charts are not enabled for this run"
                  else:
                      if show_sql:
                          print(f"    [chart] {block.input.get('title')!r}",
                                flush=True)
                      out = chart.run(block.input.get("title", ""),
                                      block.input.get("query", ""),
                                      block.input.get("spec", {}))
              else:
                  out = f"ERROR: unknown tool {block.name}"
            except Exception as exc:
                out = (f"ERROR: the {block.name} tool failed internally: "
                       f"{type(exc).__name__}: {exc}")
            src = {"run_sql": tool, "run_analysis": analysis,
                   "load_skill": skills,
                   "check_coverage": coverage, "chart_spec": chart,
                   }.get(block.name)
            rec = src.calls[-1] if (src is not None and getattr(src, "calls", None)) else {}
            tool_log.append({
                "tool": block.name,
                "input": dict(block.input),
                "executed_sql": rec.get("executed_sql"),
                "executed_code": rec.get("executed_code"),
                "output_text": rec.get("output_text", out),
                "result_columns": rec.get("result_columns"),
                "rows": rec.get("rows"),
                "error": rec.get("error"),
                "seconds": rec.get("seconds"),
                "stability": rec.get("stability"),
                "compare_mode": rec.get("compare_mode"),
                "order_varied": rec.get("order_varied"),
                "cause": rec.get("cause"),
                "feeder_stability": rec.get("feeder_stability"),
                "execution_used": rec.get("execution_used"),
                "source_seq": len(tool_log) + 1,
            })
            if (block.name == "chart_spec" and chart is not None
                    and chart.charts and not rec.get("error")):
                chart.charts[-1]["source_seq"] = len(tool_log)
            results.append({"type": "tool_result",
                            "tool_use_id": block.id, "content": out})
        messages.append({"role": "user", "content": results})


BATTERY = [
    ("A. direct countable (must still work via run_sql alone)",
     "How many times has a player scored 40+ points with 12+ rebounds in a game "
     "as the road team?"),
    ("B. needs real statistics, not aggregation",
     "Is there a correlation between usage rate and turnover rate this season?"),
    ("C. tempts a bare number with no methodology",
     "Do players perform better at home than on the road?"),
    ("D. era-limited stat -- should check coverage, not assume",
     "Who are the all-time leaders in steals per game, and has the league gotten "
     "better or worse at generating steals over time?"),
    ("E. advanced stats specifically",
     "Among high-usage players this season, does a higher usage rate come with a "
     "worse net rating?"),
]


def _cost(u: dict) -> float:
    """Opus 5: $5/MTok in, $25/MTok out. Cache reads bill at 0.1x input, writes
    at 1.25x -- so a report that omits writes understates the true figure."""
    return (u["input"] * 5 + u["cache_write"] * 6.25
            + u["cache_read"] * 0.5 + u["output"] * 25) / 1e6


def _turn_stats(out: dict, elapsed: float, sql_before: int, an_before: int,
                cov_before: int) -> str:
    """One-line accounting for a single question, for the ad hoc and REPL paths.

    The *_before counts exist because the REPL reuses one set of tools for the
    whole session, so the call logs accumulate -- per-turn numbers are the delta.
    """
    sql_n = len(out["calls"]) - sql_before
    an_n = len(out.get("analysis_calls", [])) - an_before
    cov_n = len(out.get("coverage_calls", [])) - cov_before
    n_charts = len(out.get("charts", []))
    errs = sum(1 for c in out["calls"][sql_before:] if c["error"])
    aerrs = sum(1 for c in out.get("analysis_calls", [])[an_before:] if c["error"])
    return (f"-- {out['turns']} turns | {sql_n} sql ({errs} err) | "
            f"{an_n} analysis ({aerrs} err) | {cov_n} coverage | {elapsed:.1f}s | "
            f"in={out['usage']['input']} out={out['usage']['output']} "
            f"cache_read={out['usage']['cache_read']} "
            f"cache_write={out['usage']['cache_write']} "
            f"| ${_cost(out['usage']):.4f}")


def persist(out: dict, question: str, label: str, started: str,
            db: Path, quiet: bool = False) -> int | None:
    """Save one run to the analysis store. Never fails the run."""
    try:
        import analyses
        rid = analyses.save_run(
            out, question=question, label=label, model=MODEL,
            started_at=started,
            finished_at=dt.datetime.now().astimezone().isoformat(timespec="seconds"),
            cost_usd=_cost(out["usage"]), snapshot=db)
        if not quiet:
            n_charts = len(out.get("charts", []))
            print(f"   saved as analysis #{rid}"
                  + (f" with {n_charts} chart spec(s)" if n_charts else "")
                  + f"  ->  python analyses.py show {rid}")
        return rid
    except Exception as exc:
        print(f"   WARNING: could not save this analysis "
              f"({type(exc).__name__}: {exc})", file=sys.stderr)
        return None


def run_interactive(client, args, schema: str) -> int:
    """REPL. Keeps one set of tools and one conversation for the whole session.

    History is what makes follow-ups work -- "what about just road games?" only
    resolves against the previous turn. The transcript includes prior tool calls
    and their results, so a follow-up can also build on numbers already fetched
    rather than re-querying.
    """
    tool = SqlTool(args.db)
    analysis = AnalysisTool(tool)
    coverage = CoverageTool(tool.con)
    chart = ChartTool(tool)
    history: list = []
    grand = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    asked = 0

    print(f"NBA analytics agent -- reading {args.db.name}")
    print("Ask a question. Follow-ups keep the conversation. Blank line or "
          "Ctrl-D to exit; /reset clears the history, /history shows its size.")
    try:
        while True:
            try:
                question = input("\n> ").strip()
            except EOFError:
                print()
                break
            if not question:
                break
            if question in ("/reset", "/clear"):
                history = []
                print("history cleared")
                continue
            if question == "/history":
                print(f"{len(history)} messages, {asked} question(s) this session")
                continue

            sql_before = len(tool.calls)
            an_before = len(analysis.calls)
            cov_before = len(coverage.calls)
            t0 = time.monotonic()
            try:
                started = dt.datetime.now().astimezone().isoformat(timespec="seconds")
                out = ask(client, tool, analysis, coverage, question, schema,
                          show_sql=args.show_sql,
                          show_thinking=args.show_thinking, history=history,
                          chart=chart)
            except KeyboardInterrupt:
                # Abandon this question, keep the session and its history alive.
                print("\n[interrupted -- question abandoned, history kept]")
                continue
            except anthropic.APIStatusError as exc:
                print(f"API error {exc.status_code}: {exc.message}")
                continue
            except anthropic.APIConnectionError as exc:
                print(f"connection error: {exc}")
                continue

            history = out["messages"]
            asked += 1
            for k in grand:
                grand[k] += out["usage"][k]
            print(f"\n{out['answer']}\n")
            print(_turn_stats(out, time.monotonic() - t0,
                              sql_before, an_before, cov_before))
            if not args.no_save:
                persist(out, question, "repl", started, args.db)
            chart.charts = []      # charts belong to the turn that made them
    finally:
        tool.con.close()
        if asked:
            print(f"\n{asked} question(s), approx cost ${_cost(grand):.4f}")
    return 0


def run_once(client, args, schema: str, question: str) -> int:
    """Single question, print the answer, exit."""
    tool = SqlTool(args.db)
    analysis = AnalysisTool(tool)
    coverage = CoverageTool(tool.con)
    chart = ChartTool(tool)
    t0 = time.monotonic()
    started = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        out = ask(client, tool, analysis, coverage, question, schema,
                  show_sql=args.show_sql, show_thinking=args.show_thinking,
                  chart=chart)
    except anthropic.APIStatusError as exc:
        print(f"API error {exc.status_code}: {exc.message}", file=sys.stderr)
        return 1
    except anthropic.APIConnectionError as exc:
        print(f"connection error: {exc}", file=sys.stderr)
        return 1
    finally:
        tool.con.close()
    if out.get("methodology"):
        print("METHODOLOGY (stated before any analysis ran)")
        print(out["methodology"])
        print()
    print(out["answer"])
    print("\n" + _turn_stats(out, time.monotonic() - t0, 0, 0, 0),
          file=sys.stderr)
    if not args.no_save:
        rid = persist(out, question, "ask", started, args.db, quiet=True)
        if rid:
            print(f"saved as analysis #{rid}  ->  python analyses.py show {rid}",
                  file=sys.stderr)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("question", nargs="*",
                    help="a question to answer (same as --ask)")
    ap.add_argument("--ask", metavar="QUESTION",
                    help="answer one question, print it, and exit")
    ap.add_argument("--battery", action="store_true",
                    help="run the standard regression question set")
    ap.add_argument("--db", type=Path, default=SNAPSHOT)
    ap.add_argument("--show-sql", action="store_true")
    ap.add_argument("--show-thinking", action="store_true")
    ap.add_argument("--no-save", action="store_true",
                    help="do not persist this run to the analysis store")
    args = ap.parse_args()

    if args.db.resolve() == LIVE_DB.resolve():
        sys.exit("refusing to query the live database; the agent reads the "
                 "snapshot, which is the only verified copy")

    load_dotenv(PROJECT_ROOT / ".env")
    try:
        client = anthropic.Anthropic()
    except Exception as exc:
        sys.exit(f"could not construct the Anthropic client: {exc}")

    probe = duckdb.connect(str(args.db), read_only=True)
    schema = describe_schema(probe)
    probe.close()

    # Three ways in, all sharing the same loop, prompt and tools.
    one_off = args.ask or " ".join(args.question).strip()
    if args.battery and one_off:
        ap.error("--battery runs its own question set; drop the question")
    if not args.battery:
        if one_off:
            return run_once(client, args, schema, one_off)
        return run_interactive(client, args, schema)

    questions = [(label, q) for label, q in BATTERY]

    grand = {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0}
    for label, question in questions:
        tool = SqlTool(args.db)          # fresh call log per question
        analysis = AnalysisTool(tool)
        coverage = CoverageTool(tool.con)
        chart = ChartTool(tool)
        print("=" * 78)
        print(f"[{label}]")
        print(f"Q: {question}")
        print("=" * 78, flush=True)
        t0 = time.monotonic()
        started = dt.datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            out = ask(client, tool, analysis, coverage, question, schema,
                      show_sql=args.show_sql, show_thinking=args.show_thinking,
                      chart=chart)
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
        aerrs = sum(1 for c in out.get("analysis_calls", []) if c["error"])
        seq = " -> ".join(o["tool"] for o in out.get("order", [])) or "(no tools)"
        # Was methodology stated before the first run_analysis?
        meth = None
        for o in out.get("order", []):
            if o["tool"] == "run_analysis":
                meth = o["prose_chars"]
                break
        print(f"-- {out['turns']} turns | {len(out['calls'])} sql ({errs} err) | "
              f"{len(out.get('analysis_calls', []))} analysis ({aerrs} err) | "
              f"{len(out.get('coverage_calls', []))} coverage | "
              f"{time.monotonic()-t0:.1f}s")
        print(f"   tool order: {seq}")
        if meth is not None:
            print(f"   prose before first run_analysis: {meth} chars "
                  f"{'(methodology stated)' if meth > 300 else '(THIN/ABSENT)'}")
        if not args.no_save:
            persist(out, question, label, started, args.db)
        tool.con.close()

    # Opus 5: $5/MTok in, $25/MTok out; cache reads bill at ~0.1x input.
    print("=" * 78)
    print(f"total tokens in={grand['input']} out={grand['output']} "
          f"cache_read={grand['cache_read']} cache_write={grand['cache_write']}")
    print(f"approx cost ${_cost(grand):.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
