# Source: cyberstryder/smoothers-new shared/monitoring.py; preserved for migration parity.
"""Pure monitoring helpers for model-entry timing and first target touches."""

from datetime import date, datetime, time as dtime

import pandas as pd
import pytz

ET = pytz.timezone("America/New_York")


def model_entry_time_for_session_utc(session_date: date) -> datetime:
    """Return 10:30 ET for the actual first trading session of the week."""
    local = ET.localize(datetime.combine(session_date, dtime(10, 30)))
    return local.astimezone(pytz.UTC)


def model_entry_time_utc(monday_date: date) -> datetime:
    """Backward-compatible Monday entry boundary used by historical callers."""
    return model_entry_time_for_session_utc(monday_date)


def first_target_touch(bars: pd.DataFrame, target: float, direction: str):
    if bars is None or bars.empty:
        return None
    direction = str(direction).upper()
    if direction == "CALL":
        hits = bars[bars["high"] >= float(target)]
    elif direction == "PUT":
        hits = bars[bars["low"] <= float(target)]
    else:
        raise ValueError(f"unsupported direction: {direction}")
    if hits.empty:
        return None
    ts = hits.index[0]
    row = hits.iloc[0]
    if hasattr(ts, "to_pydatetime"):
        ts = ts.to_pydatetime()
    return {
        "hit_time": ts,
        "exit_price": float(target),
        "bar_high": float(row["high"]),
        "bar_low": float(row["low"]),
    }

