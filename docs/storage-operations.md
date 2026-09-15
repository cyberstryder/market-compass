# Storage recovery, accounting and archive safeguards

Incident: PostgreSQL exhausted the original approximately 5 GB volume on September
15, 2026. User expanded the attached volume to 50 GB. Database recovery completed
at 03:22:32 UTC; PR36 services restarted afterward. No WAL reset or evidence
removal was performed. Approximately 02:47–03:22 UTC is a database outage interval,
not an exact per-feed missing-data interval. Verify recorded timestamps per feed;
recovered bars never stand in for unavailable historical live quotes.

## Operational accounting

Engine captures every five minutes: current database size, WAL directory bytes
when permitted, largest 15 tables/indexes and estimated live/dead rows. Queries
have five-second statement timeouts. Database connections now have a ten-second
connect timeout so failed connections do not hang indefinitely.

`STORAGE_BUDGET_GB=50` is a configured budget, **not** discovery of the filesystem
capacity. Database plus WAL is partial accounting: other databases, logs and
filesystem overhead can fill the disk earlier. The panel never labels this free
space or a filesystem health guarantee. Missing WAL permissions are shown as null.
Use Railway's volume metrics for actual disk utilization.

Warnings at 70%, high at 85%, critical at 95% of configured budget. A positive
observed growth rate predicting less than 24 hours to budget also warns. Growth
requires samples at least one hour apart, within the last day; negative growth
is floored at zero. WAL recycling or table bloat can change rates. Projections
are estimates, not guarantees. Missing reports become stale after 15 minutes.

The Feed Health panel and `Storage accounting` runtime logs expose this report.
No new Discord messages are sent. Database outages prevent database-backed UI and
state writes, so Railway monitoring remains necessary for out-of-process alarms.
Automatic backup status and exact free disk space are not currently verified.

## Lossless event export

`python -m compass.archive export --destination /durable/path/events-0001.jsonl.gz
--after-id 0 --max-rows 100000` exports up to 100,000 events (maximum one million),
including IDs, keys, source/receipt clocks and the full payload. It opens a
PostgreSQL repeatable-read, read-only snapshot; no tables are initialized or
mutated by the export. JSON lines are compressed and checked for count, SHA-256,
order, bounds and manifest consistency before atomic publication without overwrite.
`python -m compass.archive verify /path/events-0001.jsonl.gz` re-reads the entire
compressed stream. A partial failure removes only the temporary export.

The manifest contains `continuation_after_id` and a snapshot high-water mark.
**Continuation is not cross-chunk completeness proof:** concurrent transactions
can commit lower sequence IDs after a snapshot. Reconcile all IDs against a
settled source snapshot before any future eviction. There is no deletion command
or deletion eligibility receipt in this release.

These exports cover `events` only. They do not replace PostgreSQL backups, source
state/review/ledger table exports, a restore drill, or a durable object-store tier.
A local verified file does not prove durable storage. The production archive
destination is not provisioned or verified, and no automatic archive job runs.

Next: select/provision durable object storage; export and verify copied bytes;
restore into an isolated test database and compare keys/counts/checksums; implement
archive-aware research reads and outstanding-trial protection; reconcile all
source IDs; only then consider eviction of verified archived rows. Unlimited
research collection remains enabled throughout. A blanket retention deletion is
not an acceptable response to storage pressure.

An isolated event-history restoration is now available:

`python -m compass.archive restore-isolated events-0001.jsonl.gz --destination restored.sqlite`

It verifies the archive, writes a new SQLite file, reads every reconstructed event
back in ID order, and compares canonical content hashes. Existing destinations
are rejected. It does not write to production, restore strategy state, establish
multi-chunk completeness, or demonstrate durable storage. No connected tool
currently provisions an archive bucket; durable destination setup and production
copy/readback remain outstanding. Cleanup stays disabled.

## Private object-store verification job

`python -m compass.archive_object` exports at most 10,000 events by default,
uploads to a unique private object key, downloads the bytes, checks size and
SHA-256, verifies the event manifest, restores a new SQLite research database,
and writes and reads back a JSON receipt in the same bucket. It prints only the
receipt, never credentials. A mismatch exits unsuccessfully; it does not delete
source data or uploaded evidence. This verifies a bounded copy, not full-database
backup coverage or safe eviction. Repeated runs create independent objects.

Required worker variables: `DATABASE_URL`, `ARCHIVE_BUCKET`, `ARCHIVE_ENDPOINT`,
`ARCHIVE_REGION`, `ARCHIVE_ACCESS_KEY_ID`, `ARCHIVE_SECRET_ACCESS_KEY`.
Optional scope: `ARCHIVE_AFTER_ID=0`, `ARCHIVE_MAX_ROWS=10000` (maximum 100000).
`ARCHIVE_ADDRESSING_STYLE=virtual` defaults to current Railway bucket style.
Use `railway-archive.toml` for a separate one-shot service, with no restart or cron.

Provision a private Railway bucket named `compass-archive`. In the archive worker,
reference `${{Postgres.DATABASE_URL}}` and the bucket's `BUCKET`, `ENDPOINT`,
`REGION`, `ACCESS_KEY_ID`, `SECRET_ACCESS_KEY` variables using matching
`ARCHIVE_` names. Do not copy secrets into repository files or chat.
The connected direct Railway tools cannot create buckets, and the September 15
read-only infrastructure capability check returned a tool serialization failure.
Bucket creation/wiring and a real production run remain outstanding until the
bucket is provisioned. No recurring export or database deletion is enabled.
