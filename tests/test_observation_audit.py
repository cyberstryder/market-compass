from copy import deepcopy
from types import SimpleNamespace
from sqlalchemy import select, func
from compass.observation_audit import build, summarize_reviews
from compass.secondary import VERSION
from compass.store import events
from test_quote_coverage import db, archive, quote, NOW


def review(symbol='TGT', verdict='supported', value=1, at=NOW, status='observed', timely=True):
    return dict(project='morning',symbol=symbol,decided_at=at,version=VERSION,
        verdict=verdict,candidate=dict(symbol=symbol,side='long',strategy='test',source_version='v1'),
        decision=dict(timely=timely,reasons=[]),source_outcome={},
        measurements=dict(state='complete',horizons={'15':dict(due=at+900,status=status,
            return_pct=value,observation_source='retained_quote')}))


def test_corrected_comparisons_do_not_rescore_earlier_decisions():
    rows=[review(value=1),review(value=-1),review(verdict='watch',value=1),
        review(verdict='rejected',value=-1),review(value=None,status='missing'),
        review(at=NOW-1),review(verdict='insufficient_data',timely=False)]
    before=deepcopy(rows)
    result=summarize_reviews(rows,NOW,NOW+1000)
    assert result['corrected_reviews']==6 and result['earlier_reviews']==1
    point=result['comparisons'][0]['horizons'][0]
    assert point['measured']==4 and point['missing']==1
    assert point['supported_positive']==point['supported_negative']==1
    assert point['missed_positive']==point['avoided_negative']==1
    assert result['checkpoints'][0]['due']==7
    assert rows==before


def test_pending_boundary_and_old_version_remain_explicit():
    pending=review(status='pending',value=None)
    boundary=review(status='session_boundary',value=None)
    old=review(at=NOW-10);old['version']='secondary-context-v2'
    result=summarize_reviews([pending,boundary,old],NOW,NOW+1000)
    assert result['corrected_reviews']==2
    p=result['comparisons'][0]['horizons'][0]
    assert p['measured']==0 and p['pending']==p['session_boundary']==1
    assert p['supported_negative']==p['avoided_negative']==0
    assert sum(g.get('overdue',0) for g in result['checkpoints'])==1


def test_archive_audit_checks_real_post_fix_records_without_writes(db):
    cfg=SimpleNamespace(morning_enabled=True,watch_symbols=('TGT','SBUX','SPY'))
    with db.tx() as c:
        for symbol in cfg.watch_symbols:
            db.put(c,'quote:'+symbol,quote(NOW+10))
        archive(db,c,NOW+1,symbol='TGT')
        archive(db,c,NOW+9,symbol='TGT')
        archive(db,c,NOW-100,symbol='SBUX')
        count=c.execute(select(func.count()).select_from(events)).scalar_one()
        result=build(db,c,cfg,NOW+11,since=NOW)
        assert result['collected_symbols']==3 and result['post_fix_archives']==1
        assert result['no_post_fix_archive']==['SBUX','SPY']
        assert result['focus_archives']['TGT']['first']['source_ts']==NOW+1
        assert result['focus_archives']['TGT']['last']['usable_when_recorded']
        assert result['focus_archives']['SBUX']['first'] is None
        assert result['corrected_reviews']==0 and result['comparisons']==[]
        assert c.execute(select(func.count()).select_from(events)).scalar_one()==count
        assert db.get(c,'observation_audit:report') is None


def test_late_quote_is_not_claimed_usable(db):
    with db.tx() as c:
        archive(db,c,NOW+1,symbol='TGT',received=NOW+10)
        db.put(c,'quote:TGT',quote(NOW+1))
        r=build(db,c,SimpleNamespace(morning_enabled=True,watch_symbols=('TGT',)),NOW+11,since=NOW)
        assert r['post_fix_archives']==1
        assert not r['focus_archives']['TGT']['last']['usable_when_recorded']
