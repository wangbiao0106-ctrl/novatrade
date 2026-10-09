#!/usr/bin/env python3
"""Archive local trading state and start a fresh, paused paper account.

Run only after NovaTrade and its backend have stopped. This command does not
close or otherwise change an OKX account; exchange credentials remain intact.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PORT = 8787
PRODUCTION_REST_URL = "https://www.okx.com/api/v5"
PRODUCTION_WS_URL = "wss://ws.okx.com:8443/ws/v5/business"
OPERATIONAL_NAMES = {
    "backend.env", "paper-account.json", "paper-state.json", "strategies.json",
    "paper-orders.json", "paper-fills.json", "strategy-statuses.json", "risk.json",
    "order-ledger.json", "remote-reservations.json", "pending-remote-exits.json",
    "native-exit-state.json", "native-protection-state.json", "native-protection-exits.json",
    "runtime-log.json", "runtime-log.jsonl", "live-trading.json",
}
AI_FILE_RE = re.compile(r"^ai-(?:config|state|decisions)(?:-[A-Za-z0-9_-]+)?\.jsonl?$")
ENV_ASSIGNMENT_RE = re.compile(r"^(\s*(?:export\s+)?)([A-Za-z_][A-Za-z0-9_]*)\s*=")
BACKEND_COMMAND_RE = re.compile(r"(?:^|\s)(?:\S*(?:/|\\))?backend(?:/|\\)main\.py(?:\s|$)")
BACKEND_MODULE_RE = re.compile(r"(?:^|\s)-m\s+backend\.main(?:\s|$)")


class ResetError(RuntimeError):
    """The account could not be reset safely."""


def default_state_dir() -> Path:
    configured = os.environ.get("NOVATRADE_STATE_DIR")
    return Path(configured).expanduser() if configured else Path.home() / "Library/Application Support/NovaTrade"


def _initial_balance(value: str | float) -> float:
    try:
        balance = float(value)
    except (TypeError, ValueError) as error:
        raise ResetError("initial USDT must be a finite positive number") from error
    if not math.isfinite(balance) or balance <= 0:
        raise ResetError("initial USDT must be a finite positive number")
    return balance


def ensure_backend_stopped(port: int) -> None:
    """Fail before writing if a loopback listener or NovaTrade backend exists."""
    if not 1 <= port <= 65535:
        raise ResetError("backend port must be between 1 and 65535")
    for host, family in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(0.2)
            if connection.connect_ex((host, port)) == 0:
                raise ResetError(f"port {port} is in use; stop NovaTrade and its backend before resetting")
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,command="], check=True, capture_output=True,
            text=True, timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise ResetError("could not check backend processes; no state was changed") from error
    for line in result.stdout.splitlines():
        pid_text, _, command = line.strip().partition(" ")
        is_backend = BACKEND_COMMAND_RE.search(command) or BACKEND_MODULE_RE.search(command)
        if pid_text.isdigit() and int(pid_text) != os.getpid() and is_backend:
            raise ResetError(f"NovaTrade backend PID {pid_text} is running; stop it before resetting")


def _read_json(path: Path, fallback: Any) -> Any:
    if not path.exists():
        return fallback
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResetError(f"cannot read {path.name}; no state was changed") from error


def _paused_strategies(state: dict[str, Any], legacy: Any) -> list[dict[str, Any]]:
    current = state.get("strategies", [])
    if not isinstance(current, list) or not isinstance(legacy, list):
        raise ResetError("strategy configurations must be JSON arrays; no state was changed")
    if not state.get("_fastapiCanonical") and legacy:
        current_ids = {str(row.get("id")) for row in current if isinstance(row, dict)}
        legacy_ids = {str(row.get("id")) for row in legacy if isinstance(row, dict)}
        if not current or not current_ids.intersection(legacy_ids):
            current = legacy
    if any(not isinstance(row, dict) for row in current):
        raise ResetError("strategy configurations contain an invalid row; no state was changed")
    return [{**row, "enabled": False} for row in current]


def _paper_environment(original: str, initial_usdt: float) -> str:
    replacements = {
        "NOVATRADE_TRADING_MODE": "paper",
        "NOVATRADE_PAPER_INITIAL_USDT": format(initial_usdt, ".15g"),
        "OKX_DEMO": "0",
        "OKX_REST_URL": PRODUCTION_REST_URL,
        "OKX_WS_URL": PRODUCTION_WS_URL,
    }
    result: list[str] = []
    written: set[str] = set()
    for line in original.splitlines():
        match = ENV_ASSIGNMENT_RE.match(line)
        key = match.group(2) if match else None
        if key in replacements:
            if key not in written:
                result.append(f"{key}={replacements[key]}")
                written.add(key)
        else:
            result.append(line)
    for key, value in replacements.items():
        if key not in written:
            result.append(f"{key}={value}")
    return "\n".join(result) + "\n"


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")


def _atomic_write(path: Path, data: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _fresh_account(initial_usdt: float) -> tuple[bytes, dict[str, Any]]:
    # The runtime class owns the ledger schema, so this migration remains in
    # sync with future ledger changes without duplicating financial fields.
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    try:
        from backend.paper_trading import PaperTradingAccount
    except ImportError as error:
        raise ResetError("paper trading runtime is unavailable; no state was changed") from error
    with tempfile.TemporaryDirectory(prefix="novatrade-paper-reset-") as temporary:
        account_path = Path(temporary) / "paper-account.json"

        async def generate() -> dict[str, Any]:
            # Python 3.9 binds asyncio.Lock to the active loop at construction.
            account = PaperTradingAccount(account_path, initial_balance=initial_usdt)
            await account.reset(initial_balance=initial_usdt)
            return account.risk_snapshot()

        try:
            fresh_risk = asyncio.run(generate())
            return account_path.read_bytes(), fresh_risk
        except (OSError, ValueError, RuntimeError) as error:
            raise ResetError("cannot prepare fresh paper account; no state was changed") from error


def reset_paper_account(state_dir: Path, initial_usdt: float = 5000, *, port: int = DEFAULT_PORT) -> dict[str, Any]:
    initial_usdt = _initial_balance(initial_usdt)
    ensure_backend_stopped(port)
    state_dir = state_dir.expanduser().resolve()
    if state_dir.exists() and not state_dir.is_dir():
        raise ResetError("state directory is not a directory")
    existing = [path for path in state_dir.iterdir()] if state_dir.exists() else []
    selected = sorted(
        (path for path in existing if path.name in OPERATIONAL_NAMES or AI_FILE_RE.fullmatch(path.name)),
        key=lambda path: path.name,
    )
    for path in selected:
        if path.is_symlink() or not path.is_file():
            raise ResetError(f"{path.name} is not a regular local file; no state was changed")
    state = _read_json(state_dir / "paper-state.json", {})
    if not isinstance(state, dict):
        raise ResetError("paper-state.json must be a JSON object; no state was changed")
    strategies = _paused_strategies(state, _read_json(state_dir / "strategies.json", []))
    # Only the supported Codex config is retained. Retired workers' files
    # remain in the backup and are removed with the obsolete runtime state.
    config_files = [path for path in selected if path.name == "ai-config.json"]
    configs: dict[str, dict[str, Any]] = {}
    for path in config_files:
        config = _read_json(path, {})
        if not isinstance(config, dict):
            raise ResetError(f"{path.name} must be a JSON object; no state was changed")
        configs[path.name] = {**config, "provider": "codex", "enabled": False, "mode": "disabled"}
    try:
        original_env = (state_dir / "backend.env").read_text(encoding="utf-8") if (state_dir / "backend.env").exists() else ""
    except (OSError, UnicodeError) as error:
        raise ResetError("cannot read backend.env; no state was changed") from error
    fresh_account, fresh_risk = _fresh_account(initial_usdt)
    paused_statuses = {
        str(row["id"]): {"id": row["id"], "state": "paused", "pnl": 0, "cooldown": 0, "indicators": {}}
        for row in strategies if row.get("id")
    }
    fresh_state = {
        "schemaVersion": 1, "_fastapiCanonical": True, "strategies": strategies,
        "statuses": paused_statuses, "orders": [], "fills": [], "risk": fresh_risk,
    }
    replacements: dict[str, bytes] = {
        "backend.env": _paper_environment(original_env, initial_usdt).encode("utf-8"),
        "paper-account.json": fresh_account,
        "paper-state.json": _json_bytes(fresh_state),
        "strategies.json": _json_bytes(strategies),
        "runtime-log.json": _json_bytes([]),
        "live-trading.json": _json_bytes({"enabled": False}),
    }
    replacements.update({name: _json_bytes(config) for name, config in configs.items()})
    # Check again after preparing the new state, immediately before writes.
    ensure_backend_stopped(port)
    state_dir.mkdir(parents=True, exist_ok=True)
    backup_root = state_dir / "backups"
    if backup_root.is_symlink():
        raise ResetError("backups directory is a symlink; no state was changed")
    backup_root.mkdir(mode=0o700, exist_ok=True)
    backup_root.chmod(0o700)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    backup_dir = backup_root / f"paper-reset-{stamp}"
    backup_dir.mkdir(mode=0o700)
    for path in selected:
        destination = backup_dir / path.name
        shutil.copyfile(path, destination)
        destination.chmod(0o600)
    _atomic_write(backup_dir / "manifest.json", _json_bytes({
        "resetAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "initialUSDT": initial_usdt, "files": [path.name for path in selected],
    }))
    try:
        for name, data in replacements.items():
            _atomic_write(state_dir / name, data)
        for path in selected:
            if path.name not in replacements:
                path.unlink()
    except OSError as error:
        raise ResetError(f"reset failed; original files are preserved in {backup_dir}") from error
    return {
        "stateDirectory": str(state_dir), "backupDirectory": str(backup_dir),
        "mode": "paper", "initialUSDT": initial_usdt,
        "pausedStrategies": len(strategies), "pausedAIStrategies": len(configs),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", type=Path, default=default_state_dir(), help="NovaTrade Application Support directory")
    parser.add_argument("--initial-usdt", type=float, default=5000, help="fresh paper account balance (default: 5000)")
    parser.add_argument("--port", type=int, default=int(os.environ.get("NOVATRADE_FASTAPI_PORT", DEFAULT_PORT)), help="backend loopback port (default: 8787)")
    args = parser.parse_args()
    try:
        result = reset_paper_account(args.state_dir, args.initial_usdt, port=args.port)
    except (ResetError, OSError) as error:
        parser.exit(1, f"reset refused: {error}\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
