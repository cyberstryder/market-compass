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
