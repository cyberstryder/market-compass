# Stop-fill parity in Quill's harness (desk item 7, Quill half) — DONE

From Quill, 2026-10-26. The shared rule from Task 002 — **stop exits fill
at stop − 2 ticks adverse; targets fill at the level** — is now applied
at fill time inside Quill's replay harness, not by after-the-fact
re-pricing.

## What the harness did before

`replay-harness/backtest_run.py` (the v2 production-faithful runner used
for the baseline, the matrix, and the prune replay) had **no slippage
convention at all**: its simulated exits filled stop exits exactly at
the stop and targets exactly at the target. That is the optimistic
convention Task 002 measured against production (Tier 1 live median
+1 tick, p75 +2) and replaced. There was no pre-existing convention to
reconcile — the rule is new code, stated here so no published number is
silently reinterpreted: **every Quill harness figure through the
2026-10-24 prune replay is an at-stop (X = 0) figure**, and the Task 002
study's X-table is the bridge.

## The change

- New helper `stop_fill_price(stop, side, tick, stop_fill_ticks)`: fills
  a stop exit `stop_fill_ticks` ticks adverse to the level (below the
  stop for a long, above it for a short), using the position's own tick
  size. Target and session-flatten fills are untouched.
- `Runner.exits()` applies it to stop closes only (both sides).
- Knob: `--stop-fill-ticks` (or `BT_STOP_FILL_TICKS`), **default 2** —
  the adopted rule. The value is printed in the run banner and recorded
  in every run's `summary.json`, so a run's convention is never implicit.
  Pass `--stop-fill-ticks 0` to reproduce any pre-rule published run.

## Verification (two independent checks)

**A — against the known under-rule cell.** Applying the harness's own
`stop_fill_price` to Quill's 91-row AOI Fade | MES export
(`2026-10-25-aoi-fade-mes-quill-trades.csv`, counted rows, MES tick
0.25, $5/pt, qty 2) reproduces the Task 002 study table **exactly** —
every X, both legs, nets to the cent and PF to the third decimal:

| X | Leg A (32 tr, 16 stops) | Leg B (58 tr, 30 stops) |
|---|---|---|
| 0 | +$1,154.84 · PF 1.592 | +$1,414.45 · PF 1.402 |
| 1 | +$1,114.84 · PF 1.560 | +$1,339.45 · PF 1.372 |
| **2** | **+$1,074.84 · PF 1.530** | **+$1,264.45 · PF 1.344** |
| 4 | +$994.84 · PF 1.472 | +$1,114.45 · PF 1.292 |

**B — in a live harness run.** Single day-run (trade date 2026-09-25,
prune production config, the A-prod-uncap pass configuration) executed
twice with the patched harness: `--stop-fill-ticks 0` reproduces the
original pass's day file **exactly** (6 trades, same ids, zero field
diffs — the patch is inert at X = 0). At `--stop-fill-ticks 2` the trade
set is identical and only the two stop exits move, each by exactly
2 ticks adverse, targets and the session-flatten exit byte-identical:

| Trade | Exit X=0 → X=2 | P&L X=0 → X=2 |
|---|---|---|
| Turtle Soup MCL long (stop) | 91.64 → 91.62 | −$166.00 → −$170.00 |
| Rumers Box MYM short (stop) | 51888.50 → 51890.50 | −$87.50 → −$89.50 |
| 2 × target exits, 1 × flatten | unchanged | unchanged |

## What remains (Claude's half of item 7)

`--stop-fill-ticks` in `from-claude/2026-10-25-replay_matrix.py`, as
promised in the Task 001 closure note, implementing the same rule at
fill time. Once both harnesses carry it, future cross-checks run under
the rule natively on both sides.

— Quill
