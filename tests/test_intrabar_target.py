"""Intrabar target detection in the exit loop.

Stops were already detected inside completed bars; targets were only checked
against the live quote, so a target touched and faded intrabar was missed
(one-directional pessimistic bias). Bars are now scanned in chronological
order for the first touch of either level: a both-touch bar is ambiguous, so
the stop wins by conservative convention.
"""
from datetime import datetime
import pytest
from compass.config import Config
from compass.store import Store
from compass.market import NY
from compass.engine import Engine

NOW = datetime(2026, 9, 14, 9, 46, tzinfo=NY).timestamp()


@pytest.fixture
def db(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "test.db"))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    return Config(local=True, risk=100, paper_trading=True, stock_paper_trades=True)


def quote(now=NOW, bid=102.10, ask=102.11):
    return {"ts": now, "bid": bid, "ask": ask, "bid_size": 100, "ask_size": 100,
            "source": "fixture"}


def signal(side="long"):
    return {"id": "fixture-signal", "symbol": "SPY", "side": side,
            "signal_time": NOW, "signal_price": 102, "stop_distance": 1,
            "strategy": "fixture", "track": "intraday"}


def bar(o, h, l, c):
    return {"o": o, "h": h, "l": l, "c": c, "v": 100}


def open_position(db, cfg, side="long"):
    e = Engine(db, cfg)
    with db.tx() as c:
        db.put(c, "quote:SPY", quote())
        assert e.enter(c, signal(side), NOW)
        return e, db.get(c, "position:SPY")


def exit_with(e, db, now, bars, q):
    with db.tx() as c:
        for i, payload in enumerate(bars):
            db.append(c, "bar", "fixture", "SPY", NOW + 60 * (i + 1), payload)
        db.put(c, "quote:SPY", q)
        e.exits(c, now)
        return db.get(c, "position:SPY")


def test_long_target_touched_intrabar_exits(db, cfg):
    e, p = open_position(db, cfg)
    target = p["target"]
    # Bar spikes through the target but closes back below it; the live quote
    # never touches the target.
    p = exit_with(e, db, NOW + 120, [bar(103, target + 0.5, target - 1.5, target - 0.5)],
                  quote(NOW + 120, bid=target - 0.5, ask=target - 0.49))
    assert p["status"] == "closed"
    assert p["exit_reason"] == "target_detected_in_bar"
    assert p["exit"] >= target and p["pnl"] > 0


def test_bar_touching_both_levels_stops_out(db, cfg):
    e, p = open_position(db, cfg)
    p = exit_with(e, db, NOW + 120, [bar(103, p["target"] + 1, p["stop"] - 1, 102)],
                  quote(NOW + 120, bid=102, ask=102.01))
    assert p["status"] == "closed"
    assert p["exit_reason"] == "stop_detected_in_bar"
    assert p["pnl"] < 0


def test_earlier_target_beats_later_stop(db, cfg):
    e, p = open_position(db, cfg)
    target, stop = p["target"], p["stop"]
    bars = [bar(103, target + 0.5, target - 1.5, target - 0.5),  # touches target
            bar(103, stop + 1, stop - 1, stop - 0.5)]            # later crosses stop
    p = exit_with(e, db, NOW + 240, bars, quote(NOW + 240, bid=stop - 0.5, ask=stop - 0.49))
    assert p["status"] == "closed"
    assert p["exit_reason"] == "target_detected_in_bar"


def test_short_target_touched_intrabar_exits(db, cfg):
    e, p = open_position(db, cfg, side="short")
    target = p["target"]
    p = exit_with(e, db, NOW + 120, [bar(target + 1.5, target + 2, target - 0.5, target + 0.5)],
                  quote(NOW + 120, bid=target + 0.5, ask=target + 0.51))
    assert p["status"] == "closed"
    assert p["exit_reason"] == "target_detected_in_bar"
    assert p["exit"] <= target and p["pnl"] > 0


def test_quiet_bar_leaves_position_open(db, cfg):
    e, p = open_position(db, cfg)
    p = exit_with(e, db, NOW + 120, [bar(102.5, 103, 102, 102.5)],
                  quote(NOW + 120, bid=102.5, ask=102.51))
    assert p["status"] == "open"
