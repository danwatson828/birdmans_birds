// Player comparison and trade analysis (design spec items 14 and 15).
//
// Both read values computed elsewhere rather than recomputing them: the
// comparison uses the explorer's season totals and the trade uses the draft
// board's own z-scores. A player who is worth one thing on the draft board and
// another in a trade would be a defect with no way to tell which was right.
import { h, useState, useEffect, useCallback, api, post, fmt, pct, Spinner,
         useBrands, brandOf, Crest } from "/static/lib.js";
import { NarrateButton } from "/static/narrate.js";

function PlayerSearch({ label, onPick, chosen, onDrop }) {
  const [q, setQ] = useState("");
  const [hits, setHits] = useState(null);
  useEffect(() => {
    if (q.trim().length < 2) { setHits(null); return; }
    const t = setTimeout(() => {
      api(`/api/explore/search?q=${encodeURIComponent(q.trim())}`)
        .then((d) => setHits((d.players || []).slice(0, 6)))
        .catch(() => setHits(null));
    }, 200);
    return () => clearTimeout(t);
  }, [q]);
  return h("div", { className: "picker" },
    h("div", { className: "div-head" }, label),
    h("div", { className: "chosen" },
      chosen.length === 0
        ? h("span", { className: "faint" }, "nobody yet")
        : chosen.map((p) => h("button", { key: p.player_id, className: "chip on",
            onClick: () => onDrop(p) }, p.full_name, h("span", { className: "x" }, "×")))),
    h("input", { type: "search", value: q, className: "var-search",
      placeholder: "Search a player…", onChange: (e) => setQ(e.target.value) }),
    hits && hits.length > 0 && h("div", { className: "panel", style: { padding: 0 } },
      h("table", { className: "data" }, h("tbody", null, hits.map((p) =>
        h("tr", { key: p.player_id, style: { cursor: "pointer" },
            onClick: () => { onPick(p); setQ(""); setHits(null); } },
          h("td", { className: "name" }, p.full_name),
          h("td", { className: "faint" }, p.current_team_abbreviation || "—"),
          h("td", { className: "faint" }, `${p.first_season}–${p.last_season}`),
          h("td", { className: "faint" }, `${p.games} g`)))))));
}

function Comparison({ ids }) {
  const [d, setD] = useState(null);
  useEffect(() => {
    if (ids.length < 2) { setD(null); return; }
    setD("loading");
    api(`/api/compare?ids=${ids.join(",")}`)
      .then(setD).catch((e) => setD({ error: String(e) }));
  }, [ids.join(",")]);
  if (ids.length < 2) return h("p", { className: "muted" }, "Pick at least two players.");
  if (d === "loading" || !d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);
  const fmtv = (v, f) => v == null ? "—"
    : f === "pct" ? pct(v) : f === "int" ? fmt(v, 0) : fmt(v, 1);
  return h("section", { className: "block" },
    h("h3", null, "Career, side by side"),
    h("div", { className: "panel scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Metric"),
          d.players.map((p) => h("th", { key: p.player_id },
            p.bio.full_name || p.player_id)))),
        h("tbody", null, d.metrics.map((m) =>
          h("tr", { key: m.key },
            h("td", { className: "name" }, m.label),
            m.values.map((v, i) => h("td", { key: i,
                // The better figure is marked rather than left to the reader
                // to scan for, and turnovers are marked the other way round.
                className: (v != null && m.best != null
                            && Math.abs(v - m.best) < 1e-9) ? "sorted-cell" : "" },
              fmtv(v, m.format))))))) ),
    h("div", { className: "grid two", style: { marginTop: 14 } },
      d.players.map((p) => h("div", { className: "panel", key: p.player_id },
        h("h4", null, p.bio.full_name),
        h("p", { className: "faint", style: { fontSize: 12, margin: "0 0 6px" } },
          [p.bio.position, p.bio.height_raw,
           p.bio.weight_lbs ? p.bio.weight_lbs + " lb" : null,
           p.bio.college,
           p.bio.draft_year ? `drafted ${p.bio.draft_year} #${p.bio.draft_number}` : null]
            .filter(Boolean).join(" · ")),
        p.best_season && h("p", { style: { margin: "0 0 6px", fontSize: 13 } },
          h("b", null, "Best scoring season "), p.best_season.season_label, ": ",
          `${fmt(p.best_season.ppg, 1)} ppg, ${fmt(p.best_season.rpg, 1)} rpg, `
          + `${fmt(p.best_season.apg, 1)} apg over ${p.best_season.games_played} games`),
        Object.keys(p.projection || {}).length > 0 && h("p",
          { style: { margin: 0, fontSize: 13 } },
          h("b", null, "Projected next season "),
          `${fmt(p.projection.pts, 1)} pts, ${fmt(p.projection.reb, 1)} reb, `
          + `${fmt(p.projection.ast, 1)} ast per game `,
          h("span", { className: "faint" },
            `(9-cat value ${fmt(p.projection.value_9cat, 2)})`))))));
}

// Trade selection from the actual rosters, filterable. A search box is the
// wrong control here: the candidate set is 184 known players on 12 known
// rosters, not the whole league's history, and you pick by who owns them.
function RosterPicker({ label, data, chosen, onToggle }) {
  const [fantasy, setFantasy] = useState("");
  const [pro, setPro] = useState("");
  const pool = data.players.filter((p) =>
    (!fantasy || String(p.fantasy_team_id) === fantasy)
    && (!pro || p.pro_team === pro));
  return h("div", { className: "picker" },
    h("div", { className: "div-head" }, label),
    h("div", { className: "chosen" },
      chosen.length === 0
        ? h("span", { className: "faint" }, "nobody yet")
        : chosen.map((p) => h("button", { key: p.player_id, className: "chip on",
            onClick: () => onToggle(p) }, p.name,
            h("span", { className: "x" }, "\u00d7")))),
    h("div", { className: "controls", style: { marginBottom: 8 } },
      h("label", { className: "field" }, "Fantasy team",
        h("select", { value: fantasy, onChange: (e) => setFantasy(e.target.value) },
          h("option", { value: "" }, "any"),
          data.fantasy_teams.map((t) =>
            h("option", { key: t.team_id, value: String(t.team_id) },
              t.name + (t.is_me ? " (you)" : ""))))),
      h("label", { className: "field" }, "NBA team",
        h("select", { value: pro, onChange: (e) => setPro(e.target.value) },
          h("option", { value: "" }, "any"),
          data.pro_teams.map((t) => h("option", { key: t, value: t }, t))))),
    h("div", { className: "panel scroll-y", style: { maxHeight: 230, padding: 0 } },
      h("table", { className: "data" },
        h("tbody", null, pool.map((p) =>
          h("tr", { key: p.espn_id, style: { cursor: "pointer" },
              onClick: () => onToggle(p) },
            h("td", { className: "name" }, p.name,
              !p.on_board && h("span", { className: "tag warn",
                style: { marginLeft: 6 } }, "no value")),
            h("td", { className: "faint" }, p.pro_team || "\u2014"),
            h("td", { className: "faint" }, p.position || "\u2014"),
            h("td", null, p.total_value == null ? "\u2014"
                                                : fmt(p.total_value, 2))))))));
}

// Raw stats beside z-scores for everyone in the trade. A z-score alone asks
// the reader to trust a transformation they cannot see.
function TradeRoster({ title, players, pairs }) {
  if (!players.length) return null;
  return h("div", null,
    h("div", { className: "div-head" }, title),
    h("div", { className: "panel scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Player"),
          h("th", null, "Value"),
          pairs.map((c) => h("th", { key: c.stat }, c.label)))),
        h("tbody", null, players.map((p) => [
          h("tr", { key: p.player_id + "-raw" },
            h("td", { className: "name" }, p.name),
            h("td", null, h("b", null, fmt(p.total_value, 2))),
            pairs.map((c) => h("td", { key: c.stat },
              /pct/.test(c.stat) ? pct((p.stats || {})[c.stat])
                                 : fmt((p.stats || {})[c.stat], 1)))),
          h("tr", { key: p.player_id + "-z", className: "dim" },
            h("td", { className: "name faint" }, "z-score"),
            h("td", null, ""),
            pairs.map((c) => h("td", { key: c.stat,
                className: "margin " + (((p.z || {})[c.stat] || 0) > 0 ? "pos" : "neg") },
              fmt((p.z || {})[c.stat], 2)))),
        ]).flat()))));
}

function TradeFinder() {
  const [d, setD] = useState(null);
  useEffect(() => {
    api("/api/fantasy/trade-finder?limit=15")
      .then(setD).catch((e) => setD({ error: String(e) }));
  }, []);
  if (!d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);
  return h("section", { className: "block" },
    h("h3", null, `Trade finder — ${d.found} swap`
      + `${d.found === 1 ? "" : "s"} where both lineups improve`),
    h("div", { className: "panel accent", style: { marginBottom: 10 } },
      h("p", { style: { margin: 0, fontSize: 13 } },
        h("b", null, "How both sides can gain. "), d.mechanism)),
    h("div", { className: "panel warn", style: { marginBottom: 10 } },
      h("p", { style: { margin: 0, fontSize: 13 } }, d.advisory)),
    d.found === 0
      ? h("p", { className: "muted" },
          "No swap raises both starting lineups. That is a real answer, not "
          + "an empty page — with one value function a trade is zero-sum "
          + "unless depth differs, and here it does not.")
      : h("div", { className: "panel scroll-x" },
          h("table", { className: "data" },
            h("thead", null, h("tr", null,
              h("th", null, "Shape"), h("th", { className: "name" }, "You send"),
              h("th", { className: "name" }, "You get"),
              h("th", { className: "name" }, "Their team"),
              h("th", null, "You +"), h("th", null, "They +"),
              h("th", null, "Net value"), h("th", null, "Spots"))),
            h("tbody", null, d.suggestions.map((s, i) =>
              h("tr", { key: i },
                h("td", { className: "faint" }, s.shape),
                h("td", { className: "name" },
                  s.i_send.map((p) => p.name).join(" + ")),
                h("td", { className: "name" },
                  s.i_get.map((p) => p.name).join(" + ")),
                h("td", { className: "faint" }, s.their_team),
                h("td", { className: "margin pos" }, "+" + fmt(s.my_starting_gain, 2)),
                h("td", { className: "margin pos" }, "+" + fmt(s.their_starting_gain, 2)),
                h("td", { className: "faint" }, fmt(s.net_total_value_to_me, 2)),
                h("td", { className: "faint" }, s.roster_spots_to_me)))))));
}

function Trade({ a, b }) {
  const [d, setD] = useState(null);
  const run = useCallback(() => {
    if (!a.length || !b.length) { setD(null); return; }
    setD("loading");
    post("/api/fantasy/trade", {
      side_a: { team: "You", player_ids: a.map((p) => p.player_id) },
      side_b: { team: "Them", player_ids: b.map((p) => p.player_id) },
    }).then(setD).catch((e) => setD({ error: String(e) }));
  }, [a.map((p) => p.player_id).join(","), b.map((p) => p.player_id).join(",")]);
  useEffect(() => { run(); }, [run]);
  if (!a.length || !b.length)
    return h("p", { className: "muted" }, "Put at least one player on each side.");
  if (d === "loading" || !d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);
  const missing = [...d.side_a.missing, ...d.side_b.missing];
  return h("section", { className: "block" },
    h("h3", null, "Trade"),
    h("div", { className: "panel warn", style: { marginBottom: 14 } },
      h("p", { style: { margin: 0 } }, h("b", null, "Advisory. "), d.advisory)),
    missing.length > 0 && h("p", { className: "err" },
      `Not on the draft board, so not valued: ${missing.join(", ")}. `
      + "A trade evaluated without them is incomplete."),
    h("div", { className: "scoreboard" },
      [d.side_a, d.side_b].map((side, i) =>
        h("div", { className: "sb-side", key: i },
          h("div", null,
            h("div", { className: "sb-abbr" }, side.team),
            h("div", { className: "sb-ha" },
              side.players.map((p) => p.name).join(", ") || "—")),
          h("div", { className: "sb-score" }, fmt(side.total_value, 2))))),
    // Below the resolution of the underlying z-scores, "you gain 0.1" is a
    // decisive-sounding sentence about noise. The narration brief already
    // states this threshold; the page has to agree with it.
    h("p", { style: { fontSize: 14 } },
      Math.abs(d.value_delta_a) < 0.5
        ? h("span", null, h("b", null, "This is close. "),
            `The two sides differ by ${fmt(Math.abs(d.value_delta_a), 2)} of `
            + "summed draft-board value, which is within the noise of these "
            + "z-scores. Roster fit and games remaining would decide it, and "
            + "neither is modelled here.")
        : d.value_delta_a > 0
          ? h("span", null, h("b", null, "You gain "),
              `${fmt(d.value_delta_a, 2)} of summed draft-board value.`)
          : h("span", null, h("b", null, "You give up "),
              `${fmt(-d.value_delta_a, 2)} of summed draft-board value.`),
      d.counts_note ? " " + d.counts_note : ""),
    h("div", { className: "panel scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Category"),
          h("th", null, "You send"), h("th", null, "They send"),
          h("th", null, "Net to you"))),
        h("tbody", null, d.category_deltas.map((c) =>
          h("tr", { key: c.stat },
            h("td", { className: "name" }, c.label),
            h("td", { className: "faint" }, fmt(c.a_gives, 2)),
            h("td", { className: "faint" }, fmt(c.b_gives, 2)),
            h("td", { className: "margin " + (c.a_net > 0 ? "pos" : c.a_net < 0 ? "neg" : "") },
              (c.a_net > 0 ? "+" : "") + fmt(c.a_net, 2))))))),
    h("p", { className: "faint", style: { fontSize: 12 } },
      `Gains: ${d.gained_by_a.join(", ") || "none"}. `
      + `Gives up: ${d.lost_by_a.join(", ") || "none"}. `
      + "No winner is declared, because nothing validates a trade judgement."),
    h("div", { className: "panel" },
      h(NarrateButton, { kind: "trade", label: "Get a take on this trade",
        hint: "The deltas above are computed locally. This asks the model to "
            + "read them — it can say the trade is close rather than pick a "
            + "side, and it is the only action here that costs anything.",
        body: { side_a: { team: "You", player_ids: a.map((p) => p.player_id) },
                side_b: { team: "Them", player_ids: b.map((p) => p.player_id) } } })));
}

export function CompareView() {
  const [tab, setTab] = useState("compare");
  const [a, setA] = useState([]);
  const [b, setB] = useState([]);
  const [rosters, setRosters] = useState(null);
  useEffect(() => {
    api("/api/fantasy/rosters").then(setRosters)
      .catch((e) => setRosters({ error: String(e) }));
  }, []);
  const addA = (p) => setA((x) => x.find((y) => y.player_id === p.player_id)
    ? x : [...x, p].slice(0, 4));
  const addB = (p) => setB((x) => x.find((y) => y.player_id === p.player_id)
    ? x : [...x, p].slice(0, 4));
  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "Compare"),
      h("h2", null, tab === "compare" ? "Players side by side" : "Trade analyzer"),
      h("p", { className: "lede" }, tab === "compare"
        ? "Career rates, best season and next-season projection for two to "
          + "four players, from the same queries their own pages use."
        : "An N-for-M trade valued with the draft board's own category "
          + "z-scores, so a player cannot be worth one thing there and "
          + "another here.")),
    h("div", { className: "controls no-print" },
      h("label", { className: "field" }, "View",
        h("select", { value: tab, onChange: (e) => setTab(e.target.value) },
          h("option", { value: "compare" }, "player comparison"),
          h("option", { value: "trade" }, "trade analyzer")))),
    tab === "compare"
      ? h("div", null,
          h("div", { className: "grid two" },
            h(PlayerSearch, { label: "Player A", onPick: addA, chosen: a,
              onDrop: (p) => setA((x) =>
                x.filter((y) => y.player_id !== p.player_id)) }),
            h(PlayerSearch, { label: "Player B", onPick: addB, chosen: b,
              onDrop: (p) => setB((x) =>
                x.filter((y) => y.player_id !== p.player_id)) })),
          h(Comparison, { ids: [...a, ...b].map((p) => p.player_id) }))
      : !rosters ? h(Spinner, { label: "reading the league\u2026" })
      : rosters.error
        ? h("p", { className: "err" },
            "Rosters unavailable: ", rosters.error,
            " — run `python league_sync.py` to cache all twelve.")
        : h("div", null,
            h("div", { className: "grid two" },
              h(RosterPicker, { label: "You send", data: rosters, chosen: a,
                onToggle: (p) => setA((x) =>
                  x.find((y) => y.player_id === p.player_id)
                    ? x.filter((y) => y.player_id !== p.player_id)
                    : [...x, p]) }),
              h(RosterPicker, { label: "They send", data: rosters, chosen: b,
                onToggle: (p) => setB((x) =>
                  x.find((y) => y.player_id === p.player_id)
                    ? x.filter((y) => y.player_id !== p.player_id)
                    : [...x, p]) })),
            (a.length > 0 || b.length > 0) && h("section", { className: "block" },
              h("h3", null, "Who is in this trade"),
              h("div", { className: "grid two" },
                h(TradeRoster, { title: "You send", players: a,
                                 pairs: rosters.pairs }),
                h(TradeRoster, { title: "You get", players: b,
                                 pairs: rosters.pairs }))),
            h(Trade, { a, b }),
            h(TradeFinder, null)));
}
