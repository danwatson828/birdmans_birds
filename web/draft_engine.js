/* Live draft engine for "The Bubble 2.0": 9-cat head-to-head, each category.
 *
 * Plain script, no imports, no framework. The page loads it as
 * window.DraftEngine and the backtest (tests/draft_backtest.mjs) loads it with
 * require(), so the code the simulation scores is the code the page runs.
 *
 * =============================================================================
 * METHOD -- stated before the code, per the project's own rule
 * =============================================================================
 * The league counts every category every week (H2H_CATEGORY), so the quantity
 * a pick should raise is EXPECTED CATEGORY WINS PER WEEK against the eleven
 * real opponents, not a context-free player value.
 *
 * 1. WEEKLY TEAM LINES. A player contributes per-game rate x games per week x
 *    availability. Counting categories sum; FG% and FT% are made / attempted
 *    over the whole roster, so a high-volume bad shooter drags them the way he
 *    really does.
 *
 * 2. FINISHED ROSTERS, NOT PARTIAL ONES. Mid-draft, a team with 3 players is
 *    not weaker than a team with 4 in any sense that matters. Every unmade pick
 *    is filled with the player expected at that slot: the average of the
 *    players around that position in ESPN's ranking among those still
 *    available, which is how the other teams actually pick. The same filler
 *    rule applies to every team, so differences between teams come from real
 *    picks only.
 *
 * 3. WIN PROBABILITY. Each category is a normal comparison of two weekly
 *    totals. Variance = per-game dispersion x mean (counting stats) plus a
 *    schedule term (some weeks a team plays 3 games, some 4); for the
 *    percentages it is binomial on the week's attempts. Turnovers are
 *    inverted. A category already won or already lost moves little -- the
 *    curve is flat at both ends -- so balance and punting fall out of the
 *    probabilities rather than being bolted on.
 *
 * 4. GAIN of a candidate = expected category wins per week with him, minus
 *    with the player expected at this slot. Averaged over the eleven opponents'
 *    projected rosters, so who they have already taken matters.
 *
 * 5. WAIT-OR-TAKE. Opponents are simulated picking by ESPN rank plus noise to
 *    estimate whether each player survives to my next turn. With survival s
 *    and fallback gain g0 (the best I can expect next turn), taking x now is
 *    worth (1 - s)(g - g0) over waiting. LAMBDA blends that in; 0 = rank by
 *    gain alone. Its default is set by the backtest, not by taste.
 *
 * NOT HANDLED, and it matters:
 *   * Per-game rates come from the projection board. Offseason trades and role
 *     changes are only as good as that board.
 *   * Rookies have no NBA line. They are priced from the players around their
 *     ESPN rank, discounted -- an estimate, and labelled as one.
 *   * Injury status is shown, not priced, beyond the board's availability.
 *   * Daily lineup conflicts are ignored: every rostered game is counted.
 * =============================================================================
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === "object" && module.exports) module.exports = api;
  else root.DraftEngine = api;
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  const CATS = ["pts", "reb", "ast", "stl", "blk", "fg3m", "fgpct", "ftpct", "turnover"];
  const CAT_LABEL = { pts: "PTS", reb: "REB", ast: "AST", stl: "STL", blk: "BLK",
                      fg3m: "3PM", fgpct: "FG%", ftpct: "FT%", turnover: "TO" };
  // Counting categories, in contribution-vector order.
  const COUNT = ["pts", "reb", "ast", "stl", "blk", "fg3m", "turnover"];
  // Vector layout: 0..6 counting means, 7 fgm, 8 fga, 9 ftm, 10 fta,
  // 11..17 counting variances.
  const NV = 18;
  const SLOTS = ["PG", "SG", "SF", "PF", "C", "G", "F", "UT", "UT", "UT"];

  const GAMES_PER_WEEK = 3.4;          // 82 games over ~24 weeks
  // Per-game variance / mean, by category. A 25-point scorer's nightly sd is
  // ~7-8, so points are over-dispersed; the small counts are near Poisson.
  const DISPERSION = { pts: 2.5, reb: 1.2, ast: 1.1, stl: 1.0, blk: 1.1,
                       fg3m: 1.1, turnover: 1.0 };
  const SCHEDULE_CV = 0.10;            // weekly games-played swing, as a fraction
  const PCT_INFLATE = 1.2;             // shot mix makes FG%/FT% wobble a bit more than binomial
  const ROOKIE_DISCOUNT = 0.85;        // an estimate from neighbours is worth less than a line
  const DEFAULT_AVAIL = 0.75;
  const FILLER_HALF_WINDOW = 2;
  const SIMS = 400;
  const LAMBDA = 0.5;

  // ---------------------------------------------------------------------------
  // Small maths
  // ---------------------------------------------------------------------------
  function num(v) {
    if (v === null || v === undefined || v === "") return NaN;
    const x = +v;
    return Number.isFinite(x) ? x : NaN;
  }

  // Abramowitz-Stegun 7.1.26: |error| < 1.5e-7, which is far below anything
  // this model can claim.
  function phi(z) {
    const t = 1 / (1 + 0.3275911 * Math.abs(z) / Math.SQRT2);
    const y = 1 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                    - 0.284496736) * t + 0.254829592) * t * Math.exp(-z * z / 2);
    return z >= 0 ? (1 + y) / 2 : (1 - y) / 2;
  }

  function rng(seed) {                 // mulberry32
    let a = (seed >>> 0) || 1;
    return function () {
      a = (a + 0x6D2B79F5) >>> 0;
      let t = a;
      t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  }

  function gauss(rand) {
    let u = 0;
    while (u === 0) u = rand();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * rand());
  }

  // ---------------------------------------------------------------------------
  // Board -> players
  // ---------------------------------------------------------------------------
  /** Normalise draft-board rows (CSV columns, from the file or the API) into
   *  players. Rows without a model line are priced from their ESPN-rank
   *  neighbours and flagged `est`, so a rookie is visible and roughly priced
   *  rather than invisible. */
  function prepareBoard(rows) {
    const players = [];
    const seen = new Set();
    for (const r of rows) {
      if (!r || !r.name) continue;
      const espn = num(r.espn_id);
      const id = Number.isFinite(espn) ? "e" + espn : "n:" + String(r.name).toLowerCase();
      if (seen.has(id)) continue;
      seen.add(id);
      const p = {
        id, espnId: Number.isFinite(espn) ? espn : null,
        name: String(r.name), team: r.pro_team || "", posStr: r.pos_str || "",
        pos: String(r.pos_str || "").split("/").filter(Boolean),
        inj: r.injury_status && r.injury_status !== "ACTIVE" ? String(r.injury_status) : null,
        adp: num(r.draft_rank), rank: num(r.board_rank), value: num(r.total_value),
        avail: num(r.availability), gp: num(r.w_gp), est: false, line: null, z: {},
      };
      if (r.value_source === "model" && Number.isFinite(num(r.pts))) {
        const fga = num(r.fga) || 0, fta = num(r.fta) || 0;
        p.line = {
          pts: num(r.pts), reb: num(r.reb), ast: num(r.ast), stl: num(r.stl),
          blk: num(r.blk), fg3m: num(r.fg3m), turnover: num(r.turnover),
          fgm: (num(r.fg_pct) || 0) * fga, fga, ftm: (num(r.ft_pct) || 0) * fta, fta,
        };
        for (const c of CATS) p.z[c] = num(r["z_" + c]);
      }
      players.push(p);
    }
    const priced = players.filter((p) => p.line && Number.isFinite(p.adp))
      .sort((a, b) => a.adp - b.adp);
    for (const p of players) if (!p.line) imputeFromNeighbours(p, priced);
    for (const p of players) p.vec = contribution(p);
    return players;
  }

  function imputeFromNeighbours(p, priced) {
    const adp = Number.isFinite(p.adp) ? p.adp : 300;
    const near = priced.slice().sort((a, b) => Math.abs(a.adp - adp) - Math.abs(b.adp - adp))
      .slice(0, 8);
    const line = {}, z = {};
    for (const k of Object.keys(near[0].line)) {
      line[k] = ROOKIE_DISCOUNT * near.reduce((s, q) => s + q.line[k], 0) / near.length;
    }
    for (const c of CATS) {
      z[c] = ROOKIE_DISCOUNT * near.reduce((s, q) => s + (q.z[c] || 0), 0) / near.length;
    }
    p.line = line;
    p.z = z;
    p.avail = near.reduce((s, q) => s + (Number.isFinite(q.avail) ? q.avail : DEFAULT_AVAIL), 0)
      / near.length;
    p.value = ROOKIE_DISCOUNT * near.reduce((s, q) => s + q.value, 0) / near.length;
    p.est = true;
  }

  /** Weekly contribution of one player: means and variances, summable. */
  function contribution(p) {
    const v = new Float64Array(NV);
    const avail = Number.isFinite(p.avail) ? Math.min(Math.max(p.avail, 0), 1) : DEFAULT_AVAIL;
    const g = GAMES_PER_WEEK * avail;
    COUNT.forEach((c, i) => {
      const x = Math.max(p.line[c] || 0, 0);
      v[i] = g * x;
      v[11 + i] = g * DISPERSION[c] * x;
    });
    v[7] = g * p.line.fgm; v[8] = g * p.line.fga;
    v[9] = g * p.line.ftm; v[10] = g * p.line.fta;
    return v;
  }

  function add(into, v, sign = 1) {
    for (let i = 0; i < NV; i++) into[i] += sign * v[i];
    return into;
  }

  /** Category means and variances of a weekly team line. */
  function teamCats(T) {
    const mean = new Float64Array(9), vari = new Float64Array(9);
    // CATS order: pts reb ast stl blk fg3m fgpct ftpct turnover
    const countIdx = { 0: 0, 1: 1, 2: 2, 3: 3, 4: 4, 5: 5, 8: 6 };
    for (const [ci, vi] of Object.entries(countIdx)) {
      mean[ci] = T[vi];
      vari[ci] = T[11 + vi] + (SCHEDULE_CV * T[vi]) ** 2;
    }
    const fg = T[8] > 0 ? T[7] / T[8] : 0.46;
    const ft = T[10] > 0 ? T[9] / T[10] : 0.78;
    mean[6] = fg; vari[6] = T[8] > 0 ? PCT_INFLATE * fg * (1 - fg) / T[8] : 1;
    mean[7] = ft; vari[7] = T[10] > 0 ? PCT_INFLATE * ft * (1 - ft) / T[10] : 1;
    return { mean, vari };
  }

  /** P(team a beats team b) in each category. */
  function winProbs(a, b) {
    const p = new Float64Array(9);
    for (let c = 0; c < 9; c++) {
      const sd = Math.sqrt(a.vari[c] + b.vari[c]) || 1;
      const d = (a.mean[c] - b.mean[c]) / sd;
      p[c] = phi(c === 8 ? -d : d);          // fewer turnovers wins
    }
    return p;
  }

  function expectedWins(me, opps, weights) {
    let total = 0;
    for (const o of opps) {
      const p = winProbs(me, o);
      for (let c = 0; c < 9; c++) total += weights[c] * p[c];
    }
    return opps.length ? total / opps.length : 0;
  }

  // ---------------------------------------------------------------------------
  // Draft structure
  // ---------------------------------------------------------------------------
  /** Team index (0-based draft position) holding overall pick `i` (0-based). */
  function snakeTeam(i, nTeams) {
    const r = Math.floor(i / nTeams), k = i % nTeams;
    return r % 2 === 0 ? k : nTeams - 1 - k;
  }

  /** Every slot of the draft, with keepers already placed in their round. */
  function buildSlots({ nTeams, rounds, keepers = [], picks = [], owners = null }) {
    const slots = [];
    for (let i = 0; i < nTeams * rounds; i++) {
      slots.push({ overall: i + 1, round: Math.floor(i / nTeams) + 1,
                   team: owners && owners[i] != null ? owners[i] : snakeTeam(i, nTeams),
                   playerId: null, keeper: false });
    }
    for (const k of keepers) {
      if (k.playerId == null || !k.round) continue;
      const s = slots.find((x) => x.team === k.team && x.round === k.round);
      if (s) { s.playerId = k.playerId; s.keeper = true; }
    }
    for (const p of picks) {
      const s = slots[p.overall - 1];
      if (s) s.playerId = p.playerId;
    }
    return slots;
  }

  // Kuhn's augmenting paths: who fills which starting slot. Ten slots and at
  // most fourteen players, so the brute method is instant.
  function eligible(p, slot) {
    if (slot === "UT") return true;
    if (p.pos.includes(slot)) return true;
    if (slot === "G") return p.pos.includes("PG") || p.pos.includes("SG");
    if (slot === "F") return p.pos.includes("SF") || p.pos.includes("PF");
    return false;
  }

  function matchSlots(roster) {
    const owner = new Array(SLOTS.length).fill(-1);
    function tryAssign(pi, seen) {
      for (let s = 0; s < SLOTS.length; s++) {
        if (seen[s] || !eligible(roster[pi], SLOTS[s])) continue;
        seen[s] = true;
        if (owner[s] === -1 || tryAssign(owner[s], seen)) { owner[s] = pi; return true; }
      }
      return false;
    }
    let n = 0;
    for (let i = 0; i < roster.length; i++) if (tryAssign(i, [])) n++;
    const open = SLOTS.filter((_, s) => owner[s] === -1);
    return { filled: n, open, owner };
  }

  // ---------------------------------------------------------------------------
  // Recommendation
  // ---------------------------------------------------------------------------
  /**
   * @param {object} a
   * @param {Array} a.players     prepareBoard() output
   * @param {Array} a.slots       buildSlots() output
   * @param {number} a.me         my team index
   * @param {number} a.nTeams
   * @param {object} [a.punt]     {cat: true} to drop a category from the objective
   * @param {number} [a.lambda]   wait-or-take blend, 0..1
   */
  function recommend(a) {
    const { players, slots, me, nTeams } = a;
    const lambda = a.lambda ?? LAMBDA;
    const weights = CATS.map((c) => (a.punt && a.punt[c] ? 0 : 1));
    const byId = new Map(players.map((p) => [p.id, p]));

    const taken = new Set(slots.filter((s) => s.playerId != null).map((s) => s.playerId));
    const avail = players.filter((p) => !taken.has(p.id));
    const queue = slots.filter((s) => s.playerId == null);
    const rosters = Array.from({ length: nTeams }, () => []);
    for (const s of slots) {
      if (s.playerId == null) continue;
      const p = byId.get(s.playerId);
      if (p) rosters[s.team].push(p);
    }
    if (!queue.length) {
      return { done: true, rosters, standings: standings(rosters.map(sumVec), weights) };
    }

    // ESPN order among those left: how the room actually picks.
    const L = avail.slice().sort((x, y) => adpKey(x) - adpKey(y));
    const filler = fillerTable(L, queue.length);

    const future = Array.from({ length: nTeams }, () => new Float64Array(NV));
    const myQ = queue.findIndex((s) => s.team === me);
    queue.forEach((s, j) => { if (j !== myQ) add(future[s.team], filler[j]); });

    const teamVec = rosters.map((r, t) => add(sumVec(r), future[t]));
    const opps = [];
    for (let t = 0; t < nTeams; t++) if (t !== me) opps.push(teamCats(teamVec[t]));

    const onClock = queue[0];
    const out = { done: false, onClock, current: onClock.overall, round: onClock.round,
                  myTurn: onClock.team === me, myNext: myQ >= 0 ? queue[myQ] : null,
                  picksUntilMine: myQ, rosters, weights };

    // Standings use every team's finished roster, mine included with the
    // filler at my next slot.
    const myBaseVec = add(teamVec[me].slice(), myQ >= 0 ? filler[myQ] : new Float64Array(NV));
    const allVecs = teamVec.map((v, t) => (t === me ? myBaseVec : v));
    out.standings = standings(allVecs, weights);
    out.myCats = catOutlook(teamCats(myBaseVec), opps);

    if (myQ < 0) { out.candidates = []; return out; }

    const myRoster = rosters[me];
    const myLeft = queue.filter((s) => s.team === me).length;
    const baseP = meanProbs(teamCats(myBaseVec), opps);
    const base = dot(weights, baseP);
    const baseFilled = matchSlots(myRoster).filled;
    out.baseline = base;

    const cands = avail.map((p) => {
      const pr = meanProbs(teamCats(add(teamVec[me].slice(), p.vec)), opps);
      const m = matchSlots(myRoster.concat([p]));
      return {
        player: p, gain: dot(weights, pr) - base,
        catDelta: CATS.map((c, i) => pr[i] - baseP[i]),
        fillsStarter: m.filled > baseFilled,
        feasible: SLOTS.length - m.filled <= myLeft - 1,
        openAfter: m.open,
      };
    });
    if (a.survival === false) {
      for (const c of cands) { c.pSurvive = 1; c.score = c.gain - (c.feasible ? 0 : 100); }
      cands.sort((x, y) => y.score - x.score);
      out.candidates = cands;
      return out;
    }

    // Survival: picks by others before my NEXT chance at this player.
    let horizon, laterQ = -1;
    if (myQ === 0) {
      laterQ = queue.findIndex((s, j) => j > 0 && s.team === me);
      horizon = laterQ < 0 ? 0 : laterQ - 1;
    } else {
      horizon = myQ;
    }
    const surv = survival(L, cands, horizon, a.seed ?? slots.filter((s) => s.playerId != null).length);
    for (const c of cands) {
      c.pSurvive = surv.p.get(c.player.id) ?? 1;
      c.fallback = surv.fallback(c.player.id);
      c.score = myQ === 0
        ? c.gain - lambda * c.pSurvive * Math.max(0, c.gain - c.fallback)
        : c.gain;
      if (!c.feasible) c.score -= 100;    // would leave a starting slot unfillable
    }
    cands.sort((x, y) => y.score - x.score);
    out.candidates = cands;
    out.horizon = horizon;
    out.horizonLabel = myQ === 0 ? "there at your next pick" : "reaches your pick";
    return out;
  }

  function dot(w, p) {
    let t = 0;
    for (let c = 0; c < 9; c++) t += w[c] * p[c];
    return t;
  }

  function adpKey(p) { return Number.isFinite(p.adp) ? p.adp : 900 + (p.rank || 0); }

  function sumVec(list) {
    const v = new Float64Array(NV);
    for (const p of list) add(v, p.vec);
    return v;
  }

  /** filler[j] = expected weekly line of the j-th pick from now. */
  function fillerTable(L, n) {
    const pre = [new Float64Array(NV)];
    for (const p of L) pre.push(add(pre[pre.length - 1].slice(), p.vec));
    const out = [];
    for (let j = 0; j < n; j++) {
      let lo = Math.max(0, j - FILLER_HALF_WINDOW), hi = Math.min(L.length, j + FILLER_HALF_WINDOW + 1);
      if (hi <= lo) { lo = Math.max(0, L.length - 1); hi = L.length; }
      const v = pre[hi].slice();
      add(v, pre[lo], -1);
      for (let i = 0; i < NV; i++) v[i] /= Math.max(1, hi - lo);
      out.push(v);
    }
    return out;
  }

  function meanProbs(mine, opps) {
    const acc = new Float64Array(9);
    for (const o of opps) {
      const p = winProbs(mine, o);
      for (let c = 0; c < 9; c++) acc[c] += p[c];
    }
    for (let c = 0; c < 9; c++) acc[c] /= Math.max(1, opps.length);
    return acc;
  }

  function catOutlook(mine, opps) {
    const p = meanProbs(mine, opps);
    return CATS.map((c, i) => ({ cat: c, label: CAT_LABEL[c], pWin: p[i],
                                 mean: mine.mean[i] }));
  }

  /** Every team's expected category win share against the other eleven. */
  function standings(vecs, weights) {
    const cats = vecs.map(teamCats);
    const wsum = weights.reduce((s, w) => s + w, 0) || 1;
    const rows = cats.map((me, t) => {
      const opps = cats.filter((_, u) => u !== t);
      return { team: t, catWins: expectedWins(me, opps, weights),
               share: expectedWins(me, opps, weights) / wsum, means: me.mean };
    });
    // Category ranks: 1 = best, turnovers lowest-is-best.
    for (let c = 0; c < 9; c++) {
      const order = rows.slice().sort((x, y) =>
        c === 8 ? x.means[c] - y.means[c] : y.means[c] - x.means[c]);
      order.forEach((r, i) => { (r.catRank ||= [])[c] = i + 1; });
    }
    return rows;
  }

  /** Monte Carlo of the picks before my next turn. Opponents take the
   *  lowest ESPN rank plus noise; the noise widens down the board, since
   *  nobody argues about pick 3 and everybody argues about pick 120. */
  function survival(L, cands, horizon, seed) {
    const p = new Map();
    const gainOf = new Map(cands.map((c) => [c.player.id, c.feasible ? c.gain : -Infinity]));
    if (horizon <= 0) {
      const best = cands.slice().sort((x, y) => gainOf.get(y.player.id) - gainOf.get(x.player.id));
      const b1 = best[0], b2 = best[1];
      return { p, fallback: (id) => (b1 && b1.player.id !== id ? b1.gain : b2 ? b2.gain : 0) };
    }
    const N = Math.min(L.length, horizon + 80);
    const top = L.slice(0, N);
    const sigma = top.map((x) => 3 + 0.12 * Math.min(adpKey(x), 300));
    const goneCount = new Float64Array(N);
    const rand = rng(seed * 7919 + horizon);
    const keys = new Array(N);
    const fb1 = [], fb2 = [];
    // Everyone outside `top` always survives; their best gain is a floor.
    let tailBest = -Infinity, tailBestId = null, tailSecond = -Infinity;
    for (const x of L.slice(N)) {
      const g = gainOf.get(x.id) ?? -Infinity;
      if (g > tailBest) { tailSecond = tailBest; tailBest = g; tailBestId = x.id; }
      else if (g > tailSecond) tailSecond = g;
    }
    for (let s = 0; s < SIMS; s++) {
      for (let i = 0; i < N; i++) keys[i] = [adpKey(top[i]) + sigma[i] * gauss(rand), i];
      keys.sort((u, v) => u[0] - v[0]);
      const gone = new Uint8Array(N);
      for (let k = 0; k < Math.min(horizon, N); k++) { gone[keys[k][1]] = 1; goneCount[keys[k][1]]++; }
      let b1 = tailBest, b1id = tailBestId, b2 = tailSecond;
      for (let i = 0; i < N; i++) {
        if (gone[i]) continue;
        const g = gainOf.get(top[i].id) ?? -Infinity;
        if (g > b1) { b2 = b1; b1 = g; b1id = top[i].id; }
        else if (g > b2) b2 = g;
      }
      fb1.push([b1, b1id]); fb2.push(b2);
    }
    top.forEach((x, i) => p.set(x.id, 1 - goneCount[i] / SIMS));
    const fallback = (id) => {
      let t = 0;
      for (let s = 0; s < SIMS; s++) t += fb1[s][1] === id ? fb2[s] : fb1[s][0];
      const v = t / SIMS;
      return Number.isFinite(v) ? v : 0;
    };
    return { p, fallback };
  }

  // ---------------------------------------------------------------------------
  // Punt check
  // ---------------------------------------------------------------------------
  /** Play out every remaining pick: other teams by ESPN rank with the same
   *  widening noise as `survival`, me by the engine with `punt`. Returns my
   *  expected categories won per week against the eleven finished rosters,
   *  counting ALL nine categories -- a punt has to win more overall, not just
   *  stop caring about the one it gives up. */
  function finishDraft(a, punt, seed) {
    const slots = a.slots.map((s) => ({ ...s }));
    const rand = rng(seed);
    const taken = new Set(slots.filter((s) => s.playerId != null).map((s) => s.playerId));
    const key = new Map();
    for (const p of a.players) {
      if (!taken.has(p.id)) key.set(p.id, adpKey(p) + (3 + 0.12 * Math.min(adpKey(p), 300)) * gauss(rand));
    }
    for (const s of slots) {
      if (s.playerId != null) continue;
      if (s.team === a.me) {
        const r = recommend({ ...a, slots, punt, lambda: 0, survival: false });
        s.playerId = r.candidates[0].player.id;
      } else {
        let best = null, bk = Infinity;
        for (const [id, k] of key) if (k < bk) { bk = k; best = id; }
        s.playerId = best;
      }
      key.delete(s.playerId);
    }
    const byId = new Map(a.players.map((p) => [p.id, p]));
    const rosters = Array.from({ length: a.nTeams }, () => []);
    for (const s of slots) { const p = byId.get(s.playerId); if (p) rosters[s.team].push(p); }
    const cats = rosters.map((r) => teamCats(sumVec(r)));
    const opps = cats.filter((_, t) => t !== a.me);
    const pr = meanProbs(cats[a.me], opps);
    return { catWins: dot(new Array(9).fill(1), pr), probs: pr };
  }

  /** Balanced against each single-category punt, over `seeds` paired
   *  playouts. `step(i, n)` is called between playouts so a page can stay
   *  responsive; it may return a promise. */
  async function puntCheck(a, seeds = 3, step = null) {
    const options = [null, ...CATS];
    const totals = options.map(() => ({ catWins: 0, probs: new Float64Array(9) }));
    let i = 0;
    const n = options.length * seeds;
    for (let s = 0; s < seeds; s++) {
      for (let k = 0; k < options.length; k++) {
        const cat = options[k];
        // Same seed for every option, so each one faces the same room.
        const r = finishDraft(a, cat ? { [cat]: true } : {}, 9001 + s);
        totals[k].catWins += r.catWins / seeds;
        for (let c = 0; c < 9; c++) totals[k].probs[c] += r.probs[c] / seeds;
        if (step) await step(++i, n);
      }
    }
    const base = totals[0].catWins;
    return options.map((cat, k) => ({
      punt: cat, label: cat ? CAT_LABEL[cat] : "Balanced",
      catWins: totals[k].catWins, delta: totals[k].catWins - base,
      probs: Array.from(totals[k].probs),
    })).sort((x, y) => y.catWins - x.catWins);
  }

  return { CATS, CAT_LABEL, SLOTS, LAMBDA, prepareBoard, buildSlots, snakeTeam,
           recommend, matchSlots, puntCheck, finishDraft, teamCats, winProbs, contribution, sumVec,
           standings, rng, gauss, adpKey };
});
