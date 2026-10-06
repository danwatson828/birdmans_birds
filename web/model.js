// Model builder — the correlation explorer grown up.
//
// Multiple predictors, polynomial terms, logistic fits, residual diagnostics and
// side-by-side comparison. Two investigations reached the same two conclusions
// by hand (a single slope hides curvature; repeated seasons of one player are
// not independent), so both are first-class here rather than left to be
// rediscovered a third time.
import { h, useState, useEffect, useCallback, api, post, fmt, Spinner, Chart, token }
  from "/static/lib.js";
import { ClusterView } from "/static/cluster.js";

const GT = ["regular", "playoff", "play_in", "cup_final"];
const sig = (p) => p == null ? "—" : p < 1e-4 ? p.toExponential(1) : p.toFixed(4);
const DEGREE = [[1, "linear"], [2, "+ quadratic"], [3, "+ cubic"]];

// Notation nobody reads is not an explanation. The equation is shown, but the
// sentence beneath it is what actually says what the model claims.
function plainLanguage(spec, outcomeLabel, labelFor) {
  if (!spec || !spec.coefficients) return null;
  const terms = spec.coefficients.filter((c) => c.term !== "const");
  return terms.map((c) => {
    const m = c.term.match(/^(.+?)\^(\d)$/);
    const base = labelFor(m ? m[1] : c.term).toLowerCase();
    const dir = c.estimate >= 0 ? "rises" : "falls";
    const sure = c.p < 0.05
      ? `the interval [${c.ci_low.toFixed(4)}, ${c.ci_high.toFixed(4)}] excludes zero`
      : `but the interval [${c.ci_low.toFixed(4)}, ${c.ci_high.toFixed(4)}] covers zero, so this term is not distinguishable from no effect`;
    return m
      ? { term: c.term, text: `A squared/cubed term on ${base} (power ${m[2]}), `
          + `coefficient ${c.estimate.toFixed(5)} — it bends the line rather than `
          + `tilting it, so the effect of ${base} is not the same at every level. `
          + `${c.p < 0.05 ? "It earns its place: " + sure : sure}.`, ok: c.p < 0.05 }
      : { term: c.term, text: `For each additional unit of ${base}, `
          + `${outcomeLabel.toLowerCase()} ${dir} by `
          + `${Math.abs(c.estimate).toFixed(4)} on average — ${sure}.`,
          ok: c.p < 0.05 };
  });
}

// A fit or a grouping is the thing worth sending to someone, and until now the
// only way to hand it over was a list of instructions for reproducing it by
// hand. The whole specification is short enough to live in the query string.
const MODEL_DEFAULTS = { outcome: "bpg", preds: ["height_in"], degree: 2,
                         type: "linear", k: 5, from: 2017, to: 2025,
                         minG: 41, gt: "regular" };

function readModelUrl() {
  const u = new URLSearchParams(location.search);
  const int = (key, fallback) => {
    const n = Number(u.get(key));
    return Number.isFinite(n) && u.get(key) !== null && u.get(key) !== "" ? n : fallback;
  };
  const preds = (u.get("preds") || "").split(",").map((x) => x.trim()).filter(Boolean);
  const type = u.get("type");
  return {
    ...MODEL_DEFAULTS,
    ...(u.get("outcome") ? { outcome: u.get("outcome") } : {}),
    ...(preds.length ? { preds } : {}),
    ...(["linear", "logistic", "cluster"].includes(type) ? { type } : {}),
    ...(u.get("gt") ? { gt: u.get("gt") } : {}),
    degree: int("degree", MODEL_DEFAULTS.degree),
    k: int("k", MODEL_DEFAULTS.k),
    from: int("from", MODEL_DEFAULTS.from),
    to: int("to", MODEL_DEFAULTS.to),
    minG: int("min_games", MODEL_DEFAULTS.minG),
  };
}

// Seventy-four variables will not fit in one undifferentiated wall of chips.
// They are grouped by family, each group collapsible, each variable carrying a
// one-line statement of what it actually measures -- which matters most for
// the advanced metrics, where the name alone ("assist ratio", "PIE") does not
// tell you what is being counted.
function PredictorPicker({ vars, preds, togglePred, type }) {
  const groups = vars.groups || [];
  const [open, setOpen] = useState(() => new Set(["basic"]));
  const [q, setQ] = useState("");
  const toggleGroup = (k) => setOpen((prev) => {
    const next = new Set(prev);
    next.has(k) ? next.delete(k) : next.add(k);
    return next;
  });
  const needle = q.trim().toLowerCase();
  const match = (v) => !needle
    || v.label.toLowerCase().includes(needle)
    || (v.meaning || "").toLowerCase().includes(needle);
  const chosen = vars.predictors.filter((v) => preds.includes(v.key));

  return h("section", { className: "block" },
    h("h3", null, type === "cluster"
      ? `Variables \u2014 ${preds.length} chosen`
      : `Predictors \u2014 ${preds.length} of 6`),

    // What is currently selected, always visible, always removable -- with 74
    // options behind collapsed groups, a chosen variable must not be something
    // you have to go hunting for to deselect.
    h("div", { className: "chosen" },
      chosen.length === 0
        ? h("span", { className: "faint" }, "Nothing selected yet.")
        : chosen.map((v) =>
            h("button", { key: v.key, className: "vchip on",
                title: v.meaning, onClick: () => togglePred(v.key) },
              v.label, h("span", { className: "x" }, "\u00d7")))),

    h("div", { className: "panel" },
      h("input", { type: "search", value: q, className: "var-search",
        placeholder: "Filter 74 variables by name or meaning\u2026",
        onChange: (e) => setQ(e.target.value) }),
      groups.map((g) => {
        const items = vars.predictors.filter((v) => v.group === g.key && match(v));
        if (!items.length) return null;
        const isOpen = open.has(g.key) || !!needle;
        const picked = items.filter((v) => preds.includes(v.key)).length;
        return h("div", { className: "vgroup", key: g.key },
          h("button", { className: "vgroup-head", onClick: () => toggleGroup(g.key) },
            h("span", { className: "caret" }, isOpen ? "\u25be" : "\u25b8"),
            h("span", { className: "nm" }, g.label),
            h("span", { className: "ct" }, `${items.length}`),
            picked > 0 && h("span", { className: "picked" }, `${picked} selected`)),
          isOpen && h("div", { className: "vgrid" }, items.map((v) =>
            h("button", { key: v.key,
                className: "vchip" + (preds.includes(v.key) ? " on" : ""),
                onClick: () => togglePred(v.key) },
              h("span", { className: "chip-label" },
                v.label, v.player_constant
                  ? h("span", { className: "fixed-mark" }, "fixed") : null),
              v.meaning && h("span", { className: "chip-mean" }, v.meaning)))));
      })),

    h("p", { className: "muted", style: { margin: "10px 0 0", fontSize: 12 } },
      "\u201cfixed\u201d marks an attribute that cannot vary within a player. "
      + "Including one switches standard errors to player-clustered, because "
      + "repeated seasons then stop carrying independent information. "
      + "\u201cPrior season\u201d variables are blank for a player who missed "
      + "the season before, rather than carrying a value across the gap."));
}

export function ModelBuilder() {
  const init = readModelUrl();
  const [vars, setVars] = useState({ predictors: [], outcomes: [] });
  const [outcome, setOutcome] = useState(init.outcome);
  const [preds, setPreds] = useState(init.preds);
  const [degree, setDegree] = useState(init.degree);
  const [type, setType] = useState(init.type);
  const [k, setK] = useState(init.k);
  const [cluster, setCluster] = useState(null);
  const [picked, setPicked] = useState(null);
  const [from, setFrom] = useState(init.from);
  const [to, setTo] = useState(init.to);
  const [minG, setMinG] = useState(init.minG);
  const [gt, setGt] = useState(init.gt);
  const [d, setD] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => { api("/api/model/variables").then(setVars).catch(() => {}); }, []);

  // Only non-default settings go in the URL, so an untouched builder keeps a
  // clean ?v=model and a shared link says exactly what was changed.
  useEffect(() => {
    const u = new URLSearchParams(location.search);
    const put = (key, value, fallback) => {
      if (value === fallback || value == null) u.delete(key);
      else u.set(key, String(value));
    };
    put("type", type, MODEL_DEFAULTS.type);
    put("preds", preds.join(","), MODEL_DEFAULTS.preds.join(","));
    put("outcome", type === "cluster" ? null : outcome, MODEL_DEFAULTS.outcome);
    put("degree", type === "cluster" ? null : degree, MODEL_DEFAULTS.degree);
    put("k", type === "cluster" ? k : null, MODEL_DEFAULTS.k);
    put("from", from, MODEL_DEFAULTS.from);
    put("to", to, MODEL_DEFAULTS.to);
    put("min_games", minG, MODEL_DEFAULTS.minG);
    put("gt", gt, MODEL_DEFAULTS.gt);
    history.replaceState(null, "", "?" + u.toString());
  }, [type, preds, outcome, degree, k, from, to, minG, gt]);

  const run = useCallback(async () => {
    if (!preds.length) { setErr("Choose at least one predictor."); return; }
    setD(null); setErr(null); setCluster(null); setPicked(null);
    if (type === "cluster") {
      if (preds.length < 2) { setErr("Clustering needs at least two variables."); return; }
      try {
        setCluster(await post("/api/model/cluster", {
          variables: preds, k, season_from: from, season_to: to,
          min_games: minG, game_type: gt }));
      } catch (e) { setErr(String(e)); }
      return;
    }
    // Always fit the WHOLE ladder, not just up to the selected degree. Fitting
    // one model and calling it best-by-AIC is what made every selection look
    // like the winner; the comparison has to contain the alternatives for the
    // verdict to mean anything. "Form" picks which model is featured for
    // coefficients and residuals, not how many get fit.
    const specs = [1, 2, 3].map((g) =>
      ({ label: DEGREE[g - 1][1], predictors: preds, degree: g }));
    try {
      setD(await post("/api/model/fit", {
        outcome, specs, season_from: from, season_to: to,
        min_games: minG, game_type: gt, model_type: type,
        diagnostics_for: DEGREE[degree - 1][1] }));
    } catch (e) { setErr(String(e)); }
  }, [outcome, preds, degree, type, from, to, minG, gt, k]);
  useEffect(() => { run(); }, [run]);

  const togglePred = (k) => setPreds((p) =>
    p.includes(k) ? p.filter((x) => x !== k) : p.length < 6 ? [...p, k] : p);

  const featured = DEGREE[degree - 1][1];
  const dg = d && d.diagnostics;

  // Goodness of fit you can see: actual against predicted with the y = x line.
  // A tight band on the diagonal is a good model; R² is the same fact as a
  // number you have to already know how to read.
  const fitSpec = dg && dg.points && dg.fit_range && {
    layer: [
      { data: { values: [{ v: dg.fit_range[0] }, { v: dg.fit_range[1] }] },
        mark: { type: "line", color: token("--n5"), strokeDash: [4, 3], size: 1.5 },
        encoding: { x: { field: "v", type: "quantitative" },
                    y: { field: "v", type: "quantitative" } } },
      { mark: { type: "point", filled: true, size: 20, opacity: 0.3, color: token("--c1") },
        encoding: {
          x: { field: "fitted", type: "quantitative", scale: { zero: false },
               title: `Predicted ${d.outcome_label.toLowerCase()}` },
          y: { field: "actual", type: "quantitative", scale: { zero: false },
               title: `Actual ${d.outcome_label.toLowerCase()}` } } },
    ],
  };

  const residSpec2 = dg && dg.points && {
    layer: [
      { mark: { type: "point", filled: true, size: 20, opacity: 0.3, color: token("--c1") },
        encoding: {
          x: { field: "fitted", type: "quantitative", scale: { zero: false },
               title: `Predicted ${d.outcome_label.toLowerCase()}` },
          y: { field: "resid", type: "quantitative",
               title: "Actual minus predicted" } } },
      { mark: { type: "rule", color: token("--c2"), size: 1.5 },
        encoding: { y: { datum: 0 } } },
    ],
  };

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "lhs" },
      h("div", { className: "eyebrow" }, "Model builder \u00b7 exploratory"),
      h("h1", null, type === "cluster"
        ? (cluster ? `${cluster.k} groups from ${preds.length} variables` : "Group players")
        : d ? `${d.outcome_label} on ${preds.length} predictor${preds.length > 1 ? "s" : ""}`
            : "Build a model"),
      h("p", { className: "lede" }, type === "cluster"
        ? "Group players by the variables you choose, then look at who landed "
          + "together. k-means describes the sample it was given; it does not "
          + "predict anything."
        : type === "logistic"
        ? "Fit a ladder of nested specifications on one sample and compare them. "
          + "Logistic regression on whether the outcome is above its median."
        : "Fit a ladder of nested specifications on one sample and compare them. "
          + "No model is involved in the fitting — this is ordinary least squares "
          + "over the snapshot."),
      cluster && h("div", { className: "meta-row" },
        h("span", { className: "tag warn" }, "exploratory"),
        h("span", null, h("b", null, fmt(cluster.n_players)), " players"),
        h("span", null, h("b", null,
          `${((cluster.pc1_variance + cluster.pc2_variance) * 100).toFixed(0)}%`),
          " of variation shown")),
      d && h("div", { className: "meta-row" },
        h("span", { className: "tag warn" }, "exploratory"),
        h("span", null, h("b", null, fmt(d.n)), " player-seasons"),
        h("span", null, h("b", null, fmt(d.n_players)), " players"),
        h("span", { className: "tag " + (d.clustered ? "accent" : "") },
          d.clustered ? "clustered SEs" : "HC3 SEs")))),

    // Controls BESIDE the workspace, not above it. Choosing a predictor used
    // to scroll the fit off the screen, so you tuned the model blind and
    // scrolled back to see what you had done.
    h("div", { className: "split" },
    h("div", { className: "side no-print" },
    h("div", { className: "panel modelctl" },
      type !== "cluster" && h("label", { className: "field" }, "Outcome",
        h("select", { value: outcome, onChange: (e) => setOutcome(e.target.value) },
          vars.outcomes.map((v) => h("option", { key: v.key, value: v.key }, v.label)))),
      type !== "cluster" && h("label", { className: "field" }, "Featured",
        h("select", { value: degree, onChange: (e) => setDegree(Number(e.target.value)) },
          DEGREE.map(([g, l]) => h("option", { key: g, value: g }, l)))),
      h("label", { className: "field" }, "Type",
        h("select", { value: type, onChange: (e) => setType(e.target.value) },
          h("option", { value: "linear" }, "linear (OLS)"),
          h("option", { value: "logistic" }, "logistic (above median)"),
          h("option", { value: "cluster" }, "clustering (k-means)"))),
      type === "cluster" && h("label", { className: "field" }, "Groups",
        h("select", { value: k, onChange: (e) => setK(Number(e.target.value)) },
          [2, 3, 4, 5, 6, 7, 8].map((n) => h("option", { key: n, value: n }, n)))),
      h("label", { className: "field" }, "Seasons",
        h("input", { type: "number", value: from, style: { width: 74 },
          onChange: (e) => setFrom(Number(e.target.value) || 0) }), "–",
        h("input", { type: "number", value: to, style: { width: 74 },
          onChange: (e) => setTo(Number(e.target.value) || 0) })),
      h("label", { className: "field" }, "Min games",
        h("input", { type: "number", value: minG, style: { width: 68 },
          onChange: (e) => setMinG(Number(e.target.value) || 0) })),
      h("label", { className: "field" }, "Games",
        h("select", { value: gt, onChange: (e) => setGt(e.target.value) },
          GT.map((g) => h("option", { key: g, value: g }, g.replace("_", "-")))))),

    ),
    h(PredictorPicker, { vars, preds, togglePred, type })),

    h("div", { className: "work" },
    err && h("p", { className: "err" }, err),
    type === "cluster" && !cluster && !err && h(Spinner, { label: "grouping…" }),
    type === "cluster" && cluster && h("div", null,
      h("div", { className: "panel warn" },
        h("p", { style: { margin: 0 } }, h("b", null, "Exploratory. "), cluster.warning)),
      h(ClusterView, { d: cluster, picked, onPick: setPicked })),
    type !== "cluster" && !d && !err && h(Spinner, { label: "fitting…" }),

    type !== "cluster" && d && h("div", null,
      h("div", { className: "panel warn" },
        h("p", { style: { margin: 0 } }, h("b", null, "Exploratory. "), d.warning)),

      (() => {
        const feat = d.results.find((r) => r.label === featured) || d.results[0];
        if (!feat || !feat.coefficients) return null;
        const labelFor = (k) => (vars.predictors.find((v) => v.key === k) || {}).label || k;
        const eq = feat.coefficients.map((c, i) =>
          c.term === "const"
            ? c.estimate.toFixed(4)
            : `${c.estimate >= 0 ? " + " : " − "}${Math.abs(c.estimate).toFixed(5)}·${c.term}`)
          .join("");
        const plain = plainLanguage(feat, d.outcome_label, labelFor);
        return h("div", null,
          h("section", { className: "block" },
            h("h3", null, `The model — ${feat.label}`),
            h("div", { className: "panel" },
              h("div", { className: "stat-row", style: { marginBottom: 14 } },
                [[d.model_type === "logistic" ? "Pseudo R²" : "R²",
                  feat.r_squared != null ? feat.r_squared.toFixed(3) : "—"],
                 ["n", fmt(d.n)], ["Players", fmt(d.n_players)],
                 ["Terms", feat.terms.length]].map(([k, v]) =>
                  h("div", { className: "stat", key: k },
                    h("div", { className: "v" }, v), h("div", { className: "k" }, k)))),
              h("div", { className: "equation" },
                h("b", null, d.outcome_label), " = ", eq),
              plain && h("ul", { className: "plain" }, plain.map((t) =>
                h("li", { key: t.term, className: t.ok ? "" : "muted" }, t.text))),
              h("p", { className: "muted", style: { marginBottom: 0, fontSize: 12 } },
                d.cluster_note))),

          h("section", { className: "block" },
            h("h3", null, "Coefficients"),
            h("div", { className: "panel scroll-x" },
              h("table", { className: "data" },
                h("thead", null, h("tr", null,
                  ["Term", "Estimate", "95% CI", "p"].map((x) =>
                    h("th", { key: x, className: x === "Term" ? "name" : "" }, x)))),
                h("tbody", null, feat.coefficients.map((c) =>
                  h("tr", { key: c.term, className: c.p < 0.05 ? "" : "dim" },
                    h("td", { className: "name" }, c.term),
                    h("td", null, c.estimate.toFixed(5)),
                    h("td", null, `[${c.ci_low.toFixed(5)}, ${c.ci_high.toFixed(5)}]`),
                    h("td", null, sig(c.p)))))))),

          // The alternatives are context for the featured model, not peers of
          // it — collapsed, so the page has one subject.
          h("details", { className: "step", style: { marginTop: 8 } },
            h("summary", null,
              h("span", { className: "name" }, "Compare against the other fits"),
              h("span", { className: "grow muted" },
                d.parsimonious ? `recommended: ${d.parsimonious}` : ""),
              d.parsimonious && d.parsimonious !== featured
                && h("span", { className: "tag warn" }, "another model is recommended")),
            h("div", { style: { padding: "12px 14px" } },
              h("table", { className: "data" },
                h("thead", null, h("tr", null,
                  ["Specification", "Terms",
                   d.model_type === "logistic" ? "Pseudo R²" : "R²",
                   "Adj R²", "AIC", "Top term p", ""].map((x) =>
                    h("th", { key: x, className: x === "Specification" ? "name" : "" }, x)))),
                h("tbody", null, d.results.map((r) =>
                  h("tr", { key: r.label, className: r.label === featured ? "" : "dim" },
                    h("td", { className: "name" }, r.label),
                    h("td", null, r.terms ? r.terms.length : "—"),
                    h("td", null, r.r_squared != null ? r.r_squared.toFixed(4) : "—"),
                    h("td", null, r.adj_r_squared != null ? r.adj_r_squared.toFixed(4) : "—"),
                    h("td", null, r.aic != null ? r.aic.toFixed(1) : "—"),
                    h("td", { className: r.all_terms_significant ? "" : "dim" },
                      r.highest_term_p != null ? sig(r.highest_term_p) : "—"),
                    h("td", null,
                      r.label === d.parsimonious
                        ? h("span", { className: "tag ok" }, "recommended")
                        : r.label === d.best_by_aic
                          ? h("span", { className: "tag warn" }, "best AIC only")
                          : r.label === featured
                            ? h("span", { className: "tag accent" }, "shown above") : ""))))),
              d.parsimonious && d.best_by_aic && d.parsimonious !== d.best_by_aic
                && h("p", { style: { fontSize: 12 } },
                  h("b", null, `AIC prefers “${d.best_by_aic}”, but “${d.parsimonious}” `
                    + "is recommended. "),
                  "The richer model's own terms are not distinguishable from zero — "
                  + "powers of one variable are collinear, so the joint fit improves "
                  + "while the coefficients become unidentifiable."),
              d.aic_caveat && h("p", { className: "muted", style: { fontSize: 12 } },
                d.aic_caveat))));
      })(),

      fitSpec && h("section", { className: "block" },
        h("h3", null, `How well it fits — ${featured}`),
        h("div", { className: "grid two" },
          h("figure", { className: "figure" },
            h("figcaption", null, "Actual against predicted. Points on the dashed "
              + "line are exactly right; a tight band means a good fit."),
            h(Chart, { spec: fitSpec, rows: dg.points, width: "container" }),
            h("div", { className: "srcline" },
              h("span", null, `R² ${d.results.find((r) => r.label === featured)
                ?.r_squared.toFixed(3)} — the share of variation explained`))),
          h("figure", { className: "figure" },
            h("figcaption", null, "Residuals against predicted. A model with the "
              + "right shape leaves no pattern here."),
            h(Chart, { spec: residSpec2, rows: dg.points, width: "container" }),
            h("div", { className: "srcline" },
              h("span", { className: "tag " + (dg.curvature ? "bad" : "ok") },
                dg.curvature ? `curved (${dg.curvature_swing_sd} SD swing)`
                             : "no curvature"),
              h("span", { className: "tag " + (dg.fanning ? "bad" : "ok") },
                dg.fanning ? `spread grows ${dg.fanning_ratio}×`
                           : "even spread")))),
        h("div", { className: "panel" },
          h("table", { className: "data" },
            h("thead", null, h("tr", null,
              [["Fifth of the data", "name"], ["Mean predicted", ""],
               ["Mean error", ""], ["Spread of error", ""]]
                .map(([x, c]) => h("th", { key: x, className: c }, x)))),
            h("tbody", null, dg.quintiles.map((q) =>
              h("tr", { key: q.quintile },
                h("td", { className: "name" }, `${q.quintile} of 5`),
                h("td", null, q.mean_fitted.toFixed(3)),
                h("td", null, q.mean_resid.toFixed(3)),
                h("td", null, q.sd_resid.toFixed(3))))))),
        h("p", { className: "muted", style: { fontSize: 12 } },
          "Mean error near zero in every fifth means the model is not "
          + "systematically wrong anywhere along its range; spread growing from "
          + "left to right means it is less reliable for larger predictions.")),

      // The hand-off to the agent used to be a button here. It was removed on
      // the understanding that Claude Desktop reached the same tools over MCP
      // -- but NO MCP SERVER EXISTS: none in this repo, the `mcp` package is
      // not installed, and the Desktop config has no servers. So investigation
      // currently has no path at all, and this page says so rather than
      // directing the reader somewhere that cannot help them.
      h("section", { className: "block no-print" },
        h("h3", null, "This is a fit, not a finding"),
        h("div", { className: "panel" },
          h("p", { style: { marginTop: 0 } },
            "Everything above is ordinary least squares over the snapshot — no "
            + "model is consulted and nothing here is tested."),
          h("p", { className: "err", style: { margin: 0 } },
            "There is currently no way to have a relationship investigated "
            + "against a held-out era. The button here was removed on the "
            + "understanding that Claude Desktop covered it over MCP; no MCP "
            + "server has been built, so that path does not exist yet."))))));
}
