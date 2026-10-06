// Shared helpers.
export const { createElement: h, useState, useEffect, useRef, useCallback, useMemo } = React;

export const api = async (path, opts) => {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${(await r.text()).slice(0, 300)}`);
  return r.json();
};
export const post = (path, body) => api(path, {
  method: "POST",
  headers: body ? { "Content-Type": "application/json" } : {},
  body: body ? JSON.stringify(body) : undefined,
});

export const fmt = (v, digits = 1) =>
  v === null || v === undefined || v === "" ? "—"
    : typeof v === "number" ? (Number.isInteger(v) ? v.toLocaleString()
        : v.toLocaleString(undefined, { minimumFractionDigits: digits,
                                        maximumFractionDigits: digits }))
    : String(v);
export const pct = (v, d = 1) => v == null ? "—" : (v * 100).toFixed(d) + "%";
// A season is a year, not a quantity: 1946, never "1,946".
export const year = (v) => v == null || v === "" ? "—" : String(v);

// The model writes markdown. Rendered, not dumped as preformatted text -- its
// tables and emphasis are how the finding is structured.
export const md = (text) => ({
  __html: window.marked
    ? marked.parse(String(text || ""), { gfm: true, breaks: false })
    : String(text || "").replace(/[<>&]/g, (c) =>
        ({ "<": "&lt;", ">": "&gt;", "&": "&amp;" }[c])),
});

export function Spinner({ label }) {
  return h("p", { className: "muted" }, h("span", { className: "spin" }), " " + (label || "loading…"));
}

// Vega-Lite specs are stored without data by design (a chart references its saved
// query, never a stale copy of the rows), so rows are attached at render time.
const COMPOSED = ["facet", "hconcat", "vconcat", "concat", "repeat"];
export function Chart({ spec, rows, width = 680 }) {
  const box = useRef(null);
  useEffect(() => {
    if (!box.current || !spec) return;
    let dead = false;
    // "container" makes Vega measure the parent instead of being told a pixel
    // width. A fixed width is the PLOT area only -- axis titles and labels are
    // added outside it -- so a 400px chart in a 400px grid column overflowed
    // its frame by however wide its y-axis happened to be.
    const sized = COMPOSED.some((k) => k in spec) ? spec
      : width === "container"
        ? { ...spec, width: "container", height: spec.height ?? 300,
            autosize: { type: "fit", contains: "padding", resize: true } }
        : ("width" in spec) ? spec
        : { ...spec, width, height: spec.height ?? 320 };
    vegaEmbed(box.current, { ...sized, data: { values: rows || [] } },
              { actions: false, renderer: "svg" })
      .catch((e) => { if (!dead && box.current) box.current.textContent = "chart failed: " + e; });
    return () => { dead = true; };
  }, [spec, rows, width]);
  return h("div", { ref: box, className: "chart-box" });
}


// Printing a <details> that is closed on screen produces a page with the
// content missing: a collapsed <details> stays collapsed in print no matter what
// `display` its children are given, because the UA hides them through the
// element's own slot rather than through a rule a stylesheet can override.
// So the stored SQL -- which is evidence, and must be in the PDF -- is opened
// before the print and restored afterwards. Verifying the actual PDF is what
// caught this; the button never errored.
let _printRestore = [];
export function installPrintHandlers() {
  if (window.__printHandlersInstalled) return;
  window.__printHandlersInstalled = true;
  window.addEventListener("beforeprint", () => {
    _printRestore = [];
    document.querySelectorAll("details").forEach((d) => {
      _printRestore.push([d, d.open]);
      d.open = true;
    });
  });
  window.addEventListener("afterprint", () => {
    _printRestore.forEach(([d, was]) => { d.open = was; });
    _printRestore = [];
  });
}


// One season control, used by every page that needs a season. Previously each
// page rolled its own: Leaderboards had a free-text number spinner you could
// type 1823 into, the team page had a bare dropdown, and the model builder had
// two unlabelled boxes. Same component now, same behaviour, backed by the
// seasons that actually exist.
export function useSeasons(gameType = "regular") {
  const [seasons, setSeasons] = useState(null);
  useEffect(() => {
    api(`/api/explore/seasons?game_type=${gameType}`)
      .then((d) => setSeasons(d)).catch(() => setSeasons({ seasons: [], latest: null }));
  }, [gameType]);
  return seasons;
}

export function SeasonPicker({ seasons, mode, setMode, season, setSeason,
                               from, setFrom, to, setTo, allowRange = true }) {
  const list = (seasons && seasons.seasons) || [];
  const opt = (s) => h("option", { key: s.season, value: s.season }, s.label);
  if (!list.length) return h("span", { className: "muted" }, "seasons…");
  return h(React.Fragment, null,
    allowRange && h("label", { className: "field" }, "Span",
      h("select", { value: mode, onChange: (e) => setMode(e.target.value) },
        h("option", { value: "single" }, "single season"),
        h("option", { value: "range" }, "season range"))),
    mode === "range" && allowRange
      ? h("label", { className: "field" }, "Seasons",
          h("select", { value: from, onChange: (e) => setFrom(Number(e.target.value)) },
            list.map(opt)),
          h("span", { "aria-hidden": "true" }, "–"),
          h("select", { value: to, onChange: (e) => setTo(Number(e.target.value)) },
            list.map(opt)))
      : h("label", { className: "field" }, "Season",
          h("select", { value: season ?? "", onChange: (e) => setSeason(Number(e.target.value)) },
            list.map(opt))));
}

// ---------------------------------------------------------------------------
// Team branding.
//
// Colours and logos are NOT in the snapshot -- /api/brand says so with a
// `curated` flag -- so they are fetched once and shared, rather than each page
// deciding independently what a franchise looks like. A component that asks
// for an unknown team gets the neutral theme, never a crash and never a
// borrowed identity for a defunct franchise.
// ---------------------------------------------------------------------------
let _brandCache = null;
let _brandPromise = null;

export function useBrands() {
  const [brands, setBrands] = useState(_brandCache);
  useEffect(() => {
    if (_brandCache) return;
    _brandPromise = _brandPromise || api("/api/brand").catch(() => null);
    let live = true;
    _brandPromise.then((d) => {
      if (d) _brandCache = d;
      if (live && d) setBrands(d);
    });
    return () => { live = false; };
  }, []);
  return brands;
}

/* ----------------------------------------------------------------- tokens --
   ONE source of colour. Chart specs and inline styles used to carry their own
   hex literals -- 29 of them across five modules, several of which were stale
   copies of a token value, which is exactly how a palette drifts apart. These
   read the live custom properties off :root instead, so app.css stays the only
   place a colour is decided and a retune moves the charts with the page.

   Values are resolved once and cached: getComputedStyle forces style
   resolution, and calling it per mark inside a Vega spec is a real cost on a
   long leaderboard. */
const _tokenCache = new Map();
export function token(name, fallback = "#000") {
  if (_tokenCache.has(name)) return _tokenCache.get(name);
  const v = getComputedStyle(document.documentElement)
    .getPropertyValue(name).trim() || fallback;
  _tokenCache.set(name, v);
  return v;
}
/** The categorical chart ramp, --c1..--c10, in order. */
export const series = (n = 10) =>
  Array.from({ length: n }, (_, i) => token(`--c${i + 1}`));

export const brandOf = (brands, teamId) =>
  (brands && brands.teams && brands.teams[String(teamId)]) ||
  (brands && brands.neutral) ||
  { primary: token("--n8"), accent: token("--n6"), ink: token("--n0"),
    logo: null, branded: false };

/** Inline style object that drives every `var(--team*)` rule in the stylesheet. */
/* The franchise colour darkened until WHITE text on it clears 4.5:1.
 *
 * This MUST be computed per element in JS. The obvious CSS version --
 * `--team-safe: var(--team)` on :root -- looks right and is silently wrong: a
 * custom property's value is substituted where it is DECLARED, so --team-safe
 * computed once at :root against the root --team and every descendant then
 * inherited that one neutral value. The Portland correction never applied.
 *
 * Multiplicative lightness steps, not subtractive: that is the rule that was
 * derived and signed off, and it reproduces #de3034 for Portland exactly. It
 * is a no-op for 29 of the 30 franchises.
 */
const _safeCache = new Map();
export function textSafe(hex) {
  if (!hex || hex[0] !== "#" || hex.length !== 7) return hex;
  if (_safeCache.has(hex)) return _safeCache.get(hex);
  const n = parseInt(hex.slice(1), 16);
  let [r, g, b] = [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  const lum = (R, G, B) => {
    const f = (c) => { c /= 255;
      return c <= 0.03928 ? c / 12.92 : Math.pow((c + 0.055) / 1.055, 2.4); };
    return 0.2126 * f(R) + 0.7152 * f(G) + 0.0722 * f(B);
  };
  const ratio = (R, G, B) => 1.05 / (lum(R, G, B) + 0.05);   // white on it
  // rgb -> hsl -> scale L -> rgb
  const mx = Math.max(r, g, b) / 255, mn = Math.min(r, g, b) / 255;
  const l0 = (mx + mn) / 2, dl = mx - mn;
  const sat = dl === 0 ? 0 : dl / (1 - Math.abs(2 * l0 - 1));
  let hue = 0;
  if (dl !== 0) {
    const R = r / 255, G = g / 255, B = b / 255;
    hue = mx === R ? ((G - B) / dl) % 6 : mx === G ? (B - R) / dl + 2 : (R - G) / dl + 4;
    hue *= 60; if (hue < 0) hue += 360;
  }
  const toRgb = (L) => {
    const c = (1 - Math.abs(2 * L - 1)) * sat, x = c * (1 - Math.abs((hue / 60) % 2 - 1));
    const m = L - c / 2;
    const [rr, gg, bb] = hue < 60 ? [c, x, 0] : hue < 120 ? [x, c, 0]
      : hue < 180 ? [0, c, x] : hue < 240 ? [0, x, c]
      : hue < 300 ? [x, 0, c] : [c, 0, x];
    return [Math.round((rr + m) * 255), Math.round((gg + m) * 255), Math.round((bb + m) * 255)];
  };
  let out = hex;
  for (let f = 1.0; f > 0.3; f -= 0.02) {
    const [R, G, B] = toRgb(Math.max(0, l0 * f));
    if (ratio(R, G, B) >= 4.5) {
      out = "#" + [R, G, B].map((v) => v.toString(16).padStart(2, "0")).join("");
      break;
    }
  }
  _safeCache.set(hex, out);
  return out;
}

/** Inline style object that drives every `var(--team*)` rule in the stylesheet. */
export const teamStyle = (b) => b ? {
  "--team": b.primary, "--team-accent": b.accent, "--team-ink": b.ink,
  "--team-safe": textSafe(b.primary),
} : {};

/** A crest, or nothing at all -- never a placeholder box standing in for one. */
export function Crest({ brand, size = "sm", alt }) {
  if (!brand || !brand.logo) return null;
  return h("img", { className: "crest-" + size, src: brand.logo, loading: "lazy",
                    alt: alt || (brand.abbrev ? brand.abbrev + " logo" : "") });
}

/** W/L as a coloured chip: the one place the result of a game is encoded. */
export const WL = ({ won }) =>
  h("span", { className: "wl " + (won ? "w" : "l") }, won ? "W" : "L");

/** Bucket a z-score onto the five-step diverging ramp. */
export const zClass = (z) =>
  z == null ? "" :
  z >= 2 ? "z p3" : z >= 1 ? "z p2" : z > 0.25 ? "z p1" :
  z <= -2 ? "z n3" : z <= -1 ? "z n2" : z < -0.25 ? "z n1" : "";

export const record = (w, l) => (w == null || l == null) ? "—" : `${w}–${l}`;
