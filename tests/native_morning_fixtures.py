from copy import deepcopy
import time

FIXTURE_STAMP = (int(time.time()) // 60 - 120) * 60000

def signal_event(signal_id="fixture-1", stamp=FIXTURE_STAMP, is_test=False):
    # Synthetic fixture, never an observed market result.
    return {
        "schema_version": 1, "event_type": "signal", "event_id": signal_id + "-signal",
        "signal": {
            "signal_id": signal_id, "ticker": "NASDAQ:DEMO", "stream_id": "fixture", "script_version": "1.0.0",
            "logic_mode": "legacy_confirmed", "setup": "STANDARD_ORB", "timeframe_min": 1,
            "signal_at_ms": stamp, "bar_open_ms": stamp - 60000, "price": 100.0,
            "settings": {"orb_bars": 5, "alert_window": 60, "fast_open": True, "fast_min_bar": 2,
                "require_rvol": False, "rvol_threshold": 1.2, "ema_fast": 9, "ema_slow": 21, "checkpoints": True},
            "features": {"rvol": 1.5, "volume": 10000.0, "vwap": 99.5, "ema_fast": 99.7, "ema_slow": 99.3,
                "orb_high": 99.9, "orb_low": 98.5, "bar_count": 6}, "is_test": is_test,
        }, "observation": None,
    }


def checkpoint_event(signal=None, horizon=5, price=101.0, elapsed=None, continuous=True):
    event = deepcopy(signal or signal_event())
    event["event_type"] = "checkpoint"
    event["event_id"] = event["signal"]["signal_id"] + "-" + str(horizon)
    stamp = event["signal"]["signal_at_ms"]
    minutes = elapsed or horizon
    event["observation"] = {
        "horizon_min": horizon, "observed_at_ms": stamp + minutes * 60000, "price": price,
        "window_high": 102.5, "window_low": 99.0, "peak_at_ms": stamp + 3 * 60000,
        "dip_at_ms": stamp + 60000, "hit_1_ms": stamp + 2 * 60000, "hit_2_ms": stamp + 3 * 60000, "hit_3_ms": None,
        "bars_observed": minutes // event["signal"]["timeframe_min"], "continuous": continuous,
    }
    return event

