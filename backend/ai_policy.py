"""Server-side authorization and safety checks for AI decisions."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
import math
from typing import Any, Iterable

try:
    from .ai_schema import AIDecision, AIConfig, AISnapshot, SchemaError
except ImportError:  # bundled backend modules are launched as scripts
    from ai_schema import AIDecision, AIConfig, AISnapshot, SchemaError


class PolicyError(ValueError):
    """A decision cannot be admitted to the order gateway."""


MIN_OPEN_WIN_RATE = 0.45
MIN_OPEN_RISK_REWARD_RATIO = 2.0
_OPEN_CANDLE_INTERVALS = ("5m", "15m", "1H", "4H")
_ACTIVE_PENDING_ORDER_STATES = {"live", "partially_filled", "waiting", "pending", "open", "queued"}
_TERMINAL_ORDER_STATES = {"filled", "canceled", "cancelled", "rejected", "failed", "expired", "mmp_canceled"}
_EXIT_REASON_CODES = {"THESIS_INVALIDATED", "ORDER_MISTAKE"}
_MIN_EXIT_REASON_LENGTH = 8


def _exposure_quantity(row: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Read a positive exposure quantity, returning ``None`` when unknown."""
    for key in keys:
        if key not in row:
            continue
        try:
            value = float(row[key])
        except (TypeError, ValueError):
            return None
        return abs(value) if math.isfinite(value) else None
    return None


def entry_exposure_error(account: dict[str, Any], instrument_id: str) -> str | None:
    """Reject a new AI entry while this contract already has exposure.

    Positions and active orders are exchange-owned facts. Missing fields on a
    matching row are treated as unknown so a malformed account response cannot
    silently authorize a duplicate order.
    """
    if not isinstance(account, dict):
        return "current account exposure is unavailable"
    quality = account.get("dataQuality")
    if isinstance(quality, dict) and quality.get("positionsAvailable") is False:
        return "current position state is unavailable"
    if account.get("positionsKnown") is False:
        return "current position state is unavailable"
    if not isinstance(account.get("positions"), list):
        return "current position state is unavailable"
    for row in account.get("positions", []):
        if not isinstance(row, dict):
            continue
        candidate = str(row.get("instrumentID") or row.get("instId") or "")
        if candidate != instrument_id:
            continue
        quantity = _exposure_quantity(row, ("quantity", "pos", "size"))
        if quantity is None:
            return f"{instrument_id} position state is unavailable"
        if quantity > 0:
            return f"{instrument_id} already has a position; evaluate close before opening"
    if not isinstance(account.get("pendingOrders"), list):
        return "pending order state is unavailable"
    for row in account.get("pendingOrders", []):
        if not isinstance(row, dict):
            continue
        candidate = str(row.get("instrumentID") or row.get("instId") or "")
        if candidate != instrument_id:
            continue
        status = str(row.get("status") or row.get("state") or "").lower()
        if status in _TERMINAL_ORDER_STATES:
            continue
        if status not in _ACTIVE_PENDING_ORDER_STATES:
            return f"{instrument_id} pending order state is unavailable"
        quantity = _exposure_quantity(row, ("quantity", "sz", "size"))
        if quantity is None:
            return f"{instrument_id} pending order state is unavailable"
        if quantity > 0:
            return f"{instrument_id} already has a pending order; evaluate cancel before opening"
    return None


def _is_current_snapshot(snapshot: AISnapshot) -> bool:
    """Return whether the snapshot carries the server execution metadata.

    Older library callers construct small, credential-free snapshots for
    analysis and policy tests. Runtime snapshots always include at least one
    of these fields, so only that path is subject to the strict execution
    gates below.
    """
    account = snapshot.account if isinstance(snapshot.account, dict) else {}
    freshness = snapshot.dataFreshness if isinstance(snapshot.dataFreshness, dict) else {}
    return bool(
        any(key in account for key in ("authenticated", "pendingOrdersKnown"))
        or "availability" in freshness
    )


def _finite_positive(value: Any) -> bool:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(parsed) and parsed > 0


def _open_snapshot_gate(snapshot: AISnapshot, instrument_id: str) -> str | None:
    """Check server-owned facts required before an entry can be admitted."""
    if not _is_current_snapshot(snapshot):
        return None
    account = snapshot.account if isinstance(snapshot.account, dict) else {}
    if account.get("authenticated") is not True:
        return "authenticated account data is unavailable"
    if not _finite_positive(account.get("availableEquityUSD")):
        return "available account equity is unavailable"
    loss_count = account.get("todayLossCount")
    if isinstance(loss_count, bool) or not isinstance(loss_count, (int, float)) or not math.isfinite(float(loss_count)) or float(loss_count) < 0:
        return "todayLossCount is unavailable or invalid"
    if account.get("pendingOrdersKnown") is not True or not isinstance(account.get("pendingOrders"), list):
        return "pending order state is unavailable"
    if not isinstance(account.get("positions"), list):
        return "current position state is unavailable"
    exposure_error = entry_exposure_error(account, instrument_id)
    if exposure_error:
        return exposure_error
    account_quality = account.get("dataQuality")
    if not isinstance(account_quality, dict):
        return "account data quality is unavailable"
    if account_quality.get("dailyBillsAvailable") is not True or account_quality.get("dailyBillsError"):
        return "daily account loss data is unavailable"
    if account_quality.get("pendingOrdersAvailable") is not True or account_quality.get("pendingOrdersError"):
        return "pending order data is unavailable"

    risk = snapshot.risk if isinstance(snapshot.risk, dict) else {}
    risk_quality = risk.get("dataQuality")
    if not isinstance(risk_quality, dict):
        return "risk data quality is unavailable"
    if risk_quality.get("accountRefreshError") or risk_quality.get("accountRefreshRetryable"):
        return "risk account refresh is unavailable"
    if not isinstance(risk.get("killSwitch"), bool):
        return "risk kill switch state is unavailable"
    daily_pnl = risk.get("dailyPnLPercent")
    if isinstance(daily_pnl, bool) or not isinstance(daily_pnl, (int, float)) or not math.isfinite(float(daily_pnl)):
        return "risk daily PnL is unavailable"
    if risk.get("killSwitch") or float(daily_pnl) <= -5:
        return "risk kill switch is active"

    ai = snapshot.ai if isinstance(snapshot.ai, dict) else {}
    availability = ai.get("tradingAvailability")
    item = availability.get(instrument_id) if isinstance(availability, dict) else None
    if not isinstance(item, dict) or item.get("available") is not True:
        return f"{instrument_id} trading availability is unavailable"

    ticker = snapshot.tickers.get(instrument_id) if isinstance(snapshot.tickers, dict) else None
    if not isinstance(ticker, dict) or not _finite_positive(ticker.get("last")):
        return f"{instrument_id} ticker data is unavailable"
    for interval in _OPEN_CANDLE_INTERVALS:
        rows = snapshot.candles.get(f"{instrument_id}/{interval}") if isinstance(snapshot.candles, dict) else None
        if not isinstance(rows, list) or not rows:
            return f"{instrument_id} {interval} candle data is unavailable"
        if not any(isinstance(row, dict) and row.get("confirmed") is True for row in rows):
            return f"{instrument_id} {interval} confirmed candle data is unavailable"
    return None


def _check_protection_geometry(
    *, instrument_id: str, direction: str | None, entry: Any,
    stop_loss: Any, take_profit: Any,
) -> str | None:
    """Validate optional stop/target prices against the actual entry price."""
    if direction not in {"long", "short"}:
        return None
    if stop_loss is None and take_profit is None:
        return None
    # Credential-free legacy market intents do not carry a server ticker. A
    # current runtime snapshot is rejected by _open_snapshot_gate before this
    # helper, so preserve that compatibility path here.
    if entry is None:
        return None
    if not _finite_positive(entry):
        return f"{instrument_id} entry price is unavailable"
    try:
        entry_value = float(entry)
        stop_value = float(stop_loss) if stop_loss is not None else None
        target_value = float(take_profit) if take_profit is not None else None
    except (TypeError, ValueError):
        return f"{instrument_id} protection prices are invalid"
    if stop_value is not None and (not math.isfinite(stop_value) or stop_value <= 0):
        return f"{instrument_id} stop loss price is invalid"
    if target_value is not None and (not math.isfinite(target_value) or target_value <= 0):
        return f"{instrument_id} take profit price is invalid"
    ordered = (
        (stop_value is None or stop_value < entry_value)
        and (target_value is None or entry_value < target_value)
        if direction == "long" else
        (target_value is None or target_value < entry_value)
        and (stop_value is None or entry_value < stop_value)
    )
    if not ordered:
        return f"assessment entry/stop loss/take profit conflict with direction: {instrument_id}"
    return None


def _take_profit_targets(item: Any) -> list[float]:
    """Return staged target prices from an assessment/decision-like object."""
    levels = getattr(item, "takeProfitLevels", None)
    if levels is None and isinstance(item, dict):
        levels = item.get("takeProfitLevels")
    prices: list[float] = []
    if isinstance(levels, list):
        for level in levels:
            if not isinstance(level, dict):
                continue
            try:
                value = float(level.get("price"))
            except (TypeError, ValueError):
                continue
            if math.isfinite(value) and value > 0:
                prices.append(value)
    if not prices:
        value = getattr(item, "takeProfitPrice", None)
        if value is None and isinstance(item, dict):
            value = item.get("takeProfitPrice")
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            parsed = 0.0
        if math.isfinite(parsed) and parsed > 0:
            prices.append(parsed)
    return prices


def _nearest_take_profit_target(direction: str | None, targets: list[float]) -> float | None:
    """Return the first target reached in the profitable direction.

    Risk/reward gates must use the least profitable staged tranche. Using the
    farthest target could admit a plan whose initial partial exit is below the
    configured minimum while a later target makes the aggregate ratio look
    acceptable.
    """
    if not targets:
        return None
    if direction == "long":
        return min(targets)
    if direction == "short":
        return max(targets)
    return None


def _check_staged_target_geometry(
    *, instrument_id: str, direction: str | None, entry: Any,
    stop_loss: Any, item: Any,
) -> str | None:
    """Validate every staged target, rather than only the outermost target.

    The execution layer submits one protection order for each staged level.
    Checking only ``max``/``min`` can therefore admit a mixed-direction plan
    whose first or intermediate target is already on the wrong side of the
    entry.  Each actual order target must satisfy the same geometry as a
    single-target plan.
    """
    targets = _take_profit_targets(item)
    if len(targets) <= 1:
        return None
    for target in targets:
        error = _check_protection_geometry(
            instrument_id=instrument_id,
            direction=direction,
            entry=entry,
            stop_loss=stop_loss,
            take_profit=target,
        )
        if error:
            return f"staged take-profit geometry is invalid: {error}"
    return None


def _validate_cancel_scope(snapshot: AISnapshot, instrument_id: str, order_id: str) -> str | None:
    """Require a cancel intent to name a current, cancellable order."""
    if not _is_current_snapshot(snapshot):
        return None
    account = snapshot.account if isinstance(snapshot.account, dict) else {}
    if account.get("pendingOrdersKnown") is not True or not isinstance(account.get("pendingOrders"), list):
        return "pending order state is unavailable"
    allowed_states = {"live", "partially_filled", "waiting", "pending", "open", "queued"}
    for row in account["pendingOrders"]:
        if not isinstance(row, dict):
            continue
        candidate = row.get("id") or row.get("orderID") or row.get("ordId")
        if str(candidate or "") != order_id:
            continue
        if str(row.get("instrumentID") or row.get("instId") or "") != instrument_id:
            return "cancel order does not belong to the selected instrument"
        status = str(row.get("status") or row.get("state") or "").lower()
        if status not in allowed_states:
            return "cancel order is no longer cancellable"
        return None
    return "cancel order is not a current pending order"


def _position_direction(row: dict[str, Any]) -> str | None:
    side = str(row.get("side") or row.get("positionSide") or row.get("posSide") or row.get("direction") or "").lower()
    if side in {"long", "short"}:
        return side
    quantity = _exposure_quantity(row, ("quantity", "pos", "size"))
    if quantity is None or quantity == 0:
        return None
    raw = row.get("quantity", row.get("pos", row.get("size")))
    try:
        return "short" if float(raw) < 0 else "long"
    except (TypeError, ValueError):
        return None


def _validate_close_scope(snapshot: AISnapshot, instrument_id: str, direction: str) -> str | None:
    """Require a close intent to name a current, same-direction position."""
    if not _is_current_snapshot(snapshot):
        return None
    account = snapshot.account if isinstance(snapshot.account, dict) else {}
    if account.get("positionsKnown") is not True or not isinstance(account.get("positions"), list):
        return "current position state is unavailable"
    for row in account["positions"]:
        if not isinstance(row, dict):
            continue
        candidate = str(row.get("instrumentID") or row.get("instId") or "")
        if candidate != instrument_id:
            continue
        quantity = _exposure_quantity(row, ("quantity", "pos", "size"))
        if quantity is None:
            return "current position state is unavailable"
        if quantity <= 0:
            continue
        if _position_direction(row) != direction:
            return "close direction does not match the current position"
        return None
    return "close target is not a current position"


def _validate_exit_reason(snapshot: AISnapshot, decision: AIDecision) -> str | None:
    """Require an auditable reason for current-account close/cancel actions."""
    if not _is_current_snapshot(snapshot):
        return None
    if decision.reasonCode not in _EXIT_REASON_CODES:
        return "current exit requires reasonCode THESIS_INVALIDATED or ORDER_MISTAKE"
    if len(decision.reason.strip()) < _MIN_EXIT_REASON_LENGTH:
        return f"current exit reason must be at least {_MIN_EXIT_REASON_LENGTH} characters"
    return None


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def snapshot_freshness(
    snapshot: AISnapshot, config: AIConfig, *, now: datetime | None = None,
) -> dict[str, Any]:
    """Measure age with the server clock; model time guesses are not inputs.

    Older snapshot providers omit the metadata, so their polling interval is
    the age limit. Malformed explicit metadata must not silently use that
    fallback, since it may otherwise admit an entry with untrusted timing.
    """
    clock = now or utc_now()
    if clock.tzinfo is None:
        clock = clock.replace(tzinfo=timezone.utc)
    clock = clock.astimezone(timezone.utc)
    result: dict[str, Any] = {
        "capturedAt": snapshot.capturedAt,
        "evaluatedAt": clock.isoformat(timespec="seconds").replace("+00:00", "Z"),
        "ageSeconds": None,
        "maxAgeSeconds": None,
        "isStale": None,
        "valid": False,
        "error": "",
    }
    try:
        captured = datetime.fromisoformat(snapshot.capturedAt.replace("Z", "+00:00"))
        if captured.tzinfo is None:
            raise ValueError("snapshot timestamp must include a timezone")
        captured = captured.astimezone(timezone.utc)
        age = (clock - captured).total_seconds()
        result["ageSeconds"] = age
        metadata = snapshot.dataFreshness
        if not isinstance(metadata, dict):
            raise ValueError("snapshot freshness metadata is invalid")
        max_age = metadata.get("maxAgeSeconds", config.decisionIntervalSeconds)
        if isinstance(max_age, bool) or not isinstance(max_age, (int, float)):
            raise ValueError("snapshot maxAgeSeconds must be a positive finite number")
        if not math.isfinite(max_age) or max_age <= 0:
            raise ValueError("snapshot maxAgeSeconds must be a positive finite number")
        result["maxAgeSeconds"] = max_age
        result["isStale"] = age > max_age
        if "capturedAt" in metadata:
            declared = datetime.fromisoformat(metadata["capturedAt"].replace("Z", "+00:00"))
            if declared.tzinfo is None or declared.astimezone(timezone.utc) != captured:
                raise ValueError("snapshot freshness capturedAt does not match the snapshot")
        # Preserve the existing tolerance for minor clock skew. A timestamp
        # more than five seconds ahead cannot authorize a new entry.
        if age < -5:
            raise ValueError("snapshot timestamp is in the future")
    except (AttributeError, TypeError, ValueError, OverflowError) as error:
        result["error"] = str(error)
        return result
    result["valid"] = True
    return result


@dataclass
class PolicyState:
    """Small in-memory state; callers may persist these values in their ledger."""

    seenDecisionIds: set[str] = field(default_factory=set)
    lastActionAt: dict[str, datetime] = field(default_factory=dict)
    lastDecision: AIDecision | None = None

    def remember(self, decision: AIDecision, at: datetime | None = None) -> None:
        timestamp = at or utc_now()
        self.seenDecisionIds.add(decision.decisionId)
        if decision.instrumentID:
            self.lastActionAt[decision.instrumentID] = timestamp
        self.lastDecision = decision


@dataclass(frozen=True)
class PolicyResult:
    accepted: bool
    decision: AIDecision
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"accepted": self.accepted, "decision": self.decision.to_dict(), "reason": self.reason}


def _reject(reason: str, snapshot_id: str = "", *, source: AIDecision | None = None) -> PolicyResult:
    decision = AIDecision.hold(snapshot_id, reason)
    if source is not None:
        decision = replace(decision, decisionId=source.decisionId, assessments=list(source.assessments))
    return PolicyResult(False, decision, reason)


def validate_decision(
    decision: AIDecision | dict[str, Any],
    snapshot: AISnapshot | dict[str, Any],
    config: AIConfig | dict[str, Any],
    state: PolicyState | None = None,
    *,
    now: datetime | None = None,
) -> PolicyResult:
    """Validate an AI intent. Invalid/untrusted input is converted to hold.

    This function is deliberately fail-closed and has no exchange side effects.
    """
    try:
        parsed_decision = decision if isinstance(decision, AIDecision) else AIDecision.from_dict(decision)
        parsed_snapshot = snapshot if isinstance(snapshot, AISnapshot) else AISnapshot.from_dict(snapshot)
        parsed_config = config if isinstance(config, AIConfig) else AIConfig.from_dict(config)
    except (SchemaError, TypeError, ValueError) as error:
        snapshot_id = snapshot.snapshotId if isinstance(snapshot, AISnapshot) else "unknown"
        return _reject(f"schema validation failed: {error}", snapshot_id)
    def reject(reason: str, snapshot_id: str = "") -> PolicyResult:
        return _reject(reason, snapshot_id, source=parsed_decision)

    state = state or PolicyState()
    clock = (now or utc_now()).astimezone(timezone.utc)

    if not parsed_config.enabled or parsed_config.mode in {"disabled", "halted"}:
        return reject("AI worker is disabled", parsed_snapshot.snapshotId)
    if parsed_decision.snapshotId != parsed_snapshot.snapshotId:
        return reject("decision snapshotId does not match current snapshot", parsed_snapshot.snapshotId)
    if parsed_decision.assessments:
        try:
            # Validate direct dataclass/custom-runner inputs as well as JSON.
            AIDecision.from_dict(parsed_decision.to_dict())
            parsed_decision.require_complete_assessments(parsed_snapshot.observed_instruments())
        except (SchemaError, TypeError, ValueError) as error:
            return reject(f"assessment validation failed: {error}", parsed_snapshot.snapshotId)
        relevant = parsed_decision.assessments if parsed_decision.action == "hold" else [
            item for item in parsed_decision.assessments
            if parsed_decision.action == "open" and item.instrumentID == parsed_decision.instrumentID
        ]
        for item in relevant:
            if item.entryEligible:
                if item.confidence < parsed_config.minimumConfidence:
                    return reject(f"assessment confidence is below configured minimum: {item.instrumentID}", parsed_snapshot.snapshotId)
                if item.winRate is None or item.winRate < MIN_OPEN_WIN_RATE:
                    return reject(f"assessment winRate must be >= 0.45: {item.instrumentID}", parsed_snapshot.snapshotId)
                if item.riskRewardRatio is None or item.riskRewardRatio < MIN_OPEN_RISK_REWARD_RATIO:
                    return reject(f"assessment riskRewardRatio must be >= 2.0: {item.instrumentID}", parsed_snapshot.snapshotId)
                if parsed_config.requireStopLoss and item.stopLossPrice is None:
                    return reject(f"eligible assessment requires a stop loss: {item.instrumentID}", parsed_snapshot.snapshotId)
            target_prices = _take_profit_targets(item)
            staged_geometry_error = _check_staged_target_geometry(
                instrument_id=item.instrumentID,
                direction=item.direction,
                entry=item.limitPrice,
                stop_loss=item.stopLossPrice,
                item=item,
            )
            if staged_geometry_error:
                return reject(staged_geometry_error, parsed_snapshot.snapshotId)
            target_price = _nearest_take_profit_target(item.direction, target_prices)
            if item.direction in {"long", "short"} and item.limitPrice is not None and item.stopLossPrice is not None and target_price is not None:
                geometry_error = _check_protection_geometry(
                    instrument_id=item.instrumentID, direction=item.direction,
                    entry=item.limitPrice, stop_loss=item.stopLossPrice,
                    take_profit=target_price,
                )
                if geometry_error:
                    return reject(geometry_error, parsed_snapshot.snapshotId)
    try:
        captured = parse_time(parsed_snapshot.capturedAt)
        valid_until = parse_time(parsed_decision.validUntil)
    except (TypeError, ValueError):
        return reject("snapshot or decision timestamp is invalid", parsed_snapshot.snapshotId)
    if captured > clock + timedelta(seconds=5):
        return reject("snapshot timestamp is in the future", parsed_snapshot.snapshotId)
    # A fail-closed hold is safe even when the CLI returned it at the edge of
    # its validity window. This also lets malformed/timeout fallbacks stop
    # opening without being rejected as a second error.
    if parsed_decision.action == "hold":
        # A named hold is surfaced as a decision about that specific contract.
        # Validate its scope before accepting that attribution. A whole-pool
        # hold (null ID) remains safe even when no instruments are available.
        if parsed_decision.instrumentID is not None:
            if parsed_decision.instrumentID not in parsed_snapshot.observed_instruments():
                return reject("hold instrument is outside the current observation set", parsed_snapshot.snapshotId)
            if parsed_decision.instrumentID not in parsed_config.allowedInstruments:
                return reject("hold instrument is not in the AI allowlist", parsed_snapshot.snapshotId)
        return PolicyResult(True, parsed_decision, "hold")
    if valid_until <= clock:
        return reject("decision has expired", parsed_snapshot.snapshotId)
    if parsed_decision.decisionId in state.seenDecisionIds:
        return reject("duplicate decisionId", parsed_snapshot.snapshotId)
    if parsed_decision.instrumentID is None:
        return reject("instrumentID is required for an action", parsed_snapshot.snapshotId)
    snapshot_instruments = {
        str(item.get("id") or item.get("instId"))
        for item in parsed_snapshot.instruments
        if isinstance(item, dict) and (item.get("id") or item.get("instId"))
    }
    if snapshot_instruments and parsed_decision.instrumentID not in snapshot_instruments:
        return reject("instrument is outside the current snapshot universe", parsed_snapshot.snapshotId)
    if parsed_config.allowedInstruments and parsed_decision.instrumentID not in parsed_config.allowedInstruments:
        return reject("instrument is not in the AI allowlist", parsed_snapshot.snapshotId)
    if parsed_decision.action == "open":
        availability = parsed_snapshot.ai.get("tradingAvailability")
        if availability is not None:
            item = availability.get(parsed_decision.instrumentID) if isinstance(availability, dict) else None
            if not isinstance(item, dict) or item.get("available") is not True:
                reason = item.get("reason") if isinstance(item, dict) else None
                return reject(f"{parsed_decision.instrumentID}：{reason or '当前账户可交易性未通过核验'}", parsed_snapshot.snapshotId)
        if parsed_decision.assessments:
            selected = next((item for item in parsed_decision.assessments if item.instrumentID == parsed_decision.instrumentID), None)
            if selected is None or not selected.entryEligible:
                return reject("open requires an entryEligible assessment for the selected contract", parsed_snapshot.snapshotId)
            if selected.direction != parsed_decision.direction:
                return reject("open direction does not match the selected assessment", parsed_snapshot.snapshotId)
            fields = ["winRate", "riskRewardRatio", "confidence", "stopLossPrice", "takeProfitPrice", "takeProfitLevels"]
            if parsed_decision.orderType == "limit" or parsed_decision.limitPrice is not None:
                fields.append("limitPrice")
            for key in fields:
                proposed = getattr(selected, key)
                action_value = getattr(parsed_decision, key)
                same = proposed is None and action_value is None
                if key == "takeProfitLevels" and proposed is not None and action_value is not None:
                    # Staged levels are structured arrays; applying
                    # math.isclose to them raises TypeError and used to turn
                    # malformed model output into an execution-path error.
                    same = proposed == action_value
                elif proposed is not None and action_value is not None:
                    same = math.isclose(proposed, action_value, rel_tol=1e-9, abs_tol=1e-12)
                if not same:
                    return reject(f"open {key} does not match the selected assessment", parsed_snapshot.snapshotId)
            selected_targets = _take_profit_targets(selected)
            selected_target = _nearest_take_profit_target(selected.direction, selected_targets)
            if selected.limitPrice is not None and selected.stopLossPrice is not None and selected_target is not None:
                gross_ratio = abs(selected_target - selected.limitPrice) / abs(selected.limitPrice - selected.stopLossPrice)
                # Fees may reduce the gross ratio, never improve it. Allow
                # small display/rounding differences without adding a new
                # threshold or demanding a TP from legacy entry intents.
                if selected.riskRewardRatio is not None and selected.riskRewardRatio > gross_ratio * 1.01 + .01:
                    return reject("open riskRewardRatio exceeds its proposed price setup", parsed_snapshot.snapshotId)
            entry_price = selected.limitPrice
            if parsed_decision.orderType == "market":
                ticker = parsed_snapshot.tickers.get(parsed_decision.instrumentID, {})
                entry_price = ticker.get("last") if isinstance(ticker, dict) else None
            geometry_error = _check_protection_geometry(
                instrument_id=selected.instrumentID, direction=selected.direction,
                entry=entry_price, stop_loss=selected.stopLossPrice,
                take_profit=selected_target,
            )
            if geometry_error:
                return reject(geometry_error, parsed_snapshot.snapshotId)
            staged_geometry_error = _check_staged_target_geometry(
                instrument_id=selected.instrumentID,
                direction=selected.direction,
                entry=entry_price,
                stop_loss=selected.stopLossPrice,
                item=selected,
            )
            if staged_geometry_error:
                return reject(staged_geometry_error, parsed_snapshot.snapshotId)
        freshness = snapshot_freshness(parsed_snapshot, parsed_config, now=clock)
        if not freshness["valid"]:
            return reject(str(freshness["error"]), parsed_snapshot.snapshotId)
        if freshness["isStale"]:
            return reject(
                f"snapshot has expired: age {freshness['ageSeconds']:g}s exceeds "
                f"maxAgeSeconds {freshness['maxAgeSeconds']:g}s",
                parsed_snapshot.snapshotId,
            )
    if parsed_decision.action == "open" and parsed_decision.confidence < parsed_config.minimumConfidence:
        return reject("confidence is below configured minimum", parsed_snapshot.snapshotId)
    if parsed_decision.action == "open" and not parsed_config.allowOpen:
        return reject("open actions are disabled", parsed_snapshot.snapshotId)
    if parsed_decision.action == "close" and not parsed_config.allowClose:
        return reject("close actions are disabled", parsed_snapshot.snapshotId)
    if parsed_decision.action == "cancel" and not parsed_config.allowCancel:
        return reject("cancel actions are disabled", parsed_snapshot.snapshotId)
    if parsed_decision.action == "open":
        if parsed_decision.winRate is None or parsed_decision.winRate < MIN_OPEN_WIN_RATE:
            return reject("winRate must be >= 0.45 for open actions", parsed_snapshot.snapshotId)
        if parsed_decision.riskRewardRatio is None or parsed_decision.riskRewardRatio < MIN_OPEN_RISK_REWARD_RATIO:
            return reject("riskRewardRatio must be >= 2.0 for open actions", parsed_snapshot.snapshotId)
        if parsed_decision.leverage is None or parsed_decision.leverage < 1 or parsed_decision.leverage > parsed_config.maxLeverage:
            return reject(f"leverage must be between 1 and maxLeverage ({parsed_config.maxLeverage:g}) for open actions", parsed_snapshot.snapshotId)
        # The account snapshot is server-generated. When a private account is
        # available it carries today's realized losing-trade count from OKX
        # bills; reaching the configured cap blocks only new entries.
        loss_count = parsed_snapshot.account.get("todayLossCount") if isinstance(parsed_snapshot.account, dict) else None
        if loss_count is None:
            return reject("todayLossCount is unavailable", parsed_snapshot.snapshotId)
        try:
            loss_count = float(loss_count)
        except (TypeError, ValueError):
            return reject("todayLossCount is invalid", parsed_snapshot.snapshotId)
        if not math.isfinite(loss_count) or loss_count < 0:
            return reject("todayLossCount is invalid", parsed_snapshot.snapshotId)
        if loss_count >= parsed_config.maxDailyLosses:
            return reject("maximum daily losing trades reached", parsed_snapshot.snapshotId)
        if parsed_decision.direction is None or parsed_decision.orderType is None:
            return reject("open requires direction and orderType", parsed_snapshot.snapshotId)
        if parsed_config.requireStopLoss and (parsed_decision.stopLossPrice is None or parsed_decision.stopLossPrice <= 0):
            return reject("open requires a stop loss", parsed_snapshot.snapshotId)
        if parsed_decision.orderType == "limit" and (parsed_decision.limitPrice is None or parsed_decision.limitPrice <= 0):
            return reject("limit orders require limitPrice", parsed_snapshot.snapshotId)
        entry_price = parsed_decision.limitPrice
        if parsed_decision.orderType == "market":
            ticker = parsed_snapshot.tickers.get(parsed_decision.instrumentID, {})
            entry_price = ticker.get("last") if isinstance(ticker, dict) else None
        geometry_error = _check_protection_geometry(
            instrument_id=parsed_decision.instrumentID, direction=parsed_decision.direction,
            entry=entry_price, stop_loss=parsed_decision.stopLossPrice,
            take_profit=(
                _nearest_take_profit_target(parsed_decision.direction, _take_profit_targets(parsed_decision))
                if _take_profit_targets(parsed_decision)
                else parsed_decision.takeProfitPrice
            ),
        )
        if geometry_error:
            return reject(geometry_error, parsed_snapshot.snapshotId)
        staged_geometry_error = _check_staged_target_geometry(
            instrument_id=parsed_decision.instrumentID,
            direction=parsed_decision.direction,
            entry=entry_price,
            stop_loss=parsed_decision.stopLossPrice,
            item=parsed_decision,
        )
        if staged_geometry_error:
            return reject(staged_geometry_error, parsed_snapshot.snapshotId)
    if parsed_decision.action == "close" and parsed_decision.direction is None:
        return reject("close requires direction", parsed_snapshot.snapshotId)
    if parsed_decision.action == "cancel" and not parsed_decision.orderID:
        return reject("cancel requires orderID", parsed_snapshot.snapshotId)
    if parsed_decision.action in {"close", "cancel"}:
        exit_reason_error = _validate_exit_reason(parsed_snapshot, parsed_decision)
        if exit_reason_error:
            return reject(exit_reason_error, parsed_snapshot.snapshotId)
    if parsed_decision.action == "close":
        close_error = _validate_close_scope(parsed_snapshot, parsed_decision.instrumentID, parsed_decision.direction)
        if close_error:
            return reject(close_error, parsed_snapshot.snapshotId)
    if parsed_decision.action == "cancel":
        cancel_error = _validate_cancel_scope(parsed_snapshot, parsed_decision.instrumentID, parsed_decision.orderID)
        if cancel_error:
            return reject(cancel_error, parsed_snapshot.snapshotId)
    if parsed_decision.action == "open":
        gate_error = _open_snapshot_gate(parsed_snapshot, parsed_decision.instrumentID)
        if gate_error:
            return reject(gate_error, parsed_snapshot.snapshotId)
        # Runtime entries must carry the complete per-contract analysis
        # produced for this snapshot. Without this gate a caller could bypass
        # the assessment-to-intent consistency checks by submitting only
        # top-level prices and confidence. Credential-free historical callers
        # remain compatible because they do not carry execution metadata.
        if _is_current_snapshot(parsed_snapshot) and not parsed_decision.assessments:
            return reject("current entry requires complete per-contract assessments", parsed_snapshot.snapshotId)
    return PolicyResult(True, parsed_decision, "accepted")


def record_decision(state: PolicyState, result: PolicyResult, *, at: datetime | None = None) -> None:
    """Record an executed entry for the worker's decision history."""
    if result.accepted and result.decision.action == "open":
        state.remember(result.decision, at)


# Explicit alias for integrations that use the more descriptive name.
validate_ai_decision = validate_decision
