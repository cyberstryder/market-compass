from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from tests.test_smoothers_postgres_handoff import pg
from tests.test_alert_ownership import row,NOW
from compass.alert_ownership import assess,report


def test_concurrent_producers_admit_only_one_primary(pg):
    barrier=Barrier(2)
    def admit(i):
        barrier.wait(5)
        with pg.tx() as c:return assess(pg,c,row(i,source='scanner'+str(i)),NOW)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(admit,i) for i in (1,2)]
        decisions=[f.result(timeout=10) for f in futures]
    assert sorted(x['action'] for x in decisions)==['publish','suppressed']
    assert len({x['trade_id'] for x in decisions})==1
    with pg.tx() as c:
        assert len(report(pg,c,'2026-09-23')['records'])==1
