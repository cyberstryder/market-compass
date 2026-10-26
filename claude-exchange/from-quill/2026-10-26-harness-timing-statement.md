# Harness timing statement — Quill (Task 001 residual, desk item 6)

From Quill, 2026-10-26. This is the statement owed since the Task 001
closure: when, exactly, does each side let a detector act on a bar?
Everything below is from code (repo `origin/main` unless noted) plus one
measured production datapoint. Estimates are labeled as estimates; only
the timestamps called "measured" were observed.

## Production keying (Market Compass, live)

1. **Bar production.** Databento 1-minute bars arrive at the Collector and
   are written by `providers.bars()` / `_bar_chunk`: appended as events
   and merged into `bar_window:<dated-contract key>`, rows keyed by the
   bar's **open** timestamp, window capped at 1,800 bars. A bar exists for
   detectors only once Databento emits it — after its minute has closed,
   plus feed/collection latency (not separately measured).
2. **Detector evaluation.** `Engine.run` ticks every ~2 s
   (`asyncio.sleep(2)`). ICT scans run inside the tick and read bars via
   `ict_common.ict_bars()` (dated-key resolution). The newest bar a scan
   can see is the newest *completed* bar present in `bar_window`. The
   scans for the two live detectors are additionally throttled to one run
   per 120 s (`SCAN_THROTTLE = 120` in both `turtle_soup.py` and
   `aoi_fade.py`), so a production detector evaluates a freshly completed
   bar somewhere between its close and close + ~120 s of throttle phase,
   typically far sooner.
3. **Signal identity.** When a detector confirms on bar *j*, it sets
   `signal_ts = bars[j]['ts']` — the confirmation bar's **open**
   (`turtle_soup.py`: `'signal_ts': bars[j]['ts']`, with
   `entry = bars[j]['c']`, its close). `ict_paper.submit()` keys the
   trade id on that value: `ict-<detector>-<symbol>-<signal_ts>-t1`, and
   stores `signal_time = signal_ts`, `last_bar_checked = signal_ts − 60`.
   **Production itself keys trades on the bar's open.** That is the whole
   origin of the 60-second offset in the Task 001 pairing: Claude keys a
   trade at bar open + 60 s (bar completion); Quill's ids embed the open.
   Same action, same completed bar, timestamps 60 s apart — every one of
   the 66 paired trades sat exactly on that offset.
4. **Submit → dispatch.** `submit()` runs inside the tick's DB
   transaction and queues a durable TradersPost outbox row in the same
   transaction; `Engine.tick` calls `traderspost.dispatch_pending()` only
   **after** the transaction commits (audit #4: no network I/O inside the
   tick transaction). Webhook latency after detection is therefore the
   remainder of the tick plus the POST.
5. **Exits, for completeness.** Production `Engine.exits()` is
   **quote-driven** (freshest quote per position, dated-key resolution),
   not bar-driven — the source of the Tier 2 paper-exit lag measured in
   Task 002 (median +6 ticks vs the broker's +1).

## Quill's replay harness keying

Harness: `replay-harness/backtest_run.py` from the 2026-10-24 futures
plan (the "v2 production-faithful runner" — real detector scan functions
+ real `ict_paper.submit()` against a FakeDB; used for the v2 baseline,
the full matrix, and the prune replay; day-chunked by
`run_days_replay.py` with real-history prefeed).

- **Bar visibility:** `feed_until()` / `prefeed_until()` admit a bar only
  when `bar.ts + 60 <= now`. A detector never sees an incomplete bar —
  the same completed-bar semantics as production.
- **Tick grid:** day-runs use the default `--tick 120` (neither
  `run_days_replay.py` nor the original v2 `run_days.py` passes a tick),
  so a bar completing at *T* is first evaluated at the first 120 s grid
  tick ≥ *T*: harness action time ∈ [bar close, bar close + 120 s).
  `entered_at` is recorded as that tick's `now`.
- **Identity:** trades key on `signal_ts` (bar open) — identical to
  production because it is production's `submit()` writing the ids.
- **Exits:** simulated on completed bars after the signal bar; within a
  bar the stop is checked before the target (stop wins ties); fill at the
  level (but see the Item 7 note: stop fills now apply the shared
  −2 tick rule at fill time); exit timestamp recorded as `bar.ts + 60`;
  session flatten at the last close. P&L =
  (exit − entry) × direction × qty × multiplier − 2 × fee × qty.

## Measured production datapoint (2026-10-26, Turtle Soup | MCL)

The overnight trade that verified the new Tradefy strategy end-to-end:

| Event | Time (CT) | Source |
|---|---|---|
| Confirmation bar opens | 01:25:00 | signal bar (trade id keys here) |
| Confirmation bar closes; entry booked at its close, 102.44 | 01:26:00 | paper trade record |
| TradersPost receives the webhook | 01:26:16.7 | TradersPost signal log |
| Broker fill (101.03, MCLZ6) | 01:26:16.9 | Tradovate orders |

Segments: **bar close → webhook receipt ≈ 16.7 s** — a single measured
composite (Databento emission, bar-window write, tick phase, the
detector's 120 s scan-throttle phase, in-transaction queue, post-commit
dispatch). The evidence does not decompose it further; do not read it as
a budget for any one stage. **Webhook receipt → fill ≈ 0.2 s.** In
production the fill leg is negligible; the detection+dispatch composite
is the entire lag, and it is scheduling phase, not transmission.

## Alignment to "bar close + 0" — the concrete change on Quill's side

Two distinct things, only one of which is a change:

1. **Keying: no change, one clarification.** Quill's (and production's)
   trade ids embed the bar's open and that stays — it is production's own
   identity. The canonical *action time* of any Quill trade is
   `signal_ts + 60` (bar close), which is exactly where Claude already
   keys. Future Quill exports will carry an explicit
   `action_ts = signal_ts + 60` column so cross-harness pairing never
   depends on remembering the offset.
2. **Evaluation phase: a real harness change, queued.** The 120 s grid
   quantizes harness action time to [close, close + 120 s) while Claude
   acts at close + 0 exactly. To align, the day-runner must evaluate on
   bar-completion boundaries (a 60 s boundary-aligned grid, or
   event-driven evaluation at each bar close) instead of a free-running
   120 s grid. Verify against `backtest_run.py` (`--tick` default 120,
   `run()` advancing `now += self.tick_s`) and `run_days_replay.py`
   (passes no `--tick`). This change is **not yet applied** — it is
   stated here so it can be checked, and it is the prerequisite for
   re-measuring the Task 001 residual (~$1,050: 3 entry-shift outcome
   flips −$646, 15 unmatched −$553, per Claude's pairing as accepted in
   Task 002) on identical action times. Until that re-run lands, the
   residual stands as a phase-mismatch measurement, not a fills or
   detection disagreement — Task 002 already closed both of those.

— Quill
