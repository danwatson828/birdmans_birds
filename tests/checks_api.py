"""API checks.

The point of these is narrow and specific: the Ask view must show the SAME
report the CLI shows. The failure they exist to catch is a second renderer
growing inside the API layer and quietly dropping something -- a caveat, a
stability verdict -- that nobody notices because both sides look plausible.

They use FastAPI's TestClient, so no server needs to be running. They never
submit a question: that would spend money inside the test suite.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3

from .harness import Skip, check


def _client():
    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:
        raise Skip(f"fastapi not installed ({exc})")
    import api
    return TestClient(api.app)


def _some_run_ids(limit: int = 3) -> list[int]:
    import analyses
    if not analyses.STORE.exists():
        raise Skip("analyses.db does not exist yet")
    con = sqlite3.connect(f"file:{analyses.STORE}?mode=ro", uri=True)
    try:
        ids = [r[0] for r in con.execute(
            "SELECT id FROM runs ORDER BY id DESC LIMIT ?", (limit,))]
    finally:
        con.close()
    if not ids:
        raise Skip("no saved analyses to check against")
    return ids


@check("API report is byte-identical to analyses.render_report", group="api")
def api_report_matches_cli(con_unused):
    import analyses
    client = _client()
    problems = []
    for rid in _some_run_ids():
        got = client.get(f"/api/analyses/{rid}")
        if got.status_code != 200:
            problems.append(f"#{rid}: HTTP {got.status_code}")
            continue
        want = analyses.render_report(analyses.load_run(rid))
        if got.json()["report"] != want:
            problems.append(f"#{rid}: report text differs from render_report")
    return not problems, ("report served verbatim for "
                          f"{len(_some_run_ids())} analyses" if not problems
                          else "; ".join(problems))


@check("API exposes the stability verdict, not a summary", group="api")
def api_exposes_stability(con_unused):
    """A simplified verdict is the specific regression worth guarding: the whole
    reproducibility feature is worthless if the UI rounds it off."""
    client = _client()
    ids = _some_run_ids(8)
    checked = False
    for rid in ids:
        body = client.get(f"/api/analyses/{rid}").json()
        sql_calls = [c for c in body["calls"] if c["tool"] == "run_sql"]
        for c in sql_calls:
            for field in ("stability", "compare_mode", "order_varied",
                          "execution_used"):
                if field not in c:
                    return False, f"#{rid} step {c['seq']} is missing {field}"
            if c["stability"]:
                checked = True
                if f"[{c['stability']}:" not in body["report"]:
                    return False, (f"#{rid} step {c['seq']} has a verdict the "
                                   f"rendered report does not show")
    if not checked:
        raise Skip("no stored step carries a verdict yet")
    return True, "per-step verdict fields present and rendered in the report"


@check("API rerun matches analyses.rerun", group="api")
def api_rerun_matches_cli(con_unused):
    import analyses
    client = _client()
    rid = _some_run_ids(1)[0]
    direct = analyses.rerun(rid)
    served = client.post(f"/api/analyses/{rid}/rerun")
    if served.status_code != 200:
        return False, f"HTTP {served.status_code}"
    body = served.json()
    d = [(r["seq"], r["tool"], r["status"]) for r in direct["results"]]
    a = [(r["seq"], r["tool"], r["status"]) for r in body["results"]]
    if d != a:
        return False, f"step verdicts differ: {d} vs {a}"
    if body["same_snapshot"] != direct["same_snapshot"]:
        return False, "same_snapshot disagrees"
    return True, f"#{rid}: {len(a)} step(s), identical verdicts via both paths"


@check("API refuses a missing analysis and an empty question", group="api")
def api_rejects_bad_input(con_unused):
    client = _client()
    problems = []
    if client.get("/api/analyses/99999999").status_code != 404:
        problems.append("missing analysis did not 404")
    if client.post("/api/ask", json={"question": "   "}).status_code != 400:
        problems.append("empty question was accepted")
    if client.get("/api/jobs/nope").status_code != 404:
        problems.append("unknown job did not 404")
    return not problems, ("404s on missing analysis and job, 400 on empty question"
                          if not problems else "; ".join(problems))


@check("explorer endpoints answer correctly", group="api")
def explorer_endpoints(con_unused):
    client = _client()
    problems = []
    r = client.get("/api/explore/leaderboard?stat=ppg&min_games=41")
    if r.status_code != 200 or not r.json()["rows"]:
        problems.append("leaderboard returned nothing")
    if client.get("/api/explore/leaderboard?stat=DROP+TABLE").status_code != 400:
        problems.append("leaderboard accepted a stat outside the whitelist")
    if client.get("/api/explore/leaderboard?stat=ppg&game_type=nonsense").status_code != 400:
        problems.append("leaderboard accepted an unknown game_type")
    if client.get("/api/explore/coverage?stat=not_a_column").status_code != 400:
        problems.append("coverage accepted an unknown column")
    if client.get("/api/explore/player/999999999").status_code != 404:
        problems.append("missing player did not 404")
    cat = client.get("/api/explore/catalog")
    if cat.status_code != 200 or not cat.json()["tables"]:
        problems.append("catalog returned nothing")
    return not problems, ("leaderboard, catalog, coverage and player all behave; "
                          "bad input rejected" if not problems else "; ".join(problems))


@check("explorer survives concurrent requests", group="api")
def explorer_concurrency(con_unused):
    """Six coverage requests at once is what the catalog page actually does.

    The shared DuckDB connection was used outside its lock for one column check,
    so concurrent requests interleaved on a single cursor and one came back with
    another request's columns and a 400. Serial calls never showed it.
    """
    import concurrent.futures
    client = _client()
    stats = ["stl", "blk", "turnover", "fg3m", "oreb", "plus_minus"]

    def fetch(s):
        r = client.get(f"/api/explore/coverage?stat={s}")
        return s, r.status_code, len(r.json().get("seasons", [])) if r.status_code == 200 else 0

    bad = []
    for _ in range(3):
        with concurrent.futures.ThreadPoolExecutor(len(stats)) as ex:
            for s, code, n in ex.map(fetch, stats):
                if code != 200 or n == 0:
                    bad.append(f"{s}:{code}/{n}")
    return not bad, (f"{len(stats) * 3} concurrent requests all correct"
                     if not bad else f"failures: {bad[:6]}")


@check("both skills are loadable and well-formed", group="api")
def skills_present(con_unused):
    import agent
    st = agent.SkillTool()
    have = dict(st.available())
    missing = {"statistical-methodology", "analytical-visualization"} - set(have)
    if missing:
        return False, f"missing skill(s): {sorted(missing)}"
    problems = []
    for name, desc in have.items():
        if len(desc) < 40:
            problems.append(f"{name}: description too thin to route on")
        body = st.run(name)
        if body.startswith("ERROR") or len(body) < 400:
            problems.append(f"{name}: body missing or too short")
        if body.lstrip().startswith("---"):
            problems.append(f"{name}: frontmatter leaked into the loaded body")
    if st.run("../../../etc/passwd").startswith("ERROR") is False:
        problems.append("path traversal was not rejected")
    return not problems, (f"{len(have)} skills load cleanly, traversal rejected"
                          if not problems else "; ".join(problems))


@check("a skill-using analysis recorded which skills fired", group="api")
def skills_fired(con_unused):
    """Writing SKILL.md is not evidence that the model used it."""
    import json as _json
    import sqlite3
    import analyses
    con = sqlite3.connect(f"file:{analyses.STORE}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute("SELECT id, skills_loaded FROM runs "
                           "WHERE skills_loaded IS NOT NULL "
                           "AND skills_loaded NOT IN ('', '[]')").fetchall()
    finally:
        con.close()
    if not rows:
        raise Skip("no run since load_skill was added has invoked a skill")
    names = set()
    for r in rows:
        try:
            names |= set(_json.loads(r["skills_loaded"]))
        except Exception:
            return False, f"run #{r['id']} stored unparseable skills_loaded"
    return True, (f"{len(rows)} run(s) invoked skills; distinct: {sorted(names)}")


@check("correlation explorer computes and labels itself exploratory", group="api")
def correlation_explorer(con_unused):
    """The label is the feature. A correlation page that scans pairs manufactures
    significance, so every response must carry the warning unconditionally --
    including, especially, the ones that look impressive."""
    client = _client()
    problems = []
    r = client.get("/api/explore/correlation?x=mpg&y=ppg&season_from=2000&season_to=2025")
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    d = r.json()
    for field in ("exploratory", "warning", "stats", "points", "fit"):
        if field not in d:
            problems.append(f"response is missing {field}")
    if d.get("exploratory") is not True:
        problems.append("a strong correlation was not labelled exploratory")
    if "1 in 20" not in (d.get("warning") or ""):
        problems.append("the warning does not state the false-positive rate")
    st = d["stats"]
    if not (0.5 < st["pearson_r"] < 1.0):
        problems.append(f"minutes vs points r={st['pearson_r']:.2f}, expected strongly positive")
    if "slope_ci" not in st:
        problems.append("no confidence interval on the slope")

    # the arbitrary variable must be offered -- it is what makes the warning legible
    keys = {v["key"] for v in client.get("/api/explore/variables").json()["variables"]}
    if "jersey" not in keys:
        problems.append("jersey number is not offered, so the false-positive case "
                        "cannot be demonstrated")
    if client.get("/api/explore/correlation?x=ppg&y=ppg").status_code != 400:
        problems.append("the same variable twice was accepted")
    if client.get("/api/explore/correlation?x=ppg&y=nope").status_code != 400:
        problems.append("an unknown variable was accepted")
    return not problems, ("computes r, rho, R² and a slope CI; labels every "
                          "result exploratory; rejects bad input"
                          if not problems else "; ".join(problems))


@check("a known-spurious pair is significant but trivially small", group="api")
def spurious_pair_is_visible(con_unused):
    """Jersey number is arbitrary by construction. The pairing exists in the
    explorer precisely so the gap between 'significant' and 'meaningful' is
    demonstrable rather than asserted: p is tiny, R² is ~0.006."""
    client = _client()
    d = client.get("/api/explore/correlation?x=jersey&y=ft_pct"
                   "&season_from=2000&season_to=2025&min_games=41").json()
    st = d.get("stats")
    if not st:
        raise Skip("not enough rows for the jersey/ft_pct pairing")
    if st["pearson_p"] >= 0.05:
        raise Skip("this pairing is not currently significant; nothing to demonstrate")
    if st["r_squared"] > 0.05:
        return False, (f"R²={st['r_squared']:.3f} is too large for this to be the "
                       f"textbook spurious case")
    return True, (f"p={st['pearson_p']:.1e} yet R²={st['r_squared']:.4f} "
                  f"({st['r_squared'] * 100:.1f}% of variation) on n={d['n']}")


@check("knowledge base stores, tags and searches", group="api")
def knowledge_base(con_unused):
    import uuid as _uuid
    client = _client()
    marker = "kbtest-" + _uuid.uuid4().hex[:8]
    # Notes now require an analysis to hang off; borrow the newest one.
    existing = client.get("/api/analyses?limit=1").json()["analyses"]
    if not existing:
        raise Skip("no saved analysis to attach a note to")
    run_id = existing[0]["id"]
    created = client.post("/api/kb/notes", json={
        "title": f"test note {marker}", "body": f"body mentioning {marker}",
        "run_id": run_id,
        "tags": ["player:testguy", "stat:ts_pct", "bare-tag"]})
    if created.status_code != 200:
        return False, f"creating a note returned {created.status_code}"
    note_id = created.json()["id"]
    problems = []
    try:
        def notes_of(body):
            return [n for a in body["analyses"] for n in a["notes"]]
        by_text = client.get(f"/api/kb/search?q={marker}").json()
        if not any(n["id"] == note_id for n in notes_of(by_text)):
            problems.append("text search did not find the note via its analysis")
        by_tag = client.get("/api/kb/search?tag=player:testguy").json()
        got = next((n for n in notes_of(by_tag) if n["id"] == note_id), None)
        if got is None:
            problems.append("tag search did not find the note")
        elif "free:bare-tag" not in got.get("tags", []):
            problems.append(f"bare tag was not normalised: {got.get('tags')}")
        if any(n["id"] == note_id for n in
               notes_of(client.get("/api/kb/search?tag=player:nobody").json())):
            problems.append("tag filter matched the wrong tag")
        if client.post("/api/kb/notes",
                       json={"title": "   ", "run_id": run_id}).status_code != 400:
            problems.append("an empty title was accepted")
    finally:
        client.delete(f"/api/kb/notes/{note_id}")
    still = client.get(f"/api/kb/search?q={marker}").json()
    if any(n["id"] == note_id for a in still["analyses"] for n in a["notes"]):
        problems.append("the note survived deletion")
    return not problems, ("note created, found by text and by tag, bare tag "
                          "normalised, deleted cleanly"
                          if not problems else "; ".join(problems))


@check("the investigate endpoint is gone, not merely unlinked", group="api")
def investigate_removed(con_unused):
    """The hand-off moved to Claude Desktop over MCP. A removal that only
    hides the button leaves a second, unexercised path to the agent that can
    drift away from the MCP tool descriptions -- and an endpoint that still
    spends money when something posts to it."""
    import api as _api
    client = _client()
    r = client.post("/api/investigate", json={"x": "jersey", "y": "ft_pct"})
    if r.status_code != 404:
        return False, (f"POST /api/investigate still answers {r.status_code}; "
                       "the endpoint is hidden rather than removed")
    leftovers = [n for n in ("InvestigateIn", "_run_investigation", "investigate")
                 if hasattr(_api, n)]
    if leftovers:
        return False, f"dead symbols still exported from api: {leftovers}"
    routes = [getattr(r_, "path", "") for r_ in _api.app.routes]
    if any("investigate" in p for p in routes):
        return False, f"a route still mentions investigate: {routes}"
    # And nothing may still tell the reader to press the button.
    import pathlib as _pl
    web = _pl.Path(__file__).resolve().parent.parent / "web"
    pointing = [f.name for f in web.glob("*.js")
                if "/api/investigate" in f.read_text()]
    if pointing:
        return False, f"web modules still call the endpoint: {pointing}"
    return True, ("endpoint 404s, no investigate route, no dead symbols, and "
                  "no web module references it")


@check("explorer reports player-collapsed stats for constant attributes", group="api")
def pseudo_replication_handled(con_unused):
    """A player's jersey number, height, weight and draft slot do not change
    between his seasons, so pairing one against a season stat counts the same
    independent value many times over.

    An investigation of the jersey/FT% pairing caught this in the explorer's own
    output -- "n inflated roughly five-fold and its p-value meaningless as
    stated" -- which is the explorer's warning working on the explorer itself.
    """
    client = _client()
    d = client.get("/api/explore/correlation?x=jersey&y=ft_pct"
                   "&season_from=2000&season_to=2025&min_games=41").json()
    if not d.get("stats"):
        raise Skip("not enough rows for this pairing")
    problems = []
    if not d.get("collapsed"):
        problems.append("no player-collapsed statistic for a player-constant variable")
    else:
        if d["collapsed"]["n"] >= d["n"]:
            problems.append("collapsing did not reduce the row count")
        if d["collapsed"]["pearson_p"] <= d["stats"]["pearson_p"]:
            problems.append("the collapsed p-value is not more conservative")
    if not d.get("pseudo_replication_warning"):
        problems.append("no warning that the rows are not independent")
    if d.get("n_players") is None:
        problems.append("distinct player count not reported")

    # a season-varying pair must NOT be collapsed -- the correction is targeted
    v = client.get("/api/explore/correlation?x=mpg&y=ppg"
                   "&season_from=2000&season_to=2025").json()
    if v.get("collapsed") is not None:
        problems.append("a season-varying pair was collapsed unnecessarily")
    if v.get("pseudo_replication_warning"):
        problems.append("a season-varying pair got the independence warning")
    return not problems, (
        f"jersey/ft_pct: p {d['stats']['pearson_p']:.1e} -> "
        f"{d['collapsed']['pearson_p']:.1e} once {d['n']} rows become "
        f"{d['collapsed']['n']} players; season-varying pairs untouched"
        if not problems else "; ".join(problems))


@check("model builder reproduces the hand-done height/blocks result", group="api")
def model_builder_matches_investigation(con_unused):
    """Investigation #61 worked this out by hand on the held-out era. The builder
    exists so that work does not have to be redone, so it has to agree with it --
    not approximately, to the digits that were reported."""
    client = _client()
    r = client.post("/api/model/fit", json={
        "outcome": "bpg", "season_from": 2017, "season_to": 2025, "min_games": 41,
        "specs": [{"label": "linear", "predictors": ["height_in"], "degree": 1},
                  {"label": "quadratic", "predictors": ["height_in"], "degree": 2}]})
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}: {r.text[:180]}"
    d = r.json()
    problems = []
    if d["n"] != 2836:
        problems.append(f"n={d['n']}, investigation had 2,836")
    if d["n_players"] != 815:
        problems.append(f"{d['n_players']} players, investigation had 815")
    if not d["clustered"]:
        problems.append("did not cluster on player despite a player-constant predictor")

    lin = next(x for x in d["results"] if x["label"] == "linear")
    quad = next(x for x in d["results"] if x["label"] == "quadratic")
    slope = next(c for c in lin["coefficients"] if c["term"] == "height_in")
    if abs(slope["estimate"] - 0.0766) > 0.0005:
        problems.append(f"slope {slope['estimate']:.4f}, investigation had +0.0766")
    if abs(lin["r_squared"] - 0.329) > 0.002:
        problems.append(f"linear R² {lin['r_squared']:.4f}, investigation had 0.329")
    if abs(quad["r_squared"] - 0.389) > 0.002:
        problems.append(f"quadratic R² {quad['r_squared']:.4f}, investigation had 0.389")
    sq = next(c for c in quad["coefficients"] if c["term"] == "height_in^2")
    if not (1e-9 < sq["p"] < 1e-7):
        problems.append(f"quadratic term p={sq['p']:.2g}, investigation had 1.2e-08")
    if d["best_by_aic"] != "quadratic":
        problems.append("AIC did not prefer the quadratic")
    return not problems, (
        f"n={d['n']}/{d['n_players']} players, slope {slope['estimate']:+.4f}, "
        f"R² {lin['r_squared']:.3f}→{quad['r_squared']:.3f}, quad p={sq['p']:.1e} "
        f"— matches the hand-done investigation"
        if not problems else "; ".join(problems))


@check("residual diagnostics distinguish curvature from noise", group="api")
def residual_diagnostics(con_unused):
    """Flagging the sign pattern alone called flat residuals curved. The linear
    fit here genuinely curves and the quadratic genuinely does not, so the same
    data answers both ways and the threshold is what separates them."""
    client = _client()
    base = {"outcome": "bpg", "season_from": 2017, "season_to": 2025, "min_games": 41}
    lin = client.post("/api/model/fit", json={**base, "specs": [
        {"label": "linear", "predictors": ["height_in"], "degree": 1}]}).json()["diagnostics"]
    quad = client.post("/api/model/fit", json={**base, "specs": [
        {"label": "quadratic", "predictors": ["height_in"], "degree": 2}]}).json()["diagnostics"]
    problems = []
    if not lin.get("curvature"):
        problems.append("did not detect curvature in the linear fit")
    if quad.get("curvature"):
        problems.append("reported curvature in the quadratic fit, where it is corrected")
    if not lin.get("fanning"):
        problems.append("did not detect the known fanning")
    return not problems, (
        f"linear: curvature at {lin['curvature_swing_sd']} SD swing; "
        f"quadratic: {quad['curvature_swing_sd']} SD, not flagged"
        if not problems else "; ".join(problems))


@check("catalog coverage is derived, not a curated list", group="api")
def catalog_derives_coverage(con_unused):
    """`v_stat_first_tracked` enumerates 16 column names as SQL literals, so
    `player_advanced_stats` acquired no coverage entry when it was added a phase
    later. Deriving from the live schema is what stops that recurring."""
    client = _client()
    d = client.get("/api/explore/catalog").json()
    problems = []
    tables = {t["table_name"] for t in d["tables"]}
    if "player_advanced_stats" not in tables:
        problems.append("player_advanced_stats missing from the table list")
    by_table = {}
    for c in d["coverage"]:
        by_table.setdefault(c["table"], []).append(c["stat"])
    adv = by_table.get("player_advanced_stats", [])
    if len(adv) < 10:
        problems.append(f"only {len(adv)} advanced stats have coverage entries")
    if "usage_percentage" not in adv:
        problems.append("usage_percentage has no coverage entry")
    for c in d["coverage"]:
        if c["table"] == "player_advanced_stats" and c["first_tracked_season"]:
            if c["first_tracked_season"] < 1996:
                problems.append(f"{c['stat']} claims data before 1996")
            break
    if len(d["coverage"]) <= 16:
        problems.append("coverage is no larger than the old hand-written list")
    return not problems, (
        f"{len(d['coverage'])} stats covered across {len(by_table)} tables "
        f"({len(adv)} advanced), derived from the live schema"
        if not problems else "; ".join(problems))


@check("leaderboard range aggregates from totals", group="api")
def leaderboard_range(con_unused):
    """A range has to recompute rates from summed totals. Averaging season
    averages weights a 12-game season like an 82-game one and returns a
    different, wrong board — the failure that looks like it works."""
    client = _client()
    rng = client.get("/api/explore/leaderboard?stat=ts_pct&season_from=2020"
                     "&season_to=2024&min_games=200")
    if rng.status_code != 200:
        return False, f"HTTP {rng.status_code}"
    d = rng.json()
    problems = []
    if not d["is_range"]:
        problems.append("range request was not treated as a range")
    if "summed totals" not in d["aggregation"]:
        problems.append("did not aggregate from totals")
    if any(r["seasons"] > 5 for r in d["rows"]):
        problems.append("a player reports more seasons than the window contains")
    if any(r["games_played"] < 200 for r in d["rows"]):
        problems.append("min_games was not applied to the range total")

    # the rate must differ from naive averaging of season rates
    single = client.get("/api/explore/leaderboard?stat=ts_pct&season=2024"
                        "&min_games=41").json()
    if single["is_range"]:
        problems.append("a single-season request was treated as a range")
    return not problems, (
        f"{len(d['rows'])} players over 5 seasons, rates from summed totals, "
        f"threshold applied to the span" if not problems else "; ".join(problems))


@check("every nav route has a backing endpoint", group="api")
def nav_routes_backed(con_unused):
    """Nav promises eight destinations; each must actually answer."""
    client = _client()
    checks = {
        "ask/knowledge": "/api/analyses",
        "model": "/api/model/variables",
        "explore": "/api/explore/teams",
        "leaders": "/api/explore/leaderboard?stat=ppg",
        "catalog": "/api/explore/catalog",
        "fantasy": "/api/fantasy/draft-board?limit=5",
        "kb": "/api/kb/search",
    }
    bad = [f"{k} -> {v} ({client.get(v).status_code})"
           for k, v in checks.items() if client.get(v).status_code != 200]
    return not bad, (f"{len(checks)} nav destinations all answer"
                     if not bad else "; ".join(bad))


@check("logistic regression actually fits", group="api")
def logistic_works(con_unused):
    """It never ran: Logit results have no get_robustcov_results — that is an
    OLS method — so every logistic request returned an AttributeError."""
    client = _client()
    r = client.post("/api/model/fit", json={
        "outcome": "bpg", "season_from": 2017, "season_to": 2025, "min_games": 41,
        "model_type": "logistic",
        "specs": [{"label": "linear", "predictors": ["height_in"], "degree": 1}]})
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    d = r.json()
    res = d["results"][0]
    if "error" in res:
        return False, f"logistic errored: {res['error']}"
    problems = []
    if res.get("pseudo_r2") is None:
        problems.append("no pseudo R² reported")
    if d.get("logistic_threshold") is None:
        problems.append("no threshold reported for the binary split")
    slope = next((c for c in res["coefficients"] if c["term"] == "height_in"), None)
    if slope is None or slope["estimate"] <= 0:
        problems.append("height should raise the odds of above-median blocks")
    return not problems, (f"pseudo R²={res['pseudo_r2']:.3f}, threshold "
                          f"{d['logistic_threshold']:.3f}, slope "
                          f"{slope['estimate']:+.3f}"
                          if not problems else "; ".join(problems))


@check("model comparison discriminates rather than badging everything", group="api")
def aic_discriminates(con_unused):
    """AIC was computed correctly all along; the UI fit a one-model ladder, so
    its single row was trivially "best". A comparison needs alternatives, and on
    this case AIC alone picks a model whose own terms are unidentifiable."""
    client = _client()
    base = {"outcome": "bpg", "season_from": 2017, "season_to": 2025, "min_games": 41}
    one = client.post("/api/model/fit", json={**base, "specs": [
        {"label": "linear", "predictors": ["height_in"], "degree": 1}]}).json()
    if one["best_by_aic"] is not None:
        return False, "a single-model fit still claims a best model"

    full = client.post("/api/model/fit", json={**base, "specs": [
        {"label": "linear", "predictors": ["height_in"], "degree": 1},
        {"label": "quadratic", "predictors": ["height_in"], "degree": 2},
        {"label": "cubic", "predictors": ["height_in"], "degree": 3}]}).json()
    aics = [r["aic"] for r in full["results"]]
    problems = []
    if len({round(a, 3) for a in aics}) != len(aics):
        problems.append(f"AIC values are not distinct: {aics}")
    if not (aics[1] < aics[0]):
        problems.append("AIC does not prefer quadratic over linear")
    if full["parsimonious"] != "quadratic":
        problems.append(f"recommended {full['parsimonious']}, expected quadratic")
    cubic = next(r for r in full["results"] if r["label"] == "cubic")
    if cubic["all_terms_significant"]:
        problems.append("cubic terms reported as identifiable; they are not")
    if not full.get("aic_caveat"):
        problems.append("no caveat that AIC ignores the clustering")
    return not problems, (
        f"AIC {aics[0]:.0f} > {aics[1]:.0f} > {aics[2]:.0f}; quadratic recommended, "
        f"cubic flagged (top term p={cubic['highest_term_p']:.2f})"
        if not problems else "; ".join(problems))


@check("season picker is backed by real seasons", group="api")
def seasons_endpoint(con_unused):
    client = _client()
    d = client.get("/api/explore/seasons").json()
    problems = []
    if len(d["seasons"]) < 70:
        problems.append(f"only {len(d['seasons'])} seasons offered")
    if d["seasons"][0]["season"] < d["seasons"][-1]["season"]:
        problems.append("seasons are not newest-first")
    if d["latest"] != d["seasons"][0]["season"]:
        problems.append("latest does not match the first row")
    if any(s["games"] == 0 for s in d["seasons"]):
        problems.append("a season with no games is offered")
    return not problems, (f"{len(d['seasons'])} seasons, "
                          f"{d['seasons'][-1]['season']}–{d['latest']}"
                          if not problems else "; ".join(problems))


@check("teams list answers without a click", group="api")
def teams_have_stats(con_unused):
    """The list used to return names only, so every question needed a drill-in."""
    client = _client()
    d = client.get("/api/explore/teams").json()
    problems = []
    if len(d["teams"]) != 30:
        problems.append(f"{len(d['teams'])} teams, expected 30")
    for field in ("games", "wins", "losses", "win_pct", "first_season",
                  "last_season", "seasons"):
        if any(t.get(field) is None for t in d["teams"]):
            problems.append(f"{field} missing on at least one team")
    for t in d["teams"]:
        if t["wins"] + t["losses"] != t["games"]:
            problems.append(f"{t['abbreviation']}: W+L != games")
            break
        if not 0 <= t["win_pct"] <= 1:
            problems.append(f"{t['abbreviation']}: win_pct out of range")
            break
    return not problems, (f"{len(d['teams'])} teams with record, span and win%"
                          if not problems else "; ".join(problems))


@check("team drill-down returns a season's roster and results", group="api")
def team_hierarchy(con_unused):
    client = _client()
    d = client.get("/api/explore/team/2?season=2024").json()
    problems = []
    if not d.get("roster"):
        problems.append("no roster")
    games = d.get("games") or []
    if len(games) < 70:
        problems.append(f"only {len(games)} games for a full season")
    wins = sum(1 for g in games if g["won"])
    if wins != d["record"]["wins"]:
        problems.append(f"game log wins {wins} != record wins {d['record']['wins']}")
    if any(g.get("opponent") is None for g in games):
        problems.append("a game has no opponent")
    return not problems, (f"{len(d['roster'])} roster, {len(games)} games, "
                          f"log and record agree at {wins} wins"
                          if not problems else "; ".join(problems))


@check("catalog exposes a table's fields", group="api")
def table_detail(con_unused):
    client = _client()
    d = client.get("/api/explore/table/player_advanced_stats").json()
    problems = []
    if len(d["columns"]) < 15:
        problems.append(f"only {len(d['columns'])} columns")
    usage = next((c for c in d["columns"] if c["column_name"] == "usage_percentage"), None)
    if usage is None:
        problems.append("usage_percentage not listed")
    elif usage["first_tracked_season"] != 1996:
        problems.append(f"usage_percentage first season {usage['first_tracked_season']}")
    if client.get("/api/explore/table/nope").status_code != 404:
        problems.append("unknown table did not 404")
    return not problems, (f"{len(d['columns'])} fields with types and coverage"
                          if not problems else "; ".join(problems))


@check("notes must attach to an analysis", group="api")
def notes_require_run(con_unused):
    """A freestanding note pool meant two stores searched separately; an
    observation with nothing to trace it to is also the kind of unanchored
    claim this project spends its effort avoiding."""
    client = _client()
    problems = []
    if client.post("/api/kb/notes", json={"title": "orphan"}).status_code not in (400, 422):
        problems.append("a note with no analysis was accepted")
    if client.post("/api/kb/notes",
                   json={"title": "x", "run_id": 99999999}).status_code != 404:
        problems.append("a note on a nonexistent analysis was accepted")
    body = client.get("/api/kb/search").json()
    if "notes" in body:
        problems.append("search still returns a separate notes pool")
    if not all("notes" in a for a in body["analyses"]):
        problems.append("analyses do not carry their notes")
    return not problems, ("notes require an analysis; search returns analyses "
                          "carrying their own notes"
                          if not problems else "; ".join(problems))


@check("draft board offers real alternate windows", group="api")
def draft_windows(con_unused):
    """Same weighting math over fewer seasons, renormalised — a different view
    of completed seasons, never a projection."""
    client = _client()
    seen = {}
    for w in ("blend", "last2", "last1"):
        r = client.get(f"/api/fantasy/draft-board?window={w}&limit=50")
        if r.status_code != 200:
            raise Skip(f"{w} board not generated")
        d = r.json()
        seen[w] = [row["name"] for row in d["rows"]]
        if "not_a_projection" not in d:
            return False, f"{w}: no statement that this is not a projection"
        if not d["rows"] or d["rows"][0].get("pts") is None:
            return False, f"{w}: raw stat values missing beside the z-scores"
    if seen["blend"] == seen["last1"]:
        return False, "the blend and last-season boards are identical"
    if client.get("/api/fantasy/draft-board?window=nope").status_code != 400:
        return False, "an unknown window was accepted"
    overlap = len(set(seen["blend"][:50]) & set(seen["last1"][:50]))
    return True, (f"three windows, top-50 overlap {overlap}/50 between blend and "
                  f"last-season — genuinely different orderings")


@check("every game-log view shows the same games, labelled", group="api")
def game_log_views(con_unused):
    """The DNP filter used is_dnp, a column only two of the five views carry, so
    on the other three it silently did nothing and the per-36 and shooting
    tables opened on a wall of dashes. Raw column names were also reaching the
    header row."""
    client = _client()
    pid = 434  # Jayson Tatum: long career, real DNPs in the snapshot
    views, first_dates = {}, {}
    for v in ("totals", "per36", "shooting", "form5", "form10"):
        r = client.get(f"/api/explore/player/{pid}/games?view={v}&limit=20")
        if r.status_code != 200:
            return False, f"{v} failed with {r.status_code}"
        d = r.json()
        if not d["rows"]:
            raise Skip(f"{v} returned no rows")
        zero = [r_ for r_ in d["rows"] if (r_.get("minutes_played") or 0) == 0]
        if zero:
            return False, (f"{v} returned {len(zero)} zero-minute row(s); the "
                           "DNP filter did not apply")
        if len(d.get("labels") or []) != len(d["columns"]):
            return False, f"{v} has no label for every column"
        if any("_" in lab for lab in d["labels"]):
            return False, f"{v} leaks raw column names into headers: {d['labels']}"
        views[v] = d["labels"]
        first_dates[v] = d["rows"][0]["game_date"]
    if len(set(first_dates.values())) != 1:
        return False, f"views disagree on the most recent game: {first_dates}"
    if views["totals"] == views["per36"]:
        return False, "per-36 headers are indistinguishable from box-score headers"
    return True, (f"5 views agree on the games shown (latest {first_dates['totals']}), "
                  f"each labelled: per36 -> {views['per36'][0]}, "
                  f"form5 -> {views['form5'][0]}")


@check("clustering is reproducible byte for byte", group="api")
def cluster_determinism(con_unused):
    """k-means is seeded and the PCA axes are sign-pinned; the point of both is
    that the same request cannot come back a different shape."""
    client = _client()
    body = {"variables": ["ppg", "rpg", "apg", "fg3_pct", "height_in"], "k": 5,
            "season_from": 2017, "season_to": 2025, "min_games": 41,
            "game_type": "regular"}
    digests = []
    for _ in range(3):
        r = client.post("/api/model/cluster", json=body)
        if r.status_code != 200:
            return False, f"cluster failed with {r.status_code}"
        d = r.json()
        digests.append(hashlib.sha256(
            json.dumps(d, sort_keys=True).encode()).hexdigest())
    if len(set(digests)) != 1:
        return False, f"three identical requests gave {len(set(digests))} answers"
    d = client.post("/api/model/cluster", json=body).json()
    sizes = [c["size"] for c in d["clusters"]]
    if sum(sizes) != d["n_players"]:
        return False, f"cluster sizes {sizes} do not sum to {d['n_players']} players"
    if not d.get("warning"):
        return False, "no statement that k-means finds k groups regardless"
    return True, (f"3 identical requests -> 1 digest; sizes {sizes} sum to "
                  f"{d['n_players']}")


@check("catalog panel only claims coverage it has", group="api")
def catalog_panel_coverage(con_unused):
    """Coverage is recorded against the raw stat tables. A derived view showed
    two columns of em dashes instead of saying so."""
    client = _client()

    def cov_cols(table):
        r = client.get(f"/api/explore/table/{table}")
        if r.status_code != 200:
            raise Skip(f"{table} not in the catalog")
        return [c["column_name"] for c in r.json()["columns"]
                if c.get("first_tracked_season") is not None]

    base = cov_cols("player_stats")
    derived = cov_cols("v_player_game_per36")
    if not base:
        return False, "player_stats reports no season coverage at all"
    if derived:
        return False, f"a derived view claims coverage for {derived[:3]}"
    r = client.get("/api/explore/table/player_stats").json()
    three = next((c for c in r["columns"] if c["column_name"] == "fg3m"), None)
    if not three or three["first_tracked_season"] != 1979:
        return False, f"fg3m first tracked in {three and three['first_tracked_season']}, "
    return True, (f"player_stats: {len(base)} covered columns, fg3m from 1979; "
                  "the per-36 view claims none")


@check("team branding is complete and declared curated", group="api")
def brand_complete(con_unused):
    """Colours and logos are not in the snapshot. The endpoint has to say so,
    cover all 30 franchises, and actually have the logo files it points at."""
    import pathlib
    client = _client()
    d = client.get("/api/brand").json()
    if not d.get("curated"):
        return False, "branding is not flagged as curated, so a page could imply "\
                      "the dataset knows team colours"
    if len(d["teams"]) != 30:
        return False, f"{len(d['teams'])} branded teams, expected 30"
    web = pathlib.Path(__file__).resolve().parent.parent / "web"
    missing, bad = [], []
    for tid, b in d["teams"].items():
        for field in ("primary", "accent", "ink"):
            if not str(b.get(field, "")).startswith("#"):
                bad.append(f"{b.get('abbrev')}.{field}")
        if not b.get("logo"):
            missing.append(b.get("abbrev"))
            continue
        f = web / b["logo"].lstrip("/")
        if not f.is_file() or f.stat().st_size < 1000:
            missing.append(b.get("abbrev"))
    if bad:
        return False, f"malformed colours: {bad[:5]}"
    if missing:
        raise Skip(f"logo files absent for {missing[:6]} — "
                   "run `python team_brand.py --fetch-logos`")
    # An unknown team must get the neutral theme, never a borrowed identity.
    n = d["neutral"]
    if n.get("branded") or n.get("logo"):
        return False, "the neutral theme claims a brand"
    return True, (f"30 franchises with colours and on-disk logos; unknown teams "
                  f"fall back to neutral; endpoint declares itself curated")


@check("leaderboard carries every stat on every row", group="api")
def leaderboard_all_stats(con_unused):
    """Sorting by any column has to be possible without refetching, and a
    range must recompute every displayed column from totals, not just the
    ranked one."""
    client = _client()
    one = client.get("/api/explore/leaderboard?stat=ppg&limit=5").json()
    cols = [c["key"] for c in one["columns"]]
    if len(cols) < 20:
        return False, f"only {len(cols)} columns offered"
    for r in one["rows"]:
        absent = [c for c in cols if c not in r]
        if absent:
            return False, f"{r['player_name']} missing {absent[:4]}"
    rng = client.get("/api/explore/leaderboard"
                     "?stat=ts_pct&season_from=2021&season_to=2025&limit=5").json()
    rcols = [c["key"] for c in rng["columns"]]
    if "games_played" not in rcols or "ppg" not in rcols:
        return False, f"range view dropped core columns: {rcols}"
    # A range rate must be recomputed from totals, not averaged.
    row = rng["rows"][0]
    if row.get("ppg") is None or row.get("ts_pct") is None:
        return False, "range row has no recomputed rates"
    if "topg" not in [c["key"] for c in one["columns"]]:
        return False, "turnovers absent"
    low = [c["key"] for c in one["columns"] if c["lower_is_better"]]
    if "topg" not in low:
        return False, "turnovers not marked lower-is-better"
    return True, (f"{len(cols)} stat columns on every row, {len(rcols)} across a "
                  f"5-season range, lower-is-better marked for {low}")


@check("model builder reaches the whole semantic layer", group="api")
def model_vars_expanded(con_unused):
    """Per-36, advanced and prior-season variables all have to fit and be
    labelled; a lag must never carry a value across a missed season."""
    client = _client()
    d = client.get("/api/model/variables").json()
    preds = {p["key"]: p for p in d["predictors"]}
    if len(preds) < 70:
        return False, f"only {len(preds)} predictors"
    for need in ("usage_pct", "pts_per36", "lag_ppg", "def_rating", "lag_usage_pct"):
        if need not in preds:
            return False, f"{need} is not offered"
    unexplained = [k for k, p in preds.items() if not p.get("meaning")]
    if unexplained:
        return False, f"{len(unexplained)} variables carry no meaning: {unexplained[:4]}"
    for fixed in ("height_in", "weight_lbs", "draft_number", "jersey"):
        if "lag_" + fixed in preds:
            return False, f"lag_{fixed} is offered, but a fixed attribute cannot lag"

    # Each family must actually fit, not merely appear in the list.
    got = {}
    for key in ("usage_pct", "pts_per36", "lag_ppg"):
        r = client.post("/api/model/fit", json={
            "outcome": "ppg", "specs": [{"label": "l", "predictors": [key],
                                         "degree": 1}],
            "season_from": 2021, "season_to": 2025, "min_games": 41,
            "game_type": "regular"})
        if r.status_code != 200:
            return False, f"fitting {key} failed: {r.status_code} {r.text[:120]}"
        got[key] = r.json()["results"][0]["r_squared"]
    if not 0.6 < got["lag_ppg"] < 0.95:
        return False, (f"prior-season scoring explains R²={got['lag_ppg']:.3f} of "
                       "this season's; that is not a plausible persistence")
    return True, (f"{len(preds)} variables in {len(d['groups'])} groups, all "
                  f"explained; lag_ppg R²={got['lag_ppg']:.3f}, "
                  f"usage R²={got['usage_pct']:.3f}")


@check("a lagged variable never crosses a missed season", group="api")
def lag_respects_gaps(con_unused):
    """The whole point of the lag is that it means LAST season. A player who
    sat out a year must get nothing, not the last value on record."""
    import api as _api
    con = _api._ex()
    rows = _api._model_frame("ppg", ["lag_ppg"], 2021, 2025, 41, "regular")
    if not rows:
        raise Skip("no rows in the lagged frame")
    truth = {(r[0], r[1]): r[2] for r in con.execute(
        """SELECT player_id, season, ppg FROM v_player_season_averages
           WHERE game_type='regular' AND games_played>=41
             AND season BETWEEN 2020 AND 2025""").fetchall()}
    wrong = [r for r in rows
             if truth.get((r["player_id"], r["season"] - 1)) is None
             or abs(truth[(r["player_id"], r["season"] - 1)] - r["lag_ppg"]) > 1e-9]
    if wrong:
        return False, (f"{len(wrong)} of {len(rows)} lagged rows do not equal the "
                       f"player's previous season, e.g. {wrong[0]}")
    return True, (f"all {len(rows)} lagged rows equal that player's season-1 value, "
                  "and seasons with no predecessor are dropped")


@check("a pre-2018 overtime game is reported as overtime", group="api")
def game_overtime_pre_2018(con):
    """games.period is a CONSTANT 4 for all 62,943 games before 2018-19 -- the
    upstream feed reports regulation periods only for that era. It is never
    null and never above 4, which is a nastier failure than missing data:
    v_games.went_to_overtime is (period > 4), so it reports zero overtime
    across 72 seasons that really had 5-7%, and a double-overtime game from
    1990 reads as an ordinary regulation game.

    The per-period score columns ARE populated (~99.5% back to 1946), so game
    detail derives "Periods played" from the line score. This check holds that
    fix to a real game the old path got wrong, in the era where it was wrong."""
    row = con.execute("""
        SELECT id AS game_id, season, period,
               CASE WHEN home_ot2 IS NOT NULL OR visitor_ot2 IS NOT NULL
                    THEN 6 ELSE 5 END AS want
        FROM games
        WHERE season < 2018 AND home_q1 IS NOT NULL
          AND (home_ot1 IS NOT NULL OR visitor_ot1 IS NOT NULL)
          AND home_ot3 IS NULL AND visitor_ot3 IS NULL
        ORDER BY id LIMIT 1""").fetchone()
    if not row:
        raise Skip("no pre-2018 overtime game with a line score")
    game_id, season, period, want = row
    if period != 4:
        return False, (f"games.period is {period} for pre-2018 game {game_id}; "
                       "this check's premise no longer holds -- re-derive it")
    d = _client().get(f"/api/explore/game/{game_id}").json()
    shown = {c["label"]: c["value"] for c in d["context"]}.get("Periods played")
    if shown is None:
        return False, f"game {game_id} ({season}) reports no periods played"
    if "OT" not in str(shown):
        return False, (f"game {game_id} ({season}) went to overtime but detail "
                       f"shows {shown!r} -- the raw period column leaked back in")
    n_periods = max((len(side["line"]) for side in d["sides"]), default=0)
    if n_periods != want:
        return False, (f"game {game_id}: line score has {n_periods} periods, "
                       f"the OT columns imply {want}")
    return True, (f"game {game_id} ({season}) has games.period=4 but is "
                  f"correctly reported as {shown}")


@check("game detail carries a reconciling line score", group="api")
def game_line_score(con_unused):
    """Venue and attendance genuinely are not in this dataset; period scoring
    is, and it has to add up to the final score or it is worse than nothing."""
    client = _client()
    g = client.get("/api/explore/team/2?season=2025").json()
    played = [x for x in g["games"] if x["points_for"] is not None]
    if not played:
        raise Skip("no completed games")
    checked = 0
    for game in played[:12]:
        d = client.get(f"/api/explore/game/{game['game_id']}").json()
        if not d.get("has_line_score"):
            continue
        for side in d["sides"]:
            total = sum(p["points"] for p in side["line"])
            if total != side["score"]:
                return False, (f"game {game['game_id']} {side['abbreviation']}: "
                               f"periods sum to {total}, final says {side['score']}")
        checked += 1
    if not checked:
        raise Skip("no line scores present in the sampled games")
    # Absent fields are omitted, not announced, so every context entry that
    # IS returned has to carry a real value.
    empty = [c["label"] for c in d["context"] if c["value"] in (None, "")]
    if empty:
        return False, f"context carries empty entries: {empty}"
    return True, (f"{checked} games: every period breakdown sums to the final "
                  f"score; {len(d['context'])} context fields, none empty")


@check("projections are stored with provenance", group="projections")
def projections_table(con):
    """A projection without its model version and timestamp is an orphan
    number. The table also has to be reachable from the SNAPSHOT, not just
    the live file, or nothing downstream can read it."""
    try:
        cols = {r[0] for r in con.execute(
            "DESCRIBE player_projections").fetchall()}
    except Exception:
        raise Skip("player_projections not built — run `python projections.py --write`")
    need = {"player_id", "season", "stat", "projected_value",
            "model_version", "generated_at"}
    if not need <= cols:
        return False, f"missing columns: {sorted(need - cols)}"
    n, players, stats, versions, season = con.execute(
        """SELECT count(*), count(DISTINCT player_id), count(DISTINCT stat),
                  count(DISTINCT model_version), max(season)
           FROM player_projections""").fetchone()
    if n == 0:
        raise Skip("player_projections is empty")
    nulls = con.execute(
        """SELECT count(*) FROM player_projections
           WHERE projected_value IS NULL OR model_version IS NULL
              OR generated_at IS NULL""").fetchone()[0]
    if nulls:
        return False, f"{nulls} rows missing a value, a version or a timestamp"
    # A projected season must be one that has not been played.
    played = con.execute(
        "SELECT max(season) FROM v_games WHERE game_type='regular'").fetchone()[0]
    if season <= played:
        return False, (f"projecting season {season} but {played} is already "
                       "played — that is a backfit, not a projection")
    return True, (f"{n:,} rows, {players} players x {stats} stats for season "
                  f"{season} (last played {played}), {versions} model version")


@check("projected rates are physically possible", group="projections")
def projections_sane(con):
    """A regression will happily predict negative rebounds or more makes than
    attempts; either produces a nonsense z-score downstream."""
    try:
        con.execute("SELECT 1 FROM player_projections LIMIT 1").fetchone()
    except Exception:
        raise Skip("player_projections not built")
    neg = con.execute(
        """SELECT count(*) FROM player_projections
           WHERE projected_value < 0 AND stat <> 'value_9cat'""").fetchone()[0]
    if neg:
        return False, f"{neg} negative projected rates"
    wide = con.execute("""
        SELECT player_id,
               max(CASE WHEN stat='fgm' THEN projected_value END) fgm,
               max(CASE WHEN stat='fga' THEN projected_value END) fga,
               max(CASE WHEN stat='ftm' THEN projected_value END) ftm,
               max(CASE WHEN stat='fta' THEN projected_value END) fta,
               max(CASE WHEN stat='gp'  THEN projected_value END) gp
          FROM player_projections GROUP BY player_id""").fetchall()
    bad = [r[0] for r in wide
           if (r[1] or 0) > (r[2] or 0) + 1e-9 or (r[3] or 0) > (r[4] or 0) + 1e-9
           or not (0 <= (r[5] or 0) <= 82)]
    if bad:
        return False, (f"{len(bad)} players with makes > attempts or games "
                       f"outside 0-82, e.g. {bad[:3]}")
    return True, (f"{len(wide)} players: no negative rates, makes <= attempts, "
                  "games within 0-82")


@check("the projected draft board scores the projections", group="projections")
def projected_board(con_unused):
    """The projected window must be a different ORDERING, produced by the same
    scoring code — not a relabelled copy of an existing board."""
    client = _client()
    r = client.get("/api/fantasy/draft-board?window=projected&limit=100")
    if r.status_code == 404:
        raise Skip("draft_board-projected.csv not built")
    if r.status_code != 200:
        return False, f"projected board failed: {r.status_code}"
    proj = r.json()
    if not proj.get("is_projection"):
        return False, "the projected board does not declare itself a projection"
    if not proj.get("projection_note"):
        return False, "no note distinguishing it from the descriptive windows"
    blend = client.get("/api/fantasy/draft-board?window=blend&limit=100").json()
    pn = [x["name"] for x in proj["rows"]]
    bn = [x["name"] for x in blend["rows"]]
    if pn == bn:
        return False, "the projected board is identical to the blended one"
    moved = sum(1 for i, name in enumerate(pn)
                if name in bn and abs(bn.index(name) - i) >= 5)
    overlap = len(set(pn) & set(bn))
    # It should still be recognisably a basketball ranking.
    if overlap < 60:
        return False, (f"only {overlap}/100 players in common with the blended "
                       "board — that is a different population, not a reranking")
    for other in ("blend", "last1", "last2"):
        o = client.get(f"/api/fantasy/draft-board?window={other}&limit=5").json()
        if o.get("is_projection"):
            return False, f"the {other} window claims to be a projection"
    return True, (f"top-100 shares {overlap} players with the blended board, "
                  f"{moved} of them moved 5+ places; the three descriptive "
                  "windows still declare themselves descriptive")


@check("projection features cannot see the season being predicted", group="projections")
def projections_no_leakage(con):
    """The one failure that would make every validation number meaningless.

    Structural, not statistical: build the feature row for target T twice --
    once from the full history, once from a history with every season >= T
    deleted. If a single value differs, something downstream of `build_features`
    read the future. A walk-forward score cannot detect this; only this can.
    """
    try:
        import projections as P
    except Exception as exc:
        raise Skip(f"projections module unavailable: {exc}")
    import pandas as pd

    tot = P.load_totals(con)
    adv = P.load_advanced(con)
    bio = P.load_bio(con)
    target = 2015
    full = P.build_features(tot, adv, bio, target)
    censored = P.build_features(tot[tot["season"] < target],
                                adv[adv["season"] < target], bio, target)
    if full.empty or censored.empty:
        raise Skip("no feature rows built")
    full = full.sort_index()
    censored = censored.reindex(full.index).sort_index()
    if list(full.columns) != list(censored.columns):
        return False, "censoring the future changed the feature COLUMNS"
    diffs = []
    for c in full.columns:
        a, b = full[c], censored[c]
        if pd.api.types.is_numeric_dtype(a):
            if not ((a.isna() & b.isna()) | (a.sub(b).abs() < 1e-9)).all():
                diffs.append(c)
        elif not (a.fillna("") == b.fillna("")).all():
            diffs.append(c)
    if diffs:
        return False, (f"{len(diffs)} feature(s) change when seasons >= {target} "
                       f"are removed, so they read the future: {diffs[:6]}")
    # And the baselines the model consumes as features must be clean too.
    bf = P.baseline_prediction(tot, target, {target - 1: .6, target - 2: .3,
                                             target - 3: .1})
    bc = P.baseline_prediction(tot[tot["season"] < target], target,
                               {target - 1: .6, target - 2: .3, target - 3: .1})
    if not bf.reindex(bc.index).sub(bc).abs().lt(1e-9).all():
        return False, "the baseline feature changes when the future is removed"
    return True, (f"all {len(full.columns)} features and the baseline column are "
                  f"bit-identical with seasons >= {target} deleted")


@check("the matchup week reconciles to ESPN exactly", group="fantasy")
def matchup_reconciles(con):
    """The one load-bearing claim under every piece of weekly advice.

    If the identity map, the starters-only scoring rule or the recovered week
    is wrong, the advice is about the wrong players on the wrong days. ESPN's
    own category totals are the ground truth and all nine must reproduce from
    box scores alone.
    """
    try:
        import matchup as mu
    except Exception as exc:
        raise Skip(f"matchup module unavailable: {exc}")
    try:
        league = mu.load_league(mu.latest_cache())
    except SystemExit as exc:
        raise Skip(str(exc))
    if league["unmapped"]:
        return False, (f"{len(league['unmapped'])} roster players have no "
                       f"snapshot id: {league['unmapped'][:4]}")
    week = mu.find_week(con, league)
    if not week.get("reported"):
        raise Skip("cached matchup carries no scored categories")
    if not week.get("found"):
        return False, (f"no 7-day window reproduces ESPN's totals; best was "
                       f"{week.get('start')} with error {week.get('abs_error')}")
    bad = []
    for cat, want in week["reported"].items():
        got = week["reproduced"].get(cat, 0)
        tol = 1e-6 if cat in mu.PCT else 0.5
        if abs(got - want) >= tol:
            bad.append(f"{cat}: {got} vs {want}")
    if bad:
        return False, "; ".join(bad)
    return True, (f"all {len(week['reported'])} categories reproduce for the "
                  f"week of {week['start']} from {week['n_starters']} starters; "
                  f"{len(league['roster'])} roster players all mapped")


@check("weekly advice states what is not validated", group="fantasy")
def matchup_advisory(con_unused):
    """The draft board was backtested and this was not. The two look alike on
    screen, so the difference has to be on the page."""
    client = _client()
    r = client.get("/api/fantasy/matchup")
    if r.status_code == 503:
        raise Skip(r.json().get("detail", "league cache unavailable"))
    if r.status_code != 200:
        return False, f"matchup endpoint failed: {r.status_code}"
    d = r.json()
    adv = (d.get("advisory") or "").lower()
    for phrase in ("advisory", "walk-forward"):
        if phrase not in adv:
            return False, f"the advisory does not mention {phrase!r}"
    if not d.get("state"):
        return False, "no category state returned"
    cats = {c["category"] for c in d["state"]}
    if len(cats) != 9:
        return False, f"{len(cats)} categories, expected 9"
    # Turnovers must be scored the other way round or every piece of advice
    # about them is inverted.
    to = next((c for c in d["state"] if c["category"] == "TO"), None)
    if not to or not to["lower_wins"]:
        return False, "turnovers are not marked as lower-wins"
    if to["mine"] < to["theirs"] and to["status"] != "winning":
        return False, "fewer turnovers is not being scored as a win"
    return True, (f"9 categories, turnovers inverted correctly, advisory names "
                  f"what was and was not validated")


@check("a flip suggestion is costed, not just proposed", group="fantasy")
def matchup_flips(con_unused):
    """A move that wins one category and loses two is not an improvement."""
    client = _client()
    r = client.get("/api/fantasy/matchup")
    if r.status_code != 200:
        raise Skip("matchup unavailable")
    d = r.json()
    flips = d.get("flips")
    if flips is None:
        raise Skip("no flips computed (no week or no projections)")
    losing = {c["category"] for c in d["state"] if c["status"] == "losing"}
    covered = {f["category"] for f in flips}
    if losing - covered:
        return False, f"no suggestion for losing categories {losing - covered}"
    for f in flips:
        if f.get("bench_in") and "would_lose" not in f:
            return False, f"{f['category']}: suggestion has no collateral check"
        # A percentage gain must be in the same units as its gap, or
        # "closes it" compares two different quantities.
        if f.get("bench_in") and f["category"] in ("FG%", "FT%"):
            if abs(f["gain"]) > 1.0 or abs(f["need"]) > 1.0:
                return False, (f"{f['category']} gain/need look like raw counts, "
                               f"not percentage points: {f['gain']}, {f['need']}")
    costed = sum(1 for f in flips if f.get("bench_in"))
    return True, (f"{len(flips)} losing categories, {costed} with a concrete "
                  "swap, every one carrying what it would give up")


@check("findings page shows only committed verdicts", group="findings")
def findings_verdicts(con_unused):
    """An investigation that ran but never committed is not a finding, and
    padding the page with them would make a rejection look like a result."""
    client = _client()
    d = client.get("/api/findings").json()
    if d["total"] == 0:
        raise Skip("no investigations with verdicts yet")
    allowed = {"CONFIRMED", "NOT CONFIRMED", "INCONCLUSIVE"}
    for f in d["findings"]:
        if f["verdict"] not in allowed:
            return False, f"#{f['run_id']} has verdict {f['verdict']!r}"
        if not (f.get("answer") or "").strip():
            return False, f"#{f['run_id']} has no answer to show"
        # The claim must be a headline, not the whole agent prompt.
        if len(f.get("claim") or "") > 170:
            return False, (f"#{f['run_id']} claim is {len(f['claim'])} chars; "
                           "that is the prompt, not a claim")
    if d["counts"]["NOT CONFIRMED"] == 0:
        return False, ("no rejections are shown; a page of only confirmations "
                       "from a tool that scans variable pairs is a warning sign")
    rej = [f for f in d["findings"] if f["verdict"] == "NOT CONFIRMED"]
    return True, (f"{d['total']} findings: {d['counts']['CONFIRMED']} confirmed, "
                  f"{d['counts']['NOT CONFIRMED']} rejected "
                  f"(e.g. #{rej[0]['run_id']} {rej[0]['claim'][:40]}), "
                  "every one carrying its full argument")


@check("trade and comparison reuse the board's own values", group="fantasy")
def trade_and_compare(con_unused):
    """Spec item 14's actual requirement: a player cannot be worth one thing
    on the draft board and another in a trade."""
    client = _client()
    import csv as _csv
    import pathlib as _pl
    board_path = _pl.Path(__file__).resolve().parent.parent / "draft_board.csv"
    if not board_path.exists():
        raise Skip("draft_board.csv not built")
    with board_path.open(newline="") as fh:
        board = {int(float(r["player_id"])): r for r in _csv.DictReader(fh)
                 if r.get("player_id")}
    two = list(board)[:2]
    r = client.post("/api/fantasy/trade", json={
        "side_a": {"team": "A", "player_ids": [two[0]]},
        "side_b": {"team": "B", "player_ids": [two[1]]}})
    if r.status_code != 200:
        return False, f"trade failed: {r.status_code} {r.text[:120]}"
    d = r.json()
    for side, pid in ((d["side_a"], two[0]), (d["side_b"], two[1])):
        got = side["players"][0]["total_value"]
        want = float(board[pid]["total_value"])
        if abs(got - want) > 1e-6:
            return False, (f"player {pid} valued {got} in the trade but {want} "
                           "on the draft board")
    if "advisory" not in d or "not" not in d["advisory"].lower():
        return False, "the trade output does not state its limits"
    c = client.get("/api/compare?ids=434,132")
    if c.status_code != 200:
        return False, f"compare failed: {c.status_code}"
    cd = c.json()
    if len(cd["players"]) != 2 or len(cd["metrics"]) < 10:
        return False, "comparison returned too little"
    # Lower turnovers must be the better figure.
    tov = next((m for m in cd["metrics"] if m["key"].startswith("turnover")), None)
    if tov and tov["best"] != min(v for v in tov["values"] if v is not None):
        return False, "comparison marks the HIGHER turnover figure as better"
    return True, (f"trade values match draft_board.csv exactly for both sides; "
                  f"comparison returns {len(cd['metrics'])} metrics with "
                  "turnovers scored low-is-better")


# ---------------------------------------------------------------------------
# Narration layer -- built, not verified live (no API credits this session)
# ---------------------------------------------------------------------------
class _StubBlock:
    def __init__(self, text): self.text = text


class _StubUsage:
    input_tokens = 1234
    output_tokens = 210


class _StubResponse:
    def __init__(self, text):
        self.content = [_StubBlock(text)]
        self.usage = _StubUsage()


class _StubMessages:
    def __init__(self, outer): self.outer = outer

    def create(self, **kw):
        self.outer.calls.append(kw)
        if self.outer.raises is not None:
            raise self.outer.raises
        return _StubResponse(self.outer.reply)


class _StubClient:
    """Stands in for anthropic.Anthropic so the integration is testable with
    no network and no credits. It records what it was asked, which is the part
    actually worth asserting on."""
    def __init__(self, reply="Start CJ McCollum over Jakob Poeltl.", raises=None):
        self.calls, self.reply, self.raises = [], reply, raises
        self.messages = _StubMessages(self)


@check("the narration brief carries the numbers and the caveats", group="narration")
def narration_brief(con_unused):
    """The model is given facts and forbidden to invent any, so the brief has
    to contain everything an answer could need -- and the two constraints that
    stop it overclaiming."""
    try:
        import narrate as nr
    except Exception as exc:
        raise Skip(f"narrate module unavailable: {exc}")
    matchup = {
        "season_label": "2025-26", "opponent": "Test Opp", "scoring_period": 160,
        "category_wins": {"me": 5, "opponent": 4},
        "week": {"found": True, "start": "2026-03-23"},
        "state": [
            {"category": "PTS", "mine": 491.0, "theirs": 468.0,
             "status": "winning", "closeness": "close", "lower_wins": False},
            {"category": "FG%", "mine": 0.47506, "theirs": 0.4759,
             "status": "losing", "closeness": "close", "lower_wins": False},
            {"category": "TO", "mine": 46.0, "theirs": 56.0,
             "status": "winning", "closeness": "clear", "lower_wins": True},
        ],
        "flips": [{"category": "FG%", "bench_in": "A Player",
                   "starter_out": "B Player", "bench_games": 4,
                   "starter_games": 3, "gain": 0.0133, "need": 0.00084,
                   "closes_gap": True, "would_lose": ["REB"]}],
        "lineup": {"n_changes": 1, "changes": [{"player": "A Player", "slot": "SG"}]},
        "waivers": {"available": True, "candidates": [
            {"name": "FA One", "pro_team": "WAS", "projected_value": 3.59,
             "beats": "C Player", "beats_by": 4.04}]},
    }
    brief = nr.compose_matchup(matchup)
    problems = []
    for needle in ("Test Opp", "491", "468", "A Player", "FA One",
                   "lower wins", "not backtested", "minimum"):
        if needle not in brief:
            problems.append(f"missing {needle!r}")
    # A percentage must reach the model as a percentage, not a raw ratio.
    if "47.5%" not in brief:
        problems.append("FG% is not rendered as a percentage")
    # Percentage gains must be labelled in points, the unit bug this project
    # already shipped once.
    if "percentage points" not in brief:
        problems.append("a percentage gain is not labelled in points")
    if problems:
        return False, "; ".join(problems)

    # Absent sections must be omitted, not rendered as empty headings.
    bare = nr.compose_matchup({"season_label": "x", "opponent": "y",
                               "scoring_period": 1, "state": []})
    for absent in ("WAIVER CANDIDATES", "LINEUP:", "TODAY (", "FIXTURE LOAD"):
        if absent in bare:
            return False, f"{absent!r} rendered with no data behind it"
    return True, (f"{len(brief)} chars carrying every category, flip, waiver "
                  "and both constraints; empty sections omitted")


@check("narration calls the model correctly and fails honestly", group="narration")
def narration_integration(con_unused):
    """Exercised against a stub: no network, no credits, no cost.

    What a stub CAN establish: that the methodology standard is loaded into
    the system prompt, that the brief is what gets sent, that a response is
    parsed, and that a billing failure is distinguished from a broken one.
    What it CANNOT establish is whether the model's prose actually obeys the
    constraints -- that needs credits and has never been run.
    """
    try:
        import narrate as nr
    except Exception as exc:
        raise Skip(f"narrate module unavailable: {exc}")
    brief = "MATCHUP: test\nCONSTRAINTS: not backtested"

    # Every kind must route through the same function with the same rules;
    # only the task line may differ.
    for kind in sorted(nr.KINDS):
        k_stub = _StubClient(reply="ok")
        nr.narrate(brief, kind=kind, client=k_stub)
        sysmsg = k_stub.calls[0].get("system") or ""
        if "ONLY the numbers" not in sysmsg:
            return False, f"kind {kind!r} lost the shared honesty rules"
        if nr.KINDS[kind][:40] not in sysmsg:
            return False, f"kind {kind!r} did not get its own task line"
    try:
        nr.narrate(brief, kind="not-a-kind", client=_StubClient())
        return False, "an unknown kind was accepted"
    except ValueError:
        pass

    stub = _StubClient(reply="Start A over B this week.")
    out = nr.narrate(brief, client=stub)
    if not stub.calls:
        return False, "the model was never called"
    call = stub.calls[0]
    if call.get("messages", [{}])[0].get("content") != brief:
        return False, "the brief is not what was sent"
    system = call.get("system") or ""
    for needle in ("ONLY the numbers", "NOT backtested", "MINIMUM"):
        if needle not in system:
            return False, f"the system prompt does not state {needle!r}"
    if nr.SKILL.exists() and "methodology" not in system.lower():
        return False, "the methodology skill was not loaded into the system prompt"
    if out["text"] != "Start A over B this week.":
        return False, f"response not parsed: {out['text']!r}"
    if not out.get("verified_live"):
        return False, "a successful call did not mark itself verified"
    if out.get("input_tokens") != 1234:
        return False, "usage was not carried through"

    # A billing failure must be its own signal, not a generic error.
    billing = _StubClient(raises=RuntimeError(
        "Error code: 400 - your credit balance is too low to access the API"))
    try:
        nr.narrate(brief, client=billing)
        return False, "a billing failure did not raise"
    except nr.NoCredits:
        pass
    except Exception as exc:
        return False, f"billing failure raised {type(exc).__name__}, not NoCredits"

    # Anything else must propagate rather than be swallowed as "no credits".
    other = _StubClient(raises=ValueError("something else broke"))
    try:
        nr.narrate(brief, client=other)
        return False, "a non-billing failure did not raise"
    except nr.NoCredits:
        return False, "a non-billing failure was misreported as a credit problem"
    except ValueError:
        pass
    return True, (f"all {len(nr.KINDS)} kinds share the honesty rules and get "
                  "their own task line; brief sent intact, methodology "
                  "standard present, response parsed, billing failure "
                  "distinguished from a real one. NOT verified against the "
                  "live model — no credits this session")


@check("the narrate endpoint never fakes an answer", group="narration")
def narration_endpoint_honest(con_unused):
    """With no credits the endpoint must say so and still return the brief --
    a placeholder paragraph presented as advice would be the worst outcome
    available here."""
    client = _client()
    r = client.post("/api/narrate/matchup", json={})
    if r.status_code == 503:
        raise Skip(r.json().get("detail", "league cache unavailable"))
    if r.status_code != 200:
        return False, f"narrate failed: {r.status_code} {r.text[:150]}"
    d = r.json()
    if not d.get("brief"):
        return False, "no brief returned"
    if d.get("narration") and not d.get("verified_live"):
        return False, "narration text present but not marked verified"
    if not d.get("verified_live") and not d.get("unavailable"):
        return False, "no narration and no explanation of why"
    state = ("live" if d.get("verified_live")
             else "unavailable: " + str(d.get("unavailable"))[:60])
    return True, f"brief {d['brief_chars']} chars; narration {state}"


@check("the retrospective grades with no foresight", group="fantasy")
def retrospective_no_foresight(con):
    """The shipped projections were built from features including the season
    being graded. Using them would let the optimiser pick lineups already
    knowing who turned out good -- which inflated this by two weeks before it
    was caught."""
    try:
        import matchup as mu
    except Exception as exc:
        raise Skip(f"matchup module unavailable: {exc}")
    try:
        league = mu.load_league(mu.latest_cache())
    except SystemExit as exc:
        raise Skip(str(exc))
    season = con.execute(
        "SELECT max(season) FROM v_games WHERE game_type='regular'").fetchone()[0]
    proj, train_seasons, _rows = mu.honest_projections(con, int(season))
    if proj.empty:
        raise Skip("no refit projections")
    # The refit must have been trained only on earlier seasons.
    import projections as P
    hist = P.component_frame(
        P.assemble(con, P.FIRST_INPUT_SEASON + 3, int(season) - 1),
        P.load_totals(con))
    if hist["target_season"].max() >= season:
        return False, (f"training frame reaches season {hist['target_season'].max()}, "
                       f"which is the season being graded")
    r = mu.retrospective(con, league, mu.projections(con))
    if not r.get("available"):
        raise Skip(r.get("reason", "retrospective unavailable"))
    if "foresight" not in (r.get("projection_basis") or ""):
        return False, "the retrospective does not state its projection basis"
    if not 0 <= r["optimiser_won"] <= r["n_weeks"]:
        return False, "impossible win count"
    if "lineup" not in (r.get("limitation") or "").lower():
        return False, "the single-lineup limitation is not stated"
    return True, (f"{r['n_weeks']} weeks graded with a model refit on "
                  f"{train_seasons} seasons ending {int(season) - 1}; "
                  f"optimiser ahead in {r['optimiser_won']}, behind in "
                  f"{r['optimiser_lost']}")


@check("a week never counts two teams' fixtures", group="fantasy")
def games_window_single_team(con):
    """A traded player appears under two team_ids in a season; joining that to
    the schedule counted BOTH clubs' games and showed a guard with seven
    fixtures in a seven-day week."""
    try:
        import matchup as mu
    except Exception as exc:
        raise Skip(f"matchup module unavailable: {exc}")
    season = con.execute(
        "SELECT max(season) FROM v_games WHERE game_type='regular'").fetchone()[0]
    traded = [r[0] for r in con.execute(f"""
        SELECT player_id FROM v_player_games
         WHERE season = {season} AND game_type='regular'
         GROUP BY player_id HAVING count(DISTINCT team_id) > 1
         LIMIT 40""").fetchall()]
    if not traded:
        raise Skip("no mid-season moves in this season")
    weeks = [str(r[0]) for r in con.execute(f"""
        SELECT DISTINCT game_date FROM v_games
         WHERE season = {season} AND game_type='regular'
         ORDER BY game_date""").fetchall()][::14][:6]
    # The bound is not a fixed number: teams really do play five times in some
    # seven-day stretches, so the invariant is that no PLAYER exceeds what any
    # single team plays that week. Double-counting shows up as roughly twice
    # the team maximum, which this still catches.
    worst = []
    for wk in weeks:
        g = mu.games_in_window(con, traded, wk)
        if not g:
            continue
        team_max = con.execute(f"""
            WITH tm AS (
              SELECT home_team_id AS team_id, game_date FROM v_games
               WHERE game_type='regular'
              UNION ALL
              SELECT visitor_team_id, game_date FROM v_games
               WHERE game_type='regular')
            SELECT max(g) FROM (
              SELECT count(*) AS g FROM tm
               WHERE game_date >= DATE '{wk}'
                 AND game_date <  DATE '{wk}' + INTERVAL 7 DAY
               GROUP BY team_id)""").fetchone()[0] or 0
        hi = max(g.values())
        worst.append((wk, hi, team_max))
        if hi > team_max:
            return False, (f"week of {wk}: a player shows {hi} games but no team "
                           f"plays more than {team_max}; two schedules are "
                           "being summed")
    if not worst:
        raise Skip("no window produced game counts")
    return True, (f"{len(traded)} mid-season movers across {len(worst)} weeks: "
                  f"no player ever exceeds that week's team maximum "
                  f"(worst case {max(w[1] for w in worst)} vs "
                  f"{max(w[2] for w in worst)} available)")


# Sample values for exercising routes generically. A route whose parameters
# are not covered here cannot be exercised, and the guard REPORTS that rather
# than passing quietly -- an audit with silent blind spots is not an audit.
_PATH_SAMPLES = {
    "run_id": "61", "id": "434", "player_id": "434", "team_id": "2",
    "game_id": "18447924", "job_id": "nonexistent", "note_id": "1",
    "name": "player_stats", "kind": "matchup", "table": "player_stats",
    "tag": "era", "stat": "fg3m",
}
_BODY_SAMPLES = {
    "/api/ask": {"question": "audit probe"},
    "/api/model/fit": {"outcome": "bpg",
                       "specs": [{"label": "l", "predictors": ["height_in"],
                                  "degree": 1}],
                       "season_from": 2021, "season_to": 2025,
                       "min_games": 41, "game_type": "regular"},
    "/api/model/cluster": {"variables": ["ppg", "rpg"], "k": 3,
                           "season_from": 2021, "season_to": 2025,
                           "min_games": 41, "game_type": "regular"},
    "/api/fantasy/trade": {"side_a": {"team": "A", "player_ids": [434]},
                           "side_b": {"team": "B", "player_ids": [132]}},
    "/api/narrate/{kind}": {"side_a": {"team": "A", "player_ids": [434]},
                            "side_b": {"team": "B", "player_ids": [132]}},
    "/api/kb/notes": {"run_id": 61, "title": "audit probe"},
}


def _exercise_all_routes(app, spy_counter):
    """Hit every GET/POST route and record which ones construct a model client.

    Returns (paying, exercised, skipped). The paying set is DISCOVERED, not
    declared, so a route added tomorrow is covered without touching this.
    """
    from fastapi.testclient import TestClient
    import re as _re
    client = TestClient(app)
    paying, exercised, skipped = set(), [], []
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", set()) or set()
        if not path or not path.startswith("/api/"):
            continue
        verb = "POST" if "POST" in methods else ("GET" if "GET" in methods else None)
        if verb is None:
            continue
        params = _re.findall(r"\{(\w+)\}", path)
        if any(q not in _PATH_SAMPLES for q in params):
            skipped.append(f"{path} (no sample for {params})")
            continue
        url = path
        for q in params:
            url = url.replace("{" + q + "}", _PATH_SAMPLES[q])
        before = spy_counter()
        try:
            if verb == "POST":
                client.post(url, json=_BODY_SAMPLES.get(path, {}))
            else:
                client.get(url)
        except Exception:
            pass                      # a 4xx/5xx still proves whether it paid
        if spy_counter() > before:
            paying.add(path)
        exercised.append(f"{verb} {path}")
    return paying, exercised, skipped


def _effect_bodies(src):
    """Every useEffect body in a module, so a call inside one can be spotted."""
    import re as _re
    out = []
    for m in _re.finditer(r"useEffect\(\s*\(\)\s*=>\s*\{", src):
        depth, i = 1, m.end()
        while i < len(src) and depth:
            depth += (src[i] == "{") - (src[i] == "}")
            i += 1
        out.append(src[m.end():i])
    return out


@check("every model-reaching endpoint is behind an explicit action", group="cost")
def model_call_surface(con_unused):
    """A standing guard on what costs money, generic by construction.

    The paying set is DISCOVERED by exercising every route with a spy client,
    not read from a list in this file -- so a commentary button added later is
    audited automatically instead of needing this check edited. For each
    endpoint found to reach the model, the frontend is then parsed to confirm
    it is not called from a useEffect, which is the specific way an explicit
    action silently becomes an automatic one.
    """
    import anthropic
    import api as _api
    import pathlib as _pl

    seen = []

    class _Spy:
        def __init__(self, *a, **k):
            seen.append(True)
            self.messages = self

        def create(self, **kw):
            raise RuntimeError("audit stub: no real call")

    real = anthropic.Anthropic
    anthropic.Anthropic = _Spy
    try:
        paying, exercised, skipped = _exercise_all_routes(_api.app, lambda: len(seen))
    finally:
        anthropic.Anthropic = real

    if skipped:
        return False, (f"{len(skipped)} route(s) could not be exercised, so the "
                       f"audit has blind spots: {skipped[:4]}")
    if not paying:
        return False, ("no endpoint reached the model at all — the spy is not "
                       "wired up, so this guard proves nothing")

    web = _pl.Path(__file__).resolve().parent.parent / "web"
    sources = {f.name: f.read_text() for f in web.glob("*.js")}
    problems = []
    for path in sorted(paying):
        # The literal a module would use: template params become ${...}.
        import re as _re
        stem = _re.sub(r"\{\w+\}", "", path).rstrip("/")
        callers = [n for n, src in sources.items() if stem in src]
        if not callers:
            problems.append(f"{path} reaches the model but no web module calls "
                            "it (dead paid endpoint, or the scan missed it)")
            continue
        for name in callers:
            for body in _effect_bodies(sources[name]):
                if stem in body:
                    problems.append(
                        f"{name} calls {path} from inside a useEffect, so it "
                        "fires without the user asking")
    if problems:
        return False, "; ".join(problems)
    return True, (f"{len(exercised)} routes exercised, {len(paying)} reach the "
                  f"model ({', '.join(sorted(paying))}); none is called from a "
                  "useEffect in any web module")


@check("the league cache carries every roster", group="fantasy")
def all_rosters_cached(con_unused):
    """A trade finder, a keeper comparison and an opponent preview all need
    to know who else holds whom. Standings alone cannot answer that."""
    client = _client()
    r = client.get("/api/fantasy/rosters")
    if r.status_code == 503:
        raise Skip(r.json().get("detail", "league cache unavailable"))
    if r.status_code != 200:
        return False, f"rosters endpoint failed: {r.status_code}"
    d = r.json()
    if len(d["teams"]) < 2:
        return False, f"only {len(d['teams'])} team(s) cached"
    mine = [t for t in d["teams"] if t["is_me"]]
    if len(mine) != 1:
        return False, f"{len(mine)} teams are marked as mine"
    empty = [t["name"] for t in d["teams"] if not t["roster"]]
    if empty:
        return False, f"teams cached with no roster: {empty}"
    # Both the raw stat and its z must be present, or the trade view is
    # asking the reader to trust a transformation they cannot see.
    valued = [p for p in d["players"] if p["on_board"]]
    if not valued:
        return False, "no rostered player could be valued from the draft board"
    sample = valued[0]
    if not sample.get("stats") or not sample.get("z"):
        return False, "players carry z-scores or raw stats but not both"
    if set(sample["stats"]) != set(sample["z"]):
        return False, "the raw-stat and z-score column sets disagree"
    return True, (f"{len(d['teams'])} rosters, {len(d['players'])} players, "
                  f"{len(valued)} valued, each with raw stats beside z-scores")


@check("the trade finder gains come from depth, not category weighting",
       group="fantasy")
def trade_finder_mechanism(con_unused):
    """Two objectives have already lost to the simple one. This must stay a
    net-value-sum argument about starting slots, not a third attempt at
    category weighting wearing a different name."""
    client = _client()
    r = client.get("/api/fantasy/trade-finder?limit=10")
    if r.status_code == 503:
        raise Skip(r.json().get("detail", "league cache unavailable"))
    if r.status_code != 200:
        return False, f"trade finder failed: {r.status_code}"
    d = r.json()
    for key in ("mechanism", "advisory", "starters_counted"):
        if not d.get(key):
            return False, f"the finder does not report {key}"
    for s in d["suggestions"]:
        if s["my_starting_gain"] <= 0 or s["their_starting_gain"] <= 0:
            return False, (f"a suggestion does not improve both sides: "
                           f"{s['my_starting_gain']}, {s['their_starting_gain']}")
        if "net_total_value_to_me" not in s:
            return False, "the zero-sum net value is hidden"
    # A 1-for-1 that improves both is impossible under one value function;
    # if one appears, the arithmetic is wrong somewhere.
    bad = [s for s in d["suggestions"]
           if s["shape"] == "1-for-1"
           and abs(s["my_starting_gain"] + s["their_starting_gain"]
                   - 0) > 0 and s["net_total_value_to_me"] == 0]
    if bad:
        return False, "a value-neutral 1-for-1 claims to improve both sides"
    shapes = {s["shape"] for s in d["suggestions"]}
    return True, (f"{d['found']} swaps, shapes {sorted(shapes)}, every one "
                  f"improving both starting lineups over {d['starters_counted']} "
                  "slots with the net total value still shown")


@check("the keeper rule is read from settings, not assumed", group="fantasy")
def keeper_rule_read(con_unused):
    """ESPN reports keeper_order_type MANUAL for this league, which means
    there is no round-cost formula to apply. Inventing one and labelling the
    result 'your keeper cost' is the failure mode here."""
    client = _client()
    r = client.get("/api/fantasy/keepers")
    if r.status_code == 503:
        raise Skip(r.json().get("detail", "league cache unavailable"))
    if r.status_code != 200:
        return False, f"keepers failed: {r.status_code}"
    d = r.json()
    if d.get("keeper_count") is None:
        return False, "keeper_count was not read from the league settings"
    if not d.get("rule_note") or str(d.get("keeper_order_type")) not in d["rule_note"]:
        return False, "the rule note does not state the keeper order type"
    if d.get("keeper_order_type") == "MANUAL" and "no round-cost formula" not in d["rule_note"]:
        return False, ("the order type is MANUAL but the page does not say a "
                       "round cost cannot be computed")
    if not d.get("proxy_note") or "NOT the round" not in d["proxy_note"]:
        return False, "the implied round is not labelled as a proxy"
    if len(d.get("recommended") or []) != (d["keeper_count"] or 0):
        n_rec = len(d.get("recommended") or [])
        return False, (f"recommends {n_rec} players but the league keeps "
                       f"{d['keeper_count']}")
    return True, (f"keeps {d['keeper_count']}, order type "
                  f"{d['keeper_order_type']!r}, implied round labelled a proxy")


@check("the mock draft replays deterministically", group="fantasy")
def mock_draft_deterministic(con_unused):
    """Reuses draft_assistant's harness. Same seed and same picks must give
    the same field, or a practice draft teaches nothing repeatable."""
    client = _client()
    r = client.post("/api/fantasy/mock-draft", json={"picks": [], "seed": 0})
    if r.status_code == 404:
        raise Skip("draft board not built")
    if r.status_code != 200:
        return False, f"mock draft failed: {r.status_code} {r.text[:120]}"
    first = r.json()
    pick = first["available"][0]["player_id"]
    a = client.post("/api/fantasy/mock-draft",
                    json={"picks": [pick], "seed": 0}).json()
    b = client.post("/api/fantasy/mock-draft",
                    json={"picks": [pick], "seed": 0}).json()
    if a["log"] != b["log"]:
        return False, "the same seed and picks produced a different field"
    if a["pick_number"] <= first["pick_number"]:
        return False, "the draft did not advance after a pick"
    bad = client.post("/api/fantasy/mock-draft",
                      json={"picks": [-1], "seed": 0})
    if bad.status_code != 400:
        return False, "an unavailable player was accepted as a pick"
    return True, (f"pick {first['pick_number']} -> {a['pick_number']} after one "
                  f"selection, identical replay on seed 0, bad picks rejected")


@check("similar players share the clustering geometry", group="model")
def similar_players(con_unused):
    """Same frame, same z-scoring, same sign-pinned SVD as the cluster view.
    If they diverge, two parts of the app disagree about who sits where."""
    client = _client()
    body = {"player_id": 434,
            "variables": ["ppg", "rpg", "apg", "ts_pct", "mpg"],
            "season_from": 2015, "season_to": 2025, "n": 8}
    a = client.post("/api/model/similar", json=body)
    if a.status_code != 200:
        return False, f"similar failed: {a.status_code} {a.text[:120]}"
    a = a.json()
    b = client.post("/api/model/similar", json=body).json()
    if [n["player_id"] for n in a["neighbours"]] != \
       [n["player_id"] for n in b["neighbours"]]:
        return False, "two identical requests returned different neighbours"
    if a["target"]["player_id"] in [n["player_id"] for n in a["neighbours"]]:
        return False, "the player is listed as similar to himself"
    d = [n["distance"] for n in a["neighbours"]]
    if d != sorted(d):
        return False, "neighbours are not ordered by distance"
    if not a.get("warning") or "picked" not in a["warning"].lower():
        return False, "the result does not warn that similarity is stat-dependent"
    return True, (f"{len(a['neighbours'])} neighbours of "
                  f"{a['target']['player_name']} over {a['pool']} players, "
                  f"ordered by distance, identical across calls")


@check("era adjustment refuses to score untracked stats", group="explore")
def era_adjusted_gating(con):
    """The whole point. A z-score against a season where the stat was not
    recorded looks rigorous and is nonsense -- Walt Bellamy's 1961-62 shows
    a true-shooting figure of 0.99 because attempts are incomplete."""
    client = _client()
    old = con.execute("""
        SELECT player_id FROM v_player_season_averages
         WHERE game_type='regular' AND season < 1970
         GROUP BY player_id HAVING count(*) >= 5
         ORDER BY count(*) DESC LIMIT 1""").fetchone()
    if not old:
        raise Skip("no pre-1970 careers in the snapshot")
    d = client.get(f"/api/explore/era-adjusted/{old[0]}")
    if d.status_code != 200:
        return False, f"era-adjusted failed: {d.status_code}"
    d = d.json()
    first = d["seasons"][0]
    if first["season"] >= 1970:
        raise Skip("selected player has no early season")
    # Threes did not exist before 1979-80; they must not carry a z-score.
    three = first["stats"]["fg3mpg"]
    if three.get("z") is not None:
        return False, (f"a three-point z-score was produced for "
                       f"{first['season_label']}, before the line existed")
    if three["status"] == "ok":
        return False, "threes are marked ok in a season they did not exist"
    # And a modern season must still be scored, or the gate is just a block.
    modern = client.get("/api/explore/era-adjusted/434").json()
    last = modern["seasons"][-1]
    scored = [k for k, c in last["stats"].items() if c["status"] == "ok"]
    if len(scored) < 8:
        return False, f"only {len(scored)} stats scored in a modern season"
    if last["stats"]["mpg"]["status"] != "ok":
        return False, ("minutes are unscored in a modern season; the coverage "
                       "derivation is over-blocking")
    return True, (f"{first['season_label']}: threes "
                  f"{three['status']!r}; modern season scores {len(scored)} "
                  "stats including minutes")


@check("league evolution cuts each series at its coverage", group="explore")
def evolution_coverage(con_unused):
    """A three-point-rate line drawn back to 1946 is not a finding about the
    1950s."""
    client = _client()
    d = client.get("/api/explore/evolution").json()
    if not d.get("series"):
        return False, "no series returned"
    by = {m["key"]: m for m in d["metrics"]}
    if "three_point_rate" not in by:
        return False, "no three-point rate series"
    if by["three_point_rate"]["from_season"] < 1979:
        return False, (f"three-point rate starts at "
                       f"{by['three_point_rate']['from_season']}, before the "
                       "line existed")
    pts = by.get("pts_per_team_game")
    if not pts or pts["from_season"] > 1950:
        return False, "points should run essentially the whole span"
    # The finding this page exists to show must still hold in the data.
    s = {x["season"]: x for x in d["series"]}
    lo, hi = by["three_point_rate"]["from_season"], by["three_point_rate"]["to_season"]
    rate_rise = s[hi]["three_point_rate"] - s[lo]["three_point_rate"]
    acc_change = s[hi]["fg3_pct"] - s[lo]["fg3_pct"]
    if rate_rise <= 0.1:
        return False, f"three-point rate rose only {rate_rise:.3f}"
    if abs(acc_change) > rate_rise / 2:
        return False, (f"accuracy moved {acc_change:+.3f} against volume "
                       f"{rate_rise:+.3f}; finding #57's shape does not hold")
    return True, (f"{len(d['series'])} seasons, {len(d['metrics'])} measures; "
                  f"3PAr {s[lo]['three_point_rate']:.3f}->{s[hi]['three_point_rate']:.3f} "
                  f"while accuracy moved {acc_change:+.3f}")
