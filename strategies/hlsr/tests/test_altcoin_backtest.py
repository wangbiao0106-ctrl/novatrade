import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SRC = ROOT / "strategies" / "hlsr" / "src"
SPEC = importlib.util.spec_from_file_location("altcoin_backtest", SRC / "altcoin_backtest.py")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

HIGH_SPEC = importlib.util.spec_from_file_location("high_short_strategy", SRC / "high_short_strategy.py")
HIGH_MODULE = importlib.util.module_from_spec(HIGH_SPEC)
sys.modules[HIGH_SPEC.name] = HIGH_MODULE
HIGH_SPEC.loader.exec_module(HIGH_MODULE)


class BacktestHelpersTests(unittest.TestCase):
    @staticmethod
    def cache_rows(start_ms, count, quote_volume=10.0):
        return [
            {"ts": start_ms + index * MODULE.BAR_INTERVAL_MS, "quote_volume": quote_volume}
            for index in range(count)
        ]

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
                self.assertEqual(HIGH_MODULE.select_symbols(data_dir, 1, 2, 3, allow_live_selection_fallback=True), ["AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"])
            finally:
                HIGH_MODULE.candidate_symbols = original

    def test_high_short_offline_symbol_fallback_ranks_quote_volume(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            start_ms = 1_700_000_100_000
            count = HIGH_MODULE.SELECTION_DAYS * 96
            end_ms = start_ms + count * MODULE.BAR_INTERVAL_MS
            for symbol, quote_volume in (("AAA", 100), ("BBB", 300), ("CCC", 300)):
                path = data_dir / f"{symbol}_USDT_SWAP_15m_{start_ms}_{end_ms}.json"
                rows = [
                    {"ts": start_ms + index * MODULE.BAR_INTERVAL_MS, "open": 1.0, "high": 1.0,
                     "low": 1.0, "close": 1.0, "volume": 1.0, "quote_volume": quote_volume}
                    for index in range(count)
                ]
                path.write_text(json.dumps(rows))
            self.assertEqual(HIGH_MODULE.cached_bar_symbols(data_dir, start_ms, end_ms, 3), ["BBB-USDT-SWAP", "CCC-USDT-SWAP", "AAA-USDT-SWAP"])

    def test_high_short_cached_bar_symbols_rejects_malformed_training_rows(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            start_ms = 1_700_000_100_000
            count = HIGH_MODULE.SELECTION_DAYS * 96
            end_ms = start_ms + count * MODULE.BAR_INTERVAL_MS
            rows = [
                {"ts": start_ms + index * MODULE.BAR_INTERVAL_MS, "open": 1.0, "high": 1.0,
                 "low": 1.0, "close": 1.0, "volume": 1.0, "quote_volume": 10.0}
                for index in range(count)
            ]
            rows[count // 2]["quote_volume"] = "nan"
            path = data_dir / f"AAA_USDT_SWAP_15m_{start_ms}_{end_ms}.json"
            path.write_text(json.dumps(rows))
            self.assertEqual(HIGH_MODULE.cached_bar_symbols(data_dir, start_ms, end_ms, 1), [])

    def test_decodes_quote_volume_and_confirmation(self):
        row = ["1000", "1", "2", "0.5", "1.5", "10", "20", "30000000", "1"]
        bar = MODULE.decode_bar(row)
        self.assertEqual(bar.quote_volume, 30000000)
        self.assertIsNone(MODULE.decode_bar(row[:-1] + ["0"]))

    def test_decode_bar_rejects_non_finite_numeric_fields(self):
        row = ["1000", "1", "2", "0.5", "1.5", "10", "20", "30000000", "1"]
        for index in (0, 1, 2, 3, 4, 5, 7):
            malformed = row.copy()
            malformed[index] = "nan"
            self.assertIsNone(MODULE.decode_bar(malformed), f"field index {index}")
            malformed[index] = "inf"
            self.assertIsNone(MODULE.decode_bar(malformed), f"field index {index}")

    def test_rejects_impossible_ohlc_rows(self):
        row = ["1000", "100", "101", "99", "100", "10", "20", "30000000", "1"]
        self.assertIsNone(MODULE.decode_bar(row[:2] + ["90", "99", "95"] + row[5:]))

    def test_candidate_symbols_breaks_volume_ties_by_instrument(self):
        instruments = [{"instId": f"{symbol}-USDT-SWAP", "state": "live", "settleCcy": "USDT", "ctType": "linear", "baseCcy": symbol} for symbol in ("BBB", "AAA", "CCC")]
        tickers = [{"instId": "BBB-USDT-SWAP", "volCcy24h": "100"}, {"instId": "AAA-USDT-SWAP", "volCcy24h": "100"}, {"instId": "CCC-USDT-SWAP", "volCcy24h": "50"}]
        original = MODULE.http_json
        try:
            MODULE.http_json = lambda path, params: {"data": instruments if path == "/public/instruments" else tickers}
            self.assertEqual(MODULE.candidate_symbols(3), ["AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"])
        finally:
            MODULE.http_json = original

    def test_candidate_symbols_does_not_use_live_fallback_for_historical_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            start_ms = 1_700_000_100_000
            end_ms = start_ms + 60 * MODULE.BAR_INTERVAL_MS
            original = MODULE.http_json
            try:
                MODULE.http_json = lambda *_args, **_kwargs: self.fail("historical selection must not call live APIs")
                self.assertEqual(MODULE.candidate_symbols(1, start_ms=start_ms, end_ms=end_ms, data_dir=Path(temporary)), [])
            finally:
                MODULE.http_json = original

    def test_complete_bars_uses_half_open_window(self):
        start_ms = 1_700_000_100_000
        bars = [
            MODULE.Bar(start_ms + index * MODULE.BAR_INTERVAL_MS, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0)
            for index in range(2)
        ]
        self.assertTrue(MODULE._complete_bars(bars, start_ms, start_ms + 2 * MODULE.BAR_INTERVAL_MS))
        self.assertFalse(MODULE._complete_bars(bars, start_ms, start_ms + MODULE.BAR_INTERVAL_MS))

    def test_historical_candidate_selection_uses_training_slice(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            start_ms = 1_700_000_100_000
            count = MODULE.SELECTION_DAYS * 96 + 2
            end_ms = start_ms + count * MODULE.BAR_INTERVAL_MS
            rows = self.cache_rows(start_ms, count)
            for symbol, multiplier in (("AAA", 3.0), ("BBB", 2.0)):
                path = data_dir / f"{symbol}_USDT_SWAP_15m_{start_ms}_{end_ms}.json"
                path.write_text(json.dumps([{**row, "quote_volume": row["quote_volume"] * multiplier} for row in rows]))
            # A short cache is excluded instead of being promoted by a high
            # partial-window volume.
            (data_dir / f"CCC_USDT_SWAP_15m_{start_ms}_{end_ms}.json").write_text(json.dumps(rows[:10]))
            self.assertEqual(MODULE.historical_candidate_symbols(data_dir, start_ms, end_ms, 2), ["AAA-USDT-SWAP", "BBB-USDT-SWAP"])

    def test_late_volume_rows_do_not_change_causal_selection(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            start_ms = 1_700_000_100_000
            required = MODULE.SELECTION_DAYS * 96
            count = required + 2
            end_ms = start_ms + count * MODULE.BAR_INTERVAL_MS
            rows = self.cache_rows(start_ms, count)
            for symbol, multiplier in (("AAA", 2.0), ("BBB", 1.0)):
                values = [{**row, "quote_volume": row["quote_volume"] * multiplier} for row in rows]
                if symbol == "BBB":
                    values[-1]["quote_volume"] = 10_000_000_000.0
                (data_dir / f"{symbol}_USDT_SWAP_15m_{start_ms}_{end_ms}.json").write_text(json.dumps(values))
            self.assertEqual(MODULE.historical_candidate_symbols(data_dir, start_ms, end_ms, 2), ["AAA-USDT-SWAP", "BBB-USDT-SWAP"])

    def test_historical_selection_does_not_require_late_window_completeness(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            start_ms = 1_700_000_100_000
            required = MODULE.SELECTION_DAYS * 96
            count = required + 2
            end_ms = start_ms + count * MODULE.BAR_INTERVAL_MS
            rows = self.cache_rows(start_ms, count)
            rows.pop(required + 1)  # a gap after the causal training slice
            path = data_dir / f"AAA_USDT_SWAP_15m_{start_ms}_{end_ms}.json"
            path.write_text(json.dumps(rows))
            self.assertEqual(MODULE.historical_candidate_symbols(data_dir, start_ms, end_ms, 1), ["AAA-USDT-SWAP"])

    def test_historical_candidate_rejects_out_of_range_timestamps(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            start_ms = 1_700_000_100_000
            count = MODULE.SELECTION_DAYS * 96
            end_ms = start_ms + count * MODULE.BAR_INTERVAL_MS
            rows = self.cache_rows(start_ms, count)
            rows[-1]["ts"] = end_ms
            path = data_dir / f"AAA_USDT_SWAP_15m_{start_ms}_{end_ms}.json"
            path.write_text(json.dumps(rows))
            self.assertEqual(MODULE.historical_candidate_symbols(data_dir, start_ms, end_ms, 1), [])

    def test_historical_candidate_rejects_missing_and_duplicate_timestamps(self):
        start_ms = 1_700_000_100_000
        count = MODULE.SELECTION_DAYS * 96
        end_ms = start_ms + count * MODULE.BAR_INTERVAL_MS
        for broken in ("missing", "duplicate"):
            with self.subTest(broken=broken), tempfile.TemporaryDirectory() as temporary:
                data_dir = Path(temporary)
                rows = self.cache_rows(start_ms, count)
                if broken == "missing":
                    rows.pop(count // 2)
                else:
                    rows[count // 2]["ts"] = rows[count // 2 - 1]["ts"]
                path = data_dir / f"AAA_USDT_SWAP_15m_{start_ms}_{end_ms}.json"
                path.write_text(json.dumps(rows))
                self.assertEqual(MODULE.historical_candidate_symbols(data_dir, start_ms, end_ms, 1), [])

    def test_historical_candidate_tie_sorting_is_deterministic(self):
        with tempfile.TemporaryDirectory() as temporary:
            data_dir = Path(temporary)
            start_ms = 1_700_000_100_000
            count = MODULE.SELECTION_DAYS * 96
            end_ms = start_ms + count * MODULE.BAR_INTERVAL_MS
            for symbol in ("CCC", "AAA", "BBB"):
                path = data_dir / f"{symbol}_USDT_SWAP_15m_{start_ms}_{end_ms}.json"
                path.write_text(json.dumps(self.cache_rows(start_ms, count, 10.0)))
            expected = ["AAA-USDT-SWAP", "BBB-USDT-SWAP", "CCC-USDT-SWAP"]
            self.assertEqual(MODULE.historical_candidate_symbols(data_dir, start_ms, end_ms, 3), expected)
            self.assertEqual(MODULE.historical_candidate_symbols(data_dir, start_ms, end_ms, 3), expected)

    def test_partial_exit_plan_merges_duplicate_target_levels(self):
        plan = MODULE.partial_exit_plan(2.0)
        self.assertEqual([level for level, _, _ in plan], [1.0, 2.0])
        self.assertEqual([fraction for _, fraction, _ in plan], [0.3, 0.7])
        self.assertAlmostEqual(sum(fraction for _, fraction, _ in plan), 1.0)

    def test_backtest_fills_each_partial_target_only_once(self):
        bars = []
        for index in range(170):
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
                values = (1.99, 2.0, 1.94, 1.95)
            elif index == 104:
                # Still below 1R: this used to fill the 1R tranche again.
                values = (1.95, 1.96, 1.94, 1.95)
            elif index == 105:
                values = (1.95, 1.96, 1.89, 1.90)
            elif index == 106:
                values = (1.90, 1.91, 1.84, 1.85)
            else:
                values = (1.8, 1.82, 1.78, 1.8)
            bars.append(MODULE.Bar(index * 900_000, *values, 100.0, 400_000.0))
        btc = [MODULE.Bar(index * 900_000, 1.0, 1.0, 1.0, 1.0, 1.0, 1.0) for index in range(170)]
        params = MODULE.Params(16, 0.02, 0.01, "prev_low", "atr", 0.5, 0.01, 3.0, 2, "close_retest", 8)
        result, trades = MODULE.backtest("X", bars, btc, params, 0.0, 0.0, 0.0)
        self.assertEqual(result.trades, 1)
        self.assertEqual(trades[0].partials, "1R,2R,3R")

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
