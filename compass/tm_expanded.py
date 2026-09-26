"""Additional documented TM evidence; collection only, never scanner admission."""
from .universe import EXCLUDED_STOCKS, symbols

# Cadences are targets, behind a shared two-request/minute expansion budget.
GLOBAL = (
    ('accumulation', 'Accumulation watch', '/screeners/accumulation-watch/run', 900),
    ('screener_catalog', 'Screener definitions', '/screeners', 86400),
    ('put_call', 'Index put/call ratios', '/put-call-ratios', 900),
    ('sector_flow', 'Sector flow totals', '/sectors/flow?window=1d', 900),
    ('sector_map', 'Sector mappings', '/sectors/etfs', 86400),
    ('sector_analysis', 'Sector technical states', '/sectors/analysis', 900),
    ('market_map', 'Ticker flow map', '/sectors/market-map', 900),
    ('event_odds', 'Market-implied event odds', '/economic-calendar/predictions', 1800),
    ('intrashop', 'Within-fund divergences', '/institutional/divergences/intrashop', 21600),
    ('etf_positioning', 'ETF options positioning', '/institutional/etf-options', 3600),
    ('quality_universe', 'Quality score universe', '/long-term/quality-score', 86400),
)
DETAIL = (
    ('gex', 'Gamma detail', '/gex/{s}', 1800),
    ('apex_history', 'Intraday Apex history', '/gex/{s}/apex/history', 1800),
    ('apex_trend', 'Daily Apex trend', '/gex/{s}/apex/trend?days=30', 21600),
    ('apex_expiries', 'Apex across expirations', '/gex/{s}/apex/evolution', 3600),
    ('repeat_detail', 'Repeated flow detail', '/unusual-activity/repeats/{s}', 1800),
    ('ticker_flow', 'Ticker flow summary', '/flow/ticker/{s}', 1800),
    ('earnings_detail', 'Ticker earnings positioning', '/earnings-flow/flow/{s}', 3600),
    ('ticker_detail', 'Ticker analytics', '/ticker/{s}', 1800),
    ('quality_detail', 'Ticker fundamental quality', '/long-term/quality/{s}', 86400),
    ('institutional_history', 'Institutional history', '/institutional/{s}/history', 21600),
    ('divergence_history', 'Divergence history', '/institutional/divergences/{s}/history', 21600),
    ('congress_history', 'Congressional ticker history', '/politician-trades/by-ticker/{s}', 86400),
)
SECTORS = ('XLK','XLF','XLE','XLV','XLY','XLP','XLI','XLB','XLU','XLRE','XLC')


def expanded_feeds(Feed, focus):
    rows = [Feed('extra_'+k,label,path,interval,0,'archive',True) for k,label,path,interval in GLOBAL]
    # Bounded focused symbols; no arbitrary paths, index aliases, or excluded names.
    selected = [s for s in symbols(focus) if s not in EXCLUDED_STOCKS][:12]
    for s in selected:
        rows.extend(Feed('extra_'+k+'_'+s,s+' '+label,path.format(s=s),interval,0,'archive',True)
                    for k,label,path,interval in DETAIL)
    # Provider documents this history only for tracked names: keep a known core.
    for s in ('SPX','SPY','QQQ'):
        rows.append(Feed('extra_gex_history_'+s,s+' GEX history','/gex/'+s+'/historical',3600,0,'archive',True))
    for s in SECTORS:
        rows.append(Feed('extra_sector_flows_'+s,s+' constituent flow','/sectors/'+s+'/flows',3600,0,'archive',True))
    return rows
