# Close Momentum detector — shadow build for Quill to wire (Task 007)

Claude, 2026-10-28. Josh: "come up with a strategy that can be automated … up to 5 micros or a full NQ/ES."

## What it is
The one signal from the year test with cross-instrument sign stability and full-size economics:
first-30-minute direction agreeing with day-so-far direction at 15:00 CT → hold to the 15:45 flatten.
Year test (`2026-10-27-futures-method-search/fullsize_results.txt`): **YM +$55/trade, +$8.1k/yr, t 3.2,
both halves positive, max DD −$1,115, worst day −$674**. CL regime-bound (one quarter), ES marginal,
**NQ −$82/trade — never add NQ/MNQ**. MYM is the same signal at $2.90/trade (fees), 5 MYM ≈ $14.70.

## The build
Branch **`close-momentum-shadow-2026-10-28`** (pushed; patch beside this note, 559 lines, applies on main `0104e60`).
- `compass/close_momentum.py` — `signal()`, `trade_levels()`, `daily_atr()`, `scan()`, `display()`. Follows aoi_fade's
  shape: throttled scan, one submission per symbol per session, declines recorded via `ict_declines`.
- Wiring: `STRATEGY_TAGS['close_momentum']='ict-close-momentum'`, `DETECTOR_FLAGS`, `DETECTOR_SYMBOL_ALLOWLIST={'YM','MYM'}`,
  **`PAPER_ONLY_DETECTORS` (+close_momentum)**; config `ICT_CLOSE_MOMENTUM_ENABLED` (**default false**) and
  `ICT_CLOSE_MOMENTUM_SYMBOLS` (default `YM.c.0,MYM.c.0`); engine scan hook + health name; `/api/ict-close-momentum`; signal label.
- Tests: `tests/test_close_momentum.py` (14) + `test_paper_only_carve_out.py` exact-set updated. Paper-path files
  (ict_paper, aoi_fade, turtle_soup, paper_risk, carve-out, close_momentum): 133 passed, 10 skipped.
  `test_alpaca_option_stream::test_fallback_symbols_dynamic_ladder` fails on main before my change (pre-existing).

## Conventions I had to choose — check these
1. **Bracket.** The edge has no stop/target; `submit()` requires both and ≥0.5×ATR(14,1m). I attach stop 0.25×dailyATR(14)
   (≈ $740 YM, hit once in the year, −$59 on the year's result) and target 0.5×dailyATR (never hit). Daily ATR from the
   `'daily'` store (`db.recent(c,'daily',sym)` — same read as apex_magnet); fallback point table if absent.
2. **Entry timing.** Fires 15:00:00–15:05:00 CT off the 15:00 bar's open (available as the bar starts). `futures_session`'s
   index halt is 15:15–15:30 so 15:00 is open; `entry_end` isn't enforced in submit. Confirm.
3. **Quantity.** `submit()` books a fixed 2-contract leg. The test year was priced at **1 YM**. At 2 YM: $16k/yr, DD −$2.2k,
   worst day −$1.35k — Josh's daily-loss limit decides. If 1 is wanted, that's a submit() change I didn't make.
4. **Data.** Needs `bar_window:YM*` and `quote:YM*` — YM.c.0 is not in `ICT_SYMBOLS` today and I don't know the Databento
   subscription covers YM. If not, run MYM first (same signal, $2.90/trade — proves the mechanics, not the money).
5. **Cooldown/news gates** apply as to every detector; a news pause at 15:00 just skips the day.

## Ask
Review the five conventions, merge to main if acceptable, enable in shadow (`ICT_CLOSE_MOMENTUM_ENABLED=true`) on YM or MYM.
Promotion stays under the adopted gate (≥50 Tier-1 fills, p90 slippage survival). I'll reconcile paper vs. replay weekly.
