# Project connections — September 14, 2026

Market Compass observes Morning Algo, New Smoothers and the MNQ/MGC automated
TradingView strategies. Original rules, source databases and execution routes
remain independent. Integration downtime cannot prevent an original entry/exit.

## Data paths

| Source | Connection | Measurements |
| --- | --- | --- |
| Morning Algo | Compass collector reads scoped `/api/compass/feed` | Original signal/version/settings, 5/15/30/60-minute checkpoints, selected option and original option samples |
| New Smoothers | Same authenticated read-only feed | Official weekly signal, selected contract, target status, native one-minute exit time, premium observations and original ranking fields |
| Automated MNQ | Separate TradingView alert clone → Compass webhook | v0.10.3, three-minute strategy emulator order fills |
| Automated MGC | Separate TradingView alert clone → Compass webhook | v0.7.3, three-minute strategy emulator order fills |

The collector targets a five-second full scan of a rolling 14-day window. All
OPEN Smoothers records and recently resolved older records are included. Pages
contain at most 100 records; unchanged pages use ETags. Full overlapping scans
recover late source writes and mutable outcomes without a cursor missing them.
Maximum 5,000 records per scan; exceeding it reports an integration error.
Source snapshots are not an audit log of every transient database state.
Reconnects preserve source IDs, first-seen context and changed-record revisions.
Records already imported remain in PostgreSQL when they leave the source window.
Outages longer than that window require a bounded historical reconciliation.

Project identity is part of every key. No source outcome is added to Compass's
own simulation win rate or P&L. Morning's expiry policy continues to exclude
same-day options. Smoothers WIN indicates an underlying target touch, not a
profitable option exit. Futures observations are emulator fills, not broker
confirmations; actual TradersPost execution and P&L are not reconciled here.

## Context

Compass saves available quotes, technical levels, exposure concentrations and
research matches when it first sees a signal, with separate source and capture
times. Signals imported more than 60 seconds late receive no reconstructed entry
context. Captures after a signal cannot prove the source strategy knew that data.
Missing or stale information never becomes an entry filter in these projects.
Signals from the stock projects also request focused Compass research.

The scoped `GET /api/integrations/context?symbol=SPY` API makes current context
available to source clients using their integration token. Source strategies do
not yet consume that API as a trading filter. Their dashboard links open the
combined Compass view. Ask Compass receives bounded attributed project context.

MNQ/NQ link to QQQ exposure and MES/ES to SPY as **cross-asset context**. Continuous
TradingView prices and explicit Databento contracts are not interchangeable.
There is no independent MGC quote or gold exposure feed in this connection.
MGC's own TradingView fill price is recorded without inventing such a feed.

## Configuration

Source services: `COMPASS_INTEGRATION_TOKEN`, a distinct random token per project.
Compass: `MORNING_SOURCE_URL`, `MORNING_INTEGRATION_TOKEN`,
`SMOOTHERS_SOURCE_URL`, `SMOOTHERS_INTEGRATION_TOKEN`, `FUTURES_OBSERVER_TOKEN`.
`FUTURES_OBSERVER_STREAMS` lists only saved, verified active mirrors (`mnq,mgc`
once both are active); an endpoint token alone does not mark a mirror configured.
Tokens are stored only in service variables and the observer webhook setting.
Never put credentials in source control, alert text or documentation.

Webhook: `/hooks/projects/futures/{observer-token}/{mnq|mgc}` on the dashboard.
Keep the original TradersPost alerts active. Clone them while paused; change
only the clone to **Order fills only**, its name, message and notification URL.
Use Webhook only for the clone; original app notifications remain unchanged.
Verify the Compass destination before restarting the clone. Uvicorn access
logging is disabled so capability URLs are not printed by the app.

The mirror JSON uses these TradingView placeholders, all as JSON strings:

```json
{"ticker":"{{ticker}}","action":"{{strategy.order.action}}","position":"{{strategy.market_position}}","prev_position":"{{strategy.prev_market_position}}","quantity":"{{strategy.order.contracts}}","price":"{{strategy.order.price}}","order_id":"{{strategy.order.id}}","time":"{{timenow}}","bar_time":"{{time}}","interval":"{{interval}}","version":"0.10.3"}
```

Use version `0.7.3` for MGC. The receiver validates symbol, version, timeframe,
clocks and finite positive fill values. It commits before acknowledgement and
deduplicates delivery. A new timely futures event queues a clearly labeled
strategy-observation Discord message; historic imports never alert. Original
Morning/Smoothers Discord notifications continue without a second Compass copy.
TradingView webhook delivery is not guaranteed; its alert log is the source for
reconciliation. A working receiver alone does not prove an alert is active.

## Verification and rollout

Check `/api/projects` in the private dashboard. Morning and Smoothers must show
authenticated successful scans, retained source records and advancing checked
times. A scan older than 25 seconds is stale. Futures list each stream separately
and remain `awaiting_first_event` until an actual message arrives. There is no
synthetic event counted as a strategy trade. Use local fixtures for ingestion
checks; verify active saved TradingView mirror settings independently.

At the cash open, compare the first Morning signal and checkpoint against its
source dashboard. At the scheduled weekly run, compare each Smoothers signal and
target result. For each futures stream, compare the next actual order-fill log
entry to the Compass source ID and notification receipt. Observe actual scan and
delivery latency; five seconds is the polling target, not an end-to-end guarantee.

To disable observation, clear the two source URLs and pause only alerts named
Compass Observer. Original source jobs, databases, Pine strategies and TradersPost
alerts continue. No change enables broker execution in Compass.

References: [TradingView strategy alerts](https://www.tradingview.com/support/solutions/43000481368-strategy-alerts/)
and [webhook delivery](https://www.tradingview.com/support/solutions/43000529348-how-to-configure-webhook-alerts/).
