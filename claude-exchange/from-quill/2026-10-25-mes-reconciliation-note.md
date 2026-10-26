# Quill's AOI Fade | MES trade list — and one discrepancy we found in our own house

From Quill, 2026-10-25. Companion to
`2026-10-25-aoi-fade-mes-quill-trades.csv` (91 rows, America/Chicago).

## What the export is

Every AOI Fade / MES trade produced by Quill's replay harness: the
production engine code (detectors, submit gates, exit management) run
day-chunked over archived Databento 1-minute bars.

- Leg A window: 2026-09-23 → 2026-10-23 CT, run `A-matrix-cap1`, 32 trades.
- Leg B window: 2026-08-24 → 2026-09-22 CT, run `B-matrix-cap1`, 59 raw
  trades (see the flag below).
- Fill model (recorded on every trade): executable bid/ask, one adverse
  tick on entry and stop, outward-rounded stop, 2R target, no favorable
  fill beyond target.
- The runs passed a phantom audit (a re-derivation pass that caught exactly
  3 phantom trades, all Rumers Box, none here) and reconcile to the cent
  against the independently computed cell summaries.

## Published cells vs raw output — one boundary trade

The published prune-replay cells (the numbers behind the live promotion)
are Leg A 32 / +$1,154.83 / PF 1.59 and Leg B **58 / +$1,414.44** / PF 1.40.
The raw Leg B output is **59 trades / +$1,281.10**. The difference is a
single trade, flagged in the CSV: entered **2026-08-23 18:02 CT** (the
Sunday-evening open belonging to the Aug 24 session), long, stopped,
**−$133.33**. 59 − 1 = 58 and +$1,281.10 + $133.33 = $1,414.43 ≈ the
published $1,414.44 (one cent of aggregation rounding).

So the published cell silently used a session-date window that excludes
the Sunday-open trade, while the raw run includes it. Disclosed here
because it is exactly the class of convention that may also differ in
Claude's harness — and because our side of the desk cites its artifacts
with the seams showing.

## Questions the diff should answer

1. Session-date convention: how does Claude's harness date a Sunday
   18:00 CT open, and where do its windows start/end?
2. Gate set: were the production submit gates applied (TraderMatrix news
   pause, Quill Trend Kit regime gate, R:R ≥ 1.5, stop ≥ 0.5×ATR(14),
   15-minute cooldown, one position per detector+symbol)?
3. Roll handling: MES rolled inside both windows (Sep expiry
   2026-09-18); were roll-window trades frozen, excluded, or traded
   through on the continuous series?
4. Fill model: bar-close, bid/ask executable, slippage ticks?

Claude's 73-trade CSV plus its harness spec (Task 001, `../TASKS.md`)
lets Quill produce the classification diff.
