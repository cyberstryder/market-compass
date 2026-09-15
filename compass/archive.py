"""Lossless, bounded event export + full verification. Never deletes database rows.

Destinations must be separately copied to durable storage and independently
verified. This command does not claim a local export is a backup or cold tier.
"""
import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import tempfile
from sqlalchemy import select, func
from .store import Store, events

FORMAT = 'compass-events-v1'


def encode(value):
    return (json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False) + '\n').encode()


def verify(path):
    digest = hashlib.sha256()
    count, previous = 0, None
    with gzip.open(path, 'rb') as source:
        header = json.loads(source.readline())
        if header.get('format') != FORMAT: raise ValueError('Unknown archive format')
        while True:
            line = source.readline()
            if not line: raise ValueError('Missing archive manifest')
            value = json.loads(line)
            if value.get('type') == 'manifest':
                if (value['count'] != count or value['sha256'] != digest.hexdigest()
                        or value['last_id'] != (previous if previous is not None else header['after_id'])
                        or value['continuation_after_id'] != value['last_id']
                        or value['snapshot_high_water_id'] != header['through_id']):
                    raise ValueError('Archive content checksum/count mismatch')
                if source.read(1): raise ValueError('Unexpected trailing archive content')
                return {**value, 'format': FORMAT, 'after_id': header['after_id'],
                    'through_id': header['through_id'], 'verified': True}
            if set(value) != {'id','key','kind','source','symbol','ts','received','payload'}:
                raise ValueError('Invalid event schema')
            if not header['after_id'] < value['id'] <= header['through_id'] or (previous is not None and value['id'] <= previous):
                raise ValueError('Invalid archive ordering/bounds')
            digest.update(line); count += 1; previous = value['id']


def export(db, destination, after_id=0, max_rows=100000):
    if after_id < 0 or not 1 <= max_rows <= 1000000: raise ValueError('Invalid export bounds')
    destination = Path(destination)
    if destination.exists(): raise ValueError('Destination already exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.compass-export-', dir=destination.parent)
    os.close(fd)
    try:
        with db.tx() as c:
            if c.dialect.name == 'postgresql':
                from sqlalchemy import text
                c.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
                c.execute(text("SET LOCAL statement_timeout = '30s'"))
            through = c.execute(select(func.max(events.c.id))).scalar_one() or after_id
            rows = c.execute(select(events).where(events.c.id > after_id, events.c.id <= through)
                .order_by(events.c.id).limit(max_rows).execution_options(stream_results=True)).mappings()
            digest = hashlib.sha256(); count = 0; last = after_id
            with gzip.open(temporary, 'wb', compresslevel=6) as target:
                target.write(encode({'format': FORMAT, 'after_id': after_id, 'through_id': through}))
                for row in rows:
                    line = encode(dict(row)); digest.update(line); target.write(line)
                    count += 1; last = row['id']
                target.write(encode({'type': 'manifest', 'count': count, 'last_id': last,
                    'sha256': digest.hexdigest(), 'snapshot_high_water_id': through,
                    'continuation_after_id': last, 'may_have_more': count == max_rows}))
        result = verify(temporary)
        # Hard link publishes without overwriting a concurrent destination.
        os.link(temporary, destination)
        return {**result, 'path': str(destination), 'database_rows_deleted': 0,
                'durable_copy_verified': False, 'cross_chunk_completeness_verified': False, 'scope': 'events only; not a full database backup'}
    finally:
        Path(temporary).unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    p = commands.add_parser('export'); p.add_argument('--destination', required=True)
    p.add_argument('--after-id', type=int, default=0); p.add_argument('--max-rows', type=int, default=100000)
    p = commands.add_parser('verify'); p.add_argument('path')
    p = commands.add_parser('restore-isolated'); p.add_argument('path'); p.add_argument('--destination',required=True)
    args = parser.parse_args()
    if args.command == 'restore-isolated': result = restore_isolated(args.path,args.destination)
    elif args.command == 'verify': result = verify(args.path)
    else:
        db = Store(os.environ['DATABASE_URL'])
        try: result = export(db, args.destination, args.after_id, args.max_rows)
        finally: db.engine.dispose()
    print(json.dumps(result, sort_keys=True))



def restore_isolated(path, destination):
    """Restore event history into a new SQLite research file, never production."""
    destination=Path(destination)
    if destination.exists(): raise ValueError('Restore destination already exists')
    manifest=verify(path)
    destination.parent.mkdir(parents=True,exist_ok=True)
    fd,temporary=tempfile.mkstemp(prefix='.compass-restore-',dir=destination.parent)
    os.close(fd)
    restored=Store('sqlite:///'+temporary)
    try:
        restored.initialize()
        digest=hashlib.sha256(); count=0
        with restored.tx() as c, gzip.open(path,'rb') as source:
            header=json.loads(source.readline())
            if header != {'format':FORMAT,'after_id':manifest['after_id'],'through_id':manifest['through_id']}:
                raise ValueError('Archive changed during restore')
            for line in source:
                value=json.loads(line)
                if value.get('type')=='manifest':
                    if digest.hexdigest()!=manifest['sha256'] or count!=manifest['count'] or value['sha256']!=manifest['sha256'] or source.read(1):
                        raise ValueError('Archive changed during restore')
                    break
                digest.update(line);count+=1
                c.execute(events.insert().values(**value))
            else: raise ValueError('Missing manifest during restore')
            # Read every reconstructed event in order and compare canonical bytes.
            replay_digest=hashlib.sha256()
            for row in c.execute(select(events).order_by(events.c.id)).mappings():
                replay_digest.update(encode(dict(row)))
            if replay_digest.hexdigest()!=manifest['sha256']: raise ValueError('Restored content mismatch')
        restored.engine.dispose()
        os.link(temporary,destination)
        return dict(path=str(destination),rows=count,sha256=manifest['sha256'],restore_verified=True,
            durable_copy_verified=False,scope='Isolated event history only; not a full database or strategy-state restore')
    finally:
        restored.engine.dispose()
        Path(temporary).unlink(missing_ok=True)

if __name__ == '__main__': main()
