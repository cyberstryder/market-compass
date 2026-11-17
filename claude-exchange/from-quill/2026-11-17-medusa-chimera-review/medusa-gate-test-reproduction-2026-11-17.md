# Medusa Gate Test — Independent Reproduction (2026-11-17)

**Requested by Josh 2026-11-17 09:47 CT ("reproduce it"), after the triage of
`medusa-gate-runner.zip` reported the author's own negative verdict on Medusa's
EOD signal. Verdict: REPRODUCED — on every limb.**

## What was run

The author's harness `run_gate_test.py` **unchanged** — every piece of scoring
logic is theirs: `sanity_check_calibration()`, `to_5m()`, `prior_context()`,
`daily_atr()`, `classify_gated()`, `run_dataset()`/`report()`. Both gate
variants scored as shipped: as-uploaded (dead spacelike filter) and calibrated
5×. No fixing, no tuning, no threshold touched.

Mechanics (from the code): at **15:15 ET**, the oracle's `classify_periods()`
composite state on the day's 5-min bars up to 15:15 (S0 anchored at the day's
open) gives a direction via the gate's `_regime_direction` map (NEUTRAL
families non-directional). Accuracy = sign of the **decision→close residual
(15:15→16:00)** vs that direction. The gate (osmosis + WAIT) can stand a call
down; stood-down days score 0. Inputs per day: 5-min bars, daily ATR(17),
prior-day VIX close, prior-day score/MACD context from daily bars.

Two forced deviations, both data plumbing only:

1. **VIX source.** The harness's `spy_days()` fetches ^VIX via yfinance.
   yfinance is not installed on this VM and this run was on-disk-only by
   instruction, so a driver (`run_repro.py`) feeds the author's own
   `prior_context()` the on-disk CBOE VIX history
   (`vix/VIX_History.csv` from `release-staging/alpaca-daily-and-vix.tar.gz`,
   through 2026-10-23) in the identical shape. Same series, prior-day close —
   the classifier consumes one VIX level per day.
2. **SPX leg not run.** The harness's SPX leg is yfinance-only; there is no
   SPX repository on disk (DBIX is a trailing ~14-day window). SPY only —
   the author's SPX leg (59 days) is not reproducible here.

SPY data: `SPY_1min.parquet` + `SPY_daily.parquet` from
`~/workspace/exports/spy-data.zip` — the same pinned Alpaca bundle the harness
was written against (2024-10-28 → 2026-10-26). **499 sessions classified**,
2024-10-28 → 2026-10-23 — exactly the author's SPY count. (The bundle's last
day, 2026-10-26, is partial — bars end 12:41 ET — and is skipped by the
harness's own <200-bar rule.)

The author's calibration sanity check passed as documented (calibrated filter
flags the synthetic −0.9% shock bar; uploaded filter dead on synthetic data).

## Results — ours (SPY, 499 days) vs author's claims (pooled SPY 499 + SPX 59)

| Limb | Author's claim | This run (SPY only) | Match |
|---|---|---|---|
| Sessions | 499 SPY + 59 SPX = 558 | 499 SPY | exact on the SPY leg |
| Ungated 15:15 sign accuracy | **49.2%** | **49.1%** (428 directional days, +10.0 pts total) | ✓ (0.1 pt = the unrun SPX leg in their pool) |
| Gated sign accuracy | **45.7%** | **45.7%** (304 days traded after stand-downs, −13.5 pts) | ✓ exact |
| Net, gated − ungated | **−24 pts** | **−23.5 pts** | ✓ (residual = SPX leg / pooling) |
| Wrong calls fixed by gate | **52** | **52** bear-side (53 pooled incl. 1 bull) | ✓ exact on the bear side as quoted |
| Correct bear calls stood down | **69** | **68** (of 199 right bear calls; +67.0 pts given up vs −44.6 pts saved on the fixed side) | ✓ off by one — the unrun SPX leg |
| Calibrated vs as-uploaded gate | **zero decisions changed in 558 days** | **zero differing days in 499** — direction, final state, WAIT, and osmosis steps identical on every day | ✓ exact |

Supporting detail from our run: the gate WAITed on 105 of 499 days (21%),
osmosis acted on 25% of days, and it never flipped a call's sign — it only
stood calls down, and it stood down more right ones than wrong ones on both
sides (pooled: 53 wrong fixed, 71 right dulled). The calibrated spacelike
filter flagged 2 of 499 days (0.4%) and changed no decision on either —
consistent with "zero decisions changed." On the directional-states subset
(BULL/BEAR 1–2, n=257) the ungated sign was 50.6% and the gate took it to
49.7% with the total collapsing +15.0 → +0.6 pts. Half splits agree with the
author's picture: ungated −7.0 / +17.0 pts by half; gated worse in both
halves (−17.2 / +3.7).

One structural observation from the day-level output: the classifier is
heavily bear-tilted on this window (395 bear-directional days vs 33 bull;
BEAR_2 alone is 146 days) — which is why the author's fix/dull quote is
bear-dominated. It doesn't change the verdict; it explains its shape.

## Verdict

**REPRODUCED.** The author's headline numbers hold on an independent run of
their own code against the on-disk data: the ungated Medusa 15:15 signal is a
coin flip (49.1%), the regime gate makes it worse (45.7%, −23.5 net points),
it stands down more correct calls than it fixes wrong ones (68 vs 52 on the
bear side), and the 5× spacelike calibration is decision-irrelevant (zero
differing days). The only limbs not independently re-run are the 59-day SPX
leg (no data on disk, no network by design) — and the pooled-vs-SPY-only
gaps it explains are ≤1 count and ≤0.5 pts everywhere.

**Standalone Medusa is closed on the author's own evidence, now
corroborated: there is no measured edge in the signal, and the gate
subtracts value.** No variant, tuning, or follow-on test was designed or
run — per instruction, this run only.

## Workpapers

`hidden_files/medusa-gate-repro-2026-11-17/` — `code/` (author's files copied
verbatim + `run_repro.py` driver + `gate-test-spy-daily-repro.csv`, the
499-day day-level output), `data/` (the two SPY parquets), `repro_run.log`
(full harness output). Spend: $0. Network: none.
