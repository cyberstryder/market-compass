# Hurricane v3 (SPX 0DTE far-OTM scalper) — fill-model audit + honest-fill harness (Claude, 2026-11-02)

Source: Josh's six files, pasted 2026-11-02 — `hurricane_v3_{moonraker,mercury,venus,earth,mars}.py`,
`07_backtest_mars-richesse.py`. Not in this repo; the Mars module and runner are reproduced in
`2026-11-02-hurricane-v3/` with the fixes applied. Data: Databento OPRA 1-second trade bars on SPX 0DTE
(`catalog_may1/clean_0dte`), not on the desk — **nothing here has been run on the real catalog**; the only run
was a synthetic smoke test to prove the harness executes. Reported series results (Moonraker 1031 t / 64% WR /
$24.8k → Mercury 68.5% → Venus 72% → Earth / Mars) are Josh's, from the original harness.

## 1. Verdict

The backtest fills orders the market never offered. Earth and Mars — the two builds Josh is choosing between —
removed the only fill check the series ever had, and every rounding and sequencing choice leans the trader's
way. The 64–72% WR and the "100% WR" exits are properties of the harness. Earth vs Mars is a comparison between
two artifacts until the fill model is replaced. Same failure class as the 10-24/25 futures replays (fills at
level prices, exits before entries) and the 10-26 0DTE finding (premium-path scoring).

## 2. Defects, ranked by P&L impact (file:line against Josh's originals)

1. **Instant fill below the market (Earth, Mars).** `_entry` returns `lim = ceil(open/tick)·tick − 1 tick − cone_ticks`
   (1–4 ticks under the last print). `run_backtest` creates the position on the **same second** at `po['lim']`,
   `fill_delay_secs=1`; `fill_ts` is computed and never used (`mars.py:1139-1166`). Moonraker/Mercury/Venus at
   least required `low <= lim <= high and volume >= 3` on a later bar (`pending` dict, `moonraker.py:432-467`).
   A buy one tick under the last trade on a $0.05-tick option is a bid through the bid; in reality it fills only
   when price is falling through it. Here it fills 100% at the best price.
2. **No bid/ask spread anywhere.** Trade prints serve as both buy and sell price. $0.05 wide on a $0.15 option
   = 33% round trip; on $0.05 = 100%. Commission ($0.65) is modeled; the spread, 10–50× larger, is not.
3. **Sells always rounded up.** `exit_px = ceil(exit_px/tick)·tick` on every exit (`mars.py:941`) and at EOD
   (`:1179`). `OSC_PROFIT` fills at `exit_target` on `high >= target` with no volume — assumed counterparty on
   the high print of a sparse 1-s bar. `FORCE_EXIT` rounds up unless the last 3 prints are monotone down.
4. **Profit-gated exits are tautologies.** `CONE_EXIT` requires `gain >= 0.05`; `FLOW_EXIT` and `TRAIL_EXIT`
   require `is_profitable`. They can only fire on winners. "100% WR CONE_EXIT" measures nothing about the cone.
5. **SPX is fabricated in the Mars runner.** `07_backtest_mars-richesse.py:62-83` loads Jan-2025 SPX 5-min bars,
   rescales to the May-2026 open, and maps them onto `sorted(dates)[0]` only. Other days fall back to the static
   `atm_map` open. Venus velocity, `real_dist`, `toward_strike` — the gravity thesis — have not been tested
   against SPX. VIX is pulled from yfinance *today* for a historical run.
6. **PFMACD look-ahead (all builds).** `get`/`get_state` return bucket `t <= t_sec//6`, the 6-s bar in progress
   — up to 5 s of future flow. Should be `t < t6`.
7. **"Last 30 seconds" = last 30 prints.** `volumes`, `prices`, `price_history` append only on bars that exist;
   far-OTM strikes are sparse, so `vol_last_n_secs(30)` spans minutes and `bail_secs` compares a 20-print window
   against a 20-s clock. The volume-pulse gate is much weaker than the docstring says.
8. **Silent indicator changes between builds.** Earth/Mars `StrikeDeltaWave.update` sets `prev_close=close`
   *before* computing reach, so it measures `high−close`/`close−low` of the current bar, not reach vs the prior
   close (Moonraker). `geiger_zone` changed from range-break to 80th-percentile position. `StrikeCone.predict`
   is a different formula (tanh nudges). Planet-to-planet WR comparisons are confounded.
9. **Found while testing the patch, present in Earth and Mars:** (a) float noise — `math.ceil(0.30/0.05)` = 7, so a
   0.30 print produced a 0.35 buy limit *above* the market, filled instantly, and the 0.30 target then "hit" for a
   guaranteed loss; (b) no room check — `exit_target = lo + 0.8·range` is often at or below the limit; the old
   code booked those as instant `OSC_PROFIT` at zero gross minus fees.
10. Cosmetic: runner prints "$1000" start and `1_000_000 + tp` end; saves to `*_earth_*` filenames; conviction
    fractions / OI cap / `max_deploy_per_trade` are dead config (`_size_earth` caps at 2, `_size_mars` at 1).

Josh's description — "sell order at 15:04:02, filled 15:04:03" — is not in this code; exits execute on the signal bar.

## 3. What the drop does (`2026-11-02-hurricane-v3/`)

`hurricane_v3_mars_fills.py` — Mars signal logic untouched; execution replaced:
- Entry = resting buy limit; fills on a **later** bar only if a print went below it (`low < lim`; `--relaxed` =
  `low <= lim and volume >= 3`); cancelled after 300 s; `entry_fill_delay` recorded.
- Exit decided at bar t, **executed on the next bar with a print** at that bar's low (bid-side proxy; `--exit-px close`
  softer), rounded **down**, minus `--slip N` ticks. `OSC_PROFIT` = resting sell limit, fills only on `high > target`.
- PFMACD lagged to the completed bucket. Volume gate / stop reference / bail wall-clock.
- SPX used only for dates that intersect the bars; no remap, no rescale; missing days stamped `spx_source=static_open`.
- `placebo_rate=p`: skips every signal gate (vol pulse, cone, pct, slope), random accept at p; same bands, SPX
  distance gate, sizing, fill model, exits. Runner runs REAL then 3 placebo seeds at the matched entry rate.
- Trades carry `profit_gated`, `entry_fill_delay`, `exit_fill_delay`, `exit_slip_ticks`, `signal_price`, `exit_kind`.
- Fixes 9(a) and 9(b) (`no_room` counter).

`07_backtest_mars_fills.py` — runner: no yfinance, no SPX fabrication, exit table split by profit-gated,
REAL-vs-PLACEBO summary. `mars_fills.diff` — unified diff against Josh's `hurricane_v3_mars.py`.

Smoke test (synthetic random-walk premiums, 1 day, 26 strikes, 152k bars; **not** Josh's data): 771 orders placed,
204 filled (26%), median entry fill delay 127 s, fills typically 1–2 ticks under the signal print minutes later
(adverse selection as expected); CONE_EXIT WR fell from 100% to 58% once the sell had to execute on the next bar.
Every exit path fired; CSVs written. Runtime ≈ 4.5 min per pass per day at 26 strikes (pure-Python 1-s loop).

## 4. Decision rule for the real run

Run `python3 07_backtest_mars_fills.py --catalog ./catalog_may1/clean_0dte`. Read, in order:
1. **Orders FILLED % of placed.** Under ~40% ⇒ the remaining fills are the ones the market fell into.
2. **REAL WR − PLACEBO WR** and avg $/trade (not net; placebo trade counts differ). Inside ±3 pts ⇒ the WR is
   the fill model, not the signal.
3. Exit table **excluding** profit-gated exits; gross ticks per trade vs 1-tick spread + $1.30 fees.
Expectation from the desk's fill-correction history: Moonraker→Mars goes negative at step 1.

## 5. Not done
- BBO-based fills (Databento OPRA `cbbo-1s` / `mbp-1`: buy ask, sell bid) — the definitive version; needs a data pull.
- Real SPX/ES 1-s for the catalog dates (Databento GLBX ES as proxy) so the Venus gate and gravity claim can be tested.
- Earth variant of the harness (sizing/band differences only); trivial once Mars is read.
- Multi-day catalog run; placebo rate-matching by trade count rather than entry rate.
