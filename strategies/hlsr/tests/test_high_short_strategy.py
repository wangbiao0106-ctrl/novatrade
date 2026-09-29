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
        sys.path.insert(0, str(SRC))
        import hlsr_market_export

        path = Path("BEAT_USDT_SWAP_5m_20260331T065300Z_20260928T125358Z.jsonl.gz")
        self.assertEqual(hlsr_market_export.symbol_from_path(path), "BEAT-USDT-SWAP")


if __name__ == "__main__":
    unittest.main()
