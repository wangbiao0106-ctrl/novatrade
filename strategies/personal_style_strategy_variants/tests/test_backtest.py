import importlib.util
import sys
import unittest
from pathlib import Path

import numpy as np


SOURCE = Path(__file__).resolve().parents[1] / "src" / "backtest.py"
spec = importlib.util.spec_from_file_location("personal_style_variants_backtest", SOURCE)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class BacktestHelpersTest(unittest.TestCase):
    def test_rolling_mean_is_left_complete(self):
        values = np.array([1.0, 2.0, 3.0, 4.0])
        result = module.rolling_mean(values, 2)
        self.assertTrue(np.isnan(result[0]))
        np.testing.assert_allclose(result[1:], [1.5, 2.5, 3.5])

    def test_trade_cost_reduces_short_return(self):
        event = module.Event("E_extreme_reversion_short", "X", 0, 300000, 1, "short", 110.0, 0.8, 2, 1.0, 0.1, 20_000_000)
        arrays = (
            np.array([0, 300000, 600000]),
            np.array([100.0, 100.0, 100.0]),
            np.array([100.0, 100.0, 100.0]),
            np.array([100.0, 90.0, 90.0]),
            np.array([100.0, 90.0, 90.0]),
            np.array([1.0, 1.0, 1.0]),
        )
        trade = module.execute(event, arrays, 0.0005, 0.0005, "base_5bp")
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertLess(trade.net_r, trade.gross_r)


if __name__ == "__main__":
    unittest.main()
