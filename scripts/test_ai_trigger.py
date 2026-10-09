#!/usr/bin/env python3
"""Focused tests for event-driven AI prescreening and fingerprints."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.ai_schema import AIDecision, AIConfig, AISnapshot  # noqa: E402
from backend.ai_trigger import (  # noqa: E402
    data_quality_requires_evaluation,
    decision_fingerprint,
    managed_state,
    trigger_reason,
)
from backend.ai_worker import AIWorker  # noqa: E402


BTC = "BTC-USDT-SWAP"


def snapshot(**overrides: object) -> AISnapshot:
    """Create a valid, private-account snapshot with one observed contract."""
    captured_at = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    values: dict[str, object] = {
        "snapshotId": "snap-1",
        "capturedAt": captured_at,
        "instruments": [{"id": BTC}],
        "ai": {"selectedInstruments": [BTC]},
        "tickers": {BTC: {"last": "100", "bidPx": "99", "askPx": "101", "ts": "1000"}},
        "orderBook": {BTC: {"bids": [["99", "10"]], "asks": [["101", "5"]], "ts": "1000"}},
        "fundingRates": {BTC: {"fundingRate": "0.0001", "nextFundingTime": "2000"}},
        "account": {
            "authenticated": True, "todayLossCount": 0, "todayAIOrderCount": 0,
            "pendingOrders": [], "pendingOrdersKnown": True,
        },
        "risk": {},
        "dataFreshness": {},
    }
    values.update(overrides)
    return AISnapshot(**values)


class FingerprintTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AIConfig(enabled=True, mode="shadow", allowedInstruments=(BTC,))

    def test_transport_timestamps_and_snapshot_id_do_not_change_fingerprint(self) -> None:
        first = snapshot()
        second = snapshot(
            snapshotId="snap-2",
            capturedAt="2026-10-07T08:00:30Z",
            tickers={BTC: {"last": "100", "bidPx": "99", "askPx": "101", "ts": "999999"}},
            orderBook={BTC: {"bids": [["99", "10"]], "asks": [["101", "5"]], "ts": "999999"}},
        )
        self.assertEqual(decision_fingerprint(first, self.config), decision_fingerprint(second, self.config))

    def test_decision_fingerprint_changes_when_price_or_confirmed_close_changes(self) -> None:
        first = snapshot()
        price_changed = snapshot(tickers={BTC: {"last": "102", "bidPx": "101", "askPx": "103"}})
        close_changed = snapshot(candles={f"{BTC}/5m": [{
            "timestamp": "2026-10-07T08:00:00Z", "open": 99, "high": 101, "low": 98,
            "close": 102, "volume": 10, "confirmed": True,
        }]})
        baseline = decision_fingerprint(first, self.config)
        self.assertNotEqual(baseline, decision_fingerprint(price_changed, self.config))
        self.assertNotEqual(baseline, decision_fingerprint(close_changed, self.config))

    def test_account_risk_and_pending_order_changes_are_decision_events(self) -> None:
        baseline = decision_fingerprint(snapshot(), self.config)
        variants = {
            "daily_order_count": snapshot(account={
                "authenticated": True, "todayLossCount": 0,
                "todayAIOrderCount": 1,
                "pendingOrders": [], "pendingOrdersKnown": True,
            }),
            "equity": snapshot(account={
                "authenticated": True, "todayLossCount": 0,
                "todayAIOrderCount": 0, "equityUSD": 10_000,
                "pendingOrders": [], "pendingOrdersKnown": True,
            }),
            "available_equity": snapshot(account={
                "authenticated": True, "todayLossCount": 0,
                "todayAIOrderCount": 0, "availableEquityUSD": 8_000,
                "pendingOrders": [], "pendingOrdersKnown": True,
            }),
            "pnl": snapshot(account={
                "authenticated": True, "todayLossCount": 0,
                "todayAIOrderCount": 0, "todayPnLUSD": -25,
                "pendingOrders": [], "pendingOrdersKnown": True,
            }),
            "pending_order": snapshot(account={
                "authenticated": True, "todayLossCount": 0,
                "todayAIOrderCount": 0,
                "pendingOrders": [{
                    "id": "order-1", "instrumentID": BTC, "side": "buy",
                    "status": "live", "quantity": "1", "price": "100",
                }],
                "pendingOrdersKnown": True,
            }),
            "risk": snapshot(risk={"blocked": True, "reason": "daily loss"}),
            "trading_availability": snapshot(ai={
                "selectedInstruments": [BTC],
                "tradingAvailability": {BTC: {"available": False, "reason": "read only"}},
            }),
        }
        for name, changed in variants.items():
            with self.subTest(name=name):
                self.assertNotEqual(baseline, decision_fingerprint(changed, self.config))

    def test_stop_loss_guard_fingerprint_tracks_cooldown_and_burst_expiry(self) -> None:
        now = datetime.now(timezone.utc)

        def iso(value: datetime) -> str:
            return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")

        stop = {"instrumentID": BTC, "closedAt": iso(now - timedelta(minutes=5)), "realizedPnL": -10.0}
        active = snapshot(account={
            "authenticated": True, "todayLossCount": 1, "todayAIOrderCount": 1,
            "pendingOrders": [], "pendingOrdersKnown": True,
            "reentryCooldowns": {BTC: {
                "closedAt": stop["closedAt"], "blockedUntil": iso(now + timedelta(hours=1)),
                "reason": "stop_loss", "realizedPnL": -10.0,
            }},
            "recentStopLosses": [stop], "recentStopLossWindowSeconds": 3600,
            "recentStopLossLimit": 2,
            "recentStopLossBurstBlockedUntil": iso(now + timedelta(minutes=30)),
        })
        expired = snapshot(account={
            "authenticated": True, "todayLossCount": 1, "todayAIOrderCount": 1,
            "pendingOrders": [], "pendingOrdersKnown": True,
            "reentryCooldowns": {BTC: {
                "closedAt": stop["closedAt"], "blockedUntil": iso(now - timedelta(minutes=1)),
                "reason": "stop_loss", "realizedPnL": -10.0,
            }},
            "recentStopLosses": [stop], "recentStopLossWindowSeconds": 3600,
            "recentStopLossLimit": 2,
            "recentStopLossBurstBlockedUntil": iso(now - timedelta(minutes=1)),
        })
        self.assertNotEqual(
            decision_fingerprint(active, self.config),
            decision_fingerprint(expired, self.config),
        )

        changed_guard_config = replace(self.config, stopLossCooldownSeconds=30)
        self.assertNotEqual(
            decision_fingerprint(snapshot(), self.config),
            decision_fingerprint(snapshot(), changed_guard_config),
        )

    def test_funding_rate_and_next_funding_bucket_are_decision_events(self) -> None:
        baseline = decision_fingerprint(snapshot(), self.config)
        rate_changed = snapshot(fundingRates={BTC: {
            "fundingRate": "0.0002", "nextFundingTime": "2000",
        }})
        next_window_changed = snapshot(fundingRates={BTC: {
            "fundingRate": "0.0001", "nextFundingTime": "1200000",
        }})
        self.assertNotEqual(baseline, decision_fingerprint(rate_changed, self.config))
        self.assertNotEqual(baseline, decision_fingerprint(next_window_changed, self.config))

    def test_model_route_and_strategy_config_are_in_fingerprint(self) -> None:
        baseline = decision_fingerprint(snapshot(), self.config)
        changed_config = AIConfig(
            enabled=True, mode="shadow", allowedInstruments=(BTC,),
            allowOpen=False, maxDailyOrders=1,
        )
        self.assertNotEqual(baseline, decision_fingerprint(snapshot(), changed_config))
        # Retired configuration keys (cooldownSeconds, escalationModel) are
        # still accepted for migration but must not change the fingerprint.
        retired = AIConfig.from_dict({
            **self.config.to_dict(),
            "cooldownSeconds": 60, "escalationModel": "gpt-6.1-sol",
            "escalationReasoningEffort": "medium", "universeMode": "dynamic",
        })
        self.assertEqual(baseline, decision_fingerprint(snapshot(), retired))
        self.assertNotIn("cooldownSeconds", retired.to_dict())
        self.assertNotIn("escalationModel", retired.to_dict())
        # A JSON round trip must be fingerprint-neutral: an int/float drift in a
        # duration default would otherwise force a spurious model evaluation.
        self.assertEqual(baseline, decision_fingerprint(snapshot(), AIConfig.from_dict(self.config.to_dict())))

        changed_model = replace(self.config, routineModel="changed-model")
        changed_effort = replace(self.config, routineReasoningEffort="low")
        self.assertNotEqual(baseline, decision_fingerprint(snapshot(), changed_model))
        self.assertNotEqual(baseline, decision_fingerprint(snapshot(), changed_effort))

    def test_managed_and_data_quality_states_are_fail_closed(self) -> None:
        self.assertFalse(managed_state(snapshot()))
        self.assertTrue(managed_state(snapshot(account={
            "authenticated": True, "todayLossCount": 0,
            "positions": [{"id": "position-1", "instrumentID": BTC}],
        })))
        self.assertFalse(data_quality_requires_evaluation(snapshot()))
        unknown_account = snapshot(account={"authenticated": False, "todayLossCount": 0})
        self.assertTrue(data_quality_requires_evaluation(unknown_account))
        self.assertEqual(trigger_reason(snapshot(), None, "fingerprint"), "initial")
        self.assertEqual(trigger_reason(snapshot(), "fingerprint", "fingerprint"), "unchanged")
        self.assertEqual(trigger_reason(unknown_account, "fingerprint", "fingerprint"), "data-quality")

    def test_healthy_risk_metadata_does_not_disable_prescreening(self) -> None:
        healthy = snapshot(risk={"dataQuality": {
            "equitySource": "okx",
            "accountRefreshError": None,
            "accountRefreshRetryable": False,
        }})
        self.assertFalse(data_quality_requires_evaluation(healthy))


class _CountingRunner:
    def __init__(self, *, fail: bool = False) -> None:
        self.calls = 0
        self.fail = fail

    async def run(self, current: AISnapshot, config: AIConfig) -> AIDecision:
        self.calls += 1
        if self.fail:
            raise RuntimeError("runner unavailable")
        return AIDecision.hold(current.snapshotId, "no setup")


class _RejectingRunner(_CountingRunner):
    async def run(self, current: AISnapshot, config: AIConfig) -> AIDecision:
        self.calls += 1
        valid_until = (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat().replace("+00:00", "Z")
        return AIDecision(
            schemaVersion=1,
            decisionId=f"open-{self.calls}",
            snapshotId=current.snapshotId,
            action="open",
            instrumentID=BTC,
            direction="long",
            orderType="market",
            stopLossPrice=99,
            takeProfitPrice=103,
            winRate=.6,
            riskRewardRatio=2,
            leverage=1,
            confidence=.8,
            validUntil=valid_until,
            reasonCode="candidate",
            reason="candidate",
        )


class WorkerEventDrivenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AIConfig(enabled=True, mode="shadow", allowedInstruments=(BTC,))

    def test_unchanged_snapshot_skips_runner_and_writes_prescreen_audit(self) -> None:
        async def run() -> tuple[int, dict, list[dict]]:
            runner = _CountingRunner()
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NOVATRADE_AI_EVENT_DRIVEN": "1"}, clear=False):
                worker = AIWorker(config=self.config, runner=runner, state_dir=Path(directory))
                first = await worker.run_once(snapshot())
                second = await worker.run_once(snapshot(snapshotId="snap-2"))
                records = [json.loads(line) for line in (Path(directory) / "ai-decisions.jsonl").read_text().splitlines()]
                self.assertTrue(first.accepted)
                self.assertTrue(second.accepted)
                return runner.calls, worker.get_status(), records

        calls, status, records = asyncio.run(run())
        self.assertEqual(calls, 1)
        self.assertEqual(status["lastEvaluationSource"], "prescreen")
        self.assertEqual(status["skippedCycles"], 1)
        self.assertEqual(status["lastDecision"]["decisionId"], "hold")
        skips = [row for row in records if row["type"] == "prescreen-skip"]
        self.assertEqual(len(skips), 1)
        self.assertEqual(skips[0]["decisionSource"], "prescreen")
        self.assertEqual(skips[0]["reason"], "unchanged")
        self.assertTrue(skips[0]["decisionFingerprint"])

    def test_changed_signal_calls_runner_again(self) -> None:
        async def run() -> int:
            runner = _CountingRunner()
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NOVATRADE_AI_EVENT_DRIVEN": "1"}, clear=False):
                worker = AIWorker(config=self.config, runner=runner, state_dir=Path(directory))
                await worker.run_once(snapshot())
                await worker.run_once(snapshot(snapshotId="snap-2", tickers={BTC: {"last": "102", "bidPx": "101", "askPx": "103"}}))
            return runner.calls

        self.assertEqual(asyncio.run(run()), 2)

    def test_failed_model_call_does_not_advance_fingerprint_or_skip_retry(self) -> None:
        async def run() -> tuple[int, str | None]:
            runner = _CountingRunner(fail=True)
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NOVATRADE_AI_EVENT_DRIVEN": "1"}, clear=False):
                worker = AIWorker(config=self.config, runner=runner, state_dir=Path(directory))
                await worker.run_once(snapshot())
                await worker.run_once(snapshot(snapshotId="snap-2", capturedAt="2026-10-07T08:00:30Z"))
                return runner.calls, worker._last_event_fingerprint

        calls, fingerprint = asyncio.run(run())
        self.assertEqual(calls, 2)
        self.assertIsNone(fingerprint)

    def test_policy_rejection_advances_fingerprint_and_does_not_repeat_static_call(self) -> None:
        async def run() -> tuple[int, str | None]:
            runner = _RejectingRunner()
            config = AIConfig(
                enabled=True, mode="shadow", allowedInstruments=(BTC,), allowOpen=False,
            )
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NOVATRADE_AI_EVENT_DRIVEN": "1"}, clear=False):
                worker = AIWorker(config=config, runner=runner, state_dir=Path(directory))
                first = await worker.run_once(snapshot())
                second = await worker.run_once(snapshot(snapshotId="snap-2"))
                self.assertFalse(first.accepted)
                self.assertTrue(second.accepted)
                return runner.calls, worker._last_event_fingerprint

        calls, fingerprint = asyncio.run(run())
        self.assertEqual(calls, 1)
        self.assertIsNotNone(fingerprint)

    def test_open_position_forces_evaluation_even_when_market_fingerprint_is_same(self) -> None:
        async def run() -> int:
            runner = _CountingRunner()
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NOVATRADE_AI_EVENT_DRIVEN": "1"}, clear=False):
                worker = AIWorker(config=self.config, runner=runner, state_dir=Path(directory))
                await worker.run_once(snapshot())
                await worker.run_once(snapshot(
                    snapshotId="snap-2",
                    account={"authenticated": True, "todayLossCount": 0, "positions": [{"id": "position-1", "instrumentID": BTC}]},
                ))
            return runner.calls

        self.assertEqual(asyncio.run(run()), 2)

    def test_unknown_pending_orders_force_evaluation(self) -> None:
        self.assertTrue(data_quality_requires_evaluation(snapshot(account={
            "authenticated": True, "todayLossCount": 0,
            "pendingOrders": None, "pendingOrdersKnown": False,
        })))

    def test_unknown_positions_force_evaluation_even_when_pending_orders_are_known(self) -> None:
        self.assertTrue(data_quality_requires_evaluation(snapshot(account={
            "authenticated": True, "todayLossCount": 0,
            "positions": [], "positionsKnown": False,
            "pendingOrders": [], "pendingOrdersKnown": True,
        })))
        self.assertTrue(data_quality_requires_evaluation(snapshot(account={
            "authenticated": True, "todayLossCount": 0,
            "positions": [], "positionsKnown": True,
            "pendingOrders": [], "pendingOrdersKnown": True,
            "dataQuality": {"positionsAvailable": False, "positionsError": "timeout"},
        })))

    def test_pending_order_change_forces_evaluation(self) -> None:
        async def run() -> int:
            runner = _CountingRunner()
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NOVATRADE_AI_EVENT_DRIVEN": "1"}, clear=False):
                worker = AIWorker(config=self.config, runner=runner, state_dir=Path(directory))
                await worker.run_once(snapshot())
                await worker.run_once(snapshot(
                    snapshotId="snap-2",
                    account={
                        "authenticated": True, "todayLossCount": 0,
                        "pendingOrders": [{
                            "id": "order-1", "instrumentID": BTC,
                            "side": "buy", "status": "live", "quantity": "1", "price": "100",
                        }],
                        "pendingOrdersKnown": True,
                    },
                ))
            return runner.calls

        self.assertEqual(asyncio.run(run()), 2)

    def test_watchdog_rechecks_an_unchanged_snapshot_after_skip_budget(self) -> None:
        async def run() -> tuple[int, list[dict]]:
            runner = _CountingRunner()
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ,
                {"NOVATRADE_AI_EVENT_DRIVEN": "1", "NOVATRADE_AI_EVENT_MAX_SKIP_SECONDS": "1"},
                clear=False,
            ):
                state_dir = Path(directory)
                worker = AIWorker(config=self.config, runner=runner, state_dir=state_dir)
                await worker.run_once(snapshot())
                await worker.run_once(snapshot(snapshotId="snap-skip"))
                self.assertEqual(runner.calls, 1)
                worker._last_model_evaluated_at = datetime.now(timezone.utc) - timedelta(seconds=2)
                reason, fingerprint = worker._event_trigger(snapshot(snapshotId="snap-watchdog"))
                self.assertEqual(reason, "watchdog")
                self.assertIsNotNone(fingerprint)
                await worker.run_once(snapshot(snapshotId="snap-watchdog"))
                records = [json.loads(line) for line in (state_dir / "ai-decisions.jsonl").read_text().splitlines()]
            return runner.calls, records

        calls, records = asyncio.run(run())
        self.assertEqual(calls, 2)
        self.assertFalse(any(row.get("type") == "prescreen-skip" and row.get("snapshotId") == "snap-watchdog" for row in records))

    def test_shadow_mode_audits_candidate_without_skipping_model(self) -> None:
        async def run() -> tuple[int, list[dict]]:
            runner = _CountingRunner()
            with tempfile.TemporaryDirectory() as directory, patch.dict(
                os.environ, {"NOVATRADE_AI_EVENT_MODE": "shadow"}, clear=False
            ):
                state_dir = Path(directory)
                worker = AIWorker(config=self.config, runner=runner, state_dir=state_dir)
                await worker.run_once(snapshot())
                await worker.run_once(snapshot(snapshotId="snap-shadow"))
                records = [json.loads(line) for line in (state_dir / "ai-decisions.jsonl").read_text().splitlines()]
                return runner.calls, records

        calls, records = asyncio.run(run())
        self.assertEqual(calls, 2)
        self.assertTrue(any(row.get("type") == "prescreen-candidate" for row in records))

    def test_restart_forces_a_first_model_evaluation(self) -> None:
        async def run() -> tuple[int, int]:
            first_runner = _CountingRunner()
            second_runner = _CountingRunner()
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"NOVATRADE_AI_EVENT_DRIVEN": "1"}, clear=False):
                state_dir = Path(directory)
                first_worker = AIWorker(config=self.config, runner=first_runner, state_dir=state_dir)
                await first_worker.run_once(snapshot())
                second_worker = AIWorker(config=self.config, runner=second_runner, state_dir=state_dir)
                await second_worker.run_once(snapshot(snapshotId="snap-after-restart"))
            return first_runner.calls, second_runner.calls

        self.assertEqual(asyncio.run(run()), (1, 1))


if __name__ == "__main__":
    unittest.main()
