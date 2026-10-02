import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "extreme_wick_short" / "src" / "momentum_exhaustion.py"
sys.path.insert(0, str(SRC.parent))
spec = importlib.util.spec_from_file_location("momentum_exhaustion", SRC)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class MomentumExhaustionTests(unittest.TestCase):
    def test_rsi_warmup_is_causal(self):
        values = module.wilder_rsi([100 + i for i in range(20)], 14)
        self.assertEqual(values[:14], [None] * 14)
        self.assertEqual(values[14], 100.0)

    def test_prior_low_excludes_current_candle(self):
        values = module.prior_rolling_low([10.0, 8.0, 12.0, 3.0], 2)
        self.assertEqual(values[2], 8.0)
        self.assertEqual(values[3], 8.0)

    def test_qualify_requires_bearish_close_and_falling_rsi(self):
        bar = module.Bar(0, 100, 120, 90, 105, 1000)
        event = module.Event("ALT", 0, 0, 1.1, 2.0, 70, -1, 0.5, 0.5, 1.2, 0.02, 0.1, 120, 5)
        config = {"signal_parameters": {
            "upper_wick_min": 0.4, "close_position_max": 0.6, "rsi_min": 65,
            "rsi_must_fall": True, "volume_multiple": 1.0, "breakout_min_pct": 0.01,
        }}
        self.assertFalse(module.qualifies(event, [bar], config))
        bearish = module.Bar(0, 110, 120, 90, 100, 1000)
        self.assertTrue(module.qualifies(event, [bearish], config))


if __name__ == "__main__":
    unittest.main()
