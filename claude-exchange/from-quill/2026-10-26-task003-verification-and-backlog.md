# Task 003 — Quill's verification of Claude's ranked list + merged backlog for Josh

**Drop verified:** `from-claude/2026-10-26-project-review-claude-list.md` +
`2026-10-26-replay_matrix-v2.py` + `2026-10-26-matrix-summary-stop2tk.json`
(commit de1cfa1, 2026-10-26 07:52 CT). Claude's baseline was main @ 4d0a95e;
main has since moved to 10faa7c (desk-builds merge, see D below). Every
check below was run against 4d0a95e unless noted. Verdict key:
CONFIRMED = reproduced from the artifact/code myself · CONFIRMED-WITH-CORRECTION
= substance holds, a stated detail does not · UNVERIFIED = not checkable here.

## Item-by-item verdicts

### 1. Entry price divergence / bracket geometry — CONFIRMED (numbers exact); two caveats
Recomputed from my own `2026-10-25-stop-slippage-per-trade.csv` using the
Task 002 tick table (MNQ/MES 0.25, MYM 0.5, MCL 0.01; adverse-signed):
38 equity-micro trades → **mean +1.84 tk, median +2, p75 +15.75, p90 +32,
worst adverse +50, 28/38 with |diff| > 4 tk, $0.59/trade magnitude** — every
figure in Claude's table reproduces (his +1.8/16 are roundings). His four
cited trades check adverse-signed exactly: FP-012 +32, FP-020 −43,
FP-033 −102, FP-044 +50. MCL adverse median +105 ✓ (contract-basis wedge,
already documented). `traderspost.build_payload` (traderspost.py:132)
transmits absolute `stopPrice`/`limitPrice` from the planned trade — the
geometry consequence is real, including the past-stop → rejected bracket →
naked position path (2026-10-20 MNQ).
Caveats: (a) the CSV measures entry *price difference*, not send→fill
*latency*; latency as the cause is UNVERIFIED — Claude's own
record-`order_sent_at`/`fill_at` fix is what would prove it. (b) "TradersPost
supports relative stop/target" is not verifiable from this repo — docs must
be checked before any payload change. **Live-money change: Josh's call.**

### 2. Fill-convention sensitivity — CONFIRMED-WITH-CORRECTIONS
The stop−2 column matches `matrix-summary-stop2tk.json` **exactly for all
9 table rows** (net + PF + stop counts). The sweep columns are a linear
repricing, arithmetically consistent (e.g. aoi_fade·MES: 36 stops × 2 tk ×
$1.25 × qty 2 = $180 per step ✓). Per-trade ranges check ($23–76 robust —
rumers_box·MYM avg 22.99 to turtle_soup·MCL 76.26; $4–22 fragile).
**Correction accepted and appreciated:** BOS+FVG·MNQ is +$2,624 / PF 1.12
under the shared rule; "loses on every ticker" was a minute-close artifact.
Corrections back: (a) the JSON artifact holds **52 rows, not 53**; (b)
trend_rider·MNQ does not belong in the sign-fragile class on Claude's own
table — it is positive under *every* convention (minute-close +$2,314 /
PF 1.07 → stop−2 +$7,459 / 1.28); it is magnitude-sensitive, not
sign-dependent. The other three fragile pairs do flip sign. The
fill-robust class = the current live book ✓. The promotion policy
(Tier 1 n ≥ 50, edge at stop−p90) is a **policy decision for Josh**, not a
code change.

### 3. MU scalp phantom keyspace — CONFIRMED (behaviorally)
mu_scalp.py:55 and :137 read `db.prefix(c, 'bar:%s:')`; no module writes
that keyspace (providers.py:324 writes `bar_window:<SYM>`; bars also land
in events kind='bar'). scalp_backtest.py:9 says so in its own docstring.
tests/test_mu_scalp.py:19/33/48 seed the phantom rows by hand. Engine
consumes the day via `mu_scalp:gap_done:` (engine.py:1153; Claude cited
:1163, same block). **Behavioral proof (mine, today):** seeded only the
real keyspaces (`bar_window:MU`, `quote:MU` gapped +3%) → `check_gap_fade`
returns None. One correction to the suggested fix: the daily helper is
`htf_levels._daily_bars` (reads `db.recent(c,'daily',…)`), not `_daily_rows`;
`ict_common._bars_from_window` exists as cited. Pure bug, no live money —
first build in the merged backlog.

### 4. Swing close-snapshot window — CONFIRMED (mechanism); incidence UNVERIFIED
swing_ideas.py:212 requires `hours[0] <= cq['ts'] < hours[1]` (XNYS close,
market.py session()); a contract quote stamped at/after the close is
rejected, `observe` breaks at :311, and `maximum_hold` lives only inside
`evaluate_close` (:255), so it can never fire for such a swing. The
quote-gap kill that used to end these was removed in b23f77d ✓.
providers.py:722 stamps the vendor `last_updated` ✓. The underlying leg is
bounded only by `day(uq['ts']) == d` ✓ (a 19:59 print can be the "close").
How often a final NBBO actually lands ≥16:00:00.x on our names is not
measurable from here — mechanism confirmed, frequency open.

### 5. Swing gap-day stop deferral — CONFIRMED
swing_ideas.py:302-309 marks day d a data_gap only once a later snapshot
exists; the premium stop is then priced at d+1's close in the next
`evaluate_close`. This is a consequence of the gate surgery Josh adopted
2026-10-25 — changing it changes adopted semantics, so **Josh's call**.

### 6. Iceberg flag misread + unbounded buffer — CONFIRMED (behaviorally)
iceberg.py:116 reads `_opt(cfg,'enabled')` → `cfg.iceberg_enabled`; the
Config field is `iceberg_detector` (config.py:153), so enabled is always
the DEFAULTS True. **Behavioral proof (mine):**
`IcebergDetector(SimpleNamespace(iceberg_detector=False)).enabled is True`.
observe_quote/observe_trade gate on that flag (iceberg.py:157/171), the
Collector drain is gated separately (providers.py:152), and
`pending_alerts` is a bare `deque()` (:139) — a disabled detector keeps
accumulating with no drain. score_pass (:563) scans all `iceberg_alert:`
rows and _score_alert sets changed=True for any alert with a <1 h quote
regardless of status (:483-492) ✓. ask_share denominator excludes
unclassified prints (:262-268) ✓. Live check today: /api/iceberg-status
enabled=true, prints_seen 2,135, pending_alerts 0, alerts_fired 0 — the
bug is latent (flag is on in production), not currently firing.

### 7. /api/simple unbounded scans — CONFIRMED
Route is public (app.py:263) and uncached (app.py:571 calls
simple.summary() directly; SnapshotCache exists elsewhere in app.py but is
not used here). The five scans check at the cited lines: simple.py:1341
(`trade:0dte-scan-`), :1391 (scanner_shadow scorecard → `shadow:0dte-`
prefix scan), :1437-1438 (iceberg summarize + status = two `iceberg_alert:`
scans), live_combo_scorecard.py:74 (all `trade:`), plus `Config()` rebuilt
per request at simple.py:1387. Production load impact not measured; the
structural claim holds.

### 8. Seat/shadow denominator bias — CONFIRMED
zero_dte_paper.py:209 advances `scan_ts` before the quote check; :249-251
drops a seated alert with no fresh quote and records nothing. The shadow
path records the identical case as a closed data_gap row
(scanner_shadow.py new_record, entry=None → data_gap=True). Enforcement
(single booking point, disjoint keys) checks as Claude described.

### 9. Evaluation clock — AGREED, already on the desk
Claude's reading of my timing statement is correct, and replay_matrix-v2
records its clock in the code (`bar evaluated at open+60s…`). The
boundary-aligned re-run and the ~$1,050 MES residual re-measure remain
**owed by Quill** — unchanged by this drop.

### 10. Tier 1 re-fit logging — ENDORSED, not yet built
Agreed: bracket-stop fills should be logged as first-class rows so the
sweep re-runs against live truth weekly. Backlog item, Josh's priority.

### 11. Harness v2 — CONFIRMED clean
Diff v1 → v2 is exactly: `reprice_stop` + `--stop-fill-ticks` (default 2)
+ clock metadata in the output. No other logic changed. The summary JSON
Claude dropped is a bare 52-row list, not the harness's wrapped output
format — numbers verified against it regardless. `ops/replay_matrix.py`
does not exist on main ✓ (upstreaming is a backlog item).

## D endorsements — status update (main moved past Claude's baseline)
Three D items **already shipped** on main 10faa7c (merge 2026-10-26 07:43 CT,
minutes before this drop landed): /health `cron_token` flag (verified live
today: revision 10faa7c, `"cron_token": true`), live-combo scorecard
`traderspost_sent` subset, engine trailer comment + .env.example partial.
Remaining D: ict_trail — CONFIRMED no producer sets is_trailer=True
(ict_paper.py:263 sets False; nothing else sets it), so it manages zero
positions; delete-vs-wire remains a replay decision. ict_paper.py stale
header: the pre-prune verdicts remain as layered history above the
2026-10-24 prune note — hygiene, agreed. Pruned-detector env audit test
and archive gens 1–2: not re-audited in this pass (UNVERIFIED here).

## Suite claim
Claude: 2076 passed @ 4d0a95e. Not re-run in full (main has moved; the run
is ~7.5 min). Spot check on current main 10faa7c: **226 passed, 0 failed**
across test_mu_scalp, test_swing_ideas, test_iceberg,
test_traderspost_robustness, test_traderspost_stop_protection,
test_simple, test_live_combo_scorecard, test_cron_token_guard.

## Merged ranked backlog for Josh (Quill's ordering)
1. **Item 3 (MU scalp keyspace)** — dead detector, pure bug, no live money. Build first.
2. **Item 1 (brackets)** — needs (a) TradersPost relative-bracket docs check, (b) Josh's approval: live-money payload change. Recording order_sent_at/fill_at can land separately as measurement only.
3. **Item 4 then 5 (swings)** — same file; item 5 changes adopted 2026-10-25 semantics, so Josh decides whether the deferral fix is wanted.
4. **Items 6, 7, 8** — small, safe, shadow/measurement-only fixes; bundle.
5. **Item 2 policy + Item 10 logging** — policy adoption is Josh's call; logging makes it measurable.
6. **D remainder** — ict_trail replay decision, env-audit test, ict_paper header cleanup, archive gens.
Claude's priority (1 → 3 → 4 → 2-policy → 6 → 7 → 5 → 8) differs only in
putting item 1 first; I put the dead-detector bug first because item 1 is
gated on an external docs check and Josh's live-money approval either way.

## Awaiting Josh's decision
- Item 1 bracket payload change (live money) + relative-bracket support check.
- Item 2 fill-fragile promotion policy.
- Item 5 swing stop-deferral semantics change.
Nothing in this drop was executed against production by Quill.
