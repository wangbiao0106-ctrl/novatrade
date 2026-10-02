"""Regression tests for causal sweep-reversal research indicators."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[3]
RESEARCH = ROOT / "strategies" / "sweep_reversal_short" / "research"
sys.path.insert(0, str(RESEARCH))

try:  # The research engine's optional pandas dependency may be absent in CI.
    import engine  # noqa: E402
except ModuleNotFoundError:  # pragma: no cover - environment-dependent
    engine = None


class CausalResearchTests(unittest.TestCase):
    @unittest.skipIf(engine is None, "research engine dependencies are unavailable")
    def test_prior_sma_excludes_current_bar(self):
        result = engine.prior_sma(np.array([10.0, 10.0, 30.0]), 2)
        self.assertTrue(np.isnan(result[0]))
        self.assertEqual(result[1], 10.0)
        self.assertEqual(result[2], 10.0)

    @unittest.skipIf(engine is None, "research engine dependencies are unavailable")
    def test_engine_volume_baseline_excludes_sweep_volume(self):
        values = np.array([10.0, 10.0, 100.0])
        data = {"o": values, "h": values, "l": values, "c": values, "v": values,
                "t": np.arange(3, dtype=np.int64), "qv": values}
        self.assertEqual(engine.precompute(data, vol_n=2)["volmean"][2], 10.0)


if __name__ == "__main__":
    unittest.main()
