// Clustering view — k-means over chosen variables, shown in a PCA projection.
// Clicking a cluster lists who is in it, because a coloured blob is not a
// finding until you can see which players it is made of.
import { h, useState, api, fmt, pct, Spinner, Chart, series } from "/static/lib.js";

// Was ten hand-picked colours at 24% mean saturation inside an 11-point
// lightness band -- ten categories separated by hue alone at near-identical
// value, which is both hard to tell apart and the "dead monotone" this design
// pass exists to undo. --c1..--c10 run 47-89% saturation across a 28-point
// lightness spread, so the clusters survive both a small swatch and greyscale.
// Lazy, not module-scope: resolving a custom property needs the stylesheet
// applied, and token() caches, so repeated calls cost nothing.
const SERIES = () => series(10);

export function ClusterView({ d, onPick, picked }) {
  if (!d) return null;
  // The cards call the first group "Group 1"; the chart legend was reading its
  // raw label and calling the same group "0". One numbering, decided here.
  const points = d.points.map((p) => ({ ...p, group: p.cluster + 1 }));
  const spec = {
    layer: [
      { mark: { type: "point", filled: true, size: 42, opacity: 0.72 },
        encoding: {
          x: { field: "pc1", type: "quantitative",
               title: `Principal component 1 — ${(d.pc1_variance * 100).toFixed(0)}% of variation`,
               scale: { zero: false } },
          y: { field: "pc2", type: "quantitative",
               title: `Principal component 2 — ${(d.pc2_variance * 100).toFixed(0)}%`,
               scale: { zero: false } },
          color: { field: "group", type: "nominal", title: "Group",
                   scale: { domain: Array.from({ length: d.k }, (_, i) => i + 1),
                            range: SERIES().slice(0, d.k) } },
          tooltip: [{ field: "player", title: "Player" },
                    { field: "group", title: "Group" }] } },
    ],
  };
  return h("div", null,
    h("section", { className: "block" },
      h("h3", null, "Where the groups sit"),
      h("figure", { className: "figure" },
        h("figcaption", null,
          `${fmt(d.n_players)} players placed by ${d.variables.length} standardised `
          + `variables, projected onto two principal components carrying `
          + `${((d.pc1_variance + d.pc2_variance) * 100).toFixed(0)}% of the variation.`),
        h(Chart, { spec, rows: points, width: 640 }),
        h("div", { className: "srcline" },
          h("span", null, d.variables.map((v) => v.label).join(" · ")),
          h("span", null, `${d.season_from}–${d.season_to}, ≥${d.min_games} games`)))),

    h("section", { className: "block" },
      h("h3", null, `Groups — ${d.k}, click one to see who is in it`),
      h("div", { className: "grid three" }, d.clusters.map((c) =>
        h("button", { key: c.cluster,
            className: "card-link cluster-card"
              + (picked === c.cluster ? " picked" : ""),
            style: { borderLeft: `4px solid ${SERIES()[c.cluster]}` },
            onClick: () => onPick(picked === c.cluster ? null : c.cluster) },
          h("h4", null, `Group ${c.cluster + 1}`,
            h("span", { className: "faint", style: { fontWeight: 400 } },
              `  ${c.size} players`)),
          h("p", null, c.label)))),

      picked != null && (() => {
        const c = d.clusters.find((x) => x.cluster === picked);
        if (!c) return null;
        return h("div", { className: "panel", style: { marginTop: 14 } },
          h("h4", null, `Group ${picked + 1} — ${c.size} players, `
            + `nearest the centre first`),
          h("div", { className: "scroll-y scroll-x" },
            h("table", { className: "data" },
              h("thead", null, h("tr", null,
                h("th", { className: "name" }, "Player"),
                d.variables.map((v) => h("th", { key: v.key }, v.label)))),
              h("tbody", null, c.members.map((m) =>
                h("tr", { key: m.player_id },
                  h("td", { className: "name" }, m.player),
                  // A shooting percentage held as a proportion reads as "0.4"
                  // under a "Three-point %" header; the units have to match
                  // what the column says it is.
                  d.variables.map((v) =>
                    h("td", { key: v.key },
                      /_pct$/.test(v.key) ? pct(m[v.key]) : fmt(m[v.key], 1)))))))));
      })()));
}
