import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "strategies/sixty_day_support_retest_failed_breakout_short/research/backtest.py"
spec = importlib.util.spec_from_file_location("sixty_day_strategy", SOURCE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

START = int(datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)


def config():
    return json.loads((ROOT / "strategies/sixty_day_support_retest_failed_breakout_short/config/strategy.json").read_text())


def bars(rows):
    return [module.Bar(START + index * module.FIFTEEN_MINUTES, *row) for index, row in enumerate(rows)]


class FailedBreakoutTests(unittest.TestCase):
    def test_targets_close_50_20_and_runner(self):
        series = bars([
            (100, 101, 99, 100, 1),
            (100, 101, 94, 98, 1),
            (98, 99, 89, 92, 1),
            (92, 93, 84, 86, 1),
        ])
        signal = module.Signal("ALT", 0, series[0].ts, series[0].ts + module.FIFTEEN_MINUTES,
                               100, 20, 100, 95, 101)
        trade, reason = module.simulate(signal, series, config())
        self.assertIsNone(reason)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.exit_reason, "runner_target")
        self.assertEqual(trade.tp1_exit, trade.tp1)
        self.assertEqual(trade.tp2_exit, trade.tp2)
        self.assertGreater(trade.net_return, 0.08)

    def test_initial_stop_precedes_targets(self):
        series = bars([
            (100, 101, 99, 100, 1),
            (100, 106, 99, 103, 1),
        ])
        signal = module.Signal("ALT", 0, series[0].ts, series[0].ts + module.FIFTEEN_MINUTES,
                               100, 20, 100, 95, 101)
        trade, reason = module.simulate(signal, series, config())
        self.assertIsNone(reason)
        self.assertEqual(trade.exit_reason, "stop")
        self.assertLess(trade.net_return, 0.0)


if __name__ == "__main__":
    unittest.main()
