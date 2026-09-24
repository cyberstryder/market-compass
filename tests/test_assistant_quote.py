import asyncio
from types import SimpleNamespace
import pytest
from compass.assistant_quote import recover

NOW=1790253000.
TICKER='U261016C00048000'


def run(original,alpaca=None,history=None,error=False):
    calls=[]
    async def get(url,headers=None,params=None):
        calls.append((url,params))
        if error:raise RuntimeError('secret')
        if 'alpaca' in url:
            assert params=={'symbols':TICKER,'feed':'opra'}
            return {'quotes':{TICKER:alpaca or {},'WRONG':{'bp':99,'ap':100,'t':NOW}}}
        assert url.endswith('/O:'+TICKER)
        assert params['order']=='desc' and params['limit']==5000
        return {'results':history or []}
    collector=SimpleNamespace(cfg=SimpleNamespace(alpaca_key='fake',alpaca_secret='fake',massive='fake'),get=get,alpaca_headers={})
    return asyncio.run(recover(collector,TICKER,original,'massive',NOW)),calls


def test_zero_snapshot_recovers_real_historical_quote_with_original_clock():
    (quote,report),calls=run({'bid':0,'ask':0,'ts':NOW},history=[
        {'bid_price':0,'ask_price':0,'sip_timestamp':NOW*1e9},
        {'bid_price':1.3,'ask_price':1.41,'sip_timestamp':(NOW-86400)*1e9}])
    assert quote['bid']==1.3 and quote['ask']==1.41 and quote['ts']==NOW-86400
    assert quote['method']=='historical_quote' and report['status']=='recovered'
    assert len(calls)==2


def test_fresh_snapshot_skips_extra_calls():
    (quote,report),calls=run({'bid':1,'ask':1.1,'ts':NOW-10})
    assert report['status']=='not_needed' and calls==[]


def test_freshest_valid_provider_wins_without_relabeling_metadata():
    (quote,report),_=run({'bid':0,'ask':0,'ts':NOW},alpaca={'bp':2,'ap':2.2,'t':NOW-10},history=[
        {'bid_price':1.3,'ask_price':1.41,'sip_timestamp':(NOW-86400)*1e9}])
    assert quote['source']=='alpaca' and quote['ts']==NOW-10 and report['status']=='recovered'


@pytest.mark.parametrize('bid,ask,stamp',[(0,0,NOW),(2,1,NOW),(1,2,NOW+10),(1,2,NOW-8*86400),(1,2,None)])
def test_invalid_history_never_becomes_reference_price(bid,ask,stamp):
    (quote,report),_=run({'bid':0,'ask':0,'ts':NOW},history=[{'bid_price':bid,'ask_price':ask,'sip_timestamp':stamp}])
    assert quote['bid']==0 and report['status']=='no_newer_valid_quote'


def test_provider_failure_preserves_existing_stale_quote_and_hides_secrets():
    (quote,report),_=run({'bid':1,'ask':1.1,'ts':NOW-3600},error=True)
    assert quote['ts']==NOW-3600 and report['status']=='no_newer_valid_quote'
    assert 'secret' not in str(report)
