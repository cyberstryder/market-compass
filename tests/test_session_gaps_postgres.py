from sqlalchemy import insert
from tests.test_smoothers_postgres_handoff import pg
from tests.test_session_gaps import NOW, trial
from compass.session_gaps import window, totals, gap_page
from compass.setup_study import trials

def test_postgres_asset_aggregate_and_tied_cursor(pg):
    w=window(NOW,'2026-09-18')
    with pg.tx() as c:
        c.execute(insert(trials),[
            trial('a',w['since']),trial('b',w['since']),trial('s',w['since'],asset='stock')])
        assert totals(c,w)['total']==2
        p=gap_page(c,NOW,limit=1)
        assert [r['id'] for r in p['records']]==['a']
        p=gap_page(c,NOW,limit=1,cursor=p['next_cursor'])
        assert [r['id'] for r in p['records']]==['b'] and not p['has_more']
