# Gemini "MES 15m Combined Elite" — no-runner variant, HOLDOUT test

Scoped study approved by Josh 2026-10-26 21:33 CT. The main study
(`files/gemini-mes-elite-study-2026-10-26.md`) found exactly one positive
configuration in its pre-registered ablations — the strategy with the
**breakeven runner disabled** (+$1,053.52/ct, PF 1.067, n=564 on
2025-10-27 → 2026-10-26) — but that result was post-hoc and never
placebo-gated. This study froze that variant (`frozen-spec.md`, frozen
2026-10-26 21:35 CT, before any holdout data was touched) and ran it
**once** on the preceding year it has never seen: Databento MES.c.0
1-minute bars, **2024-10-27 → 2025-10-26** (350,000 bars), ending exactly
where the main study's year begins. Same engine, byte-identical
(`engine.py` sha256 `385a1da7…`); same house costs ($3 round-turn,
−2 ticks on stop fills); the only change from the strategy as written is
`use_be_runner = false`. Primary configuration: as-written holds (no
session flatten). All dollars per contract unless noted; ×2 = the
strategy's own 2-contract sizing.

## Verdict

**FAIL. The no-runner variant does not graduate.** On the untouched year
it **loses money**: **−$1,753.02 per contract (−$3,506.04 at 2
contracts), PF 0.881**, over 596 trades. It fails the profitability gate
outright, and it fails the placebo gate by a wide margin — its entries
beat only **64.5%** of shifted-entry placebos (gate: ≥ 95th percentile).
The in-sample ablation was a sample artifact: the same frozen code that
made +$1.87/ct per trade on the year it was selected from loses
−$2.94/ct per trade on the year before it.

| Pre-registered gate | Requirement | Holdout result | |
|---|---|---|---|
| (a) Profitability | Net > $0 AND PF > 1.0, house costs | Net **−$1,753.02**, PF **0.881** | **FAIL** |
| (b) Placebo | Percentile ≥ 95 (200 iters) | **64.5** | **FAIL** |
| (c) Integrity | No post-freeze rule/parameter changes | Intact — deviations are validation-tooling only, logged below | PASS |

## Primary results (variant A, runner off, as-written holds)

| Metric | Holdout 2024-10-27 → 2025-10-26 | In-sample ablation (for contrast) |
|---|---|---|
| Trades | 596 | 564 |
| Net 1ct | **−$1,753.02** | +$1,053.52 |
| Net 2ct | −$3,506.04 | +$2,107.04 |
| PF | **0.881** | 1.067 |
| Win rate | 63.8% | 65.1% |
| Avg win / avg loss | +$34.28 / −$68.43 | — / −$80.19 |
| Max drawdown (1ct) | $2,328.05 | — |
| Net at Pine's own fees ($1.24 RT) | −$704.06 | — |

Even at TradingView's cheaper cost accounting the holdout year is
negative — unlike the in-sample year, this is not a cost-model coin
flip.

### Per calendar quarter (net 1ct / PF / n), primary

| Quarter | Net | PF | n |
|---|---|---|---|
| 2024Q4 (from Oct 27) | −$455.99 | 0.780 | 99 |
| 2025Q1 | −$1,032.01 | 0.676 | 112 |
| 2025Q2 | −$295.39 | 0.948 | 165 |
| 2025Q3 | −$268.32 | 0.919 | 183 |
| 2025Q4 (Oct 1–26, partial) | +$298.68 | 1.520 | 37 |

Four of five quarters negative — the mirror image of the in-sample
ablation's 4-of-5 positive. The one positive slice is the 25-day stub at
the seam with the in-sample window; it does not carry the year.

### Where the money moves (primary, net 1ct)

By exit reason — same internal anatomy as in-sample: the snap-back exit
is the profit engine, the hard stops are the cost, and with no runner
the stops now run to their full 2.2-ATR/capped distance:

| Exit | n | Net |
|---|---|---|
| Snap-back | 353 | +$11,141.00 |
| Hard stop | 216 | −$14,780.42 |
| Full target (2.2 ATR) | 27 | +$1,886.39 |

By setup — the two setups **swap roles** versus the in-sample year,
which is itself the signature of no stable per-setup edge:

| Setup | n | Net | PF | In-sample (variant A, BE on) |
|---|---|---|---|---|
| Sniper | 349 | −$1,751.24 | 0.803 | +$32.65 / 1.004 |
| Reclaim | 247 | −$1.78 | 1.000 | −$934.68 / 0.847 |

Out-of-sample the sniper is the entire loss and the reclaim is dead
breakeven; in-sample it was the reverse.

## Placebo gate (200 iterations, main-study construction, walk BE off)

| Config | Real net 1ct | Percentile | Placebo median | p5 | p95 |
|---|---|---|---|---|---|
| **Primary (A, runner off)** | −$1,753.02 | **64.5** | −$2,335.55 | −$4,342.59 | +$105.90 |
| B mirror (ungated) | −$2,832.88 | 37.5 | −$2,447.33 | −$4,972.33 | −$78.68 |

The primary still beats the placebo *median* by ≈ $583 — some timing
information persists out-of-sample — but it sits far below the gate,
and the top 5% of random-day books running the identical machinery were
breakeven-or-better. "Better than the median random book, worse than
the machinery's own noise band" is not an edge. Variant B's real book is
worse than its own placebo median (37.5th percentile).

## Secondary readout — variant B (15:45 CT flatten, runner off; ungated)

| Metric | Value |
|---|---|
| Trades | 616 |
| Net 1ct / 2ct | −$2,832.88 / −$5,665.76 |
| PF | 0.811 |
| Win rate | 61.0% |
| Avg win / avg loss | +$32.35 / −$62.48 |
| Max drawdown (1ct) | $3,126.16 |

Quarters: 2024Q4 −$519.23/0.742 (101), 2025Q1 −$1,203.24/0.635 (117),
2025Q2 −$443.42/0.917 (168), 2025Q3 −$706.17/0.802 (189),
2025Q4-partial +$39.18/1.053 (41). By setup: sniper −$2,208.02/0.753
(358), reclaim −$624.86/0.897 (258). The flatten variant loses **more**
than the primary on the holdout — the reverse of the main study's
in-sample ordering (B −$782 vs A −$902) — and its 38 flatten exits net
−$261.50. No rescue here either.

## Diagnostics

- **Overnight holds:** 48 of 596 primary trades crossed a CT date
  (max hold 974 min), netting −$355.86 — negative, but unlike the
  in-sample year the loss is broad-based, not concentrated overnight.
- **Degraded data:** Databento flags 2 degraded days in the window
  (2025-09-17, 2025-09-24). 7 primary trades touch them, net −$20.38;
  ex-degraded the year is −$1,732.64. No conclusion rests on them.
- **Anchoring:** fills vs signal-close anchor — mean gap −0.012 pts,
  mean |gap| 0.149; **zero** of 596 fills beyond the stop or target.
  The Pine anchoring quirk is again immaterial.
- **Median hold:** 51 minutes (primary), 45 (B).

## Data & validation record

- **Pull:** Databento GLBX.MDP3, `MES.c.0` continuous, `ohlcv-1m`,
  2024-10-27T00:00Z → 2025-10-27T00:00Z (end-exclusive), quarterly
  parts, same v2 conventions as Step 0a. **Cost: estimate $0.00 →
  actual $0.00** (subscription-covered; nothing was spent).
- **Counts:** 350,000 1-min bars — exactly the metadata estimate;
  312 daily bars from the parallel `ohlcv-1d` pull.
- **Integrity:** timestamps strictly monotonic, 0 duplicates, 0
  NaN/bad/zero-volume bars. First bar 2024-10-27 17:00 CT (the Sunday
  Globex open); last bar 2025-10-26 18:59 CT, and the Step 0a series'
  first bar is 19:00 CT that evening — the two years tile to the minute
  with no overlap and no gap.
- **Cross-check:** 1-min bars aggregated by UTC calendar date (the
  `ohlcv-1d` convention) match the independent daily pull **exactly on
  O/H/L/C/V for all 312 days**.
- **Roll segments:** 5 clean contiguous segments — MESZ4 (54,213 bars)
  → MESH5 (85,306) → MESM5 (87,368) → MESU5 (88,496) → MESZ5 (34,617).
- **Gaps:** 62 gaps > 4h, every one a known calendar event — Easter /
  Good Friday weekend (73h), Christmas, New Year, the Jan 9 2025
  national day of mourning closure, holiday early closes (Thanksgiving,
  MLK, Presidents, Memorial, Juneteenth, Labor Day, Jul 3), and the
  four quarterly-roll Fridays whose continuous series ends at 08:29 CT
  (the Step 0a series shows the identical roll convention, so
  cross-study comparability holds). No unexplained interior gaps.

## Deviation log (gate c disclosure)

No strategy, engine, or runner code was changed after the freeze, and
the frozen run executed exactly once. The only fixes were in the
*validation tooling*, before any run, all logged in the workdir
`status.md`:

1. `validate_data.py` crashed on a pandas `tz_convert` misuse in its
   daily cross-check; fixed by converting via a DatetimeIndex.
2. `get_dataset_condition` was called with wrong keyword arguments
   (`start`/`end` instead of `start_date`/`end_date`); fixed — this is
   how the 2 degraded days above were obtained.
3. The daily cross-check's first keying (Globex trade date) mismatched
   because `ohlcv-1d` aggregates UTC calendar days; re-keyed to UTC
   dates, after which the cross-check is exact (above).
4. A status-log entry initially mis-stated the bar count as 350,200;
   the verified on-disk count is 350,000 (== estimate). Corrected in
   the log before this report was written; no data or result was ever
   affected.

`prep_holdout.py` differs from the main study's `prep.py` only in its
input path (documented in the frozen spec); `engine.py` is
byte-identical (sha256 verified before and after the copy).

## Bottom line

The pattern is now measured on both sides of the seam. In-sample, the
no-runner variant was +$1,054/ct at PF 1.067 with 4-of-5 quarters
positive; out-of-sample, frozen and untouched, it is **−$1,753/ct at
PF 0.881 with 4-of-5 quarters negative and a 64.5th-percentile placebo
finish**. A variant selected because it was the one green cell in an
ablation table behaved exactly as selection effects predict. The
Gemini Combined Elite — with or without its breakeven runner — is not
an edge under the house gate, on either year, and this closes the
question without a third look.

*Workdir: `hidden_files/gemini-mes-elite-holdout-2026-10-26/`
(`frozen-spec.md`, `status.md`, `scripts/`, `outputs/` with summary +
placebo JSONs and per-config trade CSVs, `data/` with the pulled
series). Study only — no production changes, no desk post.*
