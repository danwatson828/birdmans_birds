// League evolution — explorable, not a wall of charts.
//
// One metric drives a main chart you can brush to a range; a second metric can
// be overlaid INDEXED to a base year, because pace and three-point rate do not
// share an axis and plotting them raw on one makes the flatter line look like
// nothing happened. A strip of small multiples sits below, and clicking a tile
// promotes it to the main chart — a drill-down, not decoration.
//
// Every series still starts at the season its stat became reliable (the server
// cuts it from v_stat_first_tracked), so a line's shape is the game changing
// and not the bookkeeping changing.
import { h, useState, useEffect, useRef, api, fmt, pct, Spinner, token }
  from "/static/lib.js";

const PCT_KEYS = new Set(["three_point_rate", "fg3_pct", "fg_pct", "ft_rate"]);
const fmtV = (k, v) => v == null ? "—" : PCT_KEYS.has(k) ? pct(v, 1) : fmt(v, 1);

// ---------------------------------------------------------------- the chart
// Hand-drawn SVG rather than a Vega embed: this needs a brush, a hover
// readout, a second indexed axis and clickable era markers, and wiring those
// through a chart grammar is more code than drawing two paths.
function Trend({ rows, k, k2, label, label2, markers, from, to, onBrush }) {
  const [hover, setHover] = useState(null);
  const ref = useRef(null);
  const W = 980, H = 300, P = { t: 16, r: 54, b: 34, l: 54 };
  const inRange = rows.filter((r) => r.season >= from && r.season <= to
                                     && r[k] != null);
  if (inRange.length < 2) return h("p", { className: "muted" }, "not enough seasons");

  const xs = inRange.map((r) => r.season);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const X = (s) => P.l + (s - x0) / Math.max(1, x1 - x0) * (W - P.l - P.r);

  const vals = inRange.map((r) => r[k]);
  const lo = Math.min(...vals), hi = Math.max(...vals);
  const pad = (hi - lo) * 0.12 || 1;
  const Y = (v) => H - P.b - (v - (lo - pad)) / ((hi + pad) - (lo - pad)) * (H - P.t - P.b);

  // The overlay is INDEXED to its first in-range value, so two metrics with
  // different units can share a picture without a misleading second axis.
  const base2 = k2 ? (inRange.find((r) => r[k2] != null) || {})[k2] : null;
  const idx2 = (v) => base2 ? (v / base2) * 100 : null;
  const i2 = k2 ? inRange.filter((r) => r[k2] != null).map((r) => idx2(r[k2])) : [];
  const lo2 = i2.length ? Math.min(...i2) : 0, hi2 = i2.length ? Math.max(...i2) : 1;
  const pad2 = (hi2 - lo2) * 0.12 || 1;
  const Y2 = (v) => H - P.b - (idx2(v) - (lo2 - pad2)) / ((hi2 + pad2) - (lo2 - pad2))
                              * (H - P.t - P.b);

  const path = (key, yf) => inRange.filter((r) => r[key] != null)
    .map((r, i) => (i ? "L" : "M") + X(r.season).toFixed(1) + " " + yf(r[key]).toFixed(1))
    .join(" ");

  const ticks = 5;
  const yTicks = Array.from({ length: ticks }, (_, i) =>
    lo - pad + (i / (ticks - 1)) * ((hi + pad) - (lo - pad)));
  const span = x1 - x0;
  const step = span > 50 ? 10 : span > 20 ? 5 : span > 10 ? 2 : 1;
  const xTicks = [];
  for (let y = Math.ceil(x0 / step) * step; y <= x1; y += step) xTicks.push(y);

  const seasonAt = (clientX) => {
    const b = ref.current.getBoundingClientRect();
    const px = (clientX - b.left) / b.width * W;
    const s = x0 + (px - P.l) / (W - P.l - P.r) * (x1 - x0);
    return Math.max(x0, Math.min(x1, Math.round(s)));
  };

  return h("div", { className: "trend" },
    h("svg", { ref, viewBox: `0 0 ${W} ${H}`, width: "100%", role: "img",
        "aria-label": `${label} by season, ${x0} to ${x1}`,
        onMouseMove: (e) => setHover(seasonAt(e.clientX)),
        onMouseLeave: () => setHover(null) },
      yTicks.map((t, i) => h("g", { key: "y" + i },
        h("line", { x1: P.l, x2: W - P.r, y1: Y(t), y2: Y(t),
                    stroke: token("--chart-grid"), strokeWidth: 1 }),
        h("text", { x: P.l - 8, y: Y(t) + 4, textAnchor: "end",
                    fontFamily: "var(--mono)", fontSize: 10,
                    fill: token("--chart-axis") }, fmtV(k, t)))),
      xTicks.map((y) => h("text", { key: "x" + y, x: X(y), y: H - 12,
          textAnchor: "middle", fontFamily: "var(--mono)", fontSize: 10,
          fill: token("--chart-axis") }, y)),

      // Curated rule changes. Dashed, labelled, and never drawn as if the
      // data contained them.
      (markers || []).filter((m) => m.season >= x0 && m.season <= x1).map((m) =>
        h("g", { key: "m" + m.season },
          h("line", { x1: X(m.season), x2: X(m.season), y1: P.t, y2: H - P.b,
                      stroke: token("--n5"), strokeWidth: 1, strokeDasharray: "3 3" }),
          h("text", { x: X(m.season) + 4, y: P.t + 10, fontFamily: "var(--sans)",
                      fontSize: 9.5, fill: token("--chart-axis") }, m.label))),

      k2 && h("path", { d: path(k2, Y2), fill: "none", stroke: token("--c2"),
                        strokeWidth: 2, strokeDasharray: "5 3" }),
      h("path", { d: path(k, Y), fill: "none", stroke: token("--sec"),
                  strokeWidth: 2.25, strokeLinejoin: "round" }),

      hover != null && (() => {
        const r = inRange.find((x) => x.season === hover);
        if (!r || r[k] == null) return null;
        return h("g", null,
          h("line", { x1: X(hover), x2: X(hover), y1: P.t, y2: H - P.b,
                      stroke: token("--ink"), strokeWidth: 1, opacity: .35 }),
          h("circle", { cx: X(hover), cy: Y(r[k]), r: 3.5, fill: token("--sec") }),
          k2 && r[k2] != null
            && h("circle", { cx: X(hover), cy: Y2(r[k2]), r: 3, fill: token("--c2") }));
      })()),

    h("div", { className: "tr-read" },
      hover != null
        ? (() => {
            const r = rows.find((x) => x.season === hover) || {};
            return [
              h("span", { key: "s", className: "s" }, r.season_label || hover),
              h("span", { key: "a" }, h("i", { style: { background: token("--sec") } }),
                label, " ", h("b", null, fmtV(k, r[k]))),
              k2 && h("span", { key: "b" },
                h("i", { style: { background: token("--c2") } }),
                label2, " ", h("b", null, fmtV(k2, r[k2])),
                base2 ? h("em", null, ` (${(idx2(r[k2]) || 0).toFixed(0)} vs base 100)`) : null),
            ];
          })()
        : h("span", { className: "hint" }, "hover the chart for a season's values")),

    h("div", { className: "tr-brush" },
      h("label", null, "From",
        h("input", { type: "range", min: rows[0].season, max: to - 1, value: from,
          onChange: (e) => onBrush(Number(e.target.value), to) })),
      h("span", { className: "rng" }, `${from}–${to}`),
      h("label", null, "To",
        h("input", { type: "range", min: from + 1, max: rows[rows.length - 1].season,
          value: to, onChange: (e) => onBrush(from, Number(e.target.value)) })),
      h("button", { className: "linkish",
        onClick: () => onBrush(rows[0].season, rows[rows.length - 1].season) },
        "Reset range")));
}

// A tile in the small-multiples strip. Clicking promotes it to the main chart.
function Tile({ rows, m, active, onPick }) {
  const vals = rows.filter((r) => r[m.key] != null);
  if (vals.length < 2) return null;
  const W = 150, H = 40;
  const xs = vals.map((r) => r.season);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const ys = vals.map((r) => r[m.key]);
  const lo = Math.min(...ys), hi = Math.max(...ys);
  const d = vals.map((r, i) => (i ? "L" : "M")
    + ((r.season - x0) / (x1 - x0) * W).toFixed(1) + " "
    + (H - (hi === lo ? .5 : (r[m.key] - lo) / (hi - lo)) * (H - 6) - 3).toFixed(1)).join(" ");
  const first = vals[0][m.key], last = vals[vals.length - 1][m.key];
  return h("button", { className: "sm" + (active ? " on" : ""), onClick: () => onPick(m.key),
      "aria-pressed": active, title: `Chart ${m.label}` },
    h("span", { className: "sm-lab" }, m.label),
    h("svg", { viewBox: `0 0 ${W} ${H}`, width: "100%", height: H, "aria-hidden": "true" },
      h("path", { d, fill: "none", strokeWidth: 1.75,
                  stroke: active ? token("--sec") : token("--n5") })),
    h("span", { className: "sm-rng" },
      `${fmtV(m.key, first)} → ${fmtV(m.key, last)}`));
}

export function Evolution() {
  const [d, setD] = useState(null);
  const [markers, setMarkers] = useState([]);
  const [main, setMain] = useState("three_point_rate");
  const [overlay, setOverlay] = useState("fg3_pct");
  const [range, setRange] = useState(null);
  useEffect(() => {
    api("/api/explore/evolution").then(setD).catch(() => setD(false));
    api("/api/explore/era-markers").then((m) => setMarkers(m.markers || []))
      .catch(() => setMarkers([]));
  }, []);
  if (d === false) return h("p", { className: "err" }, "failed to load");
  if (!d) return h(Spinner, { label: "reading eighty seasons…" });

  // Promoting a tile must not leave the same metric as both line and overlay
  // ("X, with X overlaid"), so picking the overlay as the main clears it.
  const pickMain = (k) => { setMain(k); if (k === overlay) setOverlay(""); };
  const byKey = Object.fromEntries(d.metrics.map((m) => [m.key, m]));
  const rows = d.series;
  const lo = rows[0].season, hi = rows[rows.length - 1].season;
  const [from, to] = range || [lo, hi];
  const mainM = byKey[main] || d.metrics[0];
  const ovM = overlay ? byKey[overlay] : null;

  // Finding #57 as a preset: the two series it compared, on the seasons it used.
  const preset57 = () => {
    setMain("three_point_rate"); setOverlay("fg3_pct");
    setRange([Math.max(lo, 1983), hi]);
  };

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "lhs" },
        h("div", { className: "eyebrow" }, "League"),
        h("h1", null, "How the game changed"),
        h("p", { className: "lede" },
          `${d.span[0]}–${d.span[1]}, league totals per team-game. Each `
          + "series starts at the season its stat became reliable, so a line's "
          + "shape is the game changing and not the bookkeeping changing.")),
      h("div", { className: "meta" },
        h("span", null, h("b", null, rows.length), "seasons"),
        h("span", null, h("b", null, d.metrics.length), "measures"))),

    h("div", { className: "toolbar" },
      h("div", { className: "grp" },
        h("label", { htmlFor: "ev-main" }, "Metric"),
        h("select", { id: "ev-main", value: main,
            onChange: (e) => pickMain(e.target.value) },
          d.metrics.map((m) => h("option", { key: m.key, value: m.key }, m.label)))),
      h("div", { className: "grp" },
        h("label", { htmlFor: "ev-ov" }, "Overlay"),
        h("select", { id: "ev-ov", value: overlay,
            onChange: (e) => setOverlay(e.target.value) },
          h("option", { value: "" }, "none"),
          d.metrics.filter((m) => m.key !== main)
            .map((m) => h("option", { key: m.key, value: m.key }, m.label)))),
      h("div", { className: "spacer" }),
      h("button", { className: "btn-ghost", onClick: preset57 },
        "Finding #57 preset")),

    h("figure", { className: "figure" },
      h("figcaption", null,
        mainM.label,
        ovM ? `, with ${ovM.label} overlaid and indexed to ${from} = 100` : "",
        ". Dashed verticals are curated rule changes, not data."),
      h(Trend, { rows, k: main, k2: overlay || null, label: mainM.label,
                 label2: ovM ? ovM.label : "", markers, from, to,
                 onBrush: (a, b) => setRange([a, b]) }),
      h("div", { className: "srcline" },
        h("span", null, mainM.coverage_note
          || `${mainM.from_season}–${mainM.to_season}`),
        h("span", { className: "faint" }, "rule markers: curated"))),

    h("div", { className: "sect-head", style: { marginTop: "var(--s5)" } },
      h("h2", null, "Every measure"),
      h("span", { className: "count" }, "click a tile to chart it")),
    h("div", { className: "smalls" }, d.metrics.map((m) =>
      h(Tile, { key: m.key, rows, m, active: m.key === main, onPick: pickMain }))),

    h("p", { className: "faint", style: { fontSize: "var(--t-xs)", marginTop: "var(--s4)" } },
      d.method));
}
