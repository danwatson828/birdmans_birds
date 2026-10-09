"""Design-system invariants: layout that cannot overflow, text that can be read.

Both checks here exist because the redesign audit found the same two failures
everywhere at once rather than in one component, which means a per-component
fix would rot. They are written against the RENDERED page and the PARSED
stylesheet, not against a list of selectors someone has to remember to update.

Both skip rather than fail when the machine cannot run them: the suite also
runs inside ingest.py's publish gate, where Chrome may not exist.
"""

from __future__ import annotations

import re
from pathlib import Path

from .harness import Skip, check

PROJECT_ROOT = Path(__file__).resolve().parent.parent
APP_CSS = PROJECT_ROOT / "web" / "app.css"

# Every page TYPE the shell can reach. The nav routes are parsed out of app.js
# so a new rail item cannot be added without this check covering it, and the
# drill-downs (a team, a player, a box score) are listed in the manifest
# because they are page types in their own right rather than modes of the page
# above them -- and they are where the widest tables live.
def _pages() -> list[tuple[str, str]]:
    from .routes_manifest import all_pages
    pages = all_pages()
    if len(pages) < 8:
        raise Skip(f"route manifest returned only {len(pages)} pages")
    return pages


# ---------------------------------------------------------------------------
# 1. Horizontal overflow
# ---------------------------------------------------------------------------
@check("no route overflows horizontally at 420px", group="design")
def no_horizontal_overflow_at_420(con_unused):
    """A phone-width viewport must not produce a sideways scrollbar.

    This was broken on EVERY route: `.shell` is a grid, and the 960px
    breakpoint set `grid-template-columns: 1fr`. A bare `1fr` means
    `minmax(auto, 1fr)`, and `auto` floors the track at its max-content width,
    so the wide tables inside pushed the column out to `.main`'s own
    `max-width: 1180px`. Measured at the time: viewport 420, scrollWidth 1180,
    i.e. 760px of unreachable page on every single route.

    Measured in a real browser rather than asserted from the CSS, because the
    failure is a layout outcome and only layout can report it.
    """
    from .cdp import Browser, Server, find_chrome
    chrome = find_chrome()
    if not chrome:
        raise Skip("no Chrome/Chromium found (set CHROME_BIN to run this)")
    try:
        import fastapi, uvicorn            # noqa: F401
    except Exception as exc:
        raise Skip(f"the web app's deps are unavailable: {exc}")

    pages = _pages()
    server = browser = None
    try:
        try:
            server = Server(PROJECT_ROOT)
        except Exception as exc:
            raise Skip(f"could not start the app: {exc}")
        browser = Browser(chrome)
        session = browser.new_page(420, 900)
        bad, seen = [], 0
        for label, query in pages:
            browser.goto(session, f"{server.base}/{query}", settle=3.0)
            m = browser.eval(session, """({
              vw: window.innerWidth,
              sw: document.documentElement.scrollWidth,
              worst: (() => {
                const lim = document.documentElement.clientWidth;
                let w = null, px = 0;
                const inScroll = (e) => {
                  for (let p = e.parentElement; p; p = p.parentElement) {
                    const o = getComputedStyle(p).overflowX;
                    if (o === 'auto' || o === 'scroll') return true;
                  }
                  return false;
                };
                for (const el of document.querySelectorAll('body *')) {
                  const r = el.getBoundingClientRect();
                  if (r.width > 0 && r.right - lim > px && !inScroll(el)) {
                    px = r.right - lim;
                    w = el.tagName.toLowerCase() + '.' +
                        String(el.className || '').split(' ').slice(0,2).join('.');
                  }
                }
                return w ? `${w} (+${Math.round(px)}px)` : null;
              })(),
            })""")
            seen += 1
            over = m["sw"] - m["vw"]
            if over > 1:                   # 1px for sub-pixel rounding
                bad.append(f"{label}: scrollWidth {m['sw']} vs innerWidth "
                           f"{m['vw']} (+{over}px); widest offender "
                           f"{m['worst'] or 'unknown'}")
        if bad:
            return False, "; ".join(bad[:4]) + (f"; +{len(bad)-4} more" if len(bad) > 4 else "")
        return True, (f"{seen} page types at 420px (13 routes + 3 drill-downs), "
                      f"every one with scrollWidth == innerWidth")
    finally:
        if browser:
            browser.stop()
        if server:
            server.stop()


# ---------------------------------------------------------------------------
# 1b. Table headers
# ---------------------------------------------------------------------------
@check("no table's first row hides under its header", group="design")
def table_headers_sit_above_their_first_row(con_unused):
    """A sticky offset is measured from the nearest SCROLLPORT. Get it wrong and
    the header is drawn inside its own box by the offset amount and paints over
    the first rows.

    This has now regressed twice, which is why it is checked on EVERY table on
    EVERY route rather than on a hand-picked three. The second time, call sites
    wrote `class="table-full scroll-x"`; `overflow-x: auto` forces `overflow-y`
    to compute to `auto`, so `.table-full` became a scrollport and the header's
    `top: var(--topbar)` landed 46px down the box -- eleven tables across five
    routes, every one with the first row underneath.

    Asserted at rest AND after scrolling, because a header can be correct at
    scrollTop 0 and still detach wrongly once it pins.
    """
    from .cdp import Browser, Server, find_chrome
    chrome = find_chrome()
    if not chrome:
        raise Skip("no Chrome/Chromium found (set CHROME_BIN to run this)")
    try:
        import fastapi, uvicorn            # noqa: F401
    except Exception as exc:
        raise Skip(f"the web app's deps are unavailable: {exc}")

    # Two different invariants, because the correct behaviour differs by state:
    #   at rest   -- the first row must sit BELOW the header (the bug);
    #   scrolled  -- the first row is SUPPOSED to pass under the header, so what
    #                matters is that the header stayed pinned at the top of its
    #                scrollport rather than being pushed down into the box.
    # Two different invariants, because correct behaviour differs by state:
    #   at rest  -- the first row must sit BELOW the header. This is the bug.
    #   scrolled -- the first row is SUPPOSED to pass under the header, so what
    #               matters is that the header held its declared offset from
    #               its scrollport rather than being pushed down into the box.
    #
    # The scrollport is the nearest ancestor whose computed overflow is not
    # `visible` -- NOT "the nearest ancestor that currently overflows". An
    # element with `overflow: auto` is a scrollport whether or not it has
    # anything to scroll, and sticky offsets resolve against it either way.
    # Getting that distinction wrong is what produced this bug in the first
    # place, and it made the first two versions of this check wrong too.
    PROBE = """(() => {
      const out = [];
      const portOf = (el) => {
        for (let p = el.parentElement; p; p = p.parentElement) {
          const cs = getComputedStyle(p);
          if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') return p;
        }
        return null;                       // the viewport scrolls it
      };
      document.querySelectorAll('table.data').forEach((t, i) => {
        const th = t.querySelector('thead th');
        const td = t.querySelector('tbody tr td');
        if (!th || !td) return;
        const port = portOf(t);
        const want = Math.round(parseFloat(getComputedStyle(th).top) || 0);
        const restGap = Math.round(th.getBoundingClientRect().bottom
                                   - td.getBoundingClientRect().top);

        // Only assert pinning where something can actually scroll under it.
        let pinnedAt = null;
        const canScrollBox = port && port.scrollHeight > port.clientHeight + 4;
        const canScrollPage = !port &&
          document.documentElement.scrollHeight > window.innerHeight + 4;
        if (canScrollBox) {
          port.scrollTop = Math.min(220, port.scrollHeight);
          pinnedAt = Math.round(th.getBoundingClientRect().top
                                - port.getBoundingClientRect().top);
          port.scrollTop = 0;
        } else if (canScrollPage) {
          // Scroll far enough that THIS table's header has actually reached
          // its pin line. A fixed 320px is not enough for a table 1800px down
          // the page, and a header that simply has not been reached yet is not
          // a header that failed to pin.
          const need = window.scrollY + th.getBoundingClientRect().top - want + 120;
          const max = document.documentElement.scrollHeight - window.innerHeight;
          const to = Math.min(Math.max(need, 0), max);
          window.scrollTo(0, to);
          if (window.scrollY >= need - 2) {
            pinnedAt = Math.round(th.getBoundingClientRect().top);
          }
          window.scrollTo(0, 0);
        }
        out.push({ i, restGap, pinnedAt, want,
                   port: port ? String(port.className || port.tagName)
                                  .split(' ').slice(0, 2).join('.') : 'viewport' });
      });
      return out;
    })()"""

    pages = _pages()
    server = browser = None
    try:
        try:
            server = Server(PROJECT_ROOT)
        except Exception as exc:
            raise Skip(f"could not start the app: {exc}")
        browser = Browser(chrome)
        session = browser.new_page(1340, 950)
        bad, seen = [], 0
        for label, query in pages:
            browser.goto(session, f"{server.base}/{query}", settle=3.0)
            for r in browser.eval(session, PROBE):
                seen += 1
                if r["restGap"] > 1:
                    bad.append(f"{label} table {r['i']} in .{r['port']}: first row "
                               f"{r['restGap']}px under the header at rest")
                # once pinned, the header must sit at its declared offset from
                # the scrollport, not pushed further down into the box
                elif r["pinnedAt"] is not None and r["pinnedAt"] > r["want"] + 2:
                    bad.append(f"{label} table {r['i']} in .{r['port']}: header "
                               f"pinned {r['pinnedAt']}px into its scrollport, "
                               f"expected {r['want']}px")
        if not seen:
            return False, "found no tables at all -- the probe is wrong"
        if bad:
            return False, "; ".join(bad[:4]) + (f"; +{len(bad)-4} more" if len(bad) > 4 else "")
        return True, (f"{seen} tables across {len(pages)} page types: first row below the header at rest, header pinned at its declared offset once scrolled")
    finally:
        if browser:
            browser.stop()
        if server:
            server.stop()


# ---------------------------------------------------------------------------
# 1c. Card grids
# ---------------------------------------------------------------------------
@check("cards in a row have equal heights", group="design")
def cards_in_a_row_have_equal_heights(con_unused):
    """A card grid with ragged cards reads as broken layout.

    Checks TWO things, because the obvious one cannot fail on its own: CSS
    grid's default `align-items: stretch` already makes every box in a row the
    same height, so measuring box heights alone would pass even with wildly
    uneven content. The thing that actually looks ragged is the FOOTER -- the
    tag row -- sitting at a different height in each card.

    So: equal box heights (cheap, worth holding), AND footers aligned within a
    row. The footer alignment is what the clamping and `margin-top: auto`
    actually buy, and removing either breaks this check.

    Rows are derived from each card's measured `top`, not from a column count,
    because the grid is `auto-fill` and how many fit depends on the viewport.
    """
    from .cdp import Browser, Server, find_chrome
    chrome = find_chrome()
    if not chrome:
        raise Skip("no Chrome/Chromium found (set CHROME_BIN to run this)")
    try:
        import fastapi, uvicorn            # noqa: F401
    except Exception as exc:
        raise Skip(f"the web app's deps are unavailable: {exc}")

    PROBE = """(() => {
      const out = [];
      document.querySelectorAll('.cardgrid, .boards').forEach((grid, gi) => {
        const kids = [...grid.children].filter(c => c.getBoundingClientRect().height > 0);
        if (kids.length < 2) return;
        const rows = new Map();
        for (const c of kids) {
          const r = c.getBoundingClientRect();
          const key = Math.round(r.top);            // same top == same row
          if (!rows.has(key)) rows.set(key, []);
          const foot = c.querySelector('.foot, .b-qual');
          // The footer's BOTTOM, not its top: a tag row wraps to two lines on
          // a card with more tags, so its top legitimately sits higher while
          // the card still ends flush with its neighbours. Bottom alignment is
          // what `margin-top: auto` buys and what actually reads as tidy.
          rows.get(key).push({
            h: Math.round(r.height),
            foot: foot ? Math.round(foot.getBoundingClientRect().bottom) : null });
        }
        for (const [top, cards] of rows) {
          if (cards.length < 2) continue;           // a lone card cannot be ragged
          const hs = cards.map(c => c.h);
          const fs = cards.map(c => c.foot).filter(v => v != null);
          out.push({ grid: gi, cls: String(grid.className).split(' ')[0],
                     top, n: cards.length,
                     spread: Math.max(...hs) - Math.min(...hs), hs,
                     footSpread: fs.length === cards.length && fs.length > 1
                                 ? Math.max(...fs) - Math.min(...fs) : null });
        }
      });
      return out;
    })()"""

    pages = _pages()
    server = browser = None
    try:
        try:
            server = Server(PROJECT_ROOT)
        except Exception as exc:
            raise Skip(f"could not start the app: {exc}")
        browser = Browser(chrome)
        session = browser.new_page(1340, 950)
        bad, rows_seen = [], 0
        for label, query in pages:
            browser.goto(session, f"{server.base}/{query}", settle=3.0)
            for r in browser.eval(session, PROBE):
                rows_seen += 1
                if r["spread"] > 2:                 # 2px for sub-pixel rounding
                    bad.append(f"{label} .{r['cls']} row at y={r['top']}: "
                               f"{r['n']} card HEIGHTS differ by {r['spread']}px "
                               f"({min(r['hs'])}..{max(r['hs'])})")
                elif r["footSpread"] is not None and r["footSpread"] > 2:
                    bad.append(f"{label} .{r['cls']} row at y={r['top']}: "
                               f"{r['n']} card FOOTERS differ by "
                               f"{r['footSpread']}px at the BOTTOM -- the tag rows do not sit on a common baseline")
        if not rows_seen:
            raise Skip("no multi-card rows rendered on any page")
        if bad:
            return False, "; ".join(bad[:4]) + (f"; +{len(bad)-4} more" if len(bad) > 4 else "")
        return True, (f"{rows_seen} multi-card rows across {len(pages)} page "
                      f"types: equal heights and aligned footers")
    finally:
        if browser:
            browser.stop()
        if server:
            server.stop()


# ---------------------------------------------------------------------------
# 2. Text contrast
# ---------------------------------------------------------------------------
HEX = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _parse_root_tokens(css: str) -> dict[str, str]:
    """Resolve :root custom properties, following `var(--x)` aliases."""
    root = css[css.index(":root {"):]
    root = root[:root.index("\n}")]
    raw: dict[str, str] = {}
    for name, value in re.findall(r"(--[\w-]+)\s*:\s*([^;]+);", root):
        raw[name] = value.strip()
    out: dict[str, str] = {}

    def resolve(name, depth=0):
        if depth > 8:
            return None
        v = raw.get(name)
        if v is None:
            return None
        if HEX.match(v):
            return _expand(v)
        m = re.fullmatch(r"var\((--[\w-]+)\)", v)
        if m:
            return resolve(m.group(1), depth + 1)
        return None                        # a gradient, a font stack, a length

    for name in raw:
        got = resolve(name)
        if got:
            out[name] = got
    return out


def _expand(h: str) -> str:
    h = h.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    return "#" + h.lower()


def _lum(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    parts = []
    for i in (0, 2, 4):
        c = int(h[i:i + 2], 16) / 255
        parts.append(c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4)
    r, g, b = parts
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a: str, b: str) -> float:
    la, lb = _lum(a), _lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


# Tokens that are legitimately NOT body text and so are exempt from the 4.5:1
# floor. Each needs a reason, because "exempt" is how a contrast rule dies.
EXEMPT = {
    "--n5":      "non-text rules and borders only; asserted below to stay that way",
    "--rule":    "hairline",
    "--rule-2":  "hairline",
    "--n3":      "hairline",
    "--n4":      "border",
    "--accent-w": "a GROUND, not a foreground",
    "--accent-r": "a ground",
    "--win-w": "a ground", "--win-r": "a ground",
    "--loss-w": "a ground", "--loss-r": "a ground",
    "--warn-w": "a ground", "--warn-r": "a ground",
    "--good-w": "a ground", "--good-r": "a ground",
    "--bad-w": "a ground", "--bad-r": "a ground",
    "--pos-1": "a cell ground in the diverging ramp",
    "--neg-1": "a cell ground in the diverging ramp",
    "--pos-2": "a cell ground", "--neg-2": "a cell ground",
    "--pos-3": "a cell ground", "--neg-3": "a cell ground",
    "--cov-full": "a bar fill", "--cov-part": "a bar fill",
    "--cov-none": "a bar fill", "--cov-missing": "a bar fill",
    "--chart-grid": "a gridline",
    "--team": "a franchise colour, set inline; carries its own ink via --team-ink",
    "--team-accent": "franchise secondary, used on team ground",
    "--team-safe": "derived franchise colour for white text; equals --team by default",
    "--team-ink": "the foreground ON team colour, not on page grounds",
    "--n0": "a ground", "--n1": "a ground", "--n2": "a ground",
    "--paper": "a ground", "--card": "a ground", "--sunken": "a ground",
    "--focus": "an outline, not text",
    "--c1": "chart series on white only", "--c2": "chart series",
    "--c3": "chart series", "--c4": "chart series", "--c5": "chart series",
    "--c6": "chart series", "--c7": "chart series", "--c8": "chart series",
    "--c9": "chart series", "--c10": "chart series",
}

# The three surfaces a run of text can land on.
GROUND_TOKENS = ("--card", "--paper", "--sunken")


@check("every text token meets 4.5:1 on every ground it lands on", group="design")
def text_tokens_meet_contrast(con_unused):
    """WCAG AA for body text, checked against the stylesheet rather than a list.

    The tokens actually used as a text colour are discovered by scanning for
    `color: var(--x)` in app.css, so a new token used for text is covered the
    moment it is used, and a token that stops being used stops being checked.

    This was failing badly: `--faint` was `#788393`, which scores 3.84 / 3.62 /
    3.39 on the three grounds, and the `.faint` class appears 83 times across
    the JS modules -- much of it at 11px, the smallest type in the app.
    """
    css = APP_CSS.read_text()
    tokens = _parse_root_tokens(css)
    grounds = {g: tokens[g] for g in GROUND_TOKENS if g in tokens}
    if len(grounds) != len(GROUND_TOKENS):
        return False, f"could not resolve the ground tokens: got {sorted(grounds)}"

    # Which tokens does the stylesheet actually set as a foreground colour?
    used = set()
    for prop, name in re.findall(r"(?<![-\w])(color|fill)\s*:\s*var\((--[\w-]+)\)", css):
        used.add(name)
    if not used:
        return False, "found no `color: var(--token)` rules at all -- parser is wrong"

    checked, failures = [], []
    for name in sorted(used):
        if name in EXEMPT or name not in tokens:
            continue
        value = tokens[name]
        for gname, gvalue in grounds.items():
            r = contrast(value, gvalue)
            checked.append((name, gname, r))
            if r < 4.5:
                failures.append(f"{name} {value} on {gname} {gvalue} = {r:.2f}")
    if not checked:
        return False, "no non-exempt text tokens were checked -- the exempt list has eaten the check"
    if failures:
        return False, "; ".join(failures)

    # --n5 is exempt only because it is borders; hold it to that.
    if re.search(r"(?<![-\w])color\s*:\s*var\(--n5\)", css):
        return False, "--n5 is exempt as a border colour but is now set as text (2.03:1)"

    worst = min(checked, key=lambda t: t[2])
    names = {c[0] for c in checked}
    return True, (f"{len(names)} text tokens x {len(grounds)} grounds = "
                  f"{len(checked)} pairs, all >= 4.5:1; worst is "
                  f"{worst[0]} on {worst[1]} at {worst[2]:.2f}")
