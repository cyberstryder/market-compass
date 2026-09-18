# SPY timeframe comparison v1

The delivered SPY plan uses 15-minute candles. Its existing outcome ledger
compares opening and later checks, not different candle intervals. This separate
prospective study compares **1, 3, 5 and 15 minutes**. It sends no alerts, trades
no account, changes no live rule and does not touch existing frozen cohorts.

## Frozen comparison

- All intervals start at the cash open and check completed candles through the
  first 60 minutes. First checks are at 08:31, 08:33, 08:35 and 08:45 Chicago on
  normal sessions. No intrabar confirmation or overlapping rolling candles.
- At the first 1-minute check, freeze one common premarket range, its existing
  90% coverage test, daily ATR and opening-gap context. Capture after open+110
  seconds invalidates a full evaluation day. Later arrivals never move the
  range. Missing evidence stays visible; no weaker gate for faster intervals.
- A close above PM high selects CALL; below PM low selects PUT. A move inside
  the range is not a signal in any arm. This isolates timeframe within one
  breakout family; it does not test an inside-range trend-continuation strategy.
- Observe boundaries from +5 to +50 seconds, the same for every interval.
  Missing closing bars may arrive during that window. A missed post-activation
  check disqualifies later entry in that arm, because its first signal could
  have been missed. Historical checks and trades are never reconstructed.
- Each interval gets at most one option attempt per day. Excluded attempts
  do not chase a later signal. The stop is 0.2 x prior daily ATR from signal
  close; target is twice that risk; exit five minutes before exchange close.
  Every arm uses the same continuous quote-driven exit model.
- Choose the observed same-day standard SPY option with nearest whole-dollar
  strike to signal close (ties lower). Freeze the contract once selected. Use
  actual retained bid/ask, identical continuity/liquidity rules, $0.01 adverse
  slippage each side and $0.65 fees each side, inherited from the SPY option
  ledger. Missing premiums never become underlying-based option returns.
- Research release is a saved internal decision. There is no Discord receipt
  requirement and no fabricated receipt. The 15-minute research arm differs
  from the official delivered-plan cohort in early shared context, entry
  deadline and release clock; never pool the two.

## Period and assessment

Activation freezes the **next 20 complete exchange sessions** and final close.
The partial activation day is warmup. No extra sessions are silently added for
unfavorable results, gaps or holidays. Entire sessions missed during downtime
are explicitly accounted for at restart. New captures stop at the fixed end;
existing paths are resolved without resetting the study. A new period needs a
new version. Existing futures holdouts remain unchanged and uninspected.

Evaluation results remain hidden until the fixed final close. Coverage and
decision evidence remain inspectable. Warmup outcomes are separate and cannot
select a winner. This initial 20-session comparison is evidence, not a guarantee
of sufficient statistical power or a universally best timeframe.

Primary comparison uses the same days for all four intervals: every arm must
have a complete closed option path or a genuinely observed no-signal day, with
no missed or data-blocked checks. No-signal is zero only in this fully observed
matched daily comparison. Excluded, unresolved, blocked and unobserved arms
stay out of returns and retain their denominators. Completed-only unmatched
results are secondary and are never a substitute for matched days.

Report net daily one-contract P&L, win rate, profit factor, chronological
closed-equity drawdown, entry delay, holding time, sampled favorable/adverse
excursion and profit giveback. Show prespecified first-15 / minutes-16–30 /
minutes-31–60 entry bins, plus opening gap up/down/flat (above +0.1%, below
−0.1%, otherwise flat; missing is unknown). These are descriptive cuts,
not independently selected winners. Coverage and dependence across sessions
must be considered before any later promotion proposal. Human screen time is
not measured by automated quote-driven exits; holding time is only a proxy
for exposure duration, not required attention.

After the fixed endpoint, and only with at least ten matched days, estimate
the three prespecified mean daily differences (1m, 3m and 5m minus 15m) with
3,000 paired session bootstrap resamples. Fixed seed is 20260918 plus timeframe.
Each percentile interval is 98.333% (Bonferroni family alpha 0.05 across the
three contrasts). Resample complete days, preserving dependence among arms.
These small-sample intervals are descriptive uncertainty estimates; they do
not automatically choose a winner or establish stability in unseen regimes.

The **SPY 0DTE outcomes** page contains the finite-period comparison and a paged
decision audit. Full totals include every row in the frozen period; UI paging
does not define the study denominator. Separate tables retain shared contexts,
decisions, arms and paired option/underlying marks. No portfolio risk state,
official signal, sender ownership or chart setting is modified.
