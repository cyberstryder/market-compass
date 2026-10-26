# Task 004 — Deep verification of "what must change (all books)"

Quill, 2026-10-26. Extends the same-day quick verdict
(`from-quill/2026-10-26-what-must-change-verdict.md`, desk head `8a0d034`).
Where this pass agrees with the quick verdict, it says so in one line and
moves on; the weight here is on what the quick pass could not do: the
session-split and third-entry re-runs on our own harness (its
"unverifiable" items), the honest Smoothers recompute from stored
payloads, and the claims it did not reach. Two of its statements are
corrected below (§A).

Basis: Claude's doc was written against main `4d0a95e`. Verdicts here are
against main `a9c1882` (task-start head) unless noted. Main moved once
more during this pass, to `476207d` — the Smoothers §6 fix shipping
(§D.3). Nothing else in that 4-file diff touches a claim below.

Desk rules observed: verify and publish only. No production code, config,
or live-book change was made. Live-money items sit in §E for Josh.

---

## A. Corrections to the quick verdict

1. **Engine env vars are NOT still open.** The quick verdict listed
   "stale Engine env vars (Railway-side deletion)" as genuinely open from
   §8 Week 1. They were deleted the same morning the doc's basis predates
   (2026-10-26 07:56 CT), and I re-verified live during this pass: the
   Engine service carries **62 variables**; none of the four pruned
   detectors (`bos_fvg`, `aoi_zones`, `bos_gz`, `smt`) has a variable at
   all, `TRADERSPOST_WEBHOOK_URL_2` is gone, and the only retired-detector
   flag left is `ICT_CONTINUATION_ENABLED=false`. Claude's §1.1
   "still sits true in the Engine's variables" is **STALE**.
2. **§5(b) is narrower than "fixed."** The gap-day deferral bug is fixed
   as Josh specified (Item 5, merges `2847c79` + repair `06a455a`):
   `observe()` in `swing_ideas.py:329-352` re-checks pending gap days and
   evaluates the stop at the first post-gap snapshot — in the same pass
   when the gap day is the last pending day before the snapshot day.
   What remains is adopted design, not a bug: evaluation is close-only
   (no intraday extreme is ever consulted), and a metadata `FAILURE`
   verdict still blocks the pass. Claude's proposed intraday-extreme
   check is a *different stop semantics* from the one Josh adopted on
   2026-10-26 — it belongs in §F, not in a bug list.

## B. Claims the quick verdict did not reach (or reached only in part)

- **§0 headline ("~30 signal ideas, none measured on the underlying
  before being routed live") — PARTIAL.** The futures detectors were
  bar-replay measured before and after seating (prune replay,
  2026-10-24/25); what was missing until this month was achievable-fill
  measurement, which the repricing work closed. The options/scanner
  families were indeed adopted unmeasured on the underlying until the
  2026-10-25 underlying-first study. His sentence is right for the
  options side, overstated for futures.
- **§1.2 demotion infrastructure — CONFIRMED present.** The trailing-28d
  PF and kill-watch he asks for already exist at
  `/api/live-combo-scorecard` (`window_days: 28`, per-pair
  `trailing_28d_pf`, `kill_watch` flags; read live 2026-10-26). The
  disagreement is only about the gate itself (§F).
- **§1.3 retention, full picture.** `matrix_raw` has a 90-day RETENTION
  entry (`copy_archive.py:30`) but is excluded from the copy pump
  (`copy_archive.py:469-480`, `archive_prune_copy.py:59`) — kept, pruned
  in place, never archived (quick verdict: same conclusion). The kinds
  Claude did not name are worse: **`pick_check`, the `ict_*` event kinds,
  `squeeze`, and `flow_pulse` have no retention entry at all** —
  `copy_archive._retention_days` returns `None` for them. CONFIRMED.
- **§1.3 stock-bar depth — the seed is CONFIRMED, the depth is
  UNVERIFIABLE from here.** `stock_history.py:180` seeds `1Min` × 7 days
  + `1Day` × 120 days, exactly as he says. His "~5–6 weeks in the store"
  accumulation claim cannot be measured with the available read paths:
  `/api/bars` returns at most the 1,000 most recent bars and no endpoint
  exposes the oldest stored bar (direct DB reads are blocked from this
  side). The 2-year backfill in §E makes this moot going forward.
- **§1.4 "TraderMatrix feeds no measured signal" — WRONG as stated;
  §1.4 overall PARTIAL.** TraderMatrix gates the live futures book
  today: `ict_paper.py:264-272` declines entries during high-impact news
  windows, and those declines are logged (`ict_decline` rows, reason
  `news_pause`, since decline persistence shipped 2026-10-26). A vendor
  whose signal vetoes live entries is feeding a measured signal, even if
  its four discretionary signal families are unscored on the underlying
  (that part is CONFIRMED — no underlying scoring exists for
  `exposure_level_break`, `flow_price_breakout`, `tape_confirmed`, or
  `flow_pulse`). Massive's position is as he frames it (the options data
  layer; parity week already dated). Obsidian "zero usage" is
  UNVERIFIABLE from the artifacts on this side.
- **§2.3 / §1.1 execution logging — STALE, and now stronger than he
  asked.** Relative brackets + send logging shipped (`ee83903`):
  TradersPost payloads carry relative stop/target amounts anchored near
  the fill (Task 003 Item 1, revised per the TradersPost docs), the
  claim record stamps `order_sent_at` (exact) and the response id, and
  `fill_at`/`fill_price` remain reconciled — not stamped — fields.
  Honest caveat, same standard as the freshness guard (`6ebebc0`): no
  natural AOI signal has fired since the ship, so the first production
  proof of an anchored bracket is still owed.
- **§2 Trend Rider·MNQ "flips sign inside a 6-tick window" — UNRESOLVED
  disagreement.** Our Task 003 spot-check found `trend_rider|MNQ`
  net-positive under every convention tested then; those conventions
  were the shared-rule set, not a 6-tick sweep. Neither artifact settles
  the sweep claim. (His neighbouring claim on BOS+FVG·MNQ is consistent
  with our record: PF 1.12 at the shared rule over 836 trades is thin
  enough to be called fill-fragile.) Trend Rider is OFF in production
  either way; this is a research question, not a live-book one.
- **§3 "no underlying evaluation anywhere" — WRONG as a system claim,
  CONFIRMED as a book-scoring claim; the two must stay separate.** The
  live book's scoring is premium-path only (`zero_dte_paper.py:6-12`:
  buy at ask + 1¢, exits at ±50% of premium, 2:55 PM CT flatten; the
  one-hour entry cutoff he cites is `ENTRY_CUTOFF_SEC = 3600`,
  `zero_dte_paper.py:30`). But the *seating* was decided by
  underlying-first validation on 2026-10-25
  (`files/options-underlying-first-validation-2026-10-25.md`):
  session_sweep_reclaim cleared at slow horizons and holds the paper
  seats (AMD/MU/NFLX); non-clearing names sit in the shadow scorecard
  under a pre-decided rule. His §3.1 would unseat a decision Josh made
  on underlying evidence — §F.
- **§3/§4 quote-age wiring, one correction of address.** The 5 s standard
  is `market.fresh`'s default (`market.py:56`), which `setup_study.py`'s
  admission gate invokes bare for the *underlying* quote; the 60 s
  standard is `config.py:227 option_quote_max_age` (default 60), applied
  at `engine.py:507,86`. The contradiction is real; on current main it
  does not live at the exact lines either of us first cited.
- **§4 flow_pulse — CONFIRMED in the precise sense.** `spot_at_fire` is
  stored (`flow_pulse.py:454`) and never used to score the underlying.
  An option-contract tracker exists (`open_tracking:438`,
  `evaluate_tracking:487`, bid-minus-slippage exits, ~14-day time exit),
  so "unscored" is true of the underlying axis, not of all axes; the
  public block read 12 pulses / 0 tracked on 2026-10-26. The remaining
  §4 feed-table rows (apex, breakouts, gap_continuation, day_board,
  darkpool, iv_rank, squeeze) were not individually re-audited in this
  pass — PARTIAL by extension of the ones checked (setup_study,
  flow_pulse, tape), not independently confirmed.
- **§5 data claims — CONFIRMED in substance.** Daily seeding is 120
  calendar days (`stock_history.py`), consistent with his "~80 trading
  days"; no daily-bar backtest of the swing rules exists among the
  desk's artifacts. Book shape (three families, `premium_stop =
  entry × 0.65`, `premium_target = entry × 1.75`, `swing_ideas.py:154`;
  DTE 14–60) confirmed by the quick verdict and stands.
- **§6 ranking and constants — CONFIRMED, cites corrected.** The A+
  featured ranking is by raw `target_atr_mult` **ascending**
  (`smoothers_quality.py` module docstring + `rank_signals:164`) — the
  easiest targets rank first, which mechanically inflates the touch
  rate his §6.1 describes. The hand-picked `BEST_10` set is at
  `native_smoothers.py:15` (11 names), splitting individual vs compact
  alerts at `:273-277` — not at the `smoothers_quality.py:11-18` lines
  he cites, but the claim itself holds.
- **§6.2 backtest input — CONFIRMED available.** `native_smoothers_data
  .hourly()` requests 395 days of 30-minute bars per ticker
  (`native_smoothers_data.py:84`). That is a provider pull window, not a
  stored archive — the input exists on demand, as he says.

Everything else in the quick verdict — the Smoothers mechanism at code
level, the stale §2 live-book line, the scanner status omission, the
data-layer spot checks, the stop −2 tk caveat (Tier-1 n = 12, not 11:
11 equity-index bracket stops + 1 Turtle Soup) — I independently
confirm.

---

## C. Deliverable 2 — the futures re-runs on our harness

Method, stated once: one fixed merged dataset per leg, totals computed
once from it. Leg A (2026-09-23 → 10-23): the matrix merge
(`merged_trades-matrix.json`, 12,833 closed rows; 12,686 scored as
published). Leg B (2026-08-24 → 09-22): the prune replay's matrix-grid
merge (`B-matrix-cap1`). For these three pairs the trade sets are
**provably config-independent**: AOI·MCL, TS·MCL, and AOI·MES counts and
raw nets are identical across every matrix/production merge we hold
(cross-checked in four files). Raw harness fills put stops exactly at
the stop and targets exactly at the level (verified on all 8,973 stop
exits in the Leg A merge), so the desk rule is a clean adjustment:
every stop exit repriced −2 ticks (stop − 2 tk), targets untouched.
Sessions bucketed by entry timestamp, America/Chicago.

### C.1 Totals — his set vs ours (both legs, −2 tk)

| Pair | Claude | Quill (our harness) |
|---|---|---|
| AOI Fade·MCL | 69 tr / +$3,161.80 / PF 1.81 | 94 tr / +$3,037.52 / PF 1.52 |
| Turtle Soup·MCL | 123 tr / +$9,380.00 / PF 1.89 | 135 tr / +$10,520.00 / PF 1.95 |
| AOI Fade·MES | 73 tr / +$2,216.60 / PF 1.50 | 91 tr / +$2,200.94 / PF 1.38 |

Nets agree within −4% / +12% / −1%. Counts do not (ours +36% / +10% /
+25%), and the AOI·MCL PF gap (1.52 vs 1.81) is the largest substantive
disagreement: our extra trades net ≈ $0 in aggregate but dilute PF.

**Reconciliation (MES, the pair with artifacts on both sides).** His
per-trade CSV (`from-claude/2026-10-25-aoi-fade-mes-claude-trades-
repriced-x2.csv`, 73 rows, entries 2026-09-01 → 10-21) shares **zero
trade IDs** with our 91 — yet **63 of his 73 trades have an our-harness
entry within ±60 seconds** (35 stamped exactly −60 s, 28 exactly +60 s),
67 within 5 minutes, 71 within an hour. The two harnesses see the same
signal events and stamp them one minute apart — the evaluation-clock
difference already open on this desk as Task 003 Item 9. The residual
delta is his window (his CSV starts Sep 1, not Aug 24) plus stricter
gating in his port: the ~24 MES trades only our harness books net ≈ $0
combined (totals differ by $16). His §2 numbers are an honest read of
*his* set; they are not the production harness's set.

### C.2 Session splits (our harness, −2 tk)

| Pair | Asia 17–02 | London 02–08 | NY morn 08–11 | Post-11 11–17 |
|---|---|---|---|---|
| AOI·MCL | 24 / −$165 / PF 0.91 / WR 38% | **50 / +$3,380 / PF 2.39 / WR 60%** | 10 / +$292 / PF 1.52 | 10 / −$470 / PF 0.47 / WR 30% |
| AOI·MES | 16 / +$214 / PF 1.19 | 41 / +$754 / PF 1.28 / WR 44% | **30 / +$1,789 / PF 2.28 / WR 57%** | 4 / −$557 / 0 wins |
| TS·MCL | **43 / +$4,628 / PF 2.33 / WR 58%** | 33 / +$2,332 / PF 1.77 | 23 / −$32 / PF 0.99 | **36 / +$3,592 / PF 2.70 / WR 64%** |

Verdicts on his §2 claims:

- **TS·MCL "+$7.5k of +$9.4k in Asia at 64%" — PARTIAL.** Asia is our
  largest single bucket too (PF 2.33), but it is +$4,628 of +$10,520
  (44%, not 80%) at WR 58%, and London is also solidly positive on our
  set. The concentration is overstated.
- **AOI·MES "NY morning 64%, London 39%" — CONFIRMED in shape on both
  harnesses.** Ours: NY morning holds +$1,789 of +$2,201 (81%) at
  WR 57% / PF 2.28; London WR 44%, small positive net. His own CSV
  gives NY morning WR 63.6% and London 38.9% — the win-rates are his
  set's, the shape is real.
- **"All three lose after 11:00 CT" — PARTIAL: true for both AOI pairs,
  REFUTED for Turtle Soup.** AOI·MCL post-11: −$470, PF 0.47. AOI·MES
  post-11: −$557, 0 wins in 4. TS·MCL post-11 is **+$3,592 at PF 2.70,
  WR 64% — its best bucket by PF**. A blanket post-11 ban would have
  removed the live book's single best session cell.
- **"Sundays flat-to-negative" — NOT REPLICATED.** TS·MCL Sunday
  entries: 11 trades, +$784, PF 1.86. The AOI pairs have 2 Sunday
  trades each (MCL +$57, MES −$282) — too thin to verdict either way.
- **Consequence for §2.1's allowlists.** AOI·MCL 02:00–11:00 and
  AOI·MES 08:00–11:00 match our splits well (the AOI·MCL window keeps
  +$3,673 of the pair's +$3,038 net, discarding two negative buckets).
  **TS·MCL 17:00–02:00 does not survive our harness**: it keeps +$4,628
  and discards +$5,892 of positive London + post-11 net. Same proposal,
  opposite verdicts, pair by pair — which is why these are Josh's
  decisions in §E, not a package.

### C.3 The third-entry rule (§2.2) — REFUTED on our harness

Definition: within a pair-day (CT), a trade counts as "3rd+" when at
least two earlier same-day trades on that pair had already exited at
their stops before it entered (per-pair positions are sequential, so
exit-before-entry is the only possible reading). Our set: **115
pair-days with trades** (his denominator: 101), **30 pair-days** with a
3rd+ entry after two stops (his: 33), **59 flagged trades netting
+$841 at −2 tk** (+$977 raw). His −$662 does not replicate; on the
production harness the flagged trades are net **positive**, and the
proposed cap would have removed them. The day-sequences differ because
the trade sets differ (§C.1) — the rule may describe his port's set,
but it must not gate the live book on that basis.

---

## D. Deliverable 3 — the honest Smoothers headline

### D.1 Method

Read-only, from the stored weekly payloads themselves
(`/api/native/smoothers/history`, weeks 2026-09-21 → 2026-10-26, 420
payloads), scored with production's own `smoothers_scorecard
.option_result` — the identical function `simple.py` uses — so
`net_pnl` semantics match to the cent. The recompute ties out exactly
against the public aggregates (below), which validates the dataset.

### D.2 The numbers

Stored record: **WIN 275, LOSS 46, UNRESOLVED 43, OPEN 56** (current
week in progress).

- **Honest headline, all resolved picks: WIN/(WIN+LOSS) = 275/321 =
  85.7%.** Completed weeks only (through 2026-10-19): 264/310 =
  **85.2%**. Per-week rates swing widely: 100%, 68.4%, 100%, 81.0%,
  84.8% — two weeks record no LOSS at all.
- **Measured subset (the old headline's universe): 145 picks, all 145
  status WIN, zero LOSS**, net +$36,179.60. It decomposes exactly into
  the public block: 108 delivered / +$25,945.44 (106 with positive P&L —
  the displayed "98.1%") + 37 undelivered / +$10,234.16.
- **Excluded resolved picks: 176 of 321 (54.8%)** — 130 WINs and
  **all 46 LOSSes**. Exclusion reasons: `exit_quote_not_usable` 105,
  `entry_quote_not_usable` 71. His proposed 10%-exclusion invalidation
  threshold (§6) is exceeded five-fold.
- Claude's "60 of 61" does not reproduce from the full store (the quick
  verdict could not reproduce it from the live block either); the
  mechanism he described is exact, and worse in one respect than his
  subset showed: the measured universe contains **no LOSS at all**, so
  the old 98.1% was not merely a shrunken denominator — it was a
  denominator with the losses structurally absent.
- **Delivered-only honest rate: not computable from the read paths on
  this side.** Delivery evidence is the native outbox (DB-only;
  payloads carry no delivery fields). The structural point is
  independent of the delivery cut: `simple.py` dropped `net_pnl is
  None` *before* the delivered split, so no delivery filter can repair
  the old headline. As of today the system computes the delivered
  status-based rate itself (D.3).

### D.3 §6 fix status — SHIPPED during this verification

Main `476207d` (commit `09d90b6`, 2026-10-26, after Josh's go): the
LOSS/UNRESOLVED resolution now captures an exit quote, and `/api/simple`
counts WIN/LOSS by status for every delivered resolved pick, tallying
unmeasured resolved picks as `excluded` beside the headline — Claude's
§6 prescription, landed. The stored history above is the pre-fix
record and is not backfilled; D.2 stands as the audit of that record.
One production proof remains: the first Friday close under the new
code, where a LOSS carrying an exit quote should appear in the block.

---

## E. DECISIONS FOR JOSH (verified basis; nothing executed)

1. **Session allowlists, split by pair.** AOI Fade·MCL 02:00–11:00 CT
   and AOI Fade·MES 08:00–11:00 CT are supported by both harnesses'
   splits (§C.2) — replay-first is satisfied; live-money class, your
   call. Turtle Soup·MCL 17:00–02:00 is **not** supported on our
   harness (it would discard +$5,892 of positive net) — recommend
   against as proposed.
2. **Third-entry cap (§2.2): recommend NO action** — refuted on the
   production harness (+$841 for the flagged trades, §C.3).
3. **EOD option-chain snapshot job** — §1.3 confirmed: chain state is
   overwritten in place, nothing is retained, theta/vega are discarded
   at parse. Small build; the only fix that makes future IV-regime and
   greeks questions answerable at all.
4. **Historical bar backfill (Alpaca, $0)** — 2 years of 1-minute bars
   (+ 5 years daily). The store's true depth is currently unmeasurable
   past the API's 1,000-bar window and the seed is 7 days; this pull is
   the enabling step for the §4 scanner-family 2-year replay and the
   §6.2 Smoothers backtest.
5. **VIX daily history (CBOE, $0)** — §1.3 confirmed: only `vix:latest`
   exists; no regime study is possible until a series does.
6. **Stream newest-event-age watchdog** — §1.3's silent-death paths are
   confirmed in code (health reads "receiving" off any historical
   timestamp; futures health is written only inside the Databento
   callback; the Massive→Alpaca fallback records no error). Detect
   staleness by newest-event age instead.

## F. DECIDED DIFFERENTLY — no action unless Josh reopens

- **§1.2 promotion gate** (PF > 1.2 at −2/−4 tk, 2-week paper, demote at
  28-day PF < 1.0) vs the policies Josh ADOPTED 2026-10-25/26: futures
  seats need ≥ 50 Tier-1 fills + edge surviving p90 stop repricing;
  options need two consecutive positive 4-week windows, ≥ 40 trades,
  underlying-validated. His gate would have to supersede those.
- **`ict_trail` deletion (§2.4).** Dormancy CONFIRMED (no producer sets
  `is_trailer=True`; only the docstring references it). Deletion stays
  governed by Josh's 2026-10-24 decision: evidence check on whether any
  legs trail, due Oct 30.
- **Gao last-30-minute SPY momentum (§3.5)** — parked BY NAME under the
  paused strategy search; its MNQ version tested negative and was
  closed 2026-10-24. (The tension he notes is real: `ENTRY_CUTOFF_SEC`
  bars the book from the final hour entirely. Un-parking is Josh's.)
- **Swings TSMOM (§5.5)** — likewise parked by name.
- **Shadow-only ALL 0DTE (§3.1/§3.3)** — would unseat the 2026-10-25
  underlying-first seating (session_sweep_reclaim paper seats;
  non-clearing names already shadow under a pre-decided rule).
- **§1.2 four-week freeze** — Josh approved the 5M full-matrix replay
  as a scoped exception on 2026-10-26; the pause otherwise stands.
- **§5(b) intraday-extreme stop semantics** — not adopted; Josh's
  first-post-gap-snapshot rule (Item 5) is the standing semantics.

## Evidence index

- Re-run artifacts (this pass): `session_analysis.py` +
  `session_splits.json`, `smoothers_honest.py` + `smoothers_honest.json`,
  `count_pairs.py`, `engine_vars.py` (62-var live read), under the goal's
  `hidden_files/task004-verification-2026-10-26/`.
- His artifacts: `2026-10-26-matrix-summary-stop2tk.json`;
  `2026-10-25-aoi-fade-mes-claude-trades-repriced-x2.csv`.
- Live reads 2026-10-26: `/api/simple` (Smoothers + tape blocks),
  `/api/live-combo-scorecard`, Engine variables via Railway API.
- Main at publish: `476207d` (see D.3); verification basis `a9c1882`.

— Quill
