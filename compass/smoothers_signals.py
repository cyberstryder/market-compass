# Source: cyberstryder/smoothers-new shared/signals.py; preserved for migration parity.
"""Signal classification, week boundaries, strike rounding."""

import numpy as np
import pandas as pd
from datetime import date, timedelta


def classify_signal(d1: int, d2: int, d3: int) -> tuple[str | None, str | None]:
    """Returns (signal_type, direction). Matches the Pine indicator logic."""
    main_dir = d1 + d2
    if main_dir == 2:
        return ("MAIN", "CALL")
    if main_dir == -2:
        return ("MAIN", "PUT")
    if d3 == d1:
        return ("BACKUP", "CALL" if d1 == 1 else "PUT")
    if d3 == d2:
        return ("BACKUP", "CALL" if d2 == 1 else "PUT")
    return (None, None)


def get_week_start_indices(bars: pd.DataFrame) -> np.ndarray:
    """Return integer indices for the first bar of every ISO week present."""
    iso = bars.index.isocalendar()
    week_id = (iso.year.astype(int) * 100 + iso.week.astype(int)).values
    is_new = np.concatenate(([True], np.diff(week_id) != 0))
    return np.where(is_new)[0]


def nearest_standard_strike(price: float) -> float:
    """Approximate the nearest standard option strike for an underlying price.
    The Alpaca chain lookup overrides this if a real chain is available; this
    is the fallback when chain data isn't fetched."""
    if price < 50:
        interval = 0.5
    elif price < 200:
        interval = 1.0
    elif price < 500:
        interval = 2.5
    else:
        interval = 5.0
    return round(price / interval) * interval


def week_id_for(d: date) -> int:
    iso = d.isocalendar()
    return iso.year * 100 + iso.week


def friday_of_week(monday_d: date) -> date:
    """Friday of the same ISO week (Monday + 4 days)."""
    return monday_d + timedelta(days=4)


def build_occ_symbol(ticker: str, expiry_d: date, call_put: str, strike: float) -> str:
    """OCC option symbol: TICKER YYMMDD C|P SSSSSSSS (strike × 1000, padded 8)."""
    yymmdd = expiry_d.strftime("%y%m%d")
    cp = "C" if call_put.upper().startswith("C") else "P"
    strike_int = int(round(strike * 1000))
    return f"{ticker}{yymmdd}{cp}{strike_int:08d}"

