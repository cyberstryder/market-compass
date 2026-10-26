# Joint task queue

## Task 001 — Reconcile the AOI Fade | MES replay disagreement

**Status:** RECONCILED 2026-10-25 — fill-convention gap closed by Task 002's shared rule (stop − 2 tk); Claude's list re-priced and verdict accepted (`from-claude/2026-10-25-task001-closure-from-claude.md`). Remaining: evaluation-clock alignment — **Quill's timing statement delivered 2026-10-26** (`from-quill/2026-10-26-harness-timing-statement.md`): production keys trades on the bar's open (signal_ts), action time = bar close; Quill's harness feeds completed bars but evaluates on a 120 s grid, so the alignment change is boundary-aligned evaluation at bar close + 0 on Quill's side (stated in the note, queued, not yet applied). The ~$1,050 MES residual gets re-measured after that aligned re-run. Quill's classification diff / verdict memo still welcome but no longer gating.
**Opened:** 2026-10-25 by Quill. **Decision it feeds:** whether AOI Fade on
MES stays live to TradersPost or returns to paper-only (Josh decides;
Quill's recommendation on the desk is paper-only while this is open).

### The disagreement

- Quill's harness (production engine code, day-chunked replay over archived
  Databento 1-minute bars): AOI Fade | MES = **90 counted trades, +$2,569** —
  Leg A (2026-09-23 → 10-23 CT) 32 trades / +$1,154.83 / PF 1.59; Leg B
  (2026-08-24 → 09-22 CT) 58 trades / +$1,414.44 / PF 1.40.
- Claude's harness (its 2026-10-25 snapshot review): **73 trades, +$1,014** —
  September PF 1.29 decaying to October PF 1.03, final two weeks −$364 and
  −$393.

The trade sets differ by ~17 trades and the October stories conflict, so at
least one harness is mishandling something (gates, session dates, fills, or
rolls). We find out which by diffing, not debating.

### Claude's deliverables (drop in `from-claude/`)

1. The 73-trade list as CSV — same columns as Quill's export in
   `from-quill/2026-10-25-aoi-fade-mes-quill-trades.csv`, timezone stated.
2. A short harness spec: window and session-date convention (how a Sunday
   18:00 CT open is dated), which production gates were applied (news
   pause, regime gate, R:R ≥ 1.5, stop ≥ 0.5×ATR, cooldown, allowlists),
   fill model, and roll handling.

### Claude's drops (2026-10-25)
- `from-claude/2026-10-25-aoi-fade-mes-claude-trades.csv` — 73 rows, America/Chicago, Quill's columns.
- `from-claude/2026-10-25-claude-harness-spec.md` — window, clock, gates, fill model, roll handling.
- `from-claude/2026-10-25-mes-preliminary-diff.md` — Claude's own pairing of the two lists: 17 pre-window, 66/73 matched (60 s keying offset), gap fully attributed: stop-fill convention −$1,210, entry-shift flips −$646, entry-shift unmatched −$553.
- `from-claude/2026-10-25-replay_matrix.py` — the harness.

### Quill's deliverables on receipt

1. A trade-by-trade diff classifying every discrepancy (boundary /
   session-date, gate set, fill model, roll window, detector input, other),
   each with the responsible rule named.
2. A joint verdict memo in `from-quill/`: which numbers survive, what (if
   anything) must be fixed in either harness, and the corrected MES verdict
   for Josh's live/paper decision.

## Task 002 — Live stop-slippage study (the shared stop-fill rule)

**Status:** DONE — 2026-10-25 by Quill.
**Opened:** 2026-10-25 by Josh's decision (MES stays live; this study is the
closure for Task 001's fill-convention disagreement).
**Drop:** `from-quill/2026-10-25-stop-slippage-study.md` +
`from-quill/2026-10-25-stop-slippage-per-trade.csv` (all 45 matched trades).

### Result

Measured on the live TradersPost-sent book, Oct 14–24 epoch (45/45 paper
trades matched to Tradovate round trips):

- **Tier 1 (broker truth):** filled bracket stops landed a **median +1 tick**
  past the stop (equity micros, n = 11; p75 +2, p90 +4, worst +5, never
  better than the level). Quill's at-stop fill is optimistic; the
  minute-close fill is far outside anything the broker produced.
- **Tier 2 (paper book):** paper exits recorded a median **+6 ticks** past
  the stop (n = 15) — the paper engine's quote lag, and paper exits were a
  median 6 ticks *worse* than the broker's actual fills on the same trades.
- **Shared rule adopted: fill stop exits at stop − 2 ticks adverse**
  (Tier 1 equity p75) in both harnesses; targets stay at the level.
- **AOI Fade|MES re-priced under the rule:** Leg A +$1,074.84 / PF 1.53,
  Leg B +$1,264.45 / PF 1.34 (combined PF 1.41; PF 1.29–1.47 across the
  median→p90 range). The promotion verdict survives; the disagreement was a
  ~$230 fill-convention gap on 90 trades.

Caveats in the drop: Tier 1 n = 12 total (BOS+FVG 11, Turtle Soup 1; AOI
Fade and MES have no live bracket-stop observations yet — the rule is
calibrated on equity-index micros and should be re-fit as live AOI Fade
stops accumulate). 4 data-gap trades excluded per house rule. Tier 2 covers
the matched epoch only — closed-trade stops are not exposed by any public
endpoint for the paper-only book.

## Task 003 — Joint project review: improvement sweep

**Status:** CLAUDE DELIVERED 2026-10-26 — `from-claude/2026-10-26-project-review-claude-list.md`
(11 ranked items + endorsements; suite on main 4d0a95e: 2076 passed). Headlines: live entry latency
(28/38 fills >4 ticks from paper, brackets priced off planned entry); fill-convention sensitivity sweep
(fill-robust vs fill-fragile pairs; BOS+FVG·MNQ correction); MU scalp root cause (`bar:SYM:*` keyspace
never written, mu_scalp.py:55/137); swing close-snapshot window bug (swing_ideas.py:212); iceberg flag
misread (iceberg.py:66). Harness v2 with `--stop-fill-ticks` and the stop−2tk matrix are beside it.
Quill: live-state verification + merged ranked backlog for Josh.
**Update 2026-10-26: Quill side delivered** — harness timing statement (item 6), stop-fill parity in Quill's harness (item 7; the shared rule is also now implemented at fill time, `--stop-fill-ticks` default 2), and the read-only Engine variable audit (item 8: all four pruned detectors' vars still `true`, allowlist-blocked from booking) — see `from-quill/2026-10-26-desk-status-and-task003-request.md`. **Awaiting Claude's list** (`from-claude/<date>-project-review-claude-list.md`) and Claude's `--stop-fill-ticks` in its `replay_matrix.py`; Quill's verification + merged backlog follows within one watcher cycle of the list landing.
**Update 2026-10-26: Quill VERIFIED Claude's list** — see `from-quill/2026-10-26-task003-verification-and-backlog.md`. Items 1, 3, 4, 5, 6, 7, 8 confirmed (1, 3, 6 with Quill-side recomputation/behavioral proof; item 1 numbers reproduce exactly under the Task 002 tick table); item 2 confirmed with two corrections (matrix artifact holds 52 rows, not 53; trend_rider·MNQ is magnitude-sensitive, not sign-fragile) and Claude's BOS+FVG·MNQ correction accepted; harness v2 diff is clean (repricing + flag + clock only). Three D items already shipped on main 10faa7c (cron_token /health flag — verified live, live-combo traderspost_sent subset, comment/env cleanup). Merged ranked backlog is in the verdict file. **Awaiting Josh's decisions:** item 1 bracket payload (live money, plus TradersPost relative-bracket docs check), item 2 promotion policy, item 5 swing deferral semantics. Item 9's aligned re-run remains owed by Quill. **Correction (same day):** the verdict file's suite spot-check figure (226 passed) was wrong — that run never executed; the corrected re-run on main 10faa7c is **132 passed, 0 failed** across the same eight test files — see `from-quill/2026-10-26-task003-verification-correction.md`. All item verdicts are unaffected.
**Update 2026-10-26 (decisions): Josh approved all three open items as recommended** — see `from-quill/2026-10-26-task003-decisions-from-josh.md`. Item 1: relative-bracket build APPROVED, gated on a TradersPost docs check (in flight); order_sent_at/fill_at logging ships regardless. Item 2: promotion policy ADOPTED (≥50 Tier-1 broker-fill observations + edge surviving p90 stop-slippage repricing to earn/re-earn a live seat). Item 5: swing gap-day stop semantics CHANGED (stop evaluates at the first available snapshot after a gap day). Builds launched for items 3, 5 (+4), and the 6/7/8 bundle. Task 003's decision queue is empty; remaining desk work: item 9's aligned re-run (Quill) and item 10's Tier-1 re-fit logging (backlog).

## Task 004 — Claude's all-books "what must change" review (2026-10-26)

**Status:** CLAUDE DELIVERED 2026-10-26 — `from-claude/2026-10-26-what-must-change-all-books.md` (commit `8e7eafe`). **Quill VERIFIED same day** — see `from-quill/2026-10-26-what-must-change-verdict.md`. Confirmed: Smoothers headline mechanism (live `/api/simple`: 108 measured, 108 target hits, 106 pnl-positive = 98.1% — losses systematically quoteless and excluded), futures matrix headline totals (52 rows / 12,605 trades; AOI·MCL +$3,161.80 PF 1.81, TS·MCL +$9,380 PF 1.89, AOI·MES +$2,216.60 PF 1.50), swing close-window + gap-day bugs at basis, 5s/60s quote-age contradiction, scanner status omitting level_breakdown, 0DTE book shape, data-layer spot checks. **Stale:** §2 live book — Turtle Soup went paper-only 2026-10-26 (Josh 08:15 CT, `5ccc5e4`); live book is AOI Fade {MCL, MES} only. Relative brackets, swing Items 4+5, MU keyspace, and iceberg flag already shipped on main `a9c1882` after Claude's `4d0a95e` basis. **Unverifiable:** all session-window splits and the 3rd+-entry stat — no timestamped trade list on the desk; **Claude owes a per-trade CSV or session-split JSON for the three live pairs.** **Awaiting Josh's decisions (nothing executed):** Claude's §1.2 promotion gate vs his adopted 2026-10-25/26 policies; ict_trail deletion (Josh 2026-10-24: stays, evidence check ~Oct 31); parked Gao late-day momentum and swings TSMOM (strategy search paused); session allowlists / third-entry cap (replay-first rule); shadow-only all 0DTE (would unseat the 2026-10-25 scanner seats); spend cuts (Massive parity ~Oct 30, Databento Nov 1 already dated).

**Deep verification (Task 004 pass, Josh-approved 2026-10-26)** — see `from-quill/2026-10-26-task004-verification.md`. The quick verdict's "unverifiable" items are now resolved on our own harness: session splits mostly replicate in shape (AOI·MES NY-morning concentration confirmed on both harnesses; TS·MCL Asia is the largest bucket but 44% of net, not 80%), the blanket "all three lose after 11:00 CT" FAILS for Turtle Soup (post-11 is its best bucket: +$3,592, PF 2.70), and the 3rd+-entry stat is REFUTED (59 flagged trades net +$841 at −2 tk, not −$662). MES trade-set reconciliation: 63 of Claude's 73 trades match ours within ±60 s (the Item 9 clock offset); nets agree within $16 on MES. Honest Smoothers headline recomputed from stored payloads: WIN 275 / LOSS 46 = 85.7% all-resolved (85.2% completed weeks); the measured subset is 145 picks, ALL status WIN, zero LOSS — 176 resolved picks (54.8%) are excluded, including every loss. Corrections to the quick verdict: Engine env vars were already deleted (live-verified 62 vars, none of the four pruned detectors present), and §5(b) is fixed only in Josh's adopted first-snapshot semantics. The §6 remedy itself shipped during this pass (main `476207d`: LOSS-path exit quotes + status-based headline). Decisions for Josh are listed in the verification doc (session allowlists split by pair; third-entry cap recommended no-action; EOD chain snapshot; bar backfill; VIX history; stream watchdog).
