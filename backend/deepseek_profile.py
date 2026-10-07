"""Minimal DeepSeek Harness (dsh) ACP profile for the trading decision worker.

The stock ``acp`` profile boots the full coding-agent surface: two dozen
model-facing tool schemas, the workspace instruction loader, the skill
catalog, slash commands, goal/plan tooling and the runtime-context
boilerplate.  All of it is re-sent on every model request, while the trading
decision prompt explicitly forbids tool use.  Measured on a live decision
round, that fixed overhead costs roughly 19 KB of prompt bytes per request
(24 tool schemas at ~18.5 KB plus ~21.9 KB of runtime/skill context on the
first request of a session) before any market data is included.

This module owns a profile overlay that removes exactly that surface.  It
changes no trading semantics: the observation set, entry gates, prompt,
schema, validation and order path are untouched.  Only the coding-agent
scaffolding a schema-constrained JSON decision never uses is disabled.

One row is redirected rather than disabled: the ACP application requires the
``sessionPersistence`` service, which only the JSONL backend provides.  Its
root moves into a NovaTrade-owned directory so no decision prompt is left in
the shared ``$DSH_HOME/sessions`` tree; the ACP adapter deletes each run's
artifacts when the prompt finishes.
"""

from __future__ import annotations

from hashlib import sha256
import os
from pathlib import Path
from typing import Mapping

# ``dsh`` boots ``$DSH_HOME/profiles/<name>``.  The overlay is injected with
# ``--patch`` so no profile directory has to exist or be written.
PROFILE_NAME = "novatrade-decision"
PROFILE_DIRECTORY_NAME = "novatrade-deepseek"

# The ACP application requires the ``sessionPersistence`` service, and only the
# JSONL backend provides it. Disabling that row leaves ``acp`` permanently
# pending and dsh refuses to start ("1 required plugin did not activate").
# The overlay therefore keeps the provider and redirects its storage into a
# NovaTrade-owned directory, which the ACP adapter purges after every prompt.
SESSION_ROOT_SUBPATH = f"{PROFILE_DIRECTORY_NAME}/sessions"

# Selecting this profile requires the launcher flags below; an operator can
# set ``NOVATRADE_DEEPSEEK_PROFILE=0`` (or ``off``/``none``/``stock``) to go
# back to the vendor profile without a code change.
DISABLED_VALUES = frozenset({"0", "off", "none", "stock", "disabled"})
PROFILE_ENV = "NOVATRADE_DEEPSEEK_PROFILE"

# Decision prompts contain account and risk state, so they must not be left
# behind on disk. Two rows cannot be disabled at all: ``acp`` requires the
# ``sessionPersistence`` service from ``session-persistence-jsonl``, and the
# required ``agent-loop`` requires the ``tools`` service row. Both are kept and
# the persistence root is redirected instead; only rows that are neither
# required services nor prompt overhead may be disabled. This overlay removes
# nothing from the prompt (the optimized overlay above does that), so it is the
# fallback that still starts when a patch row is rejected.
_SAFETY_ROWS = (
    "session-log-deepseek",
    "session-projection-cache",
)

# Bundle rows that exist in @deepseek-ai/dsh-base or @deepseek-ai/dsh-acp-app.
# Every id below is tooling for an autonomous coding agent: it either adds a
# model-facing tool schema (re-sent on each request) or injects a prompt
# section the decision contract never reads.
_DISABLED_ROWS = (
    # Model-facing tool schemas.
    "tool-bash",
    "tool-pwsh",
    "tool-jobs",
    "tool-fs",
    "tool-fs-search",
    "tool-skill",
    "tool-subagent",
    "tool-subagent-fork",
    "tool-subagent-control",
    "tool-subagent-list-agents",
    "tool-workflow",
    "tool-web",
    "tool-todo",
    "tool-goal",
    "tool-ralph",
    # Prompt sections that are not part of the decision contract.
    "skill",
    "skill-filesystem",
    "agent-instructions",
    "commands",
    "command-feedback",
    "command-goal",
    "command-compact",
    "plan-mode",
    "goal",
    "goal-round-driver",
    "repeat-tool-reminder",
    # Conversation compaction.  Every decision runs in a fresh ACP session
    # with a single prompt, so there is no history to compact.
    "compaction-basic",
    "tool-result-pruner",
    # Session diagnostics caches. These are not service providers, so the ACP
    # application still boots without them. Durable session persistence is
    # redirected instead of disabled: see ``SESSION_ROOT_SUBPATH``.
    "session-log-deepseek",
    "session-projection-cache",
)

# The persona mirrors the vendor ``acp`` profile (``You are a coding agent
# powered by the {{model}} model.``) with the harness's own runtime context
# removed, so the model still knows it is an agent answering one request.
_PERSONA_PREFIX = (
    "You are an automated agent powered by the {{model}} model, answering a "
    "single machine request. Return only what that request asks for."
)

def _render_patch(rows: tuple[str, ...], *, include_system_prompt: bool = True) -> str:
    lines = [
        "# Generated by NovaTrade backend/deepseek_profile.py. Do not edit;",
        "# the trading worker rewrites this overlay from its bundled template.",
        "#",
        "# The optimized overlay removes the coding-agent surface a constrained",
        "# JSON decision never uses; the safety overlay keeps that surface and",
        "# only redirects durable session storage.",
        "",
    ]
    for row_id in rows:
        lines.append(f"- id: {row_id}")
        lines.append("  disabled: true")
    lines.extend(
        [
            "",
            "# Required by the ACP app; storage moves into the NovaTrade root.",
            "- id: session-persistence-jsonl",
            "  config:",
            f"    root: !!js dshHomePath('{SESSION_ROOT_SUBPATH}')",
        ]
    )
    if include_system_prompt:
        lines.extend(
            [
                "",
                "- id: system-prompt",
                "  config:",
                "    includeHarnessIdentity: false",
                "    includeRuntimeContext: false",
                "    personaPrefix: >-",
                f"      {_PERSONA_PREFIX}",
                "",
            ]
        )
    return "\n".join(lines).rstrip("\n") + "\n"


PATCH_YAML = _render_patch(_DISABLED_ROWS)
SAFETY_PATCH_YAML = _render_patch(_SAFETY_ROWS, include_system_prompt=False)

# Keep the tuple order stable for readers of the file and for tests.
DISABLED_ROWS = tuple(_DISABLED_ROWS)
SAFETY_ROWS = tuple(_SAFETY_ROWS)


def _profile_enabled(env: Mapping[str, str] | None = None) -> bool:
    environment = os.environ if env is None else env
    value = (environment.get(PROFILE_ENV) or "").strip().lower()
    return value not in DISABLED_VALUES


def dsh_home(env: Mapping[str, str] | None = None) -> Path:
    """Resolve ``$DSH_HOME`` the way the dsh launcher resolves it."""
    environment = os.environ if env is None else env
    configured = (environment.get("DSH_HOME") or environment.get("NOVATRADE_DSH_HOME") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path.home() / ".dsh"


def patch_path(
    home: Path | None = None, env: Mapping[str, str] | None = None,
) -> Path:
    """Return the stable, content-addressed overlay path for this template."""
    base = Path(home) if home is not None else dsh_home(env)
    digest = sha256(PATCH_YAML.encode("utf-8")).hexdigest()[:16]
    return base / PROFILE_DIRECTORY_NAME / f"{PROFILE_NAME}-{digest}.yml"


def safety_patch_path(
    home: Path | None = None, env: Mapping[str, str] | None = None,
) -> Path:
    """Return the stable path for the tool/persistence safety overlay."""
    base = Path(home) if home is not None else dsh_home(env)
    digest = sha256(SAFETY_PATCH_YAML.encode("utf-8")).hexdigest()[:16]
    return base / PROFILE_DIRECTORY_NAME / f"safety-{digest}.yml"


def ensure_patch_file(
    home: Path | None = None, env: Mapping[str, str] | None = None,
) -> Path | None:
    """Materialize the overlay and return its path, or None when unavailable.

    A missing or unwritable home is reported to the caller, which can choose
    the smaller safety overlay or fail closed before starting ACP.
    """
    if not _profile_enabled(env):
        return None
    try:
        path = patch_path(home, env)
        desired = PATCH_YAML.encode("utf-8")
        try:
            if path.read_bytes() == desired:
                return path
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(desired)
        temporary.replace(path)
        return path
    except OSError:
        return None


def ensure_safety_patch_file(
    home: Path | None = None, env: Mapping[str, str] | None = None,
) -> Path | None:
    """Materialize the minimal safety overlay used for profile fallback."""
    try:
        path = safety_patch_path(home, env)
        desired = SAFETY_PATCH_YAML.encode("utf-8")
        try:
            if path.read_bytes() == desired:
                return path
        except OSError:
            pass
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp")
        temporary.write_bytes(desired)
        temporary.replace(path)
        return path
    except OSError:
        return None


def launcher_arguments(
    patch: Path | None,
) -> list[str]:
    """Return the launcher-flag suffix for one dsh ACP invocation."""
    if patch is None:
        # ``dsh`` without a profile only prints usage. ``acp`` is the
        # installed vendor profile; the overlay is an optional patch on top
        # of it, not a dynamically registered profile name.
        return ["--profile", "acp"]
    return ["--profile", "acp", "--patch", str(patch)]


__all__ = [
    "DISABLED_ROWS",
    "PATCH_YAML",
    "PROFILE_DIRECTORY_NAME",
    "SAFETY_PATCH_YAML",
    "SAFETY_ROWS",
    "SESSION_ROOT_SUBPATH",
    "PROFILE_ENV",
    "PROFILE_NAME",
    "dsh_home",
    "ensure_patch_file",
    "ensure_safety_patch_file",
    "launcher_arguments",
    "patch_path",
    "safety_patch_path",
]
