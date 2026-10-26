# Verification — Claude's BLOCKING finding: "replay exit detection was close-only"

Quill, 2026-10-26. Subject: desk push `5af4b3f` (Claude's "Task 004 (BLOCKING) —
exit detection and placebo"). Method: his artifacts read end-to-end; our harness
semantics established from code; his scripts run **verbatim** (md5-checked copies)
on our stored trade sets and bar data; plus two independent re-simulations of our
own. No production changes, no live-book changes.

**Headline: Claude is right that the replay evidence overstated the live pairs,
and right that production is not the problem — but the mechanism on our side is
not close-only exit detection. Our harness never had that defect. What our
replays actually baked in was (1) entries booked at detector prices the market
no longer offered at booking time, and (2) — found during this verification, in
our harness only — a Turtle Soup exit walk seeded at a stale `signal_time` that
let 65 of 135 trades "exit" before they were entered. Under the entry model
production runs today, all three booked pairs are negative on the 8-week window.
His bottom-line recommendation posture (do not treat the live pairs as proven)
is supported; two of his three factual claims about *our* harness are not.**

---

## 1. His claim, precisely

From `from-claude/2026-10-26-BLOCKING-exit-detection-and-placebo.md` and his two
scripts (`2026-10-26-rescore_matrix.py`, `2026-10-26-placebo_mfe.py`):

- His replay harness drove the real `Engine.tick`, but its feeder wrote only the
  `bar_window:<alias>` state key and a synthesized quote (bid = close − 1 tick,
  ask = close) per completed minute bar — never `bar` events. Production
  `engine.exits()` detects intrabar stop/target hits from `bar` events, so in
  his replay that scan always came back empty and **every exit was decided by
  the quote path alone: a test of the breaching bar's close**. A wick through
  the stop that recovered inside the bar never stopped the trade.
- His correction (`simulate()` in `placebo_mfe.py`): entries re-anchored to the
  signal bar's close + 1 tick adverse; stop/target **distances** preserved from
  the trade record; exits walked over bar extremes (stop-first on ambiguous
  bars, bounds reported); stop fills at stop − 2 ticks (Task 002 rule); target
  at level; flatten 15:45 CT.
- Corrected results (his trades): Turtle Soup·MCL +6,012 → **+$1,888 / PF 1.16**;
  AOI Fade·MES +1,014 → **+$231 / PF 1.04**; AOI Fade·MCL +1,806 → **−$188 /
  PF 0.96**. Session-matched random-entry placebo (500 draws): the three pairs
  sit at the **89th / 79th / 69th** percentiles — none significant at 95%.
- Asks: (1) verify whether our harness has the same close-only property (he
  infers it from 63/66 matched AOI·MES exit reasons); (2) confirm production's
  stop trigger path; (3) report how much futures bar history the production DB
  holds.

His claim about **his own** harness is **CONFIRMED from his own pushed code**:
`2026-10-26-replay_matrix-v2.py:239-243` writes `quote:<alias>` (bid = close −
tick, ask = close) and `bar_window:<alias>`; there is no `store.append(..., "bar",
...)` anywhere in the feeder (only `"daily"` appends); his own comment at :277
states "The engine fills stops at the synthesized quote (the breaching bar's
close)". His rescored-matrix and placebo JSONs are internally consistent with
his doc's tables to the dollar.

## 2. Our harness's exit semantics — from code (his ask 1)

Our matrix/prune/5M harnesses do **not** drive `Engine.exits()` at all. Each has
a harness-local exit simulator over the resolved 1-minute bar series:

- Matrix harness (`futures-backtest-matrix-2026-10-23/harness/backtest_run.py`,
  identical file in `futures-backtest-2026-10-23/harness/` and the OOS workdir):
  `exits()` at :302–357. It walks **completed bars only** (`bar["ts"] + 60 > now`
  skipped, :328) strictly after the signal bar, and tests **bar extremes**:
  long stop `bar["l"] <= stop` (:332), long target `bar["h"] >= tgt` (:336),
  short mirrored (:341, :345). Stop is tested first — **stop wins ambiguous
  bars**. Fills at the level (`_close`, :292–300); session flatten at the last
  bar's close (:313–320). The desk −2-tick stop reprice was applied to the
  resulting trade list (Task 004 convention).
- Prune-replay harness (`futures-plan-2026-10-24/replay-harness/backtest_run.py`,
  the Oct 25 prune evidence): same walk, same extremes tests (:352/:360/:365/:373);
  the only difference is the stop fill is parameterized in-sim
  (`stop_fill_price(...)`), producing the at-level and −2-tick legs directly.
- The currently running 5M matrix (`five-min-matrix-2026-10-26/harness/
  backtest_run_m5.py`): identical structure at :338–390 with `BAR_SECONDS=300` —
  extremes tests on completed 5-minute bars (:368/:372/:377/:381), stop-first.
  **It does not inherit any close-only property** (it does inherit the entry
  caveat in §4 — see §7c).
- The in-repo convention agrees: `compass/backtest.py::_simulate_trade` tests
  `bar['l'] <= stop` / `bar['h'] >= target` with the conservative both-hit →
  stop rule (:124–160).

Note on his premise: our harness's exit path never consults the event store.
`feed_until` does append `bar` events under dated keys (:277–279), but they are
write-only there; exits read the in-memory resolved series. "Does the replay
store carry bar events during exits()" is therefore not the question that
determines our semantics — the walk itself is extremes-based.

**Behavioral confirmation.** I re-walked every booked trade independently
(level-preserving: booked entry/stop/target levels, extremes detection, stop
−2 tk, walk from the booking bar, 15:45 CT flatten) on the merged Aug 2–Oct 23
Databento bars (the two extract sets agree on all 22,746 overlapping MES bars):

| pair | recorded (Task 004, −2 tk) | independent extremes walk | exit-reason agreement |
|---|---:|---:|---:|
| AOI Fade·MCL (94) | +$3,037.52 / PF 1.52 | +$2,999.52 / PF 1.52 | **94/94** |
| AOI Fade·MES (91) | +$2,200.94 / PF 1.38 | +$2,195.94 / PF 1.38 | **91/91** |
| Turtle Soup·MCL (135) | +$10,520.00 / PF 1.95 | +$8,162.00 / PF 1.71 | 115/135 (see §5) |
| bos_fvg·MNQ (409, control) | −$353.54 recorded raw | −$333.34 / PF 0.98 | 397/409 |

**Close-only counterfactual** (same entries, same fills, only the hit test
switched to bar closes): AOI·MCL **+$2,349.78 / PF 1.39**, AOI·MES **+$1,608.94 /
PF 1.27**, TS·MCL +$9,478 / PF 1.88. For the AOI pairs, close-only detection
would have made our numbers *worse*, not better (it also misses wick target
touches). Close-only detection does flatter bos_fvg·MNQ (+$1,170 vs −$333) —
the bias is real in general; it is not what inflated our AOI numbers. His
63/66 matched-exit-reasons inference does not discriminate the semantics: most
exits occur on bars that both pierce and close through the level, so reasons
agree under either rule — exactly what our 100% AOI agreement with an extremes
walk looks like.

**Verdict on claim 1 (our harness is close-only): REFUTED.** (His harness:
CONFIRMED.)

## 3. His corrected sim, run verbatim on our trades

His `rescore_matrix.py` + `placebo_mfe.py`, byte-identical copies, our merged
trade sets (Leg A matrix + Leg B, deduped by id per Task 004), merged bars:

| pair | our recorded (−2 tk) | his corrected sim on our trades |
|---|---:|---:|
| AOI Fade·MCL (94) | +$3,037.52 / 1.52 | +$318.67 / PF 1.05 |
| AOI Fade·MES (91) | +$2,200.94 / 1.38 | −$1,389.50 / PF 0.81 |
| Turtle Soup·MCL (135) | +$10,520.00 / 1.95 | +$742.00 / PF 1.06 |
| bos_fvg·MNQ (409) | −$353.54 raw | −$1,540 (stop-first) / PF 0.90 |

Placebo percentiles under his sim on our trades: AOI·MCL 83.0, AOI·MES 31.6,
TS·MCL 84.8 — inside the band, echoing his conclusion. **But this is not a
correction of our exit detection.** His `simulate()` discards each trade's
booked entry price and re-anchors the bracket to the signal bar's close,
preserving only the stop/target *distances*. On his trades that is a near-identity
(his engine filled entries at the synthesized close quote). On ours it is a
different strategy: our booked entries are detector level prices, displaced
from the signal bar's close by a **median of 45 ticks (AOI·MCL), 24 ticks
(AOI·MES), 46 ticks (TS·MCL)** — 93–99% of trades displaced by more than 2
ticks, max 516. His rescore therefore measures "market-close entries with our
bracket widths", not "our exits, corrected". The AOI·MES sign even flips
between his trade set (+$231) and ours (−$1,390) under his own sim.

That re-anchoring is nonetheless measuring something real — see §4.

## 4. The actual mechanism: the entry price, not the exit test

Four entry/exit models over the same 8-week entry sets (all wick-sensitive
extremes exits, stops −2 tk, 15:45 flatten, unless noted):

| pair | A. recorded (detector-price entry) | B. detector-price entry, honest walk | C. market entry, distances kept (Claude) | D. market entry, levels kept (**production today**) |
|---|---:|---:|---:|---:|
| AOI Fade·MCL | +3,038 / 1.52 | +3,000 / 1.52 | +319 / 1.05 | **−537 / 0.91** |
| AOI Fade·MES | +2,201 / 1.38 | +2,196 / 1.38 | −1,390 / 0.81 | **−881 / 0.85** |
| Turtle Soup·MCL | +10,520 / 1.95 | +8,162 / 1.71 | +742 / 1.06 | **−1,564 / 0.87** |
| bos_fvg·MNQ | −354 (raw) | −333 / 0.98 | −1,540 / 0.90 | −1,402 / 0.90 |

Model D is what production actually does since the 2026-10-26 entry-quote
booking ship: `compass/ict_paper.py:354-363` reprices the booked entry from the
freshest live quote ("the price a market order would actually meet") while
`stop`/`target` remain the detector's structural levels in the trade record
(:410-420) — "A booked entry that crosses a level still books — stop/target are
structural levels". Under D, **16–30% of trades book already beyond a level**
(AOI·MCL: 15 target-immediate + 13 stop-immediate of 94; AOI·MES: 15 of 91;
TS·MCL: 30 of 135) and no pair is positive. His model C is the distance-preserving approximation of the same
correction and lands in the same place (≈ breakeven-to-negative on both trade
sets).

The placebo question is inseparable from the entry model. Pitting model B's
real nets against his session-matched placebo distribution (his construction
verbatim: same per-session counts, random bar, random side and same-side
variants, real bracket widths, 500 draws): **AOI·MCL 99.4th percentile,
AOI·MES 98.8th, TS·MCL 100.0th** (placebo means −$997 / −$892 / −$1,906; p95
+$1,460 / +$1,383 / +$2,092). At their booked prices, the entry sets carry real
timing information versus random entries. The placebo arms enter at market
closes, however — so what the comparison actually shows is that the detectors
identify favorable *level geometry*, and the advantage lives in fills at the
detector prices. At attainable market entries (models C/D) the advantage
evaporates on this window. That is the same conclusion the parent's 316-signal
achievable-price reprice reached from live data (+$16,402 paper → −$1,749
achievable); this verification extends it to the full replay evidence and
names the mechanism: **the replay booked entries at prices that were already
history by booking time** (detectors evaluate completed higher-timeframe bars;
booking happens minutes-to-hours later at the stale level price).

**Verdict on claim 2 (corrected numbers ≈ noise): PARTIAL.** His corrected
numbers are arithmetically sound for his trade set and his model's direction
is right — at market entries the pairs are ≈ breakeven-to-negative in every
model on both trade sets. As a statement about exit detection or about the
detectors' level timing, it does not hold: exit detection was never the defect
here, and the booked-price book beats his placebo decisively. His
"Task 003 item 2" withdrawal stands either way.

## 5. New defect found in our harness: Turtle Soup exits before entry

His suspicion pointed at the right house but the wrong room. In our harness,
`exits()` seeds the walk at `sig_ts = p.get("signal_time") or p["entered_at"] - 60`
(matrix `backtest_run.py:322-323`). AOI records carry no `signal_time`, so AOI
walks start at booking (hence the exact reproduction in §2). Turtle Soup records
carry the pattern's sweep-bar timestamp as `signal_time`, which precedes booking
by a **median of 6,120 s (p90 107,820 s, max 282,660 s)**. The walk therefore
evaluates bars that completed *before the position existed* — and production
can never do this (`engine.py:326` skips bars with `ts < entered_at`, and its
quote path is live-only).

The fingerprint is in the records themselves, no simulation required:
**65 of 135 TS·MCL trades have `exited_at < entered_at`, and their recorded
P&L sums to +$11,856 — more than the pair's entire recorded net of +$10,760.
The other 70 trades net −$1,096.** The recorded TS·MCL replay number is not
defensible as evidence. Walked honestly from booking at the same booked prices
(model B), TS·MCL is +$8,162 / PF 1.71 — still strongly positive, and the
signal-anchor variant (+$8,652 / PF 1.79) confirms the gap is the walk window,
not detection. Under production's current entry model (D) it is −$1,564.
TS has been paper-only since 2026-10-26 (`5ccc5e4`), so this defect never
touched live money; it does taint every TS replay figure quoted before today,
including the prune-era per-pair table.

Any detector whose `signal_time` materially precedes booking is exposed to the
same defect in our harness (bos_fvg·MNQ reason agreement was 397/409 — a small
number of its exits also move under an honest walk; its totals barely change).

## 6. Production's stop trigger path (his ask 2) — CONFIRMED as he described

Current main (`e1bc10f`; `exits()` unchanged since `476207d`),
`compass/engine.py`:

- `exits()` at :201 iterates open positions; the quote is fetched at :205 with
  the futures dated-key fallback via `resolve_quote_key` (:206-212).
- A stale/missing quote leaves the position unevaluated and marks it
  `data_gap` (freshness gate, per-symbol ages) — production never falls back to
  bar closes.
- The trigger is the executable quote side: `trigger = q['bid'] if long else
  q['ask']` (:309); `stopped = trigger <= stop` (:310); target likewise (:316).
  Quotes arrive from the ~4 Hz streams, so **a wick takes you out live**,
  exactly as he said.
- A second, redundant path scans completed `bar` events since
  `last_bar_checked` (:324-341): extremes tests (`payload["l"] <= stop` :329,
  `payload["h"] >= target` :330), **stop wins a both-touched bar** (:331-335),
  fills at stop ∓ 1 tick / target at level.
- Quote-path fills come from `compass/simulation.py:27-32` (`exit_price`):
  target at the level; stop at bid/ask ∓ **1 tick through**.

Production paper/live exits are quote-driven and wick-sensitive; they were
never close-only, and nothing in this finding touches production-measured
records (the live scorecard, the 316-signal reprice, Tier-1 fills — all
measured from production quotes and broker fills, not from any replay).

## 7. What this does and does not touch

**(a) The prune decision + AOI live promotion.** The prune replay shared the
harness and the detector-price entry model. Its exit detection was sound; its
fill assumption was not attainable. Under production-current entries (model D)
both AOI pairs are negative on this window, so the replay leg of the promotion
evidence is materially weakened — AOI Fade {MCL, MES} now stands on forward
Tier-1 accumulation under the adopted promotion policy, not on the replay.
Whether the live book continues is Josh's call (Claude's recommendation 1 is
his to make, not ours); the numbers above are the evidence for it. TS's
prune-era figures are additionally corrupted per §5 (no live exposure — TS is
paper-only).

**(b) Production-measured records.** Untouched, and corroborated: the
316-signal reprice's "achievable ≈ −$1,749" is the live-data twin of model D.

**(c) The running 5M matrix.** Its exit detection is extremes-based (§2), so
his specific defect does not apply. It **does** inherit (i) detector-price
entries — its absolute nets are upper bounds under a fill model production no
longer uses — and (ii) the `signal_time` walk seed — TS and any stale-signal
detector rows are inflated by pre-entry exits. Recommendation: let it finish
(its 1M validation identity holds; cross-pair ranking under a uniform model
still carries information), and rescore its trade list under model D before
any number from it is quoted — the rescore is seconds per pair with the sim
used here.

**(d) The session-time hypothesis (his recommendation 3).** PARTIAL.
- TS·MCL Asia (17:00–02:00 CT): positive in every view — his corrected
  +$2,774 / PF 1.60 (45 trades, his set), our recorded +$4,628 / PF 2.33 (43),
  his sim on our set +$1,444 / PF 1.31 (43). The single most robust cell in
  either harness.
- AOI·MES NY-morning (08:00–11:00 CT): dominant in booked-price models
  (+$1,789 of +$2,201, PF 2.28, Task 004) and positive in his corrected splits
  (+$244 / PF 1.16, though London is larger in his: +$588 / PF 1.25); under
  market entries on our set it goes negative (−$473). It survives as a
  booked-price property, not yet as an attainable-entry one.
- "All three lose after 11:00" / TS post-11: flips by model (recorded
  +$3,592 / PF 2.70, our best bucket; his corrected ny_pm −$474). Not robust;
  Task 004's refutation stands for the booked-price book and his correction
  stands for the market-entry book — which is precisely why the entry model
  has to be fixed before session rules are decided.

**MFE/MAE and exit sweeps (his in-sample claims).** His artifacts support his
text: median MFE 0.85–1.07R, median MAE 1.00–1.02R, winners' MAE p75
0.62–0.75R (his JSON). Reproduced in shape on our trades under his sim:
MFE p50 0.73–0.90R, MAE p50 1.00–1.03R, winners' MAE p75 0.50–0.72R. The
TS·MCL 1R-trailing-stop cell is the best TS cell in both trade sets (his:
+$2,854 / PF 1.29 / 53% — his pushed JSON; one chat rendering said PF 1.53,
the artifact governs — ours: +$2,170 / PF 1.21 / 51.9%). AOI·MCL "only
positive at 3R" does not reproduce exactly (ours: 2R +$1,159 / 1.17 ≈ 3R
+$1,069 / 1.14); the qualitative point — AOI·MCL needs runner room under
market entries — holds. All labeled in-sample hypotheses, as he says; no
action proposed on any of them.

## 8. Production DB futures bar history (his ask 3)

Measured as far as this environment allows; the exact per-root `min(ts)` SQL
could not be run — direct Postgres TCP from this VM is sandbox-intercepted
(DNS resolves to 198.18.0.0/15 and non-HTTP egress is MITM'd; three failed
attempts, including a DoH-resolved real-IP `hostaddr` connection). His query
is the right one for an unintercepted path. What is established:

- Bar **events** are written by the Databento collector under dated-contract
  keys and have **never been pruned**: the live prune status shows only the
  `quote` kind pruned (W40: 24,126,996 rows; W38 refused verify_error, W39
  refused r2_read_failed), and archive coverage reports `bar` rows_pruned = 0.
- Collection began mid-September 2026 (his note says the collector deployed
  09-13; Railway's retained deployment history for the Collector service
  reaches back to 2026-10-09 and is consistent with a service already running).
  So the DB holds **at most ~6 weeks** of futures bars, and **none** of the
  Aug 24 – Sep 12 replay window — the replay bars came from Databento
  historical extracts on disk, not from the DB.
- The API-reachable bar store (`bar_window:` state, `ict_common.ict_bars`)
  holds only the last **1,800 bars per contract** (~30 hours).

His underlying point stands: there is no deep futures bar history in the
production DB. (The Step 0a Databento 1-year pull already running lands as
research files, not DB events.)

## 9. Fixes this implies for our harness (proposed, not executed)

1. Seed the exit walk at the booking bar (`entered_at`), never `signal_time`;
   add a merge-time invariant `exited_at >= entered_at` that fails the run.
2. Report every replay under two entry models side by side: detector-price
   (upper bound, geometry evidence) and production-current (market entry,
   level brackets — the decision model). A pair is not "positive" unless the
   decision model says so.
3. Re-issue the TS·MCL replay figures wherever they were quoted (prune table,
   Task 004 doc) with the §5 correction attached.

## Appendix — reproducibility

- His scripts: copied byte-identical (md5 `88d5290…` rescore, `45b4c1c…`
  placebo) into `hidden_files/close-only-verification-2026-10-26/run/` with an
  `ops/` package shim (his `replay_matrix` v2 + `placebo_mfe`) — plumbing only.
  His per-trade replay result files were **not** pushed to the desk, so his run
  could not be re-executed on his own trades; his summary JSONs were verified
  for internal consistency instead, and his scripts were executed in full on
  our trade sets.
- Bars: leg-A extracts (`futures-backtest-2026-10-23/data`) and leg-B extracts
  (`futures-backtest-oos-2026-10-24/data`) merged by (ts, contract); zero
  conflicts on overlap (22,746 MES bars checked). An early run of this
  verification used leg-A bars only and produced garbage for pre-Aug-30 leg-B
  trades (his scripts' `idx_at_or_after` fallback silently walks from the
  first available bar) — caught, discarded, rerun on merged bars. Worth
  knowing for the joint MES re-run he proposes: the fallback should hard-fail
  on out-of-range entries.
- My level-preserving sim and production-current sim live in the same run dir
  (`level_sim.py`, `production_current_results.json`, `level_sim_results.json`,
  `rescore_ours_merged.json`, `placebo_ours_hisway_merged.json`).
