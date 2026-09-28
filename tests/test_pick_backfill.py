"""On-demand evidence backfill for the pick checker. Network is mocked."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from compass.store import Store
from compass import pick_backfill as bf
from compass import pick_check as pc


NOW = datetime(2026, 9, 28, 15, 0, tzinfo=timezone.utc).timestamp()  # Mon 10:00 CDT
ISO = datetime(2026, 9, 28, 14, 59, tzinfo=timezone.utc).isoformat()


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'backfill.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def cfg(**over):
    base = dict(matrix='tm-key', alpaca_key='ak', alpaca_secret='as',
                feed='sip', gap_large_caps='', gap_min_gap=0.015,
                pick_backfill=True)
    base.update(over)
    return SimpleNamespace(**base)


def apex_payload(symbol='XYZ', spot=100.0):
    return {'success': True,
            'data': {'symbol': symbol, 'spotPrice': spot,
                     'snapshotTime': ISO, 'mode': 'intraday',
                     'expirationsUsed': ['2026-10-02'],
                     'gammaFlip': 101.0,
                     'levels': [{'strike': 99.0, 'score': 60.0,
                                  'netGEX': 500000.0, 'totalOI': 1000},
                                 {'strike': 102.0, 'score': 90.0,
                                  'netGEX': 2000000.0, 'totalOI': 4000}]},
            'freshness': {}}


def minute_bars_payload(symbol='XYZ', base_ts=None):
    base_ts = base_ts if base_ts is not None else NOW - 600
    rows = []
    for i in range(10):
        t = base_ts + i * 60
        c = 99.0 + i * 0.2  # rising toward the 102 magnet
        rows.append({'t': datetime.fromtimestamp(t, tz=timezone.utc).isoformat(),
                     'o': c - 0.1, 'h': c + 0.1, 'l': c - 0.2, 'c': c,
                     'v': 1000 + i * 10})
    return {'bars': {symbol: rows}}


def daily_bars_payload(symbol='XYZ', sessions=130):
    rows = []
    day0 = datetime(2026, 5, 1, 20, 0, tzinfo=timezone.utc)
    c = 80.0
    for i in range(sessions):
        t = day0 + timedelta(days=i)
        c += 0.15
        rows.append({'t': t.isoformat(), 'o': c - 0.5, 'h': c + 0.6,
                     'l': c - 0.7, 'c': c, 'v': 5000000})
    return {'bars': {symbol: rows}}


def flow_payload(symbol='XYZ'):
    return {'success': True, 'total': 1, 'page': 1, 'pageSize': 100,
            'data': [{'id': 'p1', 'ticker': symbol, 'tradeTime': ISO,
                       'premium': 250000.0, 'type': 'call', 'strike': 100.0,
                       'expiry': '2026-10-02', 'volume': 50, 'openInterest': 200,
                       'tradeType': 'sweep', 'sentiment': 'bullish', 'score': 90.0,
                       'flowDescription': 'test print', 'isRepeatFlow': False,
                       'oiConfirmation': True}],
            'filters': {}}


def mock_http(monkeypatch, fail=()):
    """Route bf._http_get_json to fixtures; fail names a subset to explode."""
    def fake(url, headers, timeout_s):
        if 'unusual-activity' in url:
            if 'flow' in fail:
                raise bf._BackfillError('flow down')
            return flow_payload()
        if '/apex' in url:
            if 'apex' in fail:
                raise bf._BackfillError('apex down')
            return apex_payload()
        if 'data.alpaca.markets' in url:
            if 'bars' in fail:
                raise bf._BackfillError('bars down')
            return (daily_bars_payload() if 'timeframe=1Day' in url
                    else minute_bars_payload())
        raise AssertionError('unexpected url ' + url)
    monkeypatch.setattr(bf, '_http_get_json', fake)


# --- fetchers ----------------------------------------------------------------

def test_fetch_apex_parses_and_marks(monkeypatch):
    monkeypatch.setattr(bf, '_http_get_json', lambda u, h, t: apex_payload())
    apex = bf.fetch_apex('XYZ', 'k', 5, NOW)
    assert apex['symbol'] == 'XYZ'
    assert apex['spot'] == 100.0
    assert len(apex['levels']) == 3  # 2 apex + gamma flip
    assert apex['backfilled'] is True and apex['backfilled_at'] == NOW


def test_fetch_apex_symbol_mismatch(monkeypatch):
    monkeypatch.setattr(bf, '_http_get_json',
                        lambda u, h, t: apex_payload(symbol='ABC'))
    with pytest.raises(bf._BackfillError):
        bf.fetch_apex('XYZ', 'k', 5, NOW)


def test_fetch_bars_parses_alpaca(monkeypatch):
    monkeypatch.setattr(bf, '_http_get_json',
                        lambda u, h, t: minute_bars_payload())
    items = bf.fetch_bars('XYZ', 'k', 's', 'sip', '1Min', 2, 5)
    assert len(items) == 10
    assert items[0][0] == 'XYZ'
    assert items[0][1] < items[-1][1]
    assert set(items[0][2]) == {'o', 'h', 'l', 'c', 'v'}


# --- orchestration ------------------------------------------------------------

def test_backfill_writes_scanner_keys(db, monkeypatch):
    mock_http(monkeypatch)
    with db.tx() as c:
        out = bf.backfill(db, c, cfg(), 'XYZ', {'apex', 'bars', 'flow'}, NOW)
    assert sorted(out['fetched']) == ['apex', 'daily_bars', 'flow', 'minute_bars']
    assert out['errors'] == {}
    with db.tx() as c:
        apex = db.get(c, 'apex:XYZ')
        assert apex['backfilled'] is True
        assert db.get(c, 'bar_window:XYZ')  # minute bars merged
        assert db.recent(c, 'daily', 'XYZ', limit=5)  # daily bars stored
        flow = db.get(c, 'matrix:unusual_activity')
        assert flow['backfilled'] is True and len(flow['rows']) == 1
        rec = db.get(c, 'pick_backfill:XYZ')
        assert rec['succeeded_at'] == NOW
        assert rec['fetched']['apex'] == NOW


def test_ttl_suppresses_refetch(db, monkeypatch):
    mock_http(monkeypatch)
    with db.tx() as c:
        db.put(c, 'pick_backfill:XYZ',
               {'attempted_at': NOW - 60, 'succeeded_at': NOW - 60,
                'fetched': {'apex': NOW - 60, 'minute_bars': NOW - 60,
                            'daily_bars': NOW - 60, 'flow': NOW - 60}})
    monkeypatch.setattr(bf, '_http_get_json',
                        lambda u, h, t: (_ for _ in ()).throw(
                            AssertionError('must not fetch')))
    with db.tx() as c:
        out = bf.backfill(db, c, cfg(), 'XYZ', {'apex', 'bars', 'flow'}, NOW)
    assert out['fetched'] == []


def test_fail_cooldown(db, monkeypatch):
    with db.tx() as c:
        db.put(c, 'pick_backfill:XYZ', {'attempted_at': NOW - 60})  # no success
    monkeypatch.setattr(bf, '_http_get_json',
                        lambda u, h, t: (_ for _ in ()).throw(
                            AssertionError('must not fetch')))
    with db.tx() as c:
        out = bf.backfill(db, c, cfg(), 'XYZ', {'apex'}, NOW)
    assert out['fetched'] == [] and out['errors'] == {'_': 'cooldown'}


def test_backfill_never_raises(db, monkeypatch):
    monkeypatch.setattr(bf, 'fetch_apex',
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('boom')))
    monkeypatch.setattr(bf, '_http_get_json',
                        lambda u, h, t: (_ for _ in ()).throw(RuntimeError('boom')))
    with db.tx() as c:
        out = bf.backfill(db, c, cfg(), 'XYZ', {'apex', 'bars', 'flow'}, NOW)
    assert out['fetched'] == []
    assert 'apex' in out['errors']  # recorded, not raised


def test_backfill_missing_credentials(db):
    with db.tx() as c:
        out = bf.backfill(db, c, cfg(matrix='', alpaca_key='', alpaca_secret=''),
                          'XYZ', {'apex', 'bars'}, NOW)
    assert out['fetched'] == []
    assert 'apex' in out['errors'] and 'minute_bars' in out['errors']


# --- triggers: missing data vs no signal --------------------------------------

def test_flow_needs_only_when_feed_absent_or_stale(db):
    with db.tx() as c:
        assert bf.flow_needs(db, c, NOW) is True  # missing entirely
        # Fresh feed with zero prints for XYZ: a real "no flow" answer.
        db.put(c, 'matrix:unusual_activity',
               {'source': 'tradermatrix', 'received': NOW, 'rows': []})
        assert bf.flow_needs(db, c, NOW) is False
        db.put(c, 'matrix:unusual_activity',
               {'source': 'tradermatrix', 'received': NOW - 3600, 'rows': []})
        assert bf.flow_needs(db, c, NOW) is True  # stale


def test_apex_needs_missing_or_stale(db):
    with db.tx() as c:
        assert bf.apex_needs(db, c, 'XYZ', NOW) is True
        db.put(c, 'apex:XYZ', {'symbol': 'XYZ', 'received': NOW})
        assert bf.apex_needs(db, c, 'XYZ', NOW) is False
        db.put(c, 'apex:XYZ', {'symbol': 'XYZ', 'received': NOW - 3600})
        assert bf.apex_needs(db, c, 'XYZ', NOW) is True


def test_gap_needs_only_when_unevaluated(db):
    from compass.market import day
    with db.tx() as c:
        assert bf.gap_needs(db, c, 'XYZ', NOW) is True
        # An evaluated-but-excluded state is a real answer, not missing data.
        db.put(c, 'gap_cont:XYZ:%s' % day(NOW),
               {'symbol': 'XYZ', 'day': day(NOW), 'stage': 'excluded',
                'excluded': 'gap_below_minimum'})
        assert bf.gap_needs(db, c, 'XYZ', NOW) is False


# --- pick_check wiring ---------------------------------------------------------

def test_check_completes_on_network_failure(db, monkeypatch):
    monkeypatch.setattr(bf, '_http_get_json',
                        lambda u, h, t: (_ for _ in ()).throw(
                            bf._BackfillError('vendor down')))
    with db.tx() as c:
        out = pc.check(db, c, NOW, 'XYZ', 'long', 100.0, 104.0, 'sal', cfg())
    assert out['pillars']['apex']['alignment'] == 'no_data'
    assert out['pillars']['tape']['alignment'] == 'no_data'
    assert out['backfilled'] == []
    assert out['note'].startswith('Research evidence only')


def test_check_backfills_apex_end_to_end(db, monkeypatch):
    mock_http(monkeypatch)
    with db.tx() as c:
        out = pc.check(db, c, NOW, 'XYZ', 'long', 100.0, 104.0, 'sal', cfg())
    assert 'apex' in out['backfilled']
    apex = out['pillars']['apex']
    assert apex['alignment'] == 'info'  # magnet rows present; pin/stall note
    assert 'sits between spot and the target' in (apex['note'] or '')
    assert apex['detail']['backfilled'] is True
    assert apex['detail']['nearest_above']['magnet'] == 102.0
    assert apex['detail']['signal'] == 'approaching'
    assert out['spot'] == 100.0
    # Tape gets unconfirmed flow context from the backfilled feed.
    assert out['pillars']['tape']['alignment'] == 'info'
    assert out['pillars']['tape']['detail']['backfilled'] is True
    # Gap evaluated for real: -0.6% open vs the 1.5% minimum -> excluded.
    gap = out['pillars']['gap']
    assert gap['alignment'] == 'neutral'
    assert gap['detail']['stage'] == 'excluded'
    assert gap['detail']['backfilled'] is True
    # Fixture uptrend has no close-only range break: evaluated, none found.
    brk = out['pillars']['breakout']
    assert brk['alignment'] == 'no_data'
    assert brk['detail']['backfilled'] is True
    assert out['backfilled'] == ['apex', 'breakout', 'gap', 'tape']
    assert gap['detail']['backfilled'] is True


def test_check_partial_backfill_still_improves(db, monkeypatch):
    mock_http(monkeypatch, fail=('bars',))  # apex + flow work, bars down
    with db.tx() as c:
        out = pc.check(db, c, NOW, 'XYZ', 'long', 100.0, 104.0, 'sal', cfg())
    assert out['backfilled'] == ['apex', 'tape']
    # No bars -> no session signal yet, but no longer no_data.
    assert out['pillars']['apex']['alignment'] == 'info'
    assert out['spot'] == 100.0
    # Gap/breakout could not be evaluated without bars: still no_data,
    # and not claimed as backfilled.
    assert out['pillars']['gap']['alignment'] == 'no_data'
    assert out['pillars']['breakout']['alignment'] == 'no_data'


def test_tape_info_upgrade_without_fetch(db, monkeypatch):
    """Fresh feed, zero trials, prints present -> info, and no refetch."""
    with db.tx() as c:
        part = flow_payload()
        rows = [{'vendor_id': 'p1', 'symbol': 'XYZ', 'source_ts': NOW - 3600,
                 'received': NOW, 'premium': 250000.0, 'option_type': 'call'}]
        db.put(c, 'matrix:unusual_activity',
               {'source': 'tradermatrix', 'received': NOW, 'rows': rows})
    monkeypatch.setattr(bf, '_http_get_json',
                        lambda u, h, t: (_ for _ in ()).throw(
                            AssertionError('feed is fresh; must not refetch')))
    with db.tx() as c:
        out = pc.check(db, c, NOW, 'XYZ', 'long', 100.0, 104.0, 'sal', cfg())
    tape = out['pillars']['tape']
    assert tape['alignment'] == 'info'
    assert 'unconfirmed' in tape['note']
    assert tape['detail']['prints'] == 1
    # Standing feed data: section improves, but nothing was backfilled.
    assert out['backfilled'] == []
    assert tape['detail'].get('backfilled') is not True


def test_check_without_cfg_unchanged(db):
    """Legacy call path: no cfg -> no backfill, all no_data as before."""
    with db.tx() as c:
        out = pc.check(db, c, NOW, 'XYZ', 'long', 100.0, 104.0, 'sal')
    assert out['pillars']['apex']['alignment'] == 'no_data'
    assert out['backfilled'] == []


def test_backfill_flag_off(db, monkeypatch):
    mock_http(monkeypatch)
    with db.tx() as c:
        out = pc.check(db, c, NOW, 'XYZ', 'long', 100.0, 104.0, 'sal',
                       cfg(pick_backfill=False))
    assert out['backfilled'] == []
    assert out['pillars']['apex']['alignment'] == 'no_data'
