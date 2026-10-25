"""Live-combo scorecard: trailing-28-day stats for the live futures book.

Futures plan phase 1 (Josh, 2026-10-24): after the prune, the live book
is AOI Fade|MCL, Turtle Soup|MCL, Golden Zone|MCL + Golden Zone|MNQ
(watch status), and Rumers Box|MYM. This module reports, per combo, the
closed paper trades of the trailing 28 days — count, win rate, profit
factor, net $ — and flags any combo with PF < 1.0 as kill_watch. That
flag is the pre-committed Turtle Soup kill rule (trailing-4-week
PF < 1.0 -> the disable conversation), generalized to every live combo
so the watch is uniform. Read-only: nothing here gates, blocks, sizes,
or changes a trade; enforcement stays a human decision on this evidence.

Counting rules match the headline stats exactly: closed ICT paper
trades only, and data_gap trades are excluded (Josh, 2026-10-06 —
trades without correct data count as neither wins nor losses).
"""
from .market import number

WINDOW_DAYS = 28

# The post-prune live combos: (detector, symbol root). Golden Zone|MNQ is
# watch-status (paper attribution until 100+ trades); the rest are the
# proven core plus Rumers Box's one surviving combo. AOI Fade|MES was
# added 2026-10-25 (Josh: MES activation — it cleared both replay legs)
# so its live record is tracked from day one.
LIVE_COMBOS = (
    ('aoi_fade', 'MCL'),
    ('aoi_fade', 'MES'),
    ('turtle_soup', 'MCL'),
    ('golden_zone', 'MCL'),
    ('golden_zone', 'MNQ'),
    ('rumers_box', 'MYM'),
)


def combo_stats(trades):
    """Pure aggregation over closed-trade dicts: count, WR, PF, net.

    A scratch (pnl == 0) counts as a trade but neither a win nor a loss.
    PF is None when there are no losses (unbounded — never a kill flag)
    or no winning/losing trades at all.
    """
    out = {'trades': 0, 'wins': 0, 'losses': 0, 'win_rate': None,
           'gross_profit': 0.0, 'gross_loss': 0.0, 'pf': None,
           'net': 0.0, 'kill_watch': False}
    gp = gl = 0.0
    for t in trades:
        pnl = number(t.get('pnl'))
        if pnl is None:
            continue
        out['trades'] += 1
        out['net'] += pnl
        if pnl > 0:
            out['wins'] += 1
            gp += pnl
        elif pnl < 0:
            out['losses'] += 1
            gl += -pnl
    out['gross_profit'] = round(gp, 2)
    out['gross_loss'] = round(gl, 2)
    out['net'] = round(out['net'], 2)
    if out['trades']:
        out['win_rate'] = round(out['wins'] / out['trades'], 4)
    if gl > 0:
        out['pf'] = round(gp / gl, 3)
    out['kill_watch'] = out['pf'] is not None and out['pf'] < 1.0
    return out


def summarize(db, c, now_ts, days=WINDOW_DAYS):
    """Trailing-window stats per live combo from the trade ledger."""
    since = now_ts - days * 86400
    by_combo = {combo: [] for combo in LIVE_COMBOS}
    for key, t in db.prefix(c, 'trade:').items():
        if not isinstance(t, dict):
            continue
        if t.get('source') != 'ict_paper' or t.get('status') != 'closed':
            continue
        if t.get('data_gap'):
            continue
        exited = number(t.get('exited_at'))
        if exited is None or exited < since:
            continue
        root = str(t.get('symbol') or '').split('.')[0].upper()
        combo = (t.get('detector'), root)
        if combo in by_combo:
            by_combo[combo].append(t)
    combos = [{'detector': detector, 'symbol': root,
               **combo_stats(by_combo[(detector, root)])}
              for detector, root in LIVE_COMBOS]
    return {'asof': now_ts, 'window_days': days, 'since': since,
            'combos': combos,
            'kill_watch': ['%s|%s' % (x['detector'], x['symbol'])
                           for x in combos if x['kill_watch']]}
