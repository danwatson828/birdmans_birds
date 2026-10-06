---
name: analytical-visualization
description: Use when emitting a chart spec, choosing how to visualise a statistical result, or deciding what a scatter, trend, leaderboard, regression, or residual chart should look like. Defines the house standard for analytical charts — labelled axes with units, uncertainty shown, honest scales, no chart-junk — and the specific shapes to use for regression findings.
---

# Analytical visualisation

A chart in this project is a Vega-Lite spec built on a saved query, rendered by
the frontend. It is evidence attached to a claim, not decoration.

## Always

- **Label both axes in words, with units.** `ts_edge_pp` is a column name, not a
  label. "Home TS% minus road TS% (percentage points)" is a label.
- **Title states the finding or the scope**, including the season range and
  `game_type`, so the chart is readable detached from the surrounding text.
- **Show uncertainty whenever you are showing an estimate.** A mean without an
  interval invites the reader to believe it more than the data supports. Use
  `errorband` for a fitted line, `errorbar` or `rule` for point estimates.
- **Sort categorical axes by value**, not alphabetically, unless the category has
  a natural order (season, month, round).

## Never

- No 3-D, no pie charts, no dual y-axes with different scales.
- No truncated y-axis on a bar chart — bar length encodes magnitude, and cutting
  the baseline lies about it. Truncating is acceptable on a *line* chart when the
  variation would otherwise be invisible; say so in the title or subtitle.
- No colour carrying information that is not also in position or a label; no
  gradient backgrounds, drop shadows, or gridline clutter.
- No legend where direct labelling on the mark is clearer.

## Shapes for statistical findings

**Scatter with fitted line** — the default for any claimed relationship between
two continuous variables. Layer `point` under `line`, with an `errorband` for the
confidence interval. Never show a fitted line without the scatter behind it: the
line alone hides the spread that determines whether the fit means anything.

**Coefficient / forest plot** — for a model with several inputs, or one effect
across several subgroups. Layer `rule` (the interval, `x` to `x2`) under `point`
(the estimate), one row per term, with a `rule` at zero for reference. Sort by
effect size. This is what makes "which of these actually matters" readable.

**Residual plot** — fitted values on x, residuals on y, with a `rule` at zero.
Required alongside any regression claim. Its job is to let the reader see
curvature or fanning that the R² alone hides.

## Era shading

When a chart spans seasons where a stat was not reliably recorded, shade or
exclude that range and say which in the title. `check_coverage` gives the
boundary. An unshaded all-time chart of steals or three-pointers reads as a
trend when it is partly a recording artefact.
