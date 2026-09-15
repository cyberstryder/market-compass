# Source: cyberstryder/smoothers-new shared/quality.py; preserved for migration parity.
"""Transparent vNext quality scoring for Monday signal ranking.

v1.1 keeps the audit score from v1 but makes A+ Featured selection follow the
strongest historical result directly: raw target difficulty relative to ATR.

A+ Featured selection:
- rank actionable signals by raw target_atr_mult ascending;
- use ticker/direction only as deterministic tie-breakers;
- Bayesian reliability does not reorder A+;
- non-negative modeled option return receives no penalty or bonus;
- missing priceability / modeled return is not actionable for A+;
- negative modeled option return is not actionable for A+.

The 0-100 Quality Score remains an informational index used for the broader
A/B/WATCH presentation. It is not a predicted win probability.

Quality Score components:
- 85% target/ATR attainability
- 15% Bayesian reliability, shrunk toward the optimizer backtest WR
- option economics can only penalize missing/negative setups

MAIN/BACKUP, direction, trend alignment, and min-fold WR intentionally receive
zero weight because prior analysis did not show robust forward value.
"""

from __future__ import annotations

from typing import Iterable

QUALITY_VERSION = "atr_featured_v1_1"
DEFAULT_PRIOR_WR = 0.76
PRIOR_STRENGTH = 12.0
ATR_WEIGHT = 0.85
RELIABILITY_WEIGHT = 0.15
FEATURED_COUNT = 10

TIER_A_PLUS = "A+"
TIER_A = "A"
TIER_B = "B"
TIER_WATCH = "WATCH"


def _clamp(value: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, value))


def atr_attainability_score(target_atr_mult) -> float:
    """Map target/ATR to 0-100 for the informational Quality Score.

    <=0.30 is capped at 100 and >=0.75 at zero. Raw target_atr_mult, not this
    capped component, controls A+ Featured ordering in v1.1.
    """
    if target_atr_mult is None:
        return 0.0
    try:
        x = float(target_atr_mult)
    except (TypeError, ValueError):
        return 0.0
    if x <= 0.30:
        return 100.0
    if x >= 0.75:
        return 0.0
    return round(100.0 * (0.75 - x) / 0.45, 2)


def bayesian_reliability_score(wins, losses, backtest_wr=None) -> float:
    """Return a 0-100 shrunk reliability score.

    The optimizer backtest WR is treated as a 12-trade prior. If unavailable or
    implausible, use the historical system baseline (76%). This component is
    informational for A+ selection in v1.1.
    """
    try:
        w = max(0, int(wins or 0))
        l = max(0, int(losses or 0))
    except (TypeError, ValueError):
        w, l = 0, 0

    try:
        prior = float(backtest_wr)
    except (TypeError, ValueError):
        prior = DEFAULT_PRIOR_WR
    if not 0.50 <= prior <= 0.95:
        prior = DEFAULT_PRIOR_WR

    posterior = (w + PRIOR_STRENGTH * prior) / (w + l + PRIOR_STRENGTH)
    return round(_clamp(posterior * 100.0, 0.0, 100.0), 2)


def option_economics_adjustment(entry_premium, est_return_pct) -> float:
    """Penalty-only option component for the informational Quality Score.

    Any non-negative modeled return receives no penalty or bonus. Missing
    priceability is penalized modestly; a negative modeled return at target is
    penalized more heavily and is also non-actionable for A+ selection.
    """
    try:
        premium = float(entry_premium)
    except (TypeError, ValueError):
        premium = 0.0

    if premium <= 0 or est_return_pct is None:
        return -7.5
    try:
        ret = float(est_return_pct)
    except (TypeError, ValueError):
        return -7.5
    if ret < 0:
        return -15.0
    return 0.0


def score_signal(*, target_atr_mult, alltime_wins=0, alltime_losses=0,
                 backtest_wr=None, entry_premium=None, est_return_pct=None) -> dict:
    """Build auditable v1.1 score components for one signal."""
    atr_score = atr_attainability_score(target_atr_mult)
    reliability_score = bayesian_reliability_score(
        alltime_wins, alltime_losses, backtest_wr
    )
    option_adjustment = option_economics_adjustment(
        entry_premium, est_return_pct
    )
    raw = (
        ATR_WEIGHT * atr_score
        + RELIABILITY_WEIGHT * reliability_score
        + option_adjustment
    )
    score = round(_clamp(raw, 0.0, 100.0), 2)
    return {
        "quality_version": QUALITY_VERSION,
        "quality_score": score,
        "quality_atr_score": atr_score,
        "quality_reliability_score": reliability_score,
        "quality_option_adjustment": option_adjustment,
    }


def _option_actionable(signal: dict) -> bool:
    try:
        premium = float(signal.get("entry_premium"))
    except (TypeError, ValueError):
        return False
    ret = signal.get("est_return_pct")
    try:
        return premium > 0 and ret is not None and float(ret) >= 0
    except (TypeError, ValueError):
        return False


def _raw_atr_key(signal: dict):
    """Deterministic A+ ordering: easiest raw target/ATR first."""
    try:
        atr = float(signal.get("target_atr_mult"))
    except (TypeError, ValueError):
        atr = float("inf")
    return (
        atr,
        str(signal.get("ticker") or ""),
        str(signal.get("direction") or ""),
    )


def rank_signals(signals: Iterable[dict], featured_count: int = FEATURED_COUNT) -> list[dict]:
    """Rank a Monday cohort and assign A+/A/B/WATCH tiers.

    - every signal receives a global informational quality_rank;
    - top N actionable signals by raw target_atr_mult become A+ Featured;
    - Bayesian reliability never reorders A+ in v1.1;
    - non-featured A/B thresholds remain based on the informational score;
    - missing target/ATR or option economics forces WATCH;
    - negative modeled option return forces WATCH/non-featured;
    - all signals remain in the cohort and database.
    """
    rows = [dict(s) for s in signals]

    def quality_rank_key(s):
        score = float(s.get("quality_score") or 0.0)
        try:
            atr = float(s.get("target_atr_mult"))
        except (TypeError, ValueError):
            atr = 999.0
        return (-score, atr, str(s.get("ticker") or ""))

    rows.sort(key=quality_rank_key)
    for idx, s in enumerate(rows, start=1):
        s["quality_rank"] = idx
        s["featured_rank"] = None
        s["is_featured"] = False

    eligible = sorted(
        (
            s for s in rows
            if s.get("target_atr_mult") is not None and _option_actionable(s)
        ),
        key=_raw_atr_key,
    )

    featured_map = {}
    for featured_rank, s in enumerate(
        eligible[:max(0, int(featured_count))], start=1
    ):
        ident = (
            s.get("id")
            if s.get("id") is not None
            else (s.get("ticker"), s.get("direction"))
        )
        featured_map[ident] = featured_rank

    for s in rows:
        ident = (
            s.get("id")
            if s.get("id") is not None
            else (s.get("ticker"), s.get("direction"))
        )
        featured_rank = featured_map.get(ident)
        featured = featured_rank is not None
        s["featured_rank"] = featured_rank
        s["is_featured"] = featured

        if s.get("target_atr_mult") is None or not _option_actionable(s):
            tier = TIER_WATCH
        elif featured:
            tier = TIER_A_PLUS
        else:
            atr = float(s["target_atr_mult"])
            score = float(s.get("quality_score") or 0.0)
            if score >= 65.0 and atr <= 0.45:
                tier = TIER_A
            elif score >= 45.0 and atr <= 0.55:
                tier = TIER_B
            else:
                tier = TIER_WATCH
        s["quality_tier"] = tier

    return rows

