"""Paper-only detector carve-out (Josh 2026-10-25; Turtle Soup added
2026-10-26).

Golden Zone, Rumers Box and Turtle Soup keep paper trading —
positions, attribution, brake-shadow and the live-combo scorecard all
see them — but their trades are NEVER mirrored to TradersPost:
submit() routes them through the standard skip marking
(traderspost_sent=False + 'paper-only' reason on both the trade and
position copies) and never queues a signal. Because every broker-close
path keys on traderspost_sent, a never-sent position can never emit a
broker close either — including the session-flatten path, which
before this change queued a close for any -t1 position without
checking the flag (now gated, with the sent flatten as the positive
control below).

aoi_fade mirroring is unchanged — since 2026-10-26 it is the only
mirrored detector (the live book is AOI Fade {MCL, MES}).

The carve-out gates NEW sends only: a Turtle Soup position opened
before 2026-10-26 carries traderspost_sent=True, and its broker close
still fires — the sent-Turtle-Soup flatten control below proves it.
"""
from datetime import datetime, timezone

import pytest

from compass.store import Store
from compass.config import Config
from compass import ict_paper
from compass.engine import Engine
from compass.market import CT
from compass.simulation import FILL_VERSION
import compass.traderspost as traderspost

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()  # Mon 12:00 CT


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'po.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.ict_golden_zone = True
    c.ict_rumers_box = True
    c.ict_aoi_fade = True
    c.ict_turtle_soup = True
    c.traderspost_enabled = True
    return c


@pytest.fixture
def sent(monkeypatch):
    calls = []

    def fake_send(cfg, db, trade):
        calls.append(trade)
        return True, 'ok'

    monkeypatch.setattr(traderspost, 'send_signal', fake_send)
    return calls


def sig(**kw):
    base = {'direction': 'long', 'entry': 100.0, 'stop': 98.0,
            'target': 104.0, 'rr': 2.0, 'signal_ts': NOW - 60,
            'level_kind': 'test'}
    base.update(kw)
    return base


def submit(db, cfg, detector, symbol, **kw):
    with db.tx() as c:
        r = ict_paper.submit(db, c, cfg, NOW, detector, symbol, sig(**kw))
        assert r['submitted'], r
    traderspost.dispatch_pending(cfg, db)
    return r


def get(db, key):
    with db.tx() as c:
        return db.get(c, key)


# ---------- (a) submit: paper-only detectors never queue ----------

def test_paper_only_set_is_exact():
    assert ict_paper.PAPER_ONLY_DETECTORS == {'golden_zone', 'rumers_box',
                                              'turtle_soup'}


@pytest.mark.parametrize('detector,symbol', [
    ('golden_zone', 'MCL.v.0'),
    ('rumers_box', 'MYM.c.0'),
    ('turtle_soup', 'MCL.v.0'),
])
def test_paper_only_submit_never_queues(db, cfg, sent, detector, symbol):
    r = submit(db, cfg, detector, symbol)
    assert sent == []  # nothing was dispatched to TradersPost
    trade = get(db, 'trade:' + r['trade_id'] + '-t1')
    assert trade['traderspost_sent'] is False
    assert 'paper-only' in trade['traderspost_skip_reason']
    # The position copy carries the same marks (exits() copies it over
    # the trade record at close — the flag must survive).
    pos = get(db, f'position:ict:{detector}:{symbol}:t1')
    assert pos['traderspost_sent'] is False
    assert 'paper-only' in pos['traderspost_skip_reason']
    # Paper-only is not invisible: the paper position is live.
    assert pos['status'] == 'open'


@pytest.mark.parametrize('detector,symbol', [
    ('aoi_fade', 'MCL.v.0'),
    ('aoi_fade', 'MES.c.0'),
])
def test_mirrored_detectors_still_queue(db, cfg, sent, detector, symbol):
    r = submit(db, cfg, detector, symbol)
    assert len(sent) == 1
    trade = get(db, 'trade:' + r['trade_id'] + '-t1')
    assert trade['traderspost_sent'] is True


# ---------- (b) close: a never-sent position emits no broker close ----------

def stamp(value):
    return datetime.fromisoformat(value).replace(tzinfo=CT).timestamp()


EXIT_NOW = stamp("2026-09-14T08:46:00")  # Monday, futures session open
GZ_POS_KEY = "position:ict:golden_zone:MNQ.c.0:t1"
GZ_TRADE_ID = "ict-golden_zone-MNQ-c-0-123-t1"
RB_POS_KEY = "position:ict:rumers_box:MYM.c.0:t1"
RB_TRADE_ID = "ict-rumers_box-MYM-c-0-123-t1"
TS_POS_KEY = "position:ict:turtle_soup:MCL.v.0:t1"
TS_TRADE_ID = "ict-turtle_soup-MCL-v-0-123-t1"


@pytest.fixture
def close_calls(monkeypatch):
    calls = []

    def fake_close(cfg, db, symbol, ref=None):
        calls.append(symbol)
        return True, "ok"

    monkeypatch.setattr(traderspost, "send_close_signal", fake_close)
    return calls


def put_position(db, pos_key, trade_id, symbol, **over):
    p = {
        "id": trade_id, "symbol": symbol, "asset": "future",
        "side": "long", "status": "open", "entry": 100.0, "stop": 98.0,
        "target": 104.0, "qty": 2, "multiplier": 2, "fee": 0.5,
        "tick": 0.25, "entered_at": EXIT_NOW - 60, "last_quote_ts": 0,
        "last_bar_checked": EXIT_NOW - 120, "fill_version": FILL_VERSION,
        "flatten_at": stamp("2026-09-14T15:45:00"),
        "traderspost_sent": False,
        "traderspost_skip_reason": "skip %s: paper-only detector" % symbol,
    }
    p.update(over)
    with db.tx() as c:
        db.put(c, pos_key, p)
        db.put(c, "trade:" + trade_id, dict(p))
    return p


def run_exits(db, symbol, bid, ask):
    cfg = Config(local=True, paper_trading=True)
    engine = Engine(db, cfg)
    with db.tx() as c:
        db.put(c, "quote:" + symbol,
               {"ts": EXIT_NOW, "bid": bid, "ask": ask,
                "bid_size": 10, "ask_size": 10})
        engine.exits(c, EXIT_NOW)
    traderspost.dispatch_pending(cfg, db)


def test_never_sent_golden_zone_stop_out_fires_no_close(db, close_calls):
    put_position(db, GZ_POS_KEY, GZ_TRADE_ID, "MNQ.c.0")
    run_exits(db, "MNQ.c.0", bid=97.0, ask=97.25)  # through the 98.0 stop
    p = get(db, GZ_POS_KEY)
    assert p["status"] == "closed" and p["exit_reason"] == "stop"
    assert close_calls == []


@pytest.mark.parametrize('pos_key,trade_id,symbol', [
    (GZ_POS_KEY, GZ_TRADE_ID, "MNQ.c.0"),
    (RB_POS_KEY, RB_TRADE_ID, "MYM.c.0"),
    (TS_POS_KEY, TS_TRADE_ID, "MCL.v.0"),
])
def test_never_sent_flatten_fires_no_close(db, close_calls,
                                           pos_key, trade_id, symbol):
    # flatten_at already passed: the session-flatten branch closes the
    # position. Before the 2026-10-25 gate this queued a broker close
    # for ANY -t1 position regardless of traderspost_sent.
    put_position(db, pos_key, trade_id, symbol,
                 flatten_at=stamp("2026-09-14T08:45:00"))
    run_exits(db, symbol, bid=100.0, ask=100.25)
    p = get(db, pos_key)
    assert p["status"] == "closed" and p["exit_reason"] == "session_flatten"
    assert close_calls == []


def test_sent_flatten_still_fires_close(db, close_calls):
    # Positive control: a position that WAS sent still gets its flatten
    # close — the new gate must not break the live path.
    put_position(db, GZ_POS_KEY, GZ_TRADE_ID, "MNQ.c.0",
                 flatten_at=stamp("2026-09-14T08:45:00"),
                 traderspost_sent=True)
    run_exits(db, "MNQ.c.0", bid=100.0, ask=100.25)
    p = get(db, GZ_POS_KEY)
    assert p["status"] == "closed" and p["exit_reason"] == "session_flatten"
    assert close_calls == ["MNQ.c.0"]


def test_sent_turtle_soup_flatten_still_fires_close(db, close_calls):
    # Pre-change semantics (2026-10-26 carve-out): a Turtle Soup
    # position opened BEFORE the carve-out was mirrored, so it carries
    # traderspost_sent=True. The carve-out gates new sends only — this
    # position's broker close must still fire at flatten (the
    # 2026-10-24 rule: every paper close of a sent position sends the
    # broker close).
    put_position(db, TS_POS_KEY, TS_TRADE_ID, "MCL.v.0",
                 flatten_at=stamp("2026-09-14T08:45:00"),
                 traderspost_sent=True)
    run_exits(db, "MCL.v.0", bid=100.0, ask=100.25)
    p = get(db, TS_POS_KEY)
    assert p["status"] == "closed" and p["exit_reason"] == "session_flatten"
    assert close_calls == ["MCL.v.0"]
