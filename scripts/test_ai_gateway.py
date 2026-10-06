#!/usr/bin/env python3
"""Focused unit tests for the Codex decision boundary and order ledger."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
import tempfile
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.ai_policy import PolicyState, snapshot_freshness, validate_decision  # noqa: E402
from backend.ai_schema import AIDecision, AIInstrumentAssessment, AIChatResponse, AIConfig, AISnapshot, SchemaError, decision_json_schema, normalize_ai_chat_patch  # noqa: E402
from backend.ai_worker import AIWorker, CodexError, CodexRunner, _prompt_snapshot  # noqa: E402
from backend.order_gateway import InstrumentSpec, OrderGateway, OrderGatewayError, OrderNotSubmittedError  # noqa: E402


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def restore_prompt_snapshot(value: dict) -> dict:
    """Decode the column-table representation documented in the prompt."""
    result = dict(value)
    result["candles"] = {
        key: [dict(zip(series["columns"], row)) for row in series["rows"]]
        if isinstance(series, dict) else series
        for key, series in value["candles"].items()
    }
    return result


class AIGatewayTests(unittest.TestCase):
    def snapshot(self) -> AISnapshot:
        return AISnapshot(snapshotId="snap-1", capturedAt=iso(datetime.now(timezone.utc)), instruments=[{"id": "BTC-USDT-SWAP"}], account={"availableEquityUSD": 1000, "todayLossCount": 0}, risk={})

    def decision(self, **overrides) -> dict:
        value = {"schemaVersion": 1, "decisionId": "decision-1", "snapshotId": "snap-1", "action": "open", "instrumentID": "BTC-USDT-SWAP", "direction": "long", "orderType": "market", "riskBudgetPercent": 1, "stopLossPrice": 90, "winRate": .5, "riskRewardRatio": 2.0, "leverage": 1.0, "confidence": .9, "validUntil": iso(datetime.now(timezone.utc) + timedelta(minutes=1)), "reasonCode": "test", "reason": "test"}
        value.update(overrides)
        return value

    def assessment(self, instrument="BTC-USDT-SWAP", **overrides) -> dict:
        value = {
            "instrumentID": instrument, "direction": "long", "winRate": .5,
            "riskRewardRatio": 2.0, "limitPrice": 100, "stopLossPrice": 90,
            "takeProfitPrice": 120, "confidence": .9, "entryEligible": True,
            "unmetConditions": [], "reason": "回踩支撑，等待限价成交。",
        }
        value.update(overrides)
        return value

    def assessed_decision(self, **overrides) -> AIDecision:
        return AIDecision.from_dict(self.decision(
            orderType="limit", limitPrice=100, takeProfitPrice=120,
            assessments=[self.assessment()], **overrides,
        ))

    def grouped_snapshot(self, count=16) -> AISnapshot:
        ids = [f"COIN{index}-USDT-SWAP" for index in range(count)]
        return AISnapshot(
            snapshotId="snap-grouped", capturedAt=self.snapshot().capturedAt,
            instruments=[{"id": item, "tickSize": "0.000001"} for item in ids],
            candles={f"{item}/{interval}": [{
                "id": row, "timestamp": "2026-10-05T16:30:00Z", "open": 100.123456789,
                "high": 101.123456789, "low": 99.123456789, "close": 100.623456789,
                "volume": 10.0123456789, "confirmed": row < 59,
            } for row in range(60)] for item in ids for interval in ("5m", "15m", "1H", "4H")},
            tickers={item: {"last": "100.623456789"} for item in ids},
            orderBook={item: {"bids": [["100", "10"]], "asks": [["101", "11"]]} for item in ids},
            fundingRates={item: {"fundingRate": ".0001"} for item in ids},
            ai={"selectedInstruments": ids}, account={"availableEquityUSD": 1000, "todayLossCount": 0,
                "positions": [{"instrumentID": ids[0], "direction": "long"}], "pendingOrders": [{"id": "order-1", "instrumentID": ids[0]}]},
            risk={"blocked": False}, dataFreshness={"maxAgeSeconds": 90},
        )

    def test_grouped_workflow_preserves_complete_data_and_submits_only_final_intent(self):
        snapshot = self.grouped_snapshot()
        original = snapshot.to_dict()
        ids = snapshot.observed_instruments()

        async def run():
            calls, snapshots, schemas = [], [], []
            active = 0
            max_active = 0
            completed = 0
            gateway = AsyncMock()

            async def launch(*command, **kwargs):
                nonlocal active, max_active, completed
                self.assertEqual(command[command.index("-m") + 1], "gpt-6-luna")
                self.assertIn("model_reasoning_effort=medium", command)
                self.assertTrue(kwargs["start_new_session"])
                schema = json.loads(Path(command[command.index("--output-schema") + 1]).read_text())
                schemas.append(schema)

                async def communicate(prompt_bytes):
                    nonlocal active, max_active, completed
                    prompt = prompt_bytes.decode()
                    calls.append(prompt)
                    if "GROUP REPORTS:\n" in prompt:
                        self.assertEqual(completed, 4)
                        gateway.assert_not_awaited()
                        reports = json.loads(prompt.split("GROUP REPORTS:\n", 1)[1])
                        selected = reports[0]["assessments"][0]
                        row = self.decision(snapshotId=snapshot.snapshotId, instrumentID=selected["instrumentID"], orderType="limit", limitPrice=100, takeProfitPrice=120)
                        return json.dumps(row).encode(), b""
                    group = restore_prompt_snapshot(json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1]))
                    snapshots.append(group)
                    active += 1
                    max_active = max(max_active, active)
                    await asyncio.sleep(.01)
                    active -= 1
                    completed += 1
                    rows = [self.assessment(item) for item in group["ai"]["selectedInstruments"]]
                    row = self.decision(snapshotId=snapshot.snapshotId, instrumentID=rows[0]["instrumentID"], assessments=rows, orderType="limit", limitPrice=100, takeProfitPrice=120)
                    return json.dumps(row).encode(), b""

                return SimpleNamespace(returncode=0, communicate=communicate)

            with tempfile.TemporaryDirectory() as directory, patch("backend.ai_worker.asyncio.create_subprocess_exec", side_effect=launch):
                worker = AIWorker(config=AIConfig(enabled=True, mode="demo-active", allowedInstruments=tuple(ids)), runner=CodexRunner("stub"), order_gateway=gateway, state_dir=Path(directory))
                result = await worker.run_once(snapshot)
                gateway.assert_awaited_once()
                self.assertEqual(gateway.await_args.args[0], result.decision)
                return result, calls, snapshots, schemas, max_active

        result, calls, groups, schemas, max_active = asyncio.run(run())
        self.assertTrue(result.accepted)
        self.assertEqual(result.decision.action, "open")
        self.assertEqual([item.instrumentID for item in result.decision.assessments], ids)
        self.assertEqual(len(calls), 5)
        self.assertEqual(max_active, 4)
        union = []
        for group in groups:
            selected = group["ai"]["selectedInstruments"]
            union.extend(selected)
            self.assertEqual(len(selected), 4)
            for field in ("snapshotId", "capturedAt", "account", "risk", "dataFreshness"):
                self.assertEqual(group[field], original[field])
            for field in ("tickers", "orderBook", "fundingRates"):
                self.assertEqual(group[field], {item: original[field][item] for item in selected})
            self.assertEqual(group["candles"], {key: value for key, value in original["candles"].items() if any(key.startswith(item + "/") for item in selected)})
        self.assertEqual(union, ids)
        self.assertEqual(snapshot.to_dict(), original)
        self.assertNotIn("assessments", schemas[-1]["properties"])
        self.assertNotIn("assessments", schemas[-1]["required"])
        self.assertTrue(all(row["properties"]["snapshotId"]["enum"] == [snapshot.snapshotId] for row in schemas))

    def test_group_coordinator_can_hold_close_or_cancel_without_entry_candidate(self):
        snapshot = self.grouped_snapshot(5)
        ids = snapshot.observed_instruments()

        async def run(action):
            async def invoke(prompt, schema, config, **kwargs):
                if "GROUP REPORTS:\n" in prompt:
                    self.assertIn('"allowClose":true', prompt)
                    self.assertIn('"allowCancel":true', prompt)
                    self.assertIn('"pendingOrders"', prompt)
                    return self.decision(snapshotId=snapshot.snapshotId, action=action, instrumentID=None if action == "hold" else ids[0], orderID="order-1", winRate=None, riskRewardRatio=None)
                group = json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1])
                return self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None, assessments=[self.assessment(item, entryEligible=False, unmetConditions=["尚未确认"]) for item in group["ai"]["selectedInstruments"]])

            runner = CodexRunner("unused")
            with patch.object(runner, "_run_prompt", side_effect=invoke):
                decision = await runner.run(snapshot, AIConfig(enabled=True, mode="shadow", allowedInstruments=tuple(ids)))
                result = validate_decision(decision, snapshot, AIConfig(enabled=True, mode="shadow", allowedInstruments=tuple(ids)))
                self.assertTrue(result.accepted)
                self.assertEqual(decision.action, action)
                self.assertEqual(len(decision.assessments), 5)

        for action in ("hold", "close", "cancel"):
            with self.subTest(action=action):
                asyncio.run(run(action))

    def test_group_failure_cancels_siblings_without_coordinator(self):
        snapshot = self.grouped_snapshot()

        async def run():
            cancelled = []
            runner = CodexRunner("unused")

            async def invoke(prompt, schema, config, **kwargs):
                self.assertNotIn("GROUP REPORTS:\n", prompt)
                group = json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1])["ai"]["selectedInstruments"]
                if group[0] == "COIN0-USDT-SWAP":
                    await asyncio.sleep(.01)
                    return self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None, assessments=[])
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    cancelled.append(group[0])
                    raise

            with patch.object(runner, "_run_prompt", side_effect=invoke), self.assertRaises(CodexError):
                await runner.run(snapshot, AIConfig())
            self.assertEqual(len(cancelled), 3)

        asyncio.run(run())

    def test_group_parallelism_remains_bounded_with_more_than_four_groups(self):
        snapshot = self.grouped_snapshot(17)

        async def run():
            active = 0
            maximum = 0
            runner = CodexRunner("unused")

            async def single(group, config, **kwargs):
                nonlocal active, maximum
                active += 1
                maximum = max(active, maximum)
                await asyncio.sleep(.01)
                active -= 1
                return AIDecision.from_dict(self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None, assessments=[self.assessment(item) for item in group.observed_instruments()]))

            with patch.object(runner, "_run_single", side_effect=single), patch.object(runner, "_run_prompt", AsyncMock(return_value=self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None))):
                result = await runner.run(snapshot, AIConfig())
            self.assertEqual(maximum, 4)
            self.assertEqual(len(result.assessments), 17)

        asyncio.run(run())

    def test_group_and_coordinator_have_separate_bounded_phase_timeouts(self):
        snapshot = self.grouped_snapshot()

        async def run():
            runner = CodexRunner("unused")
            coordinator_started = False
            coordinator_cancelled = False

            async def invoke(prompt, schema, config, **kwargs):
                nonlocal coordinator_started, coordinator_cancelled
                if "GROUP REPORTS:\n" in prompt:
                    coordinator_started = True
                    try:
                        await asyncio.sleep(.15)
                    except asyncio.CancelledError:
                        coordinator_cancelled = True
                        raise
                    return self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None)
                group = json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1])["ai"]["selectedInstruments"]
                await asyncio.sleep(.15)
                return self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None, assessments=[self.assessment(item) for item in group])

            clock = asyncio.get_running_loop()
            started = clock.time()
            config = AIConfig(cliTimeoutSeconds=.25)
            with patch.object(runner, "_run_prompt", side_effect=invoke):
                decision = await runner.run(snapshot, config)
            self.assertTrue(coordinator_started)
            self.assertFalse(coordinator_cancelled)
            self.assertEqual(len(decision.assessments), 16)
            self.assertGreater(clock.time() - started, .25)
            self.assertLess(clock.time() - started, .5)
            self.assertEqual(config.cliTimeoutSeconds, .25)
            self.assertEqual(runner.last_run_metadata["analysisTimeoutSeconds"], .25)
            self.assertEqual(runner.last_run_metadata["coordinatorTimeoutSeconds"], .25)
            self.assertEqual(runner.last_run_metadata["stage"], "complete")

        asyncio.run(run())

    def test_coordinator_timeout_retains_complete_analysis_and_reports_failure_stage(self):
        snapshot = self.grouped_snapshot()

        async def run():
            runner = CodexRunner("unused")

            async def invoke(prompt, schema, config, **kwargs):
                if "GROUP REPORTS:\n" in prompt:
                    await asyncio.sleep(1)
                    raise AssertionError("coordinator must time out")
                group = json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1])["ai"]["selectedInstruments"]
                return self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None, assessments=[self.assessment(item) for item in group])

            with patch.object(runner, "_run_prompt", side_effect=invoke), self.assertRaises(CodexError) as caught:
                await runner.run(snapshot, AIConfig(cliTimeoutSeconds=.1))
            error = caught.exception
            self.assertIn("coordinator phase failed (4/4 groups complete)", str(error))
            self.assertEqual(error.safe_decision.action, "hold")
            self.assertEqual(error.safe_decision.reasonCode, "COORDINATOR_FAILED")
            self.assertEqual([item.instrumentID for item in error.safe_decision.assessments], snapshot.observed_instruments())
            with self.assertRaises(AttributeError):
                error.safe_decision = None
            self.assertEqual(runner.last_run_metadata["failureStage"], "coordinator")
            self.assertEqual(runner.last_run_metadata["completedGroups"], 4)
            self.assertEqual(runner.last_run_metadata["assessmentCount"], 16)

        asyncio.run(run())

    def test_worker_keeps_failed_coordinator_rows_paired_and_halts_until_restart(self):
        snapshot = self.grouped_snapshot()
        config = AIConfig(enabled=True, mode="demo-active", allowedInstruments=tuple(snapshot.observed_instruments()), cliTimeoutSeconds=.2, maxConsecutiveFailures=3)
        original_config = config.to_dict()

        async def run():
            runner = CodexRunner("unused")
            failed = True
            calls = 0

            async def invoke(prompt, schema, config, **kwargs):
                nonlocal calls
                calls += 1
                if "GROUP REPORTS:\n" in prompt:
                    if failed:
                        raise CodexError("coordinator unavailable")
                    return self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None)
                group = json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1])["ai"]["selectedInstruments"]
                return self.decision(snapshotId=snapshot.snapshotId, action="hold", instrumentID=None, assessments=[self.assessment(item) for item in group])

            with tempfile.TemporaryDirectory() as directory, patch.object(runner, "_run_prompt", side_effect=invoke):
                gateway = AsyncMock()
                worker = AIWorker(config=config, runner=runner, order_gateway=gateway,
                                  snapshot_provider=AsyncMock(return_value=snapshot), state_dir=Path(directory))
                for _ in range(3):
                    result = await worker.run_once(snapshot)
                    self.assertFalse(result.accepted)
                    self.assertEqual(result.decision.reasonCode, "COORDINATOR_FAILED")
                    self.assertEqual(len(result.decision.assessments), 16)
                status = worker.get_status()
                self.assertEqual(status["state"], "halted")
                self.assertEqual(status["consecutiveFailures"], 3)
                self.assertEqual(status["lastDecision"]["snapshotId"], snapshot.snapshotId)
                self.assertEqual(status["lastDecisionInstruments"], snapshot.observed_instruments())
                self.assertEqual(status["lastDecisionFreshness"]["capturedAt"], snapshot.capturedAt)
                self.assertIn("coordinator phase failed", status["lastError"])
                records = [json.loads(line) for line in (Path(directory) / "ai-decisions.jsonl").read_text().splitlines()]
                decisions = [row for row in records if row["type"] == "decision"]
                workflows = [row for row in records if row["type"] == "analysis-workflow"]
                self.assertEqual(len(decisions), 3)
                self.assertTrue(all(row["rawDecision"] is None and row["accepted"] is False for row in decisions))
                self.assertEqual(workflows[-1]["workflow"]["failureStage"], "coordinator")
                self.assertEqual(workflows[-1]["workflow"]["completedGroups"], 4)
                before = calls
                await worker.run_once(snapshot)
                self.assertEqual(calls, before)
                self.assertEqual(worker.get_config(), original_config)
                gateway.assert_not_awaited()
                # A completed halted loop must not poll the runner again.
                await worker._loop()
                self.assertEqual(calls, before)
                failed = False
                await worker.start()
                for _ in range(100):
                    if worker.get_status()["lastError"] is None:
                        break
                    await asyncio.sleep(.005)
                self.assertEqual(worker.get_status()["state"], "running")
                self.assertEqual(worker.get_status()["consecutiveFailures"], 0)
                self.assertGreater(calls, before)
                self.assertEqual(worker.get_config(), original_config)
                await worker.stop()

        asyncio.run(run())

    def test_workflow_audit_whitelists_counts_and_durations(self):
        class Runner:
            last_run_metadata = {"workflow": "grouped", "stage": "complete", "groupCount": 4,
                                 "completedGroups": 4, "durationSeconds": 1.5,
                                 "rawSnapshot": "must-not-leak", "apiKey": "must-not-leak",
                                 "analysisDurationSeconds": float("nan")}

            async def run(self, snapshot, config):
                return AIDecision.hold(snapshot.snapshotId, "no setup")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(config=AIConfig(enabled=True, mode="shadow"), runner=Runner(), state_dir=Path(directory))
                await worker.run_once(self.snapshot())
                records = [json.loads(line) for line in (Path(directory) / "ai-decisions.jsonl").read_text().splitlines()]
                return next(row for row in records if row["type"] == "analysis-workflow")

        record = asyncio.run(run())
        self.assertEqual(record["workflow"], {"workflow": "grouped", "stage": "complete", "groupCount": 4, "completedGroups": 4, "durationSeconds": 1.5})
        self.assertNotIn("must-not-leak", json.dumps(record))

    def test_group_timeout_and_external_cancel_kill_every_active_child(self):
        snapshot = self.grouped_snapshot()

        async def run(external_cancel):
            children, stopped = [], []

            async def launch(*command, **kwargs):
                process = SimpleNamespace(returncode=None, pid=10000 + len(children))
                children.append(process)

                async def communicate(prompt):
                    await asyncio.sleep(10)

                process.communicate = communicate
                return process

            async def stop(process):
                stopped.append(process.pid)
                process.returncode = -9

            runner = CodexRunner("stub")
            with patch("backend.ai_worker.asyncio.create_subprocess_exec", side_effect=launch), patch.object(runner, "_stop_timed_out_process", side_effect=stop):
                task = asyncio.create_task(runner.run(snapshot, AIConfig(cliTimeoutSeconds=.15)))
                if external_cancel:
                    while len(children) < 4:
                        await asyncio.sleep(.001)
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                else:
                    with self.assertRaises(CodexError):
                        await task
            self.assertEqual(len(children), 4)
            self.assertEqual(sorted(stopped), sorted(child.pid for child in children))

        for external in (False, True):
            with self.subTest(external=external):
                asyncio.run(run(external))

    def test_timeout_kills_descendant_after_group_leader_has_exited(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                pid_path = Path(directory) / "child.pid"
                executable = Path(directory) / "codex"
                executable.write_text(
                    "#!" + sys.executable + "\nimport subprocess, sys\nsys.stdin.read()\n"
                    "child = subprocess.Popen(['sleep', '10'])\n"
                    "open(" + repr(str(pid_path)) + ", 'w').write(str(child.pid))\n",
                    encoding="utf-8",
                )
                executable.chmod(0o700)
                runner = CodexRunner(str(executable))
                with patch.object(CodexRunner, "_decision_prompt", return_value="test snapshot"), self.assertRaises(CodexError) as raised:
                    await runner.run(self.snapshot(), AIConfig(cliTimeoutSeconds=1))
                self.assertIn("timed out", str(raised.exception))
                child_pid = int(pid_path.read_text())
                await asyncio.sleep(.02)
                result = await asyncio.create_subprocess_exec("ps", "-p", str(child_pid), "-o", "stat=", stdout=asyncio.subprocess.PIPE)
                output, _ = await result.communicate()
                self.assertTrue(not output.strip() or output.strip().startswith(b"Z"), output)

        asyncio.run(run())

    def test_historical_decision_without_assessments_is_compatible(self):
        self.assertEqual(AIDecision.from_dict(self.decision()).assessments, [])

    def test_assessment_schema_rejects_invalid_and_inconsistent_values(self):
        for changes in (
            {"winRate": float("nan")}, {"riskRewardRatio": float("inf")},
            {"limitPrice": 0}, {"stopLossPrice": -1}, {"takeProfitPrice": True},
            {"direction": "neutral"}, {"entryEligible": 1},
            {"unmetConditions": ["置信度不足"]}, {"instrumentID": "BTC"},
        ):
            with self.subTest(changes=changes), self.assertRaises(SchemaError):
                AIInstrumentAssessment.from_dict(self.assessment(**changes))

    def test_full_16_contract_hold_covers_each_observed_id(self):
        ids = [f"COIN{index}-USDT-SWAP" for index in range(16)]
        rows = [self.assessment(
            item, direction="neutral", entryEligible=False, winRate=None,
            riskRewardRatio=None, limitPrice=None, stopLossPrice=None,
            takeProfitPrice=None, unmetConditions=["历史不足，无法可靠估计。"],
        ) for item in ids]
        value = AIDecision.from_dict(self.decision(action="hold", instrumentID=None, assessments=rows))
        value.require_complete_assessments(ids)
        snapshot = AISnapshot(snapshotId="snap-1", capturedAt=self.snapshot().capturedAt, instruments=[{"id": item} for item in ids])
        self.assertTrue(validate_decision(value, snapshot, AIConfig(enabled=True, mode="shadow", allowedInstruments=tuple(ids))).accepted)
        self.assertEqual(len(value.to_dict()["assessments"]), 16)
        self.assertIsNone(value.assessments[0].winRate)
        schema = decision_json_schema(snapshot.snapshotId, ids)
        self.assertEqual(schema["properties"]["snapshotId"]["enum"], ["snap-1"])
        assessment_schema = schema["properties"]["assessments"]
        self.assertEqual(assessment_schema["minItems"], 16)
        self.assertEqual(assessment_schema["maxItems"], 16)
        self.assertEqual(assessment_schema["items"]["properties"]["instrumentID"]["enum"], ids)
        self.assertEqual(set(assessment_schema["items"]["required"]), set(assessment_schema["items"]["properties"]))

    def test_policy_rejects_incomplete_and_extra_assessment_coverage(self):
        for rows in ([self.assessment("ETH-USDT-SWAP")], [self.assessment(), self.assessment("ETH-USDT-SWAP")]):
            value = AIDecision.from_dict(self.decision(action="hold", instrumentID=None, assessments=rows))
            result = validate_decision(value, self.snapshot(), AIConfig(enabled=True, mode="shadow"))
            self.assertFalse(result.accepted)
            self.assertIn("exactly once", result.reason)

    def test_selected_open_must_match_its_assessment(self):
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP",))
        self.assertTrue(validate_decision(self.assessed_decision(), self.snapshot(), config).accepted)
        for key, value in {
            "direction": "short", "winRate": .6, "riskRewardRatio": 3,
            "confidence": .8, "limitPrice": 101, "stopLossPrice": 89, "takeProfitPrice": 125,
        }.items():
            row = self.assessed_decision().to_dict()
            row[key] = value
            with self.subTest(key=key):
                result = validate_decision(row, self.snapshot(), config)
                self.assertFalse(result.accepted)
                self.assertIn("does not match", result.reason)

    def test_open_cannot_select_an_ineligible_assessment(self):
        value = self.assessed_decision().to_dict()
        value["assessments"][0].update(entryEligible=False, unmetConditions=["尚未确认"])
        result = validate_decision(value, self.snapshot(), AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP",)))
        self.assertFalse(result.accepted)
        self.assertIn("entryEligible", result.reason)

    def test_open_ratio_cannot_exceed_its_proposed_price_setup(self):
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP",))
        value = self.assessed_decision().to_dict()
        value["riskRewardRatio"] = value["assessments"][0]["riskRewardRatio"] = 4
        result = validate_decision(value, self.snapshot(), config)
        self.assertFalse(result.accepted)
        self.assertIn("exceeds its proposed price setup", result.reason)
        value["riskRewardRatio"] = value["assessments"][0]["riskRewardRatio"] = 2.02
        self.assertTrue(validate_decision(value, self.snapshot(), config).accepted)

    def test_close_cancel_ignore_entry_quality_and_setup_price_semantics(self):
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP",))
        bad_entry = self.assessment(confidence=.1, winRate=.1, riskRewardRatio=.2, stopLossPrice=110)
        for action in ("close", "cancel"):
            with self.subTest(action=action):
                value = self.decision(action=action, orderID="order-1", assessments=[bad_entry])
                self.assertTrue(validate_decision(value, self.snapshot(), config).accepted)

    def test_valid_open_is_independent_of_nonselected_entry_quality(self):
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP", "ETH-USDT-SWAP"))
        base = self.snapshot()
        snapshot = AISnapshot(snapshotId=base.snapshotId, capturedAt=base.capturedAt, instruments=[{"id": item} for item in config.allowedInstruments], account=base.account)
        value = self.assessed_decision().to_dict()
        value["assessments"].append(self.assessment("ETH-USDT-SWAP", winRate=.1, confidence=.1, stopLossPrice=110))
        self.assertTrue(validate_decision(value, snapshot, config).accepted)

    def test_eligible_assessment_must_meet_existing_quality_gates(self):
        config = AIConfig(enabled=True, mode="shadow", minimumConfidence=.75, allowedInstruments=("BTC-USDT-SWAP",))
        for changes in ({"confidence": .7}, {"winRate": .44}, {"riskRewardRatio": 1.9}, {"stopLossPrice": None}):
            value = self.decision(action="hold", assessments=[self.assessment(**changes)])
            with self.subTest(changes=changes):
                self.assertFalse(validate_decision(value, self.snapshot(), config).accepted)

    def test_proposed_price_geometry_matches_trade_direction(self):
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP",))
        for changes in ({"direction": "short"}, {"takeProfitPrice": 95}, {"stopLossPrice": 110}):
            with self.subTest(changes=changes):
                value = self.decision(action="hold", assessments=[self.assessment(**changes)])
                result = validate_decision(value, self.snapshot(), config)
                self.assertFalse(result.accepted)
                self.assertIn("conflict with direction", result.reason)

    def test_rejected_open_preserves_id_assessments_and_raw_audit(self):
        decision = self.assessed_decision()

        class Runner:
            async def run(self, snapshot, config):
                return decision

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                gateway = AsyncMock()
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="demo-active", allowOpen=False, allowedInstruments=("BTC-USDT-SWAP",)),
                    runner=Runner(), order_gateway=gateway, state_dir=Path(directory),
                )
                result = await worker.run_once(self.snapshot())
                records = [json.loads(line) for line in (Path(directory) / "ai-decisions.jsonl").read_text().splitlines()]
                gateway.assert_not_awaited()
                return result, worker.get_status(), records[-1]

        result, status, audit = asyncio.run(run())
        self.assertFalse(result.accepted)
        self.assertEqual(result.decision.action, "hold")
        self.assertEqual(result.decision.decisionId, decision.decisionId)
        self.assertEqual(result.decision.assessments, decision.assessments)
        self.assertEqual(status["lastDecision"]["assessments"], decision.to_dict()["assessments"])
        self.assertEqual(audit["rawDecision"], decision.to_dict())
        self.assertEqual(audit["decision"]["action"], "hold")
        self.assertEqual(audit["reason"], "open actions are disabled")

    def test_gateway_failure_keeps_latest_assessment_snapshot(self):
        decision = self.assessed_decision()

        class Runner:
            async def run(self, snapshot, config):
                return decision

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="demo-active", allowedInstruments=("BTC-USDT-SWAP",)),
                    runner=Runner(), order_gateway=AsyncMock(side_effect=RuntimeError("gateway failed")),
                    state_dir=Path(directory),
                )
                await worker.run_once(self.snapshot())
                return worker.get_status()

        status = asyncio.run(run())
        self.assertEqual(status["state"], "error")
        self.assertEqual(status["lastDecision"], decision.to_dict())
        self.assertEqual(status["lastDecisionInstruments"], ["BTC-USDT-SWAP"])
        self.assertTrue(status["lastDecisionFreshness"]["valid"])

    def test_repeated_gateway_failures_still_halt_with_latest_assessments(self):
        class Runner:
            def __init__(self):
                self.count = 0

            async def run(runner, snapshot, config):
                runner.count += 1
                return self.assessed_decision(decisionId=f"failure-{runner.count}")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="demo-active", cooldownSeconds=0, maxConsecutiveFailures=3, allowedInstruments=("BTC-USDT-SWAP",)),
                    runner=Runner(), order_gateway=AsyncMock(side_effect=RuntimeError("gateway failed")),
                    state_dir=Path(directory),
                )
                counts = []
                for _ in range(3):
                    await worker.run_once(self.snapshot())
                    counts.append(worker.get_status()["consecutiveFailures"])
                return counts, worker.get_status()

        counts, status = asyncio.run(run())
        self.assertEqual(counts, [1, 2, 3])
        self.assertEqual(status["state"], "halted")
        self.assertEqual(status["lastDecision"]["decisionId"], "failure-3")
        self.assertEqual(len(status["lastDecision"]["assessments"]), 1)

    def test_prompt_exposes_quality_gates_and_independent_per_contract_setups(self):
        config = AIConfig(enabled=True, mode="shadow", minimumConfidence=.81, allowOpen=False, maxLeverage=4)
        prompt = CodexRunner._decision_prompt(self.snapshot(), config)
        self.assertIn('"minimumConfidence":0.81', prompt)
        self.assertIn('"minimumWinRate":0.45', prompt)
        self.assertIn('"minimumRiskRewardRatio":2.0', prompt)
        self.assertIn('"allowOpen":false', prompt)
        self.assertIn('"requireStopLoss":true', prompt)
        self.assertIn('"todayLossCount":0', prompt)
        self.assertIn("Assess each contract independently", prompt)
        self.assertIn("EVERY OBSERVED CONTRACT exactly once", prompt)
        self.assertIn("Never fabricate source prices", prompt)
        self.assertIn("at most ONE contract", prompt)

    def test_hold_and_account_blocks_preserve_conditional_analysis_in_prompt(self):
        snapshot = AISnapshot(
            snapshotId="snap-plan", capturedAt=self.snapshot().capturedAt,
            instruments=[{"id": "BTC-USDT-SWAP"}],
            tickers={"BTC-USDT-SWAP": {"last": 100}},
            candles={"BTC-USDT-SWAP:15m": [
                {"open": 99, "high": 102, "low": 98, "close": 100, "confirmed": True},
                {"open": 100, "high": 101, "low": 99, "close": 100, "confirmed": False},
            ]},
            account={"todayLossCount": None},
        )
        prompt = CodexRunner._decision_prompt(snapshot, AIConfig(enabled=True, mode="shadow", allowOpen=False))
        # The analytical report remains useful even when the existing
        # admission gates cannot permit a new order from this snapshot.
        self.assertIn('"allowOpen":false', prompt)
        self.assertIn('"todayLossCount":null', prompt)
        self.assertIn("PER-CONTRACT ANALYSIS FIRST, EXECUTION ELIGIBILITY SECOND", prompt)
        self.assertIn("must not erase a contract's technical plan or numerical estimates", prompt)
        self.assertIn("even when the scenario is not currently ready to trade", prompt)
        self.assertIn("historical/backtest validation is not required", prompt)
        self.assertIn("lower numerical winRate and confidence", prompt)
        self.assertIn("not reasons to return neutral or null", prompt)
        self.assertIn("contract's measured timeframe/price/trend evidence", prompt)
        self.assertIn("required structural signal/confirmation still pending remains entryEligible=false", prompt)
        self.assertIn("limitPrice not yet touched", prompt)
        self.assertIn("NOT an unmet signal/confirmation", prompt)
        self.assertIn("do not assume unavailable account/risk values", prompt)
        self.assertIn("do not claim the snapshot has expired", prompt)
        self.assertIn("normal forming candle", prompt)

    def test_prompt_market_facts_cover_observed_pool_without_auxiliary_leakage(self):
        snapshot = AISnapshot(
            snapshotId="snap-observed-facts", capturedAt=self.snapshot().capturedAt,
            instruments=[{"id": "BTC-USDT-SWAP"}, {"id": "ETH-USDT-SWAP"}],
            ai={"selectedInstruments": ["BTC-USDT-SWAP"]},
            tickers={"BTC-USDT-SWAP": {"last": "100"}, "ETH-USDT-SWAP": {"last": "200"}},
            candles={"BTC-USDT-SWAP/15m": [
                {"open": 99, "high": 102, "low": 98, "close": 100, "confirmed": True},
                {"open": 100, "high": 500, "low": 1, "close": 400, "confirmed": False},
            ]},
        )
        prompt = CodexRunner._decision_prompt(snapshot, AIConfig())
        facts = json.loads(prompt.split("SERVER MARKET FACTS:\n", 1)[1].split("\nSNAPSHOT:\n", 1)[0])
        self.assertEqual(set(facts["instruments"]), {"BTC-USDT-SWAP"})
        # The original complete snapshot remains intact for contextual
        # reasoning; auxiliary data does not gain observation authority.
        raw = json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1])
        self.assertEqual(restore_prompt_snapshot(raw), snapshot.to_dict())
        self.assertIn("deterministic summaries", prompt)
        self.assertIn("not trading signals or mandatory strategy rules", prompt)
        self.assertIn("do not recompute their arithmetic", prompt)
        self.assertIn("no exhaustive search over setups is required", prompt)

    def test_prompt_candle_compaction_is_lossless_for_complete_16_contract_pool(self):
        ids = [f"COIN{index}-USDT-SWAP" for index in range(16)]
        candles = {
            f"{instrument}/{interval}": [{
                "id": 1791217200 + row * 300, "timestamp": "2026-10-05T16:30:00Z",
                "open": 12.345678901234567 + row, "high": 13.345678901234567 + row,
                "low": 11.345678901234567 + row, "close": 12.945678901234567 + row,
                "volume": 1234.123456789, "quoteVolume": 12345678.987654321,
                "confirmed": row != 59, "extension": {"missing": None, "tags": ["a", 1, True]},
            } for row in range(60)]
            for instrument in ids for interval in ("5m", "15m", "1H", "4H")
        }
        snapshot = AISnapshot(
            snapshotId="snap-compact", capturedAt=self.snapshot().capturedAt,
            instruments=[{"id": instrument} for instrument in ids], candles=candles,
            tickers={item: {"last": "13.123456789"} for item in ids},
            ai={"selectedInstruments": ids}, account={"todayLossCount": None},
            risk={"unknown": True}, dataFreshness={"maxAgeSeconds": 90},
        )
        original = snapshot.to_dict()
        compact = _prompt_snapshot(snapshot)
        transmitted = json.loads(json.dumps(compact, ensure_ascii=False, allow_nan=False))
        restored = restore_prompt_snapshot(transmitted)
        self.assertEqual(restored, original)
        self.assertEqual(snapshot.to_dict(), original)
        self.assertEqual(len(transmitted["candles"]), 64)
        for key, series in transmitted["candles"].items():
            self.assertEqual(len(series["rows"]), 60)
            self.assertEqual(set(series["columns"]), set(original["candles"][key][0]))
            first = restored["candles"][key][0]
            self.assertIs(type(first["id"]), int)
            self.assertIs(type(first["confirmed"]), bool)
            self.assertIs(type(first["open"]), float)
            self.assertIs(type(first["timestamp"]), str)
            self.assertIs(type(first["extension"]["tags"][2]), bool)
            self.assertIsNone(first["extension"]["missing"])
        clock = datetime.now(timezone.utc)
        compact_prompt = CodexRunner._decision_prompt(snapshot, AIConfig(), now=clock)
        with patch("backend.ai_worker._prompt_snapshot", return_value=original):
            original_prompt = CodexRunner._decision_prompt(snapshot, AIConfig(), now=clock)
        self.assertLess(len(compact_prompt.encode()), len(original_prompt.encode()) * .8)
        self.assertIn("columns:[field names],rows:[[values]]", compact_prompt)
        self.assertIn("Use the confirmed column to identify closed candles", compact_prompt)

    def test_prompt_candle_compaction_preserves_heterogeneous_and_empty_series(self):
        rows = [{"id": 1, "confirmed": True}, {"id": 2, "confirmed": False, "extra": None}]
        snapshot = AISnapshot(snapshotId="snap-heterogeneous", capturedAt=self.snapshot().capturedAt, candles={
            "BTC-USDT-SWAP/5m": rows, "BTC-USDT-SWAP/15m": [],
            "BTC-USDT-SWAP/1H": [{}, {}],
            "BTC-USDT-SWAP/4H": [
                {"id": 1, "confirmed": False, "metadata": {"numericString": "0", "empty": None}},
                {"metadata": {"numericString": "1", "empty": None}, "confirmed": True, "id": 2},
            ],
        })
        original = snapshot.to_dict()
        compact = _prompt_snapshot(snapshot)
        self.assertIsInstance(compact["candles"]["BTC-USDT-SWAP/5m"], list)
        self.assertEqual(compact["candles"]["BTC-USDT-SWAP/15m"], [])
        self.assertEqual(compact["candles"]["BTC-USDT-SWAP/4H"]["columns"], ["id", "confirmed", "metadata"])
        self.assertEqual(restore_prompt_snapshot(compact), original)
        compact["candles"]["BTC-USDT-SWAP/5m"][1]["extra"] = "changed"
        self.assertEqual(snapshot.to_dict(), original)

    def test_experimental_prompt_encoding_keeps_facts_local_and_limits_rows(self):
        rows = [{
            "timestamp": f"2026-10-05T16:{row:02d}:00Z", "open": 100 + row,
            "high": 101 + row, "low": 99 + row, "close": 100.5 + row,
            "volume": 10, "confirmed": row < 59,
        } for row in range(60)]
        snapshot = AISnapshot(
            snapshotId="snap-window", capturedAt=self.snapshot().capturedAt,
            instruments=[{"id": "BTC-USDT-SWAP"}],
            candles={"BTC-USDT-SWAP/5m": rows},
            ai={"selectedInstruments": ["BTC-USDT-SWAP"]},
        )
        with patch.dict(os.environ, {"NOVATRADE_DEEPSEEK_ENCODING": "compact10"}, clear=False):
            compact = _prompt_snapshot(snapshot)
        self.assertEqual(len(compact["candles"]["BTC-USDT-SWAP/5m"]["rows"]), 11)
        self.assertEqual(compact["candles"]["BTC-USDT-SWAP/5m"]["rows"][-1][-1], False)
        with patch.dict(os.environ, {"NOVATRADE_DEEPSEEK_ENCODING": "raw"}, clear=False):
            raw = _prompt_snapshot(snapshot)
        self.assertIsInstance(raw["candles"]["BTC-USDT-SWAP/5m"], list)

    def test_deepseek_encoding_does_not_change_codex_prompt(self):
        snapshot = self.grouped_snapshot(count=1)
        codex = AIConfig(provider="codex")
        deepseek = AIConfig(provider="deepseek-harness")
        with patch.dict(os.environ, {"NOVATRADE_DEEPSEEK_ENCODING": "compact10"}, clear=False):
            codex_prompt = CodexRunner._decision_prompt(snapshot, codex)
            deepseek_prompt = CodexRunner._decision_prompt(snapshot, deepseek)
        codex_snapshot = json.loads(codex_prompt.split("SNAPSHOT:\n", 1)[1])
        deepseek_snapshot = json.loads(deepseek_prompt.split("SNAPSHOT:\n", 1)[1])
        self.assertEqual(len(codex_snapshot["candles"]["COIN0-USDT-SWAP/5m"]["rows"]), 60)
        self.assertEqual(len(deepseek_snapshot["candles"]["COIN0-USDT-SWAP/5m"]["rows"]), 11)

    def test_production_runner_rejects_missing_duplicate_extra_and_omitted_rows(self):
        valid = self.decision(action="hold", instrumentID=None, assessments=[self.assessment()])
        variants = [
            {key: value for key, value in valid.items() if key != "assessments"},
            dict(valid, assessments=[]),
            dict(valid, assessments=[self.assessment(), self.assessment()]),
            dict(valid, assessments=[self.assessment("ETH-USDT-SWAP")]),
        ]

        async def run():
            runner = CodexRunner("stub-codex")
            for row in variants:
                process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(row).encode(), b"")))
                with self.subTest(row=row), patch("backend.ai_worker.asyncio.create_subprocess_exec", AsyncMock(return_value=process)), self.assertRaises(CodexError):
                    await runner.run(self.snapshot(), AIConfig(enabled=True, mode="shadow"))
            process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(json.dumps(valid).encode(), b"")))
            with patch("backend.ai_worker.asyncio.create_subprocess_exec", AsyncMock(return_value=process)):
                parsed = await runner.run(self.snapshot(), AIConfig(enabled=True, mode="shadow"))
                self.assertEqual(len(parsed.assessments), 1)

        asyncio.run(run())

    def test_schema_rejects_unknown_fields(self):
        with self.assertRaises(SchemaError):
            AIDecision.from_dict(self.decision(unknown=True))

    def test_schema_requires_win_rate_and_risk_reward_ratio(self):
        for field in ("winRate", "riskRewardRatio"):
            value = self.decision()
            del value[field]
            with self.subTest(field=field), self.assertRaises(SchemaError):
                AIDecision.from_dict(value)

    def test_chat_apply_requires_a_reviewed_suggestion(self):
        from backend.ai_schema import AIChatRequest

        with self.assertRaises(SchemaError):
            AIChatRequest.from_dict({"message": "提高置信度", "apply": True})

    def test_chat_null_patch_fields_mean_unchanged(self):
        self.assertEqual(
            normalize_ai_chat_patch({"minimumConfidence": 0.8, "allowOpen": None}),
            {"minimumConfidence": 0.8},
        )

    def test_universe_config_defaults_and_boundaries(self):
        defaults = AIConfig.from_dict({})
        self.assertEqual(defaults.allowedInstruments, ())
        self.assertNotIn("candidateLimit", defaults.to_dict())
        self.assertNotIn("selectionLimit", defaults.to_dict())
        self.assertEqual(defaults.routineModel, "gpt-6-luna")
        self.assertEqual(defaults.routineReasoningEffort, "medium")
        self.assertEqual(defaults.escalationModel, "gpt-6-luna")
        self.assertEqual(defaults.escalationReasoningEffort, "medium")
        self.assertEqual(defaults.maxDailyOrders, 20)
        self.assertEqual(defaults.maxDailyLosses, 5)
        self.assertEqual(defaults.marginPerOrderUSD, 500)
        self.assertEqual(defaults.maxLeverage, 5)
        self.assertEqual(defaults.cooldownSeconds, 43_200)
        self.assertEqual(defaults.cliTimeoutSeconds, 90.0)
        self.assertEqual(AIConfig.from_dict({"cliTimeoutSeconds": 45}).cliTimeoutSeconds, 45.0)
        self.assertEqual(AIConfig.from_dict({"cooldownSeconds": 60}).cooldownSeconds, 43_200)
        with self.assertRaises(SchemaError):
            AIConfig.from_dict({"allowedInstruments": ["BTC"]})
        with self.assertRaises(SchemaError):
            AIConfig.from_dict({"routineModel": "gpt-6-astra"})

    def test_worker_migrates_legacy_cli_timeout_default(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                raw = AIConfig(enabled=True, mode="shadow").to_dict()
                raw["cliTimeoutSeconds"] = 45
                (state_dir / "ai-config.json").write_text(json.dumps(raw), encoding="utf-8")
                worker = AIWorker(state_dir=state_dir)
                self.assertEqual(worker.config.cliTimeoutSeconds, 90.0)
                persisted = json.loads((state_dir / "ai-config.json").read_text(encoding="utf-8"))
                self.assertEqual(persisted["cliTimeoutSeconds"], 90.0)

        asyncio.run(run())

    def test_legacy_dynamic_config_migrates_to_fixed_allowlist(self):
        migrated = AIConfig.from_dict({
            "universeMode": "okx-hot-gainers",
            "candidateLimit": 10,
            "selectionLimit": 5,
            "allowedInstruments": ["btc-usdt-swap"],
        })
        self.assertEqual(migrated.allowedInstruments, ("BTC-USDT-SWAP",))
        self.assertNotIn("universeMode", migrated.to_dict())

    def test_legacy_model_routes_migrate_without_losing_saved_settings(self):
        legacy = AIConfig(
            enabled=True, mode="demo-active", allowedInstruments=("ETH-USDT-SWAP", "SOL-USDT-SWAP"),
            minimumConfidence=0.79, decisionIntervalSeconds=90, maxDailyOrders=12,
            marginPerOrderUSD=250, maxLeverage=3,
        ).to_dict()
        legacy.update({"routineReasoningEffort": "low", "escalationModel": "gpt-6.1-sol"})
        expected = dict(legacy, routineReasoningEffort="medium", escalationModel="gpt-6-luna")
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                config_path = state_dir / "ai-config.json"
                config_path.write_text(json.dumps(legacy), encoding="utf-8")
                worker = AIWorker(state_dir=state_dir)
                self.assertEqual(worker.get_config(), expected)
                self.assertEqual(json.loads(config_path.read_text()), expected)
                self.assertEqual(AIWorker(state_dir=state_dir).get_config(), expected)

        asyncio.run(run())

    def test_explicit_add_delete_replace_observation_commands_are_local(self):
        class UnexpectedRunner:
            async def run_chat(self, message, config):
                raise AssertionError("explicit observation command should not invoke Codex")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    config=AIConfig(allowedInstruments=("BTC-USDT-SWAP",)),
                    runner=UnexpectedRunner(),
                    state_dir=Path(directory),
                )
                added = await worker.chat("增加 ETH-USDT-SWAP")
                await worker.chat("增加 ETH-USDT-SWAP", apply=True, suggestion=added["suggestion"])
                removed = await worker.chat("删除 BTC-USDT-SWAP")
                await worker.chat("删除 BTC-USDT-SWAP", apply=True, suggestion=removed["suggestion"])
                replaced = await worker.chat("覆盖为 SOL-USDT-SWAP")
                return added, removed, replaced

        responses = asyncio.run(run())
        self.assertEqual(responses[0]["suggestion"], {"allowedInstruments": ["BTC-USDT-SWAP", "ETH-USDT-SWAP"]})
        self.assertEqual(responses[1]["suggestion"], {"allowedInstruments": ["ETH-USDT-SWAP"]})
        self.assertEqual(responses[2]["suggestion"], {"allowedInstruments": ["SOL-USDT-SWAP"]})

    def test_broad_observation_request_is_rejected(self):
        class UnexpectedRunner:
            async def run_chat(self, message, config):
                raise AssertionError("broad observation command should not invoke Codex")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(runner=UnexpectedRunner(), state_dir=Path(directory))
                return await worker.chat("从热门榜和涨幅榜自动挑选观察币种")

        response = asyncio.run(run())
        self.assertIsNone(response["suggestion"])
        self.assertIn("完整合约 ID", response["reply"])

    def test_observation_commands_handle_bare_and_combined_forms(self):
        class UnexpectedRunner:
            async def run_chat(self, message, config):
                raise AssertionError("explicit observation command should not invoke Codex")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    config=AIConfig(allowedInstruments=("BTC-USDT-SWAP",)),
                    runner=UnexpectedRunner(),
                    state_dir=Path(directory),
                )
                bare = await worker.chat("观察 SOL-USDT-SWAP")
                combined = await worker.chat("删除 BTC-USDT-SWAP 并增加 ETH-USDT-SWAP")
                return bare, combined

        bare, combined = asyncio.run(run())
        self.assertEqual(bare["suggestion"], {"allowedInstruments": ["SOL-USDT-SWAP"]})
        self.assertEqual(combined["suggestion"], {"allowedInstruments": ["ETH-USDT-SWAP"]})

    def test_market_question_with_contract_id_does_not_change_observation_set(self):
        class MarketRunner:
            async def run_chat(self, message, config):
                return AIChatResponse(
                    schemaVersion=1,
                    reply="行情问题",
                    suggestion={"allowedInstruments": ["ETH-USDT-SWAP"]},
                )

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    config=AIConfig(allowedInstruments=("BTC-USDT-SWAP",)),
                    runner=MarketRunner(),
                    state_dir=Path(directory),
                )
                return await worker.chat("BTC-USDT-SWAP 这个合约当前趋势怎么样？")

        response = asyncio.run(run())
        self.assertIsNone(response["suggestion"])

    def test_model_question_is_local_and_includes_runtime_identity(self):
        class IdentityRunner:
            def runtime_identity(self):
                return {"model": "gpt-test", "provider": "local-test"}

            async def run_chat(self, message, config):
                raise AssertionError("model metadata should not invoke Codex")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    runner=IdentityRunner(),
                    state_dir=Path(directory),
                )
                return await worker.chat("使用的 AI 模型是什么？")

        response = asyncio.run(run())
        self.assertIn("gpt-test", response["reply"])
        self.assertIn("local-test", response["reply"])
        self.assertIn("固定使用 gpt-6-luna（medium）", response["reply"])
        self.assertNotIn("升级", response["reply"])
        self.assertIsNone(response["suggestion"])

    def test_codex_schema_is_strict_provider_compatible(self):
        schema = decision_json_schema()
        self.assertEqual(schema["properties"]["schemaVersion"]["type"], "integer")
        self.assertEqual(set(schema["required"]), set(schema["properties"]))
        self.assertFalse(schema["additionalProperties"])

    def test_model_override_is_explicit_and_does_not_fallback(self):
        command = CodexRunner._command(
            Path("/tmp/ai-decision.schema.json"), "/tmp/novatrade", model="gpt-6-luna", reasoning_effort="medium"
        )
        self.assertIn("-m", command)
        self.assertEqual(command[command.index("-m") + 1], "gpt-6-luna")
        self.assertIn("model_reasoning_effort=medium", command)
        self.assertIn("mcp_servers={}", command)
        self.assertIn("plugins={}", command)
        for model, effort in (("gpt-6-astra", "ultra"), ("gpt-6.1-sol", "medium"), ("gpt-6-luna", "low")):
            with self.subTest(model=model, effort=effort), self.assertRaises(CodexError) as raised:
                CodexRunner._command(
                    Path("/tmp/ai-decision.schema.json"), "/tmp/novatrade", model=model, reasoning_effort=effort
                )
            self.assertIn("automatic model route rejected", str(raised.exception))

    def test_ai_worker_uses_routine_route_for_ordinary_snapshot(self):
        class CapturingRunner:
            def __init__(self):
                self.configs = []

            async def run(self, snapshot, config):
                self.configs.append(config)
                return AIDecision.hold(snapshot.snapshotId, "routine")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runner = CapturingRunner()
                worker = AIWorker(config=AIConfig(enabled=True, mode="shadow"), runner=runner, state_dir=Path(directory))
                await worker.run_once(self.snapshot())
                return runner.configs[0]

        config = asyncio.run(run())
        self.assertEqual(config.routineModel, "gpt-6-luna")
        self.assertEqual(config.routineReasoningEffort, "medium")

    def test_ai_worker_keeps_fixed_model_for_candidate_signals_and_conflicts(self):
        class CapturingRunner:
            def __init__(self):
                self.configs = []

            async def run(self, snapshot, config):
                self.configs.append(config)
                return AIDecision.hold(snapshot.snapshotId, "fixed model")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runner = CapturingRunner()
                worker = AIWorker(config=AIConfig(enabled=True, mode="shadow"), runner=runner, state_dir=Path(directory))
                for marker in ("multiTimeframeConflict", "candidateSignal"):
                    snapshot = AISnapshot(
                        snapshotId=f"snap-{marker}", capturedAt=iso(datetime.now(timezone.utc)),
                        instruments=[{"id": "BTC-USDT-SWAP"}], ai={marker: True},
                    )
                    await worker.run_once(snapshot)
                return runner.configs

        configs = asyncio.run(run())
        self.assertEqual(len(configs), 2)
        for config in configs:
            self.assertEqual(config.routineModel, "gpt-6-luna")
            self.assertEqual(config.routineReasoningEffort, "medium")

    def test_policy_rejects_expired_and_accepts_hold(self):
        snapshot = self.snapshot()
        expired = validate_decision(self.decision(validUntil=iso(datetime.now(timezone.utc) - timedelta(seconds=1))), snapshot, AIConfig(enabled=True, mode="shadow"), now=datetime.now(timezone.utc))
        self.assertFalse(expired.accepted)
        hold = validate_decision(AIDecision.hold(snapshot.snapshotId, "no setup"), snapshot, AIConfig(enabled=True, mode="shadow"))
        self.assertTrue(hold.accepted)

    def test_server_freshness_uses_utc_across_the_local_date_boundary(self):
        snapshot = AISnapshot(
            snapshotId="snap-1", capturedAt="2026-10-05T16:28:30Z",
            dataFreshness={"capturedAt": "2026-10-06T00:28:30+08:00", "maxAgeSeconds": 90},
        )
        utc_evaluation = datetime(2026, 10, 5, 16, 28, 49, tzinfo=timezone.utc)
        local_evaluation = datetime(2026, 10, 6, 0, 28, 49, tzinfo=timezone(timedelta(hours=8)))
        for evaluated_at in (utc_evaluation, local_evaluation):
            with self.subTest(evaluated_at=evaluated_at):
                freshness = snapshot_freshness(snapshot, AIConfig(), now=evaluated_at)
                self.assertTrue(freshness["valid"])
                self.assertEqual(freshness["ageSeconds"], 19)
                self.assertEqual(freshness["maxAgeSeconds"], 90)
                self.assertFalse(freshness["isStale"])
                self.assertEqual(freshness["evaluatedAt"], "2026-10-05T16:28:49Z")

    def test_freshness_limit_is_checked_again_at_entry_admission(self):
        captured = datetime(2026, 10, 5, 16, 28, 30, tzinfo=timezone.utc)
        snapshot = AISnapshot(
            snapshotId="snap-1", capturedAt=iso(captured), instruments=[{"id": "BTC-USDT-SWAP"}],
            account={"todayLossCount": 0}, dataFreshness={"maxAgeSeconds": 90},
        )
        config = AIConfig(enabled=True, mode="shadow")
        for age, accepted in ((19, True), (90, True), (90.001, False), (91, False)):
            with self.subTest(age=age):
                evaluated_at = captured + timedelta(seconds=age)
                result = validate_decision(
                    self.decision(validUntil=iso(evaluated_at + timedelta(minutes=1))),
                    snapshot, config, now=evaluated_at,
                )
                self.assertEqual(result.accepted, accepted)
                if not accepted:
                    self.assertEqual(result.decision.action, "hold")
                    self.assertIn("snapshot has expired", result.reason)

    def test_freshness_legacy_metadata_uses_polling_interval(self):
        captured = datetime(2026, 10, 5, 16, 28, 30, tzinfo=timezone.utc)
        snapshot = AISnapshot(snapshotId="snap-1", capturedAt=iso(captured))
        config = AIConfig(decisionIntervalSeconds=90)
        for age, is_stale in ((19, False), (91, True)):
            with self.subTest(age=age):
                freshness = snapshot_freshness(snapshot, config, now=captured + timedelta(seconds=age))
                self.assertTrue(freshness["valid"])
                self.assertEqual(freshness["maxAgeSeconds"], 90)
                self.assertEqual(freshness["isStale"], is_stale)

    def test_open_rejects_invalid_or_inconsistent_freshness_metadata(self):
        evaluated_at = datetime(2026, 10, 5, 16, 28, 49, tzinfo=timezone.utc)
        metadata_cases = [
            {"maxAgeSeconds": value} for value in (None, True, False, 0, -1, "90", float("inf"), float("nan"))
        ] + [
            {"maxAgeSeconds": 90, "capturedAt": value}
            for value in ("2026-10-05T16:28:00Z", "2026-10-05T16:28:30", "invalid", None, 90)
        ]
        for metadata in metadata_cases:
            with self.subTest(metadata=metadata):
                snapshot = AISnapshot(
                    snapshotId="snap-1", capturedAt="2026-10-05T16:28:30Z",
                    instruments=[{"id": "BTC-USDT-SWAP"}], account={"todayLossCount": 0},
                    dataFreshness=metadata,
                )
                freshness = snapshot_freshness(snapshot, AIConfig(), now=evaluated_at)
                self.assertFalse(freshness["valid"])
                self.assertTrue(freshness["error"])
                result = validate_decision(
                    self.decision(validUntil="2026-10-05T16:30:00Z"), snapshot,
                    AIConfig(enabled=True, mode="shadow"), now=evaluated_at,
                )
                self.assertFalse(result.accepted)
                self.assertEqual(result.decision.action, "hold")

    def test_open_rejects_a_future_snapshot(self):
        evaluated_at = datetime(2026, 10, 5, 16, 28, 49, tzinfo=timezone.utc)
        snapshot = AISnapshot(
            snapshotId="snap-1", capturedAt="2026-10-05T16:29:00Z",
            instruments=[{"id": "BTC-USDT-SWAP"}], account={"todayLossCount": 0},
            dataFreshness={"maxAgeSeconds": 90},
        )
        freshness = snapshot_freshness(snapshot, AIConfig(), now=evaluated_at)
        self.assertFalse(freshness["valid"])
        self.assertEqual(freshness["ageSeconds"], -11)
        self.assertIn("future", freshness["error"])
        result = validate_decision(
            self.decision(validUntil="2026-10-05T16:30:00Z"), snapshot,
            AIConfig(enabled=True, mode="shadow"), now=evaluated_at,
        )
        self.assertFalse(result.accepted)
        self.assertIn("future", result.reason)

    def test_entry_freshness_gate_leaves_hold_close_and_cancel_available(self):
        evaluated_at = datetime(2026, 10, 5, 16, 30, 1, tzinfo=timezone.utc)
        for metadata in ({"maxAgeSeconds": 90}, {"maxAgeSeconds": "invalid"}):
            snapshot = AISnapshot(
                snapshotId="snap-1", capturedAt="2026-10-05T16:28:30Z",
                instruments=[{"id": "BTC-USDT-SWAP"}], dataFreshness=metadata,
            )
            for action in ("hold", "close", "cancel"):
                with self.subTest(action=action, metadata=metadata):
                    result = validate_decision(
                        self.decision(
                            action=action, instrumentID=None if action == "hold" else "BTC-USDT-SWAP",
                            confidence=.99 if action == "hold" else 0,
                            winRate=None, riskRewardRatio=None, orderID="ord-1",
                            validUntil=iso(evaluated_at + timedelta(minutes=1)),
                        ), snapshot, AIConfig(enabled=True, mode="shadow"), now=evaluated_at,
                    )
                    self.assertTrue(result.accepted)
                    self.assertEqual(result.decision.action, action)

    def test_codex_prompt_receives_measured_freshness_and_closed_candle_semantics(self):
        snapshot = AISnapshot(
            snapshotId="snap-1", capturedAt="2026-10-05T16:28:30Z",
            candles={"BTC-USDT-SWAP/5m": [{"confirmed": True}, {"confirmed": False}]},
            dataFreshness={"maxAgeSeconds": 90},
        )
        prompt = CodexRunner._decision_prompt(
            snapshot, AIConfig(), now=datetime(2026, 10, 6, 0, 28, 49, tzinfo=timezone(timedelta(hours=8))),
        )
        freshness = json.loads(prompt.split("SERVER FRESHNESS:\n", 1)[1].split("\n", 1)[0])
        self.assertEqual(freshness["evaluatedAt"], "2026-10-05T16:28:49Z")
        self.assertEqual(freshness["ageSeconds"], 19)
        self.assertFalse(freshness["isStale"])
        self.assertIn("Do not guess the current date/time", prompt)
        self.assertIn("does not invalidate confirmed history or the entire snapshot", prompt)
        transmitted_snapshot = json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1])
        self.assertEqual(restore_prompt_snapshot(transmitted_snapshot)["candles"]["BTC-USDT-SWAP/5m"], snapshot.candles["BTC-USDT-SWAP/5m"])

    def test_named_hold_rejects_unknown_contract_and_allowlist_mismatch(self):
        snapshot = self.snapshot()
        for instrument, allowlist in (
            ("INVALID-CONTRACT", ("BTC-USDT-SWAP",)),
            ("ETH-USDT-SWAP", ("BTC-USDT-SWAP", "ETH-USDT-SWAP")),
            ("BTC-USDT-SWAP", ("ETH-USDT-SWAP",)),
            ("BTC-USDT-SWAP", ()),
        ):
            with self.subTest(instrument=instrument, allowlist=allowlist):
                result = validate_decision(
                    self.decision(action="hold", instrumentID=instrument, confidence=.99),
                    snapshot, AIConfig(enabled=True, mode="shadow", allowedInstruments=allowlist),
                )
                self.assertFalse(result.accepted)
                self.assertEqual(result.decision.action, "hold")
                self.assertIsNone(result.decision.instrumentID)

    def test_named_hold_uses_actual_selection_and_excludes_auxiliary_contracts(self):
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP", "ETH-USDT-SWAP"))
        cases = (
            ([{"id": "BTC-USDT-SWAP"}, {"id": "ETH-USDT-SWAP"}], {"selectedInstruments": ["ETH-USDT-SWAP"]}),
            ([{"id": "ETH-USDT-SWAP"}, {"id": "BTC-USDT-SWAP"}], {"selectionCount": 1}),
        )
        for instruments, ai in cases:
            snapshot = AISnapshot(
                snapshotId="snap-1", capturedAt=iso(datetime.now(timezone.utc)), instruments=instruments, ai=ai,
            )
            with self.subTest(ai=ai):
                self.assertEqual(snapshot.observed_instruments(), ["ETH-USDT-SWAP"])
                self.assertEqual(AIWorker._snapshot_observed_instruments(snapshot), snapshot.observed_instruments())
                eth = validate_decision(self.decision(action="hold", instrumentID="ETH-USDT-SWAP"), snapshot, config)
                btc = validate_decision(self.decision(action="hold", instrumentID="BTC-USDT-SWAP"), snapshot, config)
                self.assertTrue(eth.accepted)
                self.assertEqual(eth.decision.instrumentID, "ETH-USDT-SWAP")
                self.assertFalse(btc.accepted)
                self.assertIsNone(btc.decision.instrumentID)

    def test_empty_observation_set_allows_only_whole_pool_hold(self):
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP",))
        for instruments, ai in (([], {}), ([{"id": "BTC-USDT-SWAP"}], {"selectedInstruments": []})):
            snapshot = AISnapshot(
                snapshotId="snap-1", capturedAt=iso(datetime.now(timezone.utc)), instruments=instruments, ai=ai,
            )
            with self.subTest(ai=ai):
                named = validate_decision(self.decision(action="hold"), snapshot, config)
                global_hold = validate_decision(self.decision(action="hold", instrumentID=None), snapshot, config)
                self.assertFalse(named.accepted)
                self.assertTrue(global_hold.accepted)
                self.assertIsNone(global_hold.decision.instrumentID)

    def test_expired_hold_preserves_valid_specific_and_whole_pool_scope(self):
        snapshot = self.snapshot()
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=("BTC-USDT-SWAP",))
        for instrument in (None, "BTC-USDT-SWAP"):
            with self.subTest(instrument=instrument):
                result = validate_decision(
                    self.decision(
                        action="hold", instrumentID=instrument, confidence=.99,
                        validUntil=iso(datetime.now(timezone.utc) - timedelta(minutes=1)),
                    ), snapshot, config,
                )
                self.assertTrue(result.accepted)
                self.assertEqual(result.decision.instrumentID, instrument)
                self.assertEqual(result.decision.confidence, .99)

    def test_policy_requires_open_win_rate_and_risk_reward_ratio(self):
        snapshot = self.snapshot()
        config = AIConfig(enabled=True, mode="shadow")
        low_win_rate = validate_decision(self.decision(winRate=.449), snapshot, config)
        self.assertFalse(low_win_rate.accepted)
        self.assertIn("winRate", low_win_rate.reason)
        low_risk_reward = validate_decision(self.decision(riskRewardRatio=1.99), snapshot, config)
        self.assertFalse(low_risk_reward.accepted)
        self.assertIn("riskRewardRatio", low_risk_reward.reason)
        boundary = validate_decision(self.decision(winRate=.45, riskRewardRatio=2.0), snapshot, config)
        self.assertTrue(boundary.accepted)

    def test_policy_caps_ai_leverage_and_daily_losses(self):
        snapshot = self.snapshot()
        config = AIConfig(enabled=True, mode="shadow", maxLeverage=5, maxDailyLosses=5)
        self.assertTrue(validate_decision(self.decision(leverage=5), snapshot, config).accepted)
        self.assertFalse(validate_decision(self.decision(leverage=5.01), snapshot, config).accepted)
        loss_snapshot = AISnapshot(
            snapshotId=snapshot.snapshotId, capturedAt=snapshot.capturedAt,
            instruments=snapshot.instruments, account={"todayLossCount": 5},
        )
        blocked = validate_decision(self.decision(), loss_snapshot, config)
        self.assertFalse(blocked.accepted)
        self.assertIn("daily losing", blocked.reason)

    def test_policy_rejects_open_when_daily_loss_count_is_unavailable(self):
        snapshot = self.snapshot()
        snapshot = AISnapshot(snapshotId=snapshot.snapshotId, capturedAt=snapshot.capturedAt, instruments=snapshot.instruments, account={"availableEquityUSD": 1000}, risk={})
        result = validate_decision(self.decision(), snapshot, AIConfig(enabled=True, mode="shadow"))
        self.assertFalse(result.accepted)
        self.assertIn("unavailable", result.reason)

    def test_policy_does_not_block_close_or_cancel_on_signal_quality(self):
        snapshot = self.snapshot()
        config = AIConfig(enabled=True, mode="shadow")
        close = validate_decision(
            self.decision(action="close", winRate=None, riskRewardRatio=None, confidence=0), snapshot, config
        )
        self.assertTrue(close.accepted)
        cancel = validate_decision(
            self.decision(action="cancel", direction=None, orderType=None, winRate=None, riskRewardRatio=None, confidence=0, orderID="ord-1"),
            snapshot,
            config,
        )
        self.assertTrue(cancel.accepted)

    def test_policy_does_not_apply_entry_cooldown_to_exits(self):
        snapshot = self.snapshot()
        config = AIConfig(enabled=True, mode="shadow", cooldownSeconds=60)
        state = PolicyState()
        open_result = validate_decision(self.decision(), snapshot, config, state)
        self.assertTrue(open_result.accepted)
        state.remember(open_result.decision)
        close = validate_decision(
            self.decision(action="close", decisionId="decision-close", winRate=None, riskRewardRatio=None, confidence=0),
            snapshot,
            config,
            state,
        )
        self.assertTrue(close.accepted)

    def test_status_exposes_current_observation_set_and_restores_it(self):
        class HoldRunner:
            async def run(self, snapshot, config):
                return AIDecision.hold(snapshot.snapshotId, "no setup")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                snapshot = AISnapshot(
                    snapshotId="snap-observed",
                    capturedAt=iso(datetime.now(timezone.utc)),
                    instruments=[{"id": "BTC-USDT-SWAP"}, {"id": "ETH-USDT-SWAP"}],
                    ai={"selectionCount": 1, "selectedInstruments": ["ETH-USDT-SWAP"]},
                )
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="shadow"),
                    runner=HoldRunner(),
                    state_dir=state_dir,
                )
                await worker.run_once(snapshot)
                status = worker.get_status()
                audit = [json.loads(line) for line in (state_dir / "ai-decisions.jsonl").read_text().splitlines()]
                restored = AIWorker(state_dir=state_dir)
                return status, restored.get_status(), audit

        status, restored, audit = asyncio.run(run())
        self.assertEqual(status["observedInstruments"], ["ETH-USDT-SWAP"])
        self.assertEqual(status["lastDecisionInstruments"], ["ETH-USDT-SWAP"])
        decision_event = next(event for event in audit if event["type"] == "decision")
        self.assertEqual(decision_event["instruments"], ["ETH-USDT-SWAP"])
        self.assertEqual(decision_event["decision"], status["lastDecision"])
        self.assertEqual(decision_event["freshness"], status["lastDecisionFreshness"])
        self.assertTrue(status["lastDecisionFreshness"]["valid"])
        self.assertFalse(status["lastDecisionFreshness"]["isStale"])
        self.assertEqual(restored["observedInstruments"], ["ETH-USDT-SWAP"])
        self.assertEqual(restored["observationUpdatedAt"], status["observationUpdatedAt"])
        self.assertIsNone(restored["lastDecision"])
        self.assertEqual(restored["lastDecisionInstruments"], [])
        self.assertEqual(restored["lastDecisionFreshness"], {})

    def test_last_decision_scope_survives_pool_change_failure_and_lifecycle(self):
        class HoldThenFailRunner:
            async def run(self, snapshot, config):
                if snapshot.snapshotId == "snap-failure":
                    raise CodexError("Codex CLI timed out")
                return AIDecision.hold(snapshot.snapshotId, "等待完整行情")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="demo-active", allowedInstruments=("BTC-USDT-SWAP",)),
                    runner=HoldThenFailRunner(), state_dir=Path(directory),
                )
                await worker.run_once(self.snapshot())
                previous = worker.get_status()
                worker.update_config({"allowedInstruments": ["ETH-USDT-SWAP"]})
                changed = worker.get_status()
                failed = await worker.run_once(AISnapshot(
                    snapshotId="snap-failure", capturedAt=iso(datetime.now(timezone.utc)),
                    instruments=[{"id": "ETH-USDT-SWAP"}],
                ))
                after_failure = worker.get_status()
                await worker.start()
                started = worker.get_status()
                await worker.stop()
                stopped = worker.get_status()
                await worker.disable()
                disabled = worker.get_status()
                persisted = json.loads((Path(directory) / "ai-state.json").read_text())
                return previous, changed, failed, after_failure, started, stopped, disabled, persisted

        previous, changed, failed, after_failure, started, stopped, disabled, persisted = asyncio.run(run())
        self.assertEqual(previous["lastDecisionInstruments"], ["BTC-USDT-SWAP"])
        self.assertEqual(changed["observedInstruments"], [])
        self.assertFalse(failed.accepted)
        self.assertEqual(after_failure["observedInstruments"], ["ETH-USDT-SWAP"])
        for status in (changed, after_failure, started, stopped, disabled, persisted):
            self.assertEqual(status["lastDecision"], previous["lastDecision"])
            self.assertEqual(status["lastDecisionAt"], previous["lastDecisionAt"])
            self.assertEqual(status["lastDecisionInstruments"], ["BTC-USDT-SWAP"])
            self.assertEqual(status["lastDecisionFreshness"], previous["lastDecisionFreshness"])

    def test_worker_records_true_freshness_without_rewriting_a_model_hold(self):
        class IncorrectHoldRunner:
            async def run(self, snapshot, config):
                return AIDecision.from_dict(self_decision)

        self_decision = self.decision(
            action="hold", instrumentID=None, confidence=.99,
            reasonCode="STALE_SNAPSHOT", reason="快照已过期。",
        )

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                calls = []

                async def gateway(decision, snapshot):
                    calls.append(decision)

                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="demo-active"),
                    runner=IncorrectHoldRunner(), order_gateway=gateway, state_dir=Path(directory),
                )
                snapshot = AISnapshot(
                    snapshotId="snap-1", capturedAt=iso(datetime.now(timezone.utc) - timedelta(seconds=19)),
                    instruments=[{"id": "BTC-USDT-SWAP"}], dataFreshness={"maxAgeSeconds": 90},
                )
                result = await worker.run_once(snapshot)
                audit = [json.loads(line) for line in (Path(directory) / "ai-decisions.jsonl").read_text().splitlines()]
                return result, worker.get_status(), audit[-1], calls

        result, status, audit, calls = asyncio.run(run())
        self.assertTrue(result.accepted)
        self.assertEqual(result.decision.reason, "快照已过期。")
        self.assertEqual(result.decision.confidence, .99)
        freshness = status["lastDecisionFreshness"]
        self.assertTrue(freshness["valid"])
        self.assertFalse(freshness["isStale"])
        self.assertGreaterEqual(freshness["ageSeconds"], 19)
        self.assertLess(freshness["ageSeconds"], 21)
        self.assertEqual(audit["freshness"], freshness)
        self.assertEqual(calls, [])

    def test_worker_never_submits_open_from_an_expired_snapshot(self):
        decision = AIDecision.from_dict(self.decision())

        class OpenRunner:
            async def run(self, snapshot, config):
                return decision

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                calls = []

                async def gateway(decision, snapshot):
                    calls.append(decision)

                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="demo-active"),
                    runner=OpenRunner(), order_gateway=gateway, state_dir=Path(directory),
                )
                snapshot = AISnapshot(
                    snapshotId="snap-1", capturedAt=iso(datetime.now(timezone.utc) - timedelta(seconds=91)),
                    instruments=[{"id": "BTC-USDT-SWAP"}], account={"todayLossCount": 0},
                    dataFreshness={"maxAgeSeconds": 90},
                )
                result = await worker.run_once(snapshot)
                return result, worker.get_status(), calls

        result, status, calls = asyncio.run(run())
        self.assertFalse(result.accepted)
        self.assertEqual(result.decision.action, "hold")
        self.assertTrue(status["lastDecisionFreshness"]["isStale"])
        self.assertEqual(calls, [])

    def test_high_confidence_hold_keeps_scope_and_never_calls_order_gateway(self):
        class HoldRunner:
            def __init__(self, instrument):
                self.instrument = instrument

            async def run(self, snapshot, config):
                return AIDecision(
                    schemaVersion=1, decisionId="high-confidence-hold", snapshotId=snapshot.snapshotId,
                    action="hold", instrumentID=self.instrument, confidence=.99,
                    validUntil=iso(datetime.now(timezone.utc) + timedelta(minutes=1)),
                    reasonCode="INCOMPLETE_ORDERBOOK", reason="订单簿数据缺失，继续观望。",
                )

        async def run(instrument, mode):
            with tempfile.TemporaryDirectory() as directory:
                calls = []

                async def gateway(decision, snapshot):
                    calls.append(decision)

                worker = AIWorker(
                    config=AIConfig(enabled=True, mode=mode, allowedInstruments=("BTC-USDT-SWAP", "ETH-USDT-SWAP")),
                    runner=HoldRunner(instrument), order_gateway=gateway, state_dir=Path(directory),
                )
                result = await worker.run_once(AISnapshot(
                    snapshotId="snap-hold", capturedAt=iso(datetime.now(timezone.utc)),
                    instruments=[{"id": "BTC-USDT-SWAP"}, {"id": "ETH-USDT-SWAP"}],
                ))
                return result, worker.get_status(), calls

        for instrument in (None, "ETH-USDT-SWAP"):
            for mode in ("demo-active", "live-armed"):
                with self.subTest(instrument=instrument, mode=mode):
                    result, status, calls = asyncio.run(run(instrument, mode))
                    self.assertTrue(result.accepted)
                    self.assertEqual(result.decision.action, "hold")
                    self.assertEqual(result.decision.instrumentID, instrument)
                    self.assertEqual(result.decision.confidence, .99)
                    self.assertIsNone(result.decision.winRate)
                    self.assertEqual(status["lastDecisionInstruments"], ["BTC-USDT-SWAP", "ETH-USDT-SWAP"])
                    self.assertEqual(calls, [])

    def test_observation_fallback_uses_selection_count(self):
        snapshot = AISnapshot(
            snapshotId="snap-fallback",
            capturedAt=iso(datetime.now(timezone.utc)),
            instruments=[{"id": "BTC-USDT-SWAP"}, {"id": "ETH-USDT-SWAP"}],
            ai={"selectionCount": 1},
        )
        self.assertEqual(AIWorker._snapshot_observed_instruments(snapshot), ["BTC-USDT-SWAP"])

    def test_codex_jsonl_extracts_final_message(self):
        raw = '{"type":"event","item":{"type":"agent_message","text":"{\\"action\\":\\"hold\\"}"}}\n'
        self.assertEqual(CodexRunner._extract_agent_text(raw), '{"action":"hold"}')

    def test_chat_decoder_accepts_plain_conversation_text(self):
        response = CodexRunner._decode_chat_response("你好，可以先聊聊当前策略的信号和风险。")
        self.assertEqual(response.reply, "你好，可以先聊聊当前策略的信号和风险。")
        self.assertIsNone(response.suggestion)

    def test_chat_runner_failure_returns_degraded_response(self):
        class FailingRunner:
            async def run_chat(self, message, config):
                raise CodexError("Codex CLI timed out")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="shadow"),
                    runner=FailingRunner(),
                    state_dir=Path(directory),
                )
                return await worker.chat("你好，解释一下当前策略")

        response = asyncio.run(run())
        self.assertTrue(response["degraded"])
        self.assertEqual(response["errorCode"], "codex_unavailable")
        self.assertIsNone(response["suggestion"])
        self.assertIn("不会影响当前策略", response["reply"])

    def test_chat_cli_timeout_returns_degraded_response(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "codex"
                executable.write_text("#!/bin/sh\ncat >/dev/null\nsleep 1\n", encoding="utf-8")
                executable.chmod(0o700)
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="shadow", cliTimeoutSeconds=0.05),
                    runner=CodexRunner(str(executable)),
                    state_dir=Path(directory) / "state",
                )
                return await worker.chat("你好，当前策略怎么工作？")

        response = asyncio.run(run())
        self.assertTrue(response["degraded"])
        self.assertEqual(response["errorCode"], "codex_unavailable")

    def test_codex_runner_uses_isolated_environment_and_schema(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                environment = CodexRunner._safe_environment(directory)
                self.assertTrue(Path(environment["CODEX_HOME"]).is_dir())
                self.assertNotIn("OKX_API_KEY", environment)
                self.assertNotIn("OKX_SECRET_KEY", environment)
                self.assertNotIn("OKX_PASSPHRASE", environment)
                executable = Path(directory) / "codex"
                output = self.decision(action="hold", instrumentID=None, assessments=[self.assessment()])
                event = {"type": "agent_message", "text": json.dumps(output)}
                executable.write_text(
                    "#!" + sys.executable + "\nimport json, sys\nsys.stdin.read()\nprint(" + repr(json.dumps(event)) + ")\n",
                    encoding="utf-8",
                )
                executable.chmod(0o700)
                runner = CodexRunner(str(executable))
                decision = await runner.run(self.snapshot(), AIConfig(enabled=True, mode="shadow"))
                return decision

        decision = asyncio.run(run())
        self.assertEqual(decision.action, "hold")

    def test_codex_prompt_budget_checks_utf8_bytes_before_starting_cli(self):
        async def run():
            runner = CodexRunner("unused-codex")
            output = json.dumps(self.decision(action="hold", instrumentID=None, assessments=[self.assessment()])).encode("utf-8")
            for size in (999_999, 1_000_000):
                with self.subTest(size=size):
                    process = SimpleNamespace(returncode=0, communicate=AsyncMock(return_value=(output, b"")))
                    launch = AsyncMock(return_value=process)
                    with patch.object(CodexRunner, "_decision_prompt", return_value="x" * size), \
                         patch.object(CodexRunner, "_safe_environment", return_value={}), \
                         patch("backend.ai_worker.asyncio.create_subprocess_exec", launch):
                        decision = await runner.run(self.snapshot(), AIConfig())
                    self.assertEqual(decision.action, "hold")
                    launch.assert_awaited_once()
                    self.assertEqual(len(process.communicate.await_args.args[0]), size)

            for prompt in ("x" * 1_000_001, "币" * 333_334):
                with self.subTest(utf8_bytes=len(prompt.encode("utf-8"))):
                    launch = AsyncMock()
                    with patch.object(CodexRunner, "_decision_prompt", return_value=prompt), \
                         patch("backend.ai_worker.asyncio.create_subprocess_exec", launch), \
                         self.assertRaises(CodexError) as raised:
                        await runner.run(self.snapshot(), AIConfig())
                    self.assertIn("observation-pool snapshot exceeds", str(raised.exception))
                    self.assertIn("no instruments were omitted", str(raised.exception))
                    launch.assert_not_awaited()

        asyncio.run(run())

    def test_route_audit_records_compact_snapshot_collection_quality(self):
        class HoldRunner:
            async def run(self, snapshot, config):
                return AIDecision.hold(snapshot.snapshotId, "test")

        snapshot = AISnapshot(
            snapshotId="snap-quality", capturedAt=iso(datetime.now(timezone.utc)),
            instruments=[{"id": f"COIN{index}-USDT-SWAP"} for index in range(16)],
            candles={f"COIN{index}-USDT-SWAP/{interval}": [] if index == 1 and interval == "5m" else [{"confirmed": True}]
                     for index in range(16) for interval in ("5m", "15m", "1H", "4H")},
            orderBook={
                "COIN0-USDT-SWAP": {"bids": [["1", "1"]], "asks": [["2", "1"]]},
                "COIN1-USDT-SWAP": {"bids": [["1", "1"]], "asks": []},
                "COIN2-USDT-SWAP": {},
                "COIN3-USDT-SWAP": {"bids": "invalid", "asks": [["2", "1"]]},
            },
            account={"privateAccountData": "account-content-must-not-be-persisted"},
            dataFreshness={
                "collectionDurationSeconds": 2.5,
                "errors": [{"resource": "candles", "instrumentID": "COIN1-USDT-SWAP", "interval": "5m", "attempts": 3,
                            "error": "rate limited", "unexpected": "unexpected-metadata-must-not-be-persisted"}],
            },
        )

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(config=AIConfig(enabled=True, mode="shadow"), runner=HoldRunner(), state_dir=Path(directory))
                with patch.object(CodexRunner, "_decision_prompt", return_value="币种 snapshot"):
                    result = await worker.run_once(snapshot)
                self.assertTrue(result.accepted)
                events = [json.loads(line) for line in (Path(directory) / "ai-decisions.jsonl").read_text().splitlines()]
                return next(event for event in events if event["type"] == "model-route")

        event = asyncio.run(run())
        quality = event["snapshotQuality"]
        self.assertEqual(quality["instrumentCount"], 16)
        self.assertEqual(quality["candleSeriesCount"], 64)
        self.assertEqual(quality["emptyCandleSeries"], ["COIN1-USDT-SWAP/5m"])
        self.assertEqual(quality["orderBookCount"], 1)
        self.assertEqual(quality["collectionDurationSeconds"], 2.5)
        self.assertEqual(quality["collectionErrorCount"], 1)
        self.assertEqual(quality["collectionErrors"][0]["attempts"], 3)
        self.assertEqual(quality["promptBytes"], len("币种 snapshot".encode("utf-8")))
        serialized = json.dumps(event)
        self.assertNotIn("account-content-must-not-be-persisted", serialized)
        self.assertNotIn("unexpected-metadata-must-not-be-persisted", serialized)
        self.assertEqual(event["model"], "gpt-6-luna")
        self.assertEqual(event["reasoningEffort"], "medium")
        self.assertEqual(quality["promptEncoding"], "compact60")

    def test_codex_runner_reports_timeout(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "codex"
                executable.write_text("#!/bin/sh\ncat >/dev/null\nsleep 1\n", encoding="utf-8")
                executable.chmod(0o700)
                runner = CodexRunner(str(executable))
                with patch.object(CodexRunner, "_decision_prompt", return_value="test snapshot"), self.assertRaises(CodexError) as raised:
                    await runner.run(self.snapshot(), AIConfig(mode="shadow", cliTimeoutSeconds=0.05))
                self.assertIn("timed out", str(raised.exception))

        asyncio.run(run())

    def test_codex_runner_kills_descendants_that_hold_pipes_open(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "codex"
                executable.write_text(
                    "#!/usr/bin/env python3\n"
                    "import subprocess\n"
                    "import time, sys\n"
                    "sys.stdin.read()\n"
                    "subprocess.Popen(['sleep', '5'])\n"
                    "time.sleep(5)\n",
                    encoding="utf-8",
                )
                executable.chmod(0o700)
                runner = CodexRunner(str(executable))
                started = asyncio.get_running_loop().time()
                with patch.object(CodexRunner, "_decision_prompt", return_value="test snapshot"), self.assertRaises(CodexError):
                    await runner.run(self.snapshot(), AIConfig(mode="shadow", cliTimeoutSeconds=0.05))
                return asyncio.get_running_loop().time() - started

        elapsed = asyncio.run(run())
        self.assertLess(elapsed, 1.0)

    def test_codex_runner_reports_nonzero_and_oversized_output(self):
        async def run_script(script: str, **config_values):
            with tempfile.TemporaryDirectory() as directory:
                executable = Path(directory) / "codex"
                executable.write_text(script, encoding="utf-8")
                executable.chmod(0o700)
                runner = CodexRunner(str(executable))
                with self.assertRaises(CodexError) as raised:
                    await runner.run(self.snapshot(), AIConfig(mode="shadow", **config_values))
                return str(raised.exception)

        async def run():
            nonzero = await run_script("#!/bin/sh\ncat >/dev/null\nprintf 'bad' >&2\nexit 7\n")
            oversized = await run_script("#!/bin/sh\ncat >/dev/null\nhead -c 2048 /dev/zero\n", maxOutputBytes=1024)
            return nonzero, oversized

        nonzero, oversized = asyncio.run(run())
        self.assertIn("status 7", nonzero)
        self.assertIn("exceeded", oversized)

    def test_chat_apply_uses_previewed_patch_without_rerunning_model(self):
        class FakeRunner:
            def __init__(self):
                self.calls = 0

            async def run_chat(self, message, config):
                self.calls += 1
                return AIChatResponse(
                    schemaVersion=1,
                    reply="建议提高置信度。",
                    suggestion={"minimumConfidence": 0.8},
                )

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                runner = FakeRunner()
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="shadow"),
                    runner=runner,
                    state_dir=Path(directory),
                )
                preview = await worker.chat("提高最低置信度")
                applied = await worker.chat(
                    "提高最低置信度",
                    apply=True,
                    suggestion=preview["suggestion"],
                )
                return runner.calls, applied, worker.get_config()

        calls, applied, config = asyncio.run(run())
        self.assertEqual(calls, 1)
        self.assertTrue(applied["applied"])
        self.assertEqual(config["minimumConfidence"], 0.8)

    def test_gateway_rounds_quantity_and_deduplicates(self):
        calls = []

        async def submit(payload, demo):
            calls.append((payload, demo))
            return {"orderID": "ord-1", "status": "submitted"}

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                gateway = OrderGateway(Path(directory) / "ledger.json", submit=submit)
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1, lotSize=1, minSize=1)
                request = {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "orderType": "market", "quantity": 1.9, "clientOrderID": "aiabc"}
                first = await gateway.submit_intent(request, demo=True, instrument=spec, price=100)
                second = await gateway.submit_intent(request, demo=True, instrument=spec, price=100)
                return first, second

        first, second = asyncio.run(run())
        self.assertEqual(first["quantity"], 1)
        self.assertEqual(second["orderID"], "ord-1")
        self.assertEqual(len(calls), 1)

    def test_gateway_keeps_unknown_submission_reserved(self):
        async def submit(payload, demo):
            raise TimeoutError("network timeout")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                gateway = OrderGateway(Path(directory) / "ledger.json", submit=submit)
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1, lotSize=1, minSize=1)
                with self.assertRaises(TimeoutError):
                    await gateway.submit_intent({"instrumentID": "BTC-USDT-SWAP", "side": "buy", "quantity": 1, "clientOrderID": "aiunknown"}, demo=True, instrument=spec, price=100)
                return gateway.snapshot()

        snapshot = asyncio.run(run())
        self.assertIn("unresolved-aiunknown", snapshot["reservations"])

    def test_gateway_counts_unknown_ai_entry_against_daily_limit(self):
        async def submit(payload, demo):
            raise TimeoutError("network timeout")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                gateway = OrderGateway(Path(directory) / "ledger.json", submit=submit)
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1, lotSize=1, minSize=1)
                request = {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "quantity": 1, "source": "ai", "leverage": 5}
                with self.assertRaises(TimeoutError):
                    await gateway.submit_intent({**request, "clientOrderID": "aiunknown1"}, demo=True, instrument=spec, price=100, daily_order_limit=1)
                with self.assertRaises(OrderGatewayError):
                    await gateway.submit_intent({**request, "clientOrderID": "aiunknown2"}, demo=True, instrument=spec, price=100, daily_order_limit=1)

        asyncio.run(run())

    def test_gateway_releases_definitely_unsubmitted_entry_without_consuming_quota(self):
        attempts = 0

        async def submit(payload, demo):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise OrderNotSubmittedError("AI entry expired before order submission")
            return {"orderID": "order-1"}

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                ledger = Path(directory) / "ledger.json"
                gateway = OrderGateway(ledger, submit=submit)
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1)
                request = {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "quantity": 1, "source": "ai", "clientOrderID": "aideadline"}
                with self.assertRaises(OrderNotSubmittedError):
                    await gateway.submit_intent(request, demo=True, instrument=spec, price=100, daily_order_limit=1)
                restored = OrderGateway(ledger, submit=submit)
                self.assertEqual(restored.reservations, {})
                self.assertEqual(restored.daily_order_count(), 0)
                self.assertEqual(restored._state.get("submissionTimes", []), [])
                await restored.submit_intent(request, demo=True, instrument=spec, price=100, daily_order_limit=1)
                self.assertEqual(restored.daily_order_count(), 1)

        asyncio.run(run())

    def test_definitely_unsubmitted_retry_preserves_older_unknown_reservation(self):
        attempts = 0

        async def submit(payload, demo):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise TimeoutError("unknown order result")
            raise OrderNotSubmittedError("entry expired")

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                gateway = OrderGateway(Path(directory) / "ledger.json", submit=submit)
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1)
                request = {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "quantity": 1, "source": "ai", "clientOrderID": "aiolderunknown"}
                with self.assertRaises(TimeoutError):
                    await gateway.submit_intent(request, demo=True, instrument=spec, price=100)
                previous = gateway.reservations["unresolved-aiolderunknown"].copy()
                with self.assertRaises(OrderNotSubmittedError):
                    await gateway.submit_intent(request, demo=True, instrument=spec, price=100)
                self.assertEqual(gateway.reservations["unresolved-aiolderunknown"], previous)
                self.assertEqual(gateway.daily_order_count(), 1)

        asyncio.run(run())

    def test_gateway_limits_successful_ai_entries_per_utc_day(self):
        calls = []

        async def submit(payload, demo):
            calls.append(payload)
            return {"orderID": f"ord-{len(calls)}", "status": "submitted"}

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                gateway = OrderGateway(Path(directory) / "ledger.json", submit=submit)
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1, lotSize=1, minSize=1)
                request = {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "orderType": "market", "quantity": 1, "source": "ai", "leverage": 5}
                await gateway.submit_intent({**request, "clientOrderID": "aione"}, demo=True, instrument=spec, price=100, available_equity=1000, daily_order_limit=1)
                self.assertEqual(gateway.daily_order_count(), 1)
                with self.assertRaises(OrderGatewayError):
                    await gateway.submit_intent({**request, "clientOrderID": "aitwo"}, demo=True, instrument=spec, price=100, available_equity=1000, daily_order_limit=1)
                # Reduce-only exits are not counted against the entry quota.
                await gateway.submit_intent({"instrumentID": "BTC-USDT-SWAP", "side": "sell", "orderType": "market", "quantity": 1, "source": "ai", "reduceOnly": True, "clientOrderID": "aiclose"}, demo=True, instrument=spec, price=100, available_equity=1000, daily_order_limit=1)

        asyncio.run(run())

    def test_gateway_blocks_same_ai_instrument_for_twelve_hours_but_allows_close(self):
        calls = []

        async def submit(payload, demo):
            calls.append(payload)
            return {"orderID": f"ord-{len(calls)}", "status": "submitted"}

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                ledger = Path(directory) / "ledger.json"
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1, lotSize=1, minSize=1)
                request = {
                    "instrumentID": "BTC-USDT-SWAP", "side": "buy", "orderType": "market",
                    "quantity": 1, "source": "ai", "leverage": 1,
                }
                gateway = OrderGateway(ledger, submit=submit)
                await gateway.submit_intent({**request, "clientOrderID": "aitwelve1"}, demo=True, instrument=spec, price=100)
                with self.assertRaisesRegex(OrderGatewayError, "12 小时"):
                    await gateway.submit_intent({**request, "clientOrderID": "aitwelve2"}, demo=True, instrument=spec, price=100)
                restored = OrderGateway(ledger, submit=submit)
                with self.assertRaisesRegex(OrderGatewayError, "12 小时"):
                    await restored.submit_intent({**request, "clientOrderID": "aitwelve3"}, demo=True, instrument=spec, price=100)
                await restored.submit_intent(
                    {"instrumentID": "BTC-USDT-SWAP", "side": "sell", "orderType": "market", "quantity": 1,
                     "source": "ai", "reduceOnly": True, "clientOrderID": "aitwelveclose"},
                    demo=True, instrument=spec, price=100,
                )

        asyncio.run(run())

    def test_gateway_converts_fixed_margin_and_ai_leverage_to_notional(self):
        calls = []

        async def submit(payload, demo):
            calls.append(payload)
            return {"orderID": "ord-margin", "status": "submitted"}

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                gateway = OrderGateway(Path(directory) / "ledger.json", submit=submit)
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1, lotSize=1, minSize=1)
                result = await gateway.submit_intent(
                    {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "orderType": "market",
                     "targetNotional": 500 * 5, "source": "ai", "leverage": 5,
                     "clientOrderID": "aimargin"},
                    demo=True, instrument=spec, price=100, available_equity=3000,
                    daily_order_limit=20,
                )
                return result

        result = asyncio.run(run())
        self.assertEqual(result["notional"], 2500)
        self.assertEqual(result["marginUSD"], 500)
        self.assertEqual(calls[0]["quantity"], 25)

    def test_gateway_sizes_limit_orders_at_limit_price_and_rejects_margin_overrun(self):
        calls = []

        async def submit(payload, demo):
            calls.append(payload)
            return {"orderID": "ord-limit-margin", "status": "submitted"}

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                gateway = OrderGateway(Path(directory) / "ledger.json", submit=submit)
                spec = InstrumentSpec("BTC-USDT-SWAP", ctVal=1, lotSize=1, minSize=1)
                result = await gateway.submit_intent(
                    {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "orderType": "limit",
                     "price": 120, "targetNotional": 1000, "marginUSD": 500,
                     "source": "ai", "leverage": 2, "clientOrderID": "ailimitmargin"},
                    demo=True, instrument=spec, price=100, available_equity=3000,
                    daily_order_limit=20,
                )
                with self.assertRaises(OrderGatewayError):
                    await gateway.submit_intent(
                        {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "orderType": "limit",
                         "price": 120, "quantity": 9, "marginUSD": 500,
                         "source": "ai", "leverage": 2, "clientOrderID": "aioversized"},
                        demo=True, instrument=spec, price=100, available_equity=3000,
                        daily_order_limit=20,
                    )
                return result

        result = asyncio.run(run())
        self.assertEqual(result["quantity"], 8)
        self.assertEqual(result["notional"], 960)
        self.assertEqual(result["marginUSD"], 480)
        self.assertEqual(calls[0]["quantity"], 8)


if __name__ == "__main__":
    unittest.main()
