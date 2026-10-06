// Knowledge base — saved analyses, each carrying the notes attached to it.
//
// Notes are annotations on an analysis, never standalone documents. A free
// note pool meant two stores searched separately that could drift apart, and an
// observation with nothing to trace it back to is exactly the kind of unanchored
// claim this project spends its effort avoiding.
import { h, useState, useEffect, useCallback, api, post, Spinner } from "/static/lib.js";
import { Findings } from "/static/findings.js";

const KINDS = ["player", "team", "stat", "era", "free"];

function NoteForm({ runId, onSaved }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [tags, setTags] = useState("");
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState(null);
  const save = async (e) => {
    e.preventDefault();
    if (!title.trim()) return;
    setBusy(true); setErr(null);
    try {
      await post("/api/kb/notes", { title: title.trim(), body, run_id: runId,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean) });
      setTitle(""); setBody(""); setTags(""); onSaved();
    } catch (e2) { setErr(String(e2)); }
    finally { setBusy(false); }
  };
  return h("form", { className: "no-print", onSubmit: save,
      style: { marginTop: 10, paddingTop: 10, borderTop: "1px solid var(--rule-2)" } },
    h("div", { className: "controls", style: { marginBottom: 8 } },
      h("input", { type: "search", value: title, placeholder: "Note title",
        style: { minWidth: 260 }, onChange: (e) => setTitle(e.target.value) }),
      h("input", { type: "search", value: tags, style: { minWidth: 240 },
        placeholder: "tags: player:jokic, stat:ts_pct",
        onChange: (e) => setTags(e.target.value) }),
      h("button", { className: "btn-ghost", type: "submit", disabled: busy || !title.trim() },
        busy ? "saving…" : "Attach note")),
    h("textarea", { value: body, rows: 2, style: { width: "100%" },
      placeholder: "What you concluded, and why it matters later.",
      onChange: (e) => setBody(e.target.value) }),
    err && h("p", { className: "err" }, err));
}

export function KnowledgeBase({ onOpenAnalysis, initialTab }) {
  const [q, setQ] = useState("");
  const [tag, setTag] = useState("");
  const [res, setRes] = useState(null);
  const [tags, setTags] = useState([]);
  const [adding, setAdding] = useState(null);
  const [open, setOpen] = useState(null);
  // Validated findings is a TAB rather than its own rail destination: it is a
  // filter over this same store (the investigate runs that declared a held-out
  // era and committed to a verdict), and a separate route implied a second
  // archive you had to search twice. ?v=findings still lands here.
  const [tab, setTab] = useState(initialTab === "findings" ? "findings" : "all");

  const load = useCallback(async () => {
    const p = new URLSearchParams();
    if (q.trim()) p.set("q", q.trim());
    if (tag) p.set("tag", tag);
    try {
      const [r, t] = await Promise.all([api(`/api/kb/search?${p}`), api("/api/kb/tags")]);
      setRes(r); setTags(t.tags);
    } catch { setRes(false); }
  }, [q, tag]);
  useEffect(() => { const t = setTimeout(load, 200); return () => clearTimeout(t); }, [load]);

  const del = async (id) => { await fetch(`/api/kb/notes/${id}`, { method: "DELETE" }); load(); };

  // A card grid, not a stack. Every analysis used to render its full snippet,
  // every tag and every attached note inline, so the page was 8196px tall at
  // 1340px and finding anything meant scrolling past everything. A card states
  // the question and the takeaway; opening one expands its notes in place.
  const card = (a) => h("div", { key: a.id,
      className: "kcard" + (open === a.id ? " open" : "") },
    h("button", { className: "q", style: { all: "unset", cursor: "pointer" },
        onClick: () => onOpenAnalysis(a.id) }, a.question),
    a.snippet && h("span", { className: "a" },
      a.snippet.replace(/[#*`]/g, "").slice(0, 190)),
    h("span", { className: "foot" },
      h("span", null, "#" + a.id),
      a.tags.slice(0, 3).map((t) => h("button", { key: t, className: "chip ghost",
        onClick: () => setTag(t) }, t)),
      a.notes.length > 0 && h("span", { className: "chip" },
        `${a.notes.length} note${a.notes.length > 1 ? "s" : ""}`),
      h("span", { style: { marginLeft: "auto", display: "flex", gap: "var(--s2)" } },
        h("button", { className: "linkish no-print",
          onClick: () => setOpen(open === a.id ? null : a.id) },
          open === a.id ? "hide notes" : "notes"),
        h("button", { className: "linkish no-print",
          onClick: () => setAdding(adding === a.id ? null : a.id) },
          adding === a.id ? "cancel" : "+ note"))),
    open === a.id && a.notes.length > 0 && h("div", { className: "kc-notes" },
      a.notes.map((n) => h("div", { key: n.id, className: "kc-note" },
        h("div", { style: { display: "flex", gap: "var(--s2)", alignItems: "baseline" } },
          h("b", null, n.title),
          h("span", { className: "faint", style: { fontSize: 11 } }, n.created_at),
          h("button", { className: "linkish no-print",
            style: { marginLeft: "auto" }, onClick: () => del(n.id) }, "delete")),
        n.body && h("p", { style: { margin: "4px 0 0", whiteSpace: "pre-wrap" } }, n.body)))),
    adding === a.id && h(NoteForm, { runId: a.id,
      onSaved: () => { setAdding(null); load(); } }));

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "lhs" },
        h("div", { className: "eyebrow" }, "Analysis"),
        h("h1", null, "Knowledge base"),
        h("p", { className: "lede" },
          "Exact search over stored text and tags, not similarity \u2014 every hit "
          + "can be explained by the field it matched.")),
      res && res.total != null && h("div", { className: "meta" },
        h("span", null, h("b", null, res.total), "analyses"),
        h("span", null, h("b", null, res.note_count), "notes"))),

    h("div", { className: "tabs", role: "tablist" },
      h("button", { role: "tab", "aria-selected": tab === "all",
        onClick: () => setTab("all") }, "All analyses"),
      h("button", { role: "tab", "aria-selected": tab === "findings",
        onClick: () => setTab("findings") }, "Validated findings")),

    tab === "findings" ? h(Findings, null) : h("div", null,
      h("div", { className: "toolbar" },
        h("div", { className: "grp", style: { flex: "1 1 240px" } },
          h("input", { type: "search", value: q, style: { width: "100%" },
            placeholder: "Search analyses and notes\u2026",
            onChange: (e) => setQ(e.target.value) })),
        h("div", { className: "grp" },
          h("label", { htmlFor: "kb-tag" }, "Tag"),
          h("select", { id: "kb-tag", value: tag,
              onChange: (e) => setTag(e.target.value) },
            h("option", { value: "" }, "\u2014 any \u2014"),
            KINDS.map((k) => {
              const inKind = tags.filter((t) => t.kind === k);
              return inKind.length ? h("optgroup", { key: k, label: k },
                inKind.map((t) => h("option", { key: t.kind + t.value,
                  value: `${t.kind}:${t.value}` }, `${t.value} (${t.uses})`))) : null;
            }))),
        (q || tag) && h("button", { className: "btn-ghost",
          onClick: () => { setQ(""); setTag(""); } }, "Clear"),
        h("div", { className: "spacer" }),
        res && res.analyses && h("div", { className: "count" },
          `${res.analyses.length} shown`)),

      !res ? h(Spinner, null)
      : res === false ? h("p", { className: "err" }, "search failed")
      : res.analyses.length === 0 ? h("p", { className: "empty" }, "Nothing matches.")
      : h("div", { className: "cardgrid" }, res.analyses.map(card))));
}
