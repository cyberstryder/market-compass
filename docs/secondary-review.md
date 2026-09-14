# Secondary review: originals versus Compass

Requested September 14, 2026. Current rule version: `secondary-context-v2`.

## September 14 data recovery fix

The morning comparison contained 76 Smoothers records with unusable independent
quotes and minute context. Eighteen Smoothers symbols were outside the configured
scanner list, including BKNG. Version 1 also used the same daily-trend caution for
missing history and a nonconfirming direction. Those records remain unchanged and
are excluded from directional performance comparisons.

Collection now includes the union of the configured watchlist and equity symbols
observed from connected projects in the last 30 days, up to 500 symbols. The stock
socket checks for subscription changes every five seconds. This expands data
coverage without adding these symbols to the native scanner's entry universe.

The separate `secondary_data` collector checks coverage every 15 seconds and
repairs missing completed daily/minute data in batches of at most eight symbols.
Daily history uses 120 calendar days; minute recovery uses the preceding three
hours. Attempts are limited to once per symbol per five minutes for daily data
and once per minute for minute data. A waiting equity review can request a latest
quote batch (at most 100 symbols per pass). Each provider request has a six-second
timeout. There are at most three requests per pass, plus the independent ordinary
history collector. Market-closed periods repair daily history only. No new vendor
subscription is required; these use the existing Alpaca feed entitlement.

Provider references: [historical bars](https://docs.alpaca.markets/us/reference/stockbars)
and [latest quote request parameters](https://alpaca.markets/sdks/python/api_reference/data/stock/requests.html#alpaca.data.requests.StockLatestQuoteRequest).
Source timestamps and missing bars are preserved. Recovery does not fabricate
quotes or backdate historical observations into an earlier decision.

New candidates with missing required inputs enter a durable waiting queue within
the original 60-second availability window. They produce one final decision when
usable data arrive, or an insufficient-data record at expiry. Waiting does not
reset the original clock, block other source events, or publish provisional trade
alerts. Restarts preserve waiting candidates; later source outcomes cannot extend
their deadline. Final decisions, including older rule versions, are never rescored.
Original outcome revisions still update their separate outcome fields.

Daily confirmation now requires 50 consecutive completed exchange sessions through
the last session, respecting weekends and exchange holidays. Missing daily history
is required-data failure; bearish, bullish and neutral observed trends have explicit
labels. The daily EMA21/50 and target/ATR study rules otherwise stay unchanged.
When the scanner cache is missing/stale, connected equities compute the same
intraday rules from the stored completed bar window. The thirty-consecutive-minute
requirement, five-second quote age and 90-second minute age are not relaxed.

The dashboard shows current per-symbol collection, quote, minute and daily
readiness and pending deadlines separately from historical comparisons. It also
shows frozen input checks in version 2 reviews. Summary counts respect the selected
project, and the latest 100 reviews per source remain accessible despite activity
in other projects. Current readiness is not a new assessment of an old alert.

The original Smoothers, Morning Algo and TradingView futures services keep their
own alerts, rules, positions and schedules. Compass publishes a separately
labeled secondary assessment and measures all candidates, including those it
does not support. This version does not tune itself or send orders.

## Data path and timing

The secondary worker runs independently on the Compass engine service, with its
own lease, durable event acknowledgements and review table. It consumes newly imported
Morning/Smoothers source observations and futures entry observations. It also
reviews Compass-native futures setup alerts, including MES and MNQ, as a separate
baseline cohort. It never blocks the original ingestion or engine loop.

A source strategy first generates its candidate. The existing read-only import
or TradingView mirror brings it into Compass. The secondary worker then reads
already collected market data, waits within the deadline if required inputs are
missing, freezes its final decision, and queues its own alert.
This is a review **after the original trigger and before the secondary alert**.
There is no callback that changes the original signal before it fires.

The secondary loop targets two seconds plus processing time. Morning/Smoothers
import targets five seconds per scan; TradingView webhook latency is separate.
No provider request or LLM call blocks the review path; recovery runs on the
collector service. End-to-end latency
must be observed, not inferred from those targets.

A candidate must have become available at most 60 seconds ago for a timely
review. Morning/futures use the source signal time. When Smoothers reports a
modeled entry time, its timezone-aware source row creation time establishes
candidate availability; both clocks and the full model-reference age remain
recorded. No later option update resets that clock. Independent
underlying quotes must be valid and at most five seconds old; completed minute
features must be at most 90 seconds old. Future-dated observations do not qualify.
Delayed imports and reviews expiring after the deadline receive an insufficient-data
record without a fresh notification.
Activation records the current event cursor and start time; existing history is not converted
into a new stream of trade alerts. Restarting preserves acknowledgements and all
decisions. Source outcome revisions update only the separate outcome fields.

## Assessment rules

These are initial research hypotheses, not calibrated probabilities.

| Result | Meaning |
| --- | --- |
| Supported | Required current price data exist, the direction has at least three supporting conditions, and no directional caution or price rejection applies |
| Watch | Required price data exist but the context is mixed, neutral, missing a trend confirmation, or near a fresh exposure concentration |
| Rejected | Required data exist but the reference is already invalidated, its target already reached, price moved too far, the spread is too wide, or the relevant research entry window is closed |
| Insufficient data | The source is too late, direction is missing, or independent quotes/completed price context are unusable |

Morning and futures reviews compare the original direction with completed
15-minute bias, session VWAP and completed EMA9/21 structure. Smoothers instead
uses completed daily EMA21/50 bias and its recorded daily target/ATR ratio,
together with current VWAP and EMA structure. A daily target no greater than
0.75 ATR is the initial swing study ceiling. A conflicting condition produces
Watch; the original alert still runs.

Price checks use each instrument's own context. A directly comparable source
reference more than 0.5 of its current minute ATR away is rejected for intraday
cohorts. That stretch rule does not apply to the weekly cohort. A fresh exposure
level less than 0.35 minute ATR ahead of the price is a caution, not a prediction
that the market will reverse there. The underlying spread limit is the larger
of eight ticks and 0.2% of midpoint.

Available SPY/QQQ context is treated as correlated evidence. Opposing recent
vendor-classified unusual flow produces a caution; agreeing flow can provide
support. Flow must be at most 120 seconds old, have vendor score at least 85 and
premium at least $100,000. Vendor score is not our win probability. GEX and VEX
concentrations retain separate fields, clocks and vendor units. Their signs are
not used to infer dealer direction.

Cash-market flow/exposure cannot confirm an overnight futures trade. Their
absence does not veto a price-based futures review. Unknown or stale optional
inputs are listed as limitations. Cross-asset SPY/QQQ exposure levels are never
substituted directly onto ES/NQ futures price axes.

TradingView continuous futures symbols link to the selected dated data contract
for directional context. Original continuous-chart stops, targets and reference
prices are not validated against that different price basis. Explicit dated
contracts must match before absolute levels can be compared. Optional futures
such as MGC use their own entitled dated-contract feed and price context.
Missing access or unusable data receive Insufficient data.

## Alerts and dashboard

Examples of headings:

```text
SMOOTHERS | SECONDARY SUPPORTED | NVDA · LONG
MORNING ALGO | SECONDARY WATCH | META · LONG
FUTURES | SECONDARY REJECTED | MESZ6 · SHORT
FUTURES | SECONDARY INSUFFICIENT DATA | MGC1! · SHORT
```

Each message states its scope, reasons, source strategy, original timestamp,
review timestamp, quote timestamp where available, original ID, review ID and
rule version. Supported is an underlying review, not an option selection or an
account-risk approval. A delivery beyond the bounded two-minute review window
is explicitly historical. Stable event IDs and Discord's existing confirmed
outbox preserve duplicate identification after uncertain delivery.

The **Secondary review** dashboard separates TradingView futures from
Compass-native futures. It displays the four outcomes, individual reasons and
checkpoint results, with a link to the frozen inputs and original measurements.
The owner-authenticated `/api/secondary` and `/api/secondary/record` endpoints
expose the same evidence. Ask Compass also receives bounded secondary context.

## Fair comparison

For every timely candidate with a usable independent quote, freeze that quote's
midpoint as the secondary decision reference. Observe direction-adjusted
underlying percentage changes at 15, 30 and 60 minutes. Supported, Watch and
Rejected use exactly the same measurement rules. The comparison is between all
eligible original candidates and the subset the secondary rules support, using
the **same review-time reference**, not an earlier original price against a
later secondary price.

Each checkpoint first checks retained quotes at or after its deadline, received
within 30 seconds and fresh when received, then falls back to a timely live quote.
Delayed review cycles can recover still-pending windows from these archives;
see [archive coverage and limits](stock-quote-coverage.md).
Missed windows stay missing. Windows extending through the
cash close or futures research flatten deadline are marked session boundary.
No checkpoint carries into the next session. Excursions use sampled quotes and
show sampling gaps; they are not tick-complete highs or lows. Review records
never open a position or require flattening a broker account.

The report shows measured and eligible counts, selected counts, average moves
for all candidates and the selected subset, missed positive moves, avoided
negative moves, and pending/missing/session-boundary observations. A separate
per-candidate filter metric assigns zero exposure to unselected candidates; it
is an opportunity comparison, not an equity curve or portfolio P&L. Insufficient
data are counted and excluded rather than being labeled losing trades.

Results remain grouped by source, symbol, direction, strategy, original version
and review version; MES and MNQ results are not pooled together.
The cached report updates about once a minute over the most recent 30 days, with
a disclosed maximum of 5,000 records. Individual reviews update independently.
There is no claim that a small selected sample establishes an improvement.

Original Morning checkpoints and option observations, Smoothers weekly target
outcomes and premiums, and available original Compass paper outcomes remain
attached separately. Smoothers' resolved target-hit comparison is also shown.
An underlying target hit is not an option profit. TradingView futures entry/exit
instructions still do not establish complete broker fills or realized P&L.

This version measures underlying-direction selection before spread, fees and
slippage. It does not model a new option trade, runner, prop-account drawdown,
shared position limit or different exit policy. To judge executable improvement,
the later study must compare matched contracts and costs, delayed entry effects,
missed opportunities and held-out sessions. No rule is promoted automatically.

## Operation and verification

The engine defaults to `SECONDARY_REVIEW_ENABLED=true`,
`SECONDARY_REVIEW_ALERTS=true` and `SECONDARY_REVIEW_COMPASS_FUTURES=true`.
Disable only the secondary worker or only its notifications with those switches;
original source services and Compass-native strategies are unaffected.

The new review and event-acknowledgement tables use the existing serialized startup migration.
Reviews, source outcome updates and alerts use the same transaction; the worker
has a separate lease and durable per-event acknowledgements. Concurrent producer
commits cannot hide a late-committing event behind a higher sequence ID.
Malformed candidates are recorded as
secondary errors without blocking later valid candidates. No provider key,
subscription, paid history request or original source deployment is required.

Behavioral tests cover immutable decisions, source independence, deduplication,
late/historical inputs, clocks, absent overnight options context, contract
mapping, weekly versus intraday bias, missed favorable trades, checkpoints,
session boundaries, bounded messaging and authenticated access. Production
verification must show an active worker, advancing checks, healthy original
connections, and the next real candidate and secondary notification. No
synthetic production trade is needed for verification.
