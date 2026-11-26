"""Dual-ruler scoring for the 0DTE funnel (2026-11-25).

The tracked book (zero_dte_paper) scores scanner-funnel trades with a
generic bracket: entry at ask + 1c, exit at bid - 1c on a +/-50% cross
or the 2:55pm CT flatten. Each published setup ALSO carries its own
premium stop/target and exit deadline in the alert payload (the
setup-study trial payload's 'stop'/'target'/'flatten_at' premium
levels from simulation.bracket, or idea-style
'premium_stop'/'premium_target'/'exit_deadline' — the same precedence
alert_ownership.format_message renders with). Booking now captures
those levels and _update_open maintains a parallel shadow outcome
under the published ruler (pub_* keys), so signal quality and bracket
policy stop being confounded. The primary lifecycle is untouched and
the seat rule still reads the bracket ruler.
"""
import time

import pytest

from compass.store import Store
from compass import simple, zero_dte_paper
# Register every table simple.summary reads on the shared metadata
# before any Store initializes (same block as test_gap_fixes).
import compass.option_ideas  # noqa: F401
import compass.swing_ideas  # noqa: F401
import compass.spy_study  # noqa: F401
import compass.spy_timeframes  # noqa: F401
import compass.setup_study  # noqa: F401
import compass.smoothers_daily  # noqa: F401


@pytest.fixture
def db(tmp_path):
    db = Store('sqlite:///' + str(tmp_path / 'dual.db'))
    db.initialize()
    yield db
    db.engine.dispose()


OPT = "KO251125C00098000"
T0 = 1792000000.0


# ---------------------------------------------------------------- booking

def _seed_alert(db, c, now, key="evt1", **levels):
    db.put(c, "zero_dte_paper:scan_ts", now - 3600)
    payload = {
        "status": "option_setup_new",
        "publication": {
            "category": "options_0dte",
            "contract": {"symbol": OPT, "underlying": "KO",
                         "strike": 98, "type": "call",
                         "expiry": "2026-11-25"},
            "status": "option_setup_new",
        },
    }
    payload.update(levels)
    db.append(c, "alert", "setup_study", OPT, now - 5, payload, key=key)
    db.put(c, "quote:" + OPT, {"ts": now, "bid": 2.0, "ask": 2.1})


def test_booking_captures_trial_payload_levels(db):
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now, stop=1.50, target=3.10,
                    flatten_at=now + 7200)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 3600)
        rec = db.get(c, "trade:0dte-scan-evt1")
    assert opened == 1
    assert rec["pub_stop"] == 1.50
    assert rec["pub_target"] == 3.10
    assert rec["pub_deadline"] == now + 7200


def test_booking_captures_idea_style_levels_with_precedence(db):
    now = time.time()
    with db.tx() as c:
        # premium_* wins over stop/target, exit_deadline over flatten_at
        # (the precedence alert_ownership.format_message renders with).
        _seed_alert(db, c, now, premium_stop=1.44, premium_target=3.09,
                    exit_deadline=now + 5400,
                    stop=9.99, target=9.99, flatten_at=now + 7200)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 3600)
        rec = db.get(c, "trade:0dte-scan-evt1")
    assert opened == 1
    assert rec["pub_stop"] == 1.44
    assert rec["pub_target"] == 3.09
    assert rec["pub_deadline"] == now + 5400


def test_booking_without_levels_stays_bracket_only(db):
    now = time.time()
    with db.tx() as c:
        _seed_alert(db, c, now)
        opened = zero_dte_paper._open_scanner(db, c, now, now + 3600)
        rec = db.get(c, "trade:0dte-scan-evt1")
    assert opened == 1
    assert "pub_stop" not in rec
    assert "pub_target" not in rec
    assert "pub_deadline" not in rec


# ---------------------------------------------------------------- shadow exits

def _trade(**kw):
    base = {'id': 'trade:0dte-scan-t', 'kind': 'scanner', 'symbol': OPT,
            'underlying': 'KO', 'side': 'long', 'qty': 1, 'multiplier': 100,
            'entry': 1.00, 'entered_at': T0, 'status': 'open', 'fee': 0.65,
            'flatten_at': T0 + 3600,
            'pub_stop': 0.70, 'pub_target': 1.40, 'pub_deadline': T0 + 7200}
    base.update(kw)
    return base


def test_shadow_target_fires_while_bracket_trade_stays_open(db):
    # Published target 1.40 sits BELOW the bracket's +50% (1.50): the
    # shadow exits on the published ruler while the primary rides on.
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade())
        db.put(c, 'quote:' + OPT, {'ts': T0 + 600, 'bid': 1.42, 'ask': 1.45})
        zero_dte_paper._update_open(db, c, T0 + 600)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'open'
    assert t['pub_exit_reason'] == 'published target'
    assert t['pub_exit'] == pytest.approx(1.41)  # bid - 1c, same as primary
    assert t['pub_pnl'] == pytest.approx(39.70)  # (1.41-1.00)*100 - 2*0.65
    assert t['pub_data_gap'] is False


def test_shadow_stop_fires_while_bracket_trade_stays_open(db):
    # Published stop 0.70 sits ABOVE the bracket's -50% (0.50).
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade())
        db.put(c, 'quote:' + OPT, {'ts': T0 + 600, 'bid': 0.68, 'ask': 0.71})
        zero_dte_paper._update_open(db, c, T0 + 600)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'open'
    assert t['pub_exit_reason'] == 'published stop'
    assert t['pub_exit'] == pytest.approx(0.67)
    assert t['pub_pnl'] == pytest.approx(-34.30)


def test_shadow_deadline_fires_before_session_flatten(db):
    # Published deadline (T0+1800) precedes the bracket flatten (T0+3600).
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade(pub_deadline=T0 + 1800))
        db.put(c, 'quote:' + OPT, {'ts': T0 + 1800, 'bid': 1.10, 'ask': 1.13})
        zero_dte_paper._update_open(db, c, T0 + 1800)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'open'
    assert t['pub_exit_reason'] == 'published deadline'
    assert t['pub_exit'] == pytest.approx(1.09)
    assert t['pub_pnl'] == pytest.approx(7.70)


def test_shadow_outlives_primary_flatten_and_hits_target(db):
    # The KO shape: published deadline (T0+7200) falls AFTER the 2:55
    # flatten (T0+3600). The bracket trade flattens; the shadow keeps
    # tracking the same quote stream and exits on its own target.
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade())
        db.put(c, 'quote:' + OPT, {'ts': T0 + 1800, 'bid': 1.20, 'ask': 1.23})
        zero_dte_paper._update_open(db, c, T0 + 1800)
        db.put(c, 'quote:' + OPT, {'ts': T0 + 3600, 'bid': 1.20, 'ask': 1.23})
        zero_dte_paper._update_open(db, c, T0 + 3600)
        t = db.get(c, 'trade:0dte-scan-t')
        assert t['status'] == 'closed'
        assert t['exit_reason'] == 'session flatten'
        assert t['pnl'] == pytest.approx(17.70)
        assert 'pub_exit_reason' not in t  # shadow still pending
        db.put(c, 'quote:' + OPT, {'ts': T0 + 5400, 'bid': 1.65, 'ask': 1.68})
        zero_dte_paper._update_open(db, c, T0 + 5400)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['pub_exit_reason'] == 'published target'
    assert t['pub_exit'] == pytest.approx(1.64)
    assert t['pub_pnl'] == pytest.approx(62.70)
    assert t['pub_data_gap'] is False
    # Primary outcome untouched by the shadow's later resolution.
    assert t['exit'] == pytest.approx(1.19)
    assert t['pnl'] == pytest.approx(17.70)


def test_shadow_deadline_after_last_quote_prices_at_last_bid(db):
    # Published deadline passes with no fresh quote: price at the last
    # observed bid (the primary's gap convention) and flag the blind mark.
    with db.tx() as c:
        db.put(c, 'trade:0dte-scan-t', _trade())
        db.put(c, 'quote:' + OPT, {'ts': T0 + 1800, 'bid': 1.20, 'ask': 1.23})
        zero_dte_paper._update_open(db, c, T0 + 1800)
        db.put(c, 'quote:' + OPT, {'ts': T0 + 3600, 'bid': 1.20, 'ask': 1.23})
        zero_dte_paper._update_open(db, c, T0 + 3600)
        t = db.get(c, 'trade:0dte-scan-t')
        assert t['status'] == 'closed' and 'pub_exit_reason' not in t
        # Quote row never advances again; the deadline passes blind.
        zero_dte_paper._update_open(db, c, T0 + 7200)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['pub_exit_reason'] == 'published deadline'
    assert t['pub_exit'] == pytest.approx(1.19)  # last bid 1.20 - 1c
    assert t['pub_pnl'] == pytest.approx(17.70)
    assert t['pub_data_gap'] is True


def test_trade_without_levels_closes_with_no_shadow_outcome(db):
    with db.tx() as c:
        t0 = _trade()
        for k in ('pub_stop', 'pub_target', 'pub_deadline'):
            t0.pop(k)
        db.put(c, 'trade:0dte-scan-t', t0)
        db.put(c, 'quote:' + OPT, {'ts': T0 + 3600, 'bid': 1.20, 'ask': 1.23})
        zero_dte_paper._update_open(db, c, T0 + 3600)
        t = db.get(c, 'trade:0dte-scan-t')
    assert t['status'] == 'closed'
    assert 'pub_exit_reason' not in t
    assert 'pub_pnl' not in t


def test_spy_plan_trade_never_gets_shadow_outcome(db):
    # Scope limit: SPY morning-plan trades are not funnel trades, even
    # if a record somehow carries published levels.
    with db.tx() as c:
        t0 = _trade(kind='spy_plan', target_spy=101.0, stop_spy=99.0,
                    decision='CALL SETUP CONFIRMED')
        db.put(c, 'trade:0dte-spy-t', t0)
        db.put(c, 'quote:' + OPT, {'ts': T0 + 3600, 'bid': 1.65, 'ask': 1.68})
        zero_dte_paper._update_open(db, c, T0 + 3600)
        t = db.get(c, 'trade:0dte-spy-t')
    assert t['status'] == 'closed'
    assert 'pub_exit_reason' not in t
    assert 'pub_pnl' not in t


# ---------------------------------------------------------------- aggregation

def _closed(**kw):
    base = {'kind': 'scanner', 'underlying': 'KO', 'symbol': OPT,
            'status': 'closed', 'entry': 1.00, 'data_gap': False,
            'evidence': {'version': '0dte-evidence-v1'},
            'entered_at': 0, 'exited_at': 0}
    base.update(kw)
    return base


def test_simple_0dte_dual_ruler_block(db):
    now = time.time()
    with db.tx() as c:
        # A: both rulers win, published ruler wins bigger.
        db.put(c, 'trade:0dte-scan-a', _closed(
            entry=1.00, exit=1.41, pnl=39.70,
            entered_at=now - 7200, exited_at=now - 3600,
            pub_stop=0.70, pub_target=1.40, pub_exit=1.64,
            pub_exit_reason='published target', pub_pnl=62.70,
            pub_data_gap=False))
        # B: both rulers lose.
        db.put(c, 'trade:0dte-scan-b', _closed(
            entry=1.00, exit=0.66, pnl=-35.30,
            entered_at=now - 7200, exited_at=now - 3500,
            pub_stop=0.70, pub_target=1.40, pub_exit=0.67,
            pub_exit_reason='published stop', pub_pnl=-34.30,
            pub_data_gap=False))
        # C: no published levels — outside the dual-ruler population.
        db.put(c, 'trade:0dte-scan-c', _closed(
            entry=1.00, exit=1.11, pnl=10.00,
            entered_at=now - 7200, exited_at=now - 3400))
        # D: levels captured, shadow still pending — bracket arm only.
        db.put(c, 'trade:0dte-scan-d', _closed(
            entry=1.00, exit=1.06, pnl=5.00,
            entered_at=now - 7200, exited_at=now - 3300,
            pub_stop=0.70, pub_target=1.40))
        out = simple.summary(db, c, now)
    dual = out['types']['0dte']['stats']['dual_ruler']
    b = dual['bracket']
    assert b['tracked'] == 3 and b['wins'] == 2
    assert b['win_rate'] == pytest.approx(66.7)
    assert b['pnl'] == pytest.approx(9.40)
    assert b['pf'] == pytest.approx(1.266, abs=1e-3)
    p = dual['published']
    assert p['tracked'] == 2 and p['wins'] == 1
    assert p['win_rate'] == pytest.approx(50.0)
    assert p['pnl'] == pytest.approx(28.40)
    assert p['pf'] == pytest.approx(1.828, abs=1e-3)
    # Existing headline values are untouched by the new block: the
    # scorecard still counts all four measured scanner trades.
    assert out['types']['0dte']['stats']['tracked'] == 4
