// The Ask view: a saved analysis rendered as a report, not as a terminal dump.
//
// Structure comes from the stored record (methodology, answer, charts, the tool
// calls verbatim) rather than from parsing render_report()'s text. The plain-text
// report is still exactly what the API serves and what `analyses.py show` prints
// -- it is available under "Verbatim report" -- but the reading view is built
// from the same fields that produced it.
import { h, useState, useEffect, useCallback, api, post, md, Chart, Spinner, fmt } from "/static/lib.js";

const CAUSE = {
  row_order: "ORDER BY is not a total order",
  row_content: "the rows themselves changed",
  unordered_aggregate: "order-sensitive aggregate with no internal ORDER BY",
  analysis_code: "nondeterminism in the Python, not the query",
  feeder_sql: "its query is unstable and that reaches the result",
  execution_failed: "a later execution raised",
  verification_timed_out: "not verified — the check itself timed out",
};
// The answer's own "FINDINGS" heading duplicates the section label above it.
// The section structure is the page's job; the model's heading is dropped only
// when it is that exact label, so a real heading is never swallowed.
const dropLeadHeading = (text) =>
  String(text || "").replace(/^\s*(?:#{1,3}\s*)?\**\s*FINDINGS\s*\**\s*\n+/i, "");

const REPRO = {
  "reproducible": ["ok", "reproducible"],
  "has unstable steps": ["bad", "unstable"],
  "unknown (predates the check)": ["warn", "unverified"],
};

function Tag({ kind, children }) { return h("span", { className: "tag " + (kind || "") }, children); }

function StepRow({ c }) {
  const verdict = c.stability
    ? h(Tag, { kind: c.stability === "stable" ? "ok" : "bad" },
        c.stability === "stable" ? "reproducible"
          : (CAUSE[c.cause] || c.stability))
    : null;
  const body = c.executed_sql || c.executed_code;
  return h("details", { className: "step" },
    h("summary", null,
      h("span", { className: "seq" }, `[${c.seq}]`),
      h("span", { className: "name" }, c.tool),
      h("span", { className: "grow muted" },
        c.error ? h("span", { className: "err" }, String(c.error).slice(0, 90))
                : `${fmt(c.rows_returned)} rows · ${fmt(c.seconds, 2)}s`),
      verdict),
    body && h("pre", null, body));
}

function Figure({ chart }) {
  return h("figure", { className: "figure" },
    h("figcaption", null, chart.title || "Chart"),
    chart.error
      ? h("p", { className: "err" }, "no data: " + chart.error)
      : h(Chart, { spec: chart.spec, rows: chart.rows }),
    h("div", { className: "srcline" },
      h("span", null, `${(chart.rows || []).length} rows, from the saved query`),
      chart.stability && h("span", null,
        `query ${chart.stability}${chart.cause ? " · " + (CAUSE[chart.cause] || chart.cause) : ""}`)));
}

export function Report({ id, onRefresh }) {
  const [rec, setRec] = useState(null);
  const [rerun, setRerun] = useState(null);
  const [busy, setBusy] = useState(false);
  const [raw, setRaw] = useState(false);
  const [err, setErr] = useState(null);

  useEffect(() => {
    setRec(null); setRerun(null); setErr(null); setRaw(false);
    if (id == null) return;
    api(`/api/analyses/${id}`).then(setRec).catch((e) => setErr(String(e)));
  }, [id]);

  const doRerun = useCallback(async () => {
    setBusy(true); setRerun(null);
    try { setRerun(await post(`/api/analyses/${id}/rerun`)); }
    catch (e) { setErr(String(e)); }
    finally { setBusy(false); }
  }, [id]);

  if (id == null) return h("p", { className: "empty" },
    "Ask a question, or open a saved analysis from the list.");
  if (err) return h("p", { className: "err" }, err);
  if (!rec) return h(Spinner, { label: "loading analysis…" });

  const r = rec.run;
  const skills = (() => { try { return JSON.parse(r.skills_loaded || "[]"); } catch { return []; } })();
  const sqlSteps = rec.calls.filter((c) => c.executed_sql || c.executed_code);
  const unstable = rec.calls.filter((c) => c.stability === "unstable").length;

  // A report is a DOCUMENT: prose at a readable measure, with its provenance
  // and its actions parked in a rail beside it instead of stacked on top. The
  // old page put a metadata row, then three buttons, then the methodology,
  // then the findings all down one 1180px column, so the answer began about
  // 400px below the question.
  return h("article", { className: "split wide" },
    h("div", { className: "side no-print" },
      h("div", { className: "panel" },
        h("div", { className: "div-head" }, "This run"),
        h("div", { className: "runmeta" },
          h("div", null, h("span", null, "Model"), h("b", null, r.model)),
          h("div", null, h("span", null, "Turns"), h("b", null, r.turns)),
          h("div", null, h("span", null, "Cost"),
            h("b", null, "$" + Number(r.cost_usd || 0).toFixed(4))),
          h("div", null, h("span", null, "Ran"),
            h("b", null, new Date(r.started_at).toLocaleDateString())),
          h("div", null, h("span", null, "Snapshot"),
            h("b", { title: r.snapshot_sha256 }, r.snapshot_sha256.slice(0, 12) + "\u2026"))),
        h("div", { style: { display: "flex", gap: "var(--s2)", flexWrap: "wrap",
                            marginTop: "var(--s3)" } },
          unstable > 0
            ? h("span", { className: "chip bad" },
                `${unstable} unstable step${unstable > 1 ? "s" : ""}`)
            : h("span", { className: "chip ok" }, "reproducible"),
          skills.map((sk) => h("span", { key: sk, className: "chip ghost" }, sk)))),
      h("div", { className: "panel" },
        h("div", { className: "div-head" }, "Actions"),
        h("div", { className: "stackbtn" },
          h("button", { className: "btn", onClick: () => window.print() },
            "Download PDF"),
          h("button", { className: "btn-ghost", onClick: doRerun, disabled: busy },
            busy ? "rerunning\u2026" : "Rerun on current snapshot"),
          h("button", { className: "btn-ghost", onClick: () => setRaw(!raw) },
            raw ? "Hide verbatim report" : "Verbatim report")))),

    h("div", { className: "work" },
    h("header", { className: "page-head" },
      h("div", { className: "lhs" },
        h("div", { className: "eyebrow" },
          `Analysis #${r.id}${r.label ? " \u00b7 " + r.label : ""}`),
        h("h1", null, r.question),
        // Screen keeps provenance in the rail; the rail is .no-print, so the
        // PDF would have lost the verdict and the snapshot entirely. This line
        // is print-only and carries exactly what the rail carries.
        h("div", { className: "print-only provenance" },
          h("span", null, `Analysis #${r.id}`),
          h("span", null, `Model ${r.model}`),
          h("span", null, `${r.turns} turns`),
          h("span", null, `$${Number(r.cost_usd || 0).toFixed(4)}`),
          h("span", null, new Date(r.started_at).toLocaleString()),
          h("span", null, `Snapshot ${r.snapshot_sha256.slice(0, 16)}\u2026`),
          h("span", null, unstable > 0
            ? `${unstable} unstable step${unstable > 1 ? "s" : ""}`
            : "reproducible"),
          skills.length > 0 && h("span", null, skills.join(", "))))),

    r.methodology && h("section", { className: "block" },
      h("h3", null, "Methodology, stated before the analysis ran"),
      h("div", { className: "methodology" },
        h("div", { className: "prose", dangerouslySetInnerHTML: md(r.methodology) }))),

    h("section", { className: "block" },
      h("h3", null, "Findings"),
      h("div", { className: "prose", dangerouslySetInnerHTML: md(dropLeadHeading(r.final_answer)) })),

    rec.charts.length > 0 && h("section", { className: "block" },
      h("h3", null, `Evidence — ${rec.charts.length} chart${rec.charts.length > 1 ? "s" : ""}`),
      rec.charts.map((c) => h(Figure, { key: c.id, chart: c }))),

    h("section", { className: "block" },
      h("h3", null, `Queries and code as executed — ${sqlSteps.length} step${sqlSteps.length > 1 ? "s" : ""}, verbatim`),
      sqlSteps.map((c) => h(StepRow, { key: c.seq, c }))),

    rerun && h("section", { className: "block" },
      h("h3", null, "Rerun against the current snapshot"),
      h("div", { className: "panel" },
        h("p", { className: "muted", style: { marginTop: 0 } },
          rerun.same_snapshot
            ? "Same snapshot as when this ran."
            : "SNAPSHOT HAS CHANGED since this analysis ran."),
        h("table", { className: "data" },
          h("thead", null, h("tr", null,
            ["Step", "Tool", "Result", "Detail"].map((x) => h("th", { key: x }, x)))),
          h("tbody", null, rerun.results.map((x) => h("tr", {
              key: x.seq, className: x.status === "identical" ? "" : "dim" },
            h("td", null, x.seq), h("td", { className: "name" }, x.tool),
            h("td", null, x.status === "identical"
              ? h(Tag, { kind: "ok" }, "identical")
              : h(Tag, { kind: "bad" }, x.status)),
            h("td", { style: { textAlign: "left", whiteSpace: "normal" } },
              x.detail || "")))))))
    ,
    raw && h("section", { className: "block" },
      h("h3", null, "Verbatim report (identical to `analyses.py show`)"),
      h("pre", { className: "step", style: { padding: "14px 16px", fontSize: "12px",
                                             whiteSpace: "pre-wrap", margin: 0 } }, rec.report))));
}

export function AskBar({ onDone }) {
  const [q, setQ] = useState("");
  const [job, setJob] = useState(null);
  const [err, setErr] = useState(null);

  useEffect(() => {
    if (!job || ["done", "error"].includes(job.status)) return;
    const t = setTimeout(async () => {
      try {
        const j = await api(`/api/jobs/${job.job_id}`);
        setJob({ ...j, job_id: job.job_id });
        if (j.status === "done") onDone(j.run_id);
      } catch (e) { setErr(String(e)); }
    }, 1500);
    return () => clearTimeout(t);
  }, [job, onDone]);

  const submit = async (e) => {
    e.preventDefault();
    if (!q.trim()) return;
    setErr(null);
    try { setJob(await post("/api/ask", { question: q.trim() })); setQ(""); }
    catch (e2) { setErr(String(e2)); }
  };
  const running = job && !["done", "error"].includes(job.status);

  // Proportions: the input is the page's subject, so it gets the room. The
  // button is sized to its label and says what it will do, rather than being a
  // large unexplained block beside a cramped box.
  return h("div", { className: "no-print" },
    // Trimmed: the input is the instruction. What the agent does with a
    // question belongs in the report it produces, not above the box.
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "Ask"),
      h("h2", null, "Put a question to the agent"),
      h("p", { className: "lede" },
        "It states its methodology before it computes, checks what era each "
        + "stat was actually recorded in, and saves the result as a report with "
        + "the exact SQL that ran.")),
    h("form", { className: "askbar", onSubmit: submit },
      h("textarea", { value: q, disabled: running, rows: 5,
        onChange: (e) => setQ(e.target.value),
        placeholder: "e.g. Did teams that shot more threes win more games in the 2010s?",
        onKeyDown: (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(e); } }),
      h("div", { className: "askbar-foot" },
        h("span", { className: "muted" },
          running ? "" : "⌘↩ to send · typically 20–200s · saved as a report"),
        h("button", { className: "btn", type: "submit", disabled: running || !q.trim() },
          running ? "Working…" : "Ask the agent"))),
    running && h("p", { className: "muted" }, h("span", { className: "spin" }),
      ` ${job.status} — stating methodology, querying, checking coverage.`),
    job && job.status === "error" && h("p", { className: "err" }, job.error),
    err && h("p", { className: "err" }, err));
}
