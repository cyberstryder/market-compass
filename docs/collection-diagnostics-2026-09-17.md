# Quote-write and intake diagnostics — September 17, 2026

PostgreSQL reported quote-cache deadlocks at 11:10 and 11:42 CT. Both sides
were state quote upserts. Streaming and recovery batches could take the same
symbol locks in different input orders. Quote batches now sort symbols before
opening the transaction. Within-symbol samples retain their order, older sampled
history remains retained, and latest quote state still uses its source-time guard.
The PostgreSQL test runs overlapping reversed batches and requires both commits
and all four historical samples.

## Option subscriptions

`option_subscription` events retain connection ID, sequence, contract/channel,
requested/sent/acknowledged times, first data receipt/source time and disconnect.
The stream records provider statuses after credential redaction; only an explicit
per-channel successful subscription response counts as an acknowledgement.
Authentication, a sent request, and a received quote remain separate facts.
Freshness and continuity eligibility rules are unchanged.

Events are persisted on an independent bounded queue, with idempotent identities.
Feed health displays acknowledged quote contracts versus requested contracts,
pending/dropped evidence and persistence errors. Reconnects start a new identity;
an older connection's acknowledgement cannot make the new one look acknowledged.
Unknown status formats remain available as redacted provider-status records.
There are no synthetic acknowledgements for historical requests.

Provider format references: [WebSocket quickstart](https://massive.com/docs/websocket/quickstart)
and [subscription acknowledgement behavior](https://massive.com/knowledge-base/article/how-many-tickers-can-you-subscribe-to-on-a-single-massive-websocket-connection).

## Morning request identity

Each authenticated HTTP intake attempt gets a server-generated request ID, returned
in X-Compass-Request-ID and the JSON response. A validated, bounded upstream
request ID is retained if present; credentials, paths and general headers are not.
The morning_request event is committed in the same transaction as the input,
receipt and preview. Successful duplicate requests have separate attempt IDs while
the source-event receipt remains deduplicated. The first source receipt links to
its initial request ID.

Logs distinguish received, committed, response-ready, invalid/conflicting inputs,
database failures and interrupted requests. write_at in the stored event is
before commit; the worker's committed_at log is emitted after commit returns.
Response-ready is not a client-delivery receipt. If HTTP cancellation occurs while
the worker finishes, its eventual commit still gets a separate log. Earlier
requests without IDs remain uncorrelated; no historic receipts are invented.

## Morning history recovery

Transport failures get one bounded reconnect/read retry with the same committed
cursor. Continued failure uses a capped per-stream backoff. Schema/checksum and
database failures are not silently treated as HTTP transport failures. Status
retains last success/completed scan, failure count, cursor and most recent failure;
successful recovery clears the active error while preserving its evidence.
The noon timeout recovered before this release; it is not proven to have caused
the opening's missing initial receipts.

## Closing audit

scripts/session_close_audit.py is an explicit September 17 read-only audit, not
a scheduler or backfill. It freezes its snapshot, bounds queries and identifies
failed sections. It reports option, Morning, TM, Swing, SPY, receipt and setup
counts. Futures coverage includes the previous evening and stays partial until
the documented session endpoint. Independent trial sums are not account P&L;
unresolved paths are not losses and pending later checkpoints are not failures.
