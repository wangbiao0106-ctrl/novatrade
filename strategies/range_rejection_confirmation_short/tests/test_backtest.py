import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "range_rejection_confirmation_short" / "src" / "backtest.py"
spec = importlib.util.spec_from_file_location("rrc_backtest", SRC)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class RangeRejectionTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "signal": {
                "ema_fast_period": 20, "ema_slow_period": 60, "atr_period": 14,
                "rsi_period": 14, "volume_period": 20, "prior_high_lookback": 20,
                "breakout_min_atr": 0.1, "body_ratio_min": 0.4,
                "upper_wick_ratio_min": 0.2, "close_position_max": 0.4,
                "range_atr_min": 1.5, "volume_multiple_min": 1.5,
                "rsi_min": 60, "rsi_must_fall": True,
                "ema20_distance_atr_max": 0.75, "confirmation_bars": 4,
                "confirmation_atr_break_min": 0.1,
            }
        }

    def test_screenshot_candle_is_not_a_long_wick_star(self):
        bar = module.Bar(0, 0.06880, 0.07149, 0.06169, 0.06473, 15_050_000)
        self.assertAlmostEqual(module.wick_ratio(bar), 0.2744898, places=5)
        self.assertAlmostEqual(module.close_position(bar), 0.3102041, places=5)
        self.assertLess(module.wick_ratio(bar), 0.4)

    def test_setup_requires_large_bearish_body(self):
        bar = module.Bar(0, 10.0, 11.0, 9.0, 9.2, 100.0)
        self.assertTrue(module.body_ratio(bar) >= 0.4)
        doji = module.Bar(0, 10.0, 11.0, 9.0, 10.0, 100.0)
        self.assertFalse(module.body_ratio(doji) >= 0.4)

    def test_confirmation_is_after_setup_and_below_ema(self):
        setup = module.Bar(0, 10.0, 11.0, 9.0, 9.2, 100.0)
        confirmation = module.Bar(900_000, 9.2, 9.3, 8.7, 8.8, 100.0)
        self.assertTrue(module.confirmation_matches(setup, confirmation, 9.0, 9.1, 0.2, self.config))

    def test_tp1_time_exit_uses_last_bar_close(self):
        config = {
            "risk": {"stop_atr": 0.35, "min_risk_atr": 0.75, "max_risk_atr": 3.0,
                     "tp1_r": 1.0, "tp1_fraction": 0.5, "tp2_r": 2.0, "max_hold_bars": 2},
            "costs": {"fee_rate_one_way": 0.0, "slippage_one_way": 0.0},
        }
        bars = [
            module.Bar(0, 10.0, 11.0, 9.0, 9.2, 100.0),
            module.Bar(900_000, 9.2, 9.3, 8.8, 9.0, 100.0),
            module.Bar(1_800_000, 9.0, 9.1, 6.0, 8.5, 100.0),
            module.Bar(2_700_000, 8.5, 8.8, 8.0, 8.2, 100.0),
        ]
        setup = module.Setup("ALT", 0, 0, 1.0, 10.0, 9.0, 70.0, 2.0, 0.2)
        trade = module.simulate(setup, 1, bars, config)
        self.assertEqual(trade.exit_ts, bars[3].ts)
        self.assertEqual(trade.exit_reason, "TP1后时间离场")
        self.assertEqual(trade.exit, bars[3].close)


if __name__ == "__main__":
    unittest.main()
