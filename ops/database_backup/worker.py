"""Full read-only application snapshot, private object readback and isolated restore.

No application imports, public endpoint, cron, or production restore connection.
The object copy is a point-in-time logical backup, not continuous PITR.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import shutil
import subprocess
import tempfile
import time

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
import psycopg
from psycopg import sql


def emit(stage, **values):
    print('DATABASE_BACKUP ' + json.dumps(dict(stage=stage, at=time.time(), **values),
                                        sort_keys=True), flush=True)


def sha(path):
    with path.open('rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def command(args, env=None, output=None, timeout=7200):
    # stdout/stderr can contain data or credentials; retain neither in logs.
    binary = args[0]
    if binary in {'pg_dump', 'pg_dumpall', 'pg_restore', 'initdb', 'pg_ctl', 'psql'}:
        resolved = shutil.which(binary) or '/usr/lib/postgresql/18/bin/' + binary
        if not Path(resolved).is_file():
            raise RuntimeError('Missing PostgreSQL executable: ' + binary)
        args = [resolved, *args[1:]]
    result = subprocess.run(args, env=env, stdout=subprocess.PIPE if output is None else output,
                            stderr=subprocess.PIPE, timeout=timeout, check=False)
    if result.returncode:
        raise RuntimeError('Command failed: ' + binary + ' exit ' + str(result.returncode))
    if result.stderr and binary in ('pg_dump', 'pg_restore', 'pg_dumpall'):
        raise RuntimeError('Unexpected warning from ' + binary)
    return result.stdout


def fingerprint(conn, query, label):
    count = total = xor = 0
    with conn.cursor(name='fingerprint_' + secrets.token_hex(5)) as cursor:
        cursor.itersize = 10000
        cursor.execute(query)
        for row in cursor:
            value = int.from_bytes(hashlib.sha256(row[0].encode()).digest(), 'big')
            total = (total + value) % (1 << 256)
            xor ^= value
            count += 1
            if count % 1000000 == 0:
                emit('fingerprint_progress', table=label, rows=count)
    return dict(rows=count, sha256_sum=f'{total:064x}', sha256_xor=f'{xor:064x}')


def inventory(conn, side):
    tables = conn.execute("""SELECT n.nspname,c.relname,c.relkind FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','m') AND n.nspname NOT LIKE 'pg_%'
        AND n.nspname <> 'information_schema' ORDER BY 1,2""").fetchall()
    result = {}
    for schema, name, kind in tables:
        key = schema + '.' + name
        query = sql.SQL('SELECT to_jsonb(t)::text FROM {} {} t').format(
            sql.SQL('ONLY') if kind != 'm' else sql.SQL(''), sql.Identifier(schema, name))
        emit('fingerprint_table', side=side, table=key)
        result[key] = dict(kind=kind, **fingerprint(conn, query, key))
        emit('fingerprint_complete', side=side, table=key, rows=result[key]['rows'])
    result['pg_catalog.pg_largeobject'] = fingerprint(conn,
        "SELECT jsonb_build_array(loid,pageno,encode(data,'hex'))::text FROM pg_largeobject",
        'pg_catalog.pg_largeobject')
    return result


def source_environment(url):
    env = {k:v for k,v in os.environ.items() if not k.startswith('PG')}
    env.update(PGOPTIONS='-c default_transaction_read_only=on -c lock_timeout=3000 -c statement_timeout=7200000',
               PGCONNECT_TIMEOUT='10', PGAPPNAME='compass_full_backup_readonly')
    for key, value in psycopg.conninfo.conninfo_to_dict(url).items():
        env['PGDATABASE' if key == 'dbname' else 'PG' + key.upper()] = value
    return env


def local_environment(socket, admin):
    # Whitelist process basics. No source URL, provider or alert credentials.
    env = {k:os.environ[k] for k in ('PATH','LANG','LC_ALL','TZ') if k in os.environ}
    env.update(PGHOST=str(socket), PGPORT='55432', PGUSER=admin, PGDATABASE='postgres')
    return env


def put_json(client, bucket, key, value):
    body = json.dumps(value, sort_keys=True, indent=2).encode()
    client.put_object(Bucket=bucket, Key=key, Body=body, ContentType='application/json')
    reply = client.get_object(Bucket=bucket, Key=key)['Body']
    try:
        if reply.read(len(body)+1) != body:
            raise RuntimeError('Manifest readback mismatch')
    finally:
        reply.close()


def copy_readback(client, bucket, key, path):
    checksum, size = sha(path), path.stat().st_size
    transfer = TransferConfig(multipart_threshold=64*1024*1024,
        multipart_chunksize=64*1024*1024, max_concurrency=2)
    emit('object_upload', object_key=key, bytes=size)
    client.upload_file(str(path), bucket, key, ExtraArgs={'Metadata':{'sha256':checksum}},
                       Config=transfer)
    head = client.head_object(Bucket=bucket, Key=key)
    if head['ContentLength'] != size or head.get('Metadata',{}).get('sha256') != checksum:
        raise RuntimeError('Stored object metadata mismatch')
    # Replace the local copy with the bytes actually retrieved from the object store.
    # The unique remote backup is retained if any later stage fails.
    path.unlink()
    emit('object_readback', object_key=key, bytes=size)
    client.download_file(bucket, key, str(path), Config=transfer)
    if path.stat().st_size != size or sha(path) != checksum:
        raise RuntimeError('Stored object checksum mismatch')
    return dict(object_key=key, sha256=checksum, bytes=size, full_readback_verified=True)


def run(root, client, bucket):
    url = os.environ['SOURCE_DATABASE_URL']
    source_env = source_environment(url)
    prefix = 'database-backups/compass/' + time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + secrets.token_hex(6)
    manifest = dict(format='compass-full-database-backup-v1', started_at=time.time(),
        source_project=os.environ['SOURCE_PROJECT_ID'], source_service=os.environ['SOURCE_SERVICE_ID'],
        source_read_only=True, credentials_included=False, application_started=False,
        source_commit=os.environ['SOURCE_COMMIT'], object_prefix=prefix,
        fingerprint_method='count + SHA256 row multiset sum modulo 2^256 + XOR',
        scope='Complete connected application database and password-free role definitions; not physical cluster/PITR')
    package = root / 'package'; package.mkdir()
    with psycopg.connect(url, connect_timeout=10, options=source_env['PGOPTIONS']) as source:
        source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        if source.execute('SHOW transaction_read_only').fetchone()[0] != 'on':
            raise RuntimeError('Source connection is not read-only')
        dbname, version, size = source.execute(
            'SELECT current_database(),version(),pg_database_size(current_database())').fetchone()
        if dbname != os.environ.get('EXPECTED_DATABASE', 'railway'):
            raise RuntimeError('Unexpected source database')
        if source.execute('SELECT count(*) FROM pg_subscription').fetchone()[0]:
            raise RuntimeError('Logical subscriptions require an explicit isolated-restore review')
        if source.execute('SELECT count(*) FROM pg_foreign_server').fetchone()[0]:
            raise RuntimeError('Foreign servers require an explicit isolated-restore review')
        free = shutil.disk_usage(root).free
        emit('preflight', database_bytes=size, worker_free_bytes=free,
             source_read_only=True, postgres_version=version)
        if free < 2.5 * size + 2*1024**3:
            raise RuntimeError('Insufficient isolated worker space for dump plus full restore')
        snapshot = source.execute('SELECT pg_export_snapshot()').fetchone()[0]
        manifest.update(database=dbname, postgres_version=version, database_bytes=size,
            snapshot_at=time.time(), exported_snapshot=snapshot,
            other_non_template_databases=[r[0] for r in source.execute(
                "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('postgres',current_database())")],
            extensions=[r[0] for r in source.execute('SELECT extname FROM pg_extension ORDER BY 1')])
        if set(manifest['extensions']) - {'plpgsql','pg_stat_statements','pg_trgm','btree_gin','btree_gist','pgcrypto','uuid-ossp'}:
            raise RuntimeError('Unreviewed database extension')
        emit('dump_started', snapshot_at=manifest['snapshot_at'])
        command(['pg_dump','--format=custom','--compress=zstd:1','--snapshot='+snapshot,
                 '--lock-wait-timeout=3000','--file='+str(package/'database.pgdump')], env=source_env)
        command(['pg_dumpall','--roles-only','--no-role-passwords','--file='+str(package/'roles.sql')], env=source_env)
        emit('dump_completed', dump_bytes=(package/'database.pgdump').stat().st_size)
        manifest['source_inventory'] = inventory(source, 'source')
    (package/'contents.txt').write_bytes(command(['pg_restore','--list',str(package/'database.pgdump')]))
    manifest['files'] = {}
    for filename in ('database.pgdump','roles.sql','contents.txt'):
        manifest['files'][filename] = copy_readback(client,bucket,prefix+'/'+filename,package/filename)
    manifest['durable_copy_verified'] = True
    put_json(client,bucket,prefix+'/snapshot-manifest.json',manifest)
    emit('backup_durable', object_prefix=prefix, dump_sha256=manifest['files']['database.pgdump']['sha256'])
    return restore(root, client, bucket, manifest)


def restore(root, client, bucket, manifest):
    package = root/'package'
    prefix = manifest['object_prefix']
    dbname = manifest['database']
    data, socket = root/'isolated_pgdata', root/'socket'; socket.mkdir()
    admin = 'restore_admin_'+secrets.token_hex(6)
    local_env = local_environment(socket, admin)
    command(['initdb','-D',str(data),'--username='+admin,'--auth-local=trust','--no-instructions'],env=local_env)
    command(['pg_ctl','-D',str(data),'-l',str(root/'isolated-postgres.log'),'-o',
        '-k '+str(socket)+" -c listen_addresses='' -p 55432 -c max_wal_size=4GB",'-w','start'],env=local_env)
    try:
        emit('restore_started', target='new local Unix-socket PostgreSQL; no application process')
        command(['psql','-X','--set=ON_ERROR_STOP=1','--file='+str(package/'roles.sql')],env=local_env)
        command(['pg_restore','--exit-on-error','--create','--dbname=postgres',
                 str(package/'database.pgdump')],env=local_env)
        with psycopg.connect(host=str(socket),port=55432,user=admin,dbname=dbname) as restored:
            restored.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            manifest['restored_inventory'] = inventory(restored, 'restored')
            manifest['invalid_indexes'] = restored.execute(
                'SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready').fetchone()[0]
            manifest['restored_tables'] = len(manifest['restored_inventory'])-1
        if manifest['source_inventory'] != manifest['restored_inventory'] or manifest['invalid_indexes']:
            raise RuntimeError('Isolated restore verification mismatch')
        manifest.update(isolated_restore_verified=True,verified_at=time.time(),
            table_count=len(manifest['source_inventory'])-1,
            row_count=sum(v['rows'] for v in manifest['source_inventory'].values()),
            restore_target='new disposable PostgreSQL 18; Unix socket only',
            source_database_rows_deleted=0)
    finally:
        command(['pg_ctl','-D',str(data),'-m','fast','-w','stop'],env=local_env,timeout=120)
    put_json(client,bucket,prefix+'/verified-manifest.json',manifest)
    receipt = {k:manifest[k] for k in ('format','snapshot_at','verified_at','source_project','source_service',
        'source_commit','database_bytes','table_count','row_count','isolated_restore_verified',
        'invalid_indexes','object_prefix','durable_copy_verified','files')}
    receipt.update(manifest_key=prefix+'/verified-manifest.json',receipt_readback_verified=True,
        source_database_rows_deleted=0,application_started=False)
    put_json(client,bucket,prefix+'/receipt.json',receipt)
    emit('verified',**receipt)
    return receipt


def resume(root, client, bucket, prefix):
    if not re.fullmatch(r'database-backups/compass/[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}', prefix):
        raise RuntimeError('Unexpected restore prefix')
    reply = client.get_object(Bucket=bucket, Key=prefix+'/snapshot-manifest.json')['Body']
    try:
        manifest = json.loads(reply.read(4*1024*1024))
    finally:
        reply.close()
    if (manifest['object_prefix'] != prefix or not manifest['durable_copy_verified']
            or manifest['source_project'] != os.environ['SOURCE_PROJECT_ID']
            or manifest['database'] != os.environ.get('EXPECTED_DATABASE', 'railway')
            or manifest['files']['database.pgdump']['sha256'] != os.environ['EXPECTED_DUMP_SHA256']):
        raise RuntimeError('Resume manifest identity mismatch')
    if shutil.disk_usage(root).free < 2.5 * manifest['database_bytes'] + 2*1024**3:
        raise RuntimeError('Insufficient isolated restore space')
    package = root/'package'; package.mkdir()
    transfer = TransferConfig(multipart_chunksize=64*1024*1024, max_concurrency=2)
    for name in ('database.pgdump','roles.sql','contents.txt'):
        evidence = manifest['files'][name]
        if evidence['object_key'] != prefix+'/'+name:
            raise RuntimeError('Unexpected backup object key')
        path = package/name
        emit('resume_readback', object_key=evidence['object_key'], bytes=evidence['bytes'])
        client.download_file(bucket, evidence['object_key'], str(path), Config=transfer)
        if path.stat().st_size != evidence['bytes'] or sha(path) != evidence['sha256']:
            raise RuntimeError('Resume object checksum mismatch')
    emit('resume_verified', object_prefix=prefix, production_database_connected=False)
    return restore(root, client, bucket, manifest)


def main():
    os.umask(0o077)
    client = boto3.client('s3',endpoint_url=os.environ['ARCHIVE_ENDPOINT'],
        region_name=os.environ['ARCHIVE_REGION'],aws_access_key_id=os.environ['ARCHIVE_ACCESS_KEY_ID'],
        aws_secret_access_key=os.environ['ARCHIVE_SECRET_ACCESS_KEY'],
        config=Config(s3={'addressing_style':os.environ.get('ARCHIVE_ADDRESSING_STYLE','virtual')},
                      retries={'max_attempts':3,'mode':'standard'}))
    try:
        with tempfile.TemporaryDirectory(prefix='compass-full-backup-') as tmp:
            if os.environ.get('RESTORE_PREFIX'):
                resume(Path(tmp),client,os.environ['ARCHIVE_BUCKET'],os.environ['RESTORE_PREFIX'])
            else:
                run(Path(tmp),client,os.environ['ARCHIVE_BUCKET'])
    finally:
        client.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # SQL/SDK exceptions can embed credentials, statements, or payloads.
        emit('failed',error_type=type(error).__name__,
             detail=str(error) if type(error) is RuntimeError else 'Inspect bounded diagnostics before retrying')
        raise SystemExit(1) from None
