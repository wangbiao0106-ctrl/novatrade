"""Durable local USDT swap matching against production market quotes.

This account has no exchange client and never sends an authenticated request.
Quantities are contract counts; all cash, margin, fee and PnL calculations use
the public instrument's ``ctVal * ctMult`` conversion.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_FLOOR
import json
import math
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

try:
    from .ai_schema import (
        ACCOUNT_DAILY_LOSS_PERCENT,
        DEFAULT_RECENT_STOP_LOSS_LIMIT,
        DEFAULT_RECENT_STOP_LOSS_WINDOW_SECONDS,
        DEFAULT_STOP_LOSS_COOLDOWN_SECONDS,
    )
    from .order_gateway import InstrumentSpec, OrderNotSubmittedError
except ImportError:  # bundled backend modules are launched as scripts
    from ai_schema import (
        ACCOUNT_DAILY_LOSS_PERCENT,
        DEFAULT_RECENT_STOP_LOSS_LIMIT,
        DEFAULT_RECENT_STOP_LOSS_WINDOW_SECONDS,
        DEFAULT_STOP_LOSS_COOLDOWN_SECONDS,
    )
    from order_gateway import InstrumentSpec, OrderNotSubmittedError


class PaperTradingError(OrderNotSubmittedError):
    """A paper intent was rejected without accepting an order."""


__all__ = [
    "ACCOUNT_DAILY_LOSS_PERCENT",
    "DEFAULT_RECENT_STOP_LOSS_LIMIT",
    "DEFAULT_RECENT_STOP_LOSS_WINDOW_SECONDS",
    "DEFAULT_STOP_LOSS_COOLDOWN_SECONDS",
    "PaperTradingAccount",
    "PaperTradingError",
]


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        if isinstance(value, bool):
            raise ValueError
        result = float(value)
        if not math.isfinite(result) or (positive and result <= 0):
            raise ValueError
        return result
    except (TypeError, ValueError) as error:
        raise PaperTradingError(f"{name} must be finite" + (" and positive" if positive else "")) from error


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _strategy_uuid(value: Any) -> str:
    text = str(value or "manual")
    try:
        return str(UUID(text))
    except ValueError:
        return str(uuid5(NAMESPACE_URL, f"novatrade:paper:strategy:{text}"))


def _floor_quantity(value: float, lot: float) -> float:
    return float((Decimal(str(value)) / Decimal(str(lot))).to_integral_value(rounding=ROUND_FLOOR) * Decimal(str(lot)))


def _aligned_price(value: Any, tick: float) -> float | None:
    if value is None:
        return None
    price = _number(value, "price", positive=True)
    if tick > 0:
        price = _floor_quantity(price, tick)
    return _number(price, "aligned price", positive=True)


def _protection_moves_toward_profit(
    side: str,
    *,
    current_stop: float | None,
    proposed_stop: float | None,
    current_targets: list[float],
    proposed_targets: list[float],
) -> bool:
    """Reject an update that moves either protection side against profit."""
    if side not in {"long", "short"}:
        return False
    epsilon = 1e-12
    if current_stop is not None and proposed_stop is not None:
        if side == "long" and proposed_stop + epsilon < current_stop:
            return False
        if side == "short" and proposed_stop - epsilon > current_stop:
            return False
    if current_targets and not proposed_targets:
        return False
    if current_targets and proposed_targets:
        reverse = side == "short"
        current = sorted(current_targets, reverse=reverse)
        proposed = sorted(proposed_targets, reverse=reverse)
        overlap = min(len(current), len(proposed))
        if side == "long":
            if any(proposed[index] + epsilon < current[index] for index in range(overlap)):
                return False
            if len(proposed) > len(current) and any(value + epsilon < current[-1] for value in proposed[len(current):]):
                return False
        else:
            if any(proposed[index] - epsilon > current[index] for index in range(overlap)):
                return False
            if len(proposed) > len(current) and any(value - epsilon > current[-1] for value in proposed[len(current):]):
                return False
    return True


class PaperTradingAccount:
    """One serialized, atomically persisted local account.

    Mutations are async and guarded by one lock. Projection methods are
    synchronous copies, so a read cannot suspend halfway through a mutation.
    """

    def __init__(
        self, state_path: Path, *, initial_balance: float = 5000,
        fee_rate: float = 0.0005, slippage_bps: float = 2,
        max_drawdown_percent: float = 0,
        max_daily_loss_percent: float = ACCOUNT_DAILY_LOSS_PERCENT,
        stop_loss_cooldown_seconds: float = DEFAULT_STOP_LOSS_COOLDOWN_SECONDS,
        recent_stop_loss_window_seconds: float = DEFAULT_RECENT_STOP_LOSS_WINDOW_SECONDS,
        recent_stop_loss_limit: int = DEFAULT_RECENT_STOP_LOSS_LIMIT,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.state_path = Path(state_path)
        self.fee_rate = _number(fee_rate, "fee rate")
        self.slippage_bps = _number(slippage_bps, "slippage")
        self.max_drawdown_percent = _number(max_drawdown_percent, "maximum drawdown")
        self.max_daily_loss_percent = _number(max_daily_loss_percent, "maximum daily loss")
        self.stop_loss_cooldown_seconds = _number(stop_loss_cooldown_seconds, "stop loss cooldown")
        self.recent_stop_loss_window_seconds = _number(recent_stop_loss_window_seconds, "recent stop loss window", positive=True)
        if isinstance(recent_stop_loss_limit, bool) or not isinstance(recent_stop_loss_limit, int) or recent_stop_loss_limit < 1:
            raise PaperTradingError("recent stop loss limit must be a positive integer")
        self.recent_stop_loss_limit = recent_stop_loss_limit
        self._stop_loss_history_invalid = False
        if self.fee_rate < 0 or self.slippage_bps < 0:
            raise PaperTradingError("fee rate and slippage must be nonnegative")
        if self.stop_loss_cooldown_seconds < 0:
            raise PaperTradingError("stop loss cooldown must be nonnegative")
        if self.max_drawdown_percent < 0 or self.max_drawdown_percent >= 100:
            raise PaperTradingError("maximum drawdown must be zero or below 100 percent")
        if self.max_daily_loss_percent <= 0 or self.max_daily_loss_percent >= 100:
            raise PaperTradingError("maximum daily loss must be above zero and below 100 percent")
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._lock = asyncio.Lock()
        initial_balance = _number(initial_balance, "initial balance", positive=True)
        if self.state_path.exists():
            try:
                self._state = json.loads(self.state_path.read_text(encoding="utf-8"))
                self._validate_state()
            except (OSError, ValueError, TypeError, KeyError) as error:
                # A corrupt account must never silently mint a new balance.
                raise PaperTradingError("paper account state is invalid; restore a backup or explicitly reset it") from error
        else:
            self._state = self._fresh_state(initial_balance)
            self._save()

    def _fresh_state(self, balance: float) -> dict[str, Any]:
        now = self._clock()
        return {
            "schemaVersion": 1, "initialBalance": balance, "cash": balance,
            "orders": [], "fills": [], "positions": {}, "quotes": {},
            "closedTrades": [], "audit": [], "dailyOrderCounts": {},
            "dayStartDay": now.astimezone(timezone.utc).date().isoformat(),
            "dayStartEquity": balance, "equityPeak": balance,
            "killSwitch": False, "killSwitchDay": None, "riskReason": None,
            "updatedAt": _iso(now),
        }

    def _validate_state(self) -> None:
        if not isinstance(self._state, dict) or self._state.get("schemaVersion") != 1:
            raise ValueError("unknown paper schema")
        for name in ("initialBalance", "cash", "dayStartEquity", "equityPeak"):
            _number(self._state[name], name)
        for name in ("orders", "fills", "closedTrades", "audit"):
            if not isinstance(self._state[name], list):
                raise ValueError(f"{name} is invalid")
        for name in ("positions", "quotes", "dailyOrderCounts"):
            if not isinstance(self._state[name], dict):
                raise ValueError(f"{name} is invalid")
        for position in self._state["positions"].values():
            for name in ("quantity", "entryPrice", "contractValue", "leverage", "margin"):
                _number(position[name], name, positive=True)

    def _save(self) -> None:
        self._state["updatedAt"] = _iso(self._clock())
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False, allow_nan=False), encoding="utf-8")
        temporary.replace(self.state_path)

    def _equity(self) -> float:
        return self._state["cash"] + sum(self._unrealized(position) for position in self._state["positions"].values())

    @staticmethod
    def _unrealized(position: dict[str, Any]) -> float:
        sign = 1 if position["side"] == "long" else -1
        return (position.get("markPrice", position["entryPrice"]) - position["entryPrice"]) * position["quantity"] * position["contractValue"] * sign

    def _reserved_margin(self) -> float:
        return sum(position["margin"] for position in self._state["positions"].values()) + sum(
            order["reservedMargin"] + order.get("reservedFee", 0) for order in self._state["orders"] if order["status"] == "live"
        )

    def _available(self) -> float:
        # Unrealized profits are not spendable cash for new entries.
        return max(0.0, self._state["cash"] - self._reserved_margin())

    def _roll_day(self) -> None:
        day = self._clock().astimezone(timezone.utc).date().isoformat()
        if day > self._state["dayStartDay"]:
            self._state["dayStartDay"] = day
            self._state["dayStartEquity"] = self._equity()

    def _update_risk(self) -> None:
        self._roll_day()
        equity = self._equity()
        self._state["equityPeak"] = max(self._state["equityPeak"], equity)
        baseline, peak = self._state["dayStartEquity"], self._state["equityPeak"]
        # Fixed account-level breaker. The threshold is a constructor parameter
        # so the paper broker and the exchange path share one number instead of
        # re-deriving 5% as a literal here.
        if baseline > 0 and equity <= baseline * (1 - self.max_daily_loss_percent / 100) + 1e-9:
            self._state["killSwitch"] = True
            self._state["killSwitchDay"] = self._state.get("killSwitchDay") or self._state["dayStartDay"]
            self._state["riskReason"] = f"账户日内亏损达到 {self.max_daily_loss_percent:g}%"
        elif self.max_drawdown_percent > 0 and peak > 0 and equity <= peak * (1 - self.max_drawdown_percent / 100) + 1e-9:
            self._state["killSwitch"] = True
            self._state["killSwitchDay"] = self._state.get("killSwitchDay") or self._state["dayStartDay"]
            self._state["riskReason"] = f"账户回撤达到 {self.max_drawdown_percent:g}%"
        if self._state["killSwitch"]:
            # The account breaker closes exposure and removes resting entries,
            # matching the production service's account-wide cleanup rule.
            for order in self._state["orders"]:
                if order["status"] == "live" and not order["reduceOnly"]:
                    order.update({"status": "canceled", "reservedMargin": 0.0, "reservedFee": 0.0})
                    self._event("order-canceled", orderID=order["id"], instrumentID=order["instrumentID"], reason="risk_kill_switch")
            for position in list(self._state["positions"].values()):
                quote = self._state["quotes"].get(position["instrumentID"], {"last": position["markPrice"]})
                self._protective_close(position, quote, quantity=position["quantity"], reason="risk_kill_switch")

    def _stop_loss_trades(self, *, window_seconds: float | None = None) -> list[dict[str, Any]]:
        """Return durable full stop-loss exits, optionally limited to a window."""
        now = self._clock().astimezone(timezone.utc)
        cutoff = None if window_seconds is None else now.timestamp() - window_seconds
        self._stop_loss_history_invalid = False
        result: list[dict[str, Any]] = []
        for trade in self._state["closedTrades"]:
            if not isinstance(trade, dict) or trade.get("reason") != "stop_loss":
                continue
            try:
                closed_at = datetime.fromisoformat(str(trade.get("closedAt", "")).replace("Z", "+00:00")).astimezone(timezone.utc)
                realized_pnl = float(trade.get("realizedPnL", 0))
                instrument_id = str(trade.get("instrumentID") or "")
                if not instrument_id or not math.isfinite(realized_pnl):
                    raise ValueError
            except (TypeError, ValueError):
                self._stop_loss_history_invalid = True
                continue
            if cutoff is not None and closed_at.timestamp() < cutoff:
                continue
            result.append({
                "instrumentID": instrument_id,
                "closedAt": _iso(closed_at),
                "realizedPnL": realized_pnl,
                "side": str(trade.get("side") or ""),
            })
        return sorted(result, key=lambda row: row["closedAt"])

    def _entry_guard_values(self, request: dict[str, Any]) -> tuple[float, float, int]:
        cooldown = _number(request.get("stopLossCooldownSeconds", self.stop_loss_cooldown_seconds), "stop loss cooldown")
        window = _number(request.get("recentStopLossWindowSeconds", self.recent_stop_loss_window_seconds), "recent stop loss window", positive=True)
        raw_limit = request.get("recentStopLossLimit", self.recent_stop_loss_limit)
        if isinstance(raw_limit, bool) or not isinstance(raw_limit, int) or raw_limit < 1:
            raise PaperTradingError("recent stop loss limit must be a positive integer")
        return cooldown, window, raw_limit

    def _check_ai_entry_guard(self, request: dict[str, Any], instrument_id: str) -> None:
        """Block rapid re-entry after a stop or a burst of stop-loss exits."""
        if request.get("source") != "ai":
            return
        cooldown, window, limit = self._entry_guard_values(request)
        now = self._clock().astimezone(timezone.utc)
        stop_losses = self._stop_loss_trades()
        if self._stop_loss_history_invalid:
            raise PaperTradingError("stop-loss history is invalid; AI entry is blocked")
        latest = next((row for row in reversed(stop_losses) if row["instrumentID"] == instrument_id), None)
        if latest is not None and cooldown > 0:
            closed_at = datetime.fromisoformat(latest["closedAt"].replace("Z", "+00:00"))
            blocked_until = closed_at.timestamp() + cooldown
            if now.timestamp() < blocked_until:
                until = _iso(datetime.fromtimestamp(blocked_until, tz=timezone.utc))
                reason = f"AI re-entry blocked for {instrument_id} until {until}: previous stop-loss cooldown"
                self._event("entry-rejected", instrumentID=instrument_id, source="ai", reason="stop_loss_cooldown", blockedUntil=until)
                self._save()
                raise PaperTradingError(reason)
        recent = [row for row in stop_losses if now.timestamp() - datetime.fromisoformat(row["closedAt"].replace("Z", "+00:00")).timestamp() <= window]
        if len(recent) >= limit:
            oldest = datetime.fromisoformat(recent[0]["closedAt"].replace("Z", "+00:00"))
            blocked_until = oldest.timestamp() + window
            if now.timestamp() < blocked_until:
                until = _iso(datetime.fromtimestamp(blocked_until, tz=timezone.utc))
                reason = f"AI entries paused until {until}: {len(recent)} stop-loss exits in {window:g}s"
                self._event("entry-rejected", instrumentID=instrument_id, source="ai", reason="recent_stop_loss_burst", blockedUntil=until, stopLossCount=len(recent))
                self._save()
                raise PaperTradingError(reason)

    def _event(self, event_type: str, **fields: Any) -> None:
        self._state["audit"].append({"type": event_type, "timestamp": _iso(self._clock()), "executionMode": "paper", **fields})

    def _quote(self, instrument_id: str, price: float, quote: dict[str, Any] | None = None) -> dict[str, float]:
        result = {"last": _number(price, "market price", positive=True)}
        quote = quote or {}
        for target, names in (("bid", ("bid", "bidPx")), ("ask", ("ask", "askPx"))):
            for name in names:
                value = quote.get(name)
                if value is not None and value != "":
                    parsed = _number(value, target, positive=True)
                    result[target] = parsed
                    break
        if result.get("ask", result["last"]) < result.get("bid", result["last"]) and "ask" in result and "bid" in result:
            raise PaperTradingError("market quote is crossed")
        self._state["quotes"][instrument_id] = result
        return result

    def _execution_price(self, side: str, quote: dict[str, float], limit: float | None = None) -> float:
        reference = quote.get("ask" if side == "buy" else "bid", quote["last"])
        adverse = reference * (1 + (1 if side == "buy" else -1) * self.slippage_bps / 10_000)
        if limit is not None:
            adverse = min(limit, adverse) if side == "buy" else max(limit, adverse)
        return adverse

    @staticmethod
    def _touches(order: dict[str, Any], quote: dict[str, float]) -> bool:
        reference = quote.get("ask" if order["side"] == "buy" else "bid", quote["last"])
        return reference <= order["price"] if order["side"] == "buy" else reference >= order["price"]

    @staticmethod
    def _spec(instrument: InstrumentSpec) -> dict[str, Any]:
        if not instrument.instrumentID or not instrument.instrumentID.endswith("-USDT-SWAP"):
            raise PaperTradingError("only USDT linear swaps are supported")
        for name in ("ctVal", "ctMult", "lotSize", "minSize"):
            _number(getattr(instrument, name), name, positive=True)
        if _number(instrument.tickSize, "tick size") < 0:
            raise PaperTradingError("tick size must be nonnegative")
        return {"instrumentID": instrument.instrumentID, "ctVal": instrument.ctVal, "ctMult": instrument.ctMult,
                "lotSize": instrument.lotSize, "minSize": instrument.minSize, "tickSize": instrument.tickSize}

    @staticmethod
    def _targets(levels: Any, take_profit: Any, *, side: str, entry: float, quantity: float, lot: float) -> list[dict[str, Any]]:
        if levels is None or levels == []:
            levels = [{"price": take_profit, "quantityPercent": 100}] if take_profit is not None else []
        if not isinstance(levels, list) or not all(isinstance(level, dict) for level in levels):
            raise PaperTradingError("take profit levels must be objects")
        result, prices = [], set()
        for level in levels:
            price = _number(level.get("price"), "take profit price", positive=True)
            percent = _number(level.get("quantityPercent"), "take profit percent", positive=True)
            if price in prices or percent > 100:
                raise PaperTradingError("take profit prices must be unique and percentages at most 100")
            if (side == "long" and price <= entry) or (side == "short" and price >= entry):
                raise PaperTradingError("take profit must be beyond the entry price")
            prices.add(price)
            result.append({"price": price, "quantityPercent": percent, "executed": False})
        if result and not math.isclose(sum(level["quantityPercent"] for level in result), 100, abs_tol=1e-6):
            raise PaperTradingError("take profit percentages must sum to 100")
        result.sort(key=lambda level: level["price"], reverse=side == "short")
        remaining = quantity
        for index, level in enumerate(result):
            level["quantity"] = remaining if index == len(result) - 1 else _floor_quantity(quantity * level["quantityPercent"] / 100, lot)
            remaining = max(0, remaining - level["quantity"])
        return result

    def _protection(self, request: dict[str, Any], side: str, entry: float, quantity: float, lot: float) -> tuple[float | None, list[dict[str, Any]]]:
        raw_stop = request.get("stopLossTriggerPrice", request.get("stopLossPrice"))
        stop = _number(raw_stop, "stop loss", positive=True) if raw_stop is not None else None
        if stop is not None and ((side == "long" and stop >= entry) or (side == "short" and stop <= entry)):
            raise PaperTradingError("stop loss must be on the protective side of entry")
        targets = self._targets(request.get("takeProfitLevels"), request.get("takeProfitTriggerPrice", request.get("takeProfitPrice")),
                                side=side, entry=entry, quantity=quantity, lot=lot)
        return stop, targets

    async def submit_intent(
        self, request: dict[str, Any], *, instrument: InstrumentSpec, price: float,
        quote: dict[str, Any] | None = None, daily_order_limit: int | None = None,
        entry_preflight: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        async with self._lock:
            self._roll_day()
            client_id = str(request.get("clientOrderID") or uuid4().hex)
            existing_order = next((order for order in self._state["orders"] if order["clientOrderID"] == client_id), None)
            if existing_order is not None:
                return self._result(existing_order)
            spec = self._spec(instrument)
            instrument_id = str(request.get("instrumentID") or "")
            if instrument_id != instrument.instrumentID:
                raise PaperTradingError("request and instrument specification do not match")
            side = str(request.get("side") or "").lower()
            side = {"long": "buy", "short": "sell"}.get(side, side)
            order_type = str(request.get("orderType") or "market").lower()
            if side not in {"buy", "sell"} or order_type not in {"market", "limit"}:
                raise PaperTradingError("side or order type is invalid")
            reduce_only = bool(request.get("reduceOnly"))
            request = dict(request)
            if order_type == "limit":
                request["price"] = _aligned_price(_number(request.get("price"), "limit price", positive=True), instrument.tickSize)
            for field in ("stopLossTriggerPrice", "stopLossPrice", "takeProfitTriggerPrice", "takeProfitPrice"):
                if request.get(field) is not None:
                    request[field] = _aligned_price(request[field], instrument.tickSize)
            if isinstance(request.get("takeProfitLevels"), list):
                request["takeProfitLevels"] = [
                    {**level, "price": _aligned_price(level.get("price"), instrument.tickSize)} if isinstance(level, dict) else level
                    for level in request["takeProfitLevels"]
                ]
            if request.get("source") == "ai" and not reduce_only and entry_preflight is not None:
                refreshed = await entry_preflight(request)
                quote = refreshed["quote"]
                price = quote["last"]
            market_quote = self._quote(instrument_id, price, quote)
            limit_price = _number(request.get("price"), "limit price", positive=True) if order_type == "limit" else None
            fill_price = self._execution_price(side, market_quote, limit_price)
            sizing_price = limit_price if limit_price is not None else fill_price
            leverage = _number(request.get("leverage") if request.get("leverage") is not None else 1, "leverage", positive=True)
            if leverage < 1:
                raise PaperTradingError("leverage must be at least one")
            margin_mode = str(request.get("marginMode") or "isolated").lower()
            if margin_mode not in {"isolated", "cross"}:
                raise PaperTradingError("margin mode must be isolated or cross")
            # Local positions always isolate their margin from the account.
            margin_mode = "isolated"
            contract_value = instrument.ctVal * instrument.ctMult
            if request.get("quantity") is not None:
                quantity = _number(request["quantity"], "quantity", positive=True)
                units = quantity / instrument.lotSize
                if quantity < instrument.minSize - 1e-9 or not math.isclose(units, round(units), abs_tol=1e-8):
                    raise PaperTradingError("quantity must align with lot size and minimum size")
            else:
                notional = _number(request.get("targetNotional"), "target notional", positive=True)
                quantity = _floor_quantity(notional / (contract_value * sizing_price), instrument.lotSize)
            position = self._state["positions"].get(instrument_id)
            direction = "long" if side == "buy" else "short"
            pending = [order for order in self._state["orders"] if order["status"] == "live" and order["instrumentID"] == instrument_id]
            if reduce_only:
                if position is None or position["side"] == direction:
                    raise PaperTradingError("reduce-only order has no matching position")
                requested_position_side = str(request.get("positionSide") or position["side"]).lower()
                if requested_position_side not in {position["side"], "net"}:
                    raise PaperTradingError("reduce-only position side does not match")
                quantity = min(quantity, position["quantity"])
                leverage = position["leverage"]
            else:
                self._update_risk()
                if self._state["killSwitch"]:
                    raise PaperTradingError(self._state["riskReason"] or "risk kill switch is active")
                if position is not None or any(not order["reduceOnly"] for order in pending):
                    raise PaperTradingError("current instrument already has a position or entry order")
                self._check_ai_entry_guard(request, instrument_id)
                if request.get("source") == "ai":
                    deadline = _number(request.get("_aiEntryDeadline"), "AI entry deadline", positive=True)
                    if self._clock().timestamp() >= deadline:
                        raise PaperTradingError("AI entry expired before paper order submission")
                maximum = daily_order_limit if daily_order_limit is not None else request.get("_dailyOrderLimit")
                if maximum is not None and self.daily_order_count() >= int(maximum):
                    raise PaperTradingError("maximum daily paper orders exceeded")
                margin_cap = request.get("marginUSD")
                if margin_cap is not None:
                    margin_cap = _number(margin_cap, "margin cap", positive=True)
                    # Both the initial margin and immediate entry fee must fit
                    # within the fixed per-order cash budget.
                    quantity_cap = _floor_quantity(margin_cap / (contract_value * sizing_price * (1 / leverage + self.fee_rate)), instrument.lotSize)
                    if request.get("quantity") is None:
                        quantity = min(quantity, quantity_cap)
                    elif quantity > quantity_cap + 1e-9:
                        raise PaperTradingError("order margin and fee exceed configured margin budget")
            if quantity <= 0 or (not reduce_only and quantity < instrument.minSize - 1e-9):
                raise PaperTradingError("target amount is below the instrument minimum size")
            reserved_margin = 0.0 if reduce_only else quantity * contract_value * sizing_price / leverage
            estimated_fee = quantity * contract_value * sizing_price * self.fee_rate
            if not reduce_only and reserved_margin + estimated_fee > self._available() + 1e-8:
                raise PaperTradingError("available paper account margin is insufficient")
            stop, targets = (None, []) if reduce_only else self._protection(request, direction, sizing_price, quantity, instrument.lotSize)
            now = _iso(self._clock())
            order = {
                "id": str(uuid4()), "clientOrderID": client_id,
                "strategyID": _strategy_uuid(request.get("strategyID")), "ownerStrategyID": str(request.get("strategyID") or "manual"),
                "instrumentID": instrument_id, "side": side, "orderType": order_type,
                "quantity": quantity, "price": limit_price, "status": "live",
                "requestedAt": now, "createdAt": now, "fillPrice": None,
                "filledQuantity": 0.0, "reservedMargin": reserved_margin,
                "reservedFee": 0.0 if reduce_only else estimated_fee,
                "reduceOnly": reduce_only, "leverage": leverage, "marginMode": margin_mode,
                "stopLossPrice": stop, "takeProfitLevels": targets,
                "source": str(request.get("source") or "manual"), "spec": spec,
                "signal": request.get("signal"),
            }
            self._state["orders"].append(order)
            if not reduce_only:
                day = self._state["dayStartDay"]
                self._state["dailyOrderCounts"][day] = self._state["dailyOrderCounts"].get(day, 0) + 1
            self._event("entry-submitted" if not reduce_only else "close-submitted", orderID=order["id"], instrumentID=instrument_id, clientOrderID=client_id)
            if order_type == "market" or self._touches(order, market_quote):
                self._fill(order, fill_price)
            self._update_risk()
            self._save()
            return self._result(order)

    def _fill(self, order: dict[str, Any], price: float, *, reason: str | None = None) -> None:
        instrument_id = order["instrumentID"]
        position = self._state["positions"].get(instrument_id)
        contract_value = order["spec"]["ctVal"] * order["spec"]["ctMult"]
        quantity = order["quantity"]
        if order["reduceOnly"]:
            if position is None or position["side"] == ("long" if order["side"] == "buy" else "short"):
                order["status"] = "rejected"
                order["reservedMargin"] = order["reservedFee"] = 0.0
                self._event("order-rejected", orderID=order["id"], reason="matching position is already flat")
                return
            quantity = min(quantity, position["quantity"])
        else:
            if self._state["killSwitch"]:
                order.update({"status": "canceled", "reservedMargin": 0.0, "reservedFee": 0.0})
                self._event("order-canceled", orderID=order["id"], reason="risk_kill_switch")
                return
            if position is not None:
                order["status"] = "rejected"
                order["reservedMargin"] = order["reservedFee"] = 0.0
                self._event("order-rejected", orderID=order["id"], reason="position appeared before entry matched")
                return
            margin = quantity * contract_value * price / order["leverage"]
            fee = quantity * contract_value * price * self.fee_rate
            own_reservation = order["reservedMargin"] + order.get("reservedFee", 0)
            if margin + fee > self._available() + own_reservation + 1e-8:
                order["status"] = "rejected"
                order["reservedMargin"] = order["reservedFee"] = 0.0
                self._event("order-rejected", orderID=order["id"], reason="cash no longer covers entry margin and fee")
                return
        fee = quantity * contract_value * price * self.fee_rate
        timestamp = _iso(self._clock())
        order.update({"status": "filled", "filledQuantity": quantity, "fillPrice": price, "reservedMargin": 0.0, "reservedFee": 0.0, "filledAt": timestamp})
        fill = {"id": str(uuid4()), "orderID": order["id"], "price": price, "quantity": quantity, "fee": fee,
                "timestamp": timestamp, "instrumentID": instrument_id, "side": order["side"], "reason": reason}
        self._state["fills"].append(fill)
        if order["reduceOnly"]:
            old_quantity = position["quantity"]
            ratio = quantity / old_quantity
            direction = 1 if position["side"] == "long" else -1
            gross_pnl = (price - position["entryPrice"]) * quantity * contract_value * direction
            entry_fee = position["entryFeeRemaining"] * ratio
            self._state["cash"] += gross_pnl - fee
            trade = {"orderID": order["id"], "positionID": position["id"], "instrumentID": instrument_id,
                     "quantity": quantity, "entryPrice": position["entryPrice"], "exitPrice": price,
                     "grossPnL": gross_pnl, "realizedPnL": gross_pnl - fee - entry_fee, "closedAt": timestamp,
                     "reason": reason or "manual_close", "strategyID": position["ownerStrategyID"], "side": position["side"]}
            self._state["closedTrades"].append(trade)
            remaining = max(0.0, old_quantity - quantity)
            if remaining < 1e-10:
                del self._state["positions"][instrument_id]
                # A resting close cannot act on a future position.
                for pending in self._state["orders"]:
                    if pending["status"] == "live" and pending["instrumentID"] == instrument_id and pending["reduceOnly"]:
                        pending["status"] = "canceled"
            else:
                position["quantity"] = remaining
                position["margin"] *= 1 - ratio
                position["entryFeeRemaining"] -= entry_fee
                position["markPrice"] = self._state["quotes"].get(instrument_id, {}).get("last", price)
                position["updatedAt"] = timestamp
            self._event("position-closed" if remaining < 1e-10 else "position-partially-closed", **trade)
        else:
            self._state["cash"] -= fee
            direction = "long" if order["side"] == "buy" else "short"
            # Limit price improvement retains the requested target quantities.
            self._state["positions"][instrument_id] = {
                "id": str(uuid4()), "instrumentID": instrument_id, "side": direction,
                "quantity": quantity, "entryPrice": price,
                "markPrice": self._state["quotes"][instrument_id]["last"],
                "contractValue": contract_value, "margin": quantity * contract_value * price / order["leverage"],
                "leverage": order["leverage"], "marginMode": "isolated",
                "entryFeeRemaining": fee, "stopLossPrice": order["stopLossPrice"],
                "takeProfitLevels": deepcopy(order["takeProfitLevels"]),
                "strategyID": order["strategyID"], "ownerStrategyID": order["ownerStrategyID"],
                "entryOrderID": order["id"], "spec": deepcopy(order["spec"]),
                "createdAt": timestamp, "updatedAt": timestamp,
            }
            self._event("entry-filled", orderID=order["id"], instrumentID=instrument_id, quantity=quantity, price=price, fee=fee)

    def _protective_close(self, position: dict[str, Any], quote: dict[str, float], *, quantity: float, reason: str) -> dict[str, Any]:
        timestamp = _iso(self._clock())
        order = {"id": str(uuid4()), "clientOrderID": "paper" + uuid4().hex[:27],
                 "strategyID": position["strategyID"], "ownerStrategyID": position["ownerStrategyID"],
                 "instrumentID": position["instrumentID"], "side": "sell" if position["side"] == "long" else "buy",
                 "orderType": "market", "quantity": min(quantity, position["quantity"]), "price": None,
                 "status": "live", "requestedAt": timestamp, "createdAt": timestamp,
                 "fillPrice": None, "filledQuantity": 0.0, "reservedMargin": 0.0, "reservedFee": 0.0, "reduceOnly": True,
                 "leverage": position["leverage"], "marginMode": "isolated", "stopLossPrice": None,
                 "takeProfitLevels": [], "source": "paper-protection", "spec": deepcopy(position["spec"]), "signal": None}
        self._state["orders"].append(order)
        self._fill(order, self._execution_price(order["side"], quote), reason=reason)
        return self._result(order)

    async def mark(self, instrument_id: str, price: float, *, bid: float | None = None, ask: float | None = None, quote: dict[str, Any] | None = None) -> list[dict[str, Any]]:
        async with self._lock:
            self._roll_day()
            market_quote = self._quote(instrument_id, price, {**(quote or {}), **({"bid": bid} if bid is not None else {}), **({"ask": ask} if ask is not None else {})})
            before = len(self._state["fills"])
            for order in self._state["orders"][:]:
                if order["status"] == "live" and order["instrumentID"] == instrument_id and self._touches(order, market_quote):
                    self._fill(order, self._execution_price(order["side"], market_quote, order["price"]))
            position = self._state["positions"].get(instrument_id)
            if position is not None:
                position["markPrice"] = market_quote["last"]
                position["updatedAt"] = _iso(self._clock())
                # Isolated liquidation cannot consume another position's
                # collateral. Gap losses are capped to the remaining margin.
                if self._unrealized(position) - position["entryFeeRemaining"] <= -position["margin"]:
                    original_cash = self._state["cash"]
                    available_margin = max(0.0, position["margin"] - position["entryFeeRemaining"])
                    self._protective_close(position, market_quote, quantity=position["quantity"], reason="liquidation")
                    capped_cash = max(self._state["cash"], original_cash - available_margin)
                    settlement_adjustment = capped_cash - self._state["cash"]
                    self._state["cash"] = capped_cash
                    if settlement_adjustment > 0:
                        # Quote execution remains visible, while isolated
                        # bankruptcy cannot seize another position's margin.
                        trade = self._state["closedTrades"][-1]
                        trade["realizedPnL"] += settlement_adjustment
                        trade["grossPnL"] += settlement_adjustment
                        trade["isolatedSettlementAdjustment"] = settlement_adjustment
                        event = next((event for event in reversed(self._state["audit"])
                                      if event.get("orderID") == trade["orderID"] and event["type"] == "position-closed"), None)
                        if event is not None:
                            event.update({key: trade[key] for key in ("realizedPnL", "grossPnL", "isolatedSettlementAdjustment")})
                else:
                    stop = position.get("stopLossPrice")
                    stop_hit = stop is not None and (market_quote["last"] <= stop if position["side"] == "long" else market_quote["last"] >= stop)
                    if stop_hit:
                        self._protective_close(position, market_quote, quantity=position["quantity"], reason="stop_loss")
                    else:
                        for target in position["takeProfitLevels"]:
                            if target["executed"]:
                                continue
                            target_hit = market_quote["last"] >= target["price"] if position["side"] == "long" else market_quote["last"] <= target["price"]
                            if not target_hit:
                                continue
                            target["executed"] = True
                            current = self._state["positions"].get(instrument_id)
                            if current is None:
                                break
                            if target["quantity"] > 0:
                                self._protective_close(current, market_quote, quantity=target["quantity"], reason="take_profit")
            self._update_risk()
            self._save()
            return deepcopy(self._state["fills"][before:])

    async def cancel_order(self, order_id: str) -> dict[str, Any]:
        async with self._lock:
            order = next((order for order in self._state["orders"] if order["id"] == order_id or order["clientOrderID"] == order_id), None)
            if order is None:
                raise PaperTradingError("paper order does not exist")
            if order["status"] == "live":
                order.update({"status": "canceled", "reservedMargin": 0.0, "reservedFee": 0.0})
                self._event("order-canceled", orderID=order["id"], instrumentID=order["instrumentID"])
                self._save()
            return self._result(order)

    async def cancel_pending_orders(self, instrument_id: str | None = None) -> list[str]:
        async with self._lock:
            ids = []
            for order in self._state["orders"]:
                if order["status"] == "live" and (instrument_id is None or order["instrumentID"] == instrument_id):
                    order.update({"status": "canceled", "reservedMargin": 0.0, "reservedFee": 0.0})
                    ids.append(order["id"])
                    self._event("order-canceled", orderID=order["id"], instrumentID=order["instrumentID"])
            self._save()
            return ids

    async def update_protection(
        self, position_id: str, *, stop_loss: float | None = None,
        take_profit_levels: list[dict[str, Any]] | None = None, take_profit: float | None = None,
    ) -> dict[str, Any]:
        """Update supplied protection sides; None preserves the current side.

        Existing staged targets retain their fixed quantities and executed
        flags when only the stop changes. Use cancel_protection to clear both.
        """
        async with self._lock:
            position = next((position for position in self._state["positions"].values() if position["id"] == position_id or position["instrumentID"] == position_id), None)
            if position is None:
                raise PaperTradingError("paper position does not exist")
            tick = position["spec"]["tickSize"]
            aligned_levels = [
                {**level, "price": _aligned_price(level.get("price"), tick)} if isinstance(level, dict) else level
                for level in take_profit_levels
            ] if isinstance(take_profit_levels, list) else take_profit_levels
            stop = position["stopLossPrice"]
            targets = deepcopy(position["takeProfitLevels"])
            if stop_loss is not None:
                stop, _ = self._protection({"stopLossPrice": _aligned_price(stop_loss, tick)},
                                           position["side"], position["markPrice"], position["quantity"], position["spec"]["lotSize"])
            if take_profit_levels is not None or take_profit is not None:
                targets = self._targets(aligned_levels, _aligned_price(take_profit, tick), side=position["side"],
                                        entry=position["markPrice"], quantity=position["quantity"], lot=position["spec"]["lotSize"])
            current_targets = [
                float(level["price"])
                for level in position["takeProfitLevels"]
                if isinstance(level, dict) and not level.get("executed") and level.get("price") is not None
            ]
            proposed_targets = [
                float(level["price"])
                for level in targets
                if isinstance(level, dict) and not level.get("executed") and level.get("price") is not None
            ]
            if not _protection_moves_toward_profit(
                position["side"],
                current_stop=position["stopLossPrice"],
                proposed_stop=stop,
                current_targets=current_targets,
                proposed_targets=proposed_targets,
            ):
                raise PaperTradingError("protection can only move toward profit")
            position["stopLossPrice"], position["takeProfitLevels"] = stop, targets
            self._event("position-protection-updated", positionID=position["id"], instrumentID=position["instrumentID"], stopLossPrice=stop, takeProfitLevels=targets)
            self._save()
            return self._position_snapshot(position)

    async def cancel_protection(self, position_id: str) -> dict[str, Any]:
        async with self._lock:
            position = next((position for position in self._state["positions"].values() if position["id"] == position_id or position["instrumentID"] == position_id), None)
            if position is None:
                raise PaperTradingError("paper position does not exist")
            position["stopLossPrice"], position["takeProfitLevels"] = None, []
            self._event("position-protection-canceled", positionID=position["id"], instrumentID=position["instrumentID"])
            self._save()
            return self._position_snapshot(position)

    async def flatten(self, prices: dict[str, float] | None = None) -> dict[str, Any]:
        async with self._lock:
            self._roll_day()
            cancelled = []
            for order in self._state["orders"]:
                if order["status"] == "live":
                    order.update({"status": "canceled", "reservedMargin": 0.0, "reservedFee": 0.0})
                    cancelled.append(order["id"])
            closed = []
            for position in list(self._state["positions"].values()):
                instrument_id = position["instrumentID"]
                quote = self._state["quotes"].get(instrument_id, {"last": position["markPrice"]})
                if prices and instrument_id in prices:
                    quote = self._quote(instrument_id, prices[instrument_id])
                closed.append(self._protective_close(position, quote, quantity=position["quantity"], reason="flatten"))
            self._update_risk()
            self._save()
            return {"scope": "account", "cancelledOrderIDs": cancelled, "closed": closed, "updatedAt": _iso(self._clock())}

    async def reset(self, initial_balance: float = 5000) -> dict[str, Any]:
        async with self._lock:
            self._state = self._fresh_state(_number(initial_balance, "initial balance", positive=True))
            self._save()
            return self.account_snapshot()

    async def reset_risk(self) -> dict[str, Any]:
        async with self._lock:
            self._roll_day()
            latch_day = self._state.get("killSwitchDay")
            if latch_day is None or self._state["dayStartDay"] > latch_day:
                self._state["killSwitch"] = False
                self._state["killSwitchDay"] = None
                self._state["riskReason"] = None
                self._state["equityPeak"] = self._equity()
            self._save()
            return self.risk_snapshot()

    @staticmethod
    def _result(order: dict[str, Any]) -> dict[str, Any]:
        contract_value = order["spec"]["ctVal"] * order["spec"]["ctMult"]
        reference = order["fillPrice"] or order["price"] or 0
        notional = order["quantity"] * contract_value * reference
        return {"orderID": order["id"], "clientOrderID": order["clientOrderID"], "instrumentID": order["instrumentID"],
                "side": order["side"], "orderType": order["orderType"], "quantity": order["quantity"],
                "status": order["status"], "message": "本地纸面成交" if order["status"] == "filled" else "本地纸面挂单",
                "submittedAt": order["requestedAt"], "notional": notional, "leverage": order["leverage"],
                "marginUSD": notional / order["leverage"], "executionMode": "paper", "strategyID": order["ownerStrategyID"]}

    def _position_snapshot(self, position: dict[str, Any]) -> dict[str, Any]:
        active_targets = [target["price"] for target in position["takeProfitLevels"] if not target["executed"] and target["quantity"] > 0]
        return {"id": position["id"], "instrumentID": position["instrumentID"], "side": position["side"],
                "quantity": position["quantity"], "entryPrice": position["entryPrice"], "markPrice": position["markPrice"],
                "unrealizedPnL": self._unrealized(position), "marginMode": "isolated", "margin": position["margin"],
                "leverage": position["leverage"], "takeProfitPrice": active_targets[0] if active_targets else None,
                "stopLossPrice": position["stopLossPrice"], "takeProfitLevels": deepcopy(position["takeProfitLevels"]),
                "strategyID": position["ownerStrategyID"], "entryOrderID": position["entryOrderID"]}

    def positions(self) -> list[dict[str, Any]]:
        return [self._position_snapshot(position) for position in self._state["positions"].values()]

    def pending_orders(self) -> list[dict[str, Any]]:
        return [{"id": order["id"], "instrumentID": order["instrumentID"], "side": order["side"],
                 "status": "live", "quantity": order["quantity"], "price": order["price"], "createdAt": order["createdAt"],
                 "filledQuantity": order["filledQuantity"], "averageFillPrice": order["fillPrice"],
                 "margin": order["reservedMargin"], "leverage": order["leverage"], "marginMode": "isolated",
                 "takeProfitPrice": order["takeProfitLevels"][0]["price"] if order["takeProfitLevels"] else None,
                 "stopLossPrice": order["stopLossPrice"], "clientOrderID": order["clientOrderID"],
                 "strategyID": order["ownerStrategyID"], "reduceOnly": order["reduceOnly"]}
                for order in self._state["orders"] if order["status"] == "live"]

    def all_orders(self) -> list[dict[str, Any]]:
        return [{"id": order["id"], "strategyID": order["strategyID"], "instrumentID": order["instrumentID"],
                 "side": "long" if order["side"] == "buy" else "short", "quantity": order["quantity"],
                 "requestedAt": order["requestedAt"], "fillPrice": order["fillPrice"], "status": order["status"],
                 "remoteOrderID": None, "clientOrderID": order["clientOrderID"], "signal": deepcopy(order["signal"])}
                for order in self._state["orders"]]

    def all_fills(self) -> list[dict[str, Any]]:
        return deepcopy(self._state["fills"])

    def lookup_order(self, instrument_id: str, client_id: str) -> dict[str, Any] | None:
        order = next((order for order in self._state["orders"] if order["instrumentID"] == instrument_id and (order["clientOrderID"] == client_id or order["id"] == client_id)), None)
        return self._result(order) if order is not None else None

    def tracked_instruments(self) -> list[str]:
        return sorted(set(self._state["positions"]) | {order["instrumentID"] for order in self._state["orders"] if order["status"] == "live"})

    def daily_order_count(self, day: str | None = None) -> int:
        return int(self._state["dailyOrderCounts"].get(day or self._clock().astimezone(timezone.utc).date().isoformat(), 0))

    def audit(self) -> list[dict[str, Any]]:
        return deepcopy(self._state["audit"][-1000:])

    def _entry_guard_snapshot(
        self, *, stop_loss_cooldown_seconds: float | None = None,
        recent_stop_loss_window_seconds: float | None = None,
        recent_stop_loss_limit: int | None = None,
    ) -> dict[str, Any]:
        cooldown = self.stop_loss_cooldown_seconds if stop_loss_cooldown_seconds is None else _number(stop_loss_cooldown_seconds, "stop loss cooldown")
        window = self.recent_stop_loss_window_seconds if recent_stop_loss_window_seconds is None else _number(recent_stop_loss_window_seconds, "recent stop loss window", positive=True)
        limit = self.recent_stop_loss_limit if recent_stop_loss_limit is None else recent_stop_loss_limit
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise PaperTradingError("recent stop loss limit must be a positive integer")
        now = self._clock().astimezone(timezone.utc)
        all_stops = self._stop_loss_trades()
        recent = [row for row in all_stops if now.timestamp() - datetime.fromisoformat(row["closedAt"].replace("Z", "+00:00")).timestamp() <= window]
        cooldowns: dict[str, dict[str, Any]] = {}
        if cooldown > 0:
            for row in all_stops:
                closed_at = datetime.fromisoformat(row["closedAt"].replace("Z", "+00:00"))
                blocked_until = closed_at.timestamp() + cooldown
                if blocked_until <= now.timestamp():
                    continue
                current = cooldowns.get(row["instrumentID"])
                if current is None or row["closedAt"] > current["closedAt"]:
                    cooldowns[row["instrumentID"]] = {
                        "closedAt": row["closedAt"], "blockedUntil": _iso(datetime.fromtimestamp(blocked_until, tz=timezone.utc)),
                        "reason": "stop_loss", "side": row["side"], "realizedPnL": row["realizedPnL"],
                    }
        burst_blocked_until = None
        if len(recent) >= limit:
            oldest = datetime.fromisoformat(recent[0]["closedAt"].replace("Z", "+00:00"))
            candidate = oldest.timestamp() + window
            if candidate > now.timestamp():
                burst_blocked_until = _iso(datetime.fromtimestamp(candidate, tz=timezone.utc))
        return {
            "reentryGuardAvailable": not self._stop_loss_history_invalid,
            "reentryCooldowns": cooldowns,
            "recentStopLosses": recent,
            "recentStopLossWindowSeconds": window,
            "recentStopLossLimit": limit,
            "recentStopLossBurstBlockedUntil": burst_blocked_until,
        }

    def account_snapshot(
        self, *, stop_loss_cooldown_seconds: float | None = None,
        recent_stop_loss_window_seconds: float | None = None,
        recent_stop_loss_limit: int | None = None,
    ) -> dict[str, Any]:
        day = self._clock().astimezone(timezone.utc).date().isoformat()
        trades = [trade for trade in self._state["closedTrades"] if trade["closedAt"][:10] == day]
        today_fills = [fill for fill in self._state["fills"] if fill["timestamp"][:10] == day]
        # Each closed trade already includes its assigned entry fee. Display
        # today's cash change from gross close PnL and today's execution fees.
        closed_gross = sum(trade["grossPnL"] for trade in trades)
        equity = self._equity()
        guard = self._entry_guard_snapshot(
            stop_loss_cooldown_seconds=stop_loss_cooldown_seconds,
            recent_stop_loss_window_seconds=recent_stop_loss_window_seconds,
            recent_stop_loss_limit=recent_stop_loss_limit,
        )
        return {"mode": "paper", "executionMode": "paper", "profile": "local-paper", "site": "local",
                "label": "纸面交易", "authenticated": True, "initialBalanceUSD": self._state["initialBalance"],
                "equityUSD": equity, "availableEquityUSD": self._available(), "totalAssetValueUSD": equity,
                "todayPnLUSD": closed_gross - sum(fill["fee"] for fill in today_fills),
                "todayLossCount": sum(1 for trade in trades if trade["realizedPnL"] < -1e-9),
                "todayAIOrderCount": self.daily_order_count(), "realizedPnLUSD": self._state["cash"] - self._state["initialBalance"],
                **guard,
                "assets": [{"id": "USDT", "currency": "USDT", "equity": equity, "available": self._available(), "usdValue": equity}],
                "positions": self.positions(), "positionsKnown": True, "pendingOrders": self.pending_orders(), "pendingOrdersKnown": True,
                "dataQuality": {"dailyBillsAvailable": True, "dailyBillsPaginationComplete": True, "positionsAvailable": True, "pendingOrdersAvailable": True},
                "updatedAt": _iso(self._clock())}

    def risk_snapshot(self) -> dict[str, Any]:
        equity, peak = self._equity(), self._state["equityPeak"]
        current_day = self._clock().astimezone(timezone.utc).date().isoformat()
        baseline = self._state["dayStartEquity"] if current_day == self._state["dayStartDay"] else equity
        return {"equity": equity, "equityPeak": peak, "dayStartEquity": baseline,
                "dayStartAt": current_day + "T00:00:00Z", "dailyPnLPercent": (equity - baseline) / baseline * 100 if baseline > 0 else 0,
                "drawdownPercent": (peak - equity) / peak * 100 if peak > 0 else 0,
                "killSwitch": self._state["killSwitch"], "reason": self._state["riskReason"], "strategyCapitals": [],
                "globalNotionals": {position["instrumentID"]: position["quantity"] * position["contractValue"] * position["markPrice"] for position in self._state["positions"].values()},
                "strategyCapitalBase": equity, "equitySource": "paper", "accountAvailable": True,
                "dataQuality": {"available": True, "equitySource": "paper"}, "updatedAt": _iso(self._clock())}
