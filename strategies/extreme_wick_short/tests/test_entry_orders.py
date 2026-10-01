import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "extreme_wick_short" / "src" / "entry_orders.py"
sys.path.insert(0, str(SRC.parent))
spec = importlib.util.spec_from_file_location("entry_orders", SRC)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class EntryOrderTests(unittest.TestCase):
    def setUp(self):
        self.bars = [
            module.engine.base.Bar(i * module.MS, 100 + i, 110 + i, 90 + i, 105 + i, 1000)
            for i in range(6)
        ]
        self.event = module.engine.base.Event(
            "ALT", 2, 2 * module.MS, 1.1, 0.0, 60.0, -1.0,
            0.4, 0.5, 0.5, 0.0, 0.1, 112, 5,
        )

    def test_prior_body_and_close_prices_exclude_signal_candle(self):
        self.assertEqual(module.prior_limit_price(self.bars, 2, "body_high", 2), 106)
        self.assertEqual(module.prior_limit_price(self.bars, 2, "close_high", 2), 106)
        self.assertEqual(module.prior_limit_price(self.bars, 2, "open_high", 2), 101)

    def test_limit_fills_when_future_candle_touches_reference(self):
        index, reason, price = module.find_fill(
            self.event, self.bars,
            {"mode": "limit", "kind": "body_high", "lookback": 2, "wait_bars": 2},
        )
        self.assertEqual(index, 3)
        self.assertIsNone(reason)
        self.assertEqual(price, 106)

    def test_signal_close_and_next_open_are_distinct_fill_prices(self):
        close = module.find_fill(self.event, self.bars, {"mode": "market_signal_close"})
        opening = module.find_fill(self.event, self.bars, {"mode": "market_next_open"})
        self.assertEqual(close[0], opening[0])
        self.assertEqual(close[2], self.bars[self.event.index].close)
        self.assertEqual(opening[2], self.bars[self.event.index + 1].open)


if __name__ == "__main__":
    unittest.main()
