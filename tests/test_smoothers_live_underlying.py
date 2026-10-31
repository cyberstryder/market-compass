"""Publication-time live underlying on Smoothers entry alerts.

Regression for 2026-10-26: the roster printed the frozen 09:30 CT entry
reference as a bare 'Stock $X' beside publication-fresh option quotes, so
on a fast morning it read as a live price (META printed 636.14 with the
market at 643.53). Entry alerts now print the reference labeled with its
freeze time plus the live underlying captured in the same publication
batch — and fall back to the labeled reference alone when the live read
is unavailable or stale. The model still runs off the reference."""
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace

from compass import smoothers_messages as messages
from compass.alert_ownership import format_message
from compass.native_smoothers import compact_line
from compass.native_smoothers_data import Data

CT = messages.CT
REF = datetime(2026, 10, 5, 9, 30, tzinfo=CT).timestamp()          # entry freeze, seconds
LIVE_MS = int(datetime(2026, 10, 5, 9, 44, tzinfo=CT).timestamp() * 1000)
LIVE = {'status': 'available', 'price': 900.10, 'trade_at_ms': LIVE_MS, 'fetched_at_ms': LIVE_MS}


def _row(**kw):
    base = dict(ticker='GS', direction='CALL', week='2026-10-05', id='GS',
                entry_price=893.15, target_price=902.09, model_entry_time=REF,
                contract=dict(symbol='GS261009C00892500', strike=892.5, expiration='2026-10-09'),
                quality_tier='A+', featured_rank=10, quality_score=84.42, target_atr_mult=0.374,
                config=dict(backtest_wr=0.85), stats_at_entry=dict(wins=5, losses=0),
                quote=dict(status='available', bid=12.40, ask=12.55, midpoint=12.475,
                           quote_at_ms=LIVE_MS),
                entry_premium=12.475, est_return_pct=41.0)
    base.update(kw)
    return base


def _roster_text(rows):
    return '\n'.join(p['content'] for p in messages.roster(rows, '2026-10-05', LIVE_MS / 1000))


def _field(payload, name):
    return next(f['value'] for f in payload['embeds'][0]['fields'] if f['name'] == name)


# --- rendering: both values -------------------------------------------------

def test_stock_text_shows_labeled_ref_and_live():
    assert messages.stock_text(_row(live_underlying=LIVE)) == \
        '$893.15 (ref 09:30 CT) · live $900.10 @ 09:44 CT'


def test_roster_row_shows_ref_and_live():
    text = _roster_text([_row(live_underlying=LIVE)])
    assert 'Stock $893.15 (ref 09:30 CT) · live $900.10 @ 09:44 CT → target $902.09' in text


def test_entry_embed_shows_ref_and_live():
    payload = messages.entry(_row(live_underlying=LIVE))
    assert _field(payload, 'Underlying Entry') == '$893.15 (ref 09:30 CT) · live $900.10 @ 09:44 CT'


def test_compact_line_shows_ref_and_live():
    p = _row(ticker='META', direction='PUT', entry_price=636.14, target_price=629.78,
             quality_score=91.0, live_underlying=dict(LIVE, price=643.53))
    # 2026-10-31 shadow board: the compact line ends with the cost
    # segment, not the retired 'Q<quality score>' (this row carries no
    # cost annotation, so it renders the quote-missing form).
    assert compact_line(p) == 'META PUT $636.14 (ref 09:30 CT) · live $643.53 @ 09:44 CT → $629.78 · Cost unavailable — entry quote missing'


# --- rendering: fallbacks ----------------------------------------------------

def test_stock_text_falls_back_to_labeled_ref_when_live_missing():
    assert messages.stock_text(_row()) == '$893.15 (ref 09:30 CT)'


def test_stock_text_falls_back_when_live_unavailable():
    p = _row(live_underlying={'status': 'unavailable', 'reason': 'trade_stale_or_future'})
    assert messages.stock_text(p) == '$893.15 (ref 09:30 CT)'


def test_roster_row_fallback_has_no_live_segment():
    text = _roster_text([_row(live_underlying={'status': 'unavailable', 'reason': 'trade_missing'})])
    assert 'Stock $893.15 (ref 09:30 CT) → target $902.09' in text
    assert '· live' not in text


def test_entry_embed_fallback_is_labeled_ref_only():
    payload = messages.entry(_row())
    assert _field(payload, 'Underlying Entry') == '$893.15 (ref 09:30 CT)'


def test_compact_line_fallback_is_labeled_ref_only():
    p = _row(ticker='META', direction='PUT', entry_price=636.14, target_price=629.78, quality_score=91.0)
    assert compact_line(p) == 'META PUT $636.14 (ref 09:30 CT) → $629.78 · Cost unavailable — entry quote missing'


def test_stock_text_without_model_entry_time_says_entry_ref():
    p = _row()
    del p['model_entry_time']
    assert messages.stock_text(p) == '$893.15 (entry ref)'


# --- ownership-layer entry format -------------------------------------------

def _ownership_row(**pub_kw):
    pub = dict(entry_price=636.14, target_price=629.78, underlying_target=629.78,
               est_return_pct=41.0, week='2026-10-05', model_entry_time=REF,
               quote=dict(bid=4.80, ask=5.25, ts=LIVE_MS / 1000),
               exit_rule='Underlying target, otherwise Friday close')
    pub.update(pub_kw)
    pub['publication'] = dict(category='smoothers', event='ENTRY',
                              contract=dict(underlying='META', strike=635, type='PUT',
                                            expiration='2026-10-09'))
    return {'payload': pub}


def test_ownership_entry_shows_ref_and_live():
    row = _ownership_row(live_underlying={'status': 'available', 'price': 643.53, 'ts': LIVE_MS / 1000})
    text = format_message(row)
    assert 'Stock $636.14 (ref 09:30 CT) → target $629.78' in text
    assert 'live $643.53 @ 09:44 CT' in text


def test_ownership_entry_fallback_is_labeled_ref_only():
    text = format_message(_ownership_row())
    assert 'Stock $636.14 (ref 09:30 CT) → target $629.78' in text
    assert 'live $' not in text


# --- data read: freshness gate ----------------------------------------------

class _Resp:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.content = json.dumps(payload).encode()

    def json(self):
        return json.loads(self.content)


class _Client:
    def __init__(self, payload, status=200):
        self.payload, self.status, self.calls = payload, status, []

    def get(self, path, params=None, headers=None):
        self.calls.append((path, params))
        return _Resp(self.payload, self.status)


def _iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat().replace('+00:00', 'Z')


def _data(payload, status=200):
    cfg = SimpleNamespace(alpaca_key='k', alpaca_secret='s',
                          alpaca_base_url='https://paper-api.alpaca.markets')
    client = _Client(payload, status)
    return Data(cfg, client), client


def _snapshots_payload(trades):
    return {'snapshots': {s: {'latestTrade': {'p': p, 't': _iso(t)}} for s, (p, t) in trades.items()}}


def test_stock_snapshots_fresh_trade_available():
    now = time.time()
    data, client = _data(_snapshots_payload({'META': (643.53, now - 5)}))
    out = data.stock_snapshots(['META'])
    assert out['META']['status'] == 'available' and out['META']['price'] == 643.53
    assert out['META']['trade_at_ms'] == int((now - 5) * 1000)
    assert 'stocks/snapshots' in client.calls[0][0]


def test_stock_snapshots_stale_trade_unavailable():
    now = time.time()
    data, _ = _data(_snapshots_payload({'META': (643.53, now - 31)}))
    out = data.stock_snapshots(['META'])
    assert out['META']['status'] == 'unavailable' and out['META']['reason'] == 'trade_stale_or_future'
    assert 'price' not in out['META']


def test_stock_snapshots_future_trade_unavailable():
    now = time.time()
    data, _ = _data(_snapshots_payload({'META': (643.53, now + 6)}))
    assert data.stock_snapshots(['META'])['META']['reason'] == 'trade_stale_or_future'


def test_stock_snapshots_missing_symbol_unavailable():
    data, _ = _data({'snapshots': {}})
    out = data.stock_snapshots(['META'])
    assert out['META'] == {'status': 'unavailable', 'reason': 'trade_missing'}


def test_stock_snapshots_provider_error_raises_for_caller_fallback():
    data, _ = _data({}, status=500)
    try:
        data.stock_snapshots(['META'])
    except ValueError:
        pass
    else:
        raise AssertionError('expected ValueError on provider HTTP 500')


def test_stock_snapshots_empty_request_makes_no_call():
    data, client = _data({})
    assert data.stock_snapshots([]) == {} and client.calls == []
