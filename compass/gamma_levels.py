"""Gamma levels from TraderMatrix signed per-strike net GEX.

MPhinance-style dealer levels: call wall, put wall, neutral gamma, and the
heaviest concentration strike. Evidence only: regime context for 0DTE, never
an entry trigger.

The vendor matrix carries SIGNED net dealer GEX per strike (positive = dealer
long gamma, negative = dealer short gamma). Call/put walls are inferred from
the sign extremes:

- call_wall: strike with the largest positive net GEX (dealers longest gamma
  -> resistance; fades work above the flip, breakouts fail)
- put_wall: strike with the most negative net GEX (dealers shortest gamma
  -> support; momentum accelerates below the flip)
- neutral_gamma: the gamma flip zero-crossing (magnet zone; market makers
  drag price here when pinned between walls)
- concentration: strike with the largest |GEX| (heaviest dealer hedging)

Sign-convention caveat (same as gamma_flip): if the vendor's per-strike GEX
is single-signed, the sign convention is unverified and no directional wall
assignment is made. Concentrations are reported as concentrations, not as
verified dealer-inventory levels.
"""

MIN_STRIKES = 5


def _rows(strikes):
    return sorted(
        (r for r in strikes
         if isinstance(r, dict) and (r.get("strike") or 0) > 0
         and r.get("gex") is not None and r.get("gex") != 0),
        key=lambda r: r["strike"],
    )


def compute_levels(strikes, spot=None):
    """Compute dealer gamma levels from matrix strike rows.

    strikes: iterable of {"strike": float, "gex": float|None} with SIGNED net
        dealer GEX per strike.
    spot: underlying price, optional.

    Returns a dict with call_wall/put_wall/neutral_gamma/concentration,
    regime vs the flip, and methodology caveats.
    """
    rows = _rows(strikes)
    n = len(rows)
    if n < MIN_STRIKES:
        return {"status": "insufficient", "levels": None,
                "reason": f"only {n} populated strikes (need {MIN_STRIKES})",
                "strikes": n}

    positives = [r for r in rows if r["gex"] > 0]
    negatives = [r for r in rows if r["gex"] < 0]
    mixed = bool(positives and negatives)

    call_wall = max(positives, key=lambda r: r["gex"])["strike"] if positives else None
    put_wall = min(negatives, key=lambda r: r["gex"])["strike"] if negatives else None
    concentration = max(rows, key=lambda r: abs(r["gex"]))["strike"]

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
    }

    out = {
        "status": "ok",
        "levels": levels,
        "strikes": n,
        "mixed_signs": mixed,
        "flip_status": flip_status,
        "method": ("Signed vendor GEX per strike; call/put walls inferred from "
                   "sign extremes, not a dealer-inventory or direction claim. "
                   "Concentrations are not verified walls or flip levels."),
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
