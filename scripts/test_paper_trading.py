#!/usr/bin/env python3
"""Verify local paper executions, accounting and durable restart boundaries."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sys
import tempfile
import unittest
from uuid import UUID

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.order_gateway import InstrumentSpec  # noqa: E402
from backend.paper_trading import PaperTradingAccount, PaperTradingError  # noqa: E402


BTC = "BTC-USDT-SWAP"
ETH = "ETH-USDT-SWAP"


class PaperTradingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "paper-account.json"
        self.now = datetime(2026, 10, 7, 23, 30, tzinfo=timezone.utc)
        self.spec = InstrumentSpec(BTC, 1, lotSize=1, minSize=1, tickSize=0.1)
        self.account = self.make_account()

    def make_account(self, **kwargs):
        return PaperTradingAccount(self.path, clock=lambda: self.now, **kwargs)

    async def submit(self, *, instrument=None, price=100, market_price=None, **request):
        instrument = instrument or self.spec
        values = {"instrumentID": instrument.instrumentID, "side": "buy", "quantity": 10,
                  "leverage": 2, "clientOrderID": "entry1", **request}
        if values.get("orderType") == "limit":
            values["price"] = price
            reference = 100 if market_price is None else market_price
        else:
            reference = price if market_price is None else market_price
        return await self.account.submit_intent(values, instrument=instrument, price=reference)

    async def test_fresh_account_is_only_five_thousand_usdt(self):
        snapshot = self.account.account_snapshot()
        self.assertEqual(snapshot["equityUSD"], 5000)
        self.assertEqual(snapshot["availableEquityUSD"], 5000)
        self.assertEqual(snapshot["assets"], [{"id": "USDT", "currency": "USDT", "equity": 5000, "available": 5000, "usdValue": 5000}])
        self.assertEqual(snapshot["mode"], "paper")
        self.assertEqual(snapshot["profile"], "local-paper")
        self.assertEqual(self.account.positions(), [])
        self.assertTrue(snapshot["pendingOrdersKnown"])

    async def test_contract_value_multiplier_quote_fee_and_slippage(self):
        spec = InstrumentSpec(BTC, 0.01, ctMult=2, lotSize=1, minSize=1)
        result = await self.account.submit_intent(
            {"instrumentID": BTC, "side": "buy", "quantity": 10, "leverage": 2, "clientOrderID": "contracts"},
            instrument=spec, price=100, quote={"bidPx": "99", "askPx": "101"},
        )
        price = 101 * 1.0002
        notional = 10 * 0.01 * 2 * price
        fee = notional * 0.0005
        self.assertEqual(result["status"], "filled")
        self.assertAlmostEqual(result["notional"], notional)
        self.assertAlmostEqual(self.account.positions()[0]["margin"], notional / 2)
        self.assertAlmostEqual(self.account.all_fills()[0]["fee"], fee)
        self.assertAlmostEqual(self.account.account_snapshot()["availableEquityUSD"], 5000 - fee - notional / 2)
        await self.account.mark(BTC, 110)
        self.assertAlmostEqual(self.account.positions()[0]["unrealizedPnL"], (110 - price) * 0.2)

    async def test_target_sizing_keeps_margin_plus_fee_within_fixed_budget(self):
        result = await self.account.submit_intent(
            {"instrumentID": BTC, "side": "buy", "targetNotional": 2500, "marginUSD": 500, "leverage": 5},
            instrument=InstrumentSpec(BTC, 0.01, lotSize=0.01, minSize=0.01), price=100,
        )
        position = self.account.positions()[0]
        fee = self.account.all_fills()[0]["fee"]
        self.assertLessEqual(position["margin"] + fee, 500 + 1e-8)
        self.assertGreater(result["notional"], 2490)

    async def test_partial_reduce_close_releases_proportional_margin_and_net_pnl(self):
        self.account = self.make_account(fee_rate=0.001, slippage_bps=0)
        await self.submit(quantity=10)
        self.assertEqual(self.account.account_snapshot()["equityUSD"], 4999)
        await self.submit(side="sell", quantity=4, reduceOnly=True, price=110, clientOrderID="partial")
        position = self.account.positions()[0]
        self.assertEqual(position["quantity"], 6)
        self.assertEqual(position["entryPrice"], 100)
        self.assertEqual(position["margin"], 300)
        self.assertAlmostEqual(self.account.account_snapshot()["realizedPnLUSD"], 38.56)
        self.assertAlmostEqual(self.account.account_snapshot()["equityUSD"], 5098.56)
        await self.submit(side="sell", quantity=50, reduceOnly=True, price=120, clientOrderID="full")
        self.assertEqual(self.account.positions(), [])
        self.assertEqual(self.account.all_fills()[-1]["quantity"], 6)
        self.assertAlmostEqual(self.account.account_snapshot()["equityUSD"], 5157.84)

    async def test_reduce_only_rejects_same_direction_and_cannot_reverse(self):
        await self.submit(quantity=10)
        with self.assertRaises(PaperTradingError):
            await self.submit(side="buy", reduceOnly=True, clientOrderID="wrong")
        await self.submit(side="sell", quantity=100, reduceOnly=True, clientOrderID="close")
        self.assertEqual(self.account.positions(), [])
        with self.assertRaises(PaperTradingError):
            await self.submit(side="sell", reduceOnly=True, clientOrderID="flat")
        self.assertEqual(len(self.account.all_fills()), 2)

    async def test_limit_reserves_margin_and_matches_executable_ask(self):
        result = await self.account.submit_intent(
            {"instrumentID": BTC, "side": "buy", "quantity": 10, "leverage": 2, "orderType": "limit", "price": 90},
            instrument=self.spec, price=100, quote={"bid": 99, "ask": 101},
        )
        self.assertEqual(result["status"], "live")
        self.assertEqual(self.account.account_snapshot()["availableEquityUSD"], 4549.55)
        self.assertEqual(self.account.positions(), [])
        # Last trade crossed the limit; the ask has not, so no fill is possible.
        await self.account.mark(BTC, 89, bid=88, ask=91)
        self.assertEqual(len(self.account.pending_orders()), 1)
        await self.account.mark(BTC, 89, bid=88, ask=89)
        self.assertEqual(self.account.pending_orders(), [])
        self.assertAlmostEqual(self.account.all_fills()[0]["price"], 89 * 1.0002)
        self.assertLessEqual(self.account.all_fills()[0]["price"], 90)

    async def test_sell_limit_uses_bid_and_never_fills_below_limit(self):
        await self.submit(side="sell", orderType="limit", price=110, clientOrderID="limitshort")
        await self.account.mark(BTC, 111, bid=109, ask=112)
        self.assertEqual(self.account.positions(), [])
        await self.account.mark(BTC, 111, bid=110, ask=112)
        self.assertEqual(self.account.positions()[0]["side"], "short")
        self.assertEqual(self.account.all_fills()[0]["price"], 110)

    async def test_cancel_returns_pending_margin_without_fill(self):
        result = await self.submit(orderType="limit", price=90)
        self.assertEqual(self.account.tracked_instruments(), [BTC])
        await self.account.cancel_order(result["orderID"])
        self.assertEqual(self.account.pending_orders(), [])
        self.assertEqual(self.account.account_snapshot()["availableEquityUSD"], 5000)
        await self.account.mark(BTC, 80)
        self.assertEqual(self.account.all_fills(), [])

    async def test_stop_loss_uses_current_market_after_gap(self):
        self.account = self.make_account(slippage_bps=0)
        await self.submit(stopLossTriggerPrice=95, takeProfitTriggerPrice=110)
        await self.account.mark(BTC, 93)
        self.assertEqual(self.account.positions(), [])
        self.assertEqual(self.account.all_fills()[-1]["price"], 93)
        self.assertEqual(self.account.all_fills()[-1]["reason"], "stop_loss")
        self.assertEqual(self.account.account_snapshot()["todayLossCount"], 1)

    async def test_staged_take_profit_closes_fixed_tranches_once(self):
        self.account = self.make_account(fee_rate=0, slippage_bps=0)
        await self.submit(takeProfitLevels=[{"price": 120, "quantityPercent": 50}, {"price": 110, "quantityPercent": 50}], stopLossTriggerPrice=95)
        await self.account.mark(BTC, 111)
        self.assertEqual(self.account.positions()[0]["quantity"], 5)
        self.assertEqual(self.account.positions()[0]["takeProfitPrice"], 120)
        await self.account.mark(BTC, 112)
        self.assertEqual(len(self.account.all_fills()), 2)
        await self.account.mark(BTC, 121)
        self.assertEqual(self.account.positions(), [])
        self.assertEqual(len(self.account.all_fills()), 3)
        self.assertEqual(self.account.account_snapshot()["equityUSD"], 5160)

    async def test_short_staged_take_profit_rounds_contract_lots(self):
        spec = InstrumentSpec(BTC, 1, lotSize=2, minSize=2)
        await self.submit(instrument=spec, side="sell", quantity=6, takeProfitLevels=[
            {"price": 80, "quantityPercent": 50}, {"price": 90, "quantityPercent": 50}], stopLossTriggerPrice=105)
        await self.account.mark(BTC, 89)
        self.assertEqual(self.account.all_fills()[-1]["quantity"], 2)
        self.assertEqual(self.account.positions()[0]["quantity"], 4)
        await self.account.mark(BTC, 79)
        self.assertEqual(self.account.all_fills()[-1]["quantity"], 4)
        self.assertEqual(self.account.positions(), [])

    async def test_bad_protection_and_duplicate_prices_are_rejected(self):
        for extra in ({"stopLossTriggerPrice": 101}, {"takeProfitTriggerPrice": 99},
                      {"takeProfitLevels": [{"price": 110, "quantityPercent": 50}, {"price": 110, "quantityPercent": 50}]},
                      {"takeProfitLevels": [{"price": 110, "quantityPercent": 60}]}):
            with self.subTest(extra=extra), self.assertRaises(PaperTradingError):
                await self.submit(**extra)
        self.assertEqual(self.account.all_orders(), [])
        self.assertEqual(self.account.account_snapshot()["equityUSD"], 5000)

    async def test_limit_stop_and_targets_align_to_public_price_tick(self):
        await self.submit(orderType="limit", price=90.07, stopLossTriggerPrice=85.07, takeProfitLevels=[
            {"price": 110.07, "quantityPercent": 50}, {"price": 120.09, "quantityPercent": 50}])
        pending = self.account.pending_orders()[0]
        self.assertEqual(pending["price"], 90)
        self.assertEqual(pending["stopLossPrice"], 85)
        self.assertEqual(pending["takeProfitPrice"], 110)
        await self.account.mark(BTC, 90)
        self.assertEqual(self.account.positions()[0]["takeProfitLevels"][-1]["price"], 120)

    async def test_targets_colliding_after_tick_alignment_are_rejected(self):
        with self.assertRaises(PaperTradingError):
            await self.submit(takeProfitLevels=[{"price": 110.01, "quantityPercent": 50}, {"price": 110.09, "quantityPercent": 50}])
        self.assertEqual(self.account.all_orders(), [])

    async def test_pending_entries_reserve_fees_before_acceptance(self):
        self.account = self.make_account(fee_rate=0.01, slippage_bps=0)
        await self.submit(orderType="limit", price=90, quantity=50, leverage=1)
        self.assertEqual(self.account.account_snapshot()["availableEquityUSD"], 455)
        # 450 margin + 4.50 entry fee fits, whereas 540 + 5.40 does not.
        await self.submit(instrument=InstrumentSpec(ETH, 1), orderType="limit", price=90, quantity=5, leverage=1, clientOrderID="fee-covered")
        self.assertEqual(self.account.account_snapshot()["availableEquityUSD"], 0.5)
        await self.account.mark(BTC, 90)
        await self.account.mark(ETH, 90)
        self.assertEqual(len(self.account.positions()), 2)

    async def test_protection_replacement_and_cancellation(self):
        await self.submit(stopLossTriggerPrice=95, takeProfitTriggerPrice=110)
        position_id = self.account.positions()[0]["id"]
        await self.account.mark(BTC, 105)
        await self.account.update_protection(position_id, stop_loss=102, take_profit=120)
        self.assertEqual(self.account.positions()[0]["stopLossPrice"], 102)
        await self.account.cancel_protection(position_id)
        await self.account.mark(BTC, 101)
        self.assertEqual(len(self.account.positions()), 1)

    async def test_protection_update_only_moves_toward_profit_for_long_and_short(self):
        await self.submit(stopLossTriggerPrice=95, takeProfitTriggerPrice=110)
        position_id = self.account.positions()[0]["id"]
        await self.account.mark(BTC, 105)
        await self.account.update_protection(position_id, stop_loss=102, take_profit=120)
        with self.assertRaises(PaperTradingError):
            await self.account.update_protection(position_id, stop_loss=99, take_profit=130)
        self.assertEqual(self.account.positions()[0]["stopLossPrice"], 102)
        self.assertEqual(self.account.positions()[0]["takeProfitPrice"], 120)

        short_spec = InstrumentSpec(ETH, 1, lotSize=1, minSize=1, tickSize=0.1)
        await self.submit(instrument=short_spec, side="sell", price=100, stopLossTriggerPrice=105, takeProfitTriggerPrice=90, clientOrderID="short-entry")
        short_position_id = next(position["id"] for position in self.account.positions() if position["side"] == "short")
        await self.account.mark(ETH, 95)
        await self.account.update_protection(short_position_id, stop_loss=102, take_profit=80)
        with self.assertRaises(PaperTradingError):
            await self.account.update_protection(short_position_id, stop_loss=108, take_profit=70)
        short_position = next(position for position in self.account.positions() if position["side"] == "short")
        self.assertEqual(short_position["stopLossPrice"], 102)
        self.assertEqual(short_position["takeProfitPrice"], 80)

    async def test_restoring_only_take_profit_preserves_existing_stop(self):
        await self.submit(stopLossTriggerPrice=95)
        position_id = self.account.positions()[0]["id"]
        await self.account.update_protection(position_id, take_profit=110)
        self.assertEqual(self.account.positions()[0]["stopLossPrice"], 95)
        self.assertEqual(self.account.positions()[0]["takeProfitPrice"], 110)

    async def test_restoring_only_stop_preserves_existing_take_profit(self):
        await self.submit(takeProfitTriggerPrice=110)
        position_id = self.account.positions()[0]["id"]
        await self.account.update_protection(position_id, stop_loss=95)
        self.assertEqual(self.account.positions()[0]["stopLossPrice"], 95)
        self.assertEqual(self.account.positions()[0]["takeProfitPrice"], 110)

    async def test_stop_update_does_not_rearm_executed_staged_targets(self):
        await self.submit(takeProfitLevels=[{"price": 110, "quantityPercent": 50}, {"price": 120, "quantityPercent": 50}])
        position_id = self.account.positions()[0]["id"]
        await self.account.mark(BTC, 111)
        self.assertEqual(self.account.positions()[0]["quantity"], 5)
        original_targets = self.account.positions()[0]["takeProfitLevels"]
        await self.account.update_protection(position_id, stop_loss=105)
        self.assertEqual(self.account.positions()[0]["takeProfitLevels"], original_targets)
        await self.account.mark(BTC, 112)
        self.assertEqual(self.account.positions()[0]["quantity"], 5)
        await self.account.mark(BTC, 121)
        self.assertEqual(self.account.positions(), [])
        self.assertEqual(self.account.all_fills()[-1]["quantity"], 5)

    async def test_idempotence_and_open_exposure_survive_restart(self):
        result = await self.submit(strategyID="codex")
        self.account = self.make_account()
        retry = await self.submit(strategyID="codex", price=200)
        self.assertEqual(result, retry)
        self.assertEqual(len(self.account.all_orders()), 1)
        self.assertEqual(len(self.account.all_fills()), 1)
        UUID(self.account.all_orders()[0]["id"])
        UUID(self.account.all_orders()[0]["strategyID"])
        UUID(self.account.all_fills()[0]["orderID"])
        self.assertIsNotNone(self.account.lookup_order(BTC, "entry1"))
        with self.assertRaises(PaperTradingError):
            await self.submit(clientOrderID="duplicate-exposure")

    async def test_pending_idempotence_and_matching_survive_restart(self):
        result = await self.submit(orderType="limit", price=90)
        self.account = self.make_account()
        self.assertEqual(len(self.account.pending_orders()), 1)
        retry = await self.submit(orderType="limit", price=90)
        self.assertEqual(result, retry)
        await self.account.mark(BTC, 89)
        self.account = self.make_account()
        self.assertEqual(self.account.pending_orders(), [])
        self.assertEqual(len(self.account.positions()), 1)
        self.assertEqual(self.account.daily_order_count(), 1)

    async def test_ai_deadline_daily_limit_and_pending_duplicate(self):
        with self.assertRaises(PaperTradingError):
            await self.submit(source="ai", _aiEntryDeadline=self.now.timestamp() - 1)
        await self.submit(source="ai", _aiEntryDeadline=(self.now + timedelta(seconds=30)).timestamp(), orderType="limit", price=90)
        with self.assertRaises(PaperTradingError):
            await self.submit(source="ai", _aiEntryDeadline=(self.now + timedelta(seconds=30)).timestamp(), clientOrderID="again")
        await self.account.cancel_pending_orders()
        with self.assertRaises(PaperTradingError):
            await self.submit(source="ai", _aiEntryDeadline=(self.now + timedelta(seconds=30)).timestamp(), clientOrderID="limit", _dailyOrderLimit=1)

    async def test_ai_reentry_cooldown_survives_restart_but_manual_entry_is_allowed(self):
        self.account = self.make_account(
            stop_loss_cooldown_seconds=3600,
            recent_stop_loss_window_seconds=3600,
            recent_stop_loss_limit=10,
            fee_rate=0,
            slippage_bps=0,
        )
        deadline = (self.now + timedelta(minutes=5)).timestamp()
        await self.submit(source="ai", _aiEntryDeadline=deadline, stopLossTriggerPrice=95)
        await self.account.mark(BTC, 90)
        self.account = self.make_account(
            stop_loss_cooldown_seconds=3600,
            recent_stop_loss_window_seconds=3600,
            recent_stop_loss_limit=10,
            fee_rate=0,
            slippage_bps=0,
        )
        with self.assertRaisesRegex(PaperTradingError, "stop-loss cooldown"):
            await self.submit(source="ai", _aiEntryDeadline=deadline, clientOrderID="ai-reentry")
        manual = await self.submit(clientOrderID="manual-reentry")
        self.assertEqual(manual["status"], "filled")

    async def test_ai_stop_loss_burst_pauses_all_entries_until_window_expires(self):
        self.account = self.make_account(
            stop_loss_cooldown_seconds=0,
            recent_stop_loss_window_seconds=3600,
            recent_stop_loss_limit=2,
            fee_rate=0,
            slippage_bps=0,
        )
        deadline = (self.now + timedelta(minutes=5)).timestamp()
        await self.submit(source="ai", _aiEntryDeadline=deadline, stopLossTriggerPrice=95, clientOrderID="btc-ai")
        await self.account.mark(BTC, 90)
        eth = InstrumentSpec(ETH, 1, lotSize=1, minSize=1, tickSize=0.1)
        await self.submit(instrument=eth, source="ai", _aiEntryDeadline=deadline, stopLossTriggerPrice=95, clientOrderID="eth-ai")
        await self.account.mark(ETH, 90)
        with self.assertRaisesRegex(PaperTradingError, "stop-loss exits"):
            await self.submit(source="ai", _aiEntryDeadline=deadline, clientOrderID="btc-after-burst")
        self.now += timedelta(seconds=3601)
        reopened = await self.submit(source="ai", _aiEntryDeadline=(self.now + timedelta(minutes=5)).timestamp(), clientOrderID="btc-after-window")
        self.assertEqual(reopened["status"], "filled")

    async def test_liquidation_loss_is_limited_to_isolated_margin(self):
        self.account = self.make_account(fee_rate=0, slippage_bps=0)
        await self.submit(quantity=100, leverage=10)
        await self.account.mark(BTC, 50)
        self.assertEqual(self.account.positions(), [])
        self.assertEqual(self.account.account_snapshot()["equityUSD"], 4000)
        self.assertEqual(self.account.all_fills()[-1]["reason"], "liquidation")
        self.assertEqual(self.account.account_snapshot()["todayPnLUSD"], -1000)
        close_event = next(event for event in self.account.audit() if event["type"] == "position-closed")
        self.assertEqual(close_event["realizedPnL"], -1000)
        self.assertEqual(close_event["grossPnL"], -1000)
        self.assertEqual(close_event["isolatedSettlementAdjustment"], 4000)
        self.assertTrue(self.account.risk_snapshot()["killSwitch"])

    async def test_daily_risk_baseline_latch_restart_and_next_day_reset(self):
        self.account = self.make_account(fee_rate=0, slippage_bps=0)
        await self.submit(quantity=50, leverage=2)
        await self.account.mark(BTC, 94)
        self.assertEqual(self.account.risk_snapshot()["dailyPnLPercent"], -6)
        self.assertTrue(self.account.risk_snapshot()["killSwitch"])
        self.account = self.make_account(fee_rate=0, slippage_bps=0)
        self.assertTrue((await self.account.reset_risk())["killSwitch"])
        self.now += timedelta(days=1)
        await self.account.mark(BTC, 94)
        self.assertEqual(self.account.risk_snapshot()["dayStartEquity"], 4700)
        self.assertTrue(self.account.risk_snapshot()["killSwitch"])
        self.assertFalse((await self.account.reset_risk())["killSwitch"])
        self.account = self.make_account(fee_rate=0, slippage_bps=0)
        self.assertEqual(self.account.daily_order_count(), 0)
        self.assertEqual(self.account.risk_snapshot()["dayStartEquity"], 4700)

    async def test_daily_loss_breaker_uses_the_configured_percentage(self) -> None:
        # The account-level threshold is a parameter, not a literal: a wider
        # cap must not trip at the default 5%, and an invalid value is refused.
        self.account = self.make_account(fee_rate=0, slippage_bps=0, max_daily_loss_percent=10)
        await self.submit(quantity=50, leverage=2)
        await self.account.mark(BTC, 94)
        self.assertEqual(self.account.risk_snapshot()["dailyPnLPercent"], -6)
        self.assertFalse(self.account.risk_snapshot()["killSwitch"])
        await self.account.mark(BTC, 89)
        self.assertLessEqual(self.account.risk_snapshot()["dailyPnLPercent"], -10)
        self.assertTrue(self.account.risk_snapshot()["killSwitch"])
        self.assertIn("10%", self.account.risk_snapshot()["reason"])
        for invalid in (0, -1, 100, 250):
            with self.subTest(invalid=invalid), self.assertRaises(PaperTradingError):
                self.make_account(max_daily_loss_percent=invalid)

    async def test_account_breaker_cancels_entries_and_flattens_positions(self):
        self.account = self.make_account(fee_rate=0, slippage_bps=0)
        await self.submit(quantity=50, leverage=2)
        await self.submit(instrument=InstrumentSpec(ETH, 1), quantity=5, orderType="limit", price=90, clientOrderID="eth-limit")
        await self.account.mark(BTC, 94)
        self.assertTrue(self.account.risk_snapshot()["killSwitch"])
        self.assertEqual(self.account.positions(), [])
        self.assertEqual(self.account.pending_orders(), [])
        await self.account.mark(ETH, 80)
        self.assertEqual(self.account.positions(), [])
        self.assertEqual(self.account.all_orders()[1]["status"], "canceled")
        self.assertEqual(self.account.all_fills()[-1]["reason"], "risk_kill_switch")

    async def test_flatten_cancels_limits_closes_positions_and_reset_erases_history(self):
        await self.submit()
        eth_spec = InstrumentSpec(ETH, 1)
        await self.submit(instrument=eth_spec, orderType="limit", price=90, clientOrderID="ethpending")
        result = await self.account.flatten({BTC: 105})
        self.assertEqual(len(result["cancelledOrderIDs"]), 1)
        self.assertEqual(len(result["closed"]), 1)
        self.assertEqual(self.account.positions(), [])
        self.assertEqual(self.account.pending_orders(), [])
        self.assertEqual(len(self.account.all_fills()), 2)
        await self.account.reset(5000)
        self.account = self.make_account()
        self.assertEqual(self.account.account_snapshot()["equityUSD"], 5000)
        self.assertEqual(self.account.all_fills(), [])
        self.assertEqual(self.account.all_orders(), [])
        self.assertEqual(self.account.audit(), [])
        self.assertEqual(self.account.daily_order_count(), 0)

    async def test_corrupt_state_never_silently_mints_initial_balance(self):
        self.path.write_text('{"cash": 1}', encoding="utf-8")
        with self.assertRaises(PaperTradingError):
            self.make_account()
        self.assertEqual(json.loads(self.path.read_text())["cash"], 1)


if __name__ == "__main__":
    unittest.main()
