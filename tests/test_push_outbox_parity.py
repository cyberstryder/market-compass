"""Bundle 3 parity gate (2026-10-30): legacy per-channel outbox tables
vs the unified push_outbox_v1, behind Config.push_outbox_unified.

For every channel: (1) the module's own producer, flag off vs flag on,
must write the identical row (modulo the channel tag); (2) deliver_one
against the legacy table vs the unified table must make the identical
Discord call and land the identical (status, delivery) across the
response matrix — receipt, no-receipt, 429, 5xx, 4xx, network error.
ICT runs the matrix for entry AND exit payloads (its formatter
switches on payload type). Any channel failing parity blocks the flip;
the code ships dark regardless (flag default off).
"""
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from compass import (day_trading_board, flow_pulse, gap_continuation,
                     ict_push, magnet_push)
from compass.store import Store, push_outbox

URL = 'https://discord.com/api/webhooks/123456789/abcdef'
NOW = 1793300000.0

MODULES = {'magnet': magnet_push, 'ict': ict_push, 'ict_exit': ict_push,
           'flow_pulse': flow_pulse, 'gap': gap_continuation,
           'day_board': day_trading_board}
CHANNEL_OF = {'ict_exit': 'ict'}


def make_db(tmp_path, name):
    s = Store('sqlite:///' + str(tmp_path / name))
    s.initialize()
    return s


def cfg(unified):
    return SimpleNamespace(
        push_outbox_unified=unified,
        magnet_push_enabled=True, magnet_push_signals=('broke_through',),
        ict_push_enabled=True, ict_push_webhook=URL,
        flow_pulse_push_enabled=True, flow_pulse_webhook=URL,
        gap_push_enabled=True, day_board_push_enabled=True,
        discord_routes={'intraday': URL, 'spy_morning': URL,
                        'magnets': URL})


def seed(variant, db, unified, monkeypatch=None):
    """Write one pending row via the channel's own producer."""
    c = cfg(unified)
    with db.tx() as conn:
        if variant == 'magnet':
            row = {'signal': 'broke_through', 'magnet': 100.0, 'spot': 101.0,
                   'distance_pct': 0.01, 'role': 'resistance',
                   'vs_flip': None, 'gamma_flip': None, 'magnet_score': 5,
                   'tests': 3}
            assert magnet_push.maybe_queue(
                db, conn, c, 'SPY', row, 'in_radius', '2026-10-30', NOW)
        elif variant in ('ict', 'ict_exit'):
            monkeypatch.setattr(ict_push, 'time',
                                SimpleNamespace(time=lambda: NOW))
            if variant == 'ict':
                trade = {'id': 'ict-1', 'detector': 'aoi_fade',
                         'symbol': 'MES.c.0', 'side': 'long', 'entry': 100.0,
                         'stop': 99.0, 'target': 102.0, 'qty': 1,
                         'entered_at': NOW}
                assert ict_push.maybe_queue(db, conn, c, trade)
            else:
                trade = {'id': 'ict-1', 'detector': 'aoi_fade',
                         'symbol': 'MES.c.0', 'side': 'long', 'entry': 100.0,
                         'exit': 102.0, 'pnl': 200.0,
                         'exit_reason': 'target_hit', 'exited_at': NOW}
                assert ict_push.maybe_queue_exit(db, conn, c, trade)
        elif variant == 'flow_pulse':
            pulse = {'symbol': 'SPY', 'direction': 'bullish',
                     'directional_premium': 2e6, 'opposing_premium': 1e5,
                     'print_count': 5, 'max_score': 95, 'top_prints': [],
                     'suggested_contract': None,
                     'window_start': NOW - 1800, 'window_end': NOW}
            assert flow_pulse._maybe_queue_push(
                db, conn, c, pulse, '2026-10-30', NOW)
        elif variant == 'gap':
            state = {'break_direction': 'up', 'gap_pct': 0.02,
                     'break_level': 100.0,
                     'pillars': {'price': {'status': 'supportive'}}}
            assert gap_continuation._maybe_queue_push(
                db, conn, c, 'SPY', '2026-10-30', NOW, state)
        elif variant == 'day_board':
            board = {'rows': [{'symbol': 'SPY', 'total': 6,
                               'day_pct': 0.012,
                               'components': {'flow': {'score': 1}}}]}
            assert day_trading_board._maybe_queue_push(
                db, conn, c, '2026-10-30', NOW, board, 'build')


def only_row(db, table):
    with db.tx() as conn:
        rows = [dict(r) for r in conn.execute(select(table)).mappings()]
    assert len(rows) == 1
    return rows[0]


class FakeClient:
    def __init__(self, script):
        self.script = script
        self.calls = []

    def post(self, url, **kw):
        self.calls.append((url, kw))
        if isinstance(self.script, Exception):
            raise self.script
        return self.script


SCENARIOS = {
    'receipt': lambda: httpx.Response(200, json={'id': '123456'}),
    'no_receipt': lambda: httpx.Response(200, json={}),
    'rate_limited': lambda: httpx.Response(
        429, json={'retry_after': 5}, headers={'retry-after': '5'}),
    'server_error': lambda: httpx.Response(500, json={}),
    'client_error': lambda: httpx.Response(400, json={}),
    'network': lambda: httpx.ConnectError(
        'down', request=httpx.Request('POST', URL)),
}

VARIANTS = ['magnet', 'ict', 'ict_exit', 'flow_pulse', 'gap', 'day_board']


@pytest.mark.parametrize('variant', VARIANTS)
def test_producer_parity(tmp_path, monkeypatch, variant):
    legacy = make_db(tmp_path, 'legacy.db')
    unified = make_db(tmp_path, 'unified.db')
    seed(variant, legacy, False, monkeypatch)
    seed(variant, unified, True, monkeypatch)
    mod = MODULES[variant]
    a = only_row(legacy, mod.outbox if hasattr(mod, 'outbox') else mod._outbox)
    b = only_row(unified, push_outbox)
    assert b.pop('channel') == CHANNEL_OF.get(variant, variant)
    assert a == b


@pytest.mark.parametrize('variant', VARIANTS)
@pytest.mark.parametrize('scenario', sorted(SCENARIOS))
def test_deliver_parity(tmp_path, monkeypatch, variant, scenario):
    legacy = make_db(tmp_path, 'legacy.db')
    unified = make_db(tmp_path, 'unified.db')
    seed(variant, legacy, False, monkeypatch)
    seed(variant, unified, True, monkeypatch)
    mod = MODULES[variant]
    table = mod.outbox if hasattr(mod, 'outbox') else mod._outbox
    fc_a, fc_b = FakeClient(SCENARIOS[scenario]()), FakeClient(SCENARIOS[scenario]())
    got_a = mod.deliver_one(legacy, fc_a, URL, now=NOW)
    got_b = mod.deliver_one(unified, fc_b, URL, now=NOW,
                            table=push_outbox,
                            channel=CHANNEL_OF.get(variant, variant))
    assert got_a == got_b
    assert fc_a.calls == fc_b.calls
    a = only_row(legacy, table)
    b = only_row(unified, push_outbox)
    assert (a['status'], a['delivery']) == (b['status'], b['delivery'])


def test_channel_isolation(tmp_path, monkeypatch):
    db = make_db(tmp_path, 'iso.db')
    seed('magnet', db, True, monkeypatch)
    seed('ict', db, True, monkeypatch)
    fc = FakeClient(httpx.Response(200, json={'id': '999'}))
    assert ict_push.deliver_one(db, fc, URL, now=NOW,
                                table=push_outbox, channel='ict')
    with db.tx() as conn:
        rows = {r['channel']: dict(r) for r in
                conn.execute(select(push_outbox)).mappings()}
    assert rows['ict']['status'] == 'delivered'
    assert rows['magnet']['status'] == 'pending'


def test_flag_defaults_off(monkeypatch):
    from compass.config import Config
    monkeypatch.delenv('PUSH_OUTBOX_UNIFIED', raising=False)
    factory = Config.__dataclass_fields__['push_outbox_unified'].default_factory
    assert factory() is False
    monkeypatch.setenv('PUSH_OUTBOX_UNIFIED', 'true')
    assert factory() is True
