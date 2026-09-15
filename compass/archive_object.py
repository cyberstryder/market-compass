"""One bounded production event export, private object readback and isolated restore."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import uuid
from .archive import export,verify,restore_isolated
from .store import Store


def sha(path):
    digest=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()


def run(db,client,bucket,after_id=0,max_rows=10000,through_id=None):
    if not bucket or not 1<=max_rows<=100000:raise ValueError('Invalid archive scope')
    with tempfile.TemporaryDirectory(prefix='compass-archive-') as tmp:
        source=Path(tmp)/'source.gz'
        manifest=export(db,source,after_id,max_rows,through_id)
        checksum=sha(source)
        key='events/'+uuid.uuid4().hex+'/'+checksum+'.jsonl.gz'
        # A unique key per run prevents accidental overwrites of previous evidence.
        with source.open('rb') as body:
            client.put_object(Bucket=bucket,Key=key,Body=body,ContentType='application/gzip',
                Metadata={'sha256':checksum,'format':'compass-events-v1'})
        response=client.get_object(Bucket=bucket,Key=key)
        downloaded=Path(tmp)/'readback.gz'
        size=source.stat().st_size;written=0
        try:
            with downloaded.open('xb') as target:
                while True:
                    block=response['Body'].read(1024*1024)
                    if not block:break
                    written+=len(block)
                    if written>size:raise ValueError('Readback size mismatch')
                    target.write(block)
        finally:response['Body'].close()
        if written!=size or sha(downloaded)!=checksum:raise ValueError('Readback checksum mismatch')
        copied=verify(downloaded)
        if copied!=verify(source):raise ValueError('Readback manifest mismatch')
        restored=restore_isolated(downloaded,Path(tmp)/'restored.sqlite')
        receipt=dict(object_key=key,compressed_sha256=checksum,compressed_bytes=size,
            rows=manifest['count'],after_id=manifest['after_id'],last_id=manifest['last_id'],
            snapshot_high_water_id=manifest['snapshot_high_water_id'],range_through_id=through_id,
            continuation_after_id=manifest['continuation_after_id'],may_have_more=manifest['may_have_more'],
            content_sha256=manifest['sha256'],durable_copy_verified=True,
            isolated_restore_verified=restored['restore_verified'],database_rows_deleted=0,
            cross_chunk_completeness_verified=False,scope='Bounded events only; not a full database backup')
        receipt_key=key+'.receipt.json'
        encoded=json.dumps(receipt,sort_keys=True).encode()
        client.put_object(Bucket=bucket,Key=receipt_key,Body=encoded,ContentType='application/json')
        check=client.get_object(Bucket=bucket,Key=receipt_key)['Body']
        try:
            if check.read(len(encoded)+1)!=encoded:raise ValueError('Receipt readback mismatch')
        finally:check.close()
        return {**receipt,'receipt_key':receipt_key,'receipt_readback_verified':True}


def main():
    import boto3
    from botocore.config import Config
    required=['ARCHIVE_BUCKET','ARCHIVE_ENDPOINT','ARCHIVE_REGION','ARCHIVE_ACCESS_KEY_ID','ARCHIVE_SECRET_ACCESS_KEY','DATABASE_URL']
    if any(not os.environ.get(k) for k in required):raise ValueError('Archive configuration incomplete')
    if not os.environ['ARCHIVE_ENDPOINT'].startswith('https://'):raise ValueError('HTTPS archive endpoint required')
    client=boto3.client('s3',endpoint_url=os.environ['ARCHIVE_ENDPOINT'],region_name=os.environ['ARCHIVE_REGION'],
        aws_access_key_id=os.environ['ARCHIVE_ACCESS_KEY_ID'],aws_secret_access_key=os.environ['ARCHIVE_SECRET_ACCESS_KEY'],
        config=Config(connect_timeout=5,read_timeout=30,retries={'mode':'standard','max_attempts':2},
            s3={'addressing_style':os.environ.get('ARCHIVE_ADDRESSING_STYLE','virtual')}))
    db=Store(os.environ['DATABASE_URL'])
    try:
        if os.environ.get('ARCHIVE_MODE')=='scheduled':
            from .archive_schedule import scheduled
            result=scheduled(db,client,os.environ['ARCHIVE_BUCKET'])
            print('Archive schedule: '+json.dumps(result,sort_keys=True))
        else:
            print('Archive verification: '+json.dumps(run(db,client,os.environ['ARCHIVE_BUCKET'],
                int(os.environ.get('ARCHIVE_AFTER_ID','0')),int(os.environ.get('ARCHIVE_MAX_ROWS','10000'))),sort_keys=True))
    finally:db.engine.dispose();client.close()


if __name__=='__main__':
    try:main()
    except Exception as error:
        print('Archive verification failed: '+type(error).__name__)
        raise SystemExit(1) from None
