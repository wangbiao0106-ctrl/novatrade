from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[3]
LAB = ROOT / "strategies" / "liquid_crypto_trend_long"
sys.path.insert(0, str(LAB / "src"))
spec = importlib.util.spec_from_file_location("lctl_backtest", LAB / "src" / "backtest.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


HOUR = module.HOUR_MS


def write_export(path: Path, rows: list[dict]) -> None:
    with gzip.open(path, "wt") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def bar(hour: int, price: float, quote_volume: float, confirmed: bool = True) -> dict:
    return {"timestamp_ms": hour * HOUR, "open": price, "high": price * 1.001,
            "low": price * 0.999, "close": price, "quote_volume": quote_volume,
            "confirmed": confirmed}


class RollingWindowTests(unittest.TestCase):
    def test_rolling_mean_matches_manual(self):
        values = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        result = module.rolling_mean(values, 3)
        self.assertTrue(np.isnan(result[0]) and np.isnan(result[1]))
        self.assertAlmostEqual(result[2], 2.0)
        self.assertAlmostEqual(result[4], 4.0)

    def test_rolling_mean_stays_nan_on_gap(self):
        values = np.array([1.0, np.nan, 3.0, 4.0])
        result = module.rolling_mean(values, 3)
        self.assertTrue(np.isnan(result[3]))

    def test_rolling_std_matches_numpy(self):
        values = np.array([0.01, -0.02, 0.03, 0.005, -0.01, 0.02])
        result = module.rolling_std(values, 4)
        self.assertAlmostEqual(result[3], values[:4].std(ddof=0), places=12)

    def test_rolling_sum_requires_min_bars(self):
        volume = np.array([[1.0], [2.0], [3.0], [4.0]])
        result = module.rolling_sum(volume, 3, 3)
        self.assertTrue(np.isnan(result[1, 0]))
        self.assertAlmostEqual(result[2, 0], 6.0)
        self.assertAlmostEqual(result[3, 0], 9.0)


class EligibilityTests(unittest.TestCase):
    def test_point_in_time_uses_only_past_hours(self):
        # 24 hours of low volume then a single spike; the spike may not make the
        # symbol eligible for the hours that precede it.
        volume = np.full((30, 1), 1_000_000.0)
        volume[25, 0] = 500_000_000.0
        eligible = module.eligibility(volume, 30_000_000, 24, 12)
        self.assertFalse(eligible[24, 0], "spike must not leak backwards")
        self.assertTrue(eligible[25, 0])

    def test_threshold_is_inclusive_at_boundary(self):
        volume = np.full((24, 1), 1_250_000.0)   # 24 * 1.25M = 30M exactly
        eligible = module.eligibility(volume, 30_000_000, 24, 12)
        self.assertTrue(eligible[23, 0])


class TrendPositionTests(unittest.TestCase):
    def make_prices(self, values: list[float]) -> np.ndarray:
        return np.array([[v] for v in values], dtype=np.float64)

    def test_duration_cap_flattens_then_waits_for_flip(self):
        prices = np.concatenate([np.linspace(100, 200, 120), np.linspace(200, 60, 60)])
        positions = module.trend_positions(self.make_prices(list(prices)), 5, 0.01, 20)
        self.assertEqual(positions[0, 0], 0.0)
        self.assertEqual(positions[10, 0], 1.0)
        # after 20 bars in one direction the cap flattens the book
        self.assertEqual(positions[119, 0], 0.0)
        self.assertEqual(positions[150, 0], 0.0, "cap must survive until the band flips")

    def test_hysteresis_holds_inside_the_band(self):
        # 100 -> 102 breaks above the +1% band and opens the long; 101.5 sits
        # back inside the band and must keep the long instead of flattening.
        prices = [100.0] * 5 + [102.0] + [101.5] * 3 + [99.0] * 2
        positions = module.trend_positions(self.make_prices(prices), 3, 0.01, 1000)
        self.assertEqual(positions[4, 0], 0.0)
        self.assertEqual(positions[5, 0], 1.0)
        self.assertEqual(positions[8, 0], 1.0, "inside the band the direction is held")
        self.assertEqual(positions[10, 0], -1.0, "below the lower band flips the direction")

    def test_missing_bar_forces_flat_without_extending_duration(self):
        prices = [100.0] * 6 + [110.0] * 8 + [np.nan] + [110.0] * 8
        positions = module.trend_positions(self.make_prices(prices), 5, 0.01, 6)
        self.assertEqual(positions[13, 0], 0.0)
        self.assertEqual(positions[14, 0], 0.0)
        self.assertEqual(positions[15, 0], 0.0, "gap must not reset or extend the run counter")


class OverlappingWindowTests(unittest.TestCase):
    def test_overlapping_exports_do_not_sum_volume(self):
        with tempfile.TemporaryDirectory() as tmp:
            data = Path(tmp)
            full = [bar(h, 100.0 + h, 1_000.0) for h in range(10)]
            overlap = [bar(h, 100.0 + h, 1_000.0) for h in range(8, 14)]
            # the overlapping window carries fewer 5m rows for hour 8 and 9
            overlap[0]["confirmed"] = False
            write_export(data / "AAA_USDT_SWAP_5m_20260101T000000Z_20260102T000000Z.jsonl.gz", full)
            write_export(data / "AAA_USDT_SWAP_5m_20260102T000000Z_20260103T000000Z.jsonl.gz", overlap)
            panel = module.load_hourly(data, {"AAA"}, None)
        array = panel["AAA"]
        self.assertEqual(len(array), 14)
        # hours 0..7 keep the full window's volume, hours 8..13 keep the overlap
        self.assertTrue(np.allclose(array[:, 5], 1_000.0))


class MetricTests(unittest.TestCase):
    def test_metrics_on_constant_series(self):
        series = np.full(24 * 365, 0.001)
        stat = module.metrics(series)
        self.assertAlmostEqual(stat.total_return, 1.001 ** len(series) - 1, places=9)
        self.assertAlmostEqual(stat.max_drawdown, 0.0)
        self.assertEqual(stat.sharpe, 0.0)

    def test_drawdown_is_measured_from_the_peak(self):
        series = np.array([0.1, 0.1, -0.5, 0.1])
        stat = module.metrics(series)
        self.assertLess(stat.max_drawdown, -0.4)


class ConfigContractTests(unittest.TestCase):
    def test_declared_config_matches_the_rule_source(self):
        config = json.loads((LAB / "config" / "strategy.json").read_text())
        self.assertEqual(config["version"], "1.0.0")
        self.assertEqual(config["position"]["direction"], "long_only")
        self.assertEqual(config["signal"]["ma_window_bars"], 72)
        self.assertEqual(config["signal"]["max_direction_bars"], 96)
        self.assertEqual(config["universe"]["min_quote_volume_24h_usdt"], 30_000_000)
        self.assertIn("# 高流动性趋势",
                      (LAB / "STRATEGY.md").read_text().splitlines()[0])

    def test_universe_snapshot_is_partitioned(self):
        universe = json.loads((LAB / "config" / "universe.json").read_text())
        exclude = {s.upper() for s in universe["exclude"]}
        crypto = {s.upper() for s in universe["crypto_universe"]}
        self.assertFalse(exclude & crypto, "crypto and exclude must not overlap")
        for symbol in ("BTC", "ETH"):
            self.assertIn(symbol, crypto, "mainstream coins are in scope for a long-only rule")


if __name__ == "__main__":
    unittest.main()
