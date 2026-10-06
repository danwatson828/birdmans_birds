# nba-analytics — design system

**Direction B's fields and chips, Direction C's table density, Direction A's
serif for report prose only.**

Team colour arrives as a FIELD on the surfaces that belong to one franchise
(the team hero, the scoreboard sides), as a spine on a row inside a
league-wide table, and nowhere else. Tables run at 26px rows with mono tabular
figures and 1px rules instead of card chrome. The serif is no longer a
headline face: it carries the body of a saved analysis, where a person reads
paragraphs, and nothing else.

Superseded: Phase 8d set this to Direction A throughout, which put a 42px
serif headline over a dense table and read as a magazine pretending to be an
instrument.

This file is the written form of `web/app.css`'s `:root`. The stylesheet is the
implementation and the only place a value is decided; this document says *why*
each value is what it is, so the next pass can disagree with a reason rather
than with taste.

---

## 1. The three dials

From `design-taste-frontend` (installed from `Leonxlnx/taste-skill` as
`skills/taste-skill/SKILL.md`), §1. Baseline is 8 / 6 / 4; this project runs:

| Dial | Baseline | Here | Why |
|---|---|---|---|
| `DESIGN_VARIANCE` | 8 | **5** | 8-10 is masonry and "massive empty zones". Asymmetry fights a sortable table. But 3-4 is the public-sector preset, and that register was tried twice here and read as absence of decision. 5 keeps the grid and spends the variance on type scale. |
| `MOTION_INTENSITY` | 6 | **2** | Tier 1-3: "no automatic animations, `:hover` and `:active` only, `prefers-reduced-motion` is the default mode anyway". A reference work should not move. |
| `VISUAL_DENSITY` | 4 | **8** | Tier 8-10, "Cockpit": tight padding, 1px lines rather than card boxes, and *mandatory mono for all numbers*. A leaderboard here is 17 numeric columns. |

**The skill disclaims this category.** Its §13 lists "Dashboards / dense product
UI" and "Data tables" as out of scope and instructs the reader to say so. Its
marketing-page machinery (hero rules, bento, logo walls, eyebrow budgets, "no
20-row data tables") does not apply. Its craft layer — type, colour
calibration, contrast, focus, motion, the §11 redesign protocol — does, and is
what this system is built from.

---

## 2. Colour

### Ground and ink

Warm paper, not cool. The ground is a faint warm white, which is what lets an
ink-blue accent and thirty saturated franchise colours sit on it without the
page reading blue-on-blue.

| Token | Value | Role |
|---|---|---|
| `--n0` / `--card` | `#ffffff` | raised surface |
| `--n1` / `--paper` | `#fbfaf7` | page ground |
| `--n2` / `--sunken` | `#f3f1ec` | sunken, hover |
| `--n3` / `--rule-2` | `#e8e5de` | hairline |
| `--n4` / `--rule` | `#d8d4cb` | border |
| `--n5` | `#b4afa4` | **non-text only** — 2.03:1, borders and rules |
| `--n6` / `--faint` | `#686c75` | faint text — worst ground 4.66 |
| `--n7` / `--muted` | `#5c6069` | muted text — 5.58 |
| `--n8` / `--ink-2` | `#3a3d44` | secondary text — 9.64 |
| `--n9` / `--ink` | `#16171a` | primary ink — 15.88 |

### The contrast floor is not negotiable

**Every token used as a text colour clears WCAG AA 4.5:1 on all three grounds
it can land on** (`--card`, `--paper`, `--sunken`). This is enforced by
`tests/checks_design.py::text_tokens_meet_contrast`, which discovers the text
tokens by scanning `app.css` for `color: var(--x)` rather than reading a list,
so a new token is covered the moment it is used.

The previous pass failed this. `--faint` was `#788393` at 3.84 / 3.62 / 3.39,
and `.faint` appears 83 times across the JS modules, much of it at 11px — the
smallest type in the app. `--warn` was `#9a6b10` and failed on two of three.

A token exempt from the floor must carry a reason in `EXEMPT` in that check.
"Exempt" with no reason is how a contrast rule dies.

### The three rules for where colour goes

1. **TEAM colour** is the franchise's own, applied where you are looking at
   that franchise, never as site chrome. It arrives as `--team`,
   `--team-accent`, `--team-ink` set inline from `/api/brand`, so one
   stylesheet serves thirty identities. The brand data is curated
   (`curated: true`) and hand-entered; it is not derived from the logo.
2. **DATA colour** encodes a value, never a mood. Wins `--win` `#116b43`,
   losses `--loss` `#a8322a`, above/below on the `--pos-3 … --neg-3` diverging
   ramp. If it is coloured it means something, and the same hue means the same
   thing on every page.
3. **STRUCTURE stays neutral.** Paper ground, ink text, hairlines. Colour is a
   figure on that ground, which is what makes it legible.

### Site accent

`--accent` `#14387a`, a deep ink blue — 9.91:1 as text on paper, and dark
enough to carry white on a button. Deliberately not any franchise's colour.
One accent, used identically everywhere (skill §4.2, Colour Consistency Lock).

### `--team-safe`

The franchise colour darkened just enough to carry white text at 4.5:1. It
equals `--team` for 29 of 30 franchises. Portland's `#e03a3e` scores **4.34
against white and 4.36 against near-black**, so no choice of text colour fixes
it and the swatch itself has to move, to `#de3034` (4.59 against white, a 4%
lightness reduction).

Direction A puts team colour on a masthead band and a row spine rather than
behind text, so nothing currently needs `--team-safe`. It exists so a future
surface that *does* set white on team colour reaches for a correct value
instead of inventing one.

### Categorical series

`--c1 … --c10`, 47-89% saturation across a 28-point lightness spread, so the
series survives both a 4px swatch and greyscale printing. Read from JS via
`series(n)` in `lib.js`.

The cluster view used to define its own ten colours at 24% mean saturation
inside an 11-point lightness band — ten categories separated by hue alone at
near-identical value. That set is gone.

### No colour literals outside this file

`app.css` is the only place a colour is written. JS reads tokens through
`token("--name")` in `lib.js`, which resolves a custom property once and caches
it. There were 29 hard-coded hex values across five modules
(`explore.js` 10, `cluster.js` 10, `model.js` 4, `evolution.js` 3, `lib.js` 2),
several of them stale copies of a token value. There are now zero.

---

## 3. Type

Self-hosted, latin subset, declared in `web/fonts.css`. No CDN and no network,
so the app and its exported PDFs render identically offline.

| Role | Face | Licence |
|---|---|---|
| UI, headings, labels, chips | **Archivo** | SIL OFL 1.1 |
| Figures | **IBM Plex Mono** | SIL OFL 1.1 |
| Report prose only | **Source Serif 4** | SIL OFL 1.1 |

Archivo is one variable file carrying **both** a weight axis (400-800) and a
width axis (75-100), so the condensed uppercase labels this design leans on
come from the same file as the body text rather than from a second face. It
replaced IBM Plex Sans.

Source Serif 4 and IBM Plex Sans ship as single **variable** files with a
weight range; IBM Plex Mono is static, so its three weights are three files.
Six files, 243KB total, deduplicated **by content hash** — Google Fonts serves
the identical variable file for every weight you ask for, so requesting 400,
600 and 700 yields three copies of one 122KB font.

**Every figure is mono.** At `VISUAL_DENSITY` 8 a leaderboard puts seventeen
numeric columns side by side; a proportional face gives each column a different
width per row and the eye cannot run down it. `table.data` is mono with
`font-variant-numeric: tabular-nums`; `td.name` opts back into the sans,
because a name is read rather than scanned.

Scale, with real jumps rather than a smooth ramp — the ratio *is* the
hierarchy: `11 · 13 · 14.5 · 17 · 21 · 26 · 33 · 42`
(`--t-xs … --t-4xl`).

---

## 4. Space and shape

`--s1 … --s8`: `4 · 8 · 12 · 16 · 20 · 26 · 34 · 42`.

Radii are tight — `--r1` 3px, `--r2` 5px, `--r3` 8px. Broadsheet is a
rules-not-boxes direction: a panel is a ruled area, not a pill.

`--rail` 244px.

---

## 5. Layout

**Grid tracks that hold tables use `minmax(0, 1fr)`, never a bare `1fr`.**

A bare `1fr` is `minmax(auto, 1fr)`, and `auto` floors the track at its
max-content width. The 960px breakpoint set `.shell { grid-template-columns:
1fr }`, so the wide tables inside pushed the single column out to `.main`'s own
`max-width: 1180px`: at a 420px viewport the document was 1180px wide, **760px
of unreachable page on every route**. `.conf-cols` had the same shape.

Enforced by `tests/checks_design.py::no_horizontal_overflow_at_420`, which
drives a real headless Chrome over every route read out of `app.js` and
compares `scrollWidth` to `innerWidth`.

---

## 6. Focus and motion

One rule covers the document:

```css
:where(a, button, select, input, textarea, summary, [tabindex]):focus-visible {
  outline: 2px solid var(--focus); outline-offset: 2px;
}
```

`:focus-visible` rather than `:focus`, so clicking a table row to drill down
does not leave a ring behind. Before this pass the stylesheet had exactly one
focus rule and it covered `select`/`input`/`textarea` — the thirteen nav links,
every button and every sortable header showed nothing at all.

A skip link precedes the rail, because keyboard users otherwise pass thirteen
nav links before reaching content on every page.

`prefers-reduced-motion: reduce` is honoured globally rather than
per-component, so a rule added later cannot forget to. It was absent entirely
while the stylesheet ran transitions.

---

## 7. Icons

**Phosphor Icons** (regular, MIT), self-hosted. Source SVGs are committed under
`web/icons/`; `web/icons.js` is generated from them and inlines the paths.

Inlined rather than referenced from a sprite with `<use href="…#id">`: a
cross-file `<use>` is resolved by the renderer, and headless Chrome does not
reliably resolve it when printing to PDF — the icon silently vanishes from the
export. Inlining makes screen and PDF render from the same markup.

The nav was previously Unicode dingbats — `◆ ✳ ∿ ❋ ✓ ◉ ≡ ⇄ ◢ ▤ ◈ ⚔ ◇`. Those
are not an icon set: the glyphs come from whatever font happens to cover them,
so they share no stroke weight or optical size, and `⚔` renders as a colour
emoji on several platforms.

To regenerate after adding a name to the list:

```bash
# downloads to web/icons/, then rebuilds web/icons.js from those files
python - <<'EOF'
import urllib.request, pathlib, re, json
NAMES = [...]  # see the list at the top of the generator in the Phase 8d notes
EOF
```

---

## 8. Print

The exported PDF is a deliverable, not a screenshot of one, and it reads from
**the same tokens as the screen**. The print block previously carried its own
`#000` / `#333` / `#999` / `#bbb` and drifted from the app every time the app
changed.

Grounds go white, because paper is white and printing a warm ground wastes
toner without adding information. Ink and rules are tokens.

Two things the print block must keep:

- `print-color-adjust: exact` on everything whose **meaning** is its colour —
  coverage bars, win/loss and diverging cells, the ranked column, chips and
  verdicts. Chrome drops background paint by default, which would strip the
  encoding out of the PDF rather than merely mute it.
- `break-inside: avoid` on figures, so a chart is never split across a fold.

`details` are expanded before printing by `installPrintHandlers()` in `lib.js`
and restored afterwards, so the SQL of every step is present in the PDF.

---

## 9. What this system refuses

From the skill set, overridden deliberately:

- **Dark mode** (§8 "dual-mode by default", §14 "tested in both modes"). Light
  only, by instruction. The skill's own carve-out is print-emulating editorial,
  which the PDF export makes partly true here.
- **A real design system package** (§2.A would route "enterprise analytics" to
  IBM Carbon). This is a zero-build UMD app with no `package.json`;
  `redesign-skill`'s Rules say work with the existing stack and do not migrate.
- **"No 20-row data tables"** (§4.9, §14). That rule is for marketing pages.
  Data is the product here.
- **Noise, grain, background imagery, Picsum placeholders**
  (`redesign-skill` → Colour → "flat design with zero texture"). Decorative
  imagery behind data is how you get a chart nobody trusts.
- **"Dashboards always have a left sidebar — try top navigation"**. Thirteen
  grouped routes need a rail.
- **Zero em-dashes** (§9.G). Held for new UI chrome. Not applied retroactively
  to saved analyses: that is agent-written prose, §11.C says preserve copy
  voice, and rewriting it would alter stored records.

---

## 10. Page shapes (8e)

A design system that only sets colour and type leaves every page the same
centred column. These are the shapes, and the reason each exists.

| Page | Shape | Why |
|---|---|---|
| Home | Crest-imagery header, prioritised entry tiles, recent analyses as cards | Four identical cards said every destination was equally likely; Ask and Teams carry the traffic, so the lead tile is twice the width |
| Teams | Two conference columns, divisions within | Thirty franchises fit on one screen in the shape the list is actually read in |
| Team | Colour field, one-line stat strip, Roster/Results tabs | Roster and results are peers, not a sequence; stacked they made the page 1374px tall |
| Game | Scoreboard, stat strip, line score, box-score tabs | A game is a RESULT first; both box scores rendered in sequence put the thing you clicked for last |
| Player | Header card with team band, trend, four tabs | Career totals, trend, game log, era-adjusted, similar players and seasons down one column came to 3016px |
| Leaderboard | Sticky filter bar, full-width table | The filters scrolled away from the table they filtered; the 1180px cap wasted 160px of a 1340px screen |
| Ask | Prose at measure beside a sticky provenance/actions rail | The answer began ~400px below the question |
| Knowledge base | Card grid, Validated findings as a tab | Rendering every snippet, tag and note inline made it 8196px; findings is a filter over the same store, not a second archive |
| Model builder | Controls rail beside the workspace | Choosing a predictor scrolled the fit off screen, so you tuned the model blind |
| Catalog | Table list with fields in a side panel | Already this shape; retuned only |

**Primitives** live in `app.css`: `.split` / `.split.wide`, `.toolbar`,
`.tabs`, `.hero-team`, `.hero-player`, `.statrow`, `.scoreboard`, `.cardgrid`
+ `.kcard`, `.entry`, `.conf`. Every one collapses to a single
`minmax(0, 1fr)` column at the 960px breakpoint — omitting that is exactly how
the model builder came to overflow a 420px viewport by 150px.

**`.chip` is the status pill. `.vchip` is the variable-picker button.** They
were both called `.chip`; the status pill's `white-space: nowrap` then stopped
long variable names wrapping and pushed the model builder 376px past the
viewport edge. Two components, two names.

## 11. Navigation

Thirteen rail items in three groups named for what you are doing:

- **League** — Overview, Teams → Team → Game, Players → Player, Leaderboards, League evolution
- **Analysis** — Ask, Model builder, Knowledge base, Dataset catalog
- **Fantasy** — This week, Draft board, Keepers & draft, Compare & trade

A sticky topbar carries the breadcrumb and the global search, both of which
were previously repeated inside individual pages.

**No old URL dies.** `?v=explore` resolves by what it asked for —
`view=player` → Players, `view=leaderboard` → Leaderboards, `view=catalog` →
Dataset catalog, otherwise Teams. `?v=findings` → Knowledge base with the
findings tab selected. The map is `LEGACY` in `app.js`.
