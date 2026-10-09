#!/usr/bin/env python3
"""Verify account execution gates and durable order outcomes without network."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from dataclasses import replace
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import main  # noqa: E402
from backend.ai_policy import validate_decision  # noqa: E402
from backend.ai_schema import AIConfig, AIDecision, AISnapshot  # noqa: E402
from backend.ai_worker import CodexRunner  # noqa: E402
from backend.order_gateway import InstrumentSpec, OrderGateway, OrderGatewayError, OrderNotSubmittedError  # noqa: E402

BTC = "BTC-USDT-SWAP"
NEIRO = "NEIRO-USDT-SWAP"


def iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def exchange_error(code: str, detail: str = "Exchange read failed") -> HTTPException:
    error = HTTPException(502, detail)
    error.exchange_code = code
    return error


def instrument_row(instrument: str = BTC, **overrides) -> dict:
    return {"instId": instrument, "state": "live", "settleCcy": "USDT", "ctType": "linear",
            "ctVal": "0.01", "ctMult": "1", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1",
            **overrides}


def entry_request() -> dict:
    return {"instrumentID": BTC, "side": "buy", "orderType": "limit", "price": 100,
            "quantity": 1, "leverage": 1, "source": "ai", "clientOrderID": "aiexecution1",
            "stopLossTriggerPrice": 90, "takeProfitTriggerPrice": 130,
            "_aiEntryReferencePrice": 100, "_aiEntryPlannedEntryPrice": 100,
            "_aiEntryDeadline": (datetime.now(timezone.utc) + timedelta(minutes=2)).timestamp()}


def unverifiable_order_responses() -> tuple[dict, ...]:
    return ({"code": "0"}, {"code": "0", "data": None}, {"code": "0", "data": []},
            {"code": "0", "data": {"ordId": "wrong-shape"}}, {"code": "0", "data": [None]},
            {"code": "0", "data": [{}]}, {"code": "0", "data": [{"ordId": ""}]},
            {"code": "0", "data": [{"ordId": "   "}]})


class NoNetworkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        # Any unmocked path must fail locally, including a future new helper.
        for method in ("get", "request"):
            guard = patch.object(main.OKX_HTTP_CLIENT, method, new=AsyncMock(
                side_effect=AssertionError("Unexpected exchange network request")))
            guard.start()
            self.addCleanup(guard.stop)


class AccountExecutionUniverseTests(NoNetworkTests):
    async def test_authenticated_instrument_source_filters_account_market(self):
        rows = [instrument_row(), instrument_row("OFF-USDT-SWAP", state="suspend"),
                instrument_row("ETH-USD-SWAP", settleCcy="ETH", ctType="inverse"),
                instrument_row("INVERSE-USDT-SWAP", ctType="inverse"),
                instrument_row("MISSING-USDT-SWAP", state=""), None, {}]
        private = AsyncMock(return_value={"data": rows})
        public = AsyncMock(side_effect=AssertionError("Public instruments cannot authorize trading"))
        with patch.object(main, "okx_private_request", new=private), \
             patch.object(main, "okx_get", new=public):
            available = await main._account_swap_instruments()
        self.assertEqual(available, {BTC: rows[0]})
        private.assert_awaited_once_with("GET", "/account/instruments", params={"instType": "SWAP"})
        public.assert_not_awaited()

    async def test_malformed_account_list_is_unknown_rather_than_empty(self):
        for payload in ({}, {"data": None}, {"data": {"instId": BTC}}):
            with self.subTest(payload=payload), \
                 patch.object(main, "okx_private_request", new=AsyncMock(return_value=payload)):
                with self.assertRaises(HTTPException) as context:
                    await main._account_swap_instruments()
                self.assertEqual(context.exception.status_code, 502)

    async def test_spec_comes_from_current_account_contract(self):
        private = AsyncMock(return_value={"data": [instrument_row()]})
        public = AsyncMock(side_effect=AssertionError("Unexpected public specification read"))
        with patch.object(main, "okx_private_request", new=private), patch.object(main, "okx_get", new=public):
            spec = await main._instrument_spec(BTC)
        self.assertEqual(spec.instrumentID, BTC)
        self.assertEqual(spec.ctVal, .01)
        self.assertEqual(spec.lotSize, .01)
        self.assertEqual(spec.tickSize, .1)
        private.assert_awaited_once_with("GET", "/account/instruments", params={"instType": "SWAP"})
        public.assert_not_awaited()

    async def test_public_price_does_not_authorize_unsupported_account_contract(self):
        public = AsyncMock(return_value={"data": [{"instId": NEIRO, "last": "0.1"}]})
        with patch.object(main, "okx_private_request", new=AsyncMock(return_value={"data": [instrument_row()]})), \
             patch.object(main, "okx_get", new=public), patch.object(main, "OKX_DEMO", True):
            with self.assertRaises(HTTPException) as context:
                await main._instrument_spec(NEIRO)
        self.assertEqual(context.exception.status_code, 422)
        self.assertIn(NEIRO, context.exception.detail)
        self.assertIn("模拟盘", context.exception.detail)
        self.assertIn("未提交订单", context.exception.detail)
        public.assert_not_awaited()

    async def test_availability_retains_supported_and_unsupported_observed_contracts(self):
        for demo, label in ((True, "模拟盘"), (False, "实盘账户")):
            with self.subTest(demo=demo), patch.object(main, "OKX_DEMO", demo), \
                 patch.object(main, "_account_swap_instruments", new=AsyncMock(return_value={BTC: instrument_row()})):
                availability = await main._ai_trading_availability([BTC, NEIRO])
            self.assertEqual(list(availability), [BTC, NEIRO])
            self.assertIs(availability[BTC]["available"], True)
            self.assertIs(availability[NEIRO]["available"], False)
            self.assertIn(label, availability[NEIRO]["reason"])

    async def test_failed_account_read_marks_every_contract_unknown(self):
        for error in (exchange_error("51001"), httpx.ReadTimeout("Account read timed out")):
            with self.subTest(error=type(error).__name__), \
                 patch.object(main, "_account_swap_instruments", new=AsyncMock(side_effect=error)):
                availability = await main._ai_trading_availability([BTC, NEIRO])
            self.assertEqual(list(availability), [BTC, NEIRO])
            for item in availability.values():
                self.assertIsNone(item["available"])
                self.assertIn("无法核验", item["reason"])

    async def test_snapshot_preserves_market_data_when_execution_is_blocked_or_unknown(self):
        instruments = [BTC, NEIRO, "UNKNOWN-USDT-SWAP"]
        config = AIConfig(allowedInstruments=tuple(instruments))
        availability = {BTC: {"available": True, "reason": ""},
                        NEIRO: {"available": False, "reason": "当前模拟盘不支持此合约"},
                        instruments[2]: {"available": None, "reason": "无法核验"}}
        candles = [{"timestamp": iso(datetime.now(timezone.utc)), "open": 100, "high": 102,
                    "low": 99, "close": 101, "volume": 10, "confirmed": True}]
        public = {"data": [{"last": "101", "bids": [["100", "1"]], "asks": [["102", "1"]]}]}
        with patch.object(main, "ai_worker", SimpleNamespace(config=config)), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "contracts", new=AsyncMock(return_value=[{"id": item} for item in instruments])), \
             patch.object(main, "market_candles", new=AsyncMock(return_value={"candles": candles})), \
             patch.object(main, "okx_get", new=AsyncMock(return_value=public)), \
             patch.object(main, "account", new=AsyncMock(return_value={"authenticated": True, "todayLossCount": 0})), \
             patch.object(main, "risk", new=AsyncMock(return_value={})), \
             patch.object(main, "_ai_trading_availability", new=AsyncMock(return_value=availability)), \
             patch.object(main, "_get_order_gateway", return_value=SimpleNamespace(daily_order_count=lambda: 0)), \
             patch.object(main._AIDataCollector, "_pace_candles", new=AsyncMock()):
            snapshot = await main._ai_snapshot()
        self.assertEqual(snapshot.observed_instruments(), instruments)
        self.assertEqual([row["id"] for row in snapshot.instruments], instruments)
        self.assertEqual(snapshot.ai["tradingMode"], "demo")
        self.assertEqual(snapshot.ai["tradingAvailability"], availability)
        self.assertEqual(set(snapshot.tickers), set(instruments))
        self.assertEqual(set(snapshot.orderBook), set(instruments))
        for instrument in instruments:
            for interval in ("5m", "15m", "1H", "4H"):
                self.assertEqual(snapshot.candles[f"{instrument}/{interval}"], candles)

    async def test_execution_rechecks_account_universe_after_snapshot_admission(self):
        now = datetime.now(timezone.utc)
        config = AIConfig(enabled=True, mode="demo-active", allowedInstruments=(NEIRO,))
        snapshot = AISnapshot(
            snapshotId="previously-tradable", capturedAt=iso(now), instruments=[{"id": NEIRO}],
            account={"availableEquityUSD": 1000, "todayLossCount": 0},
            ai={"tradingMode": "demo", "tradingAvailability": {NEIRO: {"available": True, "reason": ""}}},
            dataFreshness={"maxAgeSeconds": 90},
        )
        decision = AIDecision.from_dict({
            "schemaVersion": 1, "decisionId": "account-changed", "snapshotId": snapshot.snapshotId,
            "action": "open", "instrumentID": NEIRO, "direction": "long", "orderType": "limit",
            "limitPrice": .1, "stopLossPrice": .09, "takeProfitPrice": .12, "leverage": 1,
            "winRate": .5, "riskRewardRatio": 2, "confidence": .9,
            "validUntil": iso(now + timedelta(minutes=1)), "reasonCode": "test", "reason": "test",
        })
        admitted = validate_decision(decision, snapshot, config, now=now)
        self.assertTrue(admitted.accepted, admitted.reason)
        current_universe = AsyncMock(return_value={BTC: instrument_row()})
        ticker = AsyncMock(side_effect=AssertionError("Rejected instrument must stop before price reads"))
        private = AsyncMock(side_effect=AssertionError("Rejected instrument must never write an order"))
        with patch.object(main, "ai_worker", SimpleNamespace(config=config)), \
             patch.object(main, "OKX_DEMO", True), patch.object(main, "read_state", return_value={}), \
             patch.object(main, "_account_swap_instruments", new=current_universe), \
             patch.object(main, "_ticker_last", new=ticker), \
             patch.object(main, "_get_order_gateway") as gateway_factory, \
             patch.object(main, "okx_private_request", new=private):
            with self.assertRaises(HTTPException) as context:
                await main._ai_execute(admitted.decision, snapshot)
        self.assertEqual(context.exception.status_code, 422)
        self.assertIn(NEIRO, context.exception.detail)
        current_universe.assert_awaited_once_with()
        ticker.assert_not_awaited()
        gateway_factory.assert_not_called()
        private.assert_not_awaited()

    async def test_execution_rejects_open_when_current_position_exists(self):
        now = datetime.now(timezone.utc)
        config = AIConfig(enabled=True, mode="demo-active", allowedInstruments=(BTC,), maxLeverage=5)
        snapshot = AISnapshot(
            snapshotId="position-gate", capturedAt=iso(now), instruments=[{"id": BTC}],
            account={"authenticated": True, "pendingOrdersKnown": True,
                     "availableEquityUSD": 1000, "todayLossCount": 0,
                     "positions": [], "pendingOrders": []},
            dataFreshness={"maxAgeSeconds": 90},
        )
        decision = AIDecision(
            1, "position-gate-decision", snapshot.snapshotId, "open", instrumentID=BTC,
            direction="long", orderType="market", stopLossPrice=90, takeProfitPrice=120,
            leverage=2, winRate=.5, riskRewardRatio=2, confidence=.9,
            validUntil=iso(now + timedelta(minutes=1)),
        )
        refreshed_account = {
            "authenticated": True, "pendingOrdersKnown": True,
            "availableEquityUSD": 1000, "todayLossCount": 0,
            "positions": [{"instrumentID": BTC, "quantity": 1, "side": "long"}],
            "pendingOrders": [],
        }
        with patch.object(main, "ai_worker", SimpleNamespace(config=config)), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "account", new=AsyncMock(return_value=refreshed_account)), \
             patch.object(main, "risk", new=AsyncMock(return_value={
                 "killSwitch": False, "dailyPnLPercent": 0,
                 "dataQuality": {"equitySource": "okx"},
             })), \
             patch.object(main, "_instrument_spec", new=AsyncMock(side_effect=AssertionError("position gate must run first"))), \
             patch.object(main, "_get_order_gateway") as gateway_factory:
            with self.assertRaises(HTTPException) as context:
                await main._ai_execute(decision, snapshot)
        self.assertEqual(context.exception.status_code, 409)
        self.assertIn("already has a position", context.exception.detail)
        gateway_factory.assert_not_called()

    async def test_execution_rejects_open_when_current_pending_order_exists(self):
        now = datetime.now(timezone.utc)
        config = AIConfig(enabled=True, mode="demo-active", allowedInstruments=(BTC,), maxLeverage=5)
        snapshot = AISnapshot(
            snapshotId="pending-gate", capturedAt=iso(now), instruments=[{"id": BTC}],
            account={"authenticated": True, "pendingOrdersKnown": True,
                     "availableEquityUSD": 1000, "todayLossCount": 0,
                     "positions": [], "pendingOrders": []},
            dataFreshness={"maxAgeSeconds": 90},
        )
        decision = AIDecision(
            1, "pending-gate-decision", snapshot.snapshotId, "open", instrumentID=BTC,
            direction="short", orderType="limit", limitPrice=100, stopLossPrice=110,
            takeProfitPrice=80, leverage=2, winRate=.5, riskRewardRatio=2, confidence=.9,
            validUntil=iso(now + timedelta(minutes=1)),
        )
        refreshed_account = {
            "authenticated": True, "pendingOrdersKnown": True,
            "availableEquityUSD": 1000, "todayLossCount": 0,
            "positions": [],
            "pendingOrders": [{"instrumentID": BTC, "id": "pending-1", "status": "live", "quantity": 1}],
        }
        with patch.object(main, "ai_worker", SimpleNamespace(config=config)), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "account", new=AsyncMock(return_value=refreshed_account)), \
             patch.object(main, "risk", new=AsyncMock(return_value={
                 "killSwitch": False, "dailyPnLPercent": 0,
                 "dataQuality": {"equitySource": "okx"},
             })), \
             patch.object(main, "_instrument_spec", new=AsyncMock(side_effect=AssertionError("pending gate must run first"))), \
             patch.object(main, "_get_order_gateway") as gateway_factory:
            with self.assertRaises(HTTPException) as context:
                await main._ai_execute(decision, snapshot)
        self.assertEqual(context.exception.status_code, 409)
        self.assertIn("pending order", context.exception.detail)
        gateway_factory.assert_not_called()

    async def test_execution_refreshes_authenticated_account_and_risk_before_entry(self):
        now = datetime.now(timezone.utc)
        config = AIConfig(enabled=True, mode="demo-active", allowedInstruments=(BTC,), maxLeverage=5)
        snapshot = AISnapshot(
            snapshotId="refresh-before-entry", capturedAt=iso(now), instruments=[{"id": BTC}],
            account={
                "authenticated": True, "pendingOrdersKnown": True,
                "availableEquityUSD": 1000, "todayLossCount": 0,
                "positions": [], "pendingOrders": [],
            },
            dataFreshness={"maxAgeSeconds": 90},
        )
        decision = AIDecision(
            1, "refresh-entry", snapshot.snapshotId, "open", instrumentID=BTC,
            direction="long", orderType="market", stopLossPrice=90, takeProfitPrice=120,
            leverage=2, winRate=.5, riskRewardRatio=2, confidence=.9,
            validUntil=iso(now + timedelta(minutes=1)),
        )

        class Gateway:
            async def submit_intent(self, request, **kwargs):
                return {"orderID": "refreshed-order"}

        refreshed_account = {
            "authenticated": True, "pendingOrdersKnown": True,
            "availableEquityUSD": 900, "todayLossCount": 0,
            "positions": [], "pendingOrders": [],
        }
        refreshed_risk = {
            "killSwitch": False, "dailyPnLPercent": 0,
            "dataQuality": {
                "equitySource": "okx",
                "accountRefreshError": None,
                "accountRefreshRetryable": False,
            },
        }
        account_refresh = AsyncMock(return_value=refreshed_account)
        risk_refresh = AsyncMock(return_value=refreshed_risk)
        with patch.object(main, "ai_worker", SimpleNamespace(config=config)), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "account", new=account_refresh), \
             patch.object(main, "risk", new=risk_refresh), \
             patch.object(main, "_instrument_spec", new=AsyncMock(return_value=InstrumentSpec(BTC, ctVal=1))), \
             patch.object(main, "_ticker_last", new=AsyncMock(return_value=100)), \
             patch.object(main, "_get_order_gateway", return_value=Gateway()):
            result = await main._ai_execute(decision, snapshot)

        self.assertEqual(result["orderID"], "refreshed-order")
        account_refresh.assert_awaited_once_with()
        risk_refresh.assert_awaited_once_with()

    async def test_ai_entries_use_isolated_and_market_close_remains_reduce_only(self):
        now = datetime.now(timezone.utc)
        snapshot = AISnapshot(
            snapshotId="margin-mode-snapshot", capturedAt=iso(now),
            instruments=[{"id": BTC}],
            account={
                "availableEquityUSD": 1000, "todayLossCount": 0,
                "positions": [], "pendingOrders": [],
            },
            dataFreshness={"maxAgeSeconds": 90},
        )
        entry = AIDecision(
            1, "isolated-entry", snapshot.snapshotId, "open", instrumentID=BTC,
            direction="long", orderType="market", stopLossPrice=90, takeProfitPrice=120,
            leverage=2, winRate=.5, riskRewardRatio=2, confidence=.9,
            validUntil=iso(now + timedelta(minutes=1)),
        )
        close = AIDecision(
            1, "early-close", snapshot.snapshotId, "close", instrumentID=BTC,
            direction="long", orderType="market", validUntil=iso(now + timedelta(minutes=1)),
        )

        class Gateway:
            def __init__(self):
                self.requests = []

            async def submit_intent(self, request, **kwargs):
                self.requests.append(request)
                return {"orderID": "test-order"}

        gateway = Gateway()
        with patch.object(main, "ai_worker", SimpleNamespace(config=AIConfig(enabled=True, mode="demo-active", maxLeverage=5))), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "_instrument_spec", new=AsyncMock(return_value=InstrumentSpec(BTC, ctVal=1))), \
             patch.object(main, "_ticker_last", new=AsyncMock(return_value=100)), \
             patch.object(main, "_get_order_gateway", return_value=gateway):
            await main._ai_execute(entry, snapshot)
            close_snapshot = replace(snapshot, account={
                **snapshot.account,
                "positions": [{"instrumentID": BTC, "quantity": 2, "marginMode": "isolated"}],
            })
            await main._ai_execute(close, close_snapshot)

        self.assertEqual(gateway.requests[0]["marginMode"], "isolated")
        self.assertFalse(gateway.requests[0]["reduceOnly"])
        self.assertEqual(gateway.requests[1]["marginMode"], "isolated")
        self.assertTrue(gateway.requests[1]["reduceOnly"])
        self.assertEqual(gateway.requests[1]["quantity"], 2)


class AIExecutionPolicyTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.config = AIConfig(enabled=True, mode="demo-active", allowedInstruments=(BTC, NEIRO))

    def snapshot(self, ai=None):
        return AISnapshot(snapshotId="execution-snapshot", capturedAt=iso(self.now),
                          instruments=[{"id": BTC}, {"id": NEIRO}],
                          account={"availableEquityUSD": 1000, "todayLossCount": 0},
                          ai={} if ai is None else ai, dataFreshness={"maxAgeSeconds": 90})

    def decision(self, **overrides):
        return AIDecision.from_dict({
            "schemaVersion": 1, "decisionId": "execution-decision", "snapshotId": "execution-snapshot",
            "action": "open", "instrumentID": BTC, "direction": "long", "orderType": "limit",
            "limitPrice": 100, "stopLossPrice": 90, "takeProfitPrice": 120, "leverage": 1,
            "winRate": .5, "riskRewardRatio": 2, "confidence": .9,
            "validUntil": iso(self.now + timedelta(minutes=1)), "reasonCode": "test", "reason": "test",
            **overrides,
        })

    def assessment(self, instrument, **overrides):
        return {"instrumentID": instrument, "direction": "long", "winRate": .5,
                "riskRewardRatio": 2, "limitPrice": 100, "stopLossPrice": 90, "takeProfitPrice": 120,
                "confidence": .9, "entryEligible": False, "unmetConditions": ["等待确认"],
                "reason": "1H 回踩支撑，等待确认", **overrides}

    def validate(self, decision, snapshot):
        return validate_decision(decision, snapshot, self.config, now=self.now)

    def test_only_verified_true_authorizes_open(self):
        availability = {BTC: {"available": True, "reason": ""}, NEIRO: {"available": False, "reason": "不支持"}}
        result = self.validate(self.decision(), self.snapshot({"tradingAvailability": availability}))
        self.assertTrue(result.accepted, result.reason)
        self.assertEqual(result.decision.action, "open")

    def test_false_unknown_and_truthy_values_cannot_authorize_open(self):
        for value in (False, None, 1, "true", {}, []):
            with self.subTest(available=value):
                snapshot = self.snapshot({"tradingAvailability": {BTC: {"available": value, "reason": "账户不可交易"}}})
                result = self.validate(self.decision(), snapshot)
                self.assertFalse(result.accepted)
                self.assertEqual(result.decision.action, "hold")
                self.assertIn(BTC, result.reason)
                self.assertIn("账户不可交易", result.reason)

    def test_missing_contract_and_malformed_availability_fail_closed(self):
        for availability in ({}, {NEIRO: {"available": True}}, {BTC: None}, {BTC: {}}, [], "true"):
            with self.subTest(availability=availability):
                result = self.validate(self.decision(), self.snapshot({"tradingAvailability": availability}))
                self.assertFalse(result.accepted)
                self.assertEqual(result.decision.action, "hold")
                self.assertIn(BTC, result.reason)

    def test_legacy_snapshot_without_execution_map_remains_compatible(self):
        self.assertTrue(self.validate(self.decision(), self.snapshot()).accepted)

    def test_unavailable_entry_does_not_block_hold_close_or_cancel(self):
        for value in (False, None):
            for action in ("hold", "close", "cancel"):
                with self.subTest(available=value, action=action):
                    snapshot = self.snapshot({"tradingAvailability": {BTC: {"available": value}}})
                    decision = self.decision(action=action, orderID="existing-order" if action == "cancel" else None)
                    result = self.validate(decision, snapshot)
                    self.assertTrue(result.accepted, result.reason)
                    self.assertEqual(result.decision.action, action)

    def test_rejected_open_retains_every_contract_technical_plan(self):
        rows = [self.assessment(BTC, entryEligible=True, unmetConditions=[]), self.assessment(NEIRO)]
        decision = self.decision(assessments=rows)
        snapshot = self.snapshot({"tradingAvailability": {BTC: {"available": False, "reason": "模拟盘不支持"}}})
        result = self.validate(decision, snapshot)
        self.assertFalse(result.accepted)
        self.assertEqual(result.decision.to_dict()["assessments"], rows)
        self.assertEqual(result.decision.action, "hold")

    def test_analysis_and_coordinator_receive_current_account_gate(self):
        availability = {BTC: {"available": True, "reason": ""}, NEIRO: {"available": False, "reason": "模拟盘不支持"}}
        snapshot = self.snapshot({"tradingMode": "demo", "tradingAvailability": availability})
        prompts = [(CodexRunner._decision_prompt(snapshot, self.config, now=self.now),
                    "SERVER ENTRY GATES (authoritative):\n"),
                   (CodexRunner._coordinator_prompt(snapshot, self.config, []), "SERVER ENTRY GATES:\n")]
        for prompt, marker in prompts:
            gates = json.loads(prompt.split(marker, 1)[1].split("\n", 1)[0])
            self.assertEqual(gates["tradingMode"], "demo")
            self.assertEqual(gates["tradingAvailability"], availability)
            self.assertTrue(gates["tradingAvailability"][BTC]["available"])
            self.assertFalse(gates["tradingAvailability"][NEIRO]["available"])
        self.assertIn("Preserve the fixed observed contract pool", prompts[0][0])
        group = CodexRunner._group_snapshot(snapshot, [NEIRO])
        self.assertEqual(group.observed_instruments(), [NEIRO])
        self.assertEqual(CodexRunner._entry_gates(group, self.config)["tradingAvailability"][NEIRO], availability[NEIRO])


class OrderSubmissionOutcomeTests(NoNetworkTests):
    def setUp(self):
        super().setUp()
        guard = patch.object(main, "okx_get", new=AsyncMock(side_effect=self.public_prices))
        guard.start()
        self.addCleanup(guard.stop)

    async def public_prices(self, route, params):
        timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        rows = {
            "/market/ticker": {"instId": BTC, "last": "100", "ts": timestamp},
            "/market/books": {"bids": [["99.99", "100"]], "asks": [["100.01", "100"]], "ts": timestamp},
            "/public/mark-price": {"instId": BTC, "markPx": "100", "ts": timestamp},
        }
        return {"data": [rows[route]]}

    def gateway(self, path, *, lookup=None):
        async def submit(payload, demo):
            return await main.submit_order(payload, demo=demo)

        return OrderGateway(path, submit=submit, lookup=lookup)

    async def submit(self, gateway):
        return await gateway.submit_intent(entry_request(), demo=True, instrument=InstrumentSpec(BTC, ctVal=1),
                                           price=100, available_equity=1000, daily_order_limit=2)

    async def test_leverage_failure_is_definitely_unsubmitted_and_never_posts_order(self):
        for error in (exchange_error("51001", "Unknown contract"), httpx.ReadTimeout("Leverage timed out")):
            writes = AsyncMock(side_effect=[{"data": []}, {"data": []}, error])
            with self.subTest(error=type(error).__name__), patch.object(main, "private_ready", return_value=True), \
                 patch.object(main, "OKX_DEMO", True), patch.object(main, "okx_private_request", new=writes):
                with self.assertRaises(OrderNotSubmittedError) as context:
                    await main.submit_order(entry_request(), demo=True)
            self.assertIs(context.exception.__cause__, error)
            self.assertIn("订单未提交", str(context.exception))
            self.assertEqual([call.args[1] for call in writes.await_args_list], ["/account/positions", "/trade/orders-pending", "/account/set-leverage"])

    async def test_ai_entry_cannot_change_leverage_with_existing_cross_exposure(self):
        writes = AsyncMock(side_effect=[
            {"data": [{"instId": BTC, "pos": "1.75", "lever": "3"}]},
            {"data": []},
        ])
        with patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), patch.object(main, "okx_private_request", new=writes):
            with self.assertRaises(OrderNotSubmittedError) as context:
                await main.submit_order(entry_request(), demo=True)
        self.assertIn("已有持仓", str(context.exception))
        self.assertEqual([call.args[1] for call in writes.await_args_list], ["/account/positions", "/trade/orders-pending"])

    async def test_leverage_failure_releases_new_durable_reservation_and_daily_allowance(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "okx_private_request", new=AsyncMock(side_effect=[{"data": []}, {"data": []}, httpx.ReadTimeout("Leverage timed out")])):
            path = Path(directory) / "orders.json"
            gateway = self.gateway(path)
            with self.assertRaises(OrderNotSubmittedError):
                await self.submit(gateway)
            self.assertEqual(gateway.reservations, {})
            self.assertEqual(gateway.daily_order_count(), 0)
            restored = self.gateway(path)
            self.assertEqual(restored.reservations, {})
            self.assertEqual(restored.daily_order_count(), 0)
            with patch.object(main, "okx_private_request", new=AsyncMock(return_value={"data": [{"ordId": "new-order"}]})):
                result = await self.submit(restored)
            self.assertEqual(result["orderID"], "new-order")
            self.assertEqual(restored.daily_order_count(), 1)

    async def test_order_post_timeout_remains_unknown_and_counts_after_restart(self):
        timeout = httpx.ReadTimeout("Order result unknown")
        writes = AsyncMock(side_effect=[
            {"data": []}, {"data": []}, {"data": [{}]},
            {"data": []}, {"data": []}, timeout,
        ])
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), patch.object(main, "okx_private_request", new=writes):
            path = Path(directory) / "orders.json"
            gateway = self.gateway(path)
            with self.assertRaises(httpx.ReadTimeout) as context:
                await self.submit(gateway)
            self.assertIs(context.exception, timeout)
            self.assertEqual([call.args[1] for call in writes.await_args_list], [
                "/account/positions", "/trade/orders-pending", "/account/set-leverage",
                "/account/positions", "/trade/orders-pending", "/trade/order",
            ])
            reservation = gateway.reservations["unresolved-aiexecution1"]
            self.assertFalse(reservation["inFlight"])
            self.assertEqual(gateway.daily_order_count(), 1)
            restored = self.gateway(path)
            self.assertEqual(restored.reservations, gateway.reservations)
            self.assertEqual(restored.daily_order_count(), 1)

    async def test_failed_retry_preserves_previous_unknown_reservation(self):
        writes = AsyncMock(side_effect=[
            {"data": []}, {"data": []}, {"data": [{}]},
            {"data": []}, {"data": []}, httpx.ReadTimeout("Order result unknown"),
        ])
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), patch.object(main, "okx_private_request", new=writes):
            path = Path(directory) / "orders.json"
            gateway = self.gateway(path)
            with self.assertRaises(httpx.ReadTimeout):
                await self.submit(gateway)
            previous = deepcopy(gateway.reservations)
            with patch.object(main, "okx_private_request", new=AsyncMock(side_effect=exchange_error("51001"))):
                with self.assertRaises(OrderGatewayError):
                    await self.submit(gateway)
            self.assertEqual(gateway.reservations, previous)
            self.assertEqual(gateway.daily_order_count(), 1)
            self.assertEqual(self.gateway(path).reservations, previous)

    async def test_lookup_only_typed_absent_code_releases_unknown_result(self):
        for code, detail, absent in (("51603", "Order absent", True),
                                     ("51001", "Unknown contract", True),
                                     ("51001", "51603 appears only in message", True),
                                     ("", "51603 appears only in message", False)):
            error = exchange_error(code, detail)
            private = AsyncMock(side_effect=error)
            with self.subTest(code=code, detail=detail), patch.object(main, "okx_private_request", new=private):
                if absent:
                    self.assertIsNone(await main._lookup_order(BTC, "aiexecution1", True))
                else:
                    with self.assertRaises(HTTPException) as context:
                        await main._lookup_order(BTC, "aiexecution1", True)
                    self.assertIs(context.exception, error)
            private.assert_awaited_once_with("GET", "/trade/order", params={"instId": BTC, "clOrdId": "aiexecution1"})

    async def test_lookup_maps_existing_order(self):
        private = AsyncMock(return_value={"data": [{"ordId": "known-order", "clOrdId": "aiexecution1", "state": "live"}]})
        with patch.object(main, "okx_private_request", new=private):
            result = await main._lookup_order(BTC, "aiexecution1", True)
        self.assertEqual(result, {"orderID": "known-order", "clientOrderID": "aiexecution1", "status": "live"})

    async def test_success_response_without_verifiable_order_is_not_explicit_absence(self):
        for payload in unverifiable_order_responses():
            private = AsyncMock(return_value=payload)
            with self.subTest(payload=payload), patch.object(main, "okx_private_request", new=private):
                with self.assertRaises(HTTPException) as context:
                    await main._lookup_order(BTC, "aiexecution1", True)
            self.assertEqual(context.exception.status_code, 502)
            self.assertIn("no verifiable order result", context.exception.detail)
            private.assert_awaited_once_with("GET", "/trade/order", params={"instId": BTC, "clOrdId": "aiexecution1"})

    async def test_unverifiable_success_keeps_durable_unknown_reservation_during_reconciliation(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "okx_private_request", new=AsyncMock(side_effect=[
                 {"data": []}, {"data": []}, {"data": [{}]},
                 {"data": []}, {"data": []}, httpx.ReadTimeout("unknown"),
             ])):
            path = Path(directory) / "orders.json"
            gateway = self.gateway(path, lookup=main._lookup_order)
            with self.assertRaises(httpx.ReadTimeout):
                await self.submit(gateway)
            previous = deepcopy(gateway.reservations)
            for payload in unverifiable_order_responses():
                with self.subTest(payload=payload), \
                     patch.object(main, "okx_private_request", new=AsyncMock(return_value=payload)):
                    with self.assertRaises(HTTPException) as context:
                        await gateway.reconcile(demo=True)
                self.assertEqual(context.exception.status_code, 502)
                self.assertEqual(gateway.reservations, previous)
                self.assertEqual(gateway.daily_order_count(), 1)
                restored = self.gateway(path, lookup=main._lookup_order)
                self.assertEqual(restored.reservations, previous)
                self.assertEqual(restored.daily_order_count(), 1)

    async def test_reconcile_releases_unknown_contract_and_explicit_absence(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "okx_private_request", new=AsyncMock(side_effect=[
                 {"data": []}, {"data": []}, {"data": [{}]},
                 {"data": []}, {"data": []}, httpx.ReadTimeout("unknown"),
             ])):
            path = Path(directory) / "orders.json"
            gateway = self.gateway(path, lookup=main._lookup_order)
            with self.assertRaises(httpx.ReadTimeout):
                await self.submit(gateway)
            previous = deepcopy(gateway.reservations)
            with patch.object(main, "okx_private_request", new=AsyncMock(side_effect=exchange_error("51001"))):
                await gateway.reconcile(demo=True)
            self.assertEqual(gateway.reservations, {})
            self.assertEqual(gateway.daily_order_count(), 0)
            self.assertEqual(self.gateway(path).reservations, {})


if __name__ == "__main__":
    unittest.main()
