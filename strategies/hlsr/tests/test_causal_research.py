"""Regression tests for causal HLSR aggregation and signal generation."""

from __future__ import annotations

import gzip
import importlib.util
import json
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
HLSR_SRC = ROOT / "strategies" / "hlsr" / "src"
sys.path.insert(0, str(HLSR_SRC))


def _module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


high_short = _module("causal_high_short_strategy", HLSR_SRC / "high_short_strategy.py")
signal_generator = _module("causal_hlsr_signal_generator", HLSR_SRC / "hlsr_signal_generator.py")


class CausalResearchTests(unittest.TestCase):
    def test_high_short_resample_drops_gaps_and_duplicates(self):
        bars = [high_short.Bar(i * 900_000, 10, 11, 9, 10, 1, 1)
                for i in range(16)]
        # Replace one expected child with a duplicate timestamp. A count-only
        # implementation would incorrectly treat this as a complete 4h bar.
        bars[5] = bars[4]
        self.assertEqual(high_short.resample(bars, 240), [])

    def test_signal_generator_requires_exact_source_children(self):
        rows = []
        for index in (0, 1, 1):  # duplicate child, missing child 2
            ts = 1_700_000_000_000 + index * 300_000
            rows.append({"timestamp_ms": ts, "open": 100, "high": 101,
                         "low": 99, "close": 100, "volume": 1,
                         "quote_volume": 1, "confirmed": True})
        with tempfile.NamedTemporaryFile(suffix=".jsonl.gz") as temporary:
            path = Path(temporary.name)
            with gzip.open(path, "wt") as stream:
                for row in rows:
                    stream.write(json.dumps(row) + "\n")
            self.assertEqual(signal_generator.load_bars(path, source_minutes=5), [])

    def test_signal_generator_serializes_rejection_reasons(self):
        """A passing candidate must expose the tuple, not the function object."""
        bars = []
        for index in range(200):
            close = 70.0
            # Keep a high liquidity level around 101 while the close remains
            # at 70, so the sweep can close back below that level.
            high = 101.0
            low = 69.5
            open_price = 70.0
            if index == 160:
                open_price, high, low, close = 101.0, 103.0, 100.0, 100.0
            elif index == 161:
                open_price, high, low, close = 101.0, 102.0, 99.0, 100.0
            elif index == 162:
                open_price, high, low, close = 100.0, 101.0, 99.0, 100.0
            bars.append(high_short.Bar(index * 900_000, open_price, high, low, close, 1.0, 400_000.0))
        n = len(bars)
        closes = [bar.close for bar in bars]
        highs = [bar.high for bar in bars]
        lows = [bar.low for bar in bars]
        volumes = [bar.quote_volume for bar in bars]
        signal = signal_generator._candidate_signal(
            "TEST-USDT-SWAP", bars, closes, highs, lows, volumes,
            [55] * n, [1.0] * n,
            [("bearish", 100.0, 100.0, 80.0, 70.0, 1.0)] * 56,
            160, 161, signal_generator.STANDARD_PARAMS, 0.0, 2.0, "TP1:30%,TP2:30%,TP3:40%",
        )
        self.assertIsNotNone(signal)
        self.assertIsInstance(signal.rejection_reasons, tuple)
        self.assertEqual(signal.rejection_score, len(signal.rejection_reasons))

    def test_generate_signals_defaults_to_loaded_config(self):
        base = signal_generator.LabConfig.load()
        custom = replace(base, params=replace(base.params, swing_lookback=99))
        bars = [high_short.Bar(index * 900_000, 100, 101, 99, 100, 1, 1_000_000)
                for index in range(200)]
        observed = []
        original_load = signal_generator.LabConfig.load
        original_candidate = signal_generator._candidate_signal
        try:
            signal_generator.LabConfig.load = staticmethod(lambda path=None: custom)
            signal_generator._candidate_signal = lambda *args, **kwargs: observed.append(args[11]) or None
            signal_generator.generate_signals("TEST-USDT-SWAP", bars)
        finally:
            signal_generator.LabConfig.load = staticmethod(original_load)
            signal_generator._candidate_signal = original_candidate
        self.assertTrue(observed)
        self.assertEqual(observed[0].swing_lookback, 99)


if __name__ == "__main__":
    unittest.main()
