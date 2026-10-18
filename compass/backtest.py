"""Backtesting framework for ICT futures detectors.

Runs detector pure functions on historical bars, simulates execution,
reports P&L. No DB required — detectors' pure functions are called directly.

Design:
- Input: bars (list of {ts, o, h, l, c}), levels (session highs/lows)
- For each bar, call detector's pure function (e.g., detect_sweep)
- Record signals, simulate execution (entry at next bar, exit at stop/target)
- Report: trades, P&L, win rate, max drawdown, Sharpe

Supported detectors (2026-10-17):
- turtle_soup: detect_sweep() — needs levels (session highs/lows)
- aoi_zones: find_order_block() + _ob_trade_levels() — needs bars only

Usage:
    from compass.backtest import backtest_turtle_soup
    result = backtest_turtle_soup(bars, levels, symbol='MNQ.c.0')
    print(result['summary'])

For production use, bars come from the DB (bar_window keys) via an API
endpoint. This module is pure — it doesn't touch the DB.
"""
from .market import number


def _session_levels(bars, up_to_idx=None):
    """Compute session highs/lows from bars.

    Groups bars by day (CT), returns [(price, kind, day)] for each day's high/low.
    Used as levels for Turtle Soup. Day is included so backtest can filter
    to recent levels only (stale levels from 30 days ago are not traded).

    Args:
        bars: List of bar dicts.
        up_to_idx: If provided, only use bars[:up_to_idx+1] (progressive mode).
            This prevents LOOKAHEAD: the current day's FINAL high/low are not
            knowable until the day ends. In progressive mode, the forming day
            contributes its running high/low from bars seen so far only.

    FIX (2026-10-18): Progressive levels eliminate lookahead. Previously
    _session_levels(bars) was called once on the FULL array, so the current
    day's final high/low were usable as targets — a target was taken from a
    bar two days in the future.
    """
    from datetime import datetime
    from .market import CT
    if up_to_idx is not None:
        bars = bars[:up_to_idx+1]
    by_day = {}
    for b in bars:
        dt = datetime.fromtimestamp(b['ts'], CT)
        day = dt.date()
        if day not in by_day:
            by_day[day] = {'high': b['h'], 'low': b['l']}
        else:
            by_day[day]['high'] = max(by_day[day]['high'], b['h'])
            by_day[day]['low'] = min(by_day[day]['low'], b['l'])
    levels = []
    for day, hl in sorted(by_day.items()):
        levels.append((hl['high'], 'session_high', day))
        levels.append((hl['low'], 'session_low', day))
    return levels


def _simulate_trade(entry_bar_idx, bars, signal, symbol, flatten_hour=15, flatten_min=45, max_bars_held=100):
    """Simulate one trade from entry to exit.

    Args:
        entry_bar_idx: Index of bar where entry occurs (signal bar + 1).
        bars: Full bar list.
        signal: Dict with entry, stop, target, direction.
        symbol: Symbol for P&L calculation (needs spec).
        flatten_hour/min: Session flatten time (CT).
        max_bars_held: Maximum bars to hold before forcing exit (prevents
            runaway trades if flatten check fails, e.g., daily bars).

    Returns:
        Dict with exit price, exit reason, P&L, bars held, or None if
        entry_bar_idx is beyond bars.
    """
    if entry_bar_idx >= len(bars):
        return None
    from .instruments import future_spec
    spec = future_spec(symbol)
    if not spec:
        return None
    entry = signal['entry']
    stop = signal['stop']
    target = signal['target']
    direction = signal['direction']
    mult = spec['multiplier']
    fee = spec['fee']
    tick = spec['tick']
    # Simulate: check each bar after entry for stop/target hit
    for j in range(entry_bar_idx, len(bars)):
        bar = bars[j]
        # Max hold: force exit if trade held too long (prevents runaway)
        # This catches cases where flatten check fails (e.g., daily bars,
        # timestamp issues, or bars not in chronological order).
        if j - entry_bar_idx >= max_bars_held:
            exit_price = bar['c']
            pnl = (exit_price - entry) * mult if direction == 'long' else (entry - exit_price) * mult
            pnl -= 2 * fee
            return {
                'exit_price': exit_price, 'exit_reason': 'max_hold',
                'pnl': pnl, 'bars_held': j - entry_bar_idx,
                'exit_ts': bar['ts'],
            }
        # Check flatten time (15:45 CT)
        from datetime import datetime
        from .market import CT
        dt = datetime.fromtimestamp(bar['ts'], CT)
        if dt.hour > flatten_hour or (dt.hour == flatten_hour and dt.minute >= flatten_min):
            # Flatten at bar open
            exit_price = bar['o']
            pnl = (exit_price - entry) * mult if direction == 'long' else (entry - exit_price) * mult
            pnl -= 2 * fee  # entry + exit fees
            return {
                'exit_price': exit_price, 'exit_reason': 'flatten',
                'pnl': pnl, 'bars_held': j - entry_bar_idx,
                'exit_ts': bar['ts'],
            }
        # Check stop/target (intrabar: if both hit, assume stop hit first for longs, target first for shorts? No — use conservative: if bar range includes both, exit at stop (worse case))
        if direction == 'long':
            if bar['l'] <= stop:
                # Stop hit
                pnl = (stop - entry) * mult - 2 * fee
                return {
                    'exit_price': stop, 'exit_reason': 'stop',
                    'pnl': pnl, 'bars_held': j - entry_bar_idx,
                    'exit_ts': bar['ts'],
                }
            if bar['h'] >= target:
                # Target hit
                pnl = (target - entry) * mult - 2 * fee
                return {
                    'exit_price': target, 'exit_reason': 'target',
                    'pnl': pnl, 'bars_held': j - entry_bar_idx,
                    'exit_ts': bar['ts'],
                }
        else:  # short
            if bar['h'] >= stop:
                pnl = (entry - stop) * mult - 2 * fee
                return {
                    'exit_price': stop, 'exit_reason': 'stop',
                    'pnl': pnl, 'bars_held': j - entry_bar_idx,
                    'exit_ts': bar['ts'],
                }
            if bar['l'] <= target:
                pnl = (entry - target) * mult - 2 * fee
                return {
                    'exit_price': target, 'exit_reason': 'target',
                    'pnl': pnl, 'bars_held': j - entry_bar_idx,
                    'exit_ts': bar['ts'],
                }
    # Never exited — close at last bar close
    last = bars[-1]
    exit_price = last['c']
    pnl = (exit_price - entry) * mult if direction == 'long' else (entry - exit_price) * mult
    pnl -= 2 * fee
    return {
        'exit_price': exit_price, 'exit_reason': 'end_of_data',
        'pnl': pnl, 'bars_held': len(bars) - 1 - entry_bar_idx,
        'exit_ts': last['ts'],
    }


def backtest_turtle_soup(bars, symbol='MNQ.c.0', levels=None,
                         sweep_ticks=None, confirm_bars=5, atr_frac=0.3,
                         buffer_mult=2.0, adx_max=25, min_rr=1.5):
    """Backtest Turtle Soup on historical bars.

    Args:
        bars: List of {ts, o, h, l, c} dicts, ascending by ts.
        symbol: Symbol for P&L calculation.
        levels: Optional list of (price, kind) for session levels. If None,
            computed from bars via _session_levels().
        sweep_ticks: Override for sweep threshold (if None, computed from ATR).
        confirm_bars: Bars to confirm reclaim (default 5, post-2026-10-17 fix).
        atr_frac: ATR fraction for sweep_ticks (default 0.3, post-fix).
        buffer_mult: Buffer multiplier for stop (default 2.0, post-fix).
        adx_max: Only trade if ADX < adx_max (default 25, post-fix).
        min_rr: Minimum risk/reward (default 1.5).

    Returns:
        Dict with 'trades', 'summary', 'signals'.
    """
    from .turtle_soup import detect_sweep, _sweep_ticks, _atr
    from .regime import adx as _adx
    if not bars or len(bars) < 50:
        return {'trades': [], 'summary': {'error': 'insufficient bars'}, 'signals': []}
    # FIX (2026-10-18): Do NOT precompute levels from full array (LOOKAHEAD).
    # Levels are computed progressively inside the loop via _session_levels(bars, up_to_idx=i).
    trades = []
    signals = []
    # Use while loop (not for) so we can skip ahead after trades.
    # For loop reassignment of i doesn't work in Python.
    i = 50
    # FIX (2026-10-18): Track seen sweep events, not level+direction.
    # Old: key=(level_price, direction) with stale ts → one setup re-fired 53 times
    #   because nearby levels created different keys for the same price action.
    # New: one physical sweep event → one trade. Keyed by (sweep_bar_ts, direction).
    seen_sweeps = set()  # (sweep_bar_ts, direction) -> already traded
    while i < len(bars):
        # Get bars up to current (for detector)
        window = bars[:i+1]
        # FIX (2026-10-18) LOOKAHEAD: Compute levels progressively from bars seen so far.
        # The forming day contributes its RUNNING high/low, not its final high/low.
        levels = _session_levels(bars, up_to_idx=i)
        # ADX filter (post-2026-10-17 fix): skip if trending
        try:
            adx_val = _adx(window, 14)
            if adx_val is not None and adx_val >= adx_max:
                i += 1
                continue
        except Exception:
            adx_val = None
        # Compute sweep_ticks from ATR (if not overridden)
        st = sweep_ticks
        if st is None:
            st = _sweep_ticks(window, 0.25, atr_frac)
        # Try each level (only recent levels, last 5 days)
        # Stale levels from 30 days ago are not meaningful support/resistance.
        from datetime import datetime
        from .market import CT as _CT
        current_day = datetime.fromtimestamp(bars[i]['ts'], _CT).date()
        recent_levels = []
        for lvl in levels:
            if len(lvl) == 3:
                price, kind, day = lvl
                # Only use levels from last 5 days
                if (current_day - day).days <= 5:
                    recent_levels.append((price, kind))
            else:
                # Backward compat: levels without day (shouldn't happen)
                recent_levels.append((lvl[0], lvl[1]))
        sig_found = None
        for price, kind in recent_levels:
            # Determine direction from kind
            k = (kind or '').lower()
            if 'low' in k:
                directions = ('long',)
            elif 'high' in k:
                directions = ('short',)
            else:
                directions = ('long', 'short')
            for direction in directions:
                # Opposite levels for target (also only recent)
                if direction == 'long':
                    opposing = [p for p, k2 in recent_levels if k2.startswith('session_high')]
                else:
                    opposing = [p for p, k2 in recent_levels if k2.startswith('session_low')]
                sig = detect_sweep(
                    window, price, direction, sweep_ticks=st,
                    confirm_bars=confirm_bars, level_kind=kind,
                    opposing=opposing, min_rr=min_rr, buffer_mult=buffer_mult)
                if sig:
                    # FIX (2026-10-18) STALE SIGNALS: Only accept freshly-confirmed signals.
                    # Old code accepted signals hours after signal_ts, modeling fills
                    # at hours-old prices. Require signal_ts == current bar's ts.
                    if sig.get('signal_ts') != bars[i]['ts']:
                        continue
                    # FIX (2026-10-18) DUPLICATES: One sweep event → one trade.
                    # Use sweep_ts (not level_price) so nearby levels swept by the
                    # same price action don't each fire a separate signal.
                    sweep_key = (sig.get('sweep_ts', sig['signal_ts']), sig['direction'])
                    if sweep_key in seen_sweeps:
                        continue
                    seen_sweeps.add(sweep_key)
                    sig_found = sig
                    break
            if sig_found:
                break
        if sig_found:
            sig = sig_found
            # Record signal
            signals.append({
                'signal_ts': sig['signal_ts'],
                'direction': sig['direction'],
                'entry': sig['entry'],
                'stop': sig['stop'],
                'target': sig['target'],
                'level_price': sig['level_price'],
                'rr': sig['rr'],
                'adx': adx_val,
            })
            # Simulate trade: entry at next bar (i+1)
            # Find entry bar index (first bar with ts > signal_ts)
            entry_idx = None
            for j in range(i+1, len(bars)):
                if bars[j]['ts'] > sig['signal_ts']:
                    entry_idx = j
                    break
            if entry_idx is None:
                i += 1
                continue
            result = _simulate_trade(entry_idx, bars, sig, symbol)
            if result:
                trades.append({
                    'entry_ts': sig['signal_ts'],
                    'entry': sig['entry'],
                    'stop': sig['stop'],
                    'target': sig['target'],
                    'direction': sig['direction'],
                    'level_price': sig['level_price'],
                    **result,
                })
                # Skip ahead to after this trade exits (no overlapping)
                i = entry_idx + result['bars_held'] + 1
            else:
                i += 1
        else:
            i += 1
    # Calculate summary
    if not trades:
        summary = {'trades': 0, 'pnl': 0.0, 'win_rate': 0.0, 'wins': 0, 'losses': 0}
    else:
        pnls = [t['pnl'] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total = sum(pnls)
        # Max drawdown
        equity = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            equity += p
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        # Sharpe (assuming daily bars, 252 trading days/year, risk-free 0)
        # For intraday, this is approximate.
        import statistics
        if len(pnls) > 1:
            avg = statistics.mean(pnls)
            stdev = statistics.stdev(pnls)
            sharpe = (avg / stdev * (252 ** 0.5)) if stdev > 0 else 0
        else:
            sharpe = 0
        summary = {
            'trades': len(trades),
            'pnl': round(total, 2),
            'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
            'wins': len(wins),
            'losses': len(losses),
            'avg_win': round(sum(wins) / len(wins), 2) if wins else 0,
            'avg_loss': round(sum(losses) / len(losses), 2) if losses else 0,
            'max_drawdown': round(max_dd, 2),
            'sharpe': round(sharpe, 2),
            'profit_factor': round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 0,
        }
    return {'trades': trades, 'summary': summary, 'signals': signals}


def backtest_aoi_zones(bars, symbol='MES.c.0', session_start_hour=8, session_end_hour=15,
                       stop_atr_mult=0.5, disp_atr_mult=1.5):
    """Backtest AOI Zones on historical bars.

    Args:
        bars: List of {ts, o, h, l, c} dicts, ascending by ts.
        symbol: Symbol for P&L calculation.
        session_start_hour/end_hour: Only trade during this session (CT).
            Default 8-15 (post-2026-10-17 fix: session filter).
        stop_atr_mult: Stop buffer as ATR multiple (default 0.5, post-fix).
        disp_atr_mult: Displacement bar ATR multiple (default 1.5).

    Returns:
        Dict with 'trades', 'summary', 'signals'.
    """
    from .aoi_zones import find_order_block, _ob_trade_levels
    from datetime import datetime
    from .market import CT
    if not bars or len(bars) < 30:
        return {'trades': [], 'summary': {'error': 'insufficient bars'}, 'signals': []}
    trades = []
    signals = []
    # Use while loop (not for) so we can skip ahead after trades.
    i = 30
    # Track last OB formed_ts to avoid duplicate signals on same OB
    last_ob_ts = None
    while i < len(bars):
        # Session filter (post-2026-10-17 fix): only trade 08:00-15:00 CT
        dt = datetime.fromtimestamp(bars[i]['ts'], CT)
        if not (session_start_hour <= dt.hour < session_end_hour):
            i += 1
            continue
        window = bars[:i+1]
        # Find order block in last 400 bars
        ob = find_order_block(window[-400:], disp_atr_mult=disp_atr_mult)
        if not ob:
            i += 1
            continue
        # Deduplicate: skip if same OB as last signal
        if ob.get('formed_ts') == last_ob_ts:
            i += 1
            continue
        last_ob_ts = ob.get('formed_ts')
        # Get trade levels (with 0.5x ATR stop, post-fix)
        # Note: _ob_trade_levels uses 0.5x ATR after 2026-10-17 fix
        levels = _ob_trade_levels(ob, window[-1]['c'])
        if not levels:
            i += 1
            continue
        signals.append({
            'signal_ts': bars[i]['ts'],
            'direction': levels['direction'],
            'entry': levels['entry'],
            'stop': levels['stop'],
            'target': levels['target'],
            'level_price': levels['level_price'],
            'ob': ob,
        })
        # Simulate trade: entry at next bar
        entry_idx = i + 1
        if entry_idx >= len(bars):
            i += 1
            continue
        result = _simulate_trade(entry_idx, bars, levels, symbol)
        if result:
            trades.append({
                'entry_ts': bars[i]['ts'],
                'entry': levels['entry'],
                'stop': levels['stop'],
                'target': levels['target'],
                'direction': levels['direction'],
                **result,
            })
            # Skip ahead (one position at a time)
            i = entry_idx + result['bars_held'] + 1
        else:
            i += 1
    # Summary (same as Turtle Soup)
    if not trades:
        summary = {'trades': 0, 'pnl': 0.0, 'win_rate': 0.0, 'wins': 0, 'losses': 0}
    else:
        pnls = [t['pnl'] for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p <= 0]
        total = sum(pnls)
        equity = 0
        peak = 0
        max_dd = 0
        for p in pnls:
            equity += p
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd
        import statistics
        if len(pnls) > 1:
            avg = statistics.mean(pnls)
            stdev = statistics.stdev(pnls)
            sharpe = (avg / stdev * (252 ** 0.5)) if stdev > 0 else 0
        else:
            sharpe = 0
        summary = {
            'trades': len(trades),
            'pnl': round(total, 2),
            'win_rate': round(len(wins) / len(trades) * 100, 1) if trades else 0,
            'wins': len(wins),
            'losses': len(losses),
            'avg_win': round(sum(wins) / len(wins), 2) if wins else 0,
            'avg_loss': round(sum(losses) / len(losses), 2) if losses else 0,
            'max_drawdown': round(max_dd, 2),
            'sharpe': round(sharpe, 2),
            'profit_factor': round(sum(wins) / abs(sum(losses)), 2) if losses and sum(losses) != 0 else 0,
        }
    return {'trades': trades, 'summary': summary, 'signals': signals}
