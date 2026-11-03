"""Swings rebuild (2026-11-02, Josh): Workstream A decoupling regression +
Workstream B C2 forward premium scorer.

Workstream A: daily-bar collection for the watchlist must run with
SWING_IDEAS_ENABLED off (the retired surface), while swing-idea
generation stays off. Before the fix, secondary_data.coverage() only
admitted watch symbols to the coverage universe (and tagged them with
the daily-collecting "swing" project) when cfg.swing_ideas was true, so
watch-only names silently stopped receiving daily bars — starving
odte_book's trailing range and the C2 scorer's closes.
"""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

import compass.app  # noqa: F401  (registers every table in store meta, as other suites rely on)
from compass.config import Config
from compass.secondary_data import coverage, plan
from compass.store import Store
from compass.swing_ideas import SwingIdeas, swings

NOW = datetime(2026, 9, 14, 15, 0, tzinfo=timezone.utc).timestamp()


@pytest.fixture
def db(tmp_path):
    store = Store('sqlite:///' + str(tmp_path / 'swings_rebuild.db'))
    store.initialize()
    yield store
    store.engine.dispose()


def retired_cfg(**kw):
    # Production posture since 2026-11-01: swing ideas retired (flag
    # off). POOL is a watch-only name: on the watchlist, referenced by
    # no connected project, not a discovery seed, not a sector ETF.
    base = dict(local=True, stocks=('SPY', 'POOL'), watchlist=(),
                sector_etfs=(), swing_ideas=False, morning_enabled=False,
                discovery=False)
    base.update(kw)
    return Config(**base)


def test_coverage_covers_watch_only_names_with_flag_off(db):
    cfg = retired_cfg()
    assert not cfg.swing_ideas
    assert 'POOL' in cfg.watch_symbols
    with db.tx() as c:
        rows = {r['symbol']: r for r in coverage(db, c, cfg, NOW)['rows']}
    assert 'POOL' in rows, 'watch-only name missing from coverage universe'
    row = rows['POOL']
    assert 'swing' in row['projects']  # daily-collection project tag
    assert row['collection_enabled']
    assert not row['daily_ready']  # no bars seeded: collection is owed


def test_plan_schedules_daily_collection_for_watch_only_names_flag_off(db):
    cfg = retired_cfg()
    jobs = plan(SimpleNamespace(db=db, cfg=cfg), NOW)
    assert 'POOL' in jobs['daily']


def test_no_swing_ideas_generated_with_flag_off(db):
    cfg = retired_cfg()
    worker = SwingIdeas(db, cfg)
    with db.tx() as c:
        worker.tick(c, NOW)
        assert c.execute(select(func.count()).select_from(swings)).scalar_one() == 0
        assert not db.recent(c, 'swing_candidate')
        state = db.get(c, 'swing_ideas:worker', {})
        assert state.get('enabled') is False


# ---------------------------------------------------------------------------
# Workstream B — C2 forward premium scorer

from datetime import date, timedelta

from compass.active_observations import inventory
from compass.market import session
from compass.swings_c2 import (SwingsC2, basket_for, episodes as c2_episodes,
                               exit_quote, rank_universe, select_contract,
                               wilder_atr)
from compass.swing_signals import sessions_between

SESSIONS = list(sessions_between('2026-05-01', '2026-09-21'))
REB1 = '2026-09-11'   # last session of its ISO week
REB2 = '2026-09-18'   # next week's last session
R1 = SESSIONS.index(REB1)

BASELINES = ['B%02d' % j for j in range(15)]
SYMBOLS = ['WIN1', 'WIN2', 'MISS', 'FEW'] + BASELINES


def closes_for(sym):
    """Deterministic close path per symbol over SESSIONS."""
    out = {}
    for i, d in enumerate(SESSIONS):
        if sym == 'WIN1':
            c = 100.0 if i <= R1 - 65 else 100.0 + min(i - (R1 - 65), 60) * 0.8 \
                + max(0, i - (R1 - 5)) * 0.05
        elif sym == 'WIN2':
            c = 100.0 if i <= R1 - 65 else 100.0 + min(i - (R1 - 65), 60) * 0.5 \
                + max(0, i - (R1 - 5)) * 0.05
        elif sym == 'FEW':
            c = 100.0 + 0.02 * i
        elif sym.startswith('B'):
            c = 100.0 + (0.010 + 0.001 * int(sym[1:])) * i
        else:  # MISS and fallback
            c = 100.0 + 0.02 * i
        out[d] = c
    return out


def seed_bars(db, through, skip=(), overrides=None):
    """Append daily bars for all symbols for sessions <= through.
    skip: {(symbol, date)} bars to omit. overrides: {(symbol, date):
    dict(o,h,l,c)} replacement bars."""
    overrides = overrides or {}
    with db.tx() as c:
        for sym in SYMBOLS:
            closes = closes_for(sym)
            prev = None
            for d in SESSIONS:
                if d > through:
                    break
                if sym == 'FEW' and SESSIONS.index(d) < len(SESSIONS) - 40:
                    prev = closes[d]
                    continue
                if (sym, d) in skip:
                    prev = closes[d]
                    continue
                close = closes[d]
                bar = dict(o=prev if prev is not None else close,
                           h=close * 1.01, l=close * 0.99, c=close, v=10000)
                if (sym, d) in overrides:
                    bar = {**bar, **overrides[(sym, d)]}
                db.append(c, 'daily', 'test', sym, session(d)[0], bar,
                          'day:' + sym + d)
                prev = close


def contract(symbol, expiry, strike, typ, delta, bid, ask):
    return {'symbol': symbol, 'expiry': expiry, 'strike': strike, 'type': typ,
            'oi': 500, 'delta': delta, 'gamma': 0.05, 'theta': -0.03,
            'vega': 0.1, 'iv': 0.3, 'volume': 200,
            'quote': {'ts': NOW, 'bid': bid, 'ask': ask,
                      'bid_size': 5, 'ask_size': 5}}


def seed_snapshot(db, sym, date_str, contracts, underlying_close):
    payload = {'date': date_str, 'symbol': sym, 'source': 'test',
               'complete': True, 'asof': NOW,
               'underlying_close': underlying_close, 'contracts': contracts}
    with db.tx() as c:
        db.append(c, 'chain_eod', 'test', sym, NOW, payload,
                  'chain_eod:' + date_str + ':' + sym)


def good_snapshot(db, sym, date_str, bid=2.00, ask=2.10):
    close = closes_for(sym)[date_str]
    strike = round(close)
    seed_snapshot(db, sym, date_str, [
        contract(sym + 'C1', '2026-10-09', strike, 'call', 0.50, bid, ask),
        contract(sym + 'P1', '2026-10-09', strike, 'put', -0.50, 1.0, 1.1),
        contract(sym + 'C2', '2026-10-09', strike, 'call', 0.90, 5.0, 5.1),
        contract(sym + 'C3', '2026-09-21', strike, 'call', 0.50, 1.0, 1.1),
    ], close)
    return sym + 'C1'


def c2_cfg():
    return retired_cfg(stocks=tuple(SYMBOLS))


def get_episodes(db):
    with db.tx() as c:
        return {p['symbol']: p for p in
                c.execute(select(c2_episodes.c.payload)).scalars().all()}


def at(d, hour=15):
    return datetime.fromisoformat(d).replace(hour=hour, tzinfo=timezone.utc).timestamp()


def test_c2_pure_ranking_and_selection():
    sessions = ['2026-01-%02d' % d for d in range(1, 29)] + \
               ['2026-02-%02d' % d for d in range(1, 29)] + \
               ['2026-03-%02d' % d for d in range(1, 16)]
    ci = len(sessions) - 1
    rows = {s: [(d, 100.0) for d in sessions] for s in ('AAA', 'BBB', 'CCC')}
    rows['AAA'] = [(d, 100.0 if i <= ci - 65 else 130.0) for i, d in enumerate(sessions)]
    rows['BBB'] = [(d, 100.0 if i <= ci - 65 else 130.0) for i, d in enumerate(sessions)]
    rows['FEW'] = [(d, 100.0) for d in sessions[-40:]]
    elig = rank_universe(rows, sessions, ci)
    assert set(elig) == {'AAA', 'BBB', 'CCC'}  # FEW lacks the history and the anchor closes
    wide = {'S%02d' % j: 0.10 for j in range(18)}
    wide.update({'AAA': 0.30, 'BBB': 0.30})
    basket = basket_for(wide)  # k = round(10% of 20) = 2
    assert [s for s, _ in basket] == ['AAA', 'BBB']  # tie broken by symbol
    assert wilder_atr([{'o': 1, 'h': 2, 'l': 1, 'c': 1.5}] * 10) == [None] * 10
    snap = {'date': '2026-09-11', 'underlying_close': 100.0, 'contracts': [
        contract('X1', '2026-10-09', 101, 'call', None, 1.0, 1.1),   # no delta: ineligible
        contract('X2', '2026-10-09', 101, 'call', 0.5, 0.0, 1.1),    # no bid: ineligible
        contract('X3', '2026-10-09', 104, 'call', 0.5, 1.0, 1.1),    # strike outside ±3%
        contract('X4', '2026-10-09', 100, 'call', 0.5, 1.0, 1.1),    # eligible
    ]}
    assert select_contract(snap)['symbol'] == 'X4'
    assert exit_quote(snap, 'X4') == (1.0, 1.1, NOW)
    assert exit_quote(snap, 'X4'.replace('4', '9')) is None
    zero_bid = {'contracts': [contract('X5', '2026-10-09', 100, 'call', 0.5, 0.0, 0.4)]}
    assert exit_quote(zero_bid, 'X5') == (0.0, 0.4, NOW)  # verified worthless is a price


def test_c2_bootstrap_basket_entry_pricing_and_pinning(db):
    seed_bars(db, '2026-09-15', skip={('MISS', SESSIONS[R1 - 5])})
    held1 = good_snapshot(db, 'WIN1', REB1)
    held2 = good_snapshot(db, 'WIN2', REB1)
    scorer = SwingsC2(db, c2_cfg())
    scorer.process(at('2026-09-16'))
    eps = get_episodes(db)
    assert set(eps) == {'WIN1', 'WIN2'}
    for sym, held in (('WIN1', held1), ('WIN2', held2)):
        p = eps[sym]
        assert p['signal']['rebalance_date'] == REB1
        assert p['stage'] == 'open' and p['premium_status'] == 'open'
        assert p['entry']['contract_symbol'] == held
        assert p['entry']['ask'] == 2.10 and p['entry']['bid'] == 2.00
        assert p['entry']['snapshot_date'] == REB1
        assert p['signal']['stop'] < p['signal']['underlying_entry']
    assert eps['WIN1']['signal']['rank'] == 1 and eps['WIN2']['signal']['rank'] == 2
    with db.tx() as c:
        state = db.get(c, 'swings_c2:state', {})
        assert state['counts'] == {'created': 2, 'opened': 2, 'closed': 0,
                                   'open': 2, 'data_gap': 0}
        assert state['last_basket'][0]['symbol'] == 'WIN1'
        inv = inventory(db, c, at('2026-09-16'))
        assert held1 in inv['options'] and held2 in inv['options']  # §4.1 pinning


def test_c2_stop_exit_priced_from_archive(db):
    seed_bars(db, '2026-09-15', skip={('MISS', SESSIONS[R1 - 5])},
              overrides={('WIN2', '2026-09-15'): dict(o=129.0, h=130.0, l=1.0, c=128.0)})
    good_snapshot(db, 'WIN1', REB1)
    held2 = good_snapshot(db, 'WIN2', REB1)
    # Exit snapshot on the stop date carries the held contract at bid 1.50.
    seed_snapshot(db, 'WIN2', '2026-09-15',
                  [contract(held2, '2026-10-09', 130, 'call', 0.4, 1.50, 1.60)],
                  closes_for('WIN2')['2026-09-15'])
    scorer = SwingsC2(db, c2_cfg())
    scorer.process(at('2026-09-16'))
    eps = get_episodes(db)
    win2 = eps['WIN2']
    assert win2['status'] == 'closed'
    assert win2['underlying_exit']['reason'] == 'stop'
    assert win2['underlying_exit']['date'] == '2026-09-15'
    assert win2['underlying_exit']['price'] == win2['signal']['stop']
    assert win2['exit']['bid'] == 1.50 and win2['exit']['reason'] == 'stop'
    assert win2['pnl'] == pytest.approx((1.50 - 2.10) * 100 - 1.30)
    assert win2['outcome'] == 'loss'
    assert eps['WIN1']['status'] == 'open'
    with db.tx() as c:
        kinds = [r['payload']['event'] for r in db.recent(c, 'swing_c2', limit=50)]
        assert 'exit_priced' in kinds and 'episode_created' in kinds  # §4.3 surface


def test_c2_rebalance_out_stale_walkback(db):
    seed_bars(db, '2026-09-15', skip={('MISS', SESSIONS[R1 - 5])})
    held1 = good_snapshot(db, 'WIN1', REB1)
    good_snapshot(db, 'WIN2', REB1)
    scorer = SwingsC2(db, c2_cfg())
    scorer.process(at('2026-09-16'))
    # WIN1 has no bar on the next rebalance date: membership ends and the
    # underlying exit walks back to its last available close (Sep 17).
    seed_bars(db, '2026-09-18', skip={('WIN1', REB2), ('MISS', SESSIONS[R1 - 5])})
    seed_snapshot(db, 'WIN1', REB2,
                  [contract(held1, '2026-10-09', 148, 'call', 0.45, 2.50, 2.60)],
                  closes_for('WIN1')[REB2])
    scorer.process(at('2026-09-21'))
    eps = get_episodes(db)
    win1 = eps['WIN1']
    assert win1['status'] == 'closed'
    assert win1['underlying_exit']['reason'] == 'rebal_out_stale'
    assert win1['underlying_exit']['date'] == '2026-09-17'
    assert win1['pnl'] == pytest.approx((2.50 - 2.10) * 100 - 1.30)
    assert eps['WIN2']['status'] == 'open'
    # The vacated seat is refilled at the Sep 18 rebalance (top baseline).
    assert any(p['signal']['rebalance_date'] == REB2 for s, p in eps.items()
               if s not in ('WIN1', 'WIN2'))


def test_c2_entry_data_gap_still_completes_underlying(db):
    seed_bars(db, '2026-09-15', skip={('MISS', SESSIONS[R1 - 5])},
              overrides={('WIN2', '2026-09-15'): dict(o=129.0, h=130.0, l=1.0, c=128.0)})
    for sym in ('WIN1', 'WIN2'):
        close = closes_for(sym)[REB1]
        seed_snapshot(db, sym, REB1, [
            contract(sym + 'P1', '2026-10-09', round(close), 'put', -0.5, 1.0, 1.1),
            contract(sym + 'C9', '2026-10-09', round(close), 'call', None, 1.0, 1.1),
        ], close)
    scorer = SwingsC2(db, c2_cfg())
    scorer.process(at('2026-09-16'))
    eps = get_episodes(db)
    assert eps['WIN1']['status'] == 'data_gap' and eps['WIN1']['entry'] is None
    win2 = eps['WIN2']
    assert win2['status'] == 'data_gap'  # premium leg resolved as data_gap...
    assert win2['underlying_exit']['reason'] == 'stop'  # ...underlying still scored
    with db.tx() as c:
        counts = db.get(c, 'swings_c2:state', {})['counts']
        assert counts['created'] == 2 and counts['opened'] == 0
        assert counts['data_gap'] == 2 and counts['closed'] == 0


def test_c2_episodes_table_created_without_module_import(tmp_path):
    """Production import order (regression, 2026-11-03): Store.initialize()
    runs meta.create_all at engine boot, BEFORE app.py lazily imports
    compass.swings_c2 for the engine-role spawn. When the episodes table
    was declared in swings_c2 itself it never registered in time and
    production failed with: relation "swing_c2_episodes" does not exist.
    A fresh interpreter that never imports swings_c2 (or compass.app)
    must still get the table from compass.store alone."""
    import subprocess
    import sys
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    code = (
        "from compass.store import Store\n"
        "from sqlalchemy import inspect\n"
        f"s = Store('sqlite:///{tmp_path}/fresh.db')\n"
        "s.initialize()\n"
        "names = inspect(s.engine).get_table_names()\n"
        "assert 'swing_c2_episodes' in names, names\n"
        "print('OK')\n"
    )
    r = subprocess.run([sys.executable, '-c', code], capture_output=True,
                       text=True, cwd=root)
    assert r.returncode == 0 and 'OK' in r.stdout, r.stderr[-2000:]
