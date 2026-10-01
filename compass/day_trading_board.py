"""Day Trading Board: the morning watchlist generator.

REVIVED 2026-10-01 — rebuilt on alternative feeds after the vendor research
keys (research:earnings, research:economic_calendar, research:sector_dashboard)
proved permanently empty (board scored all zeros 09-30/10-01). New sources:
sector performance from sector-ETF daily bars (day_board_feeds.py, zero new
dependencies), a static 2026 economic calendar, and an FMP earnings feed
(stubbed until FMP_API_KEY is set — the board degrades gracefully without it).

Built at 08:30 CT, refreshed at 09:00 CT, frozen after the open. A ranked,
transparent checklist of the names with the most going on today — catalysts,
movers, flow, sector tailwind, and names already on our radar.

This is a watchlist, NOT a signal: no entry, no direction call, no alert
fires from the board itself. The A+ alert gate (alerts only for on-board
names) is specified but deliberately NOT wired in this version — it stays
off until the board's measurement (§4 review) earns it.

Evening review: at session close each board name is checked for (a) any
scanner candidate, (b) any tape-confirmed setup, (c) any alert, and (d) its
day range. The weekly table reports the board hit rate and the honest check:
what fraction of the day's alerts came from off-board names.
"""
from .market import number, session, day
from .gap_continuation import sector_pillar

MOVER_PCT = 0.02
FLOW_MIN_PREMIUM = 250000
FLOW_WINDOW = 3600
SCAN_THROTTLE = 60
BOARD_LIMIT = 15


def _today_str(now):
    return day(now)


def _prior_close(db, c, symbol, today):
    closes = []
    for r in db.recent(c, 'daily', symbol, limit=5):
        p = r.get('payload') or {}
        if day(r['ts']) < today and number(p.get('c')) is not None:
            closes.append((r['ts'], number(p['c'])))
    return sorted(closes)[-1][1] if closes else None


def _ref_price(db, c, symbol, now):
    q = db.get(c, 'quote:' + symbol) or {}
    if number(q.get('bid')) is not None and number(q.get('ask')) is not None:
        return (number(q['bid']) + number(q['ask'])) / 2
    bars = db.recent(c, 'bar', symbol, limit=1)
    if bars:
        return number((bars[0].get('payload') or {}).get('c'))
    return None


def _earnings_component(earnings_feed, symbol, today):
    """+2 reports during market hours, +1 before-open/after-close."""
    items = (earnings_feed or {}).get('items') or []
    for row in items:
        if not isinstance(row, dict):
            continue
        sym = row.get('symbol') or row.get('ticker')
        if str(sym or '').upper() != symbol.upper():
            continue
        date = str(row.get('date') or row.get('earnings_date') or '')
        if today not in date:
            continue
        timing = str(row.get('timing') or row.get('time') or row.get('when') or '').lower()
        if 'dur' in timing:  # during market hours
            return {'score': 2, 'detail': {'timing': timing or 'during'}}
        return {'score': 1, 'detail': {'timing': timing or 'unknown'}}
    return {'score': 0, 'detail': {}}


def _economic_component(cal_feed, sector, today):
    """+1 when a high-impact release is scheduled today and sector is known."""
    items = (cal_feed or {}).get('items') or (cal_feed or {}).get('events') or []
    names = []
    for row in items:
        if not isinstance(row, dict):
            continue
        date = str(row.get('date') or row.get('day') or '')
        if today not in date:
            continue
        impact = str(row.get('impact') or row.get('importance') or '').lower()
        if 'high' in impact or impact.strip() == '3':
            names.append(row.get('name') or row.get('event') or 'release')
    if names and sector:
        return {'score': 1, 'detail': {'releases': names[:5], 'sector': sector}}
    return {'score': 0, 'detail': {'releases': names[:5]} if names else {}}


def _flow_component(flow_rows, symbol, now, day_pct):
    """+1 when same-direction net premium >= $250k in the last 60 minutes."""
    start = now - FLOW_WINDOW
    call_premium = put_premium = 0.0
    for row in flow_rows or []:
        if not isinstance(row, dict) or row.get('symbol') != symbol:
            continue
        stamp = number(row.get('source_ts'))
        if stamp is None or stamp < start:
            continue
        premium = number(row.get('premium')) or 0
        opt = str(row.get('option_type') or '').lower()
        if opt in ('call', 'c'):
            call_premium += premium
        elif opt in ('put', 'p'):
            put_premium += premium
    if day_pct is None or abs(day_pct) < 0.005:
        directional = max(call_premium, put_premium)
    else:
        directional = call_premium if day_pct > 0 else put_premium
    if directional >= FLOW_MIN_PREMIUM:
        return {'score': 1, 'detail': {'directional_premium': directional,
                                       'call_premium': call_premium,
                                       'put_premium': put_premium}}
    return {'score': 0, 'detail': {}}


def _radar_component(db, c, symbol, today, now):
    """+1 when the symbol is already on our radar today."""
    reasons = []
    for event in db.recent(c, 'alert', limit=500):
        p = event.get('payload') or {}
        if (event.get('symbol') == symbol and p.get('status') == 'setup_triggered'
                and day(event.get('ts', 0)) == today):
            reasons.append('scanner_candidate')
            break
    magnet = (db.get(c, 'apex_magnet:latest', {}) or {}).get(symbol)
    if magnet:
        reasons.append('apex_magnet')
    gap = db.get(c, 'gap_cont:%s:%s' % (symbol, today), {})
    if gap and gap.get('stage') not in (None, 'excluded', 'pending'):
        reasons.append('gap_continuation')
    if reasons:
        return {'score': 1, 'detail': {'reasons': reasons}}
    return {'score': 0, 'detail': {}}


def score_symbol(db, c, symbol, now, ctx):
    """Transparent per-component scorecard for one symbol."""
    today = _today_str(now)
    prior_close = _prior_close(db, c, symbol, today)
    ref = _ref_price(db, c, symbol, now)
    day_pct = (ref - prior_close) / prior_close if ref and prior_close else None
    sector = None
    try:
        from .apex_magnet import _sector_for
        sector = _sector_for(symbol, ctx.get('sector_map') or {})
    except Exception:
        sector = None
    direction = 'up' if (day_pct or 0) >= 0 else 'down'
    sector_read = sector_pillar(ctx.get('sector_dashboard'), ctx.get('sector_map'),
                                symbol, direction)
    components = {
        'earnings': _earnings_component(ctx.get('earnings'), symbol, today),
        'economic': _economic_component(ctx.get('economic_calendar'), sector, today),
        'mover': {'score': 1 if day_pct is not None and abs(day_pct) >= MOVER_PCT else 0,
                  'detail': {'day_pct': day_pct} if day_pct is not None else {}},
        'flow': _flow_component(ctx.get('flow_rows'), symbol, now, day_pct),
        'sector': {'score': 1 if sector_read['status'] == 'supportive' else 0,
                   'detail': sector_read['detail']},
        'radar': _radar_component(db, c, symbol, today, now),
    }
    total = sum(comp['score'] for comp in components.values())
    return {'symbol': symbol, 'total': total, 'day_pct': day_pct,
            'components': components}


def build_board(db, c, cfg, now, ctx):
    # MU explicitly included per Josh (2026-10-01) — ensures it's scored even
    # if the STOCK_SYMBOLS env var drifts from the 15-stock expansion.
    universe = list(dict.fromkeys([*cfg.stocks, 'MU']))
    rows = [score_symbol(db, c, symbol, now, ctx) for symbol in universe]
    rows.sort(key=lambda r: (-r['total'], -(abs(r['day_pct'] or 0))))
    return {'day': _today_str(now), 'built_at': now, 'refreshed_at': now,
            'frozen': False, 'frozen_at': None, 'rows': rows[:BOARD_LIMIT],
            'universe': len(rows)}


def scan(db, c, cfg, now):
    """Cadence driver: build 08:30 CT, refresh 09:00 CT, freeze at the open."""
    if not getattr(cfg, 'day_trading_board', True):
        return {'ran': False, 'reason': 'disabled'}
    today = _today_str(now)
    hours = session(today)
    if not hours:
        return {'ran': False, 'reason': 'no_session'}
    if now - db.get(c, 'day_board:scanned_at', 0) < SCAN_THROTTLE:
        return {'ran': False, 'reason': 'throttled'}
    sess_open = hours[0]
    t_build, t_refresh = sess_open - 3600, sess_open - 1800
    key = 'board:' + today
    board = db.get(c, key)
    # Alternative feeds (day_board_feeds.py): sector rotation from sector-ETF
    # daily bars, static 2026 economic calendar, FMP earnings (stubbed without
    # FMP_API_KEY). Flow rows still come from the matrix unusual-activity feed.
    # The dead vendor research keys are no longer read.
    from .day_board_feeds import build_ctx as _day_board_feeds_ctx
    feed_ctx = _day_board_feeds_ctx(db, c)
    ctx = {'flow_rows': (db.get(c, 'matrix:unusual_activity', {}) or {}).get('rows') or [],
           'sector_dashboard': feed_ctx['sector_dashboard'],
           'sector_map': feed_ctx['sector_map'],
           'earnings': feed_ctx['earnings'],
           'economic_calendar': feed_ctx['economic_calendar']}
    action = None
    if board is None and now >= t_build:
        board = build_board(db, c, cfg, now, ctx)
        action = 'built'
    elif board is not None and not board.get('frozen') and now >= t_refresh \
            and board.get('refreshed_at', 0) < t_refresh:
        built_at = board.get('built_at', now)
        board = build_board(db, c, cfg, now, ctx)
        board['built_at'] = built_at
        action = 'refreshed'
    elif board is not None and not board.get('frozen') and now >= sess_open:
        board['frozen'] = True
        board['frozen_at'] = now
        action = 'frozen'
    if board is not None and action:
        db.put(c, key, board)
    review_action = None
    if board is not None and board.get('frozen') and now >= hours[1]:
        review = evening_review(db, c, cfg, now)
        review_action = 'reviewed' if review['ran'] else None
    db.put(c, 'day_board:scanned_at', now)
    return {'ran': True, 'action': action, 'review': review_action,
            'symbols': len(board['rows']) if board else 0}


def evening_review(db, c, cfg, now):
    """At session close: did board names produce candidates/setups/alerts?"""
    today = _today_str(now)
    board = db.get(c, 'board:' + today)
    if not board:
        return {'ran': False, 'reason': 'no_board'}
    if db.get(c, 'board_review:' + today):
        return {'ran': False, 'reason': 'already_reviewed'}
    alerts_today = {}
    for event in db.recent(c, 'alert', limit=2000):
        if day(event.get('ts', 0)) != today:
            continue
        alerts_today.setdefault(event.get('symbol'), []).append(event)
    tape_today = set()
    for event in db.recent(c, 'tape_confirmation', limit=500):
        if day(event.get('ts', 0)) == today:
            tape_today.add(event.get('symbol'))
    board_names = {r['symbol'] for r in board['rows']}
    names, off_board_alerts, total_alerts = [], 0, 0
    for symbol, events in alerts_today.items():
        total_alerts += len(events)
        if symbol not in board_names:
            off_board_alerts += len(events)
    for row in board['rows']:
        symbol = row['symbol']
        events = alerts_today.get(symbol, [])
        candidates = [e for e in events
                      if (e.get('payload') or {}).get('status') == 'setup_triggered']
        day_bars = [b for b in db.recent(c, 'bar', symbol, limit=500)
                    if day(b['ts']) == today]
        closes = [number((b.get('payload') or {}).get('c')) for b in day_bars]
        closes = [x for x in closes if x]
        names.append({'symbol': symbol, 'total': row['total'],
                      'had_candidate': bool(candidates),
                      'had_tape_confirmation': symbol in tape_today,
                      'had_alert': bool(events),
                      'day_range_pct': ((max(closes) - min(closes)) / min(closes)
                                        if len(closes) > 1 else None)})
    review = {'day': today, 'reviewed_at': now, 'names': names,
              'board_hit_rate': (sum(1 for n in names if n['had_candidate']) / len(names)
                                 if names else None),
              'off_board_alert_fraction': (off_board_alerts / total_alerts
                                           if total_alerts else None)}
    db.put(c, 'board_review:' + today, review)
    return {'ran': True, 'names': len(names)}


def display(db, c, now, days=7):
    """Today's board plus the weekly review table."""
    today = _today_str(now)
    board = db.get(c, 'board:' + today) or {}
    reviews = []
    for key, review in db.prefix(c, 'board_review:').items():
        if isinstance(review, dict) and review.get('day') != today:
            reviews.append(review)
    reviews = sorted(reviews, key=lambda r: r['day'], reverse=True)[:days]
    table = []
    for r in reviews:
        table.append({'day': r['day'], 'names': len(r['names']),
                      'board_hit_rate': r.get('board_hit_rate'),
                      'off_board_alert_fraction': r.get('off_board_alert_fraction')})
    return {'asof': now, 'board': board.get('rows', []),
            'frozen': board.get('frozen'), 'frozen_at': board.get('frozen_at'),
            'built_at': board.get('built_at'),
            'weekly_reviews': table,
            'note': 'Watchlist only — no entries, no direction calls, no alerts. '
                    'The A+ alert gate is specified but not wired.'}
