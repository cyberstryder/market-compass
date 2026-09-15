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

## September 14 findings and corrections

The first production forensic audit found two unresolved trend-pullback trials:
MYMU6, with no retained quote from 20:22:18.065 UTC until 20:22:35.762 UTC;
and YMU6, with a 15.212-second retained-quote gap at 20:30:18.303 UTC.
They had already replayed 1,808 and 1,846 samples respectively. These are genuine
missing intervals in the recorded path, not a total archive failure. Their
historical outcomes remain unresolved.

Dow collection used prior-day volume-ranked September contracts on the
[September 14 CME roll date](https://www.cmegroup.com/trading/equity-index/rolldates.html).
MES/MNQ already selected December under the customary quarterly policy. YM/MYM
now use explicit December lead contracts in their existing isolated CBOT stream.
The YM.v.0/MYM.v.0 configuration names remain compatible labels, but the recorded
policy explicitly identifies customary quarterly selection. Selection rejects a
still-valid provider mapping to an obsolete lead quarter. No claim is made that
rolling guarantees uninterrupted quotes.

Open trials/positions retain subscriptions to their original contracts and are
never spliced to new-month prices. A periodic check on incoming records/heartbeats
reconnects the optional stream when its lead quarter changes. No historical API
purchase or new subscription plan is introduced; existing live OHLCV replay and
quote subscriptions use the selected dated contracts. The 15-second observation
limit is unchanged.

TraderMatrix matrices expose genuine source snapshotTime but no refresh schedule
in the inspected envelopes. Thermal/VIX expose an envelope timestamp; preserve it
as vendor_envelope_ts, without treating it as independently confirmed market
observation time. Six index-gamma constituents and 36 signal rows had individual
source clocks despite missing aggregate clocks. They must be judged individually.
Unusual-flow's latest event had stopped before the cash close; subsequent
post-close aging is not itself proof of feed failure. Intraday cached matrix
delays remain a vendor issue pending an endpoint refresh guarantee.

## Before-session input acceptance

The operational audit now separates current quote/minute freshness from retained
history structure during closed sessions. It checks the full collected stock
universe for 30 consecutive recent minute slots; exposes TGT/SBUX input details
and cached Smoothers daily readiness; and checks the expected December Dow
contracts for archived two-sided quote samples, within-window source gaps,
completed minute history and completed 15-minute context. Historical feature
checks run at their last completed bar and never establish current eligibility.

Futures outcome counts are the prospective repeated-entry cohort, with its
activation clock and report time, so the old September gaps stay identifiable.
The existing ten-minute audit continues across the overnight reopen. A closed
session with stale quote timestamps is not called a live-feed failure, and no
future-session trial or successful checkpoint is inferred before it occurs.

## Overnight ES/MCL investigation — September 14

The archived ESZ6 gap near 22:45 UTC and MCLV6 gaps near 22:53, 22:58 and
23:25 UTC show usable quotes resuming approximately 17–22 seconds after the
prior usable quote. The next quotes were received near their source timestamps.
The 15-second research continuity limit therefore marked these trials unresolved.
Collector vendor logs continued, but those logs do not prove that the independent
Databento callback streams were continuously processing quotes. Retained events
alone cannot establish whether raw quotes were absent, skipped or delayed.

`Futures ingress` private logs now provide cumulative per-connection counts and
maxima every 30 seconds when callbacks run: incoming MBP1 records before sampling,
valid/invalid BBO counts, source and local callback gaps, event/SDK receive ages,
selected quote writes, completed write calls, and callback/write duration. SDK
receive time is a provider clock, not local socket receipt. A completed write call
does not establish a committed insert. An absent log during silence is not proof
of disconnect; the next callback can report the gap. These counters are bounded to
32 instruments per stream and never synthesize quotes or alter historical results.

The first instrumented deployment reproduced a processing backlog during replay:
CME quote event/SDK-receive clocks were 37–42 seconds behind callback execution,
while consecutive source records were less than one second apart. Optional streams
also showed 22–28-second receipt delays. This is direct evidence of a current
processing delay, but does not retrospectively establish every old gap's cause.

Quote and replay-bar persistence now runs in a bounded worker per stream. Receipt
callbacks enqueue the existing sampled quotes without waiting for those database
writes; the worker batches adjacent quote jobs into transactions. Source clocks,
sampling throttle and retained-path freshness rules are unchanged. Queue overflow
and worker failure raise explicit errors for controlled reconnect; they never
silently refresh or fill a missing quote. Private persistence logs expose queue
age, depth and write duration. Ingress write-call timings now measure enqueue calls.
