// Keepers, mock draft and the season recap — the three fantasy tools that
// are about a decision rather than a week.
import { h, useState, useEffect, api, post, fmt, Spinner, useBrands, brandOf, teamStyle } from "/static/lib.js";
import { NarrateButton } from "/static/narrate.js";

function Keepers() {
  const [d, setD] = useState(null);
  const [keep, setKeep] = useState(null);          // null until data arrives
  useEffect(() => {
    api("/api/fantasy/keepers").then((x) => {
      setD(x);
      setKeep(new Set((x.recommended || []).map((r) => r.name)));
    }).catch((e) => setD({ error: String(e) }));
  }, []);
  if (!d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);
  const chosen = keep || new Set();
  const toggle = (n) => setKeep((s2) => {
    const next = new Set(s2); next.has(n) ? next.delete(n) : next.add(n); return next;
  });
  // Rank by the board's own total value, so "value rank" means the same thing
  // here as on the draft board rather than being a second opinion.
  const ranked = [...d.rows].sort((x, y) =>
    (y.total_value ?? -Infinity) - (x.total_value ?? -Infinity));
  const keptValue = ranked.filter((r) => chosen.has(r.name))
    .reduce((t, r) => t + (r.total_value || 0), 0);

  return h("div", null,
    h("div", { className: "sect-head" },
      h("h2", null, "Keeper decision"),
      h("span", { className: "count" },
        `${chosen.size} kept \u00b7 ${fmt(keptValue, 2)} total value`)),
    h("div", { className: "panel accent", style: { marginBottom: "var(--s3)" } },
      h("p", { style: { margin: 0, fontSize: "var(--t-sm)" } },
        h("b", null, "The rule. "), d.rule_note)),
    h("div", { className: "panel warn", style: { marginBottom: "var(--s3)" } },
      h("p", { style: { margin: 0, fontSize: "var(--t-sm)" } },
        h("b", null, "Implied round is a proxy. "), d.proxy_note, " ", d.advisory)),
    h("div", { className: "table-full scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", { className: "pick" }, "Keep"),
          h("th", null, "#"),
          h("th", { className: "name" }, "Player"),
          h("th", null, "Pos"), h("th", null, "Board rank"),
          h("th", null, "Implied round"), h("th", null, "Total value"),
          h("th", null, "Per game"))),
        h("tbody", null, ranked.map((r, i) =>
          h("tr", { key: r.name, className: chosen.has(r.name) ? "kept" : "" },
            h("td", { className: "pick" },
              h("input", { type: "checkbox", checked: chosen.has(r.name),
                "aria-label": `Keep ${r.name}`,
                onChange: () => toggle(r.name) })),
            h("td", { className: "faint" }, r.on_board ? i + 1 : "\u2014"),
            h("td", { className: "name" }, r.name),
            h("td", { className: "faint" }, r.position || "\u2014"),
            h("td", null, r.board_rank ?? "\u2014"),
            h("td", null, r.implied_round ?? "\u2014"),
            h("td", null, r.on_board ? h("b", null, fmt(r.total_value, 2))
                                     : h("span", { className: "chip warn" }, "no value")),
            h("td", { className: "faint" },
              r.on_board ? fmt(r.per_game_value, 2) : "\u2014")))))));
}

// The draft room as a real BOARD: rounds down, twelve teams across, picks
// filling the cell in the drafting team's colour, snake order reflected by
// reversing even rounds so a column really is one team.
//
// `log` gives pick, round, team_id and is_me, which is everything the grid
// needs; nothing here is invented.
function DraftBoard({ d, brands }) {
  const teams = d.teams || 12;
  const rounds = d.rounds || 14;
  const byPick = Object.fromEntries((d.log || []).map((l) => [l.pick, l]));
  // Snake: odd rounds run left to right, even rounds right to left. Laying the
  // grid out in SEAT order (rather than pick order) is what makes a column one
  // team all the way down.
  const seatOf = (pick) => {
    const r = Math.ceil(pick / teams);
    const within = pick - (r - 1) * teams;         // 1..teams
    return r % 2 === 1 ? within : teams - within + 1;
  };
  const cells = [];
  for (let r = 1; r <= rounds; r++) {
    for (let seat = 1; seat <= teams; seat++) {
      const base = (r - 1) * teams;
      const within = r % 2 === 1 ? seat : teams - seat + 1;
      const pick = base + within;
      cells.push({ r, seat, pick, l: byPick[pick] });
    }
  }
  const brandFor = (tid) => brandOf(brands, tid);
  return h("div", { className: "db-wrap scroll-x" },
    h("div", { className: "db", style: { "--cols": teams } },
      h("div", { className: "db-corner" }, "R"),
      Array.from({ length: teams }, (_, i) =>
        h("div", { key: "h" + i, className: "db-seat" }, i + 1)),
      cells.map((c) => [
        c.seat === 1 && h("div", { key: "r" + c.r, className: "db-round" }, c.r),
        h("div", { key: `${c.r}-${c.seat}`,
            className: "db-cell" + (c.l ? " filled" : "")
                       + (c.l && c.l.is_me ? " mine" : "")
                       + (c.pick === d.pick_number ? " now" : ""),
            style: c.l ? teamStyle(brandFor(c.l.team_id)) : undefined,
            title: c.l ? `Pick ${c.pick} · ${c.l.name}` : `Pick ${c.pick}` },
          h("span", { className: "p" }, c.pick),
          c.l
            ? h("span", { className: "n" }, c.l.name)
            : c.pick === d.pick_number
              ? h("span", { className: "n on" }, "on the clock")
              : null)])));
}

function MockDraft() {
  const [picks, setPicks] = useState([]);
  const [seed, setSeed] = useState(0);
  const [d, setD] = useState(null);
  const [busy, setBusy] = useState(false);
  const brands = useBrands();
  const load = (p2, s2) => {
    setBusy(true);
    post("/api/fantasy/mock-draft", { picks: p2, seed: s2 })
      .then((r) => { setD(r); setBusy(false); })
      .catch((e) => { setD({ error: String(e) }); setBusy(false); });
  };
  useEffect(() => { load(picks, seed); }, [picks, seed]);
  if (!d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);
  return h("div", null,
    h("div", { className: "sect-head" },
      h("h2", null, d.complete
        ? `Draft complete \u2014 ${d.my_roster.length} players`
        : `Pick ${d.pick_number}, round ${d.round}`),
      h("span", { className: "count" },
        d.complete ? `value ${fmt(d.my_value, 2)}`
          : d.picks_until_my_next != null
            ? `${d.picks_until_my_next} picks until you are up`
            : "on the clock")),
    h("div", { className: "toolbar no-print" },
      h("div", { className: "grp" },
        h("label", { htmlFor: "md-seed" }, "Seed"),
        h("input", { id: "md-seed", type: "number", value: seed, style: { width: 64 },
          onChange: (e) => { setPicks([]); setSeed(Number(e.target.value) || 0); } })),
      h("button", { className: "btn-ghost", onClick: () => setPicks([]) }, "Restart"),
      picks.length > 0 && h("button", { className: "btn-ghost",
        onClick: () => setPicks((p2) => p2.slice(0, -1)) }, "Undo last pick"),
      h("div", { className: "spacer" }),
      h("div", { className: "count" }, d.note)),

    h("div", { className: "dr-grid" },
      h("div", { className: "dr-board" }, h(DraftBoard, { d, brands })),
      h("aside", { className: "dr-rail" },
        h("section", { className: "panel" },
          h("div", { className: "div-head" },
            d.complete ? "Your roster" : "Best available \u2014 take one"),
          d.complete
            ? h("ol", { className: "dr-mine" },
                d.my_roster.map((n, i) => h("li", { key: i }, n)))
            : h("ul", { className: "dr-av" }, (d.available || []).map((a) =>
                h("li", { key: a.player_id },
                  h("button", { disabled: busy,
                      onClick: () => setPicks((p2) => [...p2, a.player_id]) },
                    h("span", { className: "n" }, a.name),
                    h("span", { className: "t" }, a.pos || "\u2014"),
                    h("span", { className: "v" }, fmt(a.total_value, 2))))))),
        !d.complete && d.my_roster.length > 0 && h("section", { className: "panel" },
          h("div", { className: "div-head" },
            `Your team (${d.my_roster.length}) \u00b7 ${fmt(d.my_value, 2)}`),
          h("ol", { className: "dr-mine" },
            d.my_roster.map((n, i) => h("li", { key: i }, n)))))));
}

export function FantasyLab() {
  const [tab, setTab] = useState("keepers");
  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "Fantasy"),
      h("h2", null, "Keepers, mock draft and recap"),
      h("p", { className: "lede" },
        "The decisions that sit outside a given week: who to keep, practice "
        + "against the field before the real draft, and what the season just "
        + "gone actually cost.")),
    h("div", { className: "controls no-print" },
      h("label", { className: "field" }, "View",
        h("select", { value: tab, onChange: (e) => setTab(e.target.value) },
          h("option", { value: "keepers" }, "keeper decision"),
          h("option", { value: "draft" }, "mock draft"),
          h("option", { value: "recap" }, "season recap")))),
    tab === "keepers" ? h(Keepers, null)
      : tab === "draft" ? h(MockDraft, null)
      : h(Recap, null));
}
