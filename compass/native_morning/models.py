from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, strict=True)


class OptionPreview(StrictModel):
    ticker: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_:.\-]+$")
    price: float = Field(gt=0)


class SettingsSnapshot(StrictModel):
    orb_bars: int = Field(ge=1, le=30)
    alert_window: int = Field(ge=5, le=120)
    fast_open: bool
    fast_min_bar: int = Field(ge=1, le=5)
    require_rvol: bool
    rvol_threshold: float
    ema_fast: int = Field(ge=1, le=10000)
    ema_slow: int = Field(ge=1, le=10000)
    checkpoints: bool


class Features(StrictModel):
    rvol: float | None
    volume: float | None
    vwap: float | None
    ema_fast: float | None
    ema_slow: float | None
    orb_high: float | None
    orb_low: float | None
    bar_count: int = Field(ge=1)


class Signal(StrictModel):
    signal_id: str = Field(min_length=1, max_length=500, pattern=r"^[A-Za-z0-9_:|.\-]+$")
    ticker: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_:!.\-]+$")
    stream_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    script_version: str = Field(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9_.\-]+$")
    logic_mode: Literal["legacy_confirmed", "rth_confirmed"]
    setup: Literal["STANDARD_ORB", "FAST_OPEN", "BOTH"]
    timeframe_min: int = Field(ge=1, le=5)
    signal_at_ms: int = Field(gt=0)
    bar_open_ms: int = Field(gt=0)
    price: float = Field(gt=0)
    settings: SettingsSnapshot
    features: Features
    is_test: bool = False

    @model_validator(mode="after")
    def time_order(self):
        if not 0 < self.signal_at_ms - self.bar_open_ms <= self.timeframe_min * 60000:
            raise ValueError("Signal must be timestamped at the close of its bar")
        return self


class Observation(StrictModel):
    horizon_min: Literal[5, 15, 30, 60]
    observed_at_ms: int = Field(gt=0)
    price: float = Field(gt=0)
    window_high: float = Field(gt=0)
    window_low: float = Field(gt=0)
    peak_at_ms: int = Field(gt=0)
    dip_at_ms: int = Field(gt=0)
    hit_1_ms: int | None = Field(default=None, gt=0)
    hit_2_ms: int | None = Field(default=None, gt=0)
    hit_3_ms: int | None = Field(default=None, gt=0)
    bars_observed: int = Field(ge=1, le=1440)
    continuous: bool


class StockBar(StrictModel):
    open_at_ms: int = Field(gt=0)
    open: float = Field(gt=0)
    high: float = Field(gt=0)
    low: float = Field(gt=0)
    close: float = Field(gt=0)
    volume: float | None = Field(ge=0)

    @model_validator(mode="after")
    def range_order(self):
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close):
            raise ValueError("Candle range must contain open and close")
        return self


class Event(StrictModel):
    schema_version: Literal[1, 2]
    event_type: Literal["signal", "checkpoint"]
    event_id: str = Field(min_length=1, max_length=520)
    signal: Signal
    observation: Observation | None = None
    stock_bars: list[StockBar] | None = Field(default=None, max_length=60)

    @model_validator(mode="after")
    def consistency(self):
        s, o = self.signal, self.observation
        if self.schema_version == 1 and self.stock_bars is not None:
            raise ValueError("Candle history requires schema 2")
        if self.schema_version == 2 and (s.timeframe_min != 1 or s.signal_at_ms - s.bar_open_ms != 60000):
            raise ValueError("Schema 2 requires complete one-minute entry candles")
        if self.event_type == "signal":
            if o is not None or self.stock_bars is not None or self.event_id != s.signal_id + "-signal":
                raise ValueError("Invalid signal event identity or observation")
            return self
        if o is None or self.event_id != s.signal_id + "-" + str(o.horizon_min):
            raise ValueError("Invalid checkpoint event identity")
        if not s.settings.checkpoints:
            raise ValueError("Checkpoints were disabled for this signal")
        elapsed = o.observed_at_ms - s.signal_at_ms
        if not o.horizon_min * 60000 <= elapsed <= 24 * 3600000:
            raise ValueError("Checkpoint precedes its horizon or exceeds one day")
        tolerance = max(1e-8, s.price * 1e-9)
        if o.window_high + tolerance < max(s.price, o.price) or o.window_low - tolerance > min(s.price, o.price):
            raise ValueError("Checkpoint range does not contain entry and observed price")
        for t in [o.peak_at_ms, o.dip_at_ms, o.hit_1_ms, o.hit_2_ms, o.hit_3_ms]:
            if t is not None and not s.signal_at_ms <= t <= o.observed_at_ms:
                raise ValueError("Range or milestone timestamp lies outside observation window")
        for pct, t in [(1, o.hit_1_ms), (2, o.hit_2_ms), (3, o.hit_3_ms)]:
            if (t is not None) != (o.window_high + tolerance >= s.price * (1 + pct / 100)):
                raise ValueError("Milestone and observed high disagree")
        if o.continuous and elapsed != o.bars_observed * s.timeframe_min * 60000:
            raise ValueError("Continuous coverage does not match bar count")
        if self.schema_version == 2:
            if self.stock_bars is None:
                raise ValueError("Schema 2 checkpoints require cumulative candle history")
            previous = s.signal_at_ms - 60000
            for b in self.stock_bars:
                offset = b.open_at_ms - s.signal_at_ms
                if not 0 <= offset < 3600000 or offset % 60000 or b.open_at_ms <= previous or b.open_at_ms + 60000 > o.observed_at_ms:
                    raise ValueError("Candle timestamps must be ordered, unique and within the first hour after entry")
                previous = b.open_at_ms
            if elapsed <= 3600000:
                bars = self.stock_bars
                if len(bars) != o.bars_observed or not bars or bars[-1].open_at_ms + 60000 != o.observed_at_ms:
                    raise ValueError("Checkpoint must carry every observed candle")
                if (abs(max(s.price, *(b.high for b in bars)) - o.window_high) > tolerance
                        or abs(min(s.price, *(b.low for b in bars)) - o.window_low) > tolerance
                        or abs(bars[-1].close - o.price) > tolerance):
                    raise ValueError("Candle history and checkpoint prices disagree")
        return self

