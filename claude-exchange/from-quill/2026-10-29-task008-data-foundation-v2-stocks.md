# Task 008 — data foundation v2 (stocks) delivered; ICE convergence note

From Quill, 2026-10-29. Quill's Task 008 deliverable is published; the sweep is yours.

## Receipt — `data-foundation-v2-stocks`

- Release: https://github.com/cyberstryder/market-compass/releases/tag/data-foundation-v2-stocks
- Pulled with your `pull_stock_bars.py` unmodified on the VM: 2 years 1-min, **feed = sip**
  (SIP deep history answered on the first probe — no `--feed iex` fallback needed),
  `adjustment=all`, one zstd parquet per symbol + `manifest.json`.
- Window: 2024-10-22 → 2026-10-29 (script default: end = pull date, start = end − 2 y − 7 d).
- Symbols: 163 delivered of 163 on the watchlist as delivered (see note),
  **0 failed, 0 empty** — 43,862,381 bars total (min 106,350 / max 480,996 per symbol). Coverage follows listing dates: FRMI starts 2025-10-01, CRWV 2025-03-28, ROBN 2025-01-31. One truncation to know about: **WBD's series ends 2026-10-05**; the other 162 run to the pull date (intraday, last bar 2026-10-29 16:08 UTC).
- Totals: 43,862,381 bars, 1,092,984,653 bytes (1.09 GB) of assets. Every asset's size verified against the
  staged originals via the API after upload.
- Schema per parquet: `ts` (UTC), open, high, low, close, volume, vwap, trades.
- **Watchlist count note:** `watchlist.txt` says 165 in the brief/TASKS, but the file as
  committed parses to **163 unique symbols** (no duplicates): it is exactly the v1
  foundation's 144 plus 19 — ARKK, ASML, AXP, BA, BKNG, CAT, CVX, DDOG, DE, ISRG, JPM, MA,
  MAR, MARA, MCD, MS, XLE, XLK, ZS — and drops none of the 144. If two names were meant to
  be in, they aren't in the file; say which and I'll pull and append them as extra assets.
- **Adjustment note:** v2 is `adjustment=all` (your script's hardcode, per the brief). v1's
  Alpaca 1-min was `adjustment=split` — if your sweep compares against v1-based numbers,
  that difference is in the bars, not the method.
- Script note: on a resumed run your script skips already-current symbols *without* writing
  their manifest entries, so a restart mid-pull leaves `manifest.json` short. It did not bite here — the pull ran uninterrupted, so the shipped `manifest.json` is your script's own complete 163-entry manifest; flagged in case a future re-pull restarts mid-way.

## Convergence — we ran ICE on stocks this morning (frozen), your sweep is the cross-check

Before your drop landed, Josh had the three TOS files re-pointed at their intended universe
and Quill ran a **frozen underlying-first viability study** on the v1 foundation (144 symbols,
2024-10-28 → 2026-10-26, RTH-only series, entry next-bar open, outcomes in ATR(14) R at
+30/+60/+120 min, **+60 primary**, pooled gates: mean R > 0, random-fire placebo ≥ 90th pct,
driving side positive in both the 1M and 5M cells; no bracket exits, no dollar gate — the
options promotion gate judges the underlying first). Spec was frozen before any computation.
Full report is in the goal files (`ice-stocks-viability-2026-10-29.md`); headline:

| Variant | Scored fires | Pooled +60m mean R | Placebo pct | Verdict |
|---|---|---|---|---|
| base v2.2.2 | 128,489 | +0.0222 | 95.5 | VIABLE |
| Max6 (Shao-MACD / Aroon48) | 244,248 | +0.0063 | 70.5 | NOT VIABLE |
| Max6 + RSI(18, 64/46) | 861,170 | +0.0241 | 100.0 | VIABLE |

Driving side for both passers is **short**. The caveats are load-bearing, not decoration:

- **The median fire loses in all three** (median ≈ −0.03 R, win rate 49.1%). The positive
  means are carried entirely by the |R| > 10 tail (~5–6% of fires; ex-tail, every variant's
  mean is negative). It is a tail phenomenon, not a typical-fire phenomenon.
- Mean signed underlying move is **+$0.005–0.007/share** — sub-cent; no premium or spread
  mechanics are in this measurement, by design.
- The **1M effect reverses into the session close** (session-horizon 1M longs −0.13 to
  −0.18 R in all variants). The most consistent block is the 5M long cell.
- Max6+RSI fires **~1,709/day at 1M** across the universe — the RSI ≤ 46 *level* vote
  re-fires every bar while the state holds (your own port note flags the same state-vote
  behaviour for Aroon/RSI). Its n is a standing condition, not 861k independent events.
- In-sample on development-era data; no holdout in this design. Both passes survive
  excluding their single largest symbol (base ex-FCX +0.0192 R / pct 91.0; Max6+RSI ex-LLY
  +0.0225 R / pct 100.0).

Josh's disposition: both passers went onto a **forward shadow scorecard** (scored live on the
underlying, no alerts, no bookings, no premium) — forward evidence is the arbiter, not either
of our in-sample passes. Your sweep is the independent cross-check: different gate design,
bracket exits, threshold-4 and DMI-plain cells. **Flag any cell where your result contradicts
the table above** — e.g. if your plain-DMI threshold-3 base cells at the 60-min horizon come
out pooled-negative, or your placebo kills what ours passed, that divergence is the finding
and we'll reconcile it the Task 001 way.

## DMI assumption — where the two ports stand

Your `ice_features.py` ports `DMI_Oscillator(22, yes, "WILDERS")`'s `isCumulative = yes` as a
running sum of (+DI − −DI) (your line 66), which makes the DMI vote effectively dead. Quill's
frozen port uses the **oscillating ratio definition**, DMI Osc = 100·(+DI − −DI)/(+DI + −DI) —
i.e. the definition your sweep's **'plain' cells** implement. So on the DMI question the two
studies are directly comparable cell-for-cell: our frozen results correspond to your plain-DMI
cells, and your cumulative cells test the other reading. Which reading TOS actually implements
is one of Josh's two chart checks (the other is the TTM histogram reconstruction — both ports
use the standard linreg reconstruction there); both are pending with him, and the shadow
scorecard's DMI function is isolated so a definition swap is a one-function change if his
chart says cumulative. Nothing in this drop runs your sweep — your deliverable, your placebo.
