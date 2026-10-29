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
    # Sector ETFs for the day trading board's sector feed — QUOTES/BARS ONLY.
    # These must never enter the trading/scan universe: no option chains, no
    # 0DTE scanning, no paper trading. They exist solely so the sector
    # rotation feed (day_board_feeds.sector_dashboard_from_closes) has daily
    # bars. Wired into data collection via universe.data_symbols + the
    # "dayboard" project in secondary_data.coverage, deliberately separate
    # from cfg.stocks which drives every scanner and trading path.
    sector_etfs: tuple = field(default_factory=lambda: tuple(s.strip() for s in env("SECTOR_ETF_SYMBOLS","XLK,XLF,XLE,XLV,XLY,XLP,XLI,XLB,XLU,XLRE,XLC").split(",") if s.strip()))
    futures: tuple = field(default_factory=lambda: tuple(env("FUTURES_SYMBOLS","MES.c.0,MNQ.c.0").split(",")))
    extra_futures: tuple = field(default_factory=lambda: tuple(s.strip() for s in env("EXTRA_FUTURES_SYMBOLS","MGC.v.0,GC.v.0,MCL.v.0,CL.v.0,YM.v.0,MYM.v.0").split(",") if s.strip()))
    alpaca_key: str = field(default_factory=lambda: env("APCA_API_KEY_ID"))
    alpaca_secret: str = field(default_factory=lambda: env("APCA_API_SECRET_KEY"))
    # Dedicated OPRA stream keys (defaults to main keys if not set).
    # Use these to give the OPRA WebSocket its own API keys, avoiding
    # the 1-connection-per-account limit when another app (e.g. Obsidian)
    # holds a stream with the main keys.
    alpaca_opra_key: str = field(default_factory=lambda: env("APCA_OPRA_KEY_ID") or env("APCA_API_KEY_ID"))
    alpaca_opra_secret: str = field(default_factory=lambda: env("APCA_OPRA_SECRET_KEY") or env("APCA_API_SECRET_KEY"))
    # Alpaca API base URL: live (https://api.alpaca.markets) vs paper
    # (https://paper-api.alpaca.markets). Defaults to live; set to paper
    # explicitly for paper trading.
    alpaca_base_url: str = field(default_factory=lambda: env("APCA_API_BASE_URL", "https://api.alpaca.markets"))
    feed: str = field(default_factory=lambda: env("ALPACA_FEED","sip"))
    databento: str = field(default_factory=lambda: env("DATABENTO_API_KEY"))
    massive: str = field(default_factory=lambda: env("MASSIVE_API_KEY"))
    matrix: str = field(default_factory=lambda: env("TRADERMATRIX_API_KEY"))
    fmp_key: str = field(default_factory=lambda: env("FMP_API_KEY",""))
    discord: str = field(default_factory=lambda: env("DISCORD_WEBHOOK_URL"))
    discord_routes: dict = field(default_factory=lambda: {
        key: env('DISCORD_' + key.upper() + '_WEBHOOK_URL')
        # Condensed 2026-09-28: one route per Discord channel. Dead variables
        # DISCORD_UNUSUAL_OPTIONS_WEBHOOK_URL, DISCORD_SWING_WEBHOOK_URL,
        # DISCORD_SYSTEM_WEBHOOK_URL and DISCORD_EXPOSURE_WEBHOOK_URL are gone.
        for key in ('spy_morning', 'options_0dte', 'intraday', 'magnets', 'options_ideas',
                    'smoothers', 'futures', 'research')})
    discord_fallback: bool = field(default_factory=lambda: env('DISCORD_SHARED_FALLBACK', 'true') == 'true')
    spy_morning_brief: bool = field(default_factory=lambda: env("SPY_MORNING_BRIEF_ENABLED", "false") == "true")
    spy_chart_discord: bool = field(default_factory=lambda: env("SPY_CHART_DISCORD_ENABLED", "true") == "true")
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
    flow_pulse: bool = field(default_factory=lambda: env("FLOW_PULSE_ENABLED","true")=="true")
    zero_dte_paper: bool = field(default_factory=lambda: env("ZERO_DTE_PAPER_ENABLED","true")=="true")
    # 0DTE scanner seat/shadow split (underlying-first validation, 2026-10-25).
    # Empty = the validated defaults in compass/scanner_shadow.py. Override
    # format: "family:NAME,NAME;family2:NAME" (seats) / "family,family".
    scanner_paper_seats: str = field(default_factory=lambda: env("SCANNER_PAPER_SEATS",""))
    scanner_promoted_families: str = field(default_factory=lambda: env("SCANNER_PROMOTED_FAMILIES",""))
    darkpool: bool = field(default_factory=lambda: env("DARKPOOL_ENABLED","true")=="true")
    iv_rank: bool = field(default_factory=lambda: env("IV_RANK_ENABLED","true")=="true")
    squeeze: bool = field(default_factory=lambda: env("SQUEEZE_ENABLED","true")=="true")
    flow_pulse_min_premium: float = field(default_factory=lambda: float(env("FLOW_PULSE_MIN_PREMIUM","1000000")))
    flow_pulse_min_score: float = field(default_factory=lambda: float(env("FLOW_PULSE_MIN_SCORE","90")))
    flow_pulse_min_ratio: float = field(default_factory=lambda: float(env("FLOW_PULSE_MIN_RATIO","2.0")))
    flow_pulse_max_dte: float = field(default_factory=lambda: float(env("FLOW_PULSE_MAX_DTE","45")))
    flow_pulse_push_enabled: bool = field(default_factory=lambda: env("FLOW_PULSE_PUSH_ENABLED","false")=="true")
    flow_pulse_track_enabled: bool = field(default_factory=lambda: env("FLOW_PULSE_TRACK_ENABLED","true")=="true")
    alpaca_option_stream: bool = field(default_factory=lambda: env("ALPACA_OPTION_STREAM_ENABLED","false")=="true")
    flow_pulse_webhook: str = field(default_factory=lambda: env("DISCORD_FLOW_PULSE_WEBHOOK_URL",""))
    ict_push_enabled: bool = field(default_factory=lambda: env("ICT_PUSH_ENABLED","false")=="true")
    ict_push_webhook: str = field(default_factory=lambda: env("DISCORD_ICT_WEBHOOK_URL",""))
    # CrossTrade webhook bridge for live futures automation (2026-10-13)
    # Disabled by default; enable with CROSSTRADE_ENABLED=true
    crosstrade_enabled: bool = field(default_factory=lambda: env("CROSSTRADE_ENABLED","false")=="true")
    crosstrade_webhook_url: str = field(default_factory=lambda: env("CROSSTRADE_WEBHOOK_URL",""))
    crosstrade_key: str = field(default_factory=lambda: env("CROSSTRADE_KEY",""))
    crosstrade_account: str = field(default_factory=lambda: env("CROSSTRADE_ACCOUNT",""))
    traderspost_enabled: bool = field(default_factory=lambda: env("TRADERSPOST_ENABLED","false")=="true")
    traderspost_webhook_url: str = field(default_factory=lambda: env("TRADERSPOST_WEBHOOK_URL",""))
    traderspost_webhook_url_2: str = field(default_factory=lambda: env("TRADERSPOST_WEBHOOK_URL_2",""))
    tape_confirmed: bool = field(default_factory=lambda: env("TAPE_CONFIRMED_ENABLED","true")=="true")
    gap_push_enabled: bool = field(default_factory=lambda: env("GAP_PUSH_ENABLED","true")=="true")
    day_board_push_enabled: bool = field(default_factory=lambda: env("DAY_BOARD_PUSH_ENABLED","true")=="true")
    futures_scanner_push_enabled: bool = field(default_factory=lambda: env("FUTURES_SCANNER_PUSH_ENABLED","false")=="true")
    # ICT futures detectors. Defaults mirror PRODUCTION (verified
    # 2026-10-19 against /api/simple drill stats: every detector listed
    # default-true below has live paper trades this epoch; the env var
    # remains as an override — set it to "false" to disable. Continuation
    # and trend_rider stay default-OFF deliberately (Josh, 2026-10-14 /
    # 2026-10-08). Before this cleanup every default read "false" while
    # production ran them all via env vars — a lost env var would have
    # silently disabled live detectors on redeploy.
    # PRUNE (Josh, 2026-10-24, futures plan phase 1): bos_fvg, bos_gz_vwap,
    # aoi_zones and smt_divergence now default OFF. Three datasets agree
    # (production-config backtest, the full strategy x ticker matrix, and
    # an independent 8-week replay): the structure-chasing detectors bleed
    # while the level-fades earn. Env vars remain as overrides, and the
    # ict_paper allowlists block their old combos as defense in depth.
    ict_session_liquidity: bool = field(default_factory=lambda: env("ICT_SESSION_LIQUIDITY_ENABLED","true")=="true")
    ict_htf_levels: bool = field(default_factory=lambda: env("ICT_HTF_LEVELS_ENABLED","true")=="true")
    ict_turtle_soup: bool = field(default_factory=lambda: env("ICT_TURTLE_SOUP_ENABLED","true")=="true")
    ict_smt_divergence: bool = field(default_factory=lambda: env("ICT_SMT_DIVERGENCE_ENABLED","false")=="true")
    ict_aoi_zones: bool = field(default_factory=lambda: env("ICT_AOI_ZONES_ENABLED","false")=="true")
    ict_aoi_fade: bool = field(default_factory=lambda: env("ICT_AOI_FADE_ENABLED","true")=="true")
    ict_trend_rider: bool = field(default_factory=lambda: env("ICT_TREND_RIDER_ENABLED","false")=="true")
    # Gap fade detector (futures): fade overnight gaps at RTH open
    gap_fade_enabled: bool = field(default_factory=lambda: env("GAP_FADE_ENABLED","false")=="true")
    gap_fade_min_pct: float = field(default_factory=lambda: float(env("GAP_FADE_MIN_PCT","0.005")))
    gap_fade_max_pct: float = field(default_factory=lambda: float(env("GAP_FADE_MAX_PCT","0.02")))
    gap_fade_target_pct: float = field(default_factory=lambda: float(env("GAP_FADE_TARGET_PCT","0.5")))
    # VIX regime filter: adjust strategy selection based on VIX level
    vix_regime_enabled: bool = field(default_factory=lambda: env("VIX_REGIME_ENABLED","true")=="true")
    vix_low_threshold: float = field(default_factory=lambda: float(env("VIX_LOW_THRESHOLD","15.0")))
    vix_high_threshold: float = field(default_factory=lambda: float(env("VIX_HIGH_THRESHOLD","25.0")))
    mu_scalp: bool = field(default_factory=lambda: env("MU_SCALP_ENABLED","true")=="true")
    ict_tier_a_b: bool = field(default_factory=lambda: env("ICT_TIER_A_B_ENABLED","false")=="true")
    ict_golden_zone: bool = field(default_factory=lambda: env("ICT_GOLDEN_ZONE_ENABLED","true")=="true")
    ict_gz_ema_fast: int = field(default_factory=lambda: int(env("ICT_GZ_EMA_FAST","9")))
    ict_gz_ema_slow: int = field(default_factory=lambda: int(env("ICT_GZ_EMA_SLOW","21")))
    ict_gz_ema_cross_lookback: int = field(default_factory=lambda: int(env("ICT_GZ_EMA_CROSS_LOOKBACK","5")))
    ict_gz_ema_cross: bool = field(default_factory=lambda: env("ICT_GZ_EMA_CROSS_ENABLED","false")=="true")
    ict_bos_fvg: bool = field(default_factory=lambda: env("ICT_BOS_FVG_ENABLED","false")=="true")
    ict_bos_gz_vwap: bool = field(default_factory=lambda: env("ICT_BOS_GZ_VWAP_ENABLED","false")=="true")
    ict_rumers_box: bool = field(default_factory=lambda: env("ICT_RUMERS_BOX_ENABLED","true")=="true")
    ict_trend_bias: bool = field(default_factory=lambda: env("ICT_TREND_BIAS_ENABLED","false")=="true")
    ict_trend_bias_sma_len: int = field(default_factory=lambda: int(env("ICT_TREND_BIAS_SMA_LEN","50")))
    ict_futures_paper: bool = field(default_factory=lambda: env("ICT_FUTURES_PAPER_ENABLED","true")=="true")
    # Claude gauntlet survivors (2026-10-28, Josh's swap decision):
    # compass/survivors.py detectors S1-S4. Default ON in code — the
    # deploy is the enablement act; live sends additionally require the
    # survivors:state tripwire to be un-tripped (see survivors.py).
    survivor_enabled: bool = field(default_factory=lambda: env("SURVIVOR_DETECTORS_ENABLED","true")=="true")
    ict_paper_min_stop_atr: float = field(default_factory=lambda: float(env("ICT_PAPER_MIN_STOP_ATR","0.5")))
    ict_paper_cooldown_min: float = field(default_factory=lambda: float(env("ICT_PAPER_COOLDOWN_MIN","15")))
    ict_brake_shadow: bool = field(default_factory=lambda: env("ICT_BRAKE_SHADOW_ENABLED", "true") == "true")
    # Freshness guard (Josh 2026-10-26, go-live): at the TradersPost
    # send-queue decision in ict_paper.submit(), skip the LIVE send
    # when the fresh quote is already beyond the trade's approved
    # stop/target, or has moved adversely from the booked entry by
    # >10% of target distance (see compass/freshness_guard.py). The
    # paper trade books regardless. Default ON — production needs no
    # var set; FRESHNESS_GUARD_ENABLED=false is the no-deploy rollback.
    freshness_guard: bool = field(default_factory=lambda: env("FRESHNESS_GUARD_ENABLED", "true") == "true")
    # Shadow iceberg detector (2026-10-25): reconstructs institutional
    # options accumulation from the OPRA print stream. SHADOW ONLY —
    # logs alerts + scored outcomes, never trades, never alerts Discord
    # in v1. Defaults: an institutional parent worked in slices over
    # half an hour is hundreds of contracts, tens of prints, five
    # figures of premium, predominantly at the ask, spread across
    # several 5-minute sub-windows; see compass/iceberg.py.
    iceberg_detector: bool = field(default_factory=lambda: env("ICEBERG_DETECTOR_ENABLED","true")=="true")
    iceberg_min_contracts_30m: int = field(default_factory=lambda: int(env("ICEBERG_MIN_CONTRACTS_30M","500")))
    iceberg_min_premium_30m: float = field(default_factory=lambda: float(env("ICEBERG_MIN_PREMIUM_30M","25000")))
    iceberg_min_prints_30m: int = field(default_factory=lambda: int(env("ICEBERG_MIN_PRINTS_30M","25")))
    iceberg_min_ask_share: float = field(default_factory=lambda: float(env("ICEBERG_MIN_ASK_SHARE","0.65")))
    iceberg_min_active_subwindows: int = field(default_factory=lambda: int(env("ICEBERG_MIN_ACTIVE_SUBWINDOWS","3")))
    iceberg_vol_oi_ratio: float = field(default_factory=lambda: float(env("ICEBERG_VOL_OI_RATIO","0.5")))
    iceberg_no_concession_tol: float = field(default_factory=lambda: float(env("ICEBERG_NO_CONCESSION_TOL","0.05")))
    iceberg_cooldown_min: float = field(default_factory=lambda: float(env("ICEBERG_COOLDOWN_MIN","30")))
    iceberg_max_tracked: int = field(default_factory=lambda: int(env("ICEBERG_MAX_TRACKED_CONTRACTS","5000")))
    ict_symbols: tuple = field(default_factory=lambda: tuple(s.strip() for s in env("ICT_SYMBOLS","MNQ.c.0,MES.c.0,MGC.v.0,MCL.v.0,MYM.c.0,M2K.c.0,M6A.c.0,M6E.c.0").split(",") if s.strip()))
    ict_htf_tradingview: bool = field(default_factory=lambda: env("ICT_HTF_TRADINGVIEW","false")=="true")
    ict_tv_cache_dir: str = field(default_factory=lambda: env("ICT_TV_CACHE_DIR",""))
    ict_smt_pairs: tuple = field(default_factory=lambda: tuple(p.strip() for p in env("ICT_SMT_PAIRS","MNQ.c.0:MES.c.0").split(",") if p.strip()))
    gap_continuation: bool = field(default_factory=lambda: env("GAP_CONTINUATION_ENABLED","true")=="true")
    gap_min_gap: float = field(default_factory=lambda: float(env("GAP_MIN_GAP","0.015")))
    gap_large_caps: str = field(default_factory=lambda: env("GAP_LARGE_CAP_SYMBOLS",""))
    breakouts: bool = field(default_factory=lambda: env("BREAKOUTS_ENABLED","true")=="true")
    trap_paper: bool = field(default_factory=lambda: env("TRAP_PAPER_ENABLED","false")=="true")
    # Dashboard display cutoff: stats and the P/L calendar ignore records
    # created before this day (options-only regime began 2026-09-30).
    # Display-only; nothing is deleted.
    dashboard_cutoff: str = field(default_factory=lambda: env("DASHBOARD_CUTOFF_DAY","2026-09-30"))
    # REVIVED 2026-10-01: Day board rebuilt on alternative feeds —
    # sector performance from sector ETF daily bars (SECTOR_ETF_SYMBOLS,
    # quotes/bars only), a static 2026 economic calendar, and an FMP
    # earnings feed (stubbed until FMP_API_KEY is set). The dead vendor
    # research keys are no longer used. See day_board_feeds.py.
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
    stream_limit: int = field(default_factory=lambda: min(900,max(10,int(env("OPTION_STREAM_CONTRACTS","500")))))
    # Massive's OWN subscription cap, independent of the shared selection
    # size above: both streams subscribe the shared set, so the
    # OPTION_STREAM_CONTRACTS raise aimed at Alpaca (500 -> 900 on
    # 2026-10-20) oversubscribed Massive past its vendor limit and it
    # errored instead of streaming; it had already died once at the 500
    # cap when intraday pinned demand peaked the set. Default 500 = the
    # level Massive last verifiably delivered at (2026-10-20 morning
    # parity run); lower MASSIVE_OPTION_STREAM_CONTRACTS if the vendor
    # limit trips again. Massive takes its subset in selection priority
    # order (see option_stream.massive_wanted).
    massive_stream_limit: int = field(default_factory=lambda: min(900,max(10,int(env("MASSIVE_OPTION_STREAM_CONTRACTS","500")))))
    history_budget: float = field(default_factory=lambda: float(env("FUTURES_HISTORY_BUDGET_USD","0.10")))
    watchlist: tuple = field(default_factory=lambda: symbols(env("WATCH_SYMBOLS")))
    discovery: bool = field(default_factory=lambda: env("DISCOVERY_RESEARCH_ENABLED","false")=="true")
    discovery_seeds: tuple = field(default_factory=lambda: symbols(env("DISCOVERY_SEED_SYMBOLS","PONY,FLY,ENPH,AAOI,DAL,S,TTWO,RIG,CARR,PGEN,SDGR,EWTX,ASX")))
    discovery_limit: int = field(default_factory=lambda: int(env("DISCOVERY_DYNAMIC_LIMIT","50")))
    scanner: bool = field(default_factory=lambda: env("SCANNER_ENABLED","true")=="true")
    scanner_paper: bool = field(default_factory=lambda: env("SCANNER_PAPER_TRADES","true")=="true")
    research: bool = field(default_factory=lambda: env("RESEARCH_FEEDS_ENABLED","true")=="true")
    option_focus: int = field(default_factory=lambda: int(env("OPTION_FOCUS_SYMBOLS","16")))
    chain_dte: int = field(default_factory=lambda: int(env("OPTION_CHAIN_MAX_DTE","90")))
    # 0DTE option entry gates (relaxed 2026-09-30: the 5s quote / 8% spread /
    # 30-min-cutoff trio was rejecting most same-day contracts; late session
    # tightened further the same day: 5-min entry cutoff + 5-min flatten so
    # late-day legs like the 2026-09-30 SPY drop are tradable).
    option_quote_max_age: int = field(default_factory=lambda: int(env("OPTION_QUOTE_MAX_AGE","60")))
    option_max_spread_pct: float = field(default_factory=lambda: float(env("OPTION_MAX_SPREAD_PCT","0.12")))
    option_entry_cutoff_min: int = field(default_factory=lambda: int(env("OPTION_ENTRY_CUTOFF_MIN","5")))
    # Late-session flatten for intraday option positions (default 5 min before the
    # close). The entry cutoff must be >= this: entries allowed after the flatten
    # would be flattened on the next tick.
    option_flatten_min: int = field(default_factory=lambda: int(env("OPTION_FLATTEN_MIN","5")))
    # 0DTE contract pool: the scanner day-trades short-dated contracts for 0DTE-style
    # moves. 0 = same-day expiry only; 7 (default) includes the nearest weekly so
    # names without daily expirations (e.g. MU) are covered too. Positions still
    # flatten 5 minutes before the close (OPTION_FLATTEN_MIN) regardless of expiry.
    option_0dte_max_dte: int = field(default_factory=lambda: int(env("OPTION_0DTE_MAX_DTE","7")))
    obsidian_url: str = field(default_factory=lambda: env("OBSIDIAN_FEED_URL"), repr=False)
    # Morning/ORB retired 2026-10 (see ops/ORB_RETIREMENT.md); default off, env override still honored.
    morning_enabled: bool = field(default_factory=lambda: env("MORNING_ORB_ENABLED","false")=="true")
    orb_setups: bool = field(default_factory=lambda: env("ORB_SETUPS_ENABLED","false")=="true")
    morning_url: str = field(default_factory=lambda: env("MORNING_SOURCE_URL"))
    morning_token: str = field(default_factory=lambda: env("MORNING_INTEGRATION_TOKEN"))
    native_morning_intake_token: str = field(default_factory=lambda: env("NATIVE_MORNING_INTAKE_TOKEN"), repr=False)
    smoothers_url: str = field(default_factory=lambda: env("SMOOTHERS_SOURCE_URL"))
    smoothers_token: str = field(default_factory=lambda: env("SMOOTHERS_INTEGRATION_TOKEN"))
    futures_observer_token: str = field(default_factory=lambda: env("FUTURES_OBSERVER_TOKEN"))
    # Audit #3 endpoint lockdown: bearer token for the state-mutating
    # cron/agent endpoints (see CRON_TOKEN_ENDPOINTS in app.py). Empty =
    # lockdown off, endpoints keep their historical public behavior.
    cron_token: str = field(default_factory=lambda: env("COMPASS_CRON_TOKEN"), repr=False)
    observer_streams: tuple = field(default_factory=lambda: tuple(s for s in env("FUTURES_OBSERVER_STREAMS").split(",") if s))
    secondary: bool = field(default_factory=lambda: env("SECONDARY_REVIEW_ENABLED", "true") == "true")
    secondary_alerts: bool = field(default_factory=lambda: env("SECONDARY_REVIEW_ALERTS", "true") == "true")
    secondary_native: bool = field(default_factory=lambda: env("SECONDARY_REVIEW_COMPASS_FUTURES", "true") == "true")

    @property
    def watch_symbols(self):
        extra = self.watchlist or (saved_watchlist() if not self.local and self.scanner else ())
        # 2026-10-15: REVERTED OPRA filter (d35d433). The filter restricted
        # scanning from 144 tickers to 16 OPRA tickers, which may have killed
        # 0DTE/Ideas signals on tickers outside the 16. The MRNA observation_gap
        # problem should be handled per-ticker, not by nuking scanner coverage.
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
        # SIL/SI dropped 2026-10-15: Tradeify does not support SIL.
        if any(s not in {r+'.v.0' for r in ('MGC','GC','MCL','CL','YM','MYM')} for s in self.extra_futures):
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
        if not 0<=self.option_flatten_min<=120:
            raise ValueError("OPTION_FLATTEN_MIN must be 0-120 minutes")
        if self.option_entry_cutoff_min<self.option_flatten_min:
            raise ValueError("OPTION_ENTRY_CUTOFF_MIN must be >= OPTION_FLATTEN_MIN")
        if not 0<=self.option_0dte_max_dte<=30:
            raise ValueError("OPTION_0DTE_MAX_DTE must be 0-30 days")
        if self.iceberg_min_contracts_30m<1 or self.iceberg_min_premium_30m<=0 or self.iceberg_min_prints_30m<1:
            raise ValueError("ICEBERG volume/premium/print thresholds must be positive")
        if not 0<self.iceberg_min_ask_share<=1 or not 1<=self.iceberg_min_active_subwindows<=6:
            raise ValueError("ICEBERG ask share must be in (0,1] and active subwindows 1-6")
        if self.iceberg_vol_oi_ratio<=0 or not 0<=self.iceberg_no_concession_tol<1 or self.iceberg_cooldown_min<0 or self.iceberg_max_tracked<100:
            raise ValueError("Invalid ICEBERG detector bounds")
        if not 0<=self.discovery_limit<=100 or len(self.discovery_seeds)>100:
            raise ValueError("Discovery limits must be between zero and 100")
        if self.discovery and len(set(self.watch_symbols)|set(self.discovery_seeds))>500:
            raise ValueError("Discovery base coverage exceeds 500 symbols")
        if not self.watch_symbols or len(self.watch_symbols)>500:
            raise ValueError("Watchlist must contain 1–500 symbols")
        for token in (self.morning_token, self.native_morning_intake_token, self.smoothers_token, self.futures_observer_token, self.cron_token):
            if token and len(token) < 32: raise ValueError("Integration tokens require 32 or more characters")
        for url in (self.morning_url, self.smoothers_url):
            if url and not url.startswith("https://"): raise ValueError("Source connections require HTTPS")
        if not set(self.observer_streams)<= {"mnq","mgc"}: raise ValueError("Unknown configured observer stream")
