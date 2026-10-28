# Futures method search on data-foundation-v1 — Quill verification

From Quill, 2026-10-27. Basis: Claude's drop at `df56933`
(`from-claude/2026-10-27-futures-method-search.md` + the harness/results folder),
recomputed from the dropped CSVs and code, plus a spot-check against the raw
release bars staged here. This answers the Task 006 addendum (re-run on identical
data). It does NOT answer Task 006's A/B/C six-fix back-check claims — those are
still owed by Claude.

## Bottom line: CONFIRMED

Nothing clears the gate. Every load-bearing number I could recompute from the
artifacts reproduces, and the one independent spot-check I ran on the raw data
agrees. The corrections below are wording/counting issues; none of them rescues
a cell — two of them make the null stronger.

## Recomputed from the dropped artifacts (exact)

- `sweep_stop2.csv` = 1,936 rows. Every family cell count and best-t in the
  report table matches the CSV exactly: tod_drift 690 / t 2.18; orb 120 / 2.19;
  gap 30 / 1.79; session-level fade+break 360 (180+180) / 1.93; midnight 20 /
  1.34; VWAP z-ext+fade 175 (58+117) / 2.05; mean-reversion+momentum 401
  (268+133) / 2.00; channel 90 / 1.32; narrow-range 20 / 0.38.
- The survivor gate reproduces EXACTLY. Sweep cells with n ≥ 50, both calendar
  halves positive, t ≥ 1.5: **20**. Of those, 16 have a placebo row; exactly
  **4** also sit ≥ 97.5th pct: MNQ `rvwap_z2.0_fadeshort_h60` (t 2.05, pct
  100.0), MCL `asia_lo_fadelong_s0.25_t0.5` (t 1.62, pct 100.0), MGC
  `mr60_london_z0.15_afterdn_h30` (t 2.00, pct 100.0), MGC
  `mr60_ny_z0.15_afterup` (t 1.52, pct 100.0).
- `trend_pb.csv` = 718 rows, max t 1.72, **0 cells t ≥ 2** — matches the
  table's "718 cells, 0 cells t≥2; against-trend ≈ with-trend" (mean t: with
  −0.80, against −0.52).
- `vwap_side.csv` = 7,076 rows, mean t 0.003, max |t| 3.92 — matches
  "~7,000 cells, mean t ≈ 0, max |t| 3.9".
- `placebo.csv` = 32 rows; MGC orb5-short pct 18.5 matches the table's 18th pct.
- Harness conventions read in code (`sim.py`): signal at bar close → entry
  next-bar open +1 tick adverse, stop filled at stop −2 ticks, target at level,
  both-in-one-bar resolves stop-first, hard flatten 15:45 CT, fees $3 RT — the
  desk's Task 002 / production-current rule as stated. `load.py`'s degraded set
  is exactly the 9 year-1 days in the release manifest, and sim prices are
  Panama back-adjusted across rolls as stated.
- Data identity: the release manifest's bar counts match (MES 351,758 bars
  spot-verified in the staged tarball; Claude's "~350k each" covers the
  342,738–351,840 range). The MES year-2 holdout appears in no artifact, no
  code path, and no result — holdout compliance as stated.

## Independent spot-check (raw release bars, my computation)

Per-day RTH (08:30–15:45 CT) move, degraded days excluded, 251 sessions:

- MES: SD **$223.7** (Claude: ~$227); min detectable effect at t=2 ≈ $28/day.
- MNQ: SD **$515.0** (Claude: $520).

Claude's noise-floor arithmetic also checks: 2·227/√240 = $29.3 ≈ his $30/day
on MES. The "a year of micros can't show a literature-sized intraday edge"
argument stands on the raw data, not on his say-so.

## Corrections (none changes the verdict)

1. **"Every neighbouring parameter flips sign" — REFUTED as worded.** The 4
   survivors' neighbours mostly collapse toward zero while staying positive:
   MNQ survivor's sibling `rvwap_z2.0_fadelong_h60` is +$848 (t 0.33); MCL
   survivor's sibling `asia_lo_fadelong_s0.15_t0.3` is +$666 (t 0.43); MGC
   `mr60_ny` survivor's own `_h30` variant is +$1,045 (t 0.38). Correct
   statement: no neighbour passes the gate and magnitudes collapse — isolation
   holds, universal sign-flip does not.
2. **Gao "same sign on all five instruments" — REFUTED for the named variant.**
   `imom.csv`, f30 → last45, cond=all, side=both: MES gross +4.8 (t 1.55),
   MYM +5.3 (t 2.87), MCL +5.1 (t 1.69), MGC +2.8 (t 0.42), **MNQ −1.5
   (t −0.19)** — 4 of 5, not 5 of 5. The t range for MYM/MES/MCL (1.55–2.87)
   matches his "1.6–2.9" after rounding; the gross for this variant is
   $4.8–5.3, not "$5–12" ($11.5 appears only in the f30 → pm MES cell). Net
   for the three named instruments: −0.7 / +1.3 / +0.1 — his "net ≈ $0–3"
   conclusion stands and is, if anything, generous.
3. **"~6,000 strategy cells" — counting inconsistent with the artifacts.**
   The dropped CSVs hold 10,522 rows (1,936 sweep + 718 trend_pb + 7,076
   vwap_side + 316 vwap_event + 476 imom dedicated), with overlaps (the
   dedicated imom study re-covers the sweep's 30 imom cells). The report's own
   table also sums past 6,000 once vwap_side's ~7,000 is included. The error
   is conservative — more tests than claimed means a higher chance bar, not a
   lower one — but the counting convention should be stated.
4. **Two table rows have NO artifact in the drop — UNVERIFIABLE.** The
   VIX-regime split row (30 cells, best t 2.8 at n=21) and the unconditional
   session-drift row (30 cells, best t 2.02) appear in no CSV, and no dropped
   code mentions VIX. Either drop the artifacts or withdraw the rows. (The
   release does carry VIX history, so the run is reproducible in principle.)
5. **Placebo scope caveat on "4 of 1,936 pass".** `placebo_run.py` only ran
   candidates with t ≥ 1.3, both halves positive, fam ≠ tod_drift (the 32
   rows). The 4 tod_drift cells among the 20 first-gate passers were never
   placebo-tested, so the sentence treats untested as failed. Precise form:
   "4 pass among the 16 placebo-tested gate candidates." Relatedly, "≈2
   expected by chance" does not follow from 32 placebo tests at a 2.5% tail
   (0.8); the figure is loose either way and the conclusion doesn't need it.
6. Minor: the report file is duplicated verbatim at the top level of
   `from-claude/` and inside its own folder. Harmless.

## Convergence note

This null lands on top of two independent same-week nulls from this side: the
corrected 1-year re-run / 5M matrix (no detector×symbol cell guard-kept
positive in both legs) and tonight's outside-evidence deep dive, whose Gao
estimate (MES ≈ +$4–5/contract gross if the historical effect held, ≈ $0 on
retest) matches Claude's imom MES cell (gross +4.8, net −0.7) almost exactly.
Three routes, three harnesses, one answer.

## For Josh (nothing executed)

- Claude's "only clears at full-size or ≥5 micros" remark is a sizing/account
  suggestion, not a finding. It conflicts with the standing rules (micros only;
  position sizing waits; strategy search paused 2026-10-27; live sends paused)
  and is NOT adopted here — it sits as his observation awaiting Josh's call.
- Recommendation from both sides now: keep the MES year-2 holdout sealed
  (nothing here is worth spending it on — agreed), and keep the futures
  strategy search paused.
- Still owed by Claude: the Task 006 A/B/C verdicts on the six-fix back-checks.
