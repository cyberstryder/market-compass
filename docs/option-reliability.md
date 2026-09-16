# Option receipt reliability

The option sampler previously discarded the last quote inside each one-second
sampling interval unless another quote arrived later. It now coalesces that quote
and flushes it when the interval ends, retaining its source and socket-read clocks.
Pending quotes count toward diagnostics. Existing bounded queues still fail loudly.

Massive stream and REST samples are archived even when a newer quote has already
won the latest-state update. Cache timestamps remain monotonic; archive identities
remain idempotent. This prevents concurrent writers from losing valid older samples.

The dedicated live recovery reader checks only open intraday option observations
whose quote is more than two seconds old. Each cycle uses at most four concurrent
requests, a two-second timeout, a five-second per-contract cooldown, and a two-second
supervisor interval. Selection rotates by oldest attempt, with a 200-observation
bound and explicit truncation. HTTP 429 backs off globally for 60 seconds; 401/403
for 300 seconds. There is no paid fallback or automatic entitlement change.

GET /v3/quotes/{optionsTicker} requests at most 32 quotes within the preceding five
seconds. Only valid bid/ask quotes still within five seconds of their original SIP
time at receipt are retained; REST retrieval time never becomes source time. The
original five-second freshness and fifteen-second continuity gates remain intact.
This is timely live recovery, not historical repair. Prior unresolved outcomes stay
unchanged. New observations carry collection_version=option-reliability-v1 and get
a separate validation count. A completed result is not proof of profitability.

Provider contract: https://massive.com/docs/rest/options/quotes

A second recovery stage makes one Alpaca request for up to four contracts where
Massive returned no usable fresh quote. It explicitly requests feed=opra; no
indicative substitution is allowed. The same original-time, five-second freshness
checks apply. Access failures back off for five minutes, other failures one minute.
Responses retain provider identity and quote timestamps, including stale timestamps
in diagnostics. Entries using this collector version are counted separately as
option-reliability-v2. No provider subscription purchase is performed.

Alpaca contract: https://docs.alpaca.markets/us/reference/optionlatestquotes

## Receive isolation and transient database recovery

September 15 verification found option source ages of 28–68 seconds despite
small persistence queues. Option receipt still shared the main collector event
loop with synchronous database operations. It now runs on a dedicated loop;
connection health writes run off that loop. A regression test blocks the main
loop and verifies option receipt continues. This removes a buffering risk; it
does not establish that all observed delay was local.

COMEX logged a persistence OperationalError at 16:48:51 UTC, followed by
clustered observation gaps and resubscription at 16:50:50. Futures writers now
retain and retry the same batch up to three attempts on OperationalError
(250/500 ms pauses). Exhaustion remains explicit. Database telemetry failures
no longer stop receipt. Optional feeds reconnect after five seconds for chained
database operational failures; other errors retain the 120-second backoff.
Original source timestamps and observation freshness limits are unchanged.
Forward validation must separately assess option age, completed observations,
and futures write/reconnect failures; healthy deployments alone are insufficient.

## Continuity qualification and dashboard

New intraday option entries use quote-continuity-v1: at least five distinct
usable source timestamps in the preceding 30 seconds, spanning at least 20
seconds, latest no older than five seconds, maximum interior gap ten seconds.
Only quotes available and fresh when recorded qualify. At most eight eligible
contracts are checked; truncation is retained in the candidate record. Passing
contracts are ranked by smaller maximum gap, then more samples, retaining the
prior ranking for ties. This is a research eligibility policy, not a guarantee
of future liquidity. Original strategy alert systems remain authoritative.

The Observation reliability panel shows collector-version cohorts over a bounded
24-hour window, unfinished observations, completed/opened and unresolved/opened
rates, exclusions separately, quote source age, queue depth, latest recovery
results and evidence age. Option collector v3 marks this release. Comparisons
across versions are descriptive: contracts, selection policy and durations differ.

## September 16: stock receipt and recovery isolation (v4)

The regular-session failure audit showed fresh archived option samples paired
with stale/invalid underlying samples, including MU and SPY near 19:30 UTC.
Other failures had genuine option-update gaps. Code inspection found that the
stock socket and live option REST recovery still shared the collector loop with
synchronous chain/database work. Stock quote and completed-bar persistence also
shared one writer. These are verified contention paths; historical telemetry
cannot attribute every unresolved observation to them.

Stock receipt and live option recovery now each run on a dedicated loop. Recovery
creates and closes its own HTTP client on that loop and retains the existing
request limits, cooldowns, entitlement backoffs and original source timestamps.
Stock quotes and bars use independent writers. In-flight writes are drained on
shutdown; pending/failed writes remain explicit. Sample rates remain unchanged.
Out-of-order Alpaca stock samples are archived without rewinding latest state.

Stock telemetry separates source-to-socket latency, socket-to-completed-storage
delay and silence since the last quote. The dashboard reports the latest
per-symbol maxima, not percentiles or a session-wide service-level result.
An old quiet symbol and a delayed quote are different observations. After-hours
telemetry alone cannot establish regular-session reliability.

New option stream/recovery and stock samples use `option-reliability-v4`.
New option observations retain both their option and underlying collection
versions. No historical statuses, five-second freshness checks, fifteen-second
continuity limit, continuity qualification or portfolio rules change. A quote
that reached the socket promptly but was stored too late remains ineligible
for recorded-path replay under the original storage-time rule.

Acceptance remains a full subsequent cash session: compare versioned completed,
unresolved and excluded cohorts; inspect every new failure cluster using both
stream clocks and archive evidence. A successful deployment is only startup
verification. Roll back application code if needed; no schema migration or
rewriting of historical outcomes is required.
