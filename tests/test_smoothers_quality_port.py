import unittest

from compass.smoothers_quality import (
    QUALITY_VERSION,
    atr_attainability_score,
    bayesian_reliability_score,
    option_economics_adjustment,
    rank_signals,
    score_signal,
)


class QualityScoreTests(unittest.TestCase):
    def test_atr_score_is_monotonic(self):
        vals = [atr_attainability_score(x) for x in (0.30, 0.35, 0.40, 0.45, 0.55, 0.75)]
        self.assertEqual(vals, sorted(vals, reverse=True))
        self.assertEqual(vals[0], 100.0)
        self.assertEqual(vals[-1], 0.0)

    def test_big_atr_gap_dominates_reliability_extremes(self):
        easy = score_signal(
            target_atr_mult=0.35, alltime_wins=0, alltime_losses=10,
            backtest_wr=0.55, entry_premium=2.0, est_return_pct=50,
        )
        hard = score_signal(
            target_atr_mult=0.55, alltime_wins=10, alltime_losses=0,
            backtest_wr=0.90, entry_premium=2.0, est_return_pct=50,
        )
        self.assertGreater(easy["quality_score"], hard["quality_score"])

    def test_option_component_penalizes_only_missing_or_negative(self):
        self.assertEqual(option_economics_adjustment(2.0, 100), 0.0)
        self.assertEqual(option_economics_adjustment(2.0, 20), 0.0)
        self.assertEqual(option_economics_adjustment(2.0, 0), 0.0)
        self.assertEqual(option_economics_adjustment(None, None), -7.5)
        self.assertEqual(option_economics_adjustment(2.0, -10), -15.0)

    def test_bayesian_reliability_is_shrunk(self):
        score = bayesian_reliability_score(1, 0, 0.75)
        self.assertGreater(score, 75.0)
        self.assertLess(score, 80.0)

    def test_rank_assigns_exact_top_ten_featured_by_raw_atr(self):
        signals = []
        for i in range(15):
            atr = 0.25 + i * 0.02
            q = score_signal(
                target_atr_mult=atr,
                alltime_wins=(0 if i < 10 else 10),
                alltime_losses=(10 if i < 10 else 0),
                backtest_wr=(0.55 if i < 10 else 0.90),
                entry_premium=1.5,
                est_return_pct=50,
            )
            signals.append({
                "id": i + 1,
                "ticker": f"T{i:02d}",
                "direction": "CALL",
                "target_atr_mult": atr,
                "entry_premium": 1.5,
                "est_return_pct": 50,
                **q,
            })
        ranked = rank_signals(signals)
        featured = sorted(
            (s for s in ranked if s["is_featured"]),
            key=lambda s: s["featured_rank"],
        )
        self.assertEqual(len(featured), 10)
        self.assertEqual([s["ticker"] for s in featured], [f"T{i:02d}" for i in range(10)])
        self.assertTrue(all(s["quality_tier"] == "A+" for s in featured))
        self.assertEqual([s["featured_rank"] for s in featured], list(range(1, 11)))

    def test_featured_order_ignores_quality_score_and_reliability(self):
        easy = {
            "id": 1, "ticker": "EASY", "direction": "CALL",
            "target_atr_mult": 0.25, "entry_premium": 2.0,
            "est_return_pct": 20, "quality_score": 10.0,
        }
        hard = {
            "id": 2, "ticker": "HARD", "direction": "CALL",
            "target_atr_mult": 0.50, "entry_premium": 2.0,
            "est_return_pct": 100, "quality_score": 99.0,
        }
        ranked = rank_signals([hard, easy], featured_count=1)
        by_ticker = {s["ticker"]: s for s in ranked}
        self.assertTrue(by_ticker["EASY"]["is_featured"])
        self.assertEqual(by_ticker["EASY"]["featured_rank"], 1)
        self.assertFalse(by_ticker["HARD"]["is_featured"])
        self.assertLess(by_ticker["HARD"]["quality_rank"], by_ticker["EASY"]["quality_rank"])

    def test_low_positive_return_no_longer_demotes_easy_atr(self):
        easy_q = score_signal(
            target_atr_mult=0.28, alltime_wins=2, alltime_losses=3,
            backtest_wr=0.70, entry_premium=2.0, est_return_pct=10,
        )
        hard_q = score_signal(
            target_atr_mult=0.45, alltime_wins=10, alltime_losses=0,
            backtest_wr=0.90, entry_premium=2.0, est_return_pct=80,
        )
        rows = [
            {"id": 1, "ticker": "LOWATR", "direction": "CALL", "target_atr_mult": 0.28,
             "entry_premium": 2.0, "est_return_pct": 10, **easy_q},
            {"id": 2, "ticker": "HARDER", "direction": "CALL", "target_atr_mult": 0.45,
             "entry_premium": 2.0, "est_return_pct": 80, **hard_q},
        ]
        ranked = rank_signals(rows, featured_count=1)
        featured = next(s for s in ranked if s["is_featured"])
        self.assertEqual(featured["ticker"], "LOWATR")
        self.assertEqual(easy_q["quality_option_adjustment"], 0.0)

    def test_missing_atr_or_negative_option_is_watch(self):
        rows = []
        for idx, (atr, ret) in enumerate(((None, 100), (0.25, -5)), start=1):
            q = score_signal(
                target_atr_mult=atr, alltime_wins=10, alltime_losses=0,
                backtest_wr=0.90, entry_premium=2.0, est_return_pct=ret,
            )
            rows.append({
                "id": idx, "ticker": f"X{idx}", "direction": "CALL",
                "target_atr_mult": atr, "entry_premium": 2.0,
                "est_return_pct": ret, **q,
            })
        ranked = rank_signals(rows)
        self.assertTrue(all(s["quality_tier"] == "WATCH" for s in ranked))
        self.assertTrue(all(not s["is_featured"] for s in ranked))
        self.assertTrue(all(s["featured_rank"] is None for s in ranked))

    def test_raw_atr_ties_are_deterministic_not_reliability_based(self):
        rows = [
            {"id": 1, "ticker": "BBB", "direction": "CALL", "target_atr_mult": 0.30,
             "entry_premium": 2.0, "est_return_pct": 50, "quality_score": 99.0},
            {"id": 2, "ticker": "AAA", "direction": "CALL", "target_atr_mult": 0.30,
             "entry_premium": 2.0, "est_return_pct": 50, "quality_score": 10.0},
        ]
        ranked = rank_signals(rows, featured_count=2)
        featured = sorted(
            (s for s in ranked if s["is_featured"]),
            key=lambda s: s["featured_rank"],
        )
        self.assertEqual([s["ticker"] for s in featured], ["AAA", "BBB"])

    def test_version_is_stable(self):
        self.assertEqual(QUALITY_VERSION, "atr_featured_v1_1")


if __name__ == "__main__":
    unittest.main()

