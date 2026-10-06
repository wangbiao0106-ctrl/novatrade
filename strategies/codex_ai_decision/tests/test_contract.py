#!/usr/bin/env python3
"""Contract tests for the Codex AI strategy package."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
STRATEGY = ROOT / "strategies" / "codex_ai_decision"


class CodexAIStrategyContractTests(unittest.TestCase):
    def test_identity_and_runtime_mapping(self) -> None:
        config = json.loads((STRATEGY / "config/strategy.json").read_text(encoding="utf-8"))
        self.assertEqual(config["strategy_id"], "codex_ai_decision")
        self.assertEqual(config["provider"], "codex")
        self.assertEqual(config["source_of_truth"], "strategies/codex_ai_decision/STRATEGY.md")
        runtime = config["runtime_integration"]
        self.assertEqual(runtime["strategy_id"], "codex")
        self.assertEqual(runtime["decision_endpoint"], "/api/v1/ai/strategies/codex")
        self.assertFalse(runtime["enabled_by_default"])
        self.assertEqual(config["lifecycle"], "candidate")
        self.assertIn("规则真源", (STRATEGY / "STRATEGY.md").read_text(encoding="utf-8"))

    def test_codex_route_is_fixed(self) -> None:
        config = json.loads((STRATEGY / "config/strategy.json").read_text(encoding="utf-8"))
        self.assertEqual(config["model_route"], {
            "provider": "codex", "model": "gpt-6-luna", "reasoning_effort": "medium",
        })


if __name__ == "__main__":
    unittest.main()
