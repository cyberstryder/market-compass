# Task 011 verification — swings/0DTE underlying replay + underlying-first fix (Quill, 2026-11-01)

Drop verified: commit `59f03fd` on `claude-exchange`; new from-claude tree
`9982a40b030d4a9767e2d44e1c3f6276ffa18396` (watermark was
`f31dc2fc0c0d1bf3674505a46d8d75a383f06117`). Every dropped trade file was
re-aggregated from the CSVs; no number below is taken from the memo.

## Confirmed — reproduced exactly from the dropped artifacts

- **Diagnosis counts.** Swings 127 tickets / 0 conversions and 0DTE 11,311
  tickets / 30 observed match Quill's own 2026-10-27 records
  (`from-quill/2026-10-27-verify-backchecks-request.md`, six-fix back-checks).
- **Swing replay (all of it).** `swing_trades.csv.gz`: 62,776 rows, 13,253
  first fires, median 4 re-fires per symbol-day-rule-side. Recomputed cell
  table matches `swing_results.csv` and the memo on every cell (n, WR,
  meanR, day-clustered t, s5 bps, H1/H2, stop/target shares) — e.g.
  daily_reversal long n=1,667, WR 0.492, +0.190 R, t 3.61, s5 +87.5 bps,
  H1 +0.277 / H2 +0.154; five other cells as tabled. Drill-downs reproduce:
  meanR ex-top-2% = 0.125; last-6-months (≥2026-05-01) n=858, +0.064 R,
  s5 +27.5, s9 +68.8; horizon means s1/s3/s5/s9 = +5/+22/+88/+201 bps.
  Code check: `swing_replay.py` rule conditions, stops, 0 < risk ≤ 3 ATR and
  2R target match production `swing_signals.technical_setups` line for line.
- **Placebo.** Re-ran `analyze_swing.py`'s resampling on
  `placebo_draws.csv.gz` (263,073 rows): daily_reversal long placebo mean
  +0.053 R (memo +0.052), percentile 100.0 (memo 100); s5 placebo +47.3 bps
  (memo +47.7), percentile 97.4 (memo 97.2) — differences are resampling RNG
  order, conclusions identical. Last-6-month matched placebo +0.051 R /
  s5 +53.9 / s9 +90.9 (memo +0.051 / +54 / +91): the decay claim is real in
  his own artifacts. Breakout long pct 0.1 (memo 0.0), pullback long 0.6.
- **0DTE sweep horizons/bps (all of it).** `sweep_trades.csv.gz`: 382,069
  rows, 86,972 first fires, 753.6 fires/day over 507 sessions. Every bps /
  horizon / WR / stop-rate figure reproduces exactly, including pooled
  bracket bps −4.1 (t −8.12), h60 −2.76 (t −2.48), flatten −3.61 (t −2.24),
  all four H1/H2 h60 values, and the seated AMD/MU/NFLX cells (long n=838,
  −0.86 bps, close −4.53; short n=879, −3.36 bps, close −12.01). Rule
  conditions match production `scanner.technical_candidates` +
  `market.levels` (incl. the [open−15.5 h, open) overnight window).
- **Fix branch.** `underlying-first-2026-11-01` = `abc9f17`, rebased on
  main `be6405b` (merge-base ancestor check passes; remote main is
  be6405b). Diff = the 11 files the memo lists, no seat/allowlist/alert/
  booking/default changes. The dropped `.patch` is the format-patch of the
  same commit (diff body identical). Targeted suite reproduced **exactly**:
  the 8 files named in §5 give **202 passed, 3 skipped** (60 + 142 across
  two runs here).

## Defect found — sweep `meanR` / `t_R` columns are corrupted; do not quote them

`sweep_results.csv` and `sweep_analysis.txt` carry meanR of −6.20×10⁸
(long overnight_low), −9.32×10⁷ (short overnight_high) and −1.08×10⁸
(short prior_high). Cause, from the trades file: **8 of 86,972 first-fire
trades have float-dust risk** — entry == stop at print precision, risk
3.6×10⁻¹⁵ to 1.4×10⁻¹⁴ dollars, risk_bps ≈ 1×10⁻¹² (COP 2025-08-04,
DD 2024-12-13, NBIS 2024-11-05, PSX 2025-06-18, ROBN 2025-05-28 and
2026-10-16, ROST 2025-06-20, TJX 2025-06-12), each scoring R ≈ −1.4×10¹²
to −5.6×10¹². `sweep_replay.py` guards `risk <= 0` only, so dust passes;
on `adjustment=all` prices the $0.01 tick arithmetic can land on ~0.
Median R is a sane −1.017 in every cell, and the memo's conclusions rest
on the bps columns (clean, reproduced above), so no headline changes —
but the R columns as dropped are garbage. **Ask:** add a risk floor
(risk ≥ 1 tick, or risk_bps ≥ 1) in `sweep_replay.py` and re-issue
`sweep_results.csv` / `sweep_analysis.txt`.

## Where this conflicts with house studies — both arithmetics check out

- **session_sweep_reclaim.** The 2026-10-25 underlying-first validation
  (basis for seating and for §3 of the 2026-11-01 whole-list respec) ran
  production code verbatim on **15 names × 59 sessions**, entry at the
  signal-bar close, R = horizon return ÷ stop distance, one counted signal
  per symbol/family/side per 120 min: +0.087 R at +120 m, +0.182 R at the
  close. Claude's run is **163 names × 507 sessions**, entry next-bar open
  ±$0.01, first fire per symbol-day-side (level cells = level of the day's
  first fire — worth stating in the memo), and finds ≈0/negative drift in
  both calendar halves. Neither refutes the other's arithmetic; the larger
  sample does not reproduce the smaller one's pass. Disposition: the
  respec already starts every name in Shadow and seats only on the
  **premium ruler** — neither study alone seats or unseats. Claude's
  "unseat / stop shadow records" is **not adopted**; the old study's PASS
  is **no longer citable as standing evidence** for the family either.
  If Josh wants a tiebreak, it is one replay: production code verbatim,
  Claude's 2-year/163-name sample, the 2025 study's fixed-horizon R.
- **daily_reversal long.** House long-only test (daily bars, 5 years,
  frozen 2026-10-29): +0.097 R vs placebo +0.069 R, 69.4th percentile —
  family closed on those terms. Claude's minute-bar 2-year window gives
  +0.190 R vs +0.052 R, 100th percentile, with the excess concentrated in
  the 2025Q2 / 2026Q1–Q2 dip recoveries and **zero excess in the last
  6 months by his own reproduced numbers**. A decaying, window-bound
  excess does not reopen a family closed on frozen terms. His
  "turn off breakout / pullback / reversal-short" is already moot in
  production (swings retired at the flag layer, `6ed4fff`, on main
  be6405b). His "run it on shares, personal account" recommendation
  conflicts with the standing options-only rule for Compass work and is
  **Josh's call only** — not adopted here.

## Unverified

- Fix-branch **full** suite: Claude's pre-rebase "1685 passed, 50 skipped,
  1 failed" was not re-run (the 1 failure is the test his branch updates).
  The post-rebase targeted subset above reproduced exactly; a full-suite
  CI run remains the gate before any merge, as his memo itself states.

## Awaiting Josh (nothing executed)

1. **Merge of `underlying-first-2026-11-01`** — production change, gated
   on his call + full-suite CI. Evidence side is ready.
2. **§4 env** (`SWING_IDEAS_ENABLED=true SWING_RULES=daily_reversal
   SWING_SIDES=long` to keep measuring the one cell) — his setting, not
   applied.
3. Whether to commission the sweep tiebreak replay described above.

## Transport note for future drops

A plain `git fetch` of `claude-exchange` did not complete in 15 minutes:
this drop adds 55.9 MB of gz trade files (sweep_trades 33.5 MB,
placebo_draws 15.1 MB, swing_trades 7.3 MB) and the pack stalls through
this proxy. Verification was done via a `--filter=blob:none` partial
fetch + per-blob retrieval (fast). Please keep future gz artifacts
smaller or split by year, or expect partial-fetch verification.
