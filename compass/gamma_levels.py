"""Gamma levels from TraderMatrix signed per-strike net GEX.

Methodology (aligned with TraderMatrix's documented wall logic, adapted to
the fields the /gex/{symbol}/matrix endpoint actually returns):

The vendor defines walls from OI weighted by proximity and size, but the
matrix carries no OI field -- only signed net dealer GEX per strike
(positive = dealer long gamma, negative = dealer short gamma). Walls are
therefore inferred from sign-appropriate GEX extremes within spot-relative
regions, with proximity weighting on the concentration:

- call_wall: largest positive net GEX among strikes ABOVE spot
  (dealers longest gamma above -> resistance)
- put_wall: most negative net GEX among strikes BELOW spot
  (dealers shortest gamma below -> support)
- neutral_gamma: the gamma flip zero-crossing (magnet zone)
- concentration: largest proximity-weighted |GEX|
  (weight = 1 / (1 + 10 * |strike - spot| / spot); nearer strikes count more)

Evidence only: regime context for 0DTE, never an entry trigger.

Caveats: these are GEX-derived inferences, not the vendor's OI-based walls.
If the vendor's per-strike GEX is single-signed, the sign convention is
unverified and no directional wall assignment is made.
"""

MIN_STRIKES = 5
PROXIMITY_K = 10.0


def _rows(strikes):
    return sorted(
        (r for r in strikes
         if isinstance(r, dict) and (r.get("strike") or 0) > 0
         and r.get("gex") is not None and r.get("gex") != 0),
        key=lambda r: r["strike"],
    )


def _proximity_weight(strike, spot):
    """1.0 at spot, decaying with distance. 1% away -> ~0.91, 5% -> ~0.67."""
    if not spot or spot <= 0:
        return 1.0
    return 1.0 / (1.0 + PROXIMITY_K * abs(strike - spot) / spot)


def compute_levels(strikes, spot=None):
    """Compute dealer gamma levels from matrix strike rows.

    strikes: iterable of {"strike": float, "gex": float|None} with SIGNED net
        dealer GEX per strike.
    spot: underlying price. Required for spot-relative walls; without it only
        the proximity-unweighted concentration is reported.

    Returns a dict with call_wall/put_wall/neutral_gamma/concentration,
    regime vs the flip, and methodology caveats.
    """
    rows = _rows(strikes)
    n = len(rows)
    if n < MIN_STRIKES:
        return {"status": "insufficient", "levels": None,
                "reason": f"only {n} populated strikes (need {MIN_STRIKES})",
                "strikes": n}

    above = [r for r in rows if spot and r["strike"] > spot]
    below = [r for r in rows if spot and r["strike"] < spot]

    positives_above = [r for r in above if r["gex"] > 0]
    negatives_below = [r for r in below if r["gex"] < 0]
    mixed = bool(positives_above and negatives_below)

    call_wall = (max(positives_above, key=lambda r: r["gex"])["strike"]
                 if positives_above else None)
    put_wall = (min(negatives_below, key=lambda r: r["gex"])["strike"]
                if negatives_below else None)

    # Proximity-weighted concentration: heaviest dealer hedging nearest spot.
    def _wscore(r):
        return abs(r["gex"]) * _proximity_weight(r["strike"], spot)
    concentration = max(rows, key=_wscore)["strike"]

    # Neutral gamma: reuse the flip computation (cumulative net gamma
    # zero-crossing). Imported lazily to keep this module dependency-light.
    neutral = None
    flip_status = None
    try:
        from .gamma_flip import compute_flip
        flip = compute_flip(rows, spot)
        flip_status = flip.get("status")
        if flip.get("status") == "ok":
            neutral = flip.get("flip")
    except Exception:
        flip_status = "error"

    levels = {
        "call_wall": float(call_wall) if call_wall is not None else None,
        "put_wall": float(put_wall) if put_wall is not None else None,
        "neutral_gamma": float(neutral) if neutral is not None else None,
        "concentration": float(concentration),
        "call_wall_gex": next((r["gex"] for r in rows if r["strike"] == call_wall), None),
        "put_wall_gex": next((r["gex"] for r in rows if r["strike"] == put_wall), None),
        "concentration_gex": next((r["gex"] for r in rows if r["strike"] == concentration), None),
        "concentration_weight": round(_proximity_weight(concentration, spot), 3) if spot else None,
    }

    out = {
        "status": "ok",
        "levels": levels,
        "strikes": n,
        "strikes_above_spot": len(above),
        "strikes_below_spot": len(below),
        "mixed_signs": mixed,
        "flip_status": flip_status,
        "method": ("Spot-relative walls from signed vendor GEX: call wall = "
                   "max positive GEX above spot, put wall = min negative GEX "
                   "below spot, concentration = proximity-weighted |GEX|. "
                   "GEX-derived inferences, not the vendor's OI-based walls."),
        "evidence_only": "regime context; never an entry trigger",
    }
    if spot and spot > 0:
        out["spot"] = float(spot)
        if neutral:
            out["distance_to_neutral_pct"] = round((spot - neutral) / neutral * 100, 3)
            out["regime"] = ("above_flip" if spot > neutral else
                             "below_flip" if spot < neutral else "at_flip")
        if call_wall and put_wall:
            if spot > call_wall:
                out["position"] = "above_call_wall"
            elif spot < put_wall:
                out["position"] = "below_put_wall"
            else:
                out["position"] = "between_walls"
    return out


def describe(levels_item):
    """One-line human summary of stored gamma levels for alerts/reports."""
    levels = (levels_item or {}).get("levels") or {}
    parts = []
    if levels.get("call_wall"):
        parts.append(f"Call wall {levels['call_wall']:g}")
    if levels.get("put_wall"):
        parts.append(f"Put wall {levels['put_wall']:g}")
    if levels.get("neutral_gamma"):
        parts.append(f"Neutral {levels['neutral_gamma']:g}")
    if levels.get("concentration"):
        parts.append(f"Heaviest {levels['concentration']:g}")
    regime = (levels_item or {}).get("regime")
    if regime:
        parts.append("dealers " + ("long gamma (dampened)" if regime == "above_flip"
                                  else "short gamma (amplified)" if regime == "below_flip"
                                  else "at flip"))
    if not parts:
        return "gamma levels unavailable"
    return " · ".join(parts)
