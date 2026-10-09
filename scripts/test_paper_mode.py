#!/usr/bin/env python3
"""Verify the production API routes local paper orders without private calls."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
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
from backend import main
from backend.ai_schema import AIConfig, AIDecision, AISnapshot
from backend.paper_trading import PaperTradingAccount
from backend.order_gateway import OrderNotSubmittedError

BTC = "BTC-USDT-SWAP"
SPEC = {"instId": BTC, "state": "live", "settleCcy": "USDT", "ctType": "linear",
        "ctVal": "0.01", "ctMult": "1", "lotSz": "0.01", "minSz": "0.01", "tickSz": "0.1"}


class PaperModeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name)
        self.broker = PaperTradingAccount(self.path / "paper-account.json")
        self.quote = {"instId": BTC, "last": "100", "bidPx": "99.9", "askPx": "100.1"}
        self.worker = SimpleNamespace(config=AIConfig(enabled=False, mode="disabled", allowedInstruments=(BTC,)),
                                      disable=AsyncMock(), start=AsyncMock())
        self.worker.update_config = self.update_config
        self.worker.get_status = lambda: {"mode": self.worker.config.mode, "enabled": self.worker.config.enabled}
        self.private = AsyncMock(side_effect=AssertionError("paper must never call private OKX APIs"))
        patches = [patch.object(main, "TRADING_MODE", "paper"),
                   patch.object(main, "paper_account", self.broker),
                   patch.object(main, "state_dir", return_value=self.path),
                   patch.object(main, "paper_instrument_cache", None),
                   patch.object(main, "paper_quote_lock", asyncio.Lock()),
                   patch.object(main, "position_protection_lock", asyncio.Lock()),
                   patch.object(main, "ai_workers", {"codex": self.worker}),
                   patch.object(main, "ai_worker", self.worker),
                   patch.object(main, "okx_get", new=AsyncMock(side_effect=self.public)),
                   patch.object(main, "okx_private_request", new=self.private),
                   patch.object(main, "_get_order_gateway", side_effect=AssertionError("exchange gateway in paper mode")),
                   patch.object(main.OKX_HTTP_CLIENT, "get", new=AsyncMock(side_effect=AssertionError("unmocked network"))),
                   patch.object(main.OKX_HTTP_CLIENT, "request", new=AsyncMock(side_effect=AssertionError("unmocked network")))]
        self.private_patch = patches[9]
        for guard in patches:
            guard.start()
            self.addCleanup(guard.stop)

    def update_config(self, values):
        self.worker.config = AIConfig.from_dict({**self.worker.config.to_dict(), **values})
        return self.worker.config.to_dict()

    async def public(self, route, params):
        timestamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        if route == "/public/instruments":
            return {"data": [SPEC, {**SPEC, "instId": "BAD-USDT-SWAP", "state": "suspend"}]}
        if route in {"/market/ticker", "/market/tickers"}:
            return {"data": [{**self.quote, "ts": timestamp}]}
        if route == "/market/books":
            return {"data": [{"bids": [[self.quote["bidPx"], "10000"]],
                              "asks": [[self.quote["askPx"], "10000"]], "ts": timestamp}]}
        if route == "/public/mark-price":
            return {"data": [{"instId": BTC, "markPx": self.quote["last"], "ts": timestamp}]}
        raise AssertionError(route)

    async def entry(self, **overrides):
        return await main.create_paper_order({"instrumentID": BTC, "side": "long", "quantity": 10,
                                              "leverage": 2, "clientOrderID": "entry-1", **overrides})

    async def test_empty_account_and_routes_ignore_existing_private_credentials(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1:8787",
                                    headers={"Authorization": "Bearer " + main.token()}) as client:
            account = (await client.get("/api/v1/account")).json()
            self.assertEqual((account["mode"], account["profile"], account["equityUSD"]), ("paper", "local-paper", 5000))
            self.assertEqual([asset["currency"] for asset in account["assets"]], ["USDT"])
            for route in ("positions", "orders", "paper/orders", "paper/fills"):
                response = await client.get("/api/v1/" + route)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json(), [])
            self.assertEqual((await client.get("/api/v1/risk")).json()["dataQuality"]["equitySource"], "paper")
        self.private.assert_not_awaited()

    async def test_market_order_fills_locally_and_survives_reload(self):
        result = await self.entry()
        self.assertEqual(result["status"], "filled")
        self.assertGreater(result["fillPrice"], 100.1)
        self.assertEqual(len(await main.paper_fills()), 1)
        loaded = PaperTradingAccount(self.path / "paper-account.json")
        self.assertEqual(loaded.positions(), self.broker.positions())
        self.assertLess(loaded.account_snapshot()["equityUSD"], 5000)
        self.private.assert_not_awaited()

    async def test_limit_matches_a_later_public_quote_and_flatten_disables_workers(self):
        result = await self.entry(orderType="limit", price=90)
        self.assertEqual(result["status"], "live")
        self.assertEqual(len(await main.orders()), 1)
        self.quote.update(last="89", bidPx="88.9", askPx="89.1")
        self.assertEqual(len(await main.positions()), 1)
        flattened = await main.flatten_ai()
        self.assertEqual(flattened["scope"], "account")
        self.assertEqual(len(flattened["closed"]), 1)
        self.assertEqual(await main.positions(), [])
        self.worker.disable.assert_awaited_once()
        self.private.assert_not_awaited()

    async def test_paper_mode_blocks_live_switch_and_legacy_remote_submit(self):
        for call in (main.enable_live_trading, lambda: main.live_order({})):
            with self.assertRaises(HTTPException) as error:
                await call()
            self.assertEqual(error.exception.status_code, 409)
        self.private_patch.stop()
        with self.assertRaises(HTTPException) as error:
            await main.okx_private_request("POST", "/trade/order", body={})
        self.assertEqual(error.exception.status_code, 409)
        with self.assertRaises(OrderNotSubmittedError):
            await main.submit_order({}, demo=False)

    async def test_flatten_cancels_entries_before_refresh_can_match_them(self):
        result = await self.entry(orderType="limit", price=90)
        self.quote.update(last="89", bidPx="88.9", askPx="89.1")
        flattened = await main.flatten_ai()
        self.assertEqual(flattened["cancelledOrderIDs"], [result["id"]])
        self.assertEqual(flattened["closed"], [])
        self.assertEqual(await main.paper_fills(), [])
        self.assertEqual(self.broker.account_snapshot()["equityUSD"], 5000)

    async def test_enabling_ai_uses_paper_mode_without_live_switch(self):
        with patch.object(main, "_ensure_ai_worker", new=AsyncMock(return_value=self.worker)), patch.object(main, "OKX_DEMO", False):
            result = await main.enable_ai()
        self.assertEqual(result["mode"], "paper-active")
        self.worker.start.assert_awaited_once()
        for mode in ("demo-active", "live-armed"):
            with self.assertRaises(HTTPException):
                main._check_ai_run_mode({"mode": mode})
        main._check_ai_run_mode({"mode": "shadow"})

    async def test_ai_entry_and_close_use_local_account_and_keep_contract_sizing(self):
        self.update_config({"enabled": True, "mode": "paper-active"})
        self.quote.update(bidPx="100", askPx="100.1")
        now = datetime.now(timezone.utc)
        iso = lambda value: value.isoformat(timespec="seconds").replace("+00:00", "Z")
        snapshot = AISnapshot(snapshotId="paper-test", capturedAt=iso(now), instruments=[{"id": BTC}],
                              tickers={BTC: dict(self.quote)},
                              account=await main.account(), risk=await main.risk(),
                              ai={"tradingMode": "paper"}, dataFreshness={"maxAgeSeconds": 90})
        decision = AIDecision.from_dict({"schemaVersion": 1, "decisionId": "entry", "snapshotId": snapshot.snapshotId,
                                        "action": "open", "instrumentID": BTC, "direction": "long", "orderType": "limit", "limitPrice": 99,
                                        "stopLossPrice": 90, "takeProfitPrice": 130, "leverage": 2,
                                        "winRate": .7, "riskRewardRatio": 3, "confidence": .9,
                                        "validUntil": iso(now + timedelta(minutes=1)), "reasonCode": "test", "reason": "paper integration test"})
        result = await main._ai_execute(decision, snapshot)
        self.assertEqual(result["executionMode"], "paper")
        self.assertLessEqual(result["marginUSD"], self.worker.config.marginPerOrderUSD)
        self.quote.update(last="98", bidPx="97.9", askPx="98.1")
        await main.positions()
        self.assertEqual(len(self.broker.positions()), 1)
        close = AIDecision.from_dict({**decision.to_dict(), "action": "close", "decisionId": "exit",
                                      "orderType": "market", "limitPrice": None})
        await main._ai_execute(close, snapshot)
        self.assertEqual(self.broker.positions(), [])
        self.assertEqual(self.broker.daily_order_count(), 1)
        self.private.assert_not_awaited()

    async def test_startup_starts_local_matcher_without_exchange_reconciliation(self):
        with patch.object(main, "_ensure_ai_worker", new=AsyncMock(return_value=self.worker)), \
             patch.object(main, "_reconcile_order_gateway_once", new=AsyncMock(side_effect=AssertionError("exchange reconciliation"))), \
             patch.object(main, "native_exit_task", None), patch.object(main, "paper_market_task", None):
            await main.start_ai_worker()
            self.assertIsNone(main.native_exit_task)
            self.assertIsNotNone(main.paper_market_task)
            main.paper_market_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await main.paper_market_task

    async def test_hold_restores_missing_protection_without_replacing_existing_side(self):
        await self.entry(stopLossTriggerPrice=90)
        now = datetime.now(timezone.utc)
        snapshot = AISnapshot(snapshotId="protection-test", capturedAt=main.now_iso(), instruments=[{"id": BTC}],
                              account=await main.account(), risk=await main.risk())
        def hold(stop, target):
            return AIDecision.from_dict({
                "schemaVersion": 1, "decisionId": "protection", "snapshotId": snapshot.snapshotId,
                "action": "hold", "confidence": .95, "winRate": None, "riskRewardRatio": None,
                "validUntil": (now + timedelta(minutes=1)).isoformat(timespec="seconds").replace("+00:00", "Z"),
                "reasonCode": "test", "reason": "继续持有并补全已有持仓保护", "assessments": [{
                    "instrumentID": BTC, "direction": "long", "winRate": .7, "riskRewardRatio": 2,
                    "limitPrice": 100, "stopLossPrice": stop, "takeProfitPrice": target,
                    "confidence": .95, "entryEligible": False, "unmetConditions": ["已有持仓"],
                    "reason": "补全保护，保留原先有效的退出计划"}]})
        await main._ai_execute(hold(95, 120), snapshot)
        position = self.broker.positions()[0]
        self.assertEqual((position["stopLossPrice"], position["takeProfitPrice"]), (90, 120))
        await self.broker.cancel_protection(position["id"])
        await self.broker.update_protection(position["id"], take_profit=120)
        await main._ai_execute(hold(95, 130), snapshot)
        position = self.broker.positions()[0]
        self.assertEqual((position["stopLossPrice"], position["takeProfitPrice"]), (95, 120))
        self.private.assert_not_awaited()

    async def test_removed_ai_strategy_routes_cannot_recreate_runtime_state(self):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://127.0.0.1:8787",
                                    headers={"Authorization": "Bearer " + main.token()}) as client:
            prefix = "/api/v1/ai/strategies/deepseek/"
            for resource in ("status", "config", "decisions", "audit"):
                self.assertEqual((await client.get(prefix + resource)).status_code, 404)
            for resource in ("enable", "disable", "flatten"):
                self.assertEqual((await client.post(prefix + resource)).status_code, 404)
            self.assertEqual((await client.patch(prefix + "config", json={"enabled": True})).status_code, 404)
            self.assertEqual((await client.post(prefix + "chat", json={"message": "查询策略状态"})).status_code, 404)
            with patch.object(main, "_ensure_ai_worker", new=AsyncMock(return_value=self.worker)):
                response = await client.patch("/api/v1/ai/config", json={"provider": "deepseek-harness"})
            self.assertEqual(response.status_code, 422)
        self.assertFalse(any("deepseek" in path.name for path in self.path.iterdir()))
        self.assertEqual(self.broker.account_snapshot()["equityUSD"], 5000)
        self.worker.disable.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
