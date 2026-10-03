import unittest

try:
    from build_kline_cache import aggregate_candles
except ModuleNotFoundError:  # pragma: no cover - supports unittest discovery from repo root
    from scripts.build_kline_cache import aggregate_candles


def candle(timestamp_ms: int, price: float) -> dict:
    return {
        "timestamp_ms": timestamp_ms,
        "open": price,
        "high": price + 1,
        "low": price - 1,
        "close": price + 0.5,
        "volume": 2.0,
        "quote_volume": 20.0,
    }


class AggregateCandleTests(unittest.TestCase):
    def test_builds_all_requested_timeframes_in_one_pass(self) -> None:
        base = 0
        candles = [candle(base + index * 5 * 60 * 1000, float(index + 1)) for index in range(48)]
        result = aggregate_candles(candles, ("15m", "30m", "1h", "4h"))
        self.assertEqual([len(result[timeframe]) for timeframe in ("15m", "30m", "1h", "4h")], [16, 8, 4, 1])
        self.assertEqual(result["15m"][0]["open"], 1.0)
        self.assertEqual(result["15m"][0]["close"], 3.5)
        self.assertEqual(result["15m"][0]["high"], 4.0)
        self.assertEqual(result["15m"][0]["low"], 0.0)
        self.assertEqual(result["15m"][0]["volume"], 6.0)
        self.assertEqual(result["4h"][0]["quote_volume"], 960.0)

    def test_drops_partial_and_missing_bars(self) -> None:
        interval = 5 * 60 * 1000
        candles = [candle(index * interval, 1.0) for index in range(3)]
        candles += [candle(5 * interval, 1.0), candle(6 * interval, 1.0), candle(7 * interval, 1.0)]
        result = aggregate_candles(candles, ("15m",))
        self.assertEqual(len(result["15m"]), 1)
        self.assertEqual(result["15m"][0]["timestamp_ms"], 0)


if __name__ == "__main__":
    unittest.main()
