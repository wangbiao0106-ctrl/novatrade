import importlib.util
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "hlsr" / "src"
sys.path.insert(0, str(SRC))
spec = importlib.util.spec_from_file_location("high_short_strategy", SRC / "high_short_strategy.py")
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

    def test_failed_breakout_requires_depth_beyond_the_swept_level(self):
        """第 4 项不能与"扫顶"前置条件重复：仅收盘低于前高不算失败突破。

        修复前 `failed_breakout = closes[index] < previous_swing_high`，而扫顶已经
        要求过同一条件，该项恒为真，评分整体虚高一分。
        """
        params = module.Params(swing_lookback=6, wick_ratio=0.6, volume_multiple=1.0,
                               minimum_rejection_score=2, confirmation_window=4, stop_atr=0.25,
                               trail_bars=2, allow_range=True, zone_required="any")
        self.assertEqual(params.reject_depth_atr, 0.1)
        self.assertGreater(params.reject_depth_atr, 0)

        def reasons(close):
            # 价 100 的前高、ATR14 = 1；成交额与均量相同，因此放量项不成立。
            return module.rejection_reasons(params, open_price=100.4, high=100.6, low=99.6,
                                            close=close, previous_swing_high=100.0, atr14=1.0,
                                            volume=1000.0, mean_volume=1000.0)

        self.assertNotIn("failed_breakout", reasons(99.95))   # 浅跌破：-0.05 ATR
        self.assertIn("failed_breakout", reasons(99.80))      # 深跌破：-0.20 ATR
        self.assertNotIn("failed_breakout", reasons(100.05))  # 收盘仍在前高上方

    def test_symbol_from_path_builds_a_valid_instrument_id(self):
        import hlsr_market_export

        path = Path("BEAT_USDT_SWAP_5m_20260331T065300Z_20260928T125358Z.jsonl.gz")
        self.assertEqual(hlsr_market_export.symbol_from_path(path), "BEAT-USDT-SWAP")


class SourceIntegrityTests(unittest.TestCase):
    """源数据完整性：不完整桶必须丢弃，暖机不足不得给出信号。"""

    def _write_source(self, rows):
        import gzip
        import json
        import tempfile

        handle = tempfile.NamedTemporaryFile(suffix=".jsonl.gz", delete=False)
        handle.close()
        path = Path(handle.name)
        with gzip.open(path, "wt") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        self.addCleanup(path.unlink)
        return path

    def _row(self, ts, price, confirmed=True):
        return {"timestamp_ms": ts, "open": price, "high": price + 0.5, "low": price - 0.5,
                "close": price, "volume": 1, "quote_volume": 1, "confirmed": confirmed}

    def test_incomplete_source_bucket_is_dropped(self):
        import hlsr_signal_generator as generator

        base = 1_700_000_000_000 // 900_000 * 900_000
        rows = []
        for bucket in range(3):
            # 前两个桶各 3 根 5m，最后一个桶只有 2 根（不完整）。
            children = 3 if bucket < 2 else 2
            for child in range(children):
                rows.append(self._row(base + bucket * 900_000 + child * 300_000, 100 + bucket + child * 0.1))
        bars = generator.load_bars(self._write_source(rows), source_minutes=5)
        self.assertEqual(len(bars), 2)
        self.assertEqual([bar.ts for bar in bars], [base, base + 900_000])

    def test_generate_signals_needs_enough_four_hour_history(self):
        import hlsr_signal_generator as generator

        base = 1_700_000_000_000 // 900_000 * 900_000
        # 400 根 15m ≈ 25 根 4H，少于 regime 需要的 55 根 → 不得给出信号。
        bars = [generator.Bar(base + index * 900_000, 100 + index * 0.01, 100.2 + index * 0.01,
                              99.8 + index * 0.01, 100 + index * 0.01, 1, 1_000_000)
                for index in range(400)]
        self.assertEqual(generator.generate_signals("TEST-USDT-SWAP", bars), [])

    def test_number_treats_missing_values_as_missing(self):
        import altcoin_backtest

        for missing in (None, "", "  ", "x"):
            self.assertIsNone(altcoin_backtest.number(missing, default=None))
        self.assertEqual(altcoin_backtest.number("1.25", default=None), 1.25)
        self.assertEqual(altcoin_backtest.number(None), 0.0)
        self.assertIsNone(altcoin_backtest.decode_bar(["1700000000000", "", "2", "0.5", "1.5", "10", "0", "15", "1"]))


if __name__ == "__main__":
    unittest.main()
