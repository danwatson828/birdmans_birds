---
name: statistical-methodology
description: Use when a question asks whether two things are related, whether a difference is real, whether one group outperforms another, what predicts what, or requests a regression, correlation, or significance test. Defines the reporting standard every statistical claim in this project must meet — effect size with a confidence interval, R², p-value, sample size, and a residual check — and the methodology that must be stated before the analysis runs.
---

# Statistical methodology

A number without its uncertainty is not a finding. This project has already shipped
one answer whose headline figure was right and whose reasoning was retrofitted
afterwards; the standard below exists because the failure is invisible from the
output alone.

## State the design BEFORE running anything

Four named parts, in this order, before the first `run_analysis` call:

- **SAMPLE** — exactly which rows: season range, `game_type`, and any minimum
  minutes or games threshold, with the reason for *that* threshold and not another.
- **COMPARISON** — what is being compared against what. If the question is purely
  descriptive with no comparison group, say so explicitly rather than leaving it
  unstated.
- **CONFOUNDS** — what could produce this result other than the effect being
  claimed, and which of those you are handling.
- **NOT HANDLED** — the confounds you are *not* addressing. This list is almost
  never empty, and writing "none" is nearly always wrong.

Committing to the design before seeing the numbers is the whole point. Compute
first and you have chosen the framing that fits the answer you already saw.

## What a regression-type finding must report

Never a bare coefficient or a bare correlation. Every model-based claim reports:

| | |
|---|---|
| **Effect size** | the coefficient in the units of the question ("+0.42 net rating per +1% usage"), not a standardised score alone |
| **Confidence interval** | 95% CI on that coefficient. If it straddles zero, say so in words |
| **p-value** | alongside the CI, never instead of it |
| **R²** | how much is actually explained. A significant model explaining 2% of variance is a weak finding, and must be described as one |
| **n** | the sample the model was fit on, after every filter |
| **Residual check** | see below — a model whose residuals are patterned has the wrong form, and its CI is not trustworthy |

## The residual check is not optional

Fit, then look at the residuals against the fitted values. Report what you saw:

- **Curvature** → the linear form is wrong. Say so; do not report the slope as if it were.
- **Fanning** (heteroscedasticity) → the CI is too narrow. Say so, or use robust
  standard errors (`.fit(cov_type="HC3")`) and say that you did.
- **A few extreme points carrying the fit** → report the effect with and without them.

Plot the residuals as well as describing them. "Residuals look fine" without a
plot is an assertion, not a check.

## Significance is not importance

With n in the tens of thousands almost everything is significant. When an effect
is statistically significant but trivially small, lead with the size, not the
p-value. Conversely a wide CI on a small sample means "we cannot tell", which is
a real answer — report it as one rather than reaching for the point estimate.

## Confirming an exploratory finding

A correlation that arrived from the explorer is a **hypothesis**, and the explorer
scans pairs, so roughly one unrelated pair in twenty already looks significant.
Your job is to test it, not to dress it up. The verdict must be reachable in both
directions, and "NOT CONFIRMED" is the expected outcome for most of them.

Required, in this order:

1. **Split before you look.** Nominate a discovery era and a held-out era up
   front, in the methodology, and say which is which before running anything.
   Roughly two thirds / one third by season is fine; the point is that the split
   is declared rather than chosen after seeing which cut is kinder.
2. **Restate the effect size**, not just the sign, in the discovery era.
3. **Test it on the held-out era you named.** Report the effect and CI there.
   An effect that shrinks toward zero, flips sign, or whose CI covers zero out of
   sample has failed, whatever the pooled p-value says.
4. **Ask what else explains it** before crediting the variable: an era trend both
   variables share, a position or role confound, selection from the games
   threshold, or a mechanical relationship between the two measures.
5. **State a verdict explicitly**: `CONFIRMED`, `NOT CONFIRMED`, or
   `INCONCLUSIVE` — with the reason. A variable that is arbitrary by construction
   (a jersey number, a database id, a draft slot treated as a number) should be
   named as such; a correlation with it is an artefact to explain, not a finding
   to report.

A pooled p-value below 0.05 is not confirmation. It is the thing that made the
pair worth testing.

## Reproducibility

Seed every random draw (`np.random.default_rng(0)`, `random_state=0` on any
bootstrap, resample, or permutation test). Sort before any top-N or head/tail
with enough keys that no two rows can tie. The analysis is re-executed and
compared automatically; an unseeded draw will be rejected and cost you a turn.
