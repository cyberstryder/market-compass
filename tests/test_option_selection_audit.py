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
    ('expiry','No eligible listed call contract within 7 DTE'),
    ('quote','Missing or invalid fresh quote'),
    ('spread','Spread exceeds simulation liquidity limit'),
    ('portfolio','Portfolio cap: three simultaneous simulated positions'),
])
def test_quiet_selection_exposes_real_gate_without_changing_it(db,case,reason):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    with db.tx() as c:
        seed(db,c)
        if case=='chain':db.put(c,'chain:SPY',{})
        if case=='expiry':
            chain=db.get(c,'chain:SPY');chain['contracts'][0]['expiry']='2026-09-22';db.put(c,'chain:SPY',chain)
        if case=='quote':db.put(c,'quote:'+CONTRACT,quote(NOW-120,2,2.01))
        if case=='spread':db.put(c,'quote:'+CONTRACT,quote(NOW,1,2))
        if case=='portfolio':
            for symbol in ('A','B','C'):db.put(c,'position:'+symbol,dict(status='open',asset='option'))
        audit={}
        assert not Engine(db,cfg).options(c,signal(),NOW,quiet=True,diagnostics=audit)
        assert audit['reason']==reason and audit['status']=='blocked'
        assert not db.recent(c,'alert') and not db.prefix(c,'trade:')


def test_pending_expiration_keeps_last_contract_reason_without_retry_alert_spam(db):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
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
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
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
    cfg=Config(paper_trading=True,local=True)
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


# Offsets are quote stamp minus decision time: -90 is beyond the
# book's 60s decision-time stale bound (SCANNER_TRIGGER_QUOTE_MAX_AGE,
# applied at the retry since the 2026-11-03 alignment; the retry
# previously used fresh()'s 5s default and -6 was the stale case);
# +120 is beyond the bounded future-skew tolerance
# (QUOTE_FUTURE_SKEW_TOLERANCE_S), i.e. corrupt, not clock skew. The
# old +2 case pinned the pre-2026-11-02 semantics under which vendor
# stamps a few seconds ahead of the engine clock were rejected.
@pytest.mark.parametrize('offset', [-90, 120])
def test_retry_freezes_exact_underlying_clock_and_contemporaneous_loss_lock(db,offset):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
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
        assert 'daily_loss_locked' not in saved['risk_context']
        assert saved['reason']=='Fresh underlying quote required'
        risk.update(realized=0,entries=0);db.put(c,key(NOW,'option'),risk)
        scanner.retry_options(c,{'SPY':quote(NOW+121)},NOW+121,engine)
        assert db.get(c,'pending_options:test')['last_selection']==saved
        assert not db.prefix(c,'trade:')


def test_live_retry_uses_current_underlying_and_clock_after_slow_scan(db):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    at=NOW+4
    with db.tx() as c:
        seed(db,c)
        db.put(c,'quote:SPY',quote(at))
        db.put(c,'quote:'+CONTRACT,quote(at,2,2.01))
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        # The scan snapshot predates a quote that is valid at the real retry.
        Scanner(db,cfg).retry_options(c,{'SPY':quote(NOW-10)},NOW,Engine(db,cfg,clock=lambda:at))
        p=db.get(c,'pending_options:test');a=p['last_selection']
        assert p['status']=='entered' and a['at']==at and a['scan_started_at']==NOW
        assert a['underlying_evidence']['checked_at']==at
        assert a['underlying_evidence']['age_seconds']==0
        assert a['selected_quote_evidence']['quote_age_seconds']==0
        assert next(iter(db.prefix(c,'trade:').values()))['entered_at']==at


@pytest.mark.parametrize('offset',[-90,120])
def test_live_retry_still_rejects_truly_stale_or_future_underlying(db,offset):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    at=NOW+4
    with db.tx() as c:
        seed(db,c);db.put(c,'quote:SPY',quote(at+offset))
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        Scanner(db,cfg).retry_options(c,{'SPY':quote()},NOW,Engine(db,cfg,clock=lambda:at))
        a=db.get(c,'pending_options:test')['last_selection']
        assert a['reason']=='Fresh underlying quote required'
        assert a['underlying_evidence']['age_seconds']==-offset
        assert not db.prefix(c,'trade:')


def test_live_retry_accepts_vendor_clock_skewed_underlying(db):
    # Production defect, 2026-11-02: Alpaca underlying quotes stamped
    # ~3s ahead of the engine clock were rejected as future quotes,
    # producing 1,717 'Fresh underlying quote required' blocks in one
    # RTH morning. A stamp inside the bounded skew tolerance is
    # current data and the retry must proceed on it.
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    at=NOW+4
    with db.tx() as c:
        seed(db,c);db.put(c,'quote:SPY',quote(at+2))
        db.put(c,'quote:'+CONTRACT,quote(at,2,2.01))
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        Scanner(db,cfg).retry_options(c,{'SPY':quote(at+2)},NOW,Engine(db,cfg,clock=lambda:at))
        p=db.get(c,'pending_options:test')
        assert p['status']=='entered'
        assert p['last_selection']['underlying_evidence']['age_seconds']==-2
        assert p['last_selection']['underlying_evidence']['fresh'] is True


def test_live_retry_expiration_uses_actual_time_not_scan_start(db):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    with db.tx() as c:
        seed(db,c);db.put(c,'quote:SPY',quote(NOW+121))
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        Scanner(db,cfg).retry_options(c,{'SPY':quote()},NOW,Engine(db,cfg,clock=lambda:NOW+121))
        assert db.get(c,'pending_options:test')['status']=='expired'
        assert not db.prefix(c,'trade:')


def _selection_events(db,c):
    return db.recent(c,'option_selection_diagnostic')


def test_selection_rejection_is_emitted_as_event_with_contract_reason(db):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    with db.tx() as c:
        seed(db,c)
        db.put(c,'quote:'+CONTRACT,quote(NOW-120,2,2.01))
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        Scanner(db,cfg).retry_options(c,{'SPY':quote()},NOW,Engine(db,cfg))
        events=_selection_events(db,c)
        assert len(events)==1 and events[0]['symbol']=='SPY'
        p=events[0]['payload']
        assert p['signal_id']=='test-selection' and p['final'] is False
        assert p['chain_age_seconds']==0 and p['checked_contracts']==1
        assert p['rejections'][0]['symbol']==CONTRACT
        assert p['rejections'][0]['reason']=='Missing or invalid fresh quote'
        assert p['rejections'][0]['quote_age_seconds']==120
        # Recording is additional: gate outcome and the state-row audit are unchanged.
        pending=db.get(c,'pending_options:test')
        assert pending['status']=='waiting'
        assert pending['last_selection']['rejections'][0]['quote_age_seconds']==120
        assert not db.prefix(c,'trade:')


def test_selection_chain_age_is_emitted_as_event(db):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    with db.tx() as c:
        seed(db,c)
        chain=db.get(c,'chain:SPY');chain['asof']=NOW-121;db.put(c,'chain:SPY',chain)
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        Scanner(db,cfg).retry_options(c,{'SPY':quote()},NOW,Engine(db,cfg))
        events=_selection_events(db,c)
        assert len(events)==1
        p=events[0]['payload']
        assert p['audit_reason']=='No recent options chain'
        assert p['chain_age_seconds']==121 and p['rejections']==[]
        assert db.get(c,'pending_options:test')['status']=='waiting'


def test_selection_spread_rejection_is_emitted_as_event(db):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    with db.tx() as c:
        seed(db,c)
        db.put(c,'quote:'+CONTRACT,quote(NOW,1,2))
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        Scanner(db,cfg).retry_options(c,{'SPY':quote()},NOW,Engine(db,cfg))
        events=_selection_events(db,c)
        assert len(events)==1
        p=events[0]['payload']
        assert p['rejections'][0]['symbol']==CONTRACT
        assert p['rejections'][0]['reason']=='Spread exceeds simulation liquidity limit'
        assert p['rejections'][0]['quote_age_seconds']==0


def test_selection_expiry_emits_final_event_carrying_last_rejections(db):
    cfg=Config(paper_trading=True,local=True,stocks=('SPY',),futures=(),setup_study=False)
    scanner,engine=Scanner(db,cfg),Engine(db,cfg)
    with db.tx() as c:
        seed(db,c)
        db.put(c,'quote:'+CONTRACT,quote(NOW-6,2,2.01))
        db.put(c,'pending_options:test',dict(signal=signal(),status='waiting',expires_at=NOW+120))
        scanner.retry_options(c,{'SPY':quote()},NOW,engine)
        scanner.retry_options(c,{'SPY':quote(NOW+121)},NOW+121,engine)
        events=_selection_events(db,c)
        finals=[e for e in events if e['payload']['final']]
        assert len(events)==2 and len(finals)==1
        p=finals[0]['payload']
        assert 'Option selection window ended' in p['expiry_reason']
        assert p['rejections'][0]['symbol']==CONTRACT
        # 6s old is inside the 60s option quote window but predates the
        # signal, so entry_check's stamp rule is the recorded killer.
        assert p['rejections'][0]['reason']=='No quote after signal'
        assert p['rejections'][0]['quote_age_seconds']==6
        # Expiry behavior is unchanged: one skip alert, no trade.
        assert db.get(c,'pending_options:test')['status']=='expired'
        assert len(db.recent(c,'alert'))==1 and not db.prefix(c,'trade:')
