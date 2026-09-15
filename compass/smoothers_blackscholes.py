# Source-equivalent math, not a guarantee of future option value.
"""Minimal Black-Scholes for the dashboard's "win would be unprofitable" check.

No external dependencies — just math. We need two things:
  1. Back out implied volatility from the current option mark.
  2. Reprice the option at the *target* underlying price, keeping that IV and
     today's remaining time-to-expiry. That repriced value is the best a future
     target-hit could pay (a later hit has even less time value), so if it's
     already below entry premium the trade is a locked loss.

Rates/dividends are ignored (r=0): negligible over a <=5-day weekly.
"""

import math


def _norm_cdf(x: float) -> float:
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def bs_price(S: float, K: float, T: float, sigma: float,
             option_type: str, r: float = 0.0) -> float:
    """Black-Scholes price. option_type: 'CALL' or 'PUT'. T in years."""
    is_call = option_type.upper().startswith("C")
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        return intrinsic
    sqrtT = math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / (sigma * sqrtT)
    d2 = d1 - sigma * sqrtT
    disc = math.exp(-r * T)
    if is_call:
        return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def implied_vol(price: float, S: float, K: float, T: float,
                option_type: str, r: float = 0.0) -> float | None:
    """Recover sigma from an observed price via bisection. Returns None if the
    price is below intrinsic (stale/arb quote) or otherwise unsolvable."""
    is_call = option_type.upper().startswith("C")
    intrinsic = max(S - K, 0.0) if is_call else max(K - S, 0.0)
    if T <= 0 or price <= 0 or price < intrinsic - 1e-6:
        return None
    lo, hi = 1e-4, 5.0
    # Ensure the target price is bracketed; price can't exceed S (call) / K (put).
    if bs_price(S, K, T, hi, option_type, r) < price:
        return None
    for _ in range(80):
        mid = 0.5 * (lo + hi)
        val = bs_price(S, K, T, mid, option_type, r)
        if abs(val - price) < 1e-6:
            return mid
        if val > price:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def value_at_target(entry_premium: float, mark: float, S: float, K: float,
                    target: float, T: float, option_type: str,
                    r: float = 0.0) -> dict:
    """Given the current mark and underlying S, back out IV and reprice the
    contract at underlying=target with the same time/IV. Returns the at-target
    value and whether the position is 'dead' (best-case win < entry premium).

    Falls back to intrinsic-at-target when IV can't be solved (treats remaining
    time value as zero — the conservative expiry-floor case).
    """
    iv = implied_vol(mark, S, K, T, option_type, r)
    if iv is None:
        is_call = option_type.upper().startswith("C")
        vat = max(target - K, 0.0) if is_call else max(K - target, 0.0)
        iv_used = None
    else:
        vat = bs_price(target, K, T, iv, option_type, r)
        iv_used = iv
    dead = (entry_premium is not None and entry_premium > 0 and vat < entry_premium)
    return {"value_at_target": vat, "iv": iv_used, "dead": dead}

