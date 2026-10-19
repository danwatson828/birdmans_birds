"""Live draft room checks.

None of these read the database: the draft room runs off the committed board
CSVs and ESPN's draft payload, so these hold whatever the snapshot contains.
The engine is JavaScript (the page runs it), so its checks drive it through
node and skip where node is not installed rather than re-implementing it here.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from .harness import Skip, check

ROOT = Path(__file__).resolve().parent.parent

# The shape of ESPN's mDraftDetail + mTeam + mSettings payload, cut to the
# fields the parser reads. Includes both traps: an unmade pick is playerId -1,
# and older team objects carry location + nickname instead of name.
ESPN_FIXTURE = {
    "settings": {"draftSettings": {"pickOrder": [7, 3, 10, 1], "keeperCount": 1}},
    "teams": [{"id": 7, "abbrev": "SEV", "name": "Seven Seconds"},
              {"id": 3, "abbrev": "THR", "location": "Three", "nickname": "Point"},
              {"id": 10, "abbrev": "ME", "name": "Birdman"},
              {"id": 1, "abbrev": "ONE"}],
    "draftDetail": {"drafted": False, "inProgress": True, "picks": [
        {"overallPickNumber": 2, "roundId": 1, "roundPickNumber": 2, "teamId": 3,
         "playerId": -1},
        {"overallPickNumber": 1, "roundId": 1, "roundPickNumber": 1, "teamId": 7,
         "playerId": 3112335},
        {"overallPickNumber": 7, "roundId": 2, "roundPickNumber": 3, "teamId": 3,
         "playerId": 4278073, "keeper": True},
    ]},
}


@check("live-draft parser reads ESPN's draft payload", group="draft")
def live_draft_parser(con_unused):
    import league_sync as ls
    d = ls.extract_live_draft(ESPN_FIXTURE)
    names = {t["team_id"]: t["name"] for t in d["teams"]}
    picks = d["picks"]
    problems = []
    if [p["overall"] for p in picks] != [1, 2, 7]:
        problems.append(f"picks not sorted by overall: {[p['overall'] for p in picks]}")
    if picks[1]["espn_player_id"] is not None:
        problems.append("an unmade pick (playerId -1) was read as made")
    if d["made"] != 2:
        problems.append(f"made={d['made']}, expected 2")
    if not picks[2]["keeper"] or picks[0]["keeper"]:
        problems.append("keeper flag not carried")
    if names.get(3) != "Three Point" or names.get(1) != "Team 1":
        problems.append(f"team names wrong: {names}")
    if d["pick_order"] != [7, 3, 10, 1]:
        problems.append(f"pick order wrong: {d['pick_order']}")
    return not problems, "; ".join(problems) or "order, picks, keepers and names parsed"


ENGINE_PROBE = r"""
const fs = require("fs");
const E = require(process.argv[1]);
const [head, ...lines] = fs.readFileSync(process.argv[2], "utf8").trim().split("\n");
const cols = head.split(",");
const rows = lines.map((l) => { const v = l.split(","); return Object.fromEntries(cols.map((c, i) => [c, v[i]])); });
const players = E.prepareBoard(rows);
const out = {};
const order = Array.from({ length: 48 }, (_, i) => E.snakeTeam(i, 12));
out.round2 = order.slice(12, 24);
out.myPicks = Array.from({ length: 168 }, (_, i) => i).filter((i) => E.snakeTeam(i, 12) === 3).slice(0, 4).map((i) => i + 1);
out.rookies = players.filter((p) => p.est).length;
out.rookiesPriced = players.filter((p) => p.est).every((p) => p.line && Number.isFinite(p.line.pts));
// Team 0 keeps the top player in round 2; then picks 1-3 are made.
const top = players.slice().sort((a, b) => E.adpKey(a) - E.adpKey(b));
const slots = E.buildSlots({ nTeams: 12, rounds: 14,
  keepers: [{ team: 0, playerId: top[0].id, round: 2 }],
  picks: [1, 2, 3].map((o) => ({ overall: o, playerId: top[o].id })) });
out.keeperSlot = slots.find((s) => s.playerId === top[0].id).overall;
const t0 = Date.now();
const r = E.recommend({ players, slots, me: 3, nTeams: 12 });
out.ms = Date.now() - t0;
out.current = r.current;
out.myTurn = r.myTurn;
out.keeperOffered = r.candidates.some((c) => c.player.id === top[0].id);
out.takenOffered = r.candidates.some((c) => [1, 2, 3].some((o) => c.player.id === top[o].id));
out.topFeasible = r.candidates[0].feasible;
out.probsInRange = r.myCats.every((c) => c.pWin > 0 && c.pWin < 1);
// Punting a category must take it out of the objective entirely.
const pr = E.recommend({ players, slots, me: 3, nTeams: 12, punt: { ftpct: true } });
out.puntWeight = pr.weights[7];
console.log(JSON.stringify(out));
"""


@check("draft engine: snake order, keepers, and a fast recommendation", group="draft")
def draft_engine(con_unused):
    node = shutil.which("node")
    if not node:
        raise Skip("node is not installed")
    board = ROOT / "draft_board-projected.csv"
    if not board.exists():
        raise Skip("draft_board-projected.csv not built")
    res = subprocess.run([node, "-e", ENGINE_PROBE, str(ROOT / "web" / "draft_engine.js"),
                          str(board)], capture_output=True, text=True, timeout=60)
    if res.returncode:
        return False, res.stderr.strip()[-300:]
    o = json.loads(res.stdout)
    problems = []
    if o["round2"] != list(range(11, -1, -1)):
        problems.append(f"round 2 is not round 1 reversed: {o['round2']}")
    if o["myPicks"] != [4, 21, 28, 45]:
        problems.append(f"4th slot picks {o['myPicks']}, expected [4, 21, 28, 45]")
    if o["keeperSlot"] != 24:
        problems.append(f"team 1's round-2 keeper landed at pick {o['keeperSlot']}, expected 24")
    if o["current"] != 4 or not o["myTurn"]:
        problems.append(f"on the clock: pick {o['current']}, mine={o['myTurn']}")
    if o["keeperOffered"] or o["takenOffered"]:
        problems.append("a kept or drafted player was recommended")
    if not o["rookies"] or not o["rookiesPriced"]:
        problems.append("rookies missing or unpriced")
    if not o["topFeasible"] or not o["probsInRange"]:
        problems.append("top pick infeasible or a win probability out of (0,1)")
    if o["puntWeight"] != 0:
        problems.append("punting FT% did not remove it from the objective")
    if o["ms"] > 2000:
        problems.append(f"recommendation took {o['ms']} ms")
    return not problems, "; ".join(problems) or (
        f"snake, keepers, {o['rookies']} rookies priced, recommendation in {o['ms']} ms")


@check("standalone draft room bakes in the engine and both boards", group="draft")
def standalone_build(con_unused):
    import build_draft_page as b
    html = b.build(fragment=False)
    problems = []
    if "/static/" in html:
        problems.append("still references /static, so it needs the app to load")
    if "window.DRAFT_DATA" not in html or "DraftEngine" not in html:
        problems.append("data or engine not inlined")
    data = html.split("window.DRAFT_DATA = ", 1)[1].split(";</script>", 1)[0]
    boards = json.loads(data.replace("<\\/", "</"))["boards"]
    if len(boards.get("projected", [])) < 300:
        problems.append(f"projected board has {len(boards.get('projected', []))} rows")
    frag = b.build(fragment=True)
    if "<body" in frag.lower() or "<!doctype" in frag.lower():
        problems.append("--fragment left the document skeleton in")
    return not problems, "; ".join(problems) or f"{len(html) // 1024} KB, {len(boards)} boards"


@check("draft page is served and the board can include rookies", group="draft")
def draft_routes(con_unused):
    try:
        from fastapi.testclient import TestClient
    except ImportError as exc:
        raise Skip(f"fastapi not installed ({exc})")
    import api
    c = TestClient(api.app)
    problems = []
    if c.get("/draft").status_code != 200:
        problems.append("/draft did not serve the page")
    plain = c.get("/api/fantasy/draft-board", params={"window": "projected"}).json()
    full = c.get("/api/fantasy/draft-board",
                 params={"window": "projected", "unranked": 1}).json()
    if plain.get("unranked"):
        problems.append("unranked rows returned without being asked for")
    if not full.get("unranked") or any(r.get("board_rank") is not None for r in full["unranked"]):
        problems.append("unranked=1 did not return the rookies")
    return not problems, "; ".join(problems) or (
        f"page served; {len(full['unranked'])} unranked rows on request")
