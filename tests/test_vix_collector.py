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
    def __init__(self, massive="k", vix_regime_enabled=False,
                 vix_yahoo_fallback=False):
        self.massive = massive
        self.vix_regime_enabled = vix_regime_enabled
        self.vix_yahoo_fallback = vix_yahoo_fallback


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
    assert should_run_detector("turtle_soup", info["regime"]) is True
    # High VIX disables breakout detectors but keeps fades
    assert should_run_detector("continuation", "high") is False
    assert should_run_detector("turtle_soup", "high") is True
    # Low VIX disables fade detectors but keeps breakouts
    assert should_run_detector("turtle_soup", "low") is False
    assert should_run_detector("continuation", "low") is True


def test_regime_info_disabled_returns_normal():
    db, cfg = FakeDB(), FakeCfg(vix_regime_enabled=False)
    info = get_regime_info(db, None, cfg)
    assert info == {"enabled": False, "regime": "normal", "vix": None}


# --- Yahoo fallback (Massive->Alpaca migration, 2026-11-14) ---------------
# Alpaca has no VIX; with VIX_YAHOO_FALLBACK on, scan() serves Yahoo ^VIX
# when Massive is absent or failing. Default off = Massive-only (pinned
# by test_scan_without_api_key / test_scan_fetch_error above).

_YAHOO_VIX_PAYLOAD = {
    "chart": {
        "result": [{
            "meta": {"symbol": "^VIX", "regularMarketPrice": 17.25,
                     "regularMarketTime": 1790697600},
            "timestamp": [1790658000],
            "indicators": {"quote": [{"close": [17.25]}]},
        }],
        "error": None,
    }
}


def _fake_httpx_dispatch(massive_payload=None, massive_raise=None,
                         yahoo_payload=None):
    """Fake httpx whose Client dispatches on the request URL."""
    class FakeResp:
        def __init__(self, payload, raise_for=None):
            self._payload, self._raise = payload, raise_for

        def raise_for_status(self):
            if self._raise:
                raise self._raise

        def json(self):
            return self._payload

    class FakeClient:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get(self, url, params=None, headers=None):
            if "massive.com" in url:
                return FakeResp(massive_payload, massive_raise)
            assert "finance.yahoo.com" in url and "%5EVIX" in url
            return FakeResp(yahoo_payload)

    mod = types.ModuleType("httpx")
    mod.Client = FakeClient
    return mod


def test_fetch_vix_yahoo_parses_chart(monkeypatch):
    from compass.vix_regime import fetch_vix_yahoo
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx_dispatch(
        yahoo_payload=_YAHOO_VIX_PAYLOAD))
    row = fetch_vix_yahoo()
    assert row["value"] == 17.25 and row["source"] == "yahoo"


def test_scan_yahoo_only_when_flag_on_and_no_massive_key(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx_dispatch(
        yahoo_payload=_YAHOO_VIX_PAYLOAD))
    db = FakeDB()
    cfg = FakeCfg(massive="", vix_yahoo_fallback=True)
    out = scan(db, None, cfg, 1000.0)
    assert out == {"ran": True, "fetched": True, "vix": 17.25}
    assert db.kv["vix:latest"]["source"] == "yahoo"
    assert get_vix(db, None) == 17.25


def test_scan_falls_back_to_yahoo_when_massive_fails(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx_dispatch(
        massive_raise=RuntimeError("boom"),
        yahoo_payload=_YAHOO_VIX_PAYLOAD))
    db = FakeDB()
    cfg = FakeCfg(massive="k", vix_yahoo_fallback=True)
    out = scan(db, None, cfg, 1000.0)
    assert out == {"ran": True, "fetched": True, "vix": 17.25}
    assert db.kv["vix:latest"]["source"] == "yahoo"


def test_scan_no_fallback_when_flag_off(monkeypatch):
    monkeypatch.setitem(sys.modules, "httpx", _fake_httpx_dispatch(
        massive_raise=RuntimeError("boom"),
        yahoo_payload=_YAHOO_VIX_PAYLOAD))
    db, cfg = FakeDB(), FakeCfg(massive="k", vix_yahoo_fallback=False)
    out = scan(db, None, cfg, 1000.0)
    assert out["ran"] is True and out["fetched"] is False
    assert "vix:latest" not in db.kv


def test_engine_dispatch_calls_vix_gate():
    """Regression: _should_run must actually gate detector dispatch in
    compass/engine.py (defined-but-unused happened on 2026-10-09/10)."""
    import re
    src = open(os.path.join(os.path.dirname(__file__), "..", "compass",
                            "engine.py")).read()
    # detectors the gate is specified for: 3 breakout + 3 fade
    # (continuation, morning_drive and icc were in this set until their
    # retirement on 2026-10-25 — docs/RETIRED_DETECTORS.md; gap_fade was
    # in this set until its module was deleted on 2026-11-02)
    gated = {"bos_fvg", "bos_gz_vwap", "rumers_box", "turtle_soup",
             "aoi_zones", "aoi_fade"}
    # each gated detector's dispatch line must call _should_run('<name>')
    for name in sorted(gated):
        pattern = r"_should_run\(['\"]%s['\"]\)" % re.escape(name)
        assert re.search(pattern, src), \
            "engine dispatch does not gate %r via _should_run" % name
    # context/evidence detectors must NOT be regime-gated
    for name in ("session_liquidity", "htf_levels", "trend_bias"):
        pattern = r"_should_run\(['\"]%s['\"]\)" % re.escape(name)
        assert not re.search(pattern, src), \
            "context detector %r must stay ungated" % name
