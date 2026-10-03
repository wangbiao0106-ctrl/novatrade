import importlib.util
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "strategies" / "extreme_negative_funding_110_short" / "src" / "backtest.py"
spec = importlib.util.spec_from_file_location("extreme_negative_funding", SOURCE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


DAY_START = int(datetime(2026, 1, 2, tzinfo=timezone.utc).timestamp() * 1000)


def make_bars(rows):
    return [module.Bar(DAY_START + index * module.FIVE_MINUTES, *row) for index, row in enumerate(rows)]


def config():
    return json.loads((ROOT / "strategies/extreme_negative_funding_110_short/config/strategy.json").read_text())


def funding(ts, rate=-0.02, cap=-0.02):
    return {"ALT": [module.FundingSnapshot("ALT", ts, rate, cap)]}


class ExtremeNegativeFundingTests(unittest.TestCase):
    def test_universe_uses_canonical_altcoin_pool(self):
        allowed = module.allowed_symbols(config())
        self.assertIsNotNone(allowed)
        self.assertIn("RAVE", allowed)
        self.assertNotIn("BTC", allowed)
        self.assertNotIn("NFLX", allowed)

    def test_missing_funding_never_passes_price_trigger(self):
        bars = make_bars([
            (100, 100, 99, 100, 1),
            (100, 181, 99, 170, 1),
            (170, 210, 160, 200, 1),
            (100, 112, 90, 105, 1),
        ])
        signals, counts, _ = module.day_signals("ALT", bars, config(), {})
        self.assertEqual(signals, [])
        self.assertEqual(counts["price_trigger_candidates"], 1)
        self.assertEqual(counts["funding_missing"], 1)

    def test_ignore_funding_bypasses_only_the_funding_gate(self):
        bars = make_bars([
            (100, 100, 99, 100, 1),
            (100, 181, 99, 170, 1),
            (170, 210, 160, 200, 1),
        ])
        signals, counts, _ = module.day_signals("ALT", bars, config(), {}, ignore_funding=True)
        self.assertEqual(len(signals), 1)
        self.assertIsNone(signals[0].funding_rate)
        self.assertEqual(counts["funding_bypassed_signals"], 1)

    def test_funding_must_be_at_symbol_negative_cap(self):
        bars = make_bars([(100, 100, 99, 100, 1), (100, 181, 99, 170, 1), (170, 210, 160, 200, 1)])
        c = config()
        asof = bars[2].ts + module.FIVE_MINUTES
        rejected, counts, _ = module.day_signals("ALT", bars, c, funding(asof, -0.01, -0.02))
        self.assertFalse(rejected)
        self.assertEqual(counts["funding_not_at_symbol_max_negative"], 1)
        accepted, counts, _ = module.day_signals("ALT", bars, c, funding(asof, -0.02, -0.02))
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0].day_open, 100)

        below_trigger = make_bars([
            (100, 100, 99, 100, 1), (100, 181, 99, 170, 1), (170, 209.99, 160, 200, 1)
        ])
        _, counts, _ = module.day_signals("ALT", below_trigger, c, funding(below_trigger[2].ts + module.FIVE_MINUTES))
        self.assertEqual(counts["price_trigger_candidates"], 0)

    def test_limit_order_starts_next_bar_and_intrabar_fill_waits_for_next_exit_bar(self):
        c = config()
        bars = make_bars([
            (100, 100, 99, 100, 1),
            (100, 181, 99, 170, 1),
            (170, 210, 160, 200, 1),
            (200, 212, 190, 205, 1),  # touches 210; no exits on this unknown fill bar
            (200, 208, 167, 180, 1),  # T1 = 80% of effective entry
            (180, 190, 120, 130, 1),  # T2 = 60% of effective entry
        ])
        asof = bars[2].ts + module.FIVE_MINUTES
        signals, _, _ = module.day_signals("ALT", bars, c, funding(asof))
        trade, reason = module.simulate(signals[0], bars, c)
        self.assertIsNone(reason)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.entry_index, 3)
        self.assertEqual(trade.exit_index, 5)
        self.assertEqual(trade.exit_reason, "target_2")
        self.assertAlmostEqual(trade.tp2, trade.entry * 0.6, places=9)
        self.assertEqual(trade.tp1_exit, trade.tp1)
        self.assertGreater(trade.net_return, 0.0)
        self.assertLess(trade.net_return, 0.3)

    def test_stop_wins_same_bar_and_breakeven_applies_next_bar(self):
        c = config()
        bars = make_bars([
            (100, 100, 99, 100, 1),
            (100, 181, 99, 170, 1),
            (170, 210, 160, 200, 1),
            (210, 260, 140, 200, 1),  # fill at open, then initial stop wins
        ])
        asof = bars[2].ts + module.FIVE_MINUTES
        signals, _, _ = module.day_signals("ALT", bars, c, funding(asof))
        trade, reason = module.simulate(signals[0], bars, c)
        self.assertIsNone(reason)
        self.assertEqual(trade.exit_reason, "stop")
        self.assertLess(trade.net_return, 0.0)

        bars = make_bars([
            (100, 100, 99, 100, 1), (100, 181, 99, 170, 1), (170, 210, 160, 200, 1),
            (200, 212, 190, 205, 1), (200, 208, 167, 180, 1),
            (180, 220, 175, 200, 1),  # next bar crosses the moved breakeven stop
        ])
        signals, _, _ = module.day_signals("ALT", bars, c, funding(asof))
        trade, reason = module.simulate(signals[0], bars, c)
        self.assertIsNone(reason)
        self.assertEqual(trade.exit_reason, "breakeven_stop")
        self.assertEqual(trade.exit_index, 5)

    def test_research_overrides_wait_and_initial_stop(self):
        c = config()
        bars = make_bars([
            (100, 100, 99, 100, 1),
            (100, 181, 99, 170, 1),
            (170, 210, 160, 200, 1),  # signal bar
            (200, 205, 190, 200, 1),  # wait bar; order is inactive
            (200, 212, 190, 205, 1),  # activation/fill bar
            (200, 205, 167, 180, 1),
        ])
        asof = bars[2].ts + module.FIVE_MINUTES
        signals, _, _ = module.day_signals("ALT", bars, c, funding(asof))
        trade, reason = module.simulate(
            signals[0], bars, c, entry_wait_bars=2, initial_stop_pct=0.15
        )
        self.assertIsNone(reason)
        self.assertIsNotNone(trade)
        self.assertEqual(trade.entry_index, 4)
        self.assertAlmostEqual(trade.initial_stop, trade.entry * 1.15, places=9)

    def test_non_midnight_series_cannot_invent_day_open(self):
        bars = [module.Bar(DAY_START + module.FIVE_MINUTES, 100, 210, 99, 200, 1)]
        signals, counts, _ = module.day_signals("ALT", bars, config(), funding(bars[0].ts + module.FIVE_MINUTES))
        self.assertFalse(signals)
        self.assertEqual(counts["price_trigger_candidates"], 0)


if __name__ == "__main__":
    unittest.main()
