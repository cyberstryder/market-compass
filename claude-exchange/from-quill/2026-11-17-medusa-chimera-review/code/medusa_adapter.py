"""
Phase 1 adapter — Medusa regime (gravity) feeding the Chimera engine (reflexes).

Design (per the merge spec agreed 2026-11-16):
  * ONE canonical regime source: Medusa's classifier from medusa_oracle.py
    (v2.5-fix3, filed verbatim at ~/workspace/medusa/incoming-2026-11-16/).
    classify_periods() composite is the production path — the same dict
    build_brief() consumes — with classify_regime() on day-to-date bars as
    the early-day fallback.
  * Chimera's cones / strike selection / inventory / rotation / flatten are
    untouched. Only ChimeraEngine.update_regime() is overridden.
  * Medusa runs on COMPLETED 5-minute bars (that is what the oracle consumes),
    aggregated here from the 1-minute feed. The regime therefore refreshes on
    5-minute boundaries, not every minute.
  * Medusa-scale inputs: s0 = day open anchor (first 1-min close, same anchor
    the baseline replay uses), atr = daily ATR(17) from prior daily bars
    (Medusa's own compute_atr_series), vix / prior_score / daily MACD from the
    same prior_context() the baseline uses — identical inputs, different
    classifier, so the A/B isolates the regime layer.

State mapping (Medusa 11-state -> Chimera 5-state), score = effective_score:
  BULL_3 / BULL_2            -> TRENDING_BULL
  BULL_1 / NEUTRAL_BULL /
  NEUTRAL_BULL_CHOPPY        -> CHOPPY_BULL
  NEUTRAL                    -> UNKNOWN   (Chimera: not high-confidence,
                                           blocks new entries, manages only)
  NEUTRAL_BEAR_CHOPPY /
  NEUTRAL_BEAR / BEAR_1      -> CHOPPY_BEAR
  BEAR_2 / BEAR_3            -> TRENDING_BEAR
Chimera's own is_high_confidence property then applies unchanged
(trending |score| >= 0.18, choppy |score| >= 0.28).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

_MEDUSA_DIR = Path.home() / "workspace" / "medusa" / "incoming-2026-11-16"
if str(_MEDUSA_DIR) not in sys.path:
    sys.path.insert(0, str(_MEDUSA_DIR))

import medusa_oracle as mo  # noqa: E402

from chimera.core.types import Regime, RegimeState  # noqa: E402
from chimera.engine.chimera import ChimeraEngine  # noqa: E402

STATE_MAP: Dict[str, RegimeState] = {
    "BULL_3": RegimeState.TRENDING_BULL,
    "BULL_2": RegimeState.TRENDING_BULL,
    "BULL_1": RegimeState.CHOPPY_BULL,
    "NEUTRAL_BULL": RegimeState.CHOPPY_BULL,
    "NEUTRAL_BULL_CHOPPY": RegimeState.CHOPPY_BULL,
    "NEUTRAL": RegimeState.UNKNOWN,
    "NEUTRAL_BEAR_CHOPPY": RegimeState.CHOPPY_BEAR,
    "NEUTRAL_BEAR": RegimeState.CHOPPY_BEAR,
    "BEAR_1": RegimeState.CHOPPY_BEAR,
    "BEAR_2": RegimeState.TRENDING_BEAR,
    "BEAR_3": RegimeState.TRENDING_BEAR,
    "UNKNOWN": RegimeState.UNKNOWN,
}


def map_medusa_regime(med: dict, prior_score_avg: float = 0.0) -> Regime:
    """Medusa classifier dict -> Chimera Regime object."""
    state_str = med.get("state", "UNKNOWN")
    regime = Regime(
        state=STATE_MAP.get(state_str, RegimeState.UNKNOWN),
        score=float(med.get("effective_score", 0.0)),
        components={k: float(v) for k, v in med.get("components", {}).items()},
        prior_score=float(prior_score_avg),
    )
    regime.medusa_state = state_str  # type: ignore[attr-defined]
    regime.medusa_raw_score = float(med.get("score", 0.0))  # type: ignore[attr-defined]
    return regime


class MedusaRegimeFeed:
    """Buffers 1-min bars, aggregates completed 5-min bars, classifies."""

    def __init__(self, s0: float, atr_daily: float, vix: float,
                 prior_score_avg: float = 0.0,
                 daily_macd_hist: float = 0.0,
                 daily_macd_slope: float = 0.0):
        self.s0 = float(s0)
        self.atr = float(atr_daily)
        self.vix = float(vix)
        self.prior_score_avg = float(prior_score_avg)
        self.daily_macd_hist = float(daily_macd_hist)
        self.daily_macd_slope = float(daily_macd_slope)

        self._bars_1m: List[dict] = []
        self._bars_5m: List[dict] = []
        self._open_bucket: Optional[pd.Timestamp] = None
        self._regime: Regime = Regime(state=RegimeState.UNKNOWN, score=0.0)
        self._regime_medusa_state = "UNKNOWN"
        self._n_classified = 0
        self.history: List[dict] = []   # one row per classification refresh

    # ── feed ─────────────────────────────────────────────
    def push(self, ts: pd.Timestamp, o: float, h: float, l: float,
             c: float, v: float) -> None:
        self._bars_1m.append({"ts": ts, "open": o, "high": h, "low": l,
                              "close": c, "volume": v})
        bucket = ts.floor("5min")
        if self._open_bucket is None:
            self._open_bucket = bucket
        elif bucket > self._open_bucket:
            self._close_bucket(self._open_bucket)
            self._open_bucket = bucket
            self._reclassify()

    def _close_bucket(self, bucket: pd.Timestamp) -> None:
        rows = [b for b in self._bars_1m if b["ts"].floor("5min") == bucket]
        if not rows:
            return
        vol = sum(b["volume"] for b in rows)
        self._bars_5m.append({
            "datetime_est": bucket,
            "open": rows[0]["open"],
            "high": max(b["high"] for b in rows),
            "low": min(b["low"] for b in rows),
            "close": rows[-1]["close"],
            # ^GSPC prints no usable volume; fall back to volume-neutral 1.0,
            # the same spirit as Chimera's own classifier fallback.
            "volume": vol if vol > 0 else 1.0,
        })

    # ── classification ───────────────────────────────────
    def _reclassify(self) -> None:
        if len(self._bars_5m) < 4:
            return
        df = pd.DataFrame(self._bars_5m)
        result = mo.classify_periods(
            df, self.s0, self.atr, self.vix,
            prior_score_avg=self.prior_score_avg,
            daily_macd_hist=self.daily_macd_hist,
            daily_macd_slope=self.daily_macd_slope,
        )
        if result is not None:
            med = result["composite"]
        else:
            med = mo.classify_regime(
                df, self.s0, self.atr, self.vix,
                prior_score_avg=self.prior_score_avg,
                daily_macd_hist=self.daily_macd_hist,
                daily_macd_slope=self.daily_macd_slope,
            )
        self._regime = map_medusa_regime(med, self.prior_score_avg)
        self._regime_medusa_state = med.get("state", "UNKNOWN")
        self._n_classified += 1
        self.history.append({
            "ts": str(df["datetime_est"].iloc[-1]),
            "medusa_state": self._regime_medusa_state,
            "effective_score": float(med.get("effective_score", 0.0)),
            "chimera_state": self._regime.state.value,
        })

    def current_regime(self) -> Regime:
        return self._regime

    @property
    def n_classified(self) -> int:
        return self._n_classified


class MedusaChimeraEngine(ChimeraEngine):
    """ChimeraEngine whose regime comes from the Medusa feed.

    Everything else — cones, strike selection, inventory, rotation,
    flatten — is the unmodified Chimera v1 engine.
    """

    def __init__(self, cfg=None, feed: Optional[MedusaRegimeFeed] = None,
                 rotate_only_when_trending: bool = False):
        super().__init__(cfg)
        self.feed = feed
        # Phase 2 variant: Chimera's disagreement rotation was tuned for a
        # twitchy 1-min regime. Against Medusa's 5-min gravity, disagreement
        # inside CHOPPY states is mostly noise — optionally allow rotation
        # only while Medusa reads TRENDING (BULL_2/3 or BEAR_2/3 mapped).
        self.rotate_only_when_trending = rotate_only_when_trending

    def _handle_disagreement(self, now, regime_bias, present_bias):
        if self.rotate_only_when_trending and not self.regime.is_trending:
            return []
        return super()._handle_disagreement(now, regime_bias, present_bias)

    def update_regime(self, bars, s0: float, atr: float, vix: float = 18.0,
                      prior_score: float = 0.0, daily_macd_hist: float = 0.0,
                      daily_macd_slope: float = 0.0):
        # Signature mirrors ChimeraEngine.update_regime so the replay harness
        # can drive both engines identically. The trailing-window arguments
        # are intentionally ignored: Medusa classifies the day-to-date
        # completed 5-minute bars held by the feed.
        if self.feed is not None:
            self.regime = self.feed.current_regime()
