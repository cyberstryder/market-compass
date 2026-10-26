# BLOCKING — replay exit detection was close-only; corrected, the live pairs do not beat placebo

Claude, 2026-10-26 12:40 CT. Supersedes the exit-side numbers in my Task 001/003 drops.

## The defect
`engine.exits()` detects intrabar stop/target hits from **bar events**
(`engine.py:316  bars=self.db.recent(c,"bar",p["symbol"],…)` → `stop_detected_in_bar` /
`target_detected_in_bar`). My replay harness wrote only the `bar_window:<alias>` state key, never
`bar` events, so `recent()` returned nothing and every exit in the 12,605-trade matrix was decided on
the synthesized **minute-close quote only**. A wick through the stop did not stop the trade; a wick to
the target did not fill it. Production has 4 Hz MBP-1 quotes plus the bar scan, so wicks DO trigger.
Quill: 63 of our 66 matched AOI Fade|MES trades had identical exit reasons, which strongly suggests
Quill's day-chunked harness has the same property. Please verify whether its replay store carries bar
events during `exits()`, and confirm production's stop trigger path (quote `trigger<=stop` at 4 Hz).

## Corrected numbers (entries from the replay; exits re-simulated on bar extremes, stop −2 tk, 15:45 flatten)
`2026-10-26-matrix-rescored-bar-extremes.json` (all 53 pairs; stop-first and target-first ambiguous-bar bounds)

| pair | n | matrix (close-only) | **bar-extreme, stop-first** | target-first |
|---|---:|---:|---:|---:|
| turtle_soup·MCL | 123 | +6,012 / 1.43 | **+1,888 / 1.16 / 43%** | same |
| aoi_fade·MES | 73 | +1,014 / 1.18 | **+231 / 1.04 / 40%** | same |
| aoi_fade·MCL | 69 | +1,806 / 1.34 | **−188 / 0.96 / 41%** | same |
| rumers_box·MYM | 18 | +334 / 1.48 | +384 / 1.53 | same |
| golden_zone·MNQ | 12 | +127 / 1.52 | −43 / 0.83 | same |
| bos_fvg·MNQ | 695 | −4,127 / 0.86 | −2,342 / 0.90 | −1,732 / 0.93 |

The ambiguous-bar convention is almost irrelevant (both-touched bars are rare at 1-min on these stops).

## Placebo control (`2026-10-26-placebo-mfe-results.json`, script `2026-10-26-placebo_mfe.py`)
500 draws per pair: same trade count per session bucket the detector traded in, random entry bar in
that session, random side (and a same-side-mix variant), the real trades' stop/target distances,
same simulator.

| pair | real (corrected) | placebo mean / p5 / p95 | real's percentile |
|---|---:|---:|---:|
| turtle_soup·MCL | +1,888 | −1,167 / −4,818 / +3,140 | **89** (same-sides 92) |
| aoi_fade·MES | +231 | −708 / −2,564 / +1,510 | **79** (78) |
| aoi_fade·MCL | −188 | −806 / −2,883 / +1,242 | **69** (73) |

None reaches 95. The live book is positive drift inside the random-entry band, after costs.

## MFE/MAE (R units, from entry to as-run exit)
Median MFE ≈ 1.0R, median MAE ≈ 1.0R on all three; winners' MAE p75 = 0.38–0.75R → stops are not too
tight. In-sample exit sweep from the real entries: Turtle Soup·MCL best = 1R trailing stop, no target
(+2,854 / 1.29 / 53%); AOI Fade·MCL only positive at 3R target (+1,587 / 1.28) because it then runs to
flatten (4% of trades reach 2R). Hypotheses for a year of data, not changes.

## What I recommend to Josh (not my call)
1. Live futures → paper or 1 contract until a corrected, placebo-gated replay on ≥1 year of data says otherwise.
2. Both harnesses: write `bar` events in replay (or detect on extremes) — one-line fix in mine; re-run MES jointly.
3. The session-time structure (Asia MCL, NY-AM MES) survived the correction better than the patterns; test it as the hypothesis.
4. Year of MCL/MES/MNQ 1-min from Databento hist (~$30). Does the production DB hold more than ~6 weeks of futures bars (collector deployed 09-13)? `select min(ts) from events where kind='bar' and symbol like 'MES%'` — please report.

## Status of my prior claims
- Task 003 item 2 "fill-robust vs fill-fragile": the robust class shrinks to "not negative"; withdrawn as a positive finding.
- Session-window facts (10-26 plan §2): directionally intact under the correction (Turtle Soup·MCL Asia +2,774 / PF 1.60 vs London/NY ≤ 0), magnitude halved.
- Everything about entry latency, Smoothers' denominator, MU keyspace, swing hang, iceberg flag: unaffected.
