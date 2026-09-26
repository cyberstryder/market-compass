"""Verify stopped Atlas sources still match the preserved Compass archives."""
import hashlib
import json
import os
import time

import psycopg
import worker
import atlas_import


def main():
    archive = atlas_import.client('destination')
    source_objects = atlas_import.client('source')
    bucket = os.environ['ARCHIVE_BUCKET']
    prefix = os.environ['ATLAS_VERIFIED_DATABASE_PREFIX']
    artifact_prefix = os.environ['ATLAS_VERIFIED_ARTIFACT_PREFIX']
    def read(key):
        body = archive.get_object(Bucket=bucket, Key=key)['Body']
        try:
            return json.loads(body.read(16*1024*1024))
        finally:
            body.close()
    manifest = read(prefix+'/verified-manifest.json')
    if (manifest['source_project'] != os.environ['SOURCE_PROJECT_ID'] or
            not manifest['isolated_restore_verified'] or manifest['invalid_indexes']):
        raise RuntimeError('Archive identity or restore verification mismatch')
    url = os.environ['SOURCE_DATABASE_URL']
    with psycopg.connect(url, connect_timeout=10,
            options='-c default_transaction_read_only=on -c statement_timeout=7200000') as db:
        db.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        now = worker.inventory(db, 'final_source')
        others = [r[0] for r in db.execute("SELECT datname FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('postgres',current_database())")]
    if now != manifest['source_inventory'] or others:
        raise RuntimeError('Source database changed; final backup required')
    artifacts = read(artifact_prefix+'/manifest.json')
    source_bucket = os.environ['ATLAS_MNQ_BUCKET_NAME']
    listed = atlas_import.listing(source_objects, source_bucket)
    if set(listed) != set(artifacts['files']):
        raise RuntimeError('Source artifact keys changed')
    for key, info in listed.items():
        body = source_objects.get_object(Bucket=source_bucket, Key=key, IfMatch=info['etag'])['Body']
        digest = hashlib.sha256()
        try:
            for block in body.iter_chunks(chunk_size=1024*1024):
                digest.update(block)
        finally:
            body.close()
        if digest.hexdigest() != artifacts['files'][key]['sha256']:
            raise RuntimeError('Source artifact content changed')
    receipt = dict(checked_at=time.time(), source_project=manifest['source_project'],
        database_prefix=prefix, artifact_prefix=artifact_prefix,
        source_database_matches_verified_archive=True,
        source_artifacts_match_verified_archive=True,
        table_count=manifest['table_count'], row_count=manifest['row_count'],
        artifact_count=len(listed), source_rows_deleted=0)
    worker.put_json(archive, bucket, prefix+'/retirement-check.json', receipt)
    worker.emit('atlas_retirement_check_verified', **receipt)
    source_objects.close()
    archive.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        worker.emit('failed', error_type=type(error).__name__,
            detail=str(error) if type(error) is RuntimeError else 'Inspect bounded diagnostics')
        raise SystemExit(1) from None
