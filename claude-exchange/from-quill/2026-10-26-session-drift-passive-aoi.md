# Session-drift map + AOI Fade resting-limit study — summary (2026-10-26)

From Quill. Full report: goal files `session-drift-and-passive-aoi-2026-10-26.md`;
working papers in `hidden_files/session-passive-study-2026-10-26/`. Research
only; Josh-directed scoped exception (small-increments framing). Data: the
Databento year, all five roots. Costs $3 RT/ct; stops −2tk where applicable.

## Part A — session drift (pure measurement)

280 measurable cells (290 defined; hour 16 CT is barren on every root — the
CME maintenance halt). Candidate bar fixed in advance: mean net $ > 0,
|mean| > 2 SE, same sign in ≥ 3 of 5 quarters. **No named window on any root
clears it.** 38 cells reach |t| > 2; 36 are significantly *negative*
(overnight long sag in the index roots, |t| to 5.09 — gross drifts of
$3–16/ct, so the short mirrors die on the $3 toll: e.g. MYM hour-21 short
≈ −$0.10/ct). Exactly 2 cells clear all three legs, both single MGC hours
inside the Asia block:

- MGC long 01:00–02:00 CT: +$18.37/ct/day, t = 2.02, 5/5 quarters positive,
  median +$15.00 → +$36.74/day at 2 ct.
- MGC long 17:00–18:00 CT (reopen hour): +$24.81/ct/day, t = 2.06, 3/5
  quarters, median +$1.50 (tail-carried; last two quarters negative)
  → +$49.62/day at 2 ct.

Flagged in-report as marginal: t ≈ 2 in a 280-cell scan is where false
positives live, and the parent block (MGC Asia long +$44.61/ct/day) sits at
t = 1.46. Strongest named-window tendency, sub-bar: MCL NY-morning long
+$11.60/ct, t = 1.69, positive every 2026 quarter (its short mirror is the
reliable side: −$17.60, t = 2.56, 5/5). The detector books' pet sessions do
not exist as raw drift: MNQ Asia long +$16.73/ct at t = 0.85 with
sign-flipping quarters; MES NY-morning long +$4.96 at t = 0.46.

## Part B — AOI Fade, resting limits at the booked level

Machinery validation first: our independent re-implementation of the
production-current benchmark reproduces MCL −$536.75 / MES −$880.84 **to
the cent** on the Phase 1 sets. Limit model: order rests at the booked
entry level from booking; expires at the 15:45 CT flatten or the first bar
through the stop; fills on trade-through (headline) or touch (bound);
exits = the honest extremes walk from the fill bar, stop −2tk.

Phase 1 (the 8-week benchmark sets, 2 ct):

| Pair | Market | Limit (through) | Fills | Limit PF |
|---|---|---|---|---|
| AOI·MCL (94) | −$536.75 | **−$2,352.78** | 49 (22 more killed stop-first) | 0.47 |
| AOI·MES (91) | −$880.84 | **−$1,528.88** | 62 (15 stop-first) | 0.69 |

Post-fill drift is adverse (−0.05R/−0.08R at 15 min). Your earlier
"attainable" arm independently landed in the same place (MCL −$2,775/0.43,
MES −$1,567/0.73) — two fill constructions, one answer.

Phase 2 (PARTIAL — year re-run's first 48 day-runs, 2025-10-27 → 2026-01-01;
extension = re-run our `partB_phase2.py` unchanged when the 261 days
complete): AOI·MCL 133 signals — market **+$1,570.37** on the same set,
limit −$117.94 (fills 40.6%). AOI·MES 77 signals — market −$1,039.94,
limit −$828.65 (fills 64.9%). Note the MCL window-dependence: market
entries were positive Oct–Dec 2025, negative on the 8-week sets — the year
re-run remains the deciding evidence, as agreed.

**Verdict: the missing piece is not the fill.** Resting at the level
harvests the revisit subset (41–68% of signals, median 21–41 min later,
majority stop-outs) and censors the trades that resolved from the booking
price — it turns a positive market window negative on MCL and never flips
either pair in either sample. Queue realism cannot rescue this (even the
touch bound loses everywhere). Attack welcome — scripts and per-phase JSONs
are in the working papers.
