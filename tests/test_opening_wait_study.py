from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import json

import httpx
import pytest

from compass.market import session
from compass.opening_wait_study import (
    History, WAITS, analyze, daily_risks, normalize, prepare_days, simulate, summary,
)
from compass.store import Store


def day_fixture(day='2026-09-15'):
    opened, closed = session(day)
    rows = [[opened+i*60, 100., 100.2, 99.8, 100., 1000.] for i in range(121)]
    return dict(date=day, open=opened, close=closed, rows=rows, high=101., low=99.,
                risk=1., premarket_bars=60, gap_bucket='small')


def daily_fixture(end='2026-09-15', count=25):
    days = []
    d = date.fromisoformat(end)
    while len(days) < count:
        if session(d.isoformat()):
            days.append(d.isoformat())
        d -= timedelta(days=1)
    return [dict(t=day+'T04:00:00Z', o=100, h=102, l=98, c=100, v=1000) for day in reversed(days)]


def test_first_close_only_and_entry_on_next_bar():
    d = day_fixture()
    d['rows'][4] = [d['open']+240, 100., 101.3, 99.8, 101.2, 1000.]
    d['rows'][5] = [d['open']+300, 101.3, 101.4, 101.1, 101.2, 1000.]
    r = simulate(d, 5, 'minimum_wait')
    assert r['entry_minute'] == 5
    assert r['entry_reference'] == 101.3
    # An intrabar high without a confirmed close is not an entry.
    d['rows'][4][4] = 100.
    assert simulate(d, 5, 'minimum_wait')['entry_minute'] == 6
    # A larger candle cannot use a smaller candle's earlier close.
    assert not simulate(d, 15, 'candle_confirmation')['traded']


def test_wait_and_candle_size_are_separate_policies():
    d = day_fixture()
    d['rows'][5][2] = 101.4
    d['rows'][5][4] = 101.2
    assert simulate(d, 5, 'minimum_wait')['entry_minute'] == 6
    assert not simulate(d, 5, 'candle_confirmation')['traded']


def test_future_bars_cannot_change_entry_decision():
    d = day_fixture()
    d['rows'][4][2:5] = [101.4, 99.8, 101.2]
    d['rows'][5][1] = 101.3
    a = simulate(d, 5, 'minimum_wait')
    for row in d['rows'][10:]:
        row[1:5] = [200., 201., 199., 200.]
    b = simulate(d, 5, 'minimum_wait')
    assert (a['entry_minute'], a['entry_reference'], a['side']) == (b['entry_minute'], b['entry_reference'], b['side'])


def test_stop_gap_and_ambiguous_path_bounds():
    d = day_fixture()
    d['rows'][2][2:5] = [101.4, 99.8, 101.2]
    d['rows'][3][1:5] = [102., 102.1, 101.5, 102.]
    d['rows'][4][1:5] = [100., 100.2, 99.8, 100.]
    gap = simulate(d, 3, 'minimum_wait')
    assert gap['exit_reason'] == 'stop_gap' and gap['net_R'] < -2
    d['rows'][3][1:5] = [102., 104.1, 100.9, 102.]
    bounded = simulate(d, 3, 'minimum_wait')
    assert bounded['exit_reason'] == 'same_bar_ambiguous'
    assert bounded['net_R'] < -1 and 1.9 < bounded['upper_R'] < 2
    assert bounded['cost_R']['2'] < bounded['cost_R']['0.5']


def test_short_target_and_gap_conservative_limit():
    d = day_fixture()
    d['rows'][2][2:5] = [100.2, 98.5, 98.8]
    d['rows'][3][1:5] = [98.7, 98.8, 98.6, 98.7]
    d['rows'][4][1:5] = [95., 95.1, 94.9, 95.]
    r = simulate(d, 3, 'minimum_wait')
    assert r['side'] == -1 and r['exit_reason'] == 'target_gap_limit'
    assert 1.9 < r['net_R'] < 2


def test_risk_uses_only_prior_days_and_missing_daily_history_is_excluded():
    raw = daily_fixture()
    old = daily_risks(raw)['2026-09-15']
    raw[-1].update(h=200, l=1, c=150)
    assert daily_risks(raw)['2026-09-15'] == old
    raw.pop(-3)
    assert '2026-09-15' not in daily_risks(raw)


def test_common_cohort_premarket_session_boundaries_and_missing_minutes():
    d = day_fixture()
    pre = [[d['open']-i*60, 100., 101., 99., 100., 100.] for i in range(1, 61)]
    outside = [d['open']-6*3600, 100., 500., 1., 100., 100.]
    days, excluded = prepare_days(d['rows']+pre+[outside], daily_fixture(), d['date'], d['date'])
    assert not excluded and days[0]['high'] == 101 and days[0]['low'] == 99
    assert len(days[0]['rows']) == 121
    days, excluded = prepare_days(d['rows'][:50]+d['rows'][51:]+pre, daily_fixture(), d['date'], d['date'])
    assert not days and excluded[0]['missing_regular_minutes'] == 1


def test_duplicate_conflicts_and_invalid_bars_fail():
    r = dict(t='2026-09-15T13:30:00Z', o=100, h=101, l=99, c=100, v=100)
    assert len(normalize([r, r])) == 1
    with pytest.raises(ValueError, match='conflicting'):
        normalize([r, {**r, 'c': 100.1}])
    with pytest.raises(ValueError, match='invalid'):
        normalize([{**r, 'h': 98}])


def test_no_trade_days_and_missed_early_moves_are_counted():
    d = day_fixture()
    d['rows'][2][2:5] = [101.4, 99.8, 101.2]
    d['rows'][3][1:5] = [101.3, 102.5, 101.2, 101.4]
    early = simulate(d, 3, 'minimum_wait')
    late = simulate(d, 15, 'minimum_wait')
    s = summary([late], {d['date']: early})
    assert s['sessions'] == s['no_trade'] == s['early_move_sessions'] == s['missed_early_moves'] == 1
    assert s['mean_net_R_per_session'] == 0 and s['mean_net_R_per_trade'] is None


def test_held_out_outcomes_cannot_change_selected_wait():
    days = []
    current = date(2026, 1, 2)
    while len(days) < 70:
        if session(current.isoformat()):
            d = day_fixture(current.isoformat())
            d['rows'][2][2:5] = [101.4, 99.8, 101.2]
            d['rows'][3][1:5] = [101.3, 103.4, 101.2, 103.2]
            days.append(d)
        current += timedelta(days=1)
    split = days[50]['date']
    a, rows = analyze(days, split)
    for d in days[50:]:
        for row in d['rows']:
            row[1:5] = [98., 98.2, 97.8, 98.]
    b, _ = analyze(days, split)
    for p in a:
        assert a[p]['selected_on_development'] == b[p]['selected_on_development']
        assert a[p]['development'] == b[p]['development']
        assert all(v['sessions'] == 20 for v in a[p]['validation'].values())
    assert len(rows) == 70*2*len(WAITS)


def test_history_paginates_and_reuses_compressed_evidence(tmp_path):
    db = Store('sqlite:///'+str(tmp_path/'study.db'))
    db.initialize()
    history = History(db, 'test-key', 'test-secret')
    calls = []
    def respond(request):
        calls.append(dict(request.url.params))
        second = 'page_token' in request.url.params
        return httpx.Response(200, json={'bars': {'SPY': [dict(t='2026-09-15T13:'+('31' if second else '30')+':00Z', o=100, h=101, l=99, c=100, v=100)]},
                                         'next_page_token': None if second else 'page2'})
    history.client.close()
    history.client = httpx.Client(transport=httpx.MockTransport(respond))
    first = history.fetch('1Min', '2026-09-15', '2026-09-16')
    assert len(first) == 2 and len(calls) == 2 and calls[1]['page_token'] == 'page2'
    assert history.fetch('1Min', '2026-09-15', '2026-09-16') == first and len(calls) == 2
    history.client.close()
    db.engine.dispose()
