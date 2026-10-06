"""Local FastAPI backend for the Ask view (DESIGN_SPEC section 14).

Reuses the CLI pieces rather than reimplementing them, which is the whole point:
the agent loop, the tools, the persistence and the report renderer are already
verified by tests/, and a second implementation would drift from them silently.
Every endpoint here is a thin wrapper over agent.py or analyses.py.

Single user, local only. No auth, no multi-tenancy, no Explore mode -- all three
are deliberately out of scope for this step.

    .venv/bin/python -m uvicorn api:app --reload --port 8001
"""
from __future__ import annotations

import datetime as dt
import re
import threading
import traceback
import uuid
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import agent
import analyses
import team_brand

PROJECT_ROOT = Path(__file__).resolve().parent
WEB_DIR = PROJECT_ROOT / "web"

load_dotenv(PROJECT_ROOT / ".env")
app = FastAPI(title="nba-analytics", version="1.0")


# =============================================================================
# Agent runs
# =============================================================================
# One question at a time. The agent holds a DuckDB connection and spends real
# money per call; for a single-user local tool a queue of one is the honest
# model, and it keeps the snapshot's reader count predictable.
_agent_lock = threading.Lock()
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


class AskRequest(BaseModel):
    question: str
    label: str = "ask-ui"


def _set_job(job_id: str, **fields) -> None:
    with _jobs_lock:
        _jobs[job_id].update(fields)


def _run_agent(job_id: str, question: str, label: str) -> None:
    """Execute one question exactly as `agent.py --ask` does."""
    started = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    with _agent_lock:
        _set_job(job_id, status="running", started_at=started)
        tool = analysis = coverage = chart = None
        try:
            client = anthropic.Anthropic()
            tool = agent.SqlTool(analyses.SNAPSHOT)
            analysis = agent.AnalysisTool(tool)
            coverage = agent.CoverageTool(tool.con)
            chart = agent.ChartTool(tool)
            schema = agent.describe_schema(tool.con)
            out = agent.ask(client, tool, analysis, coverage, question, schema,
                            chart=chart)
            # Same persistence path as the CLI, so a UI run and a CLI run are
            # indistinguishable in the store.
            run_id = agent.persist(out, question, label, started,
                                   analyses.SNAPSHOT, quiet=True)
            _set_job(job_id, status="done", run_id=run_id,
                     turns=out.get("turns"), usage=out.get("usage"))
        except Exception as exc:
            _set_job(job_id, status="error",
                     error=f"{type(exc).__name__}: {exc}",
                     traceback=traceback.format_exc()[-4000:])
        finally:
            if tool is not None:
                try:
                    tool.con.close()
                except Exception:
                    pass


@app.post("/api/ask")
def ask(req: AskRequest) -> dict:
    """Start a question. Returns immediately; poll /api/jobs/{id}.

    A question takes 20-130s. Holding an HTTP connection open that long works
    locally but gives the UI nothing to show, so the run is a job and the view
    polls it.
    """
    question = req.question.strip()
    if not question:
        raise HTTPException(400, "question is empty")
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"id": job_id, "status": "queued", "question": question}
    threading.Thread(target=_run_agent, args=(job_id, question, req.label),
                     daemon=True).start()
    return {"job_id": job_id, "status": "queued",
            "queued_behind": 1 if _agent_lock.locked() else 0}


@app.get("/api/jobs/{job_id}")
def job(job_id: str) -> dict:
    with _jobs_lock:
        j = _jobs.get(job_id)
    if j is None:
        raise HTTPException(404, f"no job {job_id}")
    return j


# =============================================================================
# Saved analyses
# =============================================================================
def _takeaway(answer: str | None, limit: int = 150) -> str | None:
    """First real sentence of a saved answer, for one-line display."""
    import re as _re
    if not answer:
        return None
    # The heading arrives as "# FINDINGS", "**FINDINGS**" or bare, depending on
    # how the model formatted that run; splitting on "#" alone left the word in.
    body = _re.split(r"[#*\s]*\bFINDINGS\b[:*\s]*", answer, maxsplit=1)[-1]
    body = _re.sub(r"[#*`>|\-]{1,}", " ", body)
    body = _re.sub(r"\s+", " ", body).strip()
    if not body:
        return None
    m = _re.match(r"(.{40,}?[.!?])(\s|$)", body)
    out = (m.group(1) if m else body)[:limit].strip()
    return out + ("…" if len(out) >= limit else "")


@app.get("/api/analyses")
def list_analyses(limit: int = 50) -> dict:
    con = analyses.connect()
    try:
        rows = con.execute(
            """SELECT r.id, r.label, r.question, r.started_at, r.turns,
                      r.cost_usd, r.model,
                      COUNT(*) FILTER (WHERE tc.tool='run_sql')         AS sql_steps,
                      COUNT(*) FILTER (WHERE tc.stability='unstable')   AS unstable,
                      COUNT(*) FILTER (WHERE tc.tool='run_sql'
                                         AND tc.stability IS NULL)      AS unchecked,
                      COUNT(DISTINCT ch.id)                             AS charts,
                      r.final_answer                                   AS final_answer
               FROM runs r
               LEFT JOIN tool_calls tc ON tc.run_id = r.id
               LEFT JOIN charts ch     ON ch.run_id = r.id
               GROUP BY r.id ORDER BY r.id DESC LIMIT ?""", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            # The same three-way verdict the store supports: pre-guard steps are
            # unknown, not silently upgraded to reproducible.
            d["reproducibility"] = ("has unstable steps" if d["unstable"]
                                    else "unknown (predates the check)"
                                    if d["unchecked"] else "reproducible")
            out.append(d)
        # A one-line takeaway for the compact list. The stored answer opens with
        # a FINDINGS heading and then the conclusion; this lifts that first real
        # sentence so the overview can show what an analysis concluded without
        # pasting a paragraph of it.
        for d in out:
            d["takeaway"] = _takeaway(d.pop("final_answer", None))
        # Tags come back with the list so the sidebar can filter on them without
        # a second round trip per row.
        tagmap = _tags_for(con, "run", [d["id"] for d in out])
        for d in out:
            d["tags"] = tagmap.get(d["id"], [])
        return {"analyses": out}
    finally:
        con.close()


@app.get("/api/analyses/{run_id}")
def get_analysis(run_id: int, output: bool = False) -> dict:
    """One analysis, with the report rendered by analyses.render_report itself.

    The report text is NOT rebuilt here. Rendering it a second way is exactly how
    a UI drifts from the CLI -- a dropped caveat or a simplified stability line
    would be invisible until someone compared them by hand. The frontend shows
    this string verbatim and renders charts from the specs alongside it.
    """
    try:
        rec = analyses.load_run(run_id)
    except SystemExit as exc:
        raise HTTPException(404, str(exc))
    import json as _json

    import duckdb
    # A stored spec carries NO data: validate_chart_spec rejects inline
    # data.values on purpose, so a chart references its saved query rather than
    # embedding a stale copy of the rows. The rows therefore have to be fetched
    # here, by re-executing that exact query read-only. Without this the frontend
    # renders axes and no marks -- which it did, and which looked like a working
    # chart until the marks were actually counted.
    charts = []
    con = duckdb.connect(str(analyses.SNAPSHOT), read_only=True)
    try:
        by_call = {c["id"]: c for c in rec["calls"]}
        for ch in rec["charts"]:
            call = by_call.get(ch["tool_call_id"])
            rows, cols, err = [], [], None
            if call and call["executed_sql"]:
                try:
                    cur = con.execute(call["executed_sql"])
                    cols = [d[0] for d in cur.description]
                    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
                except Exception as exc:
                    err = f"{type(exc).__name__}: {exc}"
            else:
                err = "no stored query for this chart"
            charts.append({"id": ch["id"], "title": ch["title"],
                           "tool_call_id": ch["tool_call_id"],
                           "spec": _json.loads(ch["spec_json"]),
                           "columns": cols, "rows": rows, "error": err,
                           "stability": call["stability"] if call else None,
                           "cause": call["cause"] if call else None})
    finally:
        con.close()
    return {"run": rec["run"], "calls": rec["calls"], "charts": charts,
            "report": analyses.render_report(rec, show_output=output)}


@app.post("/api/analyses/{run_id}/rerun")
def rerun_analysis(run_id: int) -> dict:
    """Re-execute a saved analysis's stored SQL -- analyses.rerun, unchanged."""
    try:
        return analyses.rerun(run_id)
    except SystemExit as exc:
        raise HTTPException(404, str(exc))


@app.get("/api/health")
def health() -> dict:
    return {"ok": True, "snapshot": analyses.SNAPSHOT.name,
            "snapshot_sha256": analyses.snapshot_sha256(),
            "store": analyses.STORE.name,
            "agent_busy": _agent_lock.locked()}


# =============================================================================
# The Ask view
# =============================================================================
if WEB_DIR.is_dir():
    # No caching on the front-end assets. This is a local single-user tool that
    # is edited while it runs, and a stale cached module produces errors that
    # look like code bugs -- "lib.js does not provide an export named X" when the
    # export is plainly there. Correctness over a saved round trip on localhost.
    class _NoCacheStatic(StaticFiles):
        def file_response(self, *args, **kwargs):
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-store, must-revalidate"
            return resp

    app.mount("/static", _NoCacheStatic(directory=WEB_DIR), name="static")

    # Logos are immutable once downloaded, so they are the one asset that does
    # cache -- 30 PNGs re-fetched on every navigation is visible as a flicker.
    if (WEB_DIR / "logos").is_dir():
        app.mount("/logos", StaticFiles(directory=WEB_DIR / "logos"), name="logos")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(WEB_DIR / "index.html",
                            headers={"Cache-Control": "no-store, must-revalidate"})


# =============================================================================
# Explorer -- no LLM (design spec section 14, "Explore" mode)
# =============================================================================
# Every endpoint below is a parameterised read against the snapshot. The UI's own
# semantic layer lives here rather than in the frontend: game_type defaults to
# 'regular' on every route, because `postseason = false` is not "regular season"
# (it also holds play-in and Cup-final games), and the explorer is the easiest
# place in the system to mislead yourself with a wrong default.
import duckdb as _duckdb

_explore_con = None
# Reentrant on purpose: _rows() holds this while calling _ex(), which takes it
# again to create the connection on first use. A plain Lock deadlocks there --
# the first explorer request hung forever rather than erroring.
_explore_lock = threading.RLock()


def _ex():
    """One shared read-only connection. DuckDB serialises reads internally."""
    global _explore_con
    with _explore_lock:
        if _explore_con is None:
            _explore_con = _duckdb.connect(str(analyses.SNAPSHOT), read_only=True)
        return _explore_con


def _rows(sql: str, params: list | None = None) -> list[dict]:
    with _explore_lock:
        cur = _ex().execute(sql, params or [])
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, r)) for r in cur.fetchall()]


GAME_TYPES = ("regular", "playoff", "play_in", "cup_final")


def _game_type(value: str) -> str:
    if value not in GAME_TYPES:
        raise HTTPException(400, f"game_type must be one of {GAME_TYPES}")
    return value


@app.get("/api/explore/search")
def explore_search(q: str, limit: int = 20) -> dict:
    """Players and teams matching a query, for the explorer's jump box."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"players": [], "teams": []}
    like = f"%{q}%"
    players = _rows(
        """SELECT p.player_id, p.full_name, p.position, p.current_team_abbreviation,
                  MIN(t.season) AS first_season, MAX(t.season) AS last_season,
                  SUM(t.games_played) AS games
           FROM v_players p
           JOIN v_player_season_totals t USING (player_id)
           WHERE p.full_name ILIKE ? AND t.game_type = 'regular'
           GROUP BY 1,2,3,4
           ORDER BY games DESC, p.player_id
           LIMIT ?""", [like, limit])
    teams = _rows(
        """SELECT team_id, full_name, abbreviation, conference, division
           FROM v_teams WHERE (full_name ILIKE ? OR abbreviation ILIKE ?)
             AND is_nba_franchise
           ORDER BY full_name LIMIT 10""", [like, like])
    return {"players": players, "teams": teams}


@app.get("/api/explore/player/{player_id}")
def explore_player(player_id: int, game_type: str = "regular") -> dict:
    gt = _game_type(game_type)
    bio = _rows("SELECT * FROM v_players WHERE player_id = ?", [player_id])
    if not bio:
        raise HTTPException(404, f"no player {player_id}")
    seasons = _rows(
        """SELECT season, season_label, team_id, games_played, mpg, ppg, rpg, apg,
                  spg, bpg, fg3mpg, topg, fg_pct, fg3_pct, ft_pct, ts_pct, efg_pct
           FROM v_player_season_averages
           WHERE player_id = ? AND game_type = ?
           ORDER BY season, team_id""", [player_id, gt])
    career = _rows(
        """SELECT SUM(games_played) AS games, SUM(minutes) AS minutes,
                  SUM(pts) AS pts, SUM(reb) AS reb, SUM(ast) AS ast,
                  SUM(stl) AS stl, SUM(blk) AS blk, SUM(turnover) AS turnover,
                  MIN(season) AS first_season, MAX(season) AS last_season
           FROM v_player_season_totals
           WHERE player_id = ? AND game_type = ?""", [player_id, gt])
    return {"player": bio[0], "seasons": seasons,
            "career": career[0] if career else {}, "game_type": gt}


# The semantic layer already builds per-36, shooting and rolling-form views.
# They existed only as rows in the catalogue; these give them a way onto a
# player page, which is the reason they were written.
# The game-log views expose raw column names; a header reading "turnover" or
# "fg3m last n incl" is the database's vocabulary, not the reader's. Labelling
# here keeps the mapping in one place instead of as regex surgery in the UI.
GAME_COL_LABELS = {
    "pts": "PTS", "reb": "REB", "ast": "AST", "stl": "STL", "blk": "BLK",
    "fg3m": "3PM", "turnover": "TO", "fga": "FGA", "fta": "FTA",
    "true_shooting_attempts": "TSA", "ts_pct": "TS%", "efg_pct": "eFG%",
    "games_in_window": "GAMES IN AVG",
}


def _game_col_label(col: str) -> str:
    """PTS, PTS/36, PTS (5-game avg) -- from one base vocabulary."""
    if col.endswith("_per36"):
        return _game_col_label(col[: -len("_per36")]) + "/36"
    if col.endswith("_last_n_incl"):
        return _game_col_label(col[: -len("_last_n_incl")]) + " avg"
    return GAME_COL_LABELS.get(col, col.replace("_", " ").upper())


GAME_VIEWS = {
    "totals":  {"view": "v_player_games", "label": "Box score",
                "cols": ["pts", "reb", "ast", "stl", "blk", "fg3m", "turnover"]},
    "per36":   {"view": "v_player_game_per36", "label": "Per 36 minutes",
                "cols": ["pts_per36", "reb_per36", "ast_per36", "stl_per36",
                         "blk_per36", "fg3m_per36", "turnover_per36"]},
    "shooting": {"view": "v_player_game_shooting", "label": "Shooting",
                 "cols": ["pts", "fga", "fta", "fg3m", "true_shooting_attempts",
                          "ts_pct", "efg_pct"]},
    "form5":   {"view": "v_player_rolling_5", "label": "Rolling 5-game form",
                "cols": ["pts_last_n_incl", "reb_last_n_incl", "ast_last_n_incl",
                         "stl_last_n_incl", "blk_last_n_incl", "fg3m_last_n_incl",
                         "ts_pct_last_n_incl", "games_in_window"]},
    "form10":  {"view": "v_player_rolling_10", "label": "Rolling 10-game form",
                "cols": ["pts_last_n_incl", "reb_last_n_incl", "ast_last_n_incl",
                         "stl_last_n_incl", "blk_last_n_incl", "fg3m_last_n_incl",
                         "ts_pct_last_n_incl", "games_in_window"]},
}


@app.get("/api/explore/player/{player_id}/games")
def explore_player_games(player_id: int, view: str = "totals",
                         season: int | None = None,
                         game_type: str = "regular", limit: int = 200) -> dict:
    gt = _game_type(game_type)
    if view not in GAME_VIEWS:
        raise HTTPException(400, f"view must be one of {sorted(GAME_VIEWS)}")
    v = GAME_VIEWS[view]
    # These views differ in what they carry: the per-36, shooting and rolling
    # views drop is_dnp and season_label. Asking each view what it has beats
    # hardcoding a column list per view and discovering the difference as a 500.
    have = {r["column_name"] for r in _rows(
        """SELECT column_name FROM information_schema.columns
           WHERE table_schema='main' AND table_name=?""", [v["view"]])}
    base = [c for c in ("game_id", "game_date", "season", "season_label",
                        "opponent_team_id", "is_home", "minutes_played")
            if c in have]
    cols = [c for c in v["cols"] if c in have]
    sel = ", ".join(f'"{c}"' for c in base + cols)
    where = ["player_id = ?", "game_type = ?"]
    args: list = [player_id, gt]
    # Every one of these views carries the DNP rows -- checked against the
    # snapshot, 103 zero-minute rows for a single player in all five. Filtering
    # on is_dnp alone silently did nothing on the four views that lack the
    # column, so the per-36 and shooting tables opened on a wall of dashes.
    # minutes_played exists everywhere and means the same thing.
    if "is_dnp" in have:
        where.append("NOT is_dnp")
    elif "minutes_played" in have:
        where.append("coalesce(minutes_played, 0) > 0")
    if season is not None and "season" in have:
        where.append("season = ?")
        args.append(season)
    args.append(limit)
    rows = _rows(f"""SELECT {sel} FROM {v['view']}
                     WHERE {' AND '.join(where)}
                     ORDER BY game_date DESC, game_id DESC LIMIT ?""", args)
    return {"player_id": player_id, "view": view, "label": v["label"],
            "source_view": v["view"], "columns": cols,
            "labels": [_game_col_label(c) for c in cols],
            "views": [{"key": k, "label": val["label"], "view": val["view"]}
                      for k, val in GAME_VIEWS.items()],
            "season": season, "game_type": gt, "rows": rows}


@app.get("/api/explore/game/{game_id}")
def explore_game(game_id: int) -> dict:
    """One game's box score, both teams."""
    # Venue and attendance are not in this dataset -- `games` has no such
    # column -- but the period-by-period scoring, overtimes, bonus state and
    # In-Season Tournament stage all are, and a line score is the context a
    # box score is normally read beside.
    # Venue, attendance and officials are not in this dataset. The period-by-
    # period scoring is -- but on the base `games` table, not on v_games, which
    # is a curated view. Joining rather than widening the view keeps the
    # semantic layer untouched.
    head = _rows("""SELECT v.game_id, v.game_date, v.season, v.season_label,
                           v.game_type, v.postseason, v.status_state, v.period,
                           v.went_to_overtime, v.ist_stage,
                           -- cast in SQL: materialising a TIMESTAMPTZ makes
                           -- DuckDB reach for pytz, which is not a dependency
                           -- this project carries for one display string.
                           strftime(v.tipoff_utc AT TIME ZONE 'UTC',
                                    '%Y-%m-%d %H:%M') AS tipoff_utc,
                           v.total_points, v.home_margin, v.winning_team_id,
                           v.home_team_id, v.visitor_team_id,
                           v.home_team_abbreviation, v.visitor_team_abbreviation,
                           v.home_team_score, v.visitor_team_score,
                           b.home_q1, b.home_q2, b.home_q3, b.home_q4,
                           b.home_ot1, b.home_ot2, b.home_ot3,
                           b.visitor_q1, b.visitor_q2, b.visitor_q3, b.visitor_q4,
                           b.visitor_ot1, b.visitor_ot2, b.visitor_ot3
                    FROM v_games v JOIN games b ON b.id = v.game_id
                    WHERE v.game_id = ?""", [game_id])
    if not head:
        raise HTTPException(404, f"no game {game_id}")
    g = head[0]
    lines = _rows(
        """SELECT player_id, player_name, team_id, is_dnp,
                  minutes_played, pts, reb, oreb, dreb, ast, stl, blk, turnover,
                  pf, fgm, fga, fg3m, fg3a, ftm, fta, plus_minus
           FROM v_player_games WHERE game_id = ?
           ORDER BY team_id, is_dnp, minutes_played DESC, player_id""", [game_id])
    sides = []
    for tid, abbr, score in ((g["home_team_id"], g["home_team_abbreviation"],
                              g["home_team_score"]),
                             (g["visitor_team_id"], g["visitor_team_abbreviation"],
                              g["visitor_team_score"])):
        players = [r for r in lines if r["team_id"] == tid]
        played = [r for r in players if not r["is_dnp"]]
        totals = {k: sum((r[k] or 0) for r in played)
                  for k in ("pts", "reb", "ast", "stl", "blk", "turnover",
                            "fgm", "fga", "fg3m", "fg3a", "ftm", "fta")}
        home = tid == g["home_team_id"]
        pre = "home" if home else "visitor"
        # Only the periods that were actually played: every game has q1-q4,
        # almost none has ot3, and a row of empty OT columns is noise.
        line = [{"period": lab, "points": g.get(f"{pre}_{key}")}
                for key, lab in (("q1", "Q1"), ("q2", "Q2"), ("q3", "Q3"),
                                 ("q4", "Q4"), ("ot1", "OT"), ("ot2", "2OT"),
                                 ("ot3", "3OT"))
                if g.get(f"{pre}_{key}") is not None]
        sides.append({"team_id": tid, "abbreviation": abbr, "score": score,
                      "is_home": home,
                      "brand": team_brand.brand_for(tid),
                      "line": line,
                      "players": players, "totals": totals})
    has_line = any(side["line"] for side in sides)
    periods_played = max((len(side["line"]) for side in sides), default=0)
    context = [
        {"label": "Season", "value": g.get("season_label")},
        {"label": "Game type", "value": (g.get("game_type") or "").replace("_", " ")},
        # Derived from the line score, NOT from games.period. Before 2018-19
        # that column is a CONSTANT 4 -- all 62,943 of those games, never null
        # and never above 4 -- because the upstream feed reports regulation
        # periods only for that era. A constant 4 is a nastier failure than a
        # null would be: a double-overtime game from 1990 reads as an ordinary
        # regulation game rather than as missing data. It also makes
        # v_games.went_to_overtime (period > 4) report zero overtime across 72
        # seasons, 1946-2017, where the line score shows 5-7%. The per-period
        # score columns are ~99.5% populated back to 1946, so the line is the
        # honest source.
        {"label": "Periods played",
         "value": ((f"{periods_played} ({periods_played - 4} OT)"
                    if periods_played > 4 else periods_played)
                   if periods_played else g.get("period"))},
        {"label": "Tournament stage",
         "value": (g.get("ist_stage") or "").replace("_", " ") or None},
        {"label": "Tip-off (UTC)", "value": g.get("tipoff_utc")},
        {"label": "Margin",
         "value": (abs(g["home_margin"]) if g.get("home_margin") is not None else None)},
        {"label": "Total points", "value": g.get("total_points")},
    ]
    # No "not in this dataset" list: a field the data lacks is simply omitted
    # rather than announced. `context` is already filtered to what exists.
    return {"game": g, "sides": sides,
            "context": [c for c in context if c["value"] not in (None, "")],
            "has_line_score": has_line}


@app.get("/api/explore/seasons")
def explore_seasons(game_type: str = "regular") -> dict:
    """Seasons that actually exist, for the season picker.

    A free-text number input let you type 1823 and get an empty page; offering
    the real list means the control cannot ask for a season the data has not got.
    """
    gt = _game_type(game_type)
    rows = _rows("""SELECT season, ANY_VALUE(season_label) AS label,
                           COUNT(*) AS games
                    FROM v_games WHERE game_type = ?
                    GROUP BY season ORDER BY season DESC""", [gt])
    return {"game_type": gt, "seasons": rows,
            "latest": rows[0]["season"] if rows else None}


@app.get("/api/brand")
def brand_table() -> dict:
    """Team colours and logo paths.

    Flagged `curated: true` because none of this is in the snapshot -- the
    `teams` table has no colour, logo, arena or founding-year column. A page
    that themes itself from this should not imply the dataset knows it.
    """
    have, want = team_brand.have_logos()
    return {"curated": True,
            "source": "brand colours hand-entered; logos from ESPN's CDN, "
                      "downloaded locally by team_brand.py --fetch-logos",
            "logos_present": have, "logos_expected": want,
            "teams": team_brand.all_brands(),
            "neutral": team_brand.brand_for(None)}


@app.get("/api/explore/teams")
def explore_teams(game_type: str = "regular") -> dict:
    """Every franchise with its actual record, not a list of names to click.

    The previous version returned name/abbreviation/conference only, so the page
    could tell you a team existed and nothing else — every question needed a
    click. Years active, games and win percentage are computed here so the list
    itself answers something.
    """
    gt = _game_type(game_type)
    rows = _rows(
        """WITH g AS (
             SELECT home_team_id AS team_id, season,
                    CASE WHEN home_team_score > visitor_team_score THEN 1 ELSE 0 END AS win
             FROM v_games WHERE game_type = ?
             UNION ALL
             SELECT visitor_team_id, season,
                    CASE WHEN visitor_team_score > home_team_score THEN 1 ELSE 0 END
             FROM v_games WHERE game_type = ?)
           SELECT t.team_id, t.full_name, t.abbreviation, t.conference, t.division,
                  t.city, COUNT(*) AS games, SUM(g.win) AS wins,
                  MIN(g.season) AS first_season, MAX(g.season) AS last_season,
                  COUNT(DISTINCT g.season) AS seasons
           FROM v_teams t JOIN g USING (team_id)
           WHERE t.is_nba_franchise
           GROUP BY t.team_id, t.full_name, t.abbreviation, t.conference,
                    t.division, t.city
           ORDER BY t.full_name""", [gt, gt])
    # Last completed season's record, because "how were they last year" is the
    # question a standings-shaped list is actually asked. Franchise-wide
    # win% ranks the Spurs first and tells you nothing about now.
    latest = _rows("SELECT max(season) AS s FROM v_games WHERE game_type = ?", [gt])
    last_season = latest[0]["s"] if latest else None
    recent = {r["team_id"]: r for r in _rows(
        """WITH g AS (
             SELECT home_team_id AS team_id,
                    CASE WHEN home_team_score > visitor_team_score THEN 1 ELSE 0 END AS win
             FROM v_games WHERE game_type = ? AND season = ?
             UNION ALL
             SELECT visitor_team_id,
                    CASE WHEN visitor_team_score > home_team_score THEN 1 ELSE 0 END
             FROM v_games WHERE game_type = ? AND season = ?)
           SELECT team_id, COUNT(*) AS games, SUM(win) AS wins
           FROM g GROUP BY team_id""", [gt, last_season, gt, last_season])}
    for r in rows:
        r["losses"] = (r["games"] or 0) - (r["wins"] or 0)
        r["win_pct"] = (r["wins"] / r["games"]) if r["games"] else None
        cur = recent.get(r["team_id"])
        r["last_games"] = cur["games"] if cur else None
        r["last_wins"] = cur["wins"] if cur else None
        r["last_losses"] = (cur["games"] - cur["wins"]) if cur else None
        r["last_win_pct"] = (cur["wins"] / cur["games"]) if cur and cur["games"] else None
        r["brand"] = team_brand.brand_for(r["team_id"])
    return {"game_type": gt, "last_season": last_season, "teams": rows}


@app.get("/api/explore/team/{team_id}")
def explore_team(team_id: int, season: int | None = None,
                 game_type: str = "regular") -> dict:
    gt = _game_type(game_type)
    team = _rows("SELECT * FROM v_teams WHERE team_id = ?", [team_id])
    if not team:
        raise HTTPException(404, f"no team {team_id}")
    if season is None:
        got = _rows("SELECT MAX(season) AS s FROM v_games WHERE game_type = ?", [gt])
        season = got[0]["s"] if got else None
    record = _rows(
        """SELECT COUNT(*) AS games,
                  SUM(CASE WHEN (home_team_id = ? AND home_team_score > visitor_team_score)
                             OR (visitor_team_id = ? AND visitor_team_score > home_team_score)
                           THEN 1 ELSE 0 END) AS wins
           FROM v_games
           WHERE season = ? AND game_type = ?
             AND (home_team_id = ? OR visitor_team_id = ?)""",
        [team_id, team_id, season, gt, team_id, team_id])
    # Eleven columns, not seven: threes, turnovers and the three shooting
    # splits are the ones you actually compare rotation players on, and the
    # view already carries them.
    roster = _rows(
        """SELECT player_id, player_name, games_played, minutes, mpg, ppg, rpg, apg,
                  spg, bpg, fg3mpg, topg, fg_pct, fg3_pct, ft_pct, ts_pct, efg_pct,
                  pts_per36, reb_per36, ast_per36
           FROM v_player_season_averages
           WHERE team_id = ? AND season = ? AND game_type = ?
           ORDER BY mpg DESC, player_id""", [team_id, season, gt])
    seasons = _rows(
        """SELECT DISTINCT season FROM v_games
           WHERE (home_team_id = ? OR visitor_team_id = ?) AND game_type = ?
           ORDER BY season DESC""", [team_id, team_id, gt])
    # The season's actual games, so team -> season -> results is a hierarchy
    # rather than a page that changes one number when you pick a season.
    games = _rows(
        """SELECT game_id, game_date, season_label,
                  home_team_id, visitor_team_id,
                  home_team_abbreviation, visitor_team_abbreviation,
                  home_team_score, visitor_team_score,
                  CASE WHEN home_team_id = ? THEN TRUE ELSE FALSE END AS is_home
           FROM v_games
           WHERE season = ? AND game_type = ?
             AND (home_team_id = ? OR visitor_team_id = ?)
           ORDER BY game_date, game_id""",
        [team_id, season, gt, team_id, team_id])
    for g in games:
        us = g["home_team_score"] if g["is_home"] else g["visitor_team_score"]
        them = g["visitor_team_score"] if g["is_home"] else g["home_team_score"]
        g["points_for"], g["points_against"] = us, them
        g["won"] = (us is not None and them is not None and us > them)
        g["opponent"] = (g["visitor_team_abbreviation"] if g["is_home"]
                         else g["home_team_abbreviation"])
    # What the dataset actually knows about a franchise. Arena, founding year
    # and ownership are NOT in it -- `teams` carries conference, division, city,
    # name and abbreviation and nothing more -- so the page states the absence
    # rather than leaving a blank that reads as a loading failure.
    span = _rows(
        """SELECT MIN(season) AS first_season, MAX(season) AS last_season,
                  COUNT(DISTINCT season) AS seasons, COUNT(*) AS games,
                  SUM(CASE WHEN (home_team_id = ? AND home_team_score > visitor_team_score)
                             OR (visitor_team_id = ? AND visitor_team_score > home_team_score)
                           THEN 1 ELSE 0 END) AS wins
           FROM v_games
           WHERE game_type = ? AND (home_team_id = ? OR visitor_team_id = ?)""",
        [team_id, team_id, gt, team_id, team_id])
    fr = span[0] if span else {}
    facts = [
        {"label": "Conference", "value": (team[0].get("conference") or "").strip() or None},
        {"label": "Division", "value": (team[0].get("division") or "").strip() or None},
        {"label": "City", "value": team[0].get("city")},
        {"label": "Seasons played", "value": fr.get("seasons")},
        {"label": "First season", "value": fr.get("first_season")},
        {"label": "Franchise record",
         "value": (f"{fr['wins']}-{fr['games'] - fr['wins']}"
                   if fr.get("games") else None)},
        # All-time win rate over the franchise's whole history, not the
        # selected season -- the team page could show a 53-29 year with no
        # indication of whether that is normal for this franchise.
        {"label": "All-time win %",
         "value": (f"{fr['wins'] / fr['games']:.3f}"
                   if fr.get("games") else None)},
    ]
    return {"team": team[0], "season": season, "game_type": gt,
            "brand": team_brand.brand_for(team_id),
            "record": record[0] if record else {}, "roster": roster,
            "games": games, "facts": facts,
            "franchise": fr,
            "seasons": [r["season"] for r in seasons]}


# Leaderboard columns are whitelisted, not interpolated from user input: these
# names go into the SQL text, which is the one place a parameter cannot go.
LEADERBOARD_STATS = {
    "ppg": ("ppg", "Points per game"), "rpg": ("rpg", "Rebounds per game"),
    "apg": ("apg", "Assists per game"), "spg": ("spg", "Steals per game"),
    "bpg": ("bpg", "Blocks per game"), "mpg": ("mpg", "Minutes per game"),
    "fg3mpg": ("fg3mpg", "Threes per game"), "topg": ("topg", "Turnovers per game"),
    "ts_pct": ("ts_pct", "True shooting %"), "efg_pct": ("efg_pct", "Effective FG%"),
    "fg_pct": ("fg_pct", "Field goal %"), "fg3_pct": ("fg3_pct", "Three-point %"),
    "ft_pct": ("ft_pct", "Free throw %"),
    "pts_per36": ("pts_per36", "Points per 36"),
    "reb_per36": ("reb_per36", "Rebounds per 36"),
    "ast_per36": ("ast_per36", "Assists per 36"),
    "stl_per36": ("stl_per36", "Steals per 36"),
    "blk_per36": ("blk_per36", "Blocks per 36"),
    "fg3m_per36": ("fg3m_per36", "Threes per 36"),
    "turnover_per36": ("turnover_per36", "Turnovers per 36"),
}

# Which direction is "best" for each stat. Turnovers are the one where the
# leaderboard's default sort should not be descending, and a table that lets
# you sort by anything needs to know this per column rather than per page.
LOWER_IS_BETTER = {"topg", "turnover_per36"}

# Display order and formatting for the all-stats grid. One list so the header,
# the row and the sort control cannot disagree about what a column is.
LEADERBOARD_COLUMNS = [
    ("games_played", "G", "int"), ("mpg", "MPG", "num"),
    ("ppg", "PPG", "num"), ("rpg", "RPG", "num"), ("apg", "APG", "num"),
    ("spg", "SPG", "num"), ("bpg", "BPG", "num"), ("fg3mpg", "3PM", "num"),
    ("topg", "TO", "num"),
    ("fg_pct", "FG%", "pct"), ("fg3_pct", "3P%", "pct"),
    ("ft_pct", "FT%", "pct"), ("ts_pct", "TS%", "pct"), ("efg_pct", "eFG%", "pct"),
    ("pts_per36", "PTS/36", "num"), ("reb_per36", "REB/36", "num"),
    ("ast_per36", "AST/36", "num"), ("stl_per36", "STL/36", "num"),
    ("blk_per36", "BLK/36", "num"), ("fg3m_per36", "3PM/36", "num"),
    ("turnover_per36", "TO/36", "num"),
]


# Rates over a multi-season range are recomputed from summed totals, never
# averaged across seasons. Averaging season averages weights a 12-game season
# equally with an 82-game one and quietly produces a different leaderboard --
# the kind of UI-only "range support" that looks right and is not.
RANGE_RATES = {
    "ppg":    "SUM(pts)            / NULLIF(SUM(games_played),0)",
    "rpg":    "SUM(reb)            / NULLIF(SUM(games_played),0)",
    "apg":    "SUM(ast)            / NULLIF(SUM(games_played),0)",
    "spg":    "SUM(stl)            / NULLIF(SUM(games_played),0)",
    "bpg":    "SUM(blk)            / NULLIF(SUM(games_played),0)",
    "mpg":    "SUM(minutes)        / NULLIF(SUM(games_played),0)",
    "fg3mpg": "SUM(fg3m)           / NULLIF(SUM(games_played),0)",
    "topg":   "SUM(turnover)       / NULLIF(SUM(games_played),0)",
    "fg_pct": "SUM(fgm)            / NULLIF(SUM(fga),0)",
    "ft_pct": "SUM(ftm)            / NULLIF(SUM(fta),0)",
    "efg_pct": "(SUM(fgm) + 0.5*SUM(fg3m)) / NULLIF(SUM(fga),0)",
    "ts_pct": "SUM(pts) / NULLIF(2*(SUM(fga) + 0.44*SUM(fta)),0)",
    "fg3_pct": "SUM(fg3m)          / NULLIF(SUM(fg3a),0)",
    # Per-36 over a range is total production over total minutes, times 36 --
    # not the average of per-season per-36 figures, for the same reason the
    # rates above are not averages of averages.
    "pts_per36":      "36.0 * SUM(pts)      / NULLIF(SUM(minutes),0)",
    "reb_per36":      "36.0 * SUM(reb)      / NULLIF(SUM(minutes),0)",
    "ast_per36":      "36.0 * SUM(ast)      / NULLIF(SUM(minutes),0)",
    "stl_per36":      "36.0 * SUM(stl)      / NULLIF(SUM(minutes),0)",
    "blk_per36":      "36.0 * SUM(blk)      / NULLIF(SUM(minutes),0)",
    "fg3m_per36":     "36.0 * SUM(fg3m)     / NULLIF(SUM(minutes),0)",
    "turnover_per36": "36.0 * SUM(turnover) / NULLIF(SUM(minutes),0)",
}


@app.get("/api/explore/leaderboard")
def explore_leaderboard(stat: str = "ppg", season: int | None = None,
                        season_from: int | None = None,
                        season_to: int | None = None,
                        min_games: int = 41, game_type: str = "regular",
                        limit: int = 25) -> dict:
    gt = _game_type(game_type)
    if stat not in LEADERBOARD_STATS:
        raise HTTPException(400, f"stat must be one of {sorted(LEADERBOARD_STATS)}")
    col, label = LEADERBOARD_STATS[stat]

    latest = _rows("SELECT MAX(season) AS s FROM v_games WHERE game_type = ?", [gt])
    latest = latest[0]["s"] if latest else None
    if season_from is None and season_to is None:
        season_from = season_to = (season if season is not None else latest)
    else:
        season_from = season_from if season_from is not None else latest
        season_to = season_to if season_to is not None else latest
    if season_from > season_to:
        season_from, season_to = season_to, season_from
    is_range = season_from != season_to

    # Every stat comes back on every row, so the table can be re-sorted by any
    # column without another request. The chosen `stat` still decides WHO is in
    # the top N -- re-sorting reorders that set, it does not silently requery a
    # different one, and the UI says so.
    all_cols = ", ".join(f'"{c}"' for c, _lab, _f in LEADERBOARD_COLUMNS)
    desc = "ASC" if stat in LOWER_IS_BETTER else "DESC"

    if not is_range:
        rows = _rows(
            f"""SELECT player_id, player_name, team_id, {all_cols},
                       {col} AS value, 1 AS seasons
                FROM v_player_season_averages
                WHERE season = ? AND game_type = ? AND games_played >= ?
                  AND {col} IS NOT NULL
                ORDER BY value {desc}, player_id
                LIMIT ?""", [season_from, gt, min_games, limit])
    else:
        expr = RANGE_RATES.get(stat)
        if expr is None:
            raise HTTPException(
                400, f"{stat} cannot be aggregated across a range; pick one season")
        # min_games applies to the TOTAL across the range, which is the honest
        # reading of "at least this many games" for a multi-season query.
        # Same rule for every displayed column, not just the ranked one:
        # recomputed from summed totals, never averaged across seasons.
        range_cols = ", ".join(
            f'SUM(games_played) AS games_played' if c == "games_played"
            else f'{RANGE_RATES[c]} AS "{c}"'
            for c, _lab, _f in LEADERBOARD_COLUMNS if c in RANGE_RATES or c == "games_played")
        rows = _rows(
            f"""SELECT player_id, ANY_VALUE(player_name) AS player_name,
                       -- DISTINCT: a player traded mid-season has one row per
                       -- team in the totals view, so COUNT(*) reported seven
                       -- "seasons" inside a five-season window.
                       COUNT(DISTINCT season) AS seasons,
                       {range_cols},
                       {expr} AS value
                FROM v_player_season_totals
                WHERE season BETWEEN ? AND ? AND game_type = ?
                GROUP BY player_id
                HAVING SUM(games_played) >= ? AND {expr} IS NOT NULL
                ORDER BY value {desc}, player_id
                LIMIT ?""", [season_from, season_to, gt, min_games, limit])
        for r in rows:
            r["team_id"] = None

    base = {"ts_pct": "fga", "efg_pct": "fga", "fg_pct": "fga", "ft_pct": "fta",
            "fg3mpg": "fg3m", "spg": "stl", "bpg": "blk", "topg": "turnover",
            # mpg was the one rate with no coverage strip, which is how a
            # 1961-62 minutes leaderboard rendered with no warning at all.
            "mpg": "minutes_played"}.get(stat)
    cov = _rows("SELECT * FROM v_stat_first_tracked WHERE stat = ?",
                [base]) if base else []
    if base and not cov:
        # v_stat_first_tracked enumerates sixteen raw columns and minutes is
        # not one of them, so the lookup came back empty and the leaderboard
        # showed no coverage warning at all for minutes per game. Derive it
        # live on the same value-not-null rule used everywhere else.
        zero_missing = base.lower() in agent.ZERO_MEANS_MISSING
        expr = (f'SUM(CASE WHEN "{base}" > 0 THEN 1 ELSE 0 END)'
                if zero_missing else f'COUNT("{base}")')
        try:
            per = _rows(f"""SELECT season, COUNT(*) AS played, {expr} AS have
                            FROM v_player_games
                            WHERE game_type = ? AND NOT is_dnp
                            GROUP BY season ORDER BY season""", [gt])
        except Exception:
            per = []
        if per:
            first_any = next((r["season"] for r in per if (r["have"] or 0) > 0), None)
            first_full = None
            for i, r in enumerate(per):
                if all((x["have"] or 0) / x["played"] >= 0.95
                       for x in per[i:] if x["played"]):
                    first_full = r["season"]
                    break
            cov = [{"stat": base, "first_tracked_season": first_any,
                    "first_full_season": first_full, "derived_live": True}]
    shown = [{"key": c, "label": lab, "format": f,
              "lower_is_better": c in LOWER_IS_BETTER}
             for c, lab, f in LEADERBOARD_COLUMNS
             if not is_range or c in RANGE_RATES or c == "games_played"]
    return {"stat": stat, "label": label, "season": season_from,
            "season_from": season_from, "season_to": season_to,
            "is_range": is_range, "game_type": gt,
            "columns": shown,
            "stats": [{"key": k, "label": v[1]} for k, v in LEADERBOARD_STATS.items()],
            "min_games": min_games, "rows": rows,
            "aggregation": ("rates recomputed from summed totals across "
                            f"{season_to - season_from + 1} seasons"
                            if is_range else "single season"),
            "coverage": cov[0] if cov else None}


@app.get("/api/explore/catalog")
def explore_catalog() -> dict:
    """What is in the database and how far back each stat can be trusted.

    Coverage is computed by scanning the live schema, not read from
    `v_stat_first_tracked`. That view enumerates 16 column names as SQL literals,
    so when `player_advanced_stats` arrived in a later phase its 15 columns
    silently acquired no coverage entry at all -- the catalog looked complete
    while omitting a whole table's worth of stats. Deriving the list means adding
    a table cannot fail to show up here.
    """
    tables = _rows(
        """SELECT table_name, table_type FROM information_schema.tables
           WHERE table_schema = 'main'
           ORDER BY table_type DESC, table_name""")
    for t in tables:
        t["internal"] = t["table_name"].startswith("_")
        try:
            t["rows"] = _rows(f'SELECT COUNT(*) AS n FROM "{t["table_name"]}"')[0]["n"]
        except Exception:
            t["rows"] = None
    span = _rows("""SELECT MIN(season) AS first_season, MAX(season) AS last_season,
                           COUNT(DISTINCT season) AS seasons FROM v_games""")
    return {"tables": tables, "span": span[0] if span else {},
            "coverage": _derived_coverage(),
            "coverage_source": ("computed live from every numeric column of "
                                "player_stats and player_advanced_stats")}


_SKIP_COLS = {"id", "game_id", "player_id", "team_id", "season", "postseason"}


def _derived_coverage() -> list[dict]:
    """First-seen / first-reliable season for every stat column that exists.

    Denominator is PLAYED rows only. Measuring against all roster rows counts
    DNPs, which are all-NULL, and understates every stat -- a mistake this
    project has made twice.
    """
    out: list[dict] = []
    for table, joinsql in (
            ("player_stats",
             "FROM v_player_games g WHERE NOT g.is_dnp"),
            ("player_advanced_stats",
             "FROM v_player_games g JOIN player_advanced_stats a "
             "ON a.game_id = g.game_id AND a.player_id = g.player_id "
             "WHERE NOT g.is_dnp")):
        try:
            cols = _rows(f"""SELECT column_name, data_type
                             FROM information_schema.columns
                             WHERE table_schema='main' AND table_name=?
                             ORDER BY ordinal_position""", [table])
        except Exception:
            continue
        for c in cols:
            name = c["column_name"]
            if name in _SKIP_COLS or name.startswith("_"):
                continue
            if not any(k in (c["data_type"] or "").upper()
                       for k in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "BIGINT")):
                continue
            src = "a" if table == "player_advanced_stats" else "g"
            # Same distinction the coverage tool makes: a stored zero means
            # "not recorded" for minutes and "none of them" for a counting
            # stat. Counting non-nulls reported `min` as fully covered back
            # to 1946, which is how a 31.3 mpg Bill Russell reached the page.
            recorded = (f'SUM(CASE WHEN {src}."{name}" > 0 THEN 1 ELSE 0 END)'
                        if name.lower() in agent.ZERO_MEANS_MISSING
                        else f'COUNT({src}."{name}")')
            try:
                per = _rows(f"""SELECT g.season,
                                       COUNT(*) AS played,
                                       {recorded} AS recorded
                                {joinsql} AND g.game_type='regular'
                                GROUP BY g.season ORDER BY g.season""")
            except Exception:
                continue
            seen = [r for r in per if r["recorded"] > 0]
            full = [r for r in per
                    if r["played"] and r["recorded"] / r["played"] >= 0.95]
            out.append({
                "stat": name, "table": table,
                "zero_means_missing": name.lower() in agent.ZERO_MEANS_MISSING,
                "first_tracked_season": seen[0]["season"] if seen else None,
                "first_full_season": full[0]["season"] if full else None,
                "seasons_with_data": len(seen),
                "seasons_absent": sum(1 for r in per if r["recorded"] == 0)})
    return sorted(out, key=lambda r: (r["table"], r["stat"]))


@app.get("/api/explore/table/{name}")
def explore_table(name: str) -> dict:
    """One table's fields, with coverage where the column is a stat.

    The catalog listed table names and a flat stat list side by side, so there
    was no way to ask "what is actually in player_advanced_stats" — the two
    halves never joined up.
    """
    known = {t["table_name"] for t in _rows(
        "SELECT table_name FROM information_schema.tables WHERE table_schema='main'")}
    if name not in known:
        raise HTTPException(404, f"no table or view {name!r}")
    cols = _rows("""SELECT column_name, data_type, is_nullable, ordinal_position
                    FROM information_schema.columns
                    WHERE table_schema='main' AND table_name=?
                    ORDER BY ordinal_position""", [name])
    kind = _rows("""SELECT table_type FROM information_schema.tables
                    WHERE table_schema='main' AND table_name=?""", [name])
    try:
        rowcount = _rows(f'SELECT COUNT(*) AS n FROM "{name}"')[0]["n"]
    except Exception:
        rowcount = None
    cov = {c["stat"]: c for c in _derived_coverage() if c["table"] == name}
    for c in cols:
        hit = cov.get(c["column_name"])
        c["first_tracked_season"] = hit["first_tracked_season"] if hit else None
        c["first_full_season"] = hit["first_full_season"] if hit else None
        c["seasons_absent"] = hit["seasons_absent"] if hit else None
    # A handful of example rows makes the shape concrete in a way a type list
    # does not; capped and never for the internal log.
    sample = []
    if not name.startswith("_"):
        try:
            sample = _rows(f'SELECT * FROM "{name}" LIMIT 3')
        except Exception:
            sample = []
    return {"table": name, "kind": kind[0]["table_type"] if kind else None,
            "rows": rowcount, "columns": cols, "sample": sample}


@app.get("/api/explore/coverage")
def explore_coverage(stat: str, game_type: str = "regular") -> dict:
    """Per-season completeness for one stat, for shading a chart's unreliable era.

    Denominator is PLAYED rows only. Measuring against all roster rows counts
    DNPs -- which are all-NULL -- and understates every stat; that mistake has
    been made twice in this project already.
    """
    gt = _game_type(game_type)
    # Under the lock like every other query. Executing on the shared connection
    # outside it let two concurrent requests interleave on one cursor -- the
    # dataset-catalog page fires six of these at once, and one came back with
    # another request's columns and 400'd. Serial calls never showed it.
    with _explore_lock:
        cols = {d[0] for d in
                _ex().execute("SELECT * FROM v_player_games LIMIT 0").description}
    if stat not in cols:
        raise HTTPException(400, f"unknown column {stat!r}")
    # A stored zero means "not recorded" for minutes and "none of them" for a
    # counting stat; see agent.ZERO_MEANS_MISSING. Counting non-nulls showed
    # minutes at 100% in 1946, when in fact almost nobody has a figure.
    zero_missing = stat.lower() in agent.ZERO_MEANS_MISSING
    recorded = (f'SUM(CASE WHEN "{stat}" > 0 THEN 1 ELSE 0 END)'
                if zero_missing else f'COUNT("{stat}")')
    rows = _rows(
        f"""SELECT season, season_label, COUNT(*) AS played_rows,
                   {recorded} AS recorded,
                   ROUND(100.0 * {recorded} / NULLIF(COUNT(*), 0), 1) AS pct
            FROM v_player_games
            WHERE game_type = ? AND NOT is_dnp
            GROUP BY season, season_label
            ORDER BY season""", [gt])
    first = _rows("SELECT * FROM v_stat_first_tracked WHERE stat = ?", [stat])
    return {"stat": stat, "game_type": gt, "seasons": rows,
            "first_tracked": first[0] if first else None}


# =============================================================================
# Correlation explorer -- no LLM (design spec section 14)
# =============================================================================
# Scanning variable pairs manufactures chance patterns: at p<0.05, one pair in
# twenty looks "significant" with nothing behind it. So every result this
# endpoint returns is labelled exploratory, and confirming one is a separate act
# that goes through the agent with a held-out era.
#
# `jersey_number`, `draft_number` and the physical attributes are deliberately
# offered alongside the performance stats. An explorer that only exposes
# plausibly-related variables hides its own false-positive rate; being able to
# plot points-per-game against jersey number is what makes the warning legible.
CORR_VARS: dict[str, dict] = {
    # key: (label, sql expression, source, coverage column or None)
    "ppg":        {"label": "Points per game",       "expr": "a.ppg",      "cov": None},
    "rpg":        {"label": "Rebounds per game",     "expr": "a.rpg",      "cov": "reb"},
    "apg":        {"label": "Assists per game",      "expr": "a.apg",      "cov": "ast"},
    "spg":        {"label": "Steals per game",       "expr": "a.spg",      "cov": "stl"},
    "bpg":        {"label": "Blocks per game",       "expr": "a.bpg",      "cov": "blk"},
    "topg":       {"label": "Turnovers per game",    "expr": "a.topg",     "cov": "turnover"},
    "fg3mpg":     {"label": "Threes made per game",  "expr": "a.fg3mpg",   "cov": "fg3m"},
    "mpg":        {"label": "Minutes per game",      "expr": "a.mpg",      "cov": None},
    "fg_pct":     {"label": "Field goal %",          "expr": "a.fg_pct",   "cov": "fga"},
    "fg3_pct":    {"label": "Three-point %",         "expr": "a.fg3_pct",  "cov": "fg3a"},
    "ft_pct":     {"label": "Free throw %",          "expr": "a.ft_pct",   "cov": "fta"},
    "ts_pct":     {"label": "True shooting %",       "expr": "a.ts_pct",   "cov": "fga"},
    "games":      {"label": "Games played",          "expr": "a.games_played", "cov": None},
    "height_in":  {"label": "Height (inches)",       "expr": "p.height_inches", "cov": None},
    "weight_lbs": {"label": "Weight (lb)",           "expr": "p.weight_lbs",    "cov": None},
    "draft_number": {"label": "Draft pick number",   "expr": "p.draft_number",  "cov": None},
    "jersey":     {"label": "Jersey number (arbitrary)",
                   "expr": "TRY_CAST(p.jersey_number AS DOUBLE)", "cov": None},
}
MAX_SCATTER_POINTS = 1200

# Variables that come from v_players: one value per player, constant across all
# of that player's seasons. Pairing one of these against a season-varying stat
# at player-season grain is pseudo-replication -- a player with 15 seasons
# contributes 15 rows carrying one jersey number, so n is inflated and the
# p-value with it. Found by an investigation of the jersey/FT% pairing, which
# reported the explorer's own n as "inflated roughly five-fold and its p-value
# meaningless as stated". It was right, so the explorer now reports the
# player-collapsed statistic alongside the naive one and says which to trust.
PLAYER_CONSTANT = {"height_in", "weight_lbs", "draft_number", "jersey"}


@app.get("/api/explore/variables")
def explore_variables() -> dict:
    return {"variables": [{"key": k, "label": v["label"], "coverage_stat": v["cov"]}
                          for k, v in CORR_VARS.items()]}


@app.get("/api/explore/correlation")
def explore_correlation(x: str, y: str, season_from: int = 2000,
                        season_to: int = 2025, min_games: int = 41,
                        game_type: str = "regular") -> dict:
    gt = _game_type(game_type)
    for key in (x, y):
        if key not in CORR_VARS:
            raise HTTPException(400, f"unknown variable {key!r}; "
                                     f"choose from {sorted(CORR_VARS)}")
    if x == y:
        raise HTTPException(400, "pick two different variables")
    if season_from > season_to:
        season_from, season_to = season_to, season_from

    vx, vy = CORR_VARS[x], CORR_VARS[y]
    rows = _rows(
        f"""SELECT a.player_id, a.player_name, a.season, a.season_label,
                   {vx['expr']} AS x, {vy['expr']} AS y
            FROM v_player_season_averages a
            JOIN v_players p USING (player_id)
            WHERE a.game_type = ? AND a.season BETWEEN ? AND ?
              AND a.games_played >= ?
              AND {vx['expr']} IS NOT NULL AND {vy['expr']} IS NOT NULL
            ORDER BY a.player_id, a.season""",
        [gt, season_from, season_to, min_games])

    n = len(rows)
    if n < 10:
        return {"x": x, "y": y, "n": n, "insufficient": True,
                "exploratory": True, "points": [], "stats": None}

    import statistics

    from scipy import stats as st
    xs = [float(r["x"]) for r in rows]
    ys = [float(r["y"]) for r in rows]
    r_p, p_p = st.pearsonr(xs, ys)
    r_s, p_s = st.spearmanr(xs, ys)
    slope, intercept, _, _, stderr = st.linregress(xs, ys)
    # 95% CI on the slope, so the reader sees the uncertainty and not just r.
    tcrit = st.t.ppf(0.975, n - 2)
    lo, hi = slope - tcrit * stderr, slope + tcrit * stderr

    # Thin for the browser, deterministically: every k-th row of a fixed order,
    # never a random sample, so the same query draws the same picture twice.
    step = max(1, n // MAX_SCATTER_POINTS)
    pts = [{"x": r["x"], "y": r["y"], "player": r["player_name"],
            "season": r["season_label"]} for r in rows[::step]]

    # Collapse to one row per player when either variable cannot vary within a
    # player. This is the statistic to read; the naive one is kept only so the
    # difference between them is visible rather than quietly corrected.
    n_players = len({r["player_id"] for r in rows})
    collapsed = None
    if (x in PLAYER_CONSTANT or y in PLAYER_CONSTANT) and n_players >= 10:
        agg: dict = {}
        for r in rows:
            a = agg.setdefault(r["player_id"], [[], []])
            a[0].append(float(r["x"])); a[1].append(float(r["y"]))
        cx = [sum(v[0]) / len(v[0]) for v in agg.values()]
        cy = [sum(v[1]) / len(v[1]) for v in agg.values()]
        cr, cp = st.pearsonr(cx, cy)
        cslope, _, _, _, cse = st.linregress(cx, cy)
        ct = st.t.ppf(0.975, len(cx) - 2)
        collapsed = {"n": len(cx), "pearson_r": cr, "pearson_p": cp,
                     "r_squared": cr ** 2, "slope": cslope,
                     "slope_ci": [cslope - ct * cse, cslope + ct * cse]}

    xmin, xmax = min(xs), max(xs)
    fit = [{"x": xmin, "fit": intercept + slope * xmin},
           {"x": xmax, "fit": intercept + slope * xmax}]

    cov = {}
    for key, var in (("x", vx), ("y", vy)):
        if var["cov"]:
            got = _rows("SELECT * FROM v_stat_first_tracked WHERE stat = ?",
                        [var["cov"]])
            if got:
                cov[key] = got[0]
    return {
        "x": x, "y": y, "x_label": vx["label"], "y_label": vy["label"],
        "season_from": season_from, "season_to": season_to,
        "game_type": gt, "min_games": min_games, "n": n,
        "n_players": n_players, "collapsed": collapsed,
        "player_constant": sorted(({x, y} & PLAYER_CONSTANT)),
        "points": pts, "point_step": step, "fit": fit,
        "stats": {"pearson_r": r_p, "pearson_p": p_p,
                  "spearman_rho": r_s, "spearman_p": p_s,
                  "r_squared": r_p ** 2, "slope": slope,
                  "slope_ci": [lo, hi], "intercept": intercept,
                  "x_sd": statistics.pstdev(xs), "y_sd": statistics.pstdev(ys)},
        "coverage": cov,
        # Never omitted, never conditional on the result looking weak.
        "exploratory": True,
        "warning": ("This page scans variable pairs, so roughly "
                    "1 in 20 unrelated pairs will look significant at p<0.05. "
                    "A correlation here is a hypothesis, not a finding. There is "
                    "currently no automated path to test one on a held-out "
                    "era; the investigate hand-off was removed and its MCP "
                    "replacement is not built."),
        "pseudo_replication_warning": (
            f"{', '.join(CORR_VARS[k]['label'] for k in sorted({x, y} & PLAYER_CONSTANT))} "
            f"does not vary within a player, so these {n} player-seasons carry only "
            f"{n_players} independent values. The row-level p-value is overstated; "
            f"read the one-row-per-player figures below it."
            if ({x, y} & PLAYER_CONSTANT) and collapsed else None),
    }


# =============================================================================
# Knowledge base -- saved analyses + the user's own notes, tagged and searchable
# =============================================================================
# Structured, not RAG. Tags are exact (player / team / stat / era / free) and
# search is over the text the system actually stored. The point is that "show me
# everything about Jokic" returns exactly the items tagged Jokic, not the items
# that embed near him -- and that the answer is explainable from a join.
class NoteIn(BaseModel):
    """A note is an annotation ON an analysis, never a standalone document.

    Freestanding notes made the knowledge base two stores that had to be
    searched separately and could drift apart; an annotation that cannot be
    traced to the analysis it comments on is also the kind of claim this project
    spends its effort avoiding.
    """
    title: str
    body: str = ""
    run_id: int                   # required
    tags: list[str] = []          # "kind:value", or bare value -> kind "free"


def _parse_tag(raw: str) -> tuple[str, str]:
    raw = (raw or "").strip()
    kind, _, value = raw.partition(":")
    if not value:
        return "free", kind.strip().lower()
    kind = kind.strip().lower()
    if kind not in ("player", "team", "stat", "era", "free"):
        kind = "free"
    return kind, value.strip().lower()


def _apply_tags(con, item_type: str, item_id: int, raw_tags: list[str]) -> None:
    con.execute("DELETE FROM item_tags WHERE item_type=? AND item_id=?",
                (item_type, item_id))
    for raw in raw_tags:
        kind, value = _parse_tag(raw)
        if not value:
            continue
        con.execute("INSERT OR IGNORE INTO tags (kind, value) VALUES (?,?)",
                    (kind, value))
        tag_id = con.execute("SELECT id FROM tags WHERE kind=? AND value=?",
                             (kind, value)).fetchone()[0]
        con.execute("INSERT OR IGNORE INTO item_tags (tag_id, item_type, item_id) "
                    "VALUES (?,?,?)", (tag_id, item_type, item_id))


def _tags_for(con, item_type: str, ids: list[int]) -> dict[int, list[str]]:
    if not ids:
        return {}
    marks = ",".join("?" * len(ids))
    out: dict[int, list[str]] = {i: [] for i in ids}
    for r in con.execute(
            f"""SELECT it.item_id, t.kind, t.value FROM item_tags it
                JOIN tags t ON t.id = it.tag_id
                WHERE it.item_type=? AND it.item_id IN ({marks})
                ORDER BY t.kind, t.value""", [item_type, *ids]):
        out[r["item_id"]].append(f"{r['kind']}:{r['value']}")
    return out


@app.get("/api/kb/tags")
def kb_tags() -> dict:
    con = analyses.connect()
    try:
        rows = con.execute(
            """SELECT t.kind, t.value, COUNT(*) AS uses
               FROM tags t JOIN item_tags it ON it.tag_id = t.id
               GROUP BY t.id ORDER BY t.kind, uses DESC, t.value""").fetchall()
        return {"tags": [dict(r) for r in rows]}
    finally:
        con.close()


@app.post("/api/kb/notes")
def kb_add_note(note: NoteIn) -> dict:
    if not note.title.strip():
        raise HTTPException(400, "a note needs a title")
    now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
    con = analyses.connect()
    try:
        if con.execute("SELECT 1 FROM runs WHERE id=?",
                       (note.run_id,)).fetchone() is None:
            raise HTTPException(404, f"no analysis {note.run_id} to attach to")
        cur = con.execute(
            "INSERT INTO notes (title, body, run_id, created_at, updated_at) "
            "VALUES (?,?,?,?,?)",
            (note.title.strip(), note.body, note.run_id, now, now))
        note_id = cur.lastrowid
        _apply_tags(con, "note", note_id, note.tags)
        con.commit()
        return {"id": note_id}
    finally:
        con.close()


@app.delete("/api/kb/notes/{note_id}")
def kb_delete_note(note_id: int) -> dict:
    con = analyses.connect()
    try:
        cur = con.execute("DELETE FROM notes WHERE id=?", (note_id,))
        con.execute("DELETE FROM item_tags WHERE item_type='note' AND item_id=?",
                    (note_id,))
        con.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, f"no note {note_id}")
        return {"deleted": note_id}
    finally:
        con.close()


class TagsIn(BaseModel):
    tags: list[str]


@app.put("/api/kb/analyses/{run_id}/tags")
def kb_tag_run(run_id: int, body: TagsIn) -> dict:
    con = analyses.connect()
    try:
        if con.execute("SELECT 1 FROM runs WHERE id=?", (run_id,)).fetchone() is None:
            raise HTTPException(404, f"no analysis {run_id}")
        _apply_tags(con, "run", run_id, body.tags)
        con.commit()
        return {"run_id": run_id, "tags": _tags_for(con, "run", [run_id])[run_id]}
    finally:
        con.close()


@app.get("/api/kb/search")
def kb_search(q: str = "", tag: str = "", limit: int = 50) -> dict:
    """Analyses, each carrying its own notes. One result type, not two pools.

    A note matches by its own text or tags, and brings its analysis with it --
    so searching "coin flip" finds the analysis whether that phrase is in the
    answer or in something you wrote about it afterwards.
    """
    like = f"%{q.strip()}%" if q.strip() else None
    t_kind = t_value = None
    if tag.strip():
        t_kind, t_value = _parse_tag(tag)
    con = analyses.connect()
    try:
        runs = {r["id"]: dict(r) for r in con.execute(
            "SELECT id, question, label, final_answer, started_at FROM runs")}
        notes = [dict(n) for n in con.execute(
            "SELECT id, title, body, run_id, created_at FROM notes "
            "WHERE run_id IS NOT NULL ORDER BY id DESC")]
        run_tags = _tags_for(con, "run", list(runs))
        note_tags = _tags_for(con, "note", [n["id"] for n in notes])
        for n in notes:
            n["tags"] = note_tags.get(n["id"], [])

        by_run: dict[int, list] = {}
        for n in notes:
            by_run.setdefault(n["run_id"], []).append(n)

        def matches(rid: int) -> bool:
            r = runs[rid]
            mine = by_run.get(rid, [])
            tags = set(run_tags.get(rid, [])) | {t for n in mine for t in n["tags"]}
            if t_kind and f"{t_kind}:{t_value}" not in tags:
                return False
            if not like:
                return True
            hay = " ".join([r["question"] or "", r["final_answer"] or "",
                            *[f"{n['title']} {n['body']}" for n in mine]]).lower()
            return q.strip().lower() in hay

        out = []
        for rid in sorted(runs, reverse=True):
            if not matches(rid):
                continue
            r = runs[rid]
            out.append({"id": rid, "question": r["question"], "label": r["label"],
                        "started_at": r["started_at"],
                        "snippet": (r["final_answer"] or "")[:260],
                        "tags": run_tags.get(rid, []),
                        "notes": by_run.get(rid, [])})
            if len(out) >= limit:
                break
        return {"query": q, "tag": tag, "analyses": out,
                "total": len(out),
                "note_count": sum(len(a["notes"]) for a in out)}
    finally:
        con.close()


# =============================================================================
# The investigate hand-off used to live here (POST /api/investigate).
# -----------------------------------------------------------------------------
# Removed on the understanding that the capability reaches the agent through
# Claude Desktop over MCP. THAT IS NOT TRUE TODAY: there is no MCP server in
# this repo, the `mcp` package is not installed, and the Desktop config has no
# servers registered. Investigation therefore has no working path anywhere.
# The removal itself is still the right shape -- two routes to the agent means
# two framings that can drift -- but the replacement has to be built before
# this is a migration rather than a deletion.
#
# What went with it: the InvestigateIn model, the prompt builder, the
# _run_investigation thread and its tagging. `_run_agent` itself stays -- the
# Ask view still uses it.
# =============================================================================

# =============================================================================
# Model builder -- the correlation explorer grown into something honest
# =============================================================================
# Two investigations reached the same two conclusions by hand: a single slope
# hides curvature, and repeated seasons of one player are not independent
# observations. Both are baked in here rather than left for the agent to
# rediscover a third time -- polynomial terms are a first-class option, and
# cluster-robust standard errors are the default whenever a predictor cannot
# vary within a player.
MODEL_OUTCOMES = {
    k: v for k, v in CORR_VARS.items()
    if k not in ("height_in", "weight_lbs", "draft_number", "jersey")
}
MAX_PREDICTORS = 6


class ModelSpec(BaseModel):
    label: str = ""
    predictors: list[str]
    degree: int = 1               # 1 linear, 2 adds squares, 3 adds cubes


class ModelRequest(BaseModel):
    outcome: str
    specs: list[ModelSpec]
    season_from: int = 2000
    season_to: int = 2025
    min_games: int = 41
    game_type: str = "regular"
    model_type: str = "linear"    # linear | logistic
    logistic_threshold: float | None = None
    # Which spec's residuals to return. The ladder is always fit in full so the
    # AIC comparison is real, but the reader is judging one model at a time.
    diagnostics_for: str | None = None
    cluster_by_player: bool | None = None   # None = decide from the predictors


# =============================================================================
# The model builder's variable layer
# -----------------------------------------------------------------------------
# The builder used to offer the correlation explorer's 17 variables and nothing
# else, which left most of the semantic layer unreachable: per-36 rates, every
# advanced metric, and any notion of a prior season. Three families are added
# here, each resolved in `_model_frame` rather than enumerated by hand:
#
#   per-36      already columns on v_player_season_averages
#   advanced    per-GAME rows in player_advanced_stats, so they are aggregated
#               to player-season as a MINUTES-WEIGHTED mean -- a plain mean over
#               games would let a 4-minute appearance count as much as a start,
#               which for a rate statistic is simply wrong
#   lag_<key>   the same player's value in the PREVIOUS season, and only when
#               that season is literally season-1; a player who missed a year
#               gets NULL rather than a value silently carried across the gap
# =============================================================================
MODEL_PER36 = {
    "pts_per36": "Points per 36 minutes", "reb_per36": "Rebounds per 36 minutes",
    "ast_per36": "Assists per 36 minutes", "stl_per36": "Steals per 36 minutes",
    "blk_per36": "Blocks per 36 minutes", "fg3m_per36": "Threes per 36 minutes",
    "turnover_per36": "Turnovers per 36 minutes",
}

# key -> (column on player_advanced_stats, label, what it means in one line)
MODEL_ADVANCED = {
    "usage_pct":    ("usage_percentage", "Usage rate %",
                     "share of team possessions a player ends while on court"),
    "ts_pct_adv":   ("true_shooting_percentage", "True shooting % (advanced)",
                     "points per shooting possession, counting free throws"),
    "efg_pct_adv":  ("effective_field_goal_percentage", "Effective FG% (advanced)",
                     "field goal % crediting a three as 1.5 makes"),
    "off_rating":   ("offensive_rating", "Offensive rating",
                     "points produced per 100 possessions"),
    # No trailing qualifier clause: the lag wording wraps this string mid-
    # sentence, and "points allowed per 100 possessions - lower is better in
    # the season immediately before" is not a sentence.
    "def_rating":   ("defensive_rating", "Defensive rating",
                     "points allowed per 100 possessions, where lower is better"),
    "net_rating":   ("net_rating", "Net rating",
                     "offensive minus defensive rating per 100 possessions"),
    "pie":          ("pie", "Player impact estimate",
                     "share of the game's total statistical production"),
    "pace":         ("pace", "Pace",
                     "possessions per 48 minutes while on court"),
    "ast_pct":      ("assist_percentage", "Assist %",
                     "share of teammate field goals assisted while on court"),
    "ast_ratio":    ("assist_ratio", "Assist ratio",
                     "assists per 100 possessions used"),
    "ast_to_tov":   ("assist_to_turnover", "Assist-to-turnover",
                     "assists divided by turnovers"),
    "tov_ratio":    ("turnover_ratio", "Turnover ratio",
                     "turnovers per 100 possessions used"),
    "reb_pct":      ("rebound_percentage", "Rebound %",
                     "share of available rebounds collected while on court"),
    "oreb_pct":     ("offensive_rebound_percentage", "Offensive rebound %",
                     "share of available offensive rebounds collected"),
    "dreb_pct":     ("defensive_rebound_percentage", "Defensive rebound %",
                     "share of available defensive rebounds collected"),
}

# One-line meanings for the variables that were already here, so the selection
# UI can explain every option rather than only the new ones.
MODEL_MEANINGS = {
    "ppg": "points scored per game played",
    "rpg": "total rebounds per game played",
    "apg": "assists per game played",
    "spg": "steals per game played",
    "bpg": "blocks per game played",
    "topg": "turnovers per game played",
    "fg3mpg": "three-pointers made per game played",
    "mpg": "minutes per game played",
    "fg_pct": "field goals made divided by attempted",
    "fg3_pct": "three-pointers made divided by attempted",
    "ft_pct": "free throws made divided by attempted",
    "ts_pct": "points per shooting possession, counting free throws",
    "games": "games played in the season",
    "height_in": "listed height, fixed for a player",
    "weight_lbs": "listed weight, fixed for a player",
    "draft_number": "overall pick number, fixed for a player",
    "jersey": "shirt number — included precisely because it should predict nothing",
}

MODEL_GROUPS = {"basic": "Per game", "shooting": "Shooting",
                "per36": "Per 36 minutes", "advanced": "Advanced",
                "physical": "Fixed attributes", "prior": "Prior season"}
_GROUP_OF = {
    **{k: "basic" for k in ("ppg", "rpg", "apg", "spg", "bpg", "topg",
                            "fg3mpg", "mpg", "games")},
    **{k: "shooting" for k in ("fg_pct", "fg3_pct", "ft_pct", "ts_pct")},
    **{k: "per36" for k in MODEL_PER36},
    **{k: "advanced" for k in MODEL_ADVANCED},
    **{k: "physical" for k in ("height_in", "weight_lbs", "draft_number", "jersey")},
}

LAG_PREFIX = "lag_"
# Lagging a fixed attribute is meaningless -- last season's height is this
# season's height -- so those are not offered with the prefix.
LAGGABLE = {k for k in {**MODEL_MEANINGS, **MODEL_PER36,
                        **{k: "" for k in MODEL_ADVANCED}}
            if k not in ("height_in", "weight_lbs", "draft_number", "jersey")}


def model_var(key: str) -> dict | None:
    """Resolve any model-builder variable key, lagged or not."""
    lagged = key.startswith(LAG_PREFIX)
    base = key[len(LAG_PREFIX):] if lagged else key
    if lagged and base not in LAGGABLE:
        return None
    if base in MODEL_ADVANCED:
        col, label, mean = MODEL_ADVANCED[base]
        info = {"base": base, "label": label, "meaning": mean,
                "source": "advanced", "expr": f'adv."{base}"'}
    elif base in MODEL_PER36:
        info = {"base": base, "label": MODEL_PER36[base],
                "meaning": f"{MODEL_PER36[base].split(' per ')[0].lower()} "
                           "scaled to a 36-minute game",
                "source": "season", "expr": f'a."{base}"'}
    elif base in CORR_VARS:
        info = {"base": base, "label": CORR_VARS[base]["label"],
                "meaning": MODEL_MEANINGS.get(base, ""),
                "source": "season", "expr": CORR_VARS[base]["expr"]}
    else:
        return None
    info["key"] = key
    info["lagged"] = lagged
    info["group"] = "prior" if lagged else _GROUP_OF.get(base, "basic")
    if lagged:
        info["label"] = f"Prior season: {info['label'].lower()}"
        info["meaning"] = (f"the same player's {info['meaning']} in the season "
                           "immediately before — blank if they missed that season")
    return info


def all_model_vars() -> list[dict]:
    keys = [*CORR_VARS, *MODEL_PER36, *MODEL_ADVANCED]
    out = [model_var(k) for k in keys]
    out += [model_var(LAG_PREFIX + k) for k in keys if k in LAGGABLE]
    return [v for v in out if v]


def _model_frame(outcome: str, predictors: list[str], season_from: int,
                 season_to: int, min_games: int, gt: str):
    """Build the player-season frame for whatever mix of variables was asked for.

    Three complications are handled here so callers never see them:
      * advanced metrics live per game and are aggregated minutes-weighted;
      * a lagged variable needs the season BEFORE the window, so the inner
        query widens by one season and the outer one trims back;
      * a lag across a missed season is NULL, not the last value seen.
    """
    wanted = {outcome, *predictors}
    resolved = {}
    for key in sorted(wanted):
        info = model_var(key)
        if info is None:
            raise HTTPException(400, f"unknown variable {key!r}")
        resolved[key] = info

    need_adv = any(i["source"] == "advanced" for i in resolved.values())
    lagged = {k: i for k, i in resolved.items() if i["lagged"]}
    plain = {k: i for k, i in resolved.items() if not i["lagged"]}

    # Every distinct base column the inner query must carry.
    base_exprs = {}
    for i in resolved.values():
        base_exprs[i["base"]] = i["expr"]
    inner_sel = ", ".join(f'{e} AS "{b}"' for b, e in sorted(base_exprs.items()))

    adv_cte = ""
    if need_adv:
        adv_cols = ", ".join(
            f'SUM(s."{col}" * g.minutes_played) '
            f'/ NULLIF(SUM(CASE WHEN s."{col}" IS NULL THEN 0 '
            f'ELSE g.minutes_played END), 0) AS "{key}"'
            for key, (col, _l, _m) in MODEL_ADVANCED.items())
        # Minutes-weighted, and the denominator only counts minutes from games
        # where the metric is actually present -- otherwise a season with a few
        # missing advanced rows is quietly shrunk toward zero.
        adv_cte = f"""adv_src AS (
             SELECT g.player_id, g.season, {adv_cols}
             FROM v_player_games g
             JOIN player_advanced_stats s
               ON s.player_id = g.player_id AND s.game_id = g.game_id
             WHERE g.game_type = ? AND NOT g.is_dnp
               AND g.season BETWEEN ? AND ?
             GROUP BY g.player_id, g.season
           ),"""

    # Lags need one season of run-up before the requested window.
    inner_from = season_from - 1 if lagged else season_from
    args: list = []
    if need_adv:
        args += [gt, inner_from, season_to]
    args += [gt, inner_from, season_to, min_games]

    adv_join = ("LEFT JOIN adv_src adv ON adv.player_id = a.player_id "
                "AND adv.season = a.season") if need_adv else ""

    outer_sel, where_parts = [], []
    for key, i in plain.items():
        outer_sel.append(f'f."{i["base"]}" AS "{key}"')
        where_parts.append(f'f."{i["base"]}" IS NOT NULL')
    for key, i in lagged.items():
        # Only a genuinely consecutive season counts as "prior season".
        outer_sel.append(
            f'CASE WHEN LAG(f.season) OVER w = f.season - 1 '
            f'THEN LAG(f."{i["base"]}") OVER w END AS "{key}"')

    sql = f"""WITH {adv_cte}
           f AS (
             SELECT a.player_id, a.player_name, a.season, {inner_sel}
             FROM v_player_season_averages a
             JOIN v_players p USING (player_id)
             {adv_join}
             WHERE a.game_type = ? AND a.season BETWEEN ? AND ?
               AND a.games_played >= ?
           ),
           w AS (
             SELECT f.player_id, f.player_name, f.season, {", ".join(outer_sel)}
             FROM f
             WINDOW w AS (PARTITION BY f.player_id ORDER BY f.season)
           )
           SELECT * FROM w
           WHERE season BETWEEN ? AND ?
             AND {" AND ".join(f'"{k}" IS NOT NULL' for k in sorted(resolved))}
           ORDER BY player_id, season"""
    args += [season_from, season_to]
    return _rows(sql, args)


@app.get("/api/model/variables")
def model_variables() -> dict:
    return {
        "predictors": [{**v, "player_constant": v["base"] in PLAYER_CONSTANT}
                       for v in all_model_vars()],
        "groups": [{"key": k, "label": lab} for k, lab in MODEL_GROUPS.items()],
        "outcomes": [{"key": k, "label": v["label"],
                      "meaning": MODEL_MEANINGS.get(k, "")}
                     for k, v in MODEL_OUTCOMES.items()],
    }


@app.post("/api/model/fit")
def model_fit(req: ModelRequest) -> dict:
    """Fit one or more specifications on the same sample and compare them.

    Every spec is fit on the identical rows, so R² and AIC are comparable --
    fitting each on its own complete-cases subset would make the comparison
    meaningless in exactly the way that is hardest to notice.
    """
    import numpy as np
    import pandas as pd
    import statsmodels.api as sm

    gt = _game_type(req.game_type)
    if req.outcome not in MODEL_OUTCOMES:
        raise HTTPException(400, f"outcome must be one of {sorted(MODEL_OUTCOMES)}")
    if not req.specs:
        raise HTTPException(400, "give at least one specification")
    every = sorted({p for s in req.specs for p in s.predictors})
    for p in every:
        if model_var(p) is None:
            raise HTTPException(400, f"unknown predictor {p!r}")
    if len(every) > MAX_PREDICTORS:
        raise HTTPException(400, f"at most {MAX_PREDICTORS} distinct predictors")
    for s in req.specs:
        if not 1 <= s.degree <= 3:
            raise HTTPException(400, "degree must be 1, 2 or 3")

    rows = _model_frame(req.outcome, every, req.season_from, req.season_to,
                        req.min_games, gt)
    if len(rows) < 30:
        raise HTTPException(400, f"only {len(rows)} rows match; widen the filters")
    df = pd.DataFrame(rows)
    n_players = df["player_id"].nunique()

    # Cluster whenever any predictor is constant within a player, which is when
    # repeated seasons stop carrying independent information.
    cluster = (req.cluster_by_player
               if req.cluster_by_player is not None
               else bool(set(every) & PLAYER_CONSTANT))

    y_raw = df[req.outcome].astype(float)
    logistic = req.model_type == "logistic"
    if logistic:
        thr = (req.logistic_threshold if req.logistic_threshold is not None
               else float(y_raw.median()))
        y = (y_raw > thr).astype(float)
        if y.nunique() < 2:
            raise HTTPException(400, "the threshold puts every row in one class")
    else:
        thr, y = None, y_raw

    results, diagnostics = [], {}
    for idx, spec in enumerate(req.specs):
        terms, X = [], pd.DataFrame(index=df.index)
        for p in spec.predictors:
            base = df[p].astype(float)
            for d in range(1, spec.degree + 1):
                name = p if d == 1 else f"{p}^{d}"
                X[name] = base ** d
                terms.append(name)
        Xc = sm.add_constant(X, has_constant="add")
        try:
            if logistic:
                # Logit takes cov_type in fit(); get_robustcov_results is an OLS
                # method and raised AttributeError here, so the logistic path
                # never worked at all.
                fit = (sm.Logit(y, Xc).fit(disp=0, cov_type="cluster",
                                           cov_kwds={"groups": df["player_id"]})
                       if cluster else sm.Logit(y, Xc).fit(disp=0, cov_type="HC1"))
                cov = fit
            else:
                ols = sm.OLS(y, Xc)
                fit = (ols.fit(cov_type="cluster",
                               cov_kwds={"groups": df["player_id"]})
                       if cluster else ols.fit(cov_type="HC3"))
                cov = fit
        except Exception as exc:
            results.append({"label": spec.label or f"spec {idx + 1}",
                            "error": f"{type(exc).__name__}: {exc}"})
            continue

        ci = cov.conf_int()
        ci = ci.values if hasattr(ci, "values") else np.asarray(ci)
        coefs = []
        for i, name in enumerate(Xc.columns):
            coefs.append({"term": name,
                          "estimate": float(np.asarray(cov.params)[i]),
                          "ci_low": float(ci[i][0]), "ci_high": float(ci[i][1]),
                          "p": float(np.asarray(cov.pvalues)[i])})
        # The highest-order term is what the extra complexity buys. AIC alone
        # picked the cubic on height->blocks even though none of its three terms
        # was distinguishable from zero (all p > 0.16) -- powers of one variable
        # are collinear, so the joint fit improves while the coefficients become
        # unidentifiable. Reporting this term's p-value beside AIC is what lets
        # a reader see that.
        top = [c for c in coefs if c["term"] != "const"]
        highest = top[-1] if top else None
        out = {"label": spec.label or f"spec {idx + 1}",
               "predictors": spec.predictors, "degree": spec.degree,
               "terms": terms, "coefficients": coefs, "n": int(len(df)),
               "aic": float(fit.aic), "bic": float(fit.bic),
               "highest_term": highest["term"] if highest else None,
               "highest_term_p": highest["p"] if highest else None,
               "all_terms_significant": all(c["p"] < 0.05 for c in top) if top else False}
        if logistic:
            out.update({"pseudo_r2": float(fit.prsquared),
                        "r_squared": float(fit.prsquared), "adj_r_squared": None})
        else:
            out.update({"r_squared": float(fit.rsquared),
                        "adj_r_squared": float(fit.rsquared_adj)})
        results.append(out)

        # Residuals for the spec the caller is featuring, falling back to the
        # last one. Always returning the last spec's residuals meant selecting
        # "linear" showed the cubic model's diagnostics.
        want = (req.diagnostics_for or "").strip()
        is_target = (out["label"] == want if want
                     else idx == len(req.specs) - 1)
        if is_target and not logistic:
            fitted = np.asarray(fit.fittedvalues, dtype=float)
            resid = np.asarray(fit.resid, dtype=float)
            step = max(1, len(fitted) // 1200)
            actual = np.asarray(y, dtype=float)
            diagnostics = {
                "points": [{"fitted": float(f), "resid": float(r),
                            "actual": float(a)}
                           for f, r, a in zip(fitted[::step], resid[::step],
                                              actual[::step])],
                "fit_range": [float(min(fitted.min(), actual.min())),
                              float(max(fitted.max(), actual.max()))],
                "point_step": step,
                "quintiles": [],
            }
            order = np.argsort(fitted)
            for q, chunk in enumerate(np.array_split(order, 5)):
                diagnostics["quintiles"].append({
                    "quintile": q + 1,
                    "mean_resid": float(resid[chunk].mean()),
                    "sd_resid": float(resid[chunk].std()),
                    "mean_fitted": float(fitted[chunk].mean())})
            sds = [q["sd_resid"] for q in diagnostics["quintiles"]]
            means = [q["mean_resid"] for q in diagnostics["quintiles"]]
            overall_sd = float(resid.std()) or 1.0
            diagnostics["fanning"] = bool(sds[-1] > 1.5 * sds[0])
            diagnostics["fanning_ratio"] = round(sds[-1] / sds[0], 2) if sds[0] else None
            # A shape is only curvature if it is big enough to matter. Testing the
            # sign pattern alone flagged residual means spanning 0.03 on a fit whose
            # residual SD was 0.30 -- a "defect" one tenth the size of the noise.
            shaped = ((means[0] > means[2] < means[-1])
                      or (means[0] < means[2] > means[-1]))
            swing = max(means) - min(means)
            diagnostics["curvature"] = bool(shaped and swing > 0.25 * overall_sd)
            diagnostics["curvature_swing_sd"] = round(swing / overall_sd, 2)

    # "Best" only means something when there is something to be better than.
    # Fitting a one-model ladder and badging its only row as best AIC read as
    # "every model is best", which is how this surfaced as a bug.
    best = None
    scored = [r for r in results if "aic" in r]
    if len(scored) > 1:
        best = min(scored, key=lambda r: r["aic"])["label"]
    # The simplest spec that actually earns its last term. This is the one to
    # report when AIC and identifiability disagree, which they do whenever the
    # ladder adds powers of the same variable.
    earned = [r for r in scored if r.get("highest_term_p") is not None
              and r["highest_term_p"] < 0.05]
    parsimonious = (min(earned, key=lambda r: r["degree"])["label"]
                    if earned else None)
    # A richer spec only counts if every one of ITS terms is identifiable.
    justified = [r for r in scored if r.get("all_terms_significant")]
    if justified:
        parsimonious = max(justified, key=lambda r: r["degree"])["label"]
    return {
        "outcome": req.outcome, "outcome_label": MODEL_OUTCOMES[req.outcome]["label"],
        "model_type": req.model_type, "logistic_threshold": thr,
        "season_from": req.season_from, "season_to": req.season_to,
        "game_type": gt, "min_games": req.min_games,
        "n": int(len(df)), "n_players": int(n_players),
        "clustered": cluster,
        "cluster_note": (
            "Standard errors clustered on player: at least one predictor cannot "
            f"vary within a player, so these {len(df)} rows carry only "
            f"{n_players} independent values."
            if cluster else
            "Heteroscedasticity-robust (HC3) standard errors; no predictor is "
            "player-constant, so rows are treated as independent."),
        "results": results, "best_by_aic": best,
        "parsimonious": parsimonious,
        "aic_caveat": (
            "AIC is computed from a likelihood that treats all "
            f"{len(df)} rows as independent, but these are {n_players} players "
            "observed repeatedly — so it under-penalises extra terms here. Where "
            "AIC and the highest-term p-value disagree, prefer the simpler model."
            if cluster else None),
        "diagnostics": diagnostics,
        "exploratory": True,
        "warning": ("Fitting several specifications and keeping the "
                    "best one is a form of searching — the comparison below tells "
                    "you which fits this sample, not which is true. There is "
                    "currently no automated path to test it on a held-out "
                    "era; the investigate hand-off was removed and its MCP "
                    "replacement is not built."),
    }


# =============================================================================
# Fantasy — surfacing the draft board that already exists as a CSV
# =============================================================================
# =============================================================================
# Validated findings -- investigations that reached a verdict
# =============================================================================
_VERDICT_RE = re.compile(r"\b(NOT CONFIRMED|CONFIRMED|INCONCLUSIVE)\b")


def _finding_facets(answer: str) -> dict:
    """Pull the rigour out of a stored investigation without paraphrasing it.

    Everything here is located, not summarised: the verdict token, the held-out
    era line, and the confound headings the methodology skill asks for. The
    page then shows the investigation's own words. A generated summary of a
    statistical argument is exactly the artefact this project refuses to
    produce elsewhere, and it would be worse here, where the point is that the
    reasoning is inspectable.
    """
    text = answer or ""
    m = _VERDICT_RE.search(text.upper())
    verdict = m.group(1) if m else None
    eras = re.findall(r"(?:held[- ]out|holdout)[^.\n]{0,80}?((?:1|2)\d{3})"
                      r"\s*[-\u2013]\s*((?:1|2)\d{3})", text, re.I)
    discovery = re.findall(r"discovery[^.\n]{0,80}?((?:1|2)\d{3})"
                           r"\s*[-\u2013]\s*((?:1|2)\d{3})", text, re.I)
    # Only the one shape the methodology skill actually emits:
    #   - *Shared era trend* - league blocks and mean height both drift...
    # A looser pattern pulled in table rows and section headings ("CAVEATS",
    # "Model 1", "R2 = 0.276"), and a page whose whole claim is that the
    # reasoning is inspectable cannot afford invented structure. Where an
    # investigation did not use the convention, this is empty and the page
    # shows the argument itself instead.
    confounds = re.findall(r"^\s*[-*]\s+\*([A-Z][^*\n]{2,60}?)\*\s*[\u2014-]",
                           text, re.M)
    return {
        "verdict": verdict,
        "held_out_era": (f"{eras[0][0]}\u2013{eras[0][1]}" if eras else None),
        "discovery_era": (f"{discovery[0][0]}\u2013{discovery[0][1]}"
                          if discovery else None),
        "confounds_examined": [c.strip() for c in confounds][:8],
    }


def _finding_claim(question: str) -> str:
    """A headline for the card.

    An investigation launched from the explorer stores the WHOLE agent prompt
    as its question -- a thousand characters ending in instructions about
    nominating a held-out era -- so using it as a title puts the methodology
    boilerplate where the claim should be. The relationship and era are pulled
    out when the prompt has the explorer's shape; anything else falls back to
    its first sentence.
    """
    q = (question or "").strip()
    m = re.search(r"relationship between (.+?) and (.+?), at player-season "
                  r"grain, seasons (\d{4})\s*[-\u2013]\s*(\d{4})", q)
    if m:
        return f"{m.group(1)} vs {m.group(2)}, {m.group(3)}\u2013{m.group(4)}"
    first = re.split(r"(?<=[.?])\s", q)[0] if q else ""
    return first if len(first) <= 160 else first[:157].rstrip() + "\u2026"


@app.get("/api/findings")
def findings() -> dict:
    """Investigations that reached an explicit verdict, with their evidence.

    A finding only appears here if its answer actually states one of the three
    verdicts. An investigation that ran but never committed is not a finding,
    and padding this page with them would make a rejection look like a result.
    """
    con = analyses.connect()
    try:
        rows = con.execute(
            """SELECT id, label, question, methodology, final_answer,
                      started_at, snapshot_sha256, turns, cost_usd
               FROM runs ORDER BY id DESC""").fetchall()
        cols = ("id", "label", "question", "methodology", "final_answer",
                "started_at", "snapshot_sha256", "turns", "cost_usd")
        steps = {r[0]: r[1] for r in con.execute(
            "SELECT run_id, count(*) FROM tool_calls GROUP BY run_id").fetchall()}
        unstable = {r[0]: r[1] for r in con.execute(
            """SELECT run_id, count(*) FROM tool_calls
               WHERE stability IS NOT NULL AND stability <> 'same'
               GROUP BY run_id""").fetchall()}
    finally:
        con.close()

    out = []
    for raw in rows:
        r = dict(zip(cols, raw))
        facets = _finding_facets(r["final_answer"])
        if not facets["verdict"]:
            continue
        out.append({
            "run_id": r["id"], "label": r["label"], "question": r["question"],
            "claim": _finding_claim(r["question"]),
            "methodology": r["methodology"], "answer": r["final_answer"],
            "created": r["started_at"], "turns": r["turns"],
            "cost_usd": r["cost_usd"],
            "snapshot_sha256": (r["snapshot_sha256"] or "")[:16],
            "steps": steps.get(r["id"], 0),
            "unstable_steps": unstable.get(r["id"], 0),
            "from_explorer": "exploratory scan" in (r["question"] or "").lower(),
            **facets,
        })
    counts = {v: sum(1 for f in out if f["verdict"] == v)
              for v in ("CONFIRMED", "NOT CONFIRMED", "INCONCLUSIVE")}
    return {
        "findings": out, "counts": counts, "total": len(out),
        "note": ("Every finding here is an agent investigation that declared a "
                 "held-out era before estimating and then committed to a "
                 "verdict. The rejections matter as much as the confirmations: "
                 "a tool that scans variable pairs manufactures chance patterns, "
                 "and one that never rejects anything is not testing."),
    }


# =============================================================================
# Player comparison and trade analysis (design spec items 14 and 15)
# =============================================================================
@app.get("/api/compare")
def compare_players(ids: str, season_from: int | None = None,
                    season_to: int | None = None,
                    game_type: str = "regular") -> dict:
    """Two or more players side by side: career, peak, and current value.

    Built on the explorer's existing player queries rather than a new
    aggregation, so a number here cannot disagree with the same number on a
    player's own page.
    """
    gt = _game_type(game_type)
    try:
        pids = [int(x) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(400, "ids must be comma-separated integers")
    if not 2 <= len(pids) <= 4:
        raise HTTPException(400, "compare between 2 and 4 players")
    where = "AND season BETWEEN ? AND ?" if season_from and season_to else ""
    out = []
    for pid in pids:
        args: list = [pid, gt]
        if where:
            args += [season_from, season_to]
        career = _rows(f"""
            SELECT any_value(player_name) AS player_name,
                   count(DISTINCT season) AS seasons,
                   min(season) AS first_season, max(season) AS last_season,
                   sum(games_played) AS games, sum(minutes) AS minutes,
                   sum(pts) AS pts, sum(reb) AS reb, sum(ast) AS ast,
                   sum(stl) AS stl, sum(blk) AS blk, sum(fg3m) AS fg3m,
                   sum(turnover) AS turnover,
                   sum(fgm) AS fgm, sum(fga) AS fga,
                   sum(ftm) AS ftm, sum(fta) AS fta
              FROM v_player_season_totals
             WHERE player_id = ? AND game_type = ? {where}""", args)
        if not career or not career[0]["games"]:
            raise HTTPException(404, f"no {gt} data for player {pid}")
        c = career[0]
        g = c["games"] or 1
        per = {k: (c[k] or 0) / g for k in
               ("pts", "reb", "ast", "stl", "blk", "fg3m", "turnover")}
        per["mpg"] = (c["minutes"] or 0) / g
        rates = {
            "fg_pct": (c["fgm"] / c["fga"]) if c["fga"] else None,
            "ft_pct": (c["ftm"] / c["fta"]) if c["fta"] else None,
            "ts_pct": (c["pts"] / (2 * (c["fga"] + 0.44 * c["fta"])))
                      if (c["fga"] or c["fta"]) else None,
        }
        best = _rows("""
            SELECT season, season_label, games_played, mpg, ppg, rpg, apg,
                   ts_pct
              FROM v_player_season_averages
             WHERE player_id = ? AND game_type = ? AND games_played >= 20
             ORDER BY ppg DESC LIMIT 1""", [pid, gt])
        bio = _rows("""SELECT player_id, full_name, position, height_raw,
                              weight_lbs, draft_year, draft_number, college
                       FROM v_players WHERE player_id = ?""", [pid])
        proj = _rows("""SELECT stat, projected_value FROM player_projections
                        WHERE player_id = ? AND season =
                              (SELECT max(season) FROM player_projections)""",
                     [pid])
        out.append({
            "player_id": pid, "bio": bio[0] if bio else {},
            "career": {**c, **{f"{k}_per_game": v for k, v in per.items()},
                       **rates},
            "best_season": best[0] if best else None,
            "projection": {r["stat"]: r["projected_value"] for r in proj},
        })
    # One row per metric so the UI renders a comparison rather than N cards.
    metrics = [
        ("games", "Games", "int"), ("seasons", "Seasons", "int"),
        ("pts_per_game", "Points per game", "num"),
        ("reb_per_game", "Rebounds per game", "num"),
        ("ast_per_game", "Assists per game", "num"),
        ("stl_per_game", "Steals per game", "num"),
        ("blk_per_game", "Blocks per game", "num"),
        ("fg3m_per_game", "Threes per game", "num"),
        ("turnover_per_game", "Turnovers per game", "num"),
        ("mpg_per_game", "Minutes per game", "num"),
        ("fg_pct", "Field goal %", "pct"), ("ft_pct", "Free throw %", "pct"),
        ("ts_pct", "True shooting %", "pct"),
    ]
    rows = []
    for key, label, fmt in metrics:
        vals = [p["career"].get(key) for p in out]
        numeric = [v for v in vals if v is not None]
        # Lower turnovers are better; everything else here is higher-better.
        best_val = (min(numeric) if key.startswith("turnover") else max(numeric)) \
            if numeric else None
        rows.append({"key": key, "label": label, "format": fmt,
                     "values": vals, "best": best_val})
    return {"players": out, "metrics": rows, "game_type": gt,
            "season_from": season_from, "season_to": season_to}


@app.get("/api/fantasy/rosters")
def fantasy_rosters() -> dict:
    """Every fantasy roster, with each player's board value and raw stats.

    Needs league_sync's all-teams cache. Players are returned with both the
    z-scores the draft board ranks on AND the per-game numbers behind them,
    because a trade judged on z-scores alone asks the reader to trust a
    transformation they cannot see.
    """
    import csv
    import matchup as mu
    try:
        league = mu.load_league(mu.latest_cache())
    except SystemExit as exc:
        raise HTTPException(503, str(exc))
    teams = league["raw"].get("teams") or []
    if not teams:
        raise HTTPException(
            503, "this league cache predates all-roster support; "
                 "run `python league_sync.py` to refresh it")

    path = PROJECT_ROOT / DRAFT_WINDOWS["blend"][0]
    board = {}
    if path.exists():
        with path.open(newline="") as fh:
            for r in csv.DictReader(fh):
                try:
                    pid = int(float(r.get("player_id") or 0))
                except (TypeError, ValueError):
                    continue
                if pid:
                    board[pid] = r

    idmap = {}
    import pandas as _pd
    m = _pd.read_csv(PROJECT_ROOT / "espn_bdl_id_map.csv")
    for r in m.itertuples():
        if _pd.notna(r.bdl_player_id):
            idmap[int(r.espn_id)] = int(r.bdl_player_id)

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    def enrich(p, owner, owner_id):
        bid = idmap.get(p.get("player_id"))
        row = board.get(bid) if bid else None
        out = {"espn_id": p.get("player_id"), "player_id": bid,
               "name": p.get("name"), "pro_team": p.get("pro_team"),
               "position": p.get("position"), "slot": p.get("lineup_slot"),
               "injury_status": p.get("injury_status"),
               "fantasy_team": owner, "fantasy_team_id": owner_id,
               "on_board": row is not None}
        if row:
            out["total_value"] = num(row.get("total_value"))
            out["per_game_value"] = num(row.get("per_game_value"))
            out["availability"] = num(row.get("availability"))
            out["stats"] = {a: num(row.get(a)) for a, _z, _l in DRAFT_PAIRS}
            out["z"] = {a: num(row.get(z)) for a, z, _l in DRAFT_PAIRS}
        return out

    players, rosters = [], []
    for t in teams:
        roster = [enrich(p, t["name"], t["team_id"]) for p in t.get("roster", [])]
        players.extend(roster)
        rosters.append({"team_id": t["team_id"], "name": t["name"],
                        "is_me": bool(t.get("is_me")), "roster": roster,
                        "total_value": sum(p.get("total_value") or 0
                                           for p in roster)})
    pro_teams = sorted({p["pro_team"] for p in players if p.get("pro_team")})
    unmatched = [p["name"] for p in players if not p["on_board"]]
    return {"teams": rosters, "players": players,
            "pro_teams": pro_teams,
            "fantasy_teams": [{"team_id": t["team_id"], "name": t["name"],
                               "is_me": bool(t.get("is_me"))} for t in teams],
            "pairs": [{"stat": a, "z": z, "label": l} for a, z, l in DRAFT_PAIRS],
            "unmatched": unmatched,
            "note": ("Values come from the draft board's blended window. "
                     f"{len(unmatched)} rostered player(s) are not on the "
                     "board and cannot be valued."
                     if unmatched else
                     "Values come from the draft board's blended window.")}


@app.get("/api/fantasy/trade-finder")
def fantasy_trade_finder(limit: int = 25, min_gain: float = 0.25) -> dict:
    """Swaps where BOTH teams' starting lineups improve.

    HOW BOTH SIDES CAN GAIN, since a single value function makes a swap
    zero-sum by construction. They gain on STARTING value, not on total
    value. Only `starters_total` players score in a week, so a player buried
    eleventh on one roster contributes nothing there and can contribute a
    great deal somewhere thinner. The surplus is depth, and it is real.

    This deliberately does NOT weight by category need. The draft weights and
    the lineup optimiser have each tested a category-weighted objective
    against a simple one and lost, twice; reintroducing it here as "roster
    need" would be the same idea in a third costume. Net total value is
    reported alongside every suggestion so the zero-sum part stays visible.
    """
    data = fantasy_rosters()
    teams = data["teams"]
    me = next((t for t in teams if t["is_me"]), None)
    if me is None:
        raise HTTPException(503, "no team in this league is marked as yours")

    import matchup as mu
    try:
        rules = mu.load_league(mu.latest_cache())["rules"]
    except SystemExit:
        rules = {}
    n_start = int(rules.get("starters_total") or 10)

    def start_value(roster):
        vals = sorted((p.get("total_value") or 0.0) for p in roster)
        return sum(vals[-n_start:]) if vals else 0.0

    def valued(roster):
        return [p for p in roster if p.get("on_board")]

    import itertools

    my_base = start_value(valued(me["roster"]))
    mine = valued(me["roster"])
    out = []

    def brief(p):
        return {"name": p["name"], "player_id": p["player_id"],
                "position": p.get("position"),
                "total_value": p.get("total_value")}

    for other in teams:
        if other["is_me"]:
            continue
        theirs = valued(other["roster"])
        their_base = start_value(theirs)
        # 1-for-1 and 2-for-1 in both directions. 2-for-1 is where depth
        # trades usually live: two players a bench was already absorbing go
        # out, one who actually starts comes back.
        shapes = ([([a], [b]) for a in mine for b in theirs]
                  + [(list(pair), [b]) for pair in itertools.combinations(mine, 2)
                     for b in theirs]
                  + [([a], list(pair)) for a in mine
                     for pair in itertools.combinations(theirs, 2)])
        for send, get in shapes:
            send_ids = {p["player_id"] for p in send}
            get_ids = {p["player_id"] for p in get}
            mine_after = [p for p in mine if p["player_id"] not in send_ids] + get
            theirs_after = ([p for p in theirs if p["player_id"] not in get_ids]
                            + send)
            my_gain = start_value(mine_after) - my_base
            their_gain = start_value(theirs_after) - their_base
            if my_gain < min_gain or their_gain < min_gain:
                continue
            out.append({
                "their_team": other["name"], "their_team_id": other["team_id"],
                "shape": f"{len(send)}-for-{len(get)}",
                "i_send": [brief(p) for p in send],
                "i_get": [brief(p) for p in get],
                "my_starting_gain": round(my_gain, 3),
                "their_starting_gain": round(their_gain, 3),
                "net_total_value_to_me": round(
                    sum(p.get("total_value") or 0 for p in get)
                    - sum(p.get("total_value") or 0 for p in send), 3),
                "roster_spots_to_me": len(get) - len(send),
            })
    out.sort(key=lambda r: -(r["my_starting_gain"] + r["their_starting_gain"]))
    return {
        "suggestions": out[:limit], "found": len(out),
        "shapes_searched": ["1-for-1", "2-for-1", "1-for-2"],
        "starters_counted": n_start,
        "my_team": me["name"], "my_starting_value": round(my_base, 3),
        "mechanism": (
            f"Both sides gain because only the top {n_start} players score. A "
            "swap that is roughly even on total value can still raise both "
            "starting lineups when each side sends someone their own bench "
            "was already absorbing."),
        "advisory": (
            "Advisory. Values come from the draft board, whose weights were "
            "backtested; this depth argument on top of them was not. "
            "Positional eligibility, injuries, keeper status and whether the "
            "other manager would ever accept are not modelled."),
    }


class SimilarRequest(BaseModel):
    player_id: int
    variables: list[str] = ["ppg", "rpg", "apg", "ts_pct", "mpg"]
    season_from: int = 2015
    season_to: int = 2025
    min_games: int = 41
    game_type: str = "regular"
    n: int = 12


@app.post("/api/model/similar")
def model_similar(req: SimilarRequest) -> dict:
    """Players nearest to one player in a stat space the user chooses.

    Deliberately the SAME geometry as the clustering view: the same
    `_model_frame`, the same per-player aggregation over the window, the same
    z-scoring, and the same SVD with its signs pinned. Nearness here is plain
    Euclidean distance in that standardised space, so "similar" means
    something stateable -- close on the stats you selected, after each is put
    on a common scale -- rather than an opaque score.
    """
    import numpy as np

    gt = _game_type(req.game_type)
    if not 2 <= len(req.variables) <= 8:
        raise HTTPException(400, "choose between 2 and 8 variables")
    for v in req.variables:
        if model_var(v) is None:
            raise HTTPException(400, f"unknown variable {v!r}")

    rows = _model_frame(req.variables[0], req.variables[1:], req.season_from,
                        req.season_to, req.min_games, gt)
    if len(rows) < 20:
        raise HTTPException(400, f"only {len(rows)} rows match; widen the filters")

    agg: dict = {}
    for r in rows:
        a = agg.setdefault(r["player_id"], {"name": r["player_name"], "n": 0,
                                            "sums": [0.0] * len(req.variables)})
        a["n"] += 1
        for i, v in enumerate(req.variables):
            a["sums"][i] += float(r[v])
    ids = sorted(agg)
    if req.player_id not in agg:
        raise HTTPException(
            404, f"player {req.player_id} has no qualifying season in "
                 f"{req.season_from}-{req.season_to} at >= {req.min_games} games")
    X = np.array([[agg[i]["sums"][j] / agg[i]["n"]
                   for j in range(len(req.variables))] for i in ids], dtype=float)
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd

    me = ids.index(req.player_id)
    dist = np.linalg.norm(Z - Z[me], axis=1)
    order = np.argsort(dist)
    Zc = Z - Z.mean(axis=0)
    U, S, Vt = np.linalg.svd(Zc, full_matrices=False)
    coords = U[:, :2] * S[:2]
    var_ratio = (S ** 2) / (S ** 2).sum()
    for axis in range(2):
        if coords[:, axis].sum() < 0:
            coords[:, axis] *= -1
            Vt[axis] *= -1

    labels = {v: model_var(v)["label"] for v in req.variables}
    neighbours = []
    for idx in order[1:req.n + 1]:
        pid = ids[idx]
        neighbours.append({
            "player_id": int(pid), "player_name": agg[pid]["name"],
            "distance": round(float(dist[idx]), 4),
            "seasons": agg[pid]["n"],
            "stats": {v: round(float(X[idx][j]), 4)
                      for j, v in enumerate(req.variables)},
            "z": {v: round(float(Z[idx][j]), 3)
                  for j, v in enumerate(req.variables)},
            "pc1": round(float(coords[idx][0]), 4),
            "pc2": round(float(coords[idx][1]), 4),
        })
    return {
        "target": {"player_id": req.player_id, "player_name": agg[req.player_id]["name"],
                   "seasons": agg[req.player_id]["n"],
                   "stats": {v: round(float(X[me][j]), 4)
                             for j, v in enumerate(req.variables)},
                   "z": {v: round(float(Z[me][j]), 3)
                         for j, v in enumerate(req.variables)},
                   "pc1": round(float(coords[me][0]), 4),
                   "pc2": round(float(coords[me][1]), 4)},
        "neighbours": neighbours,
        "variables": [{"key": v, "label": labels[v]} for v in req.variables],
        "pool": len(ids),
        "pc1_variance": round(float(var_ratio[0]), 4),
        "pc2_variance": round(float(var_ratio[1]), 4),
        "season_from": req.season_from, "season_to": req.season_to,
        "min_games": req.min_games, "game_type": gt,
        "method": ("Euclidean distance in the z-scored space of the chosen "
                   "stats, averaged per player over the window. Same "
                   "standardisation and same sign-pinned SVD as the "
                   "clustering view, so the two agree about who sits where."),
        "warning": ("Similarity is only ever similarity ON THE STATS YOU "
                    "PICKED. Two players close in this space can play "
                    "nothing alike on anything you left out."),
    }


ERA_STATS = [
    ("ppg", "Points per game", "pts"), ("rpg", "Rebounds per game", "reb"),
    ("apg", "Assists per game", "ast"), ("spg", "Steals per game", "stl"),
    ("bpg", "Blocks per game", "blk"), ("topg", "Turnovers per game", "turnover"),
    ("fg3mpg", "Threes per game", "fg3m"), ("mpg", "Minutes per game", "minutes"),
    ("fg_pct", "Field goal %", "fga"), ("fg3_pct", "Three-point %", "fg3a"),
    ("ft_pct", "Free throw %", "fta"), ("ts_pct", "True shooting %", "fga"),
]


@app.get("/api/explore/era-adjusted/{player_id}")
def era_adjusted(player_id: int, game_type: str = "regular",
                 min_games: int = 41) -> dict:
    """A player's seasons as standard deviations from that season's league.

    WHY THIS IS NOT JUST A Z-SCORE. 25 points in 1998 and 25 in 2024 are not
    the same achievement, and a raw career line invites exactly that
    comparison. But a z-score computed against a season where the stat was
    not reliably recorded is worse than the raw number, because it looks
    rigorous. So every stat is checked against `v_stat_first_tracked` for
    EACH season, and where the data does not support the comparison the cell
    says so instead of carrying a figure.
    """
    gt = _game_type(game_type)
    cov = {r["stat"]: r for r in _rows("SELECT * FROM v_stat_first_tracked")}
    # v_stat_first_tracked enumerates sixteen raw columns and minutes is not
    # one of them, so every minutes-per-game cell was being refused in EVERY
    # season, modern ones included. Deriving it from the data on the same
    # rule the catalog uses (first season present at all; first season at
    # >=95% of played rows) is better than inventing a boundary and better
    # than blocking a stat that is in fact well covered.
    if "minutes" not in cov:
        # COUNTED ON VALUE, NOT ON NULL. minutes_played is non-null in 100% of
        # played rows in every season back to 1946, but it is ZERO for most
        # players before about 1970 -- the league average is 0.00 in 1950 and
        # 7.1 in 1961 against 23 in 1970. A null-count test sees full coverage
        # and scores a star's season at four minutes a game. Same trap as the
        # is_dnp bug: present but not recorded.
        got = _rows("""
            SELECT season, count(*) AS played,
                   sum(CASE WHEN minutes_played > 0 THEN 1 ELSE 0 END) AS have
              FROM v_player_games
             WHERE game_type = 'regular' AND NOT is_dnp
             GROUP BY season ORDER BY season""")
        tracked = next((r["season"] for r in got if (r["have"] or 0) > 0), None)
        # "Reliable from here on", not "reliable this once": later seasons sit
        # at 85-93%, so a one-season threshold would mark 1971 partial again.
        # 95%, matching the threshold CoverageTool uses for every other stat.
        # This was 0.80 when it was the only place deriving minutes coverage;
        # now that the shared tool derives it too, two different answers for
        # the same question is worse than either answer.
        full = None
        for i, r in enumerate(got):
            if all((g["have"] or 0) / g["played"] >= 0.95
                   for g in got[i:] if g["played"]):
                full = r["season"]
                break
        cov["minutes"] = {"stat": "minutes", "first_tracked_season": tracked,
                          "first_full_season": full}
    seasons = _rows("""
        SELECT season, season_label, team_id, games_played, mpg, ppg, rpg, apg,
               spg, bpg, topg, fg3mpg, fg_pct, fg3_pct, ft_pct, ts_pct
          FROM v_player_season_averages
         WHERE player_id = ? AND game_type = ?
         ORDER BY season""", [player_id, gt])
    if not seasons:
        raise HTTPException(404, f"no {gt} seasons for player {player_id}")

    # League baseline per season, over players who actually played enough to
    # be a meaningful comparison group. The threshold is stated, not implied.
    span = (seasons[0]["season"], seasons[-1]["season"])
    league = {r["season"]: r for r in _rows(f"""
        SELECT season,
               {", ".join(f"avg({k}) AS mu_{k}, stddev_pop({k}) AS sd_{k}"
                          for k, _l, _c in ERA_STATS)},
               count(*) AS n
          FROM v_player_season_averages
         WHERE game_type = ? AND season BETWEEN ? AND ?
           AND games_played >= ?
         GROUP BY season""", [gt, span[0], span[1], min_games])}

    out = []
    for s in seasons:
        lg = league.get(s["season"])
        row = {"season": s["season"], "season_label": s["season_label"],
               "team_id": s["team_id"], "games_played": s["games_played"],
               "comparison_n": (lg or {}).get("n"), "stats": {}}
        for key, label, raw in ERA_STATS:
            c = cov.get(raw) or {}
            first_full = c.get("first_full_season")
            tracked = c.get("first_tracked_season")
            cell = {"label": label, "value": s.get(key)}
            if tracked is None or s["season"] < tracked:
                cell["status"] = "not tracked"
                cell["note"] = (f"{raw} was not recorded before "
                                f"{tracked}" if tracked else f"{raw} has no "
                                "coverage record")
            elif first_full is not None and s["season"] < first_full:
                # Tracked but incomplete: a z-score here compares against a
                # league average computed from partial data.
                cell["status"] = "partial"
                cell["note"] = (f"{raw} was tracked from {tracked} but not "
                                f"complete until {first_full}; a league "
                                "baseline from this season is unreliable")
            elif not lg or lg.get(f"sd_{key}") in (None, 0) or s.get(key) is None:
                cell["status"] = "no baseline"
            else:
                z = (s[key] - lg[f"mu_{key}"]) / lg[f"sd_{key}"]
                cell["status"] = "ok"
                cell["league_mean"] = round(lg[f"mu_{key}"], 4)
                cell["z"] = round(z, 3)
            row["stats"][key] = cell
        out.append(row)

    usable = sum(1 for r in out for c in r["stats"].values()
                 if c["status"] == "ok")
    blocked = sorted({c["note"] for r in out for c in r["stats"].values()
                      if c.get("note")})
    return {
        "player_id": player_id, "game_type": gt, "min_games": min_games,
        "seasons": out,
        "columns": [{"key": k, "label": l} for k, l, _c in ERA_STATS],
        "usable_cells": usable,
        "coverage_notes": blocked,
        "method": (f"Each figure is standard deviations from the mean of all "
                   f"players with at least {min_games} games in THAT season. "
                   "A cell is left unscored where the stat was not tracked, "
                   "or not yet complete, in that season."),
    }


@app.get("/api/explore/evolution")
def league_evolution(game_type: str = "regular") -> dict:
    """League-wide trends across the full 1946-2025 range.

    Every series is cut at the season its underlying stat became reliable,
    from v_stat_first_tracked, rather than drawn back to 1946 with a flat
    line where the data simply is not there. A three-point-rate chart that
    runs to 1950 is not a finding about the 1950s.
    """
    gt = _game_type(game_type)
    cov = {r["stat"]: r for r in _rows("SELECT * FROM v_stat_first_tracked")}

    rows = _rows("""
        SELECT g.season, any_value(g.season_label) AS season_label,
               count(DISTINCT g.game_id) AS games,
               sum(p.pts) AS pts, sum(p.fga) AS fga, sum(p.fgm) AS fgm,
               sum(p.fg3a) AS fg3a, sum(p.fg3m) AS fg3m,
               sum(p.fta) AS fta, sum(p.ftm) AS ftm,
               sum(p.reb) AS reb, sum(p.oreb) AS oreb, sum(p.ast) AS ast,
               sum(p.stl) AS stl, sum(p.blk) AS blk, sum(p.turnover) AS tov
          FROM v_player_games p JOIN v_games g USING (game_id)
         WHERE p.game_type = ? AND NOT p.is_dnp
         GROUP BY g.season ORDER BY g.season""", [gt])

    def ok(season, stat):
        c = cov.get(stat) or {}
        full = c.get("first_full_season")
        return full is not None and season >= full

    series = []
    for r in rows:
        g = r["games"] or 0
        if not g:
            continue
        # Per TEAM-game: two teams per game, so league totals halve.
        tg = g * 2
        pt = {"season": r["season"], "season_label": r["season_label"],
              "games": g}
        if ok(r["season"], "pts"):
            pt["pts_per_team_game"] = round((r["pts"] or 0) / tg, 2)
        if ok(r["season"], "fga"):
            pt["fga_per_team_game"] = round((r["fga"] or 0) / tg, 2)
            if r["fga"]:
                pt["fg_pct"] = round((r["fgm"] or 0) / r["fga"], 4)
        if ok(r["season"], "fg3a") and r["fga"]:
            # 3-point attempt rate: the share of shots taken from three. The
            # quantity analysis #57 found rising.
            pt["three_point_rate"] = round((r["fg3a"] or 0) / r["fga"], 4)
            pt["fg3a_per_team_game"] = round((r["fg3a"] or 0) / tg, 2)
            if r["fg3a"]:
                pt["fg3_pct"] = round((r["fg3m"] or 0) / r["fg3a"], 4)
        if ok(r["season"], "fta") and r["fga"]:
            pt["ft_rate"] = round((r["fta"] or 0) / r["fga"], 4)
        if ok(r["season"], "ast"):
            pt["ast_per_team_game"] = round((r["ast"] or 0) / tg, 2)
        if ok(r["season"], "reb"):
            pt["reb_per_team_game"] = round((r["reb"] or 0) / tg, 2)
        if ok(r["season"], "turnover") and ok(r["season"], "fga"):
            # Possession estimate, the standard approximation. Needs FGA, FTA,
            # OREB and TOV all to be reliable, so it starts late.
            if ok(r["season"], "oreb") and ok(r["season"], "fta"):
                poss = ((r["fga"] or 0) + 0.44 * (r["fta"] or 0)
                        - (r["oreb"] or 0) + (r["tov"] or 0))
                pt["pace"] = round(poss / tg, 2)
        if ok(r["season"], "stl"):
            pt["stl_per_team_game"] = round((r["stl"] or 0) / tg, 2)
        if ok(r["season"], "blk"):
            pt["blk_per_team_game"] = round((r["blk"] or 0) / tg, 2)
        series.append(pt)

    metrics = [
        ("pts_per_team_game", "Points per team-game", "pts"),
        ("pace", "Possessions per team-game (estimated)", "turnover"),
        ("three_point_rate", "Share of shots from three", "fg3a"),
        ("fg3_pct", "Three-point accuracy", "fg3a"),
        ("fg3a_per_team_game", "Three-point attempts per team-game", "fg3a"),
        ("fg_pct", "Field goal accuracy", "fga"),
        ("ft_rate", "Free throws per field goal attempt", "fta"),
        ("ast_per_team_game", "Assists per team-game", "ast"),
        ("reb_per_team_game", "Rebounds per team-game", "reb"),
        ("stl_per_team_game", "Steals per team-game", "stl"),
        ("blk_per_team_game", "Blocks per team-game", "blk"),
    ]
    out_metrics = []
    for key, label, stat in metrics:
        present = [p["season"] for p in series if key in p]
        if not present:
            continue
        c = cov.get(stat) or {}
        out_metrics.append({
            "key": key, "label": label, "from_season": min(present),
            "to_season": max(present), "n": len(present),
            "coverage_note": (
                f"{stat} is reliable from {c.get('first_full_season')}, so "
                f"this series starts at {min(present)} rather than 1946."
                if c.get("first_full_season") else None)})

    return {
        "game_type": gt, "series": series, "metrics": out_metrics,
        "span": [series[0]["season"], series[-1]["season"]] if series else None,
        "finding_57": {
            "title": "Three-point accuracy predicts winning more than volume",
            "body": ("Analysis #57 found the three-point attempt rate rising "
                     "steeply across the modern era while accuracy barely "
                     "moved — and that of the two, accuracy carried more of "
                     "the relationship with winning percentage than volume "
                     "did. Both series are charted here, on the same seasons, "
                     "so the divergence is visible rather than asserted."),
            "metrics": ["three_point_rate", "fg3_pct"]},
        "method": ("League totals per season over played rows only, divided "
                   "by two per game to give a per-team-game rate. Each series "
                   "starts at the season its underlying stat became reliable."),
    }


@app.get("/api/fantasy/recap")
def fantasy_recap(season: int | None = None) -> dict:
    """The just-concluded season: lineup cost, missed wire, final standing."""
    import matchup as mu
    with _matchup_lock:
        con = _ex()
        try:
            league = mu.load_league(mu.latest_cache())
        except SystemExit as exc:
            raise HTTPException(503, str(exc))
        proj = mu.projections(con)
        return {
            "season_label": league["season_label"],
            "retrospective": mu.retrospective(con, league, proj, season),
            "waivers": mu.missed_waivers(con, league, season),
            "outlook": mu.season_outlook(
                league, strength_from=mu.roster_strength(con, league, proj)),
        }


@app.get("/api/fantasy/keepers")
def fantasy_keepers() -> dict:
    """Each rostered player's board value against what keeping them costs.

    THE RULE IS READ, NOT ASSUMED. ESPN reports keeper_count and
    keeper_order_type. In this league the order type is MANUAL, which means
    the commissioner assigns keeper draft slots by hand -- there is no
    round-forfeited formula in the settings to apply. Inventing one and
    presenting the result as "your keeper cost" would be the kind of
    confident fabrication this project exists to avoid, so the implied round
    from the player's own board rank is shown instead and labelled as the
    proxy it is.
    """
    import csv
    import matchup as mu
    try:
        league = mu.load_league(mu.latest_cache())
    except SystemExit as exc:
        raise HTTPException(503, str(exc))
    draft = league["raw"].get("draft") or {}
    meta = league["raw"].get("league") or {}
    teams = league["raw"].get("teams") or []
    n_teams = int(meta.get("size") or len(teams) or 12)
    keeper_count = draft.get("keeper_count")
    if keeper_count is None:
        keeper_count = meta.get("keeper_count")
    order_type = draft.get("keeper_order_type")

    path = PROJECT_ROOT / DRAFT_WINDOWS["blend"][0]
    if not path.exists():
        raise HTTPException(404, "draft_board.csv not found")
    board = []
    with path.open(newline="") as fh:
        for r in csv.DictReader(fh):
            try:
                board.append((int(float(r["player_id"])), r))
            except (TypeError, ValueError, KeyError):
                continue
    by_id = {pid: r for pid, r in board}

    import pandas as _pd
    m = _pd.read_csv(PROJECT_ROOT / "espn_bdl_id_map.csv")
    to_bdl = {int(r.espn_id): int(r.bdl_player_id) for r in m.itertuples()
              if _pd.notna(r.bdl_player_id)}

    me = next((t for t in teams if t.get("is_me")), None)
    if me is None:
        raise HTTPException(503, "no team in this league is marked as yours")

    rows = []
    for p in me.get("roster", []):
        bid = to_bdl.get(p.get("player_id"))
        rec = by_id.get(bid) if bid else None
        if rec is None:
            rows.append({"name": p.get("name"), "on_board": False,
                         "slot": p.get("lineup_slot")})
            continue
        try:
            rank = int(float(rec.get("board_rank")))
        except (TypeError, ValueError):
            rank = None
        rows.append({
            "name": p.get("name"), "on_board": True,
            "slot": p.get("lineup_slot"), "position": p.get("position"),
            "pro_team": p.get("pro_team"),
            "board_rank": rank,
            "total_value": float(rec.get("total_value") or 0),
            "per_game_value": float(rec.get("per_game_value") or 0),
            # Where the board says they belong, in rounds of this league's size.
            "implied_round": ((rank - 1) // n_teams + 1) if rank else None,
        })
    rows.sort(key=lambda r: (not r["on_board"], r.get("board_rank") or 9999))
    keepable = [r for r in rows if r["on_board"]]
    return {
        "keeper_count": keeper_count,
        "keeper_order_type": order_type,
        "league_size": n_teams,
        "rows": rows,
        "recommended": keepable[:keeper_count] if keeper_count else [],
        "rule_note": (
            f"This league keeps {keeper_count} player"
            f"{'' if keeper_count == 1 else 's'}. ESPN reports the keeper "
            f"order type as {order_type!r}"
            + (", which means the commissioner assigns keeper draft slots by "
               "hand — there is no round-cost formula in the settings to "
               "apply, so no keeper cost can be computed from them."
               if order_type == "MANUAL" else
               ". The round cost is derived from that setting.")),
        "proxy_note": (
            "'Implied round' is where the draft board's own ranking would "
            "place the player in a "
            f"{n_teams}-team draft. It is a proxy for what keeping them is "
            "worth, NOT the round this league would actually charge."),
        "advisory": (
            "Advisory. The board's weights were backtested walk-forward; the "
            "keeper judgement on top of them was not, and it ignores "
            "positional scarcity, injury risk and who else is keeping whom."),
    }


class MockDraftRequest(BaseModel):
    """Stateless: the client sends every pick made so far and the server
    replays the draft. Deterministic given (seed, picks), so no session state
    and a reload cannot desync the board."""
    picks: list[int] = []        # bdl player_ids the USER has taken, in order
    seed: int = 0
    opp_noise: float = 12.0


@app.post("/api/fantasy/mock-draft")
def fantasy_mock_draft(req: MockDraftRequest) -> dict:
    """Interactive practice draft against the validated opponent model.

    Reuses draft_assistant's own harness -- `snake_order`, `load_pool`, and
    the rank-plus-noise opponent rule used to validate the draft assistant --
    rather than a second simulator. If the opponent model is wrong, it is
    wrong in both places, which is the point of not forking it.
    """
    import numpy as np
    import draft_assistant as da

    pool = da.load_pool()
    order = da.snake_order(da.ROUND1_ORDER, da.N_ROUNDS)
    rng = np.random.default_rng(req.seed)
    # One noise draw per seed, exactly as simulate_draft does, so the field
    # behaves identically on every replay of the same seed.
    avail = pool.copy()
    avail["opp_key"] = (avail["draft_rank"]
                        + rng.normal(0, req.opp_noise, len(avail)))

    taken, rosters, log = set(), {t: [] for t in set(order)}, []
    user_queue = list(req.picks)
    my_turn_at = None
    for i, team in enumerate(order):
        remaining = avail[~avail["player_id"].isin(taken)]
        if remaining.empty:
            break
        if team == da.MY_TEAM_ID:
            if not user_queue:
                my_turn_at = i
                break
            pid = user_queue.pop(0)
            row = remaining[remaining["player_id"] == pid]
            if row.empty:
                raise HTTPException(
                    400, f"player {pid} is not available at pick {i + 1}")
            row = row.iloc[0]
        else:
            row = remaining.sort_values("opp_key").iloc[0]
        taken.add(int(row["player_id"]))
        rosters[team].append(row["name"])
        log.append({"pick": i + 1, "round": i // da.N_TEAMS + 1,
                    "team_id": int(team), "is_me": team == da.MY_TEAM_ID,
                    "name": row["name"], "pos": row.get("pos_str"),
                    "player_id": int(row["player_id"]),
                    "total_value": float(row["total_value"])})

    remaining = avail[~avail["player_id"].isin(taken)]
    best = remaining.sort_values("total_value", ascending=False).head(25)
    done = my_turn_at is None
    gap = (da.picks_until_next(order, da.MY_TEAM_ID, my_turn_at + 1)
           if my_turn_at is not None else None)
    return {
        "pick_number": (my_turn_at + 1) if my_turn_at is not None else len(order),
        "round": ((my_turn_at // da.N_TEAMS) + 1) if my_turn_at is not None
                 else da.N_ROUNDS,
        "on_the_clock": "you" if not done else None,
        "complete": done,
        "picks_until_my_next": gap,
        "my_roster": rosters[da.MY_TEAM_ID],
        "my_value": round(sum(p["total_value"] for p in log if p["is_me"]), 3),
        "log": log[-24:],
        "available": [{"player_id": int(r.player_id), "name": r.name,
                       "pos": r.pos_str, "pro_team": r.pro_team,
                       "total_value": round(float(r.total_value), 3),
                       "draft_rank": None if _pd_isna(r.draft_rank)
                                     else int(r.draft_rank)}
                      for r in best.itertuples()],
        "pool_size": int(len(pool)),
        "rounds": da.N_ROUNDS, "teams": da.N_TEAMS,
        "seed": req.seed,
        "note": ("Opponents pick by ESPN rank plus Gaussian noise (sigma "
                 f"{req.opp_noise}), the same model used to validate the "
                 "draft assistant. Deterministic for a given seed, so the "
                 "same seed replays the same field."),
    }


def _pd_isna(v) -> bool:
    import pandas as _pd
    return bool(_pd.isna(v))


class TradeSide(BaseModel):
    team: str = "A"
    player_ids: list[int] = []


class TradeRequest(BaseModel):
    side_a: TradeSide = TradeSide(team="A")
    side_b: TradeSide = TradeSide(team="B")
    window: str = "blend"


@app.post("/api/fantasy/trade")
def fantasy_trade(req: TradeRequest) -> dict:
    """Evaluate an N-for-M trade using the draft board's own values.

    Reuses the board CSV rather than recomputing value, exactly as spec item 14
    requires: if this computed its own z-scores, a player could be worth one
    thing on the draft board and another in a trade, and there would be no way
    to tell which was right.

    Advisory, and more so than the board it reads. No walk-forward test
    validates trade judgement -- roster context, positional need, games
    remaining and injury risk are all absent -- so the output reports category
    deltas and declines to declare a winner.
    """
    import csv
    fname, _blurb = DRAFT_WINDOWS.get(req.window, DRAFT_WINDOWS["blend"])
    path = PROJECT_ROOT / fname
    if not path.exists():
        raise HTTPException(404, f"{fname} not found; build the draft board first")
    with path.open(newline="") as fh:
        board = {}
        for r in csv.DictReader(fh):
            try:
                pid = int(float(r.get("bdl_player_id") or r.get("player_id") or 0))
            except (TypeError, ValueError):
                continue
            if pid:
                board[pid] = r
    if not req.side_a.player_ids or not req.side_b.player_ids:
        raise HTTPException(400, "both sides need at least one player")

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    def side(s: TradeSide):
        players, missing = [], []
        for pid in s.player_ids:
            r = board.get(pid)
            if r is None:
                missing.append(pid)
                continue
            players.append({
                "player_id": pid, "name": r.get("name"),
                "pro_team": r.get("pro_team"), "pos": r.get("pos_str"),
                "total_value": num(r.get("total_value")),
                "per_game_value": num(r.get("per_game_value")),
                "availability": num(r.get("availability")),
                "z": {a: num(r.get(b)) for a, b, _c in DRAFT_PAIRS},
            })
        return {"team": s.team, "players": players, "missing": missing,
                "total_value": sum(p["total_value"] for p in players),
                "z": {a: sum(p["z"][a] for p in players)
                      for a, _b, _c in DRAFT_PAIRS}}

    a, b = side(req.side_a), side(req.side_b)
    deltas = [{"stat": stat, "label": label,
               "a_gives": round(a["z"][stat], 3),
               "b_gives": round(b["z"][stat], 3),
               "a_net": round(b["z"][stat] - a["z"][stat], 3)}
              for stat, _z, label in DRAFT_PAIRS]
    return {
        "window": req.window, "side_a": a, "side_b": b,
        "value_delta_a": round(b["total_value"] - a["total_value"], 3),
        "category_deltas": deltas,
        "gained_by_a": [d["label"] for d in deltas if d["a_net"] > 0.5],
        "lost_by_a": [d["label"] for d in deltas if d["a_net"] < -0.5],
        "advisory": (
            "Advisory. Values come from the draft board, whose weights were "
            "backtested walk-forward; the TRADE judgement on top of them was "
            "not. Roster construction, positional need, games remaining and "
            "injury risk are not modelled, so this reports what each side "
            "gains and gives up by category and does not name a winner."),
        "counts_note": (
            f"{len(a['players'])}-for-{len(b['players'])}. A trade that is "
            "even on summed value can still be bad for the side sending more "
            "players, because roster spots are finite."
            if len(a["players"]) != len(b["players"]) else None),
    }


# =============================================================================
# Weekly matchup strategy, lineup and waivers (design spec phase 7)
# =============================================================================
_matchup_lock = threading.Lock()


@app.get("/api/fantasy/matchup")
def fantasy_matchup(season: int | None = None, week_start: str | None = None) -> dict:
    """Category state, what would flip a losing category, lineup and waivers.

    A reader over matchup.py, the same way the draft-board endpoint is a reader
    over draft_board.py: the reconciliation, the flip logic and the optimiser
    all live there and are exercised by the CLI and the suite, so the API
    cannot drift into a second implementation of them.
    """
    import matchup as mu
    with _matchup_lock:
        try:
            con = _ex()
            d = mu.build(con, season, week_start)
        except SystemExit as exc:
            raise HTTPException(503, str(exc))
    return d


@app.get("/api/fantasy/outlook")
def fantasy_outlook(season: int | None = None, weeks: int = 3) -> dict:
    """Season outlook, multi-week fixture load, and today's digest."""
    import matchup as mu
    with _matchup_lock:
        con = _ex()
        try:
            league = mu.load_league(mu.latest_cache(season))
        except SystemExit as exc:
            raise HTTPException(503, str(exc))
        proj = mu.projections(con)
        week = mu.find_week(con, league)
        start = week.get("start") if week.get("found") else None
        return {
            "season_label": league["season_label"],
            "outlook": mu.season_outlook(
                league, strength_from=mu.roster_strength(con, league, proj)),
            "schedule": (mu.schedule_outlook(con, league, start, weeks, proj)
                         if start else None),
            "digest": mu.daily_digest(con, league, proj),
        }


@app.get("/api/fantasy/retrospective")
def fantasy_retrospective(season: int | None = None,
                          objective: str = "production") -> dict:
    """Week-by-week: the lineup set against the optimiser's."""
    import matchup as mu
    if objective not in ("production", "categories"):
        raise HTTPException(400, "objective must be 'production' or 'categories'")
    with _matchup_lock:
        con = _ex()
        try:
            league = mu.load_league(mu.latest_cache())
        except SystemExit as exc:
            raise HTTPException(503, str(exc))
        return mu.retrospective(con, league, mu.projections(con), season, objective)


class NarrateRequest(BaseModel):
    """Only the trade brief needs an input; the rest are read from state."""
    side_a: TradeSide | None = None
    side_b: TradeSide | None = None
    window: str = "blend"


@app.post("/api/narrate/{kind}")
def narrate_kind(kind: str, req: NarrateRequest | None = None) -> dict:
    """The one paid action in this app, in four flavours.

    Every commentary button in the UI posts here. The brief is built from
    numbers already computed and checked; the model is given facts and told
    not to invent any, and the same honesty rules apply to all four kinds
    because they share one narration function. See narrate.KINDS.

    The brief is always returned even when the model cannot be reached: it is
    deterministic, it is the exact input the model would get, and it is
    useful on its own. No placeholder narration is ever substituted.
    """
    import matchup as mu
    import narrate as nr

    if kind not in nr.KINDS:
        raise HTTPException(400, f"kind must be one of {sorted(nr.KINDS)}")

    if kind == "trade":
        if not req or not req.side_a or not req.side_b:
            raise HTTPException(400, "a trade narration needs both sides")
        brief = nr.compose_trade(fantasy_trade(
            TradeRequest(side_a=req.side_a, side_b=req.side_b,
                         window=req.window)))
    else:
        with _matchup_lock:
            con = _ex()
            try:
                league = mu.load_league(mu.latest_cache())
            except SystemExit as exc:
                raise HTTPException(503, str(exc))
            proj = mu.projections(con)
            if kind == "matchup":
                m = mu.build(con, None, None)
                week = m.get("week") or {}
                sched = (mu.schedule_outlook(con, league, week["start"], 3, proj)
                         if week.get("found") else None)
                brief = nr.compose_matchup(
                    m, mu.daily_digest(con, league, proj),
                    mu.season_outlook(league,
                        strength_from=mu.roster_strength(con, league, proj)),
                    sched)
            elif kind == "outlook":
                week = mu.find_week(con, league)
                start = week.get("start") if week.get("found") else None
                brief = nr.compose_outlook({
                    "season_label": league["season_label"],
                    "outlook": mu.season_outlook(
                        league,
                        strength_from=mu.roster_strength(con, league, proj)),
                    "schedule": (mu.schedule_outlook(con, league, start, 3, proj)
                                 if start else None),
                    "digest": mu.daily_digest(con, league, proj)})
            elif kind == "recap":
                brief = nr.compose_recap(
                    mu.retrospective(con, league, proj),
                    mu.missed_waivers(con, league),
                    mu.season_outlook(
                        league,
                        strength_from=mu.roster_strength(con, league, proj)))
            else:      # retrospective
                brief = nr.compose_retrospective(
                    mu.retrospective(con, league, proj))

    out = {"kind": kind, "brief": brief, "brief_chars": len(brief),
           "narration": None, "verified_live": False}
    try:
        res = nr.narrate(brief, kind=kind)
        out.update(res)
        out["narration"] = res["text"]
    except nr.NoCredits as exc:
        out["unavailable"] = ("The model could not be reached: the API credit "
                              "balance is exhausted. The brief below is the "
                              "exact input it would receive. This path is "
                              "built and unit-tested against a stub, and has "
                              "never run live.")
        out["detail"] = str(exc)[:200]
    except Exception as exc:
        out["unavailable"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


DRAFT_WINDOWS = {
    "blend": ("draft_board.csv",
              "Three seasons, weighted 0.60 / 0.30 / 0.10 toward the most recent"),
    "last2": ("draft_board-last2.csv",
              "Last two seasons only, the same weights renormalised (0.67 / 0.33)"),
    "last1": ("draft_board-last1.csv",
              "Last completed season only"),
    # The only window that is a forecast. Everything else on this page
    # describes seasons already played; this one does not, and the UI has to
    # say which it is looking at.
    "projected": ("draft_board-projected.csv",
                  "Model projection for next season (walk-forward validated)"),
}
PROJECTED_WINDOW = "projected"

# Which z-score column each raw stat belongs beside. A z-score on its own says
# where a player ranks and nothing about what he actually does; the raw value is
# what makes it readable.
DRAFT_PAIRS = [("pts", "z_pts", "PTS"), ("reb", "z_reb", "REB"),
               ("ast", "z_ast", "AST"), ("stl", "z_stl", "STL"),
               ("blk", "z_blk", "BLK"), ("fg3m", "z_fg3m", "3PM"),
               ("fg_pct", "z_fgpct", "FG%"), ("ft_pct", "z_ftpct", "FT%"),
               ("turnover", "z_turnover", "TO")]


@app.get("/api/fantasy/draft-board")
def fantasy_draft_board(window: str = "blend", limit: int = 400) -> dict:
    """Read a board produced by draft_board.py.

    Deliberately a reader, not a re-implementation: the ranking logic, the
    volume-weighted percentage impact and the backtested weights all live in
    draft_board.py and were verified there. The alternate windows are that same
    script run over fewer seasons with its own weights renormalised — a
    different view of what has already happened, NOT a projection. Nothing here
    forecasts next season; that is the unbuilt ML pipeline.
    """
    import csv
    if window not in DRAFT_WINDOWS:
        raise HTTPException(400, f"window must be one of {sorted(DRAFT_WINDOWS)}")
    fname, blurb = DRAFT_WINDOWS[window]
    path = PROJECT_ROOT / fname
    if not path.exists():
        raise HTTPException(404, f"{fname} not found — run "
                                 f"`python draft_board.py --window {window} "
                                 f"--out {fname}`")
    with path.open(newline="") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        for k, v in list(r.items()):
            if v in ("", None):
                r[k] = None
                continue
            try:
                r[k] = float(v) if ("." in v or "e" in v.lower()) else int(v)
            except (TypeError, ValueError):
                pass
    ranked = [r for r in rows if r.get("board_rank") is not None]
    ranked.sort(key=lambda r: r["board_rank"])
    projected = window == PROJECTED_WINDOW
    return {"window": window, "window_label": blurb,
            "is_projection": projected,
            "projection_note": (
                "These are model projections for a season that has not been "
                "played, from projections.py (ridge on 110 features, fit "
                "walk-forward). Validated at 0.742 Spearman against the "
                "recency-weighted baseline's 0.712 over 32 held-out seasons. "
                "Every other window on this page describes completed seasons."
                if projected else None),
            "windows": [{"key": k, "label": v[1],
                         "available": (PROJECT_ROOT / v[0]).exists()}
                        for k, v in DRAFT_WINDOWS.items()],
            "pairs": [{"stat": a, "z": b, "label": c} for a, b, c in DRAFT_PAIRS],
            "rows": ranked[:limit], "total": len(ranked),
            "columns": list(rows[0].keys()) if rows else [],
            "generated": dt.datetime.fromtimestamp(
                path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
            "source": f"{fname}, produced by draft_board.py --window {window}",
            "not_a_projection": (
                "Every column here describes seasons already played. None of it "
                "forecasts next season."),
            }


# =============================================================================
# Clustering — k-means over chosen variables, shown in a PCA projection
# =============================================================================
# scipy + numpy rather than scikit-learn: k-means and PCA are a page of code
# against libraries already pinned here, and doing it directly lets the result
# be deterministic by construction. That matters more than usual in this
# project — run_analysis rejects its own output when two executions disagree,
# and an unseeded k-means is exactly that failure.
class ClusterRequest(BaseModel):
    variables: list[str]
    k: int = 4
    season_from: int = 2020
    season_to: int = 2025
    min_games: int = 41
    game_type: str = "regular"


@app.post("/api/model/cluster")
def model_cluster(req: ClusterRequest) -> dict:
    import numpy as np
    from scipy.cluster.vq import kmeans2

    gt = _game_type(req.game_type)
    if not 2 <= req.k <= 10:
        raise HTTPException(400, "k must be between 2 and 10")
    if not 2 <= len(req.variables) <= 8:
        raise HTTPException(400, "choose between 2 and 8 variables")
    for v in req.variables:
        if model_var(v) is None:
            raise HTTPException(400, f"unknown variable {v!r}")

    rows = _model_frame(req.variables[0], req.variables[1:], req.season_from,
                        req.season_to, req.min_games, gt)
    if len(rows) < req.k * 5:
        raise HTTPException(400, f"only {len(rows)} rows; too few for k={req.k}")

    # One row per player over the window, so a player is a point rather than a
    # point per season -- clustering player-seasons would cluster careers into
    # themselves and call it structure.
    agg: dict = {}
    for r in rows:
        a = agg.setdefault(r["player_id"], {"name": r["player_name"], "n": 0,
                                            "sums": [0.0] * len(req.variables)})
        a["n"] += 1
        for i, v in enumerate(req.variables):
            a["sums"][i] += float(r[v])
    ids = sorted(agg)
    X = np.array([[agg[i]["sums"][j] / agg[i]["n"] for j in range(len(req.variables))]
                  for i in ids], dtype=float)
    names = [agg[i]["name"] for i in ids]

    # z-score first: k-means on raw units would let points-per-game dominate
    # steals-per-game purely because its numbers are bigger.
    mu, sd = X.mean(axis=0), X.std(axis=0)
    sd[sd == 0] = 1.0
    Z = (X - mu) / sd

    centroids, labels = kmeans2(Z, req.k, seed=0, minit="++", iter=50)
    # PCA by SVD on the same standardised matrix, so the picture and the
    # clustering are in one geometry rather than two.
    Zc = Z - Z.mean(axis=0)
    U, S, Vt = np.linalg.svd(Zc, full_matrices=False)
    coords = U[:, :2] * S[:2]
    var_ratio = (S ** 2) / (S ** 2).sum()
    # Sign is arbitrary in SVD; pin it so the same query draws the same picture.
    for axis in range(2):
        if coords[:, axis].sum() < 0:
            coords[:, axis] *= -1
            Vt[axis] *= -1

    points = [{"player_id": ids[i], "player": names[i],
               "pc1": float(coords[i, 0]), "pc2": float(coords[i, 1]),
               "cluster": int(labels[i])} for i in range(len(ids))]

    clusters = []
    for c in range(req.k):
        members = [i for i in range(len(ids)) if labels[i] == c]
        if not members:
            clusters.append({"cluster": c, "size": 0, "members": [], "means": {}})
            continue
        means = {v: float(X[members, j].mean())
                 for j, v in enumerate(req.variables)}
        # Name a cluster by what most separates it, not by its number.
        zmeans = {v: float(Z[members, j].mean())
                  for j, v in enumerate(req.variables)}
        top = sorted(zmeans, key=lambda v: -abs(zmeans[v]))[:2]
        label = ", ".join(
            f"{'high' if zmeans[v] > 0 else 'low'} {model_var(v)['label'].lower()}"
            for v in top)
        centre = coords[members].mean(axis=0)
        order = sorted(members, key=lambda i: float(
            ((coords[i] - centre) ** 2).sum()))
        clusters.append({
            "cluster": c, "size": len(members), "label": label,
            "means": means, "z_means": zmeans,
            "members": [{"player_id": ids[i], "player": names[i],
                         **{v: float(X[i, j]) for j, v in enumerate(req.variables)}}
                        for i in order]})

    return {
        "variables": [{"key": v, "label": model_var(v)["label"]} for v in req.variables],
        "k": req.k, "n_players": len(ids), "n_rows": len(rows),
        "season_from": req.season_from, "season_to": req.season_to,
        "game_type": gt, "min_games": req.min_games,
        "points": points, "clusters": clusters,
        "pc1_variance": float(var_ratio[0]), "pc2_variance": float(var_ratio[1]),
        "loadings": [{"variable": model_var(v)["label"],
                      "pc1": float(Vt[0][j]), "pc2": float(Vt[1][j])}
                     for j, v in enumerate(req.variables)],
        "exploratory": True,
        "warning": ("k-means finds k groups whether or not k groups "
                    "exist — the algorithm never declines. The projection shows "
                    f"{(var_ratio[0] + var_ratio[1]) * 100:.0f}% of the variation, "
                    "so points close together on screen are not necessarily close "
                    "in the full space."),
    }
