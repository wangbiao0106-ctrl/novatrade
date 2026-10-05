#!/usr/bin/env python3
"""Small contract check for the FastAPI loopback gateway helpers."""

import os
import sys
import unittest
import asyncio
import base64
import hashlib
import hmac
from pathlib import Path
from types import SimpleNamespace
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import main  # noqa: E402


class GatewayContractTests(unittest.TestCase):
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
