# Task 012 addendum — Richesse and Clark builds (Claude, 2026-11-02 15:00 CT)

Josh dropped two newer Hurricane files after the fill audit: `hurricane_v3_mars_richesse.py` (1,639 lines) and
`hurricane_v3_mars_clark.py` (2,004 lines). Both copied into `2026-11-02-hurricane-v3/`. Line refs below are to those copies.
Not attached, so not audited: `hurricane_cmdlog.py`, `hurricane_heads.py`, `broker_hub.py`, `hurricane_hotkeys.py`.

## 1. Nothing from the fill audit was applied

Both files carry the Mars execution model unchanged: position created on the signal second at `po['lim']` with
`fill_delay_secs=1` hard-coded (richesse `:1169-1172`, clark `:1169-1172`); every sell `math.ceil`-rounded (richesse
`:975`, clark `:975`) and at EOD; `OSC_PROFIT` on `high >= target` with no volume; PFMACD reads the in-progress 6-s
bucket; windows are print-count; the `ceil(0.30/0.05)=7` float bug and target-below-bid entries are still there.
Defects 1–9 of the audit stand verbatim. The 11-02 `hurricane_v3_mars_fills.py` drop still applies to both; the
decision rule in §4 of the audit is unchanged.

## 2. Richesse = Mars + SPX cone + live Databento runner

- The fast per-strike cone is replaced by an "SPX cone" for direction (`_entry`, richesse `:737-746`). In the
  **backtest** path `self.spx_cone` is never constructed (`getattr(self,'spx_cone',None)` → None), so `cone_blended`
  stays 0.5 and `dual_agree` is never true. In the **live** path (`LiveMarsEngine`) it exists and is fed
  `tick_1s(spx, spx, spx)` from 5-min SPX updates plus `strike ± premium` estimates from near-ATM option prints
  (`on_bar`, richesse `:1399-1411`) — a proxy, not SPX. Backtest and live therefore compute different signals; a
  backtest result is not evidence for the engine that would trade.
- `LiveMarsEngine.on_bar` sets `st.position` the instant `_entry` returns and logs `entry_filled` at the limit
  (`:1403-1421`). Shadow mode is **assuming** fills at a price 1–4 ticks under the last print, not observing them.
  Exits go through the same ceil-rounded `_monitor`. The shadow P&L will reproduce the backtest artifact live.
- `StreamingPFMACD` flushes every 6 *records*, not 6 seconds (`:1263-1267`) — on a busy chain that is a fraction
  of a second; Clark's own OIVW docstring says so (`clark.py:1442-1444`). Live PFMACD ≠ backtest PFMACD.

## 3. Clark = Richesse + cbbo-1s + OI-VWMACD + Band D + $1.55 commission

- cbbo-1s is subscribed and per-strike bid/ask/size kept in `ClarkState.quotes` (`:1289-1298`), but by its own
  banner it is "recording only — gates nothing" (`:1963`) and **it is not used to price fills**. The one dataset
  that answers the fill question is streamed to `clark_samples.csv` while the engine keeps filling off the trade tape.
- OI-VWMACD (`:1429+`) = Σ(bid_sz·bid − ask_sz·ask) calls − puts on a real 6-s clock. Observation-only by default
  (`--oivw-gate` to gate). Not audited further; no fill relevance.
- Band D $1.05–$3.00 with a $300/head cap (`:311, :382`). Raises per-trade exposure 3× on an engine whose WR is a
  harness property.
- Commission $1.55/fill = $3.10 RT (IBKR-measured). Correct, and it costs $1.80/trade more than the series
  assumed. Spread still zero.
- `--live` wires `BrokerHub(IBKRPort(port=7497, market_exits=True…))` + `HeadRunner` (`:1966-1980`). Default port
  is TWS paper. The order-side code isn't attached; what *is* visible is that the decision engine feeding it is
  the unfixed one.

## 4. Recommendation (unchanged, plus one)

1. Do not run `--live`, paper or otherwise, until the catalog run (`07_backtest_mars_fills.py`) is read.
2. Clark already holds the bid/ask per strike per second. Price shadow fills from it: buy at ask (or at `lim` only
   when `ask <= lim`), sell at bid. ~30 lines in `LiveMarsEngine.on_bar`; Claude will write it on receipt of
   `hurricane_cmdlog.py` / `hurricane_heads.py` / `broker_hub.py`. That turns the shadow log into priced fills.
3. Make backtest and live compute the same signal — either feed the backtest an SPX cone from real SPX/ES 1-s, or
   remove it from live. Same for PFMACD bucketing (wall-clock in both).
