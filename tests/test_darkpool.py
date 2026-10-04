"""Tests for compass/darkpool.py (MCP dark pool discovery + confirmation)."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from compass.darkpool import (find_darkpool_tool, _extract_prints,
                              _normalize_print, confirmation_for,
                              MIN_NOTIONAL)


def test_find_darkpool_tool():
    tools = [
        {"name": "get_unusual_activity", "description": "Options flow"},
        {"name": "get_dark_pool_flow", "description": "Dark pool prints"},
        {"name": "get_gex_ticker", "description": "GEX"},
    ]
    tool = find_darkpool_tool(tools)
    assert tool["name"] == "get_dark_pool_flow"


def test_find_darkpool_tool_prefers_name_match():
    tools = [
        {"name": "get_flow", "description": "dark pool prints here"},
        {"name": "dark_pool", "description": "prints"},
    ]
    assert find_darkpool_tool(tools)["name"] == "dark_pool"


def test_find_darkpool_tool_none():
    tools = [{"name": "get_gex", "description": "gamma"}]
    assert find_darkpool_tool(tools) is None
    assert find_darkpool_tool([]) is None
    assert find_darkpool_tool(None) is None


def test_extract_prints_content_envelope():
    import json
    result = {"content": [
        {"type": "text",
         "text": json.dumps([{"symbol": "SPY", "notional": 1000000}])},
    ]}
    prints = _extract_prints(result)
    assert len(prints) == 1
    assert prints[0]["symbol"] == "SPY"


def test_extract_prints_nested_key():
    result = {"prints": [{"symbol": "QQQ", "notional": 2000000}]}
    prints = _extract_prints(result)
    assert len(prints) == 1


def test_extract_prints_empty():
    assert _extract_prints(None) == []
    assert _extract_prints({}) == []
    assert _extract_prints({"content": [{"type": "text", "text": "not json"}]}) == []


def test_normalize_print():
    p = _normalize_print({"symbol": "spy", "side": "buy",
                          "notional": "1500000", "price": 745.5,
                          "timestamp": 1791000000})
    assert p["symbol"] == "SPY"
    assert p["direction"] == "bullish"
    assert p["notional"] == 1500000.0
    assert p["price"] == 745.5


def test_normalize_print_direction_variants():
    assert _normalize_print({"symbol": "A", "direction": "bearish"})["direction"] == "bearish"
    assert _normalize_print({"symbol": "A", "side": "s"})["direction"] == "bearish"
    assert _normalize_print({"symbol": "A"})["direction"] is None


class _FakeDB:
    def __init__(self, store):
        self._store = store
    def get(self, c, key, default=None):
        return self._store.get(key, default)


def test_confirmation_same_direction():
    now = 1791000000
    store = {"darkpool:latest": {
        "status": "ok", "at": now - 100,
        "prints": [
            {"symbol": "SPY", "direction": "bullish",
             "notional": 2000000, "ts": now - 3600},
            {"symbol": "SPY", "direction": "bearish",
             "notional": 3000000, "ts": now - 3600},
        ]}}
    db = _FakeDB(store)
    out = confirmation_for("SPY", "bullish", db, None, now)
    assert out["confirmed"] is True
    assert out["strong"] is True
    assert out["print_count"] == 1  # only the bullish one matches direction
    assert out["notional"] == 2000000


def test_confirmation_below_min_notional():
    now = 1791000000
    store = {"darkpool:latest": {
        "status": "ok", "at": now - 100,
        "prints": [{"symbol": "SPY", "direction": "bullish",
                    "notional": MIN_NOTIONAL - 1, "ts": now - 3600}]}}
    db = _FakeDB(store)
    out = confirmation_for("SPY", "bullish", db, None, now)
    assert out["confirmed"] is False


def test_confirmation_stale():
    now = 1791000000
    store = {"darkpool:latest": {
        "status": "ok", "at": now - 100000,
        "prints": [{"symbol": "SPY", "direction": "bullish",
                    "notional": 5000000, "ts": now - 100000}]}}
    db = _FakeDB(store)
    out = confirmation_for("SPY", "bullish", db, None, now)
    assert out["confirmed"] is False  # outside 24h window


def test_confirmation_no_data():
    db = _FakeDB({})
    out = confirmation_for("SPY", "bullish", db, None, 1791000000)
    assert out["confirmed"] is False
    assert out["reason"] == "no_data"


def test_confirmation_direction_agnostic_counts_weak():
    now = 1791000000
    store = {"darkpool:latest": {
        "status": "ok", "at": now - 100,
        "prints": [{"symbol": "SPY", "direction": None,
                    "notional": 1000000, "ts": now - 3600}]}}
    db = _FakeDB(store)
    out = confirmation_for("SPY", "bullish", db, None, now)
    assert out["confirmed"] is True
    assert out["strong"] is False
