"""Gamma flip level from TraderMatrix signed per-strike net GEX.

Evidence only: a regime-context input, never an entry trigger. The flip is the
underlying price where cumulative dealer gamma changes sign:

- spot above the flip -> dealers long gamma -> hedging dampens moves
  (mean-reversion regime; fade extremes, distrust breakouts)
- spot below the flip -> dealers short gamma -> hedging amplifies moves
  (trend regime; momentum works, stops get run)

Sign-convention validation is built in: the flip only exists when the vendor's
per-strike GEX actually carries mixed signs. All-positive (or all-negative)
vendor GEX means the sign convention is unverified and no flip is reported.
"""

MIN_STRIKES = 5


def compute_flip(strikes, spot=None):
    """Compute the gamma flip from matrix strike rows.

    strikes: iterable of {"strike": float, "gex": float|None} with SIGNED net
        dealer GEX per strike (negative = dealer short gamma).
    spot: underlying price, optional.

    Returns a dict with flip/regime/validation fields. flip is None when the
    vendor data cannot support a flip level.
    """
    rows = sorted(
        (r for r in strikes
         if isinstance(r, dict) and (r.get("strike") or 0) > 0
         and r.get("gex") is not None and r.get("gex") != 0),
        key=lambda r: r["strike"],
    )
    n = len(rows)
    if n < MIN_STRIKES:
        return {"status": "insufficient", "flip": None,
                "reason": f"only {n} populated strikes (need {MIN_STRIKES})",
                "strikes": n}
    signs = {1 if r["gex"] > 0 else -1 for r in rows}
    mixed = signs == {1, -1}
    if not mixed:
        return {"status": "single-signed", "flip": None,
                "reason": "vendor GEX is single-signed; net-gamma sign convention unverified, no flip level",
                "strikes": n, "mixed_signs": False}

    # Cumulative net gamma from low strikes upward; the flip is the
    # zero-crossing, linearly interpolated between the straddling strikes.
    flip = None
    prev_strike, prev_cum = None, 0.0
    for r in rows:
        cum = prev_cum + r["gex"]
        if prev_strike is not None and (prev_cum == 0 or cum == 0 or
                                        (prev_cum < 0) != (cum < 0)):
            if cum == 0:
                flip = float(r["strike"])
            elif prev_cum == 0:
                flip = float(prev_strike)
            else:
                frac = abs(prev_cum) / (abs(prev_cum) + abs(cum))
                flip = float(prev_strike + frac * (r["strike"] - prev_strike))
            break
        prev_strike, prev_cum = r["strike"], cum

    if flip is None:
        return {"status": "no-crossing", "flip": None,
                "reason": "cumulative net gamma never crosses zero across strikes",
                "strikes": n, "mixed_signs": True}

    out = {"status": "ok", "flip": flip, "strikes": n, "mixed_signs": True,
           "method": ("cumulative signed vendor GEX from low strikes; "
                      "vendor position assumptions unverified")}
    if spot and spot > 0:
        out["spot"] = float(spot)
        out["distance_pct"] = round((spot - flip) / flip * 100, 3)
        out["regime"] = ("above_flip" if spot > flip else
                         "below_flip" if spot < flip else "at_flip")
    return out
