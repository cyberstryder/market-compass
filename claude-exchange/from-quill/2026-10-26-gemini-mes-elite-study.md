# Gemini "MES 15m Combined Elite" — year gauntlet

Scoped study approved by Josh 2026-10-26 21:14 CT (exception to the paused
strategy search). Subject: the Gemini-authored Pine v5 strategy in
`~/workspace/user/files/gemini-code-1793067066260.txt`, translated mechanically
and run on the Databento MES year (1-min bars, 2025-10-27 → 2026-10-26,
351,758 bars). Signals on 15-minute bars; position management walked on the
1-minute series, stop-first, so intrabar sequencing is measured, not assumed.
All dollars per contract at house costs ($3 round-turn, −2 ticks charged on
stop fills) unless noted; the strategy trades 2 contracts, so the ×2 column
is the strategy's own sizing (fees scale linearly, ×2 = 2 × per-contract).

## Verdict

**Not an edge under the house gate.** As written, the strategy **loses money
over the full year**: −$902 per contract (−$1,804 at 2 contracts), PF 0.935.
Josh's rules (daily 15:45 CT flatten) do slightly better but still lose:
−$782 per contract, PF 0.940.

**Josh's TradingView impression is real — for his window.** Over the last
two months (2026-08-26 → 10-26) the same machinery nets **+$788/ct, PF 1.434,
61.2% win rate** (variant A). The other ten months net **−$1,690/ct,
PF 0.860**. This is a window/regime effect, not a backtester illusion:
2026 Q1 alone is −$1,343/ct at PF 0.624. A long-only system behind a
permissive bull filter (close > EMA200 *or* EMA200 rising) eats that kind
of quarter in full.

**The entries do carry information — worth less than the harvest cost.**
Against the shifted-entry placebo (same exit machinery, same times of day,
random days), the headline clears the 95th-percentile gate narrowly
(**95.5th percentile**, variant A; 96.0 for B) — but the placebo median is
−$3,026/ct, so "beating random" here means losing $902 instead of $3,026.
Same signature as the Mitro MNQ cell: real timing information, negative
absolute expectancy after costs. At TradingView's own accounting
($1.24 round-turn, and even keeping our −2-tick stop charge) the full year
is ≈ breakeven: **+$200/ct** (A) / **+$348/ct** (B). The strategy's fate is
decided by the cost model and the quarter, not by its signal quality.

## Headline results

| Variant | n | Net 1ct | Net 2ct | PF | Win% | Avg W | Avg L | Max DD |
|---|---|---|---|---|---|---|---|---|
| A — as written (overnight holds) | 626 | −$902.03 | −$1,804.06 | 0.935 | 51.0% | +$40.56 | −$45.09 | $2,076.53 |
| B — 15:45 CT flatten daily | 642 | −$782.24 | −$1,564.47 | 0.940 | 50.2% | +$38.25 | −$40.93 | $2,006.97 |

### Per calendar quarter (net 1ct / PF / n), variant A

| Quarter | Net | PF | n |
|---|---|---|---|
| 2025Q4 (from Oct 27) | −$62.87 | 0.963 | 93 |
| 2026Q1 | −$1,342.68 | 0.624 | 136 |
| 2026Q2 | +$355.61 | 1.083 | 191 |
| 2026Q3 | −$119.87 | 0.969 | 165 |
| 2026Q4 (Oct 1–26, partial) | +$267.78 | 1.531 | 41 |

Variant B by quarter: 2025Q4 −$213.12/0.870 (96), 2026Q1 −$1,283.48/0.639
(141), 2026Q2 +$577.32/1.154 (197), 2026Q3 −$109.62/0.970 (167),
2026Q4 +$246.67/1.492 (41). Three of five quarters negative in both
variants; no quarter-stability case exists.

### Josh's window vs the rest (variant A)

| Slice | n | Net 1ct | PF | Win% |
|---|---|---|---|---|
| Last 2 months (2026-08-26 → 10-26) | 98 | +$787.74 | 1.434 | 61.2% |
| First 10 months | 528 | −$1,689.77 | 0.860 | — |

Variant B, last 2 months: n=98, +$896.62, PF 1.564.

### Where the money moves (variant A, net 1ct by exit reason)

| Exit | n | Net |
|---|---|---|
| Snap-back | 282 | +$10,292.75 |
| Hard stop | 170 | −$13,074.79 |
| Breakeven stop | 138 | −$436.50 |
| Full target (2.2 ATR) | 36 | +$2,316.51 |

By setup (variant A): **Sniper** 370 trades, +$32.65, PF 1.004 — breakeven.
**Reclaim** 256 trades, −$934.68, PF 0.847 — the drag. The snap-back exit
is the entire profit engine; the hard stops are the entire cost.

## Placebo gate (200 iterations, Mitro construction)

| Variant | Real net 1ct | Percentile | Placebo median | p5 | p95 |
|---|---|---|---|---|---|
| A | −$902.03 | **95.5** | −$3,025.96 | −$5,398.22 | −$1,013.90 |
| B | −$782.24 | **96.0** | −$2,792.25 | −$5,091.61 | −$890.55 |

Interpretation: the gate's letter is met (≥95th percentile) but its purpose
is not — the strategy is the least-bad way to run this exit machinery on
this instrument, not a positive-expectancy system at house costs.

## Ablations (variant A base; pre-registered)

| Ablation | n | Net 1ct | PF | Note |
|---|---|---|---|---|
| **BE runner OFF** | 564 | **+$1,053.52** | 1.067 | Only positive config. Wins 4 of 5 quarters (Q1 still −$1,143.40/0.737); win rate 65.1%, avg loss −$80.19 (full stops). **Not placebo-gated** — post-hoc observation, not a validated edge. |
| Blocked hours OFF | 797 | −$2,537.35 | 0.862 | The CSV-fitted hour filter earns its keep: the 171 extra trades lose heavily (Q1 −$2,451.95). Removing it nearly triples the loss. |
| Sniper only | 400 | +$100.57 | 1.012 | Breakeven standalone. |
| Reclaim only | 391 | −$1,367.98 | 0.849 | Negative in 4 of 5 quarters and negative even in Josh's window (−$166.95, PF 0.872). |

The breakeven runner — the script's headline feature — is a net drag in
every comparison: its 138 exits scratch out at −$436 total, but disabling
it improves the year by ≈ +$1,956 (partly a different trade set: BE-off
trades hold longer and block 62 re-entries). This replicates, on a third
independent book, the house's earlier finding that breakeven management
hurts (Stormzy replay; Mitro study, −$320 → −$5,245).

## Diagnostics

- **Anchor vs fill:** Pine anchors all levels to the signal bar's close
  while filling at the next bar's open. Measured gap: mean −0.006 pts,
  mean |gap| 0.157 pts; **zero** of 626 fills landed beyond the stop or
  target. The anchoring quirk is immaterial on 15m MES — unlike the
  detector-price artifact in our own books, this is not where the
  TradingView result comes from.
- **Overnight tail:** only 48 of 626 variant-A trades crossed a CT date
  (max hold 1,260 min; none over a day), but they net **−$939.41** — the
  whole year's loss sits in the overnight-held minority. Variant B's
  flatten caps the mechanism, not the sign (its 36 date-crossing trades —
  evening entries flattened next day — still net −$331.81), and only 35
  trades ever reached a flatten (−$253.75).
- **Degraded-data days** (Databento-flagged, flagged not excluded):
  11 of 626 A trades touch one, netting −$438.92; excluding them the year
  is still −$463.11. No conclusion rests on degraded data.
- **Median hold** 45 minutes in both variants.

## Method and translation judgments

The Pine code governs; the parent's summary of it proved accurate on every
material point. Judgments I had to make, all documented in the workdir
status file:

- **Intrabar ordering:** within each 1-minute bar, stop before target
  (house eval_exit convention: gap through either level fills at the open;
  stop otherwise fills at stop − 2 ticks; target fills at level). Pine's
  own emulator, working from 15m bars, resolves these sequences by
  assumption — one reason TradingView's print differs from this walk.
- **Breakeven arming** applies from the minute *after* the +6.5 pt high
  prints (1-min bars cannot order intra-minute events; arming first would
  be the generous reading, and I took the conservative one).
- **strategy.exit is live from the fill minute** (TV stops positions on
  their entry bar).
- FVG boxes join the entry test from the bar after creation (Pine's loop
  order); the FVG displacement test uses the current bar's ATR, as coded.
- VWAP resets at the Globex trade-date boundary (17:00 CT); only the VWAP
  line feeds entries, and only via the reclaim setup.
- Indicator seeding replicates Pine (EMA from first close; RMA seeded by
  SMA-14). Entries require a valid ATR.
- `hour()` read on America/Chicago (CME exchange time), as the task states.
- One tie-break needed a ruling: when a snap exit and the variant-B
  flatten land on the same print, the flatten (hard rule) fills at the
  15:45 print. This affected exactly 1 of 642 B trades, by 0.25 pts.

## Validation

- 15m aggregation spot-checked against the raw CSV (exact OHLCV).
- The event-search walk was verified against an independent literal
  per-minute re-implementation: **626/626 (A) and 642/642 (B) trades match
  exactly** on exit price, exit minute, and exit reason; anchor and fill
  provenance asserted for every trade (anchor = signal-bar close,
  fill = next bar's first 1-min open).

## Bottom line for the decision

The script is a competent long-only pullback system whose signal timing is
genuinely better than random (95.5th placebo percentile) and whose
TradingView shine is a real but regime-bound two months. Over the full
year it does not cover house costs in either variant, it fails
quarter-stability (Q1 2026: PF 0.62–0.64), and its profit engine is a
single exit rule (snap-back) carrying a heavy hard-stop tail. The one
constructive lead — deleting the breakeven runner — is a post-hoc,
non-placebo-gated PF 1.067 with a −$1,143 quarter still in it; by the
standards applied to every other candidate this year, that is a
hypothesis for a future pre-registered test, not a finding. Nothing here
clears the bar the Mitro and passive-AOI studies were held to.

Artifacts: workdir `hidden_files/gemini-mes-elite-study-2026-10-26/`
(status.md with the full translation log; scripts prep.py / engine.py /
run_study.py / placebo.py / validate.py; outputs summary.json, placebo.json,
per-config trade CSVs).
