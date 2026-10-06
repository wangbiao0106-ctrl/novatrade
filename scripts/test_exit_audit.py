#!/usr/bin/env python3
"""Focused fixtures for native OCO exit reconciliation and gateway logging."""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.exit_audit import sync_native_protection_exits  # noqa: E402
from backend.order_gateway import InstrumentSpec, OrderGateway  # noqa: E402


class FakeGateway:
    def __init__(self, entries: list[dict[str, object]]) -> None:
        self.entries = entries
        self.events: list[dict[str, object]] = []
        self.keys: set[str] = set()

    async def tracked_entries(self, *, demo: bool) -> list[dict[str, object]]:
        return [row for row in self.entries if row.get("demo") == demo]

    async def record_event(self, value: dict[str, object], *, event_key: str) -> bool:
        if event_key in self.keys:
            return False
        self.keys.add(event_key)
        self.events.append(value)
        return True


def history_row(*, instrument: str = "FIL-USDT-SWAP", client: str = "aidecision123") -> dict[str, str | list[str]]:
    return {
        "algoId": "algo-1",
        "instId": instrument,
        "attachAlgoClOrdId": client,
        "actualSide": "tp",
        "state": "effective",
        "actualTriggerPx": "1.1899",
        "actualTriggerPxType": "mark",
        "actualTriggerTime": "1700000000000",
        "actualSz": "10",
        "actualPx": "1.1901",
        "ordIdList": ["child-1"],
    }


class ExitAuditTests(unittest.TestCase):
    def test_exact_attach_matches_and_positions_history_adds_pnl(self):
        gateway = FakeGateway([{
            "instrumentID": "FIL-USDT-SWAP", "orderID": "entry-1", "clientOrderID": "aidecision123",
            "decisionID": "decision-1", "strategyID": "codex", "demo": True,
        }])

        async def request(method: str, path: str, *, params=None, body=None):
            if path == "/trade/orders-algo-history":
                return {"data": [history_row()]}
            if path == "/account/positions-history":
                return {"data": [{
                    "instId": "FIL-USDT-SWAP", "posId": "position-1", "uTime": "1700000000000",
                    "closeAvgPx": "1.1902", "closeTotalPos": "10", "realizedPnl": "-2.75",
                }]}
            raise AssertionError(path)

        events = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        self.assertEqual(len(events), 1)
        event = events[0]
        self.assertEqual(event["reason"], "take-profit")
        self.assertEqual(event["triggerPriceType"], "mark")
        self.assertEqual(event["fillPrice"], 1.1901)
        self.assertEqual(event["realizedPnL"], -2.75)
        self.assertIn("止盈(TP)", str(event["message"]))

    def test_mismatched_instrument_or_client_is_ignored(self):
        gateway = FakeGateway([{
            "instrumentID": "FIL-USDT-SWAP", "orderID": "entry-1", "clientOrderID": "aidecision123", "demo": True,
        }])

        async def request(method: str, path: str, *, params=None, body=None):
            return {"data": [history_row(instrument="ETH-USDT-SWAP", client="aidecision123"),
                              history_row(instrument="FIL-USDT-SWAP", client="another-client")]}

        events = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        self.assertEqual(events, [])
        self.assertEqual(gateway.events[0]["type"], "native-protection-unknown")
        self.assertEqual(gateway.events[0]["reason"], "native_protection_unknown")
        self.assertTrue(all(event["type"] != "exit-settled" for event in gateway.events))

    def test_same_history_row_is_persistently_deduplicated(self):
        gateway = FakeGateway([{
            "instrumentID": "FIL-USDT-SWAP", "orderID": "entry-1", "clientOrderID": "aidecision123", "demo": True,
        }])

        async def request(method: str, path: str, *, params=None, body=None):
            if path == "/trade/orders-algo-history":
                return {"data": [history_row()]}
            return {"data": []}

        first = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        second = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(gateway.events), 1)

    def test_gateway_submission_audit_contains_decision_and_aligned_protection(self):
        async def submit(request: dict[str, object], demo: bool) -> dict[str, object]:
            return {"orderID": "entry-1", "instrumentID": request["instrumentID"], "submittedAt": "2026-10-07T00:00:00Z"}

        with tempfile.TemporaryDirectory() as directory:
            runtime_events: list[dict[str, object]] = []

            async def sink(value: dict[str, object]) -> None:
                runtime_events.append(value)

            async def exercise() -> tuple[dict[str, object], list[dict[str, object]]]:
                gateway = OrderGateway(
                    Path(directory) / "ledger.json", submit=submit,
                    runtime_sink=sink, limits={"minOrderIntervalSeconds": 0},
                )
                result = await gateway.submit_intent({
                    "instrumentID": "FIL-USDT-SWAP", "side": "buy", "orderType": "market", "quantity": 10,
                    "source": "ai", "clientOrderID": "aidecision123", "decisionID": "decision-1", "strategyID": "codex",
                    "takeProfitTriggerPrice": 1.18999, "stopLossTriggerPrice": 1.14399,
                }, demo=True, instrument=InstrumentSpec("FIL-USDT-SWAP", 1, tickSize=0.0001), price=1.1)
                return result, await gateway.audit()

            result, audit = asyncio.run(exercise())
            self.assertEqual(result["takeProfitTriggerPrice"], 1.1899)
            self.assertEqual(result["stopLossTriggerPrice"], 1.1439)
            self.assertEqual(result["decisionID"], "decision-1")
            self.assertEqual(audit[-1]["reason"], "entry-submitted")
            self.assertIn("orderID=entry-1", audit[-1]["order"]["message"])
            self.assertEqual(runtime_events[-1]["decisionID"], "decision-1")
            self.assertIn("tp=1.1899", str(runtime_events[-1]["message"]))
            stored = json.loads((Path(directory) / "ledger.json").read_text())
            self.assertEqual(stored["reservations"]["entry-1"]["takeProfitTriggerPrice"], 1.1899)


if __name__ == "__main__":
    unittest.main()
