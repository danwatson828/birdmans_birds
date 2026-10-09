# Redesign overhaul — progress

Branch `redesign-overhaul`, local only. Working through the eleven-item
page-by-page brief. **A fresh session should be able to resume from this file
alone.**

## How to work on this

- One item at a time, in order. Full suite after each; commit after each.
- **One job at a time against shared files** (`web/app.css`, the shared table
  and card rules). Two background jobs that both swap `app.css` raced once
  already and left a broken file in the tree.
- Run the app: `preview_start` with `.claude/launch.json` name `nba-web`
  (port 8001). Headless checks spin up their own server on a free port.
- Constraints that hold for every item: light theme only, 4.5:1 text contrast,
  visible focus, reduced motion respected, no emoji icons, no CDN
  dependencies, tokens only (no new hardcoded hexes), print stylesheet on the
  same tokens, paid-endpoint guard must keep passing (narrate stays manual).
- Don't change api semantics, `semantic.sql`, data, or the meaning of any
  existing check. New **read-only** endpoints are allowed.
- Old URL params must keep working or redirect.

## Status

| # | Item | State |
|---|---|---|
| 0 | Shared table-header bug + standing check | **done** (accepted, do not redo) |
| 1 | Overview rework | **done** |
| 2 | Teams | **done** |
| 3 | Players directory (comparison lives inside) | **done** |
| 4 | Leaderboards (top-N cards) | **done** |
| 5 | League evolution (explorable) | **done** |
| 6 | Knowledge base equal-height cards | **done** |
| 7 | Catalog padding | **done** |
| 8 | This week dashboard grid | **done** |
| 9 | Keeper and Draft | **done** |
| 10 | Trade analyzer only | **done** |
| 11 | Colour / accent pass | **done** |

Leave **Ask**, **Model builder** and **Draft board** alone except for moving
them onto the shared tokens.

## Decisions made

- **Comparison is kept, not deleted.** It moves inside Players (select two,
  then compare). The `compare` nav item becomes the Trade analyzer only
  (item 10). Confirmed with the user.
- **No future fixtures exist.** All 1322 games of 2025-26 are `final`, through
  2026-06-13, and `scheduled ahead = 0`. The Overview shows the live fantasy
  matchup and the most recent results, labelled as such, rather than an
  invented schedule.
- `dist/`, `exports/` and `presentations/` added to `.gitignore` — generated
  artefacts that `git add -A` would otherwise sweep in. `*.duckdb` was already
  ignored.

## Item 0 — done (accepted)

The first body row rendered under the header on 11 tables across 5 routes,
every one by exactly 46px (`--topbar`).

Cause: `.table-full` computed `overflow: auto`, because call sites write
`class="table-full scroll-x"` and `overflow-x: auto` forces `overflow-y` to
compute to `auto`. That made the element its own scrollport, so the header's
`top: var(--topbar)` positioned it 46px *into the box*, over the first rows.

Fixed once, in the shared table layer:

```css
table.data thead th { position: sticky; top: var(--sticky-top, var(--topbar)); }
.table-full { --sticky-top: var(--topbar); }   /* the page scrolls */
.scroll-x, .scroll-y { --sticky-top: 0px; }    /* this box scrolls */
```

Source order matters: when both classes land on one element the specificity
ties, so the scrollport declaration must come last.

Standing check `tests/checks_design.py::table_headers_sit_above_their_first_row`
walks every table on every page type and asserts two different invariants —
first row below the header **at rest**, header pinned at its declared offset
**once scrolled**. Proven: PASS on fixed (19 tables / 16 page types), FAIL on
the pre-fix CSS with the exact 46px symptom.

## Item 1 — Overview rework (done)

Header band carrying the section accent and a prominent search; four
quick-launch tiles with **real** imagery (a 12-crest mosaic, and a sparkline
drawn from the same series the evolution page charts); a live "This week"
card beside Latest results; recent analyses as compact cards; freshness in the
footer.

New read-only endpoint **`/api/home`** (`games` param, capped 1..24) returning
recent games with brands, league totals, and a 46-season spark series. One
round trip instead of four. `results_are` states that these are the most
recent completed games because the snapshot holds no fixtures.

Section accents introduced here (full pass is item 11):
`--sec-league #11417f`, `--sec-analysis #5b3a8e`, `--sec-fantasy #8a4a0c`.
`--sec` is set on `.shell` from the route's nav group, so every page inherits
its section colour. All three clear 4.5:1 as text on all three grounds and
carry white at >=6.8:1.

### A real bug found and fixed on the way

`/api/home` + `/api/fantasy/matchup` firing together returned **crossed
cursors**: `totals` came back as `{"games": 3, "seasons": "pts", ...}` -- a row
from the other query. Cause: the fantasy endpoints took the shared read-only
connection via `_ex()` while holding `_matchup_lock`, not `_explore_lock`. Two
locks around one DuckDB connection is no mutual exclusion.

The same failure is recorded in a comment above `explore_coverage` ("one came
back with another request's columns and 400'd"); it had been patched at that
one call site and the cause left in place. Fixed properly: `_fx()`, a second
read-only connection for the five fantasy call sites. Verified with 12
concurrent `/api/home` against 6 `/api/fantasy/matchup` -- 0 crossed responses.

This is an api.py change, but it is a correctness fix, not a semantic one: no
endpoint's contract changed, they just stopped returning each other's rows.

## Item 2 — Teams (done)

East and West side by side, divisions grouped, each row carrying the crest,
the last-season record, win% coded win/loss, and a **franchise colour edge**
(its own 4px cell rather than a border, so it does not fight the row
hairline). All 30 franchises on one screen; no clipping at 1340 (table 538 in
a 540 box).

### `--team-safe` was broken and is now actually applied

`teamStyle()` never set it, and the CSS fallback `--team-safe: var(--team)` on
`:root` is silently wrong: a custom property is substituted where it is
DECLARED, so it computed once at :root against the root `--team` and every
descendant inherited that one neutral value. **The Portland correction has
never applied since it was introduced.**

Now computed per element by `textSafe()` in `lib.js` (multiplicative lightness
steps, memoised). Verified in the live page: POR `#e03a3e` -> `#de3034`, and a
no-op for BOS/LAL/ATL/NYK. The teams list went from 1 distinct edge colour to
22.

## Item 3 — Players directory (done)

New read-only **`/api/players`** with five views: `per_game`, `totals`,
`per36`, `shooting`, `advanced`. A DIRECTORY, not a ranking -- nothing picks
who is in it except the filters, and `limit` (capped 2000) is a safety rail
rather than a top-N. That is the structural difference from
`/api/explore/leaderboard`, which picks a stat and returns its top N.

- Advanced is the **minutes-weighted** aggregate of `player_advanced_stats`,
  reusing the exact expression the model builder already uses (denominator
  counts only minutes from games where the metric is present).
- Rates over a range are recomputed from summed totals via `RANGE_RATES`,
  never averaged across seasons -- the rule the leaderboard already follows.
- **Era blanking reuses `v_stat_first_tracked`**, not numbers typed here. The
  two exceptions are documented in the endpoint: minutes (stored as 0, not
  NULL, so the null-based table cannot see it -- 1975 comes from the project's
  own CoverageTool audit) and advanced (simply absent before 1996-97).
  Blanked columns return `null` plus a `blanked_why` reason; the page renders
  a hatched gap and a footnote, never a zero. Verified at 1960: mpg/rpg/apg/
  spg/bpg/fg3mpg/topg all null with reasons.

UI: five view tabs, every column sortable (blanks always sort last, in either
direction -- absent is not "worst"), name/team/position filters, min-games,
season single-or-range, a column picker with All/Core presets, and a sticky
first column. Select up to four players and the existing `Comparison`
component renders in place; `compare.js` now exports it, which also makes
item 10 a deletion rather than a rewrite.

## Item 4 — Leaderboards (done)

New read-only **`/api/leaders`**: twelve top-10 boards in one call, each
carrying the qualifier that makes it honest. Counting stats qualify on games
(41); percentages qualify on **attempts** -- 300 FGA, 100 FG3A, 100 FTA --
because a 100% three-point season off two attempts is not a leader. Verified:
the FG% leader has 310 attempts.

The page is a grid of cards with team colour edges, not a table. Clicking a
card opens the existing deep `Leaderboard` for that one stat (now taking
`initialStat` and `onBack`), which already supports single season, range and
a game-type toggle; career is the range across the whole span, where the
server recomputes rates from summed totals.

**Players vs Leaderboards is now structural, not cosmetic:** Players is one
wide table of everyone that the client sorts; Leaderboards is many short
rankings you scan, each stating its terms.

## Item 5 — League evolution (done)

One metric selector driving a main chart with a brush range, a hover readout,
and an optional second metric **indexed to the range's first season = 100**
(pace and three-point rate do not share an axis; plotting them raw makes the
flatter line look like nothing happened). A small-multiples strip below, where
clicking a tile promotes it to the main chart.

Era markers come from a new read-only **`/api/explore/era-markers`**, flagged
`curated: true` for the same reason `/api/brand` is -- the snapshot has no
rule-era column. Five markers (1979 three-point line, 1997 line restored,
2001 zone defence, 2004 hand-check ban, 2017 freedom of movement), drawn
dashed and labelled, with the caption saying they are curated, not data.
Verified: 4 render on the 3PAr series, the 1979 one correctly falling outside
that series' reliable range.

Finding #57 is a preset button (both series, from 1983).

Charts are hand-drawn SVG rather than Vega: a brush, a hover readout, an
indexed second axis and clickable markers are more code through a chart
grammar than as paths.

Bug caught in review: promoting a tile could set the main metric to whatever
was already overlaid ("X, with X overlaid"). Picking the overlay as the main
now clears it.

## Items 6 and 7 — card heights, catalog padding (done)

**7** is one line: horizontal cell padding went `--s2` -> `--s3` on the SHARED
`table.data th, td` rule. The catalog's columns were touching because that one
table had been given its own padding; putting it on the shared rule means no
table can miss it.

**6**: both variable runs in a card are clamped (question to 2 lines, snippet
to 3) and the footer is pinned with `margin-top: auto`.

### The check took three attempts, and the first two passed on broken CSS

1. Measured **box heights**. Could never fail -- grid's default
   `align-items: stretch` already equalises every box in a row.
2. Measured **footer tops**. Wrong in the other direction: a card with more
   tags wraps its tag row to two lines, so its footer legitimately starts
   higher while the card still ends flush.
3. Measures **footer bottoms** -- a common baseline, which is what
   `margin-top: auto` actually buys. PASS on the fix, FAIL (17-38px) with
   `margin-top: auto` removed.

Attempt 2 also surfaced a real miss: `kb.js` set `style={{all: "unset"}}` on
the question button, which wiped the `.kcard .q` clamp along with the button
chrome, so only the Overview cards were ever clamped. The button chrome is now
reset in CSS where the clamp survives it.

**Standing lesson for this branch:** three checks so far have needed the
ASSERTION corrected rather than the code. Each time the error was the same --
asserting something adjacent to the defect instead of the defect itself. Prove
a new check fails on the broken state before trusting a pass.

## Item 8 — This week (done)

A dashboard grid: nine compact category cells in the main column (score,
bar, margin, closeness, flippable chip, win/loss border), a **330px right
rail** for the advisory, the reconciliation note, flips and the waiver radar,
and the lineup behind tabs. Measured: the flips panel is **29%** of the page
width, where it previously spanned 100% for content needing about a third.

### Deviation: "daily lineup as tabs by date"

Not possible from the data and not faked. `lineup.days` is an integer count,
not a per-date breakdown -- the optimiser produces ONE lineup for the week --
and `/api/fantasy/outlook`'s digest covers a single day. Seven date tabs would
imply per-day assignments the model never made. The two tabs are the two
things that exist: **Week (N days)** with the optimised lineup, and **Today**
with real availability (starters playing, idle starters, bench with games).

### Two process notes

- `node --check` on a `.js` file does **not** reliably catch module syntax
  errors. It passed a file the browser refused. Check with a `.mjs` copy:
  `cp web/x.js /tmp/c.mjs && node --check /tmp/c.mjs`.
- When a paren count is wrong, find it by **counting depth** from the return
  to EOF, not by deleting one from the end. Deleting from the end made the
  file parse while mis-nesting the tree, so only the tail rendered and the
  page looked empty. The real extra paren was 25 lines earlier.

## Items 9 and 10 — Keeper/Draft, Trade (done)

**9.** The draft room is a real board: 14 rounds down, 12 teams across, 168
cells, each filled pick painted in the drafting team's colour, your picks
outlined and the current pick highlighted. **Even rounds are laid out
reversed**, so a column is one team all the way down -- which is what snake
order actually means. Best-available sits in a right rail. Keepers is a roster
table ranked by the board's own total value (same number as the draft board,
not a second opinion) with a keep toggle and a running kept-value total.

**10.** `CompareView` is the trade analyzer and nothing else; the nav item
reads "Trade analyzer". Player comparison lives in Players (item 3), which
renders the same exported `Comparison` component -- so this was a deletion,
not a rewrite.

## Item 11 — colour pass (done)

| Section | Accent | Hex |
|---|---|---|
| League | ink blue | `#11417f` |
| Analysis | violet | `#5b3a8e` |
| Fantasy | burnt amber | `#8a4a0c` |

`--sec` is set on `.shell` from the route's nav group and every page inherits
it, so a page cannot forget its section's colour. Verified across all 16 page
types: band, active nav state and first chart series all follow it.

Every page now opens on a **tinted band with a solid top edge**, not a 3px
rule -- the team page set the bar with a field of franchise colour and a rule
was not equal presence. Pages whose subject IS a team or player step the band
back and let the hero lead, and their stat strips take `--team-safe`.

Team colour appears wherever a team or player does: team list edges, player
directory chips, leaderboard row edges, draft board cells, home results,
player page band, scoreboard sides. Charts read `--c1..--c10` through
`token()`. **Zero hardcoded hexes in `web/*.js`** (the single grep match is a
comment).

All three accents clear 4.5:1 as text on all three grounds and carry white at
>=6.8:1. Amber is nearest the loss red; it is used only as a large header
field and never as an inline chip beside a number.

## Baseline to beat

Phase 8e contact sheet + capture harness:
`/private/tmp/claude-501/.../scratchpad/capture.py` and `sheet.py`, writing to
`scratchpad/sheet/`. 34 shots per phase (17 page types × 2 widths).
