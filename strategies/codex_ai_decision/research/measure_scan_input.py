#!/usr/bin/env python3
"""Measure one current paper scan with the normal Codex runner, without orders.

The running backend supplies account/risk through authenticated GETs. Public
market collection and the exact compact20 prompt use runtime code. No worker,
execution gateway, configuration update or local paper ledger is instantiated.
Only size/usage metadata is recorded, never raw candles or account details.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import ExitStack
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sys
from types import SimpleNamespace
from unittest.mock import patch

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend import main
from backend.ai_schema import AIConfig
from backend.ai_worker import CodexRunner


class UsageRunner(CodexRunner):
    def __init__(self):
        super().__init__()
        self.usage_events = []
        self.prompt = ""

    async def _run_prompt(self, prompt, schema, config, *, deadline=None):
        self.prompt = prompt
        return await super()._run_prompt(prompt, schema, config, deadline=deadline)

    def _extract_agent_text(self, output):
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if isinstance(event, dict) and event.get("type") == "turn.completed" and isinstance(event.get("usage"), dict):
                self.usage_events.append({key: value for key, value in event["usage"].items()
                                          if isinstance(value, int) and not isinstance(value, bool) and value >= 0})
        return super()._extract_agent_text(output)


async def measure():
    state = Path.home() / "Library/Application Support/NovaTrade"
    headers = {"Authorization": "Bearer " + (state / "locald.token").read_text().strip()}
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8787", headers=headers, timeout=20) as client:
        async def get(path):
            response = await client.get(path)
            response.raise_for_status()
            return response.json()

        health, config_dict, account, risk = await asyncio.gather(
            get("/health"), get("/api/v1/ai/strategies/codex/config"),
            get("/api/v1/account"), get("/api/v1/risk"),
        )
        if health.get("mode") != "paper" or account.get("profile") != "local-paper":
            raise RuntimeError("This measurement entrypoint requires the running local paper account")
        config = AIConfig.from_dict(config_dict)
        count = account.get("todayAIOrderCount", 0)
        account_proxy = SimpleNamespace(account_snapshot=lambda **kwargs: account, daily_order_count=lambda: count)

        async def account_read():
            return account

        async def risk_read():
            return risk

        # Read-only adapters prevent a second process from opening or writing
        # the live paper ledger. No execution or worker run_once is called.
        with ExitStack() as stack:
            stack.enter_context(patch.object(main, "ai_worker", SimpleNamespace(config=config)))
            stack.enter_context(patch.object(main, "local_paper_mode", return_value=True))
            stack.enter_context(patch.object(main, "_get_paper_account", return_value=account_proxy))
            stack.enter_context(patch.object(main, "account", new=account_read))
            stack.enter_context(patch.object(main, "risk", new=risk_read))
            print("Collecting current observed contracts with runtime collector...", flush=True)
            snapshot = await main._ai_snapshot()

        runner = UsageRunner()
        prompt = runner._decision_prompt(snapshot, config, encoding="compact20")
        print(json.dumps({"stage": "collected", "instruments": len(snapshot.observed_instruments()),
                          "promptBytes": len(prompt.encode("utf-8")), "collectionSeconds": snapshot.dataFreshness.get("collectionDurationSeconds")}), flush=True)
        error = None
        try:
            decision = await runner.run(snapshot, config)
            assessment_count = len(decision.assessments)
        except Exception as exception:
            # Record only the exception class: provider stderr may contain
            # source/account details, which are not needed for usage reports.
            error = type(exception).__name__
            assessment_count = None
        finally:
            await main.OKX_HTTP_CLIENT.aclose()

        actual_prompt = runner.prompt or prompt
        prompt_bytes = actual_prompt.encode("utf-8")
        encoded = json.loads(actual_prompt.split("\nSNAPSHOT:\n", 1)[1])
        candle_counts = {}
        for key, series in encoded["candles"].items():
            interval = key.rsplit("/", 1)[-1]
            candle_counts[interval] = candle_counts.get(interval, 0) + len(series.get("rows", []) if isinstance(series, dict) else series)
        local_time = datetime.fromisoformat(snapshot.capturedAt.replace("Z", "+00:00")).astimezone(__import__("zoneinfo").ZoneInfo("Asia/Shanghai"))
        return {
            "measuredAtAsiaShanghai": local_time.isoformat(timespec="seconds"),
            "snapshotId": snapshot.snapshotId, "model": config.routineModel,
            "reasoningEffort": config.routineReasoningEffort, "workflow": "single",
            "instrumentCount": len(snapshot.observed_instruments()),
            "runtimeCandleSeriesCount": len(snapshot.candles), "promptCandleRowCounts": candle_counts,
            "promptEncoding": "compact20", "promptBytes": len(prompt_bytes),
            "promptSHA256": hashlib.sha256(prompt_bytes).hexdigest(),
            "snapshotComponentBytes": {key: len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
                                       for key, value in encoded.items()},
            "usageSource": "Codex CLI turn.completed.usage",
            "usageEvents": runner.usage_events,
            "inputTokens": sum(event["input_tokens"] for event in runner.usage_events) if runner.usage_events and all("input_tokens" in event for event in runner.usage_events) else None,
            "cachedInputTokens": sum(event["cached_input_tokens"] for event in runner.usage_events) if runner.usage_events and all("cached_input_tokens" in event for event in runner.usage_events) else None,
            "outputTokens": sum(event["output_tokens"] for event in runner.usage_events) if runner.usage_events and all("output_tokens" in event for event in runner.usage_events) else None,
            "assessmentCount": assessment_count, "analysisErrorClass": error,
            "orderExecutionInvoked": False,
            "tokenScope": "Provider-reported CLI turn input, including CLI context/schema; cached tokens are a subset of input tokens",
        }


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional metadata-only JSON report path")
    args = parser.parse_args()
    result = asyncio.run(measure())
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
