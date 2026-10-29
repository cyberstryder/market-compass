# Task 008 addendum — the actual use: Option Hacker scan "Bull-CortesTreasure" replayed on the underlying

Claude, 2026-10-29. Josh showed the scan after the sweep (screenshot in `2026-10-29-ice-stock-sweep/results_mtf_scan/`).
It is not a 1-min trigger. It is a **multi-timeframe state filter on weeklys**: `ICEShao48wRSI1864460DMI075().bullishCross`
true within 4 D bars AND within 72 1h bars AND within 96 15m bars AND within 336 5m bars (all EXT), then option
filters DTE ≤ 5, ask 0.01–0.40, sorted by gamma descending. A lottery-ticket strategy: the question is whether the
listed names make ≥ 2σ up-moves inside 5 days more often than the same names on other days.

## Method
`mtf_scan_test.py` on `data-foundation-v2-stocks` (EXT bars present, 03:00–19:00 CT). Per symbol: 1-min → 5m/15m/1h/D
EXT bars; ICE-RSI votes on each; every RTH close (15:00 CT) scored for the four conditions. 78,136 symbol-days,
163 symbols, 2024-11-19 → 2026-10-22. Entry next RTH open; outcomes: close-to-entry at +1/+3/+5 d, max favourable
excursion over 5 d, P(MFE ≥ 1σ/2σ/3σ) with σ = trailing-20-d daily σ × √h; "lotto" = mean of max(0, MFE − 1.5σ) in
bps, a payoff proxy for a ~0.15-delta call. Baseline = all other symbol-days of the same symbols. DMI plain primary,
cum as sensitivity. Bear mirror scored for puts.

## Result — the bull scan is an anti-signal for calls

All four timeframes, plain DMI, n = 1,148 listed symbol-days (1.5%):

| horizon | mean bps | baseline | median | baseline | P(≥2σ up) | baseline | lotto bps | baseline |
|---|---|---|---|---|---|---|---|---|
| +1d | −8 | +3 | −9 | 0 | 4.4% | 4.2% | 18 | 17 |
| +3d | −47 | +25 | −31 | +9 | 5.1% | 6.6% | 38 | 50 |
| +5d | −38 | +46 | −44 | +21 | 4.7% | 7.6% | 51 | 75 |

- Listed names **underperform the same names on unlisted days by ~80 bps over 5 days**, median −44 vs +21, and make
  ≥ 2σ up-moves **less** often (4.7% vs 7.6%). The call payoff proxy is 51 vs 75 bps. Same sign in both halves
  (H1 +5d −48 vs +66; H2 −27 vs +27). Cum DMI: n 545, +5d −16 vs +46, 2σ rate 7.0% vs 7.5% — weaker, same direction.
- D-only (within 4 bars) is the same story (+5d +4 vs +46, 2σ 5.7% vs 7.6%). **The daily condition is what the scan
  is**: `bull_5m` is true on 96% of symbol-days, `bull_15m` 64%, `bull_1h` 49% — the lower timeframes filter almost
  nothing. The daily fire is Aroon(48) ≥ 70 + RSI(18) ≥ 64 + one cross, i.e. "already extended on the daily", and on
  these names that mean-reverts over the following week.
- Bear mirror (puts): all-four n 4,698 (6.0%); +5d sign-adjusted −29 vs −46 (the stock rises less than baseline, still
  rises), P(≥2σ down) 6.8% vs 6.1%, lotto 55 vs 42. Mildly favourable, **entirely H1** (H1 +15 vs −69, 2σ 9.1% vs 6.7%;
  H2 −65 vs −23, 2σ 4.9% vs 5.5%). Not stable.

## Verdict
The scan as configured selects names that are about to underperform. As a call screen it is worse than picking the
same tickers on a random day; as a put screen it worked in the first year and reversed in the second. The 5m/15m/1h
legs are decoration. Recommendation: retire the bull scan; if anything is kept it is the daily-extended condition as a
**fade** candidate (short-side), which is what Task 008's 1-min sweep also hinted (fire bar worse than its hour) — and
that goes through the normal gate, not into a scan. Quill: cross-check welcome; `scan_days_max6_rsi_plain.parquet`
has every symbol-day with conditions and outcomes.
