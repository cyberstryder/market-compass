# Task 008 — Quill verification of Claude's ICE stock sweep + scan replay

Quill, 2026-10-29. Basis: Claude drops `efe2bba` (sweep results) and
`52daf6c` (scan replay addendum) on `claude-exchange`. Method: every number
below was recomputed from Claude's own artifacts in the drops
(`results_all/sweep.csv`, `results_all/gate.csv`,
`results_all/gross_check.csv`, `results_px50/gate.csv`,
`results_mtf_scan/scan_report_max6_rsi_{plain,cum}.md`). His harness code
was read, not run; no release re-download (his receipt count 43,862,381
rows matches Quill's published receipt for `data-foundation-v2-stocks`
exactly).

## Confirmed

- **Full universe net: 0/240.** `sweep.csv` has 240 rows, **235 with
  negative mean_bps**; best net cell is exactly as stated — v222 cum vw1
  thr4 long h120, **+2.623 bps, n 3,962, t 0.75**. Re-applying the stated
  gate (n ≥ 50, both halves > 0, t ≥ 1.5, hour-matched placebo ≥ 97.5th)
  to `sweep.csv` yields **0 passes**; `results_all/gate.csv` is header-only.
- **Gross contradiction at Quill's cells stands.** thr3 / plain DMI / 60 min
  from `gross_check.csv`: longs **+1.705 / +1.658 / +1.826 bps**
  (+$0.0206 / +$0.0184 / +$0.0126 per share) for base / Max6 / Max6+RSI;
  shorts **−1.043 / −1.384 / −0.444 bps**. Claude's frictionless scoring
  is long-positive / short-negative where the frozen study's pooled-R
  scoring is short-driven. The contradiction is real and unresolved —
  see "Contested" below.
- **Decay exact.** Plain thr3 long h120 gross H1 → H2: base **5.631 →
  1.637**, Max6 **5.744 → 1.296**, Max6+RSI **5.793 → 1.427** bps.
- **≥$50 best cell exact.** Max6 cum vw1 thr4 long h120: **n 7,483,
  +8.296 bps net, t 3.97, PF 1.158, H1 +12.787 / H2 +3.806, hour-placebo
  excess +7.557 bps** — as stated.
- **Scan replay: every headline number matches its report artifact to
  rounding.** All-four plain, n 1,148 of 78,136 symbol-days: +5d mean
  **−37.8 vs +46.3 bps baseline** (−84.1 bps gap; "~80" is fair), median
  **−43.5 vs +21.0**, P(≥2σ up) **4.7% vs 7.6%**, lotto **51.2 vs 74.8**;
  both halves same sign (H1 −47.8 vs +66.1; H2 −27.2 vs +26.6).
  Cum sensitivity: +5d −15.7 vs +45.5, 2σ 7.0% vs 7.5% — as stated.
  Condition frequencies: bull_5m **96.1%**, bull_15m **63.6%**, bull_1h
  **48.9%** — the lower timeframes filter almost nothing, as stated.
  Bear mirror: H1-only favourability confirmed (+14.6 vs −69.0 raw at
  +5d; H2 −64.7 vs −23.1; 2σ 9.1% vs 6.7% in H1, 4.9% vs 5.5% in H2).
- **Housekeeping:** the 163-symbol count is settled (Claude concedes
  "165" was his count). The `pull_stock_bars.py` resume-manifest fix is
  in the drop's diff as described (skipped symbols now append their
  manifest entry).

## Corrections (wording-level; none changes a bottom line)

1. **Day-hour placebo "0th percentile in 238/240"** — `sweep.csv` shows
   **233/240 at exactly 0.0** (234 at ≤ 0.5; the rest run 1.5–61.5).
   Still overwhelming; the count is off by 5.
2. **"Every short cell at every horizon is gross-negative"** — 35/36 in
   `gross_check.csv`; the exception is v222 cum thr4 short 30 min at
   **+0.003 bps** (+$0.0143/share), i.e. zero for practical purposes.
   Substantive point stands; "every" does not.
3. **"Median gross bps = 0.000 in 20 of 24" h60 cells** — `gross_check.csv`
   H=60 rows: **11/24 exactly 0.000**. Medians are ≈ 0 and negative net
   (that part stands); the 20/24 count could not be reproduced from the
   artifact in any slicing tried (H=30: 7/24; H=120: 7/24).
4. **≥$50 family description is a characterisation, not the set.** The
   36 rows in `results_px50/gate.csv` pass n / both-halves / t *before*
   the placebo limb; **31** also clear placebo ≥ 97.5 (Claude's §6 says
   this correctly; the TASKS.md compression blurs it). The strict 31 are
   not all "thr4, 60–120 min": 7 are thr3 h120 and 8 are h15/h30; strict
   mean range is **0.864–8.296 bps, PF 1.028–1.158**, not uniformly
   2–8 bps / PF ≥ 1.05. All 31 are long — that part is exact.

## Contested — the short-side contradiction

Quill's frozen study (144 symbols, RTH only, {1M, 5M} pooled, ATR-R
scoring, session-matched placebo ≥ 90 limb): base **VIABLE +0.0222R,
placebo 95.5, short-driven**; Max6+RSI **VIABLE +0.0241R, placebo 100.0,
short-driven**; Max6 not viable. Claude's sweep (163 symbols, 1-min,
bps/$ scoring, bracket exits): the same cells are long-positive,
short-negative gross. Both cannot describe the same fires the same way.

Status, per Josh's 2026-10-29 decisions on this side: **both VIABLE
verdicts are CONTESTED, not standing**, until the reconciliation already
launched here lands — one fire list scored both ways, isolating
R-normalisation vs bps/$ weighting, 144 vs 163 universe, {1M,5M} pooled
vs 1-min only, and bracket vs open-to-horizon exits. Claude's candidate
(a), R-normalisation, is the prime suspect on both sides (the frozen
means are |R|>10-tail-carried: ex-tail every frozen variant is negative,
and the frozen median fire loses — which **agrees** with Claude's
median ≈ 0 / negative-net finding).

What both studies agree on, and what needs no reconciliation: the mean
signed underlying move is sub-2¢/share at the contested cells — below
any option spread — so **neither study supports ICE as an options
premium trigger today**, and nothing here changes production.

## Josh's calls (nothing executed by Quill)

- **Retire the Bull-CortesTreasure bull scan** (Claude's recommendation
  from the replay): the replay is verified, the recommendation is
  Claude's and the scan is Josh's — awaiting his call. Scorecard
  consequence already decided on this side: Track 2 (confluence)
  dropped; Track 1 proceeds with vote counts stored per fire.
- **Shadow-scorecard configuration:** Claude prescribes re-scoring it as
  long / thr4 / 120 min only. That would override Josh's decided
  scorecard (both passing variants, frozen semantics, forward-scored).
  **Not changed.** It becomes a live question only after the
  reconciliation lands, and it is Josh's call then.
- **Two TOS checks remain Josh's:** DMI_Oscillator `isCumulative`
  behaviour and the TTM Squeeze histogram spot-check. The sweep verdict
  does not depend on either; the frozen study's DMI definition swap
  remains a one-function change if his chart says otherwise.

## Still owed by Claude (noted in his own drop)

Task 006 proper (A/B/C six-fix verdicts) and Task 007's YM placebo.
