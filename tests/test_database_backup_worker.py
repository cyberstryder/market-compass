"""Recovery isolation and integrity gates; no production/database connections."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


spec = importlib.util.spec_from_file_location(
    "database_backup_worker", Path(__file__).parents[1] / "ops/database_backup/worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


def test_restore_environment_excludes_production_and_provider_credentials(monkeypatch, tmp_path):
    for name in ("SOURCE_DATABASE_URL", "ARCHIVE_SECRET_ACCESS_KEY", "APCA_API_SECRET_KEY",
                 "DISCORD_WEBHOOK_URL", "PGPASSWORD", "PGHOST", "PGSERVICE"):
        monkeypatch.setenv(name, "production-secret")
    env = worker.local_environment(tmp_path, "isolated_admin")
    assert env["PGHOST"] == str(tmp_path)
    assert env["PGPORT"] == "55432"
    assert env["PGUSER"] == "isolated_admin"
    assert set(env) <= {"PATH", "LANG", "LC_ALL", "TZ", "PGHOST", "PGPORT", "PGUSER", "PGDATABASE"}
    assert "production-secret" not in env.values()


def test_resume_download_checksums_before_restore_and_never_connects_to_source(monkeypatch, tmp_path):
    prefix = "database-backups/compass/20260918T004425Z-172ba7a9a380"
    contents = {name: name.encode() for name in ("database.pgdump", "roles.sql", "contents.txt")}
    manifest = dict(object_prefix=prefix, durable_copy_verified=True, source_project="project",
                    database="railway", database_bytes=1,
                    files={name: dict(object_key=prefix+"/"+name, bytes=len(value),
                                     sha256=hashlib.sha256(value).hexdigest())
                           for name, value in contents.items()})
    monkeypatch.setenv("SOURCE_PROJECT_ID", "project")
    monkeypatch.setenv("EXPECTED_DUMP_SHA256", manifest["files"]["database.pgdump"]["sha256"])
    monkeypatch.delenv("SOURCE_DATABASE_URL", raising=False)
    monkeypatch.setattr(worker.psycopg, "connect", lambda *a, **k: pytest.fail("source connection"))
    monkeypatch.setattr(worker.shutil, "disk_usage", lambda _: SimpleNamespace(free=10**12))
    def restore(root, client, bucket, value):
        assert value == manifest
        assert all((root/"package"/name).read_bytes() == data for name, data in contents.items())
        return "restored"
    monkeypatch.setattr(worker, "restore", restore)
    class Objects:
        def get_object(self, **kwargs):
            return {"Body": io.BytesIO(json.dumps(manifest).encode())}
        def download_file(self, bucket, key, path, **kwargs):
            Path(path).write_bytes(contents[key.rsplit("/", 1)[1]])
    assert worker.resume(tmp_path, Objects(), "private-bucket", prefix) == "restored"


@pytest.mark.parametrize("bad", ["../backup", "database-backups/compass/other", ""])
def test_resume_rejects_unexpected_prefix_before_reading_objects(tmp_path, bad):
    with pytest.raises(RuntimeError, match="Unexpected restore prefix"):
        worker.resume(tmp_path, None, "private-bucket", bad)


def test_object_readback_rejects_corruption(tmp_path):
    path = tmp_path/"database.pgdump"
    original = b"complete backup"
    path.write_bytes(original)
    class Objects:
        def upload_file(self, *args, **kwargs):
            self.metadata = kwargs["ExtraArgs"]["Metadata"]
        def head_object(self, **kwargs):
            return dict(ContentLength=len(original), Metadata=self.metadata)
        def download_file(self, bucket, key, target, **kwargs):
            Path(target).write_bytes(b"corrupt backup")
    with pytest.raises(RuntimeError, match="Stored object checksum mismatch"):
        worker.copy_readback(Objects(), "private-bucket", "unique-key", path)


def test_postgres_binary_fallback_survives_runuser_path_reset(monkeypatch):
    monkeypatch.setattr(worker.shutil, "which", lambda _: None)
    monkeypatch.setattr(worker.Path, "is_file", lambda _: True)
    seen = []
    def run(args, **kwargs):
        seen.append(args)
        return SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
    monkeypatch.setattr(worker.subprocess, "run", run)
    worker.command(["initdb", "--version"])
    assert seen == [["/usr/lib/postgresql/18/bin/initdb", "--version"]]
