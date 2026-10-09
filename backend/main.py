"""Direct FastAPI backend for NovaTrade.

The local API talks to OKX public REST/WebSocket endpoints directly. Runtime
state is kept in the application-support directory so the SwiftUI client can
continue using its existing JSON contract without a separate daemon.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import secrets
from typing import Any
from urllib.parse import urlencode, urlsplit
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
import uvicorn
import websockets

try:
    from .ai_market_context import collect_market_context
    from .ai_entry_preflight import EntryPreflightRejected, check_entry_preflight
    from .ai_policy import (
        ACCOUNT_DAILY_LOSS_PERCENT,
        MIN_EXIT_REASON_LENGTH,
        MIN_PROTECTION_REPLACEMENT_CONFIDENCE,
        _check_protection_geometry, entry_exposure_error, parse_time, snapshot_freshness,
    )
    from .ai_schema import AIDecision, AIChatRequest, AIConfig, AISnapshot, SchemaError, normalize_contract_ids
    from .ai_worker import AIWorker, CodexError
    from .exit_audit import sync_native_protection_exits
    from .order_gateway import InstrumentSpec, OrderGateway, OrderGatewayError, OrderNotSubmittedError
    from .paper_trading import PaperTradingAccount
except ImportError:  # bundled backend/main.py is launched as a script
    from ai_market_context import collect_market_context
    from ai_entry_preflight import EntryPreflightRejected, check_entry_preflight
    from ai_policy import (
        ACCOUNT_DAILY_LOSS_PERCENT,
        MIN_EXIT_REASON_LENGTH,
        MIN_PROTECTION_REPLACEMENT_CONFIDENCE,
        _check_protection_geometry, entry_exposure_error, parse_time, snapshot_freshness,
    )
    from ai_schema import AIDecision, AIChatRequest, AIConfig, AISnapshot, SchemaError, normalize_contract_ids
    from ai_worker import AIWorker, CodexError
    from exit_audit import sync_native_protection_exits
    from order_gateway import InstrumentSpec, OrderGateway, OrderGatewayError, OrderNotSubmittedError
    from paper_trading import PaperTradingAccount


def load_env_file() -> None:
    configured = os.getenv("NOVATRADE_ENV_FILE")
    path = Path(configured) if configured else Path.home() / "Library/Application Support/NovaTrade/backend.env"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip("\"'")
        if key and value and key not in os.environ:
            os.environ[key] = value


load_env_file()

HOST = os.getenv("NOVATRADE_FASTAPI_HOST", "127.0.0.1")
PORT = int(os.getenv("NOVATRADE_FASTAPI_PORT", "8787"))
OKX_REST = os.getenv("OKX_REST_URL", "https://www.okx.com/api/v5").rstrip("/")
OKX_WS = os.getenv("OKX_WS_URL", "wss://ws.okx.com:8443/ws/v5/business")
MAX_CANDLES = 300
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_SECRET_KEY = os.getenv("OKX_SECRET_KEY", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")
OKX_DEMO = os.getenv("OKX_DEMO", "0").lower() in {"1", "true", "yes"}
OKX_PROFILE = os.getenv("OKX_PROFILE", "python-fastapi")
OKX_SITE = os.getenv("OKX_SITE", "global")
TRADING_MODE = os.getenv("NOVATRADE_TRADING_MODE", "exchange").strip().lower()
if TRADING_MODE not in {"exchange", "paper"}:
    raise ValueError("NOVATRADE_TRADING_MODE must be exchange or paper")
PAPER_INITIAL_USDT = float(os.getenv("NOVATRADE_PAPER_INITIAL_USDT", "5000"))
if not math.isfinite(PAPER_INITIAL_USDT) or PAPER_INITIAL_USDT <= 0:
    raise ValueError("NOVATRADE_PAPER_INITIAL_USDT must be positive and finite")
if TRADING_MODE == "paper":
    # Paper fills must use the production feed even when old demo overrides
    # or exchange credentials remain in the user's environment.
    OKX_REST = "https://www.okx.com/api/v5"
    OKX_WS = "wss://ws.okx.com:8443/ws/v5/business"


# Keep one connection pool per asyncio event loop for all public/private REST
# requests. `httpx.AsyncClient` owns asyncio futures through its connection
# pool, so one module-level client cannot safely be reused by tests or a host
# that recreates the loop. The facade keeps the existing injectable `get` and
# `request` methods while routing each call to the pool for its current loop.
class _LoopAwareHTTPClient:
    def __init__(self) -> None:
        self._clients: dict[asyncio.AbstractEventLoop, httpx.AsyncClient] = {}

    def _client_for_loop(self) -> httpx.AsyncClient:
        loop = asyncio.get_running_loop()
        client = self._clients.get(loop)
        if client is None or client.is_closed:
            client = httpx.AsyncClient(
                timeout=httpx.Timeout(20.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
            )
            self._clients[loop] = client
        return client

    async def get(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self._client_for_loop().get(*args, **kwargs)

    async def request(self, *args: Any, **kwargs: Any) -> httpx.Response:
        return await self._client_for_loop().request(*args, **kwargs)

    async def aclose(self) -> None:
        clients = list(self._clients.values())
        self._clients.clear()
        for client in clients:
            with contextlib.suppress(Exception):
                await client.aclose()


OKX_HTTP_CLIENT = _LoopAwareHTTPClient()


class _LoopAwareAsyncLock:
    """Keep an asyncio lock per event loop for reusable module state."""

    def __init__(self) -> None:
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    def _lock_for_loop(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[loop] = lock
        return lock

    async def acquire(self) -> bool:
        return await self._lock_for_loop().acquire()

    def release(self) -> None:
        self._lock_for_loop().release()

    def locked(self) -> bool:
        return self._lock_for_loop().locked()

    async def __aenter__(self) -> "_LoopAwareAsyncLock":
        await self.acquire()
        return self

    async def __aexit__(self, exc_type: Any, exc_value: Any, traceback: Any) -> None:
        self.release()


def now_iso() -> str:
    # Foundation's JSONDecoder `.iso8601` strategy used by the macOS client
    # accepts whole-second timestamps but rejects fractional seconds.
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def base_currency(instrument: dict[str, Any]) -> str:
    configured = str(instrument.get("baseCcy") or "").strip()
    if configured:
        return configured
    return str(instrument.get("instId") or "").split("-", 1)[0]


def state_dir() -> Path:
    configured = os.getenv("NOVATRADE_STATE_DIR")
    path = Path(configured) if configured else Path.home() / "Library/Application Support/NovaTrade"
    path.mkdir(parents=True, exist_ok=True)
    return path


def token() -> str:
    configured = os.getenv("NOVATRADE_AUTH_TOKEN")
    path = state_dir() / "locald.token"
    if configured:
        path.write_text(configured, encoding="utf-8")
        return configured
    try:
        value = path.read_text(encoding="utf-8").strip()
    except OSError:
        value = ""
    if not value:
        value = secrets.token_hex(32)
        path.write_text(value, encoding="utf-8")
        path.chmod(0o600)
    return value


def is_loopback(host: str | None) -> bool:
    if not host:
        return False
    value = host.lower().strip()
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    elif value.count(":") == 1 and value.rsplit(":", 1)[1].isdigit():
        value = value.rsplit(":", 1)[0]
    return value in {"127.0.0.1", "localhost", "::1"}


def authorized(headers: Any, host: str | None, origin: str | None) -> bool:
    if not is_loopback(host):
        return False
    if origin and urlsplit(origin).netloc.lower() != (host or "").lower():
        return False
    supplied = headers.get("authorization", "")
    supplied = supplied[7:].strip() if supplied.lower().startswith("bearer ") else headers.get("x-novatrade-token", "")
    return bool(supplied) and hmac.compare_digest(supplied, token())


app = FastAPI(title="NovaTrade FastAPI", version="1")
ai_worker: AIWorker | None = None
# The Codex control-plane API retains its stable strategy identifier.
ai_workers: dict[str, AIWorker] = {}
order_gateway: OrderGateway | None = None
native_exit_task: asyncio.Task | None = None
native_exit_sync_lock = _LoopAwareAsyncLock()
# Protection reconciliation can run from both AI workers. Serialize the
# exchange write so two strategies cannot attach duplicate OCOs to one leg.
position_protection_lock = _LoopAwareAsyncLock()
paper_account: PaperTradingAccount | None = None
paper_market_task: asyncio.Task | None = None
paper_quote_lock = _LoopAwareAsyncLock()
paper_instrument_cache: tuple[float, dict[str, dict[str, Any]]] | None = None


def local_paper_mode() -> bool:
    return TRADING_MODE == "paper"


def _get_paper_account() -> PaperTradingAccount:
    global paper_account
    if paper_account is None:
        paper_account = PaperTradingAccount(state_dir() / "paper-account.json", initial_balance=PAPER_INITIAL_USDT)
    return paper_account


async def _refresh_paper_quotes() -> None:
    """Match only against production public quotes, including off-screen contracts."""
    async with paper_quote_lock:
        broker = _get_paper_account()
        instruments = broker.tracked_instruments()
        if not instruments:
            return
        payload = await okx_get("/market/tickers", {"instType": "SWAP"})
        quotes = {str(row.get("instId")): row for row in payload.get("data", []) if isinstance(row, dict)}
        for instrument in instruments:
            row = quotes.get(instrument, {})
            last = as_float(row.get("last"))
            if last <= 0:
                continue
            await broker.mark(instrument, last, bid=as_float(row.get("bidPx")) or None, ask=as_float(row.get("askPx")) or None)


async def _paper_market_loop() -> None:
    while True:
        try:
            await _refresh_paper_quotes()
        except (HTTPException, httpx.HTTPError, ValueError) as error:
            append_runtime_event("warning", f"纸面撮合行情刷新失败：{_ai_collection_error(error)}")
        await asyncio.sleep(1)

# The API configuration center owns the two decision providers.  Keep this
# catalog explicit so the UI can show where a strategy is defined and which
# runtime/live gates apply without inferring them from a worker's current
# mutable config.  The source paths are documentation/package metadata only;
# runtime execution never reads the repository's strategies directory.
AI_STRATEGY_CATALOG: dict[str, dict[str, Any]] = {
    "codex": {
        "packageID": "codex_ai_decision",
        "sourceOfTruth": "strategies/codex_ai_decision/STRATEGY.md",
        "runtime": {
            "handler": "backend.ai_worker.CodexRunner",
            "decisionEndpoint": "/api/v1/ai/strategies/codex",
            "eventFingerprint": "market-facts-v2",
        },
        "liveGate": {
            "requiresManualEnable": True,
            "requiresLiveTradingSwitch": True,
            "paperMode": "paper-active",
            "demoMode": "demo-active",
            "liveMode": "live-armed",
        },
    },
}


@app.on_event("shutdown")
async def close_okx_http_client() -> None:
    global native_exit_task, paper_market_task
    if paper_market_task is not None:
        paper_market_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await paper_market_task
        paper_market_task = None
    if native_exit_task is not None:
        native_exit_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await native_exit_task
        native_exit_task = None
    for worker in {id(item): item for item in ai_workers.values()}.values():
        await worker.stop()
    if ai_worker is not None and "codex" not in ai_workers:
        await ai_worker.stop()
    await OKX_HTTP_CLIENT.aclose()


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    if not authorized(request.headers, request.headers.get("host"), request.headers.get("origin")):
        return JSONResponse({"detail": "forbidden"}, status_code=403)
    return await call_next(request)


async def okx_get(path: str, params: dict[str, str]) -> dict[str, Any]:
    response = await OKX_HTTP_CLIENT.get(f"{OKX_REST}{path}", params=params)
    response.raise_for_status()
    payload = response.json()
    if payload.get("code") != "0":
        error = HTTPException(status_code=502, detail=payload.get("msg", "OKX request failed"))
        # Preserve the public exchange error code for bounded AI-only retries,
        # while keeping the existing HTTP error response unchanged.
        error.exchange_code = str(payload.get("code") or "")
        raise error
    return payload


def as_float(value: Any, default: float = 0) -> float:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else default
    except (TypeError, ValueError):
        return default


def _bill_is_today(row: dict[str, Any], day: Any) -> bool:
    timestamp = as_float(row.get("ts"))
    if timestamp <= 0:
        return False
    try:
        return datetime.fromtimestamp(timestamp / 1000, timezone.utc).date() == day
    except (OverflowError, OSError, ValueError):
        return False


def _daily_loss_count(rows: list[dict[str, Any]], day: Any) -> int:
    """Count today's distinct negative-PnL OKX bill/order entries.

    Trade PnL rows carry ``ordId``. Rows without one are fees, funding, or
    another account adjustment whose negative value must not be treated as a
    losing trade. OKX may emit multiple rows for one order, so the order ID is
    the durable identity used for de-duplication.
    """
    identifiers: set[str] = set()
    count = 0
    for row in rows:
        if not isinstance(row, dict) or not _bill_is_today(row, day):
            continue
        pnl = as_float(row.get("pnl"), 0)
        if pnl >= 0:
            continue
        identifier = str(row.get("ordId") or "").strip()
        if not identifier or identifier in identifiers:
            continue
        identifiers.add(identifier)
        count += 1
    return count


async def _account_bills_today(day: Any, *, limit: int = 100, max_pages: int = 20) -> tuple[list[dict[str, Any]], bool]:
    """Read enough OKX bill pages to establish the complete UTC-day set.

    OKX returns the newest bills first. A short page or an older-than-today
    row proves the boundary was reached. If a full page needs another cursor
    but the cursor is missing/repeated, the result is deliberately marked
    incomplete so callers cannot mistake a partial loss count for zero.
    """
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    seen_cursors: set[str] = set()
    for _ in range(max_pages):
        params: dict[str, str] = {"instType": "SWAP", "limit": str(limit)}
        if cursor is not None:
            params["after"] = cursor
        payload = await okx_private_request("GET", "/account/bills", params=params)
        page = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(page, list) or not all(isinstance(row, dict) for row in page):
            raise ValueError("OKX bills response is invalid")
        rows.extend(page)
        if len(page) < limit or not page:
            return rows, True
        if any(not _bill_is_today(row, day) and as_float(row.get("ts")) > 0 for row in page):
            return rows, True
        next_cursor = str(page[-1].get("billId") or page[-1].get("id") or "").strip()
        if not next_cursor or next_cursor in seen_cursors:
            return rows, False
        seen_cursors.add(next_cursor)
        cursor = next_cursor
    return rows, False


def quote_volume_24h(ticker: dict[str, Any], instrument: dict[str, Any], last: float) -> float:
    """Return 24h turnover in the settlement currency for a linear swap.

    OKX reports ``volCcy24h`` in base-coin units for derivatives. Convert it
    at the latest price before ranking contracts in the market sidebar. When
    that field is missing, derive turnover from contract count and the
    instrument contract value/multiplier.
    """
    if last <= 0:
        return 0
    base_volume = as_float(ticker.get("volCcy24h"))
    if base_volume > 0:
        return base_volume * last
    contracts = as_float(ticker.get("vol24h"))
    contract_value = as_float(instrument.get("ctVal"))
    contract_multiplier = as_float(instrument.get("ctMult"), 1)
    if contracts <= 0 or contract_value <= 0 or contract_multiplier <= 0:
        return 0
    return contracts * contract_value * contract_multiplier * last


def ticker_price_changes(ticker: dict[str, Any], last: float) -> tuple[float, float]:
    """Return (UTC-day percent, rolling-24h percent) using OKX ticker fields.

    ``sodUtc0`` is the UTC calendar-day open and ``open24h`` is the rolling
    24-hour open. ``sodUtc8`` is retained as a compatibility fallback for
    regional/older payloads that omit ``sodUtc0``; it must never replace the
    rolling 24h baseline.
    """
    day_open = as_float(ticker.get("sodUtc0"))
    if day_open <= 0:
        day_open = as_float(ticker.get("sodUtc8"))
    if day_open <= 0:
        day_open = last
    rolling_open = as_float(ticker.get("open24h"))
    if rolling_open <= 0:
        rolling_open = last
    day_change = (last / day_open - 1) * 100 if last > 0 and day_open > 0 else 0
    rolling_change = (last / rolling_open - 1) * 100 if last > 0 and rolling_open > 0 else 0
    return day_change, rolling_change


def ticker_liquidity_metrics(ticker: dict[str, Any], last: float) -> tuple[float, float]:
    """Return (spread in basis points, top-of-book quote depth).

    Both fields are optional on OKX's ticker feed. A zero value means the
    exchange did not provide enough data, so callers can fall back to turnover
    ranking without rejecting an otherwise valid contract.
    """
    bid = as_float(ticker.get("bidPx"))
    ask = as_float(ticker.get("askPx"))
    mid = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    spread_bps = ((ask - bid) / mid) * 10_000 if bid > 0 and ask >= bid and mid > 0 else 0
    bid_depth = bid * as_float(ticker.get("bidSz")) if bid > 0 else 0
    ask_depth = ask * as_float(ticker.get("askSz")) if ask > 0 else 0
    return max(0, spread_bps), max(0, bid_depth + ask_depth)


def private_ready() -> bool:
    return all((OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE))


def okx_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def okx_signature(timestamp: str, method: str, request_path: str, payload: str) -> str:
    message = f"{timestamp}{method.upper()}{request_path}{payload}".encode()
    return base64.b64encode(hmac.new(OKX_SECRET_KEY.encode(), message, "sha256").digest()).decode()


async def okx_private_request(method: str, path: str, *, params: dict[str, str] | None = None, body: Any = None) -> dict[str, Any]:
    if local_paper_mode():
        raise HTTPException(status_code=409, detail="本地纸面账户禁止调用 OKX 私有接口")
    if not private_ready():
        raise HTTPException(status_code=503, detail="OKX private REST credentials are not configured")
    query = f"?{urlencode(params)}" if params else ""
    # OKX signs the complete API request path, including the version prefix.
    # `OKX_REST` normally ends in `/api/v5`; keep regional endpoint overrides
    # correct by deriving the prefix from the configured URL.
    api_prefix = urlsplit(OKX_REST).path.rstrip("/")
    request_path = f"{api_prefix}{path}{query}"
    payload = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""
    timestamp = okx_timestamp()
    signature = okx_signature(timestamp, method, request_path, payload)
    headers = {
        "OK-ACCESS-KEY": OKX_API_KEY,
        "OK-ACCESS-SIGN": signature,
        "OK-ACCESS-TIMESTAMP": timestamp,
        "OK-ACCESS-PASSPHRASE": OKX_PASSPHRASE,
        "Content-Type": "application/json",
        # OKX's edge rejects the default Python client user agent for some
        # private endpoints even when the signature is valid.
        "User-Agent": "Mozilla/5.0 NovaTrade-FastAPI/1.0",
    }
    if OKX_DEMO:
        headers["x-simulated-trading"] = "1"
    # The configured REST URL already contains the `/api/v5` prefix; it
    # belongs in the signature but must not be duplicated in the URL.
    response = await OKX_HTTP_CLIENT.request(method.upper(), f"{OKX_REST}{path}{query}", headers=headers, content=payload or None)
    try:
        result = response.json()
    except ValueError as error:
        raise HTTPException(status_code=502, detail="OKX returned invalid JSON") from error
    if response.status_code >= 400 or result.get("code") != "0":
        detail = result.get("msg", f"OKX HTTP {response.status_code}")
        error = HTTPException(status_code=502, detail=detail)
        # Preserve read diagnostics; this method never retries private calls,
        # especially an order POST whose result may be uncertain.
        error.exchange_code = str(result.get("code") or "")
        error.upstream_status_code = response.status_code
        raise error
    for row in result.get("data", []):
        if isinstance(row, dict) and row.get("sCode") not in (None, "0", 0):
            raise HTTPException(status_code=502, detail=row.get("sMsg", "OKX rejected request"))
    return result


@app.get("/health")
async def health() -> dict[str, Any]:
    return {"status": "ok", "version": "fastapi-1", "mode": "paper" if local_paper_mode() or OKX_DEMO else "live" if private_ready() else "readOnly", "updatedAt": now_iso()}


@app.get("/api/v1/contracts")
async def contracts(fresh: bool = False) -> list[dict[str, Any]]:
    instruments, tickers = await asyncio.gather(
        okx_get("/public/instruments", {"instType": "SWAP"}),
        okx_get("/market/tickers", {"instType": "SWAP"}),
    )
    ticker_by_id = {item.get("instId"): item for item in tickers.get("data", [])}
    result = []
    for item in instruments.get("data", []):
        if item.get("state") != "live" or item.get("settleCcy") != "USDT" or item.get("ctType") != "linear":
            continue
        ticker = ticker_by_id.get(item.get("instId"), {})
        last = as_float(ticker.get("last"))
        day_change, rolling_change = ticker_price_changes(ticker, last)
        spread_bps, book_depth_quote = ticker_liquidity_metrics(ticker, last)
        result.append({
            "id": item["instId"], "name": item["instId"], "baseCurrency": base_currency(item),
            "quoteCurrency": item.get("settleCcy", "USDT"), "last": last,
            "changePercent": day_change,
            "rollingChangePercent": rolling_change,
            "volume24h": quote_volume_24h(ticker, item, last),
            # Keep liquidity fields available to diagnostics; Swift's
            # ContractMarket decoder intentionally ignores these extras.
            "spreadBps": spread_bps, "bookDepthQuote": book_depth_quote,
            "category": "全部", "updatedAt": now_iso(),
        })
    return result


@app.get("/api/v1/market/candles")
async def market_candles(instId: str, bar: str = "5m") -> dict[str, Any]:
    # The chart follows OKX's mobile daily series. Its daily candle is aligned
    # to UTC; labels are converted to the operator's local timezone in Swift.
    client_bar = "1D" if bar in {"1D", "1Dutc"} else bar
    exchange_bar = "1Dutc" if client_bar == "1D" else client_bar
    payload = await okx_get("/market/candles", {"instId": instId, "bar": exchange_bar, "limit": str(MAX_CANDLES)})
    candles = []
    for row in reversed(payload.get("data", [])):
        if len(row) < 9:
            continue
        candles.append({
            "id": int(int(row[0]) / 1000), "timestamp": datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
            "open": as_float(row[1]), "high": as_float(row[2]), "low": as_float(row[3]), "close": as_float(row[4]),
            "volume": as_float(row[5]), "quoteVolume": as_float(row[7]), "confirmed": str(row[8]) == "1",
        })
    return {"instrumentID": instId, "interval": client_bar, "candles": candles, "updatedAt": now_iso()}


def read_state(name: str, fallback: Any) -> Any:
    try:
        return json.loads((state_dir() / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback


def write_state(name: str, value: Any) -> None:
    path = state_dir() / name
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def append_runtime_event(level: str, message: str, fields: dict[str, Any] | None = None) -> dict[str, Any]:
    """Append a stable, scan-friendly event to the bounded runtime log."""
    event: dict[str, Any] = {
        "timestamp": now_iso(),
        "level": str(level).lower(),
        "message": str(message),
    }
    if isinstance(fields, dict):
        event.update(fields)
    values = read_state("runtime-log.json", [])
    if not isinstance(values, list):
        values = []
    values.append(event)
    write_state("runtime-log.json", values[-1000:])
    return event


async def _gateway_runtime_sink(value: dict[str, Any]) -> None:
    event_type = str(value.get("type") or "")
    level = "error" if event_type in {"native-protection-failure", "position-protection-failure"} else str(value.get("level") or "info")
    append_runtime_event(
        level,
        str(value.get("message") or value.get("type") or "gateway event"),
        value,
    )


def runtime_state() -> dict[str, Any]:
    """Load the single runtime state used by strategy dashboard endpoints.

    Older FastAPI builds wrote strategies and derived snapshots to separate
    files, while the Swift runtime persisted them in ``paper-state.json``.
    Migrate that split state once, preserving the current strategy config and
    carrying over any ledger values that can be matched by name.
    """
    state = read_state("paper-state.json", {})
    if not isinstance(state, dict):
        state = {}
    if state.get("_fastapiCanonical"):
        return state
    source = read_state("strategies.json", [])
    if isinstance(source, list) and source:
        current = state.get("strategies") or []
        current_ids = {str(item.get("id")) for item in current if isinstance(item, dict)}
        source_ids = {str(item.get("id")) for item in source if isinstance(item, dict)}
        if not current or not current_ids.intersection(source_ids):
            state["strategies"] = source
            # Derived data from the old IDs cannot safely be reused by UUID;
            # the snapshot helpers below remap compatible entries by name.
            state["statuses"] = state.get("statuses") or {}
    state.setdefault("strategies", [])
    state.setdefault("statuses", {})
    state.setdefault("orders", [])
    state.setdefault("fills", [])
    state.setdefault("risk", {})
    state["_fastapiCanonical"] = True
    write_state("paper-state.json", state)
    return state


def strategy_configs() -> list[dict[str, Any]]:
    state = runtime_state()
    return [item for item in state.get("strategies", []) if isinstance(item, dict) and item.get("id")]


def iso_now() -> str:
    return now_iso()


def legacy_entry_by_name(state: dict[str, Any], config: dict[str, Any], key: str) -> dict[str, Any] | None:
    """Find a legacy snapshot by UUID first, then by stable strategy name."""
    entries = state.get(key, [])
    if isinstance(entries, dict):
        direct = entries.get(str(config.get("id")))
        if isinstance(direct, dict):
            return direct
        entries = list(entries.values())
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("strategyID")) == str(config.get("id")):
            return entry
    name = str(config.get("name", ""))
    for entry in entries:
        if isinstance(entry, dict) and str(entry.get("strategyName", entry.get("name", ""))) == name:
            return entry
    return None


async def strategy_capital_base(state: dict[str, Any]) -> float:
    if local_paper_mode():
        return as_float(_get_paper_account().account_snapshot().get("equityUSD"))
    risk_state = state.get("risk") if isinstance(state.get("risk"), dict) else {}
    if private_ready():
        try:
            payload = await okx_private_request("GET", "/account/balance")
            row = (payload.get("data") or [{}])[0]
            usdt = next((item for item in row.get("details", []) if str(item.get("ccy", "")).upper() == "USDT"), None)
            value = as_float((usdt or {}).get("eq"))
            if value > 0:
                return value
            return as_float(row.get("totalEq"))
        except HTTPException:
            pass
    stored = as_float(risk_state.get("strategyCapitalBase"))
    if stored > 0:
        return stored
    return as_float(risk_state.get("equity"))


def strategy_status_snapshot(state: dict[str, Any], config: dict[str, Any]) -> dict[str, Any]:
    legacy = legacy_entry_by_name(state, config, "statuses") or {}
    # In the direct Python backend `enabled` is the lifecycle switch. The
    # status endpoint must still expose a complete Swift-compatible record.
    return {
        "id": str(config["id"]),
        "state": "running" if bool(config.get("enabled")) else "paused",
        "direction": "short" if config.get("type") == "sweepReversalShort" else legacy.get("direction"),
        "cooldown": int(as_float(legacy.get("cooldown"))),
        "pnl": as_float(legacy.get("pnl")),
        "lastSignal": legacy.get("lastSignal"),
        "indicators": legacy.get("indicators") if isinstance(legacy.get("indicators"), dict) else {},
        "lastEvaluatedBar": None,
    }


def strategy_capital_snapshot(state: dict[str, Any], config: dict[str, Any], base: float) -> dict[str, Any]:
    risk_state = state.get("risk") if isinstance(state.get("risk"), dict) else {}
    legacy = legacy_entry_by_name({"strategyCapitals": risk_state.get("strategyCapitals", [])}, config, "strategyCapitals") or {}
    allocation = as_float(config.get("capitalPoolPercent"), as_float(legacy.get("allocationPercent")))
    initial = base * max(0, allocation) / 100 if base > 0 else as_float(legacy.get("initialCapital"))
    equity = as_float(legacy.get("equity"), initial)
    reserved = as_float(legacy.get("reservedCapital"))
    realized = as_float(legacy.get("realizedPnL"))
    unrealized = as_float(legacy.get("unrealizedPnL"))
    return {
        "strategyID": str(config["id"]), "allocationPercent": allocation,
        "initialCapital": initial, "equity": equity, "reservedCapital": reserved,
        "availableCapital": max(0, equity - reserved), "realizedPnL": realized,
        "unrealizedPnL": unrealized, "rolloverCount": int(as_float(legacy.get("rolloverCount"))),
        "updatedAt": iso_now(), "openRisk": as_float(legacy.get("openRisk")),
        "openPositions": int(as_float(legacy.get("openPositions"))),
    }


def strategy_target_snapshot(config: dict[str, Any], contracts_data: list[dict[str, Any]]) -> dict[str, Any]:
    scope = config.get("scope") if isinstance(config.get("scope"), dict) else {}
    category = scope.get("category")
    if scope.get("mode") == "single":
        ids = [str(scope.get("instrumentIDs", [""])[0])] if scope.get("instrumentIDs") else []
    elif scope.get("mode") == "multiple":
        available = {item.get("id") for item in contracts_data}
        ids = [item for item in scope.get("instrumentIDs", []) if item in available or not available]
    elif category == "sweepCandidates":
        ids = [item["id"] for item in sorted(
            (item for item in contracts_data if as_float(item.get("volume24h")) >= 3_000_000 and str(item.get("baseCurrency", "")).upper() not in {"BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "ADA", "TRX", "TON", "AVAX", "LINK", "DOT", "LTC", "BCH", "ETC", "UNI", "ATOM", "NEAR", "APT", "SUI"}),
            key=lambda item: as_float(item.get("volume24h")), reverse=True)[:100]]
    elif category == "hotAltcoins":
        ids = [item["id"] for item in sorted(contracts_data, key=lambda item: as_float(item.get("volume24h")), reverse=True)[:20]]
    else:
        ids = []
    return {"strategyID": str(config["id"]), "strategyName": config.get("name", ""),
            "strategyType": config.get("type", "external"), "enabled": bool(config.get("enabled")),
            "universeCategory": category, "instrumentIDs": ids, "targetCount": len(ids), "refreshedAt": iso_now()}


@app.get("/api/v1/account")
async def account() -> dict[str, Any]:
    if local_paper_mode():
        await _refresh_paper_quotes()
        worker = ai_workers.get("codex") or ai_worker
        config = worker.config if worker is not None else AIConfig()
        return _get_paper_account().account_snapshot(
            stop_loss_cooldown_seconds=config.stopLossCooldownSeconds,
            recent_stop_loss_window_seconds=config.recentStopLossWindowSeconds,
            recent_stop_loss_limit=config.recentStopLossLimit,
        )
    worker = ai_workers.get("codex") or ai_worker
    guard_config = worker.config if worker is not None else AIConfig()
    gateway = _get_order_gateway()
    guard_projection = getattr(gateway, "guard_snapshot", None)
    live_guard = guard_projection(
        stop_loss_cooldown_seconds=guard_config.stopLossCooldownSeconds,
        recent_stop_loss_window_seconds=guard_config.recentStopLossWindowSeconds,
        recent_stop_loss_limit=guard_config.recentStopLossLimit,
    ) if callable(guard_projection) else {}
    if not private_ready():
        return {"mode": "readOnly", "profile": None, "site": None, "label": None, "authenticated": False,
                "equityUSD": None, "availableEquityUSD": None, "totalAssetValueUSD": None, "todayPnLUSD": None,
                "todayLossCount": None, "assets": [], "positions": [], "pendingOrders": None,
                "pendingOrdersKnown": False, "updatedAt": now_iso(), **live_guard}
    balance, config, position_payload = await asyncio.gather(
        okx_private_request("GET", "/account/balance"),
        okx_private_request("GET", "/account/config"),
        okx_private_request("GET", "/account/positions", params={"instType": "SWAP"}),
    )
    balance_row = (balance.get("data") or [{}])[0]
    config_row = (config.get("data") or [{}])[0]
    total_equity = as_float(balance_row.get("totalEq"), as_float(balance_row.get("adjEq")))
    available_equity = as_float(balance_row.get("adjEq"), total_equity)
    assets = []
    for row in balance_row.get("details", []):
        currency = row.get("ccy")
        if not currency:
            continue
        equity = as_float(row.get("eq"))
        assets.append({"id": currency, "currency": currency, "equity": equity,
                       "available": as_float(row.get("availEq"), as_float(row.get("availBal"), max(0, equity - as_float(row.get("frozenBal"))))),
                       "usdValue": as_float(row.get("eqUsd")) if row.get("eqUsd") is not None else None})
    position_rows = position_payload.get("data") if isinstance(position_payload, dict) else None
    positions: list[dict[str, Any]] = []
    positions_available = isinstance(position_rows, list)
    positions_error = None
    # Attached OCOs are converted to standalone algorithm orders after an
    # entry fills. Read those orders while building the AI snapshot so a
    # position is not incorrectly reported as unprotected.
    position_protections = await _pending_position_protections() if positions_available else {}
    if positions_available:
        for row in position_rows:
            if not isinstance(row, dict):
                positions_available = False
                positions_error = "OKX position row is invalid"
                continue
            raw_quantity = row.get("pos")
            try:
                parsed_quantity = float(raw_quantity)
                if not math.isfinite(parsed_quantity):
                    raise ValueError
            except (TypeError, ValueError):
                # A missing or malformed quantity cannot prove that this row
                # is flat. Fail closed instead of looking like no position to
                # the AI entry gate.
                positions_available = False
                positions_error = "OKX position quantity is invalid"
                continue
            if parsed_quantity == 0:
                continue
            item = position_snapshot(row, protection=position_protections.get(_position_protection_key(row)))
            if item is None:
                positions_available = False
                positions_error = "OKX position row is incomplete"
                continue
            positions.append(item)
    else:
        positions_error = "OKX position response is invalid"
    pending_orders: list[dict[str, Any]] | None = None
    pending_orders_error = None
    pending_orders_retryable = False
    try:
        pending_payload = await okx_private_request("GET", "/trade/orders-pending", params={"instType": "SWAP"})
        pending_rows = pending_payload.get("data") if isinstance(pending_payload, dict) else None
        if not isinstance(pending_rows, list):
            raise ValueError("OKX pending order response is invalid")
        pending_orders = []
        for row in pending_rows:
            if not isinstance(row, dict):
                raise ValueError("OKX pending order row is invalid")
            item = order_snapshot(row)
            if item is None:
                raise ValueError("OKX pending order row is incomplete")
            pending_orders.append(item)
    except (HTTPException, httpx.HTTPError, ValueError, TypeError) as error:
        pending_orders_error = _ai_collection_error(error)
        pending_orders_retryable = _AIDataCollector._retryable(error)
    today_pnl = None
    today_loss_count = None
    daily_bills_error = None
    daily_bills_retryable = False
    daily_bills_complete = False
    try:
        today = datetime.now(timezone.utc).date()
        rows, daily_bills_complete = await _account_bills_today(today)
        if not daily_bills_complete:
            raise ValueError("OKX bills pagination is incomplete")
        today_rows = [row for row in rows if _bill_is_today(row, today)]
        today_pnl = sum(as_float(row.get("pnl")) for row in today_rows)
        today_loss_count = _daily_loss_count(rows, today)
    except (HTTPException, httpx.HTTPError, ValueError, TypeError) as error:
        daily_bills_error = _ai_collection_error(error)
        daily_bills_retryable = _AIDataCollector._retryable(error)
    return {"mode": "paper" if OKX_DEMO else "live", "profile": OKX_PROFILE, "site": OKX_SITE,
            "label": config_row.get("label"), "authenticated": True, "equityUSD": total_equity,
            "availableEquityUSD": available_equity, "totalAssetValueUSD": total_equity, "todayPnLUSD": today_pnl,
            "todayLossCount": today_loss_count,
            "dataQuality": {
                "dailyBillsAvailable": today_loss_count is not None,
                "dailyBillsError": daily_bills_error,
                "dailyBillsRetryable": daily_bills_retryable,
                "dailyBillsPaginationComplete": daily_bills_complete,
                "positionsAvailable": positions_available,
                "positionsError": positions_error,
                "pendingOrdersAvailable": pending_orders is not None,
                "pendingOrdersError": pending_orders_error,
                "pendingOrdersRetryable": pending_orders_retryable,
            },
            "assets": assets, "positions": positions, "positionsKnown": positions_available,
            "pendingOrders": pending_orders, "pendingOrdersKnown": pending_orders is not None,
            **live_guard,
            "updatedAt": now_iso()}


def _positive_field(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        value = as_float(row.get(name))
        if value > 0:
            return value
    return None


def _protection_prices(row: dict[str, Any]) -> tuple[float | None, float | None]:
    """Extract attached take-profit and stop-loss trigger prices.

    OKX returns these directly on some order variants and inside
    ``attachAlgoOrds`` for orders submitted with attached protection.
    """
    take_profit = _positive_field(row, "takeProfitPrice", "tpTriggerPx", "takeProfitTriggerPrice")
    stop_loss = _positive_field(row, "stopLossPrice", "slTriggerPx", "stopLossTriggerPrice")
    attached = row.get("attachAlgoOrds") or row.get("attachAlgoOrders") or []
    if isinstance(attached, dict):
        attached = [attached]
    if isinstance(attached, list):
        for item in attached:
            if not isinstance(item, dict):
                continue
            take_profit = take_profit or _positive_field(item, "takeProfitPrice", "tpTriggerPx", "takeProfitTriggerPrice")
            stop_loss = stop_loss or _positive_field(item, "stopLossPrice", "slTriggerPx", "stopLossTriggerPrice")
    return take_profit, stop_loss


def _position_protection_key(row: dict[str, Any]) -> str:
    return f"{row.get('instId', '')}|{str(row.get('posSide') or 'net').lower()}"


async def _pending_position_protections(*, include_details: bool = False) -> dict[str, dict[str, Any]]:
    """Read standalone protection algorithms, retaining lookup completeness.

    A failed algorithm endpoint must never be interpreted as an empty set:
    doing so lets the reconciliation loop attach duplicate OCOs to a live
    position.  The regular account projection keeps its historical shape;
    callers that are about to write protection request ``include_details``
    and inspect the private ``_meta`` marker below.
    """
    result: dict[str, dict[str, Any]] = {}
    lookup_errors: list[str] = []
    # OKX requires `ordType` for this endpoint. Attached TP/SL orders are
    # commonly represented as OCO algorithms after the parent order fills;
    # conditional and trigger orders are valid representations as well.
    for order_type in ("conditional", "oco", "trigger"):
        try:
            payload = await okx_private_request(
                "GET", "/trade/orders-algo-pending",
                params={"instType": "SWAP", "ordType": order_type},
            )
        except Exception as error:
            lookup_errors.append(f"{order_type}: {error}")
            continue
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
            lookup_errors.append(f"{order_type}: invalid response")
            continue
        for row in payload.get("data", []) if isinstance(payload, dict) else []:
            if not isinstance(row, dict) or not row.get("instId"):
                continue
            take_profit, stop_loss = _protection_prices(row)
            if take_profit is None and stop_loss is None:
                continue
            key = _position_protection_key(row)
            algo_id = str(row.get("algoId") or row.get("algoID") or "").strip()
            owner_id = str(
                row.get("algoClOrdId") or row.get("algoClOrdID")
                or row.get("attachAlgoClOrdId") or row.get("attachAlgoClOrdID") or ""
            ).strip().lower()
            managed_by_ai = owner_id.startswith(("ai", "aip"))
            current = result.setdefault(key, {
                "takeProfitPrice": None, "stopLossPrice": None,
                "takeProfitPrices": [], "stopLossPrices": [], "algoIDs": [], "orders": [],
                "unmanagedOrders": False,
            })
            current["takeProfitPrice"] = current["takeProfitPrice"] or take_profit
            current["stopLossPrice"] = current["stopLossPrice"] or stop_loss
            if take_profit is not None and take_profit not in current["takeProfitPrices"]:
                current["takeProfitPrices"].append(take_profit)
            if stop_loss is not None and stop_loss not in current["stopLossPrices"]:
                current["stopLossPrices"].append(stop_loss)
            if algo_id and algo_id not in current["algoIDs"]:
                current["algoIDs"].append(algo_id)
            current["orders"].append(dict(row))
            if not algo_id or not managed_by_ai:
                current["unmanagedOrders"] = True
            # A missing posSide is common for net-mode algorithms. Keep an
            # instrument-only fallback for the corresponding position row.
            fallback = result.setdefault(f"{row.get('instId')}|net", {
                "takeProfitPrice": None, "stopLossPrice": None,
                "takeProfitPrices": [], "stopLossPrices": [], "algoIDs": [], "orders": [],
                "unmanagedOrders": False,
            })
            fallback["takeProfitPrice"] = fallback["takeProfitPrice"] or take_profit
            fallback["stopLossPrice"] = fallback["stopLossPrice"] or stop_loss
            if take_profit is not None and take_profit not in fallback["takeProfitPrices"]:
                fallback["takeProfitPrices"].append(take_profit)
            if stop_loss is not None and stop_loss not in fallback["stopLossPrices"]:
                fallback["stopLossPrices"].append(stop_loss)
            if algo_id and algo_id not in fallback["algoIDs"]:
                fallback["algoIDs"].append(algo_id)
            fallback["orders"].append(dict(row))
            if not algo_id or not managed_by_ai:
                fallback["unmanagedOrders"] = True
    if include_details:
        result["_meta"] = {
            "lookupComplete": not lookup_errors,
            "lookupErrors": lookup_errors,
        }
        return result
    return {
        key: {
            "takeProfitPrice": value.get("takeProfitPrice"),
            "stopLossPrice": value.get("stopLossPrice"),
        }
        for key, value in result.items()
    }


def _position_direction(position: dict[str, Any]) -> str | None:
    """Resolve a signed net/side position to the direction used by AI."""
    side = str(position.get("side") or position.get("positionSide") or position.get("posSide") or "net").lower()
    if side in {"long", "short"}:
        return side
    quantity = as_float(position.get("quantity", position.get("pos")), math.nan)
    if not math.isfinite(quantity) or quantity == 0:
        return None
    return "short" if quantity < 0 else "long"


def _assessment_targets(assessment: Any) -> list[dict[str, float]]:
    """Normalize legacy single TP and optional staged targets for execution."""
    levels = getattr(assessment, "takeProfitLevels", None)
    if isinstance(levels, list):
        result: list[dict[str, float]] = []
        for row in levels:
            if not isinstance(row, dict):
                continue
            price = as_float(row.get("price"))
            percent = as_float(row.get("quantityPercent"))
            if price > 0 and 0 < percent <= 100:
                result.append({"price": price, "quantityPercent": percent})
        if result:
            return result
    price = as_float(getattr(assessment, "takeProfitPrice", None))
    return [{"price": price, "quantityPercent": 100.0}] if price > 0 else []


def _active_assessment_targets(position: dict[str, Any], assessment: Any) -> list[dict[str, float]]:
    """Keep only staged targets that are still reachable from the current mark.

    A fast move can cross one or more targets before the next reconciliation
    cycle. Those targets cannot create a useful new trigger; the remaining
    position must be allocated across the targets still beyond the mark.
    """
    targets = _assessment_targets(assessment)
    direction = _position_direction(position)
    mark = as_float(position.get("markPrice"))
    if mark <= 0:
        mark = as_float(position.get("entryPrice"))
    if direction not in {"long", "short"} or mark <= 0:
        return targets
    if direction == "long":
        return [target for target in targets if target["price"] > mark]
    return [target for target in targets if target["price"] < mark]


def _position_assessment(decision: AIDecision, position: dict[str, Any]) -> Any | None:
    instrument = str(position.get("instrumentID") or "")
    direction = _position_direction(position)
    if not instrument or direction not in {"long", "short"}:
        return None
    for assessment in decision.assessments:
        if assessment.instrumentID == instrument and assessment.direction == direction:
            return assessment
    return None


def _protection_is_reasonable(position: dict[str, Any], assessment: Any) -> bool:
    """Check protection against the current position price when available.

    A position may have moved into profit since it opened. Comparing a
    replacement stop/target only with ``entryPrice`` rejects valid trailing
    protection (for example, a short stop below entry but above the current
    mark). New or incomplete snapshots without a usable mark still fall back
    to entry so the execution gate remains conservative.
    """
    direction = _position_direction(position)
    entry = as_float(position.get("entryPrice"))
    mark = as_float(position.get("markPrice"))
    reference = mark if mark > 0 else entry
    stop = as_float(getattr(assessment, "stopLossPrice", None))
    targets = _active_assessment_targets(position, assessment)
    if direction not in {"long", "short"} or reference <= 0 or stop <= 0:
        return False
    prices = [row["price"] for row in targets]
    if direction == "long":
        return stop < reference and all(price > reference for price in prices)
    return stop > reference and all(price < reference for price in prices)


async def _submit_position_protection(
    position: dict[str, Any], assessment: Any, *, decision: AIDecision,
) -> list[dict[str, Any]]:
    """Attach one OCO per staged target to an existing position.

    Each target owns its percentage of the position. This permits partial
    take-profit fills while keeping a stop on every tranche. Existing active
    algorithms are checked by the caller before this function is reached.
    """
    if not private_ready() or not _protection_is_reasonable(position, assessment):
        return []
    instrument_id = str(position.get("instrumentID") or "")
    direction = _position_direction(position)
    quantity = abs(as_float(position.get("quantity")))
    if not instrument_id or direction not in {"long", "short"} or quantity <= 0:
        return []
    spec = await _instrument_spec(instrument_id)
    size_total = math.floor(quantity / spec.lotSize) * spec.lotSize
    if size_total < spec.minSize:
        return []
    stop = spec.aligned_price(as_float(getattr(assessment, "stopLossPrice", None)))
    targets = _active_assessment_targets(position, assessment)
    margin_mode = str(position.get("marginMode") or "isolated").lower()
    if margin_mode not in {"cross", "isolated"}:
        margin_mode = "isolated"
    pos_side = str(position.get("side") or "net").lower()
    if pos_side not in {"long", "short"}:
        pos_side = ""
    if stop is None or stop <= 0:
        return []
    aligned_targets: list[dict[str, float]] = []
    for target in targets:
        target_price = spec.aligned_price(target["price"])
        if target_price is None or target_price <= 0:
            continue
        duplicate = next((row for row in aligned_targets if math.isclose(row["price"], target_price, rel_tol=0.0, abs_tol=1e-12)), None)
        if duplicate is None:
            aligned_targets.append({"price": target_price, "quantityPercent": target["quantityPercent"]})
        else:
            duplicate["quantityPercent"] += target["quantityPercent"]
    targets = aligned_targets
    results: list[dict[str, Any]] = []
    remaining = size_total
    created_algo_ids: list[str] = []
    try:
        # If every proposed target has already been crossed, retain a stop on
        # the live position rather than leaving it unprotected. OKX accepts a
        # conditional algorithm with only the stop-loss leg.
        if not targets:
            client_seed = f"{instrument_id}:{position.get('id')}:{decision.decisionId}:stop:{stop}:{size_total}"
            client_id = "aip" + hashlib.sha256(client_seed.encode("utf-8")).hexdigest()[:29]
            body: dict[str, Any] = {
                "instId": instrument_id, "tdMode": margin_mode,
                "side": "sell" if direction == "long" else "buy", "ordType": "conditional",
                "sz": size_total, "slTriggerPx": stop, "slOrdPx": "-1", "slTriggerPxType": "mark",
                "reduceOnly": "true", "algoClOrdId": client_id,
            }
            if pos_side:
                body["posSide"] = pos_side
            response = await okx_private_request("POST", "/trade/order-algo", body=body)
            row = (response.get("data") or [{}])[0] if isinstance(response, dict) else {}
            algo_id = row.get("algoId") if isinstance(row, dict) else None
            if not algo_id:
                raise HTTPException(status_code=502, detail="OKX protection order did not include algoId")
            return [{"algoID": algo_id, "clientOrderID": client_id, "quantity": size_total,
                     "takeProfitPrice": None, "stopLossPrice": stop}]
        percent_total = sum(target["quantityPercent"] for target in targets)
        for index, target in enumerate(targets):
            target_price = target["price"]
            normalized_percent = target["quantityPercent"] / percent_total * 100
            tranche = size_total if index == len(targets) - 1 else math.floor(size_total * normalized_percent / 100 / spec.lotSize) * spec.lotSize
            tranche = min(max(tranche, 0), remaining)
            if tranche < spec.minSize:
                continue
            remaining = round(remaining - tranche, 12)
            client_seed = f"{instrument_id}:{position.get('id')}:{decision.decisionId}:{index}:{target_price}:{stop}:{tranche}"
            client_id = "aip" + hashlib.sha256(client_seed.encode("utf-8")).hexdigest()[:29]
            body: dict[str, Any] = {
                "instId": instrument_id, "tdMode": margin_mode,
                "side": "sell" if direction == "long" else "buy", "ordType": "oco",
                "sz": tranche, "tpTriggerPx": target_price, "tpOrdPx": "-1", "tpTriggerPxType": "mark",
                "slTriggerPx": stop, "slOrdPx": "-1", "slTriggerPxType": "mark",
                "reduceOnly": "true", "algoClOrdId": client_id,
            }
            if pos_side:
                body["posSide"] = pos_side
            response = await okx_private_request("POST", "/trade/order-algo", body=body)
            row = (response.get("data") or [{}])[0] if isinstance(response, dict) else {}
            algo_id = row.get("algoId") if isinstance(row, dict) else None
            if not algo_id:
                raise HTTPException(status_code=502, detail="OKX protection order did not include algoId")
            created_algo_ids.append(str(algo_id))
            results.append({"algoID": algo_id, "clientOrderID": client_id, "quantity": tranche, "takeProfitPrice": target_price, "stopLossPrice": stop})
    except Exception:
        # A staged set is only useful as a whole.  If a later tranche fails,
        # remove successful earlier tranches so the next cycle can retry a
        # complete set without stacking orphaned OCOs.
        if created_algo_ids:
            try:
                await _cancel_position_protections(instrument_id, {"algoIDs": created_algo_ids})
            except Exception:
                # The original exchange error remains the useful diagnostic;
                # reconciliation will fail closed if cancellation is unknown.
                pass
        raise
    return results


async def _cancel_position_protections(instrument_id: str, protection: dict[str, Any]) -> list[str]:
    """Cancel the exchange-owned algo legs before replacing protection."""
    algo_ids = [str(value) for value in protection.get("algoIDs", []) if str(value).strip()]
    if not algo_ids:
        return []
    body = [{"instId": instrument_id, "algoId": algo_id} for algo_id in dict.fromkeys(algo_ids)]
    await okx_private_request("POST", "/trade/cancel-algos", body=body)
    return list(dict.fromkeys(algo_ids))


def _protection_needs_adjustment(
    position: dict[str, Any], protection: dict[str, Any], assessment: Any, decision: AIDecision,
) -> bool:
    """Permit only high-confidence, material line changes toward profit.

    Protection replacement is atomic: a worse stop paired with a better target
    must not replace the existing set. For a long position, higher stops and
    targets are safer; for a short position, lower stops and targets are safer.
    """
    direction = _position_direction(position)
    if direction not in {"long", "short"} or not protection.get("algoIDs"):
        return False
    model_confidence = as_float(getattr(decision, "confidence", 0))
    assessment_confidence = as_float(getattr(assessment, "confidence", 0))
    if model_confidence < MIN_PROTECTION_REPLACEMENT_CONFIDENCE or assessment_confidence < MIN_PROTECTION_REPLACEMENT_CONFIDENCE:
        return False
    if len(str(getattr(decision, "reason", "") or "").strip()) < MIN_EXIT_REASON_LENGTH or len(str(getattr(assessment, "reason", "") or "").strip()) < MIN_EXIT_REASON_LENGTH:
        return False
    targets = [row["price"] for row in _active_assessment_targets(position, assessment)]
    current_targets = [as_float(value) for value in protection.get("takeProfitPrices", [])]
    current_targets = [value for value in current_targets if value > 0]
    proposed_stop = as_float(getattr(assessment, "stopLossPrice", None))
    current_stops = [as_float(value) for value in protection.get("stopLossPrices", [])]
    current_stops = [value for value in current_stops if value > 0]
    if not targets or not current_targets or not current_stops or proposed_stop <= 0:
        return False

    if not _protection_moves_toward_profit(
        direction,
        current_stops=current_stops,
        proposed_stop=proposed_stop,
        current_targets=current_targets,
        proposed_targets=targets,
    ):
        return False

    def materially_different(left: list[float], right: list[float]) -> bool:
        if len(left) != len(right):
            return True
        return any(abs(a - b) / max(abs(a), abs(b), 1e-12) >= 0.01 for a, b in zip(sorted(left), sorted(right)))

    return materially_different(targets, current_targets) or materially_different([proposed_stop], current_stops)


def _protection_moves_toward_profit(
    direction: str,
    *,
    current_stops: list[float],
    proposed_stop: float | None,
    current_targets: list[float],
    proposed_targets: list[float],
) -> bool:
    """Return whether every replacement line is no worse than the current set."""
    if direction not in {"long", "short"} or proposed_stop is None or proposed_stop <= 0:
        return False
    epsilon = 1e-12
    if current_stops:
        if direction == "long" and proposed_stop + epsilon < max(current_stops):
            return False
        if direction == "short" and proposed_stop - epsilon > min(current_stops):
            return False
    if current_targets and not proposed_targets:
        return False
    if not current_targets:
        return True
    reverse = direction == "short"
    current = sorted(current_targets, reverse=reverse)
    proposed = sorted(proposed_targets, reverse=reverse)
    overlap = min(len(current), len(proposed))
    if direction == "long":
        if any(proposed[index] + epsilon < current[index] for index in range(overlap)):
            return False
        return len(proposed) <= len(current) or all(
            value + epsilon >= current[-1] for value in proposed[len(current):]
        )
    if any(proposed[index] - epsilon > current[index] for index in range(overlap)):
        return False
    return len(proposed) <= len(current) or all(
        value - epsilon <= current[-1] for value in proposed[len(current):]
    )


async def _reconcile_position_protections(decision: AIDecision, snapshot: AISnapshot) -> list[dict[str, Any]]:
    """Every model cycle restores missing protection on known positions."""
    if local_paper_mode():
        restored = []
        broker = _get_paper_account()
        async with position_protection_lock:
            for position in broker.positions():
                assessment = _position_assessment(decision, position)
                if assessment is None or not _protection_is_reasonable(position, assessment):
                    continue
                missing_tp = not position.get("takeProfitPrice")
                missing_sl = not position.get("stopLossPrice")
                protection = {"algoIDs": [position["id"]], "takeProfitPrices": [row["price"] for row in position.get("takeProfitLevels", []) if not row.get("executed") and row.get("quantity", 0) > 0] or [position.get("takeProfitPrice")], "stopLossPrices": [position.get("stopLossPrice")]}
                if not missing_tp and not missing_sl and not _protection_needs_adjustment(position, protection, assessment, decision):
                    continue
                adjust = not missing_tp and not missing_sl
                current_target_prices = [value for value in protection["takeProfitPrices"] if as_float(value) > 0]
                current_stop_prices = [value for value in protection["stopLossPrices"] if as_float(value) > 0]
                proposed_target_prices = [row["price"] for row in _active_assessment_targets(position, assessment)]
                proposed_stop_price = as_float(getattr(assessment, "stopLossPrice", None))
                if not _protection_moves_toward_profit(
                    _position_direction(position),
                    current_stops=current_stop_prices,
                    proposed_stop=proposed_stop_price,
                    current_targets=current_target_prices,
                    proposed_targets=proposed_target_prices,
                ):
                    append_runtime_event(
                        "info", f"{position['instrumentID']} 跳过逆向止盈止损调整",
                        {
                            "type": "position-protection-regression-skipped", "instrumentID": position["instrumentID"],
                            "decisionID": decision.decisionId, "currentStopLossPrices": current_stop_prices,
                            "currentTakeProfitPrices": current_target_prices, "proposedStopLossPrice": proposed_stop_price,
                            "proposedTakeProfitPrices": proposed_target_prices,
                        },
                    )
                    continue
                result = await broker.update_protection(
                    position["id"],
                    stop_loss=getattr(assessment, "stopLossPrice", None) if missing_sl or adjust else None,
                    take_profit_levels=(_active_assessment_targets(position, assessment) or None) if missing_tp or adjust else None,
                    take_profit=getattr(assessment, "takeProfitPrice", None) if missing_tp or adjust else None,
                )
                restored.append(result)
        return restored
    if not OKX_DEMO and not bool(read_state("live-trading.json", {}).get("enabled", False)):
        return []
    account_data = snapshot.account if isinstance(snapshot.account, dict) else {}
    if account_data.get("authenticated") is not True or account_data.get("positionsKnown", True) is not True:
        return []
    positions = account_data.get("positions")
    if not isinstance(positions, list):
        return []
    created: list[dict[str, Any]] = []
    async with position_protection_lock:
        # Read the exchange-owned algorithm set again immediately before any
        # write, preventing a stale AI snapshot from creating duplicates.
        pending = await _pending_position_protections(include_details=True)
        lookup_meta = pending.get("_meta") if isinstance(pending, dict) else None
        if not isinstance(lookup_meta, dict) or lookup_meta.get("lookupComplete") is not True:
            # Fail closed.  An unavailable algorithm endpoint is not evidence
            # that a position lacks protection; wait for the next cycle.
            append_runtime_event(
                "warning", "持仓保护状态无法完整核验，跳过本轮补挂",
                {"type": "position-protection-state-unknown", "errors": (lookup_meta or {}).get("lookupErrors", [])},
            )
            return []
        for position in positions:
            if not isinstance(position, dict) or as_float(position.get("quantity")) == 0:
                continue
            instrument = str(position.get("instrumentID") or "")
            key = _position_protection_key({"instId": instrument, "posSide": position.get("side")})
            current = pending.get(key) or pending.get(f"{instrument}|net")
            assessment = _position_assessment(decision, position)
            if assessment is None or not _protection_is_reasonable(position, assessment):
                continue
            has_tp = bool(current and current.get("takeProfitPrice")) or bool(position.get("takeProfitPrice"))
            has_sl = bool(current and current.get("stopLossPrice")) or bool(position.get("stopLossPrice"))
            current_target_prices = [
                as_float(value) for value in (current or {}).get("takeProfitPrices", []) if as_float(value) > 0
            ]
            if not current_target_prices and as_float(position.get("takeProfitPrice")) > 0:
                current_target_prices = [as_float(position["takeProfitPrice"])]
            current_stop_prices = [
                as_float(value) for value in (current or {}).get("stopLossPrices", []) if as_float(value) > 0
            ]
            if not current_stop_prices and as_float(position.get("stopLossPrice")) > 0:
                current_stop_prices = [as_float(position["stopLossPrice"])]
            proposed_target_prices = [row["price"] for row in _active_assessment_targets(position, assessment)]
            proposed_stop_price = as_float(getattr(assessment, "stopLossPrice", None))
            if (has_tp or has_sl) and not _protection_moves_toward_profit(
                _position_direction(position),
                current_stops=current_stop_prices,
                proposed_stop=proposed_stop_price,
                current_targets=current_target_prices,
                proposed_targets=proposed_target_prices,
            ):
                append_runtime_event(
                    "info", f"{instrument} 跳过逆向止盈止损调整",
                    {
                        "type": "position-protection-regression-skipped", "instrumentID": instrument,
                        "decisionID": decision.decisionId, "currentStopLossPrices": current_stop_prices,
                        "currentTakeProfitPrices": current_target_prices, "proposedStopLossPrice": proposed_stop_price,
                        "proposedTakeProfitPrices": proposed_target_prices,
                    },
                )
                continue
            # New entries with staged targets attach only the stop on the
            # parent order; once filled, the first protection query can show
            # a one-sided/one-target set.  Complete a demonstrably short
            # target set even when a line move would otherwise require high
            # confidence.  This is restoration of the requested plan, not a
            # discretionary price adjustment.
            expected_target_count = len(_active_assessment_targets(position, assessment))
            current_target_count = len(
                [value for value in (current or {}).get("takeProfitPrices", []) if as_float(value) > 0]
            )
            needs_completion = bool(
                current and has_tp and has_sl
                and expected_target_count > current_target_count
            )
            needs_adjustment = bool(
                current and has_tp and has_sl
                and _protection_needs_adjustment(position, current, assessment, decision)
            )
            if has_tp and has_sl and not needs_adjustment and not needs_completion:
                continue
            try:
                canceled_ids: list[str] = []
                if current and (needs_adjustment or needs_completion or not (has_tp and has_sl)):
                    # A partial or one-sided protection set must be replaced
                    # as a unit so the stop and all targets remain consistent.
                    if current.get("unmanagedOrders") or not current.get("algoIDs"):
                        raise HTTPException(status_code=409, detail="现有持仓保护单缺少可撤销的算法ID，跳过替换")
                    canceled_ids = await _cancel_position_protections(instrument, current)
                rows = await _submit_position_protection(position, assessment, decision=decision)
            except Exception as error:
                append_runtime_event(
                    "error", f"{instrument} 持仓保护单补挂失败",
                    {"type": "position-protection-failure", "instrumentID": instrument, "error": str(error), "decisionID": decision.decisionId},
                )
                continue
            if rows:
                created.extend([{**row, "instrumentID": instrument, "decisionID": decision.decisionId} for row in rows])
                append_runtime_event(
                    "info", f"{instrument} {'已调整' if needs_adjustment else '已补挂'}持仓止盈止损",
                    {
                        "type": "position-protection-adjusted" if needs_adjustment else "position-protection-restored",
                        "instrumentID": instrument, "decisionID": decision.decisionId,
                        "orders": rows, "canceledAlgoIDs": canceled_ids,
                        "decisionReason": decision.reason,
                        "assessmentReason": getattr(assessment, "reason", ""),
                    },
                )
    return created


def position_snapshot(row: dict[str, Any], *, protection: dict[str, float | None] | None = None) -> dict[str, Any] | None:
    quantity = as_float(row.get("pos"))
    entry = as_float(row.get("avgPx"))
    identifier = row.get("posId")
    if quantity == 0 or not identifier or not row.get("instId") or entry <= 0:
        return None
    side = str(row.get("posSide") or "net").lower()
    if side not in {"net", "long", "short"}:
        return None
    margin = str(row.get("mgnMode") or "").lower()
    mark_price = as_float(row.get("markPx")) if row.get("markPx") else 0
    unrealized = as_float(row.get("upl")) if row.get("upl") is not None else None
    leverage = _positive_field(row, "lever", "leverage")
    margin_value = _positive_field(row, "margin", "imr", "initialMargin")
    if margin_value is None:
        notional = _positive_field(row, "notionalUsd", "notional")
        if notional is not None and leverage is not None:
            margin_value = notional / leverage
    take_profit, stop_loss = _protection_prices(row)
    if protection:
        take_profit = take_profit or protection.get("takeProfitPrice")
        stop_loss = stop_loss or protection.get("stopLossPrice")
    return {"id": identifier, "instrumentID": row["instId"], "side": side, "quantity": quantity,
            "entryPrice": entry, "markPrice": mark_price if mark_price > 0 else None,
            "unrealizedPnL": unrealized,
            "marginMode": margin if margin in {"cross", "isolated"} else None,
            "margin": margin_value, "leverage": leverage,
            "takeProfitPrice": take_profit, "stopLossPrice": stop_loss}


def order_snapshot(row: dict[str, Any], *, instrument: dict[str, Any] | None = None) -> dict[str, Any] | None:
    identifier = row.get("ordId")
    quantity = as_float(row.get("sz"))
    created = as_float(row.get("cTime") or row.get("uTime"))
    if not identifier or not row.get("instId") or quantity <= 0 or created <= 0:
        return None
    side = str(row.get("side") or "").lower()
    if side not in {"buy", "sell"}:
        return None
    timestamp = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat().replace("+00:00", "Z")
    price = as_float(row.get("px")) if row.get("px") else None
    status = str(row.get("state") or "").lower()
    if not status:
        return None
    filled = as_float(row.get("accFillSz")) if row.get("accFillSz") is not None else None
    fill_price = _positive_field(row, "averageFillPrice", "avgPx", "fillPx")
    leverage = _positive_field(row, "lever", "leverage")
    margin_value = _positive_field(row, "margin", "imr", "initialMargin")
    notional = _positive_field(row, "notionalUsd", "notional")
    if notional is None and instrument:
        contract_value = as_float(instrument.get("ctVal"))
        contract_multiplier = as_float(instrument.get("ctMult"), 1)
        reference_price = price or fill_price or 0
        if contract_value > 0 and contract_multiplier > 0 and reference_price > 0:
            notional = quantity * contract_value * contract_multiplier * reference_price
    if margin_value is None and notional is not None and leverage is not None:
        margin_value = notional / leverage
    take_profit, stop_loss = _protection_prices(row)
    margin_mode = str(row.get("tdMode") or row.get("mgnMode") or "").lower()
    return {"id": identifier, "instrumentID": row["instId"], "side": side,
            "status": status, "quantity": quantity, "price": price,
            "createdAt": timestamp, "filledQuantity": filled if filled is not None and filled >= 0 else None,
            "averageFillPrice": fill_price, "margin": margin_value, "leverage": leverage,
            "marginMode": margin_mode if margin_mode in {"cross", "isolated"} else None,
            "takeProfitPrice": take_profit, "stopLossPrice": stop_loss}


def _assert_ai_entry_current(request: dict[str, Any]) -> None:
    """Check the server-owned deadline immediately before exchange writes."""
    if request.get("source") != "ai" or request.get("reduceOnly"):
        return
    deadline = request.get("_aiEntryDeadline")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise OrderNotSubmittedError("AI entry deadline is unavailable or invalid")
    if datetime.now(timezone.utc).timestamp() >= deadline:
        raise OrderNotSubmittedError("AI entry expired before order submission")


async def _guard_ai_leverage_change(request: dict[str, Any]) -> None:
    """Recheck that no exposure appeared before an AI entry POST.

    The worker refreshes account state before building an order, but another
    order can fill while the gateway is preparing the request. This final
    exchange-owned check closes that race. Any current position or pending
    order blocks a new AI entry, even when its leverage happens to match.
    """
    if request.get("source") != "ai" or request.get("reduceOnly") or request.get("leverage") is None:
        return
    instrument = str(request.get("instrumentID") or "")
    if _positive_field({"value": request.get("leverage")}, "value") is None:
        raise OrderNotSubmittedError("AI leverage is unavailable or invalid")
    try:
        positions, pending = await asyncio.gather(
            okx_private_request("GET", "/account/positions", params={"instType": "SWAP"}),
            okx_private_request("GET", "/trade/orders-pending", params={"instType": "SWAP"}),
        )
    except Exception as error:
        raise OrderNotSubmittedError(f"核验当前合约持仓或挂单失败，订单未提交：{error}") from error

    position_rows = positions.get("data") if isinstance(positions, dict) else None
    pending_rows = pending.get("data") if isinstance(pending, dict) else None
    if not isinstance(position_rows, list) or not isinstance(pending_rows, list):
        raise OrderNotSubmittedError("无法核验当前持仓或挂单，订单未提交")
    for row in position_rows:
        if not isinstance(row, dict):
            raise OrderNotSubmittedError("无法核验当前持仓，订单未提交")
        if row.get("instId") != instrument:
            continue
        try:
            quantity = float(row.get("pos"))
            if not math.isfinite(quantity):
                raise ValueError
        except (TypeError, ValueError):
            raise OrderNotSubmittedError("无法核验当前持仓，订单未提交")
        if quantity != 0:
            raise OrderNotSubmittedError("当前合约已有持仓，订单未提交；请先平仓")
    for row in pending_rows:
        if not isinstance(row, dict):
            raise OrderNotSubmittedError("无法核验当前挂单，订单未提交")
        if row.get("instId") != instrument:
            continue
        status = str(row.get("state") or row.get("status") or "").lower()
        if status in {"canceled", "cancelled", "filled", "rejected", "failed", "expired", "mmp_canceled"}:
            continue
        if status not in {"live", "partially_filled", "waiting", "pending", "open", "queued"}:
            raise OrderNotSubmittedError("当前合约挂单状态不可核验，订单未提交")
        try:
            quantity = float(row.get("sz"))
            if not math.isfinite(quantity):
                raise ValueError
        except (TypeError, ValueError):
            raise OrderNotSubmittedError("无法核验当前挂单，订单未提交")
        if quantity > 0:
            raise OrderNotSubmittedError("当前合约已有挂单，订单未提交；请先撤单")


async def _ai_entry_preflight(
    request: dict[str, Any], *, fee_rate: float | None = None, slippage_bps: float | None = None,
) -> dict[str, Any]:
    """Refresh all executable-price inputs at the final submission boundary."""
    _assert_ai_entry_current(request)
    instrument = str(request.get("instrumentID") or "")
    try:
        responses = await asyncio.wait_for(asyncio.gather(
            okx_get("/market/ticker", {"instId": instrument}),
            okx_get("/market/books", {"instId": instrument, "sz": "5"}),
            okx_get("/public/mark-price", {"instType": "SWAP", "instId": instrument}),
        ), timeout=5)
        rows = []
        for response in responses:
            data = response.get("data") if isinstance(response, dict) else None
            if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
                raise EntryPreflightRejected("AI entry preflight: current market data is unavailable")
            rows.append(data[0])
        result = check_entry_preflight(
            request, *rows, now=datetime.now(timezone.utc), fee_rate=fee_rate, slippage_bps=slippage_bps,
        )
    except EntryPreflightRejected:
        raise
    except (HTTPException, httpx.HTTPError, asyncio.TimeoutError, ValueError, TypeError) as error:
        raise EntryPreflightRejected("AI entry preflight: unable to refresh current market data") from error
    _assert_ai_entry_current(request)
    return result


async def submit_order(request: dict[str, Any], *, demo: bool) -> dict[str, Any]:
    if local_paper_mode():
        raise OrderNotSubmittedError("纸面交易必须通过本地撮合账本提交")
    if not private_ready():
        raise HTTPException(status_code=503, detail="OKX private REST credentials are not configured")
    if demo != OKX_DEMO:
        raise HTTPException(status_code=409, detail="order mode does not match OKX_DEMO configuration")
    instrument = str(request.get("instrumentID", ""))
    side = str(request.get("side", "")).lower()
    order_type = str(request.get("orderType", "market")).lower()
    quantity = as_float(request.get("quantity"))
    margin_mode = str(request.get("marginMode", "cross")).lower()
    if not instrument or side not in {"buy", "sell"} or order_type not in {"market", "limit"}:
        raise HTTPException(status_code=422, detail="instrumentID, side and orderType are invalid")
    if quantity <= 0 or margin_mode not in {"cross", "isolated"}:
        raise HTTPException(status_code=422, detail="quantity or marginMode is invalid")
    client_order_id = str(request.get("clientOrderID") or uuid4().hex[:32])
    if not client_order_id.isalnum() or not 1 <= len(client_order_id) <= 32:
        raise HTTPException(status_code=422, detail="clientOrderID must be 1-32 ASCII letters or digits")
    body: dict[str, Any] = {"instId": instrument, "tdMode": margin_mode, "side": side,
                            "ordType": order_type, "sz": quantity, "clOrdId": client_order_id}
    for source, target in (("positionSide", "posSide"), ("price", "px")):
        if request.get(source) is not None:
            body[target] = request[source]
    if request.get("reduceOnly"):
        body["reduceOnly"] = "true"
    _assert_ai_entry_current(request)
    if request.get("leverage") is not None and not request.get("reduceOnly"):
        await _guard_ai_leverage_change(request)
        leverage_body = {"instId": instrument, "lever": request["leverage"], "mgnMode": margin_mode}
        if request.get("positionSide"):
            leverage_body["posSide"] = request["positionSide"]
        try:
            await okx_private_request("POST", "/account/set-leverage", body=leverage_body)
        except Exception as error:
            # No order POST has been sent. Even a leverage timeout cannot
            # create a position, so the gateway must release this reservation.
            raise OrderNotSubmittedError(f"设置杠杆失败，订单未提交：{error}") from error
    levels = request.get("takeProfitLevels")
    staged_levels = (
        isinstance(levels, list)
        and len(levels) > 1
        and all(isinstance(level, dict) for level in levels)
    )
    # Never attach a single top-level TP when the request carries multiple
    # staged levels; callers may still populate the legacy field for schema
    # compatibility, but doing so would silently discard the other targets.
    tp = None if staged_levels else request.get("takeProfitTriggerPrice")
    if tp is None and not staged_levels:
        # A single legacy target is supported directly by OKX's attached
        # algorithm.  Multiple targets are deferred to standalone OCOs after
        # the entry is visible as a position; attaching only the first target
        # would silently discard the remaining staged plan.
        if isinstance(levels, list) and levels:
            first = levels[0]
            if isinstance(first, dict):
                tp = first.get("price")
    sl = request.get("stopLossTriggerPrice")
    if tp is not None or sl is not None:
        algo: dict[str, Any] = {"attachAlgoClOrdId": client_order_id}
        if tp is not None:
            algo.update({"tpTriggerPx": tp, "tpOrdPx": "-1", "tpTriggerPxType": "mark"})
        if sl is not None:
            algo.update({"slTriggerPx": sl, "slOrdPx": "-1", "slTriggerPxType": "mark"})
        body["attachAlgoOrds"] = [algo]
    # Price/spec reads, gateway lock waits and the leverage request may have
    # crossed the deadline since the worker admitted this entry intent.
    _assert_ai_entry_current(request)
    # Setting leverage is a separate exchange request.  A different worker
    # can fill or place an order during that request, after the first
    # exposure check above. Recheck immediately before the order POST so the
    # entry gate covers that final race window as well.
    if request.get("leverage") is not None and not request.get("reduceOnly"):
        await _guard_ai_leverage_change(request)
        _assert_ai_entry_current(request)
    if request.get("source") == "ai" and not request.get("reduceOnly"):
        await _ai_entry_preflight(request)
        _assert_ai_entry_current(request)
        # Protect the quote-to-POST race: OKX cancels a post-only order if it
        # would take liquidity on arrival. Keep the domain type as "limit".
        body["ordType"] = "post_only"
    result = await okx_private_request("POST", "/trade/order", body=body)
    row = (result.get("data") or [{}])[0]
    order_id = row.get("ordId")
    if not order_id:
        raise HTTPException(status_code=502, detail="OKX response did not include ordId")
    return {"orderID": order_id, "clientOrderID": row.get("clOrdId", client_order_id),
            "instrumentID": instrument, "side": side, "orderType": order_type, "quantity": quantity,
            "status": "submitted", "message": row.get("sMsg"), "submittedAt": now_iso()}


async def _account_swap_instruments() -> dict[str, dict[str, Any]]:
    """Use production specs for local paper, account-specific specs for OKX."""
    global paper_instrument_cache
    if local_paper_mode():
        current = asyncio.get_running_loop().time()
        if paper_instrument_cache is not None and current - paper_instrument_cache[0] < 300:
            return paper_instrument_cache[1]
        payload = await okx_get("/public/instruments", {"instType": "SWAP"})
    else:
        payload = await okx_private_request("GET", "/account/instruments", params={"instType": "SWAP"})
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="OKX account instrument list is invalid")
    result = {
        str(row["instId"]): row for row in rows
        if isinstance(row, dict) and row.get("instId") and row.get("state") == "live"
        and row.get("settleCcy") == "USDT" and row.get("ctType") == "linear"
    }
    if local_paper_mode():
        paper_instrument_cache = (asyncio.get_running_loop().time(), result)
    return result


async def _ai_trading_availability(instruments: list[str]) -> dict[str, dict[str, Any]]:
    """Keep all observed contracts, separately report account execution limits."""
    mode = "纸面交易" if local_paper_mode() else "模拟盘" if OKX_DEMO else "实盘账户"
    try:
        available = await _account_swap_instruments()
    except Exception as error:
        reason = f"无法核验当前 OKX {mode}可交易合约：{_ai_collection_error(error)}"
        return {instrument: {"available": None, "reason": reason} for instrument in instruments}
    return {
        instrument: {"available": instrument in available,
                     "reason": "" if instrument in available else f"当前 OKX {mode}不支持此合约"}
        for instrument in instruments
    }


async def _instrument_spec(instrument_id: str) -> InstrumentSpec:
    available = await _account_swap_instruments()
    row = available.get(instrument_id)
    if row is None:
        mode = "纸面交易" if local_paper_mode() else "模拟盘" if OKX_DEMO else "实盘账户"
        raise HTTPException(status_code=422, detail=f"{instrument_id} 当前 OKX {mode}不可交易，未提交订单")
    try:
        return InstrumentSpec.from_okx(row)
    except OrderGatewayError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _ticker_last(instrument_id: str) -> float:
    payload = await okx_get("/market/ticker", {"instId": instrument_id})
    row = (payload.get("data") or [{}])[0]
    value = as_float(row.get("last"))
    if value <= 0:
        raise HTTPException(status_code=502, detail="OKX ticker did not include a valid last price")
    return value


async def _lookup_order(instrument_id: str, client_order_id: str, demo: bool) -> dict[str, Any] | None:
    try:
        payload = await okx_private_request("GET", "/trade/order", params={"instId": instrument_id, "clOrdId": client_order_id})
    except HTTPException as error:
        # OKX 51603 means the client order id is absent. 51001 means the
        # instrument itself does not exist, which also proves an order for
        # that instrument was never accepted. Other failures remain
        # unresolved and keep the reservation.
        if getattr(error, "exchange_code", "") in {"51603", "51001"}:
            return None
        raise
    rows = payload.get("data")
    row = rows[0] if isinstance(rows, list) and rows else None
    if not isinstance(row, dict) or not str(row.get("ordId") or "").strip():
        # A malformed/empty success is not proof that an earlier order was
        # absent. Only the explicit exchange absence code can release it.
        raise HTTPException(status_code=502, detail="OKX order lookup returned no verifiable order result")
    return {"orderID": row.get("ordId"), "clientOrderID": row.get("clOrdId", client_order_id), "status": row.get("state")}


async def _gateway_submit(request: dict[str, Any], demo: bool) -> dict[str, Any]:
    return await submit_order(request, demo=demo)


async def _sync_native_protection_once() -> list[dict[str, Any]]:
    """Reconcile native OCO exits without delaying account/order requests."""
    if local_paper_mode() or not private_ready():
        return []
    async with native_exit_sync_lock:
        try:
            return await sync_native_protection_exits(
                _get_order_gateway(), demo=OKX_DEMO, private_request=okx_private_request,
            )
        except Exception as error:
            append_runtime_event(
                "error", "原生 OCO 对账失败：未标记止盈/止损，请检查 OKX 历史接口和凭据",
                {"type": "native-protection-sync-error", "source": "exchange-native-oco", "error": str(error)},
            )
            return []


async def _reconcile_order_gateway_once() -> None:
    """Resolve durable unknown submissions without stopping the worker loop."""
    if local_paper_mode() or not private_ready():
        return
    try:
        await _get_order_gateway().reconcile(demo=OKX_DEMO)
    except Exception as error:
        append_runtime_event(
            "error", "订单状态对账失败：未确认的开仓继续保留，请检查 OKX 订单查询和凭据",
            {"type": "order-ledger-reconcile-error", "source": "order-gateway", "error": str(error)},
        )


async def _native_protection_loop() -> None:
    """Low-frequency native exit sync; the persisted audit performs dedupe."""
    while True:
        await _reconcile_order_gateway_once()
        await _sync_native_protection_once()
        await asyncio.sleep(60)


def _get_order_gateway() -> OrderGateway:
    global order_gateway
    if order_gateway is None:
        order_gateway = OrderGateway(
            state_dir() / "order-ledger.json", submit=_gateway_submit, lookup=_lookup_order,
            runtime_sink=_gateway_runtime_sink,
        )
    return order_gateway


async def _validate_fixed_instruments(values: dict[str, Any]) -> None:
    """Validate a requested observation set against live USDT swap IDs."""
    if "allowedInstruments" not in values:
        return
    instruments = normalize_contract_ids(
        values.get("allowedInstruments"), "allowedInstruments", allow_empty=False
    )
    try:
        rows = await contracts(fresh=True)
    except Exception as error:
        raise HTTPException(status_code=503, detail="无法刷新 OKX 合约列表，固定观察池未更新") from error
    available = {str(row.get("id")) for row in rows if row.get("id")}
    unknown = [instrument for instrument in instruments if instrument not in available]
    if unknown:
        raise HTTPException(status_code=422, detail=f"观察合约不存在或当前不可用：{', '.join(unknown)}")
    values["allowedInstruments"] = instruments


def _ai_candidate_ids(config: AIConfig, contract_rows: list[dict[str, Any]]) -> list[str]:
    """Resolve the fixed AI observation allowlist before collecting data."""
    available = {str(row.get("id")): row for row in contract_rows if row.get("id")}
    if config.allowedInstruments:
        # Preserve the user's order and de-duplicate without ranking or
        # silently substituting another contract when one is unavailable.
        return list(dict.fromkeys(
            instrument for instrument in config.allowedInstruments if instrument in available
        ))
    # Empty is retained as a legacy configuration default. Keep the
    # historical BTC default only when that exact contract is available;
    # never fall back to an arbitrary first contract.
    return ["BTC-USDT-SWAP"] if "BTC-USDT-SWAP" in available else []


def _ai_collection_error(error: Exception) -> str:
    """Return a useful diagnostic without including request headers or keys."""
    if isinstance(error, HTTPException):
        code = getattr(error, "exchange_code", "")
        prefix = f"OKX {code}: " if code else ""
        return prefix + str(error.detail)[:240]
    if isinstance(error, httpx.HTTPStatusError):
        return f"OKX HTTP {error.response.status_code}"
    if isinstance(error, httpx.TimeoutException):
        return "OKX request timed out"
    if isinstance(error, httpx.NetworkError):
        return "OKX network connection failed"
    return type(error).__name__


class _AIDataCollector:
    """Bound snapshot fan-out and pace the 40-per-2s candle endpoint."""

    def __init__(self, *, concurrency: int = 8, candle_interval: float = 0.1) -> None:
        self.semaphore = asyncio.Semaphore(concurrency)
        self.candle_lock = asyncio.Lock()
        self.candle_interval = candle_interval
        self.next_candle_at = 0.0
        self.errors: list[dict[str, Any]] = []

    async def _pace_candles(self) -> None:
        async with self.candle_lock:
            loop = asyncio.get_running_loop()
            delay = self.next_candle_at - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
            # Ten requests a second leave headroom for the chart's requests.
            # Never catch up with a burst after a slow request or sleep.
            self.next_candle_at = loop.time() + self.candle_interval

    @staticmethod
    def _retryable(error: Exception) -> bool:
        if isinstance(error, (httpx.TimeoutException, httpx.NetworkError)):
            return True
        if isinstance(error, httpx.HTTPStatusError):
            return error.response.status_code == 429
        return isinstance(error, HTTPException) and (
            error.status_code == 429 or getattr(error, "upstream_status_code", None) == 429
            or getattr(error, "exchange_code", "") == "50011"
        )

    def unavailable(self, metadata: dict[str, Any], message: str) -> None:
        metadata["available"] = False
        metadata["error"] = message
        self.errors.append({key: value for key, value in metadata.items() if key in {
            "resource", "instrumentID", "interval", "attempts", "error",
        }})

    async def fetch(self, request, *, resource: str, instrument: str | None = None, interval: str | None = None):
        metadata: dict[str, Any] = {"resource": resource, "available": False}
        if instrument is not None:
            metadata["instrumentID"] = instrument
        if interval is not None:
            metadata["interval"] = interval
        for attempt in range(3):
            metadata["attempts"] = attempt + 1
            try:
                async with self.semaphore:
                    if resource == "candles":
                        await self._pace_candles()
                    response = await request()
                # Account/risk return partial results when one private GET
                # fails. Retry only their preserved transient read failures.
                quality = response.get("dataQuality", {}) if isinstance(response, dict) else {}
                transient_partial = (
                    resource == "account" and quality.get("dailyBillsRetryable")
                    or resource == "risk" and quality.get("accountRefreshRetryable")
                )
                if transient_partial and attempt < 2:
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                metadata.update(available=True, receivedAt=now_iso())
                return response, metadata
            except (HTTPException, httpx.HTTPError, ValueError, TypeError) as error:
                if attempt < 2 and self._retryable(error):
                    await asyncio.sleep(0.5 * (2 ** attempt))
                    continue
                self.unavailable(metadata, _ai_collection_error(error))
                return None, metadata


async def _ai_snapshot(strategy_id: str = "codex") -> AISnapshot:
    strategy_id = _strategy_id(strategy_id)
    """Build a credential-free snapshot using the selected strategy config."""
    collection_started = now_iso()
    collection_clock = asyncio.get_running_loop().time()
    worker = ai_workers.get(strategy_id) or (ai_worker if strategy_id == "codex" else None)
    config = worker.config if worker is not None else AIConfig()
    contract_rows = await contracts(fresh=True)
    available = {str(row.get("id")): row for row in contract_rows}
    candidates = _ai_candidate_ids(config, contract_rows)
    # The configured allowlist is the complete observation set; no ranking or
    # hidden prefilter can cause a selected contract to disappear.
    selected = list(candidates)
    candles: dict[str, list[dict[str, Any]]] = {}
    tickers: dict[str, dict[str, Any]] = {}
    funding: dict[str, Any] = {}
    books: dict[str, dict[str, Any]] = {}
    collector = _AIDataCollector()
    availability: dict[str, Any] = {"instruments": {}}

    async def collect_instrument(instrument: str):
        instrument_quality: dict[str, Any] = {"candles": {}}

        async def get_candles(interval: str):
            response, metadata = await collector.fetch(
                lambda: market_candles(instId=instrument, bar=interval),
                resource="candles", instrument=instrument, interval=interval,
            )
            # Keep all selected instruments and 60 rows per interval below
            # Codex's 1 MiB input limit. Preserve the current unclosed candle
            # so the model can distinguish live price from confirmed signals.
            rows = (response or {}).get("candles", [])[-60:]
            confirmed = [row for row in rows if row.get("confirmed") is True]
            metadata.update(rowCount=len(rows), confirmedRowCount=len(confirmed))
            if rows:
                metadata["latestCandleAt"] = rows[-1].get("timestamp")
                metadata["latestIsConfirmed"] = rows[-1].get("confirmed") is True
            elif metadata["available"]:
                collector.unavailable(metadata, "OKX returned no candle rows")
            if confirmed:
                metadata["latestConfirmedAt"] = confirmed[-1].get("timestamp")
            instrument_quality["candles"][interval] = metadata
            return rows

        async def get_public_row(path: str, resource: str, **params: str):
            response, metadata = await collector.fetch(
                lambda: okx_get(path, {"instId": instrument, **params}),
                resource=resource, instrument=instrument,
            )
            row = ((response or {}).get("data") or [{}])[0]
            if not isinstance(row, dict):
                row = {}
            if not row and metadata["available"]:
                collector.unavailable(metadata, f"OKX returned no {resource} data")
            if row.get("ts"):
                metadata["sourceTimestamp"] = row["ts"]
            if resource == "orderBook" and row and (not row.get("bids") or not row.get("asks")):
                collector.unavailable(metadata, "OKX returned an incomplete order book")
            instrument_quality[resource] = metadata
            return row

        intervals = ("15m", "5m", "1H", "4H")
        candle_rows, ticker, funding_row, book = await asyncio.gather(
            asyncio.gather(*(get_candles(interval) for interval in intervals)),
            get_public_row("/market/ticker", "ticker"),
            get_public_row("/public/funding-rate", "fundingRate"),
            get_public_row("/market/books", "orderBook", sz="5"),
        )
        availability["instruments"][instrument] = instrument_quality
        return instrument, dict(zip(intervals, candle_rows)), ticker, funding_row, book

    collected, (derivatives, market_context) = await asyncio.gather(
        asyncio.gather(*(collect_instrument(instrument) for instrument in selected)),
        collect_market_context(selected, okx_get),
    )
    for instrument, rows_by_interval, ticker, funding_row, book in collected:
        for interval, rows in rows_by_interval.items():
            candles[f"{instrument}/{interval}"] = rows
        tickers[instrument] = ticker
        funding[instrument] = funding_row
        books[instrument] = book

    account_result, risk_result, trading_availability = await asyncio.gather(
        collector.fetch(account, resource="account"),
        collector.fetch(risk, resource="risk"),
        _ai_trading_availability(selected),
    )
    account_snapshot, account_quality = account_result
    risk_snapshot, risk_quality = risk_result
    account_snapshot = dict(account_snapshot or {})
    if local_paper_mode():
        # Keep the model's projection aligned with the worker's configurable
        # cooldowns; the paper gateway repeats the same checks atomically.
        account_snapshot = _get_paper_account().account_snapshot(
            stop_loss_cooldown_seconds=config.stopLossCooldownSeconds,
            recent_stop_loss_window_seconds=config.recentStopLossWindowSeconds,
            recent_stop_loss_limit=config.recentStopLossLimit,
        )
    risk_snapshot = dict(risk_snapshot or {})
    # Prescreening must distinguish a confirmed empty order set from an
    # account provider that does not expose pending orders. Older providers
    # and test fixtures omit this field, so mark those snapshots unknown.
    if "pendingOrders" not in account_snapshot:
        account_snapshot["pendingOrders"] = None
    account_snapshot["pendingOrdersKnown"] = (
        account_snapshot.get("pendingOrdersKnown") is True
        and isinstance(account_snapshot.get("pendingOrders"), list)
    )
    # Unknown private data remains unknown. It is visible to Codex and the
    # existing order gateway still refuses missing equity or daily loss count.
    if account_quality["available"] and (not account_snapshot.get("authenticated") or account_snapshot.get("todayLossCount") is None):
        collector.unavailable(account_quality, "Authenticated account or daily loss count is unavailable")
    if account_snapshot.get("dataQuality", {}).get("dailyBillsError"):
        account_quality["sourceError"] = account_snapshot["dataQuality"]["dailyBillsError"]
    if risk_snapshot.get("dataQuality", {}).get("accountRefreshError"):
        collector.unavailable(risk_quality, risk_snapshot["dataQuality"]["accountRefreshError"])
    availability.update(account=account_quality, risk=risk_quality)
    # AI entry count is local and durable in the order gateway. Keep it in
    # the credential-free snapshot so Codex can explain why a new entry may
    # be refused before the gateway's atomic quota check.
    account_snapshot["todayAIOrderCount"] = _get_paper_account().daily_order_count() if local_paper_mode() else _get_order_gateway().daily_order_count()
    collection_completed = now_iso()
    # Include collection latency in freshness: stamping only the end makes
    # earlier ticker/book data appear new after a slow request or retry.
    captured = collection_started
    raw = {"capturedAt": captured, "instruments": [available[item] for item in candidates], "candles": candles, "tickers": tickers, "orderBook": books, "fundingRates": funding, "derivatives": derivatives, "marketContext": market_context, "account": account_snapshot, "risk": risk_snapshot}
    snapshot_id = __import__("hashlib").sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
    return AISnapshot(
        snapshotId=snapshot_id,
        capturedAt=captured,
        instruments=raw["instruments"],
        candles=candles,
        tickers=tickers,
        orderBook=books,
        fundingRates=funding,
        derivatives=derivatives,
        marketContext=market_context,
        account=account_snapshot,
        risk=risk_snapshot,
        ai={
            "mode": config.mode,
            "enabled": config.enabled,
            "maxDailyOrders": config.maxDailyOrders,
            "maxDailyLosses": config.maxDailyLosses,
            "marginPerOrderUSD": config.marginPerOrderUSD,
            "maxLeverage": config.maxLeverage,
            "stopLossCooldownSeconds": config.stopLossCooldownSeconds,
            "recentStopLossWindowSeconds": config.recentStopLossWindowSeconds,
            "recentStopLossLimit": config.recentStopLossLimit,
            "todayAIOrderCount": account_snapshot.get("todayAIOrderCount", 0),
            "todayLossCount": account_snapshot.get("todayLossCount"),
            "observationCount": len(selected),
            "selectedInstruments": list(selected),
            "tradingMode": "paper" if local_paper_mode() else "demo" if OKX_DEMO else "live",
            "tradingAvailability": trading_availability,
        },
        dataFreshness={
            # Polling cadence and order-admission freshness are separate
            # controls. A slower decision interval must not make a snapshot
            # stale while its model call is still being evaluated. The window
            # is configured (snapshotMaxAgeSeconds) because it must track the
            # provider's real latency, and it bounds the single model call.
            "capturedAt": captured, "maxAgeSeconds": config.snapshotMaxAgeSeconds,
            "collectionStartedAt": collection_started, "collectionCompletedAt": collection_completed,
            "collectionDurationSeconds": round(asyncio.get_running_loop().time() - collection_clock, 3),
            "availability": availability, "errors": collector.errors,
        },
    )


def _pending_order_for_cancel(snapshot: AISnapshot, decision: AIDecision) -> dict[str, Any]:
    account = snapshot.account if isinstance(snapshot.account, dict) else {}
    current_snapshot = any(key in account for key in ("authenticated", "pendingOrdersKnown")) or (
        isinstance(snapshot.dataFreshness, dict) and "availability" in snapshot.dataFreshness
    )
    if not current_snapshot and "pendingOrdersKnown" not in account:
        # Compatibility for credential-free callers that predate the account
        # execution map. Runtime snapshots always carry pendingOrdersKnown.
        return {}
    if account.get("pendingOrdersKnown") is not True or not isinstance(account.get("pendingOrders"), list):
        raise HTTPException(status_code=409, detail="pending order state is unavailable")
    for row in account["pendingOrders"]:
        if not isinstance(row, dict):
            continue
        order_id = str(row.get("id") or row.get("orderID") or row.get("ordId") or "")
        if order_id != str(decision.orderID):
            continue
        instrument = str(row.get("instrumentID") or row.get("instId") or "")
        if instrument != str(decision.instrumentID):
            raise HTTPException(status_code=409, detail="cancel order does not belong to the selected instrument")
        status = str(row.get("status") or row.get("state") or "").lower()
        if status not in {"live", "partially_filled", "waiting", "pending", "open", "queued"}:
            raise HTTPException(status_code=409, detail="cancel order is no longer cancellable")
        return row
    raise HTTPException(status_code=409, detail="cancel order is not a current pending order")


async def _ai_cancel(decision: AIDecision, snapshot: AISnapshot, *, demo: bool) -> dict[str, Any]:
    if not decision.instrumentID:
        raise HTTPException(status_code=422, detail="cancel requires instrumentID")
    if not decision.orderID:
        raise HTTPException(status_code=422, detail="cancel requires orderID")
    current_snapshot = any(key in snapshot.account for key in ("authenticated", "pendingOrdersKnown")) or (
        isinstance(snapshot.dataFreshness, dict) and "availability" in snapshot.dataFreshness
    )
    if current_snapshot:
        try:
            snapshot = replace(snapshot, account=await account())
        except (HTTPException, httpx.HTTPError, ValueError, TypeError) as error:
            raise HTTPException(status_code=409, detail="无法刷新挂单状态，撤单已拒绝") from error
    _pending_order_for_cancel(snapshot, decision)
    if local_paper_mode():
        await _get_paper_account().cancel_order(decision.orderID)
        return {"action": "cancel", "orderID": decision.orderID, "instrumentID": decision.instrumentID, "status": "cancelled", "submittedAt": now_iso()}
    gateway = _get_order_gateway()
    cancel = getattr(gateway, "cancel_order", None)
    if cancel is not None:
        await cancel(
            instrument_id=decision.instrumentID, order_id=decision.orderID, demo=demo,
            cancel=lambda: okx_private_request(
                "POST", "/trade/cancel-order",
                body={"instId": decision.instrumentID, "ordId": decision.orderID},
            ),
        )
    else:
        # Compatibility for injected test gateways created before the shared
        # cancellation path existed. The snapshot ownership check above still
        # applies in every path.
        await okx_private_request("POST", "/trade/cancel-order", body={"instId": decision.instrumentID, "ordId": decision.orderID})
    return {"action": "cancel", "orderID": decision.orderID, "instrumentID": decision.instrumentID, "status": "cancelled", "submittedAt": now_iso()}


async def _ai_execute(decision: AIDecision, snapshot: AISnapshot, strategy_id: str = "codex") -> dict[str, Any]:
    """Apply current account/risk state immediately before submitting an AI intent."""
    strategy_id = _strategy_id(strategy_id)
    worker = ai_workers.get(strategy_id) or (ai_worker if strategy_id == "codex" else None)
    config = worker.config if worker is not None else AIConfig()
    # Protection reconciliation is intentionally independent of the selected
    # top-level action. A hold/cancel round still evaluates every existing
    # position and restores missing OCO legs from the complete assessments.
    if decision.action == "cancel":
        await _reconcile_position_protections(decision, snapshot)
        return await _ai_cancel(decision, snapshot, demo=OKX_DEMO)
    if decision.action == "hold":
        restored = await _reconcile_position_protections(decision, snapshot)
        return {"action": "hold", "status": "reconciled", "protectionOrders": restored, "decisionID": decision.decisionId}
    if not decision.instrumentID or decision.direction not in {"long", "short"}:
        raise HTTPException(status_code=422, detail="AI action requires an instrument and direction")
    if decision.action == "close":
        account_data = snapshot.account if isinstance(snapshot.account, dict) else {}
        current_snapshot = any(key in account_data for key in ("authenticated", "pendingOrdersKnown")) or (
            isinstance(snapshot.dataFreshness, dict) and "availability" in snapshot.dataFreshness
        )
        if current_snapshot:
            try:
                snapshot = replace(snapshot, account=await account())
            except (HTTPException, httpx.HTTPError, ValueError, TypeError) as error:
                raise HTTPException(status_code=409, detail="无法刷新持仓状态，平仓已拒绝") from error
        # A close decision can target one position while other positions still
        # need missing or updated protection. Reconcile the full account after
        # the fresh position read so every AI cycle keeps all managed positions
        # protected.
        await _reconcile_position_protections(decision, snapshot)
    if decision.action == "open":
        account_data = snapshot.account if isinstance(snapshot.account, dict) else {}
        current_snapshot = any(key in account_data for key in ("authenticated", "pendingOrdersKnown")) or (
            isinstance(snapshot.dataFreshness, dict) and "availability" in snapshot.dataFreshness
        )
        if current_snapshot:
            try:
                fresh_account = await account()
                if local_paper_mode():
                    fresh_account = _get_paper_account().account_snapshot(
                        stop_loss_cooldown_seconds=config.stopLossCooldownSeconds,
                        recent_stop_loss_window_seconds=config.recentStopLossWindowSeconds,
                        recent_stop_loss_limit=config.recentStopLossLimit,
                    )
                snapshot = replace(snapshot, account=fresh_account, risk=await risk())
            except (HTTPException, httpx.HTTPError, ValueError, TypeError) as error:
                raise HTTPException(status_code=409, detail="无法刷新账户或风险状态，开仓已拒绝") from error
            risk_quality = snapshot.risk.get("dataQuality") if isinstance(snapshot.risk, dict) else None
            daily_pnl = as_float(snapshot.risk.get("dailyPnLPercent"), math.nan) if isinstance(snapshot.risk, dict) else math.nan
            expected_source = "paper" if local_paper_mode() else "okx"
            if not isinstance(risk_quality, dict) or risk_quality.get("equitySource") != expected_source or risk_quality.get("accountRefreshError") or risk_quality.get("accountRefreshRetryable"):
                raise HTTPException(status_code=409, detail="risk account refresh is unavailable")
            if not isinstance(snapshot.risk.get("killSwitch"), bool) or not math.isfinite(daily_pnl):
                raise HTTPException(status_code=409, detail="risk state is unavailable")
            if snapshot.risk.get("killSwitch") or daily_pnl <= -ACCOUNT_DAILY_LOSS_PERCENT:
                raise HTTPException(status_code=409, detail="risk kill switch is active")
        account_data = snapshot.account if isinstance(snapshot.account, dict) else {}
        await _reconcile_position_protections(decision, snapshot)
        if current_snapshot or "positions" in account_data or "pendingOrders" in account_data:
            exposure_error = entry_exposure_error(account_data, decision.instrumentID)
            if exposure_error:
                raise HTTPException(status_code=409, detail=exposure_error)
    entry_deadline = None
    if decision.action == "open":
        freshness = snapshot_freshness(snapshot, config, now=datetime.now(timezone.utc))
        if not freshness["valid"] or freshness["isStale"]:
            raise HTTPException(status_code=409, detail=freshness["error"] or "AI snapshot has expired")
        entry_deadline = min(
            parse_time(snapshot.capturedAt) + timedelta(seconds=freshness["maxAgeSeconds"]),
            parse_time(decision.validUntil),
        ).timestamp()
    # The snapshot's account section is generated by the authenticated
    # server and includes a UTC-day count of distinct negative-PnL OKX bills.
    # Entries stop at the cap; close/cancel remain available above it.
    if decision.action == "open":
        loss_count = snapshot.account.get("todayLossCount")
        if loss_count is None:
            raise HTTPException(status_code=409, detail="daily loss count is unavailable")
        loss_count_value = as_float(loss_count, math.nan)
        if not math.isfinite(loss_count_value) or loss_count_value < 0:
            raise HTTPException(status_code=409, detail="daily loss count is invalid")
        if loss_count_value >= config.maxDailyLosses:
            raise HTTPException(status_code=409, detail="maximum daily AI losses exceeded")
    live_enabled = bool(read_state("live-trading.json", {}).get("enabled", False))
    demo = OKX_DEMO
    if not local_paper_mode() and not demo and not live_enabled:
        raise HTTPException(status_code=409, detail="live trading is disabled")
    # Recheck the account's own instrument universe before reserving margin
    # or setting leverage. The public price feed alone cannot authorize it.
    spec = await _instrument_spec(decision.instrumentID)
    quote = None
    if local_paper_mode():
        ticker = await okx_get("/market/ticker", {"instId": decision.instrumentID})
        quote = (ticker.get("data") or [{}])[0]
        last = as_float(quote.get("last"))
        if last <= 0:
            raise HTTPException(status_code=502, detail="OKX ticker did not include a valid last price")
    else:
        last = await _ticker_last(decision.instrumentID)
    if decision.action == "open":
        entry_price = decision.limitPrice if decision.orderType == "limit" else last
        decision_targets = _assessment_targets(decision)
        decision_take_profit = decision.takeProfitPrice
        if decision_targets:
            # Risk/reward admission is based on the first reachable tranche;
            # using the farthest target could admit a staged plan whose first
            # partial exit does not meet the configured minimum ratio.
            decision_take_profit = min(row["price"] for row in decision_targets) if decision.direction == "long" else max(row["price"] for row in decision_targets)
        geometry_error = _check_protection_geometry(
            instrument_id=decision.instrumentID, direction=decision.direction,
            entry=entry_price, stop_loss=decision.stopLossPrice,
            take_profit=decision_take_profit,
        )
        if geometry_error:
            raise HTTPException(status_code=422, detail=geometry_error)
    equity = as_float(snapshot.account.get("availableEquityUSD"), 0)
    if decision.action == "open" and equity <= 0:
        raise HTTPException(status_code=409, detail="available account equity is unavailable")
    reduce_only = decision.action == "close"
    leverage = as_float(decision.leverage, 0) if not reduce_only else 1
    position_margin_mode = "isolated"
    if not reduce_only:
        if leverage < 1 or leverage > config.maxLeverage:
            raise HTTPException(status_code=422, detail="AI leverage exceeds configured maximum")
        target_notional = config.marginPerOrderUSD * leverage
    else:
        # Prefer the exact current position quantity so an AI close does not
        # depend on an entry-only risk budget. Fall back to the configured
        # margin size if a position is not present in the snapshot.
        position_quantity = 0.0
        matching_positions: list[dict[str, Any]] = []
        positions = snapshot.account.get("positions", []) if isinstance(snapshot.account.get("positions"), list) else []
        for position in positions:
            if not isinstance(position, dict) or position.get("instrumentID") != decision.instrumentID:
                continue
            position_side = str(position.get("side") or position.get("positionSide") or position.get("posSide") or "net").lower()
            if position_side == "net":
                signed_quantity = as_float(position.get("quantity"), 0)
                if signed_quantity == 0:
                    continue
                position_side = "short" if signed_quantity < 0 else "long"
            if position_side in {"long", "short"} and position_side != decision.direction:
                continue
            matching_positions.append(position)
        current_snapshot = any(key in snapshot.account for key in ("authenticated", "pendingOrdersKnown")) or (
            isinstance(snapshot.dataFreshness, dict) and "availability" in snapshot.dataFreshness
        )
        if len(matching_positions) != 1 and current_snapshot:
            raise HTTPException(status_code=409, detail="当前合约没有可按方向核验的持仓")
        position = matching_positions[0] if matching_positions else {}
        position_quantity = abs(as_float(position.get("quantity")))
        if position_quantity <= 0 and current_snapshot:
            raise HTTPException(status_code=409, detail="当前持仓数量不可用")
        position_side = str(position.get("side") or position.get("positionSide") or position.get("posSide") or "net").lower()
        if position_side == "net":
            signed_quantity = as_float(position.get("quantity"), 0)
            if signed_quantity == 0:
                if current_snapshot:
                    raise HTTPException(status_code=409, detail="当前持仓方向不可用")
            else:
                position_side = "short" if signed_quantity < 0 else "long"
        if current_snapshot and position_side not in {"long", "short"}:
            raise HTTPException(status_code=409, detail="当前持仓方向不可用")
        if position_side in {"long", "short"}:
            position_margin_mode = str(position.get("marginMode") or "").lower()
            if position_margin_mode not in {"cross", "isolated"}:
                position_margin_mode = "isolated"
        else:
            position_margin_mode = str(position.get("marginMode") or "").lower()
            if position_margin_mode not in {"cross", "isolated"}:
                position_margin_mode = "isolated"
        target_notional = max(config.marginPerOrderUSD, 1.0)
    side = "buy" if decision.direction == "long" else "sell"
    if reduce_only:
        side = "sell" if decision.direction == "long" else "buy"
    request = {
        "instrumentID": decision.instrumentID, "side": side, "orderType": decision.orderType or "market",
        "targetNotional": target_notional, "price": decision.limitPrice,
        "takeProfitTriggerPrice": decision.takeProfitPrice or (
            _assessment_targets(decision)[0]["price"] if _assessment_targets(decision) else None
        ),
        "takeProfitLevels": decision.takeProfitLevels,
        "stopLossTriggerPrice": decision.stopLossPrice,
        # AI entries are always isolated. A reduce-only close follows the
        # existing position's mode so an older cross position can still be
        # closed safely while new AI exposure stays isolated.
        "marginMode": position_margin_mode if reduce_only else "isolated",
        "reduceOnly": reduce_only, "source": "ai",
        "decisionID": decision.decisionId,
        "strategyID": strategy_id,
        "stopLossCooldownSeconds": config.stopLossCooldownSeconds,
        "recentStopLossWindowSeconds": config.recentStopLossWindowSeconds,
        "recentStopLossLimit": config.recentStopLossLimit,
        # Include strategy and action in the deterministic id. Providers can
        # legitimately emit the same decisionId, while an exit must never be
        # mistaken for a retried entry by the shared gateway ledger.
        "clientOrderID": (
            "ai" + hashlib.sha256(
                f"{strategy_id}:{config.provider}:{snapshot.snapshotId}:{decision.action}:{decision.decisionId}".encode("utf-8")
            ).hexdigest()
        )[:32],
    }
    if not reduce_only:
        request["leverage"] = leverage
        request["marginUSD"] = config.marginPerOrderUSD
        request["_aiEntryDeadline"] = entry_deadline
        request["_aiEntryTickSize"] = spec.tickSize
    elif position_quantity > 0:
        request["quantity"] = position_quantity
        if position_side in {"long", "short"}:
            request["positionSide"] = position_side
    if local_paper_mode():
        try:
            broker = _get_paper_account()
            output = await broker.submit_intent(
                request, instrument=spec, price=last, quote=quote, daily_order_limit=config.maxDailyOrders,
                entry_preflight=lambda value: _ai_entry_preflight(
                    value, fee_rate=broker.fee_rate, slippage_bps=broker.slippage_bps,
                ),
            )
            append_runtime_event("order", f"纸面订单 {decision.instrumentID} {side} 已本地提交", {"order": output})
            return output
        except EntryPreflightRejected:
            raise
        except OrderGatewayError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
    gateway = _get_order_gateway()
    try:
        return await gateway.submit_intent(
            request, demo=demo, instrument=spec, price=last,
            available_equity=equity, daily_order_limit=config.maxDailyOrders,
        )
    except EntryPreflightRejected:
        raise
    except OrderGatewayError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


def _strategy_id(value: str) -> str:
    if value not in AI_STRATEGY_CATALOG:
        raise HTTPException(status_code=404, detail="unknown AI strategy")
    return value


def _worker_halted(worker: Any) -> bool:
    """Return whether a worker is latched in the failure-halt state.

    Read defensively so status reads also work against fakes and future worker
    implementations that expose the same state under a different object.
    """
    status = getattr(worker, "status", None)
    return getattr(status, "state", None) == "halted"


async def _ensure_ai_worker(strategy_id: str = "codex", *, start: bool = False) -> AIWorker:
    global ai_worker
    strategy_id = _strategy_id(strategy_id)
    existing = ai_workers.get(strategy_id)
    if existing is None:
        existing = AIWorker(
            snapshot_provider=lambda: _ai_snapshot("codex"),
            order_gateway=lambda decision, snapshot: _ai_execute(decision, snapshot, "codex"),
            state_dir=state_dir(), strategy_id="codex",
        )
        ai_worker = existing
        ai_workers[strategy_id] = existing
    # Status/configuration reads must not resurrect a worker that halted after
    # repeated failures. An explicit enable/config update calls `start()`;
    # startup may start a clean persisted configuration via `start=True`.
    if start and not _worker_halted(existing) and existing.config.enabled and existing.config.mode not in {"disabled", "halted"}:
        await existing.start()
    return existing


@app.on_event("startup")
async def start_ai_worker() -> None:
    global native_exit_task, paper_market_task
    await _ensure_ai_worker("codex", start=True)
    if local_paper_mode():
        _get_paper_account()
        if paper_market_task is None or paper_market_task.done():
            paper_market_task = asyncio.create_task(_paper_market_loop())
        return
    await _reconcile_order_gateway_once()
    if native_exit_task is None or native_exit_task.done():
        native_exit_task = asyncio.create_task(_native_protection_loop())


@app.get("/api/v1/live/trading-status")
async def live_trading_status() -> dict[str, Any]:
    if local_paper_mode():
        return {"mode": "paper", "profile": "local-paper", "enabled": False,
                "available": False, "message": "纸面交易 · 本地撮合", "updatedAt": now_iso()}
    enabled = read_state("live-trading.json", {}).get("enabled", False)
    mode = "paper" if OKX_DEMO else "live" if private_ready() else "readOnly"
    return {"mode": mode, "profile": OKX_PROFILE if private_ready() else None, "enabled": enabled,
            "available": private_ready() and not OKX_DEMO,
            "message": "实盘账户已连接" if private_ready() and not OKX_DEMO else "未配置可下单的实盘凭据", "updatedAt": now_iso()}


@app.post("/api/v1/live/trading/enable")
async def enable_live_trading() -> dict[str, Any]:
    if local_paper_mode():
        raise HTTPException(status_code=409, detail="纸面交易模式不支持启用实盘交易")
    if not private_ready() or OKX_DEMO:
        raise HTTPException(status_code=409, detail="live trading requires non-demo OKX private credentials")
    write_state("live-trading.json", {"enabled": True})
    return await live_trading_status()


@app.post("/api/v1/live/trading/disable")
async def disable_live_trading() -> dict[str, Any]:
    write_state("live-trading.json", {"enabled": False})
    return await live_trading_status()


@app.post("/api/v1/live/orders")
async def live_order(request: dict[str, Any]) -> dict[str, Any]:
    if local_paper_mode():
        raise HTTPException(status_code=409, detail="纸面交易模式禁止提交 OKX 实盘订单")
    if not read_state("live-trading.json", {}).get("enabled", False):
        raise HTTPException(status_code=409, detail="live trading is disabled")
    if OKX_DEMO:
        raise HTTPException(status_code=409, detail="the configured OKX account is demo mode")
    current_risk = await risk()
    if (current_risk.get("killSwitch") or as_float(current_risk.get("dailyPnLPercent")) <= -ACCOUNT_DAILY_LOSS_PERCENT) and not bool(request.get("reduceOnly")):
        raise HTTPException(status_code=409, detail="risk kill switch is active")
    instrument_id = str(request.get("instrumentID") or "")
    spec = await _instrument_spec(instrument_id)
    price = await _ticker_last(instrument_id)
    try:
        return await _get_order_gateway().submit_intent(request, demo=False, instrument=spec, price=price)
    except OrderGatewayError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


@app.get("/api/v1/ai/status")
async def ai_status() -> dict[str, Any]:
    worker = await _ensure_ai_worker()
    return worker.get_status()


@app.get("/api/v1/ai/config")
async def ai_config() -> dict[str, Any]:
    worker = await _ensure_ai_worker()
    return worker.get_config()


def _check_strategy_provider(strategy_id: str, values: dict[str, Any]) -> None:
    _strategy_id(strategy_id)
    expected = "codex"
    provider = values.get("provider")
    if provider is not None and provider != expected:
        raise HTTPException(status_code=422, detail=f"{strategy_id} strategy requires provider={expected}")


def _check_ai_run_mode(values: dict[str, Any]) -> None:
    mode = values.get("mode")
    if mode in {None, "disabled", "halted", "shadow"}:
        return
    if local_paper_mode():
        if mode != "paper-active":
            raise HTTPException(status_code=409, detail="本地纸面账户只能使用 paper-active 执行模式")
    elif mode == "paper-active":
        raise HTTPException(status_code=409, detail="paper-active requires a local paper account")
    if mode == "live-armed" and (OKX_DEMO or not read_state("live-trading.json", {}).get("enabled", False)):
        raise HTTPException(status_code=409, detail="live AI mode requires the separate live trading switch")


@app.get("/api/v1/ai/strategies")
async def ai_strategies() -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for strategy_id in AI_STRATEGY_CATALOG:
        worker = await _ensure_ai_worker(strategy_id)
        catalog = AI_STRATEGY_CATALOG[strategy_id]
        result.append({
            "id": strategy_id,
            "name": "Codex AI 策略",
            "provider": worker.config.provider,
            "config": worker.get_config(),
            "status": worker.get_status(),
            "package": {"id": catalog["packageID"], "lifecycle": "candidate"},
            "source": {"ofTruth": catalog["sourceOfTruth"]},
            "runtime": dict(catalog["runtime"]),
            "liveGate": dict(catalog["liveGate"]),
        })
    return result


@app.get("/api/v1/ai/strategies/{strategy_id}/status")
async def strategy_ai_status(strategy_id: str) -> dict[str, Any]:
    strategy_id = _strategy_id(strategy_id)
    worker = await _ensure_ai_worker(strategy_id)
    value = worker.get_status()
    value.update({"strategyID": strategy_id, "provider": worker.config.provider})
    return value


@app.get("/api/v1/ai/strategies/{strategy_id}/config")
async def strategy_ai_config(strategy_id: str) -> dict[str, Any]:
    return (await _ensure_ai_worker(strategy_id)).get_config()


@app.patch("/api/v1/ai/strategies/{strategy_id}/config")
async def update_strategy_ai_config(strategy_id: str, config: dict[str, Any]) -> dict[str, Any]:
    strategy_id = _strategy_id(strategy_id)
    _check_strategy_provider(strategy_id, config)
    worker = await _ensure_ai_worker(strategy_id)
    _check_ai_run_mode(config)
    try:
        await _validate_fixed_instruments(config)
        value = worker.update_config(config)
    except (SchemaError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if worker.config.enabled and worker.config.mode not in {"disabled", "halted"}:
        await worker.start()
    else:
        await worker.stop()
    return value


@app.get("/api/v1/ai/strategies/{strategy_id}/decisions")
async def strategy_ai_decisions(strategy_id: str) -> list[dict[str, Any]]:
    worker = await _ensure_ai_worker(strategy_id)
    path = worker._path("ai-decisions.jsonl")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-1000:]
    except OSError:
        return []
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result.append(value)
        except json.JSONDecodeError:
            continue
    return result


@app.get("/api/v1/ai/strategies/{strategy_id}/audit")
async def strategy_ai_audit(strategy_id: str) -> list[dict[str, Any]]:
    return await strategy_ai_decisions(strategy_id)


@app.patch("/api/v1/ai/config")
async def update_ai_config(config: dict[str, Any]) -> dict[str, Any]:
    worker = await _ensure_ai_worker()
    _check_strategy_provider("codex", config)
    _check_ai_run_mode(config)
    try:
        await _validate_fixed_instruments(config)
        value = worker.update_config(config)
    except (SchemaError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    if worker.config.enabled and worker.config.mode not in {"disabled", "halted"}:
        await worker.start()
    else:
        await worker.stop()
    return value


@app.post("/api/v1/ai/chat")
async def ai_chat(request: dict[str, Any]) -> dict[str, Any]:
    """Conversational AI strategy configuration; never an order endpoint."""
    try:
        chat_request = AIChatRequest.from_dict(request)
    except (SchemaError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    worker = await _ensure_ai_worker()
    try:
        if chat_request.apply and chat_request.suggestion is not None:
            await _validate_fixed_instruments(chat_request.suggestion)
        return await worker.chat(
            chat_request.message,
            apply=chat_request.apply,
            suggestion=chat_request.suggestion,
        )
    except CodexError as error:
        # Chat is an informational control-plane interaction. A missing or
        # restarting Codex process must not turn the conversation into a 502;
        # return the same stable degraded shape used by AIWorker.chat.
        return {
            "schemaVersion": 1,
            "reply": "我暂时无法连接 AI 对话服务，但不会影响当前策略或订单。你可以稍后重试；配置建议只有在明确确认后才会应用。",
            "suggestion": None,
            "applied": False,
            "config": worker.get_config(),
            "degraded": True,
            "errorCode": "codex_unavailable",
        }
    except HTTPException:
        raise
    except (SchemaError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    except Exception as error:
        # Do not expose worker implementation errors as a transport failure
        # for ordinary conversation. Applying a reviewed patch remains strict.
        if chat_request.apply:
            raise HTTPException(status_code=500, detail="failed to apply AI configuration") from error
        return {
            "schemaVersion": 1,
            "reply": "AI 对话服务暂时不可用，当前策略和配置没有改变。请稍后重试。",
            "suggestion": None,
            "applied": False,
            "config": worker.get_config(),
            "degraded": True,
            "errorCode": "chat_unavailable",
        }


@app.post("/api/v1/ai/strategies/{strategy_id}/chat")
async def strategy_ai_chat(strategy_id: str, request: dict[str, Any]) -> dict[str, Any]:
    try:
        chat_request = AIChatRequest.from_dict(request)
    except (SchemaError, ValueError) as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    worker = await _ensure_ai_worker(strategy_id)
    try:
        if chat_request.apply and chat_request.suggestion is not None:
            await _validate_fixed_instruments(chat_request.suggestion)
        return await worker.chat(chat_request.message, apply=chat_request.apply, suggestion=chat_request.suggestion)
    except CodexError as error:
        return {
            "schemaVersion": 1,
            "reply": "AI 对话服务暂时不可用，当前策略和配置没有改变。请稍后重试。",
            "suggestion": None,
            "applied": False,
            "config": worker.get_config(),
            "degraded": True,
            "errorCode": "ai_provider_unavailable",
        }

@app.post("/api/v1/ai/enable")
async def enable_ai() -> dict[str, Any]:
    worker = await _ensure_ai_worker()
    # A failure halt is cleared only by an explicit configuration update, which
    # is the operator's review step. Enable must not double as that recovery:
    # update_config resets the durable latch, so checking the status here (and
    # returning before it) keeps the two paths distinct.
    if _worker_halted(worker):
        raise HTTPException(status_code=409, detail="AI worker is halted; save a configuration update after reviewing the error")
    if worker.config.mode == "disabled":
        if not local_paper_mode() and not OKX_DEMO and not read_state("live-trading.json", {}).get("enabled", False):
            raise HTTPException(status_code=409, detail="enable live trading before arming AI for live orders")
        mode = "paper-active" if local_paper_mode() else "demo-active" if OKX_DEMO else "live-armed"
        worker.update_config({"enabled": True, "mode": mode})
    elif worker.config.mode == "halted":
        raise HTTPException(status_code=409, detail="AI worker is halted; save a configuration update after reviewing the error")
    else:
        _check_ai_run_mode({"mode": worker.config.mode})
        worker.update_config({"enabled": True})
    await worker.start()
    return worker.get_status()


@app.post("/api/v1/ai/strategies/{strategy_id}/enable")
async def enable_strategy_ai(strategy_id: str) -> dict[str, Any]:
    worker = await _ensure_ai_worker(strategy_id)
    if _worker_halted(worker):
        raise HTTPException(status_code=409, detail="AI worker is halted; save a configuration update after reviewing the error")
    if worker.config.mode == "disabled":
        if not local_paper_mode() and not OKX_DEMO and not read_state("live-trading.json", {}).get("enabled", False):
            raise HTTPException(status_code=409, detail="enable live trading before arming AI for live orders")
        worker.update_config({"enabled": True, "mode": "paper-active" if local_paper_mode() else "demo-active" if OKX_DEMO else "live-armed"})
    elif worker.config.mode == "halted":
        raise HTTPException(status_code=409, detail="AI worker is halted; save a configuration update after reviewing the error")
    else:
        _check_ai_run_mode({"mode": worker.config.mode})
        worker.update_config({"enabled": True})
    await worker.start()
    return worker.get_status()


@app.post("/api/v1/ai/disable")
async def disable_ai() -> dict[str, Any]:
    worker = await _ensure_ai_worker()
    await worker.disable()
    return worker.get_status()


@app.post("/api/v1/ai/strategies/{strategy_id}/disable")
async def disable_strategy_ai(strategy_id: str) -> dict[str, Any]:
    worker = await _ensure_ai_worker(strategy_id)
    await worker.disable()
    return worker.get_status()


@app.post("/api/v1/ai/flatten")
async def flatten_ai(strategy_id: str = "codex") -> dict[str, Any]:
    """Flatten the active account and stop every AI entry worker.

    Orders and positions currently have no reliable strategy ownership field,
    so a strategy-specific flatten cannot safely target only one provider.
    The endpoint therefore has explicit account-wide semantics.
    """
    _strategy_id(strategy_id)
    workers = {id(item): item for item in ai_workers.values()}
    if ai_worker is not None:
        workers[id(ai_worker)] = ai_worker
    if not workers:
        await _ensure_ai_worker(strategy_id)
        workers = {id(item): item for item in ai_workers.values()}
    for worker in workers.values():
        await worker.disable()
    if local_paper_mode():
        broker = _get_paper_account()
        cancelled = await broker.cancel_pending_orders()
        await _refresh_paper_quotes()
        result = await broker.flatten()
        return {**result, "cancelledOrderIDs": cancelled + result["cancelledOrderIDs"], "strategyID": strategy_id}
    if not private_ready():
        raise HTTPException(status_code=503, detail="OKX private REST credentials are not configured")
    if not OKX_DEMO and not read_state("live-trading.json", {}).get("enabled", False):
        raise HTTPException(status_code=409, detail="live trading is disabled")
    cancelled: list[str] = []
    try:
        pending = await okx_private_request("GET", "/trade/orders-pending", params={"instType": "SWAP"})
        for row in pending.get("data", []):
            if row.get("instId") and row.get("ordId"):
                await okx_private_request("POST", "/trade/cancel-order", body={"instId": row["instId"], "ordId": row["ordId"]})
                cancelled.append(str(row["ordId"]))
    except HTTPException:
        pass
    closed: list[dict[str, Any]] = []
    positions_payload = await okx_private_request("GET", "/account/positions", params={"instType": "SWAP"})
    for row in positions_payload.get("data", []):
        quantity = abs(as_float(row.get("pos")))
        instrument_id = str(row.get("instId") or "")
        if quantity <= 0 or not instrument_id:
            continue
        spec = await _instrument_spec(instrument_id)
        mark = as_float(row.get("markPx"), as_float(row.get("avgPx")))
        position_side = str(row.get("posSide") or "net").lower()
        side = "sell" if position_side == "long" else "buy"
        if position_side == "net":
            side = "sell" if str(row.get("side") or "long").lower() == "long" else "buy"
        request = {
            "instrumentID": instrument_id, "side": side, "orderType": "market",
            "quantity": quantity, "marginMode": str(row.get("mgnMode") or "cross"),
            "reduceOnly": True,
        }
        if position_side in {"long", "short"}:
            request["positionSide"] = position_side
        try:
            result = await _get_order_gateway().submit_intent(request, demo=OKX_DEMO, instrument=spec, price=mark, force_reduce_only=True)
            closed.append(result)
        except (OrderGatewayError, HTTPException) as error:
            await _get_order_gateway().record_audit({"type": "flatten-error", "instrumentID": instrument_id, "error": str(error)})
    return {"strategyID": strategy_id, "scope": "account", "cancelledOrderIDs": cancelled, "closed": closed, "updatedAt": now_iso()}


@app.post("/api/v1/ai/strategies/{strategy_id}/flatten")
async def flatten_strategy_ai(strategy_id: str) -> dict[str, Any]:
    return await flatten_ai(strategy_id)


@app.get("/api/v1/ai/decisions")
async def ai_decisions() -> list[dict[str, Any]]:
    path = state_dir() / "ai-decisions.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-1000:]
    except OSError:
        return []
    result: list[dict[str, Any]] = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                result.append(value)
        except json.JSONDecodeError:
            continue
    return result


@app.get("/api/v1/ai/audit")
async def ai_audit() -> list[dict[str, Any]]:
    if local_paper_mode():
        return _get_paper_account().audit()
    return await _get_order_gateway().audit()


@app.get("/api/v1/strategies")
async def strategies() -> list[dict[str, Any]]:
    return strategy_configs()


@app.post("/api/v1/strategies")
async def create_strategy(config: dict[str, Any]) -> dict[str, Any]:
    state = runtime_state()
    values = strategy_configs()
    config = dict(config)
    config.setdefault("id", str(uuid4()))
    config.setdefault("enabled", False)
    values.append(config)
    state["strategies"] = values
    state.setdefault("statuses", {})[config["id"]] = strategy_status_snapshot(state, config)
    write_state("paper-state.json", state)
    return config


@app.patch("/api/v1/strategies/{strategy_id}")
async def update_strategy(strategy_id: str, config: dict[str, Any]) -> dict[str, Any]:
    state = runtime_state()
    values = strategy_configs()
    for index, current in enumerate(values):
        if current.get("id") == strategy_id:
            config = dict(config)
            config["id"] = strategy_id
            values[index] = config
            state["strategies"] = values
            write_state("paper-state.json", state)
            return config
    raise HTTPException(status_code=404, detail="strategy not found")


@app.delete("/api/v1/strategies/{strategy_id}")
async def delete_strategy(strategy_id: str) -> dict[str, Any]:
    state = runtime_state()
    values = strategy_configs()
    remaining = [item for item in values if item.get("id") != strategy_id]
    if len(remaining) == len(values):
        raise HTTPException(status_code=404, detail="strategy not found")
    state["strategies"] = remaining
    state.get("statuses", {}).pop(strategy_id, None)
    write_state("paper-state.json", state)
    return next(item for item in values if item.get("id") == strategy_id)


async def set_strategy_enabled(strategy_id: str, enabled: bool) -> dict[str, Any]:
    state = runtime_state()
    values = strategy_configs()
    for item in values:
        if item.get("id") == strategy_id:
            item["enabled"] = enabled
            state["strategies"] = values
            write_state("paper-state.json", state)
            return item
    raise HTTPException(status_code=404, detail="strategy not found")


@app.post("/api/v1/strategies/{strategy_id}/start")
async def start_strategy(strategy_id: str) -> dict[str, Any]:
    return await set_strategy_enabled(strategy_id, True)


@app.post("/api/v1/strategies/{strategy_id}/pause")
async def pause_strategy(strategy_id: str) -> dict[str, Any]:
    return await set_strategy_enabled(strategy_id, False)


@app.get("/api/v1/strategies/status")
async def strategy_status() -> list[dict[str, Any]]:
    state = runtime_state()
    return [strategy_status_snapshot(state, config) for config in strategy_configs()]


@app.get("/api/v1/strategies/capital")
async def strategy_capital() -> list[dict[str, Any]]:
    state = runtime_state()
    base = await strategy_capital_base(state)
    return [strategy_capital_snapshot(state, config, base) for config in strategy_configs()]


@app.get("/api/v1/strategies/targets")
async def strategy_targets(fresh: bool = False) -> list[dict[str, Any]]:
    state = runtime_state()
    try:
        contracts_data = await contracts(fresh=fresh)
    except (HTTPException, httpx.HTTPError):
        contracts_data = []
    return [strategy_target_snapshot(config, contracts_data) for config in strategy_configs()]


@app.get("/api/v1/risk")
async def risk() -> dict[str, Any]:
    state = runtime_state()
    if local_paper_mode():
        await _refresh_paper_quotes()
        value = _get_paper_account().risk_snapshot()
        value["strategyCapitals"] = [strategy_capital_snapshot(state, config, value["equity"]) for config in strategy_configs()]
        return value
    stored = state.get("risk") if isinstance(state.get("risk"), dict) else {}
    base = await strategy_capital_base(state)
    equity = as_float(stored.get("equity"))
    equity_source = "local"
    refresh_error = None
    refresh_retryable = False
    if private_ready():
        try:
            account_payload = await okx_private_request("GET", "/account/balance")
            live_equity = as_float((account_payload.get("data") or [{}])[0].get("totalEq"))
            if live_equity > 0:
                equity = live_equity
                equity_source = "okx"
        except (HTTPException, httpx.HTTPError) as error:
            refresh_error = _ai_collection_error(error)
            refresh_retryable = _AIDataCollector._retryable(error)
    # Mark-to-market daily loss measured against the UTC day-start equity. The
    # baseline is rolled and persisted here because an exchange account carries
    # no other day-start record: without it the account circuit breaker could
    # never trip outside paper mode and every entry gate would read a
    # meaningless zero.
    today = datetime.now(timezone.utc).date().isoformat()
    authoritative = equity_source == "okx" and equity > 0
    day_start = as_float(stored.get("dayStartEquity"))
    day_start_day = str(stored.get("dayStartDay") or "")
    persist = False
    if authoritative and (day_start <= 0 or day_start_day != today):
        day_start = equity
        day_start_day = today
        stored["dayStartEquity"] = day_start
        stored["dayStartDay"] = today
        stored["dayStartAt"] = today + "T00:00:00Z"
        persist = True
    daily_pnl = as_float(stored.get("dailyPnLPercent"))
    if authoritative and day_start > 0:
        daily_pnl = (equity - day_start) / day_start * 100
    peak = as_float(stored.get("equityPeak"), equity)
    if authoritative and equity > peak:
        peak = equity
        stored["equityPeak"] = peak
        persist = True
    drawdown = (peak - equity) / peak * 100 if peak > 0 and equity > 0 else as_float(stored.get("drawdownPercent"))
    kill_switch = bool(stored.get("killSwitch", False))
    reason = stored.get("reason")
    if authoritative and daily_pnl <= -ACCOUNT_DAILY_LOSS_PERCENT and not kill_switch:
        # Latch the account breaker so a later recovery within the same UTC day
        # cannot silently re-open entries.
        kill_switch = True
        reason = f"账户日内亏损达到 {ACCOUNT_DAILY_LOSS_PERCENT:g}%"
        stored.update({"killSwitch": True, "reason": reason})
        persist = True
    value = {"equity": equity, "strategyCapitalBase": base if base > 0 else None,
             "equityPeak": peak,
             "dayStartEquity": day_start if day_start > 0 else equity,
             "dayStartAt": stored.get("dayStartAt"),
             "dailyPnLPercent": daily_pnl,
             "drawdownPercent": drawdown,
             "killSwitch": kill_switch, "reason": reason,
             # dailyPnLAuthoritative is deliberately not named *Available /
             # *Error / *Retryable: it is informational, while the existing
             # refresh-error fields remain the fail-closed signals.
             "dataQuality": {"equitySource": equity_source, "accountRefreshError": refresh_error,
                             "accountRefreshRetryable": refresh_retryable,
                             "dailyPnLAuthoritative": authoritative},
             "strategyCapitals": [strategy_capital_snapshot(state, config, base) for config in strategy_configs()],
             "globalNotionals": stored.get("globalNotionals", {})}
    if persist:
        state["risk"] = {
            **stored,
            **{key: value[key] for key in (
                "equity", "equityPeak", "dayStartEquity", "dayStartAt",
                "dailyPnLPercent", "drawdownPercent", "killSwitch", "reason",
            )},
        }
        write_state("paper-state.json", state)
    return value


@app.post("/api/v1/risk/reset")
async def reset_risk() -> dict[str, Any]:
    if local_paper_mode():
        await _refresh_paper_quotes()
        await _get_paper_account().reset_risk()
        return await risk()
    state = runtime_state()
    value = await risk()
    value["killSwitch"] = False
    value["reason"] = None
    state["risk"] = value
    write_state("paper-state.json", state)
    return value


@app.get("/api/v1/logs")
async def logs() -> list[dict[str, Any]]:
    return read_state("runtime-log.json", [])


@app.post("/api/v1/logs")
async def append_log(log: dict[str, Any]) -> dict[str, Any]:
    values = await logs()
    values.append(log)
    write_state("runtime-log.json", values[-1000:])
    return log


@app.get("/api/v1/paper/orders")
async def paper_orders() -> list[dict[str, Any]]:
    if local_paper_mode():
        return _get_paper_account().all_orders()
    state = runtime_state()
    orders = state.get("orders")
    return orders if isinstance(orders, list) else read_state("paper-orders.json", [])


@app.post("/api/v1/paper/orders")
async def create_paper_order(order: dict[str, Any]) -> dict[str, Any]:
    if local_paper_mode():
        instrument_id = str(order.get("instrumentID") or "")
        spec = await _instrument_spec(instrument_id)
        ticker = await okx_get("/market/ticker", {"instId": instrument_id})
        quote = (ticker.get("data") or [{}])[0]
        price = as_float(quote.get("last"))
        try:
            result = await _get_paper_account().submit_intent(order, instrument=spec, price=price, quote=quote)
        except OrderGatewayError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        return next(row for row in _get_paper_account().all_orders() if row["id"] == result["orderID"])
    if not private_ready() or not OKX_DEMO:
        raise HTTPException(status_code=503, detail="paper orders require OKX_DEMO=1 and private REST credentials")
    current_risk = await risk()
    if (current_risk.get("killSwitch") or as_float(current_risk.get("dailyPnLPercent")) <= -ACCOUNT_DAILY_LOSS_PERCENT) and not bool(order.get("reduceOnly")):
        raise HTTPException(status_code=409, detail="risk kill switch is active")
    instrument_id = str(order.get("instrumentID") or "")
    spec = await _instrument_spec(instrument_id)
    price = await _ticker_last(instrument_id)
    try:
        result = await _get_order_gateway().submit_intent({**order, "orderType": "market"}, demo=True, instrument=spec, price=price)
    except OrderGatewayError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error
    values = await paper_orders()
    record = dict(order)
    record.update({"id": str(uuid4()), "strategyID": str(order.get("strategyID", uuid4())), "requestedAt": result["submittedAt"], "fillPrice": None, "status": "submitted", "remoteOrderID": result["orderID"], "clientOrderID": result["clientOrderID"], "signal": None})
    values.append(record)
    state = runtime_state()
    state["orders"] = values
    write_state("paper-state.json", state)
    return record


@app.get("/api/v1/paper/fills")
async def paper_fills() -> list[dict[str, Any]]:
    if local_paper_mode():
        return _get_paper_account().all_fills()
    state = runtime_state()
    fills = state.get("fills")
    return fills if isinstance(fills, list) else read_state("paper-fills.json", [])


@app.get("/api/v1/positions")
async def positions() -> list[dict[str, Any]]:
    if local_paper_mode():
        await _refresh_paper_quotes()
        return _get_paper_account().positions()
    if not private_ready():
        raise HTTPException(status_code=503, detail="OKX private REST credentials are not configured")
    payload = await okx_private_request("GET", "/account/positions", params={"instType": "SWAP"})
    protections = await _pending_position_protections()
    return [item for item in (position_snapshot(row, protection=protections.get(_position_protection_key(row))) for row in payload.get("data", [])) if item]


@app.get("/api/v1/orders")
async def orders() -> list[dict[str, Any]]:
    if local_paper_mode():
        await _refresh_paper_quotes()
        return _get_paper_account().pending_orders()
    if not private_ready():
        raise HTTPException(status_code=503, detail="OKX private REST credentials are not configured")
    payload = await okx_private_request("GET", "/trade/orders-pending", params={"instType": "SWAP"})
    try:
        instruments = await _account_swap_instruments()
    except Exception:
        instruments = {}
    return [item for item in (order_snapshot(row, instrument=instruments.get(row.get("instId"))) for row in payload.get("data", [])) if item]


@app.websocket("/api/v1/stream")
async def stream(websocket: WebSocket) -> None:
    if not authorized(websocket.headers, websocket.headers.get("host"), websocket.headers.get("origin")):
        await websocket.close(code=1008)
        return
    await websocket.accept()
    await websocket.send_json({"type": "connection", "timestamp": now_iso(), "payload": "connected"})
    upstream = None
    try:
        subscription = json.loads(await websocket.receive_text())
        instrument = subscription.get("instrumentID", "BTC-USDT-SWAP")
        interval = subscription.get("interval", "1m")
        channels = subscription.get("channels", [])
        await websocket.send_json({"type": "connection", "timestamp": now_iso(), "instrumentID": instrument, "payload": "subscribed:" + ",".join(channels)})
        if "candle" not in channels:
            while True:
                await websocket.receive_text()
        await websocket.send_json({"type": "connection", "timestamp": now_iso(), "instrumentID": instrument, "payload": "okx_wss_connecting"})
        # OKX WSS is a direct market-data connection. Recent websockets
        # versions auto-discover HTTP(S)_PROXY, whose local proxy can reject
        # the TLS upgrade; keep this upstream independent of that setting.
        upstream = await websockets.connect(OKX_WS, proxy=None, open_timeout=10)
        # Keep the live stream on the same exchange bar as the REST history.
        # The client keeps `1D` as its persisted display value, while OKX has
        # separate UTC+8 (`1D`) and UTC (`1Dutc`) daily series.
        exchange_interval = "1Dutc" if interval == "1D" else interval
        await upstream.send(json.dumps({"op": "subscribe", "args": [{"channel": "candle" + exchange_interval, "instId": instrument}]}))
        await websocket.send_json({"type": "connection", "timestamp": now_iso(), "instrumentID": instrument, "payload": "okx_wss_subscribed"})
        async for raw in upstream:
            message = json.loads(raw)
            for row in message.get("data", []):
                if len(row) < 9:
                    continue
                candle = {"id": int(int(row[0]) / 1000), "timestamp": datetime.fromtimestamp(int(row[0]) / 1000, timezone.utc).isoformat().replace("+00:00", "Z"),
                          "open": as_float(row[1]), "high": as_float(row[2]), "low": as_float(row[3]), "close": as_float(row[4]),
                          "volume": as_float(row[5]), "quoteVolume": as_float(row[7]), "confirmed": str(row[8]) == "1"}
                await websocket.send_json({"type": "candle", "timestamp": now_iso(), "instrumentID": instrument, "payload": json.dumps(candle, separators=(",", ":"))})
    except (WebSocketDisconnect, websockets.WebSocketException, asyncio.CancelledError, OSError):
        pass
    finally:
        if upstream:
            with contextlib.suppress(Exception):
                await upstream.close()
        with contextlib.suppress(Exception):
            await websocket.close()


if __name__ == "__main__":
    token()
    uvicorn.run(app, host=HOST, port=PORT)
