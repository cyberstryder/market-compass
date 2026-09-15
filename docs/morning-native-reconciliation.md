# Native candle reconciliation

The initial Compass history importer omitted `morning_stock_bars_v2` rows written
directly by Morning v1.3 `accept_frame`. Its derived checkpoint events use schema
1 and contain no candle arrays. Research batches alone also do not represent the
explicit signal-to-native-candle mapping. Consequently, the latest 100 imported
signals were incorrectly presented as having no complete native candle paths.
That was an importer coverage limitation, not proof the source lacked those bars.

The authenticated Morning history route now adds `stream=stock`. It exports each
non-test source signal, original initial-receipt metadata and its complete native
candle inventory (up to 180 minutes), including individually hashed candles and
their actual source receipt times. Repeated inventories discover later frame writes.
Signals recovered without any entry/checkpoint event are included.

Compass prioritizes this stream and imports candles in bounded atomic batches.
Individual candles remain immutable; changed prices fail the entire page. The
latest source inventory may grow, but a smaller inventory never deletes previously
saved Compass candles. The report compares timestamps and hashes per signal and
shows missing, extra and conflicting entries against the latest source snapshot.
Source candle coverage and Compass coverage are shown separately. A matched
inventory can still have genuinely incomplete source observations.

At the end of each stock scan, a bounded report audit logs reconciliation and
source/Compass coverage counts for the latest 100 signals. Its truncation is
explicit. This is snapshot parity, not proof of watchlist-wide live capture.

Tests use actual v1.3 source frame ingestion with primary packets deliberately
missing and recovery frames retained. They reproduce the event-only false gap,
restore 180 native candles, match the original source coverage/exit calculations,
preserve real source gaps, reject conflicting candles, discover inventory growth,
and expose shrinkage without deletion. Existing alerts and execution stay with
their original programs.
