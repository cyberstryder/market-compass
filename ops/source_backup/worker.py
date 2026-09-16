"""One source snapshot, isolated PG18 restore, authenticated encrypted export.

Never imports a trading application. Production connections are read-only.
Only a fresh local Unix-socket PostgreSQL server accepts restore commands.
The encrypted export shuts down after an authenticated acknowledgement or 2h.
"""
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import psycopg
from psycopg import sql
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes


def sha(path):
    with open(path, 'rb') as f:
        return hashlib.file_digest(f, 'sha256').hexdigest()


def command(args, env=None, output=None):
    result = subprocess.run(args, env=env, stdout=subprocess.PIPE if output is None else output,
                            stderr=subprocess.PIPE, timeout=600, check=False)
    if result.returncode:
        # Connection strings, passwords, and data must never enter runtime logs.
        raise RuntimeError('Command failed: ' + args[0] + ' (exit ' + str(result.returncode) + ')')
    if result.stderr and args[0] in ('pg_dump', 'pg_restore'):
        raise RuntimeError('Unexpected warning from ' + args[0] + '; inspect privately before accepting')
    return result.stdout


def fingerprint(conn, query):
    """Order-independent SHA256 multiset fingerprint, including duplicates."""
    count = total = xor = 0
    with conn.cursor(name='fingerprint_' + secrets.token_hex(5)) as cursor:
        cursor.execute(query)
        for row in cursor:
            value = int.from_bytes(hashlib.sha256(row[0].encode()).digest(), 'big')
            total = (total + value) % (1 << 256)
            xor ^= value
            count += 1
    return dict(rows=count, sha256_sum=f'{total:064x}', sha256_xor=f'{xor:064x}')


def inventory(conn):
    tables = conn.execute("""SELECT n.nspname,c.relname,c.relkind FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE c.relkind IN ('r','p','m') AND n.nspname NOT LIKE 'pg_%'
        AND n.nspname <> 'information_schema' ORDER BY 1,2""").fetchall()
    result = {}
    for schema, name, kind in tables:
        query = sql.SQL('SELECT to_jsonb(t)::text FROM {} {} t').format(
            sql.SQL('ONLY') if kind != 'm' else sql.SQL(''), sql.Identifier(schema, name))
        result[schema + '.' + name] = dict(kind=kind, **fingerprint(conn, query))
    result['pg_catalog.pg_largeobject'] = fingerprint(conn,
        'SELECT jsonb_build_array(loid,pageno,encode(data,\'hex\'))::text FROM pg_largeobject')
    return result


def encrypt(source, destination, public_pem):
    key, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    public = serialization.load_pem_public_key(public_pem.encode())
    wrapped = public.encrypt(key, padding.OAEP(mgf=padding.MGF1(hashes.SHA256()),
                                             algorithm=hashes.SHA256(), label=None))
    header = json.dumps(dict(format='compass-backup-envelope-v1',
        key=base64.b64encode(wrapped).decode(), nonce=base64.b64encode(nonce).decode(),
        plaintext_sha256=sha(source)), sort_keys=True).encode()
    encryptor = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
    encryptor.authenticate_additional_data(header)
    with open(source, 'rb') as src, open(destination, 'xb') as dst:
        dst.write(struct.pack('>I', len(header))); dst.write(header)
        for block in iter(lambda: src.read(1024 * 1024), b''):
            dst.write(encryptor.update(block))
        dst.write(encryptor.finalize()); dst.write(encryptor.tag)


def backup(root):
    source_url = os.environ['SOURCE_DATABASE_URL']
    source_env = {**os.environ, 'PGDATABASE': source_url,
        'PGOPTIONS': '-c default_transaction_read_only=on -c lock_timeout=3000 -c statement_timeout=600000',
        'PGCONNECT_TIMEOUT': '10', 'PGAPPNAME': 'compass_source_backup_readonly'}
    for key, value in psycopg.conninfo.conninfo_to_dict(source_url).items():
        envkey = 'PGDATABASE' if key == 'dbname' else 'PG' + key.upper()
        source_env[envkey] = value
    dest = root / 'package'; dest.mkdir()
    manifest = dict(format='compass-source-database-backup-v1',
        source=os.environ['BACKUP_SOURCE_LABEL'], started_at=time.time(),
        source_project=os.environ['SOURCE_PROJECT_ID'], source_service=os.environ['SOURCE_SERVICE_ID'],
        source_read_only=True, credentials_included=False, application_started=False,
        fingerprint_method='count + SHA256 row multiset sum modulo 2^256 + XOR',
        scope='Complete connected application database; role definitions without passwords; not a physical cluster/PITR backup')
    with psycopg.connect(source_url, connect_timeout=10, options=source_env['PGOPTIONS']) as source:
        source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        snapshot = source.execute('SELECT pg_export_snapshot()').fetchone()[0]
        dbname, version, size = source.execute(
            'SELECT current_database(),version(),pg_database_size(current_database())').fetchone()
        if dbname in ('postgres', 'template0', 'template1'):
            raise RuntimeError('Unexpected source database name; refusing ambiguous restore target')
        manifest.update(database=dbname, postgres_version=version, database_bytes=size,
                        snapshot_at=time.time(), exported_snapshot=snapshot)
        manifest['other_non_template_databases'] = [r[0] for r in source.execute(
            "SELECT datname FROM pg_database WHERE NOT datistemplate AND datname NOT IN ('postgres',current_database())")]
        manifest['source_inventory'] = inventory(source)
        command(['pg_dump', '--format=custom', '--snapshot=' + snapshot, '--lock-wait-timeout=3000',
                 '--file=' + str(dest / 'database.pgdump')], env=source_env)
        command(['pg_dumpall', '--roles-only', '--no-role-passwords', '--file=' + str(dest / 'roles.sql')], env=source_env)
        manifest['roles'] = [r[0] for r in source.execute("SELECT rolname FROM pg_roles WHERE rolname NOT LIKE 'pg_%' ORDER BY 1")]
    manifest['dump_sha256'] = sha(dest / 'database.pgdump')
    manifest['dump_bytes'] = (dest / 'database.pgdump').stat().st_size
    (dest / 'contents.txt').write_bytes(command(['pg_restore', '--list', str(dest / 'database.pgdump')]))
    data, socket = root / 'isolated_pgdata', root / 'isolated_socket'; socket.mkdir()
    admin = 'restore_admin_' + secrets.token_hex(6)
    command(['initdb', '-D', str(data), '--username=' + admin, '--auth-local=trust', '--no-instructions'])
    local_env = {k:v for k,v in os.environ.items() if not k.startswith('PG') and k != 'SOURCE_DATABASE_URL'}
    local_env.update(PGHOST=str(socket), PGPORT='55432', PGUSER=admin, PGDATABASE='postgres')
    # No TCP listener, no production mount, no source connection URL in restore env.
    command(['pg_ctl', '-D', str(data), '-l', str(root / 'isolated-postgres.log'),
             '-o', '-k ' + str(socket) + " -c listen_addresses='' -p 55432", '-w', 'start'])
    try:
        command(['psql', '-X', '--set=ON_ERROR_STOP=1', '--file=' + str(dest / 'roles.sql')], env=local_env)
        command(['pg_restore', '--exit-on-error', '--create', '--dbname=postgres',
                 str(dest / 'database.pgdump')], env=local_env)
        with psycopg.connect(host=str(socket), port=55432, user=admin, dbname=dbname) as restored:
            restored.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
            manifest['restored_inventory'] = inventory(restored)
            manifest['invalid_indexes'] = restored.execute(
                'SELECT count(*) FROM pg_index WHERE NOT indisvalid OR NOT indisready').fetchone()[0]
            manifest['restore_target'] = 'new disposable PostgreSQL 18 cluster; Unix socket only'
        if manifest['source_inventory'] != manifest['restored_inventory'] or manifest['invalid_indexes']:
            raise RuntimeError('Isolated restore content verification failed')
        manifest.update(isolated_restore_verified=True, verified_at=time.time(),
                        table_count=len(manifest['source_inventory'])-1,
                        row_count=sum(v['rows'] for v in manifest['source_inventory'].values()))
    finally:
        command(['pg_ctl', '-D', str(data), '-m', 'fast', '-w', 'stop'])
    (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2, sort_keys=True))
    archive = root / 'source-backup.tar'
    with tarfile.open(archive, 'w') as output:
        for path in sorted(dest.iterdir()): output.add(path, arcname=path.name)
    encrypted = root / 'source-backup.enc'
    encrypt(archive, encrypted, os.environ['BACKUP_PUBLIC_KEY'])
    receipt = {k:manifest[k] for k in ('source','snapshot_at','verified_at','database_bytes','dump_bytes',
        'dump_sha256','table_count','row_count','isolated_restore_verified','invalid_indexes')}
    receipt.update(encrypted_sha256=sha(encrypted), encrypted_bytes=encrypted.stat().st_size)
    # Keep only the encrypted export while awaiting collection; no database access needed.
    shutil.rmtree(dest); shutil.rmtree(data); archive.unlink()
    print('SOURCE_BACKUP_VERIFIED ' + json.dumps(receipt, sort_keys=True), flush=True)
    return encrypted, receipt


def main():
    token = os.environ.get('BACKUP_DOWNLOAD_TOKEN', '')
    if len(token) < 40 or not os.environ.get('BACKUP_PUBLIC_KEY'):
        raise RuntimeError('Missing backup export authorization or encryption key')
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='compass-source-backup-') as tmp:
        encrypted, receipt = backup(Path(tmp))
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path == '/health':
                    self.send_response(200); self.end_headers(); self.wfile.write(b'ready'); return
                if not hmac.compare_digest(self.headers.get('Authorization', ''), 'Bearer ' + token):
                    self.send_response(404); self.end_headers(); return
                if self.path == '/receipt':
                    body = json.dumps(receipt).encode()
                    self.send_response(200); self.send_header('Content-Type','application/json')
                    self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)
                elif self.path == '/backup':
                    self.send_response(200); self.send_header('Content-Type','application/octet-stream')
                    self.send_header('Content-Length',str(encrypted.stat().st_size)); self.end_headers()
                    with encrypted.open('rb') as f: shutil.copyfileobj(f, self.wfile)
                else:
                    self.send_response(404); self.end_headers()
            def do_POST(self):
                if self.path != '/acknowledge' or not hmac.compare_digest(
                        self.headers.get('Authorization',''), 'Bearer ' + token):
                    self.send_response(404); self.end_headers(); return
                self.send_response(200); self.end_headers(); self.wfile.write(b'closing encrypted export')
                threading.Thread(target=server.shutdown, daemon=True).start()
        server = ThreadingHTTPServer(('0.0.0.0', int(os.environ.get('PORT','8080'))), Handler)
        timer = threading.Timer(7200, server.shutdown); timer.daemon=True; timer.start()
        try: server.serve_forever()
        finally: timer.cancel(); server.server_close()


if __name__ == '__main__':
    try: main()
    except Exception as error:
        print('SOURCE_BACKUP_FAILED ' + type(error).__name__ +
              (': ' + str(error) if type(error) is RuntimeError else ''), flush=True)
        raise SystemExit(1) from None
