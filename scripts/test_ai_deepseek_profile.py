#!/usr/bin/env python3
"""Exercise the prompt-only DeepSeek Harness decision profile.

The profile removes the coding-agent surface (tool schemas, workspace
instructions, skill catalog, runtime boilerplate) that a schema-constrained
trading decision never uses but pays for on every request.  These checks run
without credentials and without contacting a model.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
import os
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend import deepseek_profile  # noqa: E402
from backend.deepseek_harness import DeepSeekHarnessError, DeepSeekHarnessRunner  # noqa: E402


class DecisionProfileTemplateTests(unittest.TestCase):
    def test_template_disables_the_coding_agent_surface(self):
        for row_id in ("tool-bash", "tool-fs", "tool-web", "tool-subagent", "tool-workflow",
                       "skill", "agent-instructions", "commands", "plan-mode", "compaction-basic"):
            self.assertIn(f"- id: {row_id}\n  disabled: true", deepseek_profile.PATCH_YAML)

    def test_template_removes_harness_runtime_context(self):
        self.assertIn("- id: system-prompt", deepseek_profile.PATCH_YAML)
        self.assertIn("includeHarnessIdentity: false", deepseek_profile.PATCH_YAML)
        self.assertIn("includeRuntimeContext: false", deepseek_profile.PATCH_YAML)
        self.assertIn("{{model}}", deepseek_profile.PATCH_YAML)

    def test_template_entries_are_unique_and_bounded(self):
        entries = [line for line in deepseek_profile.PATCH_YAML.splitlines() if line.startswith("- id: ")]
        ids = [line.split(": ", 1)[1] for line in entries]
        self.assertEqual(len(ids), len(set(ids)), "duplicate overlay entry")
        # The overlay is a prompt-budget device: it must stay trivial itself.
        self.assertLess(len(deepseek_profile.PATCH_YAML.encode("utf-8")), 2048)

    def test_template_keeps_the_json_infrastructure(self):
        # The decision contract still needs the agent loop, the model route and
        # schema validation; only the unused agent surface may be disabled.
        for kept in ("llm", "agent", "agent-loop", "tools", "llm-deepseek"):
            self.assertNotIn(f"- id: {kept}\n  disabled: true", deepseek_profile.PATCH_YAML)

    def test_safety_overlay_disables_tools_and_session_persistence(self):
        for row_id in deepseek_profile.SAFETY_ROWS:
            self.assertIn(f"- id: {row_id}\n  disabled: true", deepseek_profile.SAFETY_PATCH_YAML)
        self.assertNotIn("system-prompt", deepseek_profile.SAFETY_PATCH_YAML)


class HarnessTelemetryTests(unittest.TestCase):
    def test_usage_parser_accepts_nested_and_direct_acp_fields(self):
        usage = {}
        DeepSeekHarnessRunner._collect_usage({
            "params": {"update": {
                "sessionUpdate": "usage_update",
                "inputTokens": 120,
                "output_tokens": 7,
                "reasoningTokens": 3,
            }},
            "result": {"tokenUsage": {"cacheReadTokens": 40, "cacheWriteTokens": 2}},
        }, usage)
        self.assertEqual(usage, {
            "inputTokens": 120, "outputTokens": 7, "reasoningTokens": 3,
            "cacheReadTokens": 40, "cacheWriteTokens": 2,
        })

    def test_payload_metrics_separate_prompt_contract_and_schema(self):
        prompt = "BASE\nSTRICT OUTPUT CONTRACT (authoritative): contract\nJSON SCHEMA:\n{}"
        self.assertEqual(
            DeepSeekHarnessRunner._payload_metrics(prompt),
            {
                "modelPayloadBytes": len(prompt.encode()),
                "basePromptBytes": len(b"BASE"),
                "strictContractBytes": len(b"\nSTRICT OUTPUT CONTRACT (authoritative): contract"),
                "schemaBytes": 2,
            },
        )


class DecisionProfileBootstrapTests(unittest.TestCase):
    def test_bootstrap_writes_a_content_addressed_patch(self):
        with tempfile.TemporaryDirectory() as home:
            path = deepseek_profile.ensure_patch_file(Path(home))
            self.assertIsNotNone(path)
            assert path is not None
            self.assertTrue(path.is_file())
            self.assertEqual(path.read_text(encoding="utf-8"), deepseek_profile.PATCH_YAML)
            self.assertEqual(path.parent, Path(home) / deepseek_profile.PROFILE_DIRECTORY_NAME)
            # Re-running must be idempotent and must not rewrite the file.
            stamp = path.stat().st_mtime_ns
            self.assertEqual(deepseek_profile.ensure_patch_file(Path(home)), path)
            self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_bootstrap_replaces_a_stale_patch(self):
        with tempfile.TemporaryDirectory() as home:
            path = deepseek_profile.patch_path(Path(home))
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("stale\n", encoding="utf-8")
            self.assertEqual(deepseek_profile.ensure_patch_file(Path(home)), path)
            self.assertEqual(path.read_text(encoding="utf-8"), deepseek_profile.PATCH_YAML)

    def test_bootstrap_is_skipped_when_disabled(self):
        for value in ("0", "off", "none", "stock", "disabled", " OFF "):
            self.assertIsNone(
                deepseek_profile.ensure_patch_file(env={deepseek_profile.PROFILE_ENV: value}),
                f"{value!r} must disable the overlay",
            )

    def test_bootstrap_survives_an_unwritable_home(self):
        with tempfile.TemporaryDirectory() as home:
            blocked = Path(home) / "missing" / deepseek_profile.PROFILE_DIRECTORY_NAME
            with patch.object(Path, "mkdir", side_effect=OSError("read-only")):
                self.assertIsNone(deepseek_profile.ensure_patch_file(Path(home)))
            self.assertFalse(blocked.exists())

    def test_safety_bootstrap_is_idempotent(self):
        with tempfile.TemporaryDirectory() as home:
            path = deepseek_profile.ensure_safety_patch_file(Path(home))
            self.assertIsNotNone(path)
            assert path is not None
            self.assertEqual(path.read_text(encoding="utf-8"), deepseek_profile.SAFETY_PATCH_YAML)
            stamp = path.stat().st_mtime_ns
            self.assertEqual(deepseek_profile.ensure_safety_patch_file(Path(home)), path)
            self.assertEqual(path.stat().st_mtime_ns, stamp)

    def test_dsh_home_accepts_novatrade_override(self):
        self.assertEqual(
            deepseek_profile.dsh_home({"NOVATRADE_DSH_HOME": "~/isolated-dsh"}),
            Path.home() / "isolated-dsh",
        )

    def test_launcher_arguments_patch_installed_acp_profile(self):
        arguments = deepseek_profile.launcher_arguments(Path("/tmp/overlay.yml"))
        self.assertEqual(arguments[0], "--profile")
        self.assertEqual(arguments[1], "acp")
        self.assertEqual(arguments[2], "--patch")
        self.assertEqual(arguments[3], "/tmp/overlay.yml")
        self.assertEqual(deepseek_profile.launcher_arguments(None), ["--profile", "acp"])


class HarnessRunnerProfileTests(unittest.TestCase):
    def test_vendor_launcher_pins_the_decision_profile(self):
        runner = DeepSeekHarnessRunner(executable=["/usr/bin/fake-dsh"])
        # An explicit executable is honored verbatim: no implicit overlay.
        self.assertEqual(runner.profile_arguments, [])
        self.assertEqual(runner.launch_command, ["/usr/bin/fake-dsh"])

    def test_discovered_launcher_appends_profile_and_patch(self):
        patch_path = Path(tempfile.gettempdir()) / "novatrade-decision-test.yml"
        with patch("backend.deepseek_harness._profile_dsh_command", return_value=["/usr/bin/dsh"]), \
             patch("backend.deepseek_harness._ensure_profile_patch", return_value=patch_path):
            runner = DeepSeekHarnessRunner()
        self.assertEqual(
            runner.launch_command,
            ["/usr/bin/dsh", "--profile", "acp", "--patch", str(patch_path)],
        )
        self.assertEqual(runner.profile_patch, str(patch_path))
        self.assertEqual(runner.profile_mode, "optimized")

    def test_falls_back_to_the_vendor_profile_when_unavailable(self):
        with patch("backend.deepseek_harness._profile_dsh_command", return_value=["/usr/bin/dsh"]), \
             patch("backend.deepseek_harness._ensure_profile_patch", return_value=None), \
             patch("backend.deepseek_harness._ensure_safety_patch", return_value=Path("/tmp/safety-test.yml")):
            runner = DeepSeekHarnessRunner()
        self.assertEqual(runner.launch_command, ["/usr/bin/dsh", "--profile", "acp", "--patch", "/tmp/safety-test.yml"])
        self.assertEqual(runner.profile_mode, "safety")

    def test_profile_opt_out_uses_stock_acp_without_writing_overlay(self):
        with tempfile.TemporaryDirectory() as home, patch.dict(
            os.environ,
            {
                deepseek_profile.PROFILE_ENV: "0",
                "DSH_HOME": home,
            },
            clear=False,
        ), patch("backend.deepseek_harness._profile_dsh_command", return_value=["/usr/bin/dsh"]):
            runner = DeepSeekHarnessRunner()
        self.assertEqual(runner.launch_command, ["/usr/bin/dsh", "--profile", "acp"])
        self.assertIsNone(runner.profile_patch)
        self.assertFalse((Path(home) / deepseek_profile.PROFILE_DIRECTORY_NAME).exists())

    def test_profile_rejection_retries_once_with_safety_acp(self):
        optimized_patch = Path(tempfile.gettempdir()) / "novatrade-decision-fallback-test.yml"
        safety_patch = Path(tempfile.gettempdir()) / "novatrade-safety-fallback-test.yml"
        async def run() -> tuple[str, list[list[str]], dict]:
            with patch("backend.deepseek_harness._profile_dsh_command", return_value=["/usr/bin/dsh"]), \
                 patch("backend.deepseek_harness._ensure_profile_patch", return_value=optimized_patch), \
                 patch("backend.deepseek_harness._ensure_safety_patch", return_value=safety_patch):
                runner = DeepSeekHarnessRunner()
                launches: list[list[str]] = []

                async def attempt(prompt, *, launch, profile_name, cwd=None, timeout_seconds=90.0, max_output_bytes=1_000_000):
                    launches.append(list(launch))
                    if len(launches) == 1:
                        raise DeepSeekHarnessError("unknown profile patch")
                    return "{\"action\":\"hold\"}"

                with patch.object(runner, "_run_prompt_once", new=AsyncMock(side_effect=attempt)):
                    result = await runner.run_prompt("decision")
                return result, launches, dict(runner.last_run_metadata)

        result, launches, metadata = asyncio.run(run())
        self.assertEqual(result, '{"action":"hold"}')
        self.assertEqual(launches, [
            ["/usr/bin/dsh", "--profile", "acp", "--patch", str(optimized_patch)],
            ["/usr/bin/dsh", "--profile", "acp", "--patch", str(safety_patch)],
        ])
        self.assertEqual(metadata["profileFallback"], "acp-safety")
        self.assertIn("unknown profile", metadata["profileFallbackReason"])

    def test_safe_environment_does_not_forward_exchange_credentials(self):
        with patch.dict(os.environ, {
            "OKX_API_KEY": "okx-secret",
            "OKX_SECRET_KEY": "okx-secret",
            "OKX_PASSPHRASE": "okx-secret",
            "DEEPSEEK_API_KEY": "deepseek-secret",
        }, clear=False):
            environment = DeepSeekHarnessRunner._safe_environment("/tmp/novatrade-test")
        self.assertNotIn("OKX_API_KEY", environment)
        self.assertNotIn("OKX_SECRET_KEY", environment)
        self.assertNotIn("OKX_PASSPHRASE", environment)
        self.assertEqual(environment["DEEPSEEK_API_KEY"], "deepseek-secret")
        self.assertEqual(environment["HOME"], str(Path("/tmp/novatrade-test").resolve()))

    def test_profile_error_patterns_cover_patch_loader_failures(self):
        for detail in (
            "patch: entry tools not found",
            "unknown option: --patch",
            "failed to parse overlay file",
            "failed to read patch file",
        ):
            self.assertTrue(DeepSeekHarnessRunner._is_profile_error(DeepSeekHarnessError(detail)))

    def test_nonpositive_deadline_fails_before_starting_child(self):
        async def run() -> None:
            runner = DeepSeekHarnessRunner(executable=[sys.executable, "-c", "raise SystemExit(1)"])
            with self.assertRaisesRegex(DeepSeekHarnessError, "timeout must be positive"):
                await runner.run_prompt("decision", timeout_seconds=0)

        asyncio.run(run())

    def test_missing_launcher_still_reports_unavailable(self):
        with patch("backend.deepseek_harness._profile_dsh_command", return_value=None):
            runner = DeepSeekHarnessRunner()
        self.assertIsNone(runner.launch_command)


if __name__ == "__main__":
    unittest.main(verbosity=2)
