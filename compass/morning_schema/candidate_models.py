# Vendored unchanged from morning-algo-tracker f459740a452a3cdf17fbb62af3d38edc1fc63c4d.
"""Strict research-only events, separate from live signals and delivery jobs."""
from datetime import datetime
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import Field, model_validator
from .models import Features, SettingsSnapshot, StockBar, StrictModel

KINDS = ("BASELINE", "BLOCKED_ORB", "BLOCKED_FAST", "DELAYED_CONFIRM", "PULLBACK_RECLAIM")


class ResearchPolicy(StrictModel):
    version: Literal["shadow_v1"]
    candidate_window_min: Literal[180]
    followup_min: Literal[180]
    delayed_bars: int = Field(ge=1, le=10)
    max_extension_pct: float = Field(gt=0, le=5)
    pullback_pct: float = Field(gt=0, le=5)
    reclaim_wait_min: int = Field(ge=1, le=60)
    reclaim_cooldown_min: int = Field(ge=1, le=60)


class ResearchSession(StrictModel):
    session_id: str = Field(min_length=1, max_length=500, pattern=r"^[A-Za-z0-9_:|.\-]+$")
    ticker: str = Field(min_length=1, max_length=50, pattern=r"^[A-Za-z0-9_:!.\-]+$")
    stream_id: str = Field(min_length=1, max_length=32, pattern=r"^[A-Za-z0-9_\-]+$")
    script_version: Literal["1.2.0", "1.3.0"]
    logic_mode: Literal["legacy_confirmed", "rth_confirmed"]
    timeframe_min: Literal[1]
    session_open_ms: int = Field(gt=0)
    activated_at_ms: int = Field(gt=0)
    settings: SettingsSnapshot
    policy: ResearchPolicy
    is_test: bool = False

    @model_validator(mode="after")
    def session_times(self):
        stamp = datetime.fromtimestamp(self.session_open_ms / 1000, ZoneInfo("America/New_York"))
        if (stamp.hour, stamp.minute, stamp.second, stamp.microsecond) != (9, 30, 0, 0) or stamp.weekday() > 4:
            raise ValueError("Session must start at weekday 09:30 New York")
        offset = self.activated_at_ms - self.session_open_ms
        if not 0 <= offset < 180 * 60000 or offset % 60000:
            raise ValueError("Activation must be a one-minute bar in the first three regular hours")
        return self


class FilterSnapshot(StrictModel):
    trade_window: bool
    above_orb: bool
    above_vwap: bool
    ema_trend: bool
    volume_ok: bool
    green: bool
    vwap_cross: bool
    ema_cross: bool
    ema_rising: bool
    fast_window: bool
    already_triggered: bool


class ResearchRecord(StrictModel):
    record_id: str = Field(min_length=1, max_length=550)
    kind: Literal["BASELINE", "BLOCKED_ORB", "BLOCKED_FAST", "DELAYED_CONFIRM", "PULLBACK_RECLAIM"]
    at_ms: int = Field(gt=0)
    price: float = Field(gt=0)
    previous_close: float = Field(gt=0)
    features: Features
    filters: FilterSnapshot
    anchor_at_ms: int | None = Field(default=None, gt=0)
    anchor_price: float | None = Field(default=None, gt=0)
    source_signal_id: str | None = Field(default=None, max_length=500, pattern=r"^[A-Za-z0-9_:|.\-]+$")
    baseline_setup: Literal["STANDARD_ORB", "FAST_OPEN", "BOTH"] | None = None


def failed_filters(record):
    f = record["filters"]
    fields = ("trade_window", "above_vwap", "ema_trend", "volume_ok") if record["kind"] == "BLOCKED_ORB" else ("fast_window", "ema_rising", "volume_ok") if record["kind"] == "BLOCKED_FAST" else ()
    return [key for key in fields if not f[key]] + (["daily_limit"] if fields and f["already_triggered"] else [])


class ResearchBatch(StrictModel):
    schema_version: Literal[3]
    event_type: Literal["research"]
    event_id: str = Field(min_length=1, max_length=550)
    observed_at_ms: int = Field(gt=0)
    part: int = Field(ge=1, le=20)
    session: ResearchSession
    bars: list[StockBar] = Field(min_length=1, max_length=10)
    records: list[ResearchRecord] = Field(max_length=40)

    @model_validator(mode="after")
    def consistency(self):
        s, end = self.session, self.observed_at_ms
        if self.event_id != s.session_id + "-research-" + str(end) + "-" + str(self.part):
            raise ValueError("Research batch identity mismatch")
        if not s.activated_at_ms < end <= s.session_open_ms + 390 * 60000 or (end-s.session_open_ms) % 60000:
            raise ValueError("Invalid batch closing timestamp")
        previous, by_close = 0, {}
        for b in self.bars:
            if (not max(s.activated_at_ms, end-10*60000) <= b.open_at_ms < end
                    or b.open_at_ms >= s.session_open_ms+360*60000
                    or (b.open_at_ms-s.session_open_ms) % 60000 or b.open_at_ms <= previous):
                raise ValueError("Candles must be ordered and unique within the rolling ten-minute window")
            by_close[b.open_at_ms+60000] = b
            previous = b.open_at_ms
        ids = set()
        for r in self.records:
            if r.record_id != s.session_id+"-"+r.kind+"-"+str(r.at_ms) or r.record_id in ids:
                raise ValueError("Research record identity mismatch")
            ids.add(r.record_id)
            b = by_close.get(r.at_ms)
            if b is None or r.at_ms > s.session_open_ms+180*60000 or abs(r.price-b.close) > max(1e-8, r.price*1e-9):
                raise ValueError("Records require their confirmed candidate candle")
            if (r.anchor_at_ms is None) != (r.anchor_price is None) or (r.anchor_at_ms is not None and not s.activated_at_ms <= r.anchor_at_ms <= r.at_ms):
                raise ValueError("Invalid research anchor")
            x, v = r.filters, r.features
            if x.green != (b.close > b.open) or x.above_orb != (v.orb_high is not None and b.close > v.orb_high) or x.above_vwap != (v.vwap is not None and b.close > v.vwap):
                raise ValueError("Filter snapshot disagrees with prices")
            if x.ema_trend != (v.ema_fast is not None and v.ema_slow is not None and v.ema_fast > v.ema_slow):
                raise ValueError("Filter snapshot disagrees with EMAs")
            if r.kind == "BASELINE":
                if not r.source_signal_id or not r.baseline_setup or x.already_triggered:
                    raise ValueError("Baseline research requires a first live-signal identity")
            elif r.source_signal_id is not None or r.baseline_setup is not None:
                raise ValueError("A candidate cannot impersonate a live signal")
            if r.kind == "BLOCKED_ORB" and (not x.above_orb or r.previous_close > v.orb_high or not failed_filters(r.model_dump())):
                raise ValueError("Blocked ORB requires a crossing and a failed filter")
            if r.kind == "BLOCKED_FAST" and (not (x.green and x.above_vwap and x.vwap_cross) or not failed_filters(r.model_dump())):
                raise ValueError("Blocked Fast Open requires a fresh VWAP crossing and a failed filter")
            if r.kind == "DELAYED_CONFIRM":
                if (not all((x.trade_window, x.above_orb, x.above_vwap, x.ema_trend, x.volume_ok)) or x.already_triggered
                        or r.anchor_at_ms is None or not 0 < r.at_ms-r.anchor_at_ms <= s.policy.delayed_bars*60000
                        or r.price > r.anchor_price*(1+s.policy.max_extension_pct/100)+1e-8):
                    raise ValueError("Invalid delayed confirmation")
            if r.kind == "PULLBACK_RECLAIM":
                if (not all((x.green, x.above_vwap, x.ema_trend, x.volume_ok)) or not (x.ema_cross or x.vwap_cross)
                        or v.ema_fast is None or r.price <= v.ema_fast or r.anchor_at_ms is None
                        or not 0 < r.at_ms-r.anchor_at_ms <= s.policy.reclaim_wait_min*60000):
                    raise ValueError("Invalid pullback reclaim")
        return self

