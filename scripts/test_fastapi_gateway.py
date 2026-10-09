#!/usr/bin/env python3
"""Small contract check for the FastAPI loopback gateway helpers."""

import os
import sys
import unittest
import asyncio
import base64
import hashlib
import hmac
import json
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import main  # noqa: E402


class GatewayContractTests(unittest.TestCase):
    def test_http_client_uses_a_distinct_pool_for_each_event_loop(self):
        async def make_client_and_close():
            client = main.OKX_HTTP_CLIENT._client_for_loop()
            await client.aclose()
            return id(client)

        first = asyncio.run(make_client_and_close())
        second = asyncio.run(make_client_and_close())
        self.assertNotEqual(first, second)

    def test_enable_is_refused_while_the_failure_halt_is_latched(self):
        """Enable must not double as the halt recovery; only a config update is."""
        worker = SimpleNamespace(
            config=main.AIConfig(enabled=True, mode="paper-active"),
            status=SimpleNamespace(state="halted"),
            update_config=lambda values: worker.config.to_dict(),
            start=AsyncMock(),
            get_status=lambda: {"state": "halted", "enabled": False},
        )
        previous_workers = main.ai_workers
        previous_worker = main.ai_worker
        try:
            main.ai_workers = {"codex": worker}
            main.ai_worker = worker
            with self.assertRaises(main.HTTPException) as raised:
                asyncio.run(main.enable_ai())
            self.assertEqual(raised.exception.status_code, 409)
            self.assertIn("halted", raised.exception.detail)
            worker.start.assert_not_awaited()
        finally:
            main.ai_workers = previous_workers
            main.ai_worker = previous_worker

    def test_ai_status_does_not_restart_a_halted_worker(self):
        worker = SimpleNamespace(
            config=main.AIConfig(enabled=True, mode="paper-active"),
            status=SimpleNamespace(state="halted"),
            get_status=lambda: {"state": "halted", "enabled": False},
            start=AsyncMock(),
        )
        previous_workers = main.ai_workers
        previous_worker = main.ai_worker
        try:
            main.ai_workers = {"codex": worker}
            main.ai_worker = worker
            status = asyncio.run(main.ai_status())
            self.assertEqual(status["state"], "halted")
            worker.start.assert_not_awaited()
        finally:
            main.ai_workers = previous_workers
            main.ai_worker = previous_worker

    def test_ai_strategy_catalog_exposes_package_source_runtime_and_live_gate(self):
        class StubWorker:
            def __init__(self, strategy_id: str, provider: str):
                self.config = main.AIConfig(provider=provider)

            def get_config(self):
                return self.config.to_dict()

            def get_status(self):
                return {"enabled": False, "mode": "disabled"}

        workers = {
            "codex": StubWorker("codex", "codex"),
        }

        async def ensure(strategy_id: str):
            return workers[strategy_id]

        with patch.object(main, "_ensure_ai_worker", side_effect=ensure):
            catalog = asyncio.run(main.ai_strategies())

        self.assertEqual([item["id"] for item in catalog], ["codex"])
        self.assertEqual(catalog[0]["package"]["id"], "codex_ai_decision")
        for item in catalog:
            self.assertTrue(item["source"]["ofTruth"].endswith("/STRATEGY.md"))
            self.assertTrue(item["runtime"]["decisionEndpoint"].startswith("/api/v1/ai/strategies/"))
            self.assertTrue(item["liveGate"]["requiresManualEnable"])
            self.assertTrue(item["liveGate"]["requiresLiveTradingSwitch"])

    def test_exchange_risk_derives_mark_to_market_daily_loss_and_latches(self):
        """The account breaker must read real equity, not an unwritten field."""
        import tempfile as _tempfile

        async def run():
            with _tempfile.TemporaryDirectory() as directory:
                state_dir = Path(directory)
                equity = {"value": 10_000.0}

                async def private(method, path, **kwargs):
                    self.assertEqual(method, "GET")
                    if path == "/account/balance":
                        return {"data": [{"totalEq": str(equity["value"]), "details": []}]}
                    raise AssertionError(f"unexpected private path {path}")

                with patch.object(main, "TRADING_MODE", "exchange"), \
                        patch.object(main, "state_dir", return_value=state_dir), \
                        patch.object(main, "private_ready", return_value=True), \
                        patch.object(main, "okx_private_request", side_effect=private):
                    first = await main.risk()
                    self.assertEqual(first["dataQuality"]["equitySource"], "okx")
                    self.assertTrue(first["dataQuality"]["dailyPnLAuthoritative"])
                    self.assertEqual(first["dayStartEquity"], 10_000.0)
                    self.assertEqual(first["dailyPnLPercent"], 0)
                    self.assertFalse(first["killSwitch"])
                    # A 5% intraday drawdown latches the breaker, and the reason
                    # names the configured percentage.
                    equity["value"] = 9_500.0
                    breach = await main.risk()
                    self.assertLessEqual(breach["dailyPnLPercent"], -5)
                    self.assertTrue(breach["killSwitch"])
                    self.assertIn("5%", breach["reason"])
                    # The latch survives a subsequent recovery within the day.
                    equity["value"] = 10_400.0
                    recovered = await main.risk()
                    self.assertTrue(recovered["killSwitch"])
                    # And it is durable on disk for the next process.
                    persisted = json.loads((state_dir / "paper-state.json").read_text(encoding="utf-8"))
                    self.assertTrue(persisted["risk"]["killSwitch"])
                    self.assertEqual(persisted["risk"]["dayStartEquity"], 10_000.0)

        asyncio.run(run())

    def test_daily_loss_count_deduplicates_negative_bills_by_order(self):
        today_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        rows = [
            {"ts": str(today_ms), "ordId": "close-1", "pnl": "-2"},
            {"ts": str(today_ms), "ordId": "close-1", "pnl": "-1"},
            {"ts": str(today_ms), "ordId": "close-2", "pnl": "-0.5"},
            {"ts": str(today_ms), "ordId": "win-1", "pnl": "3"},
            {"ts": str(today_ms), "billId": "funding-1", "pnl": "-100"},
        ]
        self.assertEqual(main._daily_loss_count(rows, datetime.now(timezone.utc).date()), 2)

    def test_client_timestamps_and_swap_base_currency(self):
        self.assertRegex(main.now_iso(), r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
        self.assertEqual(main.base_currency({"instId": "BTC-USDT-SWAP", "baseCcy": ""}), "BTC")
        self.assertEqual(main.base_currency({"instId": "BTC-USDT", "baseCcy": "XBT"}), "XBT")

    def test_swap_turnover_uses_quote_currency_for_hot_ranking(self):
        # volCcy24h is base-coin quantity on OKX derivatives. Multiplying by
        # last keeps a cheap, high-unit coin from outranking a larger USDT
        # turnover contract merely because its unit count is bigger.
        self.assertEqual(
            main.quote_volume_24h(
                {"volCcy24h": "1000000", "vol24h": "1"},
                {"ctVal": "10", "ctMult": "1"},
                0.2,
            ),
            200000,
        )

    def test_swap_turnover_falls_back_to_contract_multiplier(self):
        self.assertEqual(
            main.quote_volume_24h(
                {"volCcy24h": "", "vol24h": "1000"},
                {"ctVal": "10", "ctMult": "2"},
                0.5,
            ),
            10000,
        )

    def test_ai_candidate_ids_preserve_fixed_allowlist_order(self):
        config = main.AIConfig.from_dict({
            "allowedInstruments": ["MID-USDT-SWAP", "HIGH-USDT-SWAP", "MID-USDT-SWAP"],
        })
        rows = [
            {"id": "LOW-USDT-SWAP", "volume24h": 10},
            {"id": "HIGH-USDT-SWAP", "volume24h": 100},
            {"id": "MID-USDT-SWAP", "volume24h": 50},
        ]
        self.assertEqual(main._ai_candidate_ids(config, rows), ["MID-USDT-SWAP", "HIGH-USDT-SWAP"])

    def test_ai_snapshot_preserves_complete_fixed_observation_set(self):
        config = main.AIConfig.from_dict({
            "allowedInstruments": [f"COIN{index}-USDT-SWAP" for index in range(6)],
        })
        rows = [{"id": f"COIN{index}-USDT-SWAP", "volume24h": 100 - index} for index in range(6)]
        previous_worker = main.ai_worker
        try:
            main.ai_worker = SimpleNamespace(config=config)
            with patch.object(main, "contracts", new=AsyncMock(return_value=rows)), \
                 patch.object(main, "market_candles", new=AsyncMock(return_value={"candles": []})), \
                 patch.object(main, "okx_get", new=AsyncMock(return_value={"data": [{}]})), \
                 patch.object(main, "_ai_trading_availability", new=AsyncMock(return_value={
                     instrument: {"available": True, "reason": ""} for instrument in config.allowedInstruments
                 })), \
                 patch.object(main, "account", new=AsyncMock(return_value={})), \
                 patch.object(main, "risk", new=AsyncMock(return_value={})), \
                 patch.object(main, "now_iso", return_value="2026-10-05T00:00:00Z"):
                snapshot = asyncio.run(main._ai_snapshot())
                selected = snapshot.ai["selectedInstruments"]
                self.assertEqual(len(selected), 6)
                self.assertEqual(snapshot.ai["observationCount"], len(selected))
                self.assertEqual(len(snapshot.candles), len(selected) * 4)
        finally:
            main.ai_worker = previous_worker

    def test_ai_fixed_universe_does_not_fallback_to_unselected_contract(self):
        config = main.AIConfig.from_dict({"allowedInstruments": ["MID-USDT-SWAP"]})
        rows = [
            {"id": "LOW-USDT-SWAP", "volume24h": 10},
            {"id": "MID-USDT-SWAP", "volume24h": 50},
        ]
        self.assertEqual(main._ai_candidate_ids(config, rows), ["MID-USDT-SWAP"])
        empty = main.AIConfig.from_dict({"allowedInstruments": ["MISSING-USDT-SWAP"]})
        self.assertEqual(main._ai_candidate_ids(empty, rows), [])

    def test_ticker_price_changes_keep_utc_day_and_rolling_24h_separate(self):
        day, rolling = main.ticker_price_changes(
            {"sodUtc0": "100", "sodUtc8": "105", "open24h": "108"},
            110,
        )
        self.assertAlmostEqual(day, 10)
        self.assertAlmostEqual(rolling, 1.85185185185)

    def test_ticker_price_changes_fall_back_to_utc8_then_last(self):
        day, rolling = main.ticker_price_changes({"sodUtc0": "0", "sodUtc8": "105", "open24h": ""}, 110)
        self.assertAlmostEqual(day, (110 / 105 - 1) * 100)
        self.assertEqual(rolling, 0)
        day, rolling = main.ticker_price_changes({"sodUtc0": "", "sodUtc8": ""}, 110)
        self.assertEqual(day, 0)
        self.assertEqual(rolling, 0)

    def test_position_snapshot_keeps_margin_leverage_and_protection_prices(self):
        snapshot = main.position_snapshot({
            "posId": "position-1", "instId": "BTC-USDT-SWAP", "posSide": "long",
            "pos": "2", "avgPx": "100", "markPx": "105", "upl": "10",
            "mgnMode": "isolated", "imr": "40", "lever": "5",
            "attachAlgoOrds": [{"tpTriggerPx": "110", "slTriggerPx": "95"}],
        })
        self.assertEqual(snapshot["margin"], 40)
        self.assertEqual(snapshot["leverage"], 5)
        self.assertEqual(snapshot["takeProfitPrice"], 110)
        self.assertEqual(snapshot["stopLossPrice"], 95)

    def test_pending_position_protections_reads_okx_algorithm_order_types(self):
        calls = []

        async def fake_request(method, path, *, params=None, body=None):
            calls.append((method, path, params))
            if params["ordType"] == "oco":
                return {"data": [{
                    "instId": "FIL-USDT-SWAP", "posSide": "net",
                    "tpTriggerPx": "1.1899", "slTriggerPx": "1.1439",
                }]}
            return {"data": []}

        previous = main.okx_private_request
        main.okx_private_request = fake_request
        try:
            protections = asyncio.run(main._pending_position_protections())
        finally:
            main.okx_private_request = previous

        self.assertEqual(protections["FIL-USDT-SWAP|net"], {
            "takeProfitPrice": 1.1899,
            "stopLossPrice": 1.1439,
        })
        self.assertEqual([call[2]["ordType"] for call in calls], ["conditional", "oco", "trigger"])

    def test_pending_position_protections_marks_partial_lookup_incomplete(self):
        async def fake_request(method, path, *, params=None, body=None):
            if params["ordType"] == "conditional":
                raise RuntimeError("temporary OKX timeout")
            return {"data": []}

        previous = main.okx_private_request
        main.okx_private_request = fake_request
        try:
            protections = asyncio.run(main._pending_position_protections(include_details=True))
        finally:
            main.okx_private_request = previous

        self.assertFalse(protections["_meta"]["lookupComplete"])
        self.assertIn("conditional:", protections["_meta"]["lookupErrors"][0])

    def test_pending_position_protections_marks_unowned_algo_unmanaged(self):
        async def fake_request(method, path, *, params=None, body=None):
            if params["ordType"] == "oco":
                return {"data": [{
                    "instId": "FIL-USDT-SWAP", "posSide": "net", "algoId": "manual-1",
                    "tpTriggerPx": "1.1899", "slTriggerPx": "1.1439",
                }]}
            return {"data": []}

        previous = main.okx_private_request
        main.okx_private_request = fake_request
        try:
            protections = asyncio.run(main._pending_position_protections(include_details=True))
        finally:
            main.okx_private_request = previous

        self.assertTrue(protections["FIL-USDT-SWAP|net"]["unmanagedOrders"])

    def test_staged_take_profit_schema_requires_full_position_allocation(self):
        payload = {
            "schemaVersion": 1, "decisionId": "staged-1", "snapshotId": "snap-1",
            "action": "hold", "instrumentID": None, "direction": None, "orderType": None,
            "riskBudgetPercent": None, "limitPrice": None, "stopLossPrice": None,
            "takeProfitPrice": None,
            "takeProfitLevels": [
                {"price": 110, "quantityPercent": 40},
                {"price": 120, "quantityPercent": 60},
            ],
            "winRate": 0, "riskRewardRatio": 0, "leverage": None, "confidence": .8,
            "validUntil": "2026-10-07T00:00:00Z", "reasonCode": "hold", "reason": "复评",
            "orderID": None, "assessments": [],
        }
        decision = main.AIDecision.from_dict(payload)
        self.assertEqual(decision.takeProfitLevels[1]["quantityPercent"], 60)
        with self.assertRaises(main.SchemaError):
            main.AIDecision.from_dict({**payload, "takeProfitLevels": [{"price": 110, "quantityPercent": 99}]})

    def test_high_confidence_material_protection_change_is_allowed_but_noise_is_not(self):
        position = {"instrumentID": "BTC-USDT-SWAP", "side": "long", "quantity": 2, "entryPrice": 100}
        protection = {"algoIDs": ["algo-1"], "takeProfitPrices": [110], "stopLossPrices": [95]}
        assessment = SimpleNamespace(
            takeProfitLevels=[{"price": 120, "quantityPercent": 100}],
            takeProfitPrice=120, stopLossPrice=98, confidence=.9,
            reason="突破确认且成交量放大。",
        )
        decision = SimpleNamespace(confidence=.9, reason="趋势确认，原保护线已不适用。")
        self.assertTrue(main._protection_needs_adjustment(position, protection, assessment, decision))
        decision.confidence = .7
        self.assertFalse(main._protection_needs_adjustment(position, protection, assessment, decision))

        staged_assessment = SimpleNamespace(
            takeProfitLevels=[
                {"price": 112, "quantityPercent": 50},
                {"price": 120, "quantityPercent": 50},
            ],
            takeProfitPrice=120, stopLossPrice=92, confidence=.7,
            reason="分批目标调整，但信号未出现实质变化。",
        )
        self.assertFalse(main._protection_needs_adjustment(position, protection, staged_assessment, decision))

    def test_protection_adjustment_only_moves_toward_profit(self):
        decision = SimpleNamespace(confidence=.95, reason="趋势延续，保护线跟随盈利方向移动。")

        long_position = {"instrumentID": "BTC-USDT-SWAP", "side": "long", "quantity": 2, "entryPrice": 100}
        long_protection = {"algoIDs": ["long-algo"], "takeProfitPrices": [120], "stopLossPrices": [95]}

        # A long stop and target can only move upward. A mixed update that
        # worsens either side must be rejected as one atomic replacement.
        long_better = SimpleNamespace(
            takeProfitLevels=[{"price": 130, "quantityPercent": 100}],
            takeProfitPrice=130, stopLossPrice=105, confidence=.95,
            reason="趋势延续，抬高止损并扩大止盈目标。",
        )
        self.assertTrue(main._protection_needs_adjustment(long_position, long_protection, long_better, decision))

        long_worse_stop = SimpleNamespace(
            takeProfitLevels=[{"price": 130, "quantityPercent": 100}],
            takeProfitPrice=130, stopLossPrice=90, confidence=.95,
            reason="重新评估后调整保护线。",
        )
        self.assertFalse(main._protection_needs_adjustment(long_position, long_protection, long_worse_stop, decision))

        long_worse_target = SimpleNamespace(
            takeProfitLevels=[{"price": 110, "quantityPercent": 100}],
            takeProfitPrice=110, stopLossPrice=105, confidence=.95,
            reason="重新评估后调整保护线。",
        )
        self.assertFalse(main._protection_needs_adjustment(long_position, long_protection, long_worse_target, decision))

        staged_protection = {"algoIDs": ["staged-algo"], "takeProfitPrices": [100, 120, 140], "stopLossPrices": [95]}
        staged_assessment = SimpleNamespace(
            takeProfitLevels=[
                {"price": 110, "quantityPercent": 30},
                {"price": 115, "quantityPercent": 30},
                {"price": 150, "quantityPercent": 40},
            ],
            takeProfitPrice=150, stopLossPrice=105, confidence=.95,
            reason="分批目标中间档回撤。",
        )
        self.assertFalse(main._protection_needs_adjustment(long_position, staged_protection, staged_assessment, decision))

        short_position = {"instrumentID": "FIL-USDT-SWAP", "side": "short", "quantity": -2, "entryPrice": 100}
        short_protection = {"algoIDs": ["short-algo"], "takeProfitPrices": [80], "stopLossPrices": [105]}

        # A short stop and target can only move downward.
        short_better = SimpleNamespace(
            takeProfitLevels=[{"price": 70, "quantityPercent": 100}],
            takeProfitPrice=70, stopLossPrice=95, confidence=.95,
            reason="趋势延续，压低止损并下移止盈目标。",
        )
        self.assertTrue(main._protection_needs_adjustment(short_position, short_protection, short_better, decision))

        short_worse_stop = SimpleNamespace(
            takeProfitLevels=[{"price": 70, "quantityPercent": 100}],
            takeProfitPrice=70, stopLossPrice=110, confidence=.95,
            reason="重新评估后调整保护线。",
        )
        self.assertFalse(main._protection_needs_adjustment(short_position, short_protection, short_worse_stop, decision))

        short_worse_target = SimpleNamespace(
            takeProfitLevels=[{"price": 90, "quantityPercent": 100}],
            takeProfitPrice=90, stopLossPrice=95, confidence=.95,
            reason="重新评估后调整保护线。",
        )
        self.assertFalse(main._protection_needs_adjustment(short_position, short_protection, short_worse_target, decision))

    def test_protection_adjustment_rejects_a_worse_line_even_with_a_better_other_line(self):
        position = {"instrumentID": "BTC-USDT-SWAP", "side": "long", "quantity": 2, "entryPrice": 100}
        protection = {"algoIDs": ["algo-1"], "takeProfitPrices": [110], "stopLossPrices": [95]}
        assessment = SimpleNamespace(
            takeProfitLevels=[{"price": 120, "quantityPercent": 100}],
            takeProfitPrice=120, stopLossPrice=92, confidence=.9,
            reason="目标上移但保护线回撤。",
        )
        decision = SimpleNamespace(confidence=.9, reason="复评要求调整保护线。")
        self.assertFalse(main._protection_needs_adjustment(position, protection, assessment, decision))

        short_position = {"instrumentID": "BTC-USDT-SWAP", "side": "short", "quantity": -2, "entryPrice": 100}
        short_protection = {"algoIDs": ["algo-2"], "takeProfitPrices": [90], "stopLossPrices": [105]}
        short_assessment = SimpleNamespace(
            takeProfitLevels=[{"price": 80, "quantityPercent": 100}],
            takeProfitPrice=80, stopLossPrice=108, confidence=.9,
            reason="目标下移但保护线回撤。",
        )
        self.assertFalse(main._protection_needs_adjustment(short_position, short_protection, short_assessment, decision))

    def test_position_protection_uses_mark_price_for_profitable_staged_positions(self):
        short_position = {
            "instrumentID": "FIL-USDT-SWAP", "side": "short", "quantity": -180,
            "entryPrice": 100, "markPrice": 80,
        }
        short_assessment = SimpleNamespace(
            stopLossPrice=85,
            takeProfitPrice=None,
            takeProfitLevels=[
                {"price": 75, "quantityPercent": 50},
                {"price": 70, "quantityPercent": 50},
            ],
        )
        self.assertTrue(main._protection_is_reasonable(short_position, short_assessment))

        long_position = {
            "instrumentID": "BTC-USDT-SWAP", "side": "long", "quantity": 2,
            "entryPrice": 100, "markPrice": 120,
        }
        long_assessment = SimpleNamespace(
            stopLossPrice=115,
            takeProfitPrice=None,
            takeProfitLevels=[
                {"price": 130, "quantityPercent": 40},
                {"price": 140, "quantityPercent": 60},
            ],
        )
        self.assertTrue(main._protection_is_reasonable(long_position, long_assessment))

    def test_order_snapshot_calculates_margin_from_contract_spec(self):
        snapshot = main.order_snapshot({
            "ordId": "order-1", "instId": "BTC-USDT-SWAP", "side": "buy",
            "state": "live", "sz": "2", "px": "100", "cTime": "1700000000000",
            "accFillSz": "1", "avgPx": "99", "lever": "5", "tdMode": "isolated",
            "attachAlgoOrds": [{"tpTriggerPx": "110", "slTriggerPx": "95"}],
        }, instrument={"ctVal": "1", "ctMult": "1"})
        self.assertEqual(snapshot["margin"], 40)
        self.assertEqual(snapshot["averageFillPrice"], 99)
        self.assertEqual(snapshot["takeProfitPrice"], 110)
        self.assertEqual(snapshot["stopLossPrice"], 95)
        self.assertEqual(snapshot["marginMode"], "isolated")

    def test_loopback_host_parsing(self):
        self.assertTrue(main.is_loopback("127.0.0.1:8787"))
        self.assertTrue(main.is_loopback("[::1]:8787"))
        self.assertTrue(main.is_loopback("::1"))
        self.assertFalse(main.is_loopback("127.0.0.1.evil.example:8787"))

    def test_authorization_accepts_bearer_token_and_matching_origin(self):
        previous = main.token
        main.token = lambda: "test-token"
        try:
            self.assertTrue(main.authorized({"authorization": "Bearer test-token"}, "127.0.0.1:8787", "http://127.0.0.1:8787"))
            self.assertFalse(main.authorized({"authorization": "Bearer test-token"}, "127.0.0.1:8787", "http://127.0.0.1:3000"))
            self.assertFalse(main.authorized({"authorization": "Bearer wrong"}, "127.0.0.1:8787", None))
        finally:
            main.token = previous

    def test_okx_signature_matches_v5_hmac_contract(self):
        previous = main.OKX_SECRET_KEY
        main.OKX_SECRET_KEY = "secret"
        try:
            expected = base64.b64encode(hmac.new(b"secret", b"2020-12-08T09:08:57.715ZGET/api/v5/account/balance", hashlib.sha256).digest()).decode()
            self.assertEqual(main.okx_signature("2020-12-08T09:08:57.715Z", "GET", "/api/v5/account/balance", ""), expected)
        finally:
            main.OKX_SECRET_KEY = previous


if __name__ == "__main__":
    unittest.main()
