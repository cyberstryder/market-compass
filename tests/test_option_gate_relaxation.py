"""0DTE option entry gates: relaxed quote freshness, spread, and late-session cutoff.

Covers the 2026-09-30 relaxation:
- OPTION_QUOTE_MAX_AGE (default 60s) for option contract quotes, replacing the
  shared 5s fresh() default in the 0DTE selection loop and entry_check.
- OPTION_MAX_SPREAD_PCT (default 0.12) replacing the hardcoded 8% option spread.
- OPTION_ENTRY_CUTOFF_MIN (default 15) replacing the 30-minute late-session
  entry block for options; stocks keep 30 minutes.
"""
from datetime import datetime

import pytest

from compass.engine import Engine
from compass.config import Config
from compass.market import CT
from compass.store import Store


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=CT).timestamp()


MONDAY = stamp("2026-09-14T08:46:00")
T_40_BEFORE_CLOSE = stamp("2026-09-14T14:20:00")
T_20_BEFORE_CLOSE = stamp("2026-09-14T14:40:00")
T_10_BEFORE_CLOSE = stamp("2026-09-14T14:50:00")


@pytest.fixture
def db(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "gates.db"))
    s.initialize()
    yield s
    s.engine.dispose()


def quote(now, bid=2, ask=2.01):
    return {"ts": now, "bid": bid, "ask": ask, "bid_size": 10, "ask_size": 10}


def underlying_signal(signal_time, signal_price=100):
    return {"id": "underlying", "symbol": "SPY", "side": "long",
            "signal_time": signal_time, "signal_price": signal_price,
            "stop_distance": 1, "track": "intraday"}


def contracts(symbol="O:OPT"):
    return [{"symbol": symbol, "expiry": "2026-09-14", "type": "call",
             "strike": 100, "multiplier": 100}]


def test_option_quote_up_to_sixty_seconds_old_passes_selection(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    with db.tx() as c:
        db.put(c, "chain:SPY", {"asof": MONDAY, "contracts": contracts()})
        # 45s old: would have been rejected under the old 5s gate.
        db.put(c, "quote:O:OPT", quote(MONDAY - 45))
        assert e.options(c, underlying_signal(MONDAY - 60), MONDAY)
        assert db.get(c, "position:O:OPT")["status"] == "open"


def test_option_quote_older_than_sixty_seconds_still_rejected(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    with db.tx() as c:
        db.put(c, "chain:SPY", {"asof": MONDAY, "contracts": contracts()})
        db.put(c, "quote:O:OPT", quote(MONDAY - 300))
        assert not e.options(c, underlying_signal(MONDAY - 360), MONDAY)
        assert db.get(c, "position:O:OPT") is None


def test_option_spread_up_to_twelve_percent_passes(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    with db.tx() as c:
        db.put(c, "chain:SPY", {"asof": MONDAY, "contracts": contracts()})
        # 10% spread: rejected under the old 8% gate.
        db.put(c, "quote:O:OPT", quote(MONDAY, bid=10, ask=11))
        assert e.options(c, underlying_signal(MONDAY), MONDAY)
        assert db.get(c, "position:O:OPT")["status"] == "open"


def test_option_spread_above_twelve_percent_rejected(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    with db.tx() as c:
        db.put(c, "chain:SPY", {"asof": MONDAY, "contracts": contracts()})
        # ~14.8% spread.
        db.put(c, "quote:O:OPT", quote(MONDAY, bid=10, ask=11.6))
        assert not e.options(c, underlying_signal(MONDAY), MONDAY)
        assert db.get(c, "position:O:OPT") is None


def test_option_entry_allowed_twenty_minutes_before_close(db):
    # Old gate blocked everything after ~14:30 CT; new 15-min cutoff allows it.
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    with db.tx() as c:
        db.put(c, "chain:SPY", {"asof": T_20_BEFORE_CLOSE, "contracts": contracts()})
        db.put(c, "quote:O:OPT", quote(T_20_BEFORE_CLOSE))
        assert e.options(c, underlying_signal(T_20_BEFORE_CLOSE), T_20_BEFORE_CLOSE)
        assert db.get(c, "position:O:OPT")["status"] == "open"


def test_option_entry_blocked_inside_fifteen_minute_cutoff(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    with db.tx() as c:
        db.put(c, "chain:SPY", {"asof": T_10_BEFORE_CLOSE, "contracts": contracts()})
        db.put(c, "quote:O:OPT", quote(T_10_BEFORE_CLOSE))
        assert not e.options(c, underlying_signal(T_10_BEFORE_CLOSE), T_10_BEFORE_CLOSE)
        assert db.get(c, "position:O:OPT") is None


def test_stock_entry_keeps_thirty_minute_cutoff(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    sig = {"id": "stock", "symbol": "SPY", "side": "long",
           "signal_time": T_40_BEFORE_CLOSE, "signal_price": 100,
           "stop_distance": 1, "track": "intraday"}
    with db.tx() as c:
        db.put(c, "quote:SPY", quote(T_20_BEFORE_CLOSE, bid=100, ask=100.05))
        reason, _ = e.entry_check(c, sig, T_20_BEFORE_CLOSE)
        assert reason == "Outside research entry session"


def test_stock_quote_freshness_unchanged_at_five_seconds(db):
    e = Engine(db, Config(local=True, risk=100, paper_trading=True))
    sig = {"id": "stock", "symbol": "SPY", "side": "long",
           "signal_time": T_40_BEFORE_CLOSE, "signal_price": 100,
           "stop_distance": 1, "track": "intraday"}
    with db.tx() as c:
        db.put(c, "quote:SPY", quote(T_40_BEFORE_CLOSE - 30, bid=100, ask=100.05))
        reason, _ = e.entry_check(c, sig, T_40_BEFORE_CLOSE)
        assert reason == "Missing, stale, locked/invalid, or empty bid/ask quote"


def test_option_gate_env_overrides(monkeypatch):
    monkeypatch.setenv("OPTION_QUOTE_MAX_AGE", "90")
    monkeypatch.setenv("OPTION_MAX_SPREAD_PCT", "0.15")
    monkeypatch.setenv("OPTION_ENTRY_CUTOFF_MIN", "5")
    cfg = Config(local=True)
    assert cfg.option_quote_max_age == 90
    assert cfg.option_max_spread_pct == pytest.approx(0.15)
    assert cfg.option_entry_cutoff_min == 5


def test_option_gate_config_defaults():
    cfg = Config(local=True)
    assert cfg.option_quote_max_age == 60
    assert cfg.option_max_spread_pct == pytest.approx(0.12)
    assert cfg.option_entry_cutoff_min == 15


def test_option_gate_config_validation(monkeypatch):
    monkeypatch.setenv("OPTION_QUOTE_MAX_AGE", "0")
    with pytest.raises(ValueError):
        Config(local=True).validate()
    monkeypatch.setenv("OPTION_QUOTE_MAX_AGE", "60")
    monkeypatch.setenv("OPTION_MAX_SPREAD_PCT", "0.9")
    with pytest.raises(ValueError):
        Config(local=True).validate()
    monkeypatch.setenv("OPTION_MAX_SPREAD_PCT", "0.12")
    monkeypatch.setenv("OPTION_ENTRY_CUTOFF_MIN", "200")
    with pytest.raises(ValueError):
        Config(local=True).validate()
