from copy import deepcopy
from compass.smoothers_scorecard import summarize


def signal(status='WIN', exit_bid=1):
    contract=dict(symbol='DEMO260925C00100000',multiplier=100)
    def q(bid,ask,at):
        return dict(status='available',bid=bid,ask=ask,quote_at_ms=at,
                    quote_age_seconds=1,contract=contract)
    return dict(ticker='DEMO',status=status,contract=contract,resolution_time=2000,
                quote=q(1.9,2,1000000),exit_quote=q(exit_bid,exit_bid+.1,2000000))


def test_target_hit_can_be_option_loss_and_open_quote_is_not_realized():
    r=summarize([signal(),signal('OPEN'),signal('LOSS',3)])
    assert r['underlying_states']=={'WIN':1,'OPEN':1,'LOSS':1}
    assert r['option_states']=={'loss':1,'pending':1,'profit':1}
    assert r['target_hits_with_option_loss']==1 and r['net_pnl']==-2.64


def test_missing_stale_wrong_contract_and_old_exit_quotes_remain_unknown():
    rows=[]
    for edit in ('missing','stale','contract','old','wide'):
        p=deepcopy(signal())
        if edit=='missing':p.pop('exit_quote')
        if edit=='stale':p['exit_quote']['quote_age_seconds']=6
        if edit=='contract':p['exit_quote']['contract']={'symbol':'OTHER'}
        if edit=='old':p['exit_quote']['quote_at_ms']=1900000
        if edit=='wide':p['exit_quote']['status']='wide_spread'
        rows.append(p)
    r=summarize(rows)
    assert r['measured']==0 and r['net_pnl'] is None
    assert r['option_states']=={'unavailable':5}
