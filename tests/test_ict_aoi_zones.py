from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo
import pytest
from compass.store import Store
from compass import aoi_zones as az

CT = ZoneInfo('America/Chicago')
DAY = datetime(2026, 9, 25, tzinfo=CT)  # Friday
NY_AM_OPEN = datetime(2026, 9, 25, 7, 0, tzinfo=CT).timestamp()
NOW = datetime(2026, 9, 25, 12, 5, tzinfo=CT).timestamp()
SYM = 'NQ.c.0'


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'aoi.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def ny_am_range_bars(open_ts, n=270, lo=90.0, hi=110.0, last_close=95.0):
    """Window rows [ts,o,h,l,c,v]; extremes pinned at the first/last bar."""
    rows = []
    for i in range(n):
        ts = open_ts + i * 60
        o, h, l, c = 100.0, 100.2, 99.8, 100.0
        if i == 0:
            l = lo
        if i == n - 1:
            h, l, c = hi, 99.8, last_close
        rows.append([ts, o, h, l, c, 100])
    return rows


def quiet_dicts(start_ts, n=30, base=100.0, rng=0.4):
    return [{'ts': start_ts + i * 60, 'o': base, 'h': base + rng / 2,
             'l': base - rng / 2, 'c': base} for i in range(n)]


def seed_full(db, c, last_close=95.0):
    """NY AM range + post-session quiet bars + bearish candle + bullish displacement."""
    rows = ny_am_range_bars(NY_AM_OPEN, last_close=last_close)
    t = NY_AM_OPEN + 270 * 60  # 11:30 CT
    for i in range(30):
        ts = t + i * 60
        rows.append([ts, 100.0, 100.2, 99.8, 100.0, 100])
    ts = t + 30 * 60
    rows.append([ts, 100.5, 100.6, 99.9, 100.0, 100])      # bearish candle
    rows.append([ts + 60, 100.0, 103.0, 99.9, 102.8, 100])  # bullish displacement
    db.put(c, 'bar_window:' + SYM, rows)
    return rows


class Cfg:
    ict_aoi_zones = True
    ict_symbols = (SYM,)


# --- range split math ---

def test_range_split_premium_discount_equilibrium():
    bars = az._bars_from_window(ny_am_range_bars(NY_AM_OPEN))
    row = az._classify_symbol(SYM, bars, NOW, 0.05, 1.5, 'ny_am', az.SESSION_DEFS)
    assert row['range_high'] == 110.0 and row['range_low'] == 90.0
    assert row['range_mid'] == pytest.approx(100.0)
    by_kind = {z['zone_kind']: z for z in row['zones']}
    assert by_kind['premium']['top'] == 110.0 and by_kind['premium']['bottom'] == pytest.approx(100.0)
    assert by_kind['discount']['top'] == pytest.approx(100.0) and by_kind['discount']['bottom'] == 90.0
    assert by_kind['equilibrium']['top'] == pytest.approx(101.0)
    assert by_kind['equilibrium']['bottom'] == pytest.approx(99.0)


def test_bias_discount_long_premium_short_equilibrium_neutral():
    for last_close, bias in ((95.0, 'long'), (105.0, 'short'), (100.0, 'neutral')):
        bars = az._bars_from_window(ny_am_range_bars(NY_AM_OPEN, last_close=last_close))
        row = az._classify_symbol(SYM, bars, NOW, 0.05, 1.5, 'ny_am', az.SESSION_DEFS)
        assert row['bias'] == bias, last_close
        assert all(z['bias'] == bias for z in row['zones'])


# --- order block identification ---

def test_find_order_block_bullish_displacement():
    bars = quiet_dicts(1000.0, 30)
    bars.append({'ts': 1000.0 + 30 * 60, 'o': 100.5, 'h': 100.6, 'l': 99.9, 'c': 100.0})
    bars.append({'ts': 1000.0 + 31 * 60, 'o': 100.0, 'h': 103.0, 'l': 99.9, 'c': 102.8})
    ob = az.find_order_block(bars)
    assert ob is not None
    assert ob['zone_kind'] == 'bullish_ob'
    assert ob['top'] == 100.6 and ob['bottom'] == 99.9
    assert ob['bias'] == 'long'
    assert ob['formed_ts'] == 1000.0 + 30 * 60


def test_find_order_block_bearish_displacement():
    bars = quiet_dicts(1000.0, 30)
    bars.append({'ts': 1000.0 + 30 * 60, 'o': 99.5, 'h': 100.1, 'l': 99.4, 'c': 100.0})
    bars.append({'ts': 1000.0 + 31 * 60, 'o': 100.0, 'h': 100.1, 'l': 97.0, 'c': 97.2})
    ob = az.find_order_block(bars)
    assert ob is not None
    assert ob['zone_kind'] == 'bearish_ob'
    assert ob['top'] == 100.1 and ob['bottom'] == 99.4
    assert ob['bias'] == 'short'


def test_find_order_block_none_without_displacement():
    bars = quiet_dicts(1000.0, 40)  # uniform small ranges: nothing >= 1.5x ATR
    assert az.find_order_block(bars) is None


def test_find_order_block_none_when_too_few_bars():
    assert az.find_order_block(quiet_dicts(1000.0, 10)) is None


def test_find_order_block_none_on_doji_displacement():
    bars = quiet_dicts(1000.0, 30)
    bars.append({'ts': 1000.0 + 30 * 60, 'o': 100.0, 'h': 103.0, 'l': 97.0, 'c': 100.0})
    assert az.find_order_block(bars) is None  # huge range but c == o


def test_find_order_block_picks_most_recent_displacement():
    bars = quiet_dicts(1000.0, 30)
    bars.append({'ts': 1000.0 + 30 * 60, 'o': 100.5, 'h': 100.6, 'l': 99.9, 'c': 100.0})
    bars.append({'ts': 1000.0 + 31 * 60, 'o': 100.0, 'h': 103.0, 'l': 99.9, 'c': 102.8})
    bars += quiet_dicts(1000.0 + 32 * 60, 20)
    bars.append({'ts': 1000.0 + 52 * 60, 'o': 100.9, 'h': 101.6, 'l': 100.8, 'c': 101.5})
    bars.append({'ts': 1000.0 + 53 * 60, 'o': 101.0, 'h': 101.1, 'l': 97.5, 'c': 97.8})
    ob = az.find_order_block(bars)
    assert ob['zone_kind'] == 'bearish_ob'
    assert ob['formed_ts'] == 1000.0 + 52 * 60


# --- scan / display ---

def test_scan_disabled_flag(db):
    with db.tx() as c:
        assert az.scan(db, c, SimpleNamespace(ict_aoi_zones=False, ict_symbols=(SYM,)), NOW) == \
            {'ran': False, 'reason': 'disabled'}
        assert az.scan(db, c, SimpleNamespace(), NOW) == {'ran': False, 'reason': 'disabled'}


def test_scan_throttle_and_idempotent_signal(db):
    with db.tx() as c:
        seed_full(db, c)
        assert az.scan(db, c, Cfg(), NOW)['ran'] is True
        assert az.scan(db, c, Cfg(), NOW + 10) == {'ran': False, 'reason': 'throttled'}
        # Re-scan after throttle clears: the same OB must not double-append.
        db.put(c, 'ict_aoi_zones:scanned_at', 0)
        assert az.scan(db, c, Cfg(), NOW + 200)['ran'] is True
        assert len(db.recent(c, 'ict_aoi_signal', limit=10)) == 1


def test_scan_integration_snapshot_and_ob_signal(db):
    with db.tx() as c:
        seed_full(db, c)
        result = az.scan(db, c, Cfg(), NOW)
    assert result == {'ran': True, 'symbols': 1, 'excluded': {}}
    with db.tx() as c:
        latest = db.get(c, 'ict_aoi_zones:latest')
        row = latest['rows'][SYM]
        assert len(row['zones']) == 3
        assert row['order_block']['zone_kind'] == 'bullish_ob'
        assert row['order_block']['bias'] == 'long'
        assert row['bias'] == 'short'  # displacement close 102.8 sits in premium
        signals = db.recent(c, 'ict_aoi_signal', limit=10)
        assert len(signals) == 1
        assert signals[0]['payload']['zone_kind'] == 'bullish_ob'


def test_scan_no_bars_excluded(db):
    with db.tx() as c:
        result = az.scan(db, c, Cfg(), NOW)
    assert result == {'ran': True, 'symbols': 0, 'excluded': {'no_bars': 1}}


def test_scan_no_range_bars_excluded(db):
    with db.tx() as c:
        # Bars only after the NY AM window: nothing fully inside it.
        t = datetime(2026, 9, 25, 12, 0, tzinfo=CT).timestamp()
        db.put(c, 'bar_window:' + SYM, [[t + i * 60, 100, 100.2, 99.8, 100, 10]
                                        for i in range(30)])
        result = az.scan(db, c, Cfg(), NOW)
    assert result == {'ran': True, 'symbols': 0, 'excluded': {'no_range_bars': 1}}


def test_display_payload_shape(db):
    with db.tx() as c:
        seed_full(db, c)
        az.scan(db, c, Cfg(), NOW)
        payload = az.display(db, c, NOW)
    assert payload['asof'] == NOW
    assert payload['range_session'] == 'ny_am'
    assert len(payload['rows']) == 1
    assert len(payload['signals']) == 1
    assert payload['note'] == 'Research evidence only. No auto-admission, no alerts, no trades.'
