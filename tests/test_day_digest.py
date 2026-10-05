"""Focused tests for the end-of-day digest (compass/day_digest.py)."""
import time
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from compass.store import Store
from compass import day_digest
from compass.native_outbox import outbox as native_outbox

CT = ZoneInfo("America/Chicago")


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'digest.db'))
    db.initialize()
    yield db
    db.engine.dispose()


def _now_today():
    d = datetime.now(CT).date()
    return datetime(d.year, d.month, d.day, 12, 0, tzinfo=CT).timestamp()


def test_smoothers_entry_and_exit(db):
    now = _now_today()
    pub_entry = {'contract': {'underlying': 'META', 'strike': 745, 'type': 'CALL',
                              'expiration': '2026-10-07'},
                 'entry_price': 744.17, 'target_price': 751.61,
                 'quote': {'bid': 7.8, 'ask': 7.95}}
    pub_exit = {'contract': {'underlying': 'COP', 'strike': 127, 'type': 'CALL',
                             'expiration': '2026-10-09'},
                'exit_reason': 'target', 'quote': {'bid': 2.72, 'ask': 3.3}}
    with db.tx() as c:
        c.execute(db.insert(native_outbox).values(
            id='s1', program='smoothers', event_key='M1:entry', created=now,
            status='delivered', payload={},
            delivery={'publication_input': pub_entry}))
        c.execute(db.insert(native_outbox).values(
            id='s2', program='smoothers', event_key='C1:target', created=now,
            status='delivered', payload={},
            delivery={'publication_input': pub_exit}))
        out = day_digest.assemble(db, c, now + 3600)
    sec = next(s for s in out['sections'] if s['key'] == 'smoothers')
    text = "\n".join(sec['lines'])
    assert 'META 745 CALL' in text and '744.17' in text
    assert 'COP 127 CALL' in text and 'Target hit' in text


def test_futures_opened_closed_and_pnl(db):
    now = _now_today()
    closed = {'id': 'ict-1', 'symbol': 'SIL.v.0', 'direction': 'long', 'qty': 1,
              'strategy': 'ict-aoi-zones', 'entry': 61.30, 'stop': 61.25,
              'target': 61.41, 'exit': 61.24, 'pnl': -63.0, 'exit_reason': 'stop',
              'entered_at': now, 'exited_at': now + 60}
    open_t = {'id': 'ict-2', 'symbol': 'MCL.v.0', 'direction': 'long', 'qty': 1,
              'strategy': 'ict-aoi-fade', 'entry': 50.0, 'stop': 49.9,
              'target': 50.2, 'entered_at': now + 120}
    with db.tx() as c:
        db.put(c, 'trade:ict-1', closed)
        db.put(c, 'trade:ict-2', open_t)
        out = day_digest.assemble(db, c, now + 3600)
    sec = next(s for s in out['sections'] if s['key'] == 'futures')
    text = "\n".join(sec['lines'])
    assert 'micro silver' in text and '-$63.00' in text and 'stopped out' in text
    assert 'micro crude' in text and 'entry 50' in text
    assert 'Realized today: -$63.00' in text


def test_flow_pulse_and_flash(db):
    now = _now_today()
    pulse = {'symbol': 'MRK', 'direction': 'bearish', 'directional_premium': 1700000,
             'suggested_contract': {'strike': 140, 'type': 'PUT', 'expiration': '2026-10-30'}}
    check = {'ticker': 'AAPL', 'direction': 'long', 'entry': 332.48, 'target': 335,
             'invalidation': 330, 'pattern': 'FLOOR BOUNCE #4', 'setup_score': 93,
             'evidence_score': 0.25, 'source': 'flash_agentic'}
    with db.tx() as c:
        db.append(c, 'flow_pulse', 'flow_pulse', 'MRK', now, pulse, key='fp1')
        db.append(c, 'pick_check', 'dashboard', 'AAPL', now, check, key='pc1')
        out = day_digest.assemble(db, c, now + 3600)
    fp = next(s for s in out['sections'] if s['key'] == 'flow_pulse')
    assert any('MRK BEARISH' in l and '$1.7M' in l for l in fp['lines'])
    fl = next(s for s in out['sections'] if s['key'] == 'flash_agentic')
    assert any('FLOOR BOUNCE #4 (1)' in l and 'AAPL' in l for l in fl['lines'])


def test_empty_day_has_no_lines(db):
    # A timestamp long ago with no seeded data: every section is empty.
    with db.tx() as c:
        out = day_digest.assemble(db, c, 1_000_000_000.0)
    assert out['date']
    assert all(s['lines'] == [] for s in out['sections'])
