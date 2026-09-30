"""Daily status page aggregation: one trading day across every section."""
from datetime import datetime, timezone

import pytest

from compass.store import Store
from compass.futures import risk_day
from compass import daily_status

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()  # Mon 12:00 CT
DAY = '2026-09-28'


def trade(**kw):
    base = {'id': 't', 'symbol': 'SPY', 'side': 'long', 'strategy': 'x',
            'asset': 'option', 'qty': 1, 'entry': 1.0, 'status': 'closed',
            'pnl': 0.0, 'entered_at': NOW - 3600, 'exited_at': NOW - 1800,
            'risk_day': risk_day(NOW)}
    base.update(kw)
    return base


def seed(c, s):
    s.put(c, 'trade:a', trade(id='a', strategy='0dte-underlying-orb-v2',
                              symbol='SPY', pnl=25.0))
    s.put(c, 'trade:old', trade(id='old', strategy='0dte-underlying-orb-v2',
                                risk_day='2000-01-01', status='closed',
                                pnl=99.0))
    s.put(c, 'trade:f', trade(id='f', strategy='golden_zone_break',
                              asset='future', symbol='MNQ.c.0', pnl=50.0,
                              side='short'))
    s.append(c, 'apex_magnet_signal', 'apex_magnet', 'WFC', NOW - 900,
             {'signal': 'broke_through', 'spot': 80.18, 'magnet': 80.0}, 'sig:1')
    s.append(c, 'apex_magnet_signal', 'apex_magnet', 'TSLA', NOW - 26 * 3600,
             {'signal': 'approaching', 'spot': 353.0, 'magnet': 360.0}, 'sig:2')
    s.append(c, 'tape_confirmation', 'tape_confirmed', 'NVDA', NOW - 700,
             {'premium': 300000}, 'tape:1')
    s.append(c, 'alert', 'engine', 'SPY', NOW - 500,
             {'strategy': '0dte-underlying-orb-v2'}, 'alert:1')
    s.append(c, 'pick_check', 'pickcheck', 'META', NOW - 400,
             {'direction': 'short'}, 'pc:1')
    s.put(c, 'board:' + DAY, {'day': DAY, 'built_at': NOW - 7200,
                              'frozen': True, 'universe': 15,
                              'rows': [{'symbol': 'AAPL', 'total': 82.5,
                                        'day_pct': 0.012}]})
    s.put(c, 'gap_cont:G1', {'day': DAY, 'qualified': True, 'symbol': 'MU',
                             'gap_pct': 0.025, 'direction': 'up'})
    s.put(c, 'breakout:B1', {'day': DAY, 'status': 'fresh', 'symbol': 'AMD',
                             'pattern': 'range_break', 'direction': 'long'})


def test_daily_status_aggregates_one_day(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'daily.db'))
    s.initialize()
    with s.tx() as c:
        seed(c, s)
        d = daily_status.build(s, c, None, NOW, DAY)
    assert d['day'] == DAY
    z = d['paper']['zero_dte']
    assert z['taken'] == 1 and z['net_pnl'] == 25.0  # old-day trade excluded
    f = d['paper']['futures']
    assert f['taken'] == 1 and f['net_pnl'] == 50.0
    sc = d['scanners']
    assert sc['apex_signals'] == 1  # yesterday's signal excluded
    assert sc['apex_by_state'] == {'broke_through': 1}
    assert sc['apex_broke_through'][0]['symbol'] == 'WFC'
    assert sc['apex_broke_through'][0]['level'] == 80.0
    assert sc['tape_confirmations'] == 1
    assert sc['gaps_qualified'] == 1 and sc['gaps'][0]['symbol'] == 'MU'
    assert sc['breakouts_fresh'] == 1
    assert d['board']['built'] and d['board']['frozen']
    assert d['board']['top'][0]['symbol'] == 'AAPL'
    assert d['alerts']['sent'] == 1
    assert d['pick_checks']['count'] == 1
    assert d['pick_checks']['tickers'] == ['META']
    assert d['smoothers']['week'] == '2026-09-28'


def test_daily_status_defaults_to_today(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'daily2.db'))
    s.initialize()
    with s.tx() as c:
        d = daily_status.build(s, c, None, NOW, None)
    assert d['day'] == DAY
    assert d['paper']['zero_dte']['taken'] == 0


def test_daily_status_rejects_bad_day(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'daily3.db'))
    s.initialize()
    with s.tx() as c:
        with pytest.raises(ValueError):
            daily_status.build(s, c, None, NOW, 'not-a-day')


def test_historical_day_excludes_current_open_trades(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'daily4.db'))
    s.initialize()
    with s.tx() as c:
        seed(c, s)
        # A position opened today and still open: summary.build always
        # includes it, but a historical Daily page must not show it.
        s.put(c, 'trade:live', trade(id='live', strategy='0dte-underlying-orb-v2',
                                     symbol='QQQ', status='open', pnl=None,
                                     exited_at=None))
        past = daily_status.build(s, c, None, NOW, '2026-09-21')
    z = past['paper']['zero_dte']
    assert z['taken'] == 0 and z['open'] == 0 and z['trades'] == []
    assert past['smoothers']['week'] == '2026-09-21'
    # The current day still shows the open position.
    with s.tx() as c:
        cur = daily_status.build(s, c, None, NOW, DAY)
    assert cur['paper']['zero_dte']['open'] == 1
