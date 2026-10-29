"""Claude gauntlet survivors (2026-10-28): signal engines, submit path,
TradersPost payloads, tripwire + review accounting.

The signal definitions were fidelity-verified against the gauntlet's
frozen gate-arm trade lists over the foundation year (exact match, all
four) before this module shipped; these tests pin the definitions on
synthetic fixture bars and cover the production wiring: 1-contract
records in the ICT shapes, bracketed (S2/S4) vs bracket-less (S1/S3)
payloads, the tripwire gate on sends, and exits() tolerance of
stop=None horizon positions.
"""
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass.config import Config
from compass import survivors, traderspost
from compass.engine import Engine
from compass.simulation import FILL_VERSION

CT = ZoneInfo("America/Chicago")
NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()  # Mon 12:00 CT


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'sv.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.traderspost_enabled = False
    return c


def _ts(day, tod):
    return datetime(2026, 9, day, tod // 60, tod % 60,
                    tzinfo=CT).timestamp()


def _seed_bars(feed, price, half_spread, days=range(7, 23), tod=180,
               per_day=10):
    """16 flat sessions of history: ATR = 2*half_spread, closes flat."""
    for d in days:
        for i in range(per_day):
            feed.feed({'ts': _ts(d, tod + i), 'o': price,
                       'h': price + half_spread, 'l': price - half_spread,
                       'c': price, 'v': 100})


# ---------------------------------------------------------------------
# Signal engines on fixture bars
# ---------------------------------------------------------------------
def test_s1_rvwap_fade_short():
    feed = survivors.SurvivorFeed('MNQ', 0.25)
    _seed_bars(feed, 20000.0, 0.5)          # atr 1.0 -> atr_t 4
    fired = []
    for i in range(31):                     # 08:30..09:00 RTH bars
        close = 20000.0 if i < 30 else 20000.75
        fired += feed.feed({'ts': _ts(23, 510 + i), 'o': 20000.0,
                            'h': close, 'l': 20000.0, 'c': close, 'v': 100})
    assert len(fired) == 1
    s = fired[0]
    assert s['strategy'] == 'S1' and s['dir'] == -1 and s['hold'] == 60
    assert s['tod'] == 540


def test_s2_asia_low_fade_long():
    feed = survivors.SurvivorFeed('MCL', 0.01)
    _seed_bars(feed, 100.0, 0.5)            # atr 1.0 -> atr_t 100
    fired = []
    bars = [
        (60, 99.0, 100.0),                  # asia low = 99.0
        (119, 99.2, 100.2),
        (120, 99.5, 100.3),                 # no pierce yet
        (121, 98.9, 99.5),                  # pierce + close back above
    ]
    for tod, lo, close in bars:
        fired += feed.feed({'ts': _ts(23, tod), 'o': 100.0, 'h': close,
                            'l': lo, 'c': close, 'v': 100})
    assert len(fired) == 1
    s = fired[0]
    assert s['strategy'] == 'S2' and s['dir'] == 1
    assert s['stop_ticks'] == 25.0 and s['target_ticks'] == 50.0


def test_s3_london_mr60_long():
    feed = survivors.SurvivorFeed('MGC', 0.1)
    _seed_bars(feed, 4000.0, 1.0)           # atr 2.0 -> atr_t 20
    fired = []
    for i, close in enumerate([4000.0, 4000.0, 3999.5]):
        fired += feed.feed({'ts': _ts(23, 120 + i), 'o': 4000.0,
                            'h': 4000.0, 'l': close, 'c': close, 'v': 100})
    assert len(fired) == 1
    s = fired[0]
    assert s['strategy'] == 'S3' and s['dir'] == 1 and s['hold'] == 30


def test_s4_ny_mr60_fade_short():
    feed = survivors.SurvivorFeed('MGC', 0.1)
    _seed_bars(feed, 4000.0, 1.0)
    fired = []
    for i, close in enumerate([4000.0, 4000.0, 4000.5]):
        fired += feed.feed({'ts': _ts(23, 540 + i), 'o': 4000.0,
                            'h': close, 'l': 4000.0, 'c': close, 'v': 100})
    assert len(fired) == 1
    s = fired[0]
    assert s['strategy'] == 'S4' and s['dir'] == -1
    assert s['stop_ticks'] == 5.0 and s['target_ticks'] == 5.0


def test_feed_state_roundtrip_preserves_signals():
    feed = survivors.SurvivorFeed('MGC', 0.1)
    _seed_bars(feed, 4000.0, 1.0)
    feed.feed({'ts': _ts(23, 120), 'o': 4000.0, 'h': 4000.0, 'l': 4000.0,
               'c': 4000.0, 'v': 100})
    restored = survivors.SurvivorFeed.from_state(feed.to_state())
    fired = restored.feed({'ts': _ts(23, 121), 'o': 4000.0, 'h': 4000.0,
                           'l': 3999.5, 'c': 3999.5, 'v': 100})
    assert len(fired) == 1 and fired[0]['strategy'] == 'S3'


def test_no_signals_before_atr_warms():
    feed = survivors.SurvivorFeed('MGC', 0.1)
    _seed_bars(feed, 4000.0, 1.0, days=range(7, 17))  # only 10 sessions
    fired = feed.feed({'ts': _ts(23, 120), 'o': 4000.0, 'h': 4000.0,
                       'l': 3999.0, 'c': 3999.0, 'v': 100})
    assert fired == []


# ---------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------
def _trade(**kw):
    base = {'symbol': 'MGC.v.0', 'side': 'long', 'entry': 4000.0,
            'signal_price': 4000.0, 'stop': 3990.0, 'target': 4020.0,
            'qty': 1, 'detector': 'survivor_s4', 'is_first_leg': True}
    base.update(kw)
    return base


def test_payload_bracketed_has_nested_brackets():
    p = traderspost.build_payload(None, _trade())
    assert p['stopLoss'] == {'type': 'stop', 'stopPrice': 3990.0}
    assert p['takeProfit'] == {'limitPrice': 4020.0}
    assert p['quantity'] == 1 and p['orderType'] == 'market'


def test_payload_no_bracket_is_plain_market():
    p = traderspost.build_payload(
        None, _trade(stop=None, target=None, no_bracket=True,
                     detector='survivor_s3'))
    assert 'stopLoss' not in p and 'takeProfit' not in p
    assert p['action'] == 'buy' and p['quantity'] == 1


def test_payload_missing_stop_without_flag_still_fails_closed():
    assert traderspost.build_payload(None, _trade(stop=None)) is None


# ---------------------------------------------------------------------
# Submit path
# ---------------------------------------------------------------------
def _sig(strat, **kw):
    base = {'strategy': strat,
            'dir': survivors.DIRECTION[strat],
            'close': 100.0, 'ts': NOW - 60, 'tod': 720,
            'sdate': '2026-09-28', 'atr_t': 100.0,
            'bar_index': 0}
    if strat in ('S2', 'S4'):
        base.update({'stop_ticks': 25.0, 'target_ticks': 50.0})
    if strat in survivors.HORIZON:
        base['hold'] = survivors.HORIZON[strat]
    base.update(kw)
    return base


def test_submit_s2_books_bracket_one_contract(db, cfg):
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    assert r['submitted'] and r['qty'] == 1
    assert r['stop'] == 99.75 and r['target'] == 100.5
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s2:MCL.v.0:t1')
        trade = db.get(c, 'trade:' + r['id'])
    assert pos['status'] == 'open' and pos['qty'] == 1
    assert pos['id'].endswith('-t1') and trade['id'] == pos['id']
    assert pos['stop'] == 99.75 and pos['target'] == 100.5
    # TP disabled: nothing queued, no sent flag.
    assert not pos.get('traderspost_sent')


def test_submit_s1_horizon_flatten_is_time_exit(db, cfg):
    sig = _sig('S1', close=20000.0)
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S1', 'MNQ.c.0', sig)
    assert r['submitted'] and r['stop'] is None and r['target'] is None
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s1:MNQ.c.0:t1')
    # Exit at the close of the bar 60 after the entry bar:
    # signal ts + (60 + 2) minutes, before the 15:45 CT flatten.
    assert pos['flatten_at'] == sig['ts'] + 62 * 60
    assert pos['no_bracket'] is True


def test_submit_second_signal_blocked_while_open(db, cfg):
    with db.tx() as c:
        survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                  _sig('S2'))
        r2 = survivors.submit_survivor(db, c, cfg, NOW + 60, 'S2',
                                       'MCL.v.0', _sig('S2', ts=NOW))
    assert r2['submitted'] is False
    assert r2['reason'] == 'existing_position'


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(cfg, db, trade):
        calls.append(trade)
        return True, 'ok'

    monkeypatch.setattr(traderspost, 'send_signal', fake_send)
    return calls


def test_submit_s1_live_queues_bracketless_send(db, cfg, sent):
    cfg.traderspost_enabled = True
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S1', 'MNQ.c.0',
                                      _sig('S1', close=20000.0))
    assert r['submitted'] and r['traderspost_queued']
    traderspost.dispatch_pending(cfg, db)
    assert len(sent) == 1
    payload = traderspost.build_payload(None, dict(sent[0]))
    assert 'stopLoss' not in payload and 'takeProfit' not in payload
    with db.tx() as c:
        trade = db.get(c, 'trade:' + r['id'])
    assert trade['traderspost_sent'] is True


def test_submit_s2_live_queues_bracketed_send(db, cfg, sent):
    cfg.traderspost_enabled = True
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    assert r['traderspost_queued']
    traderspost.dispatch_pending(cfg, db)
    assert len(sent) == 1
    payload = traderspost.build_payload(None, dict(sent[0]))
    assert payload['stopLoss']['stopPrice'] == 99.75
    assert payload['takeProfit']['limitPrice'] == 100.5


def test_tripped_state_blocks_live_send_but_books_paper(db, cfg, sent):
    cfg.traderspost_enabled = True
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        st['live'] = False
        st['tripped_at'] = NOW
        db.put(c, survivors.STATE_KEY, st)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    assert r['submitted']  # paper booking stands
    assert 'survivor live sends off' in r['traderspost_skip_reason']
    traderspost.dispatch_pending(cfg, db)
    assert sent == []
    with db.tx() as c:
        trade = db.get(c, 'trade:' + r['id'])
    assert trade['traderspost_sent'] is False


# ---------------------------------------------------------------------
# Tripwire + review accounting
# ---------------------------------------------------------------------
def _closed_trade(i, pnl, sent_flag=True):
    return {'id': 'ict-survivor_s1-MNQ.c.0-%d-t1' % i,
            'detector': 'survivor_s1', 'strategy': 'survivor_s1_rvwap_fade',
            'symbol': 'MNQ.c.0', 'status': 'closed', 'pnl': pnl,
            'traderspost_sent': sent_flag}


def test_tripwire_trips_at_minus_1750(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        for i, pnl in enumerate([-1000.0, -800.0]):
            db.put(c, 'trade:' + _closed_trade(i, pnl)['id'],
                   _closed_trade(i, pnl))
        st = survivors.account_live(db, c, st, NOW)
        assert st['live'] is False and st['tripped_at'] == NOW
        assert st['combined_live_pnl'] == -1800.0
        assert st['live_fills'] == 4


def test_tripwire_holds_above_threshold_and_ignores_unsent(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        t1 = _closed_trade(1, -1700.0)
        t2 = _closed_trade(2, -5000.0, sent_flag=False)
        db.put(c, 'trade:' + t1['id'], t1)
        db.put(c, 'trade:' + t2['id'], t2)
        st = survivors.account_live(db, c, st, NOW)
        assert st['live'] is True
        assert st['combined_live_pnl'] == -1700.0


def test_review_due_at_50_fills(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        for i in range(25):
            t = _closed_trade(i, 10.0)
            db.put(c, 'trade:' + t['id'], t)
        st = survivors.account_live(db, c, st, NOW)
        assert st['live_fills'] == 50
        assert st['review_due'] is True and st['review_due_at'] == NOW


def test_digest_surfaces_survivor_state(db):
    from compass.day_digest import assemble
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        st['review_due'] = True
        db.put(c, survivors.STATE_KEY, st)
        out = assemble(db, c, NOW)
    sections = {s['key']: s for s in out['sections']}
    assert 'survivors' in sections
    text = '\n'.join(sections['survivors']['lines'])
    assert 'LIVE' in text and 'REVIEW DUE' in text


# ---------------------------------------------------------------------
# exits() with stop=None (horizon positions)
# ---------------------------------------------------------------------
def _horizon_position(flatten_at):
    return {'id': 'ict-survivor_s1-MNQ.c.0-1-t1', 'detector': 'survivor_s1',
            'strategy': 'survivor_s1_rvwap_fade', 'symbol': 'MNQ.c.0',
            'asset': 'future', 'side': 'short', 'qty': 1, 'entry': 20000.0,
            'stop': None, 'target': None, 'tick': 0.25, 'multiplier': 2,
            'fee': 1.5, 'status': 'open', 'entered_at': NOW - 120,
            'flatten_at': flatten_at, 'fill_version': FILL_VERSION,
            'last_quote_ts': 0, 'last_bar_checked': 0, 'mark': 20000.0,
            'unrealized': -3.0}


def test_exits_tolerates_none_stop_and_flattens(db, cfg):
    engine = Engine(db, cfg)
    key = 'position:ict:survivor_s1:MNQ.c.0:t1'
    with db.tx() as c:
        db.put(c, key, _horizon_position(NOW + 3600))
        db.put(c, 'quote:MNQ.c.0', {'bid': 19999.75, 'ask': 20000.25,
                                    'last': 20000.0, 'ts': NOW})
    with db.tx() as c:
        engine.exits(c, NOW)  # must not raise on stop=None
        pos = db.get(c, key)
        assert pos['status'] == 'open'
    with db.tx() as c:
        pos = db.get(c, key)
        pos['flatten_at'] = NOW - 1
        db.put(c, key, pos)
    with db.tx() as c:
        engine.exits(c, NOW + 60)
        pos = db.get(c, key)
    assert pos['status'] == 'closed'
    assert pos['exit_reason'] == 'session_flatten'
