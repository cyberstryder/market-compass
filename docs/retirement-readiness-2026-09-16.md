# Morning Algo and Smoothers retirement readiness

Evidence date: September 16, 2026, before the regular equity session. This is a
preparation record. Both original applications remain running and own their
official Discord alerts. No source service, volume, repository, or TradingView
alert was retired by this work.

## Verified source backups

| Source | Snapshot UTC | Application tables | Rows | Dump size | Isolated restore |
|---|---|---:|---:|---:|---|
| Morning Algo | 2026-09-16 12:50:22 | 12 | 317,190 | 33,870,154 bytes | Passed |
| Smoothers | 2026-09-16 12:51:55 | 4 | 4,456 | 130,665 bytes | Passed |

Both sources run PostgreSQL 18.6. Each export is a complete custom-format dump of
its connected `railway` application database, taken from an exported repeatable-read,
read-only snapshot. The verification inventory and dump use the same snapshot.
Role definitions are included without passwords. There were no additional
non-template databases other than the standard `postgres` administration database.

Each dump was restored with owners and permissions into a new local PostgreSQL 18
cluster inside its separate verification worker. The restore server exposed only a
Unix socket; no production volume was mounted. Every application table's row count
and SHA-256 row multiset fingerprints matched. Large-object inventories matched
(both empty), and both restored databases had zero invalid indexes. The worker
never started either trading application.

The export crossed the temporary download endpoint encrypted with AES-256-GCM and
an RSA-wrapped data key. The private key remained outside Railway. Downloaded
ciphertext, decrypted package, and inner dump checksums were verified locally.
The delivered backup bundle contains directly restorable dumps, manifests,
role definitions, archive contents lists, and the diagnostic evidence.

| Dump | SHA-256 |
|---|---|
| Morning | `575fa35f0b515cbd3359b3cb2a65d9ded80984e615a4da10c44aafb3bdbbdc3a` |
| Smoothers | `13739f824854cd64cbcd3e0969a0eb0147ca18c1cdde82c69f517801f1bbe05c` |

These snapshots cover the stated times. Take another full backup after the final
source session/week and before eventual shutdown. They are not continuous PITR,
physical cluster backups, or exports of Railway environment secrets. Keep the
original Railway variables available until a separate secure configuration
recovery record exists. Compass's bounded event archives do not replace these
database backups.

Implementation: isolated branch `codex/source-backup-restore-20260916`, commit
`e86c911c3b732aa19833ff983ca04434db923bba`. Its root Railway configuration is
worker-specific and must never replace the application's production configuration.
The reusable worker lives under `ops/source_backup/`.

## Exact retirement inventory

| Resource | Morning Algo | Smoothers |
|---|---|---|
| Railway project | Morning Algo Tracker | New Smoothers |
| Project ID | `98824a1f-2a42-4023-8159-a98f0657c2f3` | `77861f99-253a-4065-8d68-037dbf6b1f3e` |
| Production environment | `5d584acf-b042-4d92-bcd0-a7680a8d3416` | `e0f2e0b7-ed16-4dec-ba95-585536d27e84` |
| Application service | Morning Algo Tracker | smoothers-new |
| Application service ID | `30af07ca-beae-405e-9356-4646ce90f7c6` | `da2b0ac7-802c-454e-bdcf-8e50a03b77ca` |
| Database service | Postgres | Postgres |
| Database service ID | `7201d70c-7963-4bd5-bf29-2a3751b9f53d` | `00f658af-b22e-4ee4-84db-4588bc8b1970` |
| Volume ID, mounted at `/var/lib/postgresql/data` | `23819860-c7de-4288-a2d5-143bfeade0f4` | `28717429-c419-4486-87b9-a5ee51550b37` |
| GitHub repository | [cyberstryder/morning-algo-tracker](https://github.com/cyberstryder/morning-algo-tracker) | [cyberstryder/smoothers-new](https://github.com/cyberstryder/smoothers-new) |
| Default branch | main | main |
| Verified deployed source commit | `41ac6c739ab2d5d68154dd4cd0e0acd09ecc5ae6` | `fa3892b6bd12a7f17d026339c6f566eae8a95c80` |
| Current application deployment | `fdb2ccf1-7e5b-49d8-8aae-e0c1d9899e9a` | `09a1c96a-2917-494f-906a-e94979d4ae20` |
| Repository status | Private, not archived | Private, not archived |
| Public application domain | `morning-algo-tracker-production.up.railway.app` | `smoothers-new-production.up.railway.app` |
| GitHub workflow | `.github/workflows/ci.yml`, push/PR checks | `.github/workflows/compass-bridge.yml`, push/PR checks |

Neither inspected repository workflow has a scheduled trading job. Smoothers'
trading schedules run in its application process. Repository archiving therefore
does not stop either Railway application or its alerts.

Temporary verification services created for this drill:

| Project | Service ID | Successful backup deployment |
|---|---|---|
| Morning Algo Tracker | `6866c3d0-c311-44cb-8f16-bf990eb0aa17` | `b32a31e2-0eb9-4786-b969-fd6d7e2210fc` |
| New Smoothers | `d823c7a6-6e3b-41e1-b047-c353afed8148` | `6e288f57-796c-4cd6-a16e-5b8b8c226760` |

These workers have no persistent volumes, no cron, and restart policy NEVER.
After durable export verification, acknowledge and close their download servers
and clear their database-reference/download-token variables without redeploying.
The stopped service definitions can be removed with the original projects later.
Their domain IDs are respectively `4faea773-37b5-467c-a12e-a6d835686501` and
`6e788d5d-21c4-4e6f-8753-986fb3428f32`.

Do not include Market Compass's engine, collector, dashboard, Postgres, or its
research runner in the original-app retirement. ZenTrading, SPX Monitor,
atlas-exposure-engine, and Atlas AI are outside this retirement scope.

## Dependencies to resolve at cutover

| Dependency | Current state | Retirement action after acceptance |
|---|---|---|
| Morning TradingView original alert | Feeds the original application | Preserve during comparison; pause at the accepted sender boundary only |
| Additional Compass Morning intake | `direct_shadow` scheduled for 2026-09-16 | Prove actual entry receipts and checkpoints; a schedule alone does not prove the external alert is active |
| Compass source polling | Authenticated Morning/Smoothers feed integrations remain enabled | Retain during validation; disable polling after final source evidence is captured |
| Morning source credentials | `MORNING_SOURCE_URL`, `MORNING_INTEGRATION_TOKEN` in Compass | Remove only after polling ends; retain separate `NATIVE_MORNING_INTAKE_TOKEN` |
| Smoothers source credentials | `SMOOTHERS_SOURCE_URL`, `SMOOTHERS_INTEGRATION_TOKEN` in Compass | Remove only after final source comparison; native jobs use Compass's own Alpaca access |
| Smoothers configuration | Compass owns all 76 configurations and immutable revisions | Preserve revision/history and confirm the next native cohort freezes the intended revision |
| Official Discord senders | Both original applications | Reconcile final receipts, pause original sender, then activate the accepted Compass sender; never overlap ownership |
| Futures TradingView observer clones | Separate original-strategy evidence | Keep as a separate decision; they are not Morning/Smoothers retirement resources |

Configuration revision observed at 12:54 UTC:
`23c51d04bfb5f79e5db77aa1fa76b2246ca38fbe720211954f5b1144f4aa16f5`.
Both ownership records were still `original`.

## Remaining acceptance gates

Morning requires a complete direct-intake session with all source signals matched,
candle inventories reconciled, option selections and scheduled samples accounted
for, and the expected entry/edit previews matched to original Discord delivery
evidence. A truncated report, missing direct entry provenance, missing candles,
or ambiguous deliveries blocks acceptance. The existing handoff controller also
requires a fresh post-session source check and a future session boundary.

Smoothers requires a full native weekly cohort: entry, configuration revision,
ranking, featured signals, target observations, premium measurements, final
closure, and notification comparisons. The week beginning September 14 was
marked `missed` because native ownership was introduced after its entry window.
Historical imports do not establish a native full-week cycle. The next candidate
week is September 21–25, subject to its actual calendar and complete evidence.

Additional implementation blocker found during retirement review: Smoothers has
no equivalent of Morning's guarded prepare/activate/rollback API. Its four
notification queue call sites omit `event_time`; the shared outbox deliberately
keeps such events in shadow when ownership has an `effective_from` boundary.
Its current messages are shadow previews, including a summary explicitly labelled
“shadow only.” Complete and review the Smoothers handoff, event-time eligibility,
and official message parity before switching it. Do not bypass this with a manual
ownership database edit or by weakening the shared outbox gate.

## Quote and candle investigation

September 15 regular-session candles were compared with retrospective Alpaca SIP
bars for SPY, QQQ, TGT, SBUX, RIOT, BSX, BMY, ORCL, NOK, MAR, MCD, and VRT.
All provider-present minutes were in Compass, with matching OHLCV values.
Ten symbols, including SPY, had all 390 minutes. TGT had 389 and MAR had 388;
the same three missing minute slots were absent from the provider response.
This comparison does not identify the market reason for source absence.

Eight recent unresolved `option-reliability-v3` observations were compared with
Massive's historical quote endpoint. During each terminal gap the endpoint
returned either zero quotes or only the last observed millisecond's quote with
finer timestamp precision. No later quote was found before that gap ended.
The bounded examples support source quote silence, not a newly proven collector
drop. They do not prove all unresolved observations have the same cause.

Recorded quotes in the inspected windows generally reached the socket within
0.04–0.17 seconds of source time and were written within roughly 0.01–1.05 seconds.
Historical REST results do not establish what was available at the decision time.
No missing quote was invented, timestamp refreshed, or frozen outcome rescored.
The 15-second continuity limit and entry rules remain unchanged.

SPY had 19 minutes first persisted more than 30 seconds after candle close despite
its complete final archive. All 19 belonged to the 16:51–17:12 UTC (12:51–13:12 Eastern) window;
maximum delay after close was 812.6 seconds. No later regular-session SPY minute
exceeded that 30-second post-close threshold in this audit. The affected collector
logs show option receipt delays, import timeouts and a persistence failure before
its replacement with the receipt-isolation build. This bounds the historical
incident; it does not prove a single cause for every late minute or validate the
next live session. The final evidence file records exact source and receipt clocks.
Live timeliness and final archive completeness remain separate acceptance tests.

## Reversible retirement sequence

1. Complete the appropriate session/week evidence and the Smoothers delivery
   implementation gates. Review the exact original/native notification pairs.
2. Capture final source database snapshots and repeat isolated restore validation.
   Preserve deployment commits, settings, and externally managed secrets for recovery.
3. Schedule a future clean boundary. For Morning use its existing guarded handoff.
   For Smoothers use the reviewed handoff once implemented; do not change sender
   ownership through ad-hoc SQL.
4. Pause the original sender, verify no pending/ambiguous requests, then activate
   Compass. Keep the original app and database available during the observation
   and rollback period. Old shadow notifications must remain shadow.
5. If delivery is wrong, revoke the Compass owner first, reconcile in-flight and
   ambiguous deliveries, then resume the original sender. Restore the appropriate
   routing schedule without mixing original and direct provenance mid-session.
6. After accepted official operation, stop source polling and original application
   jobs. Preserve the databases and backup copies. Archive the two exact GitHub
   repositories only after the final source commits are recorded.
7. Delete original services/volumes/projects only as a separate final retirement
   action after recovery prerequisites and the rollback period are satisfied.
   This preparation does not authorize or perform deletion.

## Restore one delivered database in isolation

Unzip the backup bundle. Each source folder contains `database.pgdump`, `roles.sql`,
`contents.txt`, and `manifest.json`. Verify the dump's SHA-256 against its manifest.
Use PostgreSQL 18. The following example creates an isolated local Docker container
with no network or published ports. Run it from the unpacked bundle directory:

```bash
docker run -d --name compass-restore-morning --network none \
  -e POSTGRES_USER=restore_admin -e POSTGRES_HOST_AUTH_METHOD=trust \
  -v "$PWD/morning:/backup:ro" postgres:18-bookworm
docker exec compass-restore-morning pg_isready -U restore_admin -d postgres
# Continue only after pg_isready succeeds.
docker exec compass-restore-morning psql -X -U restore_admin -d postgres \
  --set=ON_ERROR_STOP=1 --file=/backup/roles.sql
docker exec compass-restore-morning pg_restore -U restore_admin \
  --exit-on-error --create --dbname=postgres /backup/database.pgdump
```

For Smoothers, use a separate container name and mount the `smoothers` folder.
Keep original applications and Discord senders disabled in a recovery drill.
Application credentials must be supplied separately before an intentional recovery;
no broker or alert-delivery validation was part of this database-only drill.

Method references: [PostgreSQL pg_dump](https://www.postgresql.org/docs/18/app-pgdump.html),
[PostgreSQL pg_restore](https://www.postgresql.org/docs/18/app-pgrestore.html), and
[Railway volume backups](https://docs.railway.com/integrations/api/manage-volumes).
