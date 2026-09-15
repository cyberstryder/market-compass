from sqlalchemy import select
from compass.option_ideas import OptionIdeas
from compass.forward_audit import capture,KEY
from compass.config import Config
from compass.obsidian import ingest_page,contracts
from compass.universe import data_symbols
from test_option_ideas import db,cfg,NOW,seed,signal,rows,q,CALL


def opened(db,cfg):
    service=OptionIdeas(db,cfg)
    with db.tx() as c:
        seed(db,c);service.queue(c,signal(),NOW);service.tick(c,NOW)
    return service


def archive(db,monkeypatch,stamp,option_bid):
    monkeypatch.setattr('compass.store.time.time',lambda:stamp)
    with db.tx() as c:
        for symbol,quote in [('SPY',q(stamp)),(CALL,q(stamp,option_bid,option_bid+.05))]:
            db.append(c,'quote','test',symbol,stamp,quote)
            db.put(c,'quote:'+symbol,quote)


def test_delayed_worker_replays_option_target_before_retrace(db,cfg,monkeypatch):
    service=opened(db,cfg)
    for delta,bid in [(5,3.5),(10,2),(15,2),(20,2)]:archive(db,monkeypatch,NOW+delta,bid)
    with db.tx() as c:
        service.tick(c,NOW+20);p=rows(c)[0]
        assert p['status']=='closed' and p['exit_reason']=='premium_target'
        assert p['finished_at']==NOW+5 and p['exit']==3.09


def test_real_quote_gap_not_repaired_by_later_target(db,cfg,monkeypatch):
    service=opened(db,cfg);archive(db,monkeypatch,NOW+20,3.5)
    with db.tx() as c:
        service.tick(c,NOW+20);p=rows(c)[0]
        assert p['status']=='unresolved' and p['pnl'] is None
        assert p['gap_detail']['reason']=='missing_continuous_paired_quotes'


def test_djt_excluded_without_deleting_original_history(db,cfg):
    from test_obsidian import event
    e={**event(), 'ticker':'DJT'}
    ingest_page(db,'test',dict(events=[e],cursor=1),NOW)
    cfg.stocks=('SPY','DJT')
    with db.tx() as c:
        assert 'DJT' not in cfg.watch_symbols
        assert 'DJT' not in data_symbols(db,c,cfg,NOW)
        assert contracts(c,NOW)==[]


def test_forward_boundary_is_durable_and_old_records_separate(db,cfg):
    opened(db,cfg)
    first=capture(db,cfg,NOW+60);second=capture(db,cfg,NOW+120)
    assert first['since']==second['since']==NOW+60
    assert second['options_before']['total']==1 and second['options_after']['total']==0
