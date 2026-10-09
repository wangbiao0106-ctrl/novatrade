"""Long-running, fail-closed Codex decision worker.

The worker only creates intents.  A caller-supplied order gateway remains the
sole component allowed to turn an admitted intent into an exchange request.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import replace
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import signal
import shutil
import tempfile
try:
    # Python 3.11+ includes TOML parsing in the standard library. The
    # bundled macOS runtime currently uses Python 3.9, where ``tomli`` is
    # available as the compatible backport.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.9
    try:
        import tomli as tomllib
    except ModuleNotFoundError:  # pragma: no cover - portable fallback
        tomllib = None  # type: ignore[assignment]
from typing import Any, Protocol

try:
    from .ai_market_facts import market_facts, primary_entry_quality
    from .ai_trigger import decision_fingerprint, managed_state, structure_identity, trigger_reason
    from .ai_policy import (
        MIN_EXIT_REASON_LENGTH,
        MIN_OPEN_RISK_REWARD_RATIO,
        MIN_OPEN_WIN_RATE,
        MIN_PROTECTION_REPLACEMENT_CONFIDENCE,
        PolicyResult, PolicyState, record_decision, snapshot_freshness, validate_decision,
    )
    from .ai_schema import (
        AIDecision, AIChatResponse, AIConfig, AISnapshot, AIStatus, SchemaError,
        DEFAULT_CLI_TIMEOUT_SECONDS, LEGACY_DEFAULT_CLI_TIMEOUT_SECONDS,
        DEFAULT_DECISION_INTERVAL_SECONDS, LEGACY_DEFAULT_DECISION_INTERVAL_SECONDS,
        FIXED_AI_MODEL, FIXED_AI_REASONING_EFFORT,
        PRIMARY_ENTRY_INTERVAL, MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES,
        ai_chat_json_schema, decision_json_schema, dumps, normalize_ai_chat_patch,
        normalize_contract_ids,
    )
except ImportError:  # launched from bundled backend/main.py as a script
    from ai_market_facts import market_facts, primary_entry_quality
    from ai_trigger import decision_fingerprint, managed_state, structure_identity, trigger_reason
    from ai_policy import (
        MIN_EXIT_REASON_LENGTH,
        MIN_OPEN_RISK_REWARD_RATIO,
        MIN_OPEN_WIN_RATE,
        MIN_PROTECTION_REPLACEMENT_CONFIDENCE,
        PolicyResult, PolicyState, record_decision, snapshot_freshness, validate_decision,
    )
    from ai_schema import (
        AIDecision, AIChatResponse, AIConfig, AISnapshot, AIStatus, SchemaError,
        DEFAULT_CLI_TIMEOUT_SECONDS, LEGACY_DEFAULT_CLI_TIMEOUT_SECONDS,
        DEFAULT_DECISION_INTERVAL_SECONDS, LEGACY_DEFAULT_DECISION_INTERVAL_SECONDS,
        FIXED_AI_MODEL, FIXED_AI_REASONING_EFFORT,
        PRIMARY_ENTRY_INTERVAL, MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES,
        ai_chat_json_schema, decision_json_schema, dumps, normalize_ai_chat_patch,
        normalize_contract_ids,
    )


class CodexError(RuntimeError):
    """Codex could not produce a valid decision."""

    def __init__(self, message: str, *, safe_decision: AIDecision | None = None) -> None:
        super().__init__(message)
        self._safe_decision = safe_decision

    @property
    def safe_decision(self) -> AIDecision | None:
        """Complete analytical results attached to a non-executable fallback."""
        return self._safe_decision


_AUTOMATIC_MODELS = frozenset({FIXED_AI_MODEL})
_AUTOMATIC_REASONING = frozenset({FIXED_AI_REASONING_EFFORT})
# The prompt cap is deliberately not the CLI output cap: maxOutputBytes
# truncates the provider's stdout/stderr, while this bounds the request we are
# willing to send. Raising the output cap must not raise the prompt budget.
_MAX_DECISION_PROMPT_BYTES = 1_000_000
# A grouped cycle runs an analysis phase and a coordinator phase. The analysis
# phase keeps this share of the remaining budget so a slow analysis can never
# starve the coordinator that actually produces the decision.
_GROUPED_ANALYSIS_BUDGET_SHARE = 0.6
# A phase needs a usable slice of time even when the snapshot has already
# expired: management actions (close/cancel/protection) do not require a fresh
# snapshot, and a zero-length phase would turn a stale snapshot into a
# consecutive-failure halt.
_MIN_PIPELINE_BUDGET_SHARE = 0.5

# The analysis and coordinator prompts share one policy contract.
_SHARED_POLICY_RULES = (
    "DECISION POLICY (follow these four groups in order):\n"
    "1. DATA CONTRACT AND QUALITY: Preserve the fixed observed contract pool and maintain exactly one assessment per contract. "
    "Assess contracts independently; a missing resource for one contract does not erase complete data for another. "
    "Use SERVER MARKET FACTS as measured summaries and do not recompute their arithmetic. "
    "Inspect the complete raw confirmed 4H OHLC history for trend, candle bodies/wicks and price structure; auxiliary raw candles may resolve execution/risk ambiguity. "
    "Calculate riskRewardRatio from the same proposed entry, stop and target, and never fabricate prices or source facts. "
    "For a long plan use stopLossPrice < entry < takeProfitPrice; for a short plan use takeProfitPrice < entry < stopLossPrice. "
    "winRate is a subjective estimate for this conditional plan, not a verified historical success rate; confidence is confidence in the returned action. "
    "Use confirmed=true candles for closed-candle signals; a forming candle is context, not a closed-candle confirmation. "
    f"PRIMARY ENTRY ANALYSIS: {PRIMARY_ENTRY_INTERVAL} is the primary entry timeframe for BOTH long and short plans. "
    "Read its closed-candle trend, higher/lower highs and lows, support/resistance, and pullback/rebound or breakout structure. "
    "Find conditional buy points for long plans and sell points for short plans; do not assume a trade must exist or treat a fixed indicator as an automatic signal. "
    f"An open needs at least {MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES} valid confirmed OHLC {PRIMARY_ENTRY_INTERVAL} candles, as measured by primaryEntryQuality. "
    "5m, 15m, 1H, current price, order book and funding are auxiliary execution/risk context; they cannot replace or independently override the 4H entry thesis. "
    "Forming 4H candles are context only. In each assessment reason explain the 4H trend or structure, the proposed entry conditions, and the invalidation level. "
    "If entry conditions are unmet, use hold and list the waiting conditions. Insufficient primary history blocks only open, never close/cancel/protection management. "
    "Keep a conditional technical plan and numerical estimates even when execution is blocked, unless the relevant data is genuinely unavailable. "
    "PER-CONTRACT ANALYSIS FIRST, EXECUTION ELIGIBILITY SECOND.\n"
    "2. ENTRY GATES: action=open is allowed only when the selected assessment is entryEligible, all SERVER ENTRY GATES pass, "
    "the contract is available, and there is no current position or active pending order for it. "
    "Use hold for an absent or blocked entry candidate, but do not let an entry block erase analysis or management actions. "
    "Select at most one contract and one action per round. Copy the selected assessment's direction, quality values, protection "
    "levels, and limitPrice exactly into an open decision.\n"
    "3. POSITION AND ORDER MANAGEMENT: Every round first considers current positions and pending orders. "
    "A position or pending order blocks replacement open for that contract; it does not block protection review or a justified exit. "
    "Close only for materially invalidated market evidence or ORDER_MISTAKE, using the actual position direction. "
    "Cancel only for materially changed setup or ORDER_MISTAKE, using the real current order ID. "
    f"For close/cancel use reasonCode=THESIS_INVALIDATED or ORDER_MISTAKE and at least {MIN_EXIT_REASON_LENGTH} characters of concrete evidence in reason. "
    "Do not use routine duplicate replacement as an exit reason.\n"
    "4. PROTECTION AND STAGED TAKE-PROFIT: For every current position compare the latest assessment with existing protection. "
    "Restore missing stop-loss/take-profit protection. Staged takeProfitLevels may split profit-taking, must sum to 100%, use unique "
    "prices in the profitable direction, and keep a valid stop for the remaining position. "
    f"Adjust an existing protection line only with high confidence (server threshold {MIN_PROTECTION_REPLACEMENT_CONFIDENCE:.2f}) and clear material thesis change; ordinary "
    "noise must not move it.\n"
    "OUTPUT: Return strict JSON matching the schema, with UTC whole-second validUntil and concise Simplified Chinese reasons. "
    "SERVER FRESHNESS is authoritative: use its evaluatedAt, ageSeconds and isStale values; do not guess the current time or claim a valid fresh snapshot is expired. "
    "Unknown account, risk, freshness, or order state is never zero or safe by assumption; the server policy is authoritative.\n"
)


def _freshness_bounded_budget(snapshot: AISnapshot, config: AIConfig, ceiling: float) -> float:
    """Return the model budget for one cycle, bounded by snapshot freshness.

    Entry admission requires a snapshot younger than ``maxAgeSeconds``, so a
    call that runs past that window can only ever return management actions.
    The budget is therefore ``min(ceiling, max(remaining_age, floor))`` where
    the floor is half the CLI allowance: an already-expired snapshot still gets
    a usable slice for close/cancel/protection instead of collapsing to a
    zero-length phase. When the CLI allowance is more than twice the window the
    floor therefore exceeds the window, which is the operator's explicit choice
    of latency over entry frequency; when the server supplies no usable
    freshness metadata, the ceiling alone applies.
    """
    freshness = snapshot_freshness(snapshot, config)
    if not freshness.get("valid"):
        return ceiling
    max_age = freshness.get("maxAgeSeconds")
    if isinstance(max_age, bool) or not isinstance(max_age, (int, float)) or not math.isfinite(float(max_age)) or max_age <= 0:
        return ceiling
    age = freshness.get("ageSeconds")
    remaining = float(max_age) - (float(age) if isinstance(age, (int, float)) and not isinstance(age, bool) else 0.0)
    usable = max(remaining, config.cliTimeoutSeconds * _MIN_PIPELINE_BUDGET_SHARE)
    return min(ceiling, usable)


def _grouped_pipeline_budget_seconds(snapshot: AISnapshot, config: AIConfig) -> float:
    """Total two-phase grouped budget, bounded by the snapshot admission window.

    Without the bound, ``2 * cliTimeoutSeconds`` of work against a snapshot that
    expires after ``maxAgeSeconds`` makes every grouped ``open`` structurally
    unreachable.
    """
    return _freshness_bounded_budget(snapshot, config, 2 * config.cliTimeoutSeconds)


def _event_driven_enabled() -> bool:
    return _event_driven_mode() != "off"


def _event_driven_mode() -> str:
    explicit = os.getenv("NOVATRADE_AI_EVENT_MODE", "").strip().lower()
    if explicit in {"off", "shadow", "on"}:
        return explicit
    return "on" if os.getenv("NOVATRADE_AI_EVENT_DRIVEN", "0").strip().lower() in {"1", "true", "yes", "on"} else "off"


def _event_max_skip_seconds() -> float:
    try:
        value = float(os.getenv("NOVATRADE_AI_EVENT_MAX_SKIP_SECONDS", "900"))
    except (TypeError, ValueError):
        return 900.0
    return value if math.isfinite(value) and value > 0 else 900.0


def _prompt_snapshot(snapshot: AISnapshot, *, encoding: str | None = None) -> dict[str, Any]:
    """Encode the model snapshot, optionally selecting a shorter candle window.

    The runtime/audit snapshot is unchanged. Repeated candle field names are
    removed only from the model prompt in compact modes; all fields and all row
    values remain present in the runtime snapshot. Windowed modes preserve
    all collected primary-interval rows and the latest auxiliary forming row.
    Heterogeneous objects stay objects to preserve
    absent-vs-null.
    """
    selected_encoding = (encoding or "compact60").strip().lower()
    result = snapshot.to_dict()
    for key, rows in result["candles"].items():
        if not rows:
            continue
        primary_interval = key.endswith("/" + PRIMARY_ENTRY_INTERVAL) or key.endswith(":" + PRIMARY_ENTRY_INTERVAL)
        if selected_encoding in {"compact20", "compact10"} and not primary_interval:
            limit = 20 if selected_encoding == "compact20" else 10
            confirmed = [row for row in rows if row.get("confirmed") is True]
            forming = next((row for row in reversed(rows) if row.get("confirmed") is False), None)
            rows = [*confirmed[-limit:], *([forming] if forming is not None else [])]
            result["candles"][key] = rows
        if selected_encoding == "raw":
            continue
        columns = list(rows[0])
        fields = set(columns)
        if all(set(row) == fields for row in rows):
            result["candles"][key] = {
                "columns": columns,
                "rows": [[row[column] for column in columns] for row in rows],
            }
    return result


class CodexRunnerProtocol(Protocol):
    async def run(self, snapshot: AISnapshot, config: AIConfig) -> AIDecision: ...


class CodexRunner:
    """Invoke an isolated Codex CLI subprocess for one snapshot."""

    def __init__(self, executable: str | None = None) -> None:
        self.executable = executable or os.getenv("NOVATRADE_CODEX_BIN", "codex")
        self.last_run_metadata: dict[str, Any] = {}

    @staticmethod
    def _validate_override(model: str, reasoning_effort: str) -> None:
        """Reject an unsafe route before starting a subprocess.

        Automatic polling uses one fixed model and effort. A malformed
        configuration must not fall back to the global Codex config.
        """
        if model not in _AUTOMATIC_MODELS:
            raise CodexError(
                f"automatic model route rejected model={model!r}; "
                f"allowed models are {sorted(_AUTOMATIC_MODELS)}"
            )
        if reasoning_effort not in _AUTOMATIC_REASONING:
            raise CodexError(
                f"automatic model route rejected reasoning_effort={reasoning_effort!r}; "
                f"allowed values are {sorted(_AUTOMATIC_REASONING)}"
            )

    @classmethod
    def _command(
        cls,
        schema_path: Path,
        workdir: str,
        *,
        model: str,
        reasoning_effort: str,
    ) -> list[str]:
        cls._validate_override(model, reasoning_effort)
        # Keep the provider and CODEX_HOME from the isolated environment, but
        # override model and reasoning for this invocation.  There is no retry
        # without these flags: an unsupported provider/model is fail-closed.
        return [
            "exec", "--ephemeral", "--sandbox", "read-only",
            "--output-schema", str(schema_path), "--json", "--skip-git-repo-check",
            "--ignore-rules", "-m", model, "-c", f"model_reasoning_effort={reasoning_effort}",
            "-c", "mcp_servers={}", "-c", "plugins={}",
            "-C", workdir,
        ]

    @staticmethod
    def _route_values(config: AIConfig) -> tuple[str, str]:
        return config.routineModel, config.routineReasoningEffort

    @staticmethod
    async def _stop_timed_out_process(process: asyncio.subprocess.Process) -> None:
        """Stop a timed-out CLI without waiting forever on inherited pipes."""
        try:
            # Descendants can retain the pipes after the group leader exits.
            # The process group still needs terminating in that case.
            os.killpg(process.pid, signal.SIGKILL)
        except (AttributeError, OSError):
            if process.returncode is None:
                process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        # A CLI may leave a child holding stdout/stderr open after its parent
        # exits. Close the reader transports so cleanup cannot block the API.
        for stream in (process.stdout, process.stderr):
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                transport.close()

    @staticmethod
    def runtime_identity() -> dict[str, str]:
        """Read only non-secret model metadata for the conversation context."""
        if tomllib is None:
            return {"model": "Codex 默认模型", "provider": "default"}
        configured_home = os.environ.get("NOVATRADE_CODEX_HOME")
        codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
        path = codex_home / "config.toml"
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
            provider_key = str(data.get("model_provider") or "default")
            providers = data.get("model_providers") if isinstance(data.get("model_providers"), dict) else {}
            provider = providers.get(provider_key) if isinstance(providers, dict) else {}
            provider_name = str((provider or {}).get("name") or provider_key)
            model = str(data.get("model") or "Codex 默认模型")
            return {"model": model, "provider": provider_name}
        except (OSError, tomllib.TOMLDecodeError, TypeError, ValueError):
            return {"model": "Codex 默认模型", "provider": "default"}

    @staticmethod
    def _toml_literal(value: Any) -> str | None:
        """Serialize the small provider config subset needed by Codex CLI."""
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return repr(value)
        if isinstance(value, str):
            return json.dumps(value, ensure_ascii=False)
        if isinstance(value, list):
            items = [CodexRunner._toml_literal(item) for item in value]
            if any(item is None for item in items):
                return None
            return "[" + ", ".join(item for item in items if item is not None) + "]"
        if isinstance(value, Mapping):
            parts: list[str] = []
            for key, item in value.items():
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                    return None
                literal = CodexRunner._toml_literal(item)
                if literal is None:
                    return None
                parts.append(f"{key} = {literal}")
            return "{" + ", ".join(parts) + "}"
        return None

    @classmethod
    def _prepare_codex_home(cls, workdir: str) -> Path:
        """Create a per-invocation Codex home without user MCP/plugin state."""
        codex_home = Path(workdir) / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        if tomllib is None:
            return codex_home
        configured_home = os.environ.get("NOVATRADE_CODEX_HOME")
        source_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
        try:
            source = tomllib.loads((source_home / "config.toml").read_text(encoding="utf-8"))
            provider_key = source.get("model_provider")
            providers = source.get("model_providers")
            provider = providers.get(provider_key) if isinstance(providers, Mapping) else None
            if not isinstance(provider_key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", provider_key):
                return codex_home
            if not isinstance(provider, Mapping):
                return codex_home
            lines = [f"model_provider = {json.dumps(provider_key)}", f"[model_providers.{provider_key}]"]
            for key, value in provider.items():
                if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                    continue
                literal = cls._toml_literal(value)
                if literal is not None:
                    lines.append(f"{key} = {literal}")
            config_path = codex_home / "config.toml"
            config_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            try:
                config_path.chmod(0o600)
            except OSError:
                pass
        except (OSError, ValueError, TypeError, tomllib.TOMLDecodeError):
            # The CLI can still use its normal auth/provider discovery when no
            # local provider config is available.
            pass
        return codex_home

    @staticmethod
    def _safe_environment(workdir: str) -> dict[str, str]:
        # An allowlist prevents future credentials added to the parent process
        # from silently becoming available to the model.
        codex_home = CodexRunner._prepare_codex_home(workdir)
        path = os.environ.get("PATH", "/usr/bin:/bin")
        return {
            "PATH": path,
            "HOME": workdir,
            "TMPDIR": workdir,
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
            "CODEX_HOME": str(codex_home),
        }

    @staticmethod
    def _extract_agent_text(output: str) -> str:
        """Extract the final agent_message from Codex --json JSONL output."""
        candidates: list[str] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            def visit(value: Any) -> None:
                if isinstance(value, Mapping):
                    kind = value.get("type")
                    if kind in {"agent_message", "assistant_message"}:
                        text = value.get("text")
                        if isinstance(text, str):
                            candidates.append(text)
                        content = value.get("content")
                        if isinstance(content, str):
                            candidates.append(content)
                        elif isinstance(content, list):
                            for part in content:
                                if isinstance(part, Mapping) and isinstance(part.get("text"), str):
                                    candidates.append(part["text"])
                    for nested in value.values():
                        visit(nested)
                elif isinstance(value, list):
                    for nested in value:
                        visit(nested)
            visit(event)
        if candidates:
            return candidates[-1].strip()
        # Some CLI versions emit a single JSON object without an event type.
        return output.strip()

    @staticmethod
    def _decode_decision(text: str) -> dict[str, Any]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE | re.DOTALL).strip()
        decoder = json.JSONDecoder()
        try:
            value, _ = decoder.raw_decode(text)
        except json.JSONDecodeError:
            start = text.find("{")
            if start < 0:
                raise CodexError("Codex agent_message did not contain JSON")
            try:
                value, _ = decoder.raw_decode(text[start:])
            except json.JSONDecodeError as error:
                raise CodexError("Codex agent_message contained invalid JSON") from error
        if not isinstance(value, dict):
            raise CodexError("Codex decision must be an object")
        return value

    @classmethod
    def _decode_chat_response(cls, text: str) -> AIChatResponse:
        """Decode structured chat output while accepting ordinary prose."""
        cleaned = text.strip()
        if not cleaned:
            raise CodexError("Codex chat response was empty")
        try:
            value = cls._decode_decision(cleaned)
        except CodexError:
            # Casual conversation may be returned as plain text despite the
            # JSON preference. It is still a valid answer with no patch.
            return AIChatResponse(schemaVersion=1, reply=cleaned[:8000], suggestion=None)
        try:
            return AIChatResponse.from_dict(value)
        except SchemaError:
            # Preserve a useful reply from a malformed JSON envelope, but
            # never treat its unknown fields as an executable patch.
            reply = value.get("reply") if isinstance(value, Mapping) else None
            if isinstance(reply, str) and reply.strip():
                return AIChatResponse(schemaVersion=1, reply=reply.strip()[:8000], suggestion=None)
            raise

    @staticmethod
    def _check_prompt_budget(prompt: str) -> bytes:
        prompt_bytes = prompt.encode("utf-8")
        if len(prompt_bytes) > _MAX_DECISION_PROMPT_BYTES:
            raise CodexError(
                f"Full observation-pool snapshot exceeds the Codex prompt budget: "
                f"{len(prompt_bytes)} UTF-8 bytes > {_MAX_DECISION_PROMPT_BYTES}; "
                "no instruments were omitted and no CLI was started"
            )
        return prompt_bytes

    async def _run_prompt(
        self, prompt: str, schema: dict[str, Any], config: AIConfig, *, deadline: float | None = None,
    ) -> dict[str, Any]:
        prompt_bytes = self._check_prompt_budget(prompt)
        with tempfile.TemporaryDirectory(prefix="novatrade-codex-") as workdir:
            schema_path = Path(workdir) / "ai-decision.schema.json"
            schema_path.write_text(json.dumps(schema, separators=(",", ":")), encoding="utf-8")
            model, reasoning_effort = self._route_values(config)
            command = [self.executable, *self._command(
                schema_path, workdir, model=model, reasoning_effort=reasoning_effort,
            )]
            remaining = config.cliTimeoutSeconds
            if deadline is not None:
                remaining = min(remaining, deadline - asyncio.get_running_loop().time())
            if remaining <= 0:
                raise CodexError("Codex decision workflow timed out")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=self._safe_environment(workdir), cwd=workdir,
                    start_new_session=True,
                )
                try:
                    if deadline is not None:
                        remaining = max(0, min(remaining, deadline - asyncio.get_running_loop().time()))
                    stdout, stderr = await asyncio.wait_for(process.communicate(prompt_bytes), timeout=remaining)
                except asyncio.TimeoutError as error:
                    await self._stop_timed_out_process(process)
                    raise CodexError("Codex CLI timed out") from error
                except asyncio.CancelledError:
                    await asyncio.shield(self._stop_timed_out_process(process))
                    raise
            except FileNotFoundError as error:
                raise CodexError("Codex CLI executable is unavailable") from error
            if len(stdout) > config.maxOutputBytes or len(stderr) > config.maxOutputBytes:
                raise CodexError("Codex CLI output exceeded configured limit")
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[:500]
                raise CodexError(
                    f"Codex CLI exited with status {process.returncode} for model={model!r}, "
                    f"reasoning_effort={reasoning_effort!r}: {detail}"
                )
            try:
                return self._decode_decision(self._extract_agent_text(stdout.decode("utf-8", errors="strict")))
            except (UnicodeDecodeError, SchemaError, CodexError) as error:
                raise CodexError(str(error)) from error


    async def _run_single(
        self, snapshot: AISnapshot, config: AIConfig, *, deadline: float | None = None,
        prompt: str | None = None,
    ) -> AIDecision:
        raw = await self._run_prompt(
            prompt if prompt is not None else self._decision_prompt(snapshot, config),
            decision_json_schema(snapshot.snapshotId, snapshot.observed_instruments()),
            config, deadline=deadline,
        )
        try:
            if "assessments" not in raw:
                raise SchemaError("new AI decisions must include per-contract assessments")
            decision = AIDecision.from_dict(raw)
            decision.require_complete_assessments(snapshot.observed_instruments())
            if decision.snapshotId != snapshot.snapshotId:
                raise SchemaError("decision snapshotId does not match current snapshot")
            if decision.instrumentID is not None and decision.instrumentID not in snapshot.observed_instruments():
                raise SchemaError("decision instrumentID is outside this analysis group")
            return decision
        except (SchemaError, TypeError, ValueError) as error:
            raise CodexError(str(error)) from error

    @staticmethod
    def _group_snapshot(snapshot: AISnapshot, instruments: list[str]) -> AISnapshot:
        selected = set(instruments)
        return replace(
            snapshot,
            instruments=[row for row in snapshot.instruments if (row.get("id") or row.get("instId")) in selected],
            candles={key: rows for key, rows in snapshot.candles.items() if any(
                key == item or key.startswith(item + "/") or key.startswith(item + ":") for item in instruments
            )},
            tickers={key: row for key, row in snapshot.tickers.items() if key in selected},
            orderBook={key: row for key, row in snapshot.orderBook.items() if key in selected},
            fundingRates={key: row for key, row in snapshot.fundingRates.items() if key in selected},
            ai=dict(snapshot.ai, selectedInstruments=list(instruments), selectionCount=len(instruments), observationCount=len(instruments)),
        )

    @staticmethod
    def _coordinator_prompt(snapshot: AISnapshot, config: AIConfig, decisions: list[AIDecision]) -> str:
        reports = []
        for decision in decisions:
            row = decision.to_dict()
            reports.append({"tentativeDecision": {key: value for key, value in row.items() if key != "assessments"},
                            "assessments": row["assessments"]})
        return (
            "You are the final AI coordinator for NovaTrade. Return exactly ONE JSON decision matching the supplied schema. "
            "Never call tools, access files or place orders. Review ALL group reports and the original account/risk context. "
            + _SHARED_POLICY_RULES
            + "COORDINATOR OUTPUT: Choose at most one open/close/cancel action or hold. Do not return assessments; the server "
            "attaches the exact complete original per-contract reports. Do not revise or re-estimate those reports. For open, "
            "select an eligible report and copy its fields exactly; for close/cancel use the real current direction or order ID. "
            "Copy the exact snapshotId and write validUntil as a whole-second UTC ISO-8601 timestamp with a Z suffix.\n"
            "SERVER FRESHNESS:\n" + dumps(snapshot_freshness(snapshot, config)) + "\n"
            "SERVER ENTRY GATES:\n" + dumps(CodexRunner._entry_gates(snapshot, config)) + "\n"
            "OBSERVED CONTRACTS:\n" + dumps(snapshot.observed_instruments()) + "\n"
            "COPY EXACT SNAPSHOT ID:\n" + snapshot.snapshotId + "\n"
            "ACCOUNT:\n" + dumps(snapshot.account) + "\nRISK:\n" + dumps(snapshot.risk) + "\n"
            "CURRENT TICKERS:\n" + dumps({key: row for key, row in snapshot.tickers.items() if key in snapshot.observed_instruments()}) + "\n"
            "GROUP REPORTS:\n" + dumps(reports) + "\n"
        )

    async def _run_grouped(self, snapshot: AISnapshot, config: AIConfig, *, deadline: float) -> AIDecision:
        observed = snapshot.observed_instruments()
        groups = [observed[index:index + 4] for index in range(0, len(observed), 4)]
        semaphore = asyncio.Semaphore(4)
        clock = asyncio.get_running_loop()
        analysis_started = clock.time()
        analysis_budget = min(config.cliTimeoutSeconds, max(0.0, deadline - analysis_started) * _GROUPED_ANALYSIS_BUDGET_SHARE)
        analysis_deadline = analysis_started + analysis_budget
        self.last_run_metadata.update(
            stage="analysis", groupCount=len(groups), completedGroups=0,
            analysisBudgetSeconds=round(analysis_budget, 3),
        )

        async def analyze(instruments: list[str]) -> AIDecision:
            async with semaphore:
                group = self._group_snapshot(snapshot, instruments)
                decision = await self._run_single(
                    group, config, deadline=analysis_deadline,
                    prompt="This is an independent analysis group. Its tentative action is NOT an execution authorization; "
                           "the final coordinator will review the complete observation pool.\n"
                           + self._decision_prompt(group, config, encoding="compact20"),
                )
                self.last_run_metadata["completedGroups"] += 1
                return decision

        tasks = [asyncio.create_task(analyze(group)) for group in groups]
        batch = asyncio.gather(*tasks)
        try:
            try:
                decisions = await asyncio.wait_for(batch, timeout=max(0, analysis_deadline - clock.time()))
            except (asyncio.TimeoutError, CodexError) as error:
                self.last_run_metadata.update(failureStage="analysis")
                # A provider adapter may replace the telemetry dict mid-run, so
                # every workflow field is read defensively: a missing counter
                # must never hide the real failure behind a KeyError.
                completed = self.last_run_metadata.get("completedGroups", 0)
                raise CodexError(
                    f"{getattr(self, 'provider_label', 'Codex')} analysis phase failed ({completed}/{len(groups)} groups complete): "
                    f"{str(error) or 'timed out'}"
                ) from error
            finally:
                self.last_run_metadata["analysisDurationSeconds"] = round(clock.time() - analysis_started, 3)
            flattened_ids = [item.instrumentID for decision in decisions for item in decision.assessments]
            if len(flattened_ids) != len(observed) or len(set(flattened_ids)) != len(flattened_ids) or set(flattened_ids) != set(observed):
                raise CodexError("grouped assessments do not cover the observation pool exactly once")
            by_id = {item.instrumentID: item for decision in decisions for item in decision.assessments}
            assessments = [by_id[item] for item in observed]
            schema = decision_json_schema(snapshot.snapshotId, observed)
            del schema["properties"]["assessments"]
            schema["required"].remove("assessments")
            coordinator_started = clock.time()
            coordinator_budget = min(config.cliTimeoutSeconds, max(0.0, deadline - coordinator_started))
            coordinator_deadline = coordinator_started + coordinator_budget
            self.last_run_metadata.update(
                stage="coordinator", assessmentCount=len(assessments),
                coordinatorBudgetSeconds=round(coordinator_budget, 3),
            )
            try:
                raw = await asyncio.wait_for(
                    self._run_prompt(self._coordinator_prompt(snapshot, config, decisions), schema, config, deadline=coordinator_deadline),
                    timeout=max(0, coordinator_deadline - clock.time()),
                )
                if "assessments" in raw:
                    raise CodexError("final coordinator must not replace per-contract assessments")
                decision = replace(AIDecision.from_dict(raw), assessments=assessments)
                decision.require_complete_assessments(observed)
                return decision
            except (asyncio.TimeoutError, CodexError, SchemaError, KeyError, TypeError, ValueError) as error:
                self.last_run_metadata.update(failureStage="coordinator")
                fallback = replace(
                    AIDecision.hold(snapshot.snapshotId, "逐币评估已完成，最终决策失败；本轮不执行交易。",
                                    decision_id=snapshot.snapshotId + "-coordinator-failed"),
                    reasonCode="COORDINATOR_FAILED", assessments=assessments,
                )
                raise CodexError(
                    f"{getattr(self, 'provider_label', 'Codex')} coordinator phase failed ({len(groups)}/{len(groups)} groups complete): "
                    f"{str(error) or 'timed out'}", safe_decision=fallback,
                ) from error
            finally:
                self.last_run_metadata["coordinatorDurationSeconds"] = round(clock.time() - coordinator_started, 3)
        except (SchemaError, KeyError, TypeError, ValueError) as error:
            raise CodexError(str(error)) from error
        finally:
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            if batch.done() and not batch.cancelled():
                batch.exception()

    async def run(self, snapshot: AISnapshot, config: AIConfig) -> AIDecision:
        clock = asyncio.get_running_loop()
        started = clock.time()
        grouped = len(snapshot.observed_instruments()) > 4
        pipeline_budget = (
            _grouped_pipeline_budget_seconds(snapshot, config) if grouped
            else _freshness_bounded_budget(snapshot, config, config.cliTimeoutSeconds)
        )
        self.last_run_metadata = {
            "workflow": "grouped" if grouped else "single", "stage": "starting",
            "observedCount": len(snapshot.observed_instruments()), "groupCount": 0,
            "completedGroups": 0, "assessmentCount": 0,
            # Configured per-CLI ceilings.
            "analysisTimeoutSeconds": config.cliTimeoutSeconds,
            "coordinatorTimeoutSeconds": config.cliTimeoutSeconds if grouped else 0,
            # The budget actually available to the whole grouped pipeline after
            # the snapshot admission window is taken into account.
            "pipelineBudgetSeconds": round(pipeline_budget, 3),
        }
        try:
            # Preserve the whole-pool budget guard before ANY CLI starts.
            # Grouped analysis already divides the observation pool into
            # several model calls. Keep those calls small enough to finish
            # within the shared phase deadline; SERVER MARKET FACTS still use
            # the complete collected candle history.
            prompt = self._decision_prompt(snapshot, config, encoding="compact20" if grouped else "compact60")
            self._check_prompt_budget(prompt)
            if not grouped:
                self.last_run_metadata.update(stage="analysis", groupCount=1)
                decision = await self._run_single(snapshot, config, prompt=prompt, deadline=started + pipeline_budget)
                self.last_run_metadata.update(completedGroups=1, assessmentCount=len(decision.assessments), analysisDurationSeconds=round(clock.time() - started, 3))
            else:
                # cliTimeoutSeconds remains a per-CLI ceiling. The two phases
                # share pipelineBudgetSeconds, which never exceeds the snapshot
                # admission window: a decision produced after that window could
                # not authorize an entry, so spending longer than the window
                # would only consume provider budget. Order and snapshot
                # freshness are never extended by this budget.
                decision = await self._run_grouped(snapshot, config, deadline=started + pipeline_budget)
            self.last_run_metadata["stage"] = "complete"
            return decision
        except asyncio.CancelledError:
            self.last_run_metadata["stage"] = "cancelled"
            raise
        except Exception:
            self.last_run_metadata.setdefault("failureStage", self.last_run_metadata.get("stage", "starting"))
            self.last_run_metadata["stage"] = "failed"
            raise
        finally:
            self.last_run_metadata["durationSeconds"] = round(clock.time() - started, 3)

    @staticmethod
    def _entry_gates(snapshot: AISnapshot, config: AIConfig) -> dict[str, Any]:
        return {
            "primaryEntryInterval": PRIMARY_ENTRY_INTERVAL,
            "minimumPrimaryConfirmedCandles": MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES,
            "primaryEntryQuality": {
                instrument: primary_entry_quality(snapshot, instrument)
                for instrument in snapshot.observed_instruments()
            },
            "minimumConfidence": config.minimumConfidence,
            "minimumWinRate": MIN_OPEN_WIN_RATE,
            "minimumRiskRewardRatio": MIN_OPEN_RISK_REWARD_RATIO,
            "allowOpen": config.allowOpen,
            "allowClose": config.allowClose,
            "allowCancel": config.allowCancel,
            "requireStopLoss": config.requireStopLoss,
            "maxLeverage": config.maxLeverage,
            "marginPerOrderUSD": config.marginPerOrderUSD,
            "maxDailyOrders": config.maxDailyOrders,
            "maxDailyLosses": config.maxDailyLosses,
            "todayLossCount": snapshot.account.get("todayLossCount"),
            "todayAIOrderCount": snapshot.account.get("todayAIOrderCount"),
            "risk": snapshot.risk,
            "tradingMode": snapshot.ai.get("tradingMode"),
            "tradingAvailability": snapshot.ai.get("tradingAvailability"),
        }

    @staticmethod
    def _decision_prompt(
        snapshot: AISnapshot,
        config: AIConfig,
        *,
        now: datetime | None = None,
        encoding: str = "compact60",
    ) -> str:
        freshness = snapshot_freshness(snapshot, config, now=now)
        entry_gates = CodexRunner._entry_gates(snapshot, config)
        selected_encoding = (encoding or "compact60").strip().lower()
        if selected_encoding in {"compact20", "compact10"}:
            limit = 20 if selected_encoding == "compact20" else 10
            candle_encoding_note = (
                f"SNAPSHOT preserves all collected {PRIMARY_ENTRY_INTERVAL} rows (up to 60), including forming context. "
                f"Auxiliary candle series contain the latest {limit} confirmed rows plus the latest forming row when present; "
                "the local SERVER MARKET FACTS still use the complete collected history. "
            )
        else:
            selected_encoding = "compact60"
            candle_encoding_note = (
                "SNAPSHOT candle series may be encoded as {columns:[field names],rows:[[values]]}; every supplied row, "
                "field, precision and type is preserved. "
            )
        return (
            "You are a constrained trading decision engine. Return exactly one JSON decision matching the supplied schema. "
            "Never call tools, access files, place orders, or include prose. "
            + _SHARED_POLICY_RULES
            + "ANALYSIS OUTPUT: Return assessments for EVERY OBSERVED CONTRACT exactly once. For each assessment provide "
            "long/short/neutral direction, limitPrice, stopLossPrice, takeProfitPrice, optional one-to-four "
            "takeProfitLevels whose percentages sum to 100, winRate, riskRewardRatio, confidence, entryEligible, "
            "unmetConditions, and a specific concise Simplified Chinese reason. These are conditional plans, not placed orders. "
            "For adequate data, compare the strongest long and short scenarios and retain a directional conditional plan even "
            "when it is not ready to trade. Use neutral/null values only when data is genuinely unavailable or no defensible estimate exists. "
            "Do not inflate quality values to pass gates; list only the main blockers. "
            + candle_encoding_note
            + "Reconstruct columns/rows by pairing columns with row values; heterogeneous rows remain objects. "
            "A proposed limitPrice need not already be touched.\n"
            "SERVER FRESHNESS:\n" + json.dumps(freshness, ensure_ascii=False, separators=(",", ":")) + "\n"
            "SERVER ENTRY GATES (authoritative):\n" + json.dumps(entry_gates, ensure_ascii=False, separators=(",", ":")) + "\n"
            "OBSERVED CONTRACTS (complete assessment coverage required):\n" + json.dumps(snapshot.observed_instruments(), ensure_ascii=False, separators=(",", ":")) + "\n"
            "COPY EXACT SNAPSHOT ID:\n" + snapshot.snapshotId + "\n"
            "SERVER MARKET FACTS:\n" + dumps(market_facts(snapshot)) + "\n"
            "SNAPSHOT:\n" + dumps(_prompt_snapshot(snapshot, encoding=selected_encoding)) + "\n"
        )

    async def invoke(self, snapshot: AISnapshot, config: AIConfig) -> AIDecision:
        """Compatibility alias for callers that name the operation invoke."""
        return await self.run(snapshot, config)

    async def run_chat(self, message: str, config: AIConfig) -> AIChatResponse:
        """Ask Codex for a conversational reply and an optional safe config patch.

        This uses the same isolated subprocess boundary as trading decisions.
        The model receives no exchange credentials and cannot invoke an order
        gateway; its output is only a validated configuration suggestion.
        """
        with tempfile.TemporaryDirectory(prefix="novatrade-codex-chat-") as workdir:
            schema_path = Path(workdir) / "ai-chat.schema.json"
            schema_path.write_text(json.dumps(ai_chat_json_schema(), separators=(",", ":")), encoding="utf-8")
            prompt = (
                "You are NovaTrade's strategy copilot and conversational assistant. Return exactly one JSON object matching the supplied schema. "
                "You may have ordinary conversation, explain signals, market concepts, risk, and the current strategy, and suggest only safe strategy configuration fields. "
                "Never place, cancel, or modify orders; never change enabled state or demo/live mode. "
                "Set suggestion to null when the user is asking a question or the requested change is unsafe. "
                "Observation targets are a fixed allowlist. Only suggest allowedInstruments when the user explicitly names one or more complete "
                "*-USDT-SWAP contract IDs in the current message. Never infer or suggest a category, ranking, board, coin family, or dynamic universe. "
                "For add/remove/replace requests, return the final complete allowlist, using null for fields that should remain unchanged.\n"
                "CODEX RUNTIME (authoritative, do not guess): " + json.dumps(self.runtime_identity(), ensure_ascii=False) + "\n"
                "CURRENT CONFIG:\n" + dumps(config) + "\nUSER MESSAGE:\n" + message + "\n"
            )
            # Keep chat on the same controlled route as routine decisions and
            # disable configured MCP connections for a prompt-only exchange.
            model, reasoning_effort = self._route_values(config)
            self._validate_override(model, reasoning_effort)
            command = [
                self.executable, "exec", "--ephemeral", "--sandbox", "read-only",
                "--output-schema", str(schema_path), "--json", "--skip-git-repo-check",
                "--ignore-rules", "-m", model,
                "-c", f"model_reasoning_effort={reasoning_effort}",
                "-c", "mcp_servers={}", "-c", "plugins={}", "-C", workdir,
            ]
            try:
                process = await asyncio.create_subprocess_exec(
                    *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE, env=self._safe_environment(workdir), cwd=workdir,
                    start_new_session=True,
                )
                try:
                    stdout, stderr = await asyncio.wait_for(
                        process.communicate(prompt.encode("utf-8")), timeout=config.cliTimeoutSeconds
                    )
                except asyncio.TimeoutError as error:
                    await self._stop_timed_out_process(process)
                    raise CodexError("Codex CLI timed out") from error
            except FileNotFoundError as error:
                raise CodexError("Codex CLI executable is unavailable") from error
            if len(stdout) > config.maxOutputBytes or len(stderr) > config.maxOutputBytes:
                raise CodexError("Codex CLI output exceeded configured limit")
            if process.returncode != 0:
                detail = stderr.decode("utf-8", errors="replace")[:500]
                raise CodexError(f"Codex CLI exited with status {process.returncode}: {detail}")
            try:
                text = self._extract_agent_text(stdout.decode("utf-8", errors="strict"))
                return self._decode_chat_response(text)
            except (UnicodeDecodeError, SchemaError, CodexError) as error:
                raise CodexError(str(error)) from error


def parse_codex_output(output: str, snapshot_id: str = "unknown") -> AIDecision:
    """Parse CLI output and return a safe hold for malformed output."""
    try:
        text = CodexRunner._extract_agent_text(output)
        return AIDecision.from_dict(CodexRunner._decode_decision(text))
    except (CodexError, SchemaError, json.JSONDecodeError, UnicodeError, TypeError, ValueError) as error:
        return AIDecision.hold(snapshot_id, f"invalid Codex output: {error}")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _state_dir() -> Path:
    path = Path(os.getenv("NOVATRADE_STATE_DIR", str(Path.home() / "Library/Application Support/NovaTrade")))
    path.mkdir(parents=True, exist_ok=True)
    return path


class AIWorker:
    """Own the worker lifecycle, persistence, policy and order gateway call."""

    def __init__(
        self,
        snapshot_provider: Callable[[], Awaitable[AISnapshot | Mapping[str, Any]]] | None = None,
        order_gateway: Callable[[AIDecision, AISnapshot], Awaitable[Any]] | None = None,
        *,
        config: AIConfig | Mapping[str, Any] | None = None,
        runner: CodexRunnerProtocol | None = None,
        state_dir: Path | None = None,
        strategy_id: str = "codex",
    ) -> None:
        self.snapshot_provider = snapshot_provider
        self.order_gateway = order_gateway
        self.config = config if isinstance(config, AIConfig) else AIConfig.from_dict(config)
        if strategy_id != "codex":
            raise ValueError("AI strategy must be codex")
        self.strategy_id = "codex"
        self.runner = runner or CodexRunner()
        self.state_dir = state_dir or _state_dir()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.policy_state = PolicyState()
        self._last_event_fingerprint: str | None = None
        self._last_model_evaluated_at: datetime | None = None
        self._event_skip_count = 0
        self._event_mode_seen = False
        self.observed_instruments: list[str] = []
        self.observation_updated_at: str | None = None
        self.status = AIStatus(mode=self.config.mode, enabled=self.config.enabled, updatedAt=_now_iso())
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._schedule_changed = asyncio.Event()
        self._load_persisted()

    def _path(self, name: str) -> Path:
        return self.state_dir / name

    def _load_persisted(self) -> None:
        migrated_config = False
        persisted_fingerprint: str | None = None
        persisted_state: str | None = None
        persisted_failures = 0
        try:
            raw = json.loads(self._path("ai-config.json").read_text(encoding="utf-8"))
            normalized = dict(raw) if isinstance(raw, Mapping) else {}
            # Only the previous default scan interval is migrated. Customized
            # intervals remain intact, as do enablement and the failure latch
            # restored below before writing the normalized configuration.
            if (
                isinstance(normalized.get("decisionIntervalSeconds"), (int, float))
                and not isinstance(normalized.get("decisionIntervalSeconds"), bool)
                and float(normalized["decisionIntervalSeconds"]) == LEGACY_DEFAULT_DECISION_INTERVAL_SECONDS
            ):
                normalized["decisionIntervalSeconds"] = DEFAULT_DECISION_INTERVAL_SECONDS
            # maxDailyLosses=0 used to mean "no losing trade is acceptable",
            # which closed every entry from the first round (0 >= 0). allowOpen
            # already expresses "never open", so lift a persisted zero to the
            # usable minimum instead of discarding the rest of the settings.
            if normalized.get("maxDailyLosses") == 0 and not isinstance(normalized.get("maxDailyLosses"), bool):
                normalized["maxDailyLosses"] = 1
            # The original default was 45 seconds and was never exposed as a
            # user setting. Treat that persisted value as the old default so
            # existing installations receive the larger grouped-analysis budget
            # without requiring a manual reset.
            if (
                isinstance(normalized.get("cliTimeoutSeconds"), (int, float))
                and not isinstance(normalized.get("cliTimeoutSeconds"), bool)
                and float(normalized["cliTimeoutSeconds"]) == LEGACY_DEFAULT_CLI_TIMEOUT_SECONDS
            ):
                normalized["cliTimeoutSeconds"] = DEFAULT_CLI_TIMEOUT_SECONDS
            self.config = AIConfig.from_dict(normalized)
            migrated_config = isinstance(raw, Mapping) and raw != self.config.to_dict()
            self.status = AIStatus(mode=self.config.mode, enabled=self.config.enabled, updatedAt=_now_iso())
        except SchemaError:
            # A retired or invalid saved configuration must never enable the
            # remaining worker through a caller-supplied default.
            self.config = AIConfig()
            migrated_config = True
        except (OSError, ValueError, json.JSONDecodeError):
            pass
        # Keep the last concrete observation set visible across service
        # restarts. It is informational only; a fresh snapshot replaces it
        # before the next decision is evaluated.
        try:
            state = json.loads(self._path("ai-state.json").read_text(encoding="utf-8"))
            observed = state.get("observedInstruments", []) if isinstance(state, dict) else []
            if isinstance(observed, list):
                self.observed_instruments = list(dict.fromkeys(
                    item.strip() for item in observed
                    if isinstance(item, str) and item.strip()
                ))
            updated_at = state.get("observationUpdatedAt") if isinstance(state, dict) else None
            self.observation_updated_at = updated_at if isinstance(updated_at, str) else None
            # A restart always forces one model evaluation. Keep the persisted
            # fingerprint for diagnostics, but do not restore it as skip state.
            fingerprint = state.get("decisionFingerprint") if isinstance(state, dict) else None
            if isinstance(fingerprint, str) and fingerprint:
                persisted_fingerprint = fingerprint
            # A failure halt is a durable safety latch, not process memory.
            # Without restoring it, a crash loop or a restart would silently
            # resume trading after maxConsecutiveFailures was reached.
            raw_state = state.get("state") if isinstance(state, dict) else None
            if isinstance(raw_state, str) and raw_state in {"halted", "error"}:
                persisted_state = raw_state
            raw_failures = state.get("consecutiveFailures") if isinstance(state, dict) else None
            if isinstance(raw_failures, int) and not isinstance(raw_failures, bool) and raw_failures >= 0:
                persisted_failures = raw_failures
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            pass
        halted = persisted_state == "halted"
        self.status = replace(
            self.status,
            state=persisted_state or "stopped",
            mode=self.config.mode,
            # A restored halt always reports a disabled worker, matching what
            # run_once() enforces while halted.
            enabled=False if halted else self.config.enabled,
            consecutiveFailures=persisted_failures,
            updatedAt=_now_iso(),
            observedInstruments=list(self.observed_instruments),
            observationUpdatedAt=self.observation_updated_at,
            decisionFingerprint=persisted_fingerprint,
        )
        # lastDecision is not restored, so its snapshot universe must also
        # remain empty rather than being inferred from current observations.
        if migrated_config:
            # Persist the normalized fixed-universe representation so a
            # restart cannot restore a removed dynamic-board mode.
            self._persist()
        # Rehydrate decision IDs so a service restart cannot replay the same
        # accepted intent. The append-only audit is the durable policy ledger.
        try:
            for line in self._path("ai-decisions.jsonl").read_text(encoding="utf-8").splitlines()[-2000:]:
                event = json.loads(line)
                decision = event.get("decision") if isinstance(event, dict) else None
                if event.get("accepted") and isinstance(decision, dict) and decision.get("decisionId"):
                    self.policy_state.seenDecisionIds.add(str(decision["decisionId"]))
        except (OSError, json.JSONDecodeError, AttributeError):
            pass

    def _persist(self) -> None:
        path = self._path("ai-config.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.config.to_dict(), ensure_ascii=False), encoding="utf-8")
        temporary.replace(path)
        state_path = self._path("ai-state.json")
        state_tmp = state_path.with_suffix(".tmp")
        state = self.status.to_dict()
        if self._last_event_fingerprint is not None:
            state["decisionFingerprint"] = self._last_event_fingerprint
        state_tmp.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
        state_tmp.replace(state_path)

    def _audit(self, event: Mapping[str, Any]) -> None:
        with self._path("ai-decisions.jsonl").open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"at": _now_iso(), **dict(event)}, ensure_ascii=False, separators=(",", ":")) + "\n")

    def get_config(self) -> dict[str, Any]:
        return self.config.to_dict()

    def get_status(self) -> dict[str, Any]:
        return self.status.to_dict()

    @staticmethod
    def _snapshot_observed_instruments(snapshot: AISnapshot) -> list[str]:
        """Share the policy's resolution of the actual observation set."""
        return snapshot.observed_instruments()

    def _event_trigger(self, snapshot: AISnapshot) -> tuple[str, str | None]:
        """Return the event reason and fingerprint before a model call."""
        if not _event_driven_enabled():
            # Toggling the optimization off must not leave stale event state
            # that could suppress the first call after it is enabled again.
            self._event_mode_seen = False
            self._last_event_fingerprint = None
            self._last_model_evaluated_at = None
            self._event_skip_count = 0
            return "disabled", None
        if not self._event_mode_seen:
            self._event_mode_seen = True
            self._last_event_fingerprint = None
            self._last_model_evaluated_at = None
            self._event_skip_count = 0
        fingerprint = decision_fingerprint(snapshot, self.config)
        reason = trigger_reason(snapshot, self._last_event_fingerprint, fingerprint)
        if reason == "unchanged":
            if self._last_model_evaluated_at is None:
                reason = "watchdog"
            elif (datetime.now(timezone.utc) - self._last_model_evaluated_at).total_seconds() >= _event_max_skip_seconds():
                reason = "watchdog"
        return reason, fingerprint

    def _record_prescreen_skip(
        self, snapshot: AISnapshot, fingerprint: str, reason: str,
    ) -> PolicyResult:
        self._event_skip_count += 1
        decision = AIDecision.hold(
            snapshot.snapshotId,
            "关键特征未变化，本轮不调用模型。",
            decision_id=f"prescreen-{fingerprint[:20]}-{self._event_skip_count}",
        )
        freshness = snapshot_freshness(snapshot, self.config)
        now = _now_iso()
        self._last_event_fingerprint = fingerprint
        self.status = replace(
            self.status,
            updatedAt=now,
            observedInstruments=list(snapshot.observed_instruments()),
            observationUpdatedAt=snapshot.capturedAt,
            lastEvaluationSource="prescreen",
            lastEvaluationAt=now,
            skippedCycles=self._event_skip_count,
            decisionFingerprint=fingerprint,
        )
        self._audit({
            "type": "prescreen-skip", "reason": reason,
            "decisionSource": "prescreen", "snapshotId": snapshot.snapshotId,
            "decisionFingerprint": fingerprint, "freshness": freshness,
            "accepted": True, "policyReason": "prescreen unchanged",
            "decision": decision.to_dict(),
        })
        self._persist()
        return PolicyResult(True, decision, "prescreen unchanged")

    @staticmethod
    def _snapshot_quality(snapshot: AISnapshot, config: AIConfig) -> dict[str, Any]:
        """Keep collection diagnostics without recording prices or account data."""
        prompt_encoding = "compact20" if len(snapshot.observed_instruments()) > 4 else "compact60"
        prompt = CodexRunner._decision_prompt(snapshot, config, encoding=prompt_encoding)
        metadata = snapshot.dataFreshness
        duration = metadata.get("collectionDurationSeconds")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration < 0:
            duration = None
        errors = metadata.get("errors")
        compact_errors = []
        for error in errors[:50] if isinstance(errors, list) else []:
            if not isinstance(error, Mapping):
                continue
            compact = {
                key: value[:300] for key in ("resource", "instrumentID", "interval", "error")
                if isinstance(value := error.get(key), str)
            }
            attempts = error.get("attempts")
            if isinstance(attempts, int) and not isinstance(attempts, bool):
                compact["attempts"] = attempts
            compact_errors.append(compact)
        return {
            "instrumentCount": len(snapshot.observed_instruments()),
            "candleSeriesCount": len(snapshot.candles),
            "emptyCandleSeries": sorted(key for key, rows in snapshot.candles.items() if not rows),
            "orderBookCount": sum(
                1 for book in snapshot.orderBook.values()
                if isinstance(book, Mapping) and isinstance(book.get("bids"), list) and book["bids"]
                and isinstance(book.get("asks"), list) and book["asks"]
            ),
            "collectionDurationSeconds": duration,
            "collectionErrors": compact_errors,
            "collectionErrorCount": len(errors) if isinstance(errors, list) else 0,
            "promptBytes": len(prompt.encode("utf-8")),
            "promptBytesScope": "decision-prompt-only",
            "promptEncoding": prompt_encoding,
        }

    @classmethod
    def route_for_snapshot(cls, snapshot: AISnapshot, config: AIConfig) -> tuple[str, str, str]:
        """Use one fixed provider route for every snapshot."""
        model, effort = CodexRunner._route_values(config)
        CodexRunner._validate_override(model, effort)
        return model, effort, "fixed-model"

    async def chat(
        self,
        message: str,
        *,
        apply: bool = False,
        suggestion: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Return a configuration conversation without creating any order.

        ``apply`` is an explicit user action. Even when true, only the
        allowlisted strategy fields returned by ``AIChatResponse`` can be
        persisted; worker enablement, run mode, and account selection remain
        outside the conversational authority boundary.
        """
        if apply:
            # Apply exactly the previewed patch. Re-running the model during
            # confirmation could produce a different configuration.
            normalized = normalize_ai_chat_patch(suggestion)
            self.update_config(normalized)
            self._audit({
                "type": "chat", "applied": True, "suggestion": normalized,
                "messageLength": len(message),
            })
            return {
                "schemaVersion": 1,
                "reply": "配置建议已应用。",
                "suggestion": normalized,
                "applied": True,
                "config": self.get_config(),
            }

        # Answer deterministic control-plane questions locally. This avoids
        # spending a model round trip on metadata and exact observation-set
        # commands that have an exact server-side meaning.
        if re.search(r"(模型|model|版本)", message, re.IGNORECASE):
            identity = getattr(self.runner, "runtime_identity", lambda: {"model": "Codex 默认模型", "provider": "default"})()
            route_model = self.config.routineModel
            route_effort = self.config.routineReasoningEffort
            return {
                "schemaVersion": 1,
                "reply": (
                    f"AI 策略固定使用 {route_model}（{route_effort}）。"
                    f"服务提供方是 {identity['provider']}；"
                    f"CLI 全局默认模型 {identity['model']} 不会被自动轮询继承。"
                ),
                "suggestion": None,
                "applied": False,
                "config": self.get_config(),
            }
        observation = self._observation_chat_result(message)
        if observation is not None:
            suggestion, reply = observation
            self._audit({
                "type": "chat", "applied": False, "suggestion": suggestion,
                "messageLength": len(message), "deterministic": True,
            })
            return {
                "schemaVersion": 1,
                "reply": reply,
                "suggestion": suggestion,
                "applied": False,
                "config": self.get_config(),
            }

        operation = getattr(self.runner, "run_chat", None)
        if operation is None:
            return self._chat_degraded_response(message, "Codex runner does not support chat")
        try:
            response = await operation(message, self.config)
            if isinstance(response, str):
                response = AIChatResponse(schemaVersion=1, reply=response[:8000], suggestion=None)
            elif not isinstance(response, AIChatResponse):
                response = AIChatResponse.from_dict(response)
            normalized = normalize_ai_chat_patch(response.suggestion)
            # A model response may only change the observation set when the
            # user named exact contract IDs in this message. This closes the
            # old path where a broad request such as "热门币" could produce a
            # dynamic or guessed allowlist.
            if normalized.get("allowedInstruments"):
                mentioned = set(self._extract_contract_ids(message))
                current = set(self.config.allowedInstruments)
                observation_intent = bool(re.search(
                    r"观察(?:池|列表|范围|标的|币种|合约)|监控(?:池|列表|范围|标的|币种|合约)|关注(?:池|列表|范围|标的|币种|合约)|白名单|交易范围|添加|增加|新增|加入|纳入|删除|移除|去掉|剔除|取消|覆盖|替换|改为|改成|设置为|add|remove|delete|replace",
                    message,
                    re.IGNORECASE,
                )) or bool(re.search(
                    r"(?:^|我想|请|帮我|想要)\s*(?:观察|监控|关注)\s*(?:一下\s*)?(?=[A-Za-z0-9]+-USDT-SWAP)",
                    message,
                    re.IGNORECASE,
                ))
                if not observation_intent or not mentioned or not set(normalized["allowedInstruments"]).issubset(current | mentioned):
                    normalized.pop("allowedInstruments", None)
        except Exception as error:
            # Conversation is informational and must remain usable when the
            # optional model process is unavailable. Configuration is never
            # changed on this path; callers can retry without losing context.
            return self._chat_degraded_response(message, str(error))
        self._audit({
            "type": "chat", "applied": False, "suggestion": normalized or None,
            "messageLength": len(message),
        })
        return {
            "schemaVersion": 1,
            "reply": response.reply,
            "suggestion": normalized or None,
            "applied": False,
            "config": self.get_config(),
        }

    @staticmethod
    def _extract_contract_ids(message: str) -> list[str]:
        """Extract only complete, explicitly named linear swap IDs."""
        matches = re.findall(
            r"(?<![A-Za-z0-9])([A-Za-z0-9]+-USDT-SWAP)(?![A-Za-z0-9])",
            message,
            flags=re.IGNORECASE,
        )
        # Reuse the schema's canonicalization and strict suffix check. A
        # malformed token is deliberately ignored here so it can be reported
        # as a broad/ambiguous request instead of becoming a target.
        try:
            return normalize_contract_ids([item.upper() for item in matches], "chat.message.contracts")
        except SchemaError:
            return []

    def _observation_chat_result(self, message: str) -> tuple[dict[str, Any] | None, str] | None:
        """Resolve explicit add/remove/replace requests without a model call.

        Observation changes are intentionally deterministic. A category or
        ranking word without at least one exact USDT swap ID is rejected
        instead of being interpreted as a dynamic universe.
        """
        ids = self._extract_contract_ids(message)
        target_words = r"观察(?:池|列表|范围|标的|币种|合约|哪些|哪几|哪种)|监控(?:池|列表|范围|币|合约|标的)|白名单|交易范围|关注(?:池|列表|范围|币|合约|标的)|添加|增加|新增|加入|纳入|删除|移除|去掉|剔除|取消|覆盖|替换|改为|改成|设置为|add|remove|delete|replace"
        broad_words = r"热门|涨幅|成交额|主流|山寨|榜|动态|扫描|筛选|挑选|高价值|全部|所有|任意|自动选"
        target_request = bool(re.search(target_words, message, re.IGNORECASE)) or bool(re.search(
            r"(?:^|我想|请|帮我|想要)\s*(?:观察|监控|关注)\s*(?:一下\s*)?(?=[A-Za-z0-9]+-USDT-SWAP)",
            message,
            re.IGNORECASE,
        ))
        broad_request = bool(re.search(broad_words, message, re.IGNORECASE))

        if not ids:
            broad_target = broad_request and bool(re.search(r"币|标的|合约|观察|监控", message, re.IGNORECASE))
            if target_request or broad_target:
                return None, "观察标的必须明确写出完整合约 ID，例如 BTC-USDT-SWAP、ETH-USDT-SWAP；不支持热门榜、涨幅榜或其他宽泛分类。"
            return None
        # Exact IDs in an ordinary market question are context, not a config
        # mutation. Only target/config language enters the deterministic path.
        if not target_request:
            return None

        current = list(self.config.allowedInstruments)
        if not current:
            # An empty legacy allowlist has always meant the safe default BTC
            # observation. Treat it as the current set for add/remove so the
            # user's operation has an unsurprising result.
            current = ["BTC-USDT-SWAP"]
        operation_patterns = {
            "remove": r"删除|移除|去掉|剔除|取消|remove|delete",
            "add": r"添加|增加|新增|加入|纳入|add",
            "replace": r"覆盖|替换|改为|改成|设置为|replace",
        }
        operation_matches = sorted(
            (match.start(), match.end(), operation)
            for operation, pattern in operation_patterns.items()
            for match in re.finditer(pattern, message, re.IGNORECASE)
        )
        operation_types = {item[2] for item in operation_matches}
        if len(operation_types) > 1:
            # Support a clear combined command such as
            # “删除 BTC-USDT-SWAP 并增加 ETH-USDT-SWAP”. Each operation owns
            # the IDs between its verb and the next operation verb.
            by_operation: dict[str, list[str]] = {key: [] for key in operation_patterns}
            for index, (_, end, operation) in enumerate(operation_matches):
                next_start = operation_matches[index + 1][0] if index + 1 < len(operation_matches) else len(message)
                by_operation[operation].extend(self._extract_contract_ids(message[end:next_start]))
            assigned = set(item for values in by_operation.values() for item in values)
            if assigned != set(ids):
                return None, "请把增加、删除或覆盖分别写清，并在每个操作后列出完整的 *-USDT-SWAP 合约 ID。"
            replace_ids = list(dict.fromkeys(by_operation["replace"]))
            final = replace_ids if replace_ids else list(current)
            final = [item for item in final if item not in set(by_operation["remove"])]
            final.extend(item for item in by_operation["add"] if item not in final)
            if not final:
                return None, "固定观察池至少要保留一个合约；删除请求会清空观察池，请改为覆盖成至少一个明确的 *-USDT-SWAP 合约。"
            return {"allowedInstruments": final}, f"已按你的要求更新固定观察合约：{', '.join(final)}。请确认应用配置。"

        if operation_types == {"remove"}:
            operation = "remove"
        elif operation_types == {"add"}:
            operation = "add"
        else:
            operation = "replace"

        if operation == "add":
            final = list(dict.fromkeys([*current, *ids]))
            verb = "增加"
        elif operation == "remove":
            final = [item for item in current if item not in set(ids)]
            verb = "删除"
            if not final:
                return None, "固定观察池至少要保留一个合约；删除请求会清空观察池，请改为覆盖成至少一个明确的 *-USDT-SWAP 合约。"
        else:
            final = ids
            verb = "覆盖"
        return {"allowedInstruments": final}, f"已按你的要求{verb}固定观察合约：{', '.join(final)}。请确认应用配置。"

    def _chat_degraded_response(self, message: str, error: str) -> dict[str, Any]:
        """Return a stable response when the optional chat model fails."""
        detail = (error or "unknown error").strip()[:500]
        self._audit({
            "type": "chat", "applied": False, "degraded": True,
            "error": detail, "messageLength": len(message),
        })
        return {
            "schemaVersion": 1,
            "reply": "我暂时无法连接 AI 对话服务，但不会影响当前策略或订单。你可以稍后重试；配置建议只有在明确确认后才会应用。",
            "suggestion": None,
            "applied": False,
            "config": self.get_config(),
            "degraded": True,
            "errorCode": "codex_unavailable",
        }

    def update_config(self, values: Mapping[str, Any], *, clear_halt: bool = True) -> dict[str, Any]:
        """Merge a configuration patch.

        ``clear_halt`` defaults to true because an explicit configuration
        update from the operator is the documented recovery from a failure
        halt. The internal pause path (``disable``) passes false so that
        disabling and re-enabling a halted worker cannot silently clear the
        latch without a review.
        """
        previous_interval = self.config.decisionIntervalSeconds
        merged = self.config.to_dict()
        merged.update(dict(values))
        self.config = AIConfig.from_dict(merged)
        if self.config.decisionIntervalSeconds != previous_interval:
            self._schedule_changed.set()
        if any(key in values for key in ("allowedInstruments", "universeMode", "candidateLimit", "selectionLimit")):
            # The previous snapshot no longer describes the requested
            # universe. Clear it until the next cycle resolves fresh symbols.
            self.observed_instruments = []
            self.observation_updated_at = None
        # Configuration changes are decision-relevant events. Force the next
        # enabled cycle through the model instead of reusing a prior fingerprint.
        self._last_event_fingerprint = None
        self._last_model_evaluated_at = None
        self._event_skip_count = 0
        halted = self.status.state == "halted"
        self.status = replace(
            self.status,
            state="stopped" if (halted and clear_halt) else self.status.state,
            consecutiveFailures=0 if clear_halt else self.status.consecutiveFailures,
            mode=self.config.mode,
            enabled=self.config.enabled,
            updatedAt=_now_iso(),
            observedInstruments=list(self.observed_instruments),
            observationUpdatedAt=self.observation_updated_at,
            skippedCycles=0,
            decisionFingerprint=None,
        )
        self._persist()
        return self.get_config()

    async def run_once(self, snapshot: AISnapshot | Mapping[str, Any] | None = None) -> PolicyResult:
        if self.status.state == "halted":
            return PolicyResult(False, AIDecision.hold("unknown", "AI worker is halted"), "AI worker is halted")
        if not self.config.enabled or self.config.mode in {"disabled", "halted"}:
            return PolicyResult(False, AIDecision.hold("unknown", "AI worker is disabled"), "AI worker is disabled")
        self.status = replace(
            self.status,
            state="running",
            mode=self.config.mode,
            enabled=True,
            updatedAt=_now_iso(),
            observedInstruments=list(self.observed_instruments),
            observationUpdatedAt=self.observation_updated_at,
            lastDecisionInstruments=list(self.status.lastDecisionInstruments),
            lastDecisionFreshness=dict(self.status.lastDecisionFreshness),
        )
        parsed_snapshot: AISnapshot | None = None
        workflow_audited = False
        try:
            if snapshot is None:
                if self.snapshot_provider is None:
                    raise RuntimeError("snapshot provider is not configured")
                snapshot = await self.snapshot_provider()
            parsed_snapshot = snapshot if isinstance(snapshot, AISnapshot) else AISnapshot.from_dict(snapshot)
            self.observed_instruments = self._snapshot_observed_instruments(parsed_snapshot)
            self.observation_updated_at = parsed_snapshot.capturedAt
            decision_instruments = list(self.observed_instruments)
            trigger, fingerprint = self._event_trigger(parsed_snapshot)
            if fingerprint is not None and trigger == "unchanged":
                if _event_driven_mode() == "on":
                    return self._record_prescreen_skip(parsed_snapshot, fingerprint, trigger)
                self._audit({
                    "type": "prescreen-candidate", "reason": trigger,
                    "decisionSource": "prescreen-shadow", "snapshotId": parsed_snapshot.snapshotId,
                    "decisionFingerprint": fingerprint,
                })
            operation = getattr(self.runner, "run", None) or getattr(self.runner, "invoke", None)
            if operation is None:
                raise CodexError("Codex runner has no run/invoke operation")
            model, reasoning_effort, route_reason = self.route_for_snapshot(parsed_snapshot, self.config)
            # Keep routing visible in the audit trail.  The runner receives a
            # normal AIConfig, so fake runners and custom providers remain
            # compatible with the existing protocol.
            self._audit({
                "type": "model-route", "model": model,
                "reasoningEffort": reasoning_effort, "reason": route_reason,
                "snapshotId": parsed_snapshot.snapshotId,
                "snapshotQuality": self._snapshot_quality(parsed_snapshot, self.config),
                "structure": structure_identity(parsed_snapshot),
            })
            decision = await operation(parsed_snapshot, self.config)
            self._audit_analysis_workflow(parsed_snapshot)
            workflow_audited = True
            evaluated_at = datetime.now(timezone.utc)
            freshness = snapshot_freshness(parsed_snapshot, self.config, now=evaluated_at)
            result = validate_decision(decision, parsed_snapshot, self.config, self.policy_state, now=evaluated_at)
            self._audit({"type": "decision", "accepted": result.accepted, "reason": result.reason, "decision": result.decision.to_dict(), "rawDecision": decision.to_dict(), "snapshotId": parsed_snapshot.snapshotId, "instruments": decision_instruments, "freshness": freshness})
            # A policy rejection is still a completed, deterministic model
            # evaluation. Advance the event state so a static signal does not
            # spend one model request per poll on the same rejected intent.
            # Unknown/invalid snapshots and runner failures never reach this
            # point, and gateway failures below still leave the fingerprint
            # unchanged so they are retried on the next poll.
            if fingerprint is not None and not result.accepted:
                self._last_event_fingerprint = fingerprint
                self._last_model_evaluated_at = evaluated_at
                self._event_skip_count = 0
                self.status = replace(
                    self.status,
                    lastEvaluationSource="model",
                    lastEvaluationAt=_now_iso(),
                    skippedCycles=0,
                    decisionFingerprint=fingerprint,
                )
            failures = self.status.consecutiveFailures
            self.status = replace(
                self.status,
                state="running",
                mode=self.config.mode,
                enabled=True,
                consecutiveFailures=failures,
                lastDecisionAt=_now_iso(),
                lastError=None,
                lastDecision=result.decision.to_dict(),
                updatedAt=_now_iso(),
                observedInstruments=list(self.observed_instruments),
                observationUpdatedAt=self.observation_updated_at,
                lastDecisionInstruments=decision_instruments,
                lastDecisionFreshness=freshness,
            )
            # Keep the evaluated setup visible even if the downstream gateway
            # fails. Its assessments must stay paired with this snapshot.
            # A hold is still a complete model evaluation. The execution
            # gateway uses that cycle to reconcile existing positions and
            # restore missing protection; it must not be treated as a no-op.
            should_reconcile = (
                result.accepted
                and self.order_gateway is not None
                and self.config.mode != "shadow"
                and (result.decision.action != "hold" or managed_state(parsed_snapshot))
            )
            if should_reconcile:
                await self.order_gateway(result.decision, parsed_snapshot)
                # A gateway return is the only point at which an entry is
                # known to have been submitted. In particular, an
                # OrderNotSubmittedError must not record a submitted entry.
                record_decision(self.policy_state, result, at=evaluated_at)
            if fingerprint is not None:
                self.status = replace(
                    self.status,
                    lastEvaluationSource="model",
                    lastEvaluationAt=_now_iso(),
                    skippedCycles=0,
                )
            if result.accepted:
                if fingerprint is not None:
                    self._last_event_fingerprint = fingerprint
                    self._last_model_evaluated_at = evaluated_at
                    self._event_skip_count = 0
                self.status = replace(self.status, consecutiveFailures=0)
                if fingerprint is not None:
                    self.status = replace(self.status, decisionFingerprint=fingerprint)
                else:
                    self.status = replace(
                        self.status,
                        lastEvaluationSource="model",
                        lastEvaluationAt=_now_iso(),
                        skippedCycles=0,
                        decisionFingerprint=None,
                    )
            self._persist()
            return result
        except Exception as error:
            message = str(error)[:500]
            if parsed_snapshot is not None and not workflow_audited:
                self._audit_analysis_workflow(parsed_snapshot)
            failures = self.status.consecutiveFailures + 1
            mode = "halted" if failures >= self.config.maxConsecutiveFailures else self.config.mode
            enabled = False if mode == "halted" else self.config.enabled
            safe_decision = error.safe_decision if isinstance(error, CodexError) else None
            if parsed_snapshot is None:
                safe_decision = None
            if safe_decision is not None and parsed_snapshot is not None:
                try:
                    safe_decision = AIDecision.from_dict(safe_decision.to_dict())
                    safe_decision.require_complete_assessments(parsed_snapshot.observed_instruments())
                    if safe_decision.action != "hold" or safe_decision.snapshotId != parsed_snapshot.snapshotId:
                        safe_decision = None
                except (SchemaError, TypeError, ValueError):
                    safe_decision = None
            if safe_decision is not None and parsed_snapshot is not None:
                freshness = snapshot_freshness(parsed_snapshot, self.config)
                self.status = replace(
                    self.status, lastDecisionAt=_now_iso(), lastDecision=safe_decision.to_dict(),
                    lastDecisionInstruments=list(parsed_snapshot.observed_instruments()),
                    lastDecisionFreshness=freshness,
                )
                self._audit({"type": "decision", "accepted": False, "reason": message,
                             "decision": safe_decision.to_dict(), "rawDecision": None,
                             "snapshotId": parsed_snapshot.snapshotId,
                             "instruments": list(parsed_snapshot.observed_instruments()), "freshness": freshness})
            self.status = replace(
                self.status,
                state="halted" if mode == "halted" else "error",
                mode=mode,
                enabled=enabled,
                consecutiveFailures=failures,
                lastError=message,
                updatedAt=_now_iso(),
                observedInstruments=list(self.observed_instruments),
                observationUpdatedAt=self.observation_updated_at,
                lastDecisionInstruments=list(self.status.lastDecisionInstruments),
                lastDecisionFreshness=dict(self.status.lastDecisionFreshness),
            )
            self._audit({"type": "error", "error": message, "failures": failures})
            self._persist()
            return PolicyResult(False, safe_decision or AIDecision.hold("unknown", message), message)

    def _audit_analysis_workflow(self, snapshot: AISnapshot) -> None:
        metadata = getattr(self.runner, "last_run_metadata", None)
        if not isinstance(metadata, Mapping) or not metadata:
            return
        compact: dict[str, Any] = {}
        for key in ("workflow", "stage", "failureStage", "outcome"):
            value = metadata.get(key)
            allowed = {"single", "grouped"} if key == "workflow" else {"starting", "analysis", "coordinator", "complete", "failed", "cancelled", "success", "error"}
            if isinstance(value, str) and value in allowed:
                compact[key] = value
        for key in ("provider", "model", "reasoningEffort"):
            value = metadata.get(key)
            if key in metadata:
                compact[key] = value if isinstance(value, str) and value.strip() and len(value) <= 256 else None
        for key in ("observedCount", "groupCount", "completedGroups", "assessmentCount"):
            if key in metadata:
                value = metadata.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    compact[key] = value
        for key in ("analysisTimeoutSeconds", "coordinatorTimeoutSeconds", "analysisDurationSeconds",
                    "coordinatorDurationSeconds", "durationSeconds", "pipelineBudgetSeconds",
                    "analysisBudgetSeconds", "coordinatorBudgetSeconds"):
            if key in metadata:
                value = metadata.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    compact[key] = value
        for key in ("requestId", "inputTokens", "outputTokens", "reasoningTokens", "cacheReadTokens",
                    "cacheWriteTokens", "basePromptBytes", "strictContractBytes", "schemaBytes",
                    "modelPayloadBytes"):
            if key in metadata:
                value = metadata.get(key)
                compact[key] = value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
        if compact:
            self._audit({"type": "analysis-workflow", "snapshotId": snapshot.snapshotId, "workflow": compact})

    async def _loop(self) -> None:
        clock = asyncio.get_running_loop()
        while not self._stop.is_set():
            cycle_started = clock.time()
            await self.run_once()
            if self.status.state == "halted":
                break
            while not self._stop.is_set():
                self._schedule_changed.clear()
                interval = self.config.decisionIntervalSeconds
                elapsed = max(0.0, clock.time() - cycle_started)
                # Scan slots are measured from the previous start. Skip any
                # slots consumed by an overrun rather than immediately
                # launching catch-up calls against the model.
                next_slot = max(1, math.ceil(elapsed / interval))
                remaining = max(0.0, cycle_started + next_slot * interval - clock.time())
                if remaining == 0:
                    break
                try:
                    await asyncio.wait_for(self._schedule_changed.wait(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                # A changed interval recalculates the deadline from the same
                # start; it does not cause an extra immediate scan.

    async def start(self) -> None:
        if self.status.state == "halted":
            # A failure halt is cleared only by an explicit configuration
            # update (update_config resets the latch). Starting must never
            # silently resume trading after maxConsecutiveFailures, so persist
            # the halted status and leave the worker stopped.
            self.status = replace(self.status, enabled=False, updatedAt=_now_iso())
            self._persist()
            return
        if self._task is None or self._task.done():
            self._stop.clear()
            self.status = replace(
                self.status,
                state="running",
                mode=self.config.mode,
                enabled=self.config.enabled,
                updatedAt=_now_iso(),
                observedInstruments=list(self.observed_instruments),
                observationUpdatedAt=self.observation_updated_at,
                lastDecisionInstruments=list(self.status.lastDecisionInstruments),
                lastDecisionFreshness=dict(self.status.lastDecisionFreshness),
            )
            self._persist()
            self._task = asyncio.create_task(self._loop(), name="novatrade-ai-worker")

    async def stop(self) -> None:
        self._stop.set()
        self._schedule_changed.set()
        task = self._task
        if task is None:
            return
        # The loop may currently be awaiting a provider process for up to the
        # configured model timeout. Setting the event alone cannot interrupt
        # that await, which would make emergency flatten wait for the model
        # before it can reach the order gateway. Provider runners handle
        # CancelledError by terminating their child process and draining it.
        self._task = None
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def disable(self) -> None:
        # A pause must not clear a failure halt: disabling and re-enabling would
        # otherwise resume trading without the configuration review the halt
        # requires.
        self.update_config({"enabled": False, "mode": "disabled"}, clear_halt=False)
        await self.stop()
        self.status = replace(
            self.status,
            state="halted" if self.status.state == "halted" else "stopped",
            mode="disabled",
            enabled=False,
            updatedAt=_now_iso(),
            observedInstruments=list(self.observed_instruments),
            observationUpdatedAt=self.observation_updated_at,
            lastDecisionInstruments=list(self.status.lastDecisionInstruments),
            lastDecisionFreshness=dict(self.status.lastDecisionFreshness),
        )
        self._persist()
