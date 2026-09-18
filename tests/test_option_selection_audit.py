from datetime import datetime, timezone
import pytest
from compass.config import Config
from compass.engine import Engine
from compass.scanner import Scanner
from compass.futures import risk_day
from compass.paper_risk import key, ledgers
from compass.option_selection_audit import snapshot
from compass.forward_audit import failure_evidence
from test_scanner import db, NOW, quote

CONTRACT='O:SPY260914C00101000'


def signal():
    return dict(id='test-selection',symbol='SPY',side='long',signal_time=NOW,decided_at=NOW,
        signal_price=101.1,invalidation=100,context={'atr14':1},strategy='compass-scanner-test',track='intraday')


def seed(db,c):
    db.put(c,'chain:SPY',dict(asof=NOW,complete=True,source='fixture',contracts=[
        dict(symbol=CONTRACT,expiry='2026-09-14',type='call',strike=101,multiplier=100)]))
    db.put(c,'quote:'+CONTRACT,quote(NOW,2,2.01))


@pytest.mark.parametrize('case,reason',[
    ('chain','No recent options chain'),
    ('expiry','No eligible listed same-day call contract'),
    ('quote','Missing or invalid fresh quote'),
    ('spread','Spread exceeds simulation liquidity limit'),
    ('daily_loss','Daily simulated loss limit'),
    ('portfolio','Portfolio cap: three simultaneous simulated positions'),
    ('premium','One unit exceeds risk or stop invalid'),
])
def test_quiet_selection_exposes_real_gate_without_changing_it(db,case,reason):
    cfg=Config(local=True,stocks=('SPY',),futures=(),setup_study=False)
    with db.tx() as c:
        seed(db,c)
        if case=='chain':db.put(c,'chain:SPY',{})
        if case=='expiry':
            chain=db.get(c,'chain:SPY');chain['contracts'][0]['expiry']='2026-09-15';db.put(c,'chain:SPY',chain)
        if case=='quote':db.put(c,'quote:'+CONTRACT,quote(NOW-6,2,2.01))
        if case=='spread':db.put(c,'quote:'+CONTRACT,quote(NOW,1,2))
        if case=='daily_loss':
            risk=ledgers(db,c,NOW,persist=True)['option'];risk.update(realized=-cfg.daily_loss,entries=1)
            db.put(c,key(NOW,'option'),risk)
        if case=='portfolio':
            for symbol in ('A','B','C'):db.put(c,'position:'+symbol,dict(status='open',asset='option'))
        if case=='premium':db.put(c,'quote:'+CONTRACT,quote(NOW,10,10.01))
        audit={}
        assert not Engine(db,cfg).options(c,signal(),NOW,quiet=True,diagnostics=audit)
        assert audit['reason']==reason and audit['status']=='blocked'
        assert not db.recent(c,'alert') and not db.prefix(c,'trade:')


def test_pending_expiration_keeps_last_contract_reason_without_retry_alert_spam(db):
    cfg=Config(local=True,stocks=('SPY',),futures=(),setup_study=False)
    scanner,engine=Scanner(db,cfg),Engine(db,cfg)
    with db.tx() as c:
        seed(db,c)
        db.put(c,'quote:'+CONTRACT,quote(NOW-6,2,2.01))
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        for at in (NOW,NOW+1):scanner.retry_options(c,{'SPY':quote(at)},at,engine)
        assert not db.recent(c,'alert')
        pending=db.get(c,'pending_options:test')
        assert pending['last_selection']['rejections'][0]['quote_age_seconds']==6
        scanner.retry_options(c,{'SPY':quote(NOW+121)},NOW+121,engine)
        expired=db.get(c,'pending_options:test')
        assert expired['status']=='expired' and expired['last_selection']==pending['last_selection']
        assert len(db.recent(c,'alert'))==1 and not db.prefix(c,'trade:')


def test_successful_selection_still_enters_one_position_and_records_contract(db):
    cfg=Config(local=True,stocks=('SPY',),futures=(),setup_study=False)
    with db.tx() as c:
        seed(db,c)
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        Scanner(db,cfg).retry_options(c,{'SPY':quote()},NOW,Engine(db,cfg))
        pending=db.get(c,'pending_options:test')
        assert pending['status']=='entered' and pending['last_selection']['selected_contract']==CONTRACT
        assert len(db.prefix(c,'trade:'))==1 and len(db.recent(c,'alert'))==1


def test_audit_keeps_unknown_history_and_separate_cash_and_overnight_risk(db,monkeypatch):
    at=datetime(2026,9,14,23,tzinfo=timezone.utc).timestamp()
    monkeypatch.setattr('compass.store.time.time',lambda:at)
    cfg=Config(local=True)
    with db.tx() as c:
        db.put(c,'pending_options:old',dict(signal=signal(),status='expired',reason='Generic expiration'))
        db.put(c,'pending_options:new',dict(signal=signal(),status='waiting',last_selection={
            'at':at,'reason':'Daily simulated loss limit','rejections':[{'symbol':CONTRACT,'reason':'Daily simulated loss limit'}]}))
        db.put(c,'position:closed',dict(status='closed',asset='option'))
        db.put(c,'position:live',dict(status='open',asset='stock'))
        db.put(c,'risk:2026-09-14',dict(realized=-350,entries=4))
        db.put(c,'risk:2026-09-15',dict(realized=0,entries=0))
        report=snapshot(db,c,cfg,at)
    assert report['candidates']==2 and report['instrumented']==report['missing_diagnostics']==1
    assert report['last_attempt_reasons']=={'Daily simulated loss limit':1}
    assert report['open_positions']=={'stock':1}
    assert report['legacy_risk']==[dict(day='2026-09-14',realized=-350,entries=4),dict(day='2026-09-15',realized=0,entries=0)]


def test_archive_evidence_does_not_claim_a_usable_stream_was_missing():
    rows=[dict(status='unresolved',archive_check={
        'option':dict(usable_rows=3,stale_or_invalid_when_recorded=0),
        'underlying':dict(usable_rows=0,stale_or_invalid_when_recorded=1)})]
    assert failure_evidence(rows)=={'option: usable samples present; inspect gap timing':1,
        'underlying: stale or invalid archived samples':1}


@pytest.mark.parametrize('offset', [-6, 2])
def test_retry_freezes_exact_underlying_clock_and_contemporaneous_loss_lock(db,offset):
    cfg=Config(local=True,stocks=('SPY',),futures=(),setup_study=False)
    scanner,engine=Scanner(db,cfg),Engine(db,cfg)
    with db.tx() as c:
        seed(db,c)
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        risk=ledgers(db,c,NOW,persist=True)['option'];risk.update(realized=-390.5,entries=40)
        db.put(c,key(NOW,'option'),risk)
        q=quote(NOW+offset)
        scanner.retry_options(c,{'SPY':q},NOW,engine)
        saved=db.get(c,'pending_options:test')['last_selection']
        assert saved['underlying_evidence']['age_seconds']==-offset
        assert saved['underlying_evidence']['fresh'] is False
        assert saved['underlying_evidence']['quote']['ts']==q['ts']
        assert saved['risk_context']['daily_loss_locked'] is True
        assert saved['reason']=='Fresh underlying quote required'
        risk.update(realized=0,entries=0);db.put(c,key(NOW,'option'),risk)
        scanner.retry_options(c,{'SPY':quote(NOW+121)},NOW+121,engine)
        assert db.get(c,'pending_options:test')['last_selection']==saved
        assert not db.prefix(c,'trade:')
