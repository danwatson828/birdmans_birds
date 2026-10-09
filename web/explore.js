// Explorer — no LLM. Player and team pages, leaderboards, dataset catalog,
// coverage shading (design spec section 14).
//
// The UI carries the semantic layer with it: every view is regular-season by
// default and says so, because `postseason = false` is not "regular season", and
// every stat that was not always recorded is shown against its coverage rather
// than presented as if the whole span were comparable.
import { h, useState, useEffect, useRef, useCallback, api, post, fmt, pct, year, Spinner, Chart, useSeasons, SeasonPicker, useBrands, brandOf, teamStyle, Crest, WL, record, token } from "/static/lib.js";
import { Comparison } from "/static/compare.js";

const GT = ["regular", "playoff", "play_in", "cup_final"];
const RATE = new Set(["fg_pct", "fg3_pct", "ft_pct", "ts_pct", "efg_pct"]);
const num = (v, k) => RATE.has(k) ? pct(v) : fmt(v, 1);

function GameTypePicker({ value, onChange }) {
  return h("label", { className: "muted" }, "Games ",
    h("select", { value, onChange: (e) => onChange(e.target.value) },
      GT.map((g) => h("option", { key: g, value: g }, g.replace("_", "-")))));
}

// ---------------------------------------------------------------- coverage
function CoverageStrip({ stat }) {
  const [d, setD] = useState(null);
  useEffect(() => { setD(null);
    api(`/api/explore/coverage?stat=${encodeURIComponent(stat)}`).then(setD).catch(() => setD(false));
  }, [stat]);
  if (d === false) return null;
  if (!d) return h(Spinner, { label: "coverage…" });
  const rows = Array.isArray(d.seasons) ? d.seasons : [];
  if (rows.length === 0) return h("p", { className: "muted" }, "no coverage data");
  // Coverage bands come from --cov-*: the same four values drive this bar,
  // the legend under it and the catalog page, and they print with
  // print-color-adjust: exact so the encoding survives the PDF.
  const band = (p) => p == null ? token("--cov-missing")
    : p >= 95 ? token("--cov-full") : p >= 50 ? token("--cov-part") : token("--cov-none");
  return h("div", null,
    h("div", { className: "coverage-bar", title: `${stat} completeness by season` },
      rows.map((s) => h("i", { key: s.season,
        style: { flex: 1, background: band(s.pct) },
        title: `${s.season_label}: ${s.pct == null ? "no data" : s.pct + "%"} of played rows` }))),
    h("div", { className: "legend" },
      h("span", null, h("i", { style: { background: token("--cov-full") } }), "≥95% recorded"),
      h("span", null, h("i", { style: { background: token("--cov-part") } }), "partial"),
      h("span", null, h("i", { style: { background: token("--cov-none") } }), "<50%"),
      d.first_tracked && h("span", null,
        `first recorded ${d.first_tracked.first_tracked_season}, first full season ${d.first_tracked.first_full_season}`),
      h("span", null, `${rows[0].season_label} → ${rows[rows.length - 1].season_label}`)));
}

// ---------------------------------------------------------------- player
// A real trend chart: pick the stat, pick the grain, and see the season average
// drawn across it. The old one was hardcoded to points per game by season with
// no reference line, so a 24-point season and a 24-point career looked alike.
const TREND_STATS = [
  ["ppg", "Points", "pts"], ["rpg", "Rebounds", "reb"], ["apg", "Assists", "ast"],
  ["spg", "Steals", "stl"], ["bpg", "Blocks", "blk"],
  ["fg3mpg", "Threes made", "fg3m"], ["topg", "Turnovers", "turnover"],
  ["mpg", "Minutes", "minutes_played"], ["ts_pct", "True shooting %", null],
  ["fg_pct", "Field goal %", null], ["fg3_pct", "Three-point %", null],
];

function PlayerTrend({ playerId, seasons, gameType, playerName }) {
  const [stat, setStat] = useState("ppg");
  const [grain, setGrain] = useState("season");
  const [games, setGames] = useState(null);
  const [gameSeason, setGameSeason] = useState(
    seasons.length ? seasons[seasons.length - 1].season : null);
  const meta = TREND_STATS.find((x) => x[0] === stat) || TREND_STATS[0];
  const [, label, gameKey] = meta;
  const isRate = /_pct$/.test(stat);

  useEffect(() => {
    if (grain !== "game" || !gameSeason) return;
    setGames(null);
    api(`/api/explore/player/${playerId}/games?view=totals&game_type=${gameType}`
        + `&season=${gameSeason}&limit=200`)
      .then(setGames).catch(() => setGames(false));
  }, [playerId, grain, gameSeason, gameType]);

  // Per-game grain only exists for stats the box score records; a shooting
  // percentage for one game is a different (and much noisier) quantity, so
  // those stay season-only rather than silently plotting something else.
  const canGame = gameKey != null;
  useEffect(() => { if (!canGame && grain === "game") setGrain("season"); },
           [canGame, grain]);

  let rows = [], xField = "season", xTitle = "Season (start year)", mean = null;
  if (grain === "season") {
    rows = seasons.filter((r) => r[stat] != null)
      .map((r) => ({ season: r.season, label: r.season_label, value: r[stat] }));
  } else if (games && games.rows) {
    rows = games.rows.filter((r) => r[gameKey] != null)
      .slice().reverse()
      .map((r, i) => ({ season: i + 1, label: r.game_date, value: r[gameKey] }));
    xField = "season"; xTitle = `Game of the ${gameSeason} season`;
  }
  if (rows.length) mean = rows.reduce((a, r) => a + r.value, 0) / rows.length;

  // Every marker shows its own number when there are few enough to read; past
  // that the labels would collide, so the value moves to the hover tooltip.
  const dense = rows.length > 24;
  const base = {
    x: { field: "season", type: "ordinal", title: xTitle,
         axis: { labelAngle: -60, labelOverlap: true } },
    y: { field: "value", type: "quantitative", title: label,
         scale: { zero: isRate ? false : true } },
    tooltip: [{ field: "label", title: "When" },
              { field: "value", title: label,
                format: isRate ? ".1%" : ".3" }],
  };
  const spec = {
    layer: [
      { mark: { type: "line", point: rows.length <= 40, strokeWidth: 2 },
        encoding: { ...base, color: { value: token("--c1") } } },
      ...(dense ? [] : [{
        mark: { type: "text", dy: -11, fontSize: 10.5, fontWeight: 600,
                color: token("--accent-2") },
        encoding: { ...base,
          text: { field: "value", type: "quantitative",
                  format: isRate ? ".1%" : ".1f" } } }]),
      // The reference line is what makes a trend readable: without it every
      // series looks like it is going somewhere.
      ...(mean != null ? [{
        mark: { type: "rule", strokeDash: [5, 4], strokeWidth: 1.5,
                color: token("--c2") },
        encoding: { y: { datum: mean, type: "quantitative" } } }] : []),
    ],
  };

  return h("section", { className: "block" },
    h("h3", null, "Trend"),
    h("div", { className: "controls no-print" },
      h("label", { className: "field" }, "Stat",
        h("select", { value: stat, onChange: (e) => setStat(e.target.value) },
          TREND_STATS.map(([k, lab]) => h("option", { key: k, value: k }, lab)))),
      h("label", { className: "field" }, "By",
        h("select", { value: grain, disabled: !canGame,
            onChange: (e) => setGrain(e.target.value) },
          h("option", { value: "season" }, "season"),
          h("option", { value: "game", disabled: !canGame }, "game"))),
      grain === "game" && h("label", { className: "field" }, "Season",
        h("select", { value: gameSeason ?? "",
            onChange: (e) => setGameSeason(Number(e.target.value)) },
          seasons.map((r) => h("option", { key: r.season, value: r.season },
            r.season_label || r.season))))),
    !canGame && h("p", { className: "faint", style: { fontSize: 11, marginTop: -4 } },
      "Shooting percentages are shown by season only \u2014 a single game's "
      + "percentage is a different, far noisier quantity."),
    grain === "game" && games === null ? h(Spinner, null) :
    rows.length === 0 ? h("p", { className: "muted" }, "Nothing recorded for this view.") :
    h("figure", { className: "figure" },
      h("figcaption", null,
        `${playerName} \u2014 ${label.toLowerCase()} by `
        + `${grain === "season" ? "season" : "game"}, ${gameType.replace("_", "-")}. `
        + `Dashed line is the ${grain === "season" ? "career" : "season"} average, `
        + `${isRate ? pct(mean) : fmt(mean, 1)}.`),
      h(Chart, { spec, rows, width: 640 })));
}

// Similar players — the clustering view's geometry, asked a different
// question. Same frame, same z-scoring, same sign-pinned SVD; "near" here is
// Euclidean distance in that space, which is a statement you can argue with.
const SIM_VARS = [
  ["ppg", "Points"], ["rpg", "Rebounds"], ["apg", "Assists"],
  ["spg", "Steals"], ["bpg", "Blocks"], ["fg3mpg", "Threes"],
  ["topg", "Turnovers"], ["mpg", "Minutes"], ["ts_pct", "True shooting %"],
  ["fg_pct", "Field goal %"], ["usage_pct", "Usage rate"],
];

function SimilarPlayers({ playerId, onOpenPlayer }) {
  const [vars, setVars] = useState(["ppg", "rpg", "apg", "ts_pct", "mpg"]);
  const [d, setD] = useState(null);
  useEffect(() => {
    if (vars.length < 2) { setD(null); return; }
    setD("loading");
    post("/api/model/similar", {
      player_id: playerId, variables: vars,
      season_from: 2015, season_to: 2025, min_games: 41, n: 10 })
      .then(setD).catch((e) => setD({ error: String(e) }));
  }, [playerId, vars.join(",")]);
  const toggle = (k) => setVars((v) =>
    v.includes(k) ? v.filter((x) => x !== k) : v.length < 8 ? [...v, k] : v);
  return h("section", { className: "block" },
    h("h3", null, "Similar players"),
    h("div", { className: "panel", style: { marginBottom: 10 } },
      h("div", { className: "vgrid" }, SIM_VARS.map(([k, lab]) =>
        h("button", { key: k, className: "chip" + (vars.includes(k) ? " on" : ""),
            onClick: () => toggle(k) },
          h("span", { className: "chip-label" }, lab))))),
    vars.length < 2
      ? h("p", { className: "muted" }, "Pick at least two stats.")
      : d === "loading" || !d ? h(Spinner, null)
      : d.error ? h("p", { className: "err" }, d.error)
      : h("div", null,
          h("p", { className: "faint", style: { fontSize: 12, marginTop: 0 } },
            d.method, " ", h("b", null, d.warning)),
          h("div", { className: "panel scroll-x" },
            h("table", { className: "data" },
              h("thead", null, h("tr", null,
                h("th", { className: "name" }, "Player"),
                h("th", null, "Distance"),
                d.variables.map((v) => h("th", { key: v.key }, v.label)))),
              h("tbody", null, [
                h("tr", { key: "target", className: "win" },
                  h("td", { className: "name" },
                    h("b", null, d.target.player_name), " (this player)"),
                  h("td", null, "\u2014"),
                  d.variables.map((v) => h("td", { key: v.key },
                    /_pct$/.test(v.key) ? pct(d.target.stats[v.key])
                                        : fmt(d.target.stats[v.key], 1)))),
                ...d.neighbours.map((n) =>
                  h("tr", { key: n.player_id, style: { cursor: "pointer" },
                      onClick: () => onOpenPlayer && onOpenPlayer(n.player_id) },
                    h("td", { className: "name" },
                      h("a", { href: "#", onClick: (e) => e.preventDefault() },
                        n.player_name)),
                    h("td", null, fmt(n.distance, 2)),
                    d.variables.map((v) => h("td", { key: v.key },
                      /_pct$/.test(v.key) ? pct(n.stats[v.key])
                                          : fmt(n.stats[v.key], 1))))),
              ]))),
          h("p", { className: "faint", style: { fontSize: 11 } },
            `Across ${d.pool} players with at least ${d.min_games} games in `
            + `${d.season_from}\u2013${d.season_to}.`)));
}

// Era-adjusted: the same seasons as standard deviations from that season's
// league, with any cell the coverage data cannot support left unscored.
function EraAdjusted({ playerId }) {
  const [d, setD] = useState(null);
  const [showZ, setShowZ] = useState(true);
  useEffect(() => {
    api(`/api/explore/era-adjusted/${playerId}`).then(setD)
      .catch((e) => setD({ error: String(e) }));
  }, [playerId]);
  if (!d) return null;
  if (d.error) return h("p", { className: "err" }, d.error);
  const cls = (z) => z == null ? "" :
    z >= 2 ? "z p3" : z >= 1 ? "z p2" : z > 0.25 ? "z p1" :
    z <= -2 ? "z n3" : z <= -1 ? "z n2" : z < -0.25 ? "z n1" : "";
  return h("section", { className: "block" },
    h("h3", null, "Against the league of its own season"),
    h("p", { className: "faint", style: { fontSize: 12, marginTop: 0 } }, d.method),
    h("div", { className: "controls no-print" },
      h("label", { className: "field" }, "Show",
        h("select", { value: showZ ? "z" : "raw",
            onChange: (e) => setShowZ(e.target.value === "z") },
          h("option", { value: "z" }, "standard deviations"),
          h("option", { value: "raw" }, "raw values")))),
    h("div", { className: "panel scroll-x scroll-y" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Season"), h("th", null, "G"),
          d.columns.map((c) => h("th", { key: c.key }, c.label)))),
        h("tbody", null, d.seasons.map((s) =>
          h("tr", { key: s.season },
            h("td", { className: "name" }, s.season_label),
            h("td", { className: "faint" }, s.games_played),
            d.columns.map((c) => {
              const cell = s.stats[c.key] || {};
              if (cell.status !== "ok") {
                return h("td", { key: c.key, className: "faint",
                    title: cell.note || cell.status },
                  cell.status === "not tracked" ? "\u2014" : "n/a");
              }
              return h("td", { key: c.key,
                  className: showZ ? cls(cell.z) : "" },
                showZ ? (cell.z > 0 ? "+" : "") + fmt(cell.z, 2)
                      : (/_pct$/.test(c.key) ? pct(cell.value)
                                             : fmt(cell.value, 1)));
            })))))),
    (d.coverage_notes || []).length > 0 && h("details", { className: "step" },
      h("summary", null,
        `Why ${d.columns.length * d.seasons.length - d.usable_cells} cell(s) `
        + "are unscored"),
      h("ul", { className: "faint", style: { fontSize: 12 } },
        d.coverage_notes.map((n, i) => h("li", { key: i }, n)))));
}

function PlayerPage({ id, onOpenTeam, onOpenPlayer }) {
  const [gt, setGt] = useState("regular");
  const [d, setD] = useState(null);
  const [tab, setTab] = useState("seasons");
  const brands = useBrands();
  const tmBrand = (tid) => brandOf(brands, tid);
  useEffect(() => { setD(null);
    api(`/api/explore/player/${id}?game_type=${gt}`).then(setD).catch(() => setD(false));
  }, [id, gt]);
  if (d === false) return h("p", { className: "err" }, "player not found");
  if (!d) return h(Spinner, null);
  const p = d.player, c = d.career;
  const per = (k) => c.games ? c[k] / c.games : null;
  // Header card carrying the player's current franchise colour as a band,
  // then the trend, then the rest behind TABS. Previously this page stacked
  // career totals, a trend chart, a game log, era-adjusted stats, similar
  // players and a season table down one column -- 3016px tall at 1340px, so
  // the season table nobody could see was also the thing most people came for.
  const tb = brandOf(brands, (d.seasons[0] || {}).team_id);
  const tabs = [["seasons", `Seasons (${d.seasons.length})`],
                ["games", "Game log"],
                ["era", "Era-adjusted"],
                ["similar", "Similar players"]];
  return h("div", { style: teamStyle(tb) },
    h("header", { className: "hero-player" },
      tb.logo && h("img", { src: tb.logo, alt: "",
        style: { width: 44, height: 44, objectFit: "contain", flex: "0 0 44px" } }),
      h("div", { className: "who" },
        h("h1", null, p.full_name),
        h("div", { className: "where" },
          [p.position, p.height_raw, p.weight_lbs && p.weight_lbs + " lb",
           c.first_season && `${year(c.first_season)}\u2013${year(c.last_season)}`]
            .filter(Boolean).join(" \u00b7 "))),
      h("div", { className: "no-print" }, h(GameTypePicker, { value: gt, onChange: setGt }))),

    h("div", { className: "statrow", style: { marginTop: "var(--s3)" } },
      [["Games", fmt(c.games)], ["PPG", fmt(per("pts"), 1)], ["RPG", fmt(per("reb"), 1)],
       ["APG", fmt(per("ast"), 1)], ["SPG", fmt(per("stl"), 1)], ["BPG", fmt(per("blk"), 1)],
       ["Minutes", fmt(c.minutes)],
       ["College", p.college || "\u2014"],
       ["Draft", p.draft_year ? `${p.draft_year} R${p.draft_round} P${p.draft_number}` : "\u2014"]]
        .map(([k, v]) => h("div", { className: "st", key: k },
          h("div", { className: "k" }, k), h("div", { className: "v" }, v)))),

    d.seasons.length > 0 && h("div", { style: { marginTop: "var(--s4)" } },
      h(PlayerTrend, { playerId: id, seasons: d.seasons, gameType: gt,
                       playerName: p.full_name })),

    h("div", { className: "tabs", role: "tablist" }, tabs.map(([k, lab]) =>
      h("button", { key: k, role: "tab", "aria-selected": tab === k,
                    onClick: () => setTab(k) }, lab))),

    tab === "seasons" && h("div", { className: "table-full scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          ["Season", "Tm", "G", "MPG", "PPG", "RPG", "APG", "SPG", "BPG", "3PM",
           "TOV", "FG%", "3P%", "FT%", "TS%"]
            .map((x) => h("th", { key: x, className: x === "Season" ? "name" : "" }, x)))),
        h("tbody", null, d.seasons.map((sn, i) => h("tr", { key: i },
          h("td", { className: "name" }, sn.season_label),
          h("td", null, h("a", { href: "#", title: tmBrand(sn.team_id).abbrev || "",
              onClick: (e) => { e.preventDefault(); onOpenTeam(sn.team_id, sn.season); } },
            h(Crest, { brand: tmBrand(sn.team_id), size: "xs", alt: "" }),
            h("span", { style: { marginLeft: 5 } },
              tmBrand(sn.team_id).abbrev || sn.team_id))),
          h("td", null, fmt(sn.games_played)), h("td", null, fmt(sn.mpg)),
          h("td", null, fmt(sn.ppg)), h("td", null, fmt(sn.rpg)), h("td", null, fmt(sn.apg)),
          h("td", null, fmt(sn.spg)), h("td", null, fmt(sn.bpg)), h("td", null, fmt(sn.fg3mpg)),
          h("td", null, fmt(sn.topg)), h("td", null, pct(sn.fg_pct)),
          h("td", null, pct(sn.fg3_pct)), h("td", null, pct(sn.ft_pct)),
          h("td", null, pct(sn.ts_pct))))))),

    tab === "games"   && h(PlayerGameLog, { playerId: id, gameType: gt }),
    tab === "era"     && h(EraAdjusted, { playerId: id }),
    tab === "similar" && h(SimilarPlayers, { playerId: id, onOpenPlayer: onOpenPlayer }));
}

// ---------------------------------------------------------------- team
const ROSTER_COLS = [
  ["games_played", "G", "int"], ["mpg", "MPG", "num"],
  ["ppg", "PPG", "num"], ["rpg", "RPG", "num"], ["apg", "APG", "num"],
  ["spg", "SPG", "num"], ["bpg", "BPG", "num"], ["fg3mpg", "3PM", "num"],
  ["topg", "TO", "num"],
  ["fg_pct", "FG%", "pct"], ["fg3_pct", "3P%", "pct"], ["ft_pct", "FT%", "pct"],
  ["ts_pct", "TS%", "pct"], ["efg_pct", "eFG%", "pct"],
  ["pts_per36", "PTS/36", "num"], ["reb_per36", "REB/36", "num"],
  ["ast_per36", "AST/36", "num"],
];
const cell = (v, kind) => kind === "pct" ? pct(v) : kind === "int" ? fmt(v, 0) : fmt(v, 1);

function TeamPage({ id, season, onOpenPlayer, onOpenGame }) {
  const [gt, setGt] = useState("regular");
  const [sea, setSea] = useState(season || null);
  const [d, setD] = useState(null);
  const [sort, setSort] = useState("mpg");
  const [dir, setDir] = useState(1);
  const [tab, setTab] = useState("roster");
  useEffect(() => { setD(null);
    const q = sea ? `&season=${sea}` : "";
    api(`/api/explore/team/${id}?game_type=${gt}${q}`).then((x) => { setD(x); setSea(x.season); })
      .catch(() => setD(false));
  }, [id, gt, sea]);
  if (d === false) return h("p", { className: "err" }, "team not found");
  if (!d) return h(Spinner, null);
  const t = d.team, rec = d.record || {}, b = d.brand || {};
  const losses = (rec.games || 0) - (rec.wins || 0);
  const by = (k) => () => { if (sort === k) setDir(-dir); else { setSort(k); setDir(1); } };
  const roster = [...d.roster].sort((x, y) =>
    dir * ((y[sort] ?? -Infinity) - (x[sort] ?? -Infinity)));

  // Hero field, compact stat strip, then TABS. Roster and results used to be
  // two long tables stacked down one column, so a team page was ~1400px tall
  // and you scrolled past 21 roster rows to reach the schedule. They are peers,
  // not a sequence, which is what a tab strip says.
  const tabs = [["roster", `Roster (${d.roster.length})`],
                ["results", `Results (${(d.games || []).length})`]];
  return h("div", { className: "team-page", style: teamStyle(b) },
    h("header", { className: "hero-team" },
      b.logo && h("img", { className: "crest", src: b.logo, alt: `${t.full_name} logo` }),
      h("div", { className: "who" },
        h("h1", null, t.full_name),
        h("div", { className: "where" },
          [t.abbreviation, (t.conference || "").trim(), (t.division || "").trim()]
            .filter(Boolean).join(" \u00b7 "))),
      h("div", { className: "rec" },
        h("div", { className: "big" }, rec.games ? record(rec.wins, losses) : "\u2014"),
        h("div", { className: "sub" },
          `${sea ? sea + "\u2013" + String(sea + 1).slice(2) : ""} `
          + `${gt.replace("_", "-")}`
          + (rec.games ? ` \u00b7 ${pct(rec.wins / rec.games)}` : "")))),

    h("div", { className: "statrow", style: { marginTop: "var(--s3)" } },
      (d.facts || []).filter((f) => f.value != null).map((f) =>
        h("div", { className: "st", key: f.label },
          h("div", { className: "k" }, f.label),
          h("div", { className: "v" }, String(f.value))))),

    h("div", { className: "toolbar", style: { marginTop: "var(--s4)" } },
      h("div", { className: "grp" },
        h(SeasonPicker, { seasons: { seasons: d.seasons.map((x) => ({ season: x, label: x })) },
                          mode: "single", season: sea, setSeason: setSea,
                          allowRange: false })),
      h("div", { className: "grp" }, h(GameTypePicker, { value: gt, onChange: setGt })),
      h("div", { className: "spacer" }),
      h("div", { className: "count" },
        `${d.roster.length} players \u00b7 ${(d.games || []).length} games`)),

    h("div", { className: "tabs", role: "tablist" }, tabs.map(([k, lab]) =>
      h("button", { key: k, role: "tab", "aria-selected": tab === k,
                    onClick: () => setTab(k) }, lab))),

    tab === "roster" && h("div", { className: "table-full scroll-x" },
      h("table", { className: "data sortable" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Player"),
          ROSTER_COLS.map(([k, lab]) =>
            h("th", { key: k, onClick: by(k), style: { cursor: "pointer" },
                      className: sort === k ? "sorted" : "" },
              lab, sort === k ? (dir > 0 ? " \u25be" : " \u25b4") : "")))),
        h("tbody", null, roster.map((p) => h("tr", { key: p.player_id },
          h("td", { className: "name" },
            h("a", { href: "#", onClick: (e) => { e.preventDefault(); onOpenPlayer(p.player_id); } },
              p.player_name)),
          ROSTER_COLS.map(([k, _l, kind]) =>
            h("td", { key: k, className: sort === k ? "sorted-cell" : "" },
              cell(p[k], kind)))))))),

    tab === "results" && h("div", { className: "table-full scroll-x" },
      h("table", { className: "data results" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Date"),
          h("th", { style: { textAlign: "left" } }, "Matchup"),
          h("th", null, "Result"), h("th", null, "Score"), h("th", null, "Margin"))),
        h("tbody", null, (d.games || []).map((g) => {
          const m = (g.points_for ?? 0) - (g.points_against ?? 0);
          return h("tr", { key: g.game_id, className: g.won ? "win" : "loss",
              style: { cursor: "pointer" },
              onClick: () => onOpenGame && onOpenGame(g.game_id, sea) },
            h("td", { className: "name faint" }, g.game_date),
            h("td", { className: "matchup" },
              h("span", { className: "ha" }, g.is_home ? "vs" : "at"),
              h("span", { className: "opp" }, g.opponent)),
            h("td", null, h(WL, { won: g.won })),
            h("td", { className: "score" },
              h("span", { className: g.won ? "score-win" : "score-lose" },
                fmt(g.points_for, 0)),
              h("span", { className: "faint" }, "\u2013"),
              h("span", { className: g.won ? "score-lose" : "score-win" },
                fmt(g.points_against, 0))),
            h("td", { className: "margin " + (m > 0 ? "pos" : m < 0 ? "neg" : "") },
              (m > 0 ? "+" : "") + m));
        })))));
}

// ---------------------------------------------------------------- leaderboard
const STATS = [["ppg", "Points"], ["rpg", "Rebounds"], ["apg", "Assists"],
  ["spg", "Steals"], ["bpg", "Blocks"], ["fg3mpg", "Threes"], ["mpg", "Minutes"],
  ["topg", "Turnovers"], ["ts_pct", "True shooting %"], ["efg_pct", "Effective FG%"],
  ["fg_pct", "Field goal %"], ["ft_pct", "Free throw %"]];
const COVERAGE_BASE = { ts_pct: "fga", efg_pct: "fga", fg_pct: "fga", ft_pct: "fta",
  fg3mpg: "fg3m", spg: "stl", bpg: "blk", topg: "turnover",
  mpg: "minutes_played" };

function Leaderboard({ onOpenPlayer }) {
  const [stat, setStat] = useState("ppg");
  // A real range, not a UI convenience: the server recomputes rates from summed
  // totals across the span, because averaging season averages weights a 12-game
  // season the same as an 82-game one and silently returns a different board.
  const [mode, setMode] = useState("single");
  const [season, setSeason] = useState(null);
  const [from, setFrom] = useState(null);
  const [to, setTo] = useState(null);
  const [minG, setMinG] = useState(41);
  const [gt, setGt] = useState("regular");
  const [d, setD] = useState(null);
  const brands = useBrands();
  const seasons = useSeasons(gt);
  useEffect(() => {
    if (!seasons || season != null) return;
    const l = seasons.latest;
    setSeason(l); setTo(l); setFrom(Math.max(l - 4, seasons.seasons.at(-1).season));
  }, [seasons, season]);
  const range = mode === "range";
  useEffect(() => { if (season == null) return; setD(null);
    const q = range ? `&season_from=${from}&season_to=${to}`
                    : `&season=${season}`;
    api(`/api/explore/leaderboard?stat=${stat}&min_games=${minG}&game_type=${gt}${q}`)
      .then(setD).catch(() => setD(false));
  }, [stat, season, range, from, to, minG, gt]);
  const base = COVERAGE_BASE[stat];
  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "lhs" },
        h("div", { className: "eyebrow" }, "League ranking"),
        h("h1", null, d
          ? `${d.label} \u2014 ${d.is_range ? `${d.season_from}\u2013${d.season_to}` : d.season} `
            + `${gt.replace("_", "-")} season${d.is_range ? "s" : ""}`
          : "Leaderboard"),
        // The caveats are chips, not a run-on sentence: each is a separate
        // condition on the ranking and reads as one.
        h("div", { style: { display: "flex", gap: "var(--s2)", flexWrap: "wrap",
                            marginTop: "var(--s2)" } },
          h("span", { className: "chip ghost" }, d && d.is_range
            ? `min ${minG} games across the span` : `min ${minG} games`),
          d && h("span", { className: d.is_range ? "chip info" : "chip ghost" },
            d.aggregation),
          h("span", { className: "chip ghost" }, "ties broken by player_id"))),
      h("div", { className: "meta" },
        d && h("span", null, h("b", null, d.rows ? d.rows.length : "\u2014"), "ranked"))),
    // A filter bar that belongs to the table: one row, sticky under the
    // topbar, so changing the stat does not scroll the ranking away. The old
    // `.controls` wrapped onto three ragged lines and left the screen on the
    // first scroll.
    h("div", { className: "toolbar" },
      h("div", { className: "grp" },
        h("label", { htmlFor: "lb-stat" }, "Rank by"),
        h("select", { id: "lb-stat", value: stat,
            onChange: (e) => setStat(e.target.value) },
          ((d && d.stats) || STATS.map(([k, l]) => ({ key: k, label: l })))
            .map((o) => h("option", { key: o.key, value: o.key }, o.label)))),
      h("div", { className: "grp" },
        h(SeasonPicker, { seasons, mode, setMode, season, setSeason,
                          from, setFrom, to, setTo })),
      h("div", { className: "grp" },
        h("label", { htmlFor: "lb-min" }, "Min games"),
        h("input", { id: "lb-min", type: "number", value: minG,
          style: { width: 62 },
          onChange: (e) => setMinG(Number(e.target.value) || 0) })),
      h("div", { className: "grp" }, h(GameTypePicker, { value: gt, onChange: setGt })),
      h("div", { className: "spacer" }),
      d && d.rows && h("div", { className: "count" }, `${d.rows.length} ranked`)),

    // Coverage for the stat behind the ranking, as a strip rather than its own
    // titled section: it is a caveat on this table, not a topic.
    base && h("div", { style: { marginBottom: "var(--s4)" } },
      h(CoverageStrip, { stat: base })),

    !d ? h(Spinner, null) : d === false ? h("p", { className: "err" }, "failed") :
      h("div", { className: "table-full scroll-x" },
        h(LeaderTable, { d, stat, onOpenPlayer, brands })));
}

// Every stat on the row, and any column can order it. The ranked stat still
// decides WHO is in the top N -- re-sorting reorders that set rather than
// quietly fetching a different one, which the caption says out loud.
function LeaderTable({ d, stat, onOpenPlayer, brands }) {
  const [sort, setSort] = useState(null);
  const [dir, setDir] = useState(1);
  const cols = d.columns || [];
  const by = (k) => () => { if (sort === k) setDir(-dir); else { setSort(k); setDir(1); } };
  const key = sort || stat;
  const rows = sort
    ? [...d.rows].sort((a, b) => dir * ((b[key] ?? -Infinity) - (a[key] ?? -Infinity)))
    : d.rows;
  const fmtCell = (v, f) => f === "pct" ? pct(v) : f === "int" ? fmt(v, 0) : fmt(v, 1);
  return h("div", null,
    h("p", { className: "faint", style: { fontSize: 11, margin: "0 0 8px" } },
      sort && sort !== stat
        ? `Showing the top ${d.rows.length} by ${d.label.toLowerCase()}, reordered `
          + `by ${(cols.find((c) => c.key === sort) || {}).label}. `
          + "Change \u201cRank by\u201d to choose a different top " + d.rows.length + "."
        : `The top ${d.rows.length} by ${d.label.toLowerCase()}. `
          + "Click any column to reorder them."),
    h("div", { className: "scroll-y scroll-x panel" },
      h("table", { className: "data sortable" },
        h("thead", null, h("tr", null,
          h("th", { className: "rank" }, "#"),
          h("th", { className: "name" }, "Player"),
          h("th", null, d.is_range ? "Sns" : "Tm"),
          cols.map((c) => h("th", { key: c.key, onClick: by(c.key),
              style: { cursor: "pointer" },
              className: (sort || stat) === c.key ? "sorted" : "" },
            c.label, sort === c.key ? (dir > 0 ? " \u25be" : " \u25b4") : "")))),
        h("tbody", null, rows.map((r, i) => {
          const b = brandOf(brands, r.team_id);
          return h("tr", { key: r.player_id,
                           style: r.team_id ? teamStyle(b) : undefined,
                           className: r.team_id ? "team-row" : "" },
            // `rank` draws the franchise spine from --team, which teamStyle()
            // has already put on this row.
            h("td", { className: "rank" }, i + 1),
            h("td", { className: "name" },
              h("a", { href: "#", onClick: (e) => {
                e.preventDefault(); onOpenPlayer(r.player_id); } }, r.player_name)),
            h("td", { className: "faint" },
              d.is_range ? r.seasons
                : (b.abbrev
                    ? [h(Crest, { brand: b, size: "xs", alt: "", key: "c" }),
                       h("span", { key: "a", style: { marginLeft: 6 } }, b.abbrev)]
                    : (r.team_id ?? "\u2014"))),
            cols.map((c) => h("td", { key: c.key,
                className: c.key === (sort || stat) ? "sorted-cell" : "" },
              fmtCell(r[c.key], c.format))));
        })))));
}

// ---------------------------------------------------------------- catalog
// A side panel, not a page: choosing a table should not lose the catalogue you
// were reading, and comparing two tables' fields means flicking between rows
// rather than navigating back and forth.
function TablePanel({ name, onClose }) {
  const [d, setD] = useState(null);
  useEffect(() => { setD(null);
    api(`/api/explore/table/${encodeURIComponent(name)}`).then(setD).catch(() => setD(false));
  }, [name]);
  return h("aside", { className: "side-panel" },
    h("div", { className: "side-panel-head" },
      h("div", null,
        h("div", { className: "eyebrow" },
          d && d.kind === "VIEW" ? "View" : "Table"),
        h("h3", { style: { font: "600 15px var(--serif)", textTransform: "none",
                           letterSpacing: 0, border: 0, padding: 0, margin: 0 } },
          name)),
      h("button", { className: "btn-ghost", onClick: onClose }, "Close")),
    d === false ? h("p", { className: "err" }, "failed to load") :
    !d ? h(Spinner, null) :
    h(TablePanelBody, { d, hasCoverage: d.columns.some(
      (c) => c.first_tracked_season != null || c.first_full_season != null) }));
}

function TablePanelBody({ d, hasCoverage }) {
  return h("div", { className: "side-panel-body" },
      h("div", { className: "meta-row", style: { marginTop: 0 } },
        h("span", null, h("b", null, fmt(d.rows)), " rows"),
        h("span", null, h("b", null, d.columns.length), " columns")),
      h("h4", { style: { marginTop: 14 } }, "Fields"),
      h("div", { className: "scroll-x" },
        h("table", { className: "data" },
          h("thead", null, h("tr", null,
            [["Column", "name"], ["Type", ""], ["Nullable", ""],
             // Coverage is only known for stats that come from a base table;
             // on a derived view every row was an em dash, two columns wide.
             ...(hasCoverage ? [["First seen", ""], ["First reliable", ""]] : [])]
              .map(([x, c]) => h("th", { key: x, className: c }, x)))),
          h("tbody", null, d.columns.map((c) =>
            h("tr", { key: c.column_name },
              h("td", { className: "name" }, c.column_name),
              h("td", { className: "faint" }, c.data_type),
              h("td", { className: "faint" }, c.is_nullable === "YES" ? "yes" : "no"),
              ...(hasCoverage
                ? [h("td", null, year(c.first_tracked_season)),
                   h("td", null, year(c.first_full_season))]
                : [])))))),
      !hasCoverage && h("p", { className: "faint", style: { fontSize: 11 } },
        "No season coverage recorded for these fields — coverage is tracked on "
        + "the raw stat tables (player_stats, player_advanced_stats)."),
      d.sample.length > 0 && h("div", null,
        h("h4", { style: { marginTop: 16 } }, "Sample rows"),
        h("div", { className: "scroll-x" },
        h("table", { className: "data" },
          h("thead", null, h("tr", null, Object.keys(d.sample[0])
            .map((k, i) => h("th", { key: k, className: i === 0 ? "name" : "" }, k)))),
          h("tbody", null, d.sample.map((row, i) =>
            h("tr", { key: i }, Object.keys(d.sample[0]).map((k) =>
              h("td", { key: k, className: "faint" },
                row[k] === null ? "—"
                  : typeof row[k] === "number" && !Number.isInteger(row[k])
                    ? fmt(row[k], 2)
                    : String(row[k]).slice(0, 18))))))))));
}

function Catalog() {
  const [d, setD] = useState(null);
  const [table, setTable] = useState(null);
  useEffect(() => { api("/api/explore/catalog").then(setD).catch(() => setD(false)); }, []);
  if (d === false) return h("p", { className: "err" }, "failed");
  if (!d) return h(Spinner, null);
  return h("div", { className: table ? "with-panel" : "" },
    table && h(TablePanel, { name: table, onClose: () => setTable(null) }),
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "Dataset catalog"),
      h("h2", null, "What is in this database, and how far back it can be trusted"),
      h("p", { className: "lede" },
        "Derived from the live schema on every request, so a table cannot be "
        + "added without appearing here. Pick one to see its fields."),
      h("div", { className: "meta-row" },
        h("span", null, h("b", null, d.span.seasons), " seasons"),
        h("span", null, `${d.span.first_season}–${d.span.last_season}`),
        h("span", null, h("b", null, d.coverage.length), " stats with coverage"))),
    h("section", { className: "block" },
      h("h3", null, `Tables and views — ${d.tables.length}`),
      h("div", { className: "panel scroll-y" },
        h("table", { className: "data" },
          h("thead", null, h("tr", null,
            [["Name", "name"], ["Kind", ""], ["Rows", ""], ["", ""]]
              .map(([x, c], i) => h("th", { key: i, className: c }, x)))),
          h("tbody", null, d.tables.map((t) =>
            h("tr", { key: t.table_name, style: { cursor: "pointer" },
                className: (t.internal ? "dim" : "")
                  + (table === t.table_name ? " sel" : ""),
                onClick: () => setTable(t.table_name) },
              h("td", { className: "name" },
                h("a", { href: "#", onClick: (e) => e.preventDefault() }, t.table_name)),
              h("td", { className: "faint" },
                t.table_type === "BASE TABLE" ? "table" : "view"),
              h("td", null, fmt(t.rows)),
              h("td", null, t.internal
                ? h("span", { className: "tag" }, "internal") : ""))))))),
    h("section", { className: "block" },
      h("h3", null, "When each stat starts"),
      h("p", { className: "muted", style: { marginTop: -6 } },
        "A NULL means “not recorded”, not “did not happen”. Use the first "
        + "reliable season for anything spanning eras."),
      h("div", { className: "panel scroll-y" },
        h("table", { className: "data" },
          h("thead", null, h("tr", null,
            ["Stat", "Table", "First seen", "First reliable"]
              .map((x) => h("th", { key: x, className: x === "Stat" ? "name" : "" }, x)))),
          h("tbody", null, d.coverage.map((c) =>
            h("tr", { key: c.table + c.stat },
              h("td", { className: "name" }, c.stat),
              h("td", { className: "faint" }, c.table.replace("player_", "")),
              h("td", null, year(c.first_tracked_season)),
              h("td", null, year(c.first_full_season)))))))),
    h("section", { className: "block" },
      h("h3", null, "Coverage by season"),
      h("div", { className: "grid two" },
        ["stl", "blk", "turnover", "fg3m", "oreb", "plus_minus"].map((st) =>
          h("div", { className: "panel", key: st },
            h("h4", null, st),
            h(CoverageStrip, { stat: st }))))));
}

// ---------------------------------------------------------------- shell
// The Explorer view lives in the URL too, so a team page or the catalog can be
// linked, reloaded and printed -- the last of which is how the coverage shading
// gets verified in an actual PDF.
// Returns null when the URL names no view, so the caller can fall back to the
// section's own entry. Defaulting to "leaderboard" here made every section
// think it had been deep-linked to the leaderboard.
function readExploreUrl() {
  const u = new URLSearchParams(location.search);
  const kind = u.get("view");
  if (!kind) return null;
  const id = Number(u.get("id"));
  const season = Number(u.get("season"));
  if (["player", "team"].includes(kind) && Number.isFinite(id) && id > 0)
    return { kind, id, season: Number.isFinite(season) && season ? season : undefined };
  // A box score needs its parent team too, so "back to results" lands on the
  // season you came from rather than dumping you at the franchise list.
  if (kind === "game" && Number.isFinite(id) && id > 0) {
    const teamId = Number(u.get("team"));
    return { kind, id, teamId: Number.isFinite(teamId) && teamId > 0 ? teamId : undefined,
             season: Number.isFinite(season) && season ? season : undefined };
  }
  return ["leaderboard", "teams", "catalog", "search"].includes(kind)
    ? { kind } : null;
}

export function Explorer({ section }) {
  // The nav owns which section you are in; this component owns drill-down
  // within it (a leaderboard row into a player, a roster row into a team).
  // One module serves four rail destinations; `section` says which page type
  // it opens on. `explore` is kept as an alias so an old bookmark that reaches
  // this component directly still lands somewhere sensible.
  const entry = { leaders: "leaderboard", catalog: "catalog",
                  teams: "teams", players: "search",
                  explore: "teams" }[section] || "teams";
  // A deep link wins; otherwise the section's entry view.
  const [view, setView] = useState(() => readExploreUrl() || { kind: entry });
  // Reset only when the SECTION changes, not on every mount -- resetting on
  // mount discarded the URL's view and made deep links unreachable.
  const firstRun = useRef(true);
  useEffect(() => {
    if (firstRun.current) { firstRun.current = false; return; }
    setView({ kind: entry });
  }, [entry]);

  // The nav writes `v`; this only records drill-down within a section, so a
  // player or team page stays linkable without duplicating the section in the
  // URL twice under two different names.
  useEffect(() => {
    const u = new URLSearchParams(location.search);
    u.delete("mode");
    if (["player", "team", "teams", "game"].includes(view.kind)) {
      u.set("view", view.kind);
      if (view.id) u.set("id", String(view.id)); else u.delete("id");
      if (view.season) u.set("season", String(view.season)); else u.delete("season");
      if (view.kind === "game" && view.teamId) u.set("team", String(view.teamId));
      else u.delete("team");
    } else {
      u.delete("view"); u.delete("id"); u.delete("season"); u.delete("team");
    }
    history.replaceState(null, "", "?" + u.toString());
  }, [view]);
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);

  useEffect(() => {
    if (q.trim().length < 2) { setHits(null); return; }
    const t = setTimeout(() => {
      api(`/api/explore/search?q=${encodeURIComponent(q.trim())}`).then(setHits).catch(() => setHits(null));
    }, 220);
    return () => clearTimeout(t);
  }, [q]);

  const openPlayer = useCallback((id) => { setView({ kind: "player", id }); setQ(""); setHits(null); }, []);
  const openTeam = useCallback((id, season) => {
    setView(id == null ? { kind: "teams" } : { kind: "team", id, season });
    setQ(""); setHits(null);
  }, []);

  return h("div", null,
    // The search box moved to the topbar, where it is reachable from every
    // page instead of only from this module. What stays is the way back out of
    // a drill-down, which only makes sense while you are inside one.
    // A box score renders its own "back to results", which knows the team you
    // came from; a second generic one above it was two buttons doing almost
    // the same thing.
    ["player", "team"].includes(view.kind) && h("div", { className: "no-print",
        style: { marginBottom: "var(--s3)" } },
      h("button", { className: "btn-ghost",
        onClick: () => setView({ kind: entry }) }, "\u2190 Back")),
    hits && (hits.players.length + hits.teams.length > 0) && h("div", { className: "panel no-print",
        style: { marginBottom: 18 } },
      h("h4", null, "Matches"),
      h("table", { className: "data" }, h("tbody", null,
        hits.players.map((p) => h("tr", { key: "p" + p.player_id },
          h("td", { className: "name" },
            h("a", { href: "#", onClick: (e) => { e.preventDefault(); openPlayer(p.player_id); } },
              p.full_name)),
          h("td", null, p.position || "—"),
          h("td", null, `${p.first_season}–${p.last_season}`),
          h("td", null, fmt(p.games) + " g"))),
        hits.teams.map((t) => h("tr", { key: "t" + t.team_id },
          h("td", { className: "name" },
            h("a", { href: "#", onClick: (e) => { e.preventDefault(); openTeam(t.team_id); } },
              t.full_name)),
          h("td", null, t.abbreviation), h("td", null, t.conference || "—"), h("td", null, ""))))) ),
    view.kind === "player" && h(PlayerPage, { id: view.id, onOpenTeam: openTeam,
                                              onOpenPlayer: openPlayer }),
    view.kind === "team" && h(TeamPage, { id: view.id, season: view.season,
      onOpenPlayer: openPlayer,
      onOpenGame: (gid, season) => setView({ kind: "game", id: gid,
                                             teamId: view.id, season }) }),
    view.kind === "search" && h(PlayersLanding, { onOpenPlayer: openPlayer }),
    view.kind === "leaderboard" && h(Leaderboard, { onOpenPlayer: openPlayer }),
    view.kind === "catalog" && h(Catalog, null),
    view.kind === "teams" && h(SearchLanding, { onOpenPlayer: openPlayer,
                                                onOpenTeam: openTeam }),
    view.kind === "game" && h(BoxScore, { gameId: view.id,
      onOpenPlayer: openPlayer,
      onBack: () => setView(view.teamId
        ? { kind: "team", id: view.teamId, season: view.season }
        : { kind: "teams" }) }));
}

// TeamList (a flat, sortable 30-row franchise table) was removed here: the
// conference/division view below shows the same thirty teams in the shape
// the list is actually read in, and keeping both meant two pages claiming
// to be the teams page.

// Entry state for "Players & teams". The section has real pages behind it, but
// they were only reachable by already knowing a player's name, so this shows a
// way in that does not require one.
// Landing for "Players & teams": the full franchise table, sortable.
//
// Deliberate rather than arbitrary — the previous landing picked twelve players
// by minutes for no reason the reader could see, and buried the thing the
// section is actually organised around. Thirty teams is the whole population,
// it fits on one screen, and every row is a door into that team's seasons.
const DIVISION_ORDER = {
  East: ["Atlantic", "Central", "Southeast"],
  West: ["Northwest", "Pacific", "Southwest"],
};

function SearchLanding({ onOpenPlayer, onOpenTeam, hits }) {
  const [d, setD] = useState(null);
  useEffect(() => { api("/api/explore/teams").then(setD).catch(() => setD(false)); }, []);
  if (d === false) return h("p", { className: "err" }, "failed to load teams");
  if (!d) return h(Spinner, null);

  // A flat 30-row table sorted by franchise win% put the Spurs on top and said
  // nothing about the league as it stands. Conference, then division, then last
  // season's record is the shape this list is actually read in.
  const byConf = {};
  for (const t of d.teams) {
    const conf = (t.conference || "").trim() || "Other";
    const div = (t.division || "").trim() || "Other";
    ((byConf[conf] = byConf[conf] || {})[div] =
      byConf[conf][div] || []).push(t);
  }
  const confs = ["East", "West", ...Object.keys(byConf).filter(
    (c) => c !== "East" && c !== "West")].filter((c) => byConf[c]);

  const row = (t) => {
    const b = t.brand || {};
    const wp = t.last_win_pct;
    // A colour EDGE, a crest and the last-season record, which is what the
    // list is read for. The edge is a td rather than a border so it reads as
    // a deliberate rail at the row's full height.
    return h("tr", { key: t.team_id, className: "team-row trow",
        style: { cursor: "pointer", ...teamStyle(b) },
        onClick: () => onOpenTeam(t.team_id) },
      h("td", { className: "edge", "aria-hidden": "true" }),
      h("td", { className: "name" },
        h(Crest, { brand: b, size: "sm", alt: "" }),
        h("span", { className: "tname" }, t.full_name)),
      h("td", { className: "faint abbr" }, t.abbreviation),
      h("td", { className: "rec" },
        t.last_wins == null ? "\u2014" : record(t.last_wins, t.last_losses)),
      h("td", { className: wp == null ? "" : wp >= 0.5 ? "pos" : "neg" },
        wp == null ? "\u2014" : wp.toFixed(3)),
      h("td", { className: "faint" },
        t.win_pct != null ? t.win_pct.toFixed(3) : "\u2014"));
  };

  const head = h("thead", null, h("tr", null,
    h("th", { className: "edge", "aria-hidden": "true" }),
    h("th", { className: "name" }, "Team"), h("th", null, "Abbr"),
    h("th", null, `${d.last_season}\u2013${String(d.last_season + 1).slice(2)}`),
    h("th", null, "Win %"), h("th", null, "All-time")));

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "lhs" },
        h("div", { className: "eyebrow" }, "League"),
        h("h1", null, "Teams"),
        h("p", { className: "lede" },
          `Thirty franchises by conference and division, ordered by their ` +
          `${d.last_season}\u2013${String(d.last_season + 1).slice(2)} record. ` +
          "Open one for its seasons, roster, results and box scores.")),
      h("div", { className: "meta" },
        h("span", null, h("b", null, d.teams.length), "franchises"),
        h("span", null, h("b", null, "6"), "divisions"))),
    h("div", { className: "conf" }, confs.map((conf) => h("div", { key: conf },
      h("div", { className: "conf-head" },
        h("h3", null, conf === "East" ? "Eastern Conference"
                      : conf === "West" ? "Western Conference" : conf),
        h("span", { className: "count" },
          `${Object.values(byConf[conf]).flat().length} teams`)),
      (DIVISION_ORDER[conf] || Object.keys(byConf[conf]).sort())
        .filter((div) => byConf[conf][div])
        .map((div) => h("div", { key: div, className: "div-block" },
          h("div", { className: "dh" }, div),
          h("div", { className: "table-full scroll-x" },
            h("table", { className: "data" }, head,
              h("tbody", null,
                [...byConf[conf][div]]
                  .sort((a, b) => (b.last_win_pct ?? -1) - (a.last_win_pct ?? -1))
                  .map(row))))))))));
}

// =========================================================== PLAYERS ======
// The full-stats directory: everyone, every view, every column sortable.
//
// This is a DIRECTORY and Leaderboards is a RANKING. The two pages were built
// to look alike twice, so the split is structural rather than cosmetic: here
// nothing decides who is in the table except the filters, and the client sorts
// by any column; there, each stat picks its own top ten and states a qualifier.
//
// Comparison lives here now -- pick up to four and compare in place -- instead
// of a separate destination that made you find the players a second time.
const PV_TABS = [["per_game", "Per game"], ["totals", "Totals"],
                 ["per36", "Per 36"], ["advanced", "Advanced"],
                 ["shooting", "Shooting"]];
// "Core" is what the view is for; "All" is every column it offers.
const PV_CORE = {
  per_game: ["games_played", "mpg", "ppg", "rpg", "apg"],
  totals:   ["games_played", "minutes", "pts", "reb", "ast"],
  per36:    ["games_played", "mpg", "pts_per36", "reb_per36", "ast_per36"],
  advanced: ["games_played", "usage_pct", "ts_pct_adv", "net_rating", "pie"],
  shooting: ["games_played", "fga", "fg_pct", "fg3_pct", "ft_pct", "ts_pct"],
};

function PlayersLanding({ onOpenPlayer }) {
  const [view, setView] = useState("per_game");
  const [d, setD] = useState(null);
  const [gt, setGt] = useState("regular");
  const [minG, setMinG] = useState(20);
  const [q, setQ] = useState("");
  const [team, setTeam] = useState("");
  const [pos, setPos] = useState("");
  const [sort, setSort] = useState(null);
  const [dir, setDir] = useState(1);
  const [cols, setCols] = useState(null);          // null = every column
  const [pickOpen, setPickOpen] = useState(false);
  const [sel, setSel] = useState([]);
  const [showCmp, setShowCmp] = useState(false);
  const [mode, setMode] = useState("single");
  const [season, setSeason] = useState(null);
  const [from, setFrom] = useState(null);
  const [to, setTo] = useState(null);
  const seasons = useSeasons(gt);

  useEffect(() => {
    setD(null); setSort(null); setCols(null);
    const p = new URLSearchParams({ view, game_type: gt, min_games: String(minG) });
    if (mode === "range" && from && to) { p.set("season_from", from); p.set("season_to", to); }
    else if (season) p.set("season", season);
    api(`/api/players?${p}`).then(setD).catch(() => setD(false));
  }, [view, gt, minG, mode, season, from, to]);

  if (d === false) return h("p", { className: "err" }, "failed to load players");

  const allKeys = d ? d.columns.map((c) => c.key) : [];
  const shown = cols || allKeys;
  const teams = d ? [...new Map(d.rows.filter((r) => r.brand && r.brand.abbrev)
      .map((r) => [r.brand.abbrev, r.brand])).values()]
      .sort((x, y) => x.abbrev.localeCompare(y.abbrev)) : [];
  const positions = d
    ? [...new Set(d.rows.map((r) => r.position).filter(Boolean))].sort() : [];

  let rows = d ? d.rows : [];
  if (q.trim()) {
    const t = q.trim().toLowerCase();
    rows = rows.filter((r) => (r.player_name || "").toLowerCase().includes(t));
  }
  if (team) rows = rows.filter((r) => r.brand && r.brand.abbrev === team);
  if (pos) rows = rows.filter((r) => r.position === pos);
  rows = [...rows].sort(sort
    // A blanked stat sorts LAST in either direction: it is absent, not zero,
    // and letting it sink to the bottom of an ascending sort would read as
    // "worst".
    ? (x, y) => {
        const a2 = x[sort], b2 = y[sort];
        if (a2 == null && b2 == null) return 0;
        if (a2 == null) return 1;
        if (b2 == null) return -1;
        return dir * (b2 - a2);
      }
    : (x, y) => (x.player_name || "").localeCompare(y.player_name || ""));

  const by = (k) => () => { if (sort === k) setDir(-dir); else { setSort(k); setDir(1); } };
  const toggleSel = (r) => setSel((s) => s.find((x) => x.player_id === r.player_id)
    ? s.filter((x) => x.player_id !== r.player_id)
    : s.length >= 4 ? s : [...s, { player_id: r.player_id, name: r.player_name }]);
  const cellOf = (v, f) => v == null ? "" : f === "pct" ? pct(v)
    : f === "int" ? fmt(v, 0) : fmt(v, 1);
  const visible = d ? d.columns.filter((c) => shown.includes(c.key)) : [];

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "lhs" },
        h("div", { className: "eyebrow" }, "League"),
        h("h1", null, "Players"),
        h("p", { className: "lede" },
          "Everyone who qualifies, with every column of one stat view. Sort by "
          + "any column; select up to four players to compare them in place.")),
      d && h("div", { className: "meta" },
        h("span", null, h("b", null, fmt(rows.length, 0)), "shown"),
        h("span", null, h("b", null, fmt(d.n, 0)), "qualified"))),

    h("div", { className: "tabs", role: "tablist" }, PV_TABS.map(([k, lab]) =>
      h("button", { key: k, role: "tab", "aria-selected": view === k,
                    onClick: () => setView(k) }, lab))),

    h("div", { className: "toolbar" },
      h("div", { className: "grp", style: { flex: "1 1 180px" } },
        h("input", { type: "search", value: q, style: { width: "100%" },
          placeholder: "Filter by name…", "aria-label": "Filter by name",
          onChange: (e) => setQ(e.target.value) })),
      h("div", { className: "grp" },
        h(SeasonPicker, { seasons, mode, setMode, season, setSeason,
                          from, setFrom, to, setTo })),
      h("div", { className: "grp" },
        h("label", { htmlFor: "pv-team" }, "Team"),
        h("select", { id: "pv-team", value: team,
            onChange: (e) => setTeam(e.target.value) },
          h("option", { value: "" }, "all"),
          teams.map((b) => h("option", { key: b.abbrev, value: b.abbrev }, b.abbrev)))),
      h("div", { className: "grp" },
        h("label", { htmlFor: "pv-pos" }, "Pos"),
        h("select", { id: "pv-pos", value: pos,
            onChange: (e) => setPos(e.target.value) },
          h("option", { value: "" }, "all"),
          positions.map((x) => h("option", { key: x, value: x }, x)))),
      h("div", { className: "grp" },
        h("label", { htmlFor: "pv-min" }, "Min G"),
        h("input", { id: "pv-min", type: "number", value: minG, style: { width: 54 },
          onChange: (e) => setMinG(Number(e.target.value) || 0) })),
      h("div", { className: "grp" }, h(GameTypePicker, { value: gt, onChange: setGt })),
      h("div", { className: "spacer" }),
      h("div", { className: "grp colpick" },
        h("button", { className: "btn-ghost", "aria-expanded": pickOpen,
            onClick: () => setPickOpen(!pickOpen) },
          `Columns ${shown.length}/${allKeys.length}`),
        pickOpen && d && h("div", { className: "colmenu" },
          h("div", { className: "cm-presets" },
            h("button", { className: "linkish", onClick: () => setCols(null) }, "All"),
            h("button", { className: "linkish",
              onClick: () => setCols(PV_CORE[view].filter((k) => allKeys.includes(k))) },
              "Core")),
          d.columns.map((c) =>
            h("label", { key: c.key, className: "cm-row" },
              h("input", { type: "checkbox", checked: shown.includes(c.key),
                onChange: () => setCols((cur) => {
                  const base = cur || allKeys;
                  return base.includes(c.key)
                    ? base.filter((x) => x !== c.key)
                    : allKeys.filter((x) => base.includes(x) || x === c.key);
                }) }),
              h("span", null, c.label),
              d.blanked.includes(c.key)
                && h("span", { className: "cm-na" }, "n/a"))))) ),

    sel.length > 0 && h("div", { className: "selbar no-print" },
      h("span", null, h("b", null, sel.length), " selected"),
      sel.map((x) => h("span", { key: x.player_id, className: "chip ghost" }, x.name)),
      h("div", { className: "spacer" }),
      h("button", { className: "btn", disabled: sel.length < 2,
        onClick: () => setShowCmp(true) }, `Compare ${sel.length}`),
      h("button", { className: "btn-ghost",
        onClick: () => { setSel([]); setShowCmp(false); } }, "Clear")),

    showCmp && sel.length >= 2 && h("div", { style: { marginBottom: "var(--s5)" } },
      h(Comparison, { ids: sel.map((x) => x.player_id) })),

    !d ? h(Spinner, { label: "reading the season…" })
    : h("div", null,
        h("div", { className: "table-full scroll-x pv-wrap" },
          h("table", { className: "data sortable pv" },
            h("thead", null, h("tr", null,
              h("th", { className: "pick" }, ""),
              h("th", { className: "name stick" }, "Player"),
              h("th", null, "Tm"), h("th", null, "Pos"),
              visible.map((c) =>
                h("th", { key: c.key, onClick: by(c.key),
                    style: { cursor: "pointer" },
                    className: sort === c.key ? "sorted" : "" },
                  c.label, sort === c.key ? (dir > 0 ? " ▾" : " ▴") : "")))),
            h("tbody", null, rows.map((r) =>
              h("tr", { key: r.player_id, style: teamStyle(r.brand || {}) },
                h("td", { className: "pick" },
                  h("input", { type: "checkbox",
                    "aria-label": `Select ${r.player_name}`,
                    checked: !!sel.find((x) => x.player_id === r.player_id),
                    onChange: () => toggleSel(r) })),
                h("td", { className: "name stick" },
                  h("a", { href: "#", onClick: (e) => {
                    e.preventDefault(); onOpenPlayer(r.player_id); } }, r.player_name)),
                h("td", { className: "faint" },
                  h("span", { className: "tm" },
                    h("i", null), (r.brand || {}).abbrev || "—")),
                h("td", { className: "faint" }, r.position || "—"),
                visible.map((c) =>
                  h("td", { key: c.key,
                      className: (sort === c.key ? "sorted-cell " : "")
                                 + (r[c.key] == null ? "na" : "") },
                    cellOf(r[c.key], c.format)))))))),
        d.blanked.length > 0 && h("p", { className: "era-note" },
          h("b", null, "Blank, not zero. "),
          [...new Set(d.blanked.map((k) => d.blanked_why[k]))].join("; "), ".")));
}

// Entry state for "Players & teams". The section has real pages behind it, but
// they were only reachable by already knowing a player's name, so this shows a
// way in that does not require one.
// One game's box score, opened from a team's results.
function BoxScore({ gameId, onBack, onOpenPlayer }) {
  const [d, setD] = useState(null);
  const [boxTab, setBoxTab] = useState(null);
  useEffect(() => { setD(null);
    api(`/api/explore/game/${gameId}`).then(setD).catch(() => setD(false));
  }, [gameId]);
  if (d === false) return h("p", { className: "err" }, "failed to load box score");
  if (!d) return h(Spinner, null);
  const g = d.game;
  const COLS = [["minutes_played", "MIN"], ["pts", "PTS"], ["reb", "REB"],
    ["ast", "AST"], ["stl", "STL"], ["blk", "BLK"], ["turnover", "TO"],
    ["fgm", "FGM"], ["fga", "FGA"], ["fg3m", "3PM"], ["ftm", "FTM"],
    ["fta", "FTA"], ["pf", "PF"], ["plus_minus", "\u00b1"]];
  const away = d.sides.find((x) => !x.is_home) || d.sides[0];
  const home = d.sides.find((x) => x.is_home) || d.sides[1];
  // Default to the away side so the tabs read left-to-right like the
  // scoreboard above them.
  if (boxTab === null && away) setBoxTab(away.team_id);
  const periods = (away.line || []).map((x) => x.period);
  const winnerId = g.winning_team_id;

  // A scoreboard, not a sentence. Each side carries its own colour and crest,
  // the winner's score leads in full weight, and the period-by-period line
  // sits under it -- which is how a box score is read everywhere else.
  const sideHead = (side) => h("div", { className: "sb-side",
      style: { ...teamStyle(side.brand || {}),
               opacity: winnerId && side.team_id !== winnerId ? 0.72 : 1 } },
    side.brand && side.brand.logo
      ? h("img", { className: "sb-crest", src: side.brand.logo, alt: "" }) : null,
    h("div", null,
      h("div", { className: "sb-abbr" }, side.abbreviation),
      h("div", { className: "sb-ha" }, side.is_home ? "home" : "away")),
    h("div", { className: "sb-score" + (side.team_id === winnerId ? " won" : "") },
      fmt(side.score, 0)));

  // A game opens as a RESULT: two teams, two scores, the winner marked, then
  // the line score, then the two box scores behind tabs. Previously both box
  // scores were rendered one after the other below everything else, so the
  // page was 1530px tall and the thing you clicked for was last.
  return h("div", null,
    h("div", { className: "no-print", style: { marginBottom: "var(--s3)" } },
      h("button", { className: "btn-ghost", onClick: onBack }, "\u2190 Back to results")),

    h("div", { className: "scoreboard" },
      h("div", { className: "side away", style: teamStyle(away.brand || {}) },
        away.brand && away.brand.logo
          && h("img", { src: away.brand.logo, alt: "" }),
        h("div", null,
          h("div", { className: "nm" }, away.abbreviation),
          h("div", { className: "rec" }, "away")),
        h("div", { className: "pts " + (away.team_id === winnerId ? "win" : "lose") },
          fmt(away.score, 0))),
      h("div", { className: "mid" },
        h("div", null, g.game_date),
        h("div", null, `${g.season_label} \u00b7 ${String(g.game_type).replace("_", "-")}`)),
      h("div", { className: "side", style: teamStyle(home.brand || {}) },
        home.brand && home.brand.logo
          && h("img", { src: home.brand.logo, alt: "" }),
        h("div", null,
          h("div", { className: "nm" }, home.abbreviation),
          h("div", { className: "rec" }, "home")),
        h("div", { className: "pts " + (home.team_id === winnerId ? "win" : "lose") },
          fmt(home.score, 0)))),

    h("div", { className: "statrow", style: { marginTop: "var(--s3)" } },
      (d.context || []).map((c) =>
        h("div", { className: "st", key: c.label },
          h("div", { className: "k" }, c.label),
          h("div", { className: "v" }, String(c.value))))),

    d.has_line_score && h("div", { style: { marginTop: "var(--s4)" } },
      h("div", { className: "sect-head" }, h("h3", null, "Scoring by period")),
      h("div", { className: "table-full scroll-x" },
        h("table", { className: "data" },
          h("thead", null, h("tr", null,
            h("th", { className: "name" }, "Team"),
            periods.map((pd) => h("th", { key: pd }, pd)),
            h("th", null, "Final"))),
          h("tbody", null, [away, home].map((side) =>
            h("tr", { key: side.team_id, style: teamStyle(side.brand || {}),
                      className: "team-row" },
              h("td", { className: "name" },
                h(Crest, { brand: side.brand, size: "xs", alt: "" }),
                h("span", { style: { marginLeft: 8, fontWeight: 650 } }, side.abbreviation)),
              (side.line || []).map((x) => h("td", { key: x.period }, fmt(x.points, 0))),
              h("td", { style: { fontWeight: 700 } }, fmt(side.score, 0))))))),

    h("div", { className: "tabs", role: "tablist" }, [away, home].map((side) =>
      h("button", { key: side.team_id, role: "tab",
                    "aria-selected": boxTab === side.team_id,
                    onClick: () => setBoxTab(side.team_id) },
        `${side.abbreviation} box score`))),

    [away, home].filter((side) => side.team_id === boxTab).map((side) =>
      h("div", { key: side.team_id, className: "table-full scroll-x",
                 style: teamStyle(side.brand || {}) },
        h("table", { className: "data" },
          h("thead", null, h("tr", null,
            h("th", { className: "name" }, "Player"),
            COLS.map(([k, l]) => h("th", { key: k }, l)))),
          h("tbody", null,
            side.players.filter((pl) => !pl.is_dnp).map((pl) =>
              h("tr", { key: pl.player_id },
                h("td", { className: "name" },
                  h("a", { href: "#", onClick: (e) => {
                    e.preventDefault(); onOpenPlayer(pl.player_id); } }, pl.player_name)),
                COLS.map(([k]) => h("td", { key: k,
                    className: k === "plus_minus"
                      ? "margin " + ((pl[k] ?? 0) > 0 ? "pos" : (pl[k] ?? 0) < 0 ? "neg" : "")
                      : "" },
                  fmt(pl[k], 0))))),
            h("tr", { style: { fontWeight: 700 } },
              h("td", { className: "name" }, "Team"),
              COLS.map(([k]) => h("td", { key: k },
                side.totals[k] != null ? fmt(side.totals[k], 0) : ""))),
            side.players.some((pl) => pl.is_dnp) && h("tr", { className: "dim" },
              h("td", { className: "name" }, "Did not play"),
              // nowrap on this cell made a long inactive list set the table's
              // width, pushing PF and the plus-minus off the panel edge.
              h("td", { colSpan: COLS.length, className: "wrap" },
                side.players.filter((pl) => pl.is_dnp)
                  .map((pl) => pl.player_name).join(", ")))))))));
}


// Game log with the semantic layer's own views as toggles. per-36, shooting and
// rolling-form views were built in Phase 1 and then appeared nowhere but the
// catalogue listing; this is the page they were written for.
function PlayerGameLog({ playerId, gameType }) {
  const [view, setView] = useState("totals");
  const [d, setD] = useState(null);
  useEffect(() => { setD(null);
    api(`/api/explore/player/${playerId}/games?view=${view}`
        + `&game_type=${gameType}&limit=40`).then(setD).catch(() => setD(false));
  }, [playerId, view, gameType]);
  return h("section", { className: "block" },
    h("h3", null, "Recent games"),
    h("div", { className: "controls no-print" },
      (d && d.views ? d.views : [{ key: "totals", label: "Box score" }]).map((v) =>
        h("button", { key: v.key,
          className: "btn-ghost" + (v.key === view ? " btn-accent" : ""),
          onClick: () => setView(v.key) }, v.label))),
    !d ? h(Spinner, null) : d === false ? h("p", { className: "err" }, "failed") :
    d.rows.length === 0 ? h("p", { className: "muted" }, "No games in this view.") :
    h("div", { className: "panel scroll-y scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Date"),
          h("th", null, "MIN"),
          d.columns.map((c, i) => h("th", { key: c },
            (d.labels && d.labels[i]) || c)))),
        h("tbody", null, d.rows.map((r) =>
          h("tr", { key: r.game_id },
            h("td", { className: "name" }, r.game_date),
            h("td", null, fmt(r.minutes_played, 0)),
            d.columns.map((c) => h("td", { key: c },
              /pct/.test(c) ? pct(r[c]) : fmt(r[c], 1)))))))),
    d && d.source_view && h("p", { className: "faint", style: { fontSize: 11 } },
      "from ", h("code", null, d.source_view)));
}
