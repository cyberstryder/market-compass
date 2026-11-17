# Medusa Condition Screen — Frozen Registration (2026-11-17)

**Approved by Josh 2026-11-17 10:56 CT** ("Have me take one bounded,
pre-registered shot at it"). **This registration was written and frozen
before the run. No family, threshold, limb, or definition in it may be
changed after results are seen.** One pass only — no iteration, no added
conditions, no threshold tuning after seeing results.

## Question

The author's Medusa EOD signal reproduced as a coin flip (ungated 15:15
sign accuracy 49.1% over 499 SPY sessions, 2024-10-28 → 2026-10-23;
reproduction report: `files/medusa-gate-test-reproduction-2026-11-17.md`).
Does any pre-specified condition, knowable at the 15:15 ET decision point,
separate the **UNGATED** signal into a subset with real accuracy? (The
gate itself is not under test — it subtracts value; this screens the raw
signal.)

## Dataset

The same 499 SPY sessions and per-day records from the reproduction
(workpapers: `hidden_files/medusa-gate-repro-2026-11-17/`). Ungated
directional calls only — the 428 directional days; WAIT/flat days stay
excluded exactly as the reproduction treated them. No new data, no SPX.

Scoring is the reproduction's own: a day is correct when
`residual × dir_ungated > 0`, where `residual` is the decision→close
(15:15→16:00) move; net points for a subset = Σ `residual × dir_ungated`
over its days.

## Conditions — SIX families

All computable at 15:15 ET from data the classifier already consumes;
thresholds fixed here:

1. **VIX** (prior-day close, as fed to the classifier): <20 / 20–25 / ≥25.
2. **Day direction into the decision**: SPY day move to 15:15 positive vs
   negative.
3. **Signal strength**: |effective_score| at/above the sample median vs
   below it (median computed once over the 428, reported, not tuned).
4. **Signal direction**: bull calls only / bear calls only.
5. **Prior-day SPY move**: up vs down.
6. **Intraday range to 15:15 vs ATR(17)**: range ≥ 1.0×ATR vs < 1.0×ATR.

13 subsets in total. If a field needed for a family is not in the
reproduction CSV, it is recomputed from the same on-disk SPY bundle the
reproduction used — same bars, same definitions as the author's code
where it defines them; any definition that had to be chosen is documented
in the run report.

## PASS limbs for a subset (ALL must hold)

- (a) n ≥ 100 directional days;
- (b) sign accuracy ≥ 55%;
- (c) placebo: 1,000 permutations shuffling the condition labels across
  days — the subset's accuracy must exceed the 97.5th percentile of the
  placebo distribution for subsets of the same size (two-sided screen
  over ~13 subsets; this stricter percentile is the multiplicity guard,
  keep it);
- (d) both chronological halves (split at the sample midpoint by date)
  show accuracy > 50% AND positive net points under the reproduction's
  own points accounting.

## Verdicts

- A family subset passing all four limbs = **SEPARATES** (earns only a
  proposal for a frozen forward test — never adoption).
- Everything else = **DOES NOT SEPARATE**.
- If nothing separates, the recorded conclusion is: standalone Medusa is
  closed permanently, no second screen.

## Forbidden

Adding families, moving thresholds, re-slicing a near-miss, or reporting
any unregistered slice as a finding. Unregistered observations may be
listed in an appendix labeled exactly that, with no verdict weight.

Spend: $0. Network: none.
