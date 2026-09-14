# Futures gap and vendor freshness forensics

The private observation audit now includes up to 20 recent unresolved futures
trials since entry-variant activation, with a bounded 200-quote window around each
gap. It reports source/receipt clocks, invalid/stale/future timestamp rejection
counts, late storage, next retained quote, and the original gap metadata. It never
rewrites a trial or fills an unobserved path. No recorded quote alone cannot prove
whether the vendor was quiet or the collector disconnected.

Vendor diagnostics inspect already retained envelopes for timing/cache fields only.
Market values, original webhook bodies and credentials are excluded. The audit
also counts per-item clocks so a missing aggregate clock is not mistaken for
missing clocks across every constituent.

TraderMatrix's public [REST reference](https://www.tradermatrix.pro/developers)
shows Apex freshness.computedAt, refreshSeconds, nextRefreshAt and sessionState.
The example has a 300-second refresh interval; it is not a guarantee for matrices
or unusual flow. Preserve those fields and show overdue duration separately from
source and poll age. Do not substitute nextRefreshAt or receipt time for source
time, increase confirmation-age limits, or introduce cache-busting requests.

The documented 30 requests/minute limit is shared with MCP calls. Existing 2.6s
request-start spacing is retained. Matrices expose snapshotTime; unusual flow uses
tradeTime. The reference does not establish a matrix cache TTL or a flow latency
SLA. Any delay beyond verified local causes remains a provider question.
