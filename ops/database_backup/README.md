# Complete database recovery

This one-shot operational worker complements the bounded event archive. It takes
an exported, read-only PostgreSQL snapshot, dumps the complete application database
and password-free roles, fingerprints every table in that snapshot, and uploads
the package to the existing private archive bucket. Every object is downloaded in
full and its size and SHA-256 checked before restoration.

Restoration uses a new disposable PostgreSQL 18 cluster, a random local admin,
port 55432 on a Unix socket, and an empty TCP listen address. The restore process
receives no source database, market-data or messaging credentials. No application
is imported or started. Table counts and duplicate-sensitive content fingerprints
must match, including large objects, and every index must be valid. The final
manifest and receipt are saved and read back from the bucket before success is
reported. Production data is never restored or deleted.

Run as an unprivileged user in a separate worker with enough temporary disk for
the dump plus the restored database. The Dockerfile is an optional reproducible
image definition; the September 17 execution used the same worker embedded in a
one-shot postgres:18-bookworm service command. Do not replace the application's
root Dockerfile or Railway configuration with these operational settings.

Required protected variables:

- `SOURCE_DATABASE_URL`, `SOURCE_PROJECT_ID`, `SOURCE_SERVICE_ID`, `SOURCE_COMMIT`.
- `EXPECTED_DATABASE` (defaults to `railway`).
- Existing `ARCHIVE_BUCKET`, `ARCHIVE_ENDPOINT`, `ARCHIVE_REGION`,
  `ARCHIVE_ACCESS_KEY_ID`, `ARCHIVE_SECRET_ACCESS_KEY`, and optionally
  `ARCHIVE_ADDRESSING_STYLE`.

Keep restart policy NEVER, no cron, no public endpoint and no production volume
mount. Credential values stay in protected service configuration. Read-only
source transactions have a three-second lock timeout and two-hour statement
timeout; runtime commands have bounded timeouts. Unknown extensions, logical
subscriptions and foreign servers require explicit review before a fresh dump.

## Resume a saved backup

If the dump and object readback finish but the isolated restore fails, preserve
the unique backup prefix. Set `RESTORE_PREFIX` and `EXPECTED_DUMP_SHA256` to the
values in the `backup_durable` receipt. Resume reads and validates the saved
manifest and downloads and checks every file again. It does not connect to the
production database. The first September 17 attempt encountered a `runuser` PATH
reset that hid `initdb`; the worker now resolves PostgreSQL executables explicitly.
Do not restart an active restore just to poll progress.

Success requires a `DATABASE_BACKUP` record with `stage=verified`,
`isolated_restore_verified=true`, `invalid_indexes=0` and durable receipt readback.
A Railway SUCCESS deployment or `backup_durable` log alone is insufficient.
`snapshot-manifest.json` preserves the source fingerprints before restoration;
`verified-manifest.json` adds restored fingerprints; `receipt.json` summarizes
both. Objects have unique names and remain available if a later stage fails.

These are logical application-database snapshots. They do not provide physical
cluster recovery, continuous PITR, a backup of service credentials or cross-database
snapshot simultaneity. Refresh originals before retirement and preserve their
configuration and secret references separately. Never start restored application
outboxes or collectors as a restoration test.

## September 17 investigation

`scripts/night_recovery_audit.py` is a dated read-only audit, not a scheduled job.
It records complete September 17 cohorts and explicitly bounded quote windows.
The final compressed log chunks carry a checksum and expected chunk count; only
a fully assembled, checksum-matching result with `done.complete=true` is complete
evidence. Individual log-row omissions are not missing database observations.

Clock interpretation matters: the historical `gap_detail.checked_at` can be a
point within recorded-path replay. `archive_check.*.checked_at` carries the outer
evaluation time. The audit's `*_by_gate` names compare insertion timestamps to the
former recorded-path timestamp; they do not establish wall-clock availability at
the decision. Likewise, `events.received` is an insertion timestamp, not a commit
visibility proof. Preserve these distinctions when investigating later-arriving
quotes. Never use an audit to rewrite a frozen outcome or fabricate an entry
receipt.
