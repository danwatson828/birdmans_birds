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
| 1 | Overview rework | not started |
| 2 | Teams | not started |
| 3 | Players directory (comparison lives inside) | not started |
| 4 | Leaderboards (top-N cards) | not started |
| 5 | League evolution (explorable) | not started |
| 6 | Knowledge base equal-height cards | not started |
| 7 | Catalog padding | not started |
| 8 | This week dashboard grid | not started |
| 9 | Keeper and Draft | not started |
| 10 | Trade analyzer only | not started |
| 11 | Colour / accent pass | not started |

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

## Baseline to beat

Phase 8e contact sheet + capture harness:
`/private/tmp/claude-501/.../scratchpad/capture.py` and `sheet.py`, writing to
`scratchpad/sheet/`. 34 shots per phase (17 page types × 2 widths).
