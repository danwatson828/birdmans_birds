// Draft board — a reader for the CSVs draft_board.py produces.
//
// Each category shows the player's actual per-game value beside its z-score: the
// z-score says where he ranks, the raw number says what he does, and neither is
// readable alone. The window selector re-reads a board built by the same script
// over fewer seasons with its own weights renormalised — a different view of
// what already happened, NOT a forecast of next season.
import { h, useState, useEffect, api, fmt, pct, Spinner } from "/static/lib.js";

const RATE = new Set(["fg_pct", "ft_pct"]);

export function Fantasy() {
  const [window_, setWindow] = useState("blend");
  const [d, setD] = useState(null);
  const [q, setQ] = useState("");
  const [showZ, setShowZ] = useState(true);
  useEffect(() => { setD(null);
    api(`/api/fantasy/draft-board?window=${window_}&limit=400`)
      .then(setD).catch((e) => setD({ error: String(e) }));
  }, [window_]);

  if (!d) return h(Spinner, { label: "loading board…" });
  if (d.error) return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "Fantasy"), h("h2", null, "Draft board")),
    h("div", { className: "panel bad" }, h("p", { style: { margin: 0 } },
      "No board for this window. Generate it with ",
      h("code", null, `python draft_board.py --window ${window_}`), ".")));

  const rows = d.rows.filter((r) => !q.trim()
    || String(r.name || "").toLowerCase().includes(q.trim().toLowerCase()));

  return h("div", null,
    h("header", { className: "page-head" },
      h("div", { className: "eyebrow" }, "Fantasy · nine-category"),
      h("h2", null, "Draft board"),
      h("p", { className: "lede" },
        "Ranked by total value — per-game value discounted by availability. "
        + "Each category shows the actual per-game number beside its z-score, "
        + "with turnovers inverted and percentages weighted by volume."),
      h("div", { className: "meta-row" },
        h("span", null, h("b", null, fmt(d.total)), " ranked players"),
        h("span", null, h("b", null, "Window "), d.window_label),
        h("span", null, h("b", null, "Built "), new Date(d.generated).toLocaleString()))),

    // The projected window is the one view on this page that forecasts, so it
    // gets the opposite banner rather than the same one with a caveat bolted
    // on. Showing "descriptive, not predictive" above a projection would be
    // straightforwardly false.
    d.is_projection
      ? h("div", { className: "panel accent", style: { marginBottom: 18 } },
          h("p", { style: { margin: 0 } },
            h("b", null, "This one IS a projection. "), d.projection_note))
      : h("div", { className: "panel warn", style: { marginBottom: 18 } },
          h("p", { style: { margin: 0 } },
            h("b", null, "Descriptive, not predictive. "), d.not_a_projection,
            " Changing the window changes which completed seasons are counted and how "
            + "they are weighted — it is not a projection of next season.")),

    h("div", { className: "controls no-print" },
      h("label", { className: "field" }, "Seasons counted",
        h("select", { value: window_, onChange: (e) => setWindow(e.target.value) },
          d.windows.map((w) => h("option", { key: w.key, value: w.key,
            disabled: !w.available }, w.label)))),
      h("label", { className: "field" }, "Show",
        h("select", { value: showZ ? "both" : "raw",
            onChange: (e) => setShowZ(e.target.value === "both") },
          h("option", { value: "both" }, "value and z-score"),
          h("option", { value: "raw" }, "value only"))),
      h("input", { type: "search", value: q, placeholder: "Find a player…",
        onChange: (e) => setQ(e.target.value) }),
      h("span", { className: "muted" }, `${rows.length} shown`)),

    h("div", { className: "panel scroll-y scroll-x" },
      h("table", { className: "data" },
        h("thead", null, h("tr", null,
          h("th", null, "#"), h("th", { className: "name" }, "Player"),
          h("th", null, "Tm"), h("th", null, "Pos"),
          h("th", null, "Total"), h("th", null, "Per game"), h("th", null, "Avail"),
          d.pairs.map((p) => h("th", { key: p.z }, p.label)))),
        h("tbody", null, rows.map((r) =>
          h("tr", { key: r.player_id ?? r.name },
            h("td", null, r.board_rank),
            h("td", { className: "name" }, r.name, r.kept
              ? h("span", { className: "tag", style: { marginLeft: 6 } }, "keeper") : null),
            h("td", null, r.pro_team ?? "—"),
            h("td", null, r.pos_str ?? "—"),
            // board_rank orders by total_value (per-game value discounted by
            // availability), so showing per-game value alone made the ranking
            // look wrong -- rank 5 appeared to out-score rank 4.
            h("td", null, h("b", null, fmt(r.total_value, 2))),
            h("td", { className: "faint" }, fmt(r.per_game_value, 2)),
            h("td", { className: "faint" }, fmt(r.availability, 2)),
            // z under the value, not beside it: side by side the two numbers
            // read as one run-on figure and the column stopped scanning.
            d.pairs.map((p) => h("td", { key: p.z, className: "stacked" },
              h("span", { className: "val" },
                RATE.has(p.stat) ? pct(r[p.stat]) : fmt(r[p.stat], 1)),
              showZ && r[p.z] != null && h("span", { className: "z" },
                (r[p.z] >= 0 ? "+" : "") + r[p.z].toFixed(2)))),
            )))))
  );
}
