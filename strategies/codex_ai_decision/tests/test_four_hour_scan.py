"""Verify v1.1 primary 4H analysis without exchange or model calls."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend.ai_market_facts import market_facts, primary_entry_quality  # noqa: E402
from backend.ai_policy import validate_decision  # noqa: E402
from backend.ai_schema import AIDecision, AIConfig, AISnapshot  # noqa: E402
from backend.ai_worker import AIWorker, CodexRunner, _prompt_snapshot  # noqa: E402


BTC = "BTC-USDT-SWAP"


def iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def candles(count: int = 60, *, confirmed: bool = True) -> list[dict]:
    now = datetime.now(timezone.utc)
    return [{
        "timestamp": iso(now - timedelta(hours=4 * (count - index))),
        "open": 99 + index, "high": 102 + index, "low": 98 + index,
        "close": 100 + index, "volume": 10, "confirmed": confirmed,
    } for index in range(count)]


def snapshot(instruments: list[str] | None = None) -> AISnapshot:
    ids = instruments or [BTC]
    return AISnapshot(
        snapshotId="four-hour-scan", capturedAt=iso(datetime.now(timezone.utc)),
        instruments=[{"id": instrument} for instrument in ids],
        candles={f"{instrument}/{interval}": candles()
                 for instrument in ids for interval in ("5m", "15m", "1H", "4H")},
        tickers={instrument: {"last": 100} for instrument in ids},
        account={
            "authenticated": True, "availableEquityUSD": 5000, "todayLossCount": 0,
            "pendingOrdersKnown": True, "pendingOrders": [], "positionsKnown": True, "positions": [],
            "dataQuality": {"dailyBillsAvailable": True, "pendingOrdersAvailable": True},
        },
        risk={"killSwitch": False, "dailyPnLPercent": 0,
              "dataQuality": {"accountRefreshError": None, "accountRefreshRetryable": False}},
        ai={"selectedInstruments": ids,
            "tradingAvailability": {instrument: {"available": True} for instrument in ids}},
        dataFreshness={"maxAgeSeconds": 90, "availability": {}},
    )


def assessment(instrument: str = BTC, direction: str = "long") -> dict:
    return {
        "instrumentID": instrument, "direction": direction,
        "limitPrice": 100, "stopLossPrice": 90 if direction == "long" else 110,
        "takeProfitPrice": 120 if direction == "long" else 80,
        "winRate": .5, "riskRewardRatio": 2, "confidence": .9,
        "entryEligible": True, "unmetConditions": [],
        "reason": "4H结构确认，等待回踩入场，止损位置为结构失效价。",
    }


def decision(source: AISnapshot, *, direction: str = "long", action: str = "open") -> dict:
    plan = assessment(direction=direction)
    return {
        "schemaVersion": 1, "decisionId": "four-hour-" + action,
        "snapshotId": source.snapshotId, "action": action,
        "instrumentID": BTC if action != "hold" else None,
        "direction": direction, "orderType": "limit",
        "limitPrice": plan["limitPrice"], "stopLossPrice": plan["stopLossPrice"],
        "takeProfitPrice": plan["takeProfitPrice"], "winRate": .5,
        "riskRewardRatio": 2, "leverage": 1, "confidence": .9,
        "validUntil": iso(datetime.now(timezone.utc) + timedelta(minutes=1)),
        "reasonCode": "THESIS_INVALIDATED" if action in {"close", "cancel"} else "SETUP",
        "reason": "4H结构跌破支撑，本轮交易逻辑已失效。",
        "orderID": "pending-1" if action == "cancel" else None,
        "assessments": [plan] if action == "open" else [],
    }


def restore_prompt_snapshot(value: dict) -> dict:
    result = deepcopy(value)
    result["candles"] = {
        key: [dict(zip(rows["columns"], row)) for row in rows["rows"]]
        if isinstance(rows, dict) else rows for key, rows in result["candles"].items()
    }
    return result


class FourHourScanTests(unittest.TestCase):
    def test_compact_modes_keep_complete_primary_history_and_forming_context(self):
        source = snapshot()
        for rows in source.candles.values():
            rows[-1]["confirmed"] = False
        original = source.to_dict()
        for encoding, auxiliary_count in (("compact20", 21), ("compact10", 11)):
            with self.subTest(encoding=encoding):
                restored = restore_prompt_snapshot(_prompt_snapshot(source, encoding=encoding))
                self.assertEqual(restored["candles"][f"{BTC}/4H"], original["candles"][f"{BTC}/4H"])
                for interval in ("5m", "15m", "1H"):
                    self.assertEqual(len(restored["candles"][f"{BTC}/{interval}"]), auxiliary_count)
                    self.assertIs(restored["candles"][f"{BTC}/{interval}"][-1]["confirmed"], False)
        self.assertEqual(source.to_dict(), original)

    def test_single_grouped_and_coordinator_share_primary_rule_and_metadata(self):
        ids = [f"COIN{index}-USDT-SWAP" for index in range(5)]
        source = snapshot(ids)
        config = AIConfig(enabled=True, mode="shadow", allowedInstruments=tuple(ids))
        prompts: list[str] = []

        async def invoke(prompt, schema, config, *, deadline=None):
            prompts.append(prompt)
            payload = decision(source, action="hold")
            if "GROUP REPORTS:\n" in prompt:
                payload.pop("assessments")
            else:
                raw = restore_prompt_snapshot(json.loads(prompt.split("\nSNAPSHOT:\n", 1)[1]))
                payload["assessments"] = [assessment(instrument) for instrument in raw["ai"]["selectedInstruments"]]
                for instrument in raw["ai"]["selectedInstruments"]:
                    self.assertEqual(len(raw["candles"][f"{instrument}/4H"]), 60)
            return payload

        runner = CodexRunner("unused-codex")
        with patch.object(runner, "_run_prompt", side_effect=invoke):
            result = asyncio.run(runner.run(source, config))
        self.assertEqual(len(prompts), 3)
        self.assertEqual([item.instrumentID for item in result.assessments], ids)
        prompts.append(CodexRunner._decision_prompt(source, config))
        for prompt in prompts:
            self.assertIn("4H is the primary entry timeframe for BOTH long and short plans", prompt)
            self.assertIn("at least 20 valid confirmed OHLC 4H candles", prompt)
            self.assertIn("cannot replace or independently override the 4H entry thesis", prompt)
            self.assertIn("proposed entry conditions, and the invalidation level", prompt)
            self.assertIn("Insufficient primary history blocks only open", prompt)
            self.assertIn('"primaryEntryInterval":"4H"', prompt)
        gates = CodexRunner._entry_gates(source, config)
        self.assertEqual(set(gates["primaryEntryQuality"]), set(ids))
        self.assertTrue(all(item["canOpen"] for item in gates["primaryEntryQuality"].values()))

    def test_primary_quality_excludes_forming_bars_and_matches_measured_facts(self):
        source = snapshot()
        source.candles[f"{BTC}/4H"] = candles(20) + candles(1, confirmed=False)
        quality = primary_entry_quality(source, BTC)
        self.assertTrue(quality["canOpen"])
        self.assertEqual(quality["usableTrailingConfirmedRowCount"], 20)
        facts = market_facts(source)
        self.assertEqual(facts["instruments"][BTC]["primaryEntryQuality"], quality)
        self.assertEqual(facts["methodology"]["primaryEntryInterval"], "4H")

    def test_primary_history_deficiency_blocks_open_even_with_complete_shorter_context(self):
        malformed = candles(20)
        malformed[-1]["high"] = 1
        unknown_confirmation = candles(20)
        unknown_confirmation[-1]["confirmed"] = 1
        broken_segment = candles(60)
        broken_segment[40]["close"] = None
        variants = {
            "missing": None, "empty": [], "forming_only": candles(20, confirmed=False),
            "under20": candles(19), "malformed_ohlc": malformed,
            "unknown_confirmation": unknown_confirmation, "broken_trailing_segment": broken_segment,
            "19_closed_plus_forming": candles(19) + candles(1, confirmed=False),
        }
        for label, rows in variants.items():
            with self.subTest(label=label):
                source = snapshot()
                if rows is None:
                    source.candles.pop(f"{BTC}/4H")
                else:
                    source.candles[f"{BTC}/4H"] = rows
                result = validate_decision(decision(source), source, AIConfig(enabled=True, mode="shadow", allowedInstruments=(BTC,)))
                self.assertFalse(result.accepted)
                self.assertEqual(result.decision.action, "hold")
                self.assertIn("4H requires at least 20 valid confirmed OHLC candles", result.reason)

    def test_sufficient_primary_history_preserves_both_long_and_short_entries(self):
        source = snapshot()
        source.candles[f"{BTC}/4H"] = candles(20)
        for direction in ("long", "short"):
            with self.subTest(direction=direction):
                result = validate_decision(decision(source, direction=direction), source, AIConfig(enabled=True, mode="shadow", allowedInstruments=(BTC,)))
                self.assertTrue(result.accepted, result.reason)
                self.assertEqual(result.decision.direction, direction)

    def test_missing_primary_history_preserves_hold_close_and_cancel(self):
        base = snapshot()
        base.candles.pop(f"{BTC}/4H")
        account = dict(base.account, positions=[{"instrumentID": BTC, "quantity": 1, "side": "long"}],
                       pendingOrders=[{"id": "pending-1", "instrumentID": BTC, "status": "live", "quantity": 1}])
        source = replace(base, account=account)
        for action in ("hold", "close", "cancel"):
            with self.subTest(action=action):
                result = validate_decision(decision(source, action=action), source, AIConfig(enabled=True, mode="shadow", allowedInstruments=(BTC,)))
                self.assertTrue(result.accepted, result.reason)

    def test_hold_still_reconciles_existing_protection_with_missing_primary_history(self):
        source = snapshot()
        source.candles.pop(f"{BTC}/4H")
        source = replace(source, account=dict(source.account, positions=[{"instrumentID": BTC, "quantity": 1, "side": "long"}]))
        runner = AsyncMock()
        runner.run.return_value = AIDecision.from_dict(decision(source, action="hold"))
        gateway = AsyncMock()

        async def run():
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(config=AIConfig(enabled=True, mode="demo-active", allowedInstruments=(BTC,)),
                                  runner=runner, order_gateway=gateway, state_dir=Path(directory))
                return await worker.run_once(source)

        result = asyncio.run(run())
        self.assertTrue(result.accepted, result.reason)
        gateway.assert_awaited_once()
        self.assertEqual(gateway.await_args.args[0].action, "hold")


if __name__ == "__main__":
    unittest.main()
