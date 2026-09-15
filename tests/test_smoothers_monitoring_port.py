import unittest
from datetime import date

import pandas as pd
import pytz

from compass.smoothers_monitoring import first_target_touch, model_entry_time_utc


class MonitoringTests(unittest.TestCase):
    def _bars(self):
        idx = pd.DatetimeIndex([
            "2026-08-24T14:30:00Z",
            "2026-08-24T14:35:00Z",
            "2026-08-24T14:40:00Z",
        ])
        return pd.DataFrame(
            {
                "high": [100.5, 101.2, 102.0],
                "low": [99.8, 99.5, 98.7],
                "close": [100.2, 100.6, 99.2],
            },
            index=idx,
        )

    def test_call_uses_first_touch_and_target_fill(self):
        hit = first_target_touch(self._bars(), 101.0, "CALL")
        self.assertEqual(hit["hit_time"], pd.Timestamp("2026-08-24T14:35:00Z").to_pydatetime())
        self.assertEqual(hit["exit_price"], 101.0)
        self.assertEqual(hit["bar_high"], 101.2)

    def test_put_uses_first_touch_and_target_fill(self):
        hit = first_target_touch(self._bars(), 99.6, "PUT")
        self.assertEqual(hit["hit_time"], pd.Timestamp("2026-08-24T14:35:00Z").to_pydatetime())
        self.assertEqual(hit["exit_price"], 99.6)
        self.assertEqual(hit["bar_low"], 99.5)

    def test_model_entry_is_1430_utc_during_edt(self):
        self.assertEqual(
            model_entry_time_utc(date(2026, 8, 24)),
            pytz.UTC.localize(pd.Timestamp("2026-08-24 14:30:00").to_pydatetime()),
        )

    def test_model_entry_is_1530_utc_during_est(self):
        self.assertEqual(
            model_entry_time_utc(date(2026, 11, 9)),
            pytz.UTC.localize(pd.Timestamp("2026-11-09 15:30:00").to_pydatetime()),
        )


if __name__ == "__main__":
    unittest.main()

