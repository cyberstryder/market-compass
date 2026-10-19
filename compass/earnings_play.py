"""
Earnings Play Strategy.

Trades post-earnings gap momentum on OPRA tickers.

How it works:
1. OPRA ticker reports earnings AMC (after market close) or BMO (before market open)
2. Stock gaps overnight (AMC) or pre-market (BMO) on the earnings news
3. Next morning, if gap > 3% (significant move), trade the momentum direction:
   - Gap up > 3% -> Buy CALL, 0DTE, ATM
   - Gap down < -3% -> Buy PUT, 0DTE, ATM
4. Entry: After opening range (9:45 AM CT) to confirm momentum holds
5. Stop: Below gap low (CALL) / above gap high (PUT)
6. Target: 2R or 50% option gain
7. Exit: 2:55 PM CT (0DTE flatten)

Why post-earnings momentum (not pre-earnings):
- Pre-earnings direction is unpredictable (50/50 coin flip)
- Pre-earnings IV is inflated (options expensive, IV crush kills you)
- Post-earnings, direction is known (gap tells you)
- Post-earnings IV is crushed (options cheaper)
- Momentum often continues after earnings gap (institutions repositioning)

Filters:
- Gap must be > 3% (filters out noise, only trade significant moves)
- Must be OPRA ticker (liquid options, tight spreads)
- Must have earnings in last 24 hours (AMC last night or BMO this morning)
- Only 1 earnings play per ticker per day (no re-entry)
- Max 3 simultaneous earnings plays (respects 3-position limit)

Paper trading: Uses existing 0DTE infrastructure (1 contract, +50%/-50% or 2:55 PM CT).
"""
import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

log = logging.getLogger(__name__)
CT = ZoneInfo("America/Chicago")

# Minimum gap to trade (3%)
MIN_GAP_PCT = 3.0

# Entry delay after open (minutes) - wait for opening range to form
ENTRY_DELAY_MIN = 15  # Enter at 9:45 AM CT (15 min after 9:30 open)

# Max simultaneous earnings plays
MAX_POSITIONS = 3

def _bars_ascending(db, c, symbol, limit, since):
    """Plain bar dicts in ascending time order.

    db.recent() returns newest-first event rows with the bar nested under
    'payload' — reading b['c']/b['o'] off the row itself raises KeyError,
    which crashed this scan silently on every tick (engine catches it) so
    the strategy never fired (found 2026-10-19).
    """
    bars = []
    for r in db.recent(c, "bar", symbol, limit=limit, since=since):
        p = r.get('payload') or {}
        if not isinstance(p, dict):
            continue
        bars.append(dict(p, ts=r.get('ts', p.get('ts'))))
    bars.sort(key=lambda b: b.get('ts') or 0)
    return bars

def get_earnings_gap(db, c, symbol, now):
    """
    Check if symbol had earnings in last 24 hours and calculate gap.
    
    Returns: {
        'had_earnings': bool,
        'earnings_date': str (YYYY-MM-DD),
        'earnings_time': str (AMC/BMO),
        'prev_close': float,
        'open_price': float,
        'gap_pct': float,
        'gap_direction': 'up'/'down'/'flat',
    } or None if no earnings or no price data.
    """
    # Get earnings from intel cache
    today_str = datetime.fromtimestamp(now, CT).strftime("%Y-%m-%d")
    yesterday_str = (datetime.fromtimestamp(now, CT) - timedelta(days=1)).strftime("%Y-%m-%d")
    
    # Check both today and yesterday's earnings cache (AMC earnings from last night
    # are in yesterday's cache, BMO earnings from this morning are in today's)
    earnings_event = None
    for date_str in [today_str, yesterday_str]:
        data = db.get(c, f"intel:earnings:{date_str}")
        if data:
            for ev in data.get("opra_earnings", []):
                if ev.get("symbol") == symbol:
                    # Check if earnings were recent (last 24 hours)
                    # AMC yesterday = gaps overnight, trade today
                    # BMO today = gaps pre-market, trade today
                    if date_str == yesterday_str and ev.get("time") == "AMC":
                        earnings_event = ev
                        break
                    elif date_str == today_str and ev.get("time") == "BMO":
                        earnings_event = ev
                        break
    if not earnings_event:
        return None
    
    # Get previous close and today's open from bars (ascending plain dicts;
    # enough rows to span the prior session, not just the newest few)
    bars = _bars_ascending(db, c, symbol, limit=2000, since=now - 3*86400)
    if len(bars) < 2:
        return None
    
    # Previous close = last bar from yesterday (or last bar before today)
    # Open = first bar of today
    today_start = datetime.fromtimestamp(now, CT).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    prev_bars = [b for b in bars if b['ts'] < today_start]
    today_bars = [b for b in bars if b['ts'] >= today_start]
    
    if not prev_bars or not today_bars:
        return None
    
    prev_close = prev_bars[-1]['c']
    open_price = today_bars[0]['o']
    
    gap_pct = (open_price - prev_close) / prev_close * 100
    gap_direction = 'up' if gap_pct > 0 else 'down' if gap_pct < 0 else 'flat'
    
    return {
        'had_earnings': True,
        'earnings_date': earnings_event.get('date'),
        'earnings_time': earnings_event.get('time'),
        'prev_close': prev_close,
        'open_price': open_price,
        'gap_pct': gap_pct,
        'gap_direction': gap_direction,
    }

def should_trade_earnings_gap(gap_info):
    """
    Check if earnings gap meets trading criteria.
    Returns (should_trade, reason).
    """
    if not gap_info or not gap_info.get('had_earnings'):
        return False, 'no_earnings'
    gap_pct = abs(gap_info.get('gap_pct', 0))
    if gap_pct < MIN_GAP_PCT:
        return False, f'gap_too_small_{gap_pct:.1f}%'
    if gap_info.get('gap_direction') == 'flat':
        return False, 'no_gap'
    return True, 'gap_confirmed'

def build_earnings_signal(symbol, gap_info, bars, now):
    """
    Build earnings play signal from gap info and current bars.
    
    Returns signal dict for 0DTE paper trading, or None.
    Signal format matches what zero_dte_paper expects:
    {
        'symbol': str,
        'direction': 'long'/'short' (for option: CALL/PUT),
        'option_type': 'CALL'/'PUT',
        'entry': float (stock price),
        'stop': float,
        'target': float,
        'strategy': 'earnings-play-v1',
        'gap_pct': float,
        'earnings_date': str,
    }
    """
    gap_pct = gap_info['gap_pct']
    direction = 'long' if gap_pct > 0 else 'short'
    option_type = 'CALL' if gap_pct > 0 else 'PUT'
    
    # Current price = last bar close
    if not bars:
        return None
    current_price = bars[-1]['c']
    
    # Stop: Below gap low (CALL) / above gap high (PUT)
    # Gap low = min(prev_close, open_price) for gap up
    # Gap high = max(prev_close, open_price) for gap down
    prev_close = gap_info['prev_close']
    open_price = gap_info['open_price']
    
    if direction == 'long':
        # Stop below the gap (if price falls back into gap, momentum failed)
        gap_low = min(prev_close, open_price)
        stop = gap_low * 0.995  # 0.5% below gap low
        # Target: 2R (risk = current - stop, reward = 2x risk)
        risk = current_price - stop
        target = current_price + 2 * risk
    else:
        # Stop above the gap
        gap_high = max(prev_close, open_price)
        stop = gap_high * 1.005  # 0.5% above gap high
        risk = stop - current_price
        target = current_price - 2 * risk
    
    return {
        'symbol': symbol,
        'direction': direction,
        'option_type': option_type,
        'entry': current_price,
        'stop': stop,
        'target': target,
        'strategy': 'earnings-play-v1',
        'gap_pct': gap_pct,
        'gap_direction': gap_info['gap_direction'],
        'earnings_date': gap_info['earnings_date'],
        'earnings_time': gap_info['earnings_time'],
        'prev_close': prev_close,
        'open_price': open_price,
        'signal_ts': now,
    }

def resolve_0dte_contract(db, c, symbol, option_type, spot, now, max_dte=7):
    """
    Resolve the real listed option contract for an earnings signal.

    Mirrors engine.options() eligibility: fresh chain snapshot (<=120s old),
    matching call/put type, multiplier 100, expiry within [0, max_dte] days.
    Nearest expiry first (most gamma), then strike nearest spot (ATM).

    Returns {'symbol', 'underlying', 'strike', 'type', 'expiry'} or None.
    The earnings alert MUST carry this contract: putting the bare underlying
    ticker in publication.contract.symbol made zero_dte_paper book the stock
    itself as the option (entry = stock ask, exit needs a +-50% stock move)
    and OPRA subscribed a non-contract (2026-10-19 audit finding).
    """
    try:
        chain = db.get(c, "chain:" + symbol) or {}
    except Exception:
        return None
    if not chain or now - (chain.get("asof") or 0) > 120:
        return None
    kind = "call" if option_type == "CALL" else "put"
    today = datetime.fromtimestamp(now, CT).date()
    eligible = []
    for o in chain.get("contracts", []):
        if not isinstance(o, dict):
            continue
        if str(o.get("type") or "").lower() != kind:
            continue
        if o.get("multiplier") != 100:
            continue
        try:
            expiry = datetime.strptime(str(o.get("expiry")), "%Y-%m-%d").date()
        except (ValueError, TypeError):
            continue
        if not 0 <= (expiry - today).days <= max_dte:
            continue
        if not o.get("symbol") or o.get("strike") is None:
            continue
        eligible.append(o)
    if not eligible:
        return None
    eligible.sort(key=lambda o: (str(o.get("expiry")), abs(float(o["strike"]) - spot)))
    best = eligible[0]
    return {
        'symbol': best['symbol'],
        'underlying': symbol,
        'strike': float(best['strike']),
        'type': kind,
        'expiry': str(best.get('expiry')),
    }

def scan_earnings_plays(db, c, cfg, now, symbols):
    """
    Scan all OPRA tickers for earnings gap opportunities.
    
    Called by engine at market open (after ENTRY_DELAY_MIN).
    Returns list of signals.
    
    Args:
        db: Database
        c: Cursor
        cfg: Config
        now: Current timestamp
        symbols: List of symbols to check (OPRA tickers)
    
    Returns: List of signal dicts
    """
    signals = []
    
    # Check if we're in entry window (9:45 AM - 10:30 AM CT)
    # Don't generate signals outside this window (too late to chase momentum)
    dt = datetime.fromtimestamp(now, CT)
    if dt.hour < 9 or (dt.hour == 9 and dt.minute < 45):
        return []  # Too early, wait for opening range
    if dt.hour > 10 or (dt.hour == 10 and dt.minute > 30):
        return []  # Too late, momentum already played out
    
    # Check if we already have max positions
    open_count = 0
    for key in db.prefix(c, "position:"):
        pos = db.get(c, key, {})
        if pos.get('status') == 'open' and pos.get('strategy') == 'earnings-play-v1':
            open_count += 1
    if open_count >= MAX_POSITIONS:
        log.info(f"Earnings play: max positions ({MAX_POSITIONS}) reached, skipping")
        return []
    
    for symbol in symbols:
        # Check if already have open earnings play for this symbol
        pos_key = f"position:earnings:{symbol}"
        existing = db.get(c, pos_key, {})
        if existing.get('status') == 'open':
            continue
        
        # Check if already traded this symbol today (no re-entry)
        today_str = dt.strftime("%Y-%m-%d")
        traded_key = f"earnings_traded:{symbol}:{today_str}"
        if db.get(c, traded_key):
            continue
        
        # Get gap info
        gap_info = get_earnings_gap(db, c, symbol, now)
        if not gap_info:
            continue
        
        # Check if gap meets criteria
        should_trade, reason = should_trade_earnings_gap(gap_info)
        if not should_trade:
            log.debug(f"Earnings play {symbol}: {reason}")
            continue
        
        # Get current bars for entry price (ascending; bars[-1] is latest)
        bars = _bars_ascending(db, c, symbol, limit=30, since=now - 3600)
        if not bars:
            continue
        
        # Build signal
        signal = build_earnings_signal(symbol, gap_info, bars, now)
        if signal:
            # Resolve the actual listed contract BEFORE marking the ticker
            # traded: without a contract the 0DTE book would trade the stock
            # ticker itself. If the chain is not ready yet, leave the ticker
            # unmarked so the next engine tick retries inside the window.
            contract = resolve_0dte_contract(
                db, c, symbol, signal['option_type'], signal['entry'], now,
                max_dte=getattr(cfg, 'option_0dte_max_dte', 7))
            if not contract:
                log.info(f"Earnings play {symbol}: no eligible 0DTE contract in fresh chain, will retry")
                continue
            signal['contract'] = contract
            signals.append(signal)
            # Mark as traded (prevent re-entry today)
            db.put(c, traded_key, {"at": now, "signal": signal})
            log.info(f"Earnings play: {symbol} gap {gap_info['gap_pct']:+.1f}% -> {signal['option_type']} @ ${signal['entry']:.2f}")
    
    return signals
