# Back-checks for the six fixes (approved by Josh 2026-10-27 20:09 CT; back-checks approved 20:50 CT)

Question: what would last week's results have been under (A) the seat gate (Fix 1) and
(B) the longer selection windows (swing session-end fuse + 0DTE 120s → 30 min, Fixes 3/fuse)?
Window examined: the post-reset week, Oct 21–27, 2026. All figures pulled from production
`/api/daily-results` per session day (signed-in, 2026-10-27 ~21:00 CT).

## A. Seat gate — exact accounting

**Validation.** The Oct 26 scanner-path aggregate reproduces the record-level pull from earlier
that evening to the cent: level_breakdown 3 / −$179.90, session_sweep_reclaim 2 / −$73.60,
trend_pullback 5 / −$263.50, volume_breakout 1 / −$68.30 — 11 trades, −$585.30, and the same
−$585.30 falls out of the per-contract rows independently (11 contracts, each traded once).

**Seat determination.** Seats are session_sweep_reclaim × {AMD, MU, NFLX} only. The 2 sweep
bookings sum to −$73.60, which factors uniquely (up to a QQQ/VRT swap) as ASTS 40P (−$15.30) +
QQQ 710P or VRT 202.5C (−$58.30) — neither underlying is seated. The other 9 Oct 26 bookings are
in families that hold no seats at all. **All 11 were non-seated and would have been blocked.**

**Per-day result (scanner's own paper path only):**

| Day | Booked (actual) | Under the gate | P&L difference |
|---|---|---|---|
| Oct 21 | 0 option trades | 0 | — |
| Oct 22 | 0 (engine frozen) | 0 | — |
| Oct 23 | 0 option trades | 0 | — |
| Oct 26 | 11 trades, −$585.30 | 0 trades | +$585.30 |
| Oct 27 | 1 trade (trend_pullback, IWM 280C), **+$12.70** | 0 trades | −$12.70 |
| **Week** | **12 trades, −$572.60** | **0 trades** | **+$572.60** |

**Boundaries.** The gate cuts both ways: it also removes the one win (Oct 27). It is a
correctness fix — paper results match the seat/shadow design — not a P&L filter. Untouched by
Fix 1, by design: the scalp book (Oct 26 −$309.98, Oct 27 −$490.50; a separate producer, not the
scanner path) and futures. The seated path itself booked zero all week, so the gate changes
nothing there; the scorecard's "0 seated bookings" would finally have been the whole story.

## B. Window extensions — bounded replay

Per-ticket replay is impossible for days before the chain archive existed (intraday chains were
not recorded), so this is a reason-code census: each dead ticket's recorded final/last-attempt
reason is classified **time-curable** (fresh quote required, no recent chain, waiting for a
streamed quote — conditions a longer window can cure) vs **substance** (moved beyond entry
tolerance, invalidated, target reached — dead at any window length). Time-curable counts are an
**upper bound**: a longer window only converts a ticket if the missing quote/chain actually
arrives inside the new window while the setup is still valid, and post-expiry quote/chain
arrivals were not recorded.

**0DTE research selection (the 30-minute window):**

| Day | Tickets | Observed | Window-ended (final reason) | Invalidated (final) | Time-curable upper bound (last attempt: fresh quote + no chain) | Beyond tolerance (last attempt) |
|---|---|---|---|---|---|---|
| Oct 21–23 | 0 | 0 | 0 | 0 | 0 | 0 |
| Oct 26 | 5,531 | 18 | 4,383 (79.3%) | 1,130 (20.4%) | 3,136 (56.7%) | 1,878 (34.0%) |
| Oct 27 | 5,780 | 12 | 4,823 (83.4%) | 945 (16.4%) | 3,595 (62.2%) | 1,685 (29.1%) |
| Week | 11,311 | 30 | 9,206 (81.4%) | 2,075 (18.3%) | 6,731 (59.5%) | 3,563 (31.5%) |

Reading: at most ~60% of dead selections were killed by the clock with a curable cause; at least
~31% were already beyond tolerance at their last attempt and die under any window. The realistic
yield of the 30-minute ticket sits between those — it depends on how fast quotes/chains actually
arrive, which only forward production data (Oct 28 onward) can measure.

**Swing ideas (the session-end fuse):**

| Day | Tickets | Converted | No recent chain | Fresh quote required | Waiting for quote | Invalidated / beyond tolerance |
|---|---|---|---|---|---|---|
| Oct 26 | 76 | 0 | 54 | 17 | 3 | 2 |
| Oct 27 | 51 | 0 | 31 | 17 | 1 | 2 |
| Week | 127 | 0 | 85 | 34 | 4 | 4 |

**123 of 127 dead swing tickets (96.9%) died on time-curable reasons; only 4 (3.1%) were dead
on substance.** The 120-second fuse was the binding constraint for essentially the entire swing
book — the strongest of the two window cases, and the fuse goes further (to session end, not 30
minutes). Same upper-bound caveat: conversion still needs the chain to arrive and the price to
stay inside 0.25·ATR while waiting.

**Oct 21–23 note.** Both books show literally zero tickets on Oct 21–23 (engine freeze Oct
21–22; Oct 23's silent zero confirmed empty at the selection layer too). There is nothing to
replay for those days — the suppression census (Fix 4) exists so a future silent day carries
its own evidence.

## What to watch on Oct 28 (first session on the fixes)

- `skipped_not_seated` rows appear if the scanner path tries any non-seated booking; the paper
  book should show zero `0dte-compass-scanner-v2:*` entries outside the seats.
- Selection observed count/share vs the 12–18/day baseline; research tickets living ~30 minutes.
- First swing ticket surviving past 120 seconds — and whether any converts.
- Iceberg alert count stays at 62.
