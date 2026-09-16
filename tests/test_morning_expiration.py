"""Same-alert forward study: bounded quotes, cost math, persistence and missing evidence."""
import json
import os
import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select, func

from compass.native_morning.options import AlpacaQuotes, OptionsConfig, option_text
from compass.native_morning.option_history import schedule_history, sample_batch
from compass.native_morning.expiration import quality, net_exit, summarize_signal, aggregates, COST_MODEL
from compass.native_morning import store as st
from compass.native_morning.discord import signal_message
from native_morning_fixtures import signal_event

STAMP = int(datetime(2026, 9, 16, 13, 36, tzinfo=timezone.utc).timestamp() * 1000)
CONFIG = OptionsConfig(True, 'synthetic_key', 'synthetic_secret')


def contract(expiry='2026-09-16', strike=100):
    return dict(symbol='DEMO'+expiry.replace('-', '')[2:]+'C'+f'{int(strike*1000):08d}',
        expiration_date=expiry, strike_price=str(strike), size='100', type='call', underlying_symbol='DEMO',
        root_symbol='DEMO', tradable=True, status='active')


def provider_case(contracts=None, changes=None, now=STAMP+2000, signal=None, token=None, status=200):
    contracts = [contract(), contract('2026-09-18')] if contracts is None else contracts
    requests=[]
    def handler(request):
        requests.append(request)
        if request.url.path.endswith('/contracts'):
            assert request.url.params['expiration_date_gte']=='2026-09-16'
            return httpx.Response(200, json={'option_contracts': contracts, 'next_page_token': token})
        quotes={}
        for c in contracts:
            q=dict(t='2026-09-16T13:36:01Z', bp=1., ap=1.1, bs=10, **{'as':10})
            q.update((changes or {}).get(c['symbol'], {}))
            quotes[c['symbol']]={'latestQuote':q, 'greeks':{'delta':.5, 'theta':-.1}, 'impliedVolatility':.45}
        return httpx.Response(status, json={'snapshots':quotes})
    client=httpx.Client(transport=httpx.MockTransport(handler))
    provider=AlpacaQuotes(CONFIG, client, clock=lambda:now, pace_seconds=0)
    result=provider.lookup(signal or signal_event(stamp=STAMP)['signal'], compare=True)
    client.close()
    return result, requests


def test_distinct_contracts_share_one_request_and_near_is_not_independent():
    q, requests=provider_case([contract(strike=99),contract(strike=101),contract('2026-09-18')])
    v=q['expiration_comparison']['variants']
    assert q['contract']['expiration']=='2026-09-18'
    assert v['zero_dte']['contract']['strike']==99
    assert v['near_dte']['alias_of']=='baseline'
    assert v['near_dte']['contract']==q['contract']
    assert len(requests)==2
    assert len(requests[1].url.params['symbols'].split(','))==2
    assert v['baseline']['quote_at_ms']==v['zero_dte']['quote_at_ms']
    assert q['cost_model']==COST_MODEL


@pytest.mark.parametrize('contracts,zero,near', [
    ([contract('2026-09-18')],False,True),
    ([contract('2026-09-23')],False,False),
    ([contract()],True,False), ([],False,False)])
def test_actual_expiration_availability_is_explicit(contracts,zero,near):
    q,_=provider_case(contracts)
    variants=q['expiration_comparison']['variants']
    assert bool(variants['zero_dte'].get('contract'))==zero
    assert bool(variants['near_dte'].get('contract'))==near
    if not zero:assert variants['zero_dte']['reason']=='no_eligible_same_day_contract'
    if not near:assert variants['near_dte']['reason']=='no_eligible_contract_within_1_to_3_days'


def test_invalid_adjusted_contracts_and_incomplete_catalog_do_not_create_a_winner():
    bad=contract();bad['size']='10'
    q,_=provider_case([bad,contract('2026-09-18')])
    assert 'contract' not in q['expiration_comparison']['variants']['zero_dte']
    q,requests=provider_case(token='same')
    assert len(requests)==2 and all(v['status']=='unavailable' for v in q['expiration_comparison']['variants'].values())
    assert q['reason']=='contract_search_incomplete'


@pytest.mark.parametrize('field,value,reason', [
    ('t','2026-09-16T13:35:59Z','entry_quote_before_signal'),
    ('t','2026-09-16T13:34:00Z','quote_stale_or_future'),
    ('ap',.5,'quote_crossed'), ('bs',0,'invalid_provider_response')])
def test_bad_zero_quote_does_not_damage_baseline(field,value,reason):
    q,_=provider_case(changes={contract()['symbol']:{field:value}})
    assert q['status']=='available'
    zero=q['expiration_comparison']['variants']['zero_dte']
    assert zero['status']=='unavailable' and zero['reason']==reason
    assert '0DTE: unavailable' in option_text(q)


@pytest.mark.parametrize('test,lag', [(True,2000),(False,121000)])
def test_no_old_entry_backfill_or_test_requests(test,lag):
    q,requests=provider_case(signal=signal_event(stamp=STAMP,is_test=test)['signal'],now=STAMP+lag)
    assert not requests and all(v['status']=='unavailable' for v in q['expiration_comparison']['variants'].values())


def test_fees_spread_hurdle_and_stress_are_not_strike_increment():
    q,_=provider_case();cost=quality(q)
    assert cost['premium_at_ask']==pytest.approx(110)
    assert cost['spread_and_fees']==pytest.approx(11.30)
    assert cost['approx_stock_cost_hurdle']==pytest.approx(.226)
    later=dict(q,bid=1.3,quote_at_ms=STAMP+65000)
    result=net_exit(q,later)
    assert result['gross_pnl']==pytest.approx(20)
    assert result['net_pnl']==pytest.approx(18.7)
    assert result['stress_net_pnl']==pytest.approx(16.7)
    assert result['net_return_pct']==pytest.approx(18.7/110.65*100)
    q['greeks']={};assert quality(q)['approx_stock_cost_hurdle'] is None
    assert net_exit(q,dict(later,contract={'symbol':'OTHER'})) is None
    assert net_exit(q,dict(later,quote_at_ms=q['quote_at_ms'])) is None


class Quotes:
    def __init__(self,now=STAMP+65000):self.now,self.calls=now,[]
    def clock(self):return self.now
    def snapshots(self,contracts):
        self.calls.append(contracts)
        return {c['symbol']:dict(status='available',contract=c,bid=1.3,ask=1.4,midpoint=1.35,
            quote_at_ms=self.now-1000,fetched_at_ms=self.now) for c in contracts}


@pytest.fixture
def engine(tmp_path):
    engine=st.make_engine('sqlite:///'+str(tmp_path/'comparison.db'));st.metadata.create_all(engine)
    yield engine
    engine.dispose()


def test_two_tapes_idempotent_batched_and_quiet_with_recovery(engine):
    q,_=provider_case();signal=signal_event(stamp=STAMP)['signal'];sid=signal['signal_id']
    with engine.begin() as c:
        schedule_history(c,signal,q,STAMP+2000);schedule_history(c,signal,q,STAMP+3000)
        assert c.execute(select(func.count()).select_from(st.option_samples)).scalar_one()==60
        assert c.execute(select(func.count()).select_from(st.expiration_samples)).scalar_one()==60
    p=Quotes();assert sample_batch(engine,p);assert not sample_batch(engine,p)
    assert len(p.calls)==1 and len(p.calls[0])==2
    with engine.begin() as c:
        for table in (st.option_samples,st.expiration_samples):
            first=c.execute(select(table).where(table.c.minute==1)).mappings().one()
            assert first['status']=='complete' and json.loads(first['quote_json'])['status']=='available'
        c.execute(st.expiration_samples.update().where(st.expiration_samples.c.minute==2).values(status='fetching',lease_until_ms=STAMP+160000,lease_token='dead'))
    p.now=STAMP+165000;sample_batch(engine,p)
    with engine.connect() as c:
        missed=json.loads(c.execute(select(st.expiration_samples.c.quote_json).where(st.expiration_samples.c.minute==2)).scalar_one())
        assert missed['status']=='missed'
        assert c.execute(select(func.count()).select_from(st.outbox)).scalar_one()==0
    assert len(p.calls)==1  # no current quote substituted for missed windows


def test_slow_response_and_old_quote_excluded_from_fixed_exit(engine):
    q,_=provider_case();signal=signal_event(stamp=STAMP)['signal']
    with engine.begin() as c:schedule_history(c,signal,q,STAMP+2000)
    class Slow(Quotes):
        def snapshots(self,contracts):
            quotes=super().snapshots(contracts);self.now+=30000;return quotes
    sample_batch(engine,Slow())
    with engine.connect() as c:
        for table in (st.option_samples,st.expiration_samples):
            assert json.loads(c.execute(select(table.c.quote_json).where(table.c.minute==1)).scalar_one())['status']=='missed'


def measured_report(q,missing=None,skew=0):
    tapes=[]
    for key in ('baseline','zero_dte'):
        rows=[];ref=q['expiration_comparison']['variants'][key]
        for h in (5,15,30,60):
            if missing==(key,h):continue
            stamp=STAMP+h*60000+1000+(skew if key=='zero_dte' else 0)
            rows.append(dict(minute=h,status='complete',due_at_ms=STAMP+h*60000,
                quote=dict(status='available',contract=ref['contract'],bid=1.3,quote_at_ms=stamp,sampled_at_ms=stamp+1000)))
        tapes.append(rows)
    return summarize_signal(q,*tapes,STAMP+3700000)


def test_same_alert_cohort_all_exits_missing_is_not_a_loss_and_skew_is_visible():
    q,_=provider_case();complete=measured_report(q)
    report=aggregates([complete,measured_report(q,missing=('zero_dte',15)),measured_report(q,skew=6000),{'status':'not_collected'}])
    for pair in report['pairs']:
        assert pair['paired']==1 and pair['excluded']==3 and pair['timestamp_skew_excluded']==1
        assert all(x['variants'][pair['left']]['n']==1 for x in pair['fixed_exits'])
        assert pair['fixed_exits'][0]['variants']['zero_dte']['median_net_pnl']==pytest.approx(18.7)
    assert report['execution_eligible'] is False
    assert complete['variants']['near_dte']['alias_of']=='baseline'
    assert complete['variants']['baseline']['best_observed']['hindsight_only']
    assert summarize_signal({},[],[],STAMP)['status']=='not_collected'


def test_long_alert_fits_one_edit_and_clarifies_direction():
    q,_=provider_case(changes={contract('2026-09-18')['symbol']:{'bp':.5}})
    signal=signal_event('x'*500,stamp=STAMP)['signal']
    payload=signal_message(signal,STAMP+45000)
    content=payload['content']+'\n\n'+option_text(q)
    assert len(content)<=2000
    assert 'CALL bias is stock direction' in content
    assert 'Wide spread' in content and 'same contract' in content


def test_parallel_postgres_workers_claim_each_arm_once():
    url=os.getenv('TEST_POSTGRES_URL') or os.getenv('SMOOTHERS_TEST_DATABASE_URL')
    if not url:pytest.skip('Dedicated PostgreSQL unavailable')
    from concurrent.futures import ThreadPoolExecutor
    engine=st.make_engine(url);st.metadata.create_all(engine)
    sid='expiry-'+uuid.uuid4().hex;q,_=provider_case();signal=signal_event(sid,stamp=STAMP)['signal']
    try:
        with engine.begin() as c:schedule_history(c,signal,q,STAMP+2000)
        providers=[Quotes(),Quotes()]
        with ThreadPoolExecutor(2) as pool:list(pool.map(lambda p:sample_batch(engine,p),providers))
        with engine.connect() as c:
            for table in (st.option_samples,st.expiration_samples):
                rows=c.execute(select(table).where(table.c.signal_id==sid,table.c.status=='complete')).all()
                assert len(rows)==1
        assert sum(len(c) for p in providers for c in p.calls)==2
    finally:
        with engine.begin() as c:
            for t in (st.option_samples,st.option_tracks,st.expiration_samples,st.expiration_tracks):c.execute(t.delete().where(t.c.signal_id==sid))
        engine.dispose()
