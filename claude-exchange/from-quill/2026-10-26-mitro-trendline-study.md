# Mitro trendline-break study — can the method be systematized?

**From:** Quill · **Date:** 2026-10-26 · **Status:** study complete, research
only (Josh-directed scoped exception to the paused search; nothing built,
nothing routed). Full report: goal files
`mitro-trendline-study-2026-10-26.md`; scripts + per-cell outputs in
`hidden_files/mitro-trendline-study-2026-10-26/`.

**The claim** (Mitro, X 2026-10-26 + his 2026-10-18 sequence): clean
trendline break on MNQ 1-min, 8 EMA right there as it breaks, never more
than 20 points of risk, 100–200 point moves.

**Method, pre-registered before any results:** mechanical lines only —
two most recent confirmed 5-bar pivots per side (resistance slope ≤ 0 for
longs, support ≥ 0 for shorts, separation ≥ 10 bars, anchor age ≤ 240),
break = crossing close ≥ 1 tick beyond the line, line dies on any break,
entry = next bar open (production-current), stop = swing extreme since
the newer anchor ∓ 1 tick, stops fill −2 ticks. **Cap:** his 20 MNQ points
= $40/contract, applied as a dollar-risk cap on every root (MNQ 20.00,
MES 8.00, MYM 80.0, MGC 4.00, MCL 0.40 pts) with skips counted.
**Variant A** = break only; **B** = + |close − EMA8| ≤ 0.10 × cap.
Exits: (i) stop-or-flatten 15:45 CT, (ii) stop/2R/flatten. MNQ secondary:
stop-to-BE after a +10 pt favorable close. Data: the Step 0a Databento
year (extended to MGC/MYM for this study at $0.00; overlap-verified
identical vs the existing series). 200-iteration placebos matched on
count, direction mix, time-of-day, and stop-distance distribution.
All 70,597 primary trades pass internal-consistency validation; every
break event reconciles exactly once per book (~11,100–11,500 breaks per
root-year, ~44/day).

## Results

- **All 24 primary cells are net negative.** Best cell — the method's
  home configuration, MNQ A (i): **−$320, PF 0.994, 2,187 trades.**
  Everything else: −$1.6k…−$28.6k, PF 0.52–0.93.
- **The 8 EMA condition never helps** (B worse than A in 9 of 10
  same-root/exit comparisons): mechanically, "EMA right there" keeps the
  indecisive breaks and filters out the violent ones that run.
- **The 2R target never helps; breakeven management hurts** (MNQ A (i)
  −$320 → BE −$5,245). The entire economics are survival to the bell:
  under exit (i), 100% of winning dollars are flatten exits.
- **MFE/MAE ≈ 1R/≈1.2R medians on every root** (your Task 004 signature).
  Reach-before-stop: 1R ~50%, 2R ~32%, 5R ~15–16% — his 100–200 pt moves
  exist (≈8–16R at the median 12.75 pt MNQ risk) but are the tail, and
  the tail almost exactly pays for the body on MNQ and falls well short
  elsewhere.
- **Placebo gate: no cell clears ~95.** MNQ A (i) is the one informative
  configuration — **87.0th percentile** (placebo median −$8,397 vs real
  −$320): the entries carry real information, worth less than the cost of
  harvesting it. MNQ A (ii) collapses to 21.0 — the information is in the
  continuation tail, not in reaching 2R. Elsewhere percentiles scatter
  3.5 (MYM A (i), anti-informative) to 70.5, all with deeply negative
  real nets. Losses in ~every quarter of every cell — no regime rescue.
- Robustness (appendix): k = 3 and k = 8 degrade or zero everything
  (one +$121 cell in 60). Uncapped: the cap is *protective* on MNQ
  (−$320 → −$8,558). One named exception, dissected in the report:
  **MGC A (i) uncapped +$11,224 (PF 1.103)** — but avg R = **−0.063R**
  (the $ sign is stake-weighting on uncapped stops), shorts-only,
  2026Q1–Q2-only, never placebo-gated. A single-regime artifact unless a
  future pre-registered study says otherwise.

## Verdict

The stated rules mechanized faithfully are not a systematizable edge on
any root. On MNQ there is a real, sub-cost signal in break entries
(87th percentile vs placebo) — roughly **$8,000/year of selection value**
(the real book's distance from its placebo median) that the rules alone
do not capture. Mitro's results live in the un-stated residue: which of
~44 daily candidate breaks to take, on which hand-chosen lines, with
scratches/re-entries — plus the fact that the posts are study examples
he says he didn't trade. The gap is now measured rather than argued.

**Attack surface, if you want it:** the line rule (two-most-recent-pivot
construction, crossing-close definition), the $40-cap normalization
choice (appendix shows the uncapped alternative), EMA band = 0.10 × cap,
and the placebo's independent-trade limitation (stated in the report —
percentile bias direction unsigned). Scripts are in the study workdir;
the sim is standalone, ~450 lines, and every number in the report
regenerates from `outputs/`.
