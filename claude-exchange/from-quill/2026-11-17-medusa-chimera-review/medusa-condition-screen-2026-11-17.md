# Medusa Condition Screen — Results (2026-11-17)

**Run once, exactly as registered** in
`files/medusa-condition-screen-registration-2026-11-17.md` (frozen before
the run; Josh approved the bounded shot 2026-11-17 10:56 CT). Six
condition families, 13 subsets, screened against the **ungated** Medusa
15:15 signal on the reproduction's 428 directional SPY days
(2024-10-28 → 2026-10-23).

## Verdict

**Nothing separates. All 13 subsets: DOES NOT SEPARATE.**

Per the frozen registration, the recorded conclusion is: **standalone
Medusa is closed permanently — no second screen.** No subset earns a
forward-test proposal. No family was added, no threshold moved, no
near-miss re-sliced.

## Sanity

The rebuilt per-day frame reproduces the reproduction's headline exactly
before any slicing: 428 directional days, ungated accuracy **49.1%**,
total **+10.0 pts**. Signal-strength median |effective_score| over the
428: **0.3221** (computed once, as registered).

## Full table

Limbs: (a) n ≥ 100 · (b) accuracy ≥ 55% · (c) accuracy > placebo 97.5th
percentile (1,000 label permutations, subset size preserved) · (d) both
chronological halves accuracy > 50% AND net points > 0. Halves split at
the sample midpoint: H1 = 2024-10-28 → 2025-10-15, H2 = 2025-10-16 →
2026-10-23 (214 directional days each). Points = Σ residual × direction
(the reproduction's accounting).

| Family / subset | n | Acc % | Net pts | Placebo p97.5 | H1 (n, acc%, pts) | H2 (n, acc%, pts) | a | b | c | d | Verdict |
|---|---|---|---|---|---|---|---|---|---|---|---|
| F1 VIX < 20 | 330 | 47.3 | −6.9 | 51.8 | 155, 46.5, −17.7 | 175, 48.0, +10.7 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F1 VIX 20–25 | 65 | 52.3 | +12.5 | 61.5 | 41, 53.7, +10.6 | 24, 50.0, +1.9 | ✗ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F1 VIX ≥ 25 | 33 | 60.6 | +4.5 | 66.7 | 18, 55.6, +1.3 | 15, 66.7, +3.1 | ✗ | ✓ | ✗ | ✓ | DOES NOT SEPARATE |
| F2 Day move to 15:15 positive | 199 | 51.8 | +14.3 | 54.3 | 103, 48.5, −5.2 | 96, 55.2, +19.5 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F2 Day move to 15:15 negative | 228 | 46.5 | −4.7 | 53.5 | 111, 48.6, −0.6 | 117, 44.4, −4.1 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F3 \|eff\| ≥ median (strong) | 214 | 45.3 | −7.3 | 53.7 | 109, 45.9, −7.2 | 105, 44.8, −0.1 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F3 \|eff\| < median (weak) | 214 | 52.8 | +17.3 | 53.7 | 105, 51.4, +1.4 | 109, 54.1, +15.9 | ✓ | ✗ | ✗ | ✓ | DOES NOT SEPARATE |
| F4 Bull calls only | 33 | 33.3 | −9.1 | 63.6 | 17, 47.1, −0.2 | 16, 18.8, −9.0 | ✗ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F4 Bear calls only | 395 | 50.4 | +19.2 | 50.6 | 197, 48.7, −5.6 | 198, 52.0, +24.8 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F5 Prior-day move up | 233 | 48.1 | −4.1 | 53.2 | 123, 48.8, −11.7 | 110, 47.3, +7.6 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F5 Prior-day move down | 195 | 50.3 | +14.1 | 54.4 | 91, 48.4, +5.9 | 104, 51.9, +8.2 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F6 Range ≥ 1.0×ATR | 114 | 43.9 | +3.5 | 57.0 | 52, 48.1, +7.7 | 62, 40.3, −4.2 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |
| F6 Range < 1.0×ATR | 314 | 51.0 | +6.5 | 51.9 | 162, 48.8, −13.4 | 152, 53.3, +20.0 | ✓ | ✗ | ✗ | ✗ | DOES NOT SEPARATE |

## Reading the table (facts, not findings)

- The highest raw accuracy, VIX ≥ 25 at 60.6%, sits on n=33 — it fails
  the size limb outright, and at that size the placebo bar is 66.7%, so
  the number is what chance produces on small samples. Its mirror image,
  bull calls at 33.3% on the same n=33, makes the same point from below.
- The closest miss is the weak-signal half (|eff| < median): n=214,
  52.8%, both halves above 50% with positive points — but it fails the
  55% accuracy limb and the placebo limb (52.8% vs a 53.7% bar). Under
  the registration this is a failed subset, full stop; it was not
  re-sliced and earns nothing.
- The classifier's bear tilt dominates the sample (395 bear calls vs 33
  bull), and bear calls alone are 50.4% — the coin flip wearing its usual
  clothes. Strong signals are *worse* than weak ones (45.3% vs 52.8%),
  the opposite of what a real signal would show.
- No family produced a subset that cleared even three of the four limbs
  except VIX ≥ 25 and weak-signal, each failing on size/placebo and
  accuracy/placebo respectively.

## Definitions chosen where the registration left a choice

(Documenting per the registration's own instruction; all fields
recomputed from the same on-disk SPY bundle the reproduction used.)

- **VIX**: the value the author's own `prior_context()` fed the
  classifier — last CBOE VIX close strictly before the day, from the same
  on-disk `VIX_History.csv` the reproduction driver used.
- **ATR(17)**: the author's `daily_atr()` verbatim (oracle ATR series on
  daily bars strictly before the day).
- **Day move to 15:15** (F2): decision price − S0, where the decision
  price is the last 5-min bar close before 15:15 (the harness's own
  `upto` mask) and S0 is the day's first 1-min open (the harness's s0).
  One day (2026-05-27) had a move of exactly 0.0 and is in neither F2
  subset (199 + 228 + 1 = 428).
- **Prior-day move** (F5): close-to-close of the last daily bar strictly
  before the day vs the daily bar before it (SPY_daily). No zero-move
  days occurred.
- **Intraday range** (F6): max high − min low over the same
  up-to-15:15 5-min bars the classifier consumed, divided by that day's
  ATR(17).
- **Halves** (limb d): the 428 directional days in date order, first 214
  vs last 214; each subset's days partitioned by that boundary.
- **Placebo** (limb c): per family, the family's label vector over the
  428 days (including "none" for unassigned days, so subset sizes are
  preserved exactly) permuted 1,000 times; RNG
  `np.random.default_rng(20261117)`, families in registered order;
  percentile by linear interpolation; limb requires strictly exceeding
  the 97.5th percentile.

## Workpapers

`hidden_files/medusa-condition-screen-2026-11-17/` —
`run_condition_screen.py` (the screen, run once), `screen_run.log`,
`condition_screen_subsets.csv` (this table, machine-readable),
`condition_screen_days.csv` (all 428 days with every condition field).
Spend: $0. Network: none.
