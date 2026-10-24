"""Fail-closed TradersPost stop protection (Josh 2026-10-20).

Incident: the 07:06 CT SMT MNQ long's bracket stop (29512.75) was
REJECTED at Tradovate because the live fill (29511.25) slipped below
the stop — a sell-stop priced above the market is invalid for a long.
Entry + take-profit survived; the live position was naked while the
paper twin stopped out at 07:09. Josh closed it manually (+$350) and
directed: auto-exit immediately if a stop is rejected — fail closed.

Mechanism under test: when a traderspost_sent ICT position stops out
in paper, engine.exits() queues a TradersPost exit for the ticker and
tick-end dispatch fires traderspost.send_close_signal. If the broker
stop worked, the ticker is already flat and 'exit' is a no-op; if it
was rejected, this closes the position.
"""
from datetime import datetime

import pytest

from compass.store import Store
from compass.config import Config
from compass.engine import Engine
from compass.market import CT
from compass.simulation import FILL_VERSION
import compass.traderspost as traderspost


def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=CT).timestamp()


NOW = stamp("2026-09-14T08:46:00")  # Monday, futures session open
POS_KEY = "position:ict:bos_fvg:MNQ.c.0:t1"
TRADE_ID = "ict-bos_fvg-MNQ-c-0-123-t1"


@pytest.fixture
def db(tmp_path):
    s = Store("sqlite:///" + str(tmp_path / "sp.db"))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def close_calls(monkeypatch):
    calls = []

    def fake_close(cfg, db, symbol, ref=None):
        calls.append(symbol)
        return True, "ok"

    monkeypatch.setattr(traderspost, "send_close_signal", fake_close)
    return calls


def put_position(db, **over):
    p = {
        "id": TRADE_ID, "symbol": "MNQ.c.0", "asset": "future",
        "side": "long", "status": "open", "entry": 100.0, "stop": 98.0,
        "target": 104.0, "qty": 2, "multiplier": 2, "fee": 0.5,
        "tick": 0.25, "entered_at": NOW - 60, "last_quote_ts": 0,
        "last_bar_checked": NOW - 120, "fill_version": FILL_VERSION,
        "flatten_at": stamp("2026-09-14T15:45:00"),
        "traderspost_sent": True,
    }
    p.update(over)
    with db.tx() as c:
        db.put(c, POS_KEY, p)
        db.put(c, "trade:" + TRADE_ID, dict(p))
    return p


def run_exits(db, bid, ask):
    cfg = Config(local=True, paper_trading=True)
    engine = Engine(db, cfg)
    with db.tx() as c:
        db.put(c, "quote:MNQ.c.0",
               {"ts": NOW, "bid": bid, "ask": ask,
                "bid_size": 10, "ask_size": 10})
        engine.exits(c, NOW)
    # Audit #4: exits() queues the close inside the transaction;
    # engine.tick dispatches it after commit — mirror that here.
    traderspost.dispatch_pending(cfg, db)
    with db.tx() as c:
        return db.get(c, POS_KEY)


def test_stop_out_of_sent_trade_fires_exit(db, close_calls):
    put_position(db)
    p = run_exits(db, bid=97.0, ask=97.25)  # through the 98.0 stop
    assert p["status"] == "closed"
    assert p["exit_reason"] == "stop"
    assert close_calls == ["MNQ.c.0"]


def test_stop_out_of_unsent_trade_fires_nothing(db, close_calls):
    put_position(db, traderspost_sent=False)
    p = run_exits(db, bid=97.0, ask=97.25)
    assert p["status"] == "closed"
    assert p["exit_reason"] == "stop"
    assert close_calls == []


def test_target_hit_also_queues_close(db, close_calls):
    # 2026-10-23: a paper target hit no longer assumes the live TP filled.
    # The bracket TP is priced off the signal reference, not the fill
    # (2026-10-20 MCL: TP above fill), so paper can close and free the
    # symbol while the live position persists and the next signal stacks.
    # Every paper close of a live-sent position now queues an exit; on a
    # flat ticker it is a no-op at TradersPost.
    put_position(db)
    p = run_exits(db, bid=104.5, ask=104.75)
    assert p["status"] == "closed"
    assert p["exit_reason"] == "target"
    assert len(close_calls) == 1
