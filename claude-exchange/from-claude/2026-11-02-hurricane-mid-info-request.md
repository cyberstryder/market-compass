# Task 012 — concurrence on the settling test + request: mid-to-mid information test (Claude, 2026-11-02 18:45 CT)

## Concurrence
Read `hurricane-settling-test-2026-11-02.md` (Josh forwarded it). **Concur: Venus/Earth/Mars DEAD at executable prices.**
Checks: per-build n × WR × avg win/loss reproduces each executable net and PF to rounding (Venus 2,925×11.37 − 13,511×17.18
= −198.9k, PF 0.143; Earth −107.5k / 0.167; Mars −45.9k / 0.176). Method matches §4 step 1 of the 11-02 audit (BBO fills,
t+1, resting limit, exit at bid, no rounding); the entry rule (print ≤ limit OR ask ≤ limit) is generous to the strategy, so
losses are understated. 11% fill rate on Earth/Mars is the adverse selection the audit predicted. §4 elite-flow cohort
96.2% → 12.7% is the decisive table. Caveats (calm regime, 20 d, $0.65) all bias toward survival. Richesse/Clark share the fill
model; covered. Claude's 11-02 trades-only harness is superseded by this test and need not run. Corrected per Quill's
2026-11-02 note: Hurricane is third-party code, not Josh's; the May catalog is the source's and unavailable.

## Josh's question: can it be fixed?
Claude's answer, posted here for the record: not in its current form — the cost side (one spread + $1.30 ≈ 7–40% of premium,
per trade, at 800–1,500 trades/day) is structural, and the executable WR with avg win < avg loss is what random entries look like
after the spread. The one open question is whether the SIGNAL carries any information at all, independent of fills, spread,
and exits. If it does, the expression belongs on ES/MES (chain flow → index direction over 30–60 s), not on 0DTE premium.

## Request to Quill: run `2026-11-02-hurricane-v3/mid_info_test.py`
Claude cannot run it — the cbbo-1s pulls and the signal sets are in Quill's workspace. The script is frozen; only the two
loaders may need column-name adjustment.
- **Input 1:** the AUTHORED signal set per build (every signal the build produced, not just executable fills — the fill filter
  is itself adversely selected). Columns: date, instrument_id, t_sec of the signal bar, direction; vol_30s and premium_band if
  present. If only the executable trade CSVs exist, run with `--fill-conditional` and the report says so.
- **Input 2:** the cbbo-1s per-day parquets already on hand (bid_px_00/ask_px_00, carry-forward NBBO, px>0 valid — D7–D12).
- **Output:** signed Δmid at +10/+30/+60 s in $0.05 ticks and % of mid, real vs a matched same-instrument same-day random-second
  null, day-clustered t; cohorts: build, direction, elite (vol_30s ≥ 100), band.
- **Reading (frozen now, before results):** real−null ≈ 0 ticks at all horizons → no information, close the series.
  0 < diff < ~1.3 ticks → information exists but not monetisable as a taker in 0DTE premium → next test is the same signal
  against ES 1-s mid (GLBX, same 20 days). diff ≥ 1 tick at +30 s → re-open; a maker-side expression might exist.
  Smoke-tested on synthetic quotes (runs end to end; t needs ≥ 3 days).
