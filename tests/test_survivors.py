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
    assert s['strategy'] == 'S3' and s['dir'] == 1 and s['hold'] == 15


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


def test_v1_state_row_forces_reseed_but_loads_other_fields():
    feed = survivors.SurvivorFeed('MGC', 0.1)
    _seed_bars(feed, 4000.0, 1.0)
    feed.feed({'ts': _ts(23, 120), 'o': 4000.0, 'h': 4000.0, 'l': 4000.0,
               'c': 4000.0, 'v': 100})
    state = feed.to_state()
    # Shape of the rows latched in production at activation: written by
    # v1, seeded=True (set unconditionally) with an empty/broken history
    # latch and a recent attempt timestamp.
    state['version'] = 'survivors-v1'
    state['seeded'] = True
    state['seed_attempt_at'] = 1790000000
    restored = survivors.SurvivorFeed.from_state(state)
    assert restored.seeded is False
    assert restored.seed_attempt_at == 0
    # Everything else loads normally.
    assert restored.root == state['root'] and restored.tick == state['tick']
    assert restored.sdate == state['sdate']
    assert restored.history == state['history']
    assert restored.atr == state['atr']
    assert restored.bar_index == state['bar_index']
    assert list(restored.closes) == state['closes']
    # And the restored feed re-stamps the current version on save.
    assert restored.to_state()['version'] == survivors.VERSION


def test_v2_state_row_roundtrips_seeded_latch():
    feed = survivors.SurvivorFeed('MGC', 0.1)
    _seed_bars(feed, 4000.0, 1.0)
    feed.seeded = True
    feed.seed_attempt_at = 1790000000
    state = feed.to_state()
    assert state['version'] == survivors.VERSION
    restored = survivors.SurvivorFeed.from_state(state)
    assert restored.seeded is True
    assert restored.seed_attempt_at == 1790000000


# ---------------------------------------------------------------------
# Seed isolation (second seed fix, 2026-10-30): the seed runs on a
# dedicated connection in its own transaction — never inside the
# tick's — and banks its outcome separately, so one feed's failure
# can neither abort the tick nor starve the other feeds, and the
# 600 s retry cadence survives a failed attempt.
# ---------------------------------------------------------------------
def _append_dated_sessions(db, symbol):
    """16 complete CT sessions of dated-contract bars for one symbol,
    ending in the last few weeks relative to the real clock."""
    today = datetime.now(CT).date()
    days = [today - timedelta(days=back) for back in range(18, 2, -1)]
    bar = {'o': 100.0, 'h': 101.0, 'l': 99.0, 'c': 100.5, 'v': 10}
    with db.tx() as c:
        for d in days:
            base = datetime(d.year, d.month, d.day, 0, 0,
                            tzinfo=CT).timestamp()
            db.append_bars(c, 'bar', 'databento',
                           [(symbol, base + 60 * i, dict(bar))
                            for i in range(810)])
    return [d.isoformat() for d in days]


def test_seed_isolated_success_installs_and_banks(db):
    days = _append_dated_sessions(db, 'MNQZ6@7')
    now = datetime.now(timezone.utc).timestamp()
    feed = survivors.SurvivorFeed('MNQ', 0.25)
    assert survivors._seed_isolated(db, 'MNQ.c.0', feed, 'MNQ', now)
    assert feed.seeded is True
    assert [s['sdate'] for s in feed.history] == days
    assert feed.atr == pytest.approx(2.0)
    with db.tx() as c:
        banked = db.get(c, 'survivor_engine:MNQ.c.0')
    assert banked['seeded'] is True
    assert banked['seed_attempt_at'] == now
    assert banked['version'] == survivors.VERSION
    assert len(banked['history']) == 16


def test_seed_isolated_failure_banks_attempt_and_isolates(db,
                                                         monkeypatch):
    _append_dated_sessions(db, 'MCLZ6@3')
    now = datetime.now(timezone.utc).timestamp()
    real_seed = survivors.seed_history

    def flaky(db_, c_, feed_, root_, now_):
        if root_ == 'MNQ':
            raise RuntimeError('seed boom')
        return real_seed(db_, c_, feed_, root_, now_)

    monkeypatch.setattr(survivors, 'seed_history', flaky)
    bad = survivors.SurvivorFeed('MNQ', 0.25)
    good = survivors.SurvivorFeed('MCL', 0.1)
    # A stand-in tick transaction stays open across both attempts;
    # neither seed may poison it (the first production failure mode:
    # MNQ's timeout aborted the shared tick transaction and MCL/MGC
    # never got an attempt).
    with db.tx() as c:
        assert not survivors._seed_isolated(db, 'MNQ.c.0', bad,
                                            'MNQ', now)
        assert survivors._seed_isolated(db, 'MCL.v.0', good,
                                        'MCL', now)
        db.put(c, 'tick:sentinel', {'ok': True})
    assert bad.seeded is False and bad.seed_attempt_at == now
    assert good.seeded is True and good.atr == pytest.approx(2.0)
    with db.tx() as c:
        banked_bad = db.get(c, 'survivor_engine:MNQ.c.0')
        assert db.get(c, 'tick:sentinel') == {'ok': True}
    # The failed attempt's stamp was committed by the bank transaction
    # alone, so the 600 s retry cadence holds even if the tick segment
    # around it later aborts.
    assert banked_bad['seeded'] is False
    assert banked_bad['seed_attempt_at'] == now


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


def test_submit_s3_horizon_flatten_is_time_exit(db, cfg):
    # S3 horizon amended 30 -> 15 on 2026-11-05 (Josh's decision on
    # the frozen horizon sweep); the +2-minute execution buffer is
    # unchanged, so the booked exit is signal ts + (15 + 2) minutes.
    sig = _sig('S3', close=4000.0)
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S3', 'MGC.v.0',
                                      sig)
    assert r['submitted'] and r['stop'] is None and r['target'] is None
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s3:MGC.v.0:t1')
    assert pos['flatten_at'] == sig['ts'] + 17 * 60
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
    # 2026-11-04 safety-stop overlay: the S1 live send still carries
    # no take-profit and no strategy bracket, but it is no longer a
    # naked market entry — it carries the broker-resting safety stop
    # (0.5 x atr_t = 50 ticks x 0.25 above the short entry). The
    # booked record keeps its stop=None horizon construction; the
    # full pinning lives in tests/test_survivor_safety_stop.py.
    cfg.traderspost_enabled = True
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S1', 'MNQ.c.0',
                                      _sig('S1', close=20000.0))
    assert r['submitted'] and r['traderspost_queued']
    traderspost.dispatch_pending(cfg, db)
    assert len(sent) == 1
    payload = traderspost.build_payload(None, dict(sent[0]))
    assert payload['stopLoss'] == {'type': 'stop', 'stopPrice': 20012.5}
    assert 'takeProfit' not in payload
    with db.tx() as c:
        trade = db.get(c, 'trade:' + r['id'])
    assert trade['traderspost_sent'] is True
    assert trade['stop'] is None and trade['safety_stop'] == 20012.5


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


def test_submit_live_send_flag_set_when_queued(db, cfg, sent):
    """Discord live label: live_send is set at booking exactly when the
    booking will be queued to TradersPost (ict_push reads it)."""
    cfg.traderspost_enabled = True
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S1', 'MNQ.c.0',
                                      _sig('S1', close=20000.0))
    assert r['submitted'] and r['traderspost_queued']
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s1:MNQ.c.0:t1')
        trade = db.get(c, 'trade:' + r['id'])
    assert pos['live_send'] is True and trade['live_send'] is True


def test_submit_live_send_flag_clear_when_tp_disabled(db, cfg):
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    assert r['submitted'] and not r['traderspost_queued']
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s2:MCL.v.0:t1')
    assert pos['live_send'] is False


def test_submit_live_send_flag_clear_when_tripped(db, cfg, sent):
    cfg.traderspost_enabled = True
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        st['live'] = False
        st['tripped_at'] = NOW
        db.put(c, survivors.STATE_KEY, st)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    assert r['submitted']
    assert 'survivor live sends off' in r['traderspost_skip_reason']
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s2:MCL.v.0:t1')
    assert pos['live_send'] is False


# ---------------------------------------------------------------------
# Tripwire + review accounting
# ---------------------------------------------------------------------
def _closed_trade(i, pnl, sent_flag=True):
    return {'id': 'ict-survivor_s1-MNQ.c.0-%d-t1' % i,
            'detector': 'survivor_s1', 'strategy': 'survivor_s1_rvwap_fade',
            'symbol': 'MNQ.c.0', 'status': 'closed', 'pnl': pnl,
            'side': 'long', 'qty': 1,
            'traderspost_sent': sent_flag}


def _confirm_closed(db, c, trade, pnl):
    """Broker-confirm both legs of a fixture trade at prices whose
    confirmed P&L (long, MNQ multiplier 2, qty 1) equals pnl. Since the
    2026-11-02 fill-confirmation fix, account_live counts ONLY
    verdicted fills — these older tripwire/review tests keep their
    original intent (the accounting math) on confirmed records."""
    from compass import fill_confirmation as fc
    entry = 10000.0
    exit_ = entry + pnl / 2.0
    fc.record_verdict(db, c, trade['id'], 'entry', 'filled', NOW,
                      price=entry, at=NOW - 60, source='test')
    fc.record_verdict(db, c, trade['id'], 'exit', 'filled', NOW,
                      price=exit_, at=NOW, source='test')


def test_tripwire_trips_at_minus_1750(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        for i, pnl in enumerate([-1000.0, -800.0]):
            t = _closed_trade(i, pnl)
            db.put(c, 'trade:' + t['id'], t)
            _confirm_closed(db, c, t, pnl)
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
        _confirm_closed(db, c, t1, -1700.0)
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
            _confirm_closed(db, c, t, 10.0)
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


# ---------------------------------------------------------------------
# News pause wiring (2026-11-02, Josh: "fix and wire"): the repaired
# should_pause_for_news gate declines survivor ENTRIES only, in the
# ICT decline shape. Exits, stops and the 15:45 flatten never
# consult it. The gate itself is pinned in test_news_pause.py; here
# it is stubbed to control the window.
# ---------------------------------------------------------------------
@pytest.fixture
def news_gate(monkeypatch):
    state = {'pause': False, 'event': None}

    def fake_should_pause(db, c):
        return state['pause'], state['event']

    monkeypatch.setattr(
        'compass.tradermatrix_intel.should_pause_for_news',
        fake_should_pause)
    return state


def test_submit_declined_inside_news_window(db, cfg, news_gate):
    news_gate['pause'] = True
    news_gate['event'] = {'title': 'Non-Farm Employment Change'}
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    assert r['submitted'] is False
    assert r['reason'] == 'news_pause'
    assert r['event'] == 'Non-Farm Employment Change'
    # Nothing was booked: no position, and a later signal outside the
    # window books cleanly (no phantom existing_position block).
    with db.tx() as c:
        assert db.get(c, 'position:ict:survivor_s2:MCL.v.0:t1') is None
    news_gate['pause'] = False
    with db.tx() as c:
        r2 = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                       _sig('S2'))
    assert r2['submitted']


def test_submit_allowed_when_news_gate_clear(db, cfg, news_gate):
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    assert r['submitted']


def test_submit_proceeds_when_intel_errors(db, cfg, monkeypatch):
    def boom(db, c):
        raise RuntimeError('intel down')

    monkeypatch.setattr(
        'compass.tradermatrix_intel.should_pause_for_news', boom)
    with db.tx() as c:
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2', 'MCL.v.0',
                                      _sig('S2'))
    assert r['submitted']


def test_flatten_unaffected_inside_news_window(db, cfg, news_gate):
    engine = Engine(db, cfg)
    key = 'position:ict:survivor_s1:MNQ.c.0:t1'
    with db.tx() as c:
        db.put(c, key, _horizon_position(NOW - 1))
        db.put(c, 'quote:MNQ.c.0', {'bid': 19999.75, 'ask': 20000.25,
                                    'last': 20000.0, 'ts': NOW})
    news_gate['pause'] = True
    news_gate['event'] = {'title': 'Non-Farm Employment Change'}
    with db.tx() as c:
        engine.exits(c, NOW + 60)
        pos = db.get(c, key)
    assert pos['status'] == 'closed'
    assert pos['exit_reason'] == 'session_flatten'


def test_stop_exit_unaffected_inside_news_window(db, cfg, news_gate):
    engine = Engine(db, cfg)
    key = 'position:ict:survivor_s2:MCL.v.0:t1'
    pos = {'id': 'ict-survivor_s2-MCL.v.0-1-t1',
           'detector': 'survivor_s2',
           'strategy': 'survivor_s2_asia_low_fade', 'symbol': 'MCL.v.0',
           'asset': 'future', 'side': 'long', 'qty': 1, 'entry': 100.0,
           'stop': 99.75, 'target': 100.5, 'tick': 0.01,
           'multiplier': 1000, 'fee': 1.5, 'status': 'open',
           'entered_at': NOW - 120, 'flatten_at': NOW + 3600,
           'fill_version': FILL_VERSION, 'last_quote_ts': 0,
           'last_bar_checked': 0, 'mark': 100.0, 'unrealized': -3.0}
    with db.tx() as c:
        db.put(c, key, pos)
        db.put(c, 'quote:MCL.v.0', {'bid': 99.5, 'ask': 99.52,
                                    'last': 99.51, 'ts': NOW,
                                    'bid_size': 10, 'ask_size': 10})
    news_gate['pause'] = True
    news_gate['event'] = {'title': 'Non-Farm Employment Change'}
    with db.tx() as c:
        engine.exits(c, NOW)
        pos = db.get(c, key)
    assert pos['status'] == 'closed'
    assert pos['exit_reason'] == 'stop'


# ---------------------------------------------------------------------
# Per-strategy live-send gate (2026-11-09, Josh: pull S2, run
# S1/S3/S4 to the 50-fill review): SURVIVOR_LIVE_STRATEGIES selects
# which strategies may queue TradersPost entries inside a live book.
# Default = all four (pre-gate behavior). The gate is entry-only:
# exits key off the position's own traderspost_sent flag.
# ---------------------------------------------------------------------
def test_survivor_live_strategies_default_and_env_parsing(cfg,
                                                          monkeypatch):
    # Fixture cfg is a real Config with the env unset: all four.
    assert cfg.survivor_live_strategies == frozenset(
        {'S1', 'S2', 'S3', 'S4'})
    assert survivors.live_strategies(cfg) == frozenset(
        {'S1', 'S2', 'S3', 'S4'})
    # Env parsing: comma-separated, whitespace-tolerant.
    monkeypatch.setenv('SURVIVOR_LIVE_STRATEGIES', 'S1, S3 ,S4')
    assert Config(local=True, risk=100).survivor_live_strategies == \
        frozenset({'S1', 'S3', 'S4'})
    # An explicitly empty env value pulls every strategy.
    monkeypatch.setenv('SURVIVOR_LIVE_STRATEGIES', '')
    assert Config(local=True, risk=100).survivor_live_strategies == \
        frozenset()
    # A cfg without the attribute at all falls back to all four (the
    # send sites' getattr-default style).
    assert survivors.live_strategies(object()) == frozenset(
        {'S1', 'S2', 'S3', 'S4'})


def test_submit_s4_live_queues_under_default_set(db, cfg, sent):
    # S1/S2 queueing under the default set is pinned by the tests
    # above; S4 completes the "all four can live-send" default.
    cfg.traderspost_enabled = True
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S4', 'MGC.v.0',
                                      _sig('S4', close=4000.0))
    assert r['submitted'] and r['traderspost_queued']
    traderspost.dispatch_pending(cfg, db)
    assert len(sent) == 1
    payload = traderspost.build_payload(None, dict(sent[0]))
    assert payload['stopLoss']['stopPrice'] == 4002.5
    assert payload['takeProfit']['limitPrice'] == 3995.0
    with db.tx() as c:
        pos = db.get(c, 'position:ict:survivor_s4:MGC.v.0:t1')
    assert pos['live_send'] is True


def test_pulled_s2_books_paper_while_s3_still_sends(db, cfg, sent):
    cfg.traderspost_enabled = True
    cfg.survivor_live_strategies = frozenset({'S1', 'S3', 'S4'})
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r2 = survivors.submit_survivor(db, c, cfg, NOW, 'S2',
                                       'MCL.v.0', _sig('S2'))
        r3 = survivors.submit_survivor(db, c, cfg, NOW, 'S3',
                                       'MGC.v.0',
                                       _sig('S3', close=4000.0))
    # S2: the paper booking stands, nothing queues, and the skip
    # reason names the strategy and the gate.
    assert r2['submitted'] and not r2['traderspost_queued']
    assert 'survivor S2 live sends off' in r2['traderspost_skip_reason']
    assert 'SURVIVOR_LIVE_STRATEGIES' in r2['traderspost_skip_reason']
    # S3 in the same session, same live book: still live-sends.
    assert r3['submitted'] and r3['traderspost_queued']
    traderspost.dispatch_pending(cfg, db)
    assert len(sent) == 1  # the S3 entry only
    with db.tx() as c:
        pos2 = db.get(c, 'position:ict:survivor_s2:MCL.v.0:t1')
        trade2 = db.get(c, 'trade:' + r2['id'])
        trade3 = db.get(c, 'trade:' + r3['id'])
    assert pos2['status'] == 'open' and pos2['live_send'] is False
    assert trade2['traderspost_sent'] is False
    assert trade2['traderspost_skip_reason'] == \
        r2['traderspost_skip_reason']
    assert pos2['traderspost_skip_reason'] == \
        r2['traderspost_skip_reason']
    assert trade3['live_send'] is True
    assert trade3['traderspost_sent'] is True


@pytest.fixture
def closes(monkeypatch):
    calls = []

    def fake_close(cfg, db, symbol, ref=None):
        calls.append({'symbol': symbol, 'ref': ref})
        return True, 'ok'

    monkeypatch.setattr(traderspost, 'send_close_signal', fake_close)
    return calls


def test_live_sent_s2_position_still_exits_after_pull(db, cfg, sent,
                                                      closes):
    cfg.traderspost_enabled = True
    engine = Engine(db, cfg)
    with db.tx() as c:
        survivors.ensure_state(db, c, NOW)
        r = survivors.submit_survivor(db, c, cfg, NOW, 'S2',
                                      'MCL.v.0', _sig('S2'))
    assert r['traderspost_queued']
    traderspost.dispatch_pending(cfg, db)
    assert len(sent) == 1  # entry went out while S2 was in the set
    key = 'position:ict:survivor_s2:MCL.v.0:t1'
    with db.tx() as c:
        pos = db.get(c, key)
    assert pos['traderspost_sent'] is True
    # Pull S2 AFTER the position is live at the broker.
    cfg.survivor_live_strategies = frozenset({'S1', 'S3', 'S4'})
    # Price trades through the bracket stop (99.75): the paper close
    # must still fire the live exit. exits() consults the position's
    # traderspost_sent flag only — never the entry gate — so the
    # pulled strategy's open position is never stranded.
    with db.tx() as c:
        db.put(c, 'quote:MCL.v.0', {'bid': 99.5, 'ask': 99.52,
                                    'last': 99.51, 'ts': NOW,
                                    'bid_size': 10, 'ask_size': 10})
    with db.tx() as c:
        engine.exits(c, NOW)
        pos = db.get(c, key)
    assert pos['status'] == 'closed' and pos['exit_reason'] == 'stop'
    traderspost.dispatch_pending(cfg, db)
    assert closes == [{'symbol': 'MCL.v.0', 'ref': r['id']}]
