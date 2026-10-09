#!/usr/bin/env python3
"""Contract tests for the Codex AI strategy package."""

from __future__ import annotations

from dataclasses import fields
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).resolve().parents[3]
STRATEGY = ROOT / "strategies" / "codex_ai_decision"
sys.path.insert(0, str(ROOT / "backend"))

from ai_policy import ACCOUNT_DAILY_LOSS_PERCENT  # noqa: E402
from ai_schema import AIConfig  # noqa: E402


# Every machine parameter the package publishes must map onto exactly one
# AIConfig field. The mapping is asserted for both values and coverage, so
# adding an AIConfig parameter without publishing it here fails the contract.
SIGNAL_PARAMETER_FIELDS = {
    "minimum_confidence": "minimumConfidence",
    "decision_interval_seconds": "decisionIntervalSeconds",
    "cli_timeout_seconds": "cliTimeoutSeconds",
    "snapshot_max_age_seconds": "snapshotMaxAgeSeconds",
    "max_output_bytes": "maxOutputBytes",
    "max_consecutive_failures": "maxConsecutiveFailures",
    "max_daily_orders": "maxDailyOrders",
    "max_daily_losses": "maxDailyLosses",
    "margin_per_order_usd": "marginPerOrderUSD",
    "max_leverage": "maxLeverage",
    "stop_loss_cooldown_seconds": "stopLossCooldownSeconds",
    "recent_stop_loss_window_seconds": "recentStopLossWindowSeconds",
    "recent_stop_loss_limit": "recentStopLossLimit",
}
RISK_POLICY_FIELDS = {
    "allow_open": "allowOpen",
    "allow_close": "allowClose",
    "allow_cancel": "allowCancel",
    "require_stop_loss": "requireStopLoss",
}
# Runtime/mode fields that are not numeric strategy parameters: they describe
# how the worker runs rather than what it is allowed to risk.
RUNTIME_FIELDS = frozenset({
    "provider", "enabled", "mode", "allowedInstruments",
    "routineModel", "routineReasoningEffort",
})


def strategy_config() -> dict:
    return json.loads((STRATEGY / "config/strategy.json").read_text(encoding="utf-8"))


class CodexAIStrategyContractTests(unittest.TestCase):
    def test_identity_and_runtime_mapping(self) -> None:
        config = strategy_config()
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
        config = strategy_config()
        self.assertEqual(config["model_route"], {
            "provider": "codex", "model": "gpt-6-luna", "reasoning_effort": "medium",
        })

    def test_published_parameters_match_the_runtime_defaults(self) -> None:
        """The package's machine parameters must equal AIConfig defaults.

        These are two declarations of one configuration, so without this check
        the JSON silently drifts from the runtime contract.
        """
        config = strategy_config()
        defaults = AIConfig()
        for json_key, field in SIGNAL_PARAMETER_FIELDS.items():
            with self.subTest(parameter=json_key):
                self.assertIn(json_key, config["signal_parameters"])
                self.assertEqual(
                    type(getattr(defaults, field))(config["signal_parameters"][json_key]),
                    getattr(defaults, field),
                )
        for json_key, field in RISK_POLICY_FIELDS.items():
            with self.subTest(parameter=json_key):
                self.assertIn(json_key, config["risk_policy"])
                self.assertIs(config["risk_policy"][json_key], getattr(defaults, field))
        with self.subTest(parameter="account_daily_loss_percent"):
            self.assertEqual(
                float(config["risk_policy"]["account_daily_loss_percent"]),
                ACCOUNT_DAILY_LOSS_PERCENT,
            )

    def test_every_config_parameter_is_published(self) -> None:
        published = set(SIGNAL_PARAMETER_FIELDS) | set(RISK_POLICY_FIELDS)
        declared = {item.name for item in fields(AIConfig)}
        self.assertEqual(
            declared - RUNTIME_FIELDS,
            {SIGNAL_PARAMETER_FIELDS[key] for key in SIGNAL_PARAMETER_FIELDS}
            | {RISK_POLICY_FIELDS[key] for key in RISK_POLICY_FIELDS},
        )
        self.assertEqual(published & RUNTIME_FIELDS, set())

    def test_retired_parameter_keys_are_not_published(self) -> None:
        config = strategy_config()
        for retired in ("cooldown_seconds", "escalation_model", "escalation_reasoning_effort",
                        "risk_budget_percent"):
            self.assertNotIn(retired, config["signal_parameters"])
            self.assertNotIn(retired, config["risk_policy"])
        self.assertNotIn("cooldownSeconds", AIConfig().to_dict())


if __name__ == "__main__":
    unittest.main()
