import os
from dataclasses import dataclass, field
from .universe import symbols, saved_watchlist, EXCLUDED_STOCKS

def env(k, default=""):
    return os.environ.get(k, default).strip()

@dataclass
class Config:
    storage_budget_gb: float = field(default_factory=lambda: float(env("STORAGE_BUDGET_GB","0")))
    db: str = field(default_factory=lambda: env("DATABASE_URL","sqlite:///compass.db"))
    role: str = field(default_factory=lambda: env("COMPASS_ROLE","all"))
    password: str = field(default_factory=lambda: env("COMPASS_PASSWORD"))
    secret: str = field(default_factory=lambda: env("SESSION_SECRET"))
    local: bool = field(default_factory=lambda: env("COMPASS_LOCAL")=="true")
    stocks: tuple = field(default_factory=lambda: tuple(env("STOCK_SYMBOLS","SPY,QQQ,IWM,NVDA,AAPL").split(",")))
    futures: tuple = field(default_factory=lambda: tuple(env("FUTURES_SYMBOLS","MES.c.0,MNQ.c.0").split(",")))
    extra_futures: tuple = field(default_factory=lambda: tuple(s.strip() for s in env("EXTRA_FUTURES_SYMBOLS","MGC.v.0,GC.v.0,SIL.v.0,SI.v.0,MCL.v.0,CL.v.0,YM.v.0,MYM.v.0").split(",") if s.strip()))
    alpaca_key: str = field(default_factory=lambda: env("APCA_API_KEY_ID"))
    alpaca_secret: str = field(default_factory=lambda: env("APCA_API_SECRET_KEY"))
    feed: str = field(default_factory=lambda: env("ALPACA_FEED","sip"))
    databento: str = field(default_factory=lambda: env("DATABENTO_API_KEY"))
    massive: str = field(default_factory=lambda: env("MASSIVE_API_KEY"))
    matrix: str = field(default_factory=lambda: env("TRADERMATRIX_API_KEY"))
    openai: str = field(default_factory=lambda: env("OPENAI_API_KEY"))
    model: str = field(default_factory=lambda: env("OPENAI_MODEL","gpt-5-mini"))
    discord: str = field(default_factory=lambda: env("DISCORD_WEBHOOK_URL"))
    discord_routes: dict = field(default_factory=lambda: {
        key: env('DISCORD_' + key.upper() + '_WEBHOOK_URL')
        # Condensed 2026-09-28: one route per Discord channel. Dead variables
        # DISCORD_UNUSUAL_OPTIONS_WEBHOOK_URL, DISCORD_SWING_WEBHOOK_URL,
        # DISCORD_SYSTEM_WEBHOOK_URL and DISCORD_EXPOSURE_WEBHOOK_URL are gone.
        for key in ('spy_morning', 'options_0dte', 'intraday', 'magnets', 'options_ideas',
                    'smoothers', 'futures', 'research')})
    discord_fallback: bool = field(default_factory=lambda: env('DISCORD_SHARED_FALLBACK', 'true') == 'true')
    spy_morning_brief: bool = field(default_factory=lambda: env("SPY_MORNING_BRIEF_ENABLED", "true") == "true")
    spy_timeframes_enabled: bool = field(default_factory=lambda: env("SPY_TIMEFRAMES_ENABLED", "false") == "true")
    paper_trading: bool = field(default_factory=lambda: env("PAPER_TRADING_ENABLED", "false") == "true")
    stock_paper_trades: bool = field(default_factory=lambda: env("STOCK_PAPER_TRADES", "false") == "true")
    risk: float = field(default_factory=lambda: float(env("SHADOW_RISK_DOLLARS","100")))
    max_entries: int = field(default_factory=lambda: int(env("SHADOW_MAX_ENTRIES","0")))
    setup_study: bool = field(default_factory=lambda: env("SETUP_STUDY_ENABLED","true")=="true")
    apex_magnet: bool = field(default_factory=lambda: env("APEX_MAGNET_ENABLED","true")=="true")
    apex_magnet_radius: float = field(default_factory=lambda: float(env("APEX_MAGNET_RADIUS","0.02")))
    apex_magnet_tolerance: float = field(default_factory=lambda: float(env("APEX_MAGNET_TOLERANCE","0.001")))
    magnet_push_enabled: bool = field(default_factory=lambda: env("MAGNET_PUSH_ENABLED","false")=="true")
    magnet_push_signals: tuple = field(default_factory=lambda: tuple(
        s.strip() for s in env("MAGNET_PUSH_SIGNALS","broke_through").split(",") if s.strip()))
    tape_confirmed: bool = field(default_factory=lambda: env("TAPE_CONFIRMED_ENABLED","true")=="true")
    # ICT futures concept detectors (Phase 1: evidence-only, all default OFF).
    ict_session_liquidity: bool = field(default_factory=lambda: env("ICT_SESSION_LIQUIDITY_ENABLED","false")=="true")
    ict_htf_levels: bool = field(default_factory=lambda: env("ICT_HTF_LEVELS_ENABLED","false")=="true")
    ict_turtle_soup: bool = field(default_factory=lambda: env("ICT_TURTLE_SOUP_ENABLED","false")=="true")
    ict_smt_divergence: bool = field(default_factory=lambda: env("ICT_SMT_DIVERGENCE_ENABLED","false")=="true")
    ict_aoi_zones: bool = field(default_factory=lambda: env("ICT_AOI_ZONES_ENABLED","false")=="true")
    ict_continuation: bool = field(default_factory=lambda: env("ICT_CONTINUATION_ENABLED","false")=="true")
    ict_trendline_liquidity: bool = field(default_factory=lambda: env("ICT_TRENDLINE_LIQUIDITY_ENABLED","false")=="true")
    ict_tier_a_b: bool = field(default_factory=lambda: env("ICT_TIER_A_B_ENABLED","false")=="true")
    ict_golden_zone: bool = field(default_factory=lambda: env("ICT_GOLDEN_ZONE_ENABLED","false")=="true")
    ict_gz_ema_fast: int = field(default_factory=lambda: int(env("ICT_GZ_EMA_FAST","9")))
    ict_gz_ema_slow: int = field(default_factory=lambda: int(env("ICT_GZ_EMA_SLOW","21")))
    ict_gz_ema_cross_lookback: int = field(default_factory=lambda: int(env("ICT_GZ_EMA_CROSS_LOOKBACK","5")))
    ict_gz_ema_cross: bool = field(default_factory=lambda: env("ICT_GZ_EMA_CROSS_ENABLED","true")=="true")
    ict_bos_fvg: bool = field(default_factory=lambda: env("ICT_BOS_FVG_ENABLED","false")=="true")
    ict_bos_gz_vwap: bool = field(default_factory=lambda: env("ICT_BOS_GZ_VWAP_ENABLED","false")=="true")
    ict_morning_drive: bool = field(default_factory=lambda: env("ICT_MORNING_DRIVE_ENABLED","false")=="true")
    ict_icc: bool = field(default_factory=lambda: env("ICT_ICC_ENABLED","false")=="true")
    ict_rumers_box: bool = field(default_factory=lambda: env("ICT_RUMERS_BOX_ENABLED","false")=="true")
    ict_trend_bias: bool = field(default_factory=lambda: env("ICT_TREND_BIAS_ENABLED","false")=="true")
    ict_trend_bias_sma_len: int = field(default_factory=lambda: int(env("ICT_TREND_BIAS_SMA_LEN","50")))
    ict_futures_paper: bool = field(default_factory=lambda: env("ICT_FUTURES_PAPER_ENABLED","false")=="true")
    ict_symbols: tuple = field(default_factory=lambda: tuple(s.strip() for s in env("ICT_SYMBOLS","MNQ.c.0,MES.c.0,MGC.v.0,SIL.v.0,MCL.v.0").split(",") if s.strip()))
    ict_htf_tradingview: bool = field(default_factory=lambda: env("ICT_HTF_TRADINGVIEW","false")=="true")
    ict_tv_cache_dir: str = field(default_factory=lambda: env("ICT_TV_CACHE_DIR",""))
    ict_smt_pairs: tuple = field(default_factory=lambda: tuple(p.strip() for p in env("ICT_SMT_PAIRS","MNQ.c.0:MES.c.0").split(",") if p.strip()))
    gap_continuation: bool = field(default_factory=lambda: env("GAP_CONTINUATION_ENABLED","true")=="true")
    gap_min_gap: float = field(default_factory=lambda: float(env("GAP_MIN_GAP","0.015")))
    gap_large_caps: str = field(default_factory=lambda: env("GAP_LARGE_CAP_SYMBOLS",""))
    breakouts: bool = field(default_factory=lambda: env("BREAKOUTS_ENABLED","true")=="true")
    day_trading_board: bool = field(default_factory=lambda: env("DAY_TRADING_BOARD_ENABLED","true")=="true")
    option_ideas: bool = field(default_factory=lambda: env("OPTION_IDEAS_ENABLED","true")=="true")
    ideas_alerts: bool = field(default_factory=lambda: env("OPTION_IDEAS_ALERTS","true")=="true")
    ideas_min_dte: int = field(default_factory=lambda: int(env("OPTION_IDEAS_MIN_DTE","1")))
    ideas_max_dte: int = field(default_factory=lambda: int(env("OPTION_IDEAS_MAX_DTE","21")))
    ideas_target_dte: int = field(default_factory=lambda: int(env("OPTION_IDEAS_TARGET_DTE","7")))
    swing_ideas: bool = field(default_factory=lambda: env("SWING_IDEAS_ENABLED","true")=="true")
    swing_alerts: bool = field(default_factory=lambda: env("SWING_IDEAS_ALERTS","true")=="true")
    swing_min_dte: int = field(default_factory=lambda: int(env("SWING_MIN_DTE","14")))
    swing_max_dte: int = field(default_factory=lambda: int(env("SWING_MAX_DTE","60")))
    swing_target_dte: int = field(default_factory=lambda: int(env("SWING_TARGET_DTE","30")))
    swing_hold_sessions: int = field(default_factory=lambda: int(env("SWING_MAX_HOLD_SESSIONS","10")))
    stream_limit: int = field(default_factory=lambda: min(900,max(10,int(env("OPTION_STREAM_CONTRACTS","100")))))
    history_budget: float = field(default_factory=lambda: float(env("FUTURES_HISTORY_BUDGET_USD","0.10")))
    watchlist: tuple = field(default_factory=lambda: symbols(env("WATCH_SYMBOLS")))
    discovery: bool = field(default_factory=lambda: env("DISCOVERY_RESEARCH_ENABLED","false")=="true")
    discovery_seeds: tuple = field(default_factory=lambda: symbols(env("DISCOVERY_SEED_SYMBOLS","PONY,FLY,ENPH,AAOI,DAL,S,TTWO,RIG,CARR,PGEN,SDGR,EWTX,ASX")))
    discovery_limit: int = field(default_factory=lambda: int(env("DISCOVERY_DYNAMIC_LIMIT","50")))
    scanner: bool = field(default_factory=lambda: env("SCANNER_ENABLED","true")=="true")
    scanner_paper: bool = field(default_factory=lambda: env("SCANNER_PAPER_TRADES","true")=="true")
    research: bool = field(default_factory=lambda: env("RESEARCH_FEEDS_ENABLED","true")=="true")
    option_focus: int = field(default_factory=lambda: int(env("OPTION_FOCUS_SYMBOLS","12")))
    chain_dte: int = field(default_factory=lambda: int(env("OPTION_CHAIN_MAX_DTE","90")))
    # 0DTE option entry gates (relaxed 2026-09-30: the 5s quote / 8% spread /
    # 30-min-cutoff trio was rejecting most same-day contracts).
    option_quote_max_age: int = field(default_factory=lambda: int(env("OPTION_QUOTE_MAX_AGE","60")))
    option_max_spread_pct: float = field(default_factory=lambda: float(env("OPTION_MAX_SPREAD_PCT","0.12")))
    option_entry_cutoff_min: int = field(default_factory=lambda: int(env("OPTION_ENTRY_CUTOFF_MIN","15")))
    # 0DTE contract pool: the scanner day-trades short-dated contracts for 0DTE-style
    # moves. 0 = same-day expiry only; 7 (default) includes the nearest weekly so
    # names without daily expirations (e.g. MU) are covered too. Positions still
    # flatten 15 minutes before the close regardless of expiry.
    option_0dte_max_dte: int = field(default_factory=lambda: int(env("OPTION_0DTE_MAX_DTE","7")))
    obsidian_history: bool = field(default_factory=lambda: env("OBSIDIAN_HISTORY_AUDIT_ENABLED","false")=="true")
    obsidian_url: str = field(default_factory=lambda: env("OBSIDIAN_FEED_URL"), repr=False)
    morning_enabled: bool = field(default_factory=lambda: env("MORNING_ORB_ENABLED","true")=="true")
    orb_setups: bool = field(default_factory=lambda: env("ORB_SETUPS_ENABLED","true")=="true")
    morning_url: str = field(default_factory=lambda: env("MORNING_SOURCE_URL"))
    morning_token: str = field(default_factory=lambda: env("MORNING_INTEGRATION_TOKEN"))
    native_morning_intake_token: str = field(default_factory=lambda: env("NATIVE_MORNING_INTAKE_TOKEN"), repr=False)
    smoothers_url: str = field(default_factory=lambda: env("SMOOTHERS_SOURCE_URL"))
    smoothers_token: str = field(default_factory=lambda: env("SMOOTHERS_INTEGRATION_TOKEN"))
    futures_observer_token: str = field(default_factory=lambda: env("FUTURES_OBSERVER_TOKEN"))
    observer_streams: tuple = field(default_factory=lambda: tuple(s for s in env("FUTURES_OBSERVER_STREAMS").split(",") if s))
    secondary: bool = field(default_factory=lambda: env("SECONDARY_REVIEW_ENABLED", "true") == "true")
    secondary_alerts: bool = field(default_factory=lambda: env("SECONDARY_REVIEW_ALERTS", "true") == "true")
    secondary_native: bool = field(default_factory=lambda: env("SECONDARY_REVIEW_COMPASS_FUTURES", "true") == "true")

    @property
    def watch_symbols(self):
        extra = self.watchlist or (saved_watchlist() if not self.local and self.scanner else ())
        return tuple(s for s in symbols((*self.stocks, *extra)) if s not in EXCLUDED_STOCKS)

    def validate(self):
        import math
        if not math.isfinite(self.storage_budget_gb) or self.storage_budget_gb < 0:
            raise ValueError("Invalid storage budget")
        if self.obsidian_url:
            from .obsidian import validate_url
            validate_url(self.obsidian_url)
        if self.role not in {"all","web","collector","engine"}:
            raise ValueError("Invalid COMPASS_ROLE")
        if not self.local and self.db.startswith("sqlite"):
            raise ValueError("Production requires persistent PostgreSQL")
        if self.password and len(self.password)<16:
            raise ValueError("COMPASS_PASSWORD must be at least 16 characters")
        if any(s not in {root+suffix for root in ("MES","MNQ","ES","NQ") for suffix in (".c.0",".v.0")} for s in self.futures):
            raise ValueError("Futures specifications support ES/NQ/MES/MNQ only")
        if any(s not in {r+'.v.0' for r in ('MGC','GC','SIL','SI','MCL','CL','YM','MYM')} for s in self.extra_futures):
            raise ValueError("Additional futures require supported volume-ranked symbols")
        if self.max_entries < 0:
            raise ValueError("SHADOW_MAX_ENTRIES must be zero (unlimited) or positive")
        if self.feed not in {"sip","iex"} or self.risk<=0:
            raise ValueError("Invalid feed or simulation risk")
        if not 0<=self.history_budget<=1: raise ValueError("History recovery budget must be between $0 and $1 cumulative")
        if not 1<=self.ideas_min_dte<=self.ideas_target_dte<=self.ideas_max_dte<=90:
            raise ValueError("Invalid options ideas expiration range")
        if self.option_ideas and self.chain_dte<self.ideas_max_dte:
            raise ValueError("Options chain horizon must cover the ideas expiration range")
        if not 7<=self.swing_min_dte<=self.swing_target_dte<=self.swing_max_dte<=90 or not 1<=self.swing_hold_sessions<=30:
            raise ValueError("Invalid swing expiration range or holding period")
        if self.swing_ideas and self.chain_dte<self.swing_max_dte:
            raise ValueError("Options chain horizon must cover the swing expiration range")
        if not 1<=self.option_focus<=30 or not 1<=self.chain_dte<=180:
            raise ValueError("Invalid options focus or expiration horizon")
        if not 1<=self.option_quote_max_age<=600:
            raise ValueError("OPTION_QUOTE_MAX_AGE must be 1-600 seconds")
        if not 0.01<=self.option_max_spread_pct<=0.50:
            raise ValueError("OPTION_MAX_SPREAD_PCT must be 0.01-0.50")
        if not 0<=self.option_entry_cutoff_min<=120:
            raise ValueError("OPTION_ENTRY_CUTOFF_MIN must be 0-120 minutes")
        if not 0<=self.option_0dte_max_dte<=30:
            raise ValueError("OPTION_0DTE_MAX_DTE must be 0-30 days")
        if not 0<=self.discovery_limit<=100 or len(self.discovery_seeds)>100:
            raise ValueError("Discovery limits must be between zero and 100")
        if self.discovery and len(set(self.watch_symbols)|set(self.discovery_seeds))>500:
            raise ValueError("Discovery base coverage exceeds 500 symbols")
        if not self.watch_symbols or len(self.watch_symbols)>500:
            raise ValueError("Watchlist must contain 1–500 symbols")
        for token in (self.morning_token, self.native_morning_intake_token, self.smoothers_token, self.futures_observer_token):
            if token and len(token) < 32: raise ValueError("Integration tokens require 32 or more characters")
        for url in (self.morning_url, self.smoothers_url):
            if url and not url.startswith("https://"): raise ValueError("Source connections require HTTPS")
        if not set(self.observer_streams)<= {"mnq","mgc"}: raise ValueError("Unknown configured observer stream")
