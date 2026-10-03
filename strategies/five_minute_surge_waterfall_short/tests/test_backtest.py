import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "strategies" / "five_minute_surge_waterfall_short" / "src" / "backtest.py"
spec = importlib.util.spec_from_file_location("surge_backtest", SOURCE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def bars_from(rows):
    return [module.Bar(index * module.FIVE_MINUTES, *row) for index, row in enumerate(rows)]


class SurgeBacktestTests(unittest.TestCase):
    def setUp(self):
        self.config = {
            "signal": {
                "thresholds_exclusive": [0.2, 0.3],
                "prior_four_hour_high_bars": 48,
                "limit_reference_bars": 72,
            },
            "entry": {"limit_wait_bars": 72},
            "risk": {"atr_period": 14, "stop_atr": 0.5, "target_r": 1.0,
                     "max_hold_bars": 72, "max_stop_distance_pct": 0.15,
                     "cooldown_bars": 72},
            "costs": {"fee_rate_one_way": 0.0006, "slippage_one_way": 0.0002,
                      "funding_rate_round_trip": 0.0},
            "position_management": {"leverage": 2.0, "risk_per_trade_pct": 1.0,
                                     "account_daily_loss_limit_pct": 5.0},
        }

    def test_body_top_uses_close_for_bullish_and_open_for_bearish(self):
        self.assertEqual(module.body_top(module.Bar(0, 100, 130, 90, 120, 1)), 120)
        self.assertEqual(module.body_top(module.Bar(0, 120, 130, 90, 100, 1)), 120)

    def test_strict_threshold_and_breakout_are_causal(self):
        rows = [(100, 100, 100, 100, 1)] * 80
        rows[70] = (100, 150, 99, 100, 1)
        rows[72] = (100, 120, 99, 100, 1)   # exactly 20%, no event
        rows[73] = (100, 120.1, 99, 110, 1)  # >20%, but prior 4h high is 150
        bars = bars_from(rows)
        signals = module.build_signals("ALT", bars, self.config)
        self.assertTrue(all(signal.index != 72 for signal in signals))
        self.assertTrue(any(signal.index == 73 and not signal.breakout_4h for signal in signals))

    def test_limit_order_uses_body_top_and_starts_after_signal(self):
        rows = [(100, 101, 99, 100, 1)] * 76
        rows[72] = (100, 120, 99, 115, 1)
        rows[73] = (100, 111, 99, 100, 1)
        bars = bars_from(rows)
        signal = module.Signal("ALT", 72, bars[72].ts, 0.2, True, 0.2, 0.15,
                               105, 110, 1.0, 120)
        trade, reason = module.simulate_order(signal, bars, "limit_recent_72_body_top", self.config)
        self.assertIsNone(reason)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.entry_index, 73)
        self.assertEqual(trade.order_price, 110)
        self.assertEqual(trade.fill_wait_bars, 0)

    def test_limit_unfilled_and_gap_are_not_trades(self):
        rows = [(100, 101, 99, 100, 1)] * 76
        bars = bars_from(rows)
        signal = module.Signal("ALT", 72, bars[72].ts, 0.2, False, 0.21, 0.1,
                               105, 110, 1.0, 108)
        trade, reason = module.simulate_order(signal, bars, "limit_recent_72_body_top", self.config)
        self.assertIsNone(trade)
        self.assertEqual(reason, "limit_unfilled")
        bars[73] = module.Bar(bars[73].ts + module.FIVE_MINUTES, 100, 101, 99, 100, 1)
        trade, reason = module.simulate_order(signal, bars, "next_5m_open", self.config)
        self.assertIsNone(trade)
        self.assertEqual(reason, "entry_gap")

    def test_same_bar_stop_has_priority_over_target(self):
        rows = [(100, 101, 99, 100, 1)] * 76
        rows[73] = (100, 121, 90, 100, 1)
        bars = bars_from(rows)
        signal = module.Signal("ALT", 72, bars[72].ts, 0.2, True, 0.21, 0.1,
                               105, 110, 1.0, 105)
        trade, reason = module.simulate_order(signal, bars, "next_5m_open", self.config)
        self.assertIsNone(reason)
        self.assertEqual(trade.reason, "stop")


if __name__ == "__main__":
    unittest.main()
