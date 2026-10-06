"""Small ACP stdio client for DeepSeek Harness.

DeepSeek Harness does not expose a stable command-line "answer" mode for a
long-running service.  Its supported automation boundary is the ACP profile:
``dsh --profile acp`` speaks newline-delimited JSON-RPC on stdin/stdout.  This
module intentionally keeps that boundary narrow.  One adapter instance starts
one ACP process for one prompt, configures the route, collects assistant text
updates, and closes the session.  The caller remains responsible for validating
the returned text against its own decision schema.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
from typing import Any


class DeepSeekHarnessError(RuntimeError):
    """The Harness ACP process did not produce a usable response."""


_DEFAULT_MODEL = "deepseek-v4-pro"
_DEFAULT_REASONING_EFFORT = "low"


def _profile_dsh_command() -> list[str] | None:
    """Find the local Harness launcher without relying on a shell.

    ``dsh`` is normally installed only inside the Harness profile's private
    node_modules tree, so it is not guaranteed to be on the backend PATH.  An
    explicit ``NOVATRADE_DEEPSEEK_HARNESS_BIN`` may contain either an executable
    path or a shell-like command with arguments (for example ``node /path/bin``).
    """
    override = os.environ.get("NOVATRADE_DEEPSEEK_HARNESS_BIN", "").strip()
    if override:
        command = shlex.split(override)
        if not command:
            return None
        return command + ["--profile", "acp"]

    dsh = shutil.which("dsh")
    if dsh:
        return [dsh, "--profile", "acp"]

    # The signed desktop installation ships a self-contained launcher outside
    # the GUI process PATH. It is the most reliable entry point when NovaTrade
    # is launched from Finder/Xcode rather than from a shell.
    bundled_dsh = Path("/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh")
    if bundled_dsh.is_file():
        return [str(bundled_dsh), "--profile", "acp"]

    node = shutil.which("node")
    if node is None:
        local_node_bins = sorted((Path.home() / ".local").glob("node-v*/bin/node"), reverse=True)
        for candidate in (*local_node_bins, Path("/opt/homebrew/bin/node"), Path("/usr/local/bin/node")):
            if Path(candidate).is_file():
                node = str(candidate)
                break
    if node is None:
        return None

    configured_home = os.environ.get("DSH_HOME") or os.environ.get("NOVATRADE_DSH_HOME")
    homes = [Path(configured_home).expanduser()] if configured_home else []
    homes.append(Path.home() / ".dsh")
    for home in homes:
        script = home / "profiles" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        if script.is_file():
            return [node, str(script), "--profile", "acp"]
    return None


class DeepSeekHarnessRunner:
    """Run one prompt through the local DeepSeek Harness ACP profile.

    The process is intentionally isolated per request.  This bounds session
    state and avoids sharing one ACP session between independent strategy
    rounds.  ``run_prompt`` returns only committed assistant text; reasoning
    chunks and usage updates are ignored.
    """

    def __init__(
        self,
        executable: Sequence[str] | str | None = None,
        *,
        provider: str = "deepseek-official",
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        if executable is None:
            self.command = _profile_dsh_command()
        elif isinstance(executable, str):
            self.command = shlex.split(executable)
        else:
            self.command = list(executable)
        self.provider = provider
        self.model = model or os.environ.get("NOVATRADE_DEEPSEEK_MODEL", _DEFAULT_MODEL)
        self.reasoning_effort = reasoning_effort or os.environ.get(
            "NOVATRADE_DEEPSEEK_REASONING_EFFORT", _DEFAULT_REASONING_EFFORT
        )
        self.last_run_metadata: dict[str, Any] = {}

    @staticmethod
    def _model_value(provider: str, model: str) -> str:
        # ACP's model config option is an opaque JSON-encoded route pair.
        return json.dumps([provider, model], separators=(",", ":"))

    @staticmethod
    def _text_from_update(message: Mapping[str, Any]) -> str | None:
        params = message.get("params")
        if not isinstance(params, Mapping):
            return None
        update = params.get("update")
        if not isinstance(update, Mapping) or update.get("sessionUpdate") != "agent_message_chunk":
            return None
        content = update.get("content")
        if not isinstance(content, Mapping) or content.get("type") != "text":
            return None
        text = content.get("text")
        return text if isinstance(text, str) else None

    @staticmethod
    def _error_detail(message: Mapping[str, Any]) -> str:
        error = message.get("error")
        if isinstance(error, Mapping):
            detail = error.get("message")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()
        return "unknown ACP error"

    async def _stop_process(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except (AttributeError, OSError):
                process.kill()
        try:
            await asyncio.wait_for(process.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        for stream in (process.stdout, process.stderr):
            transport = getattr(stream, "_transport", None)
            if transport is not None:
                transport.close()

    async def _request(
        self,
        process: asyncio.subprocess.Process,
        request_id: int,
        method: str,
        params: Mapping[str, Any],
        *,
        timeout: float,
        text_chunks: list[str] | None = None,
    ) -> Mapping[str, Any]:
        if process.stdin is None or process.stdout is None:
            raise DeepSeekHarnessError("DeepSeek Harness ACP pipes are unavailable")
        request = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": dict(params)}
        try:
            process.stdin.write((json.dumps(request, ensure_ascii=False, separators=(",", ":")) + "\n").encode())
            await process.stdin.drain()
        except (BrokenPipeError, ConnectionError) as error:
            raise DeepSeekHarnessError("DeepSeek Harness ACP stdin closed") from error

        while True:
            try:
                line = await asyncio.wait_for(process.stdout.readline(), timeout=timeout)
            except asyncio.TimeoutError as error:
                raise DeepSeekHarnessError(f"DeepSeek Harness ACP timed out waiting for {method}") from error
            if not line:
                stderr = b""
                if process.stderr is not None:
                    try:
                        stderr = await asyncio.wait_for(process.stderr.read(), timeout=0.5)
                    except asyncio.TimeoutError:
                        pass
                detail = stderr.decode("utf-8", errors="replace").strip()[:500]
                suffix = f": {detail}" if detail else ""
                raise DeepSeekHarnessError(f"DeepSeek Harness ACP exited while waiting for {method}{suffix}")
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise DeepSeekHarnessError("DeepSeek Harness ACP emitted invalid JSON-RPC") from error
            if not isinstance(message, Mapping):
                continue
            if text_chunks is not None:
                text = self._text_from_update(message)
                if text is not None:
                    text_chunks.append(text)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise DeepSeekHarnessError(f"DeepSeek Harness ACP {method} failed: {self._error_detail(message)}")
            result = message.get("result")
            if not isinstance(result, Mapping):
                raise DeepSeekHarnessError(f"DeepSeek Harness ACP {method} returned an invalid result")
            return result

    async def run_prompt(self, prompt: str, *, cwd: str | None = None, timeout_seconds: float = 90.0) -> str:
        """Return the final assistant message for one text prompt."""
        if not isinstance(prompt, str) or not prompt.strip():
            raise DeepSeekHarnessError("DeepSeek Harness prompt is empty")
        if self.command is None:
            raise DeepSeekHarnessError(
                "DeepSeek Harness ACP is unavailable; set NOVATRADE_DEEPSEEK_HARNESS_BIN "
                "to the dsh launcher or node bin.js path"
            )
        if timeout_seconds <= 0:
            raise DeepSeekHarnessError("DeepSeek Harness timeout must be positive")
        workspace = str(Path(cwd or os.getcwd()).expanduser().resolve())
        process: asyncio.subprocess.Process | None = None
        chunks: list[str] = []
        started = asyncio.get_running_loop().time()
        try:
            process = await asyncio.create_subprocess_exec(
                *self.command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=workspace,
                env={**os.environ, "DSH_TOOLS_MODE": os.environ.get("DSH_TOOLS_MODE", "")},
                start_new_session=True,
            )
            remaining = lambda: max(0.1, timeout_seconds - (asyncio.get_running_loop().time() - started))
            await self._request(
                process, 1, "initialize", {"protocolVersion": 1, "clientCapabilities": {}}, timeout=remaining()
            )
            session = await self._request(
                process, 2, "session/new", {"cwd": workspace, "mcpServers": []}, timeout=remaining()
            )
            session_id = session.get("sessionId")
            if not isinstance(session_id, str) or not session_id:
                raise DeepSeekHarnessError("DeepSeek Harness ACP session/new omitted sessionId")
            # Pin the route so a global GUI setting cannot silently change the
            # strategy's provider/model between invocations.
            await self._request(
                process, 3, "session/set_config_option",
                {"sessionId": session_id, "configId": "model", "value": self._model_value(self.provider, self.model)},
                timeout=remaining(),
            )
            await self._request(
                process, 4, "session/set_config_option",
                {"sessionId": session_id, "configId": "reasoning_effort", "value": self.reasoning_effort},
                timeout=remaining(),
            )
            result = await self._request(
                process, 5, "session/prompt",
                {"sessionId": session_id, "prompt": [{"type": "text", "text": prompt}]},
                timeout=remaining(), text_chunks=chunks,
            )
            if result.get("stopReason") not in {"end_turn", "max_tokens", "max_turn_requests"}:
                raise DeepSeekHarnessError(f"DeepSeek Harness ACP stopped with {result.get('stopReason')!r}")
            text = "".join(chunks).strip()
            if not text:
                raise DeepSeekHarnessError("DeepSeek Harness ACP returned an empty assistant message")
            self.last_run_metadata = {
                "provider": self.provider,
                "model": self.model,
                "reasoningEffort": self.reasoning_effort,
                "stopReason": result.get("stopReason"),
            }
            await self._request(
                process, 6, "session/close", {"sessionId": session_id}, timeout=remaining()
            )
            return text
        except asyncio.CancelledError:
            if process is not None:
                await asyncio.shield(self._stop_process(process))
            raise
        finally:
            if process is not None:
                await self._stop_process(process)

    async def run_json(self, prompt: str, **kwargs: Any) -> dict[str, Any]:
        """Run a prompt and decode the first JSON object in the reply."""
        text = await self.run_prompt(prompt, **kwargs)
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.IGNORECASE | re.DOTALL)
        try:
            value, _ = json.JSONDecoder().raw_decode(cleaned)
        except json.JSONDecodeError:
            start = cleaned.find("{")
            if start < 0:
                raise DeepSeekHarnessError("DeepSeek Harness response did not contain a JSON object")
            try:
                value, _ = json.JSONDecoder().raw_decode(cleaned[start:])
            except json.JSONDecodeError as error:
                raise DeepSeekHarnessError("DeepSeek Harness response contained invalid JSON") from error
        if not isinstance(value, dict):
            raise DeepSeekHarnessError("DeepSeek Harness response JSON must be an object")
        return value


__all__ = ["DeepSeekHarnessError", "DeepSeekHarnessRunner"]
