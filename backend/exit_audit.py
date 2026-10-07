"""Read-only reconciliation for exchange-native OCO exits.

OKX native OCO orders can close a position without passing through the
service's reduce-only order path.  This module only reads exchange history
and records an exit when the history row has a durable link to a locally
tracked entry.  Instrument and time alone are deliberately insufficient.
"""

from __future__ import annotations

import json
from typing import Any, Awaitable, Callable


PrivateRequest = Callable[..., Awaitable[dict[str, Any]]]


def _text(row: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return None


def _number(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        try:
            value = float(row.get(name))
        except (TypeError, ValueError):
            continue
        if value == value and value not in (float("inf"), float("-inf")) and value > 0:
            return value
    return None


def _signed_number(row: dict[str, Any], *names: str) -> float | None:
    for name in names:
        try:
            value = float(row.get(name))
        except (TypeError, ValueError):
            continue
        if value == value and value not in (float("inf"), float("-inf")):
            return value
    return None


def _all_ids(row: dict[str, Any], *names: str) -> list[str]:
    values: list[str] = []
    for name in names:
        value = row.get(name)
        if isinstance(value, list):
            values.extend(str(item).strip() for item in value if str(item).strip())
        elif value is not None and str(value).strip():
            # OKX has returned both a JSON array and a comma-separated string
            # for order ID lists across endpoint versions.
            raw = str(value).strip()
            if raw.startswith("["):
                try:
                    decoded = json.loads(raw)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, list):
                    values.extend(str(item).strip() for item in decoded if str(item).strip())
                    continue
            values.extend(item.strip() for item in raw.split(",") if item.strip())
    return list(dict.fromkeys(values))


def _terminal(row: dict[str, Any]) -> bool:
    state = (_text(row, "state", "status") or "").lower()
    actual_side = (_text(row, "actualSide") or "").lower()
    if actual_side in {"tp", "sl"}:
        return True
    return state in {"effective", "partially_effective", "filled", "closed", "triggered"}


def _match_entry(row: dict[str, Any], entries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Match only exact client/order identifiers, never instrument/time."""
    row_instrument = _text(row, "instId", "instrumentID")
    attached = _text(row, "attachAlgoClOrdId", "attachAlgoClOrdID")
    algo_client = _text(row, "algoClOrdId", "algoClOrdID", "clOrdId", "clOrdID")
    child_ids = set(_all_ids(row, "ordIdList", "ordIDList", "ordId", "ordID", "childOrderIDs"))
    for entry in entries:
        entry_instrument = str(entry.get("instrumentID") or "").strip()
        if row_instrument and entry_instrument and row_instrument != entry_instrument:
            continue
        client_id = str(entry.get("clientOrderID") or "").strip()
        order_id = str(entry.get("orderID") or "").strip()
        if (client_id and (attached == client_id or algo_client == client_id)) or (order_id and order_id in child_ids):
            return entry
    return None


async def _fill_details(
    row: dict[str, Any],
    *,
    instrument_id: str,
    private_request: PrivateRequest,
) -> tuple[float | None, float | None, float | None, str | None]:
    """Read execution fields carried by the algorithm history row.

    The background reconciler intentionally stays a single low-frequency
    history request. Missing execution fields remain ``null`` in the audit;
    they are never reconstructed from a ticker or an unrelated position.
    """
    fill_price = _number(row, "fillPrice", "fillPx", "actualPx", "avgPx", "px")
    quantity = _number(row, "actualSz", "fillSz", "fillQty", "sz")
    pnl = _signed_number(row, "realizedPnl", "realizedPnL", "pnl")
    fill_time = _text(row, "fillTime", "fillTs", "actualTriggerTime", "triggerTime")
    return fill_price, quantity, pnl, fill_time


async def _child_order_details(
    row: dict[str, Any],
    *,
    instrument_id: str,
    private_request: PrivateRequest,
) -> dict[str, Any]:
    """Verify that an OCO child order really executed.

    OKX can leave an OCO history row in ``effective`` state after the trigger
    fires even when the generated reduce-only child is cancelled.  The parent
    history row is therefore not execution evidence.  Query every linked child
    order and only treat a filled state or positive accumulated fill as proof.
    """
    child_ids = _all_ids(row, "ordIdList", "ordIDList", "ordId", "ordID", "childOrderIDs")
    result: dict[str, Any] = {
        "verified": False,
        "orders": [],
        "fillPrice": None,
        "quantity": None,
    }
    if not instrument_id or not child_ids:
        return result
    for child_id in child_ids:
        try:
            payload = await private_request(
                "GET", "/trade/order",
                params={"instId": instrument_id, "ordId": child_id},
            )
        except Exception as error:
            result["orders"].append({"orderID": child_id, "state": "lookup-error", "error": str(error)})
            continue
        rows = payload.get("data") if isinstance(payload, dict) else None
        details = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
        if details is None:
            result["orders"].append({"orderID": child_id, "state": "lookup-empty"})
            continue
        state = (_text(details, "state", "status") or "unknown").lower()
        quantity = _number(details, "accFillSz", "fillSz", "fillQty", "actualSz")
        fill_price = _number(details, "fillPx", "fillPrice", "avgPx", "actualPx")
        result["orders"].append({
            "orderID": child_id,
            "state": state,
            "accFillSz": quantity,
            "fillPrice": fill_price,
        })
        if state == "filled" or quantity is not None:
            result["verified"] = True
            result["fillPrice"] = fill_price
            result["quantity"] = quantity
            return result
    return result


async def _current_position_quantity(
    *,
    instrument_id: str,
    private_request: PrivateRequest,
) -> tuple[float | None, bool | None]:
    """Return current position size and whether the exchange confirms it open."""
    try:
        payload = await private_request(
            "GET", "/account/positions",
            params={"instType": "SWAP", "instId": instrument_id},
        )
    except Exception:
        return None, None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None, None
    quantities: list[float] = []
    matched = False
    for position in rows:
        if not isinstance(position, dict):
            continue
        if _text(position, "instId", "instrumentID") != instrument_id:
            continue
        matched = True
        quantity = _signed_number(position, "pos", "quantity", "positionQty", "sz")
        if quantity is None:
            # A matching but malformed row is not proof that the position is
            # flat. Keep the exit unsettled until a later account read is
            # parseable.
            return None, None
        quantities.append(quantity)
    if not quantities:
        return (0.0, False) if not matched else (None, None)
    total = sum(abs(value) for value in quantities)
    return total, total > 0


def _protection_failure_event(
    row: dict[str, Any],
    entry: dict[str, Any],
    *,
    instrument_id: str,
    child: dict[str, Any],
    position_quantity: float | None,
    position_open: bool | None,
) -> dict[str, Any] | None:
    """Build a durable event when an OCO trigger has no executed child."""
    actual_side = (_text(row, "actualSide") or "").lower()
    if actual_side not in {"tp", "sl"} or not instrument_id:
        return None
    trigger_names = (
        ("actualTriggerPx", "triggerPx", "triggerPrice", "tpTriggerPx", "tpTriggerPrice")
        if actual_side == "tp"
        else ("actualTriggerPx", "triggerPx", "triggerPrice", "slTriggerPx", "slTriggerPrice")
    )
    trigger = _number(row, *trigger_names)
    trigger_type = _text(row, "actualTriggerPxType", "triggerPxType", "tpTriggerPxType", "slTriggerPxType")
    trigger_time = _text(row, "actualTriggerTime", "triggerTime", "ts")
    algo_id = _text(row, "algoId", "algoID")
    child_ids = _all_ids(row, "ordIdList", "ordIDList", "ordId", "ordID", "childOrderIDs")
    side_label = "止盈(TP)" if actual_side == "tp" else "止损(SL)"
    position_label = (
        str(position_quantity) if position_quantity is not None else "unknown"
    )
    state_label = ", ".join(
        f"{order.get('orderID')}: {order.get('state')}"
        for order in child.get("orders", [])
        if isinstance(order, dict)
    ) or "unknown"
    return {
        "type": "native-protection-failure",
        "source": "exchange-native-oco",
        "instrumentID": instrument_id,
        "entryOrderID": str(entry.get("orderID") or "") or None,
        "clientOrderID": str(entry.get("clientOrderID") or "") or None,
        "decisionID": entry.get("decisionID"),
        "strategyID": entry.get("strategyID"),
        "algoID": algo_id,
        "childOrderIDs": child_ids,
        "actualSide": actual_side,
        "reason": "native_protection_child_not_filled",
        "triggerPrice": trigger,
        "triggerPriceType": trigger_type,
        "triggerTime": trigger_time,
        "childOrders": child.get("orders", []),
        "positionQuantity": position_quantity,
        "positionOpen": position_open,
        "state": _text(row, "state", "status") or "effective",
        "message": (
            f"原生 OCO {side_label} 已触发，但平仓子单未成交：{instrument_id} / "
            f"触发价 {trigger if trigger is not None else 'unknown'} / 子单 {state_label} / "
            f"当前持仓 {position_label}；未标记为平仓成功"
        ),
    }


async def _closed_position_details(
    row: dict[str, Any], *, instrument_id: str, private_request: PrivateRequest,
) -> dict[str, Any] | None:
    """Attach the closest authenticated positions-history row to an OCO event."""
    trigger_ms = _number(row, "actualTriggerTime", "triggerTime", "uTime", "cTime")
    if trigger_ms is None:
        return None
    try:
        payload = await private_request(
            "GET", "/account/positions-history",
            params={"instType": "SWAP", "instId": instrument_id, "limit": "100"},
        )
    except Exception:
        return None
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return None
    candidates: list[tuple[float, dict[str, Any]]] = []
    for candidate in rows:
        if not isinstance(candidate, dict) or _text(candidate, "instId", "instrumentID") != instrument_id:
            continue
        closed_ms = _number(candidate, "uTime", "closeTime", "cTime")
        if closed_ms is None or abs(closed_ms - trigger_ms) > 120_000:
            continue
        candidates.append((abs(closed_ms - trigger_ms), candidate))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _event_from_row(row: dict[str, Any], entry: dict[str, Any], *, fill_price: float | None,
                    quantity: float | None, pnl: float | None, fill_time: str | None,
                    closed: dict[str, Any] | None = None,
                    position_quantity: float | None = None,
                    position_open: bool | None = None) -> dict[str, Any] | None:
    instrument_id = _text(row, "instId", "instrumentID") or str(entry.get("instrumentID") or "")
    actual_side = (_text(row, "actualSide") or "").lower()
    if actual_side not in {"tp", "sl"} or not instrument_id:
        return None
    trigger_names = (
        ("actualTriggerPx", "triggerPx", "triggerPrice", "tpTriggerPx", "tpTriggerPrice")
        if actual_side == "tp"
        else ("actualTriggerPx", "triggerPx", "triggerPrice", "slTriggerPx", "slTriggerPrice")
    )
    trigger = _number(row, *trigger_names)
    trigger_type = _text(row, "actualTriggerPxType", "triggerPxType", "tpTriggerPxType", "slTriggerPxType")
    trigger_time = _text(row, "actualTriggerTime", "triggerTime", "ts") or fill_time
    algo_id = _text(row, "algoId", "algoID")
    child_ids = _all_ids(row, "ordIdList", "ordIDList", "ordId", "ordID", "childOrderIDs")
    position_id = _text(closed or {}, "posId", "positionID")
    history_entry = _number(closed or {}, "openAvgPx", "openPrice", "entryPrice")
    history_exit = _number(closed or {}, "closeAvgPx", "closePrice", "exitPrice")
    history_quantity = _number(closed or {}, "closeTotalPos", "closeSz", "closeQty")
    history_pnl = _signed_number(closed or {}, "realizedPnl", "realizedPnL", "pnl")
    fill_price = fill_price or history_exit
    quantity = quantity or history_quantity
    pnl = pnl if pnl is not None else history_pnl
    # A filled child can represent one tranche of a staged take-profit.  Only
    # an explicit flat position proves that the complete entry is closed and
    # allows the gateway to release its entry reservation.  Unknown position
    # state is deliberately kept non-settled so a transient account failure
    # cannot permit a duplicate entry.
    event_type = (
        "exit-settled" if position_open is False
        else "exit-partial" if position_open is True
        else "exit-fill-unconfirmed"
    )
    event = {
        "type": event_type,
        "source": "exchange-native-oco",
        "instrumentID": instrument_id,
        "entryOrderID": str(entry.get("orderID") or "") or None,
        "clientOrderID": str(entry.get("clientOrderID") or "") or None,
        "decisionID": entry.get("decisionID"),
        "strategyID": entry.get("strategyID"),
        "positionID": position_id,
        "algoID": algo_id,
        "childOrderIDs": child_ids,
        "actualSide": actual_side,
        "reason": "take-profit" if actual_side == "tp" else "stop-loss",
        "triggerPrice": trigger,
        "triggerPriceType": trigger_type,
        "triggerTime": trigger_time,
        "actualQty": quantity,
        "fillPrice": fill_price,
        "realizedPnL": pnl,
        "entryPrice": history_entry,
        "exitPrice": history_exit,
        "state": _text(row, "state", "status") or "effective",
        "positionQuantity": position_quantity,
        "positionOpen": position_open,
    }
    side_label = "止盈(TP)" if actual_side == "tp" else "止损(SL)"
    event["message"] = (
        f"原生 OCO {'平仓完成' if event_type == 'exit-settled' else '分批止盈' if event_type == 'exit-partial' else '成交待确认'}：{instrument_id} / {side_label} / 触发价 {trigger if trigger is not None else 'unknown'} "
        f"/ 触发类型 {trigger_type or 'unknown'} / 数量 {quantity if quantity is not None else 'unknown'} "
        f"/ 成交价 {fill_price if fill_price is not None else 'unknown'} "
        f"/ PnL {pnl if pnl is not None else 'unknown'} / positionID {position_id or 'unknown'} / entryOrderID {entry.get('orderID') or 'unknown'} "
        f"/ algoID {algo_id or 'unknown'}"
    )
    return event


async def sync_native_protection_exits(
    gateway: Any,
    *,
    demo: bool,
    private_request: PrivateRequest,
) -> list[dict[str, Any]]:
    """Read OCO history and persist newly matched exits with durable dedupe."""
    entries = await gateway.tracked_entries(demo=demo)
    if not entries:
        return []
    payload = await private_request(
        "GET", "/trade/orders-algo-history",
        params={"instType": "SWAP", "ordType": "oco", "state": "effective", "limit": "100"},
    )
    rows = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return []
    created: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not _terminal(row):
            continue
        entry = _match_entry(row, entries)
        if entry is None:
            # Same instrument is intentionally not enough to identify the
            # entry. Keep a diagnostic, but never call it a settled TP/SL.
            actual_side = (_text(row, "actualSide") or "").lower()
            if actual_side in {"tp", "sl"}:
                unknown = {
                    "type": "native-protection-unknown",
                    "source": "exchange-native-oco",
                    "reason": "native_protection_unknown",
                    "instrumentID": _text(row, "instId", "instrumentID"),
                    "actualSide": actual_side,
                    "algoID": _text(row, "algoId", "algoID"),
                    "attachAlgoClOrdId": _text(row, "attachAlgoClOrdId", "attachAlgoClOrdID"),
                    "triggerTime": _text(row, "actualTriggerTime", "triggerTime", "ts"),
                    "message": "原生 OCO 已触发，但无法与本地开仓强关联，未标记为止盈/止损",
                }
                identity = (
                    str(unknown.get("algoID") or "")
                    or str(unknown.get("attachAlgoClOrdId") or "")
                    or ",".join(_all_ids(row, "ordIdList", "ordIDList", "ordId", "ordID"))
                    or "unknown"
                )
                event_key = ":".join([
                    "oco-unknown", "demo" if demo else "live", identity,
                    actual_side, str(unknown.get("triggerTime") or "unknown"),
                ])
                await gateway.record_event(unknown, event_key=event_key)
            continue
        instrument_id = _text(row, "instId", "instrumentID") or str(entry.get("instrumentID") or "")
        child = await _child_order_details(
            row, instrument_id=instrument_id, private_request=private_request,
        )
        if not child.get("verified"):
            position_quantity, position_open = await _current_position_quantity(
                instrument_id=instrument_id, private_request=private_request,
            )
            failure = _protection_failure_event(
                row,
                entry,
                instrument_id=instrument_id,
                child=child,
                position_quantity=position_quantity,
                position_open=position_open,
            )
            if failure is not None:
                identity = str(failure.get("algoID") or "") or ",".join(failure.get("childOrderIDs") or [])
                event_key = ":".join([
                    "oco-failure", "demo" if demo else "live", identity or "unknown",
                    str(failure.get("actualSide") or "unknown"),
                    str(failure.get("triggerTime") or "unknown"),
                    str(failure.get("entryOrderID") or "unknown"),
                ])
                if await gateway.record_event(failure, event_key=event_key):
                    failure = {**failure, "eventKey": event_key}
                    created.append(failure)
            continue
        fill_price, quantity, pnl, fill_time = await _fill_details(
            row, instrument_id=instrument_id, private_request=private_request,
        )
        fill_price = child.get("fillPrice") or fill_price
        quantity = child.get("quantity") or quantity
        # Never infer full account closure from the child order alone.  A
        # staged TP child is expected to fill while other tranches remain.
        position_quantity, position_open = await _current_position_quantity(
            instrument_id=instrument_id, private_request=private_request,
        )
        closed = await _closed_position_details(row, instrument_id=instrument_id, private_request=private_request)
        event = _event_from_row(
            row, entry, fill_price=fill_price, quantity=quantity, pnl=pnl,
            fill_time=fill_time, closed=closed,
            position_quantity=position_quantity, position_open=position_open,
        )
        if event is None:
            continue
        algo_id = str(event.get("algoID") or "")
        identity = algo_id or _text(row, "attachAlgoClOrdId", "algoClOrdId", "clOrdId") or ",".join(event.get("childOrderIDs") or [])
        event_key = ":".join([
            "oco", "demo" if demo else "live", identity or "unknown",
            str(event.get("actualSide") or "unknown"), str(event.get("type") or "unknown"),
            str(event.get("triggerTime") or "unknown"),
            str(event.get("entryOrderID") or "unknown"),
        ])
        if await gateway.record_event(event, event_key=event_key):
            event = {**event, "eventKey": event_key}
            created.append(event)
    return created
