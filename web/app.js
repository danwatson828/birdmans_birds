// Shell: a rail of sections, a sticky topbar carrying context, and
// URL-addressable routes.
//
// IA, 8e. The old rail was thirteen items in four groups, one of which was
// "Players & teams" -- a single landing that made you choose between two
// different jobs before it would show you anything. The groups now name what
// you are doing rather than which module the code lives in:
//
//   LEAGUE     Overview · Teams -> Team -> Game · Players -> Player ·
//              Leaderboards · League evolution
//   ANALYSIS   Ask · Model builder · Knowledge base (incl. validated
//              findings as a tab) · Dataset catalog
//   FANTASY    This week · Draft board · Keepers & draft · Compare & trade
//
// Validated findings stopped being a top-level destination because it is a
// FILTER over saved analyses, not a separate store -- /api/findings returns
// the investigate runs that declared a held-out era and committed to a
// verdict. It is a tab on the knowledge base, which is where someone looking
// for a past answer actually goes.
//
// Every old URL still resolves: see LEGACY below.
import { h, useState, useEffect, useCallback, useRef, api, fmt, Spinner, useBrands, installPrintHandlers, pct, token, brandOf, teamStyle }
  from "/static/lib.js";
import { Report, AskBar } from "/static/ask.js";
import { Explorer } from "/static/explore.js";
import { KnowledgeBase } from "/static/kb.js";
import { Matchup } from "/static/matchup.js";
import { CompareView } from "/static/compare.js";
import { Evolution } from "/static/evolution.js";
import { FantasyLab } from "/static/lab.js";
import { ModelBuilder } from "/static/model.js";
import { Fantasy } from "/static/fantasy.js";
import { iconSvg } from "/static/icons.js";

export const ROUTES = [
  { id: "home",      label: "Overview",        icon: "squares-four",       group: "League" },
  { id: "teams",     label: "Teams",           icon: "users-three",        group: "League" },
  { id: "players",   label: "Players",         icon: "magnifying-glass",   group: "League" },
  { id: "leaders",   label: "Leaderboards",    icon: "ranking",            group: "League" },
  { id: "evolution", label: "League evolution", icon: "chart-line-up",     group: "League" },
  { id: "ask",       label: "Ask",             icon: "chat-teardrop-text", group: "Analysis" },
  { id: "model",     label: "Model builder",   icon: "function",           group: "Analysis" },
  { id: "knowledge", label: "Knowledge base",  icon: "books",              group: "Analysis" },
  { id: "catalog",   label: "Dataset catalog", icon: "table",              group: "Analysis" },
  { id: "matchup",   label: "This week",       icon: "swap",               group: "Fantasy" },
  { id: "fantasy",   label: "Draft board",     icon: "clipboard-text",     group: "Fantasy" },
  { id: "lab",       label: "Keepers & draft", icon: "strategy",           group: "Fantasy" },
  { id: "compare",   label: "Trade analyzer",  icon: "arrows-left-right",  group: "Fantasy" },
];
const ROUTE_IDS = new Set(ROUTES.map((r) => r.id));

// Old links must not die. `explore` was one route holding four different page
// types, so where it lands depends on which one the URL asked for.
const LEGACY = {
  explore: (u) => ({ player: "players", search: "players", leaderboard: "leaders",
                     catalog: "catalog" }[u.get("view")] || "teams"),
  findings: () => "knowledge",
};

function readUrl() {
  const u = new URLSearchParams(location.search);
  let r = u.get("v");
  let tab = u.get("tab");
  if (r && LEGACY[r]) {
    if (r === "findings") tab = tab || "findings";
    r = LEGACY[r](u);
  }
  const id = Number(u.get("analysis"));
  return { route: ROUTE_IDS.has(r) ? r : "home",
           analysis: Number.isFinite(id) && id > 0 ? id : null,
           tab: tab || null };
}

function Nav({ route, go }) {
  let lastGroup;
  return h("nav", { className: "nav", "aria-label": "Sections" }, ROUTES.map((r) => {
    const head = r.group && r.group !== lastGroup
      ? h("div", { className: "nav-group", key: "g" + r.group }, r.group) : null;
    lastGroup = r.group;
    return [head, h("a", { key: r.id, href: `?v=${r.id}`,
        "aria-current": route === r.id ? "page" : undefined,
        onClick: (e) => { e.preventDefault(); go(r.id); } },
      h("span", { className: "glyph", "aria-hidden": "true",
          dangerouslySetInnerHTML: { __html: iconSvg(r.icon, 15) } }), r.label)];
  }).concat([
    // The live draft room is its own page, not a route in ROUTES: it also has
    // to run as a standalone file (build_draft_page.py), so it lives outside
    // the shell and this is a plain link to it. Fantasy is the last group, so
    // it lands there.
    h("a", { key: "draft-room", href: "/draft" },
      h("span", { className: "glyph", "aria-hidden": "true",
          dangerouslySetInnerHTML: { __html: iconSvg("ranking", 15) } }), "Live draft room"),
  ]));
}

// The topbar says where you are and carries the one control that belongs to
// every page. Both used to be repeated inside each page's own header.
function TopBar({ route, crumbs, go, onSearch, searchValue }) {
  const here = ROUTES.find((r) => r.id === route);
  return h("div", { className: "topbar no-print" },
    h("div", { className: "crumb" },
      h("a", { href: "?v=home", onClick: (e) => { e.preventDefault(); go("home"); } },
        here && here.group ? here.group : "League"),
      h("span", { className: "sep" }, "/"),
      (crumbs || []).length === 0
        ? h("span", { className: "here" }, here ? here.label : "Overview")
        : [h("a", { key: "r", href: `?v=${route}`,
              onClick: (e) => { e.preventDefault(); go(route); } },
            here ? here.label : ""),
           ...crumbs.flatMap((c, i) => [
             h("span", { className: "sep", key: "s" + i }, "/"),
             h("span", { className: "here", key: "c" + i }, c)])]),
    h("div", { className: "spacer" }),
    h("div", { className: "find" },
      h("input", { type: "search", value: searchValue,
        placeholder: "Search players or teams…",
        "aria-label": "Search players or teams",
        onChange: (e) => onSearch(e.target.value) })));
}

const VERDICT = {
  "reproducible": ["ok", "reproducible"],
  "has unstable steps": ["bad", "unstable"],
  "unknown (predates the check)": ["warn", "unverified"],
};

// A saved analysis is identified by its question and what it is tagged with,
// not by its row id. The id is still shown, small, because reruns and PDFs are
// addressed by it.
function SavedList({ items, selected, onSelect, filter, setFilter, tagFilter, setTagFilter }) {
  const allTags = [...new Set(items.flatMap((a) => a.tags || []))].sort();
  const shown = items.filter((a) => {
    const t = filter.trim().toLowerCase();
    if (t && !(a.question.toLowerCase().includes(t)
               || (a.tags || []).some((x) => x.toLowerCase().includes(t)))) return false;
    if (tagFilter && !(a.tags || []).includes(tagFilter)) return false;
    return true;
  });
  return h("div", { style: { display: "flex", flexDirection: "column", minHeight: 0 } },
    h("div", { style: { padding: "10px 12px", borderBottom: "1px solid var(--rule)" } },
      h("input", { type: "search", value: filter, placeholder: "Filter saved analyses…",
        style: { width: "100%", minWidth: 0 },
        onChange: (e) => setFilter(e.target.value) }),
      allTags.length > 0 && h("select", { value: tagFilter, style: { width: "100%", marginTop: 6 },
          onChange: (e) => setTagFilter(e.target.value) },
        h("option", { value: "" }, `all tags (${allTags.length})`),
        allTags.map((t) => h("option", { key: t, value: t }, t)))),
    h("div", { style: { overflowY: "auto", flex: 1 } },
      shown.length === 0
        ? h("p", { className: "muted", style: { padding: "14px 16px", fontSize: 12 } },
            "Nothing matches.")
        : shown.map((a) => {
            const [kind, label] = VERDICT[a.reproducibility] || ["", "?"];
            return h("button", { key: a.id, className: "item",
                "aria-current": String(a.id === selected), onClick: () => onSelect(a.id) },
              h("span", { className: "q" }, a.question),
              h("span", { className: "meta" },
                h("span", { className: "tag " + kind }, label),
                (a.tags || []).slice(0, 2).map((t) =>
                  h("span", { className: "tag", key: t }, t.split(":").pop())),
                h("span", { className: "faint" }, `#${a.id}`)));
          })));
}

// A sparkline drawn from real series data. Inline SVG rather than a chart
// library: these are 120x28 and a Vega embed per tile would be absurd.
function Spark({ rows, k, color, label }) {
  if (!rows || rows.length < 2) return null;
  const vals = rows.map((r) => r[k]).filter((v) => v != null);
  if (vals.length < 2) return null;
  const lo = Math.min(...vals), hi = Math.max(...vals), W = 120, H = 28;
  const d = vals.map((v, i) =>
    (i ? "L" : "M") + (i / (vals.length - 1) * W).toFixed(1) + " "
    + (H - (hi === lo ? 0.5 : (v - lo) / (hi - lo)) * (H - 4) - 2).toFixed(1)).join(" ");
  return h("svg", { viewBox: `0 0 ${W} ${H}`, width: W, height: H,
      className: "spark", role: "img", "aria-label": label },
    h("path", { d, fill: "none", stroke: color, strokeWidth: 1.75,
                strokeLinejoin: "round", strokeLinecap: "round" }));
}

function Home({ go, items, health }) {
  const brands = useBrands();
  const [d, setD] = useState(null);
  const [wk, setWk] = useState(null);
  useEffect(() => {
    api("/api/home?games=8").then(setD).catch(() => setD(false));
    // The fantasy week is the only genuinely "live" thing on this page.
    api("/api/fantasy/matchup").then(setWk).catch(() => setWk(false));
  }, []);
  const crests = brands && brands.teams
    ? Object.values(brands.teams).filter((b) => b.logo) : [];
  const bof = (id) => brandOf(brands, id);
  const tiles = [
    ["teams", "Teams", "mosaic"],
    ["players", "Players", null],
    ["leaders", "Leaderboards", null],
    ["evolution", "League evolution", "spark"],
  ];

  return h("div", { className: "home" },
    h("header", { className: "home-band" },
      h("div", { className: "hb-text" },
        h("div", { className: "eyebrow" }, "NBA Analytics"),
        h("h1", null, "Eighty seasons, every box score"),
        h("form", { className: "hb-search", role: "search",
            onSubmit: (e) => { e.preventDefault(); go("players"); } },
          h("span", { className: "g", "aria-hidden": "true",
            dangerouslySetInnerHTML: { __html: iconSvg("magnifying-glass", 17) } }),
          h("input", { type: "search", "aria-label": "Search players or teams",
            placeholder: "Search a player or a team\u2026",
            onFocus: () => go("players") }))),
      h("div", { className: "hb-stats" },
        [[d ? fmt(d.totals.seasons, 0) : "80", "Seasons"],
         [d ? fmt(d.totals.games, 0) : "\u2014", "Games"],
         ["30", "Franchises"],
         [fmt(items.length, 0), "Analyses"]].map(([v, k]) =>
          h("div", { key: k }, h("b", null, v), h("span", null, k))))),

    h("div", { className: "entry" }, tiles.map(([id, title, art]) =>
      h("a", { key: id, href: `?v=${id}`, className: "etile",
          onClick: (e) => { e.preventDefault(); go(id); } },
        h("div", { className: "et-head" },
          h("span", { className: "g", "aria-hidden": "true",
            dangerouslySetInnerHTML: {
              __html: iconSvg(ROUTES.find((r) => r.id === id).icon, 17) } }),
          h("h3", null, title)),
        art === "mosaic" && h("div", { className: "et-mosaic", "aria-hidden": "true" },
          crests.slice(0, 12).map((b) =>
            h("img", { key: b.abbrev, src: b.logo, alt: "" }))),
        art === "spark" && d && h("div", { className: "et-spark" },
          h(Spark, { rows: d.spark, k: "three_point_rate", color: token("--c2"),
                     label: "Three-point rate by season since 1980" }),
          h("span", null, "3PAr ", pct(d.spark[0].three_point_rate, 1), " \u2192 ",
            pct(d.spark[d.spark.length - 1].three_point_rate, 1))),
        !art && h("p", null, id === "players"
          ? "Every player since 1946, with totals, per-game, per-36, advanced and shooting."
          : "Top-ten boards per stat, each stating its qualifier.")))),

    h("div", { className: "home-grid" },
      h("section", { className: "hg-week" },
        h("div", { className: "sect-head" },
          h("h2", null, "This week"),
          h("button", { className: "linkish", onClick: () => go("matchup") },
            "Open matchup")),
        wk === false ? h("p", { className: "muted" }, "league sync unavailable")
        : !wk ? h(Spinner, null)
        : h("div", { className: "wk" },
            h("div", { className: "wk-score" },
              h("span", { className: "n" }, (wk.category_wins || {}).me ?? "\u2014"),
              h("span", { className: "l" }, "you"),
              h("span", { className: "sep" }, "\u2013"),
              h("span", { className: "n" }, (wk.category_wins || {}).opponent ?? "\u2014"),
              h("span", { className: "l" }, wk.opponent || "opponent")),
            h("div", { className: "wk-cats" }, (wk.state || []).map((c) =>
              h("span", { key: c.category,
                  className: "chip " + (c.status === "winning" ? "ok"
                            : c.status === "losing" ? "bad" : "ghost") },
                c.category))),
            (wk.summary || {}).flippable && wk.summary.flippable.length > 0
              && h("p", { className: "wk-note" },
                   h("b", null, "Flippable: "), wk.summary.flippable.join(", ")))),

      h("section", { className: "hg-results" },
        h("div", { className: "sect-head" },
          h("h2", null, "Latest results"),
          h("button", { className: "linkish", onClick: () => go("teams") }, "All teams")),
        !d ? h(Spinner, null) : d === false ? h("p", { className: "err" }, "failed")
        : h("div", null,
            h("ul", { className: "rg" }, d.recent_games.map((g) =>
              h("li", { key: g.game_id },
                h("span", { className: "d" }, g.game_date),
                h("span", { className: "t", style: teamStyle(bof(g.visitor_team_id)) },
                  h("i", null), g.visitor_brand.abbrev || g.visitor_team_id),
                h("span", { className: "s " + (g.home_won ? "lose" : "win") },
                  fmt(g.visitor_team_score, 0)),
                h("span", { className: "at" }, "@"),
                h("span", { className: "t", style: teamStyle(bof(g.home_team_id)) },
                  h("i", null), g.home_brand.abbrev || g.home_team_id),
                h("span", { className: "s " + (g.home_won ? "win" : "lose") },
                  fmt(g.home_team_score, 0))))),
            h("p", { className: "rg-note" }, d.results_are)))),

    items.length > 0 && h("section", { style: { marginTop: "var(--s5)" } },
      h("div", { className: "sect-head" },
        h("h2", null, "Recent analyses"),
        h("button", { className: "linkish", onClick: () => go("knowledge") },
          "Knowledge base")),
      h("div", { className: "cardgrid" }, items.slice(0, 6).map((a) =>
        h("button", { key: a.id, className: "kcard",
            onClick: () => go("ask", a.id) },
          h("span", { className: "q" }, a.question),
          a.takeaway && h("span", { className: "a" }, a.takeaway),
          h("span", { className: "foot" }, h("span", null, "#" + a.id)))))),

    health && h("footer", { className: "page-foot" },
      h("span", null, "Snapshot ", h("code", null, health.snapshot_sha256.slice(0, 12)), "\u2026"),
      h("span", null, health.snapshot),
      d && h("span", null, `through ${d.recent_games[0] ? d.recent_games[0].game_date : ""}`),
      h("span", null, `${items.length} saved analyses`)));
}

function App() {
  const first = readUrl();
  const [route, setRoute] = useState(first.route);
  // Ask opens clean. Previously the most recent analysis was whatever `selected`
  // happened to be, so the page loaded with a full prior report under the input.
  // An analysis shows only when the URL asks for one, or when a run finishes.
  const [selected, setSelected] = useState(first.analysis);
  const [items, setItems] = useState([]);
  const [health, setHealth] = useState(null);
  const [filter, setFilter] = useState("");
  const [tagFilter, setTagFilter] = useState("");
  const [tab] = useState(first.tab);
  // The drill-down tells the shell what to put in the breadcrumb; the shell
  // does not try to infer it from the URL, because only the page knows whether
  // id=2 is a team called Boston or a player called Bird.
  const [crumbs, setCrumbs] = useState([]);
  const [q, setQ] = useState("");

  const refresh = useCallback(async () => {
    try { setItems((await api("/api/analyses")).analyses); } catch { /* surfaced per page */ }
  }, []);
  useEffect(() => {
    installPrintHandlers(); refresh();
    api("/api/health").then(setHealth).catch(() => {});
  }, [refresh]);

  // The shell owns `v` and `analysis` and must leave everything else alone.
  // Rebuilding from an empty URLSearchParams wiped the drill-down params a
  // section had just written: child effects run before the parent's, so a
  // ?view=player&id=N deep link was read, rendered, and then erased from the
  // URL -- correct on screen, broken on reload and on copy-link.
  const lastOwned = useRef("");
  useEffect(() => {
    const u = new URLSearchParams(location.search);
    const changingRoute = u.get("v") !== route;
    u.set("v", route);
    if (route === "ask" && selected != null) u.set("analysis", String(selected));
    else u.delete("analysis");
    // Leaving one section for another must not carry the old section's
    // drill-down along with it.
    if (changingRoute)
      for (const k of ["view", "id", "season", "team", "type", "preds",
                       "outcome", "degree", "k", "from", "to", "min_games", "gt"])
        u.delete(k);
    const qs = u.toString();
    if (qs === lastOwned.current) return;
    lastOwned.current = qs;
    history.replaceState(null, "", "?" + qs);
  }, [route, selected]);

  const go = useCallback((id, analysis) => {
    setRoute(id);
    setCrumbs([]);
    if (analysis !== undefined) setSelected(analysis);
    window.scrollTo(0, 0);
  }, []);
  const openAnalysis = useCallback((id) => go("ask", id), [go]);

  const body = {
    home: () => h(Home, { go, items, health }),
    ask: () => h("div", null,
      h(AskBar, { onDone: async (id) => { await refresh(); setSelected(id); } }),
      h(Report, { id: selected })),
    model: () => h(ModelBuilder, null),
    // Validated findings is a TAB here, not a route: it is a filter over the
    // same store, and giving it its own rail item implied a second archive.
    knowledge: () => h(KnowledgeBase, { onOpenAnalysis: openAnalysis,
                                        initialTab: tab || "all" }),
    teams: () => h(Explorer, { section: "teams", onCrumb: setCrumbs }),
    players: () => h(Explorer, { section: "players", onCrumb: setCrumbs }),
    leaders: () => h(Explorer, { section: "leaders", onCrumb: setCrumbs }),
    catalog: () => h(Explorer, { section: "catalog", onCrumb: setCrumbs }),
    fantasy: () => h(Fantasy, null),
    matchup: () => h(Matchup, null),
    compare: () => h(CompareView, null),
    evolution: () => h(Evolution, null),
    lab: () => h(FantasyLab, null),
  }[route];

  const section = (ROUTES.find((r) => r.id === route) || {}).group || "League";
  return h("div", { className: "shell", "data-section": section },
    h("aside", { className: "rail no-print" },
      h("div", { className: "brand" },
        h("a", { href: "?v=home", onClick: (e) => { e.preventDefault(); go("home"); } },
          // The subtitle used to append the snapshot checksum, which read as a
          // string of junk under the title. The checksum belongs with the data
          // provenance, not in the masthead.
          h("div", { className: "mark" }, "NBA Analytics"),
          h("div", { className: "sub" }, "1946-47 → 2025-26"))),
      h(Nav, { route, go }),
      route === "ask" && h(SavedList, { items, selected, onSelect: setSelected,
        filter, setFilter, tagFilter, setTagFilter }),
      h("div", { className: "rail-foot" }, `${items.length} saved analyses`)),
    h("main", { className: "main", id: "main", tabIndex: -1 },
      h(TopBar, { route, crumbs, go, searchValue: q,
                  onSearch: (v) => { setQ(v); if (v.trim().length > 1 && route !== "players")
                                                 setRoute("players"); } }),
      h("div", { className: "page" }, body ? body() : h(Spinner, null))));
}

ReactDOM.createRoot(document.getElementById("root")).render(h(App));
