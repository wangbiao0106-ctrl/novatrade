from __future__ import annotations

import importlib.util
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "personal_trading_style_backtest" / "src"
sys.path.insert(0, str(SRC))
spec = importlib.util.spec_from_file_location("personal_style_price_backtest", SRC / "price_backtest.py")
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)


UTC = timezone.utc


def config() -> dict:
    return {
        "price_backtest": {
            "atr_period": 72,
            "lookback_6h_bars": 72,
            "lookback_24h_bars": 288,
            "stop_atr": 2.0,
            "target_r": 1.0,
            "max_hold_bars": 20,
            "fee_rate_one_way": 0.0,
            "slippage_one_way": 0.0,
        }
    }


class PriceBacktestTests(unittest.TestCase):
    def test_next_bar_entry_and_stop_first_when_ohlc_hits_both(self):
        start_ms = int(datetime(2026, 8, 1, tzinfo=UTC).timestamp() * 1000)
        bars = []
        for index in range(400):
            ts = start_ms + index * module.BAR_MS
            if index == 301:
                bars.append(module.Bar(ts, 100.0, 105.0, 90.0, 100.0, 100.0))
            else:
                bars.append(module.Bar(ts, 100.0, 101.0, 99.0, 100.0, 100.0))
        episode = module.Episode(
            "ALT-USDT-SWAP", "short",
            datetime.fromtimestamp(bars[300].ts / 1000, UTC),
            datetime.fromtimestamp(bars[301].ts / 1000, UTC),
            100, 100, 1, 1, 1, 0, 5, 0, 0, 0, 0, 0, "realized_exit", 0, 0,
        )
        trade = module.simulate_episode(episode, bars, config())
        self.assertIsNotNone(trade)
        assert trade is not None
        self.assertEqual(trade.entry_time, datetime.fromtimestamp(bars[301].ts / 1000, UTC).isoformat())
        self.assertEqual(trade.exit_reason, "stop")

    def test_policy_filters_use_only_prior_features(self):
        trade = module.PriceTrade(
            "ALT-USDT-SWAP", "short", "a", "b", "c", 100, 99, 102, 98, 2,
            0.5, 0.01, "target", 2, 0.2, 0.3, -0.01, 2, 1,
        )
        self.assertTrue(module.policy_matches(trade, {"direction": "short", "min_return_24h": 0.2}))
        self.assertFalse(module.policy_matches(trade, {"direction": "long"}))


if __name__ == "__main__":
    unittest.main()
