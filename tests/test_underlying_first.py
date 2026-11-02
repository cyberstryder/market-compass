"""Underlying-first measurement for the swing and 0DTE books (2026-11-01).

Both books used to produce an observation only when a streamed option quote
arrived. These tests pin the replacement: every candidate is scored on the
underlying's stored 1-minute bars, independent of option quotes, and the
option path stays a separate optional measurement.
"""
import pytest
from sqlalchemy import select
from compass.config import Config
from compass.store import Store, swing_trials as trials, swing_checkpoints as checkpoints
from compass import underlying_path as up
from compass import scanner_shadow
from compass.swing_study import register, observe, score_path
from compass.swing_ideas import SwingIdeas, swings
from test_swing_ideas import NOW, signal, quote, daily, stamp, flow, history
from compass.swing_signals import daily_context
from test_swing_study import event, seed, snapshot


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'uf.db')); db.initialize()
    yield db
    db.engine.dispose()


def bar(ts, o, h, l, c):
    return dict(ts=ts, o=o, h=h, l=l, c=c)


# ---------------------------------------------------------------- pure helpers

def test_bracket_stop_first_when_both_touch_in_one_bar():
    bars = [bar(0, 100, 100.5, 99.5, 100.2), bar(60, 100.2, 103, 98, 101)]
    r = up.bracket(bars, 0, 100.01, 'long', 99, 102.03)
    assert r['reason'] == 'stop' and r['exit'] == pytest.approx(98.99) and r['R'] == pytest.approx(-1.0099, abs=1e-3)


def test_bracket_gap_through_stop_fills_at_open_and_target_at_level():
    bars = [bar(0, 100, 100.5, 99.5, 100.2), bar(60, 97, 97.5, 96.5, 97.2)]
    r = up.bracket(bars, 0, 100.01, 'long', 99, 102.03)
    assert r['reason'] == 'stop' and r['exit'] == pytest.approx(96.99)
    bars = [bar(0, 100, 100.5, 99.5, 100.2), bar(60, 100.2, 102.5, 100.1, 102.4)]
    r = up.bracket(bars, 0, 100.01, 'long', 99, 102.03)
    assert r['reason'] == 'target' and r['exit'] == pytest.approx(102.02) and r['R'] == pytest.approx(2.0, abs=.01)
    s = up.bracket(bars, 0, 99.99, 'short', 101, 97.97)
    assert s['reason'] == 'stop'


def test_bracket_time_stop_and_incomplete_window():
    bars = [bar(t, 100, 100.3, 99.8, 100.1) for t in range(0, 600, 60)]
    done = up.bracket(bars, 0, 100.01, 'long', 99, 102.03, end=600)
    assert done['reason'] == 'time' and done['exit'] == pytest.approx(100.09)
    short = up.bracket(bars, 0, 100.01, 'long', 99, 102.03, end=3600)
    assert short['reason'] == 'incomplete'
    assert up.bracket(bars, 0, 100.01, 'long', 101, 102, end=600)['reason'] == 'invalid_geometry'


def test_price_at_uses_completed_bar_within_five_minutes_and_entry_is_next_bar_open():
    bars = [bar(t, 100 + t / 600, 100.5, 99.5, 100 + t / 600) for t in range(0, 1200, 60)]
    assert up.price_at(bars, 660) == pytest.approx(bars[10]['c'])  # bar at 600 completes at 660
    assert up.price_at(bars, 659) == pytest.approx(bars[9]['c'])
    assert up.price_at(bars, 1260 + 301) is None  # last bar completed at 1260
    i, entry = up.entry_from(bars, 125, 'long')
    assert i == 3 and entry == pytest.approx(bars[3]['o'] + .01)
    assert up.horizon_return(bars, 0, 100.0, 'short', 6) == pytest.approx(-(bars[6]['o'] / 100 - 1) * 1e4)
    assert up.horizon_return(bars, 0, 100.0, 'long', 60) is None


# ---------------------------------------------------------------- swing study

def test_candidate_without_fresh_quote_is_measured_from_signal_bar_close(db, monkeypatch):
    monkeypatch.setattr('compass.store.time.time', lambda: NOW)
    with db.tx() as c:
        db.append(c, 'daily', 'alpaca', 'SPY', stamp(daily()['through'], 0, 0), dict(c=100))  # no quote at all
        register(db, c, event(db, c), NOW, snapshot=snapshot())
        saved = c.execute(select(trials)).mappings().one()
        assert saved['status'] == 'pending'
        p = saved['payload']
        assert p['entry_mid'] == 100 and p['entry_source'] == 'signal_bar_close' and p['entry_quote'] is None
        assert p['entry_reason'] is None and 'missing_fresh_candidate_quote' in p['entry_note'] and p['first_of_day'] is True
        rows = c.execute(select(checkpoints.c.status)).scalars().all()
        assert rows and set(rows) == {'pending'} and len(rows) == 6
        # a second fire of the same rule/side/symbol/day is not first_of_day
        register(db, c, event(db, c, ident='two', at=NOW + 300), NOW + 300, snapshot=snapshot())
        second = c.execute(select(trials).where(trials.c.source_id != saved['source_id'])).mappings().one()
        assert second['payload']['first_of_day'] is False


def test_fresh_quote_still_preferred_as_reference(db, monkeypatch):
    monkeypatch.setattr('compass.store.time.time', lambda: NOW)
    with db.tx() as c:
        seed(db, c)
        register(db, c, event(db, c), NOW, snapshot=snapshot())
        p = c.execute(select(trials)).mappings().one()['payload']
        assert p['entry_source'] == 'quote_mid' and p['entry_mid'] == pytest.approx(100.01) and p['entry_note'] is None


@pytest.mark.parametrize('side', ['long', 'short'])
def test_checkpoint_falls_back_to_completed_bar_close(db, monkeypatch, side):
    clock = [NOW]; monkeypatch.setattr('compass.store.time.time', lambda: clock[0])
    with db.tx() as c:
        seed(db, c); register(db, c, event(db, c, side=side), NOW, snapshot=snapshot())
        target = c.execute(select(checkpoints.c.target_at).where(checkpoints.c.horizon == '60m')).scalar_one()
        clock[0] = target - 30
        db.append(c, 'bar', 'fixture', 'SPY', target - 120, dict(o=100.5, h=101.2, l=100.4, c=101, v=10))
        assert observe(db, c, target + 600) == 1
        cp = dict(c.execute(select(checkpoints).where(checkpoints.c.horizon == '60m')).mappings().one())
        assert cp['status'] == 'completed' and cp['reason'] == 'bar_close_reference' and cp['price'] == 101
        expected = (101 / 100.01 - 1) * 100 * (1 if side == 'long' else -1)
        assert cp['directional_return_pct'] == pytest.approx(expected)


def test_checkpoint_stays_unavailable_without_bars_or_with_stale_bar(db, monkeypatch):
    clock = [NOW]; monkeypatch.setattr('compass.store.time.time', lambda: clock[0])
    with db.tx() as c:
        seed(db, c); register(db, c, event(db, c), NOW, snapshot=snapshot())
        target = c.execute(select(checkpoints.c.target_at).where(checkpoints.c.horizon == '60m')).scalar_one()
        db.append(c, 'bar', 'fixture', 'SPY', target - 900, dict(o=100.5, h=101.2, l=100.4, c=101, v=10))  # completed 14 min early
        assert observe(db, c, target + 600) == 1
        cp = dict(c.execute(select(checkpoints).where(checkpoints.c.horizon == '60m')).mappings().one())
        assert cp['status'] == 'unavailable' and cp['reason'] == 'no_timely_usable_checkpoint_quote'


def test_score_path_records_underlying_bracket_on_the_trial(db, monkeypatch):
    monkeypatch.setattr('compass.store.time.time', lambda: NOW)
    with db.tx() as c:
        seed(db, c); register(db, c, event(db, c), NOW, snapshot=snapshot())
        row = c.execute(select(trials)).mappings().one()
        # signal: long, price 100, stop 96, target 108. Bars: entry bar, drift, then target hit.
        for k in range(0, 40):
            db.append(c, 'bar', 'fixture', 'SPY', NOW + 60 * k, dict(o=100 + k * .05, h=100.3 + k * .05, l=99.9 + k * .05, c=100.1 + k * .05, v=5))
        db.append(c, 'bar', 'fixture', 'SPY', NOW + 60 * 40, dict(o=102, h=108.5, l=101.9, c=108.2, v=5))
        result = score_path(db, c, 'SPY', 'long', row['payload'], row['id'])
        assert result['status'] == 'complete' and result['reason'] == 'target'
        assert result['entry'] == pytest.approx(100.01) and result['exit'] == pytest.approx(107.99)
        assert result['R'] == pytest.approx((107.99 - 100.01) / (100.01 - 96), abs=1e-3)
        assert result['horizon_bps']['30'] == pytest.approx((101.5 / 100.01 - 1) * 1e4, abs=.1) and result['horizon_bps']['120'] is None
        saved = c.execute(select(trials)).mappings().one()['payload']['underlying_path']
        assert saved['reason'] == 'target' and saved['status'] == 'complete'


def breakout_setup(db, c):
    """Daily history + a completed-minute cross of the 20-session high: a
    daily_breakout long signal at NOW (same construction as test_swing_ideas)."""
    rows = history(); context = daily_context(rows, NOW)
    price = context['high20'] + .1
    db.put(c, 'matrix:unusual_activity', dict(source_ts=NOW, received=NOW))
    for row in rows:
        db.append(c, 'daily', 'alpaca', 'SPY', row['ts'], row['payload'])
    db.put(c, 'scanner_features:SPY', dict(status='ready', asof=NOW, bar_start=NOW - 60,
                                           previous_bar=dict(c=context['high20'] - .1), bar=dict(c=price)))
    db.put(c, 'quote:SPY', quote(NOW, price, price + .02))


# ---------------------------------------------------------------- swing cell switches

def test_swing_rule_and_side_switches_drop_cells_before_admission(db):
    cfg = Config(local=True, stocks=('SPY',), futures=(), extra_futures=(), massive='fixture', alpaca_key='', alpaca_secret='',
                 databento='', matrix='', discord='', scanner_paper=False, swing_ideas=True, swing_rules=('daily_reversal',), swing_sides=('long',))
    svc = SwingIdeas(db, cfg)
    with db.tx() as c:
        breakout_setup(db, c)
        svc.discover(c, NOW)
        assert not c.execute(select(swings)).first()
        assert db.get(c, 'swing_scan:SPY')['technical_rules'] == []
        assert not c.execute(select(trials)).first()
    with pytest.raises(ValueError):
        Config(local=True, stocks=('SPY',), futures=(), extra_futures=(), swing_rules=('orb',)).validate()


def test_swing_switch_default_keeps_every_cell(db):
    cfg = Config(local=True, stocks=('SPY',), futures=(), extra_futures=(), massive='fixture', alpaca_key='', alpaca_secret='',
                 databento='', matrix='', discord='', scanner_paper=False, swing_ideas=True)
    assert set(cfg.swing_rules) == {'daily_breakout', 'pullback_reclaim', 'daily_reversal'} and set(cfg.swing_sides) == {'long', 'short'}
    svc = SwingIdeas(db, cfg)
    with db.tx() as c:
        breakout_setup(db, c)
        svc.discover(c, NOW)
        assert db.get(c, 'swing_scan:SPY')['technical_rules'] == ['daily_breakout']
        assert c.execute(select(swings)).first() is not None


# ---------------------------------------------------------------- 0DTE shadow / seat records

def shadow_record(key, alert_ts, flatten_at, option_type='call', stop=99.0, data_gap=True):
    return {"id": key, "kind": "scanner_shadow", "family": "session_sweep_reclaim", "symbol": "O:SPY261016C00100000",
            "underlying": "SPY", "side": "long", "entry": None, "entered_at": None, "alert_ts": alert_ts,
            "option_type": option_type, "trigger": {"signal_price": 100.0, "invalidation": stop},
            "status": "closed", "data_gap": data_gap, "flatten_at": flatten_at, "pnl": None}


def test_shadow_records_are_scored_on_the_underlying_even_when_the_option_never_quoted(db):
    T = NOW; F = NOW + 3600
    with db.tx() as c:
        db.put(c, scanner_shadow.SHADOW_PREFIX + 'a', shadow_record('a', T, F))
        db.put(c, scanner_shadow.SHADOW_PREFIX + 'b', shadow_record('b', T, F, option_type='put', stop=101.0))
        db.put(c, 'trade:0dte-scan-c', dict(shadow_record('c', T, F), kind='scanner'))
        for k in range(0, 60):
            db.append(c, 'bar', 'alpaca', 'SPY', T + 60 * k, dict(o=100 + k * .02, h=100.1 + k * .02, l=99.95 + k * .02, c=100.05 + k * .02, v=5))
        db.append(c, 'bar', 'alpaca', 'SPY', T + 60 * 20, dict(o=100.4, h=102.5, l=100.3, c=102.4, v=5))  # later revision: target bar for the call
        assert scanner_shadow.score_underlying(db, c, F + 60) == 0  # session not over yet
        assert scanner_shadow.score_underlying(db, c, F + 200) == 3
        a = db.get(c, scanner_shadow.SHADOW_PREFIX + 'a')['underlying_path']
        assert a['status'] == 'complete' and a['side'] == 'long' and a['entry'] == pytest.approx(100.01)
        assert a['bracket']['reason'] == 'target' and a['bracket']['R'] == pytest.approx(2.0, abs=.02)
        assert a['bps']['60'] is None and a['bps']['30'] == pytest.approx((100.6 / 100.01 - 1) * 1e4, abs=.1)
        b = db.get(c, scanner_shadow.SHADOW_PREFIX + 'b')['underlying_path']
        assert b['side'] == 'short' and b['bracket']['reason'] == 'stop'
        seat = db.get(c, 'trade:0dte-scan-c')
        assert seat['underlying_path']['status'] == 'complete' and seat['underlying'] == 'SPY' and seat['kind'] == 'scanner'
        assert scanner_shadow.score_underlying(db, c, F + 300) == 0  # written once
        card = scanner_shadow.scorecard(db, c)
        cell = next(x for x in card['shadow_cells'] if x['underlying'] == 'SPY')
        assert cell['alerts'] == 2 and cell['data_gaps'] == 2 and cell['scored'] == 0
        assert cell['underlying_path']['scored'] == 2 and cell['underlying_path']['bracket']['n'] == 2
        assert cell['underlying_path']['bracket']['targets'] == 1 and cell['underlying_path']['bracket']['stops'] == 1


def test_shadow_underlying_scoring_marks_unscorable_and_expired_records_once(db):
    T = NOW; F = NOW + 3600
    with db.tx() as c:
        db.put(c, scanner_shadow.SHADOW_PREFIX + 'nobars', shadow_record('nobars', T, F))
        old = shadow_record('old', T - 5 * 86400, F - 5 * 86400)
        db.put(c, scanner_shadow.SHADOW_PREFIX + 'old', old)
        db.put(c, scanner_shadow.SHADOW_PREFIX + 'notype', dict(shadow_record('notype', T, F), option_type=None))
        assert scanner_shadow.score_underlying(db, c, F + 200) == 3
        assert db.get(c, scanner_shadow.SHADOW_PREFIX + 'nobars')['underlying_path']['status'] == 'unavailable'
        assert db.get(c, scanner_shadow.SHADOW_PREFIX + 'old')['underlying_path']['status'] == 'expired'
        assert db.get(c, scanner_shadow.SHADOW_PREFIX + 'notype')['underlying_path']['status'] == 'unavailable'
        assert scanner_shadow.score_underlying(db, c, F + 300) == 0
