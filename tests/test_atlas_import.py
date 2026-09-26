import importlib.util
import io
from pathlib import Path
import sys
from datetime import datetime, timezone

import pytest


root = Path(__file__).parents[1] / 'ops/database_backup'
spec = importlib.util.spec_from_file_location('worker', root/'worker.py')
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
sys.modules['worker'] = worker
spec = importlib.util.spec_from_file_location('atlas_import', root/'atlas_import.py')
importer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(importer)


class Source:
    def __init__(self, changed=False):
        self.calls = 0
        self.changed = changed
    def get_paginator(self, name):
        assert name == 'list_objects_v2'
        return self
    def paginate(self, **kwargs):
        self.calls += 1
        return [{'Contents':[{'Key':'../research.json', 'Size':3,
            'ETag':'changed' if self.changed and self.calls > 1 else 'stable',
            'LastModified':datetime(2026,9,26,tzinfo=timezone.utc)}]}]
    def get_object(self, **kwargs):
        assert kwargs['IfMatch'] == 'stable'
        class Body(io.BytesIO):
            def iter_chunks(self, chunk_size):
                yield self.read()
        return {'Body':Body(b'abc')}


def test_copy_preserves_names_and_requires_readback(monkeypatch, tmp_path):
    monkeypatch.setenv('SOURCE_PROJECT_ID', 'atlas')
    seen = []
    def copy(client, bucket, key, path):
        assert path.parent == tmp_path and path.read_bytes() == b'abc'
        assert key.endswith('/objects/0')
        return {'bytes':3, 'sha256':'verified-checksum', 'object_key':key,
                'full_readback_verified':True}
    monkeypatch.setattr(worker, 'copy_readback', copy)
    monkeypatch.setattr(worker, 'put_json', lambda *a:seen.append(a[-1]))
    result = importer.copy_artifacts(Source(), None, 'source', 'compass', tmp_path)
    assert result['files']['../research.json']['full_readback_verified']
    assert result['object_count'] == 1 and result['byte_count'] == 3
    assert seen == [result]


def test_changed_source_never_receives_verified_manifest(monkeypatch, tmp_path):
    monkeypatch.setenv('SOURCE_PROJECT_ID', 'atlas')
    monkeypatch.setattr(worker, 'copy_readback', lambda *a:{'bytes':3})
    monkeypatch.setattr(worker, 'put_json', lambda *a:pytest.fail('must not certify changed source'))
    with pytest.raises(RuntimeError, match='inventory changed'):
        importer.copy_artifacts(Source(changed=True), None, 'source', 'compass', tmp_path)
