# SPY opening confirmation study — frozen first-pass protocol

Purpose: test whether a 15-minute opening wait gives a useful tradeoff between
early moves missed and entries that return through their trigger. This is an
underlying-price timing experiment, not an options profitability test or a
reconstruction of Arya's gamma-based recommendations.

Before requesting outcome data, freeze:

- SPY consolidated SIP one-minute bars, unadjusted, September 16, 2024 through
  September 15, 2026. Previous 14 completed daily bars supply a risk scale.
- Development ends March 13, 2026; validation begins March 16, 2026. Selection
  uses development results only. All validation-window comparisons other than
  the selected candidate versus 15 minutes are descriptive.
- Compare 3, 5, 10, 15, 20 and 30 minutes. The same premarket high/low, frozen
  from observed 04:00–09:29 New York bars, applies to every variant that day.
- Separately test (a) a minimum opening wait followed by one-minute confirmed
  closes and (b) confirmation on successive completed N-minute candles aligned
  to 09:30. This separates waiting time from candle size.
- First eligible close outside the frozen range determines direction. Enter at
  the next minute's open, at most once per session, with the last signal at
  10:30. Do not require a fresh cross at the eligibility boundary: a price already
  beyond the level can qualify, exposing the cost of chasing an earlier move.
- Risk distance is 20% of the previous 14-day mean true range, target 2R, and
  remaining positions exit at the 11:30 bar open. These are fixed research
  assumptions, not recommended live exits. Reversal/re-entry rules are excluded.
- Model one basis point adverse cost per side; also report 0.5 and 2 basis points.
  Gaps through stops use the adverse open; gap-through targets receive the limit.
  If both stop and target are touched within one minute without an opening gap,
  report stop-first/target-first bounds. Rank using the conservative bound.
- A common session cohort needs every minute from 09:30 through 11:30 inclusive,
  at least 30 premarket trade bars and one within two minutes of the opening.
  Missing minutes and conflicting duplicates are never filled in.

Report all no-trade sessions, trade count, mean net R per session and per trade,
win rate, drawdown, ambiguous paths, cost sensitivity, and re-entry into the
premarket range within ten minutes. Excursions describe the price path through
11:30 regardless of the modeled exit. A missed-early-move diagnostic compares
each variant with days when the 3-minute version's direction subsequently moves
1R: a later version misses it if it has not entered in that direction by then.
This diagnostic uses hindsight for evaluation and is not an executable strategy.

Select the development leader with at least 30 trades. Compare it with 15 minutes
on held-out sessions using a seeded, paired weekly block bootstrap (2,000 draws,
95% percentile interval). Also show calendar-quarter and pre-open gap/ATR groups;
small subgroups cannot establish a reliable adaptive rule. No timing setting is
automatically promoted to live use.

Historical gamma can be used only when its original dated snapshots exist. Audit
the retained archive separately; do not substitute today's gamma for earlier
sessions. Historical option returns require historical contracts and executable
quotes, expiry matching and options costs; SPY R results do not provide them.

## Execution and retention

`python -m compass.opening_wait_study` is an explicitly enabled, one-shot job.
It uses existing Alpaca credentials and the shared PostgreSQL connection through
Railway variable references. It calls only the historical stock bars endpoint.
The maximum is 140 requests and 30 minutes, with bounded page sizes and pagination
checks. It never reads credentials back to an operator, logs secrets, sends
notifications, calls a broker, or changes strategy configuration.

Complete raw monthly responses are compressed and cached under isolated
`spy-opening-wait-v1:input:*` state keys. Run manifests, excluded sessions and
per-day observations use `spy-opening-wait-v1:run:*`. Historical rows never enter
the live event journal, quote state or scanner context. SHA256 evidence hashes
and exact query parameters make the run reproducible. Re-running a completed
protocol returns the stored report without repeating provider requests.

Source: [Alpaca historical bars](https://docs.alpaca.markets/us/reference/stockbars).
