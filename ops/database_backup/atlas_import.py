"""Copy Atlas research objects and verify a complete DB backup in Compass storage.

Run beside worker.py in the dedicated, one-shot Compass import service. No live
Compass database connection or application process is used.
"""
import json
import os
from pathlib import Path
import secrets
import tempfile
import time

import boto3
from botocore.config import Config
import worker


def client(prefix):
    names = ({'endpoint':'ATLAS_MNQ_BUCKET_ENDPOINT', 'region':'ATLAS_MNQ_BUCKET_REGION',
              'access':'ATLAS_MNQ_BUCKET_ACCESS_KEY_ID', 'secret':'ATLAS_MNQ_BUCKET_SECRET_ACCESS_KEY'}
             if prefix == 'source' else
             {'endpoint':'ARCHIVE_ENDPOINT', 'region':'ARCHIVE_REGION',
              'access':'ARCHIVE_ACCESS_KEY_ID', 'secret':'ARCHIVE_SECRET_ACCESS_KEY'})
    endpoint = os.environ[names['endpoint']]
    if not endpoint.startswith('https://'):
        raise RuntimeError('HTTPS object endpoint required')
    return boto3.client('s3', endpoint_url=endpoint,
        region_name=os.environ[names['region']],
        aws_access_key_id=os.environ[names['access']],
        aws_secret_access_key=os.environ[names['secret']],
        config=Config(connect_timeout=10, read_timeout=60,
            retries={'max_attempts':3, 'mode':'standard'},
            s3={'addressing_style':'virtual'}))


def listing(source, bucket):
    result = {}
    for page in source.get_paginator('list_objects_v2').paginate(Bucket=bucket):
        for item in page.get('Contents', []):
            result[item['Key']] = {'bytes':item['Size'], 'etag':item['ETag'],
                                  'last_modified':item['LastModified'].isoformat()}
    return result


def copy_artifacts(source, destination, source_bucket, target_bucket, root):
    prefix = 'legacy-atlas/artifacts/' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + secrets.token_hex(6)
    before = listing(source, source_bucket)
    manifest = {'format':'atlas-artifact-import-v1', 'source_project':os.environ['SOURCE_PROJECT_ID'],
                'source_bucket':source_bucket, 'object_prefix':prefix, 'files':{},
                'source_objects_deleted':0, 'started_at':time.time()}
    for index, (key, info) in enumerate(sorted(before.items())):
        path = root / ('object-' + str(index))
        reply = source.get_object(Bucket=source_bucket, Key=key, IfMatch=info['etag'])
        try:
            with path.open('xb') as output:
                total = 0
                for block in reply['Body'].iter_chunks(chunk_size=1024*1024):
                    total += len(block)
                    if total > info['bytes']:
                        raise RuntimeError('Source artifact grew during copy')
                    output.write(block)
        finally:
            reply['Body'].close()
        if path.stat().st_size != info['bytes']:
            raise RuntimeError('Source artifact size mismatch')
        # Unique numbered destination keys avoid unsafe filenames and preserve
        # exact original names in the manifest, including empty directory keys.
        evidence = worker.copy_readback(destination, target_bucket, prefix+'/objects/'+str(index), path)
        manifest['files'][key] = {**info, **evidence,
            'source_metadata':reply.get('Metadata', {}),
            'content_type':reply.get('ContentType'), 'content_encoding':reply.get('ContentEncoding')}
        path.unlink()
    if before != listing(source, source_bucket):
        raise RuntimeError('Source artifact inventory changed during copy')
    manifest.update(verified_at=time.time(), object_count=len(before),
        byte_count=sum(x['bytes'] for x in before.values()), full_readback_verified=True,
        source_inventory_stable=True)
    worker.put_json(destination, target_bucket, prefix+'/manifest.json', manifest)
    worker.emit('atlas_artifacts_verified', object_prefix=prefix, object_count=len(before),
                byte_count=manifest['byte_count'], manifest_readback_verified=True)
    return manifest


def main():
    os.umask(0o077)
    source, destination = client('source'), client('destination')
    try:
        with tempfile.TemporaryDirectory(prefix='atlas-artifact-import-') as tmp:
            copy_artifacts(source, destination, os.environ['ATLAS_MNQ_BUCKET_NAME'],
                           os.environ['ARCHIVE_BUCKET'], Path(tmp))
    finally:
        source.close()
        destination.close()
    # The established worker exports a read-only snapshot, reads back every
    # uploaded byte, restores to a new isolated PostgreSQL, and compares tables.
    worker.main()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        worker.emit('failed', error_type=type(error).__name__,
            detail=str(error) if type(error) is RuntimeError else 'Inspect bounded diagnostics before retrying')
        raise SystemExit(1) from None
