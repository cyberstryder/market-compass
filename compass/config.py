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
        for key in ('spy_morning', 'futures', 'options_0dte', 'options_ideas', 'swing',
                    'unusual_options', 'exposure', 'intraday', 'research', 'system')})
    discord_fallback: bool = field(default_factory=lambda: env('DISCORD_SHARED_FALLBACK', 'true') == 'true')
    spy_morning_brief: bool = field(default_factory=lambda: env("SPY_MORNING_BRIEF_ENABLED", "true") == "true")
    paper_trading: bool = field(default_factory=lambda: env("PAPER_TRADING_ENABLED", "false") == "true")
    risk: float = field(default_factory=lambda: float(env("SHADOW_RISK_DOLLARS","100")))
    daily_loss: float = field(default_factory=lambda: float(env("SHADOW_MAX_DAILY_LOSS","300")))
    max_entries: int = field(default_factory=lambda: int(env("SHADOW_MAX_ENTRIES","0")))
    setup_study: bool = field(default_factory=lambda: env("SETUP_STUDY_ENABLED","true")=="true")
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
    obsidian_history: bool = field(default_factory=lambda: env("OBSIDIAN_HISTORY_AUDIT_ENABLED","false")=="true")
    obsidian_url: str = field(default_factory=lambda: env("OBSIDIAN_FEED_URL"), repr=False)
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
        if self.feed not in {"sip","iex"} or min(self.risk,self.daily_loss)<=0:
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
