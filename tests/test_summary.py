"""Summary-page aggregation: bucketing of taken trades and skip reasons."""
from datetime import datetime, timezone

from compass.store import Store
from compass.futures import risk_day
from compass import summary

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()  # Mon 12:00 CT
DAY = risk_day(NOW)


def trade(**kw):
    base = {'id': 't', 'symbol': 'SPY', 'side': 'long', 'strategy': 'x',
            'asset': 'option', 'qty': 1, 'entry': 1.0, 'status': 'closed',
            'pnl': 0.0, 'entered_at': NOW - 3600, 'exited_at': NOW - 1800,
            'risk_day': DAY}
    base.update(kw)
    return base


def test_summary_buckets_taken_and_skipped(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'summary.db'))
    s.initialize()
    with s.tx() as c:
        s.put(c, 'trade:a', trade(id='a', strategy='0dte-underlying-orb-v2',
                                  symbol='SPY', pnl=25.0))
        s.put(c, 'trade:b', trade(id='b', strategy='0dte-underlying-orb-v2',
                                  symbol='QQQ', status='open', pnl=None,
                                  exited_at=None))
        s.put(c, 'trade:c', trade(id='c', strategy='compass-scanner-orb',
                                  asset='stock', symbol='AAPL', pnl=-10.0,
                                  track='intraday'))
        s.put(c, 'trade:d', trade(id='d', strategy='swing20-v1',
                                  asset='stock', symbol='MSFT', pnl=5.0,
                                  track='swing'))
        s.put(c, 'trade:e', trade(id='e', strategy='golden_zone_break',
                                  asset='future', symbol='NQ.c.0', pnl=50.0,
                                  side='short'))
        s.put(c, 'trade:old', trade(id='old', strategy='0dte-underlying-orb-v2',
                                    risk_day='2000-01-01', status='closed',
                                    pnl=99.0))
        s.append(c, 'paper_decision', 'engine', 'SPY', NOW - 600,
                 {'strategy': '0dte-underlying-orb-v2', 'status': 'skipped',
                  'reason': 'No eligible 0DTE contract'}, 'skip:1')
        s.append(c, 'paper_decision', 'engine', 'AAPL', NOW - 500,
                 {'strategy': 'compass-scanner-orb', 'track': 'intraday',
                  'status': 'skipped', 'reason': 'Stale quote'}, 'skip:2')
        # Engine option-selection rejects keep the underlying signal's
        # strategy (no '0dte' prefix) and track 'intraday'; the
        # `options_skipped` status must still route them to the 0DTE bucket.
        s.append(c, 'paper_decision', 'engine', 'TSLA', NOW - 400,
                 {'strategy': 'underlying-orb-v2', 'track': 'intraday',
                  'status': 'options_skipped',
                  'reason': 'No recent options chain'}, 'skip:3')
        out = summary.build(s, c, NOW)
    assert out['day'] == DAY
    z = out['zero_dte']
    assert {t['symbol'] for t in z['trades']} == {'SPY', 'QQQ'}
    assert z['wins'] == 1 and z['open'] == 1 and z['net_pnl'] == 25.0
    assert len(z['skipped']) == 2
    assert {x['symbol'] for x in z['skipped']} == {'SPY', 'TSLA'}
    assert z['skipped'][0]['symbol'] == 'TSLA'  # newest first
    i = out['intraday']
    assert [t['symbol'] for t in i['trades']] == ['AAPL']
    assert i['losses'] == 1
    assert len(i['skipped']) == 1 and i['skipped'][0]['reason'] == 'Stale quote'
    f = out['futures']
    assert [t['symbol'] for t in f['trades']] == ['NQ.c.0']
    assert f['strategies'][0]['strategy'] == 'golden_zone_break'
    assert f['strategies'][0]['short'] == 1
    assert f['strategies'][0]['realized'] == 50.0
    s.engine.dispose()
