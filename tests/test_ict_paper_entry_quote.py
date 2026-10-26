"""Entry-quote booking for ICT paper trades (2026-10-26).

Paper entries used to book the detector's bar-derived signal price
verbatim while the live market order filled at the arrival quote —
2026-10-26 Turtle Soup MCL: paper 102.44 vs live fill 101.03. submit()
now books the freshest live quote (ask for a long, bid for a short,
mirroring exits()' resolution and freshness ages), keeps signal_price
for diagnostics, records the slip, and falls back to the signal price
(flagged entry_unverified) when no fresh quote exists. The TradersPost
payload is built from signal_price, so the live send is unchanged.
"""
import json
from datetime import datetime, timezone
import pytest
from compass.store import Store
from compass.config import Config
from compass.instruments import future_spec
from compass import ict_paper, traderspost

NOW = datetime(2026, 9, 28, 17, 0, tzinfo=timezone.utc).timestamp()  # Mon 12:00 CT
SYM = 'MNQ.c.0'  # golden_zone watches {MCL, MNQ}
POS = 'position:ict:golden_zone:' + SYM + ':t1'


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'ictentry.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    c = Config(local=True, risk=100)
    c.ict_futures_paper = True
    c.ict_golden_zone = True
    return c


def sig(**kw):
    base = {'direction': 'long', 'entry': 100.0, 'stop': 98.0,
            'target': 104.0, 'rr': 2.0, 'signal_ts': NOW - 60,
            'level_kind': 'test'}
    base.update(kw)
    return base


def quote(ts, bid, ask):
    return {'ts': ts, 'bid': bid, 'ask': ask, 'bid_size': 10,
            'ask_size': 10, 'source': 'fixture'}


def submit_with(db, cfg, q=None, qkey=None, sym=SYM, **kw):
    with db.tx() as c:
        if q is not None:
            db.put(c, qkey or ('quote:' + sym), q)
        r = ict_paper.submit(db, c, cfg, NOW, 'golden_zone', sym, sig(**kw))
        assert r['submitted'], r
        p = db.get(c, 'position:ict:golden_zone:%s:t1' % sym)
        t = db.get(c, 'trade:' + p['id'])
    return p, t


def expected_initial_risk(sym, booked_entry, stop, qty=2):
    spec = future_spec(sym)
    per_unit = abs(booked_entry - stop) * spec['multiplier'] \
        + 2 * spec['fee'] + 2 * spec['tick'] * spec['multiplier']
    return per_unit * qty


# --- booking at the quote ---

def test_long_books_at_ask_with_adverse_slip(db, cfg):
    p, t = submit_with(db, cfg, quote(NOW, 100.25, 100.30))
    assert p['entry'] == pytest.approx(100.30)  # ask, not the 100.0 signal
    assert p['signal_price'] == pytest.approx(100.0)
    assert p['entry_price_source'] == 'quote'
    assert p['entry_quote_ts'] == NOW
    # Adverse-positive: paid 0.30 over signal = +1.2 MNQ ticks (0.25).
    assert p['entry_slip_ticks'] == pytest.approx(1.2)
    assert not p.get('entry_unverified')
    # Risk records are computed from the BOOKED entry's stop distance.
    assert p['initial_risk'] == pytest.approx(
        expected_initial_risk(SYM, 100.30, 98.0))
    # Both stored copies carry the booking fields.
    assert t['entry'] == p['entry']
    assert t['entry_price_source'] == 'quote'
    assert t['entry_quote_ts'] == NOW


def test_short_books_at_bid_with_adverse_slip(db, cfg):
    p, _ = submit_with(db, cfg, quote(NOW, 99.70, 99.75),
                       direction='short', entry=100.0, stop=102.0,
                       target=96.0)
    assert p['entry'] == pytest.approx(99.70)  # bid, not the 100.0 signal
    assert p['signal_price'] == pytest.approx(100.0)
    assert p['entry_price_source'] == 'quote'
    # Sold 0.30 under signal = adverse +1.2 ticks for a short.
    assert p['entry_slip_ticks'] == pytest.approx(1.2)
    assert p['initial_risk'] == pytest.approx(
        expected_initial_risk(SYM, 99.70, 102.0))


def test_favorable_quote_books_negative_slip(db, cfg):
    p, _ = submit_with(db, cfg, quote(NOW, 99.85, 99.90))
    assert p['entry'] == pytest.approx(99.90)
    assert p['entry_slip_ticks'] == pytest.approx(-0.4)


def test_dated_contract_quote_books_entry(db, cfg):
    # Databento writes futures quotes under dated-contract keys only;
    # booking must resolve them exactly like exits() does.
    p, _ = submit_with(db, cfg, quote(NOW, 99.50, 99.60),
                       qkey='quote:MCLZ25@999', sym='MCL.v.0')
    assert p['entry'] == pytest.approx(99.60)
    assert p['entry_price_source'] == 'quote'
    assert p['entry_quote_ts'] == NOW
    assert p['entry_slip_ticks'] == pytest.approx(-40.0)  # MCL tick 0.01


def test_quote_last_fallback_when_no_bid_ask(db, cfg):
    q = {'ts': NOW, 'last': 99.90, 'source': 'fixture'}
    p, _ = submit_with(db, cfg, q)
    assert p['entry'] == pytest.approx(99.90)
    assert p['entry_price_source'] == 'quote_last'
    assert p['entry_quote_ts'] == NOW
    assert p['entry_slip_ticks'] == pytest.approx(-0.4)


def test_booked_entry_crossing_target_still_books(db, cfg):
    # Stop/target are structural levels; a quote already past the target
    # books anyway (the exit loop mirrors live bracket behavior).
    p, _ = submit_with(db, cfg, quote(NOW, 104.45, 104.50))
    assert p['entry'] == pytest.approx(104.50)
    assert p['entry_price_source'] == 'quote'
    assert p['status'] == 'open'


# --- fallback to the signal price ---

def _assert_fallback(p, sym=SYM, stop=98.0):
    assert p['entry'] == pytest.approx(100.0)
    assert p['signal_price'] == pytest.approx(100.0)
    assert p['entry_price_source'] == 'signal_fallback'
    assert p['entry_unverified'] is True
    assert p['entry_quote_ts'] == 0
    assert p['entry_slip_ticks'] is None
    assert p['initial_risk'] == pytest.approx(
        expected_initial_risk(sym, 100.0, stop))


def test_no_quote_falls_back_to_signal_price(db, cfg):
    p, t = submit_with(db, cfg)
    _assert_fallback(p)
    assert t['entry_unverified'] is True


def test_stale_quote_falls_back(db, cfg):
    # MNQ freshness age is 10s (exits' table); a 30s-old quote is stale.
    p, _ = submit_with(db, cfg, quote(NOW - 30, 100.25, 100.30))
    _assert_fallback(p)


def test_future_quote_falls_back(db, cfg):
    p, _ = submit_with(db, cfg, quote(NOW + 60, 99.50, 99.60),
                       sym='MCL.v.0')
    _assert_fallback(p, sym='MCL.v.0')


# --- live invariance: the TradersPost payload must not change ---

def test_payload_identical_for_same_signal(db, cfg):
    base = {'detector': 'turtle_soup', 'symbol': 'MCL.v.0',
            'side': 'short', 'stop': 103.27, 'target': 100.70,
            'qty': 2, 'is_first_leg': True}
    # Pre-fix record shape: entry IS the signal price, no signal_price.
    pre = traderspost.build_payload(cfg, {**base, 'entry': 102.44})
    # Post-fix record: booked entry differs; signal_price carries 102.44.
    post = traderspost.build_payload(
        cfg, {**base, 'entry': 101.03, 'signal_price': 102.44})
    assert pre is not None and post is not None
    assert json.dumps(pre, sort_keys=True) == json.dumps(post, sort_keys=True)
    assert post['price'] == 102.44
    assert post['stopLoss']['stopPrice'] == 103.27
    assert post['takeProfit']['limitPrice'] == 100.70
    assert post['quantity'] == 2
