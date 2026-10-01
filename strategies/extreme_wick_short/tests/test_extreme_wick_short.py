import gzip
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "extreme_wick_short" / "src" / "extreme_wick_short.py"
spec = importlib.util.spec_from_file_location("extreme_wick_short", SRC)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


def row(ts, price=100.0, confirmed=True):
    return {"timestamp_ms": ts, "open": str(price), "high": str(price + 1),
            "low": str(price - 1), "close": str(price), "quote_volume": "1000",
            "confirmed": confirmed}


class ExtremeWickShortTests(unittest.TestCase):
    def test_aggregate_requires_three_confirmed_children(self):
        base = 1_700_000_000_000 // module.FIFTEEN_MINUTES * module.FIFTEEN_MINUTES
        bars = [module.Bar(base + i * module.FIVE_MINUTES, 100, 101, 99, 100, 1) for i in range(2)]
        self.assertEqual(module.aggregate_15m(bars), [])
        bars.append(module.Bar(base + 2 * module.FIVE_MINUTES, 100, 101, 99, 100, 1))
        self.assertEqual(len(module.aggregate_15m(bars)), 1)

    def test_path_selection_ignores_partial_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            complete = root / "ALT_USDT_SWAP_5m_20260331T000000Z_20260930T190216Z.jsonl.gz"
            partial = root / "ALT_USDT_SWAP_5m_20260331T000000Z_20260930T190216Z.jsonl.gz.part"
            with gzip.open(complete, "wt") as stream:
                stream.write(json.dumps(row(1_700_000_000_000)) + "\n")
            partial.write_text("truncated")
            picked = module.choose_paths(root, set())
            self.assertEqual(picked, [("ALT", complete)])

    def test_gain_upper_bound_is_exclusive(self):
        config = {
            "signal_parameters": {"atr_period": 2, "rsi_period": 2},
            "hard_filters": {"gain24h_min": 0.5, "gain24h_max_exclusive": 1.0, "quote_volume24h_min": 0},
            "training": {"grid": {"lookback_bars": [2]}},
        }
        # 96 prior bars at 100 and the current close at exactly 200 must not
        # become a +50%..100% candidate because the upper bound is exclusive.
        bars = [module.Bar(i * module.FIFTEEN_MINUTES, 100, 101, 99, 100, 1000) for i in range(96)]
        bars.append(module.Bar(96 * module.FIFTEEN_MINUTES, 150, 160, 140, 200, 2000))
        self.assertEqual(module.build_candidates(bars, config), [])

    def test_intraday_high_can_qualify_after_close_retraces(self):
        config = {
            "signal_parameters": {"atr_period": 2, "rsi_period": 2},
            "hard_filters": {"gain24h_min": 0.5, "gain24h_max_exclusive": 1.0, "quote_volume24h_min": 0},
            "training": {"grid": {"lookback_bars": [2]}},
        }
        bars = [module.Bar(i * module.FIFTEEN_MINUTES, 100, 101, 99, 100, 1000) for i in range(96)]
        # The close is only +40%, but the intraday high reached +60%.
        bars.append(module.Bar(96 * module.FIFTEEN_MINUTES, 150, 160, 130, 140, 2000))
        bars.append(module.Bar(97 * module.FIFTEEN_MINUTES, 140, 141, 139, 140, 1000))
        self.assertEqual(len(module.build_candidates(bars, config)), 1)

    def test_quote_volume_floor_is_causal_and_enforced(self):
        config = {
            "signal_parameters": {"atr_period": 2, "rsi_period": 2},
            "hard_filters": {"gain24h_min": 0.5, "gain24h_max_exclusive": 1.0, "quote_volume24h_min": 100_000},
            "training": {"grid": {"lookback_bars": [2]}},
        }
        bars = [module.Bar(i * module.FIFTEEN_MINUTES, 100, 101, 99, 100, 1000) for i in range(96)]
        bars.append(module.Bar(96 * module.FIFTEEN_MINUTES, 100, 160, 99, 160, 1000))
        self.assertEqual(module.build_candidates(bars, config), [])

    def test_metrics_orders_trades_before_drawdown(self):
        def trade(ts, value):
            return module.Trade("ALT", ts, ts, ts, 1, 1, 1, 1, value, "止盈", 1)
        result = module.metrics([trade(3, 2), trade(1, -1), trade(2, -1)])
        self.assertEqual(result["total_r"], 0)
        self.assertEqual(result["max_drawdown_r"], 2)

    def test_confirmation_is_after_signal_bar(self):
        config = {"signal_parameters": {"retest_atr": 0.5}}
        bars = [module.Bar(i * module.FIFTEEN_MINUTES, 100, 101, 99, 100, 1) for i in range(4)]
        candidate = module.Candidate(1, bars[1].ts, 0.6, 1, 80, 0.8, 1, 2, 100, ((2, 100),))
        bars[2] = module.Bar(bars[2].ts, 100, 100.5, 98, 98.5, 1)
        bars[3] = module.Bar(bars[3].ts, 97, 97.5, 96, 96.5, 1)
        params = module.Params(2, 0.7, 0.5, 0.5, 2, 0.3, 2.5)
        self.assertEqual(module.confirm_index(bars, candidate, params, config), 2)
        self.assertEqual(bars[3].open, 97)


if __name__ == "__main__":
    unittest.main()
