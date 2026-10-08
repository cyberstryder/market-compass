"""Paper-trade submission for ICT futures detectors.

Takes a detector signal (entry/stop/target/direction) and opens a simulated
position in the legacy paper schema, so the engine's exits() loop manages
stops/targets with identical fill conventions (quote-triggered stops/targets,
intrabar bar detection, session flatten). Every position is tagged with the
detector's strategy name for per-method P&L attribution.

Gating: ICT_FUTURES_PAPER_ENABLED (default false) AND the detector's own
enabled flag must both be on. This is deliberately separate from the global
PAPER_TRADING_ENABLED: the user approved paper-trading exactly these ICT
detectors, and the dedicated flag keeps that boundary explicit.

Position keys are namespaced `position:ict:<detector>:<symbol>` (one open
position per detector+symbol) so ICT paper never collides with legacy
`position:<symbol>` entries. The exits() loop iterates the whole
`position:` prefix, so no exit-loop changes are needed.

No alerts are sent; entries/exits log `paper_decision` rows only.
"""
from .market import number
from .instruments import future_spec
from . import paper_risk
from .futures import futures_session, risk_day
from .simulation import FILL_VERSION, FILL_DESCRIPTION

STRATEGY_TAGS = {
    'golden_zone': 'ict-golden-zone',
    'bos_fvg': 'ict-bos-fvg',
    'bos_gz_vwap': 'ict-bos-gz-vwap',
    'turtle_soup': 'ict-turtle-soup',
    'smt_divergence': 'ict-smt-divergence',
    'aoi_zones': 'ict-aoi-zones',
    'continuation': 'ict-continuation',
    'morning_drive': 'yt-morning-drive',
    'icc': 'yt-icc',
    'rumers_box': 'yt-rumers-box',
    'aoi_fade': 'ict-aoi-fade',
    'trend_rider': 'ict-trend-rider',
}

# detector -> config attr carrying its own enabled flag
DETECTOR_FLAGS = {
    'golden_zone': 'ict_golden_zone',
    'bos_fvg': 'ict_bos_fvg',
    'bos_gz_vwap': 'ict_bos_gz_vwap',
    'turtle_soup': 'ict_turtle_soup',
    'smt_divergence': 'ict_smt_divergence',
    'aoi_zones': 'ict_aoi_zones',
    'continuation': 'ict_continuation',
    'morning_drive': 'ict_morning_drive',
    'icc': 'ict_icc',
    'rumers_box': 'ict_rumers_box',
    'aoi_fade': 'ict_aoi_fade',
    'trend_rider': 'ict_trend_rider',
}


def paper_enabled(cfg, detector):
    """True only when the paper flag AND the detector's own flag are on."""
    flag = DETECTOR_FLAGS.get(detector)
    if not flag:
        return False
    return bool(getattr(cfg, 'ict_futures_paper', False)) and \
        bool(getattr(cfg, flag, False))


def submit(db, c, cfg, now, detector, symbol, sig, track=None,
           flatten_at=None):
    """Open a simulated position from a detector signal.

    `sig` needs direction ('long'/'short'), entry, stop, target (prices).
    `track` (e.g. 'swing') is stored on the trade; the engine exits() loop
    skips session flatten for track=='swing'. `flatten_at` overrides the
    default session flatten (e.g. an earlier time stop).
    Returns {'submitted': bool, 'reason': str, ...}. Never raises on bad
    input: bad signals are declined with a reason.
    """
    if detector not in STRATEGY_TAGS:
        return {'submitted': False, 'reason': 'unknown_detector'}
    if not paper_enabled(cfg, detector):
        return {'submitted': False, 'reason': 'paper_disabled'}
    # Prop-firm rule: no entries when the market is closed, and no entries
    # past the session flatten time (the position would have to flatten
    # immediately). Nothing is held between sessions or over the weekend.
    sess = futures_session(now, symbol)
    if not sess['is_open']:
        return {'submitted': False, 'reason': 'market_closed'}
    flat = flatten_at if flatten_at is not None else sess['flatten_at']
    if now >= flat:
        return {'submitted': False, 'reason': 'past_flatten'}
    side = sig.get('direction')
    entry = number(sig.get('entry'))
    stop = number(sig.get('stop'))
    target = number(sig.get('target'))
    if side not in ('long', 'short') or not entry or not stop or not target:
        return {'submitted': False, 'reason': 'bad_signal'}
    spec = future_spec(symbol)
    if not spec:
        return {'submitted': False, 'reason': 'not_a_future'}
    pos_key = 'position:ict:%s:%s' % (detector, symbol)
    # 2/2/1 scale-out: check if any leg position is open
    for leg_suffix in (':t1', ':t2', ':trail'):
        prev = db.get(c, pos_key + leg_suffix, {}) or {}
        if prev.get('status') == 'open':
            return {'submitted': False, 'reason': 'existing_position'}
    prev = db.get(c, pos_key, {}) or {}
    # Churn guardrail 1: no re-entry within the cooldown after this
    # detector+symbol's last exit. Kills the machine-gun re-entry loop
    # (observed 2026-10-06: same-instrument re-entry 1-4 min after a
    # stop-out, median hold 3 min on bos_fvg).
    # 2/2/1 scale-out: check the t1 leg for the last exit time.
    cooldown_s = (number(getattr(cfg, 'ict_paper_cooldown_min', 15)) or 15) * 60
    prev = db.get(c, pos_key + ':t1', {}) or {}
    if prev.get('status') == 'closed' and prev.get('exited_at'):
        if now - prev['exited_at'] < cooldown_s:
            return {'submitted': False, 'reason': 'cooldown',
                    'exited_at': prev['exited_at']}
    direction = 1 if side == 'long' else -1
    if (stop - entry) * direction >= 0 or (target - entry) * direction <= 0:
        return {'submitted': False, 'reason': 'levels_inverted'}
    risk_pts = abs(entry - stop)
    # Churn guardrail 2: minimum stop distance relative to the detector's
    # own bar volatility. Stops under half the 14-bar ATR are noise-level
    # (the churners ran 0.25x-ATR stops; the one breakeven method uses 1x).
    # Skipped when bars are unavailable rather than blocking the trade.
    min_stop_atr = number(getattr(cfg, 'ict_paper_min_stop_atr', 0.5)) or 0.5
    atr_v = None
    try:
        from .ict_common import ict_bars, atr as _atr
        _bars = ict_bars(db, c, symbol)
        if len(_bars) >= 15:
            atr_v = _atr(_bars)
    except Exception:
        atr_v = None
    if atr_v and risk_pts < min_stop_atr * atr_v:
        return {'submitted': False, 'reason': 'stop_too_tight',
                'risk_pts': round(risk_pts, 4), 'atr': round(atr_v, 4)}
    # Regime filter (Quill Trend Kit): trend-continuation detectors are
    # blocked in range/squeeze (they chop); fade/reversion detectors are
    # blocked in strong trends (they get run over). Skipped when bars are
    # unavailable rather than blocking the trade.
    try:
        from .regime import classify as _classify, allowed as _allowed
        from .ict_common import ict_bars as _ict_bars
        _rbars = _ict_bars(db, c, symbol)
        if len(_rbars) >= 120:
            _reg = _classify(_rbars)
            if not _allowed(detector, _reg['regime'], side):
                return {'submitted': False, 'reason': 'regime_mismatch',
                        'regime': _reg['regime'],
                        'adx': round(_reg['adx'], 1) if _reg['adx'] else None}
    except Exception:
        pass
    per_unit = risk_pts * spec['multiplier'] + 2 * spec['fee'] + \
        2 * spec['tick'] * spec['multiplier']
    if per_unit <= 0:
        return {'submitted': False, 'reason': 'bad_risk'}
    # 2/2/1 scale-out (Josh 2026-10-08): 5 contracts total
    # - Leg 1: 2 contracts, target = 2R (signal target)
    # - Leg 2: 2 contracts, target = 3R (1.5x the 2R distance)
    # - Leg 3: 1 contract, no fixed target (trailer via ict_trail.py)
    # Variant B (Josh 2026-10-08): progressive staircase scale-out.
    # 2 contracts at 1R, 2 at 2R, 1 runner. Stops ratchet up as targets hit:
    # T1 hit → T2 and runner stops → breakeven. T2 hit → runner stop → 1R.
    # Backtest showed this beats plain 2/2/1 and single-position at realistic
    # continuation rates (+0.45R/trade vs -1.26R for plain 2/2/1).
    # Each leg is a separate position so the engine's exits() loop manages them.
    direction_mult = 1 if side == 'long' else -1
    risk_dist = (target - entry) * direction_mult  # positive 2R distance
    target_1r = entry + direction_mult * risk_dist * 0.5
    legs = [
        {'leg': 't1', 'qty': 2, 'target': target_1r, 'is_trailer': False},
        {'leg': 't2', 'qty': 2, 'target': target, 'is_trailer': False},
        {'leg': 'trail', 'qty': 1, 'target': None, 'is_trailer': True},
    ]
    base_trade_id = 'ict-%s-%s-%d' % (detector, symbol.replace('.', '-'),
                                      int(number(sig.get('signal_ts')) or now))
    if db.get(c, 'trade:' + base_trade_id + '-t1'):
        return {'submitted': False, 'reason': 'duplicate'}
    group_id = base_trade_id
    submitted_ids = []
    for leg in legs:
        trade_id = '%s-%s' % (base_trade_id, leg['leg'])
        leg_pos_key = '%s:%s' % (pos_key, leg['leg'])
        trade = {
            'id': trade_id, 'strategy': STRATEGY_TAGS[detector],
            'detector': detector, 'symbol': symbol, 'side': side,
            'asset': 'future', 'signal_time': sig.get('signal_ts'),
            'decided_at': now, 'signal_price': entry, 'status': 'open',
            'entry': entry, 'entered_at': now,
            'entry_quote_ts': 0, 'last_quote_ts': 0,
            'last_bar_checked': (number(sig.get('signal_ts')) or now) - 60,
            'stop': stop, 'target': leg['target'], 'qty': leg['qty'],
            'leg': leg['leg'], 'group_id': group_id,
            'is_trailer': leg['is_trailer'],
            'initial_risk': per_unit * leg['qty'], 'risk_basis': 'stop_distance',
            'tick': spec['tick'], 'multiplier': spec['multiplier'],
            'fee': spec['fee'],
            'fill_model': FILL_DESCRIPTION, 'fill_version': FILL_VERSION,
            'risk_day': risk_day(now), 'risk_policy': paper_risk.VERSION,
        'paper_portfolio': paper_risk.portfolio('future'),
        'flatten_at': flat,
        'signal_rr': number(sig.get('rr')),
        'signal_level': sig.get('level_kind'),
        'source': 'ict_paper',
        }
        if track:
            trade['track'] = track
        db.put(c, leg_pos_key, trade)
        db.put(c, 'trade:' + trade_id, trade)
        db.append(c, 'paper_decision', 'ict_paper', symbol, now,
                  {**trade, 'status': 'entered'}, 'entry:' + trade_id)
        submitted_ids.append(trade_id)
    # Queue Discord alert so Josh can see which detectors fire when
    # (use the t1 leg trade for the alert payload)
    try:
        from . import ict_push
        first_trade = db.get(c, 'trade:' + submitted_ids[0])
        if first_trade:
            ict_push.maybe_queue(db, c, cfg, first_trade)
    except Exception:
        pass
    risk = paper_risk.account(db, c, now, 'future', persist=True)
    risk['entries'] = risk.get('entries', 0) + 1
    db.put(c, paper_risk.key(now, 'future'), risk)
    return {'submitted': True, 'reason': 'entered', 'trade_id': group_id,
            'qty': 5, 'strategy': STRATEGY_TAGS[detector],
            'legs': submitted_ids}


def attribution(db, c):
    """Per-detector paper P&L attribution over closed ICT paper trades.

    Returns {strategy_tag: {'trades': n, 'wins': n, 'losses': n,
    'realized': dollars}}. Evidence/research helper; read-only.
    """
    out = {}
    for key, trade in db.prefix(c, 'trade:ict-').items():
        if not isinstance(trade, dict) or trade.get('status') != 'closed':
            continue
        tag = trade.get('strategy') or 'unknown'
        row = out.setdefault(tag, {'trades': 0, 'wins': 0, 'losses': 0,
                                   'realized': 0.0})
        row['trades'] += 1
        pnl = number(trade.get('pnl')) or 0.0
        row['realized'] += pnl
        if pnl > 0:
            row['wins'] += 1
        elif pnl < 0:
            row['losses'] += 1
    return out


def reset_paper_book(db, c, now):
    """One-shot destructive reset of the ICT futures paper book.

    Deletes all ICT paper trade records (trade:ict-*) and open paper
    positions (position:ict:*), and zeroes the futures paper-risk ledger
    so the next ledgers() call starts clean. Used to start a fresh
    sample; the paper_decision audit log is intentionally preserved.
    Returns counts of what was removed.

    Uses bulk DELETEs to avoid lock contention with the running engine
    (row-by-row deletes can hit LockNotAvailable when the engine is
    actively updating positions).
    """
    from . import paper_risk
    from .futures import risk_day
    from sqlalchemy import text
    counts = {'trades': 0, 'positions': 0, 'ledgers': 0}
    # Bulk delete with LIKE patterns — single statement per prefix.
    # If we hit lock contention, let it raise so the caller (app.py)
    # can rollback and retry the whole transaction.
    for prefix, key in [('trade:ict-%', 'trades'),
                        ('position:ict:%', 'positions')]:
        result = c.execute(
            text("DELETE FROM state WHERE key LIKE :pattern"),
            {'pattern': prefix}
        )
        counts[key] = result.rowcount or 0
    for k in list(db.prefix(c, 'paper_risk:v2:').keys()):
        if k.endswith(':futures'):
            db.delete(c, k)
            counts['ledgers'] += 1
    # Seed a clean zeroed ledger for today so ledgers() returns saved
    # state instead of attempting a reconstruction.
    db.put(c, paper_risk.key(now, 'future'),
           dict(realized=0.0, entries=0, day=risk_day(now),
                portfolio='futures', label='Futures',
                policy=paper_risk.VERSION, ready=True,
                initialized_at=now,
                migration=dict(reset='futures paper book reset for a fresh sample',
                               reset_at=now)))
    counts['ledgers'] += 1
    return counts
