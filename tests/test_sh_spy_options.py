"""Structure Hunter SPY options forward premium scorer (2026-11-04,
Josh): registration a567aac0 — signal parity against the frozen SPY
gauntlet's scored trade list, contract selection (expiry >=2h rule,
delta-nearest strike), entry gate + underlying tracking, close-based
exit precedence (stop / target / time / flatten), data_gap on missing
quotes, the §6 display-only shadow alerts (rendering, byte-identity
with the flag on/off, silence-gate non-interference, queue failure
never blocks booking), and the store.py table registration
regression (the bb1adfa lesson)."""
import csv
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

import compass.app  # noqa: F401  (registers every table in store meta)
from compass import sh_spy_options as M
from compass.active_observations import inventory
from compass.config import Config
from compass.market import NY
from compass.store import Store, events, discord_jobs, \
    sh_spy_options_episodes, sh_spy_push_outbox

DATE = '2026-11-05'           # a Thursday session, after the freeze
T0 = datetime(2026, 11, 5, 9, 30, tzinfo=NY).timestamp()
OCC_CALL = 'SPY251105C00100000'
OCC_PUT = 'SPY251105P00100000'


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'sh_spy.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def cfg(**kw):
    base = dict(local=True, stocks=(), watchlist=(), sector_etfs=(),
                swing_ideas=False, morning_enabled=False, discovery=False)
    base.update(kw)
    return Config(**base)


# ── synthetic session scaffolding ────────────────────────────────────
# The pattern engine itself is pinned by the parity test below; the
# lifecycle tests monkeypatch it out and place one known signal.

class _Ind:
    def __init__(self, bars):
        self.atr = [1.0] * bars['n']


def place_signals(monkeypatch, spec):
    """spec: {bar_index: [signal dict, ...]} placed by the fake engine."""
    def fake_engine(bars, ind):
        stream = [None] * bars['n']
        for i, sigs in spec.items():
            stream[i] = sigs
        return stream, {}
    monkeypatch.setattr(M, 'Indicators', _Ind)
    monkeypatch.setattr(M, 'run_engine', fake_engine)


def boot(db):
    """Pre-seed the bootstrap stamp: production bootstraps with a
    watermark at the newest existing bar (live-only formation); the
    scenarios below drive already-seeded bars, so they simulate a
    scorer that was live before their session started."""
    with db.tx() as c:
        db.put(c, M.STATE_KEY, {'bootstrap_at': T0})


def signal(dir=1, fail=99.0, tgt=110.0, cat=2, nm='Rising Wedge'):
    return dict(dir=dir, lvl=100.0, tgt=tgt, fail=fail, cat=cat,
                vol=True, retest=False, late=False, nm=nm)


def seed_bars(db, closes, opens=None, start=T0):
    items = []
    prev = closes[0]
    for i, cl in enumerate(closes):
        o = opens[i] if opens is not None else prev
        items.append(('SPY', start + 60 * i,
                      {'o': o, 'h': max(o, cl) + 0.5, 'l': min(o, cl) - 0.5,
                       'c': cl, 'v': 10000}))
        prev = cl
    with db.tx() as c:
        db.append_bars(c, 'bar', 'test', items)


def chain_payload(contracts, asof, spot=100.10):
    return {'contracts': contracts, 'complete': True, 'asof': asof,
            'source': 'test', 'selection_reference': spot,
            'selection_reference_ts': asof}


def call_contract(symbol, expiry, strike, delta, bid=1.20, ask=1.22, qts=0):
    return {'symbol': symbol, 'expiry': expiry, 'strike': strike,
            'type': 'call', 'delta': delta, 'iv': 0.20,
            'quote': {'ts': qts, 'bid': bid, 'ask': ask}}


def seed_chain(db, asof, contracts=None):
    if contracts is None:
        contracts = [
            call_contract('SPY251105C00099000', DATE, 99.0, 0.62),
            call_contract(OCC_CALL, DATE, 100.0, 0.51),
            call_contract('SPY251105C00101000', DATE, 101.0, 0.40),
        ]
    with db.tx() as c:
        db.put(c, 'chain:SPY', chain_payload(contracts, asof))


def seed_quote(db, symbol, ts, bid, ask):
    with db.tx() as c:
        db.put(c, 'quote:' + symbol,
               {'bid': bid, 'ask': ask, 'ts': ts, 'source': 'alpaca'})


def episodes_of(db):
    with db.tx() as c:
        return c.execute(select(sh_spy_options_episodes.c.payload)
                         .order_by(sh_spy_options_episodes.c.created)
                         ).scalars().all()


def outbox_of(db):
    with db.tx() as c:
        return c.execute(select(sh_spy_push_outbox.c.payload)
                         .order_by(sh_spy_push_outbox.c.created)
                         ).scalars().all()


def state_of(db):
    with db.tx() as c:
        return db.get(c, M.STATE_KEY, {})


def drive_to_entry(db, monkeypatch, quote=(1.20, 1.22), closes=None):
    """Signal at bar 10 (close 100.0), entry bar 11 (open 100.10);
    episode created and premium-priced in one pass."""
    boot(db)
    place_signals(monkeypatch, {10: [signal()]})
    closes = closes if closes is not None else [100.0] * 11 + [100.10] + [100.5] * 60
    opens = [100.0] * len(closes)
    opens[11] = 100.10
    seed_bars(db, closes, opens=opens)
    entry_ts = T0 + 11 * 60
    seed_chain(db, asof=entry_ts + 60)
    seed_quote(db, OCC_CALL, entry_ts + 5, quote[0], quote[1])
    scorer = M.SHSpyOptions(db, cfg())
    scorer.process(entry_ts + 60)
    return scorer


# ── store registration + pinning ─────────────────────────────────────

def test_tables_registered_and_contract_pins(db):
    import compass.store as store_mod
    assert 'sh_spy_options_episodes' in store_mod.meta.tables
    assert 'sh_spy_push_outbox_v1' in store_mod.meta.tables
    now = T0 + 3600
    p = {'id': 'ep-test', 'symbol': 'SPY', 'underlying': 'SPY',
         'contract': {'symbol': OCC_CALL}}
    with db.tx() as c:
        c.execute(db.insert(sh_spy_options_episodes).values(
            id='ep-test', underlying='SPY', status='open', created=now,
            updated=now, payload=p))
    with db.tx() as c:
        inv = inventory(db, c, now)
    assert OCC_CALL in inv['options']


# ── contract selection (registration §2) ─────────────────────────────

def _sel_contracts():
    return [
        call_contract('C0', DATE, 100.0, 0.51),
        call_contract('C1', '2026-11-06', 100.0, 0.50),
        {'symbol': 'P0', 'expiry': DATE, 'strike': 100.0, 'type': 'put',
         'delta': -0.49, 'quote': {'ts': 0, 'bid': 1.0, 'ask': 1.02}},
        {'symbol': 'P1', 'expiry': '2026-11-06', 'strike': 100.0,
         'type': 'put', 'delta': -0.50,
         'quote': {'ts': 0, 'bid': 1.0, 'ask': 1.02}},
        # admissible-set exclusions: no delta quoted
        {'symbol': 'CX', 'expiry': DATE, 'strike': 99.0, 'type': 'call',
         'delta': None, 'quote': {'ts': 0, 'bid': 1.0, 'ask': 1.02}},
    ]


def test_expiry_two_hour_boundary():
    contracts = _sel_contracts()
    before = datetime(2026, 11, 5, 13, 59, tzinfo=NY).timestamp()
    at = datetime(2026, 11, 5, 14, 0, tzinfo=NY).timestamp()
    assert M.select_contract(contracts, 1, before, 100.0)['symbol'] == 'C0'
    # At exactly 14:00 ET the 0DTE has exactly 2h left: not >2h.
    assert M.select_contract(contracts, 1, at, 100.0)['symbol'] == 'C1'
    assert M.select_contract(contracts, -1, before, 100.0)['symbol'] == 'P0'
    assert M.select_contract(contracts, -1, at, 100.0)['symbol'] == 'P1'


def test_delta_nearest_and_tiebreak():
    contracts = [
        call_contract('A', DATE, 99.0, 0.44),
        call_contract('B', DATE, 100.0, 0.52),
        call_contract('C', DATE, 101.0, 0.63),
    ]
    noon = datetime(2026, 11, 5, 12, 0, tzinfo=NY).timestamp()
    assert M.select_contract(contracts, 1, noon, 100.0)['symbol'] == 'B'
    # Exact |delta-0.50| tie (0.25/0.75 are exact in binary) ->
    # strike closest to spot wins.
    contracts = [
        call_contract('A', DATE, 99.0, 0.25),
        call_contract('B', DATE, 100.0, 0.75),
    ]
    assert M.select_contract(contracts, 1, noon, 100.4)['symbol'] == 'B'
    assert M.select_contract(contracts, 1, noon, 99.4)['symbol'] == 'A'


def test_spread_gate_bound():
    spread, gate, passes = M.spread_gate(1.20, 1.22)
    assert passes and abs(gate - 0.183) < 1e-9
    spread, gate, passes = M.spread_gate(0.10, 0.16)
    assert not passes and abs(gate - 0.05) < 1e-9   # the $0.05 floor binds
    spread, gate, passes = M.spread_gate(0.10, 0.15)
    assert passes


# ── lifecycle: entry, gate, exits, data_gap ──────────────────────────

def test_entry_priced_and_open_alert(db, monkeypatch):
    drive_to_entry(db, monkeypatch)
    eps = episodes_of(db)
    assert len(eps) == 1
    p = eps[0]
    assert p['premium_status'] == 'open' and p['status'] == 'open'
    assert p['entry']['ask'] == 1.22 and p['entry']['bid'] == 1.20
    assert p['contract']['symbol'] == OCC_CALL
    assert p['underlying_entry']['price'] == 100.10
    assert p['signal']['stop'] == 99.0 and p['signal']['target'] == 103.0
    out = outbox_of(db)
    assert len(out) == 1 and out[0]['type'] == 'open'
    body = M.format_open_message(out[0])
    assert 'SH-SPY SHADOW' in body['content']


def test_gate_excluded_underlying_still_tracked(db, monkeypatch):
    closes = [100.0] * 11 + [100.10] + [100.5] * 8 + [103.2] + [100.5] * 5
    drive_to_entry(db, monkeypatch, quote=(1.00, 1.30), closes=closes)
    eps = episodes_of(db)
    assert len(eps) == 1
    p = eps[0]
    assert p['status'] == 'gate_excluded' and p['entry'] is None
    assert p['gate_exclusion']['spread'] == pytest.approx(0.30)
    assert outbox_of(db) == []            # gate_excluded never alerts
    scorer = M.SHSpyOptions(db, cfg())
    scorer.process(T0 + len(closes) * 60 + 120)
    p = episodes_of(db)[0]
    assert p['underlying_exit']['reason'] == 'target'
    assert p['underlying_exit']['net_dollars'] == pytest.approx(
        (103.2 - 100.10 - 0.02) * 100)
    assert p['stage'] == 'done'


def test_exit_target_priced_and_close_alert(db, monkeypatch):
    closes = [100.0] * 11 + [100.10] + [100.5] * 4 + [103.2] + [100.5] * 5
    scorer = drive_to_entry(db, monkeypatch, closes=closes)
    trig_bar = 16
    instant = T0 + (trig_bar + 1) * 60
    seed_quote(db, OCC_CALL, instant + 3, 3.40, 3.44)
    scorer.process(instant + 30)
    p = episodes_of(db)[0]
    assert p['status'] == 'closed' and p['premium_status'] == 'closed'
    assert p['exit_trigger']['reason'] == 'target'
    assert p['exit']['bid'] == 3.40
    assert p['pnl'] == pytest.approx((3.40 - 1.22) * 100 - 1.30)
    out = outbox_of(db)
    assert [o['type'] for o in out] == ['open', 'close']
    assert out[1]['reason'] == 'target'


def test_exit_stop_precedence_over_time(db, monkeypatch):
    # The stop close lands exactly on the time-stop bar (sig 10 + 40):
    # stop wins the precedence order.
    closes = [100.0] * 11 + [100.10] + [100.5] * 38 + [98.9] + [100.5] * 3
    scorer = drive_to_entry(db, monkeypatch, closes=closes)
    instant = T0 + 51 * 60
    seed_quote(db, OCC_CALL, instant + 3, 0.60, 0.63)
    scorer.process(instant + 30)
    p = episodes_of(db)[0]
    assert p['exit_trigger']['reason'] == 'stop'
    assert p['status'] == 'closed'


def test_exit_time_stop(db, monkeypatch):
    closes = [100.0] * 11 + [100.10] + [100.5] * 45
    scorer = drive_to_entry(db, monkeypatch, closes=closes)
    instant = T0 + 51 * 60     # close of bar 50 = signal bar 10 + 40
    seed_quote(db, OCC_CALL, instant + 3, 1.00, 1.03)
    scorer.process(instant + 30)
    p = episodes_of(db)[0]
    assert p['exit_trigger']['reason'] == 'time'
    assert p['status'] == 'closed'


def test_flatten_at_1545_et(db, monkeypatch):
    # Late entry (signal 15:30, entry bar 15:31) so the 15:45 flatten,
    # not the 40-bar time stop, is the exit that fires.
    boot(db)
    place_signals(monkeypatch, {360: [signal()]})
    closes = [100.0] * 361 + [100.10] + [100.5] * 14   # through 15:45 ET
    opens = [100.0] * len(closes)
    opens[361] = 100.10
    seed_bars(db, closes, opens=opens)
    entry_ts = T0 + 361 * 60
    # A 15:31 ET entry takes the NEXT listed expiry (>2h rule), so the
    # chain here carries 2026-11-06 contracts.
    occ_next = 'SPY251106C00100000'
    seed_chain(db, asof=entry_ts + 60, contracts=[
        call_contract('SPY251106C00099000', '2026-11-06', 99.0, 0.62),
        call_contract(occ_next, '2026-11-06', 100.0, 0.51),
        call_contract('SPY251106C00101000', '2026-11-06', 101.0, 0.40),
    ])
    seed_quote(db, occ_next, entry_ts + 5, 1.20, 1.22)
    scorer = M.SHSpyOptions(db, cfg())
    scorer.process(entry_ts + 60)
    assert episodes_of(db)[0]['premium_status'] == 'open'
    assert episodes_of(db)[0]['contract']['symbol'] == occ_next
    flat = datetime(2026, 11, 5, 15, 45, tzinfo=NY).timestamp()
    seed_quote(db, occ_next, flat + 5, 0.90, 0.93)
    scorer.process(flat + 60)
    p = episodes_of(db)[0]
    assert p['exit_trigger']['reason'] == 'flatten'
    # Underlying flatten price = close of the last bar ending <= 15:45.
    assert p['underlying_exit']['price'] == 100.5
    assert p['status'] == 'closed' and p['exit']['bid'] == 0.90


def test_entry_data_gap_on_missing_quotes(db, monkeypatch):
    boot(db)
    place_signals(monkeypatch, {10: [signal()]})
    closes = [100.0] * 11 + [100.10] + [100.5] * 10 + [98.9] + [100.5] * 3
    seed_bars(db, closes)
    entry_ts = T0 + 11 * 60
    seed_chain(db, asof=entry_ts + 60)     # chain quotes are stale (qts=0)
    scorer = M.SHSpyOptions(db, cfg())
    scorer.process(entry_ts + M.QUOTE_WAIT_S + 30)
    p = episodes_of(db)[0]
    assert p['status'] == 'data_gap'
    assert p['entry_data_gap']['reason'] == 'entry_quote_missing'
    assert outbox_of(db) == []            # data_gap never alerts
    # The underlying leg still runs to its frozen exit.
    scorer.process(T0 + len(closes) * 60 + 120)
    p = episodes_of(db)[0]
    assert p['underlying_exit']['reason'] == 'stop'
    assert p['stage'] == 'done'


def test_exit_data_gap_on_missing_bid(db, monkeypatch):
    closes = [100.0] * 11 + [100.10] + [100.5] * 4 + [98.9] + [100.5] * 3
    scorer = drive_to_entry(db, monkeypatch, closes=closes)
    instant = T0 + 17 * 60
    scorer.process(instant + M.QUOTE_WAIT_S + 30)   # no fresher quote exists
    p = episodes_of(db)[0]
    assert p['status'] == 'data_gap'
    assert p['exit_data_gap']['reason'] == 'exit_quote_missing'
    assert p['underlying_exit']['reason'] == 'stop'
    assert [o['type'] for o in outbox_of(db)] == ['open']   # no close alert


def test_occupancy_second_signal_ignored(db, monkeypatch):
    boot(db)
    place_signals(monkeypatch, {10: [signal()], 20: [signal()]})
    closes = [100.0] * 11 + [100.10] + [100.5] * 30
    seed_bars(db, closes)
    entry_ts = T0 + 11 * 60
    seed_chain(db, asof=entry_ts + 60)
    seed_quote(db, OCC_CALL, entry_ts + 5, 1.20, 1.22)
    scorer = M.SHSpyOptions(db, cfg())
    scorer.process(T0 + len(closes) * 60 + 60)
    assert len(episodes_of(db)) == 1


def test_flatten_skipped_entry_bar(db, monkeypatch):
    # Signal on the 15:44 bar: its entry bar opens at 15:45 ET, the
    # flatten instant — no episode forms (registration §2 pin).
    boot(db)
    place_signals(monkeypatch, {374: [signal()]})
    seed_bars(db, [100.0] * 380)
    scorer = M.SHSpyOptions(db, cfg())
    scorer.process(T0 + 380 * 60)
    assert episodes_of(db) == []
    assert state_of(db)['flatten_skipped'] == 1


def test_bootstrap_watermarks_existing_bars(db, monkeypatch):
    # First-ever pass over an already-complete session forms NOTHING:
    # pricing is live-quote based, so a completed session is never
    # replayed into episodes at first boot.
    place_signals(monkeypatch, {10: [signal()]})
    seed_bars(db, [100.0] * 30)
    scorer = M.SHSpyOptions(db, cfg())
    scorer.process(T0 + 30 * 60)
    assert episodes_of(db) == []
    st = state_of(db)
    assert st['bootstrap_at'] is not None
    assert st['last_bar_ts'] == T0 + 29 * 60


def test_sample_floor_pre_freeze_session_ignored(db, monkeypatch):
    boot(db)
    place_signals(monkeypatch, {10: [signal()]})
    t_pre = datetime(2026, 11, 3, 9, 30, tzinfo=NY).timestamp()
    seed_bars(db, [100.0] * 30, start=t_pre)
    scorer = M.SHSpyOptions(db, cfg())
    scorer.process(t_pre + 30 * 60)
    assert episodes_of(db) == []


# ── §6 alerts: rendering, invariance, failure behavior ───────────────

def _open_payload():
    return {'type': 'open', 'episode_id': 'ep1', 'pattern': 'Rising Wedge',
            'dir': 1, 'contract': {'symbol': OCC_CALL, 'expiry': DATE,
                                   'strike': 100.0, 'type': 'call'},
            'entry_ask': 1.22, 'stop': 99.0, 'target': 103.0, 'at': T0}


def _close_payload():
    return {'type': 'close', 'episode_id': 'ep1', 'pattern': 'Rising Wedge',
            'dir': 1, 'contract': {'symbol': OCC_CALL, 'expiry': DATE,
                                   'strike': 100.0, 'type': 'call'},
            'entry_ask': 1.22, 'exit_bid': 3.40, 'pnl': 216.70,
            'reason': 'target', 'underlying_pnl': 308.0, 'at': T0}


def test_alert_rendering_labels_and_no_live_language():
    for body in (M.format_open_message(_open_payload()),
                 M.format_close_message(_close_payload())):
        text = body['content']
        assert text.startswith('🧪 **SH-SPY SHADOW**')
        assert 'Rising Wedge' in text and 'LONG' in text
        assert DATE in text and '100' in text
        assert 'shadow' in text.lower()
        for banned in ('LIVE', 'TradersPost', '🔴', 'live order', 'filled'):
            assert banned not in text
    close = M.format_close_message(_close_payload())['content']
    assert 'TARGET' in close and '+$216.70' in close and '$3.40' in close
    opened = M.format_open_message(_open_payload())['content']
    assert '$1.22' in opened


def _full_scenario(db, monkeypatch, alerts):
    closes = [100.0] * 11 + [100.10] + [100.5] * 4 + [103.2] + [100.5] * 5
    boot(db)
    place_signals(monkeypatch, {10: [signal()]})
    seed_bars(db, closes)
    entry_ts = T0 + 11 * 60
    seed_chain(db, asof=entry_ts + 60)
    seed_quote(db, OCC_CALL, entry_ts + 5, 1.20, 1.22)
    scorer = M.SHSpyOptions(db, cfg(sh_spy_alerts=alerts))
    scorer.process(entry_ts + 60)
    instant = T0 + 17 * 60
    seed_quote(db, OCC_CALL, instant + 3, 3.40, 3.44)
    scorer.process(instant + 30)


def _episode_dump(db):
    with db.tx() as c:
        eps = c.execute(select(sh_spy_options_episodes.c.status,
                               sh_spy_options_episodes.c.payload)
                        ).all()
        evs = c.execute(select(events.c.kind, events.c.payload)
                        .where(events.c.kind == M.KIND)).all()
    return eps, evs


def test_alert_flag_byte_identity(db, tmp_path, monkeypatch):
    _full_scenario(db, monkeypatch, alerts=True)
    eps_on, evs_on = _episode_dump(db)
    assert len(outbox_of(db)) == 2
    db2 = Store('sqlite:///' + str(tmp_path / 'sh_spy_off.db'))
    db2.initialize()
    try:
        _full_scenario(db2, monkeypatch, alerts=False)
        eps_off, evs_off = _episode_dump(db2)
        assert outbox_of(db2) == []
    finally:
        db2.engine.dispose()
    assert eps_on == eps_off      # episode rows byte-identical
    assert evs_on == evs_off      # lifecycle events byte-identical


def test_no_alert_pipeline_involvement(db, monkeypatch):
    _full_scenario(db, monkeypatch, alerts=True)
    with db.tx() as c:
        kinds = c.execute(select(events.c.kind).distinct()).scalars().all()
        jobs = c.execute(select(discord_jobs)).all()
    assert 'alert' not in kinds          # never enters the alert pipeline
    assert jobs == []                    # nothing in the shared Discord queue
    # The scanner silence gate itself is untouched and still suppresses.
    from compass.alerts import scanner_setup_silence, SCANNER_PUSH_GATE
    row = {'symbol': 'SPY', 'payload': {
        'status': 'setup_triggered', 'rule': 'flow_price_breakout',
        'strategy': '0dte-compass-scanner-v2:flow_price_breakout'}}
    decision = scanner_setup_silence(row, cfg(), T0)
    assert decision is not None and decision['gate'] == SCANNER_PUSH_GATE


def test_queue_failure_never_blocks_booking(db, monkeypatch):
    # Break the outbox insert: queue_alert must return False, land the
    # failure on the health marker path, and never raise.
    def boom(*a, **k):
        raise RuntimeError('insert down')
    monkeypatch.setattr(db, 'insert', boom)
    assert M.queue_alert(db, cfg(), _open_payload()) is False


def test_queue_failure_end_to_end_episode_still_books(db, monkeypatch):
    # Break ONLY the outbox insert (every other insert passes through):
    # episodes must book and score exactly as with a healthy queue,
    # and the failure must land on the health:sh_spy_push marker.
    real_insert = db.insert

    def flaky(table, *a, **k):
        if table is sh_spy_push_outbox:
            raise RuntimeError('outbox insert down')
        return real_insert(table, *a, **k)
    monkeypatch.setattr(db, 'insert', flaky)
    _full_scenario(db, monkeypatch, alerts=True)
    eps = episodes_of(db)
    assert len(eps) == 1 and eps[0]['status'] == 'closed'
    assert outbox_of(db) == []
    with db.tx() as c:
        health = db.get(c, 'health:sh_spy_push', {})
    assert health.get('status') == 'error'


def test_deliver_one_push_formatter_failure_fails_row(db):
    payload = _close_payload()
    payload['pnl'] = 'not-a-number'      # _money raises TypeError
    with db.tx() as c:
        c.execute(db.insert(sh_spy_push_outbox).values(
            id='row-bad', status='pending', created=T0, payload=payload,
            delivery={'attempts': 0}))
    import httpx
    client = httpx.Client(timeout=5, trust_env=False)
    try:
        attempted = M.deliver_one_push(
            db, client, 'https://discord.com/api/webhooks/123/abc', now=T0 + 10)
    finally:
        client.close()
    assert attempted is True
    with db.tx() as c:
        row = c.execute(select(sh_spy_push_outbox).where(
            sh_spy_push_outbox.c.id == 'row-bad')).mappings().first()
    assert row['status'] == 'failed'
    assert 'TypeError' in row['delivery']['error']


# ── parity: the vendored engine vs the gauntlet's scored list ────────

def _default_archive():
    # The gauntlet archive lives in the goal workspace, three levels
    # above the repo checkout. In the Docker test stage the tree is
    # only /app deep, so parents[3] does not exist — indexing it raised
    # IndexError at collection and failed the packaged suite (PR run
    # 41924739863, step 8). Fall back to this file's own directory: the
    # archive is absent there too, and the parity test below skips, as
    # designed, wherever the archive is not present.
    here = Path(__file__).resolve()
    root = here.parents[3] if len(here.parents) > 3 else here.parent
    return (root / 'goals' / 'market-compass-trading-improvements'
            / 'hidden_files' / 'structure-hunter-spy-2026-11-04')


ARCHIVE = Path(os.environ.get('SH_SPY_ARCHIVE_DIR', _default_archive()))
RAW_SHA256 = '14bd5e08a9f90a757caae94b12fc2484914ca389a2cc0ee343e61bb04b78426c'
TRADES_SHA256 = '8aab9b5f5edc24696a79b952f85e0ac13b426e26022fb71d5161fcc9aa57c051'


def _load_archived_bars(raw_path):
    from zoneinfo import ZoneInfo
    ct = ZoneInfo('America/Chicago')
    bars = {'o': [], 'h': [], 'l': [], 'c': [], 'v': [],
            'start_ct': [], 'session': []}
    with open(raw_path) as f:
        for row in csv.DictReader(f):
            ts = datetime.fromisoformat(row['t'].replace('Z', '+00:00'))
            et = ts.astimezone(NY)
            mins = et.hour * 60 + et.minute
            if not (570 <= mins < 960):
                continue
            d = et.date().isoformat()
            if d < '2026-06-01' or d > '2026-11-04':
                continue
            bars['o'].append(float(row['o'])); bars['h'].append(float(row['h']))
            bars['l'].append(float(row['l'])); bars['c'].append(float(row['c']))
            bars['v'].append(float(row['v']))
            bars['start_ct'].append(ts.astimezone(ct))
            bars['session'].append(d)
    bars['n'] = len(bars['o'])
    return bars


@pytest.mark.skipif(not (ARCHIVE / 'spy_1m_raw.csv').exists(),
                    reason='gauntlet archive lives in the goal workspace, '
                           'not the repo; parity runs where the archive is')
def test_signal_parity_vs_gauntlet_scored_list():
    for name, want in (('spy_1m_raw.csv', RAW_SHA256),
                       ('trades_scored_spy.csv', TRADES_SHA256)):
        got = hashlib.sha256((ARCHIVE / name).read_bytes()).hexdigest()
        assert got == want, '%s changed since the gauntlet freeze' % name
    bars = _load_archived_bars(ARCHIVE / 'spy_1m_raw.csv')
    ind = M.Indicators(bars)
    sig, _ = M.run_engine(bars, ind)
    trades = M.run_strategy_shares(bars, ind, sig)
    mine = [(bars['start_ct'][t['entry_bar']].strftime('%Y-%m-%d %H:%M'),
             bars['session'][t['entry_bar']], t['dir'], t['cat'], t['nm'],
             round(t['entry'], 4), t['sig_bar'])
            for t in trades
            if bars['session'][t['entry_bar']] >= '2026-08-03']
    theirs = []
    with open(ARCHIVE / 'trades_scored_spy.csv') as f:
        for row in csv.DictReader(f):
            theirs.append((row['entry_time'], row['session'],
                           int(row['dir']), int(row['cat']), row['nm'],
                           round(float(row['entry']), 4),
                           int(row['sig_bar'])))
    assert len(theirs) == 365
    assert mine == theirs
