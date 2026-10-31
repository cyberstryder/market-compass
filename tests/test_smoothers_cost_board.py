"""Smoothers shadow board (Josh-approved plan, 2026-10-30/31).

Pins the three presentation/scoring changes:
1. The board is ordered by the executable-cost key (entry spread % +
   theta rent %/day x 1.908) computed from each pick's entry quote —
   NOT by the quality tiers, which the graded record showed inverted
   (A+ -$26.49/pick vs WATCH +$75.85). Picks above the week's median
   entry spread carry a FRICTION flag with its reason.
2. Messages are shadow-board scoring notices: SHADOW BOARD roster with
   no tier sections, SCORED HIT instead of an exit 'signal', no
   'A+ Featured' entry title; the entry embed headlines Board Cost.
3. Wednesday horizon: a still-open pick is premium-scored out at
   Wednesday's chain bid (read server-side from the chain_eod events),
   stays OPEN for touch statistics, and gets a WEDNESDAY HORIZON
   notice. Missing chain data records an explicit unavailable outcome
   — never a fabricated price.
"""
import pytest
from sqlalchemy import select
from test_projects import db  # noqa: F401  (shared Store fixture)
from compass import smoothers_cost, smoothers_messages as messages
from compass.alert_ownership import format_payload
from compass.native_outbox import outbox as native_outbox
from compass.native_smoothers import (VERSION, weekly, save, monitor,
                                      wednesday_session, horizon_score)
from compass.smoothers_handoff import sessions
from compass.chain_eod import event_key as chain_event_key

WEEK = '2026-09-14'
CAL = sessions(WEEK)
WED = CAL[2]
ENTRY = CAL[0]['open'] + 3600
CONTRACT = {'symbol': 'DEMO260918C00102000', 'multiplier': 100,
            'strike': 102, 'expiration': '2026-09-18', 'type': 'CALL'}


def _quote(bid, ask, theta=-0.10, status='available'):
    mid = (bid + ask) / 2
    return dict(status=status, bid=bid, ask=ask, midpoint=mid,
                spread_pct=(ask - bid) / mid * 100,
                greeks={'theta': theta}, quote_at_ms=int(ENTRY * 1000))


def _pick(ticker, quote, **over):
    p = dict(id=ticker, week=WEEK, ticker=ticker, direction='CALL',
             status='OPEN', entry_price=100.0, target_price=102.0,
             model_entry_time=ENTRY, contract=dict(CONTRACT),
             quote=quote, entry_premium=(quote or {}).get('midpoint'),
             est_return_pct=25.0, quality_tier='WATCH', quality_score=10.0)
    p.update(over)
    return p


# ------------------------------------------------------------- cost key

def test_cost_key_matches_graded_construction():
    q = _quote(1.90, 2.00, theta=-0.2422)  # spread 5.128%, rent 12.11%/day
    assert smoothers_cost.spread_pct(q) == pytest.approx(5.128, abs=0.01)
    assert smoothers_cost.rent_pct_day(q) == pytest.approx(12.11, abs=0.01)
    assert smoothers_cost.cost_key(q) == pytest.approx(
        5.128 + 12.11 * 1.908, abs=0.05)


def test_cost_key_unusable_quotes_are_none():
    assert smoothers_cost.cost_key(None) is None
    assert smoothers_cost.cost_key({'status': 'unavailable'}) is None
    # Wide-spread quotes are still usable numbers (the provider's own
    # wide_spread status carries bid/ask/spread/greeks).
    q = _quote(1.0, 2.0, status='wide_spread')
    assert smoothers_cost.cost_key(q) is not None
    # No greeks -> no rent -> no key.
    q2 = _quote(1.9, 2.0)
    del q2['greeks']
    assert smoothers_cost.cost_key(q2) is None


def test_annotate_orders_by_cost_and_flags_friction():
    cheap = _pick('AAA', _quote(1.98, 2.00, theta=-0.08))    # spread ~1%
    mid = _pick('BBB', _quote(1.80, 2.00, theta=-0.24))      # spread ~10.5%
    wide = _pick('CCC', _quote(1.40, 2.00, theta=-0.30))     # spread ~35%
    missing = _pick('DDD', None)
    out = smoothers_cost.annotate([wide, missing, mid, cheap])
    assert [p['ticker'] for p in out] == ['AAA', 'BBB', 'CCC', 'DDD']
    assert [p['cost_rank'] for p in out] == [1, 2, 3, None]
    # Median spread of the ranked three is BBB's ~10.5%: only CCC is
    # above it, and CCC's rent is also above the median rent.
    assert out[2]['friction'] is True
    assert out[2]['friction_reason'] == 'wide spread + high rent'
    assert out[0]['friction'] is False and out[1]['friction'] is False
    assert out[3]['cost_key'] is None


def test_annotate_all_dte_classes_compete_head_to_head():
    # An 11-DTE-style low-rent contract beats a same-spread 4-DTE one:
    # no expiry-class separation anywhere in the key.
    slow = _pick('SLOW', _quote(1.90, 2.00, theta=-0.08))   # rent 4%/day
    fast = _pick('FAST', _quote(1.90, 2.00, theta=-0.24))   # rent 12%/day
    out = smoothers_cost.annotate([fast, slow])
    assert [p['ticker'] for p in out] == ['SLOW', 'FAST']


# ------------------------------------------------------------- messages

def _board_rows():
    rows = [_pick('AAA', _quote(1.98, 2.00, theta=-0.08)),
            _pick('BBB', _quote(1.80, 2.00, theta=-0.24)),
            _pick('CCC', _quote(1.40, 2.00, theta=-0.30)),
            _pick('DDD', None)]
    return smoothers_cost.annotate(rows)


def test_roster_is_a_shadow_board_ordered_by_cost():
    text = '\n'.join(p['content'] for p in messages.roster(_board_rows(), WEEK, ENTRY))
    assert 'SMOOTHERS — SHADOW BOARD' in text
    assert 'scored only · not a buy list' in text
    assert 'Horizon: Wednesday close · ranked by executable cost' in text
    # Cost order, not tier order; no tier sections anywhere.
    assert text.index('**AAA ') < text.index('**BBB ') < text.index('**CCC ') < text.index('**DDD ')
    for tier_header in ('**A+ (', '**A (', '**B (', '**WATCH ('):
        assert tier_header not in text
    assert 'Spread' in text and 'Rent' in text and 'Cost' in text
    assert '⚠ FRICTION — wide spread + high rent' in text
    assert 'Cost unavailable — entry quote missing' in text


def test_entry_embed_headlines_board_cost_not_tier():
    rows = _board_rows()
    payload = messages.entry(rows[0])
    embed = payload['embeds'][0]
    assert 'Featured' not in embed['title'] and '☀️' not in embed['title']
    fields = {f['name']: f['value'] for f in embed['fields']}
    assert 'Quality Tier' not in fields
    assert fields['Board Cost'].startswith('Cost #1 · Spread')
    assert 'not a buy list' in embed['description']


def test_target_message_is_a_scored_hit_with_horizon_note():
    p = _pick('AAA', _quote(1.98, 2.00), status='WIN',
              resolution_time=ENTRY + 90000, touch_detected_at=ENTRY + 90060,
              touch_bar={'open': 101.0, 'high': 102.5, 'low': 100.5, 'close': 102.1},
              exit_quote=_quote(2.90, 3.00),
              horizon={'date': '2026-09-16', 'status': 'scored', 'chain_bid': 2.5,
                       'entry_ask': 2.0, 'premium_net': 48.68, 'premium_pct': 25.0})
    payload = messages.target(p)
    embed = payload['embeds'][0]
    assert 'SCORED HIT' in embed['title']
    fields = {f['name']: f['value'] for f in embed['fields']}
    assert 'statistics only' in fields['Wednesday Horizon']


def test_horizon_message_scored_and_unavailable():
    p = _pick('AAA', _quote(1.98, 2.00),
              horizon={'date': '2026-09-16', 'status': 'scored', 'chain_bid': 2.5,
                       'entry_ask': 2.0, 'premium_net': 48.68, 'premium_pct': 25.0})
    body = messages.horizon(p)
    assert body['content'].startswith('WEDNESDAY HORIZON — AAA 102C')
    assert 'Scored out at Wednesday chain bid $2.50' in body['content']
    assert 'no longer count as premium outcomes' in body['content']
    p['horizon'] = {'date': '2026-09-16', 'status': 'unavailable',
                    'reason': 'contract_not_in_wednesday_chain'}
    body = messages.horizon(p)
    assert 'premium outcome unmeasurable (contract_not_in_wednesday_chain)' in body['content']


def test_delivery_embed_carries_board_cost_and_horizon():
    pub = dict(trade_id='t1', category='smoothers', event='ENTRY', horizon='swing',
               contract={'symbol': 'AAA261002C00100000', 'underlying': 'AAA',
                         'expiration': '2026-10-02', 'type': 'CALL', 'strike': 100})
    payload = dict(id='s1', project='smoothers', contract=pub['contract'], status='native_option_entry',
                   entry_price=100.0, target_price=102.0, underlying_target=102.0,
                   week=WEEK, model_entry_time=ENTRY,
                   quote=dict(bid=1.98, ask=2.00, ts=ENTRY), expires_at=ENTRY + 1800,
                   cost_rank=1, cost_key=9.6, entry_spread_pct=1.0, entry_rent_pct_day=4.5,
                   publication=pub)
    body = format_payload(dict(id='r1', source='smoothers', symbol='AAA', ts=ENTRY, payload=payload))
    fields = {f['name']: f['value'] for f in body['embeds'][0]['fields']}
    assert fields['Board Cost'].startswith('Board cost #1 · Spread 1.0%')
    pub2 = dict(pub, event='EXIT')
    payload2 = dict(payload, status='native_option_exit', exit_reason='target',
                    signal_status='WIN', exit_underlying=102.4, entry=2.0,
                    quote=dict(bid=2.9, ask=3.0, ts=ENTRY + 90000),
                    horizon={'date': '2026-09-16', 'status': 'scored', 'chain_bid': 2.5,
                             'premium_net': 48.68, 'premium_pct': 25.0},
                    publication=pub2)
    body2 = format_payload(dict(id='r2', source='smoothers', symbol='AAA', ts=ENTRY, payload=payload2))
    fields2 = {f['name']: f['value'] for f in body2['embeds'][0]['fields']}
    assert 'statistic only' in fields2['Wednesday Horizon']


# ------------------------------------------------------------- horizon

class _Provider:
    """monitor() at Wed close+600 has no active session, so bars are
    never requested on this path; quote() mirrors the loss-fix tests."""
    def quote(self, contract):
        return dict(status='available', bid=0.4, ask=0.5,
                    quote_at_ms=int((WED['close'] + 300) * 1000))


def _seed(db, pick, chain_contracts=None, chain_date='2026-09-16'):
    with db.tx() as c:
        db.put(c, VERSION + ':week:' + WEEK,
               {'week': WEEK, 'sessions': CAL, 'state': 'complete'})
        save(db, c, pick)
        if chain_contracts is not None:
            db.append(c, 'chain_eod', 'massive', pick['ticker'], WED['close'] + 600,
                      {'date': chain_date, 'symbol': pick['ticker'],
                       'contracts': chain_contracts},
                      key=chain_event_key(chain_date, pick['ticker']))


def _stored(db, pid='DEMO'):
    with db.tx() as c:
        return c.execute(select(weekly.c.payload)
                         .where(weekly.c.id == pid)).scalar_one()


def _open_pick(**over):
    p = dict(id='DEMO', week=WEEK, ticker='DEMO', status='OPEN',
             direction='CALL', entry_price=100.0, target_price=102.0,
             model_entry_time=ENTRY, created_at=ENTRY,
             last_seen_at=WED['close'] - 60, last_underlying=99.0,
             contract=dict(CONTRACT), quote=_quote(1.9, 2.0))
    p.update(over)
    return p


def test_wednesday_session_picks_the_wednesday():
    assert wednesday_session(CAL)['date'] == '2026-09-16'
    assert wednesday_session([]) is None


def test_horizon_scores_open_pick_at_wednesday_chain_bid(db):
    _seed(db, _open_pick(), chain_contracts=[
        {'symbol': CONTRACT['symbol'], 'quote': {'bid': 2.50, 'ask': 2.60}}])
    monitor(db, _Provider(), WED['close'] + 600)
    saved = _stored(db)
    # Premium fixed at the horizon; the pick itself stays OPEN so a
    # Thu/Fri touch still resolves as a statistic.
    assert saved['status'] == 'OPEN'
    h = saved['horizon']
    assert h['status'] == 'scored' and h['chain_bid'] == 2.50
    assert h['entry_ask'] == 2.0
    assert h['premium_net'] == 48.68  # (2.50-2.00)*100 - 1.32
    with db.tx() as c:
        keys = list(c.execute(select(native_outbox.c.event_key)).scalars())
    assert 'DEMO:horizon' in keys


def test_horizon_contract_absent_from_chain_is_unavailable_not_invented(db):
    _seed(db, _open_pick(), chain_contracts=[
        {'symbol': 'OTHER261016C00100000', 'quote': {'bid': 1.0, 'ask': 1.1}}])
    monitor(db, _Provider(), WED['close'] + 600)
    h = _stored(db)['horizon']
    assert h['status'] == 'unavailable'
    assert h['reason'] == 'contract_not_in_wednesday_chain'
    assert 'premium_net' not in h


def test_horizon_waits_for_the_snapshot_then_scores(db):
    _seed(db, _open_pick())  # no chain event stored yet
    monitor(db, _Provider(), WED['close'] + 600)
    assert 'horizon' not in _stored(db)  # retry window, nothing recorded
    with db.tx() as c:
        db.append(c, 'chain_eod', 'massive', 'DEMO', WED['close'] + 3600,
                  {'date': '2026-09-16', 'symbol': 'DEMO',
                   'contracts': [{'symbol': CONTRACT['symbol'],
                                  'quote': {'bid': 2.50, 'ask': 2.60}}]},
                  key=chain_event_key('2026-09-16', 'DEMO'))
    monitor(db, _Provider(), WED['close'] + 3600)
    assert _stored(db)['horizon']['status'] == 'scored'


def test_horizon_score_direct_unavailable_paths(db):
    p = _open_pick(contract=None)
    with db.tx() as c:
        h = horizon_score(c, p, WED, WED['close'] + 600)
    assert h['status'] == 'unavailable' and h['reason'] == 'no_contract_recorded'
