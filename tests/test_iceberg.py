"""Shadow iceberg detector: synthetic print-stream tests.

The detector reconstructs institutional options accumulation from the
OPRA print stream (compass/iceberg.py). These tests pin the contract:
a clustered ask-side accumulation fires exactly once per episode,
dispersed/one-sided/conceding flow does not fire, a detector fault can
never tear the option stream down (2026-10-21 lesson), cross-source
echo prints are counted once, and an outcome that cannot be priced
from a fresh quote scores data_gap — never a loss.
"""
from datetime import datetime
from types import SimpleNamespace

from compass.iceberg import (IcebergDetector, parse_occ, promotion_view,
                             score_pass, status, summarize)
from compass.market import NY, session
from compass.option_stream import OptionBuffer
from compass.store import Store

SYM = 'O:MU260116C00200000'          # MU 2026-01-16 200 call
T0 = datetime(2026, 10, 26, 10, 0, tzinfo=NY).timestamp()  # Mon 10:00 ET


def cfg(**over):
    base = dict(iceberg_detector=True, iceberg_min_contracts_30m=500,
                iceberg_min_premium_30m=25000.0, iceberg_min_prints_30m=25,
                iceberg_min_ask_share=0.65, iceberg_min_active_subwindows=3,
                iceberg_vol_oi_ratio=0.5, iceberg_no_concession_tol=0.05,
                iceberg_cooldown_min=30.0, iceberg_max_tracked=5000)
    base.update(over)
    return SimpleNamespace(**base)


def feed(det, start=T0, minutes=26, per_min=2, size=12, price=2.05,
         bid=1.95, ask=2.05, sym=SYM, src='massive'):
    """Steady prints at the ask across many minutes (an iceberg being
    worked). Alternates exchanges so the burst signature is present."""
    exchanges = (1, 17, 8)
    step = max(1, 60 // per_min)
    for m in range(minutes):
        for j in range(per_min):
            t = start + m * 60 + j * step
            det.observe_quote(sym, t, bid, ask)
            det.observe_trade(sym, t, {'p': price, 's': size,
                                       'x': exchanges[(m + j) % 3],
                                       'src': src})


def test_parse_occ():
    assert parse_occ(SYM) == {'underlying': 'MU', 'expiry': '2026-01-16',
                              'type': 'call', 'strike': 200.0}
    assert parse_occ('O:SPY251003P00745000')['type'] == 'put'
    assert parse_occ('MU') is None
    assert parse_occ('O:BAD') is None


def test_clustered_ask_side_accumulation_fires_once_per_episode():
    det = IcebergDetector(cfg())
    det.refresh_oi({SYM: 800})
    feed(det)
    alerts = det.drain_alerts()
    assert len(alerts) == 1
    a = alerts[0]
    assert a['underlying'] == 'MU' and a['type'] == 'call'
    assert a['strike'] == 200.0 and a['side'] == 'bullish'
    assert a['est_parent_contracts'] >= 500
    assert a['est_parent_premium'] >= 25000
    assert a['ask_share'] >= 0.99
    assert a['window']['oi'] == 800 and a['window']['vol_oi'] >= 0.5
    assert det.alerts_fired == 1 and det.errors == 0
    # Continued printing inside the cooldown does NOT re-fire.
    feed(det, start=T0 + 27 * 60, minutes=5)
    assert det.drain_alerts() == []
    # A fresh episode after the cooldown fires again, exactly once.
    feed(det, start=T0 + 70 * 60)
    assert len(det.drain_alerts()) == 1
    assert det.alerts_fired == 2


def test_bid_side_flow_does_not_fire():
    det = IcebergDetector(cfg())
    det.refresh_oi({SYM: 800})
    feed(det, price=1.95)            # every print at the bid: distribution
    assert det.drain_alerts() == []
    assert det.prints_seen > 0       # seen, classified, not fired


def test_thin_flow_does_not_fire():
    det = IcebergDetector(cfg())
    det.refresh_oi({SYM: 800})
    feed(det, size=2)                # 104 contracts total: retail size
    assert det.drain_alerts() == []


def test_single_burst_without_persistence_does_not_fire():
    det = IcebergDetector(cfg())
    det.refresh_oi({SYM: 100})
    # 600 contracts inside two minutes clears size/premium/prints and
    # vol-vs-OI, but accumulation confined to one sub-window is a
    # one-off block print, not a worked parent order.
    feed(det, minutes=2, per_min=25, size=12)
    assert det.drain_alerts() == []


def test_conceding_premium_does_not_fire():
    det = IcebergDetector(cfg())
    det.refresh_oi({SYM: 800})
    # Price bleeds 2.05 -> 1.80 across the window while size builds:
    # late VWAP concedes >5% vs early VWAP, so the "buyer" is being
    # filled cheaper — not institutional urgency.
    for m in range(26):
        p = 2.05 - m * 0.01
        for j in range(2):
            t = T0 + m * 60 + j * 20
            det.observe_quote(SYM, t, p - 0.10, p)
            det.observe_trade(SYM, t, {'p': p, 's': 12, 'x': 1,
                                       'src': 'massive'})
    assert det.drain_alerts() == []


def test_kill_switch_disables_detector():
    det = IcebergDetector(cfg(iceberg_detector=False))
    feed(det)
    assert det.drain_alerts() == []
    assert det.prints_seen == 0 and det.contracts == {}


def test_kill_switch_reads_the_real_config_field():
    # Task 003 item 6: the Config field is `iceberg_detector`
    # (config.py). A cfg carrying ONLY that field (the production
    # shape) must disable the detector; before the fix, __init__ read
    # the nonexistent `iceberg_enabled` and enabled was always True.
    det = IcebergDetector(SimpleNamespace(iceberg_detector=False))
    assert det.enabled is False
    det.observe_quote(SYM, T0, 1.95, 2.05)
    det.observe_trade(SYM, T0, {'p': 2.05, 's': 10, 'x': 1,
                                'src': 'massive'})
    assert det.prints_seen == 0 and det.quotes_seen == 0
    assert det.contracts == {} and det.drain_alerts() == []
    # And the real Config default keeps the detector ON (production
    # behavior unchanged: ICEBERG_DETECTOR_ENABLED defaults true).
    from compass.config import Config
    assert IcebergDetector(Config()).enabled is True


def test_kill_switch_legacy_field_name_still_works():
    # Back-compat fallback: callers passing iceberg_enabled (the old
    # misread name) are still honored when iceberg_detector is absent.
    det = IcebergDetector(SimpleNamespace(iceberg_enabled=False))
    assert det.enabled is False


def test_pending_alerts_buffer_is_bounded():
    # Task 003 item 6: pending_alerts used to be a bare deque() — a
    # stalled drain grew it without limit. It must cap at
    # PENDING_ALERTS_MAXLEN, dropping the oldest undrained alerts.
    from compass.iceberg import PENDING_ALERTS_MAXLEN
    det = IcebergDetector(cfg())
    assert det.pending_alerts.maxlen == PENDING_ALERTS_MAXLEN
    for i in range(PENDING_ALERTS_MAXLEN + 50):
        det.pending_alerts.append({'id': 'a%d' % i})
    assert len(det.pending_alerts) == PENDING_ALERTS_MAXLEN
    assert det.pending_alerts[0]['id'] == 'a50'


def test_cross_source_echo_print_counted_once():
    det = IcebergDetector(cfg())
    t = T0
    det.observe_quote(SYM, t, 1.95, 2.05)
    item = {'p': 2.05, 's': 10, 'x': 1, 'src': 'massive'}
    det.observe_trade(SYM, t, item)
    det.observe_trade(SYM, t, {**item, 'src': 'alpaca'})   # same print
    assert det.dup_prints == 1
    assert det.contracts[SYM].day_contracts == 10
    # A genuinely different print at the same second still counts.
    det.observe_trade(SYM, t, {**item, 's': 7})
    assert det.contracts[SYM].day_contracts == 17


class _Boom:
    """Observer that always raises — the stream must not notice."""

    def __init__(self):
        self.errors = 0

    def observe_quote(self, *a):
        raise RuntimeError('boom')

    def observe_trade(self, *a):
        raise RuntimeError('boom')

    def note_error(self):
        self.errors += 1


def test_observer_fault_never_reaches_the_stream():
    buf = OptionBuffer()
    boom = _Boom()
    buf.trade_observer = boom
    buf.offer(dict(ev='Q', sym=SYM, t=int(T0 * 1000), bp=1.95, ap=2.05,
                   bs=10, **{'as': 10}), {SYM}, T0, 0.0)
    buf.offer(dict(ev='T', sym=SYM, t=int(T0 * 1000), p=2.05, s=10),
              {SYM}, T0, 0.0)
    assert len(buf.trades) == 1          # print still queued for writers
    assert buf.observer_errors == 2      # both taps swallowed + counted
    assert boom.errors == 2


def test_detector_internal_fault_is_swallowed():
    det = IcebergDetector(cfg())

    def broken(state, now):
        raise RuntimeError('detector bug')

    det._evaluate = broken
    det.refresh_oi({SYM: 800})
    feed(det, minutes=3)
    assert det.prints_seen > 0
    assert det.errors > 0
    assert det.drain_alerts() == []


def _alert_row(**over):
    row = {'id': 'test-alert', 'kind': 'iceberg_alert', 'symbol': SYM,
           'underlying': 'MU', 'expiry': '2026-10-30', 'strike': 200.0,
           'type': 'call', 'side': 'bullish', 'alert_ts': T0,
           'day': '2026-10-26', 'week': '2026-W44',
           'est_parent_contracts': 600, 'est_parent_premium': 123000.0,
           'prints': 50, 'ask_share': 0.9, 'score': 0.8, 'reasons': [],
           'mark': 2.05, 'mid': 2.0, 'underlying_at_alert': None,
           'window': {}, 'scores': {}, 'mfe_pct': None, 'mae_pct': None,
           'und_mfe_pct': None, 'und_mae_pct': None, 'status': 'open'}
    row.update(over)
    return row


def _store(tmp_path):
    s = Store('sqlite:///' + str(tmp_path / 'iceberg.db'))
    s.initialize()
    return s


def test_unquotable_outcome_scores_data_gap_not_loss(tmp_path):
    db = _store(tmp_path)
    with db.tx() as c:
        db.put(c, 'iceberg_alert:test-alert', _alert_row())
        score_pass(db, c, cfg(), T0 + 3 * 3600)
        a = db.get(c, 'iceberg_alert:test-alert')
        summ = summarize(db, c, T0 + 3 * 3600)
    # +1h checkpoint is past its grace with no quote anywhere: data_gap,
    # and crucially NOT a scored loss.
    assert a['scores']['h1']['status'] == 'data_gap'
    assert a['scores']['h1']['reason'] == 'no_quote'
    assert a['status'] == 'open'          # close checkpoints not yet due
    assert summ['horizons']['h1']['data_gap'] == 1
    assert summ['horizons']['h1']['scored'] == 0


def test_outcome_scoring_end_to_end(tmp_path):
    db = _store(tmp_path)
    close = session('2026-10-26')[1]
    with db.tx() as c:
        db.put(c, 'iceberg_alert:test-alert', _alert_row())
        # Underlying bar just before the alert -> underlying_at_alert.
        db.put(c, 'bar_window:MU',
               [[T0 - 60, 209.0, 211.0, 208.0, 210.0, 1000, 210.0]])
    with db.tx() as c:
        out = score_pass(db, c, cfg(), T0 + 3 * 3600)
    assert out['alerts'] == 1
    with db.tx() as c:
        a = db.get(c, 'iceberg_alert:test-alert')
    assert a['underlying_at_alert'] == 210.0
    assert a['scores']['h1']['status'] == 'data_gap'   # never quoted
    assert 'close' not in a['scores']                  # not yet due
    # Fresh quotes land near the close: contract doubled, underlying +4.8%.
    with db.tx() as c:
        db.put(c, 'quote:' + SYM, {'ts': close - 60, 'bid': 3.9, 'ask': 4.1,
                                   'bid_size': 5, 'ask_size': 5})
        db.put(c, 'quote:MU', {'ts': close - 60, 'bid': 219.9, 'ask': 220.1})
        out = score_pass(db, c, cfg(), close + 120)
    with db.tx() as c:
        a = db.get(c, 'iceberg_alert:test-alert')
        summ = summarize(db, c, close + 120)
        st = status(db, c, close + 120)
    s = a['scores']['close']
    assert s['status'] == 'scored' and s['contract_ret_pct'] == 100.0
    assert s['underlying_ret_pct'] == 4.76
    assert summ['alerts'] == 1
    assert summ['horizons']['close']['scored'] == 1
    assert summ['horizons']['close']['mean_contract_ret_pct'] == 100.0
    assert summ['horizons']['h1']['data_gap'] == 1
    assert st['alerts_total'] == 1 and st['alerts_open'] == 1


def test_expiry_scores_from_intrinsic_value(tmp_path):
    db = _store(tmp_path)
    expiry_close = session('2026-10-30')[1]
    with db.tx() as c:
        db.put(c, 'iceberg_alert:test-alert', _alert_row())
        # MU closes expiry day at 230: the 200 call is worth 30.00.
        db.put(c, 'bar_window:MU',
               [[expiry_close - 60, 229.0, 231.0, 228.0, 230.0, 5000, 230.0]])
        out = score_pass(db, c, cfg(), expiry_close + 7200)
    with db.tx() as c:
        a = db.get(c, 'iceberg_alert:test-alert')
    s = a['scores']['expiry']
    assert s['status'] == 'scored' and s['basis'] == 'intrinsic'
    assert s['value'] == 30.0 and s['contract_ret_pct'] == 1400.0
    assert a['status'] == 'complete'      # h1/close/next_close gapped, expiry scored


def test_promotion_view():
    def mk(day_iso, ret):
        return {'day': day_iso, 'alert_ts': T0,
                'scores': {'close': {'status': 'scored',
                                     'contract_ret_pct': ret}}}
    # Two consecutive positive 4-week blocks, but only 8 scored: the
    # direction is right, the >=40 bar is not met.
    rows = [mk('2026-09-07', 10.0) for _ in range(4)] + \
           [mk('2026-10-05', 20.0) for _ in range(4)]
    view = promotion_view(rows)
    assert len(view['windows']) == 2
    assert view['two_consecutive_positive'] is True
    assert view['meets_bar'] is False and view['scored_total'] == 8
    # 22 + 22 scored across the same two blocks clears the pair bar.
    rows = [mk('2026-09-07', 10.0) for _ in range(22)] + \
           [mk('2026-10-05', 20.0) for _ in range(22)]
    view = promotion_view(rows)
    assert view['meets_bar'] is True
    assert view['meets_bar_each_window'] is False
    # A negative second block breaks the streak.
    rows = [mk('2026-09-07', 10.0) for _ in range(22)] + \
           [mk('2026-10-05', -5.0) for _ in range(22)]
    assert promotion_view(rows)['meets_bar'] is False
    assert promotion_view([])['meets_bar'] is False


def test_api_endpoints_are_public_and_answer(tmp_path):
    from starlette.testclient import TestClient
    from compass.app import create_app
    from compass.config import Config
    url = 'sqlite:///' + str(tmp_path / 'api.db')
    cfg = Config(local=True, db=url, password='test-password-12345678')
    db = Store(url)
    db.initialize()
    with db.tx() as c:
        db.put(c, 'iceberg:diag', {'prints_seen': 1234, 'alerts_fired': 1,
                                   'contracts_tracked': 40,
                                   'last_alert_at': T0})
        db.put(c, 'iceberg_alert:test-alert', _alert_row())
    client = TestClient(create_app(cfg), raise_server_exceptions=False)
    r = client.get('/api/iceberg-status')
    assert r.status_code == 200 and r.json()['ok'] is True
    assert r.json()['diag']['prints_seen'] == 1234
    assert r.json()['alerts_total'] == 1
    r = client.get('/api/iceberg-scorecard')
    assert r.status_code == 200 and r.json()['ok'] is True
    body = r.json()
    assert body['alerts'] == 1
    assert 'promotion' in body and 'horizons' in body
    assert body['by_underlying']['MU']['alerts'] == 1
