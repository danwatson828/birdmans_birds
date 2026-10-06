// The one paid control in the app, in one place.
//
// Every commentary button is this component. It is deliberately not four
// near-copies: the thing that must never drift is the honesty of the
// surrounding text -- that this is the only action costing money, that the
// brief is local, and that a failure is reported rather than papered over.
// Four copies would be four chances for one of those to be dropped.
import { h, useState, post } from "/static/lib.js";

export function NarrateButton({ kind, label, body = {}, disabled = false,
                               hint = null }) {
  const [state, setState] = useState("idle");
  const [d, setD] = useState(null);
  const run = () => {
    setState("running");
    post(`/api/narrate/${kind}`, body)
      .then((r) => { setD(r); setState("done"); })
      .catch((e) => { setD({ unavailable: String(e) }); setState("done"); });
  };
  return h("div", { className: "narrate no-print" },
    state === "idle" && h("div", null,
      h("p", { className: "faint", style: { margin: "0 0 8px", fontSize: 12 } },
        hint || "Everything above is computed locally and costs nothing. "
              + "This is the only action here that calls the model, so it "
              + "happens only when you ask."),
      h("button", { className: "btn", onClick: run, disabled },
        label || "Get a take")),
    state === "running" && h("p", { style: { margin: 0 } },
      h("span", { className: "spin" }), " Asking the model…"),
    state === "done" && d && h("div", null,
      d.narration
        ? h("div", { className: "prose", style: { marginTop: 0 } }, d.narration)
        : h("p", { className: "err", style: { marginTop: 0 } },
            d.unavailable || "No narration returned."),
      d.brief && h("details", { className: "step" },
        h("summary", null,
          `The brief it was given (${d.brief_chars} characters, computed locally)`),
        h("div", { className: "prose", style: { whiteSpace: "pre-wrap" } },
          d.brief)),
      h("button", { className: "btn-ghost", onClick: run,
                    style: { marginTop: 10 } }, "Ask again")));
}
