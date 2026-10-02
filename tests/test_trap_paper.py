import pytest
from datetime import datetime, timezone
from datetime import date, timedelta
from compass.store import Store
from compass import trap_paper as tp

NOW = datetime(2026, 10, 2, 21, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'tp.db'))
    db.initialize()
    yield db
    db.engine.dispose()


class Cfg:
    trap_paper = True
    paper_trading = True


class CfgOff:
    trap_paper = False
    paper_trading = True


def B(o, h, l, c, day='2026-09-25'):
    return {'o': o, 'h': h, 'l': l, 'c': c, 'v': 1_000_000, 'day': day}


def trap_event(kind='bear_trap', symbol='AAA', entry_day='2026-10-02'):
    return {'id': 'trap:%s:2026-10-01:%s' % (symbol, kind), 'symbol': symbol,
            'kind': kind, 'day': '2026-10-01', 'entry_day': entry_day,
            'direction': 'up' if kind == 'bear_trap' else 'down',
            'status': 'fresh', 'sessions_since_break': 1}


def test_paper_enabled_gating():
    assert tp.paper_enabled(Cfg())
    assert not tp.paper_enabled(CfgOff())
    assert not tp.paper_enabled(type('X', (), {'trap_paper': True,
                                               'paper_trading': False})())


def test_pick_expiry_is_friday_in_window():
    for entry in ['2026-10-02', '2026-10-05', '2026-10-09']:
        exp = tp._pick_expiry(entry)
        d = date.fromisoformat(exp)
        assert d.weekday() == 4, exp  # Friday
        dte = (d - date.fromisoformat(entry)).days
        assert 14 <= dte <= 21, (entry, exp)


def test_submit_bear_trap_is_bull_put(db):
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        res = tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap'), 100.0, bars)
    assert res['submitted'], res
    assert res['price_source'] == 'bs_model'  # no chain in test db
    with db.tx() as c:
        pos = db.get(c, 'trap_spread:AAA:2026-10-02')
    assert pos['spread_kind'] == 'bull_put'
    assert pos['short_strike'] == 100.0
    assert pos['long_strike'] == 95.0  # 5% OTM
    assert pos['qty'] == 1
    assert pos['credit'] > 0
    assert pos['status'] == 'open'


def test_submit_bull_trap_is_bear_call(db):
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        res = tp.submit(db, c, Cfg(), NOW, trap_event('bull_trap'), 100.0, bars)
    assert res['submitted'], res
    with db.tx() as c:
        pos = db.get(c, 'trap_spread:AAA:2026-10-02')
    assert pos['spread_kind'] == 'bear_call'
    assert pos['long_strike'] == 105.0


def test_submit_rejects_duplicates_and_limits(db):
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        r1 = tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap', 'AAA'), 100.0, bars)
        assert r1['submitted']
        # same symbol again -> rejected
        r2 = tp.submit(db, c, Cfg(), NOW, trap_event('bull_trap', 'AAA',
                                                  '2026-10-03'), 100.0, bars)
        assert not r2['submitted'] and r2['reason'] == 'existing_position'
        # fill to the cap
        for sym in ('BBB', 'CCC'):
            r = tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap', sym), 100.0, bars)
            assert r['submitted'], (sym, r)
        # fourth -> rejected
        r4 = tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap', 'DDD'), 100.0, bars)
        assert not r4['submitted'] and r4['reason'] == 'max_positions'


def test_submit_disabled(db):
    with db.tx() as c:
        bars = [B(100, 101, 99, 100) for _ in range(25)]
        res = tp.submit(db, c, CfgOff(), NOW, trap_event(), 100.0, bars)
    assert not res['submitted'] and res['reason'] == 'paper_disabled'


def test_settle_winner_keeps_credit(db):
    # Bull put spread: underlying rallies above short strike -> keep credit.
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        res = tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap'), 100.0, bars)
        assert res['submitted']
        pos = db.get(c, 'trap_spread:AAA:2026-10-02')
        expiry = pos['expiry']
        settled = tp.settle(db, c, Cfg(), NOW, expiry,
                            lambda s, d: 110.0)  # well above short strike
    assert len(settled) == 1
    pnl = settled[0]['pnl']
    # profit ~= credit*100 - fees
    assert pnl > 0
    assert abs(pnl - (pos['credit'] * 100 - tp.FEE_PER_SPREAD)) < 0.01
    with db.tx() as c:
        assert db.get(c, 'trap_spread:AAA:2026-10-02')['status'] == 'closed'


def test_settle_loser_pays_max_loss(db):
    # Bull put spread: underlying crashes below long strike -> max loss.
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        res = tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap'), 100.0, bars)
        pos = db.get(c, 'trap_spread:AAA:2026-10-02')
        expiry = pos['expiry']
        settled = tp.settle(db, c, Cfg(), NOW, expiry, lambda s, d: 80.0)
    assert len(settled) == 1
    pnl = settled[0]['pnl']
    assert pnl < 0
    # loss ~= -(width - credit)*100 - fees
    expected = -((pos['width'] - pos['credit']) * 100 + tp.FEE_PER_SPREAD)
    assert abs(pnl - expected) < 0.01


def test_settle_skips_before_expiry(db):
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap'), 100.0, bars)
        settled = tp.settle(db, c, Cfg(), NOW, '2026-10-03', lambda s, d: 110.0)
    assert settled == []
    with db.tx() as c:
        assert db.get(c, 'trap_spread:AAA:2026-10-02')['status'] == 'open'


def test_intrinsic_value_math():
    put_spread = {'spread_kind': 'bull_put', 'short_strike': 100.0,
                  'width': 5.0}
    assert tp._intrinsic_value(put_spread, 110.0) == 0.0
    assert tp._intrinsic_value(put_spread, 97.5) == 2.5
    assert tp._intrinsic_value(put_spread, 90.0) == 5.0
    call_spread = {'spread_kind': 'bear_call', 'short_strike': 100.0,
                   'width': 5.0}
    assert tp._intrinsic_value(call_spread, 90.0) == 0.0
    assert tp._intrinsic_value(call_spread, 102.5) == 2.5
    assert tp._intrinsic_value(call_spread, 110.0) == 5.0


def test_attribution(db):
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        e1 = trap_event('bear_trap', 'AAA', '2026-10-02')
        e2 = trap_event('bull_trap', 'BBB', '2026-10-05')
        tp.submit(db, c, Cfg(), NOW, e1, 100.0, bars)
        tp.submit(db, c, Cfg(), NOW, e2, 100.0, bars)
        pos_a = db.get(c, 'trap_spread:AAA:2026-10-02')
        pos_b = db.get(c, 'trap_spread:BBB:2026-10-05')
        assert pos_a['expiry'] != pos_b['expiry']
        tp.settle(db, c, Cfg(), NOW, pos_a['expiry'], lambda s, d: 110.0)
        # BBB bear call spread wins when underlying falls below short strike
        tp.settle(db, c, Cfg(), NOW, pos_b['expiry'], lambda s, d: 90.0)
        attr = tp.attribution(db, c)
    assert attr['trades'] == 2
    assert attr['wins'] == 2
    assert attr['losses'] == 0
    assert attr['realized'] > 0


def test_mark_open_writes_snapshot(db):
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        res = tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap'), 100.0, bars)
        assert res['submitted'], res
        snaps = tp.mark_open(db, c, Cfg(), NOW, '2026-10-05',
                             lambda s, d: 100.0, lambda s: bars)
    assert len(snaps) == 1
    s = snaps[0]
    assert s['trade_id'] == 'trap-AAA-2026-10-02'
    assert s['day'] == '2026-10-05'
    assert s['underlying'] == 100.0
    assert s['dte_remaining'] > 0
    assert s['mark'] >= 0
    assert s['max_profit'] > 0
    assert s['price_source'] in ('chain', 'bs_model')


def test_mark_open_skips_when_no_close(db):
    with db.tx() as c:
        bars = [B(100, 101, 99, 100,
                  day=(date(2026, 9, 1) + timedelta(days=i)).isoformat())
                for i in range(25)]
        res = tp.submit(db, c, Cfg(), NOW, trap_event('bear_trap'), 100.0, bars)
        assert res['submitted'], res
        snaps = tp.mark_open(db, c, Cfg(), NOW, '2026-10-05',
                             lambda s, d: None, lambda s: bars)
    assert snaps == []


def test_exit_study_compares_rules(db):
    with db.tx() as c:
        trade = {'id': 'trap-BBB-2026-09-01', 'strategy': 'trap-spread',
                 'symbol': 'BBB', 'status': 'closed', 'pnl': -50.0,
                 'credit': 1.0, 'credit_total': 100.0}
        db.put(c, 'trade:trap-BBB-2026-09-01', trade)
        # Trail: profitable early (take_50 triggers day 2), then faded.
        for day, unreal, dte in [('2026-09-02', 30.0, 15),
                                 ('2026-09-03', 60.0, 14),
                                 ('2026-09-04', 10.0, 13)]:
            db.append(c, 'paper_decision', 'trap_paper', 'BBB', NOW,
                      {'trade_id': 'trap-BBB-2026-09-01', 'day': day,
                       'unrealized_pnl': unreal, 'max_profit': 100.0,
                       'max_loss': 400.0, 'dte_remaining': dte,
                       'status': 'mtm'},
                      'mtm:trap-BBB-2026-09-01:' + day)
        # Closed trade with no MTM history -> baseline only.
        db.put(c, 'trade:trap-CCC-2026-09-01',
               {'id': 'trap-CCC-2026-09-01', 'strategy': 'trap-spread',
                'symbol': 'CCC', 'status': 'closed', 'pnl': 20.0,
                'credit': 1.0, 'credit_total': 100.0})
        study = tp.exit_study(db, c)
    assert study['hold_to_expiry']['trades'] == 2
    assert study['hold_to_expiry']['total_pnl'] == -30.0
    # Early-exit rules cover only the trade with an MTM trail (BBB).
    assert study['take_50']['trades'] == 1
    assert study['take_50']['total_pnl'] == 60.0
    assert study['take_25']['total_pnl'] == 30.0
    assert study['exit_7dte']['total_pnl'] == -50.0  # never triggered: held
    assert study['stop_50']['total_pnl'] == -50.0    # never triggered: held
    assert study['baseline_only'] == 1
    assert study['take_50']['wins'] == 1


def test_exit_study_empty(db):
    with db.tx() as c:
        study = tp.exit_study(db, c)
    assert study['hold_to_expiry']['trades'] == 0
    assert study['baseline_only'] == 0
