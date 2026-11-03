"""Fill confirmation for the live survivors book (2026-11-02).

The defect: survivors.py booked a live position the moment the
TradersPost webhook POST returned — but that response is acceptance
only, and on 2026-11-02 the S3 trade was rejected at Tradovate on both
legs while Compass booked it +$42 and the day-digest printed it as a
live trade. These tests pin the repair:

- verdicts (filled / rejected / no_order) recorded per leg are the only
  path into live accounting (counters, tripwire, review);
- pending sends count as NEITHER and stay pending (escalated once past
  the confirmation window, never auto-promoted, never dropped);
- the three 2026-11-02 exhibits as a regression suite: S3 rejected,
  S2 no broker order, S4 filled at broker prices ≠ modeled prices —
  through account_live, the day-digest, and /api/simple;
- the one-time history correction that marks the production record.
"""
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from compass.store import Store
from compass import fill_confirmation as fc, survivors

CT = ZoneInfo("America/Chicago")
# Monday 2026-11-02, 17:00 CT — after all three exhibits closed.
NOW = datetime(2026, 11, 2, 17, 0, tzinfo=CT).timestamp()

S3_ID = 'ict-survivor_s3-MGC.v.0-1793607435-t1'
S2_ID = 'ict-survivor_s2-MCL.v.0-1793607435-t1'
S4_ID = 'ict-survivor_s4-MGC.v.0-1793632218-t1'


@pytest.fixture
def db(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'fc.db'))
    s.initialize()
    yield s
    s.engine.dispose()


_STRATEGY_TAG = {'survivor_s1': 'survivor_s1_rvwap_fade',
                 'survivor_s2': 'survivor_s2_asia_low_fade',
                 'survivor_s3': 'survivor_s3_london_mr60',
                 'survivor_s4': 'survivor_s4_ny_mr60_fade'}


def _trade(tid, detector, symbol, side, entry, exit_, pnl, sent,
           status='closed', entered=1000.0, exited=2000.0, qty=1):
    return {'id': tid, 'detector': detector,
            'strategy': _STRATEGY_TAG.get(detector, detector + '_x'),
            'symbol': symbol,
            'side': side, 'qty': qty, 'entry': entry, 'exit': exit_,
            'pnl': pnl, 'status': status, 'entered_at': entered,
            'exited_at': exited if status == 'closed' else None,
            'traderspost_sent': sent, 'fill_version': 'v3'}


def _exhibits(db, c):
    """The three 2026-11-02 production rows, shaped as production had
    them BEFORE the correction (modeled prices, send flags)."""
    s3 = _trade(S3_ID, 'survivor_s3', 'MGC.v.0', 'long', 3904.5, 3909.0,
                42.0, True, entered=1793607435.0, exited=1793609293.0)
    s2 = _trade(S2_ID, 'survivor_s2', 'MCL.v.0', 'long', 100.21, 99.16,
                -108.0, False, entered=1793607435.0, exited=1793619595.0)
    s2['traderspost_skip_reason'] = \
        'skip MCL.v.0: already open for turtle_soup'
    s4 = _trade(S4_ID, 'survivor_s4', 'MGC.v.0', 'short', 3967.5, 3949.1,
                181.0, True, entered=1793632218.0, exited=1793651612.0)
    for t in (s3, s2, s4):
        db.put(c, 'trade:' + t['id'], t)
    return s3, s2, s4


# ---------------------------------------------------------------------
# Verdict recording
# ---------------------------------------------------------------------
def test_record_verdict_requires_known_trade_and_fill_price(db):
    with db.tx() as c:
        with pytest.raises(ValueError):
            fc.record_verdict(db, c, 'nope', 'entry', 'filled', NOW,
                              price=1.0)
        t = _trade('ict-survivor_s1-MNQ.c.0-1-t1', 'survivor_s1',
                   'MNQ.c.0', 'long', 100.0, None, None, True,
                   status='open', exited=None)
        db.put(c, 'trade:' + t['id'], t)
        with pytest.raises(ValueError):
            fc.record_verdict(db, c, t['id'], 'entry', 'filled', NOW)
        with pytest.raises(ValueError):
            fc.record_verdict(db, c, t['id'], 'entry', 'maybe', NOW)
        with pytest.raises(ValueError):
            fc.record_verdict(db, c, t['id'], 'middle', 'filled', NOW,
                              price=1.0)


def test_first_verdict_wins_unless_forced(db):
    with db.tx() as c:
        t = _trade('ict-survivor_s1-MNQ.c.0-1-t1', 'survivor_s1',
                   'MNQ.c.0', 'long', 100.0, None, None, True,
                   status='open', exited=None)
        db.put(c, 'trade:' + t['id'], t)
        row, created = fc.record_verdict(db, c, t['id'], 'entry',
                                         'rejected', NOW,
                                         reason='token expired')
        assert created and row['verdict'] == 'rejected'
        row2, created2 = fc.record_verdict(db, c, t['id'], 'entry',
                                           'filled', NOW, price=100.0)
        assert not created2 and row2['verdict'] == 'rejected'
        row3, created3 = fc.record_verdict(db, c, t['id'], 'entry',
                                           'filled', NOW, price=100.0,
                                           force=True,
                                           recorded_by='correction')
        assert created3 and row3['verdict'] == 'filled'
        assert row3['supersedes']['verdict'] == 'rejected'


# ---------------------------------------------------------------------
# Projection / accounting paths: confirm, reject, pending, timeout
# ---------------------------------------------------------------------
def test_unconfirmed_send_counts_as_neither_and_never_trips(db):
    """The phantom regression: a sent, closed, deeply negative MODELED
    trade with no verdict must not move P&L, fills, or the tripwire."""
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        t = _trade('ict-survivor_s1-MNQ.c.0-9-t1', 'survivor_s1',
                   'MNQ.c.0', 'long', 100.0, 50.0, -5000.0, True)
        db.put(c, 'trade:' + t['id'], t)
        st = survivors.account_live(db, c, st, NOW)
        assert st['combined_live_pnl'] == 0.0
        assert st['live_fills'] == 0 and st['live_trades_closed'] == 0
        assert st['live'] is True and st['live_pending'] == 1


def test_rejected_send_is_not_filled_and_carries_reason(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        t = _trade('ict-survivor_s3-MGC.v.0-9-t1', 'survivor_s3',
                   'MGC.v.0', 'long', 3904.5, 3909.0, 42.0, True)
        db.put(c, 'trade:' + t['id'], t)
        fc.record_verdict(db, c, t['id'], 'entry', 'rejected', NOW,
                          reason='Tradovate error: Access token is expired')
        trade = db.get(c, 'trade:' + t['id'])
        assert trade['live_status'] == 'not_filled'
        assert trade['fill_entry_status'] == 'rejected'
        assert 'Access token is expired' in trade['fill_entry_reason']
        st = survivors.account_live(db, c, st, NOW)
        assert st['combined_live_pnl'] == 0.0 and st['live_fills'] == 0
        assert st['live_not_filled'] == 1


def test_confirmed_open_counts_one_fill_no_pnl(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        t = _trade('ict-survivor_s1-MNQ.c.0-9-t1', 'survivor_s1',
                   'MNQ.c.0', 'long', 100.0, None, None, True,
                   status='open', exited=None)
        db.put(c, 'trade:' + t['id'], t)
        fc.record_verdict(db, c, t['id'], 'entry', 'filled', NOW,
                          price=100.25, at=NOW)
        trade = db.get(c, 'trade:' + t['id'])
        assert trade['live_status'] == 'confirmed_open'
        assert trade['fill_entry_price'] == 100.25
        st = survivors.account_live(db, c, st, NOW)
        assert st['live_fills'] == 1 and st['combined_live_pnl'] == 0.0


def test_entry_filled_exit_pending_counts_entry_only(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        t = _trade('ict-survivor_s4-MGC.v.0-9-t1', 'survivor_s4',
                   'MGC.v.0', 'short', 3967.5, 3949.1, 181.0, True)
        db.put(c, 'trade:' + t['id'], t)
        fc.record_verdict(db, c, t['id'], 'entry', 'filled', NOW,
                          price=3971.30, at=NOW - 3600)
        trade = db.get(c, 'trade:' + t['id'])
        assert trade['live_status'] == 'pending_exit'
        st = survivors.account_live(db, c, st, NOW)
        assert st['live_fills'] == 1 and st['live_trades_closed'] == 0
        assert st['combined_live_pnl'] == 0.0


def test_pending_past_window_escalates_once_and_stays_pending(db):
    with db.tx() as c:
        t = _trade('ict-survivor_s1-MNQ.c.0-9-t1', 'survivor_s1',
                   'MNQ.c.0', 'long', 100.0, 99.0, -20.0, True,
                   entered=NOW - fc.CONFIRM_WINDOW_S - 60,
                   exited=NOW - fc.CONFIRM_WINDOW_S - 30)
        db.put(c, 'trade:' + t['id'], t)
        s1 = fc.reconcile(db, c, NOW)
        assert len(s1['overdue_legs']) == 1
        assert s1['pending'] == 1
        s2 = fc.reconcile(db, c, NOW + 3600)
        assert s2['overdue_legs'] == []          # escalated exactly once
        assert s2['pending'] == 1                # still pending, not dropped
        trade = db.get(c, 'trade:' + t['id'])
        assert trade['live_status'] == 'pending'


def test_pending_inside_window_is_quiet(db):
    with db.tx() as c:
        t = _trade('ict-survivor_s1-MNQ.c.0-9-t1', 'survivor_s1',
                   'MNQ.c.0', 'long', 100.0, None, None, True,
                   status='open', exited=None, entered=NOW - 600)
        db.put(c, 'trade:' + t['id'], t)
        s = fc.reconcile(db, c, NOW)
        assert s['overdue_legs'] == [] and s['pending'] == 1


# ---------------------------------------------------------------------
# The 2026-11-02 exhibits + the one-time correction
# ---------------------------------------------------------------------
def test_correction_marks_the_three_exhibits(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        _exhibits(db, c)
        created = fc.apply_history_correction_2026_11_02(db, c, st, NOW)
        assert created == 5
        s3 = db.get(c, 'trade:' + S3_ID)
        assert s3['live_status'] == 'not_filled'
        assert s3['fill_entry_status'] == 'rejected'
        assert 'Access token is expired' in s3['fill_entry_reason']
        assert s3['pnl'] == 42.0  # modeled paper record untouched
        s2 = db.get(c, 'trade:' + S2_ID)
        assert s2['live_status'] == 'not_filled'
        assert s2['fill_entry_status'] == 'no_order'
        assert 'Never sent' in s2['fill_entry_reason']
        s4 = db.get(c, 'trade:' + S4_ID)
        assert s4['live_status'] == 'confirmed_closed'
        assert s4['fill_entry_price'] == 3971.30
        assert s4['fill_exit_price'] == 3949.10
        # (3971.30 - 3949.10) x 10 (MGC) short = +$222 gross, NOT the
        # modeled +$181.
        assert s4['live_confirmed_pnl'] == 222.0
        assert s4['pnl'] == 181.0  # modeled paper record untouched
        # Idempotent: a second application creates nothing.
        assert fc.apply_history_correction_2026_11_02(
            db, c, db.get(c, survivors.STATE_KEY), NOW) == 0


def test_counters_after_correction_match_broker_truth(db):
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        _exhibits(db, c)
        fc.apply_history_correction_2026_11_02(db, c, st, NOW)
        st = survivors.account_live(db, c, st, NOW)
        assert st['combined_live_pnl'] == 222.0
        assert st['live_trades_closed'] == 1
        assert st['live_fills'] == 2
        assert st['live_not_filled'] == 2
        assert st['live_pending'] == 0
        assert st['live'] is True


def test_digest_after_correction_tells_the_confirmed_truth(db):
    """Regression for the 2026-11-02 digest, which printed 'Combined
    live P&L $223.00 · live fills: 4 · closed live trades: 2'."""
    from compass.day_digest import assemble
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        _exhibits(db, c)
        fc.apply_history_correction_2026_11_02(db, c, st, NOW)
        survivors.account_live(db, c, st, NOW)
        out = assemble(db, c, NOW)
    sections = {s['key']: s for s in out['sections']}
    surv = '\n'.join(sections['survivors']['lines'])
    assert 'closed live trades: 1' in surv
    assert 'live fills: 2' in surv
    assert '$222' in surv
    assert 'NOT filled at the broker: 2' in surv
    fut = '\n'.join(sections['futures']['lines'])
    assert 'NOT FILLED — rejected at broker' in fut       # S3 row
    assert 'LIVE confirmed $222' in fut                   # S4 row


def test_simple_view_shows_only_confirmed_survivors(db):
    from compass import simple
    with db.tx() as c:
        st = survivors.ensure_state(db, c, NOW)
        _exhibits(db, c)
        # Pre-correction: nothing may show as live (no verdicts yet) —
        # the old code showed S3+S4 with modeled P&L.
        out = simple.summary(db, c, NOW)
        fut = out['types']['futures']
        names = [d['name'] for d in fut['drill']]
        assert not any('Survivor' in n for n in names)
        fc.apply_history_correction_2026_11_02(db, c, st, NOW)
        out = simple.summary(db, c, NOW)
        fut = out['types']['futures']
        by_name = {d['name']: d for d in fut['drill']}
        s4 = [v for k, v in by_name.items() if 'S4' in k]
        s3 = [v for k, v in by_name.items() if 'S3' in k]
        assert len(s4) == 1 and not s3
        assert s4[0]['stats']['pnl'] == 222.0  # confirmed, not +181


# ---------------------------------------------------------------------
# Endpoint
# ---------------------------------------------------------------------
def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from compass.app import create_app
    from compass.config import Config
    monkeypatch.setenv('COMPASS_PASSWORD', 'test-password-12345678')
    monkeypatch.setenv('COMPASS_LOCAL', 'true')
    monkeypatch.setenv('COMPASS_CRON_TOKEN', 'test-cron-token-' + 'x' * 40)
    cfg = Config()
    cfg.cron_token = 'test-cron-token-' + 'x' * 40
    cfg.db = 'sqlite:///' + str(tmp_path / 'verdict.db')
    cfg.password = 'test-password-12345678'
    cfg.local = True
    cfg.role = 'web'
    store = Store(cfg.db)
    store.initialize()
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    return client, store


AUTH = {'Authorization': 'Bearer ' + 'test-cron-token-' + 'x' * 40}
PATH = '/api/admin/survivor-fill-verdict'


def test_endpoint_requires_cron_token(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    r = client.post(PATH, json={'trade_id': S4_ID, 'leg': 'entry',
                                'verdict': 'filled', 'price': 1.0})
    assert r.status_code in (401, 403, 503)


def test_endpoint_records_verdict_and_updates_state(tmp_path, monkeypatch):
    client, store = _client(tmp_path, monkeypatch)
    with store.tx() as c:
        st = survivors.ensure_state(store, c, NOW)
        _exhibits(store, c)
        survivors.account_live(store, c, st, NOW)
    with client:
        r = client.post(PATH, headers=AUTH, json={
            'trade_id': S4_ID, 'leg': 'entry', 'verdict': 'filled',
            'price': 3971.30, 'at': 1793632285.0,
            'source': 'traderspost_ui', 'recorded_by': 'test'})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body['ok'] and body['created']
        assert body['live_status'] == 'pending_exit'
        r = client.post(PATH, headers=AUTH, json={
            'trade_id': S4_ID, 'leg': 'exit', 'verdict': 'filled',
            'price': 3949.10, 'at': 1793649016.0,
            'source': 'traderspost_ui', 'recorded_by': 'test'})
        body = r.json()
        assert body['live_status'] == 'confirmed_closed'
        assert body['live_confirmed_pnl'] == 222.0
        assert body['state']['combined_live_pnl'] == 222.0
        assert body['state']['live_fills'] == 2
        # Bad input is a 400, never a silent write.
        r = client.post(PATH, headers=AUTH, json={
            'trade_id': S4_ID, 'leg': 'entry', 'verdict': 'filled'})
        assert r.status_code == 400
        r = client.post(PATH, headers=AUTH, json={
            'trade_id': 'ict-turtle_soup-MCL.v.0-1-t1', 'leg': 'entry',
            'verdict': 'filled', 'price': 1.0})
        assert r.status_code == 400  # survivors only
        # GET reads the projection back.
        r = client.get(PATH, headers=AUTH,
                       params={'trade_id': S4_ID})
        assert r.status_code == 200
        assert r.json()['live']['live_status'] == 'confirmed_closed'
