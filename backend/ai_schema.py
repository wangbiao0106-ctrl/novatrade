"""Strict, dependency-light contracts for the AI trading worker.

The AI process never receives an OKX request shape.  It receives an
``AISnapshot`` and returns an ``AIDecision`` intent, which the server validates
before handing it to the order gateway.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import math
import re
from typing import Any, ClassVar, Literal, Mapping


Action = Literal["hold", "open", "close", "cancel"]
Direction = Literal["long", "short"]
OrderType = Literal["market", "limit"]
RunMode = Literal["disabled", "shadow", "demo-active", "live-armed", "halted"]
AIProvider = Literal["codex", "deepseek-harness"]
_CONTRACT_ID_RE = re.compile(r"^[A-Z0-9]+-USDT-SWAP$")
FIXED_AI_MODEL = "gpt-6-luna"
FIXED_AI_REASONING_EFFORT = "medium"
# Retained as a compatibility field for older persisted strategy configs. AI
# entries are gated by current exchange positions and pending orders; a fixed
# time-based same-contract cooldown is no longer enforced.
AI_ENTRY_COOLDOWN_SECONDS = 0
# Four parallel analysis groups can each carry tens of thousands of tokens.
# Keep enough wall-clock budget for provider queueing and model processing.
DEFAULT_CLI_TIMEOUT_SECONDS = 90.0
LEGACY_DEFAULT_CLI_TIMEOUT_SECONDS = 45.0


class SchemaError(ValueError):
    """Raised when an untrusted mapping is not an exact schema instance."""


def _mapping(value: Any, name: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaError(f"{name} must be an object")
    return value


def _required(row: Mapping[str, Any], key: str, name: str) -> Any:
    if key not in row:
        raise SchemaError(f"{name}.{key} is required")
    return row[key]


def _str(value: Any, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise SchemaError(f"{name} must be a non-empty string")
    return value.strip()


def _number(value: Any, name: str, *, minimum: float | None = None, maximum: float | None = None) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaError(f"{name} must be a finite number")
    result = float(value)
    if not math.isfinite(result):
        raise SchemaError(f"{name} must be finite")
    if minimum is not None and result < minimum:
        raise SchemaError(f"{name} must be >= {minimum}")
    if maximum is not None and result > maximum:
        raise SchemaError(f"{name} must be <= {maximum}")
    return result


def _integer(value: Any, name: str, *, minimum: int | None = None, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SchemaError(f"{name} must be an integer")
    if minimum is not None and value < minimum:
        raise SchemaError(f"{name} must be >= {minimum}")
    if maximum is not None and value > maximum:
        raise SchemaError(f"{name} must be <= {maximum}")
    return value


def _optional_number(value: Any, name: str, **limits: float) -> float | None:
    return None if value is None else _number(value, name, **limits)


def _iso(value: Any, name: str) -> str:
    result = _str(value, name)
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError as error:
        raise SchemaError(f"{name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise SchemaError(f"{name} must include a timezone")
    return result


def _object(value: Any, name: str) -> dict[str, Any]:
    return dict(_mapping(value, name))


def _list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise SchemaError(f"{name} must be an array")
    return value


def normalize_contract_ids(value: Any, name: str, *, allow_empty: bool = True) -> list[str]:
    """Normalize a list of exact linear USDT swap IDs.

    The AI must never receive a category such as ``热门`` or an unqualified
    coin name.  Canonical IDs are upper-case and include the full
    ``*-USDT-SWAP`` suffix used by OKX.
    """
    items = _list(value, name)
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, str) or not item.strip():
            raise SchemaError(f"{name} must contain non-empty contract IDs")
        instrument = item.strip().upper()
        if not _CONTRACT_ID_RE.fullmatch(instrument):
            raise SchemaError(f"{name} must contain exact *-USDT-SWAP contract IDs")
        if instrument not in seen:
            result.append(instrument)
            seen.add(instrument)
    if not allow_empty and not result:
        raise SchemaError(f"{name} must contain at least one contract ID")
    return result


def _reject_extra(row: Mapping[str, Any], allowed: set[str], name: str) -> None:
    extra = sorted(set(row) - allowed)
    if extra:
        raise SchemaError(f"{name} contains unsupported fields: {', '.join(extra)}")


def _model_name(value: Any, name: str, *, allowed: set[str]) -> str:
    """Validate an automatic route model without permitting an Astra fallback."""
    result = _str(value, name)
    if result not in allowed:
        options = ", ".join(sorted(allowed))
        raise SchemaError(f"{name} must be one of: {options}")
    return result


def _reasoning_effort(value: Any, name: str, *, allowed: set[str]) -> str:
    result = _str(value, name)
    if result not in allowed:
        options = ", ".join(sorted(allowed))
        raise SchemaError(f"{name} must be one of: {options}")
    return result


def _take_profit_levels(value: Any, name: str) -> list[dict[str, float]] | None:
    """Validate optional staged take-profit targets.

    A level is deliberately a small, explicit object rather than a positional
    tuple so provider output remains readable and can be audited.  The sum of
    ``quantityPercent`` values must be exactly one position (within a small
    decimal tolerance for JSON round-off).
    """
    if value is None:
        return None
    rows = _list(value, name)
    if not 1 <= len(rows) <= 4:
        raise SchemaError(f"{name} must contain between 1 and 4 levels")
    result: list[dict[str, float]] = []
    total = 0.0
    seen_prices: set[float] = set()
    for index, item in enumerate(rows):
        row = _mapping(item, f"{name}[{index}]")
        _reject_extra(row, {"price", "quantityPercent"}, f"{name}[{index}]")
        price = _number(_required(row, "price", f"{name}[{index}]"), f"{name}[{index}].price", minimum=0)
        percent = _number(
            _required(row, "quantityPercent", f"{name}[{index}]"),
            f"{name}[{index}].quantityPercent", minimum=0, maximum=100,
        )
        if price <= 0 or percent <= 0:
            raise SchemaError(f"{name}[{index}] price and quantityPercent must be positive")
        if price in seen_prices:
            raise SchemaError(f"{name} must not contain duplicate target prices")
        seen_prices.add(price)
        total += percent
        result.append({"price": price, "quantityPercent": percent})
    if not math.isclose(total, 100.0, rel_tol=0.0, abs_tol=1e-6):
        raise SchemaError(f"{name} quantityPercent values must sum to 100")
    return result


@dataclass(frozen=True)
class AIInstrumentAssessment:
    """A proposed setup for one observed contract, never an order receipt."""

    instrumentID: str
    direction: Literal["long", "short", "neutral"]
    winRate: float | None
    riskRewardRatio: float | None
    limitPrice: float | None
    stopLossPrice: float | None
    takeProfitPrice: float | None
    takeProfitLevels: list[dict[str, float]] | None
    confidence: float
    entryEligible: bool
    unmetConditions: list[str]
    reason: str

    _FIELDS: ClassVar[set[str]] = {
        "instrumentID", "direction", "winRate", "riskRewardRatio", "limitPrice",
        "stopLossPrice", "takeProfitPrice", "confidence", "entryEligible",
        "takeProfitLevels", "unmetConditions", "reason",
    }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AIInstrumentAssessment":
        row = _mapping(value, "assessment")
        _reject_extra(row, cls._FIELDS, "assessment")
        for key in cls._FIELDS:
            # takeProfitLevels was added after the original single-target
            # contract.  Absence remains valid for persisted/legacy decisions.
            if key != "takeProfitLevels":
                _required(row, key, "assessment")
        instrument = _str(row["instrumentID"], "assessment.instrumentID")
        if not _CONTRACT_ID_RE.fullmatch(instrument):
            raise SchemaError("assessment.instrumentID must be an exact *-USDT-SWAP contract ID")
        direction = _str(row["direction"], "assessment.direction")
        if direction not in {"long", "short", "neutral"}:
            raise SchemaError("assessment.direction is invalid")
        eligible = row["entryEligible"]
        if not isinstance(eligible, bool):
            raise SchemaError("assessment.entryEligible must be a boolean")
        unmet = [_str(item, "assessment.unmetConditions entry") for item in _list(row["unmetConditions"], "assessment.unmetConditions")]
        if eligible and (direction == "neutral" or unmet):
            raise SchemaError("an eligible assessment requires a trade direction and no unmetConditions")
        prices: dict[str, float | None] = {}
        for key in ("limitPrice", "stopLossPrice", "takeProfitPrice"):
            prices[key] = _optional_number(row[key], f"assessment.{key}", minimum=0)
            if prices[key] is not None and prices[key] <= 0:
                raise SchemaError(f"assessment.{key} must be positive or null")
        levels = _take_profit_levels(row.get("takeProfitLevels"), "assessment.takeProfitLevels")
        return cls(
            instrumentID=instrument, direction=direction,
            winRate=_optional_number(row["winRate"], "assessment.winRate", minimum=0, maximum=1),
            riskRewardRatio=_optional_number(row["riskRewardRatio"], "assessment.riskRewardRatio", minimum=0),
            **prices,
            takeProfitLevels=levels,
            confidence=_number(row["confidence"], "assessment.confidence", minimum=0, maximum=1),
            entryEligible=eligible, unmetConditions=unmet,
            reason=_str(row["reason"], "assessment.reason"),
        )


@dataclass(frozen=True)
class AIDecision:
    schemaVersion: int
    decisionId: str
    snapshotId: str
    action: Action
    instrumentID: str | None = None
    direction: Direction | None = None
    orderType: OrderType | None = None
    riskBudgetPercent: float | None = None
    limitPrice: float | None = None
    stopLossPrice: float | None = None
    takeProfitPrice: float | None = None
    takeProfitLevels: list[dict[str, float]] | None = None
    # The model chooses the leverage for each entry. Server policy caps it at
    # AIConfig.maxLeverage before the order gateway is reached.
    leverage: float | None = None
    # Model-estimated signal quality. These fields are required in the wire
    # contract; they may be null for non-entry actions and are enforced by
    # policy for open actions.
    winRate: float | None = None
    riskRewardRatio: float | None = None
    confidence: float = 0.0
    validUntil: str = ""
    reasonCode: str = ""
    reason: str = ""
    orderID: str | None = None
    # Missing only in historical records and compatibility runners. Newly
    # generated decisions must cover every observed contract exactly once.
    assessments: list[AIInstrumentAssessment] = field(default_factory=list)

    _FIELDS: ClassVar[set[str]] = {
        "schemaVersion", "decisionId", "snapshotId", "action", "instrumentID", "direction",
        "orderType", "riskBudgetPercent", "limitPrice", "stopLossPrice", "takeProfitPrice",
        "takeProfitLevels", "winRate", "riskRewardRatio", "leverage", "confidence", "validUntil", "reasonCode", "reason", "orderID", "assessments",
    }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AIDecision":
        row = _mapping(value, "decision")
        _reject_extra(row, cls._FIELDS, "decision")
        version = _required(row, "schemaVersion", "decision")
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise SchemaError("decision.schemaVersion must be 1")
        action = _str(_required(row, "action", "decision"), "decision.action")
        if action not in {"hold", "open", "close", "cancel"}:
            raise SchemaError("decision.action is invalid")
        decision_id = _str(_required(row, "decisionId", "decision"), "decision.decisionId")
        snapshot_id = _str(_required(row, "snapshotId", "decision"), "decision.snapshotId")
        instrument = None if row.get("instrumentID") is None else _str(row.get("instrumentID"), "decision.instrumentID")
        if instrument is not None and not _CONTRACT_ID_RE.fullmatch(instrument):
            raise SchemaError("decision.instrumentID must be an exact *-USDT-SWAP contract ID")
        direction = row.get("direction")
        if direction is not None:
            direction = _str(direction, "decision.direction")
            if direction not in {"long", "short"}:
                raise SchemaError("decision.direction is invalid")
        order_type = row.get("orderType")
        if order_type is not None:
            order_type = _str(order_type, "decision.orderType")
            if order_type not in {"market", "limit"}:
                raise SchemaError("decision.orderType is invalid")
        valid_until = _iso(_required(row, "validUntil", "decision"), "decision.validUntil")
        reason_code = _str(row.get("reasonCode", ""), "decision.reasonCode", nonempty=False)
        reason = _str(row.get("reason", ""), "decision.reason", nonempty=False)
        order_id = None if row.get("orderID") is None else _str(row.get("orderID"), "decision.orderID")
        win_rate = _optional_number(_required(row, "winRate", "decision"), "decision.winRate", minimum=0, maximum=1)
        risk_reward_ratio = _optional_number(_required(row, "riskRewardRatio", "decision"), "decision.riskRewardRatio", minimum=0)
        # Keep parser compatibility with old hold records while the provider
        # schema requires the field for all newly generated decisions.
        leverage = _optional_number(row.get("leverage"), "decision.leverage", minimum=0)
        assessments = [AIInstrumentAssessment.from_dict(item) for item in _list(row.get("assessments", []), "decision.assessments")]
        if len({item.instrumentID for item in assessments}) != len(assessments):
            raise SchemaError("decision.assessments contains duplicate contract IDs")
        return cls(
            schemaVersion=1, decisionId=decision_id, snapshotId=snapshot_id, action=action,
            instrumentID=instrument, direction=direction, orderType=order_type,
            riskBudgetPercent=_optional_number(row.get("riskBudgetPercent"), "decision.riskBudgetPercent", minimum=0, maximum=100),
            limitPrice=_optional_number(row.get("limitPrice"), "decision.limitPrice", minimum=0),
            stopLossPrice=_optional_number(row.get("stopLossPrice"), "decision.stopLossPrice", minimum=0),
            takeProfitPrice=_optional_number(row.get("takeProfitPrice"), "decision.takeProfitPrice", minimum=0),
            takeProfitLevels=_take_profit_levels(row.get("takeProfitLevels"), "decision.takeProfitLevels"),
            winRate=win_rate, riskRewardRatio=risk_reward_ratio, leverage=leverage,
            confidence=_number(row.get("confidence", 0), "decision.confidence", minimum=0, maximum=1),
            validUntil=valid_until, reasonCode=reason_code, reason=reason, orderID=order_id,
            assessments=assessments,
        )

    @classmethod
    def hold(cls, snapshot_id: str, reason: str, *, decision_id: str = "hold") -> "AIDecision":
        now = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
        return cls(1, decision_id, snapshot_id or "unknown", "hold", winRate=0, riskRewardRatio=0, confidence=0, validUntil=now, reasonCode="hold", reason=reason)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        # Keep persisted legacy assessment records byte-compatible when no
        # staged targets were supplied. New provider output may include the
        # field explicitly (including an empty/one-level plan).
        if payload.get("takeProfitLevels") is None:
            payload.pop("takeProfitLevels", None)
        for assessment in payload.get("assessments", []):
            if isinstance(assessment, dict) and assessment.get("takeProfitLevels") is None:
                assessment.pop("takeProfitLevels", None)
        return payload

    def require_complete_assessments(self, observed_instruments: list[str]) -> None:
        expected = set(observed_instruments)
        actual = [item.instrumentID for item in self.assessments]
        if len(actual) != len(set(actual)):
            raise SchemaError("decision.assessments contains duplicate contract IDs")
        missing = sorted(expected - set(actual))
        extra = sorted(set(actual) - expected)
        if missing or extra:
            raise SchemaError(
                "decision.assessments must cover every observed contract exactly once: "
                f"missing={missing}, extra={extra}"
            )


@dataclass(frozen=True)
class AISnapshot:
    snapshotId: str
    capturedAt: str
    instruments: list[dict[str, Any]] = field(default_factory=list)
    candles: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    tickers: dict[str, dict[str, Any]] = field(default_factory=dict)
    orderBook: dict[str, Any] = field(default_factory=dict)
    fundingRates: dict[str, Any] = field(default_factory=dict)
    account: dict[str, Any] = field(default_factory=dict)
    risk: dict[str, Any] = field(default_factory=dict)
    ai: dict[str, Any] = field(default_factory=dict)
    dataFreshness: dict[str, Any] = field(default_factory=dict)

    _FIELDS: ClassVar[set[str]] = {
        "snapshotId", "capturedAt", "instruments", "candles", "tickers", "orderBook", "fundingRates",
        "account", "risk", "ai", "dataFreshness",
    }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AISnapshot":
        row = _mapping(value, "snapshot")
        _reject_extra(row, cls._FIELDS, "snapshot")
        instruments = _list(row.get("instruments", []), "snapshot.instruments")
        if not all(isinstance(item, Mapping) for item in instruments):
            raise SchemaError("snapshot.instruments entries must be objects")
        candles = _object(row.get("candles", {}), "snapshot.candles")
        for key, value in candles.items():
            if not isinstance(key, str) or not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
                raise SchemaError("snapshot.candles must map intervals to object arrays")
        return cls(
            snapshotId=_str(_required(row, "snapshotId", "snapshot"), "snapshot.snapshotId"),
            capturedAt=_iso(_required(row, "capturedAt", "snapshot"), "snapshot.capturedAt"),
            instruments=[dict(item) for item in instruments], candles={str(k): [dict(item) for item in v] for k, v in candles.items()},
            tickers=_object(row.get("tickers", {}), "snapshot.tickers"), orderBook=_object(row.get("orderBook", {}), "snapshot.orderBook"),
            fundingRates=_object(row.get("fundingRates", {}), "snapshot.fundingRates"), account=_object(row.get("account", {}), "snapshot.account"),
            risk=_object(row.get("risk", {}), "snapshot.risk"), ai=_object(row.get("ai", {}), "snapshot.ai"),
            dataFreshness=_object(row.get("dataFreshness", {}), "snapshot.dataFreshness"),
        )

    def observed_instruments(self) -> list[str]:
        """Resolve the actual observation set, excluding auxiliary candidates.

        New snapshots provide ``selectedInstruments`` explicitly. Older
        providers use the selection count and candidate ordering instead.
        """
        selected = self.ai.get("selectedInstruments") if isinstance(self.ai, Mapping) else None
        if not isinstance(selected, list):
            count = self.ai.get("selectionCount") if isinstance(self.ai, Mapping) else None
            count = count if isinstance(count, int) and count >= 0 else len(self.instruments)
            selected = self.instruments[:count]
        result: list[str] = []
        seen: set[str] = set()
        for item in selected:
            instrument = item if isinstance(item, str) else item.get("id") if isinstance(item, Mapping) else None
            if isinstance(instrument, str) and instrument.strip() and instrument.strip() not in seen:
                value = instrument.strip()
                result.append(value)
                seen.add(value)
        return result

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AIConfig:
    # The strategy's decision engine. ``codex`` is the backwards-compatible
    # default; DeepSeek Harness uses the local ACP stdio adapter.
    provider: AIProvider = "codex"
    enabled: bool = False
    mode: RunMode = "disabled"
    allowedInstruments: tuple[str, ...] = ()
    minimumConfidence: float = 0.65
    decisionIntervalSeconds: float = 30.0
    cliTimeoutSeconds: float = DEFAULT_CLI_TIMEOUT_SECONDS
    maxOutputBytes: int = 1_000_000
    cooldownSeconds: float = 0.0
    maxConsecutiveFailures: int = 3
    allowOpen: bool = True
    allowClose: bool = True
    allowCancel: bool = True
    requireStopLoss: bool = True
    # AI strategy risk controls. Defaults are intentionally conservative and
    # apply to entries; reduce-only exits remain available.
    maxDailyOrders: int = 20
    maxDailyLosses: int = 5
    marginPerOrderUSD: float = 500.0
    maxLeverage: float = 5.0
    # Retain the legacy routing fields for configuration compatibility. Both
    # routes now represent the same fixed model and reasoning effort.
    routineModel: str = FIXED_AI_MODEL
    routineReasoningEffort: str = FIXED_AI_REASONING_EFFORT
    escalationModel: str = FIXED_AI_MODEL
    escalationReasoningEffort: str = FIXED_AI_REASONING_EFFORT

    _FIELDS: ClassVar[set[str]] = {
        "provider",
        "enabled", "mode", "allowedInstruments",
        # Legacy fields are accepted only while reading old state files. They
        # are ignored and never emitted again, so dynamic boards cannot be
        # selected through the current API.
        "universeMode", "candidateLimit", "selectionLimit",
        "minimumConfidence", "decisionIntervalSeconds",
        "cliTimeoutSeconds", "maxOutputBytes", "cooldownSeconds", "maxConsecutiveFailures",
        "allowOpen", "allowClose", "allowCancel", "requireStopLoss",
        "maxDailyOrders", "maxDailyLosses", "marginPerOrderUSD", "maxLeverage",
        "routineModel", "routineReasoningEffort", "escalationModel", "escalationReasoningEffort",
    }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any] | None) -> "AIConfig":
        row = _mapping(value or {}, "config")
        _reject_extra(row, cls._FIELDS, "config")
        provider = row.get("provider", "codex")
        if provider not in {"codex", "deepseek-harness"}:
            raise SchemaError("config.provider must be one of: codex, deepseek-harness")
        bools = {key: bool(row.get(key, getattr(cls(), key))) for key in ("enabled", "allowOpen", "allowClose", "allowCancel", "requireStopLoss")}
        for key, val in bools.items():
            if not isinstance(row.get(key, val), bool):
                raise SchemaError(f"config.{key} must be boolean")
        mode = row.get("mode", "disabled")
        if mode not in {"disabled", "shadow", "demo-active", "live-armed", "halted"}:
            raise SchemaError("config.mode is invalid")
        instruments = normalize_contract_ids(row.get("allowedInstruments", []), "config.allowedInstruments")
        # Accept only the previous known routes during migration, then
        # normalize them without discarding the user's other settings.
        _model_name(row.get("routineModel", FIXED_AI_MODEL), "config.routineModel", allowed={FIXED_AI_MODEL})
        _reasoning_effort(row.get("routineReasoningEffort", FIXED_AI_REASONING_EFFORT), "config.routineReasoningEffort", allowed={"low", FIXED_AI_REASONING_EFFORT})
        _model_name(row.get("escalationModel", FIXED_AI_MODEL), "config.escalationModel", allowed={"gpt-6.1-sol", FIXED_AI_MODEL})
        _reasoning_effort(row.get("escalationReasoningEffort", FIXED_AI_REASONING_EFFORT), "config.escalationReasoningEffort", allowed={FIXED_AI_REASONING_EFFORT})
        return cls(
            provider=provider, enabled=bools["enabled"], mode=mode, allowedInstruments=tuple(instruments),
            minimumConfidence=_number(row.get("minimumConfidence", .65), "config.minimumConfidence", minimum=0, maximum=1),
            decisionIntervalSeconds=_number(row.get("decisionIntervalSeconds", 30), "config.decisionIntervalSeconds", minimum=.1),
            cliTimeoutSeconds=_number(row.get("cliTimeoutSeconds", DEFAULT_CLI_TIMEOUT_SECONDS), "config.cliTimeoutSeconds", minimum=.1),
            maxOutputBytes=int(_number(row.get("maxOutputBytes", 1_000_000), "config.maxOutputBytes", minimum=1024, maximum=10_000_000)),
            # Read legacy values but normalize the field to zero. The live
            # entry gate is the current account position/order state.
            cooldownSeconds=0.0,
            maxConsecutiveFailures=int(_number(row.get("maxConsecutiveFailures", 3), "config.maxConsecutiveFailures", minimum=1, maximum=100)),
            allowOpen=bools["allowOpen"], allowClose=bools["allowClose"], allowCancel=bools["allowCancel"], requireStopLoss=bools["requireStopLoss"],
            maxDailyOrders=_integer(row.get("maxDailyOrders", 20), "config.maxDailyOrders", minimum=1, maximum=10000),
            maxDailyLosses=_integer(row.get("maxDailyLosses", 5), "config.maxDailyLosses", minimum=0, maximum=10000),
            marginPerOrderUSD=_number(row.get("marginPerOrderUSD", 500), "config.marginPerOrderUSD", minimum=0.01, maximum=1_000_000),
            maxLeverage=_number(row.get("maxLeverage", 5), "config.maxLeverage", minimum=1, maximum=125),
        )

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["allowedInstruments"] = list(self.allowedInstruments)
        return result

    def clone_for_provider(self, provider: AIProvider) -> "AIConfig":
        """Clone every strategy parameter while changing only the provider."""
        values = self.to_dict()
        values["provider"] = provider
        return AIConfig.from_dict(values)


# These are the only configuration fields an AI conversation may suggest.
# Worker enablement and account mode remain human-controlled operations.
AI_CHAT_PATCH_FIELDS: frozenset[str] = frozenset({
    "allowedInstruments", "minimumConfidence", "decisionIntervalSeconds",
    "cooldownSeconds", "allowOpen", "allowClose", "allowCancel", "requireStopLoss",
    "maxDailyOrders", "maxDailyLosses", "marginPerOrderUSD", "maxLeverage",
})


@dataclass(frozen=True)
class AIChatRequest:
    message: str
    apply: bool = False
    suggestion: dict[str, Any] | None = None

    _FIELDS: ClassVar[set[str]] = {"message", "apply", "suggestion"}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AIChatRequest":
        row = _mapping(value, "chat")
        _reject_extra(row, cls._FIELDS, "chat")
        message = _str(_required(row, "message", "chat"), "chat.message")
        if len(message) > 4000:
            raise SchemaError("chat.message must be at most 4000 characters")
        apply = row.get("apply", False)
        if not isinstance(apply, bool):
            raise SchemaError("chat.apply must be boolean")
        suggestion = row.get("suggestion")
        if suggestion is not None:
            suggestion = normalize_ai_chat_patch(suggestion)
        if apply and suggestion is None:
            raise SchemaError("chat.suggestion is required when apply is true")
        return cls(message=message, apply=apply, suggestion=suggestion)


def normalize_ai_chat_patch(value: Mapping[str, Any] | None) -> dict[str, Any]:
    """Validate and normalize a model-proposed, non-authoritative config patch."""
    if value is None:
        return {}
    row = _mapping(value, "chat.suggestion")
    _reject_extra(row, AI_CHAT_PATCH_FIELDS, "chat.suggestion")
    # Strict structured output requires every patch field to be present. A
    # null value means that field is intentionally left unchanged.
    row = {key: item for key, item in row.items() if item is not None}
    if "allowedInstruments" in row:
        row["allowedInstruments"] = normalize_contract_ids(
            row["allowedInstruments"], "chat.suggestion.allowedInstruments", allow_empty=False
        )
    for key in ("allowOpen", "allowClose", "allowCancel", "requireStopLoss"):
        if key in row and not isinstance(row[key], bool):
            raise SchemaError(f"chat.suggestion.{key} must be boolean")
    if "minimumConfidence" in row:
        row["minimumConfidence"] = _number(row["minimumConfidence"], "chat.suggestion.minimumConfidence", minimum=0, maximum=1)
    for key in ("decisionIntervalSeconds", "cooldownSeconds"):
        if key in row:
            row[key] = _number(row[key], f"chat.suggestion.{key}", minimum=0.0 if key == "cooldownSeconds" else 0.1)
    if "cooldownSeconds" in row:
        # Keep old chat payloads schema-compatible while making the removed
        # time-based restriction a no-op.
        row["cooldownSeconds"] = 0.0
    for key in ("maxDailyOrders", "maxDailyLosses"):
        if key in row:
            value = row[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < (1 if key == "maxDailyOrders" else 0) or value > 10000:
                raise SchemaError(f"chat.suggestion.{key} must be an integer within the configured range")
    if "marginPerOrderUSD" in row:
        row["marginPerOrderUSD"] = _number(row["marginPerOrderUSD"], "chat.suggestion.marginPerOrderUSD", minimum=0.01, maximum=1_000_000)
    if "maxLeverage" in row:
        row["maxLeverage"] = _number(row["maxLeverage"], "chat.suggestion.maxLeverage", minimum=1, maximum=125)
    return row


@dataclass(frozen=True)
class AIChatResponse:
    schemaVersion: int
    reply: str
    suggestion: dict[str, Any] | None = None

    _FIELDS: ClassVar[set[str]] = {"schemaVersion", "reply", "suggestion"}

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "AIChatResponse":
        row = _mapping(value, "chatResponse")
        _reject_extra(row, cls._FIELDS, "chatResponse")
        version = _required(row, "schemaVersion", "chatResponse")
        if isinstance(version, bool) or not isinstance(version, int) or version != 1:
            raise SchemaError("chatResponse.schemaVersion must be 1")
        reply = _str(_required(row, "reply", "chatResponse"), "chatResponse.reply")
        if len(reply) > 8000:
            raise SchemaError("chatResponse.reply must be at most 8000 characters")
        suggestion = row.get("suggestion")
        if suggestion is not None:
            suggestion = normalize_ai_chat_patch(suggestion)
        return cls(schemaVersion=1, reply=reply, suggestion=suggestion)

    def to_dict(self) -> dict[str, Any]:
        return {"schemaVersion": self.schemaVersion, "reply": self.reply, "suggestion": self.suggestion}


@dataclass(frozen=True)
class AIStatus:
    state: str = "stopped"
    mode: RunMode = "disabled"
    enabled: bool = False
    consecutiveFailures: int = 0
    lastDecisionAt: str | None = None
    lastError: str | None = None
    lastDecision: dict[str, Any] | None = None
    updatedAt: str = ""
    # The concrete instruments selected for the current observation cycle.
    # This mirrors the fixed allowlist actually handed to Codex.
    observedInstruments: list[str] = field(default_factory=list)
    observationUpdatedAt: str | None = None
    # The snapshot universe belonging to lastDecision, which can differ from
    # the current observation set after a config change or a failed cycle.
    lastDecisionInstruments: list[str] = field(default_factory=list)
    # Server-computed clock context paired with lastDecision, never a model estimate.
    lastDecisionFreshness: dict[str, Any] = field(default_factory=dict)
    # Evaluation metadata is separate from lastDecision so a prescreen hold
    # cannot overwrite the last real model decision shown to the operator.
    lastEvaluationSource: str = "model"
    lastEvaluationAt: str | None = None
    skippedCycles: int = 0
    decisionFingerprint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def decision_json_schema(
    snapshot_id: str | None = None, observed_instruments: list[str] | None = None,
) -> dict[str, Any]:
    assessment_properties: dict[str, Any] = {
        "instrumentID": {"type": "string", "pattern": "^[A-Z0-9]+-USDT-SWAP$"},
        "direction": {"type": "string", "enum": ["long", "short", "neutral"]},
        "winRate": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "riskRewardRatio": {"type": ["number", "null"], "minimum": 0},
        "limitPrice": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "stopLossPrice": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "takeProfitPrice": {"type": ["number", "null"], "exclusiveMinimum": 0},
        "takeProfitLevels": {
            "type": ["array", "null"], "minItems": 1, "maxItems": 4,
            "items": {
                "type": "object", "additionalProperties": False,
                "required": ["price", "quantityPercent"],
                "properties": {
                    "price": {"type": "number", "exclusiveMinimum": 0},
                    "quantityPercent": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
                },
            },
        },
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
        "entryEligible": {"type": "boolean"},
        "unmetConditions": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "reason": {"type": "string", "minLength": 1},
    }
    assessment_schema: dict[str, Any] = {
        "type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": list(assessment_properties), "properties": assessment_properties,
        },
    }
    if observed_instruments is not None:
        count = len(set(observed_instruments))
        assessment_schema.update(minItems=count, maxItems=count)
        if observed_instruments:
            assessment_properties["instrumentID"]["enum"] = list(dict.fromkeys(observed_instruments))
    schema = {
        "type": "object", "additionalProperties": False,
        # Codex uses the provider's strict structured-output mode, which
        # requires every declared property to be present. Optional trading
        # fields remain nullable so a hold intent can still be concise.
        "required": [
            "schemaVersion", "decisionId", "snapshotId", "action", "instrumentID", "direction",
            "orderType", "riskBudgetPercent", "limitPrice", "stopLossPrice", "takeProfitPrice",
            "takeProfitLevels", "winRate", "riskRewardRatio", "leverage", "confidence", "validUntil", "reasonCode", "reason", "orderID", "assessments",
        ],
        "properties": {
            "schemaVersion": {"type": "integer", "const": 1}, "decisionId": {"type": "string", "minLength": 1},
            "snapshotId": {"type": "string", "minLength": 1}, "action": {"type": "string", "enum": ["hold", "open", "close", "cancel"]},
            "instrumentID": {"type": ["string", "null"], "pattern": "^[A-Z0-9]+-USDT-SWAP$"}, "direction": {"type": ["string", "null"], "enum": ["long", "short", None]},
            "orderType": {"type": ["string", "null"], "enum": ["market", "limit", None]}, "riskBudgetPercent": {"type": ["number", "null"], "minimum": 0, "maximum": 100},
            "limitPrice": {"type": ["number", "null"], "minimum": 0}, "stopLossPrice": {"type": ["number", "null"], "minimum": 0},
            "takeProfitPrice": {"type": ["number", "null"], "minimum": 0}, "leverage": {"type": ["number", "null"], "minimum": 0}, "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "takeProfitLevels": {
                "type": ["array", "null"], "minItems": 1, "maxItems": 4,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["price", "quantityPercent"],
                    "properties": {
                        "price": {"type": "number", "exclusiveMinimum": 0},
                        "quantityPercent": {"type": "number", "exclusiveMinimum": 0, "maximum": 100},
                    },
                },
            },
            "winRate": {"type": ["number", "null"], "minimum": 0, "maximum": 1}, "riskRewardRatio": {"type": ["number", "null"], "minimum": 0},
            "validUntil": {"type": "string", "format": "date-time"}, "reasonCode": {"type": "string"}, "reason": {"type": "string"}, "orderID": {"type": ["string", "null"]},
            "assessments": assessment_schema,
        },
    }
    if snapshot_id is not None:
        schema["properties"]["snapshotId"]["enum"] = [snapshot_id]
    return schema


def ai_chat_json_schema() -> dict[str, Any]:
    """Strict provider schema for the conversational configuration response."""
    patch_properties: dict[str, Any] = {
        "allowedInstruments": {
            "type": ["array", "null"],
            "items": {"type": "string", "pattern": "^[A-Z0-9]+-USDT-SWAP$"},
            "minItems": 1,
        },
        "minimumConfidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "decisionIntervalSeconds": {"type": ["number", "null"], "minimum": 0.1},
        "cooldownSeconds": {"type": ["number", "null"], "minimum": 0},
        "allowOpen": {"type": ["boolean", "null"]},
        "allowClose": {"type": ["boolean", "null"]},
        "allowCancel": {"type": ["boolean", "null"]},
        "requireStopLoss": {"type": ["boolean", "null"]},
        "maxDailyOrders": {"type": ["integer", "null"], "minimum": 1, "maximum": 10000},
        "maxDailyLosses": {"type": ["integer", "null"], "minimum": 0, "maximum": 10000},
        "marginPerOrderUSD": {"type": ["number", "null"], "minimum": 0.01, "maximum": 1000000},
        "maxLeverage": {"type": ["number", "null"], "minimum": 1, "maximum": 125},
    }
    patch_schema: dict[str, Any] = {
        "type": "object", "additionalProperties": False,
        "required": list(patch_properties), "properties": patch_properties,
    }
    return {
        "type": "object", "additionalProperties": False,
        "required": ["schemaVersion", "reply", "suggestion"],
        "properties": {
            "schemaVersion": {"type": "integer", "const": 1},
            "reply": {"type": "string", "minLength": 1, "maxLength": 8000},
            "suggestion": {"anyOf": [patch_schema, {"type": "null"}]},
        },
    }


def dumps(value: Any) -> str:
    """Serialize contracts without allowing non-finite JSON values."""
    return json.dumps(value.to_dict() if hasattr(value, "to_dict") else value, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
