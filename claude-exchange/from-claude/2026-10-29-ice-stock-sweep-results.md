# Task 008 — ICE on the 1-min stock universe: sweep result

Claude, 2026-10-29, on `data-foundation-v2-stocks` (163 symbols, 2024-10-22 → 2026-10-29, SIP, adjustment=all).
Artifacts: `2026-10-29-ice-stock-sweep/results_all/` (240 cells, full universe), `results_px50/` (same 240 cells,
119 symbols with median price ≥ $50 — **a post-hoc subset, chosen after seeing the full-universe result**),
`gross_check.csv` (frictionless signed move, the Quill-comparable measurement), `build_tables.py` /
`sweep_tables.py` / `gross_check.py` (the run). Receipt: 163 parquet + manifest downloaded, 43,862,381 rows
re-counted from the files; matches Quill's receipt exactly.

## Bottom line

1. **Full universe, net of friction: 0 of 240 cells clear the gate. 235/240 are negative.** Best net cell
   +2.6 bps (v222 cum thr4 long 120 min, n 3,962, t 0.75). Friction is $0.02–0.03/share round trip
   (1¢ slip in, 1¢/sh RT fee, 1¢ through on stops) and the universe has 12 names under $10 where that is
   25–70 bps; they dominate the pooled mean.
2. **Frictionless (Quill-comparable): the composite is a long-drift signal, not a short one.** At thr 3 / 60 min,
   plain DMI, vw 1 — Quill's cells — gross signed move is **long +1.7 / +1.7 / +1.8 bps (+$0.021 / +0.018 /
   +0.013 per share), short −1.0 / −1.4 / −0.4 bps** for base / Max6 / Max6+RSI. Every short cell at every
   horizon is gross-negative. **This contradicts the frozen study's "driving side = short" for both passers**
   — flagged per Quill's request; reconcile the Task 001 way. Candidate causes, in my order: (a) R-normalisation
   (ATR units up-weight low-vol names and down-weight the large-caps that carry the long drift; bps and $/share
   weight the opposite way), (b) `adjustment=all` vs `split` — dividend-adjusted series tilt longs up by the
   yield over 2 h, which is ~0.1 bps, so not this, (c) DMI ratio vs raw (identical zero-crossings; only the
   ±0.75 threshold in Max6+RSI differs), (d) the 144 vs 163 universe. Mean signed move is sub-2¢ either way;
   both studies agree it is below friction.
3. **Median fire ≈ 0 gross, negative net, in every cell** (median gross bps = 0.000 in 20 of 24 thr3/thr4 h60
   cells). Agrees with Quill: tail-carried.
4. **Decay.** Gross long edge is first-year: base plain thr3 long h120 H1 +5.6 bps → H2 +1.6; Max6 +5.7 → +1.3;
   Max6+RSI +5.8 → +1.4. Split date 2025-10-27.
5. **Entry timing is worse than random.** Day-hour-matched placebo (same symbol, same day, same clock hour,
   random bar) puts the real mean at the **0th percentile in 238/240 cells**, both universes, both sides.
   Whatever the composite carries, it is day/regime selection; the specific bar it fires on is a bad bar to
   enter — consistent with lagging oscillators flipping after the move. Note this null is leak-prone (the pool
   includes bars after the fire, so it inherits the day's realised direction) — it is the harsh check, not the
   gate. The gate uses the hour-matched null (same symbol, same clock hour, any day).
6. **≥ $50 subset (post hoc): 36 cells pass the gate, 31 at ≥ 97.5th hour-matched placebo. All long.** Best:
   Max6 cum thr4 long 120 min, n 7,483, +8.3 bps net, t 3.97, PF 1.16, placebo mean +0.7 → excess +7.6 bps;
   H1 +12.8 / H2 +3.8; median +0.4. The family: thr 4, 60–120 min, longs, 2–8 bps net, PF 1.05–1.16, every
   one H1 ≫ H2, every one 0th pct on the day-hour null. Unconditional long 120-min net in these names is
   +0.1 bps, so this is above drift — but it is 2–8 bps on $50–$600 stocks over two hours, found by a price
   cut made after the fact, decaying, in a watchlist assembled in Sep 2026 from names that had already run
   (survivorship in the long direction is built into the universe). Not a strategy. If anyone wants to keep it
   alive, the only honest next step is the one already in motion: Quill's forward shadow scorecard, **long side,
   thr 4, 120 min**, not short thr 3.

## What this means for the options book
Nothing here survives an option spread. The largest gross effect in the whole search is +$0.09/share
(Max6 cum thr4 long 60 min, n 11.5k, median 0) over an hour; a 0–14 DTE contract's bid-ask is multiples of
that in delta-equivalent terms. ICE as an options entry trigger is not supported by this data.

## Conventions
RTH 08:30–15:00 CT; signal on bar close; entry next-bar open ± $0.01; horizons 15/30/60/120 min exit at that
bar's open; bracket 1× ATR14 stop / 2× target, stop-first, 1¢ through, flatten 14:55 CT; no entries after
14:30 CT; $0.01/sh RT fee; bps on entry price, pooled across symbols (size-neutral). Gate n ≥ 50, both halves
> 0, t ≥ 1.5, hour-matched placebo ≥ 97.5th pct (200 draws, trades subsampled to 50k per cell for the null;
real mean on the same subsample). thinkScript port regression-checked cell-for-cell against the reference
`ice_features.py` on AAPL (all 12 combos identical; outcomes match `stock_sim.py` to 1e-5 bps).
Both TOS chart checks (DMI `isCumulative`, TTM histogram) are still Josh's; the sweep carries both DMI readings
and the verdict does not depend on either.

## Housekeeping
- 163 is right; "165" was my count, not the file.
- `pull_stock_bars.py` resume-manifest flaw fixed in this drop (skipped symbols now write their entry).
- Task 006 proper (A/B/C six-fix verdicts) and Task 007's owed placebo remain open on my side.
