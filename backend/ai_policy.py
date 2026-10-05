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
            if item.direction in {"long", "short"} and all(
                value is not None for value in (item.limitPrice, item.stopLossPrice, item.takeProfitPrice)
            ):
                ordered = (
                    item.stopLossPrice < item.limitPrice < item.takeProfitPrice
                    if item.direction == "long" else item.takeProfitPrice < item.limitPrice < item.stopLossPrice
                )
                if not ordered:
                    return reject(f"assessment entry/stop loss/take profit conflict with direction: {item.instrumentID}", parsed_snapshot.snapshotId)
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
            fields = ["winRate", "riskRewardRatio", "confidence", "stopLossPrice", "takeProfitPrice"]
            if parsed_decision.orderType == "limit" or parsed_decision.limitPrice is not None:
                fields.append("limitPrice")
            for key in fields:
                proposed = getattr(selected, key)
                action_value = getattr(parsed_decision, key)
                same = proposed is None and action_value is None
                if proposed is not None and action_value is not None:
                    same = math.isclose(proposed, action_value, rel_tol=1e-9, abs_tol=1e-12)
                if not same:
                    return reject(f"open {key} does not match the selected assessment", parsed_snapshot.snapshotId)
            if all(value is not None for value in (selected.limitPrice, selected.stopLossPrice, selected.takeProfitPrice)):
                gross_ratio = abs(selected.takeProfitPrice - selected.limitPrice) / abs(selected.limitPrice - selected.stopLossPrice)
                # Fees may reduce the gross ratio, never improve it. Allow
                # small display/rounding differences without adding a new
                # threshold or demanding a TP from legacy entry intents.
                if selected.riskRewardRatio is not None and selected.riskRewardRatio > gross_ratio * 1.01 + .01:
                    return reject("open riskRewardRatio exceeds its proposed price setup", parsed_snapshot.snapshotId)
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
    if parsed_decision.action == "close" and parsed_decision.direction is None:
        return reject("close requires direction", parsed_snapshot.snapshotId)
    if parsed_decision.action == "cancel" and not parsed_decision.orderID:
        return reject("cancel requires orderID", parsed_snapshot.snapshotId)
    if parsed_decision.action == "open":
        previous = state.lastActionAt.get(parsed_decision.instrumentID)
        if previous is not None and clock - previous < timedelta(seconds=parsed_config.cooldownSeconds):
            return reject("instrument is in cooldown", parsed_snapshot.snapshotId)
    return PolicyResult(True, parsed_decision, "accepted")


def record_decision(state: PolicyState, result: PolicyResult, *, at: datetime | None = None) -> None:
    """Record only admitted actions; hold decisions do not consume cooldown."""
    if result.accepted and result.decision.action != "hold":
        state.remember(result.decision, at)


# Explicit alias for integrations that use the more descriptive name.
validate_ai_decision = validate_decision
