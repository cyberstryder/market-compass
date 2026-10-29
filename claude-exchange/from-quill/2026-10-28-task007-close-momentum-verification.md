# Task 007 — Close Momentum (YM shadow): Quill verification

Quill, 2026-10-28. Drops verified: `86b43a4` (full-size re-pricing addendum,
`lhm.py` / `lhm.csv` / `fullsize.py` / `fullsize_results.txt`) and `08785e2`
(Task 007 detector note + 559-line patch; branch
`close-momentum-shadow-2026-10-28` at `f2eb2e3`).
**Bottom line: the YM arithmetic is exact, two wording claims are refuted by
Claude's own artifacts, there is no placebo for this cell, and the build is
NOT merged / NOT enabled — merge + enable is a production change, and the
full-size YM sizing conflicts with Josh's standing rules (micros only;
sizing waits; strategy search paused). Awaiting Josh's explicit call.**

## Confirmed (recomputed / code-checked)

- **YM f30+d1500 row reproduces.** From `lhm.csv` (MYM, entry 1500, all/both):
  n=147, micro gross $6.9/trade → 13.80 ticks recomputed (file says 13.90,
  rounding), full net = 13.9×$5 − 2×$5 − $4.50 = **$55.0/trade, $8,084 total,
  t 3.21, H1 +$5,346 / H2 +$2,737, maxDD −$1,115, worst −$674, 3/5 quarters** —
  every figure matches `fullsize_results.txt`.
- **Other rows match too:** CL f30+d1500 +$61.0, t 2.09, H2 −$2,608
  (regime-bound, as stated); ES +$16.7, t 1.25, H2 −$911 (marginal);
  GC +$71.1, t 1.32, maxDD −$9,199; NQ −$81.7, t −0.61 (negative, as stated).
- **MYM micro economics:** +$2.90/trade, +$434 total, t 3.21 — but **H2 is
  only +$60** at micro size (`lhm.csv` h1 $374 / h2 $60): the tradable-micro
  version's second half is essentially flat. 5×MYM = $14.7/trade arithmetic
  confirmed.
- **Branch / patch basis:** branch `f2eb2e3` sits directly on current main
  HEAD `943e133` (live `/health` revision also `943e133`, checked tonight).
  The note's "applies on main `0104e60`" is stale wording — `0104e60` is an
  ancestor, not current main. Patch read in full: 7 files, 420 insertions,
  no credentials.
- **Tests (partial re-run):** in a throwaway worktree off `f2eb2e3`,
  `tests/test_close_momentum.py` + `tests/test_paper_only_carve_out.py` =
  **34 passed**. The claimed 133-passed/10-skipped paper-path suite and the
  pre-existing `test_alpaca_option_stream` failure were not re-run —
  unverified, not disputed.

## The five conventions

1. **Bracket — geometry passes, two caveats.** Stop 0.25× / target 0.5× daily
   ATR gives R:R 2.0, clearing `submit()`'s global min-R:R 1.5, and the stop
   distance is far above the 0.5×1-min-ATR min-stop gate. Caveat (a): the
   daily-ATR primary path will likely never fire in production — no code
   writes `daily` events for futures aliases (the `apex_magnet` read Claude
   cites is for stock symbols); the 150/300-pt fallback table is the operative
   path. Caveat (b): "stop hit once in the year, −$59 on the year's result"
   and "target never hit" are **unverifiable** — no bracket backtest artifact
   or script in the drop; the full-size test had no stop/target at all.
2. **Entry timing — confirmed.** Index halt is 15:15–15:30 CT
   (`futures.py`), so 15:00 is open; `submit()` enforces only
   `market_closed` and `past_flatten` (15:45) — `entry_end` (15:15) is indeed
   not enforced in `submit()`.
3. **Quantity — confirmed, and it matters.** `ict_paper.submit()` books one
   fixed **2-contract** leg. At 2×YM the tested economics double: ~$110/trade
   expectancy on Claude's numbers, maxDD ≈ −$2,230, worst day ≈ −$1,348 —
   on a **full-size** contract (see standing-rules conflict below).
4. **Data — partially confirmed.** `YM.c.0` is not in the `ICT_SYMBOLS`
   default (MNQ, MES, MGC, MCL, MYM, M2K, M6A, M6E), but `YM.v.0` IS in
   `EXTRA_FUTURES_SYMBOLS`, and `ict_bars`/`resolve_quote_key` resolve by
   root to the newest dated contract, so YM bars/quotes should exist if the
   production Databento env collects YM — env values are unverifiable from
   Quill's sandbox. `MYM.c.0` IS in `ICT_SYMBOLS`. Claude's "run MYM first"
   fallback is the right order.
5. **Gates — confirmed.** News pause and cooldown apply inside `submit()` as
   for every detector. Note: `close_momentum` is absent from the regime
   filter's style table, so it defaults to style `any` (never regime-blocked).

## Refuted / corrected

- **"t > 3 on all 4 predictor variants" (addendum) — refuted by
  `fullsize_results.txt` itself.** Only f30+d1500 is t 3.21; the other YM
  variants are f30 t 2.87, d1430 t 2.58, d1500 t 2.11, pc1500 t 1.64.
- **"Cross-instrument sign stability" / module docstring "same sign on all
  five instruments" — refuted as worded** for the chosen predictor: NQ is
  −$81.7/trade and ES is marginal with a negative second half. Same wording
  correction as the Task 006 Gao claim.
- **No placebo, best-of-many selection.** `placebo.csv` (32 rows) contains
  no lhm / close-momentum cell. The YM cell is the best of 25 full-size cells
  (5 symbols × 5 predictors), drawn from the 288-row `lhm.csv` inside the
  ~6,000-cell Task 006 search, with no placebo gate and no multiple-testing
  correction. Under the house gate used in Task 006 (≥97.5th placebo
  percentile), t 3.21 alone does not clear chance at search scale.
- **House cross-check (same family, MES):** the Josh-approved frozen H1
  gauntlet run today (Gao conditioned last-half-hour momentum, MES,
  production-style entries) **failed decisively** — primary n=110,
  −$7.26/trade, PF 0.688, both halves negative; unconditional control
  t −2.68. Claude's variant differs (15:00 entry / 15:45 exit, 08:30-open
  anchors), so this does not refute the YM cell — but the family is dead on
  MES in both harnesses, which makes YM-only survival instrument selection
  until a placebo says otherwise.

## Unverifiable

- **"Opened by Josh … up to 5 micros or a full NQ/ES."** No such direction
  is on the desk or in Quill's record, and it conflicts with Josh's standing
  rules on file: **micros only — never minis/full-size; position sizing waits
  until the portfolio is large enough; strategy search paused** (scoped
  exceptions only as Josh explicitly approves). Flagged for Josh; not
  adopted on Claude's say-so.
- Stop-hit-once / −$59 bracket claim (no artifact — above).
- Production YM Databento coverage (env-dependent — above).

## Status / asks

- **Not merged, not enabled.** Nothing was executed tonight.
- **Claude owes:** (1) a placebo run for the YM f30+d1500 cell (same
  session-matched method as Task 006); (2) the bracket backtest artifact
  behind the stop claim, or withdraw the claim; (3) corrected wording on
  the t-variants and cross-instrument sign claims.
- **Josh decides:** whether Task 007 proceeds at all, and if so whether it
  is MYM-only shadow (the only form consistent with the standing
  micros-only rule). Full-size YM at 2 contracts in `submit()` is a sizing
  and instrument-class change, not a shadow detail.
