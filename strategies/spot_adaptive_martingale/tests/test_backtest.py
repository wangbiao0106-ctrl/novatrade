import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "strategies/spot_adaptive_martingale/src/backtest.py"
spec = importlib.util.spec_from_file_location("spot_adaptive_martingale", SOURCE)
assert spec and spec.loader
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def config():
    return {
        "portfolio": {"initial_spot_fraction": 0.2, "min_inventory_fraction_of_initial": 0.25},
        "indicators": {"ema_fast": 3, "ema_slow": 5, "atr_period": 3, "trend_slope_bars": 2},
        "grid": {"min_step_pct": 0.01, "max_step_pct": 0.05, "atr_multiplier": 0.0,
                 "base_order_quote": 100, "multiplier": 2.0, "max_levels": 2,
                 "downtrend_order_fraction": 1.0, "take_profit_pct": 0.01,
                 "sell_fraction": 0.25, "cooldown_bars": 0},
        "risk": {"reserve_floor_quote": 100, "max_inventory_fraction": 0.8,
                 "emergency_drawdown_pct": 0.35},
        "costs": {"fee_rate": 0.0, "slippage_rate": 0.0},
    }


class BacktestTests(unittest.TestCase):
    def test_resample_rejects_missing_five_minute_bar(self):
        rows = [module.Bar(i * 300_000, 100, 101, 99, 100, 1) for i in range(12)]
        self.assertEqual(len(module.resample_1h(rows)), 1)
        rows.pop(5)
        self.assertEqual(module.resample_1h(rows), [])

    def test_bounded_martingale_and_metrics(self):
        bars = []
        prices = [100] * 130 + [99, 98, 97, 98, 99, 101, 100, 98, 96, 95, 96, 99, 101, 100, 99, 98, 100, 102, 101, 100]
        for i, price in enumerate(prices):
            bars.append(module.Bar(i * module.HOUR_MS, price, price * 1.002, price * 0.998, price, 1))
        report, trades, curve = module.simulate("TEST", bars, config(), capital=1000)
        self.assertLessEqual(report["max_level_seen"], 2)
        self.assertGreaterEqual(report["final_cash"], 100)
        self.assertEqual(len(curve), len(bars) - 120)
        self.assertTrue(all(trade.fee >= 0 for trade in trades))


if __name__ == "__main__":
    unittest.main()
