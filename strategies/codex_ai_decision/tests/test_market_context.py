"""Public evidence, quality and holistic prompt tests without live requests."""
from copy import deepcopy
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import re
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from backend import ai_market_context as context
from backend.ai_market_facts import market_facts
from backend.ai_schema import AIConfig, AISnapshot
from backend.ai_trigger import decision_fingerprint
from backend.ai_worker import CodexRunner, _prompt_snapshot
from strategies.codex_ai_decision.tests.test_holistic_scan import BTC, candles, snapshot


class PublicContextTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.stamp = str(int(self.now.timestamp() * 1000))

    def test_oi_uses_exact_contract_and_exchange_units(self):
        row = {"instId": BTC, "ts": self.stamp, "oi": "100", "oiCcy": "1", "oiUsd": "1200"}
        result = context.parse_derivative("openInterest", {"code": "0", "data": [row]}, BTC, self.now)
        self.assertEqual((result["contracts"], result["baseCurrency"], result["usd"]), (100, 1, 1200))
        self.assertEqual(result["scope"], BTC)
        for changes in ({"instId": "ETH-USDT-SWAP"}, {"ts": "1"}, {"oi": True}, {"oiUsd": "NaN"}, {"oi": "-1"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                context.parse_derivative("openInterest", {"data": [{**row, **changes}]}, BTC, self.now)

    def test_rubik_scope_time_units_column_order_and_flow_share(self):
        response = {"data": [[self.stamp, "25", "75"], [str(int(self.stamp) - 300_000), "10", "20"]]}
        result = context.parse_derivative("takerVolume", response, BTC, self.now)
        self.assertIn("currency-wide", result["scope"])
        self.assertEqual(result["period"], "5m")
        self.assertEqual(result["columns"], ["timestampUnixMilliseconds", "sellVolumeUSD", "buyVolumeUSD"])
        self.assertEqual(result["latestBuyShare"], .75)
        ratio = context.parse_derivative("longShortAccountRatio", {"data": [[self.stamp, "1.42"]]}, BTC, self.now)
        self.assertEqual(ratio["rows"][0][1], 1.42)
        zero = context.parse_derivative("takerVolume", {"data": [[self.stamp, "0", "0"]]}, BTC, self.now)
        self.assertIsNone(zero["latestBuyShare"])

    def test_rubik_sorts_caps_history_and_rejects_stale_future_malformed_sources(self):
        rows = [[str(int(self.stamp) - index * 300_000), "1"] for index in range(20)]
        result = context.parse_derivative("longShortAccountRatio", {"data": list(reversed(rows))}, BTC, self.now)
        self.assertEqual(result["rows"], [[int(row[0]), 1] for row in rows[:12]])
        invalids = [{"code": "50011", "data": rows}, {"data": []}, {"data": [rows[0], rows[0]]},
                    {"data": [[self.stamp]]}, {"data": [["1", "1"]]},
                    {"data": [[str(int(self.stamp) + 120_000), "1"]]}, {"data": [[self.stamp, "Infinity"]]}]
        for response in invalids:
            with self.subTest(response=response), self.assertRaises(ValueError):
                context.parse_derivative("longShortAccountRatio", response, BTC, self.now)

    def test_sentiment_is_daily_market_proxy_with_observation_time(self):
        row = {"timestamp": str(int(self.now.timestamp()) - 86400), "value": "59", "value_classification": "Greed"}
        result = context.parse_sentiment({"data": [row]}, self.now)
        self.assertEqual(result["value"], 59)
        self.assertIn("not per-contract", result["scope"])
        self.assertGreaterEqual(result["ageSeconds"], 86400)
        for changes in ({"timestamp": "1"}, {"value": "101"}, {"value": True}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                context.parse_sentiment({"data": [{**row, **changes}]}, self.now)

    def feed(self):
        published = (self.now - timedelta(days=20)).strftime("%a, %d %b %Y %H:%M:%S GMT")
        return f'''<rss><channel><item><title>Federal Reserve issues FOMC statement</title>
        <link>https://www.federalreserve.gov/newsevents/pressreleases/monetary20260916a.htm</link>
        <description><![CDATA[<p>Official policy announcement</p>]]></description><pubDate>{published}</pubDate>
        </item></channel></rss>'''

    def test_fed_keeps_historical_publication_age_and_official_link(self):
        result = context.parse_fed_feed(self.feed(), self.now)
        article = result["articles"][0]
        self.assertEqual(article["summary"], "Official policy announcement")
        self.assertGreaterEqual(article["publicationAgeSeconds"], 20 * 86400)
        self.assertNotEqual(article["publishedAt"], result["receivedAt"])
        self.assertIn("no future meeting calendar", result["scope"])

    def test_fed_rejects_external_links_entity_definitions_and_bad_dates(self):
        for xml in (self.feed().replace("www.federalreserve.gov", "example.com"),
                    '<!DOCTYPE rss [<!ENTITY x "test">]>' + self.feed(),
                    re.sub(r"<pubDate>.*?</pubDate>", "<pubDate>invalid</pubDate>", self.feed()), "x" * 512_001,
                    self.feed().replace("https://www.federalreserve.gov", "https://www.federalreserve.gov:443")):
            with self.subTest(xml=xml[:80]), self.assertRaises(ValueError):
                context.parse_fed_feed(xml, self.now)

    def test_statement_excerpt_ignores_navigation(self):
        parser = context._ArticleText()
        parser.feed('<nav>ignore</nav><div id="article"><p>The Committee set the rate range.</p><div><p>Second paragraph.</p></div></div><p>Footer</p>')
        text = " ".join(parser.parts)
        self.assertIn("Committee", text)
        self.assertIn("Second", text)
        self.assertNotIn("Footer", text)
        self.assertNotIn("ignore", text)


class ContextCollectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        context._GLOBAL_CACHE.clear()

    async def test_okx_v5_routes_partial_failure_and_actual_derivatives_reach_prompt(self):
        stamp = str(int(datetime.now(timezone.utc).timestamp() * 1000))
        requests = []

        async def okx(path, params):
            requests.append((path, params))
            if path == "/public/open-interest":
                return {"code": "0", "data": [{"instId": params["instId"], "ts": stamp, "oi": "100", "oiCcy": "1", "oiUsd": "1000"}]}
            if path.endswith("long-short-account-ratio"):
                return {"data": [[stamp, "1.4"]]}
            if path.endswith("taker-volume"):
                raise httpx.ReadTimeout("unsupported source")
            return {"data": [{"instId": params["instId"], "ts": stamp, "last": "100", "open24h": "80"}]}

        with patch.object(context, "_global_source", new=AsyncMock(return_value={"available": False, "error": "offline"})):
            derivatives, globals_ = await context.collect_market_context([BTC], okx)
        self.assertTrue(derivatives[BTC]["openInterest"]["available"])
        self.assertTrue(derivatives[BTC]["longShortAccountRatio"]["available"])
        self.assertFalse(derivatives[BTC]["takerVolume"]["available"])
        self.assertEqual(globals_["benchmarks"][BTC]["return24hPercent"], 25)
        self.assertIn(("/public/open-interest", {"instType": "SWAP", "instId": BTC}), requests)
        self.assertIn(("/rubik/stat/taker-volume", {"ccy": "BTC", "instType": "CONTRACTS", "period": "5m"}), requests)
        source = replace(snapshot(), derivatives=derivatives, marketContext=globals_)
        restored = AISnapshot.from_dict(source.to_dict())
        self.assertEqual(restored.derivatives, derivatives)
        prompt = CodexRunner._decision_prompt(restored, AIConfig())
        self.assertIn('"longShortAccountRatio"', prompt)
        self.assertIn('"return24hPercent":25.0', prompt)
        self.assertIn("optional exchange source unavailable", prompt)
        self.assertEqual(next(iter(_prompt_snapshot(source)["candles"])), BTC + "/15m")

    async def test_unsupported_optional_sources_remain_unknown_and_all_contracts_remain(self):
        with patch.object(context, "_global_source", new=AsyncMock(return_value={"available": False})):
            derivatives, global_ = await context.collect_market_context([BTC, "ALT-USDT-SWAP"], AsyncMock(return_value={"data": []}))
        self.assertEqual(set(derivatives), {BTC, "ALT-USDT-SWAP"})
        for measurements in derivatives.values():
            for value in measurements.values():
                self.assertFalse(value["available"])
                self.assertNotIn("rows", value)
        self.assertFalse(global_["benchmarks"][BTC]["available"])

    async def test_optional_deadline_returns_partial_state_and_cancels_requests(self):
        active = 0

        async def waiting(*args):
            nonlocal active
            active += 1
            try:
                await asyncio.Event().wait()
            finally:
                active -= 1

        with patch.object(context, "COLLECTION_TIMEOUT_SECONDS", .01), \
             patch.object(context, "_global_source", new=waiting):
            derivatives, globals_ = await context.collect_market_context([BTC], waiting)
        self.assertEqual(active, 0)
        self.assertFalse(derivatives[BTC]["openInterest"]["available"])
        self.assertIn("deadline", globals_["sentiment"]["error"])

    async def test_rubik_dispatch_is_paced_per_endpoint(self):
        times = {}

        async def okx(path, params):
            times.setdefault(path, []).append(asyncio.get_running_loop().time())
            return {"data": []}

        with patch.object(context, "_global_source", new=AsyncMock(return_value={"available": False})):
            await context.collect_market_context([BTC, "ALT-USDT-SWAP"], okx)
        for path, starts in times.items():
            if path.startswith("/rubik/"):
                self.assertEqual(len(starts), 2)
                self.assertGreaterEqual(starts[1] - starts[0], .49)

    async def test_collected_evidence_survives_runtime_snapshot_and_snapshot_hash(self):
        from backend import main
        from scripts.test_ai_snapshot import AISnapshotTests
        derivatives = {"COIN0-USDT-SWAP": {"openInterest": {"available": True, "contracts": 123}}}
        globals_ = {"sentiment": {"available": True, "value": 59}}
        fixtures = AISnapshotTests()
        with patch.object(main, "now_iso", return_value="2026-10-09T14:00:00Z"):
            enriched, _ = await fixtures.snapshot(count=1, context=(derivatives, globals_))
            basic, _ = await fixtures.snapshot(count=1)
        self.assertEqual(enriched.derivatives, derivatives)
        self.assertEqual(enriched.marketContext, globals_)
        self.assertNotEqual(enriched.snapshotId, basic.snapshotId)
        self.assertEqual(AISnapshot.from_dict(enriched.to_dict()).marketContext, globals_)

    async def test_global_cache_preserves_source_age_and_does_not_repeat_requests(self):
        stamp = str(int(datetime.now(timezone.utc).timestamp()) - 86400)
        raw = json.dumps({"data": [{"timestamp": stamp, "value": "59"}]})
        with patch.object(context, "_public_text", new=AsyncMock(return_value=raw)) as fetch:
            first = await context._global_source(None, "sentiment")
            second = await context._global_source(None, "sentiment")
        fetch.assert_awaited_once()
        self.assertTrue(second["cached"])
        self.assertEqual(first["receivedAt"], second["receivedAt"])
        self.assertEqual(first["sourceTimestampUnixMilliseconds"], second["sourceTimestampUnixMilliseconds"])
        self.assertGreaterEqual(second["ageSeconds"], 86400)

    async def test_cache_cannot_make_stale_sentiment_usable(self):
        import time
        context._GLOBAL_CACHE["sentiment"] = (time.monotonic() + 600, {
            "available": True, "sourceTimestampUnixMilliseconds": 1,
        })
        result = await context._global_source(None, "sentiment")
        self.assertFalse(result["available"])

    async def test_official_body_fetch_and_failure_are_explicit(self):
        feed = PublicContextTests()
        feed.setUp()
        body = '<div id="article"><p>Target range is maintained at the stated level.</p></div>'
        for outcome in (body, httpx.ReadTimeout("offline")):
            context._GLOBAL_CACHE.clear()
            with patch.object(context, "_public_text", new=AsyncMock(side_effect=[feed.feed(), outcome])):
                result = await context._global_source(None, "fedPolicy")
            self.assertTrue(result["available"])
            excerpt = result["articles"][0]["bodyExcerpt"]
            self.assertEqual(excerpt["available"], outcome == body)
            if outcome == body:
                self.assertIn("Target range", excerpt["text"])


class IndicatorAndFingerprintTests(unittest.TestCase):
    def facts(self, rows):
        source = replace(snapshot(), candles={BTC + "/15m": rows})
        return market_facts(source)["instruments"][BTC]["timeframes"]["15m"]

    def test_rsi_volatility_and_volume_weighted_price_have_known_arithmetic(self):
        rows = candles(20)
        result = self.facts(rows)
        self.assertEqual(result["rsi14Simple"], 100)
        expected = sum((row["high"] + row["low"] + row["close"]) / 3 for row in rows) / 20
        self.assertAlmostEqual(result["rollingTypicalPriceVWAP20"], expected)
        self.assertAlmostEqual(result["trueRangePercent"], result["meanTrueRange14"] / rows[-1]["close"] * 100)
        for row in rows:
            row.update(open=100, close=100, high=101, low=99)
        self.assertEqual(self.facts(rows)["rsi14Simple"], 50)

    def test_incomplete_invalid_or_zero_volume_does_not_fabricate_indicators(self):
        short = self.facts(candles(1))
        for key in ("rsi14Simple", "rollingTypicalPriceVWAP20", "trueRangePercent"):
            self.assertIsNone(short[key])
        rows = candles(20)
        for row in rows:
            row["volume"] = 0
        self.assertIsNone(self.facts(rows)["rollingTypicalPriceVWAP20"])
        rows = candles(20)
        rows[-1]["volume"] = None
        self.assertIsNone(self.facts(rows)["rollingTypicalPriceVWAP20"])
        rows[-1]["high"] = 1
        self.assertIsNone(self.facts(rows)["rsi14Simple"])

    def test_news_and_derivative_changes_trigger_evaluation_but_receipt_age_does_not(self):
        source = replace(snapshot(), derivatives={BTC: {"openInterest": {"contracts": 100, "available": True}}},
                         marketContext={"sentiment": {"value": 59, "available": True, "ageSeconds": 100},
                                        "fedPolicy": {"articles": [{"title": "Statement", "publishedAt": "2026-10-01T00:00:00Z"}]}})
        config = AIConfig()
        original = decision_fingerprint(source, config)
        changed = replace(source, marketContext=deepcopy(source.marketContext))
        changed.marketContext["sentiment"].update(receivedAt="2026-10-09T00:00:00Z", ageSeconds=200, cached=True)
        self.assertEqual(decision_fingerprint(changed, config), original)
        changed.marketContext["sentiment"]["value"] = 60
        self.assertNotEqual(decision_fingerprint(changed, config), original)
        changed = replace(source, derivatives=deepcopy(source.derivatives))
        changed.derivatives[BTC]["openInterest"]["contracts"] = 101
        self.assertNotEqual(decision_fingerprint(changed, config), original)
        changed = replace(source, marketContext=deepcopy(source.marketContext))
        changed.marketContext["fedPolicy"]["articles"][0]["title"] = "New policy statement"
        self.assertNotEqual(decision_fingerprint(changed, config), original)


if __name__ == "__main__":
    unittest.main()
