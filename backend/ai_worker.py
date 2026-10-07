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
    from .ai_market_facts import market_facts
    from .ai_trigger import decision_fingerprint, managed_state, structure_identity, trigger_reason
    from .deepseek_harness import DeepSeekHarnessError, DeepSeekHarnessRunner as DeepSeekACP
    from .ai_policy import MIN_OPEN_WIN_RATE, MIN_OPEN_RISK_REWARD_RATIO, PolicyResult, PolicyState, record_decision, snapshot_freshness, validate_decision
    from .ai_schema import (
        AIDecision, AIChatResponse, AIConfig, AISnapshot, AIStatus, SchemaError,
        DEFAULT_CLI_TIMEOUT_SECONDS, LEGACY_DEFAULT_CLI_TIMEOUT_SECONDS,
        FIXED_AI_MODEL, FIXED_AI_REASONING_EFFORT,
        ai_chat_json_schema, decision_json_schema, dumps, normalize_ai_chat_patch,
        normalize_contract_ids,
    )
except ImportError:  # launched from bundled backend/main.py as a script
    from ai_market_facts import market_facts
    from ai_trigger import decision_fingerprint, managed_state, structure_identity, trigger_reason
    from deepseek_harness import DeepSeekHarnessError, DeepSeekHarnessRunner as DeepSeekACP
    from ai_policy import MIN_OPEN_WIN_RATE, MIN_OPEN_RISK_REWARD_RATIO, PolicyResult, PolicyState, record_decision, snapshot_freshness, validate_decision
    from ai_schema import (
        AIDecision, AIChatResponse, AIConfig, AISnapshot, AIStatus, SchemaError,
        DEFAULT_CLI_TIMEOUT_SECONDS, LEGACY_DEFAULT_CLI_TIMEOUT_SECONDS,
        FIXED_AI_MODEL, FIXED_AI_REASONING_EFFORT,
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
_MAX_DECISION_PROMPT_BYTES = 1_000_000


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
    removed only from the model prompt in the default compact mode; all fields
    and all row values remain present. ``compact20``/``compact10`` are explicit
    experimental window modes and retain the latest forming row. Heterogeneous
    objects stay objects to preserve absent-vs-null.
    """
    selected_encoding = (encoding or os.getenv("NOVATRADE_DEEPSEEK_ENCODING", "compact60")).strip().lower()
    result = snapshot.to_dict()
    for key, rows in result["candles"].items():
        if not rows:
            continue
        if selected_encoding in {"compact20", "compact10"}:
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
            "Never call tools, access files or place orders. Independent groups have analyzed all observed contracts; "
            "their tentative decisions are recommendations only, and no order has been submitted. Review ALL group reports "
            "and original account/risk context, then choose at most one open/close/cancel action or hold. "
            "Do not return assessments: the server attaches the exact complete original per-contract reports. "
            "Do not revise, invent or re-estimate those assessments. For open, select an entryEligible assessment with "
            "no unmetConditions. When SERVER ENTRY GATES.tradingAvailability is present, the selected contract must "
            "also have available=true in the current authenticated account; false/unknown cannot open even if "
            "an assessment says eligible. Copy its instrumentID, direction, winRate, riskRewardRatio, confidence, stopLossPrice, "
            "takeProfitPrice, optional takeProfitLevels and (for a limit order) limitPrice EXACTLY into this decision. Its setup must meet every current "
            "SERVER ENTRY GATE, including allowed opening, confidence/winRate/RR, required stop, margin/leverage and daily loss/order limits. "
            "A justified resting limit order does not require the market to have touched its entry price. "
            "Closing/cancelling existing positions/orders remain possible under allowClose/allowCancel even if no new entry is eligible. "
            "When a current position exists, do not choose another open action for that instrument; first evaluate whether the "
            "latest market evidence has materially invalidated the setup or exposed an earlier order mistake. In that case, "
            "choose close with the actual position direction, reasonCode=THESIS_INVALIDATED or ORDER_MISTAKE, and at least 8 "
            "characters of concrete evidence in reason. When a current pending order exists, "
            "evaluate whether the setup has materially changed or the order was mistaken; in that case choose cancel with "
            "the actual pending order ID, reasonCode=THESIS_INVALIDATED or ORDER_MISTAKE, and at least 8 characters of concrete evidence in reason. Early close/cancel is allowed before the original take-profit or stop-loss only for "
            "one of those reasons, never as a routine duplicate replacement. "
            "For existing positions, missing protection should be restored from the latest assessment. Adjusting an existing "
            "protection line requires high confidence and clear materially changed evidence; ordinary noise is insufficient. "
            "Use the actual account positions and pending order IDs for close/cancel; never fabricate an order ID. "
            "SERVER FRESHNESS is authoritative; when valid=true and isStale=false do not claim snapshot expiry. "
            "No missing private/risk count may be assumed zero. Unknown or blocked entry gates require hold for new entries. "
            "Set leverage between 1 and maxLeverage for open. Copy the exact snapshotId below. "
            "Write validUntil as whole-second UTC ISO-8601 with Z; reason in concise Simplified Chinese (at most60 characters).\n"
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
        analysis_deadline = min(analysis_started + config.cliTimeoutSeconds, deadline)
        self.last_run_metadata.update(stage="analysis", groupCount=len(groups), completedGroups=0)

        async def analyze(instruments: list[str]) -> AIDecision:
            async with semaphore:
                group = self._group_snapshot(snapshot, instruments)
                decision = await self._run_single(
                    group, config, deadline=analysis_deadline,
                    prompt="This is an independent analysis group. Its tentative action is NOT an execution authorization; "
                           "the final coordinator will review the complete observation pool.\n" + self._decision_prompt(group, config),
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
                raise CodexError(
                    f"{getattr(self, 'provider_label', 'Codex')} analysis phase failed ({self.last_run_metadata['completedGroups']}/{len(groups)} groups complete): "
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
            self.last_run_metadata.update(stage="coordinator", assessmentCount=len(assessments))
            coordinator_started = clock.time()
            coordinator_deadline = min(coordinator_started + config.cliTimeoutSeconds, deadline)
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
        self.last_run_metadata = {
            "workflow": "grouped" if grouped else "single", "stage": "starting",
            "observedCount": len(snapshot.observed_instruments()), "groupCount": 0,
            "completedGroups": 0, "assessmentCount": 0,
            "analysisTimeoutSeconds": config.cliTimeoutSeconds,
            "coordinatorTimeoutSeconds": config.cliTimeoutSeconds if grouped else 0,
        }
        try:
            # Preserve the whole-pool budget guard before ANY CLI starts.
            prompt = self._decision_prompt(snapshot, config)
            self._check_prompt_budget(prompt)
            if not grouped:
                self.last_run_metadata.update(stage="analysis", groupCount=1)
                decision = await self._run_single(snapshot, config, prompt=prompt, deadline=started + config.cliTimeoutSeconds)
                self.last_run_metadata.update(completedGroups=1, assessmentCount=len(decision.assessments), analysisDurationSeconds=round(clock.time() - started, 3))
            else:
                # cliTimeoutSeconds remains a per-CLI ceiling. Parallel group
                # analysis shares one such phase; the final CLI has its own
                # phase. Neither snapshot nor order freshness is extended.
                decision = await self._run_grouped(snapshot, config, deadline=started + 2 * config.cliTimeoutSeconds)
            self.last_run_metadata["stage"] = "complete"
            return decision
        except asyncio.CancelledError:
            self.last_run_metadata["stage"] = "cancelled"
            raise
        except Exception:
            self.last_run_metadata.setdefault("failureStage", self.last_run_metadata["stage"])
            self.last_run_metadata["stage"] = "failed"
            raise
        finally:
            self.last_run_metadata["durationSeconds"] = round(clock.time() - started, 3)

    @staticmethod
    def _entry_gates(snapshot: AISnapshot, config: AIConfig) -> dict[str, Any]:
        return {
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
            "cooldownSeconds": config.cooldownSeconds,
            "todayLossCount": snapshot.account.get("todayLossCount"),
            "todayAIOrderCount": snapshot.account.get("todayAIOrderCount"),
            "risk": snapshot.risk,
            "tradingMode": snapshot.ai.get("tradingMode"),
            "tradingAvailability": snapshot.ai.get("tradingAvailability"),
        }

    @staticmethod
    def _decision_prompt(snapshot: AISnapshot, config: AIConfig, *, now: datetime | None = None) -> str:
        freshness = snapshot_freshness(snapshot, config, now=now)
        entry_gates = CodexRunner._entry_gates(snapshot, config)
        # The compact candle experiment belongs only to the DeepSeek route.
        # A DeepSeek tuning environment variable must never silently alter the
        # GPT/Codex prompt or its event semantics.
        encoding = (
            os.getenv("NOVATRADE_DEEPSEEK_ENCODING", "compact60")
            if config.provider == "deepseek-harness" else "compact60"
        ).strip().lower()
        if encoding == "raw":
            candle_encoding_note = (
                "SNAPSHOT candle series are raw object arrays in ascending order; every supplied row and field is present. "
            )
        elif encoding in {"compact20", "compact10"}:
            limit = 20 if encoding == "compact20" else 10
            candle_encoding_note = (
                f"SNAPSHOT candle series contain the latest {limit} confirmed rows plus the latest forming row when present; "
                "the local SERVER MARKET FACTS still use the complete collected history. "
            )
        else:
            candle_encoding_note = (
                "SNAPSHOT candle series may be encoded as {columns:[field names],rows:[[values]]}; every supplied row, "
                "field, precision and type is preserved. "
            )
        return (
            "You are a constrained trading decision engine. Return exactly one JSON decision "
            "matching the supplied schema. Never call tools, access files, place orders, or include prose. "
            "Use action=hold when no individual setup passes the entry gates or when a global safety gate blocks entry. "
            "Return assessments for EVERY OBSERVED CONTRACT exactly once, including when the total action is hold, close or cancel. "
            "Assess each contract independently: a valid opportunity does not require the entire pool or all timeframes to agree. "
            "A missing resource for one contract does not automatically invalidate the other contracts' complete data. "
            "For each assessment give long/short/neutral direction, estimated winRate, riskRewardRatio, "
            "a suggested limitPrice (the pending-order entry level), stopLossPrice, takeProfitPrice, and optional takeProfitLevels "
            "(one to four {price,quantityPercent} targets whose percentages sum to 100), confidence, "
            "entryEligible, unmetConditions and a specific Simplified Chinese reason. "
            "PER-CONTRACT ANALYSIS FIRST, EXECUTION ELIGIBILITY SECOND. These are conditional trading plans, not placed orders. "
            "Use SERVER MARKET FACTS to read each contract's measured price, confirmed-candle trends/ranges and depth quickly; "
            "these are deterministic summaries of the same complete SNAPSHOT, not trading signals or mandatory strategy rules. "
            "Use these measured summaries directly; do not recompute their arithmetic. Consult relevant raw candle rows "
            "only to resolve a specific ambiguity or verify a proposed level. Choose one concise supported conditional plan "
            "per contract; no exhaustive search over setups is required. "
            + candle_encoding_note
            + "When columns/rows encoding is used, reconstruct each row by pairing columns with row values. "
            "Use the confirmed column to identify closed candles. "
            "Candle series with heterogeneous fields retain their original object arrays. "
            "A global hold, disabled opening, account/risk block, pending confirmation or a below-threshold signal "
            "must not erase a contract's technical plan or numerical estimates. "
            "For each contract with adequate ticker and confirmed candles, inspect its actual recent price structure "
            "and compare the strongest long and short scenario. Select the better-supported conditional scenario "
            "and report its direction, entry, stop, target, reward/risk and subjective win probability even when "
            "the scenario is not currently ready to trade. An ambiguous/ranging trend can still support a conditional "
            "breakout, rejection or pullback plan anchored to observed levels; agreement across all timeframes is not required. "
            "Anchor proposed entry/stop/target to this contract's observed swing highs/lows, support/resistance or "
            "other price structure visible in the supplied confirmed candles; include a genuinely required structural signal "
            "or closed-candle confirmation in unmetConditions if it has not occurred. Do not move levels or inflate "
            "winRate/reward/risk merely to pass the entry gates. "
            "winRate means your subjective estimate that this proposed scenario reaches take profit before stop loss "
            "after its entry condition is met. It is a model estimate, not a measured historical success rate; "
            "historical/backtest validation is not required to provide this subjective estimate. Reflect weak evidence "
            "in a lower numerical winRate and confidence plus a specific caveat, without claiming a verified success rate. "
            "Use direction=neutral or null numerical values only when the relevant resource is genuinely unavailable/invalid "
            "or no defensible conditional estimate can be formed after inspecting both directions. "
            "Lack of an immediate entry signal, lack of backtesting, mixed timeframes or an account risk block alone "
            "are not reasons to return neutral or null for all technical fields. Explain any unavailable value with "
            "the exact missing resource/timeframe or contract-specific analytical limitation. Never fabricate source prices "
            "or data to complete a row. Each assessment reason must cite this contract's measured timeframe/price/trend "
            "evidence and explain its proposed scenario; do not repeat a generic no-direction/no-verifiable-setup reason across the pool. "
            "Keep each Chinese reason concise (about 60 characters or fewer): one or two measured facts plus the scenario. "
            "List only one or two main actual blockers in unmetConditions; avoid repetitive explanations. "
            "entryEligible=true means that this contract's proposed setup passes the known entry gates; "
            "it requires a supported trade direction, sufficient confidence/winRate/riskRewardRatio and no unmetConditions. "
            "SERVER ENTRY GATES.tradingAvailability reports the authenticated account's execution universe, "
            "which may be smaller than public markets in demo mode. When that map is present, only available=true "
            "can be entryEligible or selected for open. For false or unknown availability set entryEligible=false "
            "and include the server reason in unmetConditions, while preserving the full technical plan and estimates. "
            "Do not drop an observed contract, replace the fixed pool or switch trading environment to bypass this gate. "
            "A conditional hypothesis with a required structural signal/confirmation still pending remains entryEligible=false "
            "even when its estimated numerical quality passes the thresholds. Keep its directional plan and numerical values visible. "
            "When SNAPSHOT.account.positions contains a position for a contract, that contract must not be selected for action=open. "
            "Re-evaluate the existing position first: select action=close only when the latest measured evidence materially "
            "invalidates the setup or shows the earlier order was a mistake. For close use reasonCode=THESIS_INVALIDATED "
            "for a materially invalidated market thesis or ORDER_MISTAKE for an earlier order error, and put at least 8 "
            "characters of concrete evidence in reason. "
            "For an existing position, compare its current stop-loss/take-profit protection with the latest assessment. "
            "If protection is missing, provide valid replacement levels in that assessment so the server can attach protection. "
            "A high-confidence position may have its existing protection adjusted only when the latest measured evidence is clear "
            "and materially changes the trade thesis; ordinary small price noise must not move either line. State this evidence. "
            "Staged takeProfitLevels are allowed for partial profit-taking; keep the stop-loss valid for the whole remaining position. "
            "When SNAPSHOT.account.pendingOrders contains a live order, do not create a replacement open for that contract. "
            "Select action=cancel only when the setup materially changed or the earlier order was mistaken, and copy its real order ID. "
            "For cancel use reasonCode=THESIS_INVALIDATED or ORDER_MISTAKE and put at least 8 characters of concrete evidence in reason. "
            "A routine duplicate replacement is not a valid reason to close or cancel. Only one action is allowed per round. "
            "A proposed limit level may be away from the current market price: limitPrice not yet touched is not an unmet "
            "signal by itself. A real current pending order is different: it blocks a replacement open until it is cancelled. "
            "Use SERVER ENTRY GATES exactly: allowOpen must be true, confidence >= minimumConfidence, "
            "winRate >= minimumWinRate and riskRewardRatio >= minimumRiskRewardRatio. "
            "When requireStopLoss=true, a positive stopLossPrice is required. Limit orders require a positive limitPrice. "
            "todayLossCount must be available, finite, nonnegative and below maxDailyLosses; "
            "do not assume unavailable account/risk values or daily counters are zero. "
            "Respect reported risk blocks, maximum daily orders, the current-position entry gate and fixed margin/leverage limits. "
            "Otherwise list specific unmet conditions per contract. Calculate riskRewardRatio using the same proposed entry, "
            "stop loss and take profit, accounting for trading costs if supported; do not mix different setups. "
            "When prices are supplied, use stopLossPrice < limitPrice < takeProfitPrice for long, "
            "and takeProfitPrice < limitPrice < stopLossPrice for short. "
            "The total decision may select at most ONE contract for an action; never submit multiple orders in one round. "
            "For open, select an entryEligible assessment and copy its direction, winRate, riskRewardRatio, "
            "confidence, stopLossPrice, takeProfitPrice and (for a limit order) limitPrice into the total decision. "
            "SERVER FRESHNESS is authoritative: evaluatedAt is the current server UTC time; "
            "ageSeconds is the elapsed time since capturedAt, maxAgeSeconds is its limit, and "
            "isStale is computed by the server. Z denotes UTC, including when the local date differs. "
            "Do not guess the current date/time or recalculate age from your own clock. "
            "When valid=true and isStale=false, do not claim the snapshot has expired. "
            "Write validUntil as an ISO-8601 UTC timestamp with whole seconds and a Z suffix. "
            "Use candles with confirmed=true for closed-candle signals. A latest candle with "
            "confirmed=false is a normal forming candle: do not use it as a closed-candle signal, "
            "but its presence does not invalidate confirmed history or the entire snapshot. "
            "confidence is your self-assessed confidence in the returned action, including hold; "
            "it is not winRate or the probability of entering a trade. "
            "For a hold concerning one contract, set instrumentID to that exact contract ID; "
            "use instrumentID=null only when the hold applies to the entire observation pool. "
            "Write reason in Simplified Chinese and explain the specific missing data or conflicts "
            "present in the snapshot; do not invent facts or data. "
            "For action=open, include a leverage suggestion between 1 and the snapshot's maxLeverage; "
            "the server uses marginPerOrderUSD as fixed margin and rejects leverage outside that range. "
            "The daily order and loss limits are server-enforced safety gates; do not treat them as optional.\n"
            "SERVER FRESHNESS:\n" + json.dumps(freshness, ensure_ascii=False, separators=(",", ":")) + "\n"
            "SERVER ENTRY GATES (authoritative):\n" + json.dumps(entry_gates, ensure_ascii=False, separators=(",", ":")) + "\n"
            "OBSERVED CONTRACTS (complete assessment coverage required):\n" + json.dumps(snapshot.observed_instruments(), ensure_ascii=False, separators=(",", ":")) + "\n"
            "COPY EXACT SNAPSHOT ID:\n" + snapshot.snapshotId + "\n"
            "SERVER MARKET FACTS:\n" + dumps(market_facts(snapshot)) + "\n"
            "SNAPSHOT:\n" + dumps(_prompt_snapshot(snapshot, encoding=encoding)) + "\n"
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


class DeepSeekHarnessRunner(CodexRunner):
    """Use the shared ACP adapter while retaining Codex's decision workflow."""

    provider_label = "DeepSeek Harness"

    def __init__(self, executable: list[str] | str | None = None) -> None:
        self.adapter = DeepSeekACP(executable=executable)
        self.last_run_metadata: dict[str, Any] = {}

    @staticmethod
    def runtime_identity() -> dict[str, str]:
        return {
            "model": os.getenv("NOVATRADE_DEEPSEEK_MODEL", "deepseek-v4-pro"),
            "provider": "DeepSeek Harness",
            "reasoningEffort": os.getenv("NOVATRADE_DEEPSEEK_REASONING_EFFORT", "low"),
        }

    async def _run_prompt(
        self, prompt: str, schema: dict[str, Any], config: AIConfig, *, deadline: float | None = None,
    ) -> dict[str, Any]:
        remaining = config.cliTimeoutSeconds
        if deadline is not None:
            remaining = min(remaining, deadline - asyncio.get_running_loop().time())
        if remaining <= 0:
            raise CodexError("DeepSeek Harness decision workflow timed out")
        structured_prompt = (
            prompt
            + "\nSTRICT OUTPUT CONTRACT (authoritative): return exactly one JSON object, with no markdown or prose. "
            + "Every property declared in the schema must be present; use null only where the schema permits it. "
            + "The server will reject any missing, extra, or type-invalid field.\nJSON SCHEMA:\n"
            + json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        )
        if len(structured_prompt.encode("utf-8")) > _MAX_DECISION_PROMPT_BYTES:
            raise CodexError(
                f"DeepSeek decision prompt exceeds {_MAX_DECISION_PROMPT_BYTES} UTF-8 bytes"
            )
        self.last_run_metadata = {}
        try:
            with tempfile.TemporaryDirectory(prefix="novatrade-deepseek-") as workdir:
                raw = await self.adapter.run_json(
                    structured_prompt,
                    cwd=workdir,
                    timeout_seconds=remaining,
                    max_output_bytes=config.maxOutputBytes,
                )
            self.last_run_metadata.update(getattr(self.adapter, "last_run_metadata", {}))
            return raw
        except DeepSeekHarnessError as error:
            self.last_run_metadata.update(getattr(self.adapter, "last_run_metadata", {}))
            raise CodexError(str(error)) from error

    async def run_chat(self, message: str, config: AIConfig) -> AIChatResponse:
        prompt = (
            "You are NovaTrade's strategy copilot. Return exactly one JSON object matching this schema. "
            "Never place or modify orders; suggestion may only contain safe strategy fields.\n"
            "SCHEMA:\n" + dumps(ai_chat_json_schema()) + "\nCURRENT CONFIG:\n" + dumps(config)
            + "\nUSER MESSAGE:\n" + message
        )
        self.last_run_metadata = {}
        try:
            with tempfile.TemporaryDirectory(prefix="novatrade-deepseek-chat-") as workdir:
                text = await self.adapter.run_prompt(
                    prompt,
                    cwd=workdir,
                    timeout_seconds=config.cliTimeoutSeconds,
                    max_output_bytes=config.maxOutputBytes,
                )
            self.last_run_metadata.update(getattr(self.adapter, "last_run_metadata", {}))
            return CodexRunner._decode_chat_response(text)
        except DeepSeekHarnessError as error:
            self.last_run_metadata.update(getattr(self.adapter, "last_run_metadata", {}))
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
        selected_strategy = "deepseek" if strategy_id == "codex" and self.config.provider == "deepseek-harness" else strategy_id
        self.strategy_id = re.sub(r"[^A-Za-z0-9_-]+", "-", selected_strategy.strip()) or "codex"
        self.runner = runner or (DeepSeekHarnessRunner() if self.config.provider == "deepseek-harness" else CodexRunner())
        self._custom_runner = runner is not None
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
        self._load_persisted()
        if not self._custom_runner:
            self.runner = DeepSeekHarnessRunner() if self.config.provider == "deepseek-harness" else CodexRunner()

    def _path(self, name: str) -> Path:
        if self.strategy_id == "codex":
            return self.state_dir / name
        stem, suffix = name.rsplit(".", 1)
        return self.state_dir / f"{stem}-{self.strategy_id}.{suffix}"

    def _load_persisted(self) -> None:
        migrated_config = False
        persisted_fingerprint: str | None = None
        try:
            raw = json.loads(self._path("ai-config.json").read_text(encoding="utf-8"))
            self.config = AIConfig.from_dict(raw)
            # The original default was 45 seconds and was never exposed as a
            # user setting. Treat that persisted value as the old default so
            # existing installations receive the larger grouped-analysis
            # budget without requiring a manual reset.
            if (
                isinstance(raw, Mapping)
                and isinstance(raw.get("cliTimeoutSeconds"), (int, float))
                and not isinstance(raw.get("cliTimeoutSeconds"), bool)
                and float(raw["cliTimeoutSeconds"]) == LEGACY_DEFAULT_CLI_TIMEOUT_SECONDS
            ):
                self.config = AIConfig.from_dict({**raw, "cliTimeoutSeconds": DEFAULT_CLI_TIMEOUT_SECONDS})
            migrated_config = isinstance(raw, Mapping) and raw != self.config.to_dict()
            self.status = AIStatus(mode=self.config.mode, enabled=self.config.enabled, updatedAt=_now_iso())
        except (OSError, ValueError, SchemaError, json.JSONDecodeError):
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
        except (OSError, ValueError, json.JSONDecodeError, AttributeError):
            pass
        self.status = replace(
            self.status,
            mode=self.config.mode,
            enabled=self.config.enabled,
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
        prompt_encoding = (
            os.getenv("NOVATRADE_DEEPSEEK_ENCODING", "compact60")
            if config.provider == "deepseek-harness" else "compact60"
        ).strip().lower()
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
            "promptBytes": len(CodexRunner._decision_prompt(snapshot, config).encode("utf-8")),
            "promptBytesScope": "decision-prompt-only",
            "promptEncoding": prompt_encoding,
        }

    @classmethod
    def route_for_snapshot(cls, snapshot: AISnapshot, config: AIConfig) -> tuple[str, str, str]:
        """Use one fixed provider route for every snapshot."""
        if config.provider == "deepseek-harness":
            return (
                os.getenv("NOVATRADE_DEEPSEEK_MODEL", "deepseek-v4-pro"),
                os.getenv("NOVATRADE_DEEPSEEK_REASONING_EFFORT", "low"),
                "deepseek-harness",
            )
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
            route_model = identity.get("model") if self.config.provider == "deepseek-harness" else self.config.routineModel
            route_effort = identity.get("reasoningEffort", "low") if self.config.provider == "deepseek-harness" else self.config.routineReasoningEffort
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
            "errorCode": "deepseek_unavailable" if self.config.provider == "deepseek-harness" else "codex_unavailable",
        }

    def update_config(self, values: Mapping[str, Any]) -> dict[str, Any]:
        merged = self.config.to_dict()
        merged.update(dict(values))
        previous_provider = self.config.provider
        self.config = AIConfig.from_dict(merged)
        if self.config.provider != previous_provider and isinstance(self.runner, (CodexRunner, DeepSeekHarnessRunner)):
            self.runner = DeepSeekHarnessRunner() if self.config.provider == "deepseek-harness" else CodexRunner()
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
        self.status = replace(
            self.status,
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
        for key in ("workflow", "stage", "failureStage", "outcome", "profile", "profileFallback"):
            value = metadata.get(key)
            allowed = {"single", "grouped"} if key == "workflow" else {"starting", "analysis", "coordinator", "complete", "failed", "cancelled", "success", "error", "acp", "novatrade-decision"}
            if isinstance(value, str) and value in allowed:
                compact[key] = value
        # The harness profile decides how much fixed prompt overhead each
        # request carries, so keep the active profile visible in the audit.
        for key in ("provider", "model", "reasoningEffort", "sessionId", "profileFallback"):
            value = metadata.get(key)
            if key in metadata:
                compact[key] = value if isinstance(value, str) and value.strip() and len(value) <= 256 else None
        fallback_reason = metadata.get("profileFallbackReason")
        if isinstance(fallback_reason, str) and fallback_reason.strip():
            compact["profileFallbackReason"] = fallback_reason[:500]
        for key in ("observedCount", "groupCount", "completedGroups", "assessmentCount"):
            if key in metadata:
                value = metadata.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    compact[key] = value
        for key in ("analysisTimeoutSeconds", "coordinatorTimeoutSeconds", "analysisDurationSeconds",
                    "coordinatorDurationSeconds", "durationSeconds"):
            if key in metadata:
                value = metadata.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0:
                    compact[key] = value
        for key in ("requestId", "inputTokens", "outputTokens", "reasoningTokens", "cacheReadTokens",
                    "cacheWriteTokens", "basePromptBytes", "strictContractBytes", "schemaBytes",
                    "modelPayloadBytes", "initializeSeconds", "sessionCreateSeconds", "configSeconds",
                    "promptSeconds", "closeSeconds", "totalSeconds"):
            if key in metadata:
                value = metadata.get(key)
                compact[key] = value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0 else None
        if compact:
            self._audit({"type": "analysis-workflow", "snapshotId": snapshot.snapshotId, "workflow": compact})

    async def _loop(self) -> None:
        while not self._stop.is_set():
            await self.run_once()
            if self.status.state == "halted":
                break
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.config.decisionIntervalSeconds)
            except asyncio.TimeoutError:
                continue

    async def start(self) -> None:
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
        if self._task is not None:
            await self._task
            self._task = None

    async def disable(self) -> None:
        self.update_config({"enabled": False, "mode": "disabled"})
        await self.stop()
        self.status = replace(
            self.status,
            state="stopped",
            mode="disabled",
            enabled=False,
            updatedAt=_now_iso(),
            observedInstruments=list(self.observed_instruments),
            observationUpdatedAt=self.observation_updated_at,
            lastDecisionInstruments=list(self.status.lastDecisionInstruments),
            lastDecisionFreshness=dict(self.status.lastDecisionFreshness),
        )
        self._persist()
