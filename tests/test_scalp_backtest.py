"""Tests for /api/admin/backtest-scalp (gap-fade history replay).

Regression: the original handler did `from flask import request` in a
FastAPI app (Flask is not installed) -> HTTP 500 before any logic ran.
The rewrite also moved the data source from the nonexistent
db.prefix('bar:...') state keyspace to indexed events-table queries.
"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from compass.app import create_app
CRON = 'test-cron-token-' + 'x' * 40
AUTH = {'Authorization': 'Bearer ' + CRON}
from compass.config import Config
from compass.market import NY


@pytest.fixture
def cfg(tmp_path):
    return Config(paper_trading=True, stock_paper_trades=True, cron_token=CRON,
                  db="sqlite:///" + str(tmp_path / "web.db"), local=True, role="web",
                  password="x" * 20, secret="y" * 32,
                  stocks=("SPY",), futures=(), alpaca_key="", alpaca_secret="",
                  massive="", databento="", matrix="", discord="")


def _bar_ts(day, hour, minute):
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=NY).timestamp()


def _seed_day(db, symbol, day, open_px, close_px):
    items = [
        (symbol, _bar_ts(day, 9, 30),
         {"o": open_px, "h": open_px, "l": open_px, "c": open_px, "v": 1000}),
        (symbol, _bar_ts(day, 15, 59),
         {"o": close_px, "h": close_px, "l": close_px, "c": close_px, "v": 1000}),
    ]
    with db.tx() as c:
        db.append_bars(c, "bar", "fixture", items)


def _last_weekdays(n):
    """Most recent n weekdays (ET), newest first."""
    out = []
    d = datetime.now(NY).date()
    while len(out) < n:
        d -= timedelta(days=1)
        if d.weekday() < 5:
            out.append(d)
    return out


def test_backtest_scalp_counts_gap_signal(cfg):
    app = create_app(cfg)
    d1, d2, d3 = _last_weekdays(3)[2], _last_weekdays(3)[1], _last_weekdays(3)[0]
    with TestClient(app) as client:
        db = app.state.db
        # d2 gaps +3% over d1's close -> one short signal; d3 is flat -> none.
        _seed_day(db, "MU", d1, 100.0, 100.0)
        _seed_day(db, "MU", d2, 103.0, 103.5)
        _seed_day(db, "MU", d3, 103.6, 104.0)
        r = client.get("/api/admin/backtest-scalp?days=5&symbols=MU", headers=AUTH)
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert body["ok"] is True, body
    res = body["results"]["MU"]
    assert res["signals"] == 1
    sig = res["details"][0]
    assert sig["date"] == d2.isoformat()
    assert sig["gap_pct"] == 3.0
    assert sig["direction"] == "short"
    assert sig["entry"] == 103.0
    assert sig["prior_close"] == 100.0


def test_backtest_scalp_gap_down_is_long(cfg):
    app = create_app(cfg)
    d1, d2 = _last_weekdays(2)[1], _last_weekdays(2)[0]
    with TestClient(app) as client:
        db = app.state.db
        _seed_day(db, "MU", d1, 100.0, 100.0)
        _seed_day(db, "MU", d2, 97.5, 98.0)  # -2.5% gap -> long (fade the gap)
        r = client.get("/api/admin/backtest-scalp?days=3&symbols=MU", headers=AUTH)
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert body["ok"] is True, body
    res = body["results"]["MU"]
    assert res["signals"] == 1
    assert res["details"][0]["direction"] == "long"
    assert res["details"][0]["gap_pct"] == -2.5


def test_backtest_scalp_no_data_symbol_returns_zero(cfg):
    app = create_app(cfg)
    with TestClient(app) as client:
        r = client.get("/api/admin/backtest-scalp?days=5&symbols=ZZZQ", headers=AUTH)
    assert r.status_code == 200, r.text[:500]
    body = r.json()
    assert body["ok"] is True, body
    assert body["results"]["ZZZQ"]["signals"] == 0
