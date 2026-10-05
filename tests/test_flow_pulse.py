import time
from datetime import datetime, timedelta, timezone
import pytest
from compass.store import Store
from compass import flow_pulse as fp

NOW = 1_790_000_000.0


def _live_expiry(days_out=7):
    return (datetime.now(timezone.utc) + timedelta(days=days_out)).strftime('%m/%d/%y')


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'pulse.db'))
    db.initialize()
    yield db
    db.engine.dispose()


class Cfg:
    flow_pulse = True
    watch_symbols = ('HOOD', 'COIN', 'SPY')
    flow_pulse_min_premium = 1000000
    flow_pulse_min_score = 90
    flow_pulse_min_ratio = 2.0
    flow_pulse_max_dte = 45
    flow_pulse_push_enabled = False
    flow_pulse_webhook = ''


def prow(symbol='HOOD', ts=NOW - 300, option_type='call', sentiment='bullish',
         premium=1700000, score=98, strike=114.0, expiry='10/02/26',
         vendor_id='v1'):
    return {'payload': {'symbol': symbol, 'source_ts': ts,
                        'option_type': option_type, 'sentiment': sentiment,
                        'premium': premium, 'score': score, 'strike': strike,
                        'expiry': expiry},
            'source_ts': ts, 'first_seen': ts, 'last_seen': ts,
            'vendor_id': vendor_id, 'day': '2026-09-30'}


# --- direction ---

def test_bullish_call_is_bullish():
    assert fp._direction('call', 'bullish') == 'bullish'


def test_bearish_call_is_bearish():
    # The sentiment-blind flaw in tape_confirmed counted this as long flow.
    assert fp._direction('call', 'bearish') == 'bearish'


def test_bearish_put_is_bullish():
    assert fp._direction('put', 'bearish') == 'bullish'


def test_bullish_put_is_bearish():
    assert fp._direction('put', 'bullish') == 'bearish'


def test_unknown_sentiment_ignored():
    assert fp._direction('call', 'neutral') is None
    assert fp._direction('call', None) is None
    assert fp._direction(None, 'bullish') is None


# --- dte ---

def test_dte_parses_vendor_format():
    # 2026-10-01 12:00 UTC vs expiry 2026-10-02 16:00 UTC -> ~1.17 days
    dte = fp._dte('10/02/26', 1790856000.0)
    assert dte is not None and 1.0 < dte < 2.0


def test_dte_rejects_garbage():
    assert fp._dte('n/a', NOW) is None
    assert fp._dte(None, NOW) is None


# --- detect_pulses ---

def test_fires_on_motivating_example():
    pulses = fp.detect_pulses([prow()], NOW, Cfg(), Cfg.watch_symbols)
    assert len(pulses) == 1
    p = pulses[0]
    assert p['symbol'] == 'HOOD' and p['direction'] == 'bullish'
    assert p['directional_premium'] == 1700000
    assert p['max_score'] == 98 and p['print_count'] == 1


def test_no_fire_below_premium():
    pulses = fp.detect_pulses([prow(premium=500000)], NOW, Cfg(), Cfg.watch_symbols)
    assert pulses == []


def test_no_fire_below_score():
    pulses = fp.detect_pulses([prow(score=85)], NOW, Cfg(), Cfg.watch_symbols)
    assert pulses == []


def test_mixed_flow_veto():
    rows = [prow(premium=1200000, sentiment='bullish'),
            prow(premium=800000, sentiment='bearish', vendor_id='v2')]
    pulses = fp.detect_pulses(rows, NOW, Cfg(), Cfg.watch_symbols)
    assert pulses == []  # 1.2M < 2x 0.8M


def test_directional_majority_fires():
    rows = [prow(premium=2000000, sentiment='bullish'),
            prow(premium=800000, sentiment='bearish', vendor_id='v2')]
    pulses = fp.detect_pulses(rows, NOW, Cfg(), Cfg.watch_symbols)
    assert len(pulses) == 1 and pulses[0]['direction'] == 'bullish'


def test_leaps_dated_prints_excluded():
    pulses = fp.detect_pulses([prow(expiry='01/15/28')], NOW, Cfg(), Cfg.watch_symbols)
    assert pulses == []


def test_expired_prints_excluded():
    pulses = fp.detect_pulses([prow(expiry='01/02/26')], NOW, Cfg(), Cfg.watch_symbols)
    assert pulses == []


def test_stale_prints_outside_window_ignored():
    pulses = fp.detect_pulses([prow(ts=NOW - 4000)], NOW, Cfg(), Cfg.watch_symbols)
    assert pulses == []


def test_outside_universe_ignored():
    pulses = fp.detect_pulses([prow(symbol='GME')], NOW, Cfg(), Cfg.watch_symbols)
    assert pulses == []


def test_bearish_direction_aggregation():
    rows = [prow(symbol='COIN', sentiment='bearish', premium=1500000, score=91)]
    pulses = fp.detect_pulses(rows, NOW, Cfg(), Cfg.watch_symbols)
    assert len(pulses) == 1 and pulses[0]['direction'] == 'bearish'


def test_sorted_by_premium_desc():
    rows = [prow(symbol='HOOD', premium=1200000, score=91),
            prow(symbol='COIN', premium=2600000, score=92, vendor_id='v2')]
    pulses = fp.detect_pulses(rows, NOW, Cfg(), Cfg.watch_symbols)
    assert [p['symbol'] for p in pulses] == ['COIN', 'HOOD']


# --- scan persistence ---

def _insert_flow(db, rows):
    from compass.store import flow_records
    with db.tx() as c:
        for r in rows:
            c.execute(db.insert(flow_records).values(
                day=r['day'], vendor_id=r['vendor_id'],
                source_ts=r['source_ts'], payload=r['payload'],
                first_seen=r['first_seen'], last_seen=r['last_seen']))


def test_scan_persists_and_dedupes_per_day(db):
    from compass.market import day as _day
    now = time.time()
    rows = [prow(ts=now - 300, expiry=_live_expiry())]
    # rewrite timestamps to "now" so the scan sees them
    for r in rows:
        r['payload']['source_ts'] = now - 300
        r['source_ts'] = now - 300
        r['day'] = _day(now)
    _insert_flow(db, rows)
    with db.tx() as c:
        out1 = fp.scan(db, c, Cfg(), now)
    assert out1['ran'] and out1['fired'] == 1
    with db.tx() as c:
        out2 = fp.scan(db, c, Cfg(), now + 61)  # past throttle
    assert out2['ran'] and out2['fired'] == 0  # same day+symbol+direction: no refire


def test_scan_disabled(db):
    class Off(Cfg):
        flow_pulse = False
    with db.tx() as c:
        out = fp.scan(db, c, Off(), time.time())
    assert out == {'ran': False, 'reason': 'disabled'}


def test_display_returns_pulses(db):
    now = time.time()
    rows = [prow(ts=now - 300, expiry=_live_expiry())]
    for r in rows:
        r['payload']['source_ts'] = now - 300
        r['source_ts'] = now - 300
        from compass.market import day as _day
        r['day'] = _day(now)
    _insert_flow(db, rows)
    with db.tx() as c:
        fp.scan(db, c, Cfg(), now)
    with db.tx() as c:
        d = fp.display(db, c, now)
    assert d['version'] == fp.VERSION
    assert len(d['pulses']) == 1
    assert d['pulses'][0]['symbol'] == 'HOOD'


def test_format_message_under_limit():
    pulses = fp.detect_pulses([prow()], NOW, Cfg(), Cfg.watch_symbols)
    msg = fp.format_message(pulses[0])
    assert len(msg['content']) < 2000
    assert 'HOOD' in msg['content'] and 'BULLISH' in msg['content']
    assert msg['allowed_mentions'] == {'parse': []}


# --- suggested contract ---

def _chain_contract(symbol='O:MRK261030P00145000', underlying='MRK', expiry='2026-10-30',
                   strike=145.0, otype='put', delta=-0.35, oi=500, volume=200):
    return {'symbol': symbol, 'underlying': underlying, 'expiry': expiry,
            'strike': strike, 'type': otype, 'multiplier': 100,
            'oi': oi, 'volume': volume, 'delta': delta}


def _pulse(symbol='MRK', direction='bearish', expiry='10/30/26'):
    return {'symbol': symbol, 'direction': direction,
            'top_prints': [{'strike': 147.0, 'expiry': expiry, 'premium': 1149000,
                            'option_type': 'call', 'sentiment': 'bearish', 'score': 92},
                           {'strike': 152.5, 'expiry': expiry, 'premium': 586000,
                            'option_type': 'call', 'sentiment': 'bearish', 'score': 82}]}


def _seed_market(db, contracts, bid=144.90, ask=145.10):
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': NOW, 'bid': bid, 'ask': ask})
        db.put(c, 'chain:MRK', {'asof': NOW, 'contracts': contracts})
        return fp.suggest_contract(db, c, Cfg(), _pulse(), NOW)


def test_suggest_contract_picks_put_for_bearish(db):
    got = _seed_market(db, [_chain_contract(),
                            _chain_contract(symbol='O:MRK261030C00145000', otype='call', delta=0.35)])
    assert got is not None
    assert got['type'] == 'put'
    assert got['strike'] == 145.0
    assert got['expiry'] == '2026-10-30'


def test_suggest_contract_picks_call_for_bullish(db):
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': NOW, 'bid': 144.90, 'ask': 145.10})
        db.put(c, 'chain:MRK', {'asof': NOW, 'contracts': [
            _chain_contract(),
            _chain_contract(symbol='O:MRK261030C00145000', otype='call', delta=0.35)]})
        pulse = _pulse(direction='bullish')
        got = fp.suggest_contract(db, c, Cfg(), pulse, NOW)
    assert got is not None and got['type'] == 'call'


def test_suggest_contract_matches_prints_expiry(db):
    # Two expiries listed; the prints' expiry (10/30) wins over 11/20.
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': NOW, 'bid': 144.90, 'ask': 145.10})
        db.put(c, 'chain:MRK', {'asof': NOW, 'contracts': [
            _chain_contract(),
            _chain_contract(symbol='O:MRK261120P00145000', expiry='2026-11-20')]})
        got = fp.suggest_contract(db, c, Cfg(), _pulse(), NOW)
    assert got is not None and got['expiry'] == '2026-10-30'


def test_suggest_contract_none_without_chain(db):
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': NOW, 'bid': 144.90, 'ask': 145.10})
        assert fp.suggest_contract(db, c, Cfg(), _pulse(), NOW) is None


def test_suggest_contract_none_without_spot(db):
    with db.tx() as c:
        db.put(c, 'chain:MRK', {'asof': NOW, 'contracts': [_chain_contract()]})
        assert fp.suggest_contract(db, c, Cfg(), _pulse(), NOW) is None


def test_suggest_contract_none_when_expiry_unparseable(db):
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': NOW, 'bid': 144.90, 'ask': 145.10})
        db.put(c, 'chain:MRK', {'asof': NOW, 'contracts': [_chain_contract()]})
        pulse = _pulse(expiry='not-a-date')
        assert fp.suggest_contract(db, c, Cfg(), pulse, NOW) is None


def test_format_message_includes_suggestion():
    payload = {'symbol': 'MRK', 'direction': 'bearish', 'directional_premium': 1735000,
               'print_count': 2, 'max_score': 92,
               'top_prints': [{'option_type': 'CALL', 'strike': 147.0, 'expiry': '10/30/26',
                               'premium': 1149000, 'score': 92}],
               'suggested_contract': {'symbol': 'O:MRK261030P00145000', 'underlying': 'MRK',
                                      'expiry': '2026-10-30', 'strike': 145.0,
                                      'type': 'put', 'delta': -0.35}}
    text = fp.format_message(payload)['content']
    assert 'Suggested: MRK 10/30 145 PUT' in text
    assert 'research only' in text


def test_format_message_omits_suggestion_when_absent():
    payload = {'symbol': 'MRK', 'direction': 'bearish', 'directional_premium': 1735000,
               'print_count': 2, 'max_score': 92, 'top_prints': [],
               'suggested_contract': None}
    text = fp.format_message(payload)['content']
    assert 'Suggested:' not in text


def test_suggest_contract_none_when_spot_stale(db):
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': NOW - 900, 'bid': 144.90, 'ask': 145.10})
        db.put(c, 'chain:MRK', {'asof': NOW, 'contracts': [_chain_contract()]})
        assert fp.suggest_contract(db, c, Cfg(), _pulse(), NOW) is None


def test_suggest_contract_uses_live_spot_for_strike(db):
    # Spot 140 -> 145 put is >3% away and excluded; the ATM 140 put wins.
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': NOW, 'bid': 139.90, 'ask': 140.10})
        db.put(c, 'chain:MRK', {'asof': NOW, 'contracts': [
            _chain_contract(),
            _chain_contract(symbol='O:MRK261030P00140000', strike=140.0, delta=-0.50)]})
        got = fp.suggest_contract(db, c, Cfg(), _pulse(), NOW)
    assert got is not None and got['strike'] == 140.0


# --- paper tracking ---

class TrackCfg(Cfg):
    flow_pulse_track_enabled = True
    watch_symbols = ('HOOD', 'COIN', 'SPY', 'MRK')


def _quoted_contract(bid=2.90, ask=3.10, **kw):
    c = _chain_contract(**kw)
    c['quote'] = {'ts': NOW, 'bid': bid, 'ask': ask}
    return c


def _seed_track_market(db, bid=2.90, ask=3.10, spot_bid=139.90, spot_ask=140.10,
                       now=NOW):
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': now, 'bid': spot_bid, 'ask': spot_ask})
        db.put(c, 'chain:MRK', {'asof': now, 'contracts': [
            _quoted_contract(symbol='O:MRK261030P00140000', strike=140.0,
                             delta=-0.50, bid=bid, ask=ask)]})
        pulse = _pulse()
        pulse['suggested_contract'] = fp.suggest_contract(db, c, TrackCfg(), pulse, now)
        return pulse


def test_open_tracking_records_entry(db):
    key = 'flowpulse:2026-10-05:MRK:bearish'
    with db.tx() as c:
        pulse = _seed_track_market(db)
        got = fp.open_tracking(db, c, pulse, key, NOW)
    assert got == key
    with db.tx() as c:
        rec = db.get(c, 'flow_pulse_track:' + key)
    assert rec['status'] == 'open'
    assert rec['contract'] == 'O:MRK261030P00140000'
    assert rec['entry'] == 3.11            # ask 3.10 + $0.01 slippage
    assert rec['horizon_at'] == NOW + 14 * 86400
    assert rec['spot_at_fire'] == 140.0


def test_open_tracking_skips_without_suggestion(db):
    with db.tx() as c:
        got = fp.open_tracking(db, c, _pulse(), 'flowpulse:x', NOW)
    assert got is None
    with db.tx() as c:
        assert db.prefix(c, 'flow_pulse_track:') == {}


def test_open_tracking_skips_without_entry_quote(db):
    # Chain has the contract but no quote -> no track (can't price entry).
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': NOW, 'bid': 139.90, 'ask': 140.10})
        db.put(c, 'chain:MRK', {'asof': NOW, 'contracts': [
            _chain_contract(symbol='O:MRK261030P00140000', strike=140.0, delta=-0.50)]})
        pulse = _pulse()
        pulse['suggested_contract'] = fp.suggest_contract(db, c, TrackCfg(), pulse, NOW)
        assert fp.open_tracking(db, c, pulse, 'flowpulse:x', NOW) is None


def test_evaluate_tracking_ignores_before_horizon(db):
    key = 'flowpulse:2026-10-05:MRK:bearish'
    with db.tx() as c:
        pulse = _seed_track_market(db)
        fp.open_tracking(db, c, pulse, key, NOW)
    with db.tx() as c:
        res = fp.evaluate_tracking(db, c, TrackCfg(), NOW + 86400)
    assert res['ran'] and res['closed'] == 0
    with db.tx() as c:
        assert db.get(c, 'flow_pulse_track:' + key)['status'] == 'open'


def test_evaluate_tracking_closes_win_at_horizon(db):
    key = 'flowpulse:2026-10-05:MRK:bearish'
    with db.tx() as c:
        pulse = _seed_track_market(db)
        fp.open_tracking(db, c, pulse, key, NOW)
    later = NOW + 15 * 86400
    with db.tx() as c:
        db.put(c, 'chain:MRK', {'asof': later, 'contracts': [
            _quoted_contract(symbol='O:MRK261030P00140000', strike=140.0,
                             delta=-0.50, bid=3.50, ask=3.60)]})
        res = fp.evaluate_tracking(db, c, TrackCfg(), later)
    assert res['ran'] and res['closed'] == 1
    with db.tx() as c:
        rec = db.get(c, 'flow_pulse_track:' + key)
    assert rec['status'] == 'closed'
    assert rec['outcome'] == 'win'
    # exit 3.50-0.01=3.49, entry 3.11: (3.49-3.11)*100 - 2*0.65 = 36.70
    assert rec['pnl'] == 36.70
    assert rec['return_pct'] == round(100 * 36.70 / 311.0, 2)


def test_track_record_aggregates(db):
    with db.tx() as c:
        pulse = _seed_track_market(db)
        fp.open_tracking(db, c, pulse, 'flowpulse:2026-10-05:MRK:bearish', NOW)
        # Second track opens a week later so it is still open at `later`.
        fp.open_tracking(db, c, pulse, 'flowpulse:2026-10-12:MRK:bearish',
                         NOW + 7 * 86400)
    later = NOW + 15 * 86400
    with db.tx() as c:
        # Win for the first track; the second is still before its horizon.
        db.put(c, 'chain:MRK', {'asof': later, 'contracts': [
            _quoted_contract(symbol='O:MRK261030P00140000', strike=140.0,
                             delta=-0.50, bid=3.50, ask=3.60)]})
        res = fp.evaluate_tracking(db, c, TrackCfg(), later)
        assert res['closed'] == 1
    much_later = NOW + 22 * 86400  # past the second track's horizon
    with db.tx() as c:
        db.put(c, 'chain:MRK', {'asof': much_later, 'contracts': [
            _quoted_contract(symbol='O:MRK261030P00140000', strike=140.0,
                             delta=-0.50, bid=2.00, ask=2.10)]})
        db.put(c, 'flow_pulse:track_eval_at', 0)  # reset throttle
        res = fp.evaluate_tracking(db, c, TrackCfg(), much_later)
        assert res['closed'] == 1
        tr = fp.track_record(db, c)
    assert tr['tracked'] == 2
    assert tr['wins'] == 1
    assert tr['win_rate'] == 0.5
    assert tr['total_pnl'] == round(36.70 + ((1.99 - 3.11) * 100 - 1.30), 2)


def test_scan_opens_tracking_for_fired_pulse(db):
    from compass.market import day as _day
    now = time.time()
    rows = [prow(symbol='MRK', option_type='put', sentiment='bullish',
                 ts=now - 300, expiry=_live_expiry(25), strike=140.0)]
    for r in rows:
        r['payload']['source_ts'] = now - 300
        r['source_ts'] = now - 300
        r['day'] = _day(now)
    _insert_flow(db, rows)
    with db.tx() as c:
        db.put(c, 'quote:MRK', {'ts': now, 'bid': 139.90, 'ask': 140.10})
        db.put(c, 'chain:MRK', {'asof': now, 'contracts': [
            _quoted_contract(symbol='O:MRK261030P00140000', strike=140.0,
                             delta=-0.50)]})
        out = fp.scan(db, c, TrackCfg(), now)
    assert out['ran'] and out['fired'] == 1
    with db.tx() as c:
        tracks = db.prefix(c, 'flow_pulse_track:')
    assert len(tracks) == 1
    rec = next(iter(tracks.values()))
    assert rec['symbol'] == 'MRK' and rec['direction'] == 'bearish'
    assert rec['status'] == 'open'
