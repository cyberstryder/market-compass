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

# Mix/match (Josh 2026-10-14): per-detector symbol allowlist based on
# paper-trading P&L analysis. Only these detector+symbol combos are profitable.
# - AOI Fade: MCL (+$752) and MNQ (+$288) work; MES (-$220) doesn't
# - AOI Zones: only MES (+$253) is profitable; MCL breakeven, MGC/MNQ/SIL lose
# - BOS+FVG: MNQ (+$520) works; MCL/MES/MGC lose; SIL dropped 2026-10-15
#   (Tradeify doesn't support SIL; SI is full-size mini, breaks micros-only)
# - BOS+GZ/VWAP: only MCL (+$22, 1 trade) — keep for now
# - Turtle Soup: only MCL (+$445) works; MES/MGC/MNQ lose
# - Continuation: loses on ALL symbols — disabled entirely
# - MGC: loses on every detector (-$3,689 total) — excluded everywhere
# - SIL: dropped 2026-10-15 — Tradeify doesn't support micro silver;
#   SIL paper was -$1,046 over 19 trades (26% WR), 2/2/1 fails on silver
# - NEW 2026-10-15: MYM, M2K, M6A, M6E added for paper-trading evaluation
#   (Tradeify allows these micros; no P&L history yet — prune after 20-30
#   trades based on per-detector P&L, same mix/match process as 2026-10-14)
# - 2026-10-17: Turtle Soup removed from MYM. Research: Turtle Soup on MYM
#   lost -$1,793 (20 trades, avg -$90). MYM trends (real breakouts continue),
#   Turtle Soup fades breakouts (mean-reversion). Method-symbol mismatch.
#   MYM is profitable WITHOUT Turtle Soup (+$742 across AOI Zones, BOS+FVG,
#   Rumers Box). Turtle Soup works on MCL (+$748, ranging market) — keep it there.
# - PRUNE 2026-10-24 (Josh, futures plan phase 1): allowlists cut to the
#   proven core. AOI Fade {MCL} and Turtle Soup {MCL} are the surviving
#   live combos; Golden Zone {MCL, MNQ} is watch-status (NQ was tests-only
#   before — now a watch symbol per the plan; MGC and the eval roots are
#   out); Rumers Box is restricted to {MYM}, its one positive combo (MNQ
#   blocked: same logic, ~10x the loss per trade — stop geometry). The
#   Oct-15 eval roots (MYM/M2K/M6A/M6E) come off every other detector:
#   their paper evaluation never produced clean data (production scan
#   paths silently skip M2K/M6A/M6E, so that evaluation was vacuous).
#   The pruned detectors (bos_fvg, bos_gz_vwap, aoi_zones, smt_divergence)
#   get EMPTY allowlists as defense in depth: their config defaults are
#   now off, and if a flag is ever re-enabled by env the old losing combos
#   stay blocked until a replay earns them back. Evidence (three datasets,
#   same verdict — fades at levels earn, structure-chasers bleed):
#   production-config backtest (book carried by Turtle Soup|MCL and
#   AOI Fade|MCL), the full strategy x ticker matrix (only 2 of 112
#   combos cleared the exploratory bar), and an independent 8-week replay.
DETECTOR_SYMBOL_ALLOWLIST = {
    'aoi_fade': {'MCL', 'MES'},  # 2026-10-24 prune {MCL}; MES added 2026-10-25 (cleared both replay legs: 90 tr, +$2,569)
    'aoi_zones': set(),  # 2026-10-24: default-off; empty blocks re-enable by env
    'bos_fvg': set(),  # 2026-10-24: default-off; empty blocks re-enable by env
    'bos_gz_vwap': set(),  # 2026-10-24: default-off; empty blocks re-enable by env
    'smt_divergence': set(),  # 2026-10-24: default-off; was unrestricted
    'turtle_soup': {'MCL'},  # 2026-10-24 prune: proven core only (MYM out 2026-10-17)
    'continuation': set(),  # disabled entirely
    'golden_zone': {'MCL', 'MNQ'},  # 2026-10-24: watch status; MGC + eval roots out; NQ was tests-only, now a watch symbol per the plan
    'rumers_box': {'MYM'},  # Added 2026-10-24: only positive combo; MNQ blocked on stop geometry
    # Detectors not listed here have no symbol restriction (they're
    # either disabled or not in the futures mix).
}

def symbol_allowed(detector, symbol):
    """True if this detector is allowed to trade this symbol.

    Returns True for detectors not in the allowlist (no restriction).
    Returns False if the detector is in the allowlist but the symbol root
    is not in its allowed set.
    """
    allowed = DETECTOR_SYMBOL_ALLOWLIST.get(detector)
    if allowed is None:
        return True
    root = str(symbol or '').split('.')[0].upper()
    return root in allowed


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
    # Mix/match filter (Josh 2026-10-14): only allow detector+symbol combos
    # that were profitable in paper trading. Decline others.
    if not symbol_allowed(detector, symbol):
        return {'submitted': False, 'reason': 'symbol_not_allowed',
                'detector': detector, 'symbol': symbol}
    # Prop-firm rule: no entries when the market is closed, and no entries
    # past the session flatten time (the position would have to flatten
    # immediately). Nothing is held between sessions or over the weekend.
    sess = futures_session(now, symbol)
    if not sess['is_open']:
        return {'submitted': False, 'reason': 'market_closed'}
    # News pause: Don't enter new futures positions during high-impact
    # economic releases (CPI, NFP, FOMC, etc.). News chop kills entries.
    # Check TraderMatrix Economic Calendar intel (refreshed hourly by engine).
    try:
        from .tradermatrix_intel import should_pause_for_news
        pause, event = should_pause_for_news(db, c)
        if pause:
            return {'submitted': False, 'reason': 'news_pause',
                    'event': event.get('title') if event else 'high-impact news'}
    except Exception:
        pass  # Intel not available, proceed without news filter
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
    # 2026-10-18: Global minimum R:R filter. Rejects signals with
    # risk:reward below 1.5. AOI Fade was firing R:R 0.17 signals
    # (25pt reward, 146pt risk) causing -$1,482 losses across 2/2/1 legs.
    # Bos Fvg has its own min_rr; this is the backstop for all detectors.
    reward_pts = abs(target - entry)
    rr = reward_pts / risk_pts if risk_pts > 0 else 0
    min_rr = number(getattr(cfg, 'ict_paper_min_rr', 1.5)) or 1.5
    if rr < min_rr:
        return {'submitted': False, 'reason': 'rr_too_low',
                'rr': round(rr, 2), 'min_rr': min_rr,
                'risk_pts': round(risk_pts, 2), 'reward_pts': round(reward_pts, 2)}
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
    # 2026-10-18: Simplified to single position (Josh). The 2/2/1 scaling
    # (T1 2ct + T2 2ct + Trail 1ct = 5ct) was net losers — Oct 15: T1 +$47,
    # T2 -$116, Trail -$58 = net -$127. T1 hits, T2/Trail reverse and lose more.
    # Paper and live both trade 2 contracts at the detector's target.
    # No scaling, no trailer. One position per signal.
    direction_mult = 1 if side == 'long' else -1
    legs = [
        {'leg': 't1', 'qty': 2, 'target': target, 'is_trailer': False},
    ]
    base_trade_id = 'ict-%s-%s-%d' % (detector, symbol.replace('.', '-'),
                                      int(number(sig.get('signal_ts')) or now))
    if db.get(c, 'trade:' + base_trade_id + '-t1'):
        return {'submitted': False, 'reason': 'duplicate'}
    # Brake shadow evaluation (2026-10-20): measure the candidate brakes
    # (3-strike stand-down to the next 08:30 CT, correlation cap 1) against
    # the live book WITHOUT enforcing them — Josh approved shadow logging
    # only; live brakes wait for the shadow evidence. Evaluated here, after
    # every real gate has passed and before this trade's legs are written,
    # so correlation state never includes the trade itself. A failure here
    # must never affect the submission.
    shadow = None
    if getattr(cfg, 'ict_brake_shadow', True):
        try:
            from . import brake_shadow as _bs
            shadow = _bs.evaluate(db, c, now, detector, symbol, side, atr_v)
        except Exception as e:
            try:
                db.health('brake_shadow', 'error',
                          f"evaluate failed: {type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass
    group_id = base_trade_id
    submitted_ids = []
    leg_pos_keys = {}
    for leg in legs:
        trade_id = '%s-%s' % (base_trade_id, leg['leg'])
        leg_pos_key = '%s:%s' % (pos_key, leg['leg'])
        leg_pos_keys[trade_id] = leg_pos_key
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
    except Exception as e:
        # Log instead of silently swallowing (was hiding table-missing errors)
        try:
            db.health('ict_push_queue', 'error', f"{type(e).__name__}: {str(e)[:200]}")
        except:
            pass
    # CrossTrade webhook for live automation (2026-10-13)
    # Fires after successful paper submit, only for focused detectors.
    # Disabled by default (CROSSTRADE_ENABLED=true to enable).
    try:
        from . import crosstrade
        if first_trade:
            # Build trade dict for CrossTrade from the submitted trade
            # first_trade has detector, symbol, side, entry, stop, target
            crosstrade.send_webhook(db, cfg, first_trade)
    except Exception as e:
        try:
            db.health('crosstrade_webhook', 'error', f"hook failed: {type(e).__name__}: {str(e)[:200]}")
        except:
            pass
    # TradersPost webhook for live automation (2026-10-14)
    # Fires after successful paper submit. Disabled by default
    # (TRADERSPOST_ENABLED=true + TRADERSPOST_WEBHOOK_URL to enable).
    # Sends the single 2-contract position (2/2/1 removed 2026-10-18, fb54017).
    try:
        from . import traderspost
        # Skip entirely if TradersPost is disabled (avoid unnecessary db calls
        # in tests and when TradersPost is not configured).
        if not getattr(cfg, 'traderspost_enabled', False):
            pass
        else:
            # 2026-10-15: Prevent duplicate ticker stacking AND hedging.
            # 2026-10-19: FIX - check SAME detector too, not just "another".
            # 3x aoi_zones MES buys in 4 min stacked to 6 contracts because
            # the loop skipped the current detector.
            # 1. Duplicate: If ANY detector (including this one) already has
            #    an open position for this symbol, skip TradersPost.
            #    TradersPost combines same-ticker positions, so two signals
            #    on MES would stack to 4 contracts (2+2) instead of 2.
            # 2. Hedging: If another detector has an open position on a
            #    CORRELATED instrument in the OPPOSITE direction, skip
            #    TradersPost. Tradeify flags opposing positions on correlated
            #    instruments (e.g., MES short + MNQ long) as hedging.
            #    Correlated groups: Equity indices (MES, MNQ, MYM, M2K),
            #    Currencies (M6A, M6E). MGC and MCL are standalone.
            # Paper trade is still recorded, but not sent to TradersPost.
            # Use db.get for each detector (fast single-key lookup) instead of
            # db.prefix scan (slow, can deadlock inside transaction).
            first_trade = db.get(c, 'trade:' + submitted_ids[0])
            symbol = first_trade.get('symbol', '') if first_trade else ''
            side = first_trade.get('side', '').lower() if first_trade else ''
            # Extract root symbol (e.g., "MES.c.0" -> "MES", "MNQ.c.0" -> "MNQ")
            root = symbol.split('.')[0] if symbol else ''
            # Correlated groups (Tradeify hedging rules)
            CORRELATED = {
                'MES': 'EQUITY', 'MNQ': 'EQUITY', 'MYM': 'EQUITY', 'M2K': 'EQUITY',
                'M6A': 'CURRENCY', 'M6E': 'CURRENCY',
            }
            group = CORRELATED.get(root, None)
            skip_reason = None
            if symbol and side:
                # Check all active detectors for open positions
                # 2026-10-19: FIX - also check the CURRENT detector, not just
                # "another" detector. Same-detector re-entry was stacking:
                # 3x aoi_zones MES buys in 4 min (13:07, 13:09, 13:11) created
                # 6 contracts instead of 2. The loop used to skip the current
                # detector, so it never saw its own open position.
                # Derived from STRATEGY_TAGS so every detector that can
                # submit is covered automatically — the hand-maintained list
                # missed smt_divergence, morning_drive, icc, rumers_box and
                # trend_rider, leaving their open positions invisible here.
                for other_detector in STRATEGY_TAGS:
                    # Check all symbols for this detector (need to check both
                    # same-symbol and correlated-symbol positions)
                    # Build list of symbols to check: same symbol + correlated group
                    symbols_to_check = [symbol]
                    if group:
                        # Add all symbols in the same correlated group
                        for sym_root, grp in CORRELATED.items():
                            if grp == group and sym_root != root:
                                # Convert root to Compass format (e.g., "MES" -> "MES.c.0")
                                # Need to check both .c.0 and .v.0 formats
                                for suffix in ('.c.0', '.v.0'):
                                    symbols_to_check.append(f"{sym_root}{suffix}")
                    for check_symbol in symbols_to_check:
                        for suffix in ('', ':t1', ':t2', ':trail'):
                            pos_key = f'position:ict:{other_detector}:{check_symbol}{suffix}'
                            pos = db.get(c, pos_key, {}) or {}
                            # Never count the position THIS submit() just
                            # wrote: the check runs after the legs are stored,
                            # so without this exclusion every signal finds
                            # itself and skips. That bug (8bb36d6) silently
                            # stopped ALL live sends from its deploy until
                            # this fix — paper kept trading, so nothing
                            # looked wrong on the dashboard.
                            if pos.get('id') in submitted_ids:
                                continue
                            if pos.get('status') == 'open':
                                pos_side = pos.get('side', '').lower()
                                if check_symbol == symbol:
                                    # Same symbol, any direction = duplicate stacking
                                    skip_reason = f"skip {symbol}: already open for {other_detector}"
                                    break
                                elif group and pos_side and pos_side != side:
                                    # Correlated symbol, opposite direction = hedging
                                    skip_reason = f"skip {symbol} {side}: hedging vs {check_symbol} {pos_side} ({group})"
                                    break
                        if skip_reason:
                            break
                    if skip_reason:
                        break
            if not skip_reason and symbol:
                # Fail closed on unmapped symbols: map_symbol() falls back
                # to the raw Compass format ("NQ.c.0"), which TradersPost
                # would treat as an unknown ticker. Never POST that.
                mapped = traderspost.map_symbol(symbol)
                if mapped == symbol and '.' in symbol:
                    skip_reason = f"skip {symbol}: no TradersPost symbol mapping"
            if skip_reason:
                # Mark the trades FIRST: db.health() opens its own
                # transaction, and if it fails (e.g. SQLite lock in tests)
                # it must not abort the marking — the marks are what the
                # dashboard and /api/traderspost-skips read back.
                # Mark BOTH copies (trade + position): engine.exits()
                # closes a trade by copying the position record over
                # the trade record, so a flag written only to the trade
                # record is erased at close (2026-10-20: three live
                # trades filled while /api/traderspost-skips and the
                # Simple live split showed zero sends).
                for trade_id in submitted_ids:
                    for key in ('trade:' + trade_id,
                                leg_pos_keys.get(trade_id)):
                        if not key:
                            continue
                        t = db.get(c, key)
                        if t:
                            t = dict(t)
                            t['traderspost_sent'] = False
                            t['traderspost_skip_reason'] = skip_reason
                            db.put(c, key, t)
                try:
                    db.health('traderspost_webhook', 'skipped', skip_reason)
                except Exception:
                    pass
            else:
                # 2026-10-18: Single position (2 contracts) sent to TradersPost.
                # No 2/2/1 scaling — paper and live both trade the detector's
                # target with 2 contracts.
                # Audit #4: do NOT POST from here — submit() runs inside
                # the engine tick transaction, and a slow webhook must
                # never hold that transaction open. Queue a durable
                # outbox row in this transaction instead; Engine.tick
                # dispatches it after commit, and dispatch writes
                # traderspost_sent (or the failure reason) onto BOTH
                # copies — see the skip branch: exits() overwrites the
                # trade record from the position record at close.
                if submitted_ids:
                    trade = db.get(c, 'trade:' + submitted_ids[0])
                    if trade:
                        trade = dict(trade)
                        trade['is_first_leg'] = True
                        queued = traderspost.queue_signal(
                            db, c, trade,
                            trade_key='trade:' + submitted_ids[0],
                            position_key=leg_pos_keys.get(submitted_ids[0]))
                        if not queued:
                            # Payload refused at queue time (fail-closed
                            # validation in build_payload): mark exactly
                            # like a send failure, on both copies.
                            for key in ('trade:' + submitted_ids[0],
                                        leg_pos_keys.get(submitted_ids[0])):
                                if not key:
                                    continue
                                t = db.get(c, key)
                                if t:
                                    t = dict(t)
                                    t['traderspost_sent'] = False
                                    t['traderspost_skip_reason'] = \
                                        'send_failed: invalid payload ' \
                                        'at queue time'
                                    db.put(c, key, t)
    except Exception as e:
        try:
            db.health('traderspost_webhook', 'error', f"hook failed: {type(e).__name__}: {str(e)[:200]}")
        except:
            pass
    # Brake shadow: persist the evaluation now that the trade (and its
    # TradersPost outcome) exists, so realized outcomes can be scored
    # against what the brakes would have done. Bookkeeping only.
    if shadow is not None and submitted_ids:
        try:
            from . import brake_shadow as _bs
            _t = db.get(c, 'trade:' + submitted_ids[0]) or {}
            _bs.record(db, c, shadow, submitted_ids[0],
                       _t.get('traderspost_sent'))
        except Exception as e:
            try:
                db.health('brake_shadow', 'error',
                          f"record failed: {type(e).__name__}: {str(e)[:200]}")
            except Exception:
                pass
    risk = paper_risk.account(db, c, now, 'future', persist=True)
    risk['entries'] = risk.get('entries', 0) + 1
    db.put(c, paper_risk.key(now, 'future'), risk)
    return {'submitted': True, 'reason': 'entered', 'trade_id': group_id,
            'qty': 2, 'strategy': STRATEGY_TAGS[detector],
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
