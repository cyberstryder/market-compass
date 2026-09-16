from compass.spy_option_audit import choose, next_session, pnl, prior_event, valid_quote
from compass.store import Store


def test_event_must_have_been_received_before_decision():
    db=Store('sqlite://'); db.initialize()
    with db.tx() as c:
        db.append(c, 'matrix_raw', 'test', 'apex_SPY', 100, {})
        assert prior_event(c, 'matrix_raw', 'apex_SPY', 200) is None


def test_exact_expiry_and_standard_contract_before_nearest_strike():
    chain={'contracts': [dict(symbol='good', underlying='SPY', type='call', expiry='2026-09-15', multiplier=100, strike=761),
                         dict(symbol='adjusted', underlying='SPY', type='call', expiry='2026-09-15', multiplier=10, strike=761.4),
                         dict(symbol='wrong_expiry', underlying='SPY', type='call', expiry='2026-09-16', multiplier=100, strike=761.4)]}
    assert choose(chain, 761.4, 1, '2026-09-15')['symbol']=='good'
    assert choose(chain, 761.4, -1, '2026-09-15') is None
    assert next_session('2026-09-04')=='2026-09-08'


def test_quotes_and_one_contract_spread_and_fees():
    q=dict(bid=1.,ask=1.05,bid_size=1,ask_size=1)
    assert valid_quote(q)
    assert not valid_quote({**q,'ask_size':0})
    assert not valid_quote({**q,'ask':.9})
    assert not valid_quote({**q,'ask':2.})
    result=pnl(q,dict(bid=1.5))
    assert abs(result['net_dollars']-43.7)<1e-9
    assert result['debit_dollars']==105
