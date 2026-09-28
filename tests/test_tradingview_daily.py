"""Tests for compass/tradingview_daily.py and its htf_levels wiring.

No live network: protocol parsing uses fixture messages, the websocket
client is exercised with a fake socket, and fetch paths are monkeypatched.
"""
import json
import time
from types import SimpleNamespace

import pytest

from compass import tradingview_daily as tv
from compass import htf_levels as ht
from compass.store import Store
from compass.market import CT
from datetime import datetime

SYMBOL = 'ES.c.0'
NOW = datetime(2026, 9, 28, 10, 0, tzinfo=CT).timestamp()  # Monday


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'tvdaily.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def cfg(**kw):
    base = dict(ict_htf_levels=True, ict_symbols=(SYMBOL,),
                ict_htf_fractal_n=2, ict_htf_tradingview=False,
                ict_tv_cache_dir='')
    base.update(kw)
    return SimpleNamespace(**base)


def sample_bars(n=10, start_ts=1700000000):
    return [{'ts': start_ts + i * 86400, 'o': 100.0 + i, 'h': 102.0 + i,
             'l': 99.0 + i, 'c': 101.0 + i, 'v': 1000} for i in range(n)]


# --- symbol mapping ---

def test_tv_symbol_for():
    assert tv.tv_symbol_for('NQ.c.0') == 'CME_MINI:NQ1!'
    assert tv.tv_symbol_for('ES.c.0') == 'CME_MINI:ES1!'
    assert tv.tv_symbol_for('GC.v.0') == 'COMEX:GC1!'
    assert tv.tv_symbol_for('SI.v.0') == 'COMEX:SI1!'
    assert tv.tv_symbol_for('CL.v.0') == 'NYMEX:CL1!'
    assert tv.tv_symbol_for('NQ') == 'CME_MINI:NQ1!'       # bare root works
    assert tv.tv_symbol_for('ZZZ.c.0') is None
    assert tv.tv_symbol_for('') is None
    assert tv.tv_symbol_for(None) is None


# --- wire framing ---

def test_frame_roundtrip():
    msgs = [{"m": "set_auth_token", "p": ["unauthorized_user_token"]},
            {"m": "chart_create_session", "p": ["cs_abc", ""]}]
    wire = tv.frame_messages(msgs)
    payloads, rest = tv.split_frames(wire)
    assert rest == ''
    assert [json.loads(p) for p in payloads] == msgs


def test_split_frames_incomplete_tail():
    wire = tv.frame_messages([{"m": "a"}])
    payloads, rest = tv.split_frames(wire + '~m~10~m~{"m":"')
    assert len(payloads) == 1
    assert rest == '~m~10~m~{"m":"'


def test_split_frames_ping_passthrough():
    wire = tv.frame_messages(['~h~1', {"m": "a"}])
    payloads, rest = tv.split_frames(wire)
    assert payloads[0] == '~h~1' and tv.is_ping(payloads[0])
    assert json.loads(payloads[1]) == {"m": "a"}
    assert rest == ''


# --- bar parsing ---

def test_parse_array_style():
    data = {"s1": {"t": [1700000000, 1700086400],
                   "o": [100.0, 101.0], "h": [102.0, 103.0],
                   "l": [99.0, 100.0], "c": [101.0, 102.0],
                   "v": [1000, 1100]}}
    bars = tv.parse_series_payload(data)
    assert len(bars) == 2
    assert bars[0] == {'ts': 1700000000.0, 'o': 100.0, 'h': 102.0,
                       'l': 99.0, 'c': 101.0, 'v': 1000.0}
    assert bars[1]['ts'] > bars[0]['ts']  # ascending


def test_parse_row_style():
    data = {"s1": {"s": [{"i": 0, "v": [1700000000, 100, 102, 99, 101, 1000]},
                         {"i": 1, "v": [1700086400, 101, 103, 100, 102]}]}}
    bars = tv.parse_series_payload(data)
    assert len(bars) == 2
    assert bars[1]['v'] is None  # volume optional
    assert bars[0]['c'] == 101.0


def test_parse_skips_malformed():
    data = {"s1": {"t": [1700000000, 1700086400, 1700172800],
                   "o": [100.0, None, 102.0], "h": [102.0, 103.0, 104.0],
                   "l": [99.0, 100.0, 101.0], "c": [101.0, 102.0, 103.0]},
            "s2": {"s": [{"i": 0}, {"v": [1, 2]}]},
            "junk": [1, 2, 3]}
    bars = tv.parse_series_payload(data)
    assert len(bars) == 2  # the None-open row is dropped
    assert tv.parse_series_payload(None) == []
    assert tv.parse_series_payload("nope") == []


# --- websocket frame encoding (masked client frames) ---

class _FakeSock:
    def __init__(self):
        self.sent = b''
    def sendall(self, data):
        self.sent += data
    def close(self):
        pass


def test_send_text_masked_frame():
    client = tv._WSClient()
    client.sock = _FakeSock()
    client.send_text("hi")
    raw = client.sock.sent
    assert raw[0] == 0x81            # FIN + text opcode
    assert raw[1] & 0x80            # client frames are masked
    assert (raw[1] & 0x7F) == 2     # payload length 2
    mask = raw[2:6]
    payload = bytes(b ^ mask[i % 4] for i, b in enumerate(raw[6:8]))
    assert payload == b"hi"


# --- cache ---

def test_cache_roundtrip(tmp_path):
    bars = sample_bars(12)
    tv.save_cached_bars(SYMBOL, bars, 'CME_MINI:ES1!', cache_dir=str(tmp_path))
    loaded = tv.load_cached_bars(SYMBOL, cache_dir=str(tmp_path))
    assert len(loaded) == 12
    assert loaded[0]['ts'] < loaded[-1]['ts']
    assert set(loaded[0]) == {'ts', 'o', 'h', 'l', 'c'}  # v stripped
    assert tv.cache_fresh(SYMBOL, cache_dir=str(tmp_path))


def test_cache_missing_and_corrupt(tmp_path):
    assert tv.load_cached_bars('NOPE.c.0', cache_dir=str(tmp_path)) == []
    assert tv.cache_fresh('NOPE.c.0', cache_dir=str(tmp_path)) is False
    bad = tmp_path / 'BAD.c.0.json'
    bad.write_text('{not json')
    assert tv.load_cached_bars('BAD.c.0', cache_dir=str(tmp_path)) == []
    assert tv.cache_fresh('BAD.c.0', cache_dir=str(tmp_path)) is False


def test_cache_stale(tmp_path):
    bars = sample_bars(5)
    path = tv.cache_file(SYMBOL, cache_dir=str(tmp_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"alias": SYMBOL, "tv_symbol": "X",
                                "fetched_at": time.time() - 99999,
                                "bars": bars}))
    assert tv.cache_fresh(SYMBOL, cache_dir=str(tmp_path)) is False
    # stale bars are still loadable (scan degrades gracefully, not blindly)
    assert len(tv.load_cached_bars(SYMBOL, cache_dir=str(tmp_path))) == 5


def test_daily_bars_for_scan_never_raises(tmp_path):
    assert tv.daily_bars_for_scan('MISSING.c.0', cache_dir=str(tmp_path)) == []


# --- fetch graceful failures (no network) ---

def test_fetch_unmapped_symbol():
    res = tv.fetch_daily_bars('ZZZ.c.0')
    assert res == {"ok": False, "bars": [], "reason": "unmapped_symbol",
                   "tv_symbol": None}


def test_fetch_network_error_never_raises(monkeypatch):
    def boom(*a, **k):
        raise OSError("no route")
    monkeypatch.setattr(tv, "_fetch_tv_bars", boom)
    res = tv.fetch_daily_bars('ES.c.0')
    assert res["ok"] is False and res["bars"] == []
    assert res["tv_symbol"] == 'CME_MINI:ES1!'


def test_refresh_skips_fresh_cache(monkeypatch, tmp_path):
    tv.save_cached_bars(SYMBOL, sample_bars(8), 'CME_MINI:ES1!',
                        cache_dir=str(tmp_path))
    def boom(*a, **k):
        raise AssertionError("network must not be touched")
    monkeypatch.setattr(tv, "fetch_daily_bars", boom)
    res = tv.refresh([SYMBOL], cache_dir=str(tmp_path), pause_s=0)
    assert res[SYMBOL]["ok"] is True and res[SYMBOL]["cached"] is True


def test_refresh_fetches_when_stale(monkeypatch, tmp_path):
    bars = sample_bars(8)
    def fake_fetch(alias, n_bars=365, timeout=90):
        return {"ok": True, "bars": bars, "reason": None,
                "tv_symbol": "CME_MINI:ES1!"}
    monkeypatch.setattr(tv, "fetch_daily_bars", fake_fetch)
    res = tv.refresh([SYMBOL], cache_dir=str(tmp_path), force=True, pause_s=0)
    assert res[SYMBOL]["ok"] is True and res[SYMBOL]["cached"] is False
    assert len(tv.load_cached_bars(SYMBOL, cache_dir=str(tmp_path))) == 8


def test_refresh_failure_recorded(monkeypatch, tmp_path):
    def fake_fetch(alias, n_bars=365, timeout=90):
        return {"ok": False, "bars": [], "reason": "no_bars",
                "tv_symbol": "CME_MINI:ES1!"}
    monkeypatch.setattr(tv, "fetch_daily_bars", fake_fetch)
    res = tv.refresh([SYMBOL], cache_dir=str(tmp_path), force=True, pause_s=0)
    assert res[SYMBOL]["ok"] is False
    assert res[SYMBOL]["reason"] == "no_bars"
    assert tv.load_cached_bars(SYMBOL, cache_dir=str(tmp_path)) == []


# --- htf_levels wiring ---

def test_htf_tv_flag_off_unchanged(db):
    with db.tx() as c:
        res = ht.scan(db, c, cfg(), NOW)
        assert res['ran'] is False and res['reason'] == 'no_bars'
        assert res['excluded'][SYMBOL] == 'no_daily'


def test_htf_tv_flag_on_no_cache_graceful(db, tmp_path):
    with db.tx() as c:
        res = ht.scan(db, c, cfg(ict_htf_tradingview=True,
                                 ict_tv_cache_dir=str(tmp_path)), NOW)
        assert res['ran'] is False and res['reason'] == 'no_bars'
        assert res['excluded'][SYMBOL] == 'no_daily'


def test_htf_tv_flag_on_uses_cache(db, tmp_path):
    tv.save_cached_bars(SYMBOL, sample_bars(12), 'CME_MINI:ES1!',
                        cache_dir=str(tmp_path))
    with db.tx() as c:
        res = ht.scan(db, c, cfg(ict_htf_tradingview=True,
                                 ict_tv_cache_dir=str(tmp_path)), NOW)
        assert res['ran'] is True and res['rows'] > 0
        latest = db.get(c, 'ict_htf_levels:latest')
        assert latest['sources'][SYMBOL] == 'tradingview'
        kinds = {r['kind'] for r in latest['levels']}
        assert {'pdh', 'pdl', 'pwh', 'pwl'} <= kinds
    with db.tx() as c:
        disp = ht.display(db, c, NOW)
        assert disp['sources'][SYMBOL] == 'tradingview'


def test_htf_tv_cache_preferred_over_db(db, tmp_path):
    # db daily rows exist but TV cache wins when the flag is on.
    tv.save_cached_bars(SYMBOL, sample_bars(12), 'CME_MINI:ES1!',
                        cache_dir=str(tmp_path))
    with db.tx() as c:
        for i in range(6):
            d = datetime(2026, 9, 21, 16, 0, tzinfo=CT)
            ts = d.timestamp() + i * 86400
            db.append(c, 'daily', 'test', SYMBOL, ts,
                      {'o': 1.0, 'h': 2.0, 'l': 0.5, 'c': 1.5, 'v': 10},
                      key='daily:%s:%d' % (SYMBOL, i))
        res = ht.scan(db, c, cfg(ict_htf_tradingview=True,
                                 ict_tv_cache_dir=str(tmp_path)), NOW)
        latest = db.get(c, 'ict_htf_levels:latest')
        assert latest['sources'][SYMBOL] == 'tradingview'
        pdh = [r for r in latest['levels'] if r['kind'] == 'pdh'][0]
        assert pdh['price'] > 100  # from TV bars, not the db's 2.0 rows


def test_htf_tv_flag_on_db_fallback(db, tmp_path):
    # TV cache empty but db has rows -> db source, still produces levels.
    with db.tx() as c:
        for i in range(6):
            d = datetime(2026, 9, 21, 16, 0, tzinfo=CT)
            ts = d.timestamp() + i * 86400
            db.append(c, 'daily', 'test', SYMBOL, ts,
                      {'o': 100.0, 'h': 102.0, 'l': 99.0, 'c': 101.0, 'v': 10},
                      key='daily:%s:%d' % (SYMBOL, i))
        res = ht.scan(db, c, cfg(ict_htf_tradingview=True,
                                 ict_tv_cache_dir=str(tmp_path)), NOW)
        assert res['ran'] is True
        latest = db.get(c, 'ict_htf_levels:latest')
        assert latest['sources'][SYMBOL] == 'db'
