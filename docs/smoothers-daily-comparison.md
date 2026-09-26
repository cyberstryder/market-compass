# Daily Smoothers incremental-value study v1

A quiet prospective comparison of the existing Smoothers formula with Compass
intraday stock setups, scanner 0DTE observations, and options ideas. It creates
no orders, Discord jobs, paper positions, or weekly Smoothers records.

## Frozen protocol

- Freeze the enabled Smoothers ticker configurations and their revision on
  activation (DJT excluded). Collect the next 20 full exchange sessions. A
  session already open at activation is not reconstructed.
- Evaluate the same triple smoother directions and MAIN/BACKUP classification
  on each session's first completed hour. All configured qualifying candidates
  are studied, not just the weekly featured list. Process 65–90 minutes after
  the open, one ticker at a time with bounded provider work. Holidays and early
  closes use the exchange calendar. Missing history or a missed processing
  window is unavailable, never a negative signal.
- The hourly signal reference is frozen at open+60 minutes. Actual detection
  time and a fresh underlying quote anchor the research observation; nothing
  is backdated to the bar close. Processing latency is recorded explicitly.
- Retain the first observed candidate per ticker, direction, family and session
  from the existing stock/0DTE/options-ideas research tables, including candidates
  lacking an eventual option entry. Option selection and option outcomes stay
  in their original ledgers; this study compares their underlying hypotheses.
- Same ticker/direction/session defines overlap. Opposite directions are separate.
  Distinguish confirmation already available at detection from later-only
  overlap. A later signal cannot become a predictive filter at an earlier entry.
- Observe 60 trading minutes and the closes of the current, next, third and fifth
  subsequent sessions. Session-close directional underlying return is primary.
  Quotes must have been timely recorded at each endpoint. Missing quotes and
  changed split-adjusted daily reference prices exclude that endpoint. These are
  midpoint observations before costs, not option returns, fills, or stop paths.

## Interpretation

Daily results shows overlap counts, which family detected first, outcomes by
weekday/horizon and overlap group, confirmation timing, and census data errors.
No-match labels mean no match in retained evidence, not guaranteed complete
market coverage. An incomplete Smoothers ticker census stays explicitly unknown.
Open endpoint counts must not enter completed-return statistics. The report
refreshes every minute; future five-session endpoints mature after entry
collection finishes. There is no automatic promotion or new alert stream.

Keep weekly Monday outcomes separate. Review whether daily Smoothers adds
unique candidates, earlier detection, or useful confirmation known at decision
time. Unequal entry times, shared inputs, repeated tickers and the short sample
prevent causal conclusions. A redundant formula need not become another scanner.

Tests cover Tuesday selection versus unchanged weekly selection, incomplete
opening-hour bars, frozen parameters, partial-day exclusion, idempotence and
no messaging, directional overlap, actual detection timing, all comparator
families, missing endpoint quotes, and corporate-action basis changes.

## Measurement coverage repair (September 25)

Daily Smoothers now pins its frozen ticker census in active quote recovery during
its opening-hour processing window. Pending research endpoints pin their symbols
from 30 seconds before the target through five seconds after it. Existing live
stock recovery then uses its two-second retry cadence for these symbols, even
when no paper trade exists. This does not relax the source-time or recording
windows and does not repair observations whose quotes were never retained.

The daily bars already fetched for each new signal are retained as provider
reference evidence, with real receipt times. Only completed prior-session bars
are eligible; the latest completed close is frozen in the new observation.
The existing changed-price-basis exclusion remains in force. Existing observations
are not assigned references that became known later. Reports expose missing
endpoint reasons alongside measurement denominators.

The weekly scorecard separates stock target status from one-contract option
quote estimates. It requires available, fresh, matching entry and exit contracts,
ordered quote timestamps, a quote aligned with resolution, and the standard 100
multiplier. Estimates use entry ask, exit bid and an explicit $1.32 round-trip
fee assumption. Pending and unusable quotes have null P&L, never zero. Original
source premium marks remain unavailable for this calculation. This is not broker
P&L, and partial measured totals are not complete strategy returns. Weekday
option trading and new alerts remain disabled.
