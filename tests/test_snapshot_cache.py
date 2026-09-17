from concurrent.futures import ThreadPoolExecutor
from threading import Event
import pytest
from compass.snapshot_cache import SnapshotCache


def test_concurrent_dashboard_refreshes_share_one_query_and_original_clock():
    started, release = Event(), Event()
    calls = []
    cache = SnapshotCache(clock=lambda: 10)
    def build():
        calls.append(1)
        started.set()
        assert release.wait(5)
        return {'asof': 123, 'quote': {'ts': 120}}
    with ThreadPoolExecutor(max_workers=16) as pool:
        first = pool.submit(cache.get, build)
        assert started.wait(5)
        followers = [pool.submit(cache.get, build) for _ in range(15)]
        release.set()
        results = [f.result(timeout=5) for f in [first, *followers]]
    assert len(calls) == 1
    assert all(r == {'asof': 123, 'quote': {'ts': 120}} for r in results)


def test_expiry_and_failed_refresh_do_not_return_old_evidence():
    clock = [1]
    cache = SnapshotCache(clock=lambda: clock[0])
    assert cache.get(lambda: {'asof': 1}) == {'asof': 1}
    clock[0] = 3
    def failed():
        raise RuntimeError('database unavailable')
    with pytest.raises(RuntimeError, match='database unavailable'):
        cache.get(failed)
    assert cache.get(lambda: {'asof': 3}) == {'asof': 3}
