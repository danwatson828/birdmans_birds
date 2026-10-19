#!/usr/bin/env node
/* Paired mock drafts: does web/draft_engine.js beat drafting straight down the
 * board? READ-ONLY -- reads the committed board CSV, writes nothing.
 *
 * Mirrors draft_assistant.py --simulate so the two results are comparable:
 * the same opponent model (every other team takes the lowest ESPN rank plus
 * Gaussian noise drawn once per draft), the same 12 x 14 snake, the same
 * model-valued pool. Every strategy faces an identical field for a given seed,
 * so the comparison measures strategy, not luck.
 *
 * Two season scorers, because each flatters someone:
 *   zsum   draft_assistant's own: summed category z-scores plus weekly noise.
 *          This is the board's own currency, so it favours the board.
 *   lines  weekly stat lines: games drawn from availability, per-game noise,
 *          FG%/FT% as made over attempted, and the projections themselves
 *          perturbed per player (a projection is not the truth). Closer to the
 *          engine's view of the world, so it favours the engine.
 * A strategy worth shipping should not lose on either.
 *
 * IN-SAMPLE, and that is a real limit: the board both prices the players and
 * stands in for how they play. The out-of-sample harness
 * (validate_assistant.py) needs the DuckDB snapshot, which this was not run
 * against.
 *
 *     node backtest_draft_engine.mjs --drafts 200 --slot 4
 *     node backtest_draft_engine.mjs --drafts 100 --slot 4 --opp board
 */
import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";
import { fileURLToPath } from "node:url";

const ROOT = path.dirname(fileURLToPath(import.meta.url));
const E = createRequire(import.meta.url)(path.join(ROOT, "web", "draft_engine.js"));

const args = Object.fromEntries(process.argv.slice(2).reduce((acc, a, i, all) => {
  if (a.startsWith("--")) acc.push([a.slice(2), all[i + 1]]);
  return acc;
}, []));
const DRAFTS = +(args.drafts || 200);
const SLOT = +(args.slot || 4) - 1;
const BOARD = args.board || "draft_board-projected.csv";
const STRATS = (args.strategies || "board,espn,engine0,engine50,engine100").split(",");
const N_TEAMS = 12, ROUNDS = 14, WEEKS = 20, OPP_NOISE = 12;
// "espn": the room drafts by ESPN rank (draft_assistant's model).
// "board": the room drafts by this board's own rank -- opponents who know the
// projections, so beating them has to come from roster construction alone.
const OPP = args.opp || "espn";

function readCsv(file) {
  const [head, ...lines] = fs.readFileSync(path.join(ROOT, file), "utf8").trim().split("\n");
  const cols = head.split(",");
  return lines.map((l) => {
    const v = l.split(",");
    return Object.fromEntries(cols.map((c, i) => [c, v[i]]));
  });
}

// draft_assistant.load_pool: model-valued players only.
const pool = E.prepareBoard(readCsv(BOARD)).filter((p) => !p.est && Number.isFinite(p.value));

function draft(strategy, seed) {
  const rand = E.rng(seed);
  const oppKey = new Map(pool.map((p) =>
    [p.id, (OPP === "board" ? p.rank : (Number.isFinite(p.adp) ? p.adp : 999))
           + OPP_NOISE * E.gauss(rand)]));
  const picks = [];
  const takenIds = new Set();
  for (let i = 0; i < N_TEAMS * ROUNDS; i++) {
    const team = E.snakeTeam(i, N_TEAMS);
    const avail = pool.filter((p) => !takenIds.has(p.id));
    let pick;
    if (team !== SLOT || strategy === "espn") {
      pick = avail.reduce((b, p) => (oppKey.get(p.id) < oppKey.get(b.id) ? p : b));
      if (team === SLOT) pick = avail.reduce((b, p) => (adp(p) < adp(b) ? p : b));
    } else if (strategy === "board") {
      pick = avail.reduce((b, p) => (p.value > b.value ? p : b));
    } else {
      const lambda = +strategy.replace("engine", "") / 100;
      const slots = E.buildSlots({ nTeams: N_TEAMS, rounds: ROUNDS,
                                   picks: picks.map((id, k) => ({ overall: k + 1, playerId: id })) });
      pick = E.recommend({ players: pool, slots, me: SLOT, nTeams: N_TEAMS, lambda, seed: i })
        .candidates[0].player;
    }
    picks.push(pick.id);
    takenIds.add(pick.id);
  }
  const rosters = Array.from({ length: N_TEAMS }, () => []);
  const byId = new Map(pool.map((p) => [p.id, p]));
  picks.forEach((id, i) => rosters[E.snakeTeam(i, N_TEAMS)].push(byId.get(id)));
  return rosters;
}
const adp = (p) => (Number.isFinite(p.adp) ? p.adp : 999);

// ---- scorer 1: draft_assistant.simulate_season, verbatim in spirit --------
function seasonZsum(rosters, seed) {
  const rand = E.rng(seed);
  const base = rosters.map((r) => E.CATS.map((c) => r.reduce((s, p) => s + (p.z[c] || 0), 0)));
  const wins = new Float64Array(N_TEAMS), played = new Float64Array(N_TEAMS);
  for (let w = 0; w < WEEKS; w++) {
    for (let a = 0; a < N_TEAMS; a++) {
      for (let b = a + 1; b < N_TEAMS; b++) {
        for (let c = 0; c < 9; c++) {
          const va = base[a][c] + 0.55 * E.gauss(rand), vb = base[b][c] + 0.55 * E.gauss(rand);
          if (va > vb) wins[a]++; else if (vb > va) wins[b]++; else { wins[a] += 0.5; wins[b] += 0.5; }
        }
        played[a] += 9; played[b] += 9;
      }
    }
  }
  return Array.from(wins, (w, t) => w / played[t]);
}

// ---- scorer 2: weekly stat lines with projection error --------------------
const DISP = { pts: 2.5, reb: 1.2, ast: 1.1, stl: 1.0, blk: 1.1, fg3m: 1.1, turnover: 1.0 };
const COUNT = Object.keys(DISP);
function seasonLines(rosters, seed) {
  // Truth is drawn per PLAYER from a seed that does not depend on who drafted
  // him, so a player is equally good or bad whichever strategy took him.
  const truth = (p) => {
    const r = E.rng(seed ^ hash(p.id));
    const vol = Math.exp(0.12 * E.gauss(r));
    const avail = Math.min(1, Math.max(0.2, (Number.isFinite(p.avail) ? p.avail : 0.75) + 0.12 * E.gauss(r)));
    const fgp = (p.line.fga ? p.line.fgm / p.line.fga : 0.46) + 0.015 * E.gauss(r);
    const ftp = (p.line.fta ? p.line.ftm / p.line.fta : 0.78) + 0.02 * E.gauss(r);
    return { vol, avail, fgp, ftp, line: p.line };
  };
  const T = rosters.map((r) => r.map(truth));
  const rand = E.rng(seed + 17);
  const wins = new Float64Array(N_TEAMS);
  let games = 0;
  for (let w = 0; w < WEEKS; w++) {
    const week = T.map((roster) => {
      const tot = { pts: 0, reb: 0, ast: 0, stl: 0, blk: 0, fg3m: 0, turnover: 0,
                    fgm: 0, fga: 0, ftm: 0, fta: 0 };
      for (const t of roster) {
        let g = 0;
        for (let k = 0; k < 4; k++) if (rand() < 0.85 * t.avail) g++;
        if (!g) continue;
        for (const c of COUNT) {
          const m = g * t.vol * t.line[c];
          tot[c] += Math.max(0, m + Math.sqrt(DISP[c] * m) * E.gauss(rand));
        }
        const fga = g * t.vol * t.line.fga, fta = g * t.vol * t.line.fta;
        tot.fga += fga; tot.fta += fta;
        tot.fgm += fga * t.fgp + Math.sqrt(fga * t.fgp * (1 - t.fgp)) * E.gauss(rand);
        tot.ftm += fta * t.ftp + Math.sqrt(Math.max(0, fta * t.ftp * (1 - t.ftp))) * E.gauss(rand);
      }
      return [tot.pts, tot.reb, tot.ast, tot.stl, tot.blk, tot.fg3m,
              tot.fgm / tot.fga, tot.ftm / tot.fta, -tot.turnover];
    });
    for (let a = 0; a < N_TEAMS; a++) {
      for (let b = a + 1; b < N_TEAMS; b++) {
        for (let c = 0; c < 9; c++) {
          if (week[a][c] > week[b][c]) wins[a]++;
          else if (week[b][c] > week[a][c]) wins[b]++;
          else { wins[a] += 0.5; wins[b] += 0.5; }
        }
      }
    }
    games += (N_TEAMS - 1) * 9;
  }
  return Array.from(wins, (w) => w / games);
}
function hash(s) {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return h >>> 0;
}

// ---- run -------------------------------------------------------------------
const res = Object.fromEntries(STRATS.map((s) => [s, { zsum: [], lines: [], zrank: [], lrank: [] }]));
const t0 = Date.now();
for (let d = 0; d < DRAFTS; d++) {
  const seed = 1000 + d;
  for (const s of STRATS) {
    const rosters = draft(s, seed);
    const z = seasonZsum(rosters, seed + 500000);
    const l = seasonLines(rosters, seed + 900000);
    const rank = (arr) => 1 + arr.filter((v) => v > arr[SLOT]).length;
    res[s].zsum.push(z[SLOT]); res[s].lines.push(l[SLOT]);
    res[s].zrank.push(rank(z)); res[s].lrank.push(rank(l));
  }
}

const mean = (a) => a.reduce((s, x) => s + x, 0) / a.length;
const sd = (a) => { const m = mean(a); return Math.sqrt(a.reduce((s, x) => s + (x - m) ** 2, 0) / (a.length - 1)); };
function paired(a, b) {
  const d = a.map((x, i) => x - b[i]);
  const t = mean(d) / (sd(d) / Math.sqrt(d.length));
  // Normal approximation to the t tail; fine at n >= 100.
  const p = 2 * (1 - normCdf(Math.abs(t)));
  return { diff: mean(d), t, p, better: d.filter((x) => x > 0).length };
}
function normCdf(z) { return 0.5 * (1 + Math.sign(z) * Math.sqrt(1 - Math.exp(-2 * z * z / Math.PI))); }

console.log(`${DRAFTS} paired drafts, slot ${SLOT + 1} of ${N_TEAMS}, board ${BOARD}, opponents by ${OPP} rank, `
  + `${((Date.now() - t0) / 1000).toFixed(1)}s`);
console.log("strategy     zsum share  (finish)   lines share (finish)   vs board: zsum diff p      lines diff p");
for (const s of STRATS) {
  const r = res[s], b = res.board;
  const pz = s === "board" ? null : paired(r.zsum, b.zsum);
  const pl = s === "board" ? null : paired(r.lines, b.lines);
  console.log(`${s.padEnd(12)} ${mean(r.zsum).toFixed(4)}  (${mean(r.zrank).toFixed(2)})     `
    + `${mean(r.lines).toFixed(4)}  (${mean(r.lrank).toFixed(2)})    `
    + (pz ? `${pz.diff >= 0 ? "+" : ""}${pz.diff.toFixed(4)} p=${pz.p.toFixed(3)} (${pz.better}/${DRAFTS})   `
          + `${pl.diff >= 0 ? "+" : ""}${pl.diff.toFixed(4)} p=${pl.p.toFixed(3)} (${pl.better}/${DRAFTS})` : "--"));
}
