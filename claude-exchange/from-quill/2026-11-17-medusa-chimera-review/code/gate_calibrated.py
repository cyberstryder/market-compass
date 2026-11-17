"""
gate_calibrated.py — DESK CALIBRATED COPY of medusa_regime_gate.py
═════════════════════════════════════════════════════════════════════════════
Identical to the uploaded gate EXCEPT the spacelike bar filter, recalibrated
per ~/workspace/medusa/spacelike-calibration-2026-11-16.md (Erik's direction,
2026-11-17): velocity in POINTS per bar, diffusion scaling (per-bar sigma =
ATR / sqrt(78)), speed limit = 5.0 x per-bar sigma. The uploaded version
compared a log-return fraction against ATR/78 in points and could never fire.
Everything else — pressure accumulator, osmosis ladder, thresholds
0.35/0.55/0.72, WAIT at 0.28, BEAR_3/BULL_3 immunity — is the author's,
unchanged. Lorentz weight shape kept: weight = sqrt(1 - beta^2), so the
gate's own spacelike test (weight < 0.60) trips at beta > 0.8, i.e. a bar
move above ~4x per-bar sigma (~36 pts at ATR 80).
═════════════════════════════════════════════════════════════════════════════
MEDUSA-ORACLE v2.6 — Regime Gate (drop-in addition to medusa_unified.py)

PURPOSE
───────
Solves the bear-side misclassification without touching the 12-component
score or any existing logic. Works as a post-classification gate applied
AFTER classify_regime() returns a state.

THREE MECHANISMS
────────────────
1. Spacelike bar filter (from SRF/Lorentz concept)
   Anomalously fast bars near 14:30 are down-weighted before classification.
   A bar that moves > c_mkt × ATR in one 5-min period is "spacelike" — it's
   a shock, not a trend signal. Down-weighting it prevents a late bounce from
   masquerading as a regime change.

2. Contrarian pressure accumulator (in-vivo reclassification)
   Runs on the last 60 minutes of bars (13:30→14:30, ~12 bars).
   Each bar, counts the weighted sum of the 12 components firing AGAINST
   the current regime direction. When accumulated pressure crosses a
   threshold, the regime osmoses one step toward NEUTRAL.
   Graduated: higher pressure = more steps.
   Guards: BEAR_3 and BULL_3 are immune (100% accuracy, don't touch).

3. Wait signal (14:45 option)
   If after osmosis the regime lands in an ambiguous zone AND contrarian
   pressure is still building, output WAIT instead of a weak signal.
   The caller can re-run at 14:45 with 3 more bars for confirmation.

HOW TO INTEGRATE INTO medusa_unified.py
────────────────────────────────────────
Step 1 — Import at the top of medusa_unified.py:
    from medusa_regime_gate import compute_bar_weight, apply_regime_gate

Step 2 — In classify_regime(), after the state is assigned (after line 403),
    before the return statement, add:
        gate_result = apply_regime_gate(state, effective_score,
                                        bars, atr, trend_clarity)
        state               = gate_result["state"]
        gate_osmosis        = gate_result["osmosis_steps"]
        gate_wait           = gate_result["wait_signal"]
        gate_pressure       = gate_result["contrarian_pressure"]

Step 3 — Add to the return dict:
        "osmosis_steps":        gate_osmosis,
        "wait_signal":          gate_wait,
        "contrarian_pressure":  round(gate_pressure, 4),

Step 4 — In build_live_brief() / direction_score(), check gate_wait:
        if regime.get("wait_signal"):
            → print "WAIT — recheck at 14:45"
            → do not fire a trade signal

PARAMETERS (tune via Monte Carlo)
──────────────────────────────────
C_MKT_MULT      : speed limit multiplier on ATR (default 0.8 × ATR per bar)
PRESSURE_L1     : contrarian pressure for 1-step osmosis  (default 0.35)
PRESSURE_L2     : contrarian pressure for 2-step osmosis  (default 0.55)
PRESSURE_L3     : contrarian pressure for 3-step osmosis  (default 0.72)
LATE_WINDOW     : bars from end of session to examine     (default 12 = 60min)
WAIT_THRESHOLD  : pressure above this → emit WAIT signal  (default 0.28)
"""

import numpy as np
import pandas as pd

# ─────────────────────────────────────────────────────────────────────────────
# TUNABLE PARAMETERS  (sweep these in Monte Carlo)
# ─────────────────────────────────────────────────────────────────────────────

C_MKT_MULT   = 5.00   # DESK CALIBRATION: speed limit = C_MKT_MULT × (ATR / sqrt(78)) in POINTS
PRESSURE_L1  = 0.35   # 1-step osmosis trigger
PRESSURE_L2  = 0.55   # 2-step osmosis trigger
PRESSURE_L3  = 0.72   # 3-step osmosis trigger
LATE_WINDOW  = 12     # bars to examine (last 60 min of 5-min session)
WAIT_THRESHOLD = 0.28 # pressure → emit WAIT instead of weak signal

# Component weights — must mirror medusa_unified.py score assembly
COMPONENT_WEIGHTS = {
    "price_struct": 0.20,
    "vwap_pos":     0.08,
    "hh_ll":        0.07,
    "vwap_slope":   0.08,
    "macd":         0.10,
    "macd_accel":   0.10,
    "vwmacd":       0.15,
    "vol_surge":    0.10,
    "accel":        0.15,
    "dmacd":        0.08,
    "vix":          0.05,
    "trend_clarity":0.10,
}

# Osmosis ladder — regime states in order from most bear to most bull
REGIME_LADDER = [
    "BEAR_3", "BEAR_2", "BEAR_1",
    "NEUTRAL_BEAR", "NEUTRAL_BEAR_CHOPPY",
    "NEUTRAL",
    "NEUTRAL_BULL_CHOPPY", "NEUTRAL_BULL",
    "BULL_1", "BULL_2", "BULL_3",
]

# States immune to osmosis — proven accurate, never touch
IMMUNE_STATES = {"BEAR_3", "BULL_3"}

# ─────────────────────────────────────────────────────────────────────────────
# 1. SPACELIKE BAR FILTER
# ─────────────────────────────────────────────────────────────────────────────

def compute_bar_weights(bars: pd.DataFrame, atr: float,
                        c_mkt_mult: float = C_MKT_MULT) -> np.ndarray:
    """
    Returns an array of weights (0.0–1.0) for each bar in the session.
    Normal bars → weight 1.0
    Spacelike bars (fast shocks) → weight reduced by 1/gamma_p

    A bar is spacelike if its absolute move IN POINTS exceeds the market
    speed limit: c_mkt = c_mkt_mult × (ATR / sqrt(78)) where 78 is the
    approximate number of 5-min bars in a full session.
    ATR is a daily figure; diffusion scaling (sqrt) gives the per-bar
    equivalent sigma. (Desk calibration — see file header.)

    The Lorentz factor gamma_p = 1 / sqrt(1 - beta_p²) grows as the
    bar velocity approaches the speed limit. Weight = 1 / gamma_p,
    so a bar moving at 90% of c_mkt gets weight ~0.44 instead of 1.0.
    """
    if bars is None or len(bars) < 2:
        return np.ones(len(bars) if bars is not None else 0)

    bars = bars.copy()
    bars.columns = [c.lower() for c in bars.columns]
    closes = bars["close"].ffill().values.astype(float)

    # DESK CALIBRATION — velocity in POINTS, diffusion-scaled limit.
    # Per-bar sigma = ATR / sqrt(78) (78 five-min bars/session); the speed
    # limit is c_mkt_mult x that sigma, in the same points as the bar move.
    # (Uploaded version divided ATR by 78 and compared against a log-return
    # fraction — units never met, filter could never fire.)
    bars_per_day = 78.0
    sigma_bar = atr / np.sqrt(bars_per_day)
    c_mkt = c_mkt_mult * sigma_bar
    if c_mkt <= 0:
        return np.ones(len(bars))

    weights = np.ones(len(bars))
    for i in range(1, len(closes)):
        if closes[i - 1] <= 0:
            continue
        # Bar move in price points
        delta_x = abs(closes[i] - closes[i - 1])
        # Dimensionless velocity β_p = bar move / speed limit
        beta_p = delta_x / c_mkt
        beta_p = min(beta_p, 0.9999)  # numerical guard
        # Lorentz factor
        gamma_p = 1.0 / np.sqrt(1.0 - beta_p ** 2)
        # Weight: 1/gamma_p — normal bar=1.0, shock bar approaches 0
        weights[i] = 1.0 / gamma_p

    return weights


# ─────────────────────────────────────────────────────────────────────────────
# 2. CONTRARIAN PRESSURE ACCUMULATOR
# ─────────────────────────────────────────────────────────────────────────────

def _regime_direction(state: str) -> int:
    """Returns +1 for bull regimes, -1 for bear regimes, 0 for neutral."""
    if state in ("BULL_1", "BULL_2", "BULL_3",
                 "NEUTRAL_BULL", "NEUTRAL_BULL_CHOPPY"):
        return +1
    if state in ("BEAR_1", "BEAR_2", "BEAR_3",
                 "NEUTRAL_BEAR", "NEUTRAL_BEAR_CHOPPY"):
        return -1
    return 0  # NEUTRAL


def _compute_contrarian_pressure(bars: pd.DataFrame,
                                 state: str,
                                 atr: float,
                                 s0: float,
                                 late_window: int = LATE_WINDOW) -> float:
    """
    Measures how strongly the last `late_window` bars are firing against
    the current regime direction.

    Returns a pressure score 0.0–1.0:
      0.0 = all components confirm the regime
      1.0 = all components are firing against the regime

    Methodology:
    - Take the last `late_window` bars (13:30→14:30 in a 5-min session)
    - For each bar, recompute a simplified 4-component directional score
      using the highest-weight components only (price_struct, vwmacd, accel,
      macd) — the ones that respond fastest to intraday reversals
    - Apply spacelike bar weights (fast shocks count less)
    - If the weighted score disagrees with the regime direction → pressure
    """
    regime_dir = _regime_direction(state)
    if regime_dir == 0:
        return 0.0  # neutral regime → no contrarian pressure possible

    if bars is None or len(bars) < 4:
        return 0.0

    bars = bars.copy()
    bars.columns = [c.lower() for c in bars.columns]

    # Take last `late_window` bars
    late_bars = bars.iloc[-late_window:].reset_index(drop=True)
    if len(late_bars) < 4:
        return 0.0

    bar_weights = compute_bar_weights(late_bars, atr)
    prices  = late_bars["close"].ffill().values.astype(float)
    volumes = late_bars["volume"].fillna(1.0).values.astype(float)

    pressure_sum   = 0.0
    weight_sum     = 0.0

    # Anchor for price_struct: use the START of the late window, not prior-day s0.
    # This measures whether price is rising/falling WITHIN the late session,
    # not vs. yesterday's close (which is already captured by the full session score).
    late_anchor = prices[0]

    for i in range(3, len(late_bars)):
        bw = bar_weights[i]  # spacelike weight for this bar

        # -- Price structure vs late-window anchor
        move = prices[i] - late_anchor
        move_norm = move / atr if atr > 0 else 0.0
        price_dir = np.sign(move_norm) if abs(move_norm) > 0.02 else 0

        # -- MACD proxy (3-bar momentum vs 6-bar momentum)
        if i >= 5:
            mom_fast = prices[i] - prices[i - 2]
            mom_slow = prices[i] - prices[i - 5]
            macd_dir = np.sign(mom_fast - mom_slow * 0.5)
        else:
            macd_dir = np.sign(prices[i] - prices[0])

        # -- Acceleration (second derivative proxy)
        if i >= 2:
            d1 = prices[i] - prices[i - 1]
            d2 = prices[i - 1] - prices[i - 2]
            accel_dir = np.sign(d1 - d2)
        else:
            accel_dir = 0.0

        # -- Volume-weighted direction
        vol_avg = volumes.mean()
        vw_dir  = np.sign(prices[i] - prices[i - 1]) if vol_avg > 0 else 0

        # Weighted composite direction for this bar
        bar_dir = (
            price_dir * 0.35 +
            macd_dir  * 0.25 +
            accel_dir * 0.25 +
            vw_dir    * 0.15
        )

        # Is this bar contrarian to the regime?
        if regime_dir > 0:
            # Bull regime — contrarian = bar_dir < 0
            contrarian = max(0.0, -bar_dir)
        else:
            # Bear regime — contrarian = bar_dir > 0
            contrarian = max(0.0, bar_dir)

        # Time weighting: later bars count more (closer to 14:30)
        time_weight = (i + 1) / len(late_bars)

        pressure_sum += contrarian * bw * time_weight
        weight_sum   += bw * time_weight

    if weight_sum <= 0:
        return 0.0

    return min(1.0, pressure_sum / weight_sum)


# ─────────────────────────────────────────────────────────────────────────────
# 3. OSMOSIS STEP
# ─────────────────────────────────────────────────────────────────────────────

def _osmose(state: str, steps: int) -> str:
    """
    Walk the regime ladder `steps` steps toward NEUTRAL.
    Positive steps = toward neutral from bear side.
    Negative steps = toward neutral from bull side.
    """
    if state not in REGIME_LADDER:
        return state
    idx = REGIME_LADDER.index(state)
    neutral_idx = REGIME_LADDER.index("NEUTRAL")

    if idx < neutral_idx:
        # Bear side → move right (toward neutral)
        new_idx = min(idx + steps, neutral_idx)
    elif idx > neutral_idx:
        # Bull side → move left (toward neutral)
        new_idx = max(idx - steps, neutral_idx)
    else:
        new_idx = idx  # already neutral

    return REGIME_LADDER[new_idx]


# ─────────────────────────────────────────────────────────────────────────────
# 4. MAIN GATE FUNCTION
# ─────────────────────────────────────────────────────────────────────────────

def apply_regime_gate(state: str,
                      effective_score: float,
                      bars: pd.DataFrame,
                      atr: float,
                      s0: float,
                      trend_clarity: float = 0.0,
                      late_window: int = LATE_WINDOW,
                      pressure_l1: float = PRESSURE_L1,
                      pressure_l2: float = PRESSURE_L2,
                      pressure_l3: float = PRESSURE_L3,
                      wait_threshold: float = WAIT_THRESHOLD) -> dict:
    """
    Main gate. Call after classify_regime() returns a state.

    Returns dict:
      state               : final (possibly osmosed) regime state
      original_state      : state before osmosis
      osmosis_steps       : how many ladder steps were applied (0 = no change)
      wait_signal         : True → don't trade at 14:30, recheck at 14:45
      contrarian_pressure : raw pressure score 0.0–1.0
      spacelike_detected  : True if any late bar was flagged as anomalous
    """
    original_state = state
    osmosis_steps  = 0
    wait_signal    = False

    # --- Guard: immune states (BEAR_3, BULL_3) — never touch ---
    if state in IMMUNE_STATES:
        return {
            "state":               state,
            "original_state":      original_state,
            "osmosis_steps":       0,
            "wait_signal":         False,
            "contrarian_pressure": 0.0,
            "spacelike_detected":  False,
        }

    # --- Guard: NEUTRAL family — osmosis can only upgrade, not needed ---
    # (neutral states can gain confidence from trend_clarity agreement,
    #  but we don't osmose them further toward NEUTRAL — they're already there)

    # --- Compute contrarian pressure from last 60 min of bars ---
    pressure = _compute_contrarian_pressure(bars, state, atr, s0, late_window)

    # --- Detect spacelike bars in the late window ---
    spacelike_detected = False
    if bars is not None and len(bars) >= late_window:
        late_bars = bars.iloc[-late_window:]
        late_bars = late_bars.copy()
        late_bars.columns = [c.lower() for c in late_bars.columns]
        bw = compute_bar_weights(late_bars, atr)
        spacelike_detected = bool(np.any(bw < 0.60))

    # --- ADX/DMI agreement check ---
    # If trend_clarity strongly disagrees with state direction → add pressure
    regime_dir = _regime_direction(state)
    if regime_dir != 0:
        clarity_disagrees = (regime_dir > 0 and trend_clarity < -0.15) or \
                            (regime_dir < 0 and trend_clarity >  0.15)
        if clarity_disagrees:
            pressure = min(1.0, pressure + 0.12)

        # If ADX/DMI weakly agrees (near zero) on a strong regime → mild pressure
        clarity_weak = abs(trend_clarity) < 0.08 and state in (
            "BEAR_1", "BEAR_2", "NEUTRAL_BEAR", "BULL_1", "BULL_2", "NEUTRAL_BULL")
        if clarity_weak:
            pressure = min(1.0, pressure + 0.06)

    # --- Apply osmosis based on pressure level ---
    if state not in IMMUNE_STATES and _regime_direction(state) != 0:
        if pressure >= pressure_l3:
            osmosis_steps = 3
        elif pressure >= pressure_l2:
            osmosis_steps = 2
        elif pressure >= pressure_l1:
            osmosis_steps = 1

        if osmosis_steps > 0:
            state = _osmose(state, osmosis_steps)

    # --- Wait signal ---
    # Emit WAIT if:
    # (a) pressure is building but not yet at osmosis threshold, OR
    # (b) osmosis just moved us to an ambiguous boundary state
    ambiguous_after_osmosis = state in ("NEUTRAL_BEAR", "NEUTRAL_BULL",
                                         "NEUTRAL_BEAR_CHOPPY", "NEUTRAL_BULL_CHOPPY")
    if (pressure >= wait_threshold and osmosis_steps == 0) or \
       (osmosis_steps > 0 and ambiguous_after_osmosis):
        wait_signal = True

    return {
        "state":               state,
        "original_state":      original_state,
        "osmosis_steps":       osmosis_steps,
        "wait_signal":         wait_signal,
        "contrarian_pressure": round(pressure, 4),
        "spacelike_detected":  spacelike_detected,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 5. MONTE CARLO PARAMETER SWEEP (standalone utility)
# ─────────────────────────────────────────────────────────────────────────────

def monte_carlo_gate_params(results_df, bars_dict: dict,
                             atr_dict: dict, s0_dict: dict,
                             n_trials: int = 1000,
                             random_state: int = 42) -> pd.DataFrame:
    """
    Sweep gate parameters over historical results to find optimal thresholds.

    results_df  : medusa_v2_results.csv as DataFrame
    bars_dict   : dict of {date: bars_DataFrame} for each backtest day
    atr_dict    : dict of {date: atr_value}
    s0_dict     : dict of {date: s0_value}
    n_trials    : number of random parameter combinations to test

    Returns DataFrame of trial results sorted by ITM% improvement.

    Usage:
        import pandas as pd
        from medusa_regime_gate import monte_carlo_gate_params
        df = pd.read_csv('medusa_v2_results.csv')
        # (populate bars_dict, atr_dict, s0_dict from your backtest run)
        sweep = monte_carlo_gate_params(df, bars_dict, atr_dict, s0_dict)
        print(sweep.head(20))
    """
    rng = np.random.default_rng(random_state)

    # Filter to classified days only
    real = results_df[~results_df['regime_state'].isin(['UNKNOWN_D', 'UNKNOWN'])].copy()
    real['move'] = real['actual'] - real['s0']

    trial_results = []

    for trial in range(n_trials):
        # Random parameter sample
        p_l1  = rng.uniform(0.20, 0.50)
        p_l2  = rng.uniform(p_l1 + 0.10, 0.70)
        p_l3  = rng.uniform(p_l2 + 0.10, 0.90)
        p_wt  = rng.uniform(0.15, p_l1)
        p_cmt = rng.uniform(0.50, 1.20)
        p_lw  = int(rng.integers(8, 16))

        itm_hits = 0
        total    = 0

        for _, row in real.iterrows():
            date = str(row['date'])
            if date not in bars_dict:
                continue

            bars = bars_dict[date]
            atr  = atr_dict.get(date, 50.0)
            s0   = s0_dict.get(date, row['s0'])

            # Apply gate with trial params
            gate = apply_regime_gate(
                state=row['regime_state'],
                effective_score=row['effective_score'],
                bars=bars, atr=atr, s0=s0,
                trend_clarity=0.0,  # use 0 if not stored
                late_window=p_lw,
                pressure_l1=p_l1,
                pressure_l2=p_l2,
                pressure_l3=p_l3,
                wait_threshold=p_wt,
            )

            if gate['wait_signal']:
                continue  # skip this day — system said wait

            final_state = gate['state']
            dir_signal  = row['dir_signal']
            move        = row['move']

            # ITM check at 12pts OTM (representative threshold)
            otm = 12
            if dir_signal == 'BULL':
                itm = move > otm
            elif dir_signal == 'BEAR':
                itm = move < -otm
            else:
                continue

            itm_hits += int(itm)
            total    += 1

        itm_pct = itm_hits / total * 100 if total > 0 else 0.0

        trial_results.append({
            'trial':        trial,
            'p_l1':         round(p_l1, 3),
            'p_l2':         round(p_l2, 3),
            'p_l3':         round(p_l3, 3),
            'wait_thresh':  round(p_wt, 3),
            'c_mkt_mult':   round(p_cmt, 3),
            'late_window':  p_lw,
            'trades':       total,
            'itm_pct':      round(itm_pct, 2),
        })

    return pd.DataFrame(trial_results).sort_values('itm_pct', ascending=False)


# ─────────────────────────────────────────────────────────────────────────────
# QUICK SELF-TEST
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import pandas as pd
    import numpy as np

    print("medusa_regime_gate.py — self test")

    # Fake 5-min bars: bear morning, bull reversal in last 12 bars
    np.random.seed(42)
    n = 60
    prices = 5300 + np.cumsum(np.random.randn(n) * 2)
    # Force last 12 bars to reverse upward
    prices[-12:] = prices[-13] + np.linspace(0, 25, 12)

    bars = pd.DataFrame({
        "datetime_est": pd.date_range("2026-03-30 09:30", periods=n, freq="5min"),
        "open":   prices - 1,
        "high":   prices + 2,
        "low":    prices - 2,
        "close":  prices,
        "volume": np.abs(np.random.randn(n) * 1e7) + 1e7,
    })

    atr   = 80.0
    s0    = 5320.0

    # Test 1: BEAR_2 with strong reversal in last 12 bars
    result = apply_regime_gate("BEAR_2", -0.45, bars, atr, s0)
    print(f"\nTest 1 — BEAR_2 with bull reversal:")
    print(f"  Original state:      BEAR_2")
    print(f"  Final state:         {result['state']}")
    print(f"  Osmosis steps:       {result['osmosis_steps']}")
    print(f"  Contrarian pressure: {result['contrarian_pressure']}")
    print(f"  Wait signal:         {result['wait_signal']}")
    print(f"  Spacelike detected:  {result['spacelike_detected']}")

    # Test 2: BEAR_3 — immune
    result2 = apply_regime_gate("BEAR_3", -0.75, bars, atr, s0)
    print(f"\nTest 2 — BEAR_3 (immune):")
    print(f"  Final state:         {result2['state']}  (should be BEAR_3)")
    print(f"  Osmosis steps:       {result2['osmosis_steps']}  (should be 0)")

    # Test 3: BEAR_2 with confirming bear bars
    prices_bear = 5300 - np.linspace(0, 40, n)
    bars_bear = bars.copy()
    bars_bear["close"] = prices_bear
    result3 = apply_regime_gate("BEAR_2", -0.45, bars_bear, atr, s0)
    print(f"\nTest 3 — BEAR_2 with confirming bear bars:")
    print(f"  Final state:         {result3['state']}  (should stay BEAR_2)")
    print(f"  Contrarian pressure: {result3['contrarian_pressure']}  (should be low)")
    print(f"  Osmosis steps:       {result3['osmosis_steps']}  (should be 0)")

    print("\nSelf-test complete.")
