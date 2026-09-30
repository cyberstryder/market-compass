"""0DTE scanner contract pool: same-day plus weekly expiries.

Covers the 2026-09-30 expansion: the 0DTE selection loop used to accept only
same-day expiry contracts, so names without daily expirations (e.g. MU) had no
0DTE-style coverage on their weeklies. The pool now accepts expiries up to
OPTION_0DTE_MAX_DTE (default 7), preferring the nearest expiry, while positions
still flatten 15 minutes before the close regardless of expiry.
"""
from datetime import datetime

import pytest

from compass.engine import Engine
from compass.config import Config
from compass.market import CT
from compass.store import Store


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=CT).timestamp()


WEDNESDAY = stamp("2026-09-30T10:00:00")  # day(now) == "2026-09-30"


@pytest.fixture
def db(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "weekly.db"))
    s.initialize()
    yield s
    s.engine.dispose()


def quote(now, bid=2, ask=2.01):
    return {"ts": now, "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 10}


def underlying_signal(signal_time, signal_price=100):
    return {"id": "underlying", "symbol": "MU", "side": "long",
            "signal_time": signal_time, "signal_price": signal_price,
            "stop_distance": 1, "track": "intraday"}


def contract(symbol, expiry, strike=100):
    return {"symbol": symbol, "expiry": expiry, "type": "call",
            "strike": strike, "multiplier": 100}


def test_weekly_expiry_contract_selected(db):
    # MU 10/2 weekly (2 DTE): no same-day contract exists, the weekly is taken.
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    with db.tx() as c:
        db.put(c, "chain:MU", {"asof": WEDNESDAY,
                               "contracts": [contract("O:MUW", "2026-10-02")]})
        db.put(c, "quote:O:MUW", quote(WEDNESDAY))
        assert e.options(c, underlying_signal(WEDNESDAY - 60), WEDNESDAY)
        assert db.get(c, "position:O:MUW")["status"] == "open"


def test_same_day_preferred_over_weekly(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    with db.tx() as c:
        db.put(c, "chain:MU", {"asof": WEDNESDAY, "contracts": [
            contract("O:MUW", "2026-10-02"), contract("O:MU0", "2026-09-30")]})
        db.put(c, "quote:O:MUW", quote(WEDNESDAY))
        db.put(c, "quote:O:MU0", quote(WEDNESDAY))
        assert e.options(c, underlying_signal(WEDNESDAY - 60), WEDNESDAY)
        assert db.get(c, "position:O:MU0")["status"] == "open"
        assert db.get(c, "position:O:MUW") is None


def test_expiry_beyond_max_dte_excluded(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    audit = {}
    with db.tx() as c:
        db.put(c, "chain:MU", {"asof": WEDNESDAY,
                               "contracts": [contract("O:MUW", "2026-10-10")]})  # 10 DTE
        db.put(c, "quote:O:MUW", quote(WEDNESDAY))
        assert not e.options(c, underlying_signal(WEDNESDAY - 60), WEDNESDAY,
                             quiet=True, diagnostics=audit)
        assert db.get(c, "position:O:MUW") is None
    assert audit["eligible_contracts"] == 0
    assert "DTE" in audit["reason"]


def test_max_dte_zero_restores_same_day_only(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True,
                          option_0dte_max_dte=0))
    with db.tx() as c:
        db.put(c, "chain:MU", {"asof": WEDNESDAY,
                               "contracts": [contract("O:MUW", "2026-10-02")]})
        db.put(c, "quote:O:MUW", quote(WEDNESDAY))
        assert not e.options(c, underlying_signal(WEDNESDAY - 60), WEDNESDAY,
                             quiet=True)
        assert db.get(c, "position:O:MUW") is None


def test_config_default_and_validation(monkeypatch):
    assert Config(local=True).option_0dte_max_dte == 7
    monkeypatch.setenv("OPTION_0DTE_MAX_DTE", "3")
    assert Config(local=True).option_0dte_max_dte == 3
    with pytest.raises(ValueError):
        Config(local=True, option_0dte_max_dte=31).validate()
