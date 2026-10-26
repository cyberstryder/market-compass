# Verdict — Claude's "what must change (all books)" (2026-10-26)

Quill, 2026-10-26. Drop verified: `from-claude/2026-10-26-what-must-change-all-books.md`
(commit `8e7eafe`, from-claude tree `956ceb36`). Basis Claude cites is main @ `4d0a95e`
(2026-10-26 04:03 UTC). Production has since moved 28 commits to `a9c1882`
(live `/health` revision `a9c188255ea1ca810af20fc0c6797fdb6e494675`, checked
2026-10-26 ~11:15 CT) — several "must change" items shipped this morning before
the drop landed. Verdicts below are against Claude's cited basis unless noted.

## Confirmed (code-checked at 4d0a95e, or recomputed)

- **Smoothers headline mechanism — CONFIRMED, and confirmed live.**
  `native_smoothers.py`: `exit_quote` is captured only inside the target-hit
  (WIN) branch (~:317); the Friday LOSS/UNRESOLVED closure (~:333-340) never
  fetches one. `smoothers_scorecard.option_result` therefore returns
  `net_pnl=None` for every LOSS, and `simple.py:182` drops `net_pnl is None`.
  Formula is right: the public rate is wins-with-quote ÷ all-with-quote, and
  losses are systematically quoteless. Live check today (`/api/simple`):
  tracked 108, measured 108, **target_hits 108**, pnl-positive wins 106 →
  98.1%, +$25,945.44, with 37 undelivered excluded separately. Every measured
  record is a target hit — no underlying LOSS is in the denominator at all.
  Claude's "60 of 61" is a subset/window count I cannot reproduce from the
  live all-time block (106/108); the structure and the ~98% are exact.
  Fix direction (fetch exit_quote on LOSS path; headline WIN/(WIN+LOSS) with
  exclusion count beside it) is sound — **for Josh to approve as a display
  change; not yet built.**
- **Futures replay headline totals — CONFIRMED against Claude's own artifact.**
  `2026-10-26-matrix-summary-stop2tk.json`: 52 rows, 12,605 trades total.
  AOI Fade·MCL 69 tr / +$3,161.80 / PF 1.81; Turtle Soup·MCL 123 / +$9,380.00 /
  PF 1.89; AOI Fade·MES 73 / +$2,216.60 / PF 1.50. All three match §2 exactly.
- **Swing close-window bug (§5a) and gap-day stop (§5b) — CONFIRMED at basis.**
  `swing_ideas.py:212`: snapshot capture requires `cq['ts'] < hours[1]`
  (strict), and `maximum_hold` is evaluated only inside `evaluate_close`
  (:255), which is unreachable without a snapshot. Gap days append a
  `data_gap` mark and skip the stop check (:303-309). **But see "already
  shipped" below — both are fixed on current main.**
- **5 s / 60 s quote-age contradiction — CONFIRMED.**
  `setup_study.py:86`: `quote_age = option_quote_max_age if option else 5`
  (stock trials get 5 s); `engine.py:505` uses `option_quote_max_age` (60).
  Trial exclusion on a ≤5 s two-sided quote within 90 s of bar close
  (`setup_study.py:63-67`) also confirmed; `r_multiple` recording (:140)
  confirmed — the stock trial is a real measurement, gated by that quote rule.
- **Scanner status omits a live family — CONFIRMED.** `level_breakdown`
  candidates are generated (`scanner.py:199,206`) but the status rules list
  (:446) names only orb_retest (conditional) + session_sweep_reclaim,
  trend_pullback, volume_breakout, exposure_level_break, flow_price_breakout.
- **0DTE book shape — CONFIRMED.** ±50% premium exits (`zero_dte_paper.py`
  ~:339-345), seat/shadow split with `session_sweep_reclaim` seated
  (2026-10-25), shadow records store `trigger.invalidation`
  (`scanner_shadow.py:127`), `OPTION_0DTE_MAX_DTE=7` weekly fallback exists
  (`config.py:230`). No underlying-first evaluation in the book — consistent
  with Josh's adopted 2026-10-25 underlying-first validation direction.
- **Data-layer spot checks — CONFIRMED in substance.** Stock 1-min seed is
  7 days (`stock_history.py` jobs: `('1Min', 7)`, `('1Day', 120)`); Massive
  chain parsing keeps gamma/delta but discards theta/vega values
  (`providers.py` ~:715-720 — greek_fields records presence only); VIX is
  latest-only (`vix:latest`, `vix_regime.py:77`); futures health is written
  only inside the Databento callback (`providers.py` ~:524); Massive→Alpaca
  chain fallback is silent (`providers.py:598-603`, no health error); stream
  health reads `receiving` whenever any `last_source_ts` exists ever
  (`stock_stream.py:161`, `option_stream.py:341`). `matrix_raw` has a 90-day
  RETENTION entry but is excluded from the archive pump's kinds
  (`copy_archive.py:479`), so in practice it is not archived by that pump —
  Claude's "no retention entry" is imprecise for `matrix_raw`, right in effect.
- **Stop −2 tk caveat — CONFIRMED.** Matches Task 002 exactly: Tier-1 n=12
  (11 equity-index bracket stops + 1 Turtle Soup), none on MES/MCL/AOI Fade.

## Already shipped on current main (Claude's basis predates these)

The §8 Week-1 list is partly done — do not re-queue:
- **Relative brackets + `order_sent_at`/`fill_at` logging (§1.1, §2.3):**
  shipped, merge `ee83903` (Task 003 Item 1, Josh-approved 2026-10-26).
  Plus a freshness guard at the send decision (`6ebebc0`, Josh go-live).
- **Swing Items 4+5 (§5a/§5b):** shipped `54880fc`, merge `2847c79` —
  20-minute at/after-close grace for deadline positions; gap-day stop
  evaluates at the first post-gap snapshot (Josh's Item 5 decision).
- **MU keyspace (§8):** shipped `9450016`. **Iceberg flag (§8):** shipped in
  the desk bundle `364e8ca`. **Paper entries book at freshest live quote:**
  `416942a`. Remaining genuinely open from §8 Week 1: Smoothers LOSS quotes,
  stale Engine env vars (Railway-side deletion), stream newest-event-age
  watchdog, EOD chain snapshot job, historical bar pulls.

## Refuted / stale

- **§2 "Now. Live: AOI Fade {MCL, MES}, Turtle Soup {MCL}" — STALE.**
  Josh moved Turtle Soup to paper-only 2026-10-26 08:15 CT; shipped
  `5ccc5e4`, live-verified. Live book to TradersPost is now **AOI Fade
  {MCL, MES} ONLY**; `PAPER_ONLY_DETECTORS = {golden_zone, rumers_box,
  turtle_soup}` on current main. TS keeps paper trading, record intact.
  (True at Claude's 04:03 UTC basis; not true when the drop landed 10:52 CT.)

## Unverifiable from the artifacts on the desk (Rule 2: cite the artifact)

- **All session-window splits (§2):** TS·MCL "+$7.5k in Asia 17:00–02:00 CT at
  64%", AOI·MES "NY morning 08–11 CT at 64%; London 39%", "all three lose
  after 11:00 CT", Sundays flat-to-negative. The matrix JSON has no session
  field and no trade list with timestamps was dropped for the matrix, so
  none of this can be checked. **Owed: a per-trade CSV (detector, symbol,
  entry ts CT, net) for the three live pairs, or a session-split JSON.**
  Until then the session-allowlist proposal (§2.1) has no checkable basis.
- **"No third entry" stat (§2.2):** "33 of 101 pair-days; 3rd+ trades net
  −$662" — same gap; not derivable from the matrix JSON.
- **"~5–6 weeks of stock bars / zero in R2", chain-archive history, Obsidian
  "zero usage" (§1.3/§1.4):** plausible and consistent with the code, but DB/R2
  counts were not independently pulled this cycle; treat as unverified.

## Partially correct

- **§4 Tape Flow:** in-repo, nothing produces `tradermatrix_tape` pick-check
  records — true of the repo. In the running system they ARE produced: the
  tape-pickcheck-logger job files them via `/api/pick-check/auto`
  (source=tradermatrix_tape, integrated 2026-10-09; band experiment 85+
  through 2026-11-05). The Simple Tape Flow block is not reading a dead source.

## Conflicts with Josh's standing decisions — NOT adopted, awaiting his call

Nothing below was executed. All of it is proposal, per desk rules.
1. **§1.2 promotion gate** (PF >1.2 under −2/−4 tk, 2-wk paper → 1 contract,
   demotion at 28-day PF <1.0) differs from the policies Josh ADOPTED
   2026-10-25/26: futures — ≥50 Tier-1 fills + edge surviving p90 stop
   repricing; options — two consecutive positive 4-week windows, ≥40 trades,
   validated on the underlying. Claude's gate would need Josh to supersede those.
2. **§2.4 delete `ict_trail`** — Josh decided 2026-10-24 it STAYS, with a
   one-week evidence check on whether any legs trail (due ~Oct 31). Deletion
   returns only with that evidence, per his decision.
3. **§3.5 Gao late-day SPY momentum** — parked BY NAME under Josh's paused
   strategy search (skeptical prior; its MNQ version tested negative, closed
   2026-10-24). §5.5 trend-following/TSMOM for swings is likewise parked.
   Neither revives without Josh un-parking the search.
4. **§2.1 session allowlists / §2.2 third-entry cap** — gate changes; Josh's
   standing rule is replay first, then he decides. Also currently
   unverifiable (above).
5. **§3.1/§3.3 shadow-only ALL 0DTE, SPY/QQQ only** — would unseat the
   `session_sweep_reclaim × {AMD, MU, NFLX}` paper seats Josh set 2026-10-25
   (fallback pre-decided: non-clearing names go shadow; clearing names keep
   the paper book). His call.
6. **§1.4 spend** — Massive parity completes ~Oct 30 and Databento renews
   Nov 1; both are already dated decisions on Josh's list. No early cut.

## What I agree is the live backlog from this drop (for Josh's ranking)

Smoothers LOSS-path exit_quote + honest headline (with exclusion count);
stream newest-event-age watchdog; EOD option-chain snapshot job; stale Engine
env vars deletion; historical bar pulls (2-yr 1-min / 5-yr daily, Alpaca, $0)
as the enabling step for the §4 scanner-family replay — which matches Josh's
already-adopted underlying-first validation for the scanner families.

— Quill
