import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "extreme_wick_short" / "src"
spec = importlib.util.spec_from_file_location("limit_order_analysis", SRC / "limit_order_analysis.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def config():
    return {
        "observation_filter": {"intraday_gain_gt": 0.60, "thirty_day_low_multiple_min": 3.0},
        "pending_modes": {
            "mode_one_15m_pump_pullback": {"pre_bars": 2, "pre_net_min": 0, "pre_net_max": 0.1,
                                             "pre_range_max": 0.15, "pre_bar_move_max": 0.03,
                                             "pre_positive_fraction": 0.5, "pump_gain_min": 0.2,
                                             "small_pullback_min": 0, "small_pullback_max": 0.05,
                                             "pullback_low_floor": 0.95, "limit_valid_bars": 2},
            "mode_two_15m_downtrend_to_day_high_close": {"pre_bars": 2, "pre_net_down_min": 0.005,
                                                           "pre_net_down_max": 0.08, "pre_bar_move_max": 0.03,
                                                           "pre_negative_fraction": 0.5,
                                                           "anchor_high_tolerance": 0.01,
                                                           "limit_valid_bars": 2},
        },
        "risk": {"atr_period": 2, "stop_atr": 0.3, "min_risk_atr": 0,
                 "max_risk_atr": 10, "target_r": 1.5, "max_hold_bars": 4,
                 "cooldown_bars": 2, "limit_valid_bars": 2},
        "costs": {"fee_rate_one_way": 0, "slippage_one_way": 0},
    }


class LimitOrderAnalysisTests(unittest.TestCase):
    def test_observation_uses_prior_30_day_low_and_strict_gain(self):
        cfg = config()
        bars = [module.Bar(i * module.FIFTEEN_MINUTES, 1, 1.1, 1, 1, 1) for i in range(2880)]
        bars.extend([
            module.Bar(2880 * module.FIFTEEN_MINUTES, 2, 3.1, 1, 2.5, 1),
            module.Bar(2881 * module.FIFTEEN_MINUTES, 2.5, 2.5, 2.4, 2.45, 1),
        ])
        found = module.observations("ALT", bars, cfg)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].thirty_day_low, 1)
        self.assertGreater(found[0].intraday_gain, 0.60)

    def test_mode_one_order_starts_after_pullback_candle(self):
        cfg = config()
        bars = [module.Bar(i * module.FIFTEEN_MINUTES, 100, 101, 99, 100, 1) for i in range(8)]
        bars[4] = module.Bar(4 * module.FIFTEEN_MINUTES, 100, 101, 99, 102, 1)
        bars[5] = module.Bar(5 * module.FIFTEEN_MINUTES, 102, 125, 101, 125, 1)
        bars[6] = module.Bar(6 * module.FIFTEEN_MINUTES, 125, 126, 120, 122, 1)
        bars[7] = module.Bar(7 * module.FIFTEEN_MINUTES, 122, 126, 121, 123, 1)
        obs = {5: module.Observation("ALT", 5, bars[5].ts, 0.8, 1, 3.0)}
        signals = module.mode_one_signals("ALT", bars, obs, cfg)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].signal_index, 6)
        self.assertEqual(module.fill_pending(signals[0], bars, cfg), (7, 125))
        trade = module.simulate(signals[0], bars, cfg, 7, 125)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.order_ts, bars[7].ts)

    def test_mode_two_uses_confirmed_prior_day_high_close(self):
        cfg = config()
        base = 1_735_689_600_000  # 2025-01-01 00:00 UTC, 15m aligned
        bars = [module.Bar(base + i * module.FIFTEEN_MINUTES, 100, 101, 99, 100, 1) for i in range(12)]
        bars[3] = module.Bar(base + 3 * module.FIFTEEN_MINUTES, 100, 130, 99, 120, 1)
        bars[4] = module.Bar(base + 4 * module.FIFTEEN_MINUTES, 120, 121, 115, 119, 1)
        bars[5] = module.Bar(base + 5 * module.FIFTEEN_MINUTES, 119, 120, 114, 118, 1)
        bars[6] = module.Bar(base + 6 * module.FIFTEEN_MINUTES, 118, 119, 113, 117, 1)
        bars[7] = module.Bar(base + 7 * module.FIFTEEN_MINUTES, 117, 118, 112, 116, 1)
        bars[8] = module.Bar(base + 8 * module.FIFTEEN_MINUTES, 116, 117, 111, 115, 1)
        bars[9] = module.Bar(base + 9 * module.FIFTEEN_MINUTES, 115, 116, 110, 114, 1)
        obs = {9: module.Observation("ALT", 9, bars[9].ts, 0.8, 1, 3.0)}
        signals = module.mode_two_signals("ALT", bars, obs, cfg)
        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].anchor_index, 3)
        self.assertEqual(signals[0].order_price, 120)

    def test_mode_two_observation_bar_is_the_last_down_bar(self):
        cfg = config()
        base = 1_735_689_600_000
        bars = [module.Bar(base + i * module.FIFTEEN_MINUTES, 100, 101, 99, 100, 1) for i in range(10)]
        bars[3] = module.Bar(base + 3 * module.FIFTEEN_MINUTES, 100, 130, 99, 120, 1)
        bars[7] = module.Bar(base + 7 * module.FIFTEEN_MINUTES, 117, 118, 112, 116, 1)
        bars[8] = module.Bar(base + 8 * module.FIFTEEN_MINUTES, 116, 117, 111, 115, 1)
        bars[9] = module.Bar(base + 9 * module.FIFTEEN_MINUTES, 115, 117, 114, 116, 1)
        obs = {9: module.Observation("ALT", 9, bars[9].ts, 0.8, 1, 3.0)}
        self.assertEqual(module.mode_two_signals("ALT", bars, obs, cfg), [])


if __name__ == "__main__":
    unittest.main()
