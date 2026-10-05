#!/usr/bin/env python3
"""Check confirmed-candle arithmetic without model calls or exchange access."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.ai_market_facts import market_facts  # noqa: E402
from backend.ai_schema import AISnapshot  # noqa: E402


BTC = "BTC-USDT-SWAP"


def bars(count: int = 60) -> list[dict]:
    start = datetime(2026, 10, 5, tzinfo=timezone.utc)
    return [{
        "timestamp": (start + timedelta(minutes=5 * index)).isoformat().replace("+00:00", "Z"),
        "open": 99.5 + index, "high": 102 + index,
        "low": 98 + index, "close": 100 + index,
        "volume": 40 if index == count - 1 else 20,
        "confirmed": True,
    } for index in range(count)]


def snapshot(candles: list[dict] | None = None, **overrides) -> AISnapshot:
    values = {
        "snapshotId": "facts-snapshot", "capturedAt": "2026-10-05T08:00:00Z",
        "instruments": [{"id": BTC}], "ai": {"selectedInstruments": [BTC]},
        "candles": {f"{BTC}/5m": candles if candles is not None else bars()},
        "tickers": {BTC: {"last": "100", "bidPx": "99", "askPx": "101", "ts": "1791187200000"}},
        "orderBook": {BTC: {"bids": [["99", "10"], ["98", "20"]], "asks": [["101", "5"], ["102", "5"]]}},
        "fundingRates": {BTC: {"fundingRate": "-0.0001", "nextFundingTime": "1791216000000"}},
    }
    values.update(overrides)
    return AISnapshot(**values)


def timeframe(value: AISnapshot) -> dict:
    return market_facts(value)["instruments"][BTC]["timeframes"]["5m"]


class MarketFactsTests(unittest.TestCase):
    def test_exact_windows_returns_range_true_range_and_previous_volume(self):
        result = timeframe(snapshot())
        self.assertEqual(result["usableTrailingConfirmedRowCount"], 60)
        self.assertEqual(result["lastConfirmedClose"], 159)
        for period in (1, 3, 12):
            self.assertAlmostEqual(result[f"return{period}BarPercent"], (159 / (159-period) - 1) * 100)
        self.assertEqual(result["sma20"], 149.5)
        self.assertEqual(result["sma50"], 134.5)
        self.assertEqual(result["recent20High"], 161)
        self.assertEqual(result["recent20Low"], 138)
        self.assertEqual(result["meanTrueRange14"], 4)
        self.assertEqual(result["volumeRatioToPrevious20"], 2)

    def test_forming_candle_is_excluded_and_raw_snapshot_is_untouched(self):
        rows = bars()
        rows.append({"open": 9999, "high": 10010, "low": 9980, "close": 10000, "volume": 999999, "confirmed": False})
        value = snapshot(rows)
        original = deepcopy(value.to_dict())
        result = timeframe(value)
        self.assertEqual(result["confirmedRowCount"], 60)
        self.assertEqual(result["formingRowCount"], 1)
        self.assertEqual(result["lastConfirmedClose"], 159)
        self.assertEqual(result["meanTrueRange14"], 4)
        self.assertEqual(value.to_dict(), original)

    def test_insufficient_data_has_no_partial_window_estimates(self):
        result = timeframe(snapshot(bars(3)))
        self.assertIsNotNone(result["return1BarPercent"])
        for key in ("return3BarPercent", "return12BarPercent", "sma20", "sma50", "recent20High", "recent20Low", "meanTrueRange14", "volumeRatioToPrevious20"):
            self.assertIsNone(result[key], key)
        self.assertIsNotNone(timeframe(snapshot(bars(15)))["meanTrueRange14"])
        self.assertIsNone(timeframe(snapshot(bars(14)))["meanTrueRange14"])
        self.assertIsNone(timeframe(snapshot(bars(20)))["volumeRatioToPrevious20"])
        self.assertIsNotNone(timeframe(snapshot(bars(21)))["volumeRatioToPrevious20"])

    def test_true_range_includes_upward_and_downward_price_gaps(self):
        for prices, final_range in (
            ({"open": 200, "high": 202, "low": 198, "close": 201}, 89),
            ({"open": 10, "high": 12, "low": 8, "close": 11}, 105),
        ):
            rows = bars(15)
            rows[-1].update(prices)
            with self.subTest(prices=prices):
                self.assertAlmostEqual(timeframe(snapshot(rows))["meanTrueRange14"], (13*4+final_range)/14)

    def test_invalid_ohlc_breaks_series_instead_of_bridging_missing_bars(self):
        for changes in ({"close": True}, {"high": 1}, {"low": -1}, {"open": float("nan")}, {"close": "Infinity"}, {"high": None}):
            rows = bars()
            rows[49].update(changes)
            with self.subTest(changes=changes):
                result = timeframe(snapshot(rows))
                self.assertEqual(result["invalidConfirmedOHLCCount"], 1)
                self.assertEqual(result["validConfirmedRowCount"], 59)
                self.assertEqual(result["usableTrailingConfirmedRowCount"], 10)
                self.assertIsNone(result["sma20"])
                self.assertIsNone(result["return12BarPercent"])
                self.assertIsNone(result["meanTrueRange14"])
                self.assertAlmostEqual(result["return3BarPercent"], (159/156-1)*100)

    def test_true_confirmation_and_numeric_strings_are_handled_explicitly(self):
        rows = bars(3)
        for row in rows:
            for key in ("open", "high", "low", "close", "volume"):
                row[key] = str(row[key])
        self.assertEqual(timeframe(snapshot(rows))["lastConfirmedClose"], 102)
        rows[-1]["confirmed"] = 1
        result = timeframe(snapshot(rows))
        self.assertEqual(result["confirmedRowCount"], 2)
        self.assertIsNone(result["lastConfirmedClose"])

    def test_volume_missing_zero_and_negative_values_do_not_invent_ratios(self):
        for change in (None, -1, True, "NaN"):
            rows = bars()
            rows[-2]["volume"] = change
            with self.subTest(change=change):
                self.assertIsNone(timeframe(snapshot(rows))["volumeRatioToPrevious20"])
        rows = bars()
        for row in rows[:-1]:
            row["volume"] = 0
        self.assertIsNone(timeframe(snapshot(rows))["volumeRatioToPrevious20"])
        rows = bars()
        rows[-1]["volume"] = 0
        self.assertEqual(timeframe(snapshot(rows))["volumeRatioToPrevious20"], 0)

    def test_ticker_depth_and_funding_units_are_explicit(self):
        result = market_facts(snapshot())["instruments"][BTC]
        self.assertEqual(result["ticker"]["spreadBasisPoints"], 200)
        self.assertEqual(result["orderBook"]["bidTop5SizeContracts"], 30)
        self.assertEqual(result["orderBook"]["askTop5SizeContracts"], 10)
        self.assertAlmostEqual(result["orderBook"]["top5SizeImbalance"], .5)
        self.assertEqual(result["fundingRate"]["rate"], -.0001)
        self.assertEqual(result["fundingRate"]["nextFundingTimeUnixMilliseconds"], 1791216000000)

    def test_missing_foreign_and_bad_resources_preserve_observed_rows_with_nulls(self):
        eth = "ETH-USDT-SWAP"
        foreign = "DOGE-USDT-SWAP"
        value = snapshot(
            ai={"selectedInstruments": [BTC, eth]},
            tickers={BTC: {"bidPx": "101", "askPx": "99"}, foreign: {"last": "999"}},
            orderBook={BTC: {"bids": [["99", "10"], ["98", None]], "asks": []}},
            fundingRates={},
            candles={f"{foreign}/5m": bars()},
        )
        result = market_facts(value)["instruments"]
        self.assertEqual(set(result), {BTC, eth})
        self.assertIsNone(result[BTC]["ticker"]["spreadBasisPoints"])
        self.assertIsNone(result[BTC]["orderBook"]["bidTop5SizeContracts"])
        self.assertIsNone(result[BTC]["orderBook"]["askTop5SizeContracts"])
        self.assertEqual(result[BTC]["orderBook"]["invalidBidLevelCount"], 1)
        self.assertIsNone(result[eth]["ticker"]["lastPrice"])
        self.assertIsNone(result[eth]["fundingRate"]["rate"])
        self.assertEqual(set(result[eth]["timeframes"]), {"5m", "15m", "1H", "4H"})
        self.assertIsNone(result[eth]["timeframes"]["5m"]["lastConfirmedClose"])

    def test_extreme_values_never_emit_nan_or_infinity(self):
        rows = bars()
        rows[-2].update(open=1e-308, low=1e-308, high=1e-308, close=1e-308)
        rows[-1].update(open=1e308, low=1e308, high=1e308, close=1e308)
        result = market_facts(snapshot(rows, orderBook={BTC: {"bids": [["1", "1e308"]]*5, "asks": [["2", "1e308"]]*5}}))
        encoded = json.dumps(result, allow_nan=False)
        self.assertNotIn("Infinity", encoded)
        self.assertIsNone(result["instruments"][BTC]["timeframes"]["5m"]["return1BarPercent"])
        self.assertIsNone(result["instruments"][BTC]["orderBook"]["bidTop5SizeContracts"])

    def test_top_five_sizes_exclude_deeper_levels(self):
        result = market_facts(snapshot(orderBook={BTC: {
            "bids": [["99", "2"]]*5 + [["98", "999"]],
            "asks": [["101", "1"]]*5 + [["102", "999"]],
        }}))["instruments"][BTC]["orderBook"]
        self.assertEqual(result["bidTop5SizeContracts"], 10)
        self.assertEqual(result["askTop5SizeContracts"], 5)
        self.assertAlmostEqual(result["top5SizeImbalance"], 1/3)


if __name__ == "__main__":
    unittest.main()
