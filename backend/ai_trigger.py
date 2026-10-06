"""Stable event fingerprints for event-driven AI polling.

The collector may refresh every cycle, but a model request is only useful when
decision-relevant state changed. Volatile transport timestamps are excluded;
managed positions, pending orders, unknown data and configuration changes are
always treated as triggers by the worker.
"""

from __future__ import annotations

from hashlib import sha256
import json
import math
import os
from typing import Any, Mapping

try:
    from .ai_market_facts import market_facts
    from .ai_schema import AIConfig, AISnapshot
except ImportError:  # bundled backend modules are launched as scripts
    from ai_market_facts import market_facts
    from ai_schema import AIConfig, AISnapshot


FINGERPRINT_VERSION = 1


def _confirmed_structure_hash(rows: Any) -> str:
    canonical: list[dict[str, Any]] = []
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, Mapping) or row.get("confirmed") is not True:
            continue
        canonical.append({
            "timestamp": row.get("timestamp"),
            "open": _number(row.get("open")),
            "high": _number(row.get("high")),
            "low": _number(row.get("low")),
            "close": _number(row.get("close")),
            "volume": _number(row.get("volume")),
        })
    encoded = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _number(value: Any, digits: int = 8) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(float(value)):
        return None
    rounded = round(float(value), digits)
    return int(rounded) if rounded == int(rounded) else rounded


def _bucket(value: Any, scale: float) -> int | None:
    number = _number(value)
    if number is None or scale <= 0 or not math.isfinite(scale):
        return None
    return int(round(float(number) / scale))


def _price_bucket(value: Any, atr: Any) -> int | None:
    price = _number(value)
    if price is None:
        return None
    volatility = _number(atr)
    scale = abs(float(volatility)) * 0.25 if volatility and volatility > 0 else abs(float(price)) * 0.001
    return _bucket(price, max(scale, 1e-12))


def _stable(value: Any) -> Any:
    """Remove volatile timestamps while keeping nested decision state."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key in sorted(value):
            name = str(key)
            lowered = name.lower()
            if lowered in {"updatedat", "receivedat", "sourcetimestampunixmilliseconds"}:
                continue
            result[name] = _stable(value[key])
        return result
    if isinstance(value, list):
        return [_stable(item) for item in value]
    if isinstance(value, tuple):
        return [_stable(item) for item in value]
    if isinstance(value, float):
        return _number(value)
    return value


def _facts_view(snapshot: AISnapshot) -> dict[str, Any]:
    facts = market_facts(snapshot).get("instruments", {})
    result: dict[str, Any] = {}
    for instrument in snapshot.observed_instruments():
        row = facts.get(instrument, {}) if isinstance(facts, Mapping) else {}
        timeframes = row.get("timeframes", {}) if isinstance(row, Mapping) else {}
        five_minute = timeframes.get("5m", {}) if isinstance(timeframes, Mapping) else {}
        atr = five_minute.get("meanTrueRange14") if isinstance(five_minute, Mapping) else None
        ticker = row.get("ticker", {}) if isinstance(row, Mapping) else {}
        book = row.get("orderBook", {}) if isinstance(row, Mapping) else {}
        ticker_view = {
            "lastBucket": _price_bucket(ticker.get("lastPrice"), atr),
            "spreadBucket": _bucket(ticker.get("spreadBasisPoints"), 0.5),
        }
        book_view = {
            "imbalanceBucket": _bucket(book.get("top5SizeImbalance"), 0.1),
            "spreadBucket": _bucket(book.get("spreadBasisPoints"), 0.5),
            "invalidBidLevelCount": book.get("invalidBidLevelCount"),
            "invalidAskLevelCount": book.get("invalidAskLevelCount"),
        }
        timeframe_view: dict[str, Any] = {}
        for interval, values in timeframes.items() if isinstance(timeframes, Mapping) else ():
            if not isinstance(values, Mapping):
                timeframe_view[str(interval)] = None
                continue
            timeframe_view[str(interval)] = _stable({
                key: values.get(key)
                for key in (
                    "receivedRowCount", "confirmedRowCount", "formingRowCount", "validConfirmedRowCount", "invalidConfirmedOHLCCount",
                    "usableTrailingConfirmedRowCount", "lastConfirmedAt", "lastConfirmedClose",
                    "return1BarPercent", "return3BarPercent", "return12BarPercent",
                    "sma20", "sma50", "recent20High", "recent20Low", "meanTrueRange14",
                    "volumeRatioToPrevious20",
                )
            })
            timeframe_view[str(interval)]["confirmedStructureHash"] = _confirmed_structure_hash(
                snapshot.candles.get(f"{instrument}/{interval}", [])
            )
            raw_rows = snapshot.candles.get(f"{instrument}/{interval}", [])
            forming = next(
                (row for row in reversed(raw_rows) if isinstance(row, Mapping) and row.get("confirmed") is False),
                None,
            )
            if isinstance(forming, Mapping):
                timeframe_view[str(interval)]["forming"] = {
                    "timestamp": forming.get("timestamp"),
                    "openBucket": _price_bucket(forming.get("open"), atr),
                    "highBucket": _price_bucket(forming.get("high"), atr),
                    "lowBucket": _price_bucket(forming.get("low"), atr),
                    "closeBucket": _price_bucket(forming.get("close"), atr),
                }
            else:
                timeframe_view[str(interval)]["forming"] = None
        funding = row.get("fundingRate", {}) if isinstance(row, Mapping) else {}
        result[instrument] = {
            "ticker": ticker_view,
            "orderBook": book_view,
            "fundingRate": {
                "rate": _number(funding.get("rate")) if isinstance(funding, Mapping) else None,
                "nextFundingBucket": _bucket(
                    funding.get("nextFundingTimeUnixMilliseconds"), 600_000
                ) if isinstance(funding, Mapping) else None,
            },
            "timeframes": timeframe_view,
        }
    return result


def _account_view(account: Mapping[str, Any]) -> dict[str, Any]:
    positions = account.get("positions")
    position_view = []
    for row in positions if isinstance(positions, list) else []:
        if not isinstance(row, Mapping):
            continue
        position_view.append(_stable({
            key: row.get(key)
            for key in (
                "id", "instrumentID", "side", "quantity", "entryPrice", "leverage",
                "marginMode", "takeProfitPrice", "stopLossPrice",
            )
        }))
    pending = account.get("pendingOrders")
    pending_view = []
    for row in pending if isinstance(pending, list) else []:
        if not isinstance(row, Mapping):
            continue
        pending_view.append(_stable({
            key: row.get(key)
            for key in (
                "id", "instrumentID", "side", "status", "quantity", "price",
                "filledQuantity", "averageFillPrice", "leverage", "marginMode",
                "takeProfitPrice", "stopLossPrice",
            )
        }))
    return {
        "authenticated": account.get("authenticated"),
        "equityUSD": account.get("equityUSD"),
        "availableEquityUSD": account.get("availableEquityUSD"),
        "totalAssetValueUSD": account.get("totalAssetValueUSD"),
        "todayPnLUSD": account.get("todayPnLUSD"),
        "todayLossCount": account.get("todayLossCount"),
        # The daily order counter is an entry gate.  Leaving it out means a
        # static market snapshot can be prescreened forever after the quota is
        # reached, even though the admissible action has changed.
        "todayAIOrderCount": account.get("todayAIOrderCount"),
        "pendingOrdersKnown": account.get("pendingOrdersKnown"),
        "positions": sorted(position_view, key=lambda item: (str(item.get("instrumentID")), str(item.get("id")))),
        "pendingOrders": sorted(pending_view, key=lambda item: (str(item.get("instrumentID")), str(item.get("id")))),
    }


def managed_state(snapshot: AISnapshot) -> bool:
    account = snapshot.account if isinstance(snapshot.account, Mapping) else {}
    positions = account.get("positions")
    pending = account.get("pendingOrders")
    return bool(
        isinstance(positions, list) and any(isinstance(row, Mapping) for row in positions)
        or isinstance(pending, list) and any(isinstance(row, Mapping) for row in pending)
    )


def data_quality_requires_evaluation(snapshot: AISnapshot) -> bool:
    freshness = snapshot.dataFreshness if isinstance(snapshot.dataFreshness, Mapping) else {}
    if freshness.get("errors"):
        return True
    account = snapshot.account if isinstance(snapshot.account, Mapping) else {}
    if account.get("authenticated") is not True:
        return True
    if account.get("todayLossCount") is None:
        return True
    if account.get("pendingOrdersKnown") is not True or not isinstance(account.get("pendingOrders"), list):
        return True
    risk = snapshot.risk if isinstance(snapshot.risk, Mapping) else {}
    quality = risk.get("dataQuality") if isinstance(risk.get("dataQuality"), Mapping) else {}
    for key, value in quality.items():
        name = str(key).lower()
        if name.endswith("error") and value not in (None, False, ""):
            return True
        if name.endswith("retryable") and value is True:
            return True
        if name.endswith("available") and value is False:
            return True
    return False


def decision_fingerprint(snapshot: AISnapshot, config: AIConfig) -> str:
    ai = snapshot.ai if isinstance(snapshot.ai, Mapping) else {}
    facts_payload = market_facts(snapshot)
    methodology = facts_payload.get("methodology") if isinstance(facts_payload, Mapping) else None
    structure_hash = sha256(json.dumps(methodology, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    route = {
        "model": os.getenv("NOVATRADE_DEEPSEEK_MODEL", "deepseek-v4-pro")
        if config.provider == "deepseek-harness" else config.routineModel,
        "reasoningEffort": os.getenv("NOVATRADE_DEEPSEEK_REASONING_EFFORT", "low")
        if config.provider == "deepseek-harness" else config.routineReasoningEffort,
    }
    if config.provider == "deepseek-harness":
        # DeepSeek-only prompt controls must not perturb Codex/GPT event
        # semantics or trigger extra model calls in the other worker.
        route.update({
            "profile": os.getenv("NOVATRADE_DEEPSEEK_PROFILE", "optimized").strip().lower(),
            "encoding": os.getenv("NOVATRADE_DEEPSEEK_ENCODING", "compact60").strip().lower(),
        })
    payload = {
        "version": FINGERPRINT_VERSION,
        "observedInstruments": snapshot.observed_instruments(),
        "facts": _facts_view(snapshot),
        "account": _account_view(snapshot.account if isinstance(snapshot.account, Mapping) else {}),
        "risk": _stable(snapshot.risk),
        "availability": _stable(ai.get("tradingAvailability")),
        "config": {
            "provider": config.provider,
            "enabled": config.enabled,
            "mode": config.mode,
            "allowedInstruments": list(config.allowedInstruments),
            "minimumConfidence": config.minimumConfidence,
            "allowOpen": config.allowOpen,
            "allowClose": config.allowClose,
            "allowCancel": config.allowCancel,
            "requireStopLoss": config.requireStopLoss,
            "maxDailyOrders": config.maxDailyOrders,
            "maxDailyLosses": config.maxDailyLosses,
            "marginPerOrderUSD": config.marginPerOrderUSD,
            "maxLeverage": config.maxLeverage,
            "cooldownSeconds": config.cooldownSeconds,
        },
        "route": route,
        "structure": {
            "version": ai.get("structureVersion") or "market-facts-v1",
            "rulesHash": ai.get("structureRulesHash") or structure_hash,
        },
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def structure_identity(snapshot: AISnapshot) -> dict[str, str]:
    """Identify the deterministic market-facts rules used by the prescreen."""
    ai = snapshot.ai if isinstance(snapshot.ai, Mapping) else {}
    methodology = market_facts(snapshot).get("methodology")
    rules_hash = sha256(json.dumps(methodology, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {
        "version": str(ai.get("structureVersion") or "market-facts-v1"),
        "rulesHash": str(ai.get("structureRulesHash") or rules_hash),
    }


def trigger_reason(snapshot: AISnapshot, previous_fingerprint: str | None, current_fingerprint: str) -> str:
    if previous_fingerprint is None:
        return "initial"
    if managed_state(snapshot):
        return "managed-state"
    if data_quality_requires_evaluation(snapshot):
        return "data-quality"
    if previous_fingerprint != current_fingerprint:
        return "fingerprint-changed"
    return "unchanged"


__all__ = [
    "FINGERPRINT_VERSION",
    "data_quality_requires_evaluation",
    "decision_fingerprint",
    "managed_state",
    "structure_identity",
    "trigger_reason",
]
