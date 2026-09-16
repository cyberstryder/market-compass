from copy import deepcopy
from compass.zero_dte_timing import contract_spec, contract_valid, primary_exit, price_quote, analyze, WAITS, return_stats


def test_same_day_contract_exact_terms_and_half_strike_tie():
    s={'date':'2026-09-15','entry_reference':760.5,'side':-1}
    spec=contract_spec(s)
    assert spec['ticker']=='O:SPY260915P00760000'
    assert contract_valid(spec,spec)
    assert not contract_valid({**spec,'expiration_date':'2026-09-16'},spec)
    assert not contract_valid({**spec,'shares_per_contract':10},spec)
    assert not contract_valid({**spec,'additional_underlyings':[{'amount':1}]},spec)


def test_stop_requires_completed_close_not_intrabar_extreme():
    d={'open':10000,'rows':[[10000+i*60,100,105,95,100,1] for i in range(121)]}
    s={'entry_reference':100,'risk_dollars_per_share':1,'side':1,'entry_minute':5}
    assert primary_exit(d,s)==(17200,'time')
    d['rows'][9][4]=98
    assert primary_exit(d,s)==(10600,'underlying_close_stop')
    d['rows'][9][4]=102
    assert primary_exit(d,s)==(10600,'underlying_close_target')


def test_quote_time_wide_first_market_and_zero_bid_exit():
    q={'sip_timestamp':101_000_000_000_000_000,'bid_price':1,'ask_price':1.1,'bid_size':2,'ask_size':3}
    boundary=101_000_000-1
    assert price_quote({'results':[q]},boundary,True)['status']=='available'
    assert price_quote({'results':[q]},boundary+2,True)['status']=='missing_quote'
    wide={**q,'bid_price':.1}
    assert price_quote({'results':[wide,q]},boundary,True)['status']=='entry_liquidity_filter'
    zero={**q,'bid_price':0,'bid_size':0}
    r=price_quote({'results':[zero]},boundary,False)
    assert r['status']=='available' and r['quote']['zero_bid_writeoff']
    assert price_quote({'results':[zero]},boundary,True)['status']=='entry_liquidity_filter'


def records_fixture():
    out=[]
    from datetime import date,timedelta
    for i in range(70):
        d=(date(2025,1,1)+timedelta(days=i)).isoformat()
        for n in WAITS:
            out.append({'date':d,'wait':n,'status':'traded','entry':{'ask':1},
                'primary_exit':{'bid':2 if n==10 else 1,'zero_bid_writeoff':False},
                'fixed_exit':{'bid':1,'zero_bid_writeoff':False}})
    for n in WAITS:
        out.append({'date':'2026-04-01','wait':n,'status':'no_signal'})
    return out


def test_validation_cannot_change_selection_missing_excludes_whole_day():
    rows=records_fixture()
    before=analyze(rows)
    assert before['selected_on_development']==10
    rows[-1]={'date':'2026-04-01','wait':30,'status':'missing_quote'}
    after=analyze(rows)
    assert after['selected_on_development']==10
    assert after['excluded_dates']==['2026-04-01']
    assert all(x['sessions']==0 for x in after['validation'].values())


def test_fee_return_and_no_trade_denominator():
    r={'status':'traded','entry':{'ask':1},'primary_exit':{'bid':0,'zero_bid_writeoff':True}}
    out=return_stats([r,{'status':'no_signal'}],'primary_exit')
    assert out['net_dollars_one_contract']==-101.3
    assert out['zero_bid_exits']==1 and out['sessions']==2 and out['trades']==1
    assert out['mean_return_per_session']==out['mean_return_per_trade']/2
    assert out['fee_sensitivity_return_per_session']['0']>out['fee_sensitivity_return_per_session']['0.65']
