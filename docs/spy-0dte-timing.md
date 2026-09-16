# SPY 0DTE timing follow-up

The user chose 0DTE only and wants entry timing assessed. Keep the original
15-minute baseline until a meaningful comparison supports changing it.

Freeze this protocol before loading the follow-up option returns:

- Reuse original candle-confirmation signals for 3/5/10/15/20/30 minutes.
  Same premarket levels and directional rule across all windows, one trade/day.
- Dates: 2024-09-16 to 2026-09-11; validation starts 2026-03-16. Exclude
  September 14-15 because their option examples were already inspected.
  Earlier underlying-price outcomes were seen; this is a frozen option-pricing
  follow-up, not a claim that all validation market information is unseen.
- Only same-day expiry. Select the nearest whole-dollar strike (exact half
  ties lower), verify terms using the provider's as_of contract endpoint,
  standard 100-share SPY contracts, no adjusted deliverables.
- Buy the first valid historical ask 1-6 seconds after the signal boundary.
  Entry spread over 20% of midpoint or zero bid is a known no-trade filter.
  Missing evidence is excluded, not counted as a zero-return no-trade.
- Primary exit uses the first completed one-minute underlying close beyond
  a 1R stop or 2R target, otherwise 11:30 ET. R is fixed at 0.20 of prior
  14-day mean true range. Exit quote is 1-6 seconds after that close boundary.
  This explicitly replaces ambiguous intrabar ordering with close-based exits.
- Secondary exit: hold until 11:30 ET, without the stop/target. Use it only
  as a check that the conclusion is not entirely driven by one exit policy.
- Sell the bid, fees $0.65/contract/side. Also show $0 and $1.15/side.
  A valid zero bid is valued at zero as a conservative write-off, not a fill.
- Require evaluable evidence for all six windows on a date for the common
  cohort. Report every exclusion and all known no-trades.
- Select the primary-exit leader on development mean net return per session
  divided by entry premium plus fee, with at least 50 trades. Ties prefer 15.
  Validate only that choice versus 15 with 2,000 paired weekly bootstrap draws.
  Show per-trade dollar results for one contract separately from normalized
  returns. Neither measure is an actual account-equity or sizing simulation.
- Do not add historical gamma, change live entries, or compare expiries.

One-shot command: `python -m compass.zero_dte_timing`, explicitly enabled with
`RUN_ZERO_DTE_TIMING=true`. Existing credentials remain internal. The request
budget is 6,000 GETs and 20 minutes per run, with at most 10 starts/second. Source
responses are compressed and retained under isolated research state keys;
completed dates are checkpointed. No raw historical record enters live inputs.

Sources: [contract specifications](https://massive.com/docs/rest/options/contracts/contract-overview)
and [historical quotes](https://massive.com/docs/rest/options/quotes).
