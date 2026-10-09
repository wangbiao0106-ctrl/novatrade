"""Replay final AI entry checks without exchange calls or live account writes."""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))

from backend import main
from backend.ai_entry_preflight import ENTRY_PREFLIGHT_LIMITS, EntryPreflightRejected, check_entry_preflight
from backend.ai_policy import PolicyResult
from backend.ai_schema import AIDecision, AIConfig, AISnapshot
from backend.ai_worker import AIWorker, CodexRunner
from backend.order_gateway import InstrumentSpec, OrderGateway, OrderNotSubmittedError
from backend.paper_trading import PaperTradingAccount

BTC = "BTC-USDT-SWAP"


def request(side="buy", **changes):
    value = {
        "instrumentID": BTC, "source": "ai", "side": side, "orderType": "market", "quantity": 1,
        "stopLossTriggerPrice": 90 if side == "buy" else 110,
        "takeProfitTriggerPrice": 130 if side == "buy" else 70,
        "leverage": 1, "clientOrderID": "aipreflight", "marginMode": "isolated",
        "_aiEntryReferencePrice": 100, "_aiEntryPlannedEntryPrice": 100,
        "_aiEntryContractValue": 1, "_aiEntryTickSize": 0.01,
        "_aiEntryDeadline": (datetime.now(timezone.utc) + timedelta(minutes=1)).timestamp(),
    }
    value.update(changes)
    return value


def market(now=None, price=100):
    stamp = str(int((now or datetime.now(timezone.utc)).timestamp() * 1000))
    return (
        {"instId": BTC, "last": str(price), "ts": stamp},
        {"bids": [[str(price - .01), "20000"]], "asks": [[str(price + .01), "20000"]], "ts": stamp},
        {"instId": BTC, "markPx": str(price), "ts": stamp},
    )


class MarketPreflightTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.data = market(self.now)

    def check(self, value=None, data=None, **kwargs):
        return check_entry_preflight(value or request(), *(data or self.data), now=self.now, **kwargs)

    def test_published_limits_and_prompt_match_runtime(self):
        config = json.loads((ROOT / "strategies/codex_ai_decision/config/strategy.json").read_text())
        self.assertEqual(config["entry_preflight"], ENTRY_PREFLIGHT_LIMITS)
        snapshot = AISnapshot("preflight", self.now.isoformat(), instruments=[{"id": BTC}])
        self.assertEqual(CodexRunner._entry_gates(snapshot, AIConfig())["finalEntryPreflight"], ENTRY_PREFLIGHT_LIMITS)

    def test_good_long_and_short_depth_remain_tradable(self):
        for side in ("buy", "sell"):
            with self.subTest(side=side):
                result = self.check(request(side))
                self.assertGreater(result["netRiskRewardRatio"], 2)
                self.assertAlmostEqual(result["spreadBps"], 2)

    def test_stale_missing_future_and_mismatched_sources_fail_closed(self):
        for index in range(3):
            for change in ({"ts": str(int((self.now.timestamp() - 6) * 1000))},
                           {"ts": str(int((self.now.timestamp() + 2) * 1000))},
                           {"ts": None}, {"ts": True}, {"instId": "WRONG-USDT-SWAP"}):
                with self.subTest(index=index, change=change):
                    data = deepcopy(self.data)
                    data[index].update(change)
                    with self.assertRaises(EntryPreflightRejected):
                        self.check(data=data)

    def test_crossed_wide_and_malformed_books_are_rejected(self):
        for bids, asks in (([["100.1", "1"]], [["99.9", "1"]]),
                           ([["99.9", "1"]], [["100.1", "1"]]),
                           ([], [["100.01", "1"]]),
                           ([["99.99", "1"]], [["100.01", "0"]]),
                           ([["99.99", "1"]], [["100.01", "1"], ["100", "1"]])):
            data = deepcopy(self.data)
            data[1].update(bids=bids, asks=asks)
            with self.subTest(bids=bids, asks=asks), self.assertRaises(EntryPreflightRejected):
                self.check(data=data)

    def test_price_jump_and_small_stop_risk_both_bound_deviation(self):
        with self.assertRaisesRegex(EntryPreflightRejected, "deviation"):
            self.check(data=market(self.now, 100.6))
        # A 0.06% move fits the 0.5% cap but consumes over 25% of a 0.2 stop.
        with self.assertRaisesRegex(EntryPreflightRejected, "deviation"):
            self.check(request(stopLossTriggerPrice=99.8, takeProfitTriggerPrice=100.8), market(self.now, 100.06))

    def test_mark_or_last_already_at_protection_refuses_entry(self):
        for index, key, price in ((0, "last", 90), (2, "markPx", 90), (2, "markPx", 130)):
            data = deepcopy(self.data)
            data[index][key] = str(price)
            with self.subTest(index=index, price=price), self.assertRaisesRegex(EntryPreflightRejected, "already reached"):
                self.check(data=data)

    def test_depth_fees_and_nearest_tranche_cannot_be_hidden_by_a_far_target(self):
        with self.assertRaisesRegex(EntryPreflightRejected, "depth"):
            self.check(request(quantity=20001))
        with self.assertRaisesRegex(EntryPreflightRejected, "net risk/reward"):
            self.check(request(takeProfitTriggerPrice=120))
        with self.assertRaisesRegex(EntryPreflightRejected, "net risk/reward"):
            self.check(request(takeProfitLevels=[{"price": 120, "quantityPercent": 10}, {"price": 150, "quantityPercent": 90}]))
        with self.assertRaisesRegex(EntryPreflightRejected, "net risk/reward"):
            self.check(request(takeProfitTriggerPrice=120, takeProfitLevels=[{"price": 150, "quantityPercent": 100}]))
        with self.assertRaisesRegex(EntryPreflightRejected, "net risk/reward"):
            self.check(fee_rate=0.03)

    def test_resting_limit_keeps_its_price_and_compares_snapshot_market_reference(self):
        value = request(orderType="limit", price=90, stopLossTriggerPrice=80, takeProfitTriggerPrice=120)
        result = self.check(value)
        self.assertEqual(result["estimatedEntryPrice"], 90)
        self.assertEqual(value["price"], 90)

    def test_already_aligned_prices_do_not_move_another_tick(self):
        value = request(orderType="limit", price=100.1, stopLossTriggerPrice=90.1,
                        takeProfitTriggerPrice=130.1, _aiEntryTickSize=.1, _aiEntryReferencePrice=101)
        result = self.check(value, market(self.now, 101))
        self.assertEqual(result["estimatedEntryPrice"], 100.1)

    def test_depth_covers_short_size_after_adverse_slippage(self):
        data = deepcopy(self.data)
        data[1]["bids"][0][1] = "1.0002"
        # Sizing at bid alone understates the final short contract count.
        with self.assertRaisesRegex(EntryPreflightRejected, "depth"):
            self.check(request("sell", quantity=None, targetNotional=100), data)

    def test_execution_price_must_still_fit_protection_levels(self):
        for side, stop, target in (("buy", 99.9, 100.025), ("sell", 100.1, 99.975)):
            with self.subTest(side=side), self.assertRaisesRegex(EntryPreflightRejected, "execution price"):
                self.check(request(side, stopLossTriggerPrice=stop, takeProfitTriggerPrice=target, _aiEntryTickSize=.001))

    def test_spec_rounding_and_unknown_reference_are_not_bypassed(self):
        for changes in ({"_aiEntryReferencePrice": None},
                        {"_aiEntryTickSize": 10, "orderType": "limit", "price": 100.03, "stopLossTriggerPrice": 100.01},
                        {"stopLossTriggerPrice": None}, {"takeProfitTriggerPrice": None}):
            with self.subTest(changes=changes), self.assertRaises(EntryPreflightRejected):
                self.check(request(**changes))
        for values in ({"fee_rate": float("nan")}, {"slippage_bps": float("inf")}):
            with self.subTest(values=values), self.assertRaises(EntryPreflightRejected):
                self.check(**values)


class SubmissionPreflightTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_quotes_reject_before_order_submission(self):
        for failure in (httpx.ReadTimeout("quote timeout"), {"data": []}, {"data": [None]}):
            mock = AsyncMock(side_effect=failure) if isinstance(failure, Exception) else AsyncMock(return_value=failure)
            with self.subTest(failure=failure), patch.object(main, "okx_get", new=mock), \
                 patch.object(main, "okx_private_request", new=AsyncMock()) as private:
                with self.assertRaises(EntryPreflightRejected):
                    await main._ai_entry_preflight(request())
                private.assert_not_awaited()

    async def test_quote_fetch_cannot_outlive_the_entry_deadline(self):
        value = request()
        async def public(route, params):
            value["_aiEntryDeadline"] = datetime.now(timezone.utc).timestamp() - 1
            return {"data": [market()[{"/market/ticker": 0, "/market/books": 1, "/public/mark-price": 2}[route]]]}
        with patch.object(main, "okx_get", new=AsyncMock(side_effect=public)), \
             patch.object(main, "okx_private_request", new=AsyncMock()) as private:
            with self.assertRaisesRegex(OrderNotSubmittedError, "expired"):
                await main._ai_entry_preflight(value)
            private.assert_not_awaited()

    async def test_exchange_checks_after_leverage_and_releases_rejected_reservation(self):
        leveraged, order_posts = False, []
        async def private(method, route, **kwargs):
            nonlocal leveraged
            if route == "/account/set-leverage":
                leveraged = True
            if route == "/trade/order":
                order_posts.append(kwargs)
            return {"data": []}
        async def public(route, params):
            self.assertTrue(leveraged, "Final prices must be fetched after setting leverage")
            rows = market(price=101)
            return {"data": [rows[{"/market/ticker": 0, "/market/books": 1, "/public/mark-price": 2}[route]]]}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(main, "TRADING_MODE", "exchange"), patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "okx_private_request", new=AsyncMock(side_effect=private)), \
             patch.object(main, "okx_get", new=AsyncMock(side_effect=public)):
            gateway = OrderGateway(Path(directory) / "orders.json", submit=main._gateway_submit)
            with self.assertRaisesRegex(EntryPreflightRejected, "deviation"):
                await gateway.submit_intent(request(), demo=True, instrument=InstrumentSpec(BTC, 1), price=100)
            self.assertEqual(order_posts, [])
            self.assertEqual(gateway.reservations, {})
            self.assertEqual(gateway.daily_order_count(), 0)

    async def test_valid_exchange_check_allows_one_order_post(self):
        posted = []
        async def private(method, route, **kwargs):
            if route == "/trade/order":
                posted.append(kwargs["body"])
                return {"data": [{"ordId": "accepted", "clOrdId": "aipreflight"}]}
            return {"data": []}
        async def public(route, params):
            return {"data": [market()[{"/market/ticker": 0, "/market/books": 1, "/public/mark-price": 2}[route]]]}
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(main, "TRADING_MODE", "exchange"), patch.object(main, "OKX_DEMO", True), \
             patch.object(main, "private_ready", return_value=True), \
             patch.object(main, "okx_private_request", new=AsyncMock(side_effect=private)), \
             patch.object(main, "okx_get", new=AsyncMock(side_effect=public)):
            gateway = OrderGateway(Path(directory) / "orders.json", submit=main._gateway_submit)
            result = await gateway.submit_intent(request(), demo=True, instrument=InstrumentSpec(BTC, 1), price=100)
            self.assertEqual(result["orderID"], "accepted")
            self.assertEqual(len(posted), 1)
            self.assertEqual(posted[0]["attachAlgoOrds"][0]["slTriggerPx"], 90)
            self.assertEqual(gateway.daily_order_count(), 1)

    async def test_paper_check_runs_under_lock_and_rejection_creates_no_order(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = PaperTradingAccount(Path(directory) / "paper.json")
            async def preflight(value):
                self.assertTrue(broker._lock.locked())
                return check_entry_preflight(value, *market(price=101))
            with self.assertRaises(EntryPreflightRejected):
                await broker.submit_intent(request(), instrument=InstrumentSpec(BTC, 1), price=100, entry_preflight=preflight)
            self.assertEqual(broker.all_orders(), [])
            self.assertEqual(broker.daily_order_count(), 0)

    async def test_paper_fills_use_refreshed_quote_and_close_skips_preflight(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = PaperTradingAccount(Path(directory) / "paper.json")
            async def preflight(value):
                self.assertTrue(broker._lock.locked())
                return check_entry_preflight(value, *market(price=100.1))
            result = await broker.submit_intent(request(), instrument=InstrumentSpec(BTC, 1), price=99, entry_preflight=preflight)
            self.assertGreater(broker.all_orders()[0]["fillPrice"], 100.1)
            guard = AsyncMock(side_effect=AssertionError("Close must remain available"))
            await broker.submit_intent(request("sell", reduceOnly=True, clientOrderID="close"),
                                       instrument=InstrumentSpec(BTC, 1), price=100, entry_preflight=guard)
            guard.assert_not_awaited()
            self.assertEqual(broker.positions(), [])

    async def test_market_rejections_keep_assessments_without_failure_halt(self):
        now = datetime.now(timezone.utc)
        snapshot = AISnapshot("worker-preflight", now.isoformat(), instruments=[{"id": BTC}], dataFreshness={"maxAgeSeconds": 90})
        decision = AIDecision.from_dict({
            "schemaVersion": 1, "decisionId": "entry", "snapshotId": snapshot.snapshotId, "action": "open",
            "instrumentID": BTC, "direction": "long", "orderType": "market", "stopLossPrice": 90,
            "takeProfitPrice": 130, "leverage": 1, "winRate": .7, "riskRewardRatio": 3, "confidence": .9,
            "validUntil": (now + timedelta(minutes=1)).isoformat(), "reasonCode": "TEST", "reason": "测试有证据支持的入场方案",
            "assessments": [{"instrumentID": BTC, "direction": "long", "limitPrice": 100, "stopLossPrice": 90,
                             "takeProfitPrice": 130, "winRate": .7, "riskRewardRatio": 3, "confidence": .9,
                             "entryEligible": True, "unmetConditions": [], "reason": "测试有证据支持的入场方案"}],
        })
        with tempfile.TemporaryDirectory() as directory, \
             patch("backend.ai_worker.validate_decision", return_value=PolicyResult(True, decision, "accepted")):
            worker = AIWorker(config=AIConfig(enabled=True, mode="demo-active"), state_dir=Path(directory),
                              runner=SimpleNamespace(run=AsyncMock(return_value=decision)),
                              order_gateway=AsyncMock(side_effect=EntryPreflightRejected("market changed")))
            for _ in range(3):
                result = await worker.run_once(snapshot)
                self.assertFalse(result.accepted)
                self.assertEqual(result.decision.action, "hold")
                self.assertEqual(result.decision.assessments, decision.assessments)
            self.assertEqual(worker.status.consecutiveFailures, 0)
            self.assertEqual(worker.status.state, "running")
            self.assertIsNone(worker.status.lastError)
            self.assertEqual(worker.status.lastEvaluationSource, "model")
            self.assertEqual(worker.policy_state.seenDecisionIds, set())
            audit = [json.loads(row) for row in (Path(directory) / "ai-decisions.jsonl").read_text().splitlines()]
            rejected = [row for row in audit if row.get("decision", {}).get("reasonCode") == "ENTRY_PREFLIGHT_REJECTED"]
            self.assertEqual(len(rejected), 3)
            self.assertTrue(all(row["rawDecision"]["action"] == "open" and row["accepted"] is False for row in rejected))


if __name__ == "__main__":
    unittest.main()
