# TraderMatrix received-record study v1

Frozen September 17, 2026 UTC (September 16 Central) before inspecting its forward outcomes.
Model: `tm-receipt-study-v1`. Research only; existing alert rules and portfolio
limits remain separate. A score is not a probability of profit.

## Population and clocks

Every valid normalized record received through the existing $50k+ TM API feed
gets an assessment record, including delayed and nonqualifying records. Invalid
or redacted source observations remain in ingestion rejection accounting; they
are not invented vendor IDs. This is received-feed coverage, not all market flow.
Identity follows the existing feed-day plus vendor-ID key, with a separate model
version. Repeated polls and corrections do not create new study candidates.

Freeze TM's score, vendor fields and source/receipt clocks at first ingestion.
Assess observed Compass price context during that receipt's processing, within
30 seconds; record the actual assessment time. No later prices or revised TM
scores rewrite this assessment. Vendor corrections continue in the original
event archive and latest vendor record and are visible beside the frozen view.

Older stored records are inventoried separately. Their latest corrected vendor
snapshot is not relabeled as the original receipt-time score. Historical rows
do not receive invented prospective prices or a retrospectively fitted rating.

## Fixed Compass rubric

TM's numeric score is not an input to Compass's score. Some Compass components
use TM-supplied premium, volume and OI, so the inputs are not independent feeds.
Bullish/bearish direction follows vendor sentiment, never inferred from call/put.

| Component | Points |
| --- | ---: |
| EMA9/EMA21 aligned with direction | 20 |
| Current midpoint on the directional side of observed-session VWAP | 20 |
| Completed 15-minute trend aligned | 20 |
| Midpoint beyond the last five completed minutes in that direction | 10 |
| Recent minute volume / prior 20 observed minute average at least 1.25 | 10 |
| Contract volume/OI at least 2, with OI at least 100 | 10 |
| Reported premium at least $100,000 | 10 |

Missing inputs produce an incomplete assessment and a points interval, not a
zero score. Only complete numeric scores enter the primary matched comparison.
Frozen thresholds: TM at least 85; Compass at least 70. No optimization or alert
promotion is part of v1. Preserve score, delay, expiry and direction buckets.

## Measurements and comparison

Entry reference is a valid stored underlying midpoint no more than five seconds
old at assessment, with no future source timestamp. Record spread and source.
Checkpoints: 15 and 60 **trading minutes**, upcoming regular-session close, then
one, three and five subsequent session closes. Exchange calendars determine
holidays and shortened sessions. Extended-hours references are labeled separately.

Use the latest valid recorded underlying quote in the five seconds ending at
each checkpoint. It must have been fresh when stored and recorded within five
seconds of the checkpoint. Missing or late data stays unavailable. A delayed
worker can read timely stored evidence; a later backfill cannot repair it.

Primary endpoint: 60-trading-minute directional underlying percentage change.
Secondary endpoints use the same definition at the other checkpoints. Report
sample size, positive fraction, mean signed move and moves of at least +0.25%
or at most −0.25%. These are midpoint observations without fees or slippage;
they are not option returns, fills, path excursions or a trading win rate.

Compare all, TM-only, Compass-only and both on the **same cohort with both
scores and a usable checkpoint**. Also report eligible, pending and missing
denominators and selected/rejected arms. Records from one symbol/session are
correlated. Review by session and on later untouched sessions before changing
rules; overlapping observations do not establish independent statistical power.

## Operation and reporting

Use existing stock quotes and bars. Recent TM symbols can use spare slots in
the existing 500-symbol collection ceiling; original monitored symbols retain
priority and the DJT collection exclusion remains intact. Newly subscribed
symbols can initially lack receipt-time history; disclose missing inputs.
Known index option underlyings (including SPX/XSP, NDX and VIX) remain in the
ledger but are excluded from the stock-stream expansion. No ETF proxy is used
to fabricate an index outcome; unsupported reference prices stay unavailable.
No additional paid provider endpoint or options subscription is introduced.

SQL summaries cover the complete stated window with no record-count cutoff.
Detail pages are bounded and use stable keyset cursors. Lifetime received and
registered counts reconcile separately; historical inventory runs in durable
batches. New study data is stored in Compass PostgreSQL. No research messages
or trading orders are sent by this worker.
