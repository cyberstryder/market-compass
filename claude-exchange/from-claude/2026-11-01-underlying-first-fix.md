# Underlying-first measurement for swings and 0DTE — code fix (Claude, 2026-11-01)

Branch `underlying-first-2026-11-01` (`abc9f17`) rebased onto main `be6405b` (whole-list 0DTE book + swings retirement); patch `2026-11-01-underlying-first-fix.patch` beside this file.
Companion evidence: `2026-11-01-swings-0dte.md` + `2026-11-01-swings-0dte/` (two-year replay of both books on
`data-foundation-v2-stocks`). **No seat, allowlist, alert, booking or default behaviour changes.** Quill: review, run the
suite, merge; the env settings in §4 are Josh's.

## 1. The defect being fixed

Both books score the option's quote path, so an observation exists only when a streamed contract quote arrives inside a
freshness window. Swings: 127 tickets in October, 0 conversions. 0DTE: 11,311 tickets, 30 observed, in one week. The
signal itself was never measured in production. The offline replay measured it; this change makes production measure it
the same way, from the `bar` events the collector already stores.

## 2. What changed

`compass/underlying_path.py` (new, pure): `bars_between` (latest revision per minute, bounded), `entry_from` (next
completed bar open ± $0.01), `price_at` (last bar completed ≤ target, ≤ 5 min old), `horizon_return`, `bracket`
(stop-first on bar extremes, gap fills at the bar open, time stop at `end`), `summarize`. Same conventions as the replay
harness `swing_replay.py` / `sweep_replay.py`.

`compass/swing_study.py`
- `register`: a candidate with no usable 5-second quote is registered **pending** with `entry_mid = signal_price`,
  `entry_source='signal_bar_close'`, `entry_note` naming the old reason. Fresh quote still preferred (`quote_mid`).
  Clock/age/excluded-symbol failures stay `unavailable`. New `first_of_day` flag (first prospective trial for the same
  symbol/side/rule/receipt_day).
- `observe`: when `quote_at` returns `no_timely_usable_checkpoint_quote` / `quote_window_overflow`, the completed
  1-minute bar close at the target is used (`reason='bar_close_reference'`, status `completed`). Basis-change still wins.
- `score_path` (new): when the `9session` checkpoint resolves, walks the production stop and 2R target on stored bars
  from the signal to the 10-session hold deadline (session close − 15 min) and writes `underlying_path` (status, reason,
  R, bps, +30/+60/+120 bps) on the trial payload.
- PROTOCOL text updated.

`compass/swing_study_report.py`: `underlying_path` block — per rule × side: scored, mean R, mean bps, win rate,
stops/targets/time exits, for `all_fires` and `first_fire_of_day`; plus status counts.

`compass/config.py`: `SWING_RULES` (default `daily_breakout,pullback_reclaim,daily_reversal`) and `SWING_SIDES` (default
`long,short`), validated in `validate()`. `compass/swing_ideas.py`: `discover` drops switched-off cells before admission
and before study registration.

`compass/scanner_shadow.py`: `score_underlying` (kept beside Quill's `_crosscheck`, which stamps closed non-data_gap records only at +120 m / signal-day close vs the signal price — the two are complementary; this one is what fixes the denominator) — for every scanner-family record under `shadow:0dte-*` and
`trade:0dte-scan-*`, once `now ≥ flatten_at + 120 s`: side from `option_type`, entry = next bar open after `alert_ts`,
`bps` at +30/+60/+120, `flatten_bps`, `bracket` on `trigger.invalidation` / 2R / flatten time stop. Written once under
`underlying_path` (`status` complete / incomplete / unavailable / expired). Called from `update()`; failures swallowed so
the premium path is never blocked. `scorecard` cells gain `underlying_path` = {scored, bracket{n, mean R, win_rate,
stops, targets}, bps_60m, flatten_bps}; denominator is alerts.

Docs: `docs/swing-ideas.md` (new section), `docs/live-scanner.md` (new section).

Tests: `tests/test_underlying_first.py` (15 tests: helper semantics incl. stop-first and gap fills; register fallback +
`first_of_day`; checkpoint bar fallback and its 5-minute staleness bound; `score_path`; switches incl. invalid value
→ `validate()` error; shadow + seat records scored from bars, written once, unscorable/expired marked once; scorecard
summary). `tests/test_swing_study.py`: the strict-quote parametrization split — stale/future/pre-signal quote cases now
assert the fallback; clock cases unchanged. Suite: see §5.

## 3. What did not change

Option admission in `swing_ideas.py` (still needs the fresh contract quote; the ticket still lives to session end − 30
min); premium exits; `zero_dte_paper` booking and exits; seats / promoted families; `data_gap` exclusion for premium
P&L; alerts; Discord. `simple.py` reads trade records by existing keys and ignores the two new ones.

## 4. Env (Josh)

- Swings are retired at the flag layer since 6ed4fff (`SWING_IDEAS_ENABLED=false` default). That also stops the study feed
  (shared `discover()`). To keep measuring the one cell with excess on the underlying without buying options:
  `SWING_IDEAS_ENABLED=true SWING_RULES=daily_reversal SWING_SIDES=long` on the engine service. Code default keeps all six
  cells when the flag is on.
- 0DTE seats are already empty by default under the respec (61a325b); `ODTE_BOOK_SEATED_NAMES` is the operator action. The
  replay found no drift on the underlying for `session_sweep_reclaim` on the universe or on AMD/MU/NFLX, so the
  recommendation is: do not re-seat it; let the new per-alert underlying scoring be the forward evidence for every family.

## 5. Suite

Sandbox note: this sandbox has 2 CPUs and the app-lifespan tests (TestClient lifespan starts every worker against one
SQLite file) crawl here on lock contention — minutes per file, versus 5–6 min for the whole suite on the VM/CI. So:
- the 165 test files that do not start the app: **1685 passed, 50 skipped, 1 failed** (pre-rebase); the one failure was
  `test_research_mode.py::test_daily_breakout_has_independent_swing_checkpoints[False]`, which encoded the old
  strict-quote verdict and is updated in the branch (legacy daily-breakout trial is now pending with
  `entry_source='signal_bar_close'`);
- app-lifespan files touching the changed paths, run individually: `test_simple` 24, `test_simple_cache` 2,
  `test_option_ideas` 28, `test_setup_study` 29, `test_dashboard_banner_policy` 3, `test_events_export` 5, `test_core` 18,
  `test_secondary` 26, `test_discovery` 23 (+6 skipped) — all passed;
- after the rebase onto `be6405b`: `test_underlying_first`, `test_swing_study`, `test_swing_ideas`,
  `test_scanner_shadow_split`, `test_odte_book`, `test_zero_dte_evidence`, `test_research_mode`, `test_scanner` —
  **202 passed, 3 skipped**.
Quill's CI run of the full suite on the branch is the gate.
