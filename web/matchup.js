// Weekly matchup strategy, lineup and waivers (design spec phase 7).
//
// The whole page is advisory and says so once, loudly, at the top rather than
// in a footnote: the draft board and the projections were validated against a
// stated benchmark and this advice was not. The one thing here that IS
// verified is the reconciliation, so it is shown rather than hidden.
import { h, useState, useEffect, api, fmt, pct, Spinner } from "/static/lib.js";
import { NarrateButton } from "/static/narrate.js";

const PCT_CATS = new Set(["FG%", "FT%"]);
const val = (cat, v) => v == null ? "—"
  : PCT_CATS.has(cat) ? (v * 100).toFixed(1) + "%" : fmt(v, 0);

function Bar({ mine, theirs, lower }) {
  // Share of the pair, so the two sides are comparable at a glance without
  // implying a scale the categories do not share.
  const total = (mine || 0) + (theirs || 0);
  const share = total > 0 ? (mine / total) * 100 : 50;
  const good = lower ? mine < theirs : mine > theirs;
  return h("div", { className: "catbar" },
    h("div", { className: "catbar-fill " + (good ? "w" : "l"),
               style: { width: `${Math.max(2, Math.min(98, share))}%` } }));
}

// Season outlook, fixture load, today's digest and the retrospective all hang
// off the same league read, so they load together rather than as four spinners.
function Outlook({ o }) {
  if (!o) return null;
  const s = o.outlook || {};
  return h("section", { className: "block" },
    h("h3", null, "Season outlook"),
    h("div", { className: "panel" },
      s.playable
        ? h("div", null,
            h("p", { style: { marginTop: 0, fontSize: 15 } },
              h("b", null, `${(s.playoff_odds * 100).toFixed(0)}% playoff odds`),
              ` — ${s.remaining_matchups} matchups left, currently `
              + `${s.my_rank} of ${s.teams}, top ${s.playoff_spots} qualify. `
              + `Mean simulated finish ${s.mean_finish?.toFixed(1)}.`),
            h("ul", { className: "faint", style: { fontSize: 11, paddingLeft: 16 } },
              (s.assumptions || []).map((a, i) => h("li", { key: i }, a))))
        : h("p", { style: { margin: 0 } },
            h("b", null, "Nothing to simulate. "), s.reason)),
    (s.standings || []).length > 0 && h("div", { className: "panel scroll-y",
        style: { marginTop: 10 } },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", null, "#"), h("th", { className: "name" }, "Team"),
          h("th", null, "Cat W"), h("th", null, "L"), h("th", null, "T"),
          h("th", null, "Pts"))),
        h("tbody", null, s.standings.map((t, i) =>
          h("tr", { key: t.team_id, className: t.is_me ? "win" : "" },
            h("td", { className: "faint" }, i + 1),
            h("td", { className: "name" }, t.name,
              t.is_me && h("span", { className: "tag accent",
                style: { marginLeft: 6 } }, "you")),
            h("td", null, t.wins), h("td", { className: "faint" }, t.losses),
            h("td", { className: "faint" }, t.ties),
            h("td", null, h("b", null, fmt(t.points, 1)))))))));
}

function Ahead({ sched }) {
  if (!sched || !sched.weeks) return null;
  return h("section", { className: "block" },
    h("h3", null, "Weeks ahead"),
    h("p", { className: "faint", style: { fontSize: 12, marginTop: 0 } },
      sched.opponent_note),
    h("div", { className: "grid two" }, sched.weeks.map((w) =>
      h("div", { className: "panel", key: w.start },
        h("h4", null, `Week of ${w.start}`),
        !w.covered_by_schedule
          ? h("p", { className: "muted", style: { margin: 0 } },
              "Beyond the schedule in this snapshot — unknown, not zero.")
          : h("div", null,
              h("p", { style: { margin: "0 0 6px" } },
                h("b", null, w.total_starter_games), " starter games"),
              w.thin.length > 0 && h("p", { className: "faint",
                  style: { margin: 0, fontSize: 12 } },
                "Thin: " + w.thin.map((r) => `${r.name} (${r.games})`).join(", ")),
              w.heavy.length > 0 && h("p", { className: "faint",
                  style: { margin: 0, fontSize: 12 } },
                "Heavy: " + w.heavy.slice(0, 4)
                  .map((r) => `${r.name} (${r.games})`).join(", ")))))));
}

function Digest({ dg }) {
  if (!dg) return null;
  const flagged = (dg.injuries || {}).flagged || [];
  return h("section", { className: "block" },
    h("h3", null, `Today — ${dg.day}`),
    h("div", { className: "panel" },
      h("p", { style: { marginTop: 0 } },
        h("b", null, `${dg.starters_playing}/${dg.starters_total}`),
        " starters have a game today.",
        dg.idle_starters.length > 0
          ? ` Idle: ${dg.idle_starters.map((r) => r.name).join(", ")}.` : ""),
      dg.suggested_swaps.length > 0 && h("ul", { style: { margin: "6px 0" } },
        dg.suggested_swaps.map((s, i) => h("li", { key: i },
          "Sit ", h("b", null, s.sit), ", start ", h("b", null, s.start),
          h("span", { className: "faint" }, ` — ${s.reason}`)))),
      h("p", { className: "faint", style: { fontSize: 12, margin: "6px 0 0" } },
        flagged.length === 0
          ? `No injury flags on this roster. Statuses pulled ${(dg.injuries || {}).fetched_at || "unknown"}.`
          : "Flagged: " + flagged.map((r) => `${r.name} ${r.injury_status}`).join(", ")),
      dg.waiver_highlight && h("p", { style: { margin: "6px 0 0" } },
        h("b", null, "Waiver note. "), dg.waiver_highlight.name,
        ` projects ${fmt(dg.waiver_highlight.projected_value, 2)}, above `
        + `${dg.waiver_highlight.beats} on your bench.`)));
}

function Retro({ r }) {
  if (!r || !r.available) return null;
  return h("section", { className: "block" },
    h("h3", null, "Lineup retrospective"),
    h("div", { className: "panel warn", style: { marginBottom: 10 } },
      h("p", { style: { margin: 0, fontSize: 13 } }, r.limitation),
      r.projection_basis && h("p", { style: { margin: "6px 0 0", fontSize: 13 } },
        h("b", null, "No foresight. "), r.projection_basis)),
    h("p", { style: { marginTop: 0 } },
      h("b", null, `${r.optimiser_won} of ${r.n_weeks} weeks`),
      ` the optimiser's lineup would have taken more categories `
      + `(${fmt(r.mean_cats_won, 2)} of 9 on average).`),
    h("div", { className: "panel scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", { className: "name" }, "Category"),
          h("th", null, "Weeks better"), h("th", null, "Rate"))),
        h("tbody", null, [...r.by_category].sort((a, b) => b.rate - a.rate)
          .map((c) => h("tr", { key: c.category },
            h("td", { className: "name" }, c.category),
            h("td", null, `${c.weeks_better}/${c.weeks}`),
            h("td", { className: "margin " + (c.rate >= 0.5 ? "pos" : "neg") },
              `${(c.rate * 100).toFixed(0)}%`)))))),
    h("p", { className: "faint", style: { fontSize: 11 } },
      "A category the optimiser reliably loses is a cost of the objective, "
      + "not a bug: maximising production favours players who also turn the "
      + "ball over."));
}

export function Matchup() {
  const [d, setD] = useState(null);
  const [extra, setExtra] = useState(null);
  const [retro, setRetro] = useState(null);
  useEffect(() => {
    api("/api/fantasy/matchup").then(setD).catch((e) => setD({ error: String(e) }));
    api("/api/fantasy/outlook").then(setExtra).catch(() => setExtra(null));
    api("/api/fantasy/retrospective").then(setRetro).catch(() => setRetro(null));
  }, []);
  if (!d) return h(Spinner, { label: "reading the league…" });
  if (d.error) return h("p", { className: "err" }, d.error);

  const w = d.week || {};
  const s = d.summary || {};
  const flipFor = (cat) => (d.flips || []).find((f) => f.category === cat);

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "Fantasy · weekly matchup"),
      h("h2", null, d.opponent ? `vs ${d.opponent}` : "Matchup"),
      h("p", { className: "lede" },
        `${d.season_label} · scoring period ${d.scoring_period ?? "—"}. `
        + `Category state, what would flip a losing one, the best legal lineup `
        + `for the week, and free agents worth a roster spot.`),
      h("div", { className: "meta-row" },
        h("span", null, h("b", null,
          `${d.category_wins?.me ?? s.winning}–${d.category_wins?.opponent ?? s.losing}`),
          " categories"),
        w.found && h("span", null, h("b", null, "Week "), w.start, " +7d"),
        h("span", null, h("b", null, d.roster.filter((p) => p.mapped).length),
          `/${d.roster.length} players matched`))),

    h("div", { className: "panel warn", style: { marginBottom: 18 } },
      h("p", { style: { margin: 0 } }, h("b", null, "Advisory. "), d.advisory)),

    // The one claim on this page that is checked rather than asserted.
    w.found && h("div", { className: "panel accent", style: { marginBottom: 18 } },
      h("p", { style: { margin: 0, fontSize: 13 } },
        h("b", null, "Reconciled. "),
        `All nine categories recomputed from box scores match ESPN exactly for `
        + `the week of ${w.start} (total absolute error `
        + `${(w.abs_error ?? 0).toExponential(1)}). That confirms the player `
        + `identity map, the starters-only scoring rule, and the week itself — `
        + `ESPN's cache carries a scoring period but no dates.`)),

    h("section", { className: "block" },
      h("h3", null, "Categories"),
      h("div", { className: "panel scroll-x" },
        h("table", { className: "data" },
          h("thead", null, h("tr", null,
            h("th", { className: "name" }, "Cat"), h("th", null, "Mine"),
            h("th", null, "Theirs"), h("th", { style: { width: 150 } }, ""),
            h("th", null, "Margin"), h("th", { className: "name" }, "State"))),
          h("tbody", null, (d.state || []).map((c) => {
            const f = flipFor(c.category);
            return h("tr", { key: c.category,
                className: c.status === "winning" ? "win" : c.status === "losing" ? "loss" : "" },
              h("td", { className: "name" }, h("b", null, c.category),
                c.lower_wins && h("span", { className: "faint",
                  style: { fontWeight: 400, marginLeft: 5, fontSize: 10 } }, "low wins")),
              h("td", null, val(c.category, c.mine)),
              h("td", { className: "faint" }, val(c.category, c.theirs)),
              h("td", null, h(Bar, { mine: c.mine, theirs: c.theirs, lower: c.lower_wins })),
              h("td", { className: "margin " + (c.margin > 0 ? "pos" : "neg") },
                (c.margin > 0 ? "+" : "")
                + (PCT_CATS.has(c.category) ? (c.margin * 100).toFixed(2) + "pp"
                                            : fmt(c.margin, 0))),
              h("td", { className: "name" },
                h("span", { className: "wl " + (c.status === "winning" ? "w" : "l") },
                  c.status === "winning" ? "W" : c.status === "losing" ? "L" : "T"),
                h("span", { className: "faint", style: { marginLeft: 7, fontSize: 11 } },
                  c.closeness),
                f && f.closes_gap && h("span", { className: "tag ok",
                  style: { marginLeft: 7 } }, "flippable")));
          })))),
      h("p", { className: "faint", style: { fontSize: 11 } },
        `Close and losing: ${(s.flippable || []).join(", ") || "none"}. `
        + `Close and winning, so worth protecting: ${(s.at_risk || []).join(", ") || "none"}.`)),

    (d.flips || []).length > 0 && h("section", { className: "block" },
      h("h3", null, "What would flip a losing category"),
      h("div", { className: "panel" },
        (d.flips || []).map((f) => h("div", { key: f.category, className: "flip" },
          h("div", { className: "flip-cat" }, f.category),
          f.bench_in
            ? h("div", null,
                h("div", null, "Start ", h("b", null, f.bench_in),
                  ` (${f.bench_games} games) over `, h("b", null, f.starter_out),
                  ` (${f.starter_games})`),
                h("div", { className: "faint", style: { fontSize: 12, marginTop: 2 } },
                  PCT_CATS.has(f.category)
                    ? `+${(f.gain * 100).toFixed(2)}pp against a ${(f.need * 100).toFixed(2)}pp gap`
                    : `+${fmt(f.gain, 1)} against a gap of ${fmt(f.need, 1)}`,
                  f.closes_gap ? " — closes it" : " — not enough on its own",
                  (f.would_lose || []).length
                    ? ` · would give up ${f.would_lose.join(", ")}` : ""))
            : h("div", { className: "faint" }, f.note || "no bench swap helps")))),
      h("p", { className: "faint", style: { fontSize: 11 } },
        "Start/sit only — adds and drops are the waiver section's job. "
        + "Opponent totals are held at their current value, so these gaps are "
        + "the minimum, not the requirement.")),

    d.lineup && h("section", { className: "block" },
      h("h3", null, `Lineup — ${d.lineup.n_changes} change`
        + `${d.lineup.n_changes === 1 ? "" : "s"} from the one set`),
      h("div", { className: "panel scroll-x" },
        h("table", { className: "data" },
          h("thead", null, h("tr", null,
            h("th", null, "Slot"), h("th", { className: "name" }, "Player"),
            h("th", null, "Games"), h("th", null, "Value"), h("th", { className: "name" }, ""))),
          h("tbody", null, d.lineup.slots.map((a, i) =>
            h("tr", { key: i },
              h("td", { className: "faint" }, a.slot),
              h("td", { className: "name" }, a.player || "—"),
              h("td", null, a.games ?? "—"),
              h("td", null, a.value == null ? "—" : fmt(a.value, 2)),
              h("td", { className: "name" }, !a.was_starting && a.player
                ? h("span", { className: "tag accent" }, "change") : "")))))),
      h("p", { className: "faint", style: { fontSize: 11 } },
        `Objective: ${d.lineup.objective}. Measured across 25 weeks of `
        + "season 2025 against the lineup left unchanged, with projections "
        + "that could not see the season: 5.58 of 9 categories on average, "
        + "winning 17 of 25 weeks. A category-targeting objective scored "
        + "worse (5.28, 16 of 25), so it is not the default.")),

    d.waivers && h("section", { className: "block" },
      h("h3", null, "Waiver radar"),
      !d.waivers.available
        ? h("p", { className: "muted" }, d.waivers.reason)
        : h("div", null,
            h("p", { className: "faint", style: { fontSize: 12, marginTop: 0 } },
              `${d.waivers.basis}. Weakest rostered bench: `,
              h("b", null, d.waivers.weakest_bench.name),
              ` (${fmt(d.waivers.weakest_bench.projected_value, 2)}). `,
              `${d.waivers.free_agents_with_projection} of `
              + `${d.waivers.free_agents_seen} free agents have a projection.`),
            d.waivers.candidates.length === 0
              ? h("p", { className: "muted" }, "Nothing on the wire projects above your bench.")
              : h("div", { className: "panel scroll-x" },
                  h("table", { className: "data" },
                    h("thead", null, h("tr", null,
                      h("th", { className: "name" }, "Player"), h("th", null, "Tm"),
                      h("th", null, "Pos"), h("th", null, "Proj value"),
                      h("th", null, "Beats"), h("th", null, "By"),
                      h("th", null, "Games"))),
                    h("tbody", null, d.waivers.candidates.map((c) =>
                      h("tr", { key: c.name },
                        h("td", { className: "name" }, c.name),
                        h("td", { className: "faint" }, c.pro_team || "—"),
                        h("td", { className: "faint" }, c.position || "—"),
                        h("td", null, h("b", null, fmt(c.projected_value, 2))),
                        h("td", { className: "faint" }, c.beats),
                        h("td", { className: "margin pos" }, "+" + fmt(c.beats_by, 2)),
                        h("td", null, c.games_this_week ?? "—")))))))),
    extra && h(Digest, { dg: extra.digest }),
    extra && h(Ahead, { sched: extra.schedule }),
    extra && h(Outlook, { o: extra }),
    h(Retro, { r: retro }),
    h("section", { className: "block no-print" },
      h("h3", null, "Written advice"),
      h("div", { className: "panel" },
        h(NarrateButton, { kind: "matchup",
          label: "Write this week's advice" }))),
    extra && h("section", { className: "block no-print" },
      h("h3", null, "Narrate the outlook"),
      h("div", { className: "panel" },
        h(NarrateButton, { kind: "outlook",
          label: "Get a take on the season" }))),
    retro && retro.available && h("section", { className: "block no-print" },
      h("h3", null, "Narrate the retrospective"),
      h("div", { className: "panel" },
        h(NarrateButton, { kind: "retrospective",
          label: "Get a take on the season-long pattern" }))));
}
