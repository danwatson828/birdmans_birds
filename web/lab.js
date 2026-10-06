// Keepers, mock draft and the season recap — the three fantasy tools that
// are about a decision rather than a week.
import { h, useState, useEffect, api, post, fmt, Spinner } from "/static/lib.js";
import { NarrateButton } from "/static/narrate.js";

function Keepers() {
  const [d, setD] = useState(null);
  useEffect(() => {
    api("/api/fantasy/keepers").then(setD)
      .catch((e) => setD({ error: String(e) }));
  }, []);
  if (!d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);
  const keep = new Set((d.recommended || []).map((r) => r.name));
  return h("section", { className: "block" },
    h("h3", null, "Keeper decision"),
    // The rule is read from settings, and where the settings do not encode
    // one that is said rather than papered over with an invented formula.
    h("div", { className: "panel accent", style: { marginBottom: 10 } },
      h("p", { style: { margin: 0 } }, h("b", null, "The rule. "), d.rule_note)),
    h("div", { className: "panel warn", style: { marginBottom: 10 } },
      h("p", { style: { margin: 0, fontSize: 13 } },
        h("b", null, "Implied round is a proxy. "), d.proxy_note,
        " ", d.advisory)),
    h("div", { className: "panel scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Player"), h("th", null, "Pos"),
          h("th", null, "Board rank"), h("th", null, "Implied round"),
          h("th", null, "Total value"), h("th", null, "Per game"),
          h("th", { className: "name" }, ""))),
        h("tbody", null, d.rows.map((r) =>
          h("tr", { key: r.name, className: keep.has(r.name) ? "win" : "" },
            h("td", { className: "name" }, r.name),
            h("td", { className: "faint" }, r.position || "—"),
            h("td", null, r.board_rank ?? "—"),
            h("td", null, r.implied_round ?? "—"),
            h("td", null, r.on_board ? h("b", null, fmt(r.total_value, 2)) : "—"),
            h("td", { className: "faint" },
              r.on_board ? fmt(r.per_game_value, 2) : "—"),
            h("td", { className: "name" },
              keep.has(r.name)
                ? h("span", { className: "tag ok" }, "keep")
                : !r.on_board
                  ? h("span", { className: "tag warn" }, "no value")
                  : "")))))));
}

function MockDraft() {
  const [picks, setPicks] = useState([]);
  const [seed, setSeed] = useState(0);
  const [d, setD] = useState(null);
  const [busy, setBusy] = useState(false);
  const load = (p, s) => {
    setBusy(true);
    post("/api/fantasy/mock-draft", { picks: p, seed: s })
      .then((r) => { setD(r); setBusy(false); })
      .catch((e) => { setD({ error: String(e) }); setBusy(false); });
  };
  useEffect(() => { load(picks, seed); }, [picks, seed]);
  if (!d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);
  return h("section", { className: "block" },
    h("h3", null, d.complete
      ? `Mock draft complete — ${d.my_roster.length} players`
      : `Mock draft — pick ${d.pick_number}, round ${d.round}`),
    h("div", { className: "panel accent", style: { marginBottom: 10 } },
      h("p", { style: { margin: 0, fontSize: 13 } }, d.note)),
    h("div", { className: "controls no-print" },
      h("label", { className: "field" }, "Seed",
        h("input", { type: "number", value: seed, style: { width: 80 },
          onChange: (e) => { setPicks([]); setSeed(Number(e.target.value) || 0); } })),
      h("button", { className: "btn-ghost", onClick: () => setPicks([]) },
        "Restart"),
      picks.length > 0 && h("button", { className: "btn-ghost",
        onClick: () => setPicks((p) => p.slice(0, -1)) }, "Undo last pick")),
    h("div", { className: "grid two" },
      h("div", null,
        h("div", { className: "div-head" },
          d.complete ? "Your roster" : "On the clock — take one"),
        h("div", { className: "panel scroll-y", style: { padding: 0 } },
          h("table", { className: "data" },
            h("tbody", null, (d.complete ? [] : d.available).map((a) =>
              h("tr", { key: a.player_id, style: { cursor: "pointer" },
                  onClick: () => !busy && setPicks((p) => [...p, a.player_id]) },
                h("td", { className: "name" }, a.name),
                h("td", { className: "faint" }, a.pos || "—"),
                h("td", { className: "faint" }, a.pro_team || "—"),
                h("td", null, fmt(a.total_value, 2)))))))),
      h("div", null,
        h("div", { className: "div-head" },
          `Your team (${d.my_roster.length}) — value ${fmt(d.my_value, 2)}`),
        h("div", { className: "panel" },
          d.my_roster.length === 0
            ? h("p", { className: "faint", style: { margin: 0 } }, "nobody yet")
            : h("ol", { style: { margin: 0, paddingLeft: 20 } },
                d.my_roster.map((n, i) => h("li", { key: i }, n)))),
        !d.complete && d.picks_until_my_next != null && h("p",
          { className: "faint", style: { fontSize: 12 } },
          `After this pick, ${d.picks_until_my_next} others go before you `
          + "are up again."),
        h("div", { className: "div-head", style: { marginTop: 14 } }, "Recent picks"),
        h("div", { className: "panel scroll-y", style: { padding: 0, maxHeight: 220 } },
          h("table", { className: "data" },
            h("tbody", null, [...d.log].reverse().map((l) =>
              h("tr", { key: l.pick, className: l.is_me ? "win" : "" },
                h("td", { className: "faint" }, l.pick),
                h("td", { className: "name" }, l.name),
                h("td", { className: "faint" }, l.is_me ? "you" : `t${l.team_id}`)))))))));
}

function Recap() {
  const [d, setD] = useState(null);
  useEffect(() => {
    api("/api/fantasy/recap").then(setD).catch((e) => setD({ error: String(e) }));
  }, []);
  if (!d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);
  const w = d.waivers || {};
  const r = d.retrospective || {};
  return h("section", { className: "block" },
    h("h3", null, `Season recap — ${d.season_label}`),
    r.available && h("p", null,
      h("b", null, `${r.optimiser_won} of ${r.n_weeks} weeks`),
      " the optimiser's lineup would have taken more categories "
      + `(${fmt(r.mean_cats_won, 2)} of 9 on average).`),
    w.available
      ? h("div", null,
          h("div", { className: "div-head" },
            `Unrostered players who beat your weakest held (${w.weakest_held}, `
            + `${fmt(w.bar, 2)})`),
          h("div", { className: "panel scroll-x" },
            h("table", { className: "data" },
              h("thead", null, h("tr", null,
                h("th", { className: "name" }, "Player"), h("th", null, "Games"),
                h("th", null, "Season value"), h("th", null, "Better by"))),
              h("tbody", null, (w.candidates || []).map((c) =>
                h("tr", { key: c.player_id },
                  h("td", { className: "name" }, c.name),
                  h("td", { className: "faint" }, c.games),
                  h("td", null, h("b", null, fmt(c.value, 2))),
                  h("td", { className: "margin pos" }, "+" + fmt(c.beats_by, 2))))))),
          h("p", { className: "faint", style: { fontSize: 11 } }, w.caveat))
      : h("p", { className: "muted" }, w.reason),
    h("div", { className: "panel", style: { marginTop: 12 } },
      h(NarrateButton, { kind: "recap", label: "Write the season recap",
        hint: "The numbers above are computed locally. This asks the model to "
            + "read them as a post-mortem — the only action here that costs "
            + "anything." })));
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
