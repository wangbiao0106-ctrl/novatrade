#!/usr/bin/env python3
"""Regression tests for the completeness check in scripts/export_market_data.py."""

import gzip
import json
import tempfile
import unittest
from pathlib import Path

try:  # Works both as ``python scripts/test_export_market_data.py`` and unittest module.
    import export_market_data as exporter
except ModuleNotFoundError:
    from scripts import export_market_data as exporter

INTERVAL = exporter.BAR_MILLISECONDS["5m"]


def candle(timestamp_ms: int, **overrides):
    row = {
        "timestamp": exporter.iso_utc(timestamp_ms),
        "timestamp_ms": timestamp_ms,
        "open": "1",
        "high": "2",
        "low": "0.5",
        "close": "1.5",
        "volume": "7",
        "quote_volume": "10",
        "confirmed": True,
    }
    row.update(overrides)
    return row


class CompleteCandleTests(unittest.TestCase):
    def test_accepts_closed_finite_bar(self) -> None:
        self.assertTrue(exporter.is_complete_candle(candle(0)))
        self.assertTrue(exporter.is_complete_candle(candle(0, confirmed="1")))

    def test_rejects_open_bar(self) -> None:
        for value in (False, "0", "", None):
            with self.subTest(confirmed=value):
                self.assertFalse(exporter.is_complete_candle(candle(0, confirmed=value)))

    def test_rejects_missing_or_invalid_volume(self) -> None:
        missing = candle(0)
        del missing["volume"]
        self.assertFalse(exporter.is_complete_candle(missing))
        for field in ("volume", "quote_volume"):
            for value in ("NaN", "", "-1", "inf"):
                with self.subTest(field=field, value=value):
                    self.assertFalse(exporter.is_complete_candle(candle(0, **{field: value})))

    def test_accepts_zero_volume(self) -> None:
        # A quiet market legitimately trades nothing in a 5-minute bar.
        self.assertTrue(exporter.is_complete_candle(candle(0, volume="0", quote_volume="0")))

    def test_rejects_invalid_prices(self) -> None:
        for overrides in ({"close": "nan"}, {"low": "0"}, {"open": "5"}):
            with self.subTest(**overrides):
                self.assertFalse(exporter.is_complete_candle(candle(0, **overrides)))


class ExistingResultTests(unittest.TestCase):
    def _write(self, directory: Path, rows) -> Path:
        path = directory / "ALT_USDT_SWAP_5m_test.jsonl.gz"
        with gzip.open(path, "wt", encoding="utf-8") as stream:
            for row in rows:
                stream.write(json.dumps(row) + "\n")
        return path

    def _skip(self, rows) -> bool:
        with tempfile.TemporaryDirectory() as temp:
            path = self._write(Path(temp), rows)
            start, end = rows[0]["timestamp_ms"], rows[-1]["timestamp_ms"]
            return exporter.existing_result("ALT-USDT-SWAP", path.name, path, start, end, "5m") is not None

    def test_skips_only_a_complete_file(self) -> None:
        rows = [candle(index * INTERVAL) for index in range(4)]
        self.assertTrue(self._skip(rows))

    def test_open_last_bar_forces_refetch(self) -> None:
        rows = [candle(index * INTERVAL) for index in range(4)]
        rows[-1]["confirmed"] = False
        self.assertFalse(self._skip(rows))

    def test_bad_volume_forces_refetch(self) -> None:
        rows = [candle(index * INTERVAL) for index in range(4)]
        rows[1]["volume"] = "NaN"
        self.assertFalse(self._skip(rows))
        rows = [candle(index * INTERVAL) for index in range(4)]
        del rows[2]["volume"]
        self.assertFalse(self._skip(rows))


if __name__ == "__main__":
    unittest.main()
