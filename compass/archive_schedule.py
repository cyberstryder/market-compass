"""Resumable bounded archival with rolling source reconciliation. No eviction."""
import json
import hashlib
from pathlib import Path
import tempfile
import time
from sqlalchemy import select,func,text
from .archive import export
from .archive_object import run
from .store import events

KEY='archive:schedule-v1'
WIDTH=100000
LOCK=8675309041


def cycle(db,client,bucket,width=WIDTH,chunks=4,audits=1,budget=180):
    if not 1<=width<=100000 or not 1<=chunks<=4 or not 1<=audits<=4:raise ValueError('Invalid schedule bounds')
    began=time.monotonic()
    destination=hashlib.sha256(bucket.encode()).hexdigest()
    with db.tx() as c:
        progress=db.get(c,KEY,dict(frontier=0,audit_cursor=0,audit_target=0,verified_ranges=0,repairs=0,audit_passes=0,width=width,destination=destination))
        if progress['width']!=width:raise ValueError('Archive partition width changed')
        if progress.get('destination')!=destination:raise ValueError('Archive destination changed')
        high=c.execute(select(func.max(events.c.id))).scalar_one() or 0
        if high<progress['frontier']:raise ValueError('Source high water regressed')
    progress.update(source_high_water=high,started_at=time.time(),cleanup_enabled=False,
        cross_chunk_completeness_verified=False,
        basis='Rolling range snapshots; late commits are repaired by recurring audits. Not a single global snapshot or eviction approval.')
    if not progress['audit_target']:progress['audit_target']=progress['frontier']
    audited=0
    for _ in range(audits):
        if progress['audit_cursor']>=progress['audit_target'] or time.monotonic()-began>=budget:break
        after=progress['audit_cursor'];upper=after+width
        with db.tx() as c:record=db.get(c,KEY+':range:'+str(after))
        if not record or record['after']!=after or record['upper']!=upper:raise ValueError('Missing archive range receipt')
        # Audit remote availability as well as current source content. Re-upload
        # and restore a replacement on missing/corrupt objects or late commits.
        with tempfile.TemporaryDirectory(prefix='compass-audit-') as tmp:
            current=export(db,Path(tmp)/'source.gz',after,width,upper)
        old=record['receipt']
        mismatch=current['sha256']!=old['content_sha256'] or current['count']!=old['rows']
        try:
            body=client.get_object(Bucket=bucket,Key=old['receipt_key'])['Body']
            try: remote=json.loads(body.read(65537))
            finally:body.close()
            if any(remote.get(k)!=v for k,v in old.items() if k not in ('receipt_key','receipt_readback_verified')):mismatch=True
            # Stream the original object and check compressed bytes, not just HEAD.
            from hashlib import sha256
            digest=sha256();size=0
            body=client.get_object(Bucket=bucket,Key=old['object_key'])['Body']
            try:
                while True:
                    block=body.read(1024*1024)
                    if not block:break
                    size+=len(block)
                    if size>old['compressed_bytes']:mismatch=True;break
                    digest.update(block)
            finally:body.close()
            mismatch=mismatch or size!=old['compressed_bytes'] or digest.hexdigest()!=old['compressed_sha256']
        except Exception:
            # A failed read may be transient. Replacement must pass the full
            # upload/readback/restore gate before the audit can advance.
            mismatch=True
        if mismatch:
            receipt=run(db,client,bucket,after,width,upper)
            record.update(receipt=receipt,replaces_receipt=old['receipt_key'])
            progress['repairs']+=1
        record['checked_at']=time.time()
        with db.tx() as c:
            db.put(c,KEY+':range:'+str(after),record)
            progress.update(audit_cursor=upper,updated_at=time.time())
            db.put(c,KEY,progress)
        audited+=1
    if progress['audit_target'] and progress['audit_cursor']>=progress['audit_target']:
        progress.update(last_audit_pass_at=time.time(),last_audit_pass_through=progress['audit_target'],
            audit_passes=progress['audit_passes']+1,audit_cursor=0,audit_target=0)
    completed=0
    for _ in range(chunks):
        if progress['frontier']>=high or time.monotonic()-began>=budget:break
        after=progress['frontier'];upper=min(after+width,high)
        receipt=run(db,client,bucket,after,width,upper)
        if upper-after<width:
            # Preserve fresh tail data without moving fixed audit boundaries.
            # Re-export this tail next run, including any late commits.
            progress.update(tail_receipt=receipt,tail_checked_at=time.time())
            with db.tx() as c:db.put(c,KEY,progress)
            completed+=1
            break
        with db.tx() as c:
            db.put(c,KEY+':range:'+str(after),dict(after=after,upper=upper,receipt=receipt,checked_at=time.time()))
            progress.update(frontier=upper,verified_ranges=progress['verified_ranges']+1,updated_at=time.time())
            db.put(c,KEY,progress)
        completed+=1
    progress.update(updated_at=time.time(),chunks_this_run=completed,audits_this_run=audited,
        pending_id_span=max(0,high-progress['frontier']),tail_is_partial=0<high-progress['frontier']<width)
    with db.tx() as c:db.put(c,KEY,progress)
    return progress


def scheduled(db,client,bucket):
    # Session advisory lock survives short checkpoint transactions and prevents
    # concurrent schedulers from overwriting each other's progress.
    with db.engine.connect() as lock:
        postgres=lock.dialect.name=='postgresql'
        if postgres and not lock.execute(text('SELECT pg_try_advisory_lock(:key)'),{'key':LOCK}).scalar_one():
            return {'status':'already_running','cleanup_enabled':False}
        lock.commit()
        try:return cycle(db,client,bucket)
        finally:
            if postgres:
                lock.execute(text('SELECT pg_advisory_unlock(:key)'),{'key':LOCK});lock.commit()
