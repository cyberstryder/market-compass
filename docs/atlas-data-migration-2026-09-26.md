# Atlas data preserved in Compass — September 26, 2026

## Verified result

Atlas's connected application database and current research artifact bucket have
been copied into the independent Market Compass private archive bucket
`compass-archive` (bucket ID `d45a3cd4-b07d-457d-aa72-e4bc8680ef45`).

- Source project: `bfb02ef3-6447-4bdd-bc0f-1224c06127d0`, Staging.
- Source database service: `0a3e304e-a404-47bf-969c-6f1a96bea1bc`.
- Destination worker: `atlas-history-import`, service `3cc8cdae-975c-4a31-9780-5eca2dedb082`.
- Successful transfer deployment: `cd46fd73-0253-4d8d-89f2-26cbbda8ccfb`.
- Snapshot: 2026-09-26 13:49:56 America/Chicago.
- Restore verified: 2026-09-26 13:53:47 America/Chicago.
- Database size: 4,111,906,495 bytes; compressed dump: 196,457,137 bytes.
- Restored content: 9 tables, 1,391,236 rows; matching source/restore row
  multiset fingerprints; zero invalid indexes.
- Dump, password-free roles, and contents list were uploaded and fully
  downloaded; byte sizes and SHA-256 hashes matched.
- Snapshot manifest, verified manifest, and final receipt were saved and read back.
- Artifact bucket: 1 object, 77,764 bytes. Full checksum readback passed;
  source listing remained stable. Original object name and metadata are retained
  in the artifact manifest.
- Scope check deployment `baaf5a36-7b88-4d75-b59d-6eb18e465153` confirmed
  no other non-template application databases at snapshot or inspection, no user
  tables in the auxiliary `postgres` database, and only `plpgsql` extension.

## Recovery locations

Database prefix:
`database-backups/compass/20260926T184956Z-6cd9ebbd6182`

Artifact prefix:
`legacy-atlas/artifacts/20260926T184955Z-23caddef1c2d`

The database prefix uses the established backup worker's namespace. Its manifests
identify Atlas as the source; these are not Compass live observations.

Dump SHA-256:
`726961cbee1c6165d961b26d205aca341efd07ed479d9ce30f1a2265359b6413`

Use `ops/database_backup/worker.py` in resume mode with the database prefix,
expected hash, source project identity, and protected Compass archive credentials.
This restores only into a fresh disposable PostgreSQL instance; it never starts
Atlas collectors, alerts, or an application outbox.

Original source object keys map to numbered archived object keys in
`manifest.json` under the artifact prefix.

## Retirement completed — September 26, 2026

The two remaining collectors, atlas-calibration-spy and atlas-calibration-candidate,
were explicitly taken offline before the final comparison. Other non-database
workloads were already completed, crashed, or offline.

Final comparison deployment: `8fbf5b77-8f59-4011-bc28-fe0f7e0270b3`.
All 9 tables / 1,391,236 rows and the artifact matched the verified Compass
archives after the collectors stopped. The readback-verified final receipt is
`retirement-check.json` under the database prefix above.

Both Railway projects, `atlas-exposure-engine` and `Atlas AI`, were deleted
from the active workspace. Railway places them in its 48-hour recovery window
before permanent deletion of their services, environments, and stored data.
The dashboard's Deleted tab confirms both projects.

GitHub repositories `cyberstryder/Atlas`, `cyberstryder/atlas-ai-companion`,
and `cyberstryder/atlas-exposure-engine` are archived and read-only, not deleted.
This preserves full code and development history and stops scheduled GitHub
workflows.

Compass's engine, collector, dashboard, database, archive bucket, and original
Smoothers service were retained. The Compass applications still report successful
deployments. The temporary import worker has no cron, no public endpoint, and
restart policy NEVER; its source credentials have been cleared and its command
replaced with a completion notice.

The original copy is a historical archive, not a live import into Compass signal
or performance tables. Restoration remains available using the instructions above.

Validation: actual isolated restore and final source comparison passed. Nine
focused backup/import tests passed before the import. Helper code and evidence
are preserved in PR #146.

### Final comparison receipt

```json
{
  "artifact_count": 1,
  "artifact_prefix": "legacy-atlas/artifacts/20260926T184955Z-23caddef1c2d",
  "at": 1790449650.0520732,
  "checked_at": 1790449650.0214381,
  "database_prefix": "database-backups/compass/20260926T184956Z-6cd9ebbd6182",
  "row_count": 1391236,
  "source_artifacts_match_verified_archive": true,
  "source_database_matches_verified_archive": true,
  "source_project": "bfb02ef3-6447-4bdd-bc0f-1224c06127d0",
  "source_rows_deleted": 0,
  "stage": "atlas_retirement_check_verified",
  "table_count": 9
}
```

## Database receipt

```json
{
  "application_started": false,
  "at": 1790448828.086552,
  "database_bytes": 4111906495,
  "durable_copy_verified": true,
  "files": {
    "contents.txt": {
      "bytes": 11906,
      "full_readback_verified": true,
      "object_key": "database-backups/compass/20260926T184956Z-6cd9ebbd6182/contents.txt",
      "sha256": "38586941c24a043d6eee399728dac420ea850274990cba45ce87085e3773d25f"
    },
    "database.pgdump": {
      "bytes": 196457137,
      "full_readback_verified": true,
      "object_key": "database-backups/compass/20260926T184956Z-6cd9ebbd6182/database.pgdump",
      "sha256": "726961cbee1c6165d961b26d205aca341efd07ed479d9ce30f1a2265359b6413"
    },
    "roles.sql": {
      "bytes": 524,
      "full_readback_verified": true,
      "object_key": "database-backups/compass/20260926T184956Z-6cd9ebbd6182/roles.sql",
      "sha256": "8570ef097c05866fa69157fb42a917cad1067b7360d38bbaae057c2bd7d41ca2"
    }
  },
  "format": "compass-full-database-backup-v1",
  "invalid_indexes": 0,
  "isolated_restore_verified": true,
  "manifest_key": "database-backups/compass/20260926T184956Z-6cd9ebbd6182/verified-manifest.json",
  "object_prefix": "database-backups/compass/20260926T184956Z-6cd9ebbd6182",
  "receipt_readback_verified": true,
  "row_count": 1391236,
  "snapshot_at": 1790448596.3235512,
  "source_commit": "3d079b76d1a73f0ff12f0b3480ca720050fd8c17",
  "source_database_rows_deleted": 0,
  "source_project": "bfb02ef3-6447-4bdd-bc0f-1224c06127d0",
  "source_service": "0a3e304e-a404-47bf-969c-6f1a96bea1bc",
  "stage": "verified",
  "table_count": 9,
  "verified_at": 1790448827.482904
}
```
