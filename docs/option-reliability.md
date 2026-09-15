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
