"""TradersPost RELATIVE bracket tests (Task 003 Item 1, 2026-10-26).

When the paper trade booked its entry from a fresh send-time quote
(entry_price_source 'quote'/'quote_last'), build_payload sends relative
brackets — stopLoss {type: stop, amount}, takeProfit {amount} — with
price/signalPrice anchored on the BOOKED entry. Docs: TradersPost
anchors relative brackets on its own receipt-time broker quote and
falls back to signalPrice only when the broker cannot quote, so the
anchor must be the send-time quote, never the stale detector price.
Amounts are the approved geometry: distances from the SIGNAL price,
tick-rounded. Trades without a fresh booked entry send the historical
absolute payload byte-for-byte. The forms never mix (hard rejection).
The claim record carries bracket_form, the sent bracket values,
order_sent_at, and the TradersPost response id.
"""
import json
import time
import types
import urllib.request

import pytest

from compass.store import Store
from compass import traderspost


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'tp_rel.db'))
    s.initialize()
    yield s
    s.engine.dispose()


@pytest.fixture
def cfg():
    return types.SimpleNamespace(
        traderspost_enabled=True,
        traderspost_webhook_url='https://example.invalid/hook-primary',
        traderspost_webhook_url_2='')


def trade(**over):
    t = {'id': 'ict-turtle_soup-MCL-v-0-999-t1', 'detector': 'turtle_soup',
         'symbol': 'MCL.v.0', 'side': 'short', 'entry': 101.03,
         'signal_price': 102.44, 'stop': 103.27, 'target': 100.70,
         'qty': 2, 'is_first_leg': True,
         'entry_price_source': 'quote', 'tick': 0.01}
    t.update(over)
    return t


class Resp:
    def __init__(self, body=b'{"ok":true}'):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        return self._body


@pytest.fixture
def posts(monkeypatch):
    calls = []
    behavior = {'handler': None}

    def fake_urlopen(req, timeout=0):
        calls.append(req)
        handler = behavior['handler']
        if handler is not None:
            outcome = handler(req.full_url)
            if isinstance(outcome, Exception):
                raise outcome
            return outcome
        return Resp()

    monkeypatch.setattr(urllib.request, 'urlopen', fake_urlopen)
    return calls, behavior


def get(db, key):
    with db.tx() as c:
        return db.get(c, key)


# --- Relative payload shape ----------------------------------------------

def test_relative_payload_short_mcl_is_tonights_trade():
    # The 2026-10-26 Turtle Soup trade in relative form: planned
    # 102.44 / stop 103.27 / target 100.70, booked entry 101.03.
    p = traderspost.build_payload(cfg, trade())
    assert p == {
        'ticker': 'MCL', 'action': 'sell', 'quantity': 2,
        'price': 101.03, 'signalPrice': 101.03,
        'stopLoss': {'type': 'stop', 'amount': 0.83},
        'takeProfit': {'amount': 1.74},
        'strategy': 'turtle_soup', 'orderType': 'market',
        'timeInForce': 'day'}


def test_relative_payload_long_mes():
    p = traderspost.build_payload(cfg, trade(
        id='ict-aoi_fade-MES-c-0-111-t1', detector='aoi_fade',
        symbol='MES.c.0', side='long', entry=6000.25,
        signal_price=6000.0, stop=5990.0, target=6020.0, tick=0.25))
    assert p['action'] == 'buy'
    assert p['price'] == 6000.25 and p['signalPrice'] == 6000.25
    assert p['stopLoss'] == {'type': 'stop', 'amount': 10.0}
    assert p['takeProfit'] == {'amount': 20.0}


def test_amounts_come_from_signal_geometry_not_booked_entry():
    # The booked entry moved 1.41 adverse; the amounts must stay the
    # APPROVED distances (0.83 / 1.74), not the degraded live ones
    # (2.24 / 0.33) that distances-from-booked would produce.
    p = traderspost.build_payload(cfg, trade())
    assert p['stopLoss']['amount'] == 0.83
    assert p['takeProfit']['amount'] == 1.74


def test_amount_tick_rounding_burns_float_dust():
    # 100.0 - 97.3 == 2.7000000000000002 in float; on the MNQ 0.25
    # grid the amount must be exactly 2.75.
    p = traderspost.build_payload(cfg, trade(
        symbol='MNQ.c.0', side='long', entry=100.4, signal_price=100.0,
        stop=97.3, target=110.0, tick=0.25))
    assert p['stopLoss']['amount'] == 2.75
    assert p['takeProfit']['amount'] == 10.0


def test_quote_last_source_is_also_relative():
    p = traderspost.build_payload(cfg, trade(entry_price_source='quote_last'))
    assert p['stopLoss'] == {'type': 'stop', 'amount': 0.83}
    assert p['signalPrice'] == 101.03


def test_trailer_target_converts_to_ten_r_amount():
    p = traderspost.build_payload(cfg, trade(target=None))
    assert p['stopLoss']['amount'] == 0.83
    assert p['takeProfit']['amount'] == pytest.approx(8.3)


def test_degenerate_subtick_distance_falls_back_to_absolute():
    p = traderspost.build_payload(cfg, trade(
        symbol='MES.c.0', side='long', entry=100.0, signal_price=100.0,
        stop=99.999, target=101.0, tick=0.25))
    assert p['stopLoss'] == {'type': 'stop', 'stopPrice': 99.999}
    assert 'signalPrice' not in p


# --- Fallback: absolute, byte-for-byte ------------------------------------

def test_fallback_payload_is_the_historical_absolute_payload():
    p = traderspost.build_payload(cfg, trade(
        entry=102.44, entry_price_source='signal_fallback'))
    assert p == {
        'ticker': 'MCL', 'action': 'sell', 'quantity': 2,
        'price': 102.44,
        'stopLoss': {'type': 'stop', 'stopPrice': 103.27},
        'takeProfit': {'limitPrice': 100.70},
        'strategy': 'turtle_soup', 'orderType': 'market',
        'timeInForce': 'day'}


def test_fallback_matches_legacy_record_without_source_fields():
    legacy = trade()
    for k in ('entry_price_source', 'signal_price', 'tick'):
        legacy.pop(k, None)
    legacy['entry'] = 102.44
    fallback = trade(entry=102.44, entry_price_source='signal_fallback')
    assert traderspost.build_payload(cfg, fallback) == \
        traderspost.build_payload(cfg, legacy)


def test_no_payload_ever_mixes_forms():
    variants = [
        trade(),
        trade(entry_price_source='quote_last'),
        trade(entry_price_source='signal_fallback'),
        trade(entry=102.44, entry_price_source='signal_fallback'),
        trade(symbol='MES.c.0', side='long', entry=6000.25,
              signal_price=6000.0, stop=5990.0, target=6020.0, tick=0.25),
        trade(symbol='MNQ.c.0', side='long', entry=100.4,
              signal_price=100.0, stop=97.3, target=110.0, tick=0.25),
        trade(target=None),
    ]
    for t in variants:
        p = traderspost.build_payload(cfg, t)
        assert p is not None
        sl, tp = p['stopLoss'], p['takeProfit']
        assert ('amount' in sl) != ('stopPrice' in sl)
        assert ('amount' in tp) != ('limitPrice' in tp)
        assert ('amount' in sl) == ('amount' in tp)
        if 'amount' in sl:
            assert p['signalPrice'] == t['entry']
            assert sl['type'] == 'stop'
        else:
            assert 'signalPrice' not in p


# --- Claim / audit logging -------------------------------------------------

def test_claim_record_carries_relative_bracket_and_response_id(db, cfg, posts):
    calls, behavior = posts
    behavior['handler'] = lambda url: Resp(
        b'{"success":true,"id":"tp-abc-123","payload":{}}')
    before = time.time()
    ok, _ = traderspost.send_signal(cfg, db, trade())
    after = time.time()
    assert ok is True
    wire = json.loads(calls[0].data)
    assert wire['signalPrice'] == 101.03
    assert wire['stopLoss'] == {'type': 'stop', 'amount': 0.83}
    claim = get(db, 'traderspost_send:entry:ict-turtle_soup-MCL-v-0-999-t1')
    assert claim['status'] == 'sent' and claim['ok'] is True
    assert claim['bracket_form'] == 'relative'
    assert claim['stop_amount'] == 0.83
    assert claim['take_profit_amount'] == 1.74
    assert claim['anchor_price'] == 101.03
    assert before <= claim['order_sent_at'] <= after
    assert claim['tp_response_id'] == 'tp-abc-123'
    assert claim['response_ids'] == {'primary': 'tp-abc-123'}


def test_claim_record_carries_absolute_bracket(db, cfg, posts):
    calls, behavior = posts
    behavior['handler'] = lambda url: Resp(
        b'{"success":true,"id":"tp-def-456","payload":{}}')
    ok, _ = traderspost.send_signal(cfg, db, trade(
        entry=102.44, entry_price_source='signal_fallback'))
    assert ok is True
    wire = json.loads(calls[0].data)
    assert 'signalPrice' not in wire
    assert wire['stopLoss'] == {'type': 'stop', 'stopPrice': 103.27}
    claim = get(db, 'traderspost_send:entry:ict-turtle_soup-MCL-v-0-999-t1')
    assert claim['bracket_form'] == 'absolute'
    assert claim['stop_price'] == 103.27
    assert claim['take_profit_price'] == 100.70
    assert claim['tp_response_id'] == 'tp-def-456'
    assert claim['order_sent_at'] > 0


def test_response_id_none_when_body_has_no_id(db, cfg, posts):
    ok, _ = traderspost.send_signal(cfg, db, trade())
    assert ok is True
    claim = get(db, 'traderspost_send:entry:ict-turtle_soup-MCL-v-0-999-t1')
    assert claim['tp_response_id'] is None
    assert claim['bracket_form'] == 'relative'


# --- Close path never routes through the conversion ------------------------

def test_close_payload_has_no_bracket(db, cfg, posts):
    calls, _ = posts
    ok, _ = traderspost.send_close_signal(cfg, db, 'MCL.v.0',
                                          ref='ict-turtle_soup-MCL-v-0-999-t1')
    assert ok is True
    wire = json.loads(calls[0].data)
    assert wire['action'] == 'exit'
    assert 'stopLoss' not in wire and 'takeProfit' not in wire
    assert 'signalPrice' not in wire
