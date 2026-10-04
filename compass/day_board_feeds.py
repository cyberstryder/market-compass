"""Alternative data feeds for the Day Trading Board (prototype).

Replaces the dead vendor research feeds (research:earnings,
research:economic_calendar, research:sector_dashboard) with:
1. Sector performance computed from Select Sector SPDR ETF quotes
   (data the engine already collects) — zero new dependencies.
2. Earnings dates — STUBBED. Needs a Financial Modeling Prep API key
   (free tier: https://site.financialmodelingprep.com/developer/docs).
   Set FMP_API_KEY env var to enable.
3. Economic calendar — static 2026 schedule, updated a few times per year.

Prototype status (2026-10-01): feeds built and validated with real ETF
data; not wired into the engine tick (DAY_TRADING_BOARD_ENABLED=false).
"""

import json
import os
import urllib.request

# 11 Select Sector SPDR ETFs
SECTOR_ETFS = ('XLK', 'XLF', 'XLE', 'XLV', 'XLY', 'XLP',
               'XLI', 'XLB', 'XLU', 'XLRE', 'XLC')

# ETF -> GICS sector name (matches _sector_for lookup values)
ETF_SECTOR = {
    'XLK': 'Information Technology',
    'XLF': 'Financials',
    'XLE': 'Energy',
    'XLV': 'Health Care',
    'XLY': 'Consumer Discretionary',
    'XLP': 'Consumer Staples',
    'XLI': 'Industrials',
    'XLB': 'Materials',
    'XLU': 'Utilities',
    'XLRE': 'Real Estate',
    'XLC': 'Communication Services',
}

# Static GICS map for the 15-symbol watchlist. Index ETFs have no
# single sector and are intentionally absent.
SYMBOL_SECTOR = {
    'NVDA': 'Information Technology',
    'AAPL': 'Information Technology',
    'MSFT': 'Information Technology',
    'AVGO': 'Information Technology',
    'AMD': 'Information Technology',
    'MU': 'Information Technology',
    'PLTR': 'Information Technology',
    'META': 'Communication Services',
    'GOOGL': 'Communication Services',
    'NFLX': 'Communication Services',
    'AMZN': 'Consumer Discretionary',
    'TSLA': 'Consumer Discretionary',
}

# Major 2026 US economic events. Verify against a real calendar before
# relying on these; update quarterly.
ECONOMIC_CALENDAR_2026 = [
    # FOMC meetings (2-day, decision on day 2)
    {'name': 'FOMC decision', 'date': '2026-01-28', 'impact': 'high'},
    {'name': 'FOMC decision', 'date': '2026-03-18', 'impact': 'high'},
    {'name': 'FOMC decision', 'date': '2026-04-29', 'impact': 'high'},
    {'name': 'FOMC decision', 'date': '2026-06-17', 'impact': 'high'},
    {'name': 'FOMC decision', 'date': '2026-07-29', 'impact': 'high'},
    {'name': 'FOMC decision', 'date': '2026-09-16', 'impact': 'high'},
    {'name': 'FOMC decision', 'date': '2026-10-28', 'impact': 'high'},
    {'name': 'FOMC decision', 'date': '2026-12-09', 'impact': 'high'},
    # CPI releases (monthly)
    {'name': 'CPI', 'date': '2026-01-13', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-02-11', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-03-11', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-04-10', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-05-12', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-06-10', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-07-14', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-08-12', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-09-11', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-10-13', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-11-10', 'impact': 'high'},
    {'name': 'CPI', 'date': '2026-12-10', 'impact': 'high'},
    # Jobs reports (first Friday)
    {'name': 'Jobs report (NFP)', 'date': '2026-01-09', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-02-06', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-03-06', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-04-03', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-05-08', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-06-05', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-07-02', 'impact': 'high'},  # Fri before July 4
    {'name': 'Jobs report (NFP)', 'date': '2026-08-07', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-09-04', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-10-02', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-11-06', 'impact': 'high'},
    {'name': 'Jobs report (NFP)', 'date': '2026-12-04', 'impact': 'high'},
    # Quad witching
    {'name': 'Quad witching', 'date': '2026-03-20', 'impact': 'high'},
    {'name': 'Quad witching', 'date': '2026-06-19', 'impact': 'high'},
    {'name': 'Quad witching', 'date': '2026-09-18', 'impact': 'high'},
    {'name': 'Quad witching', 'date': '2026-12-18', 'impact': 'high'},
]


def sector_dashboard_from_closes(etf_closes, spy_closes):
    """Build a sector_dashboard-shaped dict from ETF daily closes.

    etf_closes: {etf: [(ts, close), ...]} oldest-first, >= 6 sessions.
    spy_closes: [(ts, close), ...] for SPY, same shape.
    Returns {'sectors': {sector_name: {'flow_direction': ..., 'rel_1d_pct': ...,
    'rel_5d_pct': ..., 'etf': ...}}} where flow_direction is bullish when
    the sector outperforms SPY on both 1d and 5d, bearish when it
    underperforms on both, else neutral.
    """
    def ret(closes, n):
        if len(closes) < n + 1:
            return None
        return (closes[-1][1] - closes[-n - 1][1]) / closes[-n - 1][1]

    spy_1d, spy_5d = ret(spy_closes, 1), ret(spy_closes, 5)
    sectors = {}
    for etf, closes in (etf_closes or {}).items():
        sector = ETF_SECTOR.get(etf)
        if not sector:
            continue
        r1, r5 = ret(closes, 1), ret(closes, 5)
        if r1 is None or r5 is None or spy_1d is None or spy_5d is None:
            continue
        rel_1d = (r1 - spy_1d) * 100
        rel_5d = (r5 - spy_5d) * 100
        if rel_1d > 0 and rel_5d > 0:
            direction = 'bullish'
        elif rel_1d < 0 and rel_5d < 0:
            direction = 'bearish'
        else:
            direction = 'neutral'
        sectors[sector] = {'flow_direction': direction,
                           'rel_1d_pct': round(rel_1d, 2),
                           'rel_5d_pct': round(rel_5d, 2),
                           'etf': etf}
    return {'sectors': sectors, 'source': 'sector_etf_quotes'}


def static_economic_calendar():
    """Static 2026 economic calendar in the shape _economic_component reads."""
    return {'items': ECONOMIC_CALENDAR_2026, 'source': 'static_2026'}


def earnings_feed(api_key=""):
    """Earnings calendar feed.

    STUBBED: needs a Financial Modeling Prep API key (free tier).
    Set FMP_API_KEY to enable; returns {'items': [...]} with
    symbol/date/timing rows when configured.
    """
    if not api_key:
        return {'items': [], 'source': 'stub',
                'note': 'Set FMP_API_KEY (free tier at '
                        'site.financialmodelingprep.com) to enable'}
    # Live fetch path: next 7 days of earnings for the watchlist.
    import datetime
    today = datetime.date.today()
    end = today + datetime.timedelta(days=7)
    url = ('https://financialmodelingprep.com/api/v3/earning_calendar'
           '?from=%s&to=%s&apikey=%s' % (today.isoformat(), end.isoformat(),
                                         api_key))
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            rows = json.loads(r.read())
    except Exception as e:
        return {'items': [], 'source': 'fmp', 'error': str(e)}
    items = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        items.append({'symbol': row.get('symbol'),
                      'date': row.get('date'),
                      'timing': row.get('time')})
    return {'items': items, 'source': 'fmp'}


def build_ctx(db, c, cfg=None):
    """Build the board context from alternative feeds (prototype)."""
    from .day_trading_board import _today_str  # noqa
    import time
    today = _today_str(int(time.time()))
    # Sector dashboard from ETF daily bars in the store.
    etf_closes, spy_closes = {}, []
    for etf in SECTOR_ETFS:
        closes = []
        for r in db.recent(c, 'daily', etf, limit=8):
            p = r.get('payload') or {}
            px = p.get('c')
            if isinstance(px, (int, float)):
                closes.append((r['ts'], px))
        if closes:
            etf_closes[etf] = sorted(closes)
    for r in db.recent(c, 'daily', 'SPY', limit=8):
        p = r.get('payload') or {}
        px = p.get('c')
        if isinstance(px, (int, float)):
            spy_closes.append((r['ts'], px))
    spy_closes = sorted(spy_closes)
    return {
        'flow_rows': [],
        'sector_dashboard': sector_dashboard_from_closes(etf_closes, spy_closes),
        'sector_map': dict(SYMBOL_SECTOR),
        'earnings': earnings_feed(getattr(cfg, 'fmp_key', '') if cfg else ''),
        'economic_calendar': static_economic_calendar(),
        '_prototype': True,
        '_etf_coverage': {e: len(v) for e, v in etf_closes.items()},
    }
