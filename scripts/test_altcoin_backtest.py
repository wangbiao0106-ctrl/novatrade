import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("altcoin_backtest", "scripts/altcoin_backtest.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

HIGH_SPEC = importlib.util.spec_from_file_location("high_short_strategy", "scripts/high_short_strategy.py")
HIGH_MODULE = importlib.util.module_from_spec(HIGH_SPEC)
sys.modules[HIGH_SPEC.name] = HIGH_MODULE
HIGH_SPEC.loader.exec_module(HIGH_MODULE)


class BacktestHelpersTests(unittest.TestCase):
    def test_short_entry_slippage_is_adverse(self):
        self.assertEqual(MODULE.short_entry_price(100.0, 0.01), 99.0)

    def test_parse_utc_accepts_z_and_defaults_naive_values_to_utc(self):
        self.assertEqual(MODULE.parse_utc("2026-09-26T19:00:00Z").isoformat(), "2026-09-26T19:00:00+00:00")
        self.assertEqual(MODULE.parse_utc("2026-09-26T19:00:00").isoformat(), "2026-09-26T19:00:00+00:00")
        self.assertEqual(HIGH_MODULE.parse_utc("2026-09-26T19:00:00Z"), MODULE.parse_utc("2026-09-26T19:00:00Z"))

    def test_high_short_uses_only_completed_htf_buckets(self):
        bars = [HIGH_MODULE.HTFBar(0, 1, 1, 1, 1, 1), HIGH_MODULE.HTFBar(240 * 60_000, 1, 1, 1, 1, 1)]
        self.assertEqual(HIGH_MODULE.latest_completed_index(bars, 240 * 60_000 - 1, 240 * 60_000), -1)
        self.assertEqual(HIGH_MODULE.latest_completed_index(bars, 240 * 60_000, 240 * 60_000), 0)

    def test_high_short_symbol_cache_preserves_ranked_selection_and_exclusions(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            cache_path = HIGH_MODULE._selection_cache_path(data_dir, 1, 2, 2)
            cache_path.write_text(json.dumps(["SOL-USDT-SWAP", "AAA-USDT-SWAP", "BBB-USDT-SWAP"]))
            self.assertEqual(HIGH_MODULE.cached_symbols(data_dir, 1, 2, 2), ["AAA-USDT-SWAP", "BBB-USDT-SWAP"])

    def test_high_short_short_cache_is_not_treated_as_complete(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            cache_path = HIGH_MODULE._selection_cache_path(data_dir, 1, 2, 3)
            cache_path.write_text(json.dumps(["AAA-USDT-SWAP"]))
            original = HIGH_MODULE.candidate_symbols
            try:
                HIGH_MODULE.candidate_symbols = lambda limit: ["AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"][:limit]
                self.assertEqual(HIGH_MODULE.select_symbols(data_dir, 1, 2, 3), ["AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"])
            finally:
                HIGH_MODULE.candidate_symbols = original

    def test_high_short_offline_symbol_fallback_ranks_quote_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            for symbol, quote_volume in (("AAA", 100), ("BBB", 300), ("CCC", 300)):
                path = data_dir / f"{symbol}_USDT_SWAP_15m_1_2.json"
                path.write_text(json.dumps([{"quote_volume": quote_volume}]))
            self.assertEqual(HIGH_MODULE.cached_bar_symbols(data_dir, 1, 2, 3), ["BBB-USDT-SWAP", "CCC-USDT-SWAP", "AAA-USDT-SWAP"])

    def test_decodes_quote_volume_and_confirmation(self):
        row = ["1000", "1", "2", "0.5", "1.5", "10", "20", "30000000", "1"]
        bar = MODULE.decode_bar(row)
        self.assertEqual(bar.quote_volume, 30000000)
        self.assertIsNone(MODULE.decode_bar(row[:-1] + ["0"]))

    def test_candidate_symbols_breaks_volume_ties_by_instrument(self):
        instruments = [{"instId": f"{symbol}-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "baseCcy": symbol} for symbol in ("BBB", "AAA", "CCC")]
        tickers = [{"instId": "BBB-USDT-SWAP", "volCcy24h": "100"}, {"instId": "AAA-USDT-SWAP", "volCcy24h": "100"}, {"instId": "CCC-USDT-SWAP", "volCcy24h": "50"}]
        original = MODULE.http_json
        try:
            MODULE.http_json = lambda path, params: {"data": instruments if path == "/public/instruments" else tickers}
            self.assertEqual(MODULE.candidate_symbols(3), ["AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"])
        finally:
            MODULE.http_json = original

    def test_probability_prior_is_uniform(self):
        result = MODULE.beta_interval(0, 0, simulations=2000)
        self.assertAlmostEqual(result["posterior_mean"], 0.5)
        self.assertLess(result["lower_95"], 0.1)
        self.assertGreater(result["upper_95"], 0.9)

    def test_parameter_grid_covers_requested_exit_families(self):
        values = MODULE.grid()
        self.assertEqual(len(values), 432)
        self.assertEqual({item.target_r for item in values}, {3.0, 4.0, 6.0})
        self.assertEqual({item.weakness for item in values}, {"prev_low", "bear_half", "ema"})
        self.assertEqual({item.entry_mode for item in values}, {"close_retest", "wick_retest", "breakdown_retest"})

    def test_stop_exits_when_bar_high_touches_stop_without_target(self):
        bars = []
        for index in range(160):
            if index < 96:
                values = (1.0, 1.0, 1.0, 1.0)
            elif index == 96:
                values = (1.99, 2.0, 1.98, 1.99)
            elif index == 97:
                values = (1.95, 1.99, 1.90, 1.98)
            elif index == 98:
                values = (1.98, 1.99, 1.97, 1.98)
            elif index in (99, 100, 101):
                values = (2.0, 2.0, 2.0, 2.0)
            elif index == 102:
                values = (1.99, 1.99, 1.95, 1.99)
            elif index == 103:
                # The high crosses the stop, while the low stays above 1R.
                values = (1.99, 2.1, 1.98, 2.0)
            else:
                values = (1.8, 1.82, 1.78, 1.8)
            bars.append(MODULE.Bar(index * 900_000, *values, 100.0, 400_000.0))
        btc = [MODULE.Bar(index * 900_000, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0) for index in range(160)]
        params = MODULE.Params(16, 0.02, 0.01, "prev_low", "atr", 0.5, 0.01, 3.0, 2, "close_retest", 8)

        result, trades = MODULE.backtest("X", bars, btc, params, 0.0, 0.0, 0.0)

        self.assertEqual(result.trades, 1)
        self.assertEqual(trades[0].outcome, "loss")
        self.assertLess(trades[0].net_r, 0.0)

    def test_aggregate_orders_trades_by_entry_time(self):
        params = MODULE.grid()[0]
        btc = [MODULE.Bar(0, 1, 1, 1, 1, 1, 1)] * 150
        early = MODULE.Trade("EARLY", 100, 101, 1, 2, 0.5, -1, "loss", "", -1)
        late = MODULE.Trade("LATE", 200, 201, 1, 2, 0.5, 1, "win", "", 1)
        original = MODULE.backtest
        try:
            MODULE.backtest = lambda symbol, bars, btc, params, fee, slip, funding: (MODULE.empty_result(params), [late if symbol == "LATE" else early])
            _, trades = MODULE.aggregate({"LATE": [], "EARLY": []}, btc, params, 0, 0, 0)
        finally:
            MODULE.backtest = original
        self.assertEqual([trade.symbol for trade in trades], ["EARLY", "LATE"])


if __name__ == "__main__":
    unittest.main()
