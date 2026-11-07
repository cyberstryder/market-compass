# Task 013 — Tester audit: independent re-implementation from registration texts only (2026-11-06)

**Opened:** 2026-11-06 by Quill, on Josh's commission. **Decision it feeds:**
whether the Market Compass gauntlet harness itself can be trusted. Josh
challenged the run of futures failures ("are you sure you're testing all
these right?") and commissioned a three-prong audit of the tester: (1) a
tick-level re-run of the freshest FAIL, (2) a cost-model audit against the
live book's real broker fills, (3) this prong — an **independent
re-implementation of two frozen registrations, from the registration texts
alone**, one FAIL and one PASS, with the trade lists diffed on our side.
Prongs 1–2 run on Quill's side; this drop is prong 3.

## What Claude is asked to deliver (drop in `from-claude/`)

**CODE ONLY** — no data, no results, no verdicts. Two self-contained Python
modules (stdlib only preferred; numpy permitted for the SH-SPY placebo RNG
as its registration specifies), one per spec below:

1. **`fvg_retest_arm_a.py`** — Arm A of the FVG-Retest Gauntlet, exactly as
   registered in **Registration Text 1** below (sha256 of the frozen file on
   Quill's side: `d00e2ea4b086344a854dd8b5d30032952748e3ee954b2c01859b1cad87020585`).
   Scope: the shared occurrence stream (§§3–5) and Arm A only (§6), with
   §7 exit simulation, §8 costs, §9 placebo, and the §10 pooled/per-root
   cell statistics for Arm A.
2. **`sh_spy_gauntlet.py`** — the Structure Hunter SPY gauntlet exactly as
   registered in **Registration Text 2** below (the S3 · Target 3R as-run
   configuration, all classes): detection, trade construction, bracket
   resolution, §5 scoring, §6 primary placebo, §7 cells/gates.

**Input format (both modules):** a CSV of 1-minute bars with header
`timestamp,open,high,low,close` — timestamp ISO-8601 with offset (the FVG
module reads MNQ/MES files, one symbol per file, America/Chicago stamps;
the SH module reads SPY regular-session bars). Each module must expose a
documented entry point that takes bar-file path(s) and writes a trade-list
CSV plus a JSON of the cell statistics its registration defines. Trade-list
columns: whatever the module needs, but at minimum symbol/instrument,
direction, entry timestamp, entry price, exit timestamp, exit price, exit
reason, gross points, risk points — timezone stated in a header comment or
the note file (desk rule 2).

**Ambiguity notes are a deliverable, not an afterthought.** With each
module, drop a short note listing **every ambiguity, gap, or
underdetermination you found in the registration text**, and the resolution
you chose for each. If a part of a spec cannot be implemented from the text
at all (e.g. a component defined only by reference to an external source),
say so explicitly, implement the narrowest reasonable interface around the
gap, and flag it — do not silently invent mechanics. These notes map each
registration's interpretation surface; they are half the audit.

## What happens on Quill's side (no action needed from Claude)

On receipt, Quill runs both modules **here**, unmodified, on the identical
panels the original gauntlets used (FVG: the Databento 1m MNQ/MES extract
2026-08-30 → 2026-10-23; SH-SPY: the cached Alpaca SPY 1m panel), converts
the bar files to the CSV format above, and diffs trade-by-trade:

- Arm A vs Quill's `trades_armA.json` (883 trades; pooled −$3,254.25,
  PF 0.701, placebo 0.0th pct under the original harness).
- SH-SPY vs the scored 365-trade list (`trades_scored_spy.csv`) and the
  parity list against Josh's TradingView export (`trades_parity_spy.csv`).

Agreement rate, every statistic that moves, and any verdict limb that
changes will be reported back on the desk as a new dated file, with
discrepancies classified (boundary / session-date, gate set, fill model,
detector input, other) per desk rule 1. Disagreements are settled by the
diff, never by assertion.

**Hard limits:** no credentials (rule 5); no live anything; this is a
measurement-methodology audit — neither module's output, whatever it shows,
promotes or demotes any strategy by itself.

---

# Registration Text 1 — FVG-Retest Gauntlet (frozen 2026-11-06)

# FVG-Retest Gauntlet — Registration (FROZEN)

**Commissioned:** Josh, 2026-11-06 ("Commission the gauntlet as spec'd").
**Frozen:** 2026-11-06 17:58 CT (2026-11-06T23:58:50Z), before any scoring
under this registration. No parameter in this document may change after
any result is seen. Spec ambiguities that surface mid-run are resolved in
the direction of the exemplar trade (§2) and disclosed in the report,
never silently.

**Question.** Stormzy's 2026-11-06 MNQ short was verified against Compass's
own bars (`files/mnq-drop-detector-check-2026-11-06.md`): break bars
11:31–11:32 CT, fresh bearish 1m FVG 27169.75–27189.50, an 11:34 retest
that put his fill inside that FVG, stop 27197.75 just beyond it, TP1
27123 at 11:36, low 26988.50. The break-entry BOS+FVG book is dead (the
2026-10-23 backtest: 836 trades, −$1,871.33, PF 0.91 pooled MNQ+MYM; MNQ
alone 404 trades, −$300.11, PF 0.98), and his management overlay replayed
on that population (2026-10-24) made it worse. Two ingredients of his
method have never been tested: **(1) entry on the retrace into the fresh
FVG instead of at the break, (2) a compression (wedge-proxy)
precondition.** This gauntlet tests exactly those, plus a control arm for
calibration.

---

## 1. Panel

- Databento Historical (GLBX.MDP3) 1-minute bars, continuous series — the
  identical extract the prior BOS+FVG backtest used:
  `hidden_files/futures-backtest-2026-10-23/data/bars_MNQ.c.0.csv` and
  `bars_MES.c.0.csv`. $0 new data spend.
- Coverage: 2026-08-30 17:00 CT → 2026-10-23 08:30 CT; MNQ 54,056 bars,
  MES 54,036 bars (~39 RTH sessions). Used as stored, including the
  in-panel contract roll; bars sorted by ts, duplicate timestamps dropped.
- The exemplar trade date (2026-11-06) is **not** in this panel; §9 governs
  how it is reported.
- No holiday calendar is applied; sessions are the CT calendar dates on
  which RTH bars exist.

## 2. Exemplar (for ambiguity resolution only — not evidence)

2026-11-06 MNQ short, per the verified detector-check: BOS close through
the 27168.00 / 27162.00 swing lows on the 11:32 bar; operative bearish FVG
27169.75–27189.50 formed by the 3-bar pattern completing on the 11:33 bar
(the 11:32 break bar is its displacement/middle bar); retest bar 11:34
(H 27190.75) put the fill inside the zone; his stop 27197.75 sat 8.25 pts
beyond the FVG's distal edge; the highest high after entry was 27193.25.

## 3. Definitions (reused from production code, stated exactly)

Reused verbatim in logic from `compass/ict_common.py` and
`compass/bos_fvg.py` (the same code the prior backtest drove):

- **Swing:** fractal, n = 2 (extreme vs 2 bars each side; ties require at
  least one strict inequality, per `fractal_swings`). A swing at bar i is
  *confirmed* at bar i + 2.
- **BOS:** per `detect_bos` — a 1m close beyond the latest confirmed swing
  (swing idx ≤ i − 2) with displacement: bar body ≥ 50% of its range, OR
  the close clears the swing by ≥ 0.5 × ATR(14) (Wilder, computed over
  bars up to and including bar i). One BOS event per confirmed swing: the
  first close beyond a given confirmed swing that satisfies displacement;
  later closes beyond the same swing are not new events.
- **FVG:** per `detect_fvgs` — classic 3-bar gap completing at bar i:
  bullish low[i] > high[i−2], zone (high[i−2], low[i]); bearish
  high[i] < low[i−2], zone (high[i], low[i−2]). `bottom`/`top` as in code.
- **Qualifying FVG width:** ≥ 1.0 pt (MNQ) / ≥ 0.25 pt (MES). (Frozen
  choice: production `detect_fvgs` has no minimum; a one-tick MNQ gap is
  not a fair value gap in any meaningful sense. Applies to all arms.)
- **ATR:** Wilder ATR(14), incremental over the full bar history to bar i.

## 4. Occurrence (shared by all arms)

An occurrence is a (BOS event, FVG) pair for one symbol:

- The BOS bar's time (CT) is within the entry window 08:30–14:45 CT.
- The FVG is in the BOS direction, is unmitigated at the BOS bar
  (`fvg_mitigated` semantics: no bar after formation, up to the BOS bar,
  has wicked into the zone), and its `formed_idx` lies in
  **[bos_idx − 30, bos_idx + 1]**. The +1 is the exemplar-directed
  resolution recorded here in advance of scoring: the displacement FVG of
  a break bar completes on the *next* bar's 3-bar pattern (the break bar
  is the middle bar), and the exemplar's operative FVG (27169.75–27189.50)
  formed exactly that way. Production's pairing (`formed_idx ≤ bos_idx`)
  would have paired the exemplar break with the earlier, higher FVG
  (27193.25–27212.25) that the trader did not use.
- If several FVGs qualify, the occurrence uses the **newest** (largest
  `formed_idx`) — production's freshest-first ordering, and the
  exemplar's operative zone.
- One occurrence per BOS event; an FVG consumed by an occurrence (taken
  or not) is never reused by a later occurrence.
- Occurrences are processed in time order per symbol. While a position
  from an earlier occurrence is open, or a limit order (§5) is pending,
  later occurrences for that symbol are skipped (one open position per
  symbol at a time). Each arm runs its own book independently.

## 5. Retest entry (Arms A, B, C — the new ingredient)

- At the BOS bar's close, a limit order rests at the FVG's **proximal
  edge**: short → `fvg.bottom` (first level a retrace up touches); long →
  `fvg.top` (mirror).
- **Fill:** the first bar after the FVG's formation bar whose range
  touches the proximal edge (short: high ≥ bottom; long: low ≤ top)
  fills at the edge price. The window is **15 bars after the break bar's
  close** (a fill bar must start within 15 bars of the BOS bar; fills
  never occur after 15:45 CT).
- **Run-away cancellation:** if, before any fill, price moves to a new
  extreme ≥ **2 × the risk** beyond the entry level in the trade
  direction (short: low ≤ entry − 2R; long: high ≥ entry + 2R), the order
  is cancelled.
- **Stop:** 1.0 pt beyond the FVG's distal edge (MNQ) / 0.25 pt (MES):
  short stop = `fvg.top + 1.0` (MNQ); long stop = `fvg.bottom − 1.0`.
  Risk R = |entry − stop| (equals FVG width + buffer by construction).
- **Risk cap:** skip the trade if R > 40 pts (MNQ) / 10 pts (MES).
- The stop is live from the fill bar itself; within any bar, the stop is
  tested before any favorable level (stop-first convention), as in every
  prior gauntlet in this project.

## 6. Arms

- **ARM 0 — control / parity.** The original BOS+FVG as historically
  traded (production `detect_signal` semantics): on the first bar after
  the BOS bar whose wick touches the occurrence FVG while its close still
  holds the zone's far edge (production `_touches` / `_holds`), enter at
  that bar's **close**; stop = distal edge ∓ 0.25 × ATR(14) at the signal
  bar (production `STOP_ATR_FRAC_DEFAULT`); target fixed at **+2R**;
  15:45 CT flatten. (Production's live target logic — opposing structure
  or 2R — is frozen to fixed 2R here per the commission; the historical
  book was exactly 2.00R on 383 of 404 MNQ trades.) Calibration role: on
  this panel, universe, and window it must land in the known dead
  territory (negative net, PF ≲ 1.0, cf. §Background). If Arm 0 comes
  back strongly positive, the run stops and the discrepancy is reported
  before any other arm is trusted.
- **ARM A — retest, plain.** §5 entry and stop; fixed target at **+2R**;
  no management; 15:45 CT flatten.
- **ARM B — retest + Stormzy management.** §5 entry and stop. Exit 50% of
  the position at **+1R** (fill at the level); on the trim, the
  remainder's stop moves to the entry price (breakeven). Thereafter the
  remainder's stop trails behind the most recent confirmed 5-bar fractal
  swing extreme (short: latest confirmed swing high; long: latest
  confirmed swing low; swings confirmed after the entry bar only; the
  stop only ever tightens; a newly confirmed swing takes effect from the
  next bar after its confirmation bar). No fixed target on the remainder;
  15:45 CT flatten regardless. The 1-contract position is scored
  fractionally (0.5 / 0.5), as in the 2026-10-24 replay's fractional
  sensitivity.
- **ARM C — retest + compression precondition.** Arm A exactly, except the
  occurrence is taken only if, evaluated on information available at the
  BOS bar's close:
  (i) the range (max high − min low) of the last 30 minutes of RTH bars
  (bos_ts − 30m, bos_ts] is ≤ 60% of the range of the preceding 120
  minutes of RTH bars (bos_ts − 120m, bos_ts], same session; and
  (ii) at least 3 confirmed swing highs with their extreme bars inside
  that same 30-minute window are strictly descending in time (short
  side; mirror: 3 confirmed swing lows strictly ascending, for longs).
  Breaks before 10:30 CT cannot satisfy the 120-minute RTH window and
  are excluded from Arm C only.

## 7. Exit simulation (all arms)

- Bars are walked strictly after the entry bar (the fill bar itself is
  tested for the stop, per §5; favorable levels are first tested on bars
  after the fill bar — conservative: a limit fill at the edge during the
  fill bar cannot also collect a favorable level printed in that same
  bar). *Correction recorded at freeze time:* for Arm 0, whose entry is
  at a bar's close, all levels are first tested on the following bar,
  matching the prior harness's `exits()` walk.
- Stop-first within every bar; if stop and target are both touched in one
  bar, the exit is the stop.
- Flatten: any position still open exits at the close of the last bar
  completing by 15:45 CT of the entry date.
- One contract per trade. Point values: MNQ $2.00/pt, MES $5.00/pt.
  Ticks: 0.25 pt both.

## 8. Costs (verbatim from the Structure Hunter as-run gauntlet)

Per `files/structure-hunter-asrun-gauntlet-2026-11-04.md` and its scoring
code (`sh_port.score_trades`): 1 contract; per trade
`net $ = (gross_pts − 2 ticks × tick) × point_value − 2 × $1.50`
(−2 ticks round-trip friction + $1.50/side commissions).
**Stress limb (p90):** exits at a stop level are worsened by a further
**4 ticks** (stop-type exits: initial stop, breakeven stop, trailing stop;
not target, trim, or flatten fills).

## 9. Placebo (delayed entry)

Per arm, 200 placebo books, RNG seeded 20261106 (frozen). Each book: for
every trade the arm actually took, a delayed twin — same symbol and
direction, same risk R in points — entered at the **close of the bar
15–60 minutes (uniform integer minutes) after the real entry bar**, stop
= entry ∓ R, and the arm's own exit rules (Arm A/C: +2R target, flatten;
Arm B: trim/BE/trail with the same R). A twin whose delayed entry falls
outside 08:30–14:45 CT, at/after 15:45 CT, or beyond the data is dropped
from that book. Twins are processed in occurrence order under the same
one-position-per-symbol rule. **Percentile is computed on net R** (sum of
trade R) per cell, following the as-run gauntlet's "Placebo pct (R)";
the dollar percentile is reported as a diagnostic only.

## 10. Cells and gate limbs (frozen)

Cells: per arm (0, A, B, C) × {pooled MNQ+MES (headline), MNQ, MES}.
Arm 0 is calibration only and is not gated.

For Arms A, B, C, a cell **passes only if every limb passes**:

1. n ≥ 50
2. net > 0 at base costs
3. net > 0 under the §8 stress repricing
4. PF > 1.0 at base costs
5. H1 and H2 both net-positive at base costs (H1/H2 = the panel's RTH
   session dates split at their midpoint — first half of session dates
   vs the rest; a trade belongs to the half containing its entry date)
6. Placebo: real net R ≥ 95th percentile of the §9 placebo distribution
7. Max drawdown < $10,000 at 1 contract (base-cost equity curve, trades
   in time order)

A pass earns a **shadow/paper proposal in the report only** — no
production changes, no alerts, no live anything. If Arms A and C both
fail every money limb (net base ≤ 0 and net stress ≤ 0 in the pooled
cell), the report states plainly that the BOS+FVG family is closed
permanently.

## 11. Integrity (required by the commission)

- Bar-count / date-range statement for the panel.
- Trade-count reconciliation across arms (shared occurrence stream).
- Independent recomputation: a second code path (or sampled
  hand-verification of ≥ 10 trades) must reproduce the headline numbers.
- Exemplar note: the panel does not contain 2026-11-06; the report states
  what the frozen rules would have done on the exemplar's verified bar
  levels (§2), labeled as one anecdote, not evidence.

---

# Registration Text 2 — Structure Hunter SPY, as-run configuration S3 · Target 3R (frozen 2026-11-04)

# Registration — Structure Hunter, SPY as-run configuration (S3 · Target 3R, all classes) Gauntlet

> **STATUS: FROZEN — 2026-11-04 12:37 CT (2026-11-04T18:37Z), before
> any gauntlet scoring under this registration.** The sha256 of this
> file is recorded in the hidden checkpoint
> `hidden_files/structure-hunter-spy-2026-11-04/freeze-spy.json` and
> in the milestone handoff to the parent — deliberately not inside
> this file (a file cannot carry its own hash). No constant,
> definition, gate, or counting rule below may change after this
> freeze; any change voids the run and requires a new registration.
> This is an **offline study on owned data ($0)**: no production code
> changes, no book changes, no data purchases. A PASS authorizes
> nothing by itself; it earns only a proposal for Josh's separate
> decision. Nothing is integrated, alerted, booked, paper-traded, or
> promoted under any outcome.

## Provenance and disclosure — why this registration exists

Structure Hunter (detection engine © Seckin_Tuncay v3.9 + test-preset
strategy wrapper) was tested on MNQ futures in two registrations
(S0: `997f17fd…`, parity gate failed as specified; as-run 3R:
`13b6e47f…`, gauntlet run 2026-11-04 — **all six cells FAIL**; the
family stands closed on futures as registered). On 2026-11-04 Josh
shared a TradingView run of the same script on **SPY** and reported
it "working really well on spy today"; after the parent read the
exports and ran the kill check, Josh commissioned this test
(2026-11-04 ~12:28 CT: "yes i want to test this"). This registration
follows the MNQ path exactly: config recovery → freeze → parity gate
→ gauntlet. The futures verdicts say nothing about this
configuration, and this run says nothing about the futures ones;
the instrument, timeframe, class set, and session rules all differ.

Evidence on the table, all of it **TradingView, gross of any cost
(commission 0 in the exports; no slippage exists in the TV
construction)** — never quoted without the GROSS label:

- Josh's real export (`SH_Strat_AMEX_SPY_2026-11-04.csv`): SPY,
  1-minute chart, **366 trades, net +$4,613 GROSS, PF 1.558, win
  rate 33.9%, max drawdown −$702**, window **2026-08-03 →
  2026-11-04** (67 NYSE sessions), fixed 100 shares per trade.
- Josh's placebo export (same chart, placebo on, delay 1–30 bars,
  TV seed 1): **325 trades, net +$1,645 GROSS, PF 1.181**.
- **Kill check (parent, already executed): NOT VOIDED.** The
  placebo keeps 36% of the real net (+$1,645 < +$4,613), passing the
  family kill criterion (void only if placebo ≥ real). Fingerprint,
  preserved here as a reported diagnostic for the gauntlet note:
  **real shorts +$1,808 (PF 1.40) vs placebo shorts −$197** — the
  short side's TV value is entirely entry timing; longs are mostly
  presence/drift (real +$2,805 vs placebo +$1,842, 66% kept).
- Josh's configuration evidence: an Inputs screenshot and the
  complete Pine source (2026-11-04 12:31 CT), then two follow-up
  screenshots (12:36 CT) — the preset dropdown open with **"S3 ·
  Target 3R" selected**, and the chart showing **timezone UTC-6
  (America/Chicago)**, **Regular trading hours** data, **ADJ**
  (adjusted) prices, and the info-table header "S3 · Target 3R" /
  STK mode. The parent's transcription is saved at
  `hidden_files/structure-hunter-spy-2026-11-04/josh-settings-readout-2026-11-04.md`.
- **Pine source note (stated plainly):** Josh's pasted source bytes
  are not archived as a file in this workspace, so a byte-level diff
  against the archived source could not be performed. Every
  load-bearing value in the readout was verified against the
  archived source text — the specification of record for the port,
  recovered from Josh's 2026-11-03 paste, sha256
  `f7f6a3ad81cda375fd43b16ef5883ed5cf302543952fb769d11d6d6f45b1fe9c`
  (preset resolution, Pine lines 215–217; the canEnter line 1224;
  instrument rules 1156–1160; the entry block's stop/target/size
  construction) — and the port of that source has now reproduced
  both of Josh's TV books (MNQ 97.1% of entries; SPY §3). If the
  pasted SPY source were later shown to differ from the archived
  source in any load-bearing way, this registration's fidelity basis
  would have to be re-examined; nothing in the readouts or the
  parity data suggests a difference.

**Question: does the as-run SPY configuration of this script —
S3R3 targets, all four classes, 1-minute RTH bars, no reverse on
opposite signal — show positive expectancy at executable prices on
SPY that its own delayed-entry placebo cannot match, pooled and per
gated class?**

## 1. The strategy — as-run configuration, pinned (zero free parameters)

The specification of record is the archived Pine source (above), as
resolved by the port (`sh_port.py`, fidelity-proven on MNQ) with the
single documented strategy-layer adaptation for this configuration
(`run_strategy_spy`, §3). Any input not listed below takes the
script's default value, as read out from Josh's settings.

**Detection (shared machinery, identical to the MNQ registrations)**
- Bar stream: regular-session bars only (the chart carries no
  extended-hours bars). Detection runs on every bar.
- Pivots: `ta.pivothigh / ta.pivotlow` length **5 / 5** (lagged 5
  bars); one pivot stream; a later same-type pivot that is more
  extreme replaces the last pivot in place.
- ATR: TradingView `ta.atr(14)` (Wilder RMA, Pine seeding) on the
  chart timeframe (1-minute).
- Minimum pattern height **1.5 × ATR**; level tolerance **15% of
  pattern height**; head excess **10% of height**; time symmetry max
  shoulder-span ratio **2.5**; prior trend required for reversal
  patterns (on); trendline touches **5 pivots** (4-pivot fallback
  after a pole test); flat threshold **0.25**; flag pole ≥ **2.0 ×
  pattern height**; max wait for a break after a pattern forms **60
  bars**. Pattern toggles: **all on except broadening (off)** —
  matching the export (20 signal names, zero Broadening rows).
  Volume confirmation computed but **not** an entry filter.

**Classes — ALL FOUR enabled (the section-2 toggles, all on)**
- **Reversal** (cat 1): H&S / inverse H&S, triple top/bottom, double
  top/bottom, cup & handle. One live neckline slot per direction;
  entry signal: a bar **close** through the neckline. Invalidation:
  the pattern's extreme pivot (cup & handle: the cup midpoint).
- **Line** (cat 2): triangle, wedge, rectangle, channel,
  classified from the two trendlines' normalized slopes and the
  end/start height ratio exactly as the script does. One live line
  pattern at a time, gated by the post-break / post-reversal block
  bars. Entry signal: a bar close through either trendline.
  Invalidation: the opposite trendline.
- **Flag/Pennant** (cat 3): the line module's pole-qualified
  reclassification, exactly as the script does.
- **Wyckoff** (cat 4): SOS/SOW schematics per the readout (climax
  lookback 50, climax volume ×2, bar range 1.5 ATR, range height
  2–8 ATR, min Phase B 10 bars, AR max 30, range lifetime 150,
  Spring/UTAD required, max depth 0.5, reclaim within 3 bars).

**Trade construction (as-run)**
- Signal queue: all signals generated on a bar are queued in the
  script's processing order (reversal breaks, line breaks, retests,
  Wyckoff); the entry loop takes the **last** qualifying signal.
  Retest signals are generated but **retest entries are OFF** (the
  readout: retest-entry filter off — only break signals qualify).
  Filters: none (volume off, BOS-trend off, chase 0/off).
- **Reverse on opposite signal: OFF** (raw input, not preset-derived
  — the settings readout; Pine line 1224: `canEnter` includes
  `(not inPos or revOpp)`). Consequence, pinned: **no entry of any
  kind is scheduled while a position is open** — no reversal, and no
  same-direction bracket re-issue. Signals arriving while in
  position are discarded, not queued.
- Reference price `ref` = the signal bar's close. **Stop** = the
  tighter of (a) pattern invalidation and (b) `ref ∓ 1.5 × ATR`
  ("Tighter of both", the S3-baseline stop). **Risk** R = |ref −
  stop| in points; a trade with R ≤ 0 does not exist.
- **Target = `ref + dir × 3.0 × R`** — the **S3R3 preset**
  construction (Pine: `eTgtM = TM_R`, `eR = 3.0`). Identification:
  the pre-freeze variant sweep over all six S3 Target presets
  scored exit agreement against Josh's export — **S3R3: 361 exits
  agreeing (of 361 exit-time matches), next best S3R2: 239, S3MM:
  239** — uniquely identified, and independently confirmed by
  Josh's dropdown screenshot. Skip if the measured move is already
  consumed (pattern target at or behind `ref`); the skip-if-target
  < 1.0R gate (minR = 1) is satisfied by construction at a 3R target
  whenever the measured move is unconsumed, and is retained as
  implemented.
- **Entry fill: next bar's open** after the signal bar
  (`process_orders_on_close = false`), friction-adjusted per §5.
  Bracket geometry stays the one computed at the signal bar.
- **Breakeven: OFF** (S3-baseline).
- **Exits:** resting stop and target from entry; **time stop 40
  bars** from the signal bar (close-all at that bar, filling at the
  next bar's open, per the script's close-all semantics as ported);
  **no session flatten and no forced close** (§2).
- **Bracket resolution:** stops fill on an intrabar **touch**
  (resting-order semantics); the script's on-chart outcome tracker
  is not ported into scoring. Within one bar touching both stop and
  target: **stop-first** (the house simulator convention, declared
  divergence from TradingView's intrabar heuristic — the same
  convention as both MNQ registrations). A bar opening beyond the
  stop or target fills at that bar's open (gap-through),
  friction-adjusted.
- **Sizing:** the script's size formula with Josh's inputs —
  `min(maxQty=100, floor($100,000 ÷ (risk × point value)))` —
  evaluates to **100 shares on every trade of this sample** (per-share
  risk never approaches $1,000), matching the export's constant
  qty 100. Unlike MNQ, there is no sizing exclusion: **the scored
  book is sized exactly as the TV book (100 shares fixed).** The
  futures daily-loss lock is inert (stock mode) and the futures
  session/flatten rules are inert (§2).

## 2. Session — as run

- Chart: **regular trading hours only**, 09:30–16:00 America/New_York,
  displayed in **America/Chicago** (export timestamps are ET − 1h;
  verified empirically in §3). No premarket or after-hours bars
  exist in the sample.
- Stock rules under Instrument mode Auto (SPY is a stock): **session
  filter OFF, flatten-at-close OFF, minimum hold 0 days**. Entries
  are permitted on any chart bar (subject to §1's occupancy rule);
  **positions may hold overnight** — 11 trades in Josh's export do,
  exiting at a later session's open on bracket gap-through or at
  the 40-bar time stop. No position is force-closed at any session
  boundary.

## 3. Fidelity basis — parity gate (executed under this registration)

- **Config identification (pre-freeze, the MNQ parity-phase role):**
  the proven detection engine ran once on the §4 bars; the strategy
  layer was swept over the six S3 Target presets. Under **S3R3** the
  port reproduced **364 of Josh's 366 exported entries (99.5%)**
  (session / direction / ±1 bar) and **361 of the 364 matched
  exits agreed on exit time and price** (max |Δprice| $0.021, mean
  $0.0044). Every other preset was far behind (exit agreement ≤
  245). The chart-timezone question was settled by the same run:
  the CT-stamped RTH hypothesis reproduces the export; no
  extended-hours hypothesis is needed or tenable at these numbers.
- **The gate (frozen):** under the pinned configuration of §1, the
  port must reproduce **≥ 90% of the exported entries** (session /
  direction / ±1 bar) over the full export window, with **exit
  agreement reported** (exit time equal and |Δexit price| ≤ $0.10)
  and **every mismatch classified** (warm-up / basis / sizing /
  detection / bracket). The formal parity run under this
  registration must reproduce the identification run's trade list
  exactly (run-time consistency check, as in the MNQ as-run
  registration §3); any divergence voids the run until explained.
  **If the gate fails: STOP — no scoring; the parity note carries
  the diagnosis.**

## 4. Data

- **Instrument:** SPY (AMEX listing on Josh's chart).
- **Panel (owned, $0):** Alpaca historical bars API, SIP feed,
  `adjustment=split`, 1-minute, pulled 2026-11-04 and cached at
  `hidden_files/structure-hunter-spy-2026-11-04/spy_1m_raw.csv`
  (97,707 bars, all hours, 2026-06-01 → 2026-11-04). The loader
  keeps regular-session bars only: **42,752 bars**, first
  2026-06-01 08:30 CT, last 2026-11-04 12:31 CT (see the Nov 4
  caveat below).
- **Adjustment basis:** TradingView equity charts are split-adjusted;
  Josh's chart shows the ADJ badge. The split-adjusted Alpaca basis
  is used. Dividend basis was checked empirically, as required:
  SPY's in-window ex-dividend date (2026-09-18) produces **no**
  parity signature — matched exit prices straddle it with max
  |Δ| $0.021 over 361 pairs, so no dividend-basis residuals exist
  in this sample to classify; had TV's basis shifted pre-ex-date
  levels by the ~$1.8 dividend, the parity numbers above would be
  impossible. The basis is therefore treated as matched, and the
  check itself is reported in the parity note.
- **Warm-up:** the engine runs from 2026-06-01 (44 sessions before
  the sample window) so detection state at the window start is the
  strategy's steady state. TV's own tester history is not archived;
  any residual early-window mismatch is classified warm-up in the
  parity note. (Identification run: zero extras and zero misses in
  the window's first ten sessions.)
- **Scored sample = the export window exactly:** trades whose
  **entry session** falls in **2026-08-03 → 2026-11-04** (67 NYSE
  sessions). Sessions are ET trading dates.
- **Halves:** the window's session list (sorted, 67 sessions) split
  at midpoint index `m = 67 // 2 = 33`: trades whose entry-session
  rank is `< 33` are H1, the rest H2.
- **Nov 4 caveat (declared):** the bars were pulled live at ~12:32 CT
  on 2026-11-04, so that session's bars end at 12:31 CT (13:31 ET).
  Every exported Nov 4 trade resolves by 12:04 CT — inside coverage.
- **Open at panel end:** a position still open when the bars end is
  not a scored trade; excluded and counted (the identification run
  had none).

## 5. Scoring frame

- **100 shares, fixed** (the TV sizing, §1). Per-trade gross points
  are the simulator's executable-fill points (next-bar-open entries,
  resting bracket, stop-first, gap-through at the open).
- **Costs (frozen by the commission):** commission **$0** (Josh's
  brokers charge $0 for online SPY share trades). Slippage **BASE
  1¢/share/side** = $2.00 per round trip at 100 shares, modeled as
  gross points − 0.02, × 100 shares. **STRESS 2¢/share/side** =
  $4.00 per round trip (gross points − 0.04). All dollar figures are
  at 100 shares; per-share terms are the point deductions above.
- **Statistics per cell:** n (scored), open-at-end count, signal
  count and late-break share (signal-level diagnostic from the
  engine stream), net $ (base costs), net $ (stress costs), mean $
  and mean R per trade (per-trade net R = (gross_pts − slip) ÷
  signal-bar risk, slip = the limb's own), PF, win rate, both
  halves' net $ and n (base costs), max drawdown $ (equity curve
  over the cell's trades in exit order, base costs), exit-reason
  counts (stop / target / time / reverse — reverse is impossible
  under §1 but reported if it ever appears), long/short split of
  the pooled cell (net $, n, PF — diagnostic).
- **Drawdown is reported, never gated.** Josh's standing budget — a
  five-figure modeled max drawdown is outside his deployment budget
  for any candidate — is a deployability note attached to the
  report, separate from the verdict.

## 6. Placebos

- **PRIMARY — the strategy's own null, ported faithfully.** For
  each signal the real book acts on, the placebo book takes the
  same signal with its **entry delayed by a uniform random 1–30
  bars** (the script's construction): at the delayed bar, stop =
  entry ∓ 1.5 × ATR (ATR stop only), target = entry ± (the signal's
  R multiple) × (placebo risk) — the R multiple here is **3.0** by
  construction of §1. The delayed entry is subject to the same
  occupancy (revOpp-off), discard, and time-stop rules as a real
  entry; a placebo signal whose delayed entry cannot occur is
  dropped (the script drops it the same way). Declared difference:
  Pine's `math.random` stream is not bit-reproducible outside
  TradingView, so the port reproduces the **construction**, not
  Josh's seed-1 draw sequence.
  **Books:** 200 placebo books, generated from the full signal
  stream (engine from 2026-06-01) with one numpy `default_rng`
  stream, **seed 20261104**, consumed book by book. Each book is a
  complete pass of the strategy simulator in placebo mode; a book's
  scored trades are its trades with entry session in the §4 window,
  and the per-cell placebo trades are the cell **partitions** of
  that book (the same partition rule as the real cells, §7).
  **Statistic (pinned):** per-trade net R = (gross_pts − 0.02) ÷
  risk (base slippage); a book's pooled net R for a cell = the sum
  over that cell's trades. **Percentile = 100 × (number of the 200
  books whose pooled net R ≤ the real book's pooled net R) ÷ 200.**
  Limb 3 of §7 reads this percentile. The same percentile computed
  on pooled net $ is reported as a labeled diagnostic only.
- **SECONDARY — house session-matched random entry (reported, not
  gated).** The house construction, adapted to this instrument with
  the adaptation declared: for each scored real trade of the cell,
  an independent uniform random entry bar within the **same ET
  session**, same direction; entry at the next bar's open; stop =
  1.5 × ATR at the signal bar; target = the real trade's R multiple
  (3.0) × that risk; the same bracket (stop-first) and 40-bar
  time-stop machinery; no flatten (none exists in this
  configuration); trades simulated independently. 200 books, each
  book containing one draw per real trade of the cell; statistic =
  percentile of the real book's pooled net $ (base costs) among the
  book nets (share of books strictly below), seed **20261105**, an
  independent stream per cell. Reported alongside the primary for
  every gated cell; **no limb reads it.**

## 7. Cells and frozen gates

- **Cells:** one book (classes all on, full queue arbitration, the
  strategy as run). Cells are **partitions of that book** by the
  class of the trade's entry signal: **pooled** (all trades),
  **Reversal** (cat 1), **Line** (cat 2) — the gated cells.
  **Flag/Pennant** (cat 3) and **Wyckoff** (cat 4) are **reported,
  never gated** (their TV counts, 9 and 11, are below any floor).
  The long/short split of the pooled cell is a reported diagnostic,
  not a gate.
- **Verdict, per gated cell — PASS requires ALL of:**
  1. **Net > 0 after base costs** (§5) **and PF > 1.0**.
  2. **Both halves net > 0** (H1 and H2, §4 split, base costs).
  3. **Primary placebo ≥ 95th percentile** (§6).
  4. **Stress-cost net > 0** (§5 stress limb).
  5. **Sample floor:** pooled cell n ≥ 50 scored trades; class cell
     n ≥ 30 scored trades. Below the floor the verdict is **THIN** —
     a measurement statement about the sample, not a pass and not a
     frequency judgment. (No THIN remedy is pre-declared for this
     instrument; a THIN cell stays THIN under this registration.)
- Any other outcome is **FAIL**. All limbs are reported win or lose,
  with the §5 statistics, the secondary placebo, and the late-break
  share. **No limb counts trades per day or any frequency measure.**
- A cell's verdict is its own. The pooled cell is the verdict on
  the strategy as run; a class cell is the verdict on that class's
  contribution within the same book.
- **TV→gauntlet bridge (reported):** for the pooled cell — Josh's
  TV gross figure (+$4,613 GROSS, his book), the port's gross on the
  scored book, then net at base costs, then net at stress costs,
  each step labeled. Josh's TV numbers are never quoted without the
  GROSS label.

## 8. Exclusions and change rule

- **No other preset is scored.** The six S3 Target presets were
  swept for identification only (§3); S0/S1/S2 elements and every
  other combination are not run. No parameter tuning after results:
  every constant above is the script default, a readout fact, or
  the recovered as-run preset; none may be adjusted in response to
  any readout.
- **Extended-hours data is not used** — the chart has none (§2).
- The on-chart "Pattern success" and Wyckoff S·T·K tracker figures
  are not scored and may not be cited as evidence for the family.
- Any change to mechanics, cells, sample, placebos, or limbs = a
  **new registration**, with the change and its reason disclosed at
  its head.

## 9. Integrity protocol

- This registration is frozen (header; hash in `freeze-spy.json`)
  before the parity gate is formally executed and before any
  gauntlet scoring. The only computations performed before this
  freeze are the data pull, the detection-engine run, and the
  §3 identification sweep (matching statistics only — no expectancy
  was scored).
- Per-stage checkpointing: the engine checkpoint, the parity trade
  file, and per-stage outputs are written once and treated as final
  in `hidden_files/structure-hunter-spy-2026-11-04/` (the VM
  restarts every ~2–4 hours; checkpoints are the recovery record).
- Headline numbers are **independently recomputed** from the saved
  trade files by a separate aggregation path (not the simulator's
  scoring functions) before the report is written — pooled and
  per-class net (base and stress), PF, halves, max drawdown, and
  the primary placebo percentile — including a **placebo-percentile
  cross-check under an independent seed** (a second 200-book set,
  seed **20261106**, reported, not gated) and at least one stored
  trade spot-checked end to end against the raw bars through a
  second code path. If the two aggregations disagree, the
  disagreement is resolved before any verdict is written. The
  report cites only recomputed figures.
- The run executes in
  `hidden_files/structure-hunter-spy-2026-11-04/`. No production
  code, book, or configuration is touched by this study at any
  stage.

---

*End of drop. Registrations above are verbatim copies of the frozen files on Quill's side (FVG sha256 `d00e2ea4…0585`); the SH-SPY registration's own hash is recorded in its freeze checkpoint per its header. — Quill, 2026-11-06*
