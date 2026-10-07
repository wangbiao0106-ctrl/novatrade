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
        "tpTriggerPx": "1.1899",
        "tpTriggerPxType": "mark",
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
                self.assertEqual(params.get("state"), "effective")
                return {"data": [history_row()]}
            if path == "/trade/order":
                self.assertEqual(params.get("ordId"), "child-1")
                return {"data": [{"ordId": "child-1", "state": "filled", "accFillSz": "10", "fillPx": "1.1901"}]}
            if path == "/account/positions":
                return {"data": []}
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
        self.assertEqual(event["triggerPrice"], 1.1899)
        self.assertEqual(event["triggerPriceType"], "mark")
        self.assertEqual(event["fillPrice"], 1.1901)
        self.assertEqual(event["realizedPnL"], -2.75)
        self.assertEqual(event["type"], "exit-settled")
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
            if path == "/trade/order":
                return {"data": [{"ordId": "child-1", "state": "filled", "accFillSz": "10", "fillPx": "1.1901"}]}
            if path == "/account/positions":
                return {"data": []}
            return {"data": []}

        first = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        second = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        self.assertEqual(len(first), 1)
        self.assertEqual(second, [])
        self.assertEqual(len(gateway.events), 1)

    def test_filled_staged_take_profit_with_remaining_position_is_not_settled(self):
        gateway = FakeGateway([{
            "instrumentID": "FIL-USDT-SWAP", "orderID": "entry-1", "clientOrderID": "aidecision123",
            "decisionID": "decision-1", "strategyID": "codex", "demo": True,
        }])

        async def request(method: str, path: str, *, params=None, body=None):
            if path == "/trade/orders-algo-history":
                return {"data": [history_row()]}
            if path == "/trade/order":
                return {"data": [{"ordId": "child-1", "state": "filled", "accFillSz": "10", "fillPx": "1.1901"}]}
            if path == "/account/positions":
                return {"data": [{"instId": "FIL-USDT-SWAP", "pos": "10"}]}
            if path == "/account/positions-history":
                return {"data": []}
            raise AssertionError(path)

        events = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "exit-partial")
        self.assertTrue(events[0]["positionOpen"])
        self.assertEqual(gateway.events[0]["type"], "exit-partial")

    def test_filled_exit_with_malformed_matching_position_is_not_settled(self):
        gateway = FakeGateway([{
            "instrumentID": "FIL-USDT-SWAP", "orderID": "entry-1", "clientOrderID": "aidecision123",
            "decisionID": "decision-1", "strategyID": "codex", "demo": True,
        }])

        async def request(method: str, path: str, *, params=None, body=None):
            if path == "/trade/orders-algo-history":
                return {"data": [history_row()]}
            if path == "/trade/order":
                return {"data": [{"ordId": "child-1", "state": "filled", "accFillSz": "10", "fillPx": "1.1901"}]}
            if path == "/account/positions":
                return {"data": [{"instId": "FIL-USDT-SWAP", "pos": "unknown"}]}
            if path == "/account/positions-history":
                return {"data": []}
            raise AssertionError(path)

        events = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "exit-fill-unconfirmed")
        self.assertIsNone(events[0]["positionOpen"])

    def test_effective_oco_with_canceled_child_and_open_position_is_protection_failure(self):
        gateway = FakeGateway([{
            "instrumentID": "FIL-USDT-SWAP", "orderID": "entry-1", "clientOrderID": "aidecision123",
            "decisionID": "decision-1", "strategyID": "codex", "demo": True,
        }])

        async def request(method: str, path: str, *, params=None, body=None):
            if path == "/trade/orders-algo-history":
                return {"data": [history_row()]}
            if path == "/trade/order":
                return {"data": [{
                    "ordId": "child-1", "state": "canceled", "accFillSz": "0",
                    "cancelSourceReason": "Order was canceled by system",
                }]}
            if path == "/account/positions":
                return {"data": [{"instId": "FIL-USDT-SWAP", "pos": "10"}]}
            raise AssertionError(path)

        events = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["type"], "native-protection-failure")
        self.assertEqual(events[0]["reason"], "native_protection_child_not_filled")
        self.assertTrue(events[0]["positionOpen"])
        self.assertEqual(events[0]["childOrders"][0]["state"], "canceled")
        self.assertEqual(gateway.events[0]["type"], "native-protection-failure")

    def test_effective_oco_without_child_fill_never_releases_entry_reservation(self):
        gateway = FakeGateway([{
            "instrumentID": "FIL-USDT-SWAP", "orderID": "entry-1", "clientOrderID": "aidecision123", "demo": True,
        }])

        async def request(method: str, path: str, *, params=None, body=None):
            if path == "/trade/orders-algo-history":
                return {"data": [history_row()]}
            if path == "/trade/order":
                return {"data": [{"ordId": "child-1", "state": "canceled", "accFillSz": "0"}]}
            if path == "/account/positions":
                return {"data": [{"instId": "FIL-USDT-SWAP", "pos": "10"}]}
            raise AssertionError(path)

        events = asyncio.run(sync_native_protection_exits(gateway, demo=True, private_request=request))
        self.assertEqual(len(events), 1)
        self.assertNotEqual(events[0]["type"], "exit-settled")
        self.assertEqual(gateway.events[0]["type"], "native-protection-failure")

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

    def test_settled_exit_releases_entry_reservation_for_a_later_open(self):
        calls: list[dict[str, object]] = []

        async def submit(request: dict[str, object], demo: bool) -> dict[str, object]:
            calls.append(request)
            return {"orderID": f"entry-{len(calls)}", "instrumentID": request["instrumentID"]}

        async def exercise() -> dict[str, dict[str, object]]:
            with tempfile.TemporaryDirectory() as directory:
                gateway = OrderGateway(
                    Path(directory) / "ledger.json", submit=submit,
                    limits={"minOrderIntervalSeconds": 0},
                )
                request = {
                    "instrumentID": "FIL-USDT-SWAP", "side": "sell", "orderType": "market",
                    "quantity": 10, "source": "ai", "leverage": 1,
                }
                await gateway.submit_intent(
                    {**request, "clientOrderID": "aifilentry1"}, demo=True,
                    instrument=InstrumentSpec("FIL-USDT-SWAP", 1), price=1,
                )
                self.assertIn("entry-1", gateway.reservations)
                await gateway.record_event(
                    {"type": "exit-settled", "entryOrderID": "entry-1", "instrumentID": "FIL-USDT-SWAP"},
                    event_key="oco-demo-algo-1-tp",
                )
                self.assertEqual(gateway.reservations, {})
                await gateway.submit_intent(
                    {**request, "clientOrderID": "aifilentry2"}, demo=True,
                    instrument=InstrumentSpec("FIL-USDT-SWAP", 1), price=1,
                )
                return gateway.reservations

        reservations = asyncio.run(exercise())
        self.assertIn("entry-2", reservations)


if __name__ == "__main__":
    unittest.main()
