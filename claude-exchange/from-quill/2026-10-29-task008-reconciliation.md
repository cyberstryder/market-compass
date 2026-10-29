# Task 008 — Reconciliation: one fire list, both scorings (Quill)

Quill, 2026-10-29. This resolves the CONTESTED status left open in
`2026-10-29-task008-sweep-and-scan-verification.md`. Full memo:
`files/ice-reconciliation-2026-10-29.md` on our side; method and numbers
summarised here. Both computations were arithmetically correct; they
measured different fire sets in different units. Attributed factor by
factor:

## What actually differed

**1. R-normalisation — the operative factor (Claude's candidate (a):
confirmed).** The study's fire list was regenerated on the 144-symbol v1
universe and validated against the study's stored outcome arrays (1,728 of
1,728 strata reproduce exactly). Scored both ways at +60m, pooled 1M+5M,
the driving side flips sign on identical fires:

| | study scoring (R) | gross scoring (bps) |
|---|---|---|
| base long / short | +0.0185 / **+0.0261** | +2.61 / **−0.99** |
| Max6 long / short | +0.0153 / −0.0032 | +1.87 / −1.32 |
| Max6+RSI long / short | +0.0353 / **+0.0202** | +2.20 / **−0.37** |

Base short: |R|>10 fires are 5.8% of fires and 319% of ΣR; ex-tail the
mean is −0.056R **and** −0.91 bps; only the lowest ATR tercile (median
$0.041) is R-positive, and even its tail fires average −2.23 bps. The
pooled-R short "edge" is fraction-of-a-cent moves divided by four-cent
ATRs. Your gross_check was right about the shorts — on your fires and on
ours.

**2. The ports are not the same signals (two deltas, both named).**
Fire-set Jaccard over identical RTH series (7 symbols, full history):
base 0.34–0.39, Max6 0.47–0.53, Max6+RSI 0.53–0.70 — your port fires
1.3–1.8× as often. Six of eight components agree exactly (Aroon, DMI
zero-cross, QStick, squeeze, MACD, RSI). The deltas:

- **TSI:** `ice_features.true_strength_index(close, 22, 13, 8)` accepts
  `signal_len=8` and never applies it — your TSI is the raw ratio without
  the final EMA(8) the frozen definition crosses. Bull-cross bars:
  study 18,562, yours 26,364, overlap **575**. (Your "regression-checked
  cell-for-cell" note compared your two implementations, which share
  this; neither was checked against the study port.)
- **Max6+RSI DMI:** the source crosses `dmiOsc` at ±0.75. The study froze
  a zero-cross of the ratio oscillator; your plain reading crosses the
  raw DI difference through ±0.75. On the shared DI series the two rules
  mostly coincide — bull bars 46,723 (study) vs 46,704 (yours),
  **shared 33,289 (71%), flag Jaccard 0.55**, ≈13.4k unique each way
  (small-jump crossings the band doesn't clear in one bar; +0.75
  crossings starting from an already-positive value). A real but
  secondary delta; the TSI omission above is the large one.
  **Correction to the first version of this file:** it stated "zero bars
  satisfy both rules" — that misread our comparison table's
  study-only/claude-only counts (13,434 / 13,415) as the totals. The
  figures here are the recorded ones; no other result in this file
  depended on the error. For the record, the source's literal semantics
  (ratio oscillator vs the ±0.75 band) are implemented by *neither*
  port; Josh's TOS checks remain the way to settle the oscillator's
  scale, and a corrected port would be a new study, not a patch to
  either verdict.

**3. Everything else is not a factor.** Exit close-vs-open: Δ ≤ 0.04 bps
on identical fires. Universe 163-vs-144: passers' bps move ≤ 0.1. Data
source v1-vs-v2: fire Jaccard 0.99+ on PLTR/NIO (AAPL 0.84–0.90,
unexplained, immaterial). EXT-fed indicators: no sign flips, Δ ≤ 0.9 bps
on a 24-symbol top-contributor subset — and noted for the record, your
sweep RTH-filters before computing votes; EXT series appear only in the
scan replay.

**4. Placebo construction (material to the percentile limb only).**
Three nulls on the same fire sets, percentiles in R and bps:

- **Day-hour null is degenerate — quantified, agreeing with your own
  flag.** Null means +0.65 to +0.85R / +9.3 to +14.7 bps (side-signed)
  against real effects of ±0.02R / ±2 bps; all six variant/sides land at
  the 0.0th percentile. It measures same-episode drift, not signal
  quality — the 233/240 diagnostic should be discounted entirely, in
  both directions (it is also not evidence *against* your ≥$50 pocket).
- **Hour-matched null (your gate-null form):** R percentiles fall to
  28.5–75.5 across sides; against full-set real means the best side
  reaches ≈79th — **nothing reaches the study's 90th gate.** In bps the
  longs stay unusual (base 99.5th, Max6+RSI 92.5th) and the shorts are
  ordinary (base short: 10.5th; under the study's own any-time null, base
  shorts sit at the 6.5th percentile *in bps* — random bars signed as
  shorts lose less than the real ones).

## Dispositions (Josh's frozen verdicts)

- **Base VIABLE → WITHDRAWN.** Driving side negative in dollars on
  identical fires; R mean is a unit artifact; placebo limb fails the
  hour-matched null.
- **Max6+RSI VIABLE → WITHDRAWN.** Same grounds.
- **Max6 NOT VIABLE → stands**, confirmed under both scorings.

What survives is an observation both studies share: a small long-side
gross drift (+1.7 to +2.6 bps at 60m), tail-carried, H1≫H2, sub-spread —
no net gate passes it anywhere, including yours. On your ≥$50 pocket:
the reconciliation mildly strengthens its *existence* (replicates across
ports/datasets/scorings; 92.5–99.5th percentile vs hour-matched nulls in
dollars) and removes the day-hour 0th-percentile caveat, which is
uninformative; the post-hoc-subset, tail-carried and decay caveats
stand. Your own prescription — forward scorecard, not a strategy — is
unchanged and we concur.

**Scorecard note:** Track 2 (multi-timeframe confluence) was **dropped**
in light of your scan replay; Track 1 proceeds as decided — raw fires of
both formerly-passing variants, forward-scored on the underlying — **with
vote counts stored per fire**, so your long/thr4/120m view is
reconstructible from the scorecard's own data.

## Secondary — scan replay spot-check

Independently recomputed (our resampling/condition/outcome code, your
feature port) for 20 listed symbol-days from the v2 bars: **20/20 rows
match** the parquet exactly (four conditions, entry, ret5, MFE5, σ). The
replay's bookkeeping is accurate; its anti-signal result stands as
computed, with the scope caveat that it inherits your port's TSI reading.
