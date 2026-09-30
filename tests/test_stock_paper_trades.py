"""STOCK_PAPER_TRADES kill switch: Josh trades options only — no share buying.

Default is OFF (options/futures only). Stock-asset signals are recorded as
evidence trials but never open simulated share positions. Option and futures
entries are unaffected.
"""
from datetime import datetime
import pytest
from compass.config import Config
from compass.store import Store
from compass.engine import Engine
from compass.market import NY

NOW = datetime(2026, 9, 14, 9, 46, tzinfo=NY).timestamp()


@pytest.fixture
def db(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "test.db"))
    s.initialize()
    yield s
    s.engine.dispose()


def cfg(tmp_path, **kw):
    return Config(paper_trading=True, db="sqlite:///" + str(tmp_path / "web.db"),
                  local=True, role="web", password="x" * 16,
                  stocks=("SPY",), futures=(), alpaca_key="", alpaca_secret="",
                  massive="", databento="", matrix="", discord="", openai="", **kw)


def quote(now=NOW, bid=102.10, ask=102.11):
    return {"ts": now, "bid": bid, "ask": ask, "bid_size": 100, "ask_size": 100,
            "source": "fixture"}


def stock_signal():
    return {"id": "stock-sig", "symbol": "SPY", "side": "long",
            "signal_time": NOW, "signal_price": 102, "stop_distance": 1,
            "strategy": "fixture", "track": "intraday"}


def option_signal():
    return {"id": "opt-sig", "symbol": "O:SPY260930C00102000", "side": "long",
            "signal_time": NOW, "signal_price": 1.5, "stop_distance": 0.5,
            "strategy": "fixture", "track": "0dte"}


def test_stock_entry_blocked_by_default(db, tmp_path):
    e = Engine(db, cfg(tmp_path))
    with db.tx() as c:
        db.put(c, "quote:SPY", quote())
        assert e.enter(c, stock_signal(), NOW) is False
        assert db.get(c, "position:SPY") is None
        skips = [a for a in db.recent(c, "alert")
                 if a["payload"].get("status") == "skipped"]
        assert skips and "Stock paper entries disabled" in skips[0]["payload"]["reason"]


def test_stock_entry_allowed_when_flag_on(db, tmp_path):
    e = Engine(db, cfg(tmp_path, stock_paper_trades=True))
    with db.tx() as c:
        db.put(c, "quote:SPY", quote())
        assert e.enter(c, stock_signal(), NOW) is True
        assert db.get(c, "position:SPY")["status"] == "open"


def test_option_entry_unaffected_by_stock_flag(db, tmp_path):
    e = Engine(db, cfg(tmp_path))
    with db.tx() as c:
        db.put(c, "quote:O:SPY260930C00102000",
               {"ts": NOW, "bid": 1.45, "ask": 1.55, "bid_size": 50,
                "ask_size": 50, "source": "fixture"})
        assert e.enter(c, option_signal(), NOW) is True
        assert db.get(c, "position:O:SPY260930C00102000")["status"] == "open"


def test_quiet_stock_skip_lands_in_paper_decisions(db, tmp_path):
    e = Engine(db, cfg(tmp_path))
    with db.tx() as c:
        db.put(c, "quote:SPY", quote())
        assert e.enter(c, stock_signal(), NOW, quiet=True) is False
        rows = db.recent(c, "paper_decision")
        assert any(r["payload"].get("status") == "skipped" and
                   "Stock paper entries disabled" in r["payload"].get("reason", "")
                   for r in rows)
