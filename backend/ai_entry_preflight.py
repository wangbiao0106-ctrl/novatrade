"""Final market admission checks for AI entries, before any order is sent."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import math
from typing import Any

try:
    from .ai_policy import MIN_OPEN_RISK_REWARD_RATIO
    from .order_gateway import OrderNotSubmittedError
except ImportError:  # bundled backend runs as scripts
    from ai_policy import MIN_OPEN_RISK_REWARD_RATIO
    from order_gateway import OrderNotSubmittedError


# Mirror strategies/codex_ai_decision/config/strategy.json; never read it at runtime.
ENTRY_PREFLIGHT_LIMITS = {
    "max_quote_age_seconds": 5.0,
    "max_future_skew_seconds": 1.0,
    "max_spread_bps": 10.0,
    "max_price_drift_bps": 50.0,
    "max_stop_distance_fraction": 0.25,
    "fee_rate_per_side": 0.0005,
    "slippage_bps": 2.0,
    "minimum_net_risk_reward_ratio": MIN_OPEN_RISK_REWARD_RATIO,
}


class EntryPreflightRejected(OrderNotSubmittedError):
    """A completed market check declined an entry; no order was submitted."""


def _reject(reason: str) -> None:
    raise EntryPreflightRejected("AI entry preflight: " + reason)


def _positive(value: Any, name: str) -> float:
    try:
        parsed = float(value)
    except (ValueError, TypeError, OverflowError):
        _reject(name + " is unavailable")
    if isinstance(value, bool) or not math.isfinite(parsed) or parsed <= 0:
        _reject(name + " is invalid")
    return parsed


def _levels(value: Any, *, buy: bool) -> list[tuple[float, float]]:
    if not isinstance(value, list) or not value:
        _reject("order book is unavailable")
    result = []
    for row in value[:5]:
        if not isinstance(row, (list, tuple)) or len(row) < 2:
            _reject("order book level is malformed")
        price, size = _positive(row[0], "book price"), _positive(row[1], "book size")
        if result and (price <= result[-1][0] if buy else price >= result[-1][0]):
            _reject("order book levels are not ordered")
        result.append((price, size))
    return result


def check_entry_preflight(
    request: dict[str, Any], ticker: dict[str, Any], book: dict[str, Any], mark: dict[str, Any],
    *, now: datetime | None = None, fee_rate: float | None = None, slippage_bps: float | None = None,
) -> dict[str, Any]:
    """Check rounded prices, current execution depth and conservative net R/R."""
    limits = ENTRY_PREFLIGHT_LIMITS
    clock = (now or datetime.now(timezone.utc)).timestamp()
    instrument = str(request.get("instrumentID") or "")
    for name, row in (("ticker", ticker), ("order book", book), ("mark price", mark)):
        if not isinstance(row, dict) or (name != "order book" and row.get("instId") != instrument):
            _reject(name + " contract is unavailable or mismatched")
        if name == "order book" and row.get("instId") not in (None, instrument):
            _reject("order book contract is mismatched")
        age = clock - _positive(row.get("ts"), name + " timestamp") / 1000
        if age > limits["max_quote_age_seconds"] or age < -limits["max_future_skew_seconds"]:
            _reject(name + " is stale or from the future")
    side = request.get("side")
    if side not in {"buy", "sell"} or request.get("orderType") not in {"market", "limit"}:
        _reject("entry side or order type is invalid")
    buy, direction = side == "buy", 1 if side == "buy" else -1
    bids, asks = _levels(book.get("bids"), buy=False), _levels(book.get("asks"), buy=True)
    bid, ask = bids[0][0], asks[0][0]
    midpoint = bid / 2 + ask / 2
    spread = (ask - bid) / midpoint * 10000
    if spread < 0 or spread > limits["max_spread_bps"]:
        _reject("order book is crossed or spread exceeds 10 bps")
    last, mark_price = _positive(ticker.get("last"), "last price"), _positive(mark.get("markPx"), "mark price")
    reference = _positive(request.get("_aiEntryReferencePrice"), "snapshot reference price")
    tick = request.get("_aiEntryTickSize", 0)
    if isinstance(tick, bool) or not isinstance(tick, (int, float)) or not math.isfinite(tick) or tick < 0:
        _reject("price tick is invalid")
    def aligned(value: Any, name: str) -> float:
        price = _positive(value, name)
        if tick > 0:
            # Gateway and paper requests may already be tick-aligned. Decimal
            # flooring keeps a second check from moving them another tick due
            # to floating-point division (for example, 100.1 / 0.1).
            step = Decimal(str(tick))
            price = float((Decimal(str(price)) / step).to_integral_value(rounding=ROUND_FLOOR) * step)
        return _positive(price, name)
    stop = aligned(request.get("stopLossTriggerPrice"), "stop price")
    raw_targets = request.get("takeProfitLevels")
    if raw_targets is not None:
        if not isinstance(raw_targets, list) or not raw_targets or not all(isinstance(row, dict) for row in raw_targets):
            _reject("take-profit levels are invalid")
        targets = [aligned(row.get("price"), "target price") for row in raw_targets]
        if len(targets) == 1 and request.get("takeProfitTriggerPrice") is not None:
            # Exchange attachments prefer the legacy field for a single
            # target; paper matching prefers the staged field. Both must
            # pass so a farther staged target cannot conceal the actual TP.
            targets.append(aligned(request["takeProfitTriggerPrice"], "legacy target price"))
    else:
        targets = [aligned(request.get("takeProfitTriggerPrice"), "target price")]
    target = min(targets) if buy else max(targets)
    limit = aligned(request.get("price"), "limit price") if request["orderType"] == "limit" else None
    planned_entry = limit if limit is not None else _positive(request.get("_aiEntryPlannedEntryPrice", reference), "planned entry")
    if direction * (planned_entry - stop) <= 0 or any(direction * (value - planned_entry) <= 0 for value in targets):
        _reject("rounded protection prices invalidate the planned entry")
    for value in (last, mark_price):
        if direction * (value - stop) <= 0 or direction * (target - value) <= 0:
            _reject("current price has already reached stop-loss or take-profit")
    max_drift = min(reference * limits["max_price_drift_bps"] / 10000,
                    abs(planned_entry - stop) * limits["max_stop_distance_fraction"])
    if max(abs(value - reference) for value in (last, mark_price, midpoint)) > max_drift + reference * 1e-12:
        _reject("market moved beyond the allowed snapshot price deviation")
    configured_fee, configured_slip = fee_rate if fee_rate is not None else 0, slippage_bps if slippage_bps is not None else 0
    if not math.isfinite(configured_fee) or configured_fee < 0 or not math.isfinite(configured_slip) or not 0 <= configured_slip < 10000:
        _reject("fee or slippage budget is invalid")
    fee = max(limits["fee_rate_per_side"], configured_fee)
    slip = max(limits["slippage_bps"], configured_slip)
    opposite = asks if buy else bids
    immediately_marketable = limit is None or direction * (limit - opposite[0][0]) >= 0
    if immediately_marketable:
        if request.get("quantity") is not None:
            quantity = _positive(request["quantity"], "order quantity")
        else:
            contract_value = _positive(request.get("_aiEntryContractValue"), "contract value")
            # Paper sizes after refreshing the quote. A sell's adverse
            # slippage lowers its sizing price and increases the contract
            # count. Use a lower bound on sizing price so this depth check
            # covers at least the eventual size, including lot/margin caps.
            sizing_price = opposite[0][0] * (1 - slip / 10000) if not buy else opposite[0][0]
            quantity = _positive(
                _positive(request.get("targetNotional"), "target notional") / (contract_value * sizing_price),
                "estimated order quantity",
            )
        remaining, worst = quantity, opposite[0][0]
        for price, size in opposite:
            if limit is not None and direction * (price - limit) > 0:
                break
            remaining -= size
            worst = price
            if remaining <= quantity * 1e-12:
                break
        if remaining > quantity * 1e-12:
            _reject("five-level execution depth is insufficient")
        entry = worst * (1 + direction * slip / 10000)
        if limit is not None:
            entry = min(limit, entry) if buy else max(limit, entry)
    else:
        entry = limit
    if direction * (entry - stop) <= 0 or any(direction * (value - entry) <= 0 for value in targets):
        _reject("estimated execution price invalidates the protection plan")
    stop_exit, target_exit = stop * (1 - direction * slip / 10000), target * (1 - direction * slip / 10000)
    risk = direction * (entry - stop_exit) + fee * (entry + stop_exit)
    reward = direction * (target_exit - entry) - fee * (entry + target_exit)
    if not math.isfinite(risk) or not math.isfinite(reward) or risk <= 0 or reward / risk + 1e-12 < limits["minimum_net_risk_reward_ratio"]:
        _reject("net risk/reward after fees and slippage is below 2.0")
    return {
        "estimatedEntryPrice": entry, "netRiskRewardRatio": reward / risk,
        "spreadBps": spread, "referencePrice": reference, "checkedAt": clock,
        "quote": {**ticker, "last": last, "bidPx": bid, "askPx": ask},
    }
