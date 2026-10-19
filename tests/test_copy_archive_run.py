"""Structural regression tests for run_copy_chunk's per-kind loop.

2026-10-19: the probe-first edit landed with 8-space indentation inside a
12-space loop body, silently dedenting the probe+walk OUT of the
`for kind in kinds:` loop. Python parsed it fine; the pump then processed
only the LAST kind (secondary_error) every run while every other kind's
weeks sat unexported — including quote's newest data week. These tests
pin the loop's per-kind behavior so a dedent can never pass silently
again. COPY itself is Postgres-only, so export_month is stubbed.
"""
import io
import time

import pytest

from compass import copy_archive as ca
from compass.store import Store

NOW = time.time()
DAY = 86400


class FakeS3:
    def __init__(self):
        self.objects = {}

    def put_object(self, Bucket, Key, Body, **kw):
        self.objects[Key] = Body.read()

    def get_object(self, Bucket, Key):
        return {'Body': io.BytesIO(self.objects[Key])}


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'run.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def test_run_processes_multiple_kinds_in_one_run(db, monkeypatch):
    exports = []

    def fake_export(db_, client, bucket, kind, ms, me):
        exports.append((kind, ca._week_str(ms)))
        with db_.tx() as c:
            db_.put(c, f'copy-archive:{kind}:{ca._week_str(ms)}',
                    {'rows': 1, 'bytes': 1, 'sha256': 'x' * 64,
                     'at': time.time()})
        return 1, 1

    monkeypatch.setattr(ca, 'export_month', fake_export)
    with db.tx() as c:
        # quote data 30 days ago (archivable: 14d retention)
        db.append(c, 'quote', 'test', 'SPYq', NOW - 30 * DAY, {'v': 1})
        # bar data 400 days ago (archivable: 365d retention)
        db.append(c, 'bar', 'test', 'SPYb', NOW - 400 * DAY, {'v': 1})
    res = ca.run_copy_chunk(db, FakeS3(), 'bkt', max_months=6)
    kinds_exported = {k for k, _p in exports}
    # The loop must reach BEYOND the first kind with work: quote's chunks
    # alone cannot consume the whole budget before bar gets exports.
    assert 'quote' in kinds_exported
    assert 'bar' in kinds_exported
    assert res['chunks_done'] == len(exports) == 6
    # The probe must have run for many kinds, not just one: empty tails
    # across the audit kinds alone are hundreds of periods.
    assert res.get('probed_done', 0) > 100


def test_probe_marks_empty_tails_for_every_kind(db, monkeypatch):
    def fake_export(db_, client, bucket, kind, ms, me):
        raise AssertionError('no data anywhere: export must not be called')

    monkeypatch.setattr(ca, 'export_month', fake_export)
    res = ca.run_copy_chunk(db, FakeS3(), 'bkt', max_months=6)
    assert res['chunks_done'] == 0
    # Every kind's periods get probe-marked in ONE run when no data exists.
    assert res.get('probed_done', 0) > 1000
    assert res['complete'] is True
