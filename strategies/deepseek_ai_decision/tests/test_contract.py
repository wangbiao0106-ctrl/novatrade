#!/usr/bin/env python3
"""Contract tests for the DeepSeek AI strategy package."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
STRATEGY = ROOT / "strategies" / "deepseek_ai_decision"


class DeepSeekAIStrategyContractTests(unittest.TestCase):
    def test_identity_and_runtime_mapping(self) -> None:
        config = json.loads((STRATEGY / "config/strategy.json").read_text(encoding="utf-8"))
        self.assertEqual(config["strategy_id"], "deepseek_ai_decision")
        self.assertEqual(config["provider"], "deepseek-harness")
        self.assertEqual(config["source_of_truth"], "strategies/deepseek_ai_decision/STRATEGY.md")
        runtime = config["runtime_integration"]
        self.assertEqual(runtime["strategy_id"], "deepseek")
        self.assertEqual(runtime["decision_endpoint"], "/api/v1/ai/strategies/deepseek")
        self.assertFalse(runtime["enabled_by_default"])
        self.assertEqual(config["lifecycle"], "candidate")
        self.assertIn("规则真源", (STRATEGY / "STRATEGY.md").read_text(encoding="utf-8"))

    def test_deepseek_route_keeps_experimental_controls_explicit(self) -> None:
        config = json.loads((STRATEGY / "config/strategy.json").read_text(encoding="utf-8"))
        route = config["model_route"]
        self.assertEqual(route["provider"], "deepseek-harness")
        self.assertEqual(route["model"], "deepseek-v4-pro")
        self.assertEqual(route["reasoning_effort"], "low")
        self.assertEqual(route["profile"], "optimized")
        self.assertEqual(route["encoding"], "compact60")


if __name__ == "__main__":
    unittest.main()
