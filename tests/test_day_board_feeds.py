"""Tests for the day-board alternative feeds prototype."""
import pytest
from compass import day_board_feeds as feeds


def _closes(prices, start_ts=1_000_000):
    return [(start_ts + i * 86400, p) for i, p in enumerate(prices)]


def test_sector_dashboard_bullish_when_outperforming():
    # ETF up 2% 1d / 3% 5d; SPY up 1% 1d / 1% 5d -> bullish
    etf = {'XLK': _closes([100, 101, 102, 101, 102, 103, 105.06])}
    spy = _closes([100, 100.5, 101, 100.8, 101, 101.2, 102.21])
    dash = feeds.sector_dashboard_from_closes(etf, spy)
    tech = dash['sectors']['Information Technology']
    assert tech['flow_direction'] == 'bullish'
    assert tech['rel_1d_pct'] > 0
    assert tech['rel_5d_pct'] > 0
    assert tech['etf'] == 'XLK'


def test_sector_dashboard_bearish_when_underperforming():
    etf = {'XLE': _closes([100, 99, 98, 99, 98, 97, 96])}
    spy = _closes([100, 101, 102, 101, 102, 103, 104])
    dash = feeds.sector_dashboard_from_closes(etf, spy)
    assert dash['sectors']['Energy']['flow_direction'] == 'bearish'


def test_sector_dashboard_neutral_when_mixed():
    # Outperforms 1d but underperforms 5d -> neutral
    etf = {'XLF': _closes([100, 98, 97, 96, 95, 96, 98])}
    spy = _closes([100, 101, 102, 103, 104, 104.5, 105])
    dash = feeds.sector_dashboard_from_closes(etf, spy)
    assert dash['sectors']['Financials']['flow_direction'] == 'neutral'


def test_sector_dashboard_skips_short_history():
    dash = feeds.sector_dashboard_from_closes(
        {'XLK': _closes([100, 101])}, _closes([100, 101]))
    assert dash['sectors'] == {}


def test_sector_dashboard_shape_matches_vendor_contract():
    # Must be readable by gap_continuation.sector_pillar
    from compass.gap_continuation import sector_pillar
    etf = {'XLK': _closes([100, 101, 102, 101, 102, 103, 105.06])}
    spy = _closes([100, 100.5, 101, 100.8, 101, 101.2, 102.21])
    dash = feeds.sector_dashboard_from_closes(etf, spy)
    read = sector_pillar(dash, {'NVDA': 'Information Technology'},
                         'NVDA', 'up')
    assert read['status'] == 'supportive'


def test_economic_calendar_has_known_events():
    cal = feeds.static_economic_calendar()
    items = cal['items']
    assert len(items) >= 30
    # NFP the day after prototype date
    nfp = [e for e in items
           if e['name'] == 'Jobs report (NFP)' and e['date'] == '2026-10-02']
    assert len(nfp) == 1
    assert nfp[0]['impact'] == 'high'


def test_economic_calendar_readable_by_board():
    from compass.day_trading_board import _economic_component
    cal = feeds.static_economic_calendar()
    comp = _economic_component(cal, 'Information Technology', '2026-10-02')
    assert comp['score'] == 1
    assert 'Jobs report (NFP)' in comp['detail']['releases']
    # Non-event day scores zero
    comp2 = _economic_component(cal, 'Information Technology', '2026-10-01')
    assert comp2['score'] == 0


def test_earnings_stub_without_key(monkeypatch):
    monkeypatch.delenv('FMP_API_KEY', raising=False)
    feed = feeds.earnings_feed()
    assert feed['items'] == []
    assert feed['source'] == 'stub'
    assert 'FMP_API_KEY' in feed['note']


def test_symbol_sector_map_covers_watchlist():
    watchlist = ['NVDA', 'AAPL', 'TSLA', 'MU', 'AMD', 'META', 'AMZN',
                 'MSFT', 'GOOGL', 'NFLX', 'AVGO', 'PLTR']
    for sym in watchlist:
        assert sym in feeds.SYMBOL_SECTOR, sym
    # All mapped sectors have a corresponding ETF
    etf_sectors = set(feeds.ETF_SECTOR.values())
    for sector in feeds.SYMBOL_SECTOR.values():
        assert sector in etf_sectors, sector


def test_sector_etfs_are_collection_only():
    """The 11 sector ETFs feed the board's sector rotation read and nothing
    else: they must never enter the scanner/trading universes (cfg.stocks,
    cfg.watch_symbols)."""
    from compass.config import Config
    cfg = Config(local=True)
    assert len(cfg.sector_etfs) == 11
    assert set(cfg.sector_etfs) == set(feeds.SECTOR_ETFS)
    assert not (set(cfg.sector_etfs) & set(cfg.stocks)), "ETF leaked into trading universe"
    assert not (set(cfg.sector_etfs) & set(cfg.watch_symbols)), "ETF leaked into watchlist"


def test_dayboard_project_gates_daily_only():
    """The dayboard collection project admits daily-bar collection but a
    dayboard-only symbol is excluded from minute-bar collection."""
    from compass import secondary_data
    assert "dayboard" in secondary_data.DAILY_COLLECTION_PROJECTS
    # dayboard-only -> excluded from minute jobs
    assert not ({"dayboard"} - secondary_data.DAYBOARD_ONLY_PROJECTS)
    # dayboard + another project -> still eligible for minute jobs
    assert {"dayboard", "swing"} - secondary_data.DAYBOARD_ONLY_PROJECTS
    assert {"dayboard", "smoothers"} - secondary_data.DAYBOARD_ONLY_PROJECTS
