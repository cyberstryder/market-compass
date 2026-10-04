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
    if (db.get(c, pos_key, {}) or {}).get('status') == 'open':
        return {'submitted': False, 'reason': 'existing_position'}
    direction = 1 if side == 'long' else -1
    if (stop - entry) * direction >= 0 or (target - entry) * direction <= 0:
        return {'submitted': False, 'reason': 'levels_inverted'}
    risk_pts = abs(entry - stop)
    per_unit = risk_pts * spec['multiplier'] + 2 * spec['fee'] + \
        2 * spec['tick'] * spec['multiplier']
    if per_unit <= 0:
        return {'submitted': False, 'reason': 'bad_risk'}
    risk_dollars = number(getattr(cfg, 'risk', 100)) or 100
    qty = min(spec['max_qty'], max(1, int(risk_dollars / per_unit)))
    if qty < 1:
        return {'submitted': False, 'reason': 'no_size'}
    trade_id = 'ict-%s-%s-%d' % (detector, symbol.replace('.', '-'),
                                 int(number(sig.get('signal_ts')) or now))
    if db.get(c, 'trade:' + trade_id):
        return {'submitted': False, 'reason': 'duplicate'}
    trade = {
        'id': trade_id, 'strategy': STRATEGY_TAGS[detector],
        'detector': detector, 'symbol': symbol, 'side': side,
        'asset': 'future', 'signal_time': sig.get('signal_ts'),
        'decided_at': now, 'signal_price': entry, 'status': 'open',
        'entry': entry, 'entered_at': now,
        'entry_quote_ts': 0, 'last_quote_ts': 0,
        'last_bar_checked': (number(sig.get('signal_ts')) or now) - 60,
        'stop': stop, 'target': target, 'qty': qty,
        'initial_risk': per_unit * qty, 'risk_basis': 'stop_distance',
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
    db.put(c, pos_key, trade)
    db.put(c, 'trade:' + trade_id, trade)
    risk = paper_risk.account(db, c, now, 'future', persist=True)
    risk['entries'] = risk.get('entries', 0) + 1
    db.put(c, paper_risk.key(now, 'future'), risk)
    db.append(c, 'paper_decision', 'ict_paper', symbol, now,
              {**trade, 'status': 'entered'}, 'entry:' + trade_id)
    # Queue Discord alert so Josh can see which detectors fire when
    try:
        from . import ict_push
        ict_push.maybe_queue(db, c, cfg, trade)
    except Exception:
        pass
    return {'submitted': True, 'reason': 'entered', 'trade_id': trade_id,
            'qty': qty, 'strategy': trade['strategy']}


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
