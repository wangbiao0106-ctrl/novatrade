import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "extreme_wick_short" / "src" / "trend_capture.py"
sys.path.insert(0, str(SRC.parent))
spec = importlib.util.spec_from_file_location("trend_capture", SRC)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class TrendCaptureTests(unittest.TestCase):
    def test_quality_gate_checks_both_train_and_validation(self):
        def summary(pf, wr=0.6, trades=10, total_r=1.0):
            return {"portfolio": {"profit_factor": pf, "win_rate": wr,
                                   "trades": trades, "total_r": total_r}}

        selection = {
            "minimum_train_trades": 10,
            "minimum_train_win_rate": 0.5,
            "minimum_train_profit_factor": 1.3,
            "minimum_validation_trades": 6,
            "minimum_validation_win_rate": 0.5,
            "minimum_validation_profit_factor": 1.5,
        }
        self.assertTrue(module.meets_quality(summary(1.4), summary(1.6), selection))
        self.assertFalse(module.meets_quality(summary(1.4), summary(1.4), selection))
        self.assertFalse(module.meets_quality(summary(1.2), summary(1.8), selection))

    def test_boundary_key_only_limits_training_cache(self):
        split = 123456
        self.assertEqual(module.boundary_key("train", split), split)
        self.assertIsNone(module.boundary_key("validation", split))
        self.assertIsNone(module.boundary_key("full", split))


if __name__ == "__main__":
    unittest.main()
