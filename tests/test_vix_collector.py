"""Tests for the VIX data pipeline in compass/vix_regime.py."""
import os
import sys
import types

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from compass.vix_regime import (fetch_vix, scan, get_vix, classify_regime,
                                should_run_detector, get_regime_info,
                                SCAN_THROTTLE, SNAPSHOT_URL)


class FakeDB:
    def __init__(self):
        self.kv = {}
        self.health_calls = []

    def get(self, c, key, default=None):
        return self.kv.get(key, default)

    def put(self, c, key, value):
        self.kv[key] = value

    def health(self, name, status, detail, **extra):
        self.health_calls.append((name, status, detail))


class FakeCfg:
    def __init__(self, massive="k", vix_regime_enabled=False):
        self.massive = massive
        self.vix_regime_enabled = vix_regime_enabled


def _fake_httpx(payload, raise_for=None):
    """Build a fake httpx module whose Client returns the given payload."""
    class FakeResp:
        def raise_for_status(self):
            if raise_for:
                raise raise_for

        def json(self):
            return payload

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None):
            assert url == SNAPSHOT_URL
            assert params["ticker"] == "I:VIX"
            assert params["apiKey"] == "k"
            return FakeResp()

    mod = types.ModuleType("httpx")
    mod.Client = FakeClient
    return mod


_VIX_PAYLOAD = {
    "results": [{
        "value": 18.42,
        "last_updated": 1790697928000000000,
        "timeframe": "REAL-TIME",
        "name": "Cboe Volatility Index",
        "ticker": "I:VIX",
        "market_status": "closed",
        "type": "indices",
    }],
    "status": "OK",
}


def test_fetch_vix_parses_massive_snapshot(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(_VIX_PAYLOAD))
    row = fetch_vix("k")
    assert row is not None
    assert row["value"] == 18.42
    assert row["market_status"] == "closed"
    assert row["source"] == "massive"


def test_fetch_vix_no_results(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx",
                        _fake_httpx({"results": [], "status": "OK"}))
    assert fetch_vix("k") is None


def test_scan_writes_vix_latest(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(_VIX_PAYLOAD))
    db, cfg = FakeDB(), FakeCfg()
    out = scan(db, None, cfg, 1000.0)
    assert out == {"ran": True, "fetched": True, "vix": 18.42}
    # get_vix probes vix:latest first and reads the 'value' field
    assert get_vix(db, None) == 18.42
    assert db.health_calls[-1][1] == "available"


def test_scan_throttles(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(_VIX_PAYLOAD))
    db, cfg = FakeDB(), FakeCfg()
    scan(db, None, cfg, 1000.0)
    out = scan(db, None, cfg, 1000.0 + SCAN_THROTTLE - 1)
    assert out == {"ran": False, "reason": "throttled"}


def test_scan_without_api_key():
    db, cfg = FakeDB(), FakeCfg(massive="")
    assert scan(db, None, cfg, 1000.0) == {"ran": False, "reason": "no_api_key"}


def test_scan_fetch_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx",
                        _fake_httpx({}, raise_for=RuntimeError("boom")))
    db, cfg = FakeDB(), FakeCfg()
    out = scan(db, None, cfg, 1000.0)
    assert out["ran"] is True and out["fetched"] is False
    assert db.health_calls[-1][1] == "error"


def test_regime_flow_end_to_end(monkeypatch):
    """Stored VIX feeds the regime classifier and detector gating."""
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx(_VIX_PAYLOAD))
    db, cfg = FakeDB(), FakeCfg(vix_regime_enabled=True)
    scan(db, None, cfg, 1000.0)
    info = get_regime_info(db, None, cfg)
    assert info["enabled"] is True
    assert info["vix"] == 18.42
    assert info["regime"] == classify_regime(18.42) == "normal"
    assert should_run_detector("gap_fade", info["regime"]) is True
    # High VIX disables breakout detectors but keeps fades
    assert should_run_detector("continuation", "high") is False
    assert should_run_detector("gap_fade", "high") is True
    # Low VIX disables fade detectors but keeps breakouts
    assert should_run_detector("gap_fade", "low") is False
    assert should_run_detector("continuation", "low") is True


def test_regime_info_disabled_returns_normal():
    db, cfg = FakeDB(), FakeCfg(vix_regime_enabled=False)
    info = get_regime_info(db, None, cfg)
    assert info == {"enabled": False, "regime": "normal", "vix": None}
