#!/usr/bin/env python3
"""Exercise AI market collection without credentials or exchange requests."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import main  # noqa: E402
from backend.ai_schema import AIDecision, AISnapshot  # noqa: E402
from backend.ai_worker import CodexRunner  # noqa: E402
from backend.order_gateway import InstrumentSpec, OrderGateway, OrderNotSubmittedError  # noqa: E402


class AISnapshotTests(unittest.IsolatedAsyncioTestCase):
    async def test_collector_limits_in_flight_requests(self):
        collector = main._AIDataCollector(concurrency=3, candle_interval=0)
        active = 0
        peak = 0

        async def request():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(0.005)
            active -= 1
            return {"data": []}

        await asyncio.gather(*(collector.fetch(request, resource="ticker") for _ in range(20)))
        self.assertEqual(peak, 3)
        self.assertEqual(collector.errors, [])

    async def test_candle_requests_are_spaced_even_with_concurrency(self):
        collector = main._AIDataCollector(concurrency=8, candle_interval=0.015)
        started = []

        async def request():
            started.append(asyncio.get_running_loop().time())
            return {"candles": []}

        await asyncio.gather(*(collector.fetch(request, resource="candles") for _ in range(12)))
        self.assertEqual(len(started), 12)
        self.assertTrue(all(later - earlier >= 0.014 for earlier, later in zip(started, started[1:])))

    async def test_retry_is_bounded_and_only_for_transient_errors(self):
        limited = HTTPException(502, "Too many requests")
        limited.exchange_code = "50011"
        request = httpx.Request("GET", "https://www.okx.com/api/v5/market/candles")
        throttled = httpx.HTTPStatusError("limited", request=request, response=httpx.Response(429, request=request))
        for error in (limited, throttled, httpx.ReadTimeout("timeout"), httpx.ConnectError("connection")):
            with self.subTest(error=type(error).__name__):
                collector = main._AIDataCollector(candle_interval=0)
                factory = AsyncMock(side_effect=error)
                with patch.object(main.asyncio, "sleep", new=AsyncMock()):
                    row, metadata = await collector.fetch(factory, resource="candles", instrument="BTC-USDT-SWAP", interval="5m")
                self.assertIsNone(row)
                self.assertEqual(factory.await_count, 3)
                self.assertEqual(metadata["attempts"], 3)
                self.assertFalse(metadata["available"])
                self.assertEqual(collector.errors[0]["instrumentID"], "BTC-USDT-SWAP")
                self.assertEqual(collector.errors[0]["interval"], "5m")

        collector = main._AIDataCollector(candle_interval=0)
        factory = AsyncMock(side_effect=HTTPException(422, "Unknown contract"))
        row, metadata = await collector.fetch(factory, resource="ticker")
        self.assertIsNone(row)
        self.assertEqual(factory.await_count, 1)
        self.assertIn("Unknown contract", metadata["error"])

    async def test_retry_success_reports_receipt_time_and_attempts(self):
        collector = main._AIDataCollector(candle_interval=0)
        factory = AsyncMock(side_effect=[httpx.ReadTimeout("timeout"), {"data": [1]}])
        with patch.object(main.asyncio, "sleep", new=AsyncMock()):
            row, metadata = await collector.fetch(factory, resource="ticker")
        self.assertEqual(row, {"data": [1]})
        self.assertEqual(metadata["attempts"], 2)
        self.assertTrue(metadata["available"])
        self.assertIn("receivedAt", metadata)
        self.assertEqual(collector.errors, [])

    async def test_okx_exchange_error_code_survives_http_mapping(self):
        request = httpx.Request("GET", "https://www.okx.com/api/v5/market/candles")
        response = httpx.Response(200, request=request, json={"code": "50011", "msg": "Requests too frequent"})
        with patch.object(main.OKX_HTTP_CLIENT, "get", new=AsyncMock(return_value=response)):
            with self.assertRaises(HTTPException) as context:
                await main.okx_get("/market/candles", {"instId": "BTC-USDT-SWAP"})
        self.assertEqual(context.exception.exchange_code, "50011")
        self.assertEqual(context.exception.status_code, 502)

    async def test_private_rate_limit_is_preserved_without_retrying_posts(self):
        request = httpx.Request("POST", "https://www.okx.com/api/v5/trade/order")
        response = httpx.Response(429, request=request, json={"code": "50011", "msg": "Requests too frequent"})
        call = AsyncMock(return_value=response)
        with patch.object(main, "private_ready", return_value=True), \
             patch.object(main.OKX_HTTP_CLIENT, "request", new=call):
            with self.assertRaises(HTTPException) as context:
                await main.okx_private_request("POST", "/trade/order", body={})
        self.assertEqual(call.await_count, 1)
        self.assertEqual(context.exception.exchange_code, "50011")
        self.assertEqual(context.exception.upstream_status_code, 429)
        self.assertTrue(main._AIDataCollector._retryable(context.exception))

    async def test_transient_partial_account_and_risk_reads_can_recover(self):
        for resource, flag, error in (("account", "dailyBillsRetryable", "dailyBillsError"), ("risk", "accountRefreshRetryable", "accountRefreshError")):
            with self.subTest(resource=resource):
                collector = main._AIDataCollector(candle_interval=0)
                partial = {"dataQuality": {flag: True, error: "OKX request timed out"}}
                complete = {"dataQuality": {flag: False, error: None}}
                request = AsyncMock(side_effect=[partial, partial, complete])
                with patch.object(main.asyncio, "sleep", new=AsyncMock()):
                    result, metadata = await collector.fetch(request, resource=resource)
                self.assertEqual(result, complete)
                self.assertEqual(metadata["attempts"], 3)
                self.assertEqual(collector.errors, [])

    def candle_rows(self):
        return [{
            "id": 1791217200 + index * 300,
            "timestamp": "2026-10-05T16:30:00Z",
            "open": 12345.678901, "high": 12347.678901, "low": 12340.678901, "close": 12346.678901,
            "volume": 1234.12345, "quoteVolume": 123456789.12345, "confirmed": index != 74,
        } for index in range(75)]

    async def snapshot(self, *, count=16, candles=None, public=None, account=None, risk=None):
        config = main.AIConfig(allowedInstruments=tuple(f"COIN{index}-USDT-SWAP" for index in range(count)))
        rows = [{"id": instrument} for instrument in config.allowedInstruments]

        async def public_rows(path, params):
            if path == "/market/books":
                self.assertEqual(params["sz"], "5")
                return {"data": [{"ts": "1791217988304", "bids": [["1", "10", "0", "1"]] * 5, "asks": [["2", "10", "0", "1"]] * 5}]}
            return {"data": [{"ts": "1791217988304", "last": "1"}]}

        with patch.object(main, "ai_worker", SimpleNamespace(config=config)), \
             patch.object(main, "contracts", new=AsyncMock(return_value=rows)), \
             patch.object(main, "market_candles", new=candles or AsyncMock(return_value={"candles": self.candle_rows()})), \
             patch.object(main, "okx_get", new=public or public_rows), \
             patch.object(main, "_ai_trading_availability", new=AsyncMock(return_value={
                 instrument: {"available": True, "reason": ""} for instrument in config.allowedInstruments
             })), \
             patch.object(main, "account", new=account or AsyncMock(return_value={"authenticated": True, "todayLossCount": 0})), \
             patch.object(main, "risk", new=risk or AsyncMock(return_value={"killSwitch": False})), \
             patch.object(main, "_get_order_gateway", return_value=SimpleNamespace(daily_order_count=lambda: 0)), \
             patch.object(main._AIDataCollector, "_pace_candles", new=AsyncMock()):
            return await main._ai_snapshot(), config

    async def test_snapshot_retains_all_contracts_confirmed_history_and_shallow_books(self):
        snapshot, config = await self.snapshot()
        self.assertEqual(snapshot.ai["selectedInstruments"], list(config.allowedInstruments))
        self.assertEqual(len(snapshot.candles), 64)
        self.assertEqual(len(snapshot.orderBook), 16)
        self.assertTrue(all(len(rows) == 60 for rows in snapshot.candles.values()))
        self.assertTrue(all(rows[-1]["confirmed"] is False for rows in snapshot.candles.values()))
        for instrument in config.allowedInstruments:
            quality = snapshot.dataFreshness["availability"]["instruments"][instrument]
            self.assertEqual(quality["candles"]["5m"]["confirmedRowCount"], 59)
            self.assertFalse(quality["candles"]["5m"]["latestIsConfirmed"])
            self.assertTrue(quality["orderBook"]["available"])
            self.assertEqual(len(snapshot.orderBook[instrument]["bids"]), 5)
        self.assertEqual(snapshot.dataFreshness["errors"], [])
        self.assertEqual(snapshot.capturedAt, snapshot.dataFreshness["collectionStartedAt"])
        prompt = CodexRunner._decision_prompt(snapshot, config, now=datetime.now(timezone.utc))
        self.assertLess(len(prompt.encode("utf-8")), 1_048_576)

    async def test_snapshot_start_time_includes_collection_latency(self):
        timestamp = datetime(2026, 10, 5, 16, 0, tzinfo=timezone.utc)
        sequence = 0

        def advancing_clock():
            nonlocal sequence
            value = timestamp + timedelta(seconds=sequence)
            sequence += 1
            return value.isoformat(timespec="seconds").replace("+00:00", "Z")

        with patch.object(main, "now_iso", side_effect=advancing_clock):
            snapshot, _ = await self.snapshot(count=1)
        self.assertEqual(snapshot.capturedAt, "2026-10-05T16:00:00Z")
        self.assertEqual(snapshot.capturedAt, snapshot.dataFreshness["collectionStartedAt"])
        self.assertGreater(snapshot.dataFreshness["collectionCompletedAt"], snapshot.capturedAt)

    async def test_collection_failures_and_empty_responses_are_visible(self):
        async def candles(instId, bar):
            if bar == "15m":
                raise HTTPException(422, "Unsupported interval")
            return {"candles": []}

        snapshot, _ = await self.snapshot(count=1, candles=candles, public=AsyncMock(return_value={"data": []}))
        errors = snapshot.dataFreshness["errors"]
        self.assertEqual(len(errors), 7)
        self.assertEqual(len(snapshot.candles), 4)
        self.assertEqual(len(snapshot.orderBook), 1)
        self.assertTrue(any(row.get("interval") == "15m" and "Unsupported interval" in row["error"] for row in errors))
        self.assertTrue(any(row["resource"] == "orderBook" and "no orderBook" in row["error"] for row in errors))
        self.assertFalse(snapshot.dataFreshness["availability"]["instruments"]["COIN0-USDT-SWAP"]["candles"]["5m"]["available"])

    async def test_unknown_private_data_is_not_substituted_with_healthy_values(self):
        snapshot, _ = await self.snapshot(
            count=1,
            account=AsyncMock(side_effect=HTTPException(503, "Private account unavailable")),
            risk=AsyncMock(return_value={"equity": 99, "dataQuality": {"equitySource": "local", "accountRefreshError": "OKX request timed out"}}),
        )
        self.assertNotIn("availableEquityUSD", snapshot.account)
        self.assertNotIn("todayLossCount", snapshot.account)
        self.assertFalse(snapshot.dataFreshness["availability"]["account"]["available"])
        self.assertFalse(snapshot.dataFreshness["availability"]["risk"]["available"])
        self.assertTrue(any(row["resource"] == "risk" and "timed out" in row["error"] for row in snapshot.dataFreshness["errors"]))


class AISubmissionFreshnessTests(unittest.IsolatedAsyncioTestCase):
    def fake_clock(self):
        captured = datetime(2026, 10, 5, 16, 28, 30, tzinfo=timezone.utc)
        clock = [captured + timedelta(seconds=19)]

        class Clock(datetime):
            @classmethod
            def now(cls, tz=None):
                return clock[0].astimezone(tz) if tz is not None else clock[0].replace(tzinfo=None)

        return captured, clock, Clock

    def entry_request(self, deadline):
        return {"instrumentID": "BTC-USDT-SWAP", "side": "buy", "orderType": "market",
                "quantity": 1, "clientOrderID": "aifreshness", "source": "ai", "leverage": 1,
                "_aiEntryDeadline": deadline}

    def gateway(self, directory):
        async def submit(payload, demo):
            return await main.submit_order(payload, demo=demo)

        return OrderGateway(Path(directory) / "orders.json", submit=submit)

    async def test_valid_entry_posts_without_sending_internal_deadline(self):
        captured, _, clock_type = self.fake_clock()
        writes = AsyncMock(return_value={"data": [{"ordId": "order-1"}]})
        request = self.entry_request((captured + timedelta(seconds=90)).timestamp())
        with patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "datetime", clock_type), \
             patch.object(main, "okx_private_request", new=writes):
            result = await main.submit_order(request, demo=True)
        self.assertEqual(result["orderID"], "order-1")
        self.assertEqual([call.args[1] for call in writes.await_args_list], ["/account/set-leverage", "/trade/order"])
        self.assertTrue(all("_aiEntryDeadline" not in call.kwargs["body"] for call in writes.await_args_list))

    async def test_expired_or_invalid_deadline_never_starts_a_write(self):
        captured, _, clock_type = self.fake_clock()
        for deadline in ((captured + timedelta(seconds=19)).timestamp(), None, True, "invalid", float("nan")):
            with self.subTest(deadline=deadline):
                writes = AsyncMock()
                with patch.object(main, "private_ready", return_value=True), \
                     patch.object(main, "OKX_DEMO", True), \
                     patch.object(main, "datetime", clock_type), \
                     patch.object(main, "okx_private_request", new=writes):
                    with self.assertRaises(OrderNotSubmittedError):
                        await main.submit_order(self.entry_request(deadline), demo=True)
                self.assertEqual(writes.await_count, 0)

    async def test_entry_expires_while_setting_leverage_and_releases_reservation(self):
        captured, clock, clock_type = self.fake_clock()
        writes = []

        async def private_write(method, path, **kwargs):
            writes.append(path)
            clock[0] = captured + timedelta(seconds=91)
            return {"data": [{"ordId": "unexpected-order"}]}

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "datetime", clock_type), \
             patch.object(main, "okx_private_request", new=private_write):
            gateway = self.gateway(directory)
            with self.assertRaises(OrderNotSubmittedError):
                await gateway.submit_intent(
                    self.entry_request((captured + timedelta(seconds=90)).timestamp()), demo=True,
                    instrument=InstrumentSpec("BTC-USDT-SWAP", ctVal=1), price=100, daily_order_limit=1,
                )
            self.assertEqual(gateway.reservations, {})
            self.assertEqual(gateway.daily_order_count(), 0)
        self.assertEqual(writes, ["/account/set-leverage"])

    async def test_entry_expires_while_waiting_for_gateway_lock(self):
        captured, clock, clock_type = self.fake_clock()
        writes = AsyncMock()
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "datetime", clock_type), \
             patch.object(main, "okx_private_request", new=writes):
            gateway = self.gateway(directory)
            await gateway._lock.acquire()
            task = asyncio.create_task(gateway.submit_intent(
                self.entry_request((captured + timedelta(seconds=90)).timestamp()), demo=True,
                instrument=InstrumentSpec("BTC-USDT-SWAP", ctVal=1), price=100, daily_order_limit=1,
            ))
            await asyncio.sleep(0)
            clock[0] = captured + timedelta(seconds=91)
            gateway._lock.release()
            with self.assertRaises(OrderNotSubmittedError):
                await task
            self.assertEqual(gateway.reservations, {})
            self.assertEqual(gateway.daily_order_count(), 0)
        self.assertEqual(writes.await_count, 0)

    async def test_ticker_and_spec_waits_recheck_snapshot_and_decision_expiry(self):
        for delayed_resource in ("ticker", "spec"):
            for expiry_seconds in (30, 90):
                with self.subTest(resource=delayed_resource, expiry=expiry_seconds):
                    captured, clock, clock_type = self.fake_clock()
                    snapshot = AISnapshot(
                        snapshotId="snapshot-1", capturedAt=captured.isoformat().replace("+00:00", "Z"),
                        account={"availableEquityUSD": 10000, "todayLossCount": 0},
                        dataFreshness={"maxAgeSeconds": 90},
                    )
                    decision = AIDecision(
                        1, "freshness1", "snapshot-1", "open", instrumentID="BTC-USDT-SWAP", direction="long",
                        orderType="market", leverage=1, validUntil=(captured + timedelta(seconds=expiry_seconds if expiry_seconds == 30 else 120)).isoformat(),
                    )

                    async def ticker(instrument):
                        if delayed_resource == "ticker":
                            clock[0] = captured + timedelta(seconds=expiry_seconds + 1)
                        return 100

                    async def spec(instrument):
                        if delayed_resource == "spec":
                            clock[0] = captured + timedelta(seconds=expiry_seconds + 1)
                        return InstrumentSpec(instrument, ctVal=1)

                    writes = AsyncMock()
                    with tempfile.TemporaryDirectory() as directory, \
                         patch.object(main, "ai_worker", SimpleNamespace(config=main.AIConfig(enabled=True, mode="demo-active"))), \
                         patch.object(main, "private_ready", return_value=True), \
                         patch.object(main, "OKX_DEMO", True), \
                         patch.object(main, "datetime", clock_type), \
                         patch.object(main, "_ticker_last", new=ticker), \
                         patch.object(main, "_instrument_spec", new=spec), \
                         patch.object(main, "okx_private_request", new=writes):
                        gateway = self.gateway(directory)
                        with patch.object(main, "_get_order_gateway", return_value=gateway):
                            with self.assertRaises(HTTPException) as rejected:
                                await main._ai_execute(decision, snapshot)
                        self.assertIn("expired", rejected.exception.detail)
                        self.assertEqual(gateway.reservations, {})
                        self.assertEqual(gateway.daily_order_count(), 0)
                    self.assertEqual(writes.await_count, 0)

    async def test_manual_and_reduce_only_orders_ignore_ai_entry_deadline(self):
        captured, _, clock_type = self.fake_clock()
        for overrides in ({"source": "manual"}, {"reduceOnly": True}):
            with self.subTest(overrides=overrides):
                writes = AsyncMock(return_value={"data": [{"ordId": "order-1"}]})
                request = {**self.entry_request(captured.timestamp()), **overrides}
                with patch.object(main, "private_ready", return_value=True), \
                     patch.object(main, "OKX_DEMO", True), \
                     patch.object(main, "datetime", clock_type), \
                     patch.object(main, "okx_private_request", new=writes):
                    await main.submit_order(request, demo=True)
                self.assertEqual(sum(call.args[1] == "/trade/order" for call in writes.await_args_list), 1)

    async def test_close_and_cancel_remain_available_with_stale_snapshot(self):
        captured, _, clock_type = self.fake_clock()
        snapshot = AISnapshot(
            snapshotId="snapshot-old", capturedAt=(captured - timedelta(hours=1)).isoformat(),
            account={"availableEquityUSD": 10000}, dataFreshness={"maxAgeSeconds": "invalid"},
        )
        for action in ("close", "cancel"):
            with self.subTest(action=action):
                decision = AIDecision(
                    1, "exit1", "snapshot-old", action, instrumentID="BTC-USDT-SWAP", direction="long",
                    orderType="market", validUntil=captured.isoformat(), orderID="order-1",
                )
                writes = AsyncMock(return_value={"data": [{"ordId": "order-1"}]})
                with tempfile.TemporaryDirectory() as directory, \
                     patch.object(main, "ai_worker", SimpleNamespace(config=main.AIConfig(enabled=True, mode="demo-active"))), \
                     patch.object(main, "private_ready", return_value=True), \
                     patch.object(main, "OKX_DEMO", True), \
                     patch.object(main, "datetime", clock_type), \
                     patch.object(main, "_ticker_last", new=AsyncMock(return_value=100)), \
                     patch.object(main, "_instrument_spec", new=AsyncMock(return_value=InstrumentSpec("BTC-USDT-SWAP", ctVal=1))), \
                     patch.object(main, "okx_private_request", new=writes):
                    gateway = self.gateway(directory)
                    with patch.object(main, "_get_order_gateway", return_value=gateway):
                        await main._ai_execute(decision, snapshot)
                    self.assertEqual(gateway.daily_order_count(), 0)
                self.assertEqual([call.args[1] for call in writes.await_args_list], ["/trade/order" if action == "close" else "/trade/cancel-order"])


if __name__ == "__main__":
    unittest.main()
