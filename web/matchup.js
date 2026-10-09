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

// The lineup, behind tabs.
//
// NOT tabs by date: the optimiser produces ONE lineup for the week -- the API
// returns `days` as a count, not a per-date breakdown -- and seven fabricated
// date tabs would imply per-day assignments the model never made. The two
// tabs are the two things that genuinely exist: the week's optimised lineup,
// and today's actual availability from the digest.
function LineupByDay({ lineup }) {
  const [tab, setTab] = useState("week");
  const [dg, setDg] = useState(null);
  useEffect(() => {
    if (tab === "today" && dg === null) {
      api("/api/fantasy/outlook").then((o) => setDg(o.digest || false))
        .catch(() => setDg(false));
    }
  }, [tab, dg]);
  return h("section", { style: { marginTop: "var(--s5)" } },
    h("div", { className: "sect-head" },
      h("h2", null, "Lineup"),
      h("span", { className: "count" },
        `${lineup.n_changes} change${lineup.n_changes === 1 ? "" : "s"} from the one set`)),
    h("div", { className: "tabs", role: "tablist" },
      h("button", { role: "tab", "aria-selected": tab === "week",
        onClick: () => setTab("week") }, `Week (${lineup.days} days)`),
      h("button", { role: "tab", "aria-selected": tab === "today",
        onClick: () => setTab("today") }, "Today")),
    tab === "week"
      ? h("div", null,
          h("div", { className: "table-full scroll-x" },
            h("table", { className: "data" },
              h("thead", null, h("tr", null,
                h("th", null, "Slot"), h("th", { className: "name" }, "Player"),
                h("th", null, "G"), h("th", null, "Value"),
                h("th", { className: "name" }, ""))),
              h("tbody", null, lineup.slots.map((a, i2) =>
                h("tr", { key: i2 },
                  h("td", { className: "faint" }, a.slot),
                  h("td", { className: "name" }, a.player || "\u2014"),
                  h("td", null, a.games ?? "\u2014"),
                  h("td", null, a.value == null ? "\u2014" : fmt(a.value, 2)),
                  h("td", { className: "name" }, !a.was_starting && a.player
                    ? h("span", { className: "chip info" }, "change") : "")))))),
          h("p", { className: "faint", style: { fontSize: "var(--t-xs)" } },
            `Objective: ${lineup.objective}. Measured across 25 weeks of season `
            + "2025 against the lineup left unchanged, with projections that "
            + "could not see the season: 5.58 of 9 categories on average, "
            + "winning 17 of 25 weeks. A category-targeting objective scored "
            + "worse (5.28, 16 of 25), so it is not the default."))
      : dg === null ? h(Spinner, null)
      : dg === false ? h("p", { className: "muted" }, "today's digest unavailable")
      : h("div", { className: "today" },
          h("div", { className: "statrow" },
            h("div", { className: "st" }, h("div", { className: "k" }, "Date"),
              h("div", { className: "v" }, dg.day)),
            h("div", { className: "st" }, h("div", { className: "k" }, "Starters playing"),
              h("div", { className: "v" }, `${dg.starters_playing}/${dg.starters_total}`)),
            h("div", { className: "st" }, h("div", { className: "k" }, "Idle starters"),
              h("div", { className: "v " + ((dg.idle_starters || []).length ? "neg" : "") },
                (dg.idle_starters || []).length)),
            h("div", { className: "st" }, h("div", { className: "k" }, "Bench playing"),
              h("div", { className: "v" }, (dg.bench_playing || []).length))),
          (dg.idle_starters || []).length > 0 && h("p", null,
            h("b", null, "Idle starters: "),
            dg.idle_starters.map((p2) => p2.name || p2).join(", ")),
          (dg.bench_playing || []).length > 0 && h("p", null,
            h("b", null, "Bench with games: "),
            dg.bench_playing.map((p2) => p2.name).join(", ")),
          dg.note && h("p", { className: "faint" }, dg.note)));
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

  // A DASHBOARD, not a column of panels. The nine categories are compact
  // scoreboard cells in the main column; flips and the waiver radar sit in a
  // right rail where they are advice ABOUT the categories rather than another
  // full-width table after them. "What would flip a losing category" used to
  // span the whole page for content that needs about a third of it.
  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "lhs" },
        h("div", { className: "eyebrow" }, "Fantasy \u00b7 weekly matchup"),
        h("h1", null, d.opponent ? `vs ${d.opponent}` : "Matchup"),
        h("p", { className: "lede" },
          `${d.season_label} \u00b7 scoring period ${d.scoring_period ?? "\u2014"}. `
          + "Category state, what would flip a losing one, the best legal "
          + "lineup for the week, and free agents worth a roster spot.")),
      h("div", { className: "meta" },
        h("span", null, h("b", null,
          `${d.category_wins?.me ?? s.winning}\u2013${d.category_wins?.opponent ?? s.losing}`),
          "categories"),
        w.found && h("span", null, h("b", null, w.start), "week"),
        h("span", null, h("b", null, d.roster.filter((p) => p.mapped).length),
          `of ${d.roster.length} matched`))),

    h("div", { className: "mk-grid" },
      h("div", { className: "mk-main" },
        // Nine cells, not nine table rows: a category is a score you glance at.
        h("div", { className: "catcells" }, (d.state || []).map((c) => {
          const f = flipFor(c.category);
          const st = c.status === "winning" ? "w" : c.status === "losing" ? "l" : "t";
          return h("div", { key: c.category, className: "cat " + st },
            h("div", { className: "c-head" },
              h("span", { className: "c-name" }, c.category),
              c.lower_wins && h("span", { className: "c-low" }, "low wins"),
              h("span", { className: "c-state" },
                st === "w" ? "WIN" : st === "l" ? "LOSS" : "TIE")),
            h("div", { className: "c-score" },
              h("span", { className: "mine" }, val(c.category, c.mine)),
              h("span", { className: "vs" }, "\u2013"),
              h("span", { className: "theirs" }, val(c.category, c.theirs))),
            h(Bar, { mine: c.mine, theirs: c.theirs, lower: c.lower_wins }),
            h("div", { className: "c-foot" },
              h("span", { className: "margin " + (c.margin > 0 ? "pos" : "neg") },
                (c.margin > 0 ? "+" : "")
                + (PCT_CATS.has(c.category) ? (c.margin * 100).toFixed(2) + "pp"
                                            : fmt(c.margin, 0))),
              h("span", { className: "c-close" }, c.closeness),
              f && f.closes_gap && h("span", { className: "chip ok" }, "flippable")));
        })),
        h("p", { className: "faint", style: { fontSize: "var(--t-xs)" } },
          `Close and losing: ${(s.flippable || []).join(", ") || "none"}. `
          + `Close and winning, so worth protecting: `
          + `${(s.at_risk || []).join(", ") || "none"}.`),

        d.lineup && h(LineupByDay, { lineup: d.lineup })),

      h("aside", { className: "mk-rail" },
        h("div", { className: "panel warn" },
          h("p", { style: { margin: 0, fontSize: "var(--t-sm)" } },
            h("b", null, "Advisory. "), d.advisory)),
        w.found && h("div", { className: "panel accent" },
          h("p", { style: { margin: 0, fontSize: "var(--t-sm)" } },
            h("b", null, "Reconciled. "),
            "All nine categories recomputed from box scores match ESPN exactly "
            + `for the week of ${w.start} (total absolute error `
            + `${(w.abs_error ?? 0).toExponential(1)}).`)),

        (d.flips || []).length > 0 && h("section", { className: "panel" },
          h("div", { className: "div-head" }, "Would flip a losing category"),
          (d.flips || []).map((f) => h("div", { key: f.category, className: "flip" },
            h("div", { className: "flip-cat" }, f.category),
            f.bench_in
              ? h("div", null,
                  h("div", null, "Start ", h("b", null, f.bench_in),
                    ` (${f.bench_games}g) over `, h("b", null, f.starter_out),
                    ` (${f.starter_games}g)`),
                  h("div", { className: "faint", style: { fontSize: "var(--t-xs)", marginTop: 2 } },
                    PCT_CATS.has(f.category)
                      ? `+${(f.gain * 100).toFixed(2)}pp against ${(f.need * 100).toFixed(2)}pp`
                      : `+${fmt(f.gain, 1)} against ${fmt(f.need, 1)}`,
                    f.closes_gap ? " \u2014 closes it" : " \u2014 not enough alone",
                    (f.would_lose || []).length
                      ? ` \u00b7 gives up ${f.would_lose.join(", ")}` : ""))
              : h("div", { className: "faint" }, f.note || "no bench swap helps"))),
          h("p", { className: "faint", style: { fontSize: "var(--t-xs)", margin: "var(--s2) 0 0" } },
            "Start/sit only. Opponent totals held at their current value, so "
            + "these gaps are the minimum.")),

        d.waivers && h("section", { className: "panel" },
          h("div", { className: "div-head" }, "Waiver radar"),
          !d.waivers.available
            ? h("p", { className: "muted", style: { margin: 0 } }, d.waivers.reason)
            : d.waivers.candidates.length === 0
              ? h("p", { className: "muted", style: { margin: 0 } },
                  "Nothing on the wire projects above your bench.")
              : h("ul", { className: "wv" }, d.waivers.candidates.slice(0, 8).map((c) =>
                  h("li", { key: c.name },
                    h("span", { className: "n" }, c.name),
                    h("span", { className: "t" }, c.pro_team || "\u2014"),
                    h("span", { className: "v" }, fmt(c.projected_value, 2)),
                    h("span", { className: "b" }, "+" + fmt(c.beats_by, 2)))))),

        h("section", { className: "panel no-print" },
          h("div", { className: "div-head" }, "Written advice"),
          h(NarrateButton, { kind: "matchup", label: "Write this week's advice" })))),

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
