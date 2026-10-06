// Validated findings — investigations that committed to a verdict.
//
// The point of this page is that the reasoning is INSPECTABLE, so nothing here
// paraphrases an investigation. The verdict, the declared eras and the
// confounds are located in the stored answer; the argument itself is rendered
// verbatim. A generated summary of a statistical case is exactly what the rest
// of this project refuses to produce, and it would be worst here.
import { h, useState, useEffect, api, md, fmt, Spinner } from "/static/lib.js";

const TONE = {
  "CONFIRMED": "ok",
  "NOT CONFIRMED": "bad",
  "INCONCLUSIVE": "warn",
};

function Finding({ f, open, onToggle }) {
  const tone = TONE[f.verdict] || "warn";
  return h("article", { className: "finding " + tone },
    h("button", { className: "finding-head", onClick: onToggle },
      h("span", { className: "tag " + tone }, f.verdict),
      h("span", { className: "finding-q" }, f.claim || f.question),
      h("span", { className: "finding-caret" }, open ? "▾" : "▸")),
    h("div", { className: "finding-facets" },
      f.discovery_era && h("span", { className: "fact" },
        h("b", null, "Discovery era"), f.discovery_era),
      f.held_out_era && h("span", { className: "fact" },
        h("b", null, "Held-out era"), f.held_out_era),
      h("span", { className: "fact" }, h("b", null, "Steps"), String(f.steps)),
      f.unstable_steps > 0 && h("span", { className: "fact" },
        h("b", null, "Unstable steps"), String(f.unstable_steps)),
      f.from_explorer && h("span", { className: "fact" },
        h("b", null, "Origin"), "explorer scan"),
      h("span", { className: "fact" }, h("b", null, "Snapshot"), f.snapshot_sha256)),
    (f.confounds_examined || []).length > 0 && h("div", { className: "confounds" },
      h("span", { className: "faint" }, "Confounds examined: "),
      f.confounds_examined.map((c) => h("span", { className: "tag", key: c }, c))),
    open && h("div", null,
      h("details", { className: "step", open: false },
        h("summary", null, "The question exactly as it was put to the agent"),
        h("div", { className: "prose", style: { whiteSpace: "pre-wrap" } }, f.question)),
      f.methodology && h("details", { className: "step", open: false },
        h("summary", null, "Methodology, as stated before any query ran"),
        h("div", { className: "prose", dangerouslySetInnerHTML: md(f.methodology) })),
      h("div", { className: "prose", style: { marginTop: 14 },
                 dangerouslySetInnerHTML: md(f.answer) }),
      h("p", { className: "faint", style: { fontSize: 11 } },
        `Analysis #${f.run_id} · ${f.turns} turns`
        + (f.cost_usd ? ` · $${Number(f.cost_usd).toFixed(2)}` : ""))));
}

export function Findings() {
  const [d, setD] = useState(null);
  const [open, setOpen] = useState(() => new Set());
  const [filter, setFilter] = useState("all");
  useEffect(() => {
    api("/api/findings").then(setD).catch((e) => setD({ error: String(e) }));
  }, []);
  if (!d) return h(Spinner, null);
  if (d.error) return h("p", { className: "err" }, d.error);

  const toggle = (id) => setOpen((p) => {
    const n = new Set(p);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });
  const shown = (d.findings || []).filter(
    (f) => filter === "all" || f.verdict === filter);

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "Validated findings"),
      h("h2", null, "What survived testing, and what did not"),
      h("p", { className: "lede" }, d.note),
      h("div", { className: "meta-row" },
        h("span", null, h("b", null, d.counts["CONFIRMED"] || 0), " confirmed"),
        h("span", null, h("b", null, d.counts["NOT CONFIRMED"] || 0), " rejected"),
        h("span", null, h("b", null, d.counts["INCONCLUSIVE"] || 0), " inconclusive"))),

    h("div", { className: "controls no-print" },
      h("label", { className: "field" }, "Show",
        h("select", { value: filter, onChange: (e) => setFilter(e.target.value) },
          h("option", { value: "all" }, "every verdict"),
          h("option", { value: "CONFIRMED" }, "confirmed only"),
          h("option", { value: "NOT CONFIRMED" }, "rejected only"),
          h("option", { value: "INCONCLUSIVE" }, "inconclusive only")))),

    shown.length === 0
      ? h("p", { className: "empty" }, "No findings with that verdict.")
      : shown.map((f) => h(Finding, { key: f.run_id, f,
          open: open.has(f.run_id), onToggle: () => toggle(f.run_id) })));
}
