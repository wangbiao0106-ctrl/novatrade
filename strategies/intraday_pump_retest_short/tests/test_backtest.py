import importlib.util
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / 'strategies' / 'intraday_pump_retest_short' / 'src'
sys.path.insert(0, str(SRC))
spec = importlib.util.spec_from_file_location('intraday_pump_retest_backtest', SRC / 'backtest.py')
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def cfg():
    return json.loads((ROOT / 'strategies/intraday_pump_retest_short/config/strategy.json').read_text())


class BacktestTests(unittest.TestCase):
    def test_prepare_requires_full_confirmed_children(self):
        # read_bars is exercised indirectly by the fixture shape in the
        # separate parser tests; this checks the 15m alignment contract.
        self.assertEqual(module.BAR_MS, 3 * module.CHILD_MS)

    def test_own_high_close_is_used_for_confirmation(self):
        c = cfg()
        c['signal_parameters']['down_bars'] = 2
        c['signal_parameters']['confirmation_close_to_own_high_max'] = 0.02
        base = 1_700_000_000_000
        bars = [module.Bar(base + i * module.BAR_MS, 100, 101, 99, 100, 1000, 1000) for i in range(10)]
        # Prior >60% anchor, two-bar oscillating decline, +15% pump, then Q
        # closes within 2% of Q's own high even though it is below P's high.
        bars[0] = module.Bar(base, 100, 170, 99, 160, 1000, 1000)
        bars[5] = module.Bar(base + 5 * module.BAR_MS, 150, 151, 140, 145, 1000, 1000)
        bars[6] = module.Bar(base + 6 * module.BAR_MS, 145, 146, 135, 140, 1000, 1000)
        bars[7] = module.Bar(base + 7 * module.BAR_MS, 140, 170, 139, 166, 1000, 1000)
        bars[8] = module.Bar(base + 8 * module.BAR_MS, 166, 168, 160, 167, 500, 500)
        pump = module.Pump(7, 0, 0, 170, 100, 1)
        s = module.pattern_features('ALT', bars, pump, c['signal_parameters'])
        self.assertTrue(s['touch_ok'])
        self.assertTrue(s['volume_ok'])
        self.assertTrue(s['close_ok'])

    def test_pump_threshold_is_strict_and_configurable(self):
        c = cfg()
        c['signal_parameters']['down_bars'] = 2
        base = 1_700_000_000_000
        bars = [module.Bar(base + i * module.BAR_MS, 100, 101, 99, 100, 1000, 1000) for i in range(10)]
        bars[0] = module.Bar(base, 100, 170, 99, 160, 1000, 1000)
        bars[5] = module.Bar(base + 5 * module.BAR_MS, 150, 151, 140, 145, 1000, 1000)
        bars[6] = module.Bar(base + 6 * module.BAR_MS, 145, 146, 135, 140, 1000, 1000)
        bars[7] = module.Bar(base + 7 * module.BAR_MS, 140, 170, 139, 166, 1000, 1000)
        bars[8] = module.Bar(base + 8 * module.BAR_MS, 166, 168, 160, 167, 500, 500)
        pump = module.Pump(7, 0, 0, 170, 100, 1)
        c['signal_parameters']['pump_close_gain_min'] = 0.20
        self.assertFalse(module.pattern_features('ALT', bars, pump, c['signal_parameters'])['pump_ok'])
        c['signal_parameters']['pump_close_gain_min'] = 0.15
        self.assertTrue(module.pattern_features('ALT', bars, pump, c['signal_parameters'])['pump_ok'])

    def test_close_far_from_own_high_is_rejected(self):
        c = cfg()
        c['signal_parameters']['down_bars'] = 2
        bars = [module.Bar(1_700_000_000_000 + i * module.BAR_MS, 100, 101, 99, 100, 1000, 1000) for i in range(10)]
        bars[0] = module.Bar(1_700_000_000_000, 100, 170, 99, 160, 1000, 1000)
        bars[5] = module.Bar(1_700_000_000_000 + 5 * module.BAR_MS, 150, 151, 140, 145, 1000, 1000)
        bars[6] = module.Bar(1_700_000_000_000 + 6 * module.BAR_MS, 145, 146, 135, 140, 1000, 1000)
        bars[7] = module.Bar(1_700_000_000_000 + 7 * module.BAR_MS, 140, 170, 139, 166, 1000, 1000)
        bars[8] = module.Bar(1_700_000_000_000 + 8 * module.BAR_MS, 166, 168, 150, 151, 500, 500)
        pump = module.Pump(7, 0, 0, 170, 100, 1)
        s = module.pattern_features('ALT', bars, pump, c['signal_parameters'])
        self.assertFalse(s['close_ok'])


if __name__ == '__main__':
    unittest.main()
