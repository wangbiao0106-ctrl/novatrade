"""Direct FastAPI backend for NovaTrade.

The local API talks to OKX public REST/WebSocket endpoints directly. Runtime
state is kept in the application-support directory so the SwiftUI client can
continue using its existing JSON contract without a separate daemon.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
from datetime import datetime, timedelta, timezone
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
    from .ai_policy import parse_time, snapshot_freshness
    from .ai_schema import AIDecision, AIChatRequest, AIConfig, AISnapshot, SchemaError, normalize_contract_ids
    from .ai_worker import AIWorker, CodexError
    from .order_gateway import InstrumentSpec, OrderGateway, OrderGatewayError, OrderNotSubmittedError
except ImportError:  # bundled backend/main.py is launched as a script
    from ai_policy import parse_time, snapshot_freshness
    from ai_schema import AIDecision, AIChatRequest, AIConfig, AISnapshot, SchemaError, normalize_contract_ids
    from ai_worker import AIWorker, CodexError
    from order_gateway import InstrumentSpec, OrderGateway, OrderGatewayError, OrderNotSubmittedError


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


# Keep one connection pool for all public/private REST requests. Creating a
# fresh AsyncClient for every request forces a new TLS handshake and made
# switching the chart interval noticeably slower than the OKX client.
OKX_HTTP_CLIENT = httpx.AsyncClient(
    timeout=httpx.Timeout(20.0),
    limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
)


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
order_gateway: OrderGateway | None = None


@app.on_event("shutdown")
async def close_okx_http_client() -> None:
    if ai_worker is not None:
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


async def okx_private_request(method: str, path: str, *, params: dict[str, str] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
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
    return {"status": "ok", "version": "fastapi-1", "mode": "paper", "updatedAt": now_iso()}


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
    # The current-candles endpoint is served from OKX's low-latency market
    # feed and returns the same wire rows as history-candles for the latest
    # window. Use UTC-aligned daily bars while preserving the client-facing
    # `1D` interval in the response.
    client_bar = "1D" if bar == "1Dutc" else bar
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
    if not private_ready():
        return {"mode": "readOnly", "profile": None, "site": None, "label": None, "authenticated": False,
                "equityUSD": None, "availableEquityUSD": None, "totalAssetValueUSD": None, "todayPnLUSD": None,
                "todayLossCount": None, "assets": [], "positions": [], "updatedAt": now_iso()}
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
    positions = [position_snapshot(row) for row in position_payload.get("data", [])]
    today_pnl = None
    today_loss_count = None
    daily_bills_error = None
    daily_bills_retryable = False
    try:
        bills = await okx_private_request("GET", "/account/bills", params={"instType": "SWAP", "limit": "100"})
        today = datetime.now(timezone.utc).date()
        rows = [row for row in bills.get("data", []) if isinstance(row, dict)]
        today_rows = [row for row in rows if _bill_is_today(row, today)]
        today_pnl = sum(as_float(row.get("pnl")) for row in today_rows)
        today_loss_count = _daily_loss_count(rows, today)
    except (HTTPException, httpx.HTTPError) as error:
        daily_bills_error = _ai_collection_error(error)
        daily_bills_retryable = _AIDataCollector._retryable(error)
    return {"mode": "paper" if OKX_DEMO else "live", "profile": OKX_PROFILE, "site": OKX_SITE,
            "label": config_row.get("label"), "authenticated": True, "equityUSD": total_equity,
            "availableEquityUSD": available_equity, "totalAssetValueUSD": total_equity, "todayPnLUSD": today_pnl,
            "todayLossCount": today_loss_count,
            "dataQuality": {"dailyBillsAvailable": today_loss_count is not None, "dailyBillsError": daily_bills_error, "dailyBillsRetryable": daily_bills_retryable},
            "assets": assets, "positions": [item for item in positions if item], "updatedAt": now_iso()}


def position_snapshot(row: dict[str, Any]) -> dict[str, Any] | None:
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
    return {"id": identifier, "instrumentID": row["instId"], "side": side, "quantity": quantity,
            "entryPrice": entry, "markPrice": mark_price if mark_price > 0 else None,
            "unrealizedPnL": unrealized,
            "marginMode": margin if margin in {"cross", "isolated"} else None}


def order_snapshot(row: dict[str, Any]) -> dict[str, Any] | None:
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
    return {"id": identifier, "instrumentID": row["instId"], "side": side,
            "status": status, "quantity": quantity, "price": price,
            "createdAt": timestamp, "filledQuantity": filled if filled is not None and filled >= 0 else None}


def _assert_ai_entry_current(request: dict[str, Any]) -> None:
    """Check the server-owned deadline immediately before exchange writes."""
    if request.get("source") != "ai" or request.get("reduceOnly"):
        return
    deadline = request.get("_aiEntryDeadline")
    if isinstance(deadline, bool) or not isinstance(deadline, (int, float)) or not math.isfinite(deadline):
        raise OrderNotSubmittedError("AI entry deadline is unavailable or invalid")
    if datetime.now(timezone.utc).timestamp() >= deadline:
        raise OrderNotSubmittedError("AI entry expired before order submission")


async def submit_order(request: dict[str, Any], *, demo: bool) -> dict[str, Any]:
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
        leverage_body = {"instId": instrument, "lever": request["leverage"], "mgnMode": margin_mode}
        if request.get("positionSide"):
            leverage_body["posSide"] = request["positionSide"]
        try:
            await okx_private_request("POST", "/account/set-leverage", body=leverage_body)
        except Exception as error:
            # No order POST has been sent. Even a leverage timeout cannot
            # create a position, so the gateway must release this reservation.
            raise OrderNotSubmittedError(f"设置杠杆失败，订单未提交：{error}") from error
    tp = request.get("takeProfitTriggerPrice")
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
    result = await okx_private_request("POST", "/trade/order", body=body)
    row = (result.get("data") or [{}])[0]
    order_id = row.get("ordId")
    if not order_id:
        raise HTTPException(status_code=502, detail="OKX response did not include ordId")
    return {"orderID": order_id, "clientOrderID": row.get("clOrdId", client_order_id),
            "instrumentID": instrument, "side": side, "orderType": order_type, "quantity": quantity,
            "status": "submitted", "message": row.get("sMsg"), "submittedAt": now_iso()}


async def _account_swap_instruments() -> dict[str, dict[str, Any]]:
    """Use the authenticated account's market, including its demo universe.

    Public instruments describe production markets; a demo account can trade
    only a subset. Never use that public list to authorize an order.
    """
    payload = await okx_private_request("GET", "/account/instruments", params={"instType": "SWAP"})
    rows = payload.get("data")
    if not isinstance(rows, list):
        raise HTTPException(status_code=502, detail="OKX account instrument list is invalid")
    return {
        str(row["instId"]): row for row in rows
        if isinstance(row, dict) and row.get("instId") and row.get("state") == "live"
        and row.get("settleCcy") == "USDT" and row.get("ctType") == "linear"
    }


async def _ai_trading_availability(instruments: list[str]) -> dict[str, dict[str, Any]]:
    """Keep all observed contracts, separately report account execution limits."""
    mode = "模拟盘" if OKX_DEMO else "实盘账户"
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
        mode = "模拟盘" if OKX_DEMO else "实盘账户"
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
        # OKX 51603 means the client order id is definitely absent. Other
        # failures must remain unresolved and keep the reservation.
        if getattr(error, "exchange_code", "") == "51603":
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


def _get_order_gateway() -> OrderGateway:
    global order_gateway
    if order_gateway is None:
        order_gateway = OrderGateway(state_dir() / "order-ledger.json", submit=_gateway_submit, lookup=_lookup_order)
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


async def _ai_snapshot() -> AISnapshot:
    """Build a credential-free snapshot for one Codex invocation."""
    collection_started = now_iso()
    collection_clock = asyncio.get_running_loop().time()
    config = ai_worker.config if ai_worker is not None else AIConfig()
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

        intervals = ("5m", "15m", "1H", "4H")
        candle_rows, ticker, funding_row, book = await asyncio.gather(
            asyncio.gather(*(get_candles(interval) for interval in intervals)),
            get_public_row("/market/ticker", "ticker"),
            get_public_row("/public/funding-rate", "fundingRate"),
            get_public_row("/market/books", "orderBook", sz="5"),
        )
        availability["instruments"][instrument] = instrument_quality
        return instrument, dict(zip(intervals, candle_rows)), ticker, funding_row, book

    collected = await asyncio.gather(*(collect_instrument(instrument) for instrument in selected))
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
    risk_snapshot = dict(risk_snapshot or {})
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
    account_snapshot["todayAIOrderCount"] = _get_order_gateway().daily_order_count()
    collection_completed = now_iso()
    # Include collection latency in freshness: stamping only the end makes
    # earlier ticker/book data appear new after a slow request or retry.
    captured = collection_started
    raw = {"capturedAt": captured, "instruments": [available[item] for item in candidates], "candles": candles, "tickers": tickers, "orderBook": books, "fundingRates": funding, "account": account_snapshot, "risk": risk_snapshot}
    snapshot_id = __import__("hashlib").sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:32]
    return AISnapshot(
        snapshotId=snapshot_id,
        capturedAt=captured,
        instruments=raw["instruments"],
        candles=candles,
        tickers=tickers,
        orderBook=books,
        fundingRates=funding,
        account=account_snapshot,
        risk=risk_snapshot,
        ai={
            "mode": config.mode,
            "enabled": config.enabled,
            "maxDailyOrders": config.maxDailyOrders,
            "maxDailyLosses": config.maxDailyLosses,
            "marginPerOrderUSD": config.marginPerOrderUSD,
            "maxLeverage": config.maxLeverage,
            "todayAIOrderCount": account_snapshot.get("todayAIOrderCount", 0),
            "todayLossCount": account_snapshot.get("todayLossCount"),
            "observationCount": len(selected),
            "selectedInstruments": list(selected),
            "tradingMode": "demo" if OKX_DEMO else "live",
            "tradingAvailability": trading_availability,
        },
        dataFreshness={
            "capturedAt": captured, "maxAgeSeconds": config.decisionIntervalSeconds,
            "collectionStartedAt": collection_started, "collectionCompletedAt": collection_completed,
            "collectionDurationSeconds": round(asyncio.get_running_loop().time() - collection_clock, 3),
            "availability": availability, "errors": collector.errors,
        },
    )


async def _ai_cancel(decision: AIDecision, *, demo: bool) -> dict[str, Any]:
    if not decision.instrumentID:
        raise HTTPException(status_code=422, detail="cancel requires instrumentID")
    if not decision.orderID:
        raise HTTPException(status_code=422, detail="cancel requires orderID")
    await okx_private_request("POST", "/trade/cancel-order", body={"instId": decision.instrumentID, "ordId": decision.orderID})
    return {"action": "cancel", "orderID": decision.orderID, "instrumentID": decision.instrumentID, "status": "cancelled", "submittedAt": now_iso()}


async def _ai_execute(decision: AIDecision, snapshot: AISnapshot) -> dict[str, Any]:
    """Apply current account/risk state immediately before submitting an AI intent."""
    if decision.action == "cancel":
        return await _ai_cancel(decision, demo=OKX_DEMO)
    if not decision.instrumentID or decision.direction not in {"long", "short"}:
        raise HTTPException(status_code=422, detail="AI action requires an instrument and direction")
    if decision.action == "open" and (snapshot.risk.get("killSwitch") or as_float(snapshot.risk.get("dailyPnLPercent")) <= -5):
        raise HTTPException(status_code=409, detail="risk kill switch is active")
    config = ai_worker.config if ai_worker is not None else AIConfig()
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
    if not demo and not live_enabled:
        raise HTTPException(status_code=409, detail="live trading is disabled")
    # Recheck the account's own instrument universe before reserving margin
    # or setting leverage. The public price feed alone cannot authorize it.
    spec = await _instrument_spec(decision.instrumentID)
    last = await _ticker_last(decision.instrumentID)
    equity = as_float(snapshot.account.get("availableEquityUSD"), 0)
    if equity <= 0:
        raise HTTPException(status_code=409, detail="available account equity is unavailable")
    reduce_only = decision.action == "close"
    leverage = as_float(decision.leverage, 0) if not reduce_only else 1
    if not reduce_only:
        if leverage < 1 or leverage > config.maxLeverage:
            raise HTTPException(status_code=422, detail="AI leverage exceeds configured maximum")
        target_notional = config.marginPerOrderUSD * leverage
    else:
        # Prefer the exact current position quantity so an AI close does not
        # depend on an entry-only risk budget. Fall back to the configured
        # margin size if a position is not present in the snapshot.
        position_quantity = 0.0
        for position in snapshot.account.get("positions", []) if isinstance(snapshot.account.get("positions"), list) else []:
            if isinstance(position, dict) and position.get("instrumentID") == decision.instrumentID:
                position_quantity = abs(as_float(position.get("quantity")))
                break
        risk_percent = as_float(decision.riskBudgetPercent)
        target_notional = equity * min(risk_percent, 100) / 100 if risk_percent > 0 else config.marginPerOrderUSD
    side = "buy" if decision.direction == "long" else "sell"
    if reduce_only:
        side = "sell" if decision.direction == "long" else "buy"
    request = {
        "instrumentID": decision.instrumentID, "side": side, "orderType": decision.orderType or "market",
        "targetNotional": target_notional, "price": decision.limitPrice,
        "takeProfitTriggerPrice": decision.takeProfitPrice, "stopLossTriggerPrice": decision.stopLossPrice,
        "marginMode": "cross", "reduceOnly": reduce_only, "source": "ai",
        "clientOrderID": ("ai" + decision.decisionId.replace("-", ""))[:32],
    }
    if not reduce_only:
        request["leverage"] = leverage
        request["marginUSD"] = config.marginPerOrderUSD
        request["_aiEntryDeadline"] = entry_deadline
    elif position_quantity > 0:
        request["quantity"] = position_quantity
    try:
        return await _get_order_gateway().submit_intent(
            request, demo=demo, instrument=spec, price=last,
            available_equity=equity, daily_order_limit=config.maxDailyOrders,
        )
    except OrderGatewayError as error:
        raise HTTPException(status_code=422, detail=str(error)) from error


async def _ensure_ai_worker() -> AIWorker:
    global ai_worker
    if ai_worker is None:
        ai_worker = AIWorker(snapshot_provider=_ai_snapshot, order_gateway=_ai_execute, state_dir=state_dir())
    if ai_worker.config.enabled and ai_worker.config.mode not in {"disabled", "halted"}:
        await ai_worker.start()
    return ai_worker


@app.on_event("startup")
async def start_ai_worker() -> None:
    await _ensure_ai_worker()
    if private_ready():
        with contextlib.suppress(Exception):
            await _get_order_gateway().reconcile(demo=OKX_DEMO)


@app.get("/api/v1/live/trading-status")
async def live_trading_status() -> dict[str, Any]:
    enabled = read_state("live-trading.json", {}).get("enabled", False)
    mode = "paper" if OKX_DEMO else "live" if private_ready() else "readOnly"
    return {"mode": mode, "profile": OKX_PROFILE if private_ready() else None, "enabled": enabled,
            "available": private_ready() and not OKX_DEMO,
            "message": "实盘账户已连接" if private_ready() and not OKX_DEMO else "未配置可下单的实盘凭据", "updatedAt": now_iso()}


@app.post("/api/v1/live/trading/enable")
async def enable_live_trading() -> dict[str, Any]:
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
    if not read_state("live-trading.json", {}).get("enabled", False):
        raise HTTPException(status_code=409, detail="live trading is disabled")
    if OKX_DEMO:
        raise HTTPException(status_code=409, detail="the configured OKX account is demo mode")
    current_risk = await risk()
    if (current_risk.get("killSwitch") or as_float(current_risk.get("dailyPnLPercent")) <= -5) and not bool(request.get("reduceOnly")):
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


@app.patch("/api/v1/ai/config")
async def update_ai_config(config: dict[str, Any]) -> dict[str, Any]:
    worker = await _ensure_ai_worker()
    if config.get("mode") == "live-armed" and (OKX_DEMO or not read_state("live-trading.json", {}).get("enabled", False)):
        raise HTTPException(status_code=409, detail="live AI mode requires the separate live trading switch")
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

@app.post("/api/v1/ai/enable")
async def enable_ai() -> dict[str, Any]:
    worker = await _ensure_ai_worker()
    if worker.config.mode == "disabled":
        if not OKX_DEMO and not read_state("live-trading.json", {}).get("enabled", False):
            raise HTTPException(status_code=409, detail="enable live trading before arming AI for live orders")
        mode = "demo-active" if OKX_DEMO else "live-armed"
        worker.update_config({"enabled": True, "mode": mode})
    elif worker.config.mode == "halted":
        raise HTTPException(status_code=409, detail="AI worker is halted; update config after reviewing the error")
    else:
        worker.update_config({"enabled": True})
    await worker.start()
    return worker.get_status()


@app.post("/api/v1/ai/disable")
async def disable_ai() -> dict[str, Any]:
    worker = await _ensure_ai_worker()
    await worker.disable()
    return worker.get_status()


@app.post("/api/v1/ai/flatten")
async def flatten_ai() -> dict[str, Any]:
    """Cancel pending orders and close current positions using reduce-only orders."""
    worker = await _ensure_ai_worker()
    await worker.disable()
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
        quantity = as_float(row.get("pos"))
        instrument_id = str(row.get("instId") or "")
        if quantity <= 0 or not instrument_id:
            continue
        spec = await _instrument_spec(instrument_id)
        mark = as_float(row.get("markPx"), as_float(row.get("avgPx")))
        side = "sell" if str(row.get("posSide") or "net").lower() == "long" else "buy"
        if str(row.get("posSide") or "net").lower() == "net":
            side = "sell" if str(row.get("side") or "long").lower() == "long" else "buy"
        try:
            result = await _get_order_gateway().submit_intent({"instrumentID": instrument_id, "side": side, "orderType": "market", "quantity": quantity, "marginMode": str(row.get("mgnMode") or "cross"), "reduceOnly": True}, demo=OKX_DEMO, instrument=spec, price=mark, force_reduce_only=True)
            closed.append(result)
        except (OrderGatewayError, HTTPException) as error:
            await _get_order_gateway().record_audit({"type": "flatten-error", "instrumentID": instrument_id, "error": str(error)})
    return {"cancelledOrderIDs": cancelled, "closed": closed, "updatedAt": now_iso()}


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
    value = {"equity": equity, "strategyCapitalBase": base if base > 0 else None,
             "equityPeak": as_float(stored.get("equityPeak"), equity),
             "dayStartEquity": as_float(stored.get("dayStartEquity"), equity), "dayStartAt": None,
             "dailyPnLPercent": as_float(stored.get("dailyPnLPercent")),
             "drawdownPercent": as_float(stored.get("drawdownPercent")),
             "killSwitch": bool(stored.get("killSwitch", False)), "reason": stored.get("reason"),
             "dataQuality": {"equitySource": equity_source, "accountRefreshError": refresh_error, "accountRefreshRetryable": refresh_retryable},
             "strategyCapitals": [strategy_capital_snapshot(state, config, base) for config in strategy_configs()],
             "globalNotionals": stored.get("globalNotionals", {})}
    return value


@app.post("/api/v1/risk/reset")
async def reset_risk() -> dict[str, Any]:
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
    state = runtime_state()
    orders = state.get("orders")
    return orders if isinstance(orders, list) else read_state("paper-orders.json", [])


@app.post("/api/v1/paper/orders")
async def create_paper_order(order: dict[str, Any]) -> dict[str, Any]:
    if not private_ready() or not OKX_DEMO:
        raise HTTPException(status_code=503, detail="paper orders require OKX_DEMO=1 and private REST credentials")
    current_risk = await risk()
    if (current_risk.get("killSwitch") or as_float(current_risk.get("dailyPnLPercent")) <= -5) and not bool(order.get("reduceOnly")):
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
    state = runtime_state()
    fills = state.get("fills")
    return fills if isinstance(fills, list) else read_state("paper-fills.json", [])


@app.get("/api/v1/positions")
async def positions() -> list[dict[str, Any]]:
    if not private_ready():
        raise HTTPException(status_code=503, detail="OKX private REST credentials are not configured")
    payload = await okx_private_request("GET", "/account/positions", params={"instType": "SWAP"})
    return [item for item in (position_snapshot(row) for row in payload.get("data", [])) if item]


@app.get("/api/v1/orders")
async def orders() -> list[dict[str, Any]]:
    if not private_ready():
        raise HTTPException(status_code=503, detail="OKX private REST credentials are not configured")
    payload = await okx_private_request("GET", "/trade/orders-pending", params={"instType": "SWAP"})
    return [item for item in (order_snapshot(row) for row in payload.get("data", [])) if item]


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
        await upstream.send(json.dumps({"op": "subscribe", "args": [{"channel": "candle" + interval, "instId": instrument}]}))
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
