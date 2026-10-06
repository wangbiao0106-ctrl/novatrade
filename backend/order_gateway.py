"""Deterministic order gateway shared by manual and AI order paths.

The gateway deliberately accepts intents rather than arbitrary OKX payloads.
It owns the small durable reservation ledger used to prevent duplicate or
unknown-result submissions from opening untracked exposure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import asyncio
import json
import math
from pathlib import Path
from typing import Any, Awaitable, Callable
from uuid import uuid4

try:
    from .ai_schema import AI_ENTRY_COOLDOWN_SECONDS
except ImportError:  # bundled backend modules are launched as scripts
    from ai_schema import AI_ENTRY_COOLDOWN_SECONDS

class OrderGatewayError(ValueError):
    pass


class OrderNotSubmittedError(OrderGatewayError):
    """An intent was rejected before any exchange order request was sent."""


def _number(value: Any, name: str, *, positive: bool = False) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as error:
        raise OrderGatewayError(f"{name} must be numeric") from error
    if not math.isfinite(parsed) or (positive and parsed <= 0):
        raise OrderGatewayError(f"{name} must be finite" + (" and positive" if positive else ""))
    return parsed


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _utc_day(value: datetime | None = None) -> str:
    return (value or datetime.now(timezone.utc)).astimezone(timezone.utc).date().isoformat()


@dataclass(frozen=True)
class InstrumentSpec:
    instrumentID: str
    ctVal: float
    ctMult: float = 1.0
    lotSize: float = 1.0
    minSize: float = 1.0
    tickSize: float = 0.0

    @classmethod
    def from_okx(cls, row: dict[str, Any]) -> "InstrumentSpec":
        return cls(
            instrumentID=str(row.get("instId") or row.get("instrumentID") or ""),
            ctVal=_number(row.get("ctVal"), "ctVal", positive=True),
            ctMult=_number(row.get("ctMult") or 1, "ctMult", positive=True),
            lotSize=_number(row.get("lotSz") or row.get("lotSize") or 1, "lotSz", positive=True),
            minSize=_number(row.get("minSz") or row.get("minSize") or 1, "minSz", positive=True),
            tickSize=_number(row.get("tickSz") or row.get("tickSize") or 0, "tickSz"),
        )

    def quantity_for_notional(self, notional: float, price: float) -> float:
        notional = _number(notional, "target notional", positive=True)
        price = _number(price, "price", positive=True)
        raw = notional / (self.ctVal * self.ctMult * price)
        quantity = math.floor(raw / self.lotSize) * self.lotSize
        if quantity < self.minSize or quantity <= 0:
            raise OrderGatewayError("target notional produces less than the instrument minimum size")
        return round(quantity, 12)

    def aligned_price(self, value: float | None) -> float | None:
        if value is None:
            return None
        value = _number(value, "price", positive=True)
        if self.tickSize > 0:
            value = math.floor(value / self.tickSize) * self.tickSize
        return round(value, 12)


class OrderGateway:
    """Serialize, authorize and submit exchange orders.

    ``submit`` and ``lookup`` are injected so the gateway can be tested with
    fake exchanges and the FastAPI module can keep its existing signed REST
    implementation in one place.
    """

    def __init__(
        self,
        state_path: Path,
        *,
        submit: Callable[[dict[str, Any], bool], Awaitable[dict[str, Any]]],
        lookup: Callable[[str, str, bool], Awaitable[dict[str, Any] | None]] | None = None,
        runtime_sink: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        limits: dict[str, float] | None = None,
    ) -> None:
        self.state_path = state_path
        self.submit = submit
        self.lookup = lookup
        self.runtime_sink = runtime_sink
        self.limits = {
            "maxInstrumentNotional": 25_000.0,
            "maxTotalNotional": 100_000.0,
            "maxMarginPercent": 25.0,
            "minOrderIntervalSeconds": 15.0,
            "maxOrdersPerHour": 60.0,
            **(limits or {}),
        }
        self._lock = asyncio.Lock()
        self._state = self._read()

    def _read(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(self.state_path.suffix + ".tmp")
        temporary.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.state_path)

    async def _emit_runtime(self, value: dict[str, Any]) -> None:
        """Best-effort human-readable runtime log; never block order state."""
        if self.runtime_sink is None:
            return
        try:
            await self.runtime_sink(value)
        except Exception:
            # The durable gateway audit is authoritative. A log-file failure
            # must not turn an accepted exchange order into an unknown result.
            return

    @property
    def reservations(self) -> dict[str, dict[str, Any]]:
        rows = self._state.setdefault("reservations", {})
        return rows if isinstance(rows, dict) else {}

    def snapshot(self) -> dict[str, Any]:
        return {
            "reservations": self.reservations.copy(),
            "dailyOrderCounts": dict(self._state.get("dailyOrderCounts", {})),
            "updatedAt": self._state.get("updatedAt"),
        }

    def daily_order_count(self, day: str | None = None) -> int:
        """Return committed and unresolved AI entries for a UTC calendar day.

        An exchange timeout leaves an unresolved reservation because the
        order result is unknown. Counting that reservation until reconciliation
        prevents a retry from consuming another entry slot and exceeding the
        daily cap.
        """
        target_day = day or _utc_day()
        value = self._state.get("dailyOrderCounts", {}).get(target_day, 0)
        count = int(value) if isinstance(value, (int, float)) and value >= 0 else 0
        for reservation in self.reservations.values():
            if not isinstance(reservation, dict):
                continue
            if (reservation.get("source") == "ai"
                    and not reservation.get("reduceOnly")
                    and reservation.get("createdDay") == target_day
                    and not reservation.get("dailyCountRecorded", False)):
                count += 1
        return count

    def _recent_ai_entry(self, instrument_id: str, now: datetime, client_order_id: str) -> bool:
        """Return whether this contract has an AI entry inside the fixed window.

        Accepted and unresolved reservations both remain in the ledger. That
        makes an unknown exchange response consume the same duplicate-entry
        protection as a confirmed order, while a definitely unsubmitted order
        is removed before this check can see it.
        """
        cutoff = now.timestamp() - AI_ENTRY_COOLDOWN_SECONDS
        for reservation in self.reservations.values():
            if not isinstance(reservation, dict):
                continue
            if (reservation.get("source") != "ai"
                    or reservation.get("reduceOnly")
                    or reservation.get("instrumentID") != instrument_id):
                continue
            # Retrying an unresolved submission with the same client ID is
            # idempotent and must reach reconciliation rather than creating a
            # second order or being mistaken for a new entry.
            if reservation.get("clientOrderID") == client_order_id:
                continue
            created_at = reservation.get("createdAt")
            if not isinstance(created_at, str):
                continue
            try:
                timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            if timestamp >= cutoff:
                return True
        return False

    def _prune_expired_entries(self, now: datetime) -> None:
        """Drop terminal AI entry reservations after the shared cooldown.

        Accepted orders remain in the ledger long enough to enforce the
        cross-provider cooldown. Keeping them forever would make
        ``maxTotalNotional`` eventually reject every new entry even after the
        old order is no longer relevant to that protection window.
        Unknown submissions stay until reconciliation because they may still
        have reached the exchange.
        """
        cutoff = now.timestamp() - AI_ENTRY_COOLDOWN_SECONDS
        for key, reservation in list(self.reservations.items()):
            if not isinstance(reservation, dict) or key.startswith("unresolved-"):
                continue
            if reservation.get("source") != "ai" or reservation.get("reduceOnly"):
                continue
            created_at = reservation.get("createdAt")
            if not isinstance(created_at, str):
                continue
            try:
                timestamp = datetime.fromisoformat(created_at.replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
            if timestamp < cutoff:
                self.reservations.pop(key, None)

    def _record_ai_order(self, reservation: dict[str, Any], *, day: str | None = None) -> None:
        if reservation.get("source") != "ai" or reservation.get("reduceOnly") or reservation.get("dailyCountRecorded"):
            return
        counts = self._state.setdefault("dailyOrderCounts", {})
        key = str(day or reservation.get("createdDay") or _utc_day())
        counts[key] = int(counts.get(key, 0)) + 1
        reservation["dailyCountRecorded"] = True
        # Retain a small rolling window while preserving today's count across
        # restarts. Historical OKX bills are the source of loss statistics.
        for old in sorted(counts)[:-8]:
            counts.pop(old, None)

    async def reconcile(self, *, demo: bool) -> None:
        """Resolve durable unknown submissions after a restart.

        A missing lookup result is intentionally retained. Only an explicit
        absent response is safe to release.
        """
        if self.lookup is None:
            return
        async with self._lock:
            changed = False
            for key, reservation in list(self.reservations.items()):
                if not key.startswith("unresolved-") or reservation.get("demo") != demo:
                    continue
                client_id = reservation.get("clientOrderID")
                if not client_id:
                    continue
                result = await self.lookup(str(reservation.get("instrumentID")), str(client_id), demo)
                if result is None:
                    self.reservations.pop(key, None)
                    changed = True
                elif result.get("orderID"):
                    self._record_ai_order(reservation)
                    self.reservations[str(result["orderID"])] = {**reservation, "orderID": result["orderID"], "inFlight": False}
                    self.reservations.pop(key, None)
                    changed = True
            if changed:
                self._state["updatedAt"] = _now()
                self._write()

    async def submit_intent(
        self,
        request: dict[str, Any],
        *,
        demo: bool,
        instrument: InstrumentSpec,
        price: float,
        available_equity: float | None = None,
        force_reduce_only: bool = False,
        daily_order_limit: int | None = None,
    ) -> dict[str, Any]:
        """Convert a validated intent into one exchange order."""
        async with self._lock:
            now = datetime.now(timezone.utc)
            self._prune_expired_entries(now)
            instrument_id = str(request.get("instrumentID") or "")
            if instrument_id != instrument.instrumentID:
                raise OrderGatewayError("instrument specification does not match request")
            side = str(request.get("side") or "").lower()
            if side not in {"buy", "sell"}:
                raise OrderGatewayError("side must be buy or sell")
            order_type = str(request.get("orderType") or "market").lower()
            if order_type not in {"market", "limit"}:
                raise OrderGatewayError("orderType must be market or limit")
            order_price = instrument.aligned_price(request.get("price"))
            if order_type == "limit" and order_price is None:
                raise OrderGatewayError("limit orders require a positive price")
            if order_price is None:
                order_price = price
            quantity = request.get("quantity")
            if quantity is None:
                # A limit order's notional is determined by its limit price,
                # which can differ from the latest ticker used for admission.
                # Size against the actual order price so the fixed margin cap
                # cannot be exceeded when the limit is above the ticker.
                quantity = instrument.quantity_for_notional(request.get("targetNotional"), order_price)
            quantity = _number(quantity, "quantity", positive=True)
            quantity = math.floor(quantity / instrument.lotSize) * instrument.lotSize
            if quantity < instrument.minSize or quantity <= 0:
                raise OrderGatewayError("quantity is below instrument minimum or not lot aligned")
            notional = abs(quantity * instrument.ctVal * instrument.ctMult * order_price)
            reduce_only = bool(request.get("reduceOnly") or force_reduce_only)
            leverage = _number(request.get("leverage", 1), "leverage", positive=True)
            margin_budget = request.get("marginUSD")
            if margin_budget is not None and not reduce_only:
                margin_budget = _number(margin_budget, "marginUSD", positive=True)
                actual_margin = notional / leverage
                if actual_margin > margin_budget + max(1e-9, margin_budget * 1e-9):
                    raise OrderGatewayError("order margin exceeds configured per-order margin")
            if not reduce_only and notional > self.limits["maxInstrumentNotional"]:
                raise OrderGatewayError("instrument notional limit exceeded")
            current_total = sum(
                _number(row.get("notional", 0), "reservation notional")
                for row in self.reservations.values()
                if isinstance(row, dict)
            )
            if not reduce_only and current_total + notional > self.limits["maxTotalNotional"]:
                raise OrderGatewayError("total notional limit exceeded")
            if not reduce_only and available_equity is not None and available_equity > 0:
                # Notional includes leverage; the account margin consumed by
                # this entry is notional / leverage.
                margin_percent = notional / leverage / available_equity * 100
                if margin_percent > self.limits["maxMarginPercent"]:
                    raise OrderGatewayError("margin percentage limit exceeded")
            client_id = str(request.get("clientOrderID") or ("ai" + uuid4().hex)[:32])
            if not client_id.isalnum() or not 1 <= len(client_id) <= 32:
                raise OrderGatewayError("clientOrderID must be 1-32 ASCII letters or digits")
            if client_id in self._state.setdefault("clientOrderIDs", {}):
                return self._state["clientOrderIDs"][client_id]
            source = str(request.get("source") or "manual")
            if source == "ai" and not reduce_only and self._recent_ai_entry(instrument_id, now, client_id):
                raise OrderGatewayError("AI 同一合约 12 小时内不允许重复开仓")
            if not reduce_only and source == "ai" and daily_order_limit is not None:
                if daily_order_limit < 1:
                    raise OrderGatewayError("maximum daily AI orders is zero")
                if self.daily_order_count(_utc_day(now)) >= daily_order_limit:
                    raise OrderGatewayError("maximum daily AI orders exceeded")
            submission_times = [float(value) for value in self._state.setdefault("submissionTimes", []) if isinstance(value, (int, float))]
            submission_times = [value for value in submission_times if now.timestamp() - value <= 3600]
            if not reduce_only and submission_times:
                if now.timestamp() - max(submission_times) < self.limits["minOrderIntervalSeconds"]:
                    raise OrderGatewayError("minimum order interval has not elapsed")
                if len(submission_times) >= self.limits["maxOrdersPerHour"]:
                    raise OrderGatewayError("maximum orders per hour exceeded")
            token = "unresolved-" + client_id
            previous_reservation = self.reservations.get(token)
            reservation = {"instrumentID": instrument_id, "notional": notional, "clientOrderID": client_id, "demo": demo, "inFlight": True, "createdAt": _now(), "createdDay": _utc_day(now), "reduceOnly": reduce_only, "source": source, "leverage": leverage, "dailyCountRecorded": False}
            reservation.update({
                "decisionID": request.get("decisionID") or request.get("decisionId"),
                "strategyID": request.get("strategyID") or request.get("strategyId"),
                "takeProfitTriggerPrice": instrument.aligned_price(request.get("takeProfitTriggerPrice")),
                "stopLossTriggerPrice": instrument.aligned_price(request.get("stopLossTriggerPrice")),
                "triggerPriceType": "mark" if request.get("takeProfitTriggerPrice") is not None or request.get("stopLossTriggerPrice") is not None else None,
            })
            if not reduce_only:
                self.reservations[token] = reservation
            self._state["updatedAt"] = _now()
            self._write()
            payload = dict(request)
            payload.update({
                "quantity": quantity,
                "clientOrderID": client_id,
                "price": order_price if order_type == "limit" else request.get("price"),
                "takeProfitTriggerPrice": instrument.aligned_price(request.get("takeProfitTriggerPrice")),
                "stopLossTriggerPrice": instrument.aligned_price(request.get("stopLossTriggerPrice")),
                "reduceOnly": reduce_only,
            })
            try:
                result = await self.submit(payload, demo)
            except OrderNotSubmittedError:
                # This call definitely sent no order POST, so it cannot
                # reserve exposure or consume the AI daily entry allowance.
                # Preserve an older unknown result with the same client ID.
                if not reduce_only:
                    if previous_reservation is None:
                        self.reservations.pop(token, None)
                    else:
                        self.reservations[token] = previous_reservation
                self._state["updatedAt"] = _now()
                self._write()
                raise
            except Exception:
                # Keep the unresolved reservation. A later reconciliation can
                # distinguish a definite absent order from an unknown result.
                reservation["inFlight"] = False
                self._write()
                raise
            order_id = str(result.get("orderID") or result.get("ordId") or "")
            if not order_id:
                reservation["inFlight"] = False
                self._write()
                raise OrderGatewayError("exchange response did not include orderID")
            self.reservations.pop(token, None)
            if not reduce_only:
                # Mark the source object before copying it into the durable
                # accepted-order reservation. Otherwise the copy would look
                # unresolved and be counted a second time on the next quota
                # check.
                self._record_ai_order(reservation)
                self.reservations[order_id] = {**reservation, "orderID": order_id, "inFlight": False}
            output = {**result, "clientOrderID": client_id, "quantity": quantity, "notional": notional,
                      "decisionID": reservation.get("decisionID"), "strategyID": reservation.get("strategyID"),
                      "takeProfitTriggerPrice": reservation.get("takeProfitTriggerPrice"),
                      "stopLossTriggerPrice": reservation.get("stopLossTriggerPrice"),
                      "triggerPriceType": reservation.get("triggerPriceType")}
            output["leverage"] = leverage
            output["marginUSD"] = notional / leverage
            self._state["clientOrderIDs"][client_id] = output
            if not reduce_only:
                submission_times.append(now.timestamp())
                self._state["submissionTimes"] = submission_times[-int(self.limits["maxOrdersPerHour"]):]
            reason = "AI 开仓已提交，原生 OCO 已挂载" if source == "ai" and not reduce_only else "订单已提交"
            output["message"] = (
                f"{reason} instrument={instrument_id} orderID={order_id} clientOrderID={client_id} "
                f"tp={output.get('takeProfitTriggerPrice') or 'none'} "
                f"sl={output.get('stopLossTriggerPrice') or 'none'} "
                f"trigger={output.get('triggerPriceType') or 'none'}"
            )
            self._state.setdefault("audit", []).append({"at": _now(), "type": "submitted", "reason": "entry-submitted" if not reduce_only else "close-submitted", "order": output})
            self._state["audit"] = self._state["audit"][-1000:]
            self._write()
            await self._emit_runtime({
                "level": "info", "type": "order-submitted",
                "reason": "entry-submitted" if not reduce_only else "close-submitted",
                "message": output["message"], "instrumentID": instrument_id,
                "orderID": order_id, "clientOrderID": client_id,
                "decisionID": output.get("decisionID"), "strategyID": output.get("strategyID"),
                "takeProfitTriggerPrice": output.get("takeProfitTriggerPrice"),
                "stopLossTriggerPrice": output.get("stopLossTriggerPrice"),
                "triggerPriceType": output.get("triggerPriceType"),
            })
            return output

    async def record_audit(self, value: dict[str, Any]) -> None:
        async with self._lock:
            self._state.setdefault("audit", []).append({"at": _now(), **value})
            self._state["audit"] = self._state["audit"][-1000:]
            self._write()

    async def tracked_entries(self, *, demo: bool) -> list[dict[str, Any]]:
        async with self._lock:
            return [dict(row) for row in self.reservations.values()
                    if isinstance(row, dict) and row.get("demo") == demo
                    and not row.get("reduceOnly") and row.get("orderID")]

    async def record_event(self, value: dict[str, Any], *, event_key: str) -> bool:
        async with self._lock:
            keys = self._state.setdefault("auditEventKeys", [])
            if not isinstance(keys, list):
                keys = []
            if event_key in keys:
                return False
            keys.append(event_key)
            self._state["auditEventKeys"] = keys[-2000:]
            self._state.setdefault("audit", []).append({"at": _now(), **value})
            self._state["audit"] = self._state["audit"][-1000:]
            self._state["updatedAt"] = _now()
            self._write()
            await self._emit_runtime(value)
            return True

    async def cancel_order(
        self,
        *,
        instrument_id: str,
        order_id: str,
        demo: bool,
        cancel: Callable[[], Awaitable[dict[str, Any]]],
    ) -> dict[str, Any]:
        """Serialize a validated cancel and release its local reservation.

        The caller owns exchange-side pending-order/contract validation. This
        method owns the same lock and audit ledger as entry submission so a
        concurrent retry cannot cancel and reserve the same order out of
        order.
        """
        if not instrument_id or not order_id:
            raise OrderGatewayError("instrumentID and orderID are required")
        async with self._lock:
            reservation = self.reservations.get(order_id)
            if reservation is not None:
                if str(reservation.get("instrumentID")) != instrument_id:
                    raise OrderGatewayError("order reservation does not match instrument")
                if bool(reservation.get("demo")) != bool(demo):
                    raise OrderGatewayError("order mode does not match reservation")
            result = await cancel()
            if reservation is not None:
                self.reservations.pop(order_id, None)
                client_id = reservation.get("clientOrderID")
                if client_id:
                    self._state.setdefault("clientOrderIDs", {}).pop(str(client_id), None)
            self._state.setdefault("audit", []).append({
                "at": _now(), "type": "cancelled", "orderID": order_id,
                "instrumentID": instrument_id, "demo": demo,
            })
            self._state["audit"] = self._state["audit"][-1000:]
            self._state["updatedAt"] = _now()
            self._write()
            return result

    async def audit(self) -> list[dict[str, Any]]:
        async with self._lock:
            return list(self._state.get("audit", []))
