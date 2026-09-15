# Morning research and native-candle import

The collector reads Morning's authenticated `/api/compass/history` endpoint using
the existing scoped integration token. Separate `events`, `research` and `stock` streams
export all available non-test history in ordered pages of up to 20 source events.
No 14/45-day truncation is applied to this history route. Requests are bounded to
8 MiB and timed out. The collector reads up to five pages per stream per cycle,
persists its cursor only with a successfully committed page, and repeats completed
scans after a minute. A scan is not a transactionally frozen source snapshot;
repeated scans recover late arrivals with lower IDs, including after restarts.

`morning_history_v1` preserves immutable source envelopes (event ID, checksum,
payload, source receipt time) and separate import times. It also stores normalized
signals, sessions, native signal/research candles and all five research candidate
types. A repeated candle keeps the earliest source receipt encountered so far.
Conflicting IDs/candles fail and roll back the whole page; the cursor stays in
place for investigation. Raw source events and normalized children commit together.
No source rows are modified and no Compass live quotes, alerts or orders are
created by this importer.

Validation is pinned to Morning's `Event` (schema 1/2) and `ResearchBatch` (schema 3)
models at source commit `f459740a452a3cdf17fbb62af3d38edc1fc63c4d`. The two modules in
`compass/morning_schema` are copied unchanged apart from provenance comments.
The stock inventory stream also preserves direct v1.3 frame candle writes; see
`morning-native-reconciliation.md`. Source protocol upgrades require explicit matching validation and fixture review.
Synthetic fixtures were generated from that source's own test helpers.

The Connected Projects transfer panel shows counts, per-stream status and last
completed scans. `scan_complete` only means the cursor reached the end of the
available source stream. It does not prove that TradingView delivered every bar,
that the source has complete history, or that imported historical data was known
to Compass in real time. Legacy schema-1 checkpoints contain no native candles.
These absences are not reconstructed from present prices.

Still pending: source-equivalent coverage/outcome reports and exports, continuous
forward parity cohorts, direct webhook ingestion, signal generation, option
enrichment, and notification ownership. Research imports are not a full source
database backup or an execution migration. Original programs remain authoritative.
