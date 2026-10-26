# Market Compass — what has to change (all books)

Claude, 2026-10-26. Basis: main @ `4d0a95e` (2076 tests passing), three code deep-reads of the options,
swing/Smoothers and data layers (file:line below), the 8-week futures replay (12,605 trades, re-run under
the Task 002 fill rule), Quill's 45-trade live slippage export, and the 10-23 chat transcript.

## 0. The diagnosis

Compass is not "a system with no edge." It is ~30 signal ideas, none of which was measured on the
underlying before being expressed in options or routed live, running on execution plumbing that has
cost more money than the signals, over a data store that holds 5–6 weeks of stock bars and no option
history. The one measured edge (fade-into-level on crude, with a smaller NY-morning version on MES) has
been the live book for two trading days. Everything else below is either unmeasurable as written or
measured by a scoreboard that drops its own losses.

The fix is the same for every book: **signal on the underlying → gate → expression → exit**, in that
order, with the harness as the judge. And fewer things.

---

## 1. Cross-cutting — do these before touching any book

### 1.1 Execution (live futures)
- **Brackets priced off the planned entry, not the fill.** 28 of 38 live equity-micro entries filled
  >4 ticks from paper (p90 32 ticks; mean ≈ 0 so P&L hides it). Stop/target distances are wrong on most
  live trades; the 10-20 naked MNQ was this. Fix: relative brackets from the fill confirmation; log
  `order_sent_at`/`fill_at`. Until shipped: 1 contract, or paper.
- **Stop-fill rule adopted (stop −2 tk)** rests on 11 live stops, none on MES/MCL/AOI Fade. Log every
  live bracket-stop fill as a first-class row and re-fit monthly.
- **Four pruned detectors' Engine vars still `true`** (Quill audit 10-26). Safe only via empty allowlists.
  Delete the vars.

### 1.2 Measurement
- **One promotion gate, written, harness-enforced:** two consecutive non-overlapping 4-week replay
  windows, ≥40 trades, PF >1.2 under both −2 and −4 tick stop fills → 2 weeks paper → 1 contract.
  Demotion: trailing-28-day PF <1.0 (scorecard already computes it). Nothing moves otherwise.
- **No resets, no parameter edits, no new detectors for 4 weeks.** There has never been a clean window.
- **Every scoreboard must count its losses.** Smoothers' public 98% is wins ÷ (wins with a quote) because
  `exit_quote` is written only on the WIN path (`native_smoothers.py:317`; LOSS path `:333-340` never
  fetches one; `simple.py:182` drops `net_pnl is None`). Same class: 0DTE drops quoteless exits as
  `data_gap`; option_ideas ends most records `excluded`/`unresolved` before an exit prints. Rule: an
  exclusion rate above 10% invalidates the headline; show it beside the number.
- **Underlying-first R at fixed horizons** (30/60/120 min; 1/3/5 sessions) for every signal family,
  stored as a trade attribute — not a bracket that only exists if a 5-second quote arrived.

### 1.3 Data (what research is possible today, and what isn't)
- **Stock 1-min bars: ~5–6 weeks** in Postgres (`stock_history.py:32` seeds 7 days; accumulates since
  mid-Sept). Zero in R2. Not a backtest dataset. **Needed:** 2+ years of 1-min bars for the ~160-symbol
  universe from Alpaca historical (free with the existing plan; one-time pull, ~2 GB compressed).
- **No end-of-day option chain snapshot is retained** (`chain:SYM` overwritten each refresh,
  `chain_archive` deleted 10-21; theta/vega discarded `providers.py:715-720`). There is no IV history, so
  no options book can be modeled retrospectively. **Needed:** one EOD snapshot per underlying per day
  (strikes ±10%, all expiries ≤60 DTE, greeks+IV) — ~5 MB/day; plus a 1-year historical IV pull for
  SPY/QQQ if 0DTE research is going to happen (Massive or CBOE DataShop).
- **VIX kept as latest only** (`vix:latest`). Needed: daily VIX history (free, CBOE) for the regime gate.
- `matrix_raw` grows unbounded, never archived or pruned (`copy_archive.py:479`); `pick_check`, squeeze,
  flow_pulse, ict_* signal kinds have no retention entry. Add them or stop writing them.
- Silent-death paths: stream health is `receiving` forever once any quote arrived (`stock_stream.py:161`,
  `option_stream.py:341`); futures health is written only inside the callback (`providers.py:524`);
  Massive chain failure falls back to Alpaca with no health error (`providers.py:598-603`). Add a
  "newest event age" watchdog per stream.

### 1.4 Spend
Nothing below Databento + Alpaca feeds a measured signal today. TraderMatrix (matrices, flow, 21 intel
feeds) feeds `exposure_level_break`, `flow_price_breakout`, `tape_confirmed`, `flow_pulse` — none scored on
the underlying. Massive's unique value is option quotes for 0DTE; if 0DTE goes shadow-only, it's optional.
Obsidian: zero usage. Decide after §3–4 below, not before.

---

## 2. Futures

**Now.** Live: AOI Fade {MCL, MES}, Turtle Soup {MCL}; paper-only: Golden Zone {MCL, MNQ}, Rumers {MYM}.
2 contracts, bracket at TradersPost, 15:45 CT flatten.

**What the replay says (8 weeks, stop −2 tk):** AOI Fade·MCL +$3.2k PF 1.81 (69 tr); Turtle Soup·MCL
+$9.4k PF 1.89 (123) — **+$7.5k of it in the Asia session 17:00–02:00 CT at 64% win; London/NY flat**;
AOI Fade·MES +$2.2k PF 1.50 (73) — **NY morning 08–11 CT at 64%; London 39%**. All three lose after
11:00 CT. Sundays flat-to-negative. Fill-fragile pairs (Turtle Soup·MNQ/MGC, BOS+FVG·MNQ, Trend
Rider·MNQ) flip sign inside a 6-tick fill window — correctly kept off live.

**Changes.**
1. Session allowlists, not just symbol allowlists: Turtle Soup·MCL 17:00–02:00 CT only; AOI Fade·MES
   08:00–11:00 CT only; AOI Fade·MCL 02:00–11:00. Replay-test first (one flag in the harness); expected
   effect is fewer trades, same net, lower drawdown.
2. No third entry on a pair-day that already has two stops (33 of 101 pair-days; 3rd+ trades net −$662).
3. Relative brackets (§1.1) before anything else.
4. Delete `ict_trail` (manages nothing) and the four fragile detectors' code paths; keep the git tag.
5. Weekly harness re-run on the week's bars, published to the desk; the 28-day scorecard is the demotion
   trigger.

**Honest size of the edge:** ~$1–1.5k/week on 2 contracts, drawdowns −$2–3k, in-sample, one regime,
crude-dominated. Enough to run, not enough to scale until it survives out-of-sample.

**Data needed:** nothing new; the Databento export already covers it. For a longer backtest, 1 year of
MCL/MES/MNQ 1-min from Databento historical (~$30).

---

## 3. 0DTE

**Now** (`zero_dte_paper.py`): any `options_0dte` alert → buy 1 contract at ask+1¢ → exit at ±50% of
premium or 13:55/14:55 CT flatten. Seated to `session_sweep_reclaim × {AMD, MU, NFLX}` since 10-25;
everything else shadow-scored with identical rules. **No underlying evaluation anywhere in the book**
(`zdp:339-345`); shadow stores `trigger.invalidation` and never scores it (`scanner_shadow.py:127`).

**Why it cannot show an edge as written.** Symmetric ±50% premium exits on same-day options are theta-,
spread- and slippage-negative by construction; the book measures option bid paths, not whether the
underlying moved; `data_gap` removes the quoteless exits; the SPY plan path is dead; entries are cut off an
hour before the only window with published evidence (last 30 min). Upstream, `setup_study.py:67`
re-checks option quotes at 5 s despite `OPTION_QUOTE_MAX_AGE=60` (`engine.py:505`), so most contracts
are rejected before an alert exists.

**Changes.**
1. **Shadow-only for all 0DTE until §4 clears a family.** No premium is bought on an unmeasured signal.
2. Replace the exit model: underlying ATR stop/target + time stop; premium modeled with
   `smoothers_blackscholes` from the underlying path (needs IV — §1.3), live quotes as a check.
3. SPY/QQQ only. Delete the weekly fallback (`OPTION_0DTE_MAX_DTE=7`) and single names.
4. Fix the 5 s/60 s quote-age contradiction (`setup_study.py:67` vs `engine.py:505`).
5. Test the one 0DTE idea with literature: first-30-min → last-30-min intraday momentum on SPY, one trade
   per day, entries *after* 14:30 CT. Requires moving the entry cutoff.

**Data needed:** SPY/QQQ 1-min bars (2 years), SPY option EOD chains with IV (1 year), VIX daily.

---

## 4. Intraday (scanner, option_ideas, evidence feeds)

**Now.** Six scanner families (`scanner.py:110-269`; status reports five — `level_breakdown` omitted
`:446`) on ~160 symbols → stock setup trial (bracket, `setup_study.py`) + option trial + `option_ideas`
(1–21 DTE). Six evidence feeds alert separately.

**The one thing that already works as a measurement:** the stock setup trial — entry ask+tick, stop =
invalidation, 2R target, `r_multiple` recorded (`setup_study.py:140`). Its blocker: a trial is `excluded`
unless a ≤5 s two-sided sized quote arrives within 90 s of bar close (`:63-67`). No fixed-horizon
fallback, so the exclusion rate decides the sample.

**Per feed (can it be scored on the underlying?):** tape_confirmed — yes (reads trial R) but Simple's
"Tape Flow" block reads a `tradermatrix_tape` source nothing in the repo produces; flow_pulse — no
(14-day time-only option exit; `spot_at_fire` stored, never scored); apex/breakouts/gap_continuation —
yes, descriptive marks only (win = return>0, no R); day board/darkpool/iv_rank/squeeze — no outcome
measurement at all (squeeze fires are logged and never scored).

**Changes.**
1. **Replay the six families on 2 years of 1-min stock bars** (same harness pattern as futures; the scanner
   reads `bar_window:SYM`). Output: R at 30/60/120 min and at the bracket, per family × symbol class.
   Expect ≥3 families to die. This is the single most valuable research step in the project.
2. Add a fixed-horizon fallback to `setup_study` so a trial is never excluded for lack of a 5 s quote.
3. **Evidence feeds → trade attributes.** Stamp tape/flow/magnet/darkpool/IV/squeeze state on every trial;
   test each as a post-hoc filter on the surviving families. Keep what moves R; delete the rest and the
   vendor behind it. Stop alerting on them.
4. `option_ideas`: pause until a family survives (1); then same exit model as §3.2.
5. Universe: 160 symbols with one quote stream is why `observation_gap` dominates. Scan 160; trade/quote
   the top 20 by liquidity.

**Data needed:** 2 years 1-min bars (§1.3); nothing else.

---

## 5. Swings

**Now** (post gate surgery 10-25): daily_breakout / pullback_reclaim / daily_reversal on daily context
(`swing_signals.py:142-177`), 14–60 DTE long option, exits at 0.65×/1.75× premium, underlying stop/target,
10-session hold — **evaluated once a day at the close snapshot**. Flow is an attribute now (good). No
quote-gap kill remains. ~80 trading days of daily history stored; no daily-bar backtest exists.

**Open bugs.** (a) Close snapshot rejected when the option's last quote stamps at/after 16:00:00 ET
(`swing_ideas.py:212`, strict `<`); the swing idles in `awaiting_daily_close` and `maximum_hold` never
fires because it's checked only inside `evaluate_close` (`:255`). (b) A `data_gap` day silently skips the
stop check; a 0.65× breach on Tuesday is realized at Wednesday's close (`:303-309`). (c) Intraday stop
touches are invisible by design.

**Changes.**
1. Fix (a) with a 20-minute grace and evaluate `exit_deadline` independently of snapshot capture.
2. Evaluate the stop against the underlying's intraday extreme on gap days; flag `late_stop`.
3. **Backtest the three rules on daily bars first** — 5 years × universe is a free Alpaca pull (~2 min).
   The 20-day breakout + 20/50 trend filter is a known, modest signal; see what it does here before
   buying premium against it.
4. Exits on the underlying (ATR-based) with premium following; 45–60 DTE to cut theta on a 10-session
   hold. Position: 1 contract is fine for measurement.
5. This book is where multi-day holds are allowed. The best-evidenced thing to test here is trend following
   on liquid ETFs/large caps with volatility-scaled sizing — through the harness first.

**Data needed:** 5 years daily bars (free); EOD option chains with IV going forward (§1.3).

---

## 6. Smoothers

**Now.** Weekly: hourly-bar triple-EMA z-score classification (`smoothers_math.py:55-83`), MAIN/BACKUP
(`smoothers_signals.py:9-20`), entry at Monday's first completed hour, target = entry·(1±pm), WIN = 1-min
touch of the underlying target any time that week, LOSS = no touch by Friday close. Option: nearest-strike,
latest expiry ≤ Friday. **No premium stop.** Cohort/BEST_10/DROPPED hand-picked on 5 weeks of results
(`smoothers_quality.py:11-18`) — in-sample selection.

**The headline number is structurally wrong.** `exit_quote` is captured only on the WIN path
(`native_smoothers.py:317`); the Friday LOSS path (`:333-340`) never fetches one, so every loss has
`net_pnl=None` and is excluded by `simple.py:182`. "60 of 61" is target-hits-that-also-made-money ÷
target-hits-with-usable-quotes. The honest headline is WIN ÷ (WIN+LOSS) from `weekly.status`
(`daily_results.py:195`), with option P&L shown as "measured subset, n=…". Second-order: `rank_signals`
favors the smallest `pm` (easiest target), which inflates touch rate, not option P&L.

**Changes.**
1. Fetch `exit_quote` on the LOSS/UNRESOLVED path; report WIN/(WIN+LOSS) and the exclusion count.
2. Backtest the smoother on stored hourly bars (395 days exist, `native_smoothers_data.py:83-96`) with a
   placebo (random entry, same target) — the cohort selection has never been tested out of sample.
3. Add a premium stop or accept that this is a binary weekly bet and size it that way.
4. Re-select the cohort on the backtest, not on five weeks of live.

**Data needed:** nothing new; hourly bars exist.

---

## 7. Extra information we need (summary)

| Need | Why | Source | Cost |
|---|---|---|---|
| 2 yrs 1-min stock bars, ~160 symbols | scanner families, 0DTE underlying, MU scalp | Alpaca historical REST | $0, one pull |
| 5 yrs daily bars, universe | swing rules backtest | Alpaca | $0 |
| EOD option chain + IV, going forward | model any options book | Massive snapshot once/day, store it | $0 extra |
| 1 yr SPY/QQQ option EOD with IV | 0DTE research | CBOE DataShop / Massive hist | ~$50–200 |
| VIX daily history | regime gate | CBOE | $0 |
| 1 yr MCL/MES/MNQ 1-min | futures out-of-sample | Databento hist | ~$30 |
| Live fill log (entry + stop, ticks) | fill rule re-fit, bracket geometry | TradersPost/Tradovate, already have | $0 |
| Stream newest-event-age watchdog | dead-while-connected | code | — |

---

## 8. Sequence

**Week 1 (freeze + fix):** relative brackets; stale env vars; MU keyspace; swing close-window; iceberg
flag; Smoothers LOSS quotes; stream watchdog; EOD chain snapshot job; historical bar pulls. Live futures
stays as is (3 pairs) or 1 contract — Josh's call. 0DTE/option_ideas/flow_pulse → shadow-only.
**Weeks 2–3 (measure):** scanner-family replay on 2 years; swing rules on 5 years; Smoothers backtest +
placebo; futures session-window replay. Publish all four to the desk under the gate in §1.2.
**Week 4 (decide):** survivors to paper; everything else deleted with a tag; vendors renewed only if an
input to a survivor. First clean 4-week live window for futures ends; first trustworthy number.
**What to stop doing:** new detectors, resets, parameter nudges, per-alert Discord streams, vendor trials.

If after the 4-week window the fill-robust futures pairs are under PF 1.2 and no scanner family clears
the gate, the project's honest conclusion is that this detector family has no edge on micros, and the
next thing to test is the lower-frequency, published set (intraday momentum, ORB held to close,
prior-day-level fades with a vol gate) — through the same harness. That would be a result.
