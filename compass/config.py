import os
from dataclasses import dataclass, field

def env(k, default=""):
    return os.environ.get(k, default).strip()

@dataclass
class Config:
    db: str = field(default_factory=lambda: env("DATABASE_URL","sqlite:///compass.db"))
    role: str = field(default_factory=lambda: env("COMPASS_ROLE","all"))
    password: str = field(default_factory=lambda: env("COMPASS_PASSWORD"))
    secret: str = field(default_factory=lambda: env("SESSION_SECRET"))
    local: bool = field(default_factory=lambda: env("COMPASS_LOCAL")=="true")
    stocks: tuple = field(default_factory=lambda: tuple(env("STOCK_SYMBOLS","SPY,QQQ,IWM,NVDA,AAPL").split(",")))
    futures: tuple = field(default_factory=lambda: tuple(env("FUTURES_SYMBOLS","MES.c.0,MNQ.c.0").split(",")))
    alpaca_key: str = field(default_factory=lambda: env("APCA_API_KEY_ID"))
    alpaca_secret: str = field(default_factory=lambda: env("APCA_API_SECRET_KEY"))
    feed: str = field(default_factory=lambda: env("ALPACA_FEED","sip"))
    databento: str = field(default_factory=lambda: env("DATABENTO_API_KEY"))
    massive: str = field(default_factory=lambda: env("MASSIVE_API_KEY"))
    matrix: str = field(default_factory=lambda: env("TRADERMATRIX_API_KEY"))
    openai: str = field(default_factory=lambda: env("OPENAI_API_KEY"))
    model: str = field(default_factory=lambda: env("OPENAI_MODEL","gpt-5-mini"))
    discord: str = field(default_factory=lambda: env("DISCORD_WEBHOOK_URL"))
    risk: float = field(default_factory=lambda: float(env("SHADOW_RISK_DOLLARS","100")))
    daily_loss: float = field(default_factory=lambda: float(env("SHADOW_MAX_DAILY_LOSS","300")))
    stream_limit: int = field(default_factory=lambda: min(900,max(10,int(env("OPTION_STREAM_CONTRACTS","100")))))
    history_budget: float = field(default_factory=lambda: float(env("FUTURES_HISTORY_BUDGET_USD","0.10")))

    def validate(self):
        if self.role not in {"all","web","collector","engine"}:
            raise ValueError("Invalid COMPASS_ROLE")
        if not self.local and self.db.startswith("sqlite"):
            raise ValueError("Production requires persistent PostgreSQL")
        if self.password and len(self.password)<16:
            raise ValueError("COMPASS_PASSWORD must be at least 16 characters")
        if any(s not in {"MES.c.0","MNQ.c.0","MES.v.0","MNQ.v.0"} for s in self.futures):
            raise ValueError("v0.1 futures specifications support MES/MNQ only")
        if self.feed not in {"sip","iex"} or min(self.risk,self.daily_loss)<=0:
            raise ValueError("Invalid feed or simulation risk")
        if not 0<=self.history_budget<=1: raise ValueError("History recovery budget must be between $0 and $1 cumulative")
