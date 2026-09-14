# Secondary review: originals versus Compass

Requested September 14, 2026. Rule version: `secondary-context-v1`.

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
already collected market data, freezes its decision, and queues its own alert.
This is a review **after the original trigger and before the secondary alert**.
There is no callback that changes the original signal before it fires.

The secondary loop targets two seconds plus processing time. Morning/Smoothers
import targets five seconds per scan; TradingView webhook latency is separate.
No new provider request or LLM call runs in the review path. End-to-end latency
must be observed, not inferred from those targets.

A candidate must have become available at most 60 seconds ago for a timely
review. Morning/futures use the source signal time. When Smoothers reports a
modeled entry time, its timezone-aware source row creation time establishes
candidate availability; both clocks and the full model-reference age remain
recorded. No later option update resets that clock. Independent
underlying quotes must be valid and at most five seconds old; completed minute
features must be at most 90 seconds old. Future-dated observations do not qualify.
Delayed imports receive an insufficient-data record without a fresh notification.
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
contracts must match before absolute levels can be compared. MGC has no
independent price feed in the current Compass integration and therefore receives
Insufficient data, not a fabricated gold confirmation.

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

Each checkpoint requires a fresh observed quote at or after its deadline and
within 30 seconds. Missed windows stay missing. Windows extending through the
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
