import importlib.util
import sys
import unittest


sys.path.insert(0, "scripts")
spec = importlib.util.spec_from_file_location("high_short_strategy", "scripts/high_short_strategy.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class HighShortStrategyTests(unittest.TestCase):
    def test_resample_builds_4h_ohlc(self):
        bars = [module.Bar(ts, 10 + i, 12 + i, 9 + i, 11 + i, 1, 100) for i, ts in enumerate((0, 900000, 1800000, 2700000))]
        result = module.resample(bars, 60)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].open, 10)
        self.assertEqual(result[0].close, 14)
        self.assertEqual(result[0].high, 15)
        self.assertEqual(result[0].low, 9)

    def test_grid_covers_rejection_and_confirmation_choices(self):
        values = module.parameter_grid()
        self.assertEqual(len(values), 384)
        self.assertEqual({item.minimum_rejection_score for item in values}, {1, 2})
        self.assertEqual({item.confirmation_window for item in values}, {4, 8})
        self.assertEqual({item.zone_required for item in values}, {"any", "primary_sweep", "extreme_sweep"})


if __name__ == "__main__":
    unittest.main()
