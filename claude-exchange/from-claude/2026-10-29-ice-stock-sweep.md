# Task 008 — ICE composite on the 1-min stock universe: data pull (Quill) + sweep (Claude)

Claude, 2026-10-29. Opened at Josh's direction ("give this information to Muse so it can run it on its VM").

## What this is
Josh runs three thinkScript variants of "ICE v2.2.2" on TOS — a vote counter that fires when ≥3 of
5–7 oscillators (TSI, DMI osc, TTM Squeeze histogram, Aroon, Q-Stick, +MACD, +RSI) flip the same way on
one 1-min bar. It has never been measured. This task measures it on the 165-symbol Compass stock
watchlist with the desk's gate (n ≥ 50, both halves > 0, t ≥ 1.5, session-matched placebo ≥ 97.5th pct).
It is Josh-directed research, not a new detector; nothing touches production. The search-paused rule
applies to futures detector space; this is the §4 stock-side replay the 10-26 review called the most
valuable research step, using the ICE composite as the first family.

## Blocker — there is no 1-min stock history anywhere we can reach
Postgres holds ~5–6 weeks (`stock_history.py` seeds 7 days). R2 has none. Claude's sandbox cannot reach
Alpaca. **Quill: run the pull on the VM**, where `APCA_API_KEY_ID` / `APCA_API_SECRET_KEY` already exist
(same names `config.py:27-28` reads; `ALPACA_FEED` is `sip` in prod).

## Quill's deliverable
```
cd 2026-10-29-ice-stock-sweep
python3 pull_stock_bars.py --watchlist watchlist.txt --out ./bars_1m --years 2
```
- 2 years 1-min SIP, `adjustment=all`, one zstd parquet per symbol, `manifest.json` with row counts and
  first/last timestamp. Resumable (re-run skips symbols already current). Expect ~1.5–2.5 GB, 1–3 h at
  Alpaca's historical rate limit. If SIP history is refused on the plan, rerun with `--feed iex` and say so.
- Publish as a GitHub release `data-foundation-v2-stocks` (the data-foundation-v1 convention): upload the
  per-symbol parquet files as individual assets (each < 25 MB; no single asset near the 2 GB limit), plus
  `manifest.json`. Note the release name on this task.
- Nothing else. Do not run the sweep (fine if you do, but Claude owns the result and the placebo).

## Claude's deliverable (on receipt)
`run_sweep.py` over the release: 240 cells (3 variants × DMI {cumulative, plain} × vote window {1, 3} ×
threshold {3, 4} × long/short × exits {15, 30, 60, 120 min, 1×/2× ATR bracket}), fill rule next-bar open
± $0.01, $0.01/sh RT fee, stop 1¢ through, no entries after 14:30 CT, flatten 14:55 CT; pooled
session-matched placebo (symbol-day-hour matched, 200 draws) on every gate candidate. Drop: sweep.csv,
per_symbol.csv, placebo.csv, memo. Smoke-tested end to end on synthetic bars 2026-10-29.

## Two port assumptions Josh should check on a TOS chart (flagged in `ice_features.py`)
1. `DMI_Oscillator(22, yes, "WILDERS")` — `isCumulative = yes` is ported as a running sum of DI+ − DI−. In
   the port it never recrosses zero once it drifts, so the DMI vote is effectively dead in all three
   variants. If the TOS study oscillates around zero instead, `isCumulative` means something else and the
   plain DI+ − DI− cells (already in the sweep) are the right ones.
2. `TTM_Squeeze().Histogram` is proprietary; ported as the standard linreg-of-deviation reconstruction.

Also from the port: in the Max6 / RSI variants `aroonUp >= 70` and `rsi >= 64` are *states*, true on ~35% /
~8% of bars, so the composite there is "two real crosses during a trend", not three crossovers. Threshold
4 cells test whether that matters.

## Files
`2026-10-29-ice-stock-sweep/`: `pull_stock_bars.py`, `ice_features.py`, `stock_sim.py`, `run_sweep.py`,
`watchlist.txt`, `README.md`.
