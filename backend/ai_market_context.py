"""Optional, bounded public evidence for holistic AI decisions.

No credentials, directions or trading gates live here. Currency-wide Rubik
statistics and daily sentiment must not be presented as single-contract data.
"""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
import math
import re
import time
from typing import Any
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx


SENTIMENT_URL = "https://api.alternative.me/fng/?limit=1"
FED_URL = "https://www.federalreserve.gov/feeds/press_monetary.xml"
GLOBAL_CACHE_SECONDS = 600
DERIVATIVES_MAX_AGE_SECONDS = 1800
SENTIMENT_MAX_AGE_SECONDS = 172800
COLLECTION_TIMEOUT_SECONDS = 18
_GLOBAL_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}


def _iso(now: datetime) -> str:
    return now.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _number(value: Any, *, positive: bool = False) -> float:
    if isinstance(value, bool):
        raise ValueError("boolean measurement")
    result = float(value)
    if not math.isfinite(result) or result < 0 or (positive and result <= 0):
        raise ValueError("invalid measurement")
    return result


def _age(timestamp_ms: Any, now: datetime, max_age: int) -> tuple[int, float]:
    stamp = _number(timestamp_ms, positive=True)
    if not stamp.is_integer():
        raise ValueError("timestamp must be whole milliseconds")
    age = now.timestamp() - stamp / 1000
    if age < -60 or age > max_age:
        raise ValueError("source observation is stale or in the future")
    return int(stamp), round(age, 3)


def unavailable(source: str, scope: str, error: str, now: datetime) -> dict[str, Any]:
    return {"available": False, "source": source, "scope": scope, "receivedAt": _iso(now), "error": error}


def parse_derivative(resource: str, response: Any, instrument: str, now: datetime) -> dict[str, Any]:
    """Validate original exchange units, timestamp and contract/currency scope."""
    if not isinstance(response, dict) or str(response.get("code", "0")) != "0":
        raise ValueError("exchange returned an error")
    rows = response.get("data")
    if not isinstance(rows, list) or not rows:
        raise ValueError("exchange returned no measurements")
    if resource == "openInterest":
        row = rows[0]
        if not isinstance(row, dict) or row.get("instId") != instrument:
            raise ValueError("open interest contract mismatch")
        stamp, age = _age(row.get("ts"), now, DERIVATIVES_MAX_AGE_SECONDS)
        return {"available": True, "scope": instrument, "source": "OKX /public/open-interest",
                "receivedAt": _iso(now), "sourceTimestampUnixMilliseconds": stamp, "ageSeconds": age,
                "contracts": _number(row.get("oi")), "baseCurrency": _number(row.get("oiCcy")),
                "usd": _number(row.get("oiUsd"))}
    columns = ["timestampUnixMilliseconds", "longShortAccountRatio"] if resource == "longShortAccountRatio" else [
        "timestampUnixMilliseconds", "sellVolumeUSD", "buyVolumeUSD"]
    parsed = []
    for row in rows:
        if not isinstance(row, list) or len(row) != len(columns):
            raise ValueError("invalid Rubik measurement columns")
        stamp = _number(row[0], positive=True)
        if not stamp.is_integer():
            raise ValueError("invalid Rubik timestamp")
        parsed.append([int(stamp), *[_number(value) for value in row[1:]]])
    parsed.sort(key=lambda row: row[0], reverse=True)
    if len({row[0] for row in parsed}) != len(parsed):
        raise ValueError("duplicate Rubik timestamps")
    stamp, age = _age(parsed[0][0], now, DERIVATIVES_MAX_AGE_SECONDS)
    result = {"available": True, "source": "OKX Rubik " + resource,
              "scope": instrument.split("-")[0] + " currency-wide contracts (not this swap alone)",
              "period": "5m", "receivedAt": _iso(now), "sourceTimestampUnixMilliseconds": stamp,
              "ageSeconds": age, "columns": columns, "rows": parsed[:12]}
    if resource == "takerVolume":
        sell, buy = parsed[0][1:]
        scale = max(sell, buy)
        result["latestBuyShare"] = (buy / scale) / (buy / scale + sell / scale) if scale else None
    return result


def parse_sentiment(response: Any, now: datetime) -> dict[str, Any]:
    row = response["data"][0]
    value = _number(row["value"])
    if value > 100:
        raise ValueError("sentiment outside 0..100")
    stamp, age = _age(_number(row["timestamp"], positive=True) * 1000, now, SENTIMENT_MAX_AGE_SECONDS)
    return {"available": True, "source": SENTIMENT_URL, "receivedAt": _iso(now),
            "scope": "Daily crypto market/BTC proxy, not per-contract sentiment",
            "value": value, "classification": str(row.get("value_classification", ""))[:80],
            "sourceTimestampUnixMilliseconds": stamp, "ageSeconds": age}


def _text(value: str, limit: int) -> str:
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]*>", " ", value))).strip()[:limit]


def _fed_url(value: str) -> bool:
    url = urlsplit(value)
    return (url.scheme == "https" and url.netloc == "www.federalreserve.gov"
            and url.path.startswith("/newsevents/pressreleases/monetary") and not url.query)


def parse_fed_feed(xml: str, now: datetime) -> dict[str, Any]:
    if len(xml.encode("utf-8")) > 512_000 or "<!DOCTYPE" in xml.upper() or "<!ENTITY" in xml.upper():
        raise ValueError("unsafe or oversized feed")
    root = ET.fromstring(xml.lstrip("\ufeff"))
    articles = []
    for item in root.findall("./channel/item"):
        link = (item.findtext("link") or "").strip()
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "")
            if published.tzinfo is None or (published - now).total_seconds() > 60 or not _fed_url(link):
                continue
        except (ValueError, TypeError, OverflowError):
            continue
        articles.append({"title": _text(item.findtext("title") or "", 300), "url": link,
                         "summary": _text(item.findtext("description") or "", 1200),
                         "publishedAt": _iso(published), "publicationAgeSeconds": round((now - published).total_seconds())})
    articles.sort(key=lambda row: row["publishedAt"], reverse=True)
    if not articles:
        raise ValueError("no valid official monetary policy publications")
    return {"available": True, "source": FED_URL, "scope": "Official historical monetary policy publications; no future meeting calendar",
            "receivedAt": _iso(now), "articles": articles[:6]}


class _ArticleText(HTMLParser):
    """Collect official article paragraphs, excluding navigation/scripts."""
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.paragraph = False
        self.parts: list[str] = []
        self.suppressed = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        if tag in {"script", "style"}:
            self.suppressed += 1
        if tag == "div" and (self.depth or attributes.get("id") == "article"):
            self.depth += 1
        if self.depth and tag == "p":
            self.paragraph = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"} and self.suppressed:
            self.suppressed -= 1
        if tag == "div" and self.depth:
            self.depth -= 1
        if tag == "p":
            self.paragraph = False
            self.parts.append("\n")

    def handle_data(self, data):
        if self.depth and self.paragraph and not self.suppressed:
            self.parts.append(data)


async def _public_text(client: httpx.AsyncClient, url: str) -> str:
    # Separate client from authenticated OKX transport; fixed trusted URLs,
    # no redirects and bounded streamed response size.
    async with client.stream("GET", url) as response:
        response.raise_for_status()
        chunks = bytearray()
        async for chunk in response.aiter_bytes():
            chunks.extend(chunk)
            if len(chunks) > 512_000:
                raise ValueError("public response exceeds size budget")
        return chunks.decode("utf-8-sig")


async def _global_source(client: httpx.AsyncClient, resource: str) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    cached = _GLOBAL_CACHE.get(resource)
    if cached is not None and time.monotonic() < cached[0]:
        result = deepcopy(cached[1])
        if resource == "sentiment" and result.get("available"):
            try:
                _, result["ageSeconds"] = _age(result["sourceTimestampUnixMilliseconds"], now, SENTIMENT_MAX_AGE_SECONDS)
            except ValueError:
                result = unavailable(SENTIMENT_URL, "daily market/BTC proxy", "cached source observation is stale", now)
        if resource == "fedPolicy" and result.get("available"):
            for article in result["articles"]:
                published = datetime.fromisoformat(article["publishedAt"].replace("Z", "+00:00"))
                article["publicationAgeSeconds"] = round((now - published).total_seconds())
        result["cached"] = True
        return result
    url = SENTIMENT_URL if resource == "sentiment" else FED_URL
    try:
        raw = await _public_text(client, url)
        if resource == "sentiment":
            import json
            result = parse_sentiment(json.loads(raw), now)
        else:
            result = parse_fed_feed(raw, now)
            statement = next((article for article in result["articles"] if "FOMC statement" in article["title"]), None)
            if statement:
                try:
                    parser = _ArticleText()
                    parser.feed(await _public_text(client, statement["url"]))
                    excerpt = _text(" ".join(parser.parts), 6000)
                    if not excerpt:
                        raise ValueError("official statement body unavailable")
                    statement["bodyExcerpt"] = {"available": True, "text": excerpt, "maxCharacters": 6000}
                except (httpx.HTTPError, ValueError, UnicodeError):
                    statement["bodyExcerpt"] = {"available": False, "error": "official statement body fetch failed"}
    except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, OverflowError, ET.ParseError, UnicodeError):
        result = unavailable(url, "market/BTC proxy" if resource == "sentiment" else "official monetary policy feed", "public source unavailable or invalid", now)
    _GLOBAL_CACHE[resource] = (time.monotonic() + (GLOBAL_CACHE_SECONDS if result["available"] else 60), deepcopy(result))
    return result


async def collect_market_context(instruments: list[str], okx_get) -> tuple[dict[str, Any], dict[str, Any]]:
    """Collect optional context alongside core prices; return partial evidence."""
    now = datetime.now(timezone.utc)
    derivatives = {instrument: {resource: unavailable("OKX " + resource, instrument, "optional collection deadline reached", now)
                               for resource in ("openInterest", "longShortAccountRatio", "takerVolume")}
                   for instrument in instruments}
    context = {resource: unavailable(url, "global context", "optional collection deadline reached", now)
               for resource, url in (("sentiment", SENTIMENT_URL), ("fedPolicy", FED_URL))}
    context["benchmarks"] = {instrument: unavailable("OKX /market/ticker", "market benchmark", "optional collection deadline reached", now)
                             for instrument in ("BTC-USDT-SWAP", "ETH-USDT-SWAP")}
    semaphore = asyncio.Semaphore(6)
    # Rubik statistics allow 5 requests per 2 seconds per endpoint; use
    # 2/second with no catch-up bursts. OI permits 20 per 2 seconds.
    route_locks: dict[str, asyncio.Lock] = {}
    next_request_at: dict[str, float] = {}

    async def pace(path):
        lock = route_locks.setdefault(path, asyncio.Lock())
        async with lock:
            loop = asyncio.get_running_loop()
            delay = next_request_at.get(path, 0) - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            next_request_at[path] = loop.time() + (.5 if path.startswith("/rubik/") else .12)

    async def derivative(instrument, resource, path, params):
        try:
            async with semaphore:
                await pace(path)
                response = await asyncio.wait_for(okx_get(path, params), timeout=5)
            derivatives[instrument][resource] = parse_derivative(resource, response, instrument, datetime.now(timezone.utc))
        except Exception:
            derivatives[instrument][resource] = unavailable("OKX " + path, instrument if resource == "openInterest" else params["ccy"] + " currency-wide contracts",
                                                            "optional exchange source unavailable or invalid", datetime.now(timezone.utc))

    async def benchmark(instrument):
        try:
            async with semaphore:
                response = await asyncio.wait_for(okx_get("/market/ticker", {"instId": instrument}), timeout=5)
            row = response["data"][0]
            stamp, age = _age(row["ts"], datetime.now(timezone.utc), 300)
            if row.get("instId") != instrument:
                raise ValueError("benchmark instrument mismatch")
            last, opened = _number(row["last"], positive=True), _number(row["open24h"], positive=True)
            change = (last / opened - 1) * 100
            if not math.isfinite(change):
                raise ValueError("invalid benchmark return")
            context["benchmarks"][instrument] = {"available": True, "source": "OKX /market/ticker", "scope": instrument,
                "receivedAt": _iso(datetime.now(timezone.utc)), "sourceTimestampUnixMilliseconds": stamp, "ageSeconds": age,
                "last": last, "return24hPercent": change}
        except Exception:
            context["benchmarks"][instrument] = unavailable("OKX /market/ticker", instrument, "benchmark unavailable or invalid", datetime.now(timezone.utc))

    async with httpx.AsyncClient(timeout=5, follow_redirects=False, headers={"User-Agent": "NovaTrade/1.5 public-market-context"}) as client:
        async def global_source(resource):
            context[resource] = await _global_source(client, resource)

        requests = [global_source(resource) for resource in ("sentiment", "fedPolicy")]
        # Dispatch important benchmark context first so an unsupported altcoin
        # cannot consume its entire optional collection budget.
        requests += [benchmark(instrument) for instrument in context["benchmarks"]]
        for instrument in instruments:
            currency = instrument.split("-")[0]
            requests += [derivative(instrument, "openInterest", "/public/open-interest", {"instType": "SWAP", "instId": instrument}),
                         derivative(instrument, "longShortAccountRatio", "/rubik/stat/contracts/long-short-account-ratio", {"ccy": currency, "period": "5m"}),
                         derivative(instrument, "takerVolume", "/rubik/stat/taker-volume", {"ccy": currency, "instType": "CONTRACTS", "period": "5m"})]
        try:
            await asyncio.wait_for(asyncio.gather(*requests), timeout=COLLECTION_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            pass
    return derivatives, context
