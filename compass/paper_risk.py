"""Versioned paper accounts. Historical combined risk states are never rewritten."""
from math import isclose, isfinite
from .futures import risk_day

VERSION = 'paper-portfolios-v2'
PORTFOLIOS = {'future': 'futures', 'option': 'zero_dte', 'stock': 'stocks'}
LABELS = {'futures': 'Futures', 'zero_dte': '0DTE options', 'stocks': 'Stocks / swing'}


def portfolio(asset):
    return PORTFOLIOS.get(asset, 'unknown')


def key(now, asset):
    return 'paper_risk:v2:'+risk_day(now)+':'+portfolio(asset)


def ledgers(db, c, now, persist=False):
    """Seed once from legacy trades, requiring reconciliation to the old ledger.

    Called by the single leased engine before writes. Read-only callers receive a
    preview until initialization. A missing partial ledger fails closed rather
    than reconstructing and double counting positions already managed under v2.
    """
    day = risk_day(now)
    saved = {asset: db.get(c, key(now, asset)) for asset in PORTFOLIOS}
    if all(saved.values()):
        return saved
    legacy = db.get(c, 'risk:'+day, {'realized': 0, 'entries': 0})
    totals = {asset: {'realized': 0., 'entries': 0} for asset in PORTFOLIOS}
    source_ids = []
    problems = []
    if any(saved.values()):
        problems.append('Partial v2 ledger; manual reconciliation required')
    for trade_id, trade in db.prefix(c, 'trade:').items():
        entered = trade.get('entered_at')
        exited = trade.get('exited_at')
        entered_today = entered is not None and risk_day(entered) == day
        exited_today = exited is not None and risk_day(exited) == day
        if ((entered_today and trade.get('risk_policy') == VERSION) or
                (exited_today and trade.get('exit_risk_policy') == VERSION)):
            problems.append('V2 activity exists without complete account state: '+trade_id)
        entry = entered_today and trade.get('risk_policy') != VERSION
        exit_ = exited_today and trade.get('exit_risk_policy') != VERSION
        if not (entry or exit_):
            continue
        asset = trade.get('asset')
        if asset not in totals:
            problems.append('Unclassified legacy trade '+trade_id)
            continue
        source_ids.append(trade_id)
        if entry:
            totals[asset]['entries'] += 1
        if exit_:
            pnl = trade.get('pnl')
            if not isinstance(pnl, (int, float)) or not isfinite(pnl):
                problems.append('Missing legacy P&L '+trade_id)
            else:
                totals[asset]['realized'] += pnl
    reconciled = {'entries': sum(t['entries'] for t in totals.values()),
                  'realized': sum(t['realized'] for t in totals.values())}
    if reconciled['entries'] != legacy.get('entries', 0) or not isclose(
            reconciled['realized'], legacy.get('realized', 0), abs_tol=.000001, rel_tol=0):
        problems.append('Legacy totals do not match retained trade history')
    result = {}
    for asset, scope in PORTFOLIOS.items():
        item = saved[asset] or dict(**totals[asset], day=day, portfolio=scope,
            label=LABELS[scope], policy=VERSION, ready=not problems,
            initialized_at=now if persist else None,
            migration=dict(legacy_key='risk:'+day, legacy=dict(legacy),
                reconstructed=reconciled, source_trade_ids=sorted(source_ids),
                opening_balance=dict(totals[asset]), problems=problems))
        # Never treat one surviving account as usable after a partial-state loss.
        if problems and any(saved.values()):
            item = {**item, 'ready': False}
        result[asset] = item
        if persist:
            db.put(c, key(now, asset), item)
    return result


def account(db, c, now, asset, persist=False):
    return ledgers(db, c, now, persist)[asset]


def snapshot(db, c, cfg, now):
    accounts = ledgers(db, c, now)
    positions = [p for p in db.prefix(c, 'position:').values() if p.get('status') == 'open']
    return dict(policy=VERSION, day=risk_day(now),
        accounts=[{**r, 'open_positions': sum(p.get('asset') == asset for p in positions),
            'limits': dict(risk_per_entry=cfg.risk,
                max_entries=cfg.max_entries, concurrent_positions=3)} for asset, r in accounts.items()],
        legacy_basis='Historical combined risk states are frozen audit evidence, not current account limits.',
        day_basis='Existing exchange risk day (17:00 America/Chicago rollover) retained for all paper accounts.')
