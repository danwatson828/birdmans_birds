// League evolution — how the game itself changed, 1946 to 2025.
//
// Every series starts at the season its stat became reliable rather than
// running flat back to 1946, so the shape of a line is the game changing and
// not the record-keeping changing. The chart vocabulary is the existing
// Vega-Lite Chart component; nothing new was built to draw these.
import { h, useState, useEffect, api, fmt, pct, Spinner, Chart, token } from "/static/lib.js";

const PCT_KEYS = new Set(["three_point_rate", "fg3_pct", "fg_pct", "ft_rate"]);

function lineSpec(key, label, isPct) {
  return {
    layer: [{
      mark: { type: "line", strokeWidth: 2, point: false },
      encoding: {
        x: { field: "season", type: "quantitative", title: "Season",
             axis: { format: "d" }, scale: { zero: false, nice: false } },
        y: { field: key, type: "quantitative", title: label,
             scale: { zero: false },
             axis: isPct ? { format: "%" } : {} },
        color: { value: token("--c1") },
        tooltip: [{ field: "season_label", title: "Season" },
                  { field: key, title: label,
                    format: isPct ? ".1%" : ".2f" }],
      },
    }],
  };
}

// The two series of finding #57 on one axis, so the divergence is the point
// of the picture rather than something the caption asserts.
function dualSpec() {
  return {
    transform: [
      { filter: "datum.three_point_rate != null && datum.fg3_pct != null" },
      { fold: ["three_point_rate", "fg3_pct"],
        as: ["measure", "value"] },
      { calculate: "datum.measure === 'three_point_rate' "
                 + "? 'Share of shots from three' : 'Three-point accuracy'",
        as: "Series" },
    ],
    mark: { type: "line", strokeWidth: 2 },
    encoding: {
      x: { field: "season", type: "quantitative", title: "Season",
           axis: { format: "d" }, scale: { zero: false, nice: false } },
      y: { field: "value", type: "quantitative", title: "Share of attempts",
           axis: { format: "%" }, scale: { zero: false } },
      color: { field: "Series", type: "nominal",
               scale: { range: [token("--c2"), token("--c3")] } },
      tooltip: [{ field: "season_label", title: "Season" },
                { field: "Series" },
                { field: "value", format: ".1%" }],
    },
  };
}

export function Evolution() {
  const [d, setD] = useState(null);
  const [picked, setPicked] = useState([
    "pts_per_team_game", "pace", "three_point_rate", "fg_pct"]);
  useEffect(() => {
    api("/api/explore/evolution").then(setD)
      .catch((e) => setD({ error: String(e) }));
  }, []);
  if (!d) return h(Spinner, { label: "reading eighty seasons…" });
  if (d.error) return h("p", { className: "err" }, d.error);

  const toggle = (k) => setPicked((p) =>
    p.includes(k) ? p.filter((x) => x !== k) : [...p, k]);
  const byKey = Object.fromEntries(d.metrics.map((m) => [m.key, m]));
  const f57 = d.finding_57;

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "League evolution"),
      h("h2", null, "How the game changed"),
      h("p", { className: "lede" },
        `${d.span[0]}–${d.span[1]}, league totals per team-game. `
        + "Each series starts at the season its stat became reliable, so a "
        + "line's shape is the game changing and not the bookkeeping."),
      h("div", { className: "meta-row" },
        h("span", null, h("b", null, d.series.length), " seasons"),
        h("span", null, h("b", null, d.metrics.length), " measures"))),

    // The validated finding leads, because it is the one thing on this page
    // that has actually been tested rather than merely plotted.
    h("section", { className: "block" },
      h("h3", null, "A validated finding"),
      h("div", { className: "panel accent", style: { marginBottom: 12 } },
        h("p", { style: { margin: 0 } },
          h("b", null, f57.title + ". "), f57.body)),
      h("figure", { className: "figure" },
        h("figcaption", null,
          "Attempt rate against accuracy, on the same seasons. The volume "
          + "line climbs; the accuracy line does not."),
        h(Chart, { spec: dualSpec(), rows: d.series, width: "container" }),
        h("div", { className: "srcline" },
          h("span", null, "from analysis #57, confirmed on a held-out era"),
          h("span", null, `${byKey.three_point_rate?.from_season}–`
            + `${byKey.three_point_rate?.to_season}`)))),

    h("section", { className: "block" },
      h("h3", null, "Choose what to chart"),
      h("div", { className: "panel" },
        h("div", { className: "vgrid" }, d.metrics.map((m) =>
          h("button", { key: m.key,
              className: "vchip" + (picked.includes(m.key) ? " on" : ""),
              onClick: () => toggle(m.key) },
            h("span", { className: "chip-label" }, m.label),
            h("span", { className: "chip-mean" },
              `${m.from_season}–${m.to_season}`)))))),

    picked.length === 0
      ? h("p", { className: "empty" }, "Pick a measure above.")
      : h("div", { className: "grid two" }, picked.map((k) => {
          const m = byKey[k];
          if (!m) return null;
          const isPct = PCT_KEYS.has(k);
          const rows = d.series.filter((s) => s[k] != null);
          const first = rows[0], last = rows[rows.length - 1];
          const fmtv = (v) => isPct ? pct(v) : fmt(v, 1);
          return h("figure", { className: "figure", key: k },
            h("figcaption", null, m.label),
            h(Chart, { spec: lineSpec(k, m.label, isPct), rows,
                       width: "container" }),
            h("div", { className: "srcline" },
              h("span", null,
                `${first.season}: ${fmtv(first[k])} → `
                + `${last.season}: ${fmtv(last[k])}`),
              h("span", { className: "faint" }, `${m.n} seasons`)),
            m.coverage_note && h("p", { className: "faint",
                style: { fontSize: 11, margin: "4px 0 0" } }, m.coverage_note));
        })),

    h("p", { className: "faint", style: { fontSize: 11 } }, d.method));
}
