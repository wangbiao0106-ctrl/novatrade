import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "extreme_wick_short" / "src" / "sample_expansion.py"
sys.path.insert(0, str(SRC.parent))
spec = importlib.util.spec_from_file_location("sample_expansion", SRC)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class SampleExpansionTests(unittest.TestCase):
    def test_strict_gain_threshold_and_short_history(self):
        config = {"signal_parameters": {"rsi_period": 14, "atr_period": 14,
                  "volume_period": 20, "breakout_lookback_bars": 20}}
        bars = [module.base.Bar(i * module.MS, 100, 101, 99, 100, 1000)
                for i in range(98)]
        bars[96] = module.base.Bar(96 * module.MS, 190, 200, 180, 185, 1000)
        self.assertEqual(module.features("ALT", bars, config), [])
        bars[96] = module.base.Bar(96 * module.MS, 190, 201, 180, 185, 1000)
        events = module.features("ALT", bars, config)
        self.assertEqual([e.index for e in events], [96])
        self.assertGreater(events[0].intraday_gain, 1.0)

    def test_gap_requires_new_contiguous_24h_history(self):
        config = {"signal_parameters": {"rsi_period": 14, "atr_period": 14,
                  "volume_period": 20, "breakout_lookback_bars": 20}}
        bars = [module.base.Bar((i + (1 if i >= 50 else 0)) * module.MS,
                               100, 201 if i == 96 else 101, 99, 100, 1000)
                for i in range(98)]
        self.assertEqual(module.features("ALT", bars, config), [])

    def test_disabled_breakout_filter_is_really_optional(self):
        bars = [module.base.Bar(0, 110, 120, 90, 100, 1000), module.base.Bar(module.MS, 100, 101, 99, 100, 1000)]
        event = module.base.Event("ALT", 0, 0, 1.1, 0.0, 60.0, -1.0, 0.4, 0.5, 0.5, -0.02, 0.1, 120, 5)
        params = {
            "upper_wick_min": 0.4,
            "close_position_max": 0.6,
            "rsi_min": 55.0,
            "rsi_must_fall": True,
            "volume_multiple": 0.5,
            "breakout_min_pct": None,
        }
        self.assertTrue(module.matches(event, bars, params))

    def test_breakout_filter_rejects_event_when_enabled(self):
        bars = [module.base.Bar(0, 110, 120, 90, 100, 1000)]
        event = module.base.Event("ALT", 0, 0, 1.1, 0.0, 60.0, -1.0, 0.4, 0.5, 0.5, 0.0, 0.1, 120, 5)
        params = {
            "upper_wick_min": 0.4,
            "close_position_max": 0.6,
            "rsi_min": 55.0,
            "rsi_must_fall": True,
            "volume_multiple": 0.5,
            "breakout_min_pct": 0.01,
        }
        self.assertFalse(module.matches(event, bars, params))

    def test_per_symbol_filter_applies_exit_cooldown(self):
        def outcome(index, entry, exit_index):
            return module.Outcome("ALT", index, index * module.MS, entry * module.MS,
                                  (exit_index + 1) * module.MS, exit_index, 100, 99, 101,
                                  98, 0.5, 0.01, 2.0, -0.01, "target")

        trades = [outcome(0, 1, 2), outcome(5, 6, 7), outcome(20, 21, 22)]
        filtered = module.per_symbol_filter(trades, cooldown=16)
        self.assertEqual([t.signal_index for t in filtered], [0, 20])


if __name__ == "__main__":
    unittest.main()
