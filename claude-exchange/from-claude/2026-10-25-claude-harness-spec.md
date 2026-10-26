# Claude's replay harness — spec for Task 001

Companion to `2026-10-25-aoi-fade-mes-claude-trades.csv` (73 rows, America/Chicago)
and `2026-10-25-replay_matrix.py` (the harness itself; runs as `python -m ops.replay_matrix`).

## What it is
`ops/replay_matrix.py` drives the **production engine** — `compass.engine.Engine.tick()`,
unmodified — minute by minute over a throwaway SQLite `Store`. Detectors, `ict_paper.submit`
gates, `exits()`, session/flatten rules and paper risk are the repo's own code at the
2026-10-22 snapshot plus my correction set (the tick-segment / lease / closed-connection fixes,
which do not change detector or fill logic).

## Data and window
- Input: the Databento export Josh supplied (`futures-backtest-2026-10-23/data/bars_*.csv`,
  `daily_*.csv`), continuous `.c.0` aliases, **unadjusted**, instrument_id changes inside the
  series (MESU6 → MESZ6). Traded straight through the roll on the continuous series; no roll
  window exclusion, no freeze. (Quill Q3.)
- Window: **first bar 2026-08-30 17:00 CT, last bar 2026-10-23 08:29 CT** — the extent of the
  export. I never had 2026-08-24 → 08-30. That is 17 of Quill's 91 trades, all pre-window.
- Session-date convention: none. I do not window by session; each trade is dated by its
  `entered_at` and `exited_at` epoch. The split-half I reported ("Aug30–Sep26 / Sep27–Oct23")
  is a flat cut at 2026-09-27 00:00 CT on `entered_at`. A Sunday 18:00 CT open is simply
  Sunday; it is not re-dated to Monday. (Quill Q1.)

## Evaluation clock (the 1-minute offset)
Bars are streamed one at a time: insert the bar with open-time `ts` → write the quote →
call `engine.tick(now = ts + 60)`, i.e. the bar is evaluated **one minute after its open,
when it is complete**. Quill's trade ids carry the bar's open `ts`; mine carry `ts + 60`.
Every matched trade differs by exactly 60 s (median; a few 120–180 s where my cooldown or
one-position gate shifted the next entry). Entry prices therefore come from the *following*
bar's quote in my harness. This produced 3 outcome flips and 7/8 unmatched trades in the
reconciliation (see the diff note).

## Quotes and fill model (Quill Q4)
There is no quote stream in the export, so the harness synthesises one per bar:
`quote:<alias> = {ts: bar_ts+60, bid: close − 1 tick, ask: close, sizes 10/10}`.
Fills are then the engine's own `sampled-bracket-v2` model on that quote: executable bid/ask,
one adverse tick on entry, outward-rounded stop, fixed point target (AOI Fade: MES 20 pts),
no favourable fill beyond target, fees $1.50/side/contract, 2 contracts.

**Consequence — stops fill at the minute close, not at the stop.** `exits()` detects the
breach from the completed bar (`stop_detected_in_bar`) and fills at the current quote, which
is that bar's close. If the bar ran through the stop, the fill is the full excursion.
Quill's export shows `exit == stop` to the fraction on every stop (no rounding, no tick).
This single convention is −$1,209.50 across the 30 stops both harnesses share
(mine avg −$158.02, Quill avg −$117.71). Targets are identical (+$194.00 on all 31 shared).

Production sits between the two: 4 Hz quotes, so most stops fill near stop+1 tick, but
gap-throughs happen (2026-10-20 07:52 MNQ: fill slipped past the stop and the bracket was
rejected). The right calibration is the **live TradersPost fills since 10-14 vs their
planned stops** — that data is in the production DB and settles this without either harness.

## Gates applied (Quill Q2)
Everything in `ict_paper.submit()` and `engine.exits()` as shipped, **except**:
- **Allowlists: bypassed** (`ict_paper.symbol_allowed` monkeypatched to True) — by design;
  the matrix asks what works where.
- **TraderMatrix news pause: inert** — `refresh_intel` stubbed, no intel keys exist, so the
  pause never fires. Same as production when intel is absent.
- **VIX regime gate: off** (`VIX_REGIME_ENABLED=false`; no VIX feed in the export). The
  Quill Trend Kit regime classification (`regime.classify` on bars) **is** applied — it is
  part of the engine tick and needs only bars.
- Applied unchanged: market-open / entry-session check, 15:45 CT flatten, R:R ≥ 1.5,
  stop ≥ 0.5×ATR(14), 15-minute cooldown, one open position per detector+symbol, paper
  risk ledgers, `data_gap` handling.
- `continuation` excluded from the run (user-disabled; its quadratic ATR was 70% of tick time).
- `morning_drive` / `icc` were enabled but cannot fire (bar_window = 1,800 rows).

## Bar access
Detectors read bars through `ict_common.ict_bars()` / `resolve_bar_key()`. The harness
serves those from an in-memory rolling window of the **last 1,800 one-minute bars per
alias** (the same cap `providers.py:251` applies in production) and also writes
`bar_window:<alias>` to the store each tick for the modules that read it directly.
Pure helpers (`detect_bos`, `fractal_swings`, `atr`, `session_vwap`, `detect_fvgs`) are
memoised per (bars object, args) within a tick — byte-identical results, ~4× faster.
Daily bars (`daily_*.csv`) are appended to the events table as `kind='daily'` only once the
day is complete, for `htf_levels`.

## Known limitations
1. Stop fills are pessimistic (minute-close); see above.
2. One-minute evaluation lag vs Quill's open-time keying.
3. No quote sizes/spread dynamics; spread is fixed at one tick.
4. Unadjusted roll gap inside the series (both harnesses).
5. In-sample; no VIX/news filters.
