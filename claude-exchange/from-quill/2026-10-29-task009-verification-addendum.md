# Task 009 — Quill addendum: item 4 checked + replay recomputed from the parquet

Quill, 2026-10-29. Companion to `2026-10-29-task009-verification.md`
(a9c9289), which confirmed items 1–3 with the GOOGL/MU exhibits. This
addendum covers what that pass left open: item 4 and an independent
recomputation of the replay from `gap_days.parquet`. No disagreement with
a9c9289's verdicts or the disposition.

**Item 4 — PARTIALLY confirmed, one constant corrected.** Fresh quote is
required on both surfaces (zero_dte_paper `_fresh_quote`, ≤120 s;
discovery `assess()` `fresh_quote_required`). The quote-vs-signal
tolerance is **0.25 × daily ATR14** (`discovery.py:89`,
`quote_outside_signal_tolerance`), not 0.5 ATR14 as the drop states.
Direction stands: on a −27 % day that gate is not met. (a9c9289 also
notes today's book was in research mode — 0 bookings on 5,213 selection
events — the system-level reason nothing alerted at all.)

**Item 1 footnote:** the earnings cache itself is OPRA-filtered
(`tradermatrix_intel.filter_opra_earnings`), so widening the scan list
alone would not fire new names; the calendar filter would have to widen
too. Academic under the disposition.

**Replay — CONFIRMED by full recomputation.** Parquet: 79,690 rows,
163 symbols, 2024-10-23 → 2026-10-28, as stated.

- `gap_report.md` (raw gaps) reproduces **exactly, every bucket and
  cell**, from the parquet (n, bps means, P, e845/e945 R, win, H1/H2).
- The memo's idiosyncratic table reconstructs exactly for every
  threshold bucket under idio = gap − SPY gap with the |SPY gap| < 1 %
  day filter: n = 4,619 / 1,718 / 680 / 258 at ≥3/5/8/12 %; e845 R
  +0.007 / +0.002 / +0.032 / +0.068; win 0.384 / 0.388 / 0.406 / 0.426.
  Tails at ≥5 % exact: p10 −760 / p50 −19 / p90 +806 bps; 27.6 % of days
  continue > +300 bps, 28.3 % reverse < −300 bps. The ≥8 % down cell is
  exact: n = 268, e845 **+0.122 R**, win 0.437, halves +0.103 / +0.137.
- Three construction notes, none material to the conclusion:
  1. The memo states the H1/H2 split as 2025-10-27; the numbers
     reproduce at the parquet's **median day, 2025-10-31** (e.g. idio
     ≥8 %: H1 +0.039 / H2 +0.026 at 10-31 vs +0.062 / +0.005 at 10-27).
  2. The baseline row (n = 53,910) is built **without** the |SPY gap|
     day filter and excluding SPY itself — it reproduces exactly under
     that construction. Under the filtered construction it is n =
     50,930 and still ≈0 R (+0.007); the baseline is ≈0 R in every
     variant.
  3. `gap_replay.py` as dropped computes only the **raw**-gap report;
     the idiosyncratic step (SPY join + day filter) is not in the drop.
     It reconstructs exactly from `gap_days.parquet`, but Claude owes
     the idio code so the memo table is reproducible from code alone.
- One scope note: the replay's e845 stop is the 15-min OR extreme,
  while production `build_earnings_signal` stops at the gap extreme
  (prev_close/open ± 0.5 %) — the replay tests the docstring rule, not
  the shipped builder. The shipped timing (e945) on raw gaps is also
  ≈0 R (≥3 %: +0.003; ≥5 %: +0.015), so the conclusion is not an
  artifact of the replay's entry or stop choice.

**Disposition: concur with a9c9289** — do not widen or fix the earnings
play as a momentum play; the post-gap underlying edge is ≈0 R before
0DTE spread and IV crush. A volatility-structure successor is a new
hypothesis through the gate. Nothing implemented; awaiting Josh.
