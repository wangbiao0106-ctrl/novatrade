#!/usr/bin/env python3
"""Installable strategy-lab packages.

The strategy laboratory owns strategy packages.  A package is a directory or
zip archive with a root ``manifest.json`` and the strategy's lab files (at a
minimum ``STRATEGY.md`` and ``config/strategy.json``).  This module deliberately
uses only the Python standard library so the package manager can be used by an
AI command in a clean checkout.

The command never edits Swift sources.  ``install`` and ``uninstall`` only
change the selected runtime package directory and its ``registry.json``.  A
running service can therefore reload the registry at its normal configuration
boundary while strategy research remains completely independent from runtime
code.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple


SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
REGISTRY_NAME = "registry.json"
DEFAULT_ROOT = Path(__file__).resolve().parents[1]


def _default_runtime_dir() -> Path:
    """Return the runtime package directory, never the source lab directory."""

    override = os.environ.get("OKX_STRATEGY_PACKAGES_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "NovaTrade" / "strategy-packages"
    return Path.home() / ".config" / "NovaTrade" / "strategy-packages"


DEFAULT_STRATEGIES_DIR = _default_runtime_dir()
STRATEGY_ID_RE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
VERSION_RE = re.compile(r"^[vV]?(0|[1-9]\d*)(?:\.(0|[1-9]\d*)){0,2}(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
LIFECYCLES = {"draft", "candidate", "finalized", "retired"}
INSTALLABLE_LIFECYCLE = "finalized"


class PackageError(ValueError):
    """A package failed validation or an install operation was unsafe."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalise_identifier(value: str) -> str:
    value = value.strip().lower().replace("-", "_").replace(" ", "_")
    return value


def _safe_relative(value: str) -> Path:
    """Return a relative path or reject traversal and platform oddities."""

    if not value or "\x00" in value or "\\" in value or Path(value).is_absolute():
        raise PackageError(f"非法包路径：{value!r}")
    parsed = PurePosixPath(value)
    if parsed.is_absolute() or any(part in ("", ".", "..") for part in parsed.parts):
        raise PackageError(f"非法包路径：{value!r}")
    # Keep package contents portable and avoid accidentally replacing the
    # package metadata or registry.
    path = Path(*parsed.parts)
    if path.name == "" or path.name.startswith(".git"):
        raise PackageError(f"非法包路径：{value!r}")
    return path


def _validate_version(value: Any) -> str:
    if not isinstance(value, str) or not VERSION_RE.fullmatch(value.strip()):
        raise PackageError("manifest.version 必须是 1、1.2 或 1.2.3[-预发布] 形式")
    return value.strip().lstrip("vV")


@dataclass(frozen=True, order=False)
class Version:
    numbers: Tuple[int, int, int]
    prerelease: Tuple[str, ...]

    @classmethod
    def parse(cls, value: str) -> "Version":
        clean = _validate_version(value)
        core, _, _build = clean.partition("+")
        numeric, _, pre = core.partition("-")
        bits = tuple(int(part) for part in numeric.split("."))
        bits = (bits + (0, 0, 0))[:3]
        return cls(bits, tuple(pre.split(".")) if pre else ())

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        if self.numbers != other.numbers:
            return self.numbers < other.numbers
        if not self.prerelease and other.prerelease:
            return False
        if self.prerelease and not other.prerelease:
            return True
        for left, right in zip(self.prerelease, other.prerelease):
            if left == right:
                continue
            left_num, right_num = left.isdigit(), right.isdigit()
            if left_num and right_num:
                return int(left) < int(right)
            if left_num != right_num:
                return left_num
            return left < right
        return len(self.prerelease) < len(other.prerelease)

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Version) and self.numbers == other.numbers and self.prerelease == other.prerelease


def _config_identity(config: Mapping[str, Any], strategy_id: str) -> Optional[str]:
    """Read the several historical strategy-id spellings used by the lab."""

    for key in ("strategy_id", "strategyId", "strategy", "id"):
        raw = config.get(key)
        if isinstance(raw, str) and raw.strip():
            return _normalise_identifier(raw)
    # A number of existing lab configs intentionally have no ``strategy`` key.
    return strategy_id


def _read_json(path: Path, label: str) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PackageError(f"无法读取 {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise PackageError(f"{label} 必须是 JSON 对象")
    return value


def _manifest_from_dir(package_dir: Path) -> Dict[str, Any]:
    path = package_dir / MANIFEST_NAME
    if not path.is_file() or path.is_symlink():
        raise PackageError(f"策略包缺少 {MANIFEST_NAME}：{package_dir}")
    return _read_json(path, MANIFEST_NAME)


def _validate_manifest(manifest: Mapping[str, Any], package_dir: Path, require_finalized: bool = False) -> Dict[str, Any]:
    errors: List[str] = []
    # Validate the whole package tree, including files not listed as
    # artifacts.  Install must never follow an untrusted symlink while
    # copying optional research or documentation files.
    for item in package_dir.rglob("*"):
        if item.is_symlink():
            errors.append(f"策略包不允许符号链接：{item.relative_to(package_dir)}")
    if manifest.get("schema_version", manifest.get("schemaVersion")) != SCHEMA_VERSION:
        errors.append(f"schema_version 必须为 {SCHEMA_VERSION}")
    strategy_id = manifest.get("strategy_id", manifest.get("strategyId"))
    if not isinstance(strategy_id, str) or not STRATEGY_ID_RE.fullmatch(strategy_id):
        errors.append("strategy_id 必须是稳定的 snake_case 标识")
        strategy_id = ""
    package_id = manifest.get("package_id", manifest.get("packageId"))
    if not isinstance(package_id, str) or not STRATEGY_ID_RE.fullmatch(package_id):
        errors.append("package_id 必须是稳定的 snake_case 标识")
    elif package_id != strategy_id:
        errors.append("package_id 必须与 strategy_id 一致")
    version = manifest.get("version")
    try:
        normalized_version = _validate_version(version)
    except PackageError as exc:
        errors.append(str(exc))
        normalized_version = str(version or "")
    display_name = manifest.get("display_name", manifest.get("displayName"))
    if not isinstance(display_name, str) or not display_name.strip():
        errors.append("display_name 不能为空")
    lifecycle = manifest.get("lifecycle", manifest.get("status"))
    if lifecycle not in LIFECYCLES:
        errors.append(f"lifecycle 必须是 {', '.join(sorted(LIFECYCLES))}")
    elif require_finalized and lifecycle != INSTALLABLE_LIFECYCLE:
        errors.append("只有 lifecycle=finalized 的实验定稿包才能安装")

    # Package metadata can describe a rule, but it may never turn on live
    # submission. The runtime has its own gate as a second line of defence.
    runtime = manifest.get("runtime")
    if isinstance(runtime, dict) and runtime.get("auto_submit_live_orders") is True:
        errors.append("策略包不得启用自动实盘下单")
    if manifest.get("auto_submit_live_orders") is True:
        errors.append("策略包不得启用自动实盘下单")

    # A package must carry the rule source and machine configuration.  The
    # manifest's artifact hashes make an imported package tamper evident.
    required_files = ("STRATEGY.md", "config/strategy.json")
    for required in required_files:
        if not (package_dir / required).is_file():
            errors.append(f"策略包缺少 {required}")
    config: Dict[str, Any] = {}
    config_path = package_dir / "config" / "strategy.json"
    if config_path.is_file():
        try:
            config = _read_json(config_path, "config/strategy.json")
            config_id = _config_identity(config, str(strategy_id))
            if strategy_id and config_id and config_id != _normalise_identifier(str(strategy_id)):
                errors.append(f"config/strategy.json 的策略标识 {config_id!r} 与 strategy_id {strategy_id!r} 不一致")
            source = config.get("source_of_truth")
            expected_source = f"strategies/{strategy_id}/STRATEGY.md"
            if source is not None and source != expected_source:
                errors.append(f"config.source_of_truth 必须为 {expected_source}")
        except PackageError as exc:
            errors.append(str(exc))

    artifacts = manifest.get("artifacts")
    artifact_map: Dict[str, str] = {}
    if not isinstance(artifacts, list) or not artifacts:
        errors.append("artifacts 必须是带 sha256 的非空数组")
    else:
        for entry in artifacts:
            if isinstance(entry, str):
                errors.append("artifacts 条目必须包含 path 和 sha256")
                continue
            if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not isinstance(entry.get("sha256"), str):
                errors.append("artifacts 条目必须包含 path 和 sha256")
                continue
            try:
                rel = _safe_relative(entry["path"])
            except PackageError as exc:
                errors.append(str(exc))
                continue
            digest = entry["sha256"].lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                errors.append(f"artifact {entry['path']} 的 sha256 无效")
                continue
            key = rel.as_posix()
            if key in artifact_map:
                errors.append(f"artifacts 重复：{key}")
            artifact_map[key] = digest
            actual = package_dir / rel
            if not actual.is_file() or actual.is_symlink():
                errors.append(f"artifact 不存在或不是普通文件：{key}")
            elif _sha256(actual) != digest:
                errors.append(f"artifact 校验失败：{key}")
    for required in required_files:
        if required not in artifact_map:
            errors.append(f"artifacts 未声明 {required}")
    if errors:
        raise PackageError("；".join(errors))
    result = dict(manifest)
    result["schema_version"] = SCHEMA_VERSION
    result["strategy_id"] = str(strategy_id)
    result["version"] = normalized_version
    result["display_name"] = str(display_name).strip()
    result["lifecycle"] = lifecycle
    result["artifacts"] = [{"path": path, "sha256": digest} for path, digest in sorted(artifact_map.items())]
    return result


class _PackageSource:
    def __init__(self, source: Path):
        self.source = source
        self._temporary: Optional[tempfile.TemporaryDirectory[str]] = None
        self.directory: Optional[Path] = None

    def __enter__(self) -> "_PackageSource":
        if self.source.is_symlink():
            raise PackageError(f"策略包入口不允许符号链接：{self.source}")
        if self.source.is_dir():
            self.directory = self.source.resolve()
            return self
        if not self.source.is_file() or self.source.suffix.lower() not in (".zip", ".strategy"):
            raise PackageError(f"策略包必须是目录或 zip 文件：{self.source}")
        self._temporary = tempfile.TemporaryDirectory(prefix="strategy-package-")
        destination = Path(self._temporary.name)
        seen_paths: set[str] = set()
        try:
            with zipfile.ZipFile(self.source) as archive:
                for info in archive.infolist():
                    rel = _safe_relative(info.filename.rstrip("/")) if info.filename.rstrip("/") else None
                    if rel is None:
                        continue
                    key = rel.as_posix()
                    if key in seen_paths:
                        raise PackageError(f"zip 包包含重复路径：{key}")
                    seen_paths.add(key)
                    mode = (info.external_attr >> 16) & 0o170000
                    if mode == stat.S_IFLNK:
                        raise PackageError(f"zip 包不允许符号链接：{info.filename}")
                    target = destination / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(info) as src, target.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
        except (zipfile.BadZipFile, OSError) as exc:
            raise PackageError(f"无法展开策略包：{exc}") from exc
        # Accept archives produced with ``zip -r strategy-dir ...`` as well
        # as the canonical root-manifest layout.  Only flatten a single
        # directory when it actually contains the manifest; arbitrary nested
        # layouts remain invalid and are reported by manifest validation.
        if not (destination / MANIFEST_NAME).is_file():
            children = [child for child in destination.iterdir() if child.name not in (".DS_Store",)]
            if len(children) == 1 and children[0].is_dir() and (children[0] / MANIFEST_NAME).is_file():
                self.directory = children[0]
            else:
                self.directory = destination
        else:
            self.directory = destination
        return self

    def __exit__(self, *_: Any) -> None:
        if self._temporary is not None:
            self._temporary.cleanup()


def validate_package(source: Path | str, require_finalized: bool = False) -> Dict[str, Any]:
    """Validate and return the canonical manifest for a package."""

    with _PackageSource(Path(source).expanduser()) as package:
        assert package.directory is not None
        return _validate_manifest(_manifest_from_dir(package.directory), package.directory, require_finalized=require_finalized)


def _registry_path(strategies_dir: Path) -> Path:
    return strategies_dir / REGISTRY_NAME


def _read_registry(strategies_dir: Path) -> Dict[str, Any]:
    path = _registry_path(strategies_dir)
    if not path.exists():
        return {"schema_version": SCHEMA_VERSION, "packages": {}}
    data = _read_json(path, REGISTRY_NAME)
    if data.get("schema_version") != SCHEMA_VERSION or not isinstance(data.get("packages", {}), dict):
        raise PackageError(f"{path} 不是受支持的策略注册表版本")
    return {"schema_version": SCHEMA_VERSION, "packages": dict(data.get("packages", {}))}


def _discover_record(strategies_dir: Path, strategy_id: str) -> Optional[Dict[str, Any]]:
    """Read a package installed by the Swift registry/API.

    The Swift actor intentionally does not maintain the CLI's registry.json;
    both installers must still be able to upgrade and remove the same package.
    """
    target = strategies_dir / strategy_id
    if not target.is_dir() or target.is_symlink():
        return None
    try:
        manifest = _validate_manifest(_manifest_from_dir(target), target, require_finalized=False)
    except PackageError:
        return None
    return {
        "strategy_id": strategy_id,
        "version": manifest["version"],
        "display_name": manifest["display_name"],
        "lifecycle": manifest["lifecycle"],
        "path": strategy_id,
        "manifest_sha256": _sha256(target / MANIFEST_NAME),
    }


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(_json_dump(value), encoding="utf-8")
    os.replace(temporary, path)


def _copy_tree_contents(source: Path, destination: Path) -> None:
    for item in source.rglob("*"):
        if item.is_symlink():
            raise PackageError(f"策略包不允许符号链接：{item}")
    destination.mkdir(parents=True, exist_ok=False)
    for item in source.iterdir():
        if item.name in (".DS_Store", ".git"):
            continue
        target = destination / item.name
        if item.is_symlink():
            raise PackageError(f"策略包不允许符号链接：{item}")
        if item.is_dir():
            shutil.copytree(item, target, symlinks=False)
        elif item.is_file():
            shutil.copy2(item, target)
        else:
            raise PackageError(f"策略包包含不支持的文件类型：{item}")


def install_package(source: Path | str, strategies_dir: Path | str = DEFAULT_STRATEGIES_DIR, force: bool = False) -> Dict[str, Any]:
    """Install or upgrade a finalized strategy package transactionally."""

    root = Path(strategies_dir).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    with _PackageSource(Path(source).expanduser()) as package:
        assert package.directory is not None
        manifest = _validate_manifest(_manifest_from_dir(package.directory), package.directory, require_finalized=True)
        strategy_id = manifest["strategy_id"]
        target = root / strategy_id
        # The id is validated above, so this check also guards against future
        # changes to the naming regex.
        if target.parent != root:
            raise PackageError("策略包目标路径越界")
        registry = _read_registry(root)
        previous = registry["packages"].get(strategy_id) or _discover_record(root, strategy_id)
        if previous and not force:
            try:
                old_version = Version.parse(str(previous["version"]))
                new_version = Version.parse(manifest["version"])
            except (KeyError, PackageError) as exc:
                raise PackageError(f"注册表中的 {strategy_id} 版本无效，请使用 --force 修复") from exc
            if new_version < old_version:
                raise PackageError(f"拒绝降级 {strategy_id}: 已安装 {previous['version']}，包为 {manifest['version']}")
            if new_version == old_version:
                raise PackageError(f"{strategy_id} 已安装版本 {manifest['version']}，如需重装请使用 --force")
        elif target.exists() and not force:
            raise PackageError(f"目标目录已存在但未在注册表中：{target}；请先卸载或使用 --force")

        staging_parent = Path(tempfile.mkdtemp(prefix=f".{strategy_id}.install-", dir=str(root)))
        staging = staging_parent / strategy_id
        backup: Optional[Path] = None
        try:
            _copy_tree_contents(package.directory, staging)
            # Re-read the copied files before replacing an existing package.
            _validate_manifest(_manifest_from_dir(staging), staging, require_finalized=True)
            if target.exists():
                backup = root / f".{strategy_id}.backup-{os.getpid()}"
                if backup.exists():
                    shutil.rmtree(backup)
                os.replace(target, backup)
            os.replace(staging, target)
            registry["packages"][strategy_id] = {
                "strategy_id": strategy_id,
                "version": manifest["version"],
                "display_name": manifest["display_name"],
                "lifecycle": manifest["lifecycle"],
                "installed_at": _utc_now(),
                "path": strategy_id,
                "manifest_sha256": _sha256(target / MANIFEST_NAME),
            }
            registry["updated_at"] = _utc_now()
            _atomic_json(_registry_path(root), registry)
            # Keep the old tree until registry.json has been committed.  This
            # makes a failed registry write recoverable as a true transaction.
            if backup is not None:
                shutil.rmtree(backup)
        except Exception:
            if target.exists() and backup is not None:
                shutil.rmtree(target)
            if backup is not None and backup.exists():
                os.replace(backup, target)
            elif backup is None and target.exists():
                # The install was a fresh add and the registry write failed.
                shutil.rmtree(target)
            raise
        finally:
            shutil.rmtree(staging_parent, ignore_errors=True)
        return registry["packages"][strategy_id]


def uninstall_package(strategy_id: str, strategies_dir: Path | str = DEFAULT_STRATEGIES_DIR, force: bool = False) -> Dict[str, Any]:
    """Remove one installed package and its registry entry."""

    strategy_id = _normalise_identifier(strategy_id)
    if not STRATEGY_ID_RE.fullmatch(strategy_id):
        raise PackageError("strategy_id 必须是稳定的 snake_case 标识")
    root = Path(strategies_dir).expanduser().resolve()
    target = root / strategy_id
    registry = _read_registry(root)
    record = registry["packages"].get(strategy_id) or _discover_record(root, strategy_id)
    if record is None and not force:
        raise PackageError(f"策略 {strategy_id} 不在注册表中；如需清理残留目录请使用 --force")
    if target.exists() and not target.is_dir():
        raise PackageError(f"策略目录不是目录：{target}")
    if target.exists():
        # Refuse to follow a user-created symlink even with --force.
        if target.is_symlink():
            raise PackageError(f"拒绝删除符号链接：{target}")
        shutil.rmtree(target)
    registry["packages"].pop(strategy_id, None)
    registry["updated_at"] = _utc_now()
    _atomic_json(_registry_path(root), registry)
    return record or {"strategy_id": strategy_id, "removed": True}


def _infer_strategy_id(strategy_dir: Path, config: Mapping[str, Any]) -> str:
    name = _normalise_identifier(str(config.get("strategy_id") or config.get("strategy") or strategy_dir.name))
    # Historical configs use SWEEP_REVERSAL_SHORT/HLSR uppercase constants.
    if name == "hlsr":
        return "hlsr"
    return name


def pack_strategy(strategy_dir: Path | str, output: Path | str, lifecycle: str = "candidate", version: Optional[str] = None) -> Dict[str, Any]:
    """Create a deterministic zip package from a lab strategy directory."""

    source = Path(strategy_dir).expanduser().resolve()
    if not source.is_dir():
        raise PackageError(f"策略实验室目录不存在：{source}")
    config = _read_json(source / "config" / "strategy.json", "config/strategy.json")
    strategy_id = _infer_strategy_id(source, config)
    if not STRATEGY_ID_RE.fullmatch(strategy_id):
        raise PackageError(f"无法从实验室目录得到合法 strategy_id：{strategy_id}")
    chosen_version = version or str(config.get("version") or "0.1.0")
    chosen_version = _validate_version(chosen_version)
    if lifecycle not in LIFECYCLES:
        raise PackageError(f"lifecycle 必须是 {', '.join(sorted(LIFECYCLES))}")
    output_path = Path(output).expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    for item in source.rglob("*"):
        if item.is_symlink():
            raise PackageError(f"策略实验室目录不允许符号链接：{item}")
    with tempfile.TemporaryDirectory(prefix="strategy-pack-") as temp:
        package_dir = Path(temp) / strategy_id
        package_dir.mkdir()
        for item in source.iterdir():
            if item.name in (MANIFEST_NAME, ".DS_Store", ".git"):
                continue
            if item.is_symlink():
                raise PackageError(f"策略实验室目录不允许符号链接：{item}")
            if item.is_dir():
                shutil.copytree(item, package_dir / item.name, symlinks=False)
            elif item.is_file():
                shutil.copy2(item, package_dir / item.name)
        artifacts: List[Dict[str, str]] = []
        for path in sorted(p for p in package_dir.rglob("*") if p.is_file()):
            rel = path.relative_to(package_dir).as_posix()
            artifacts.append({"path": rel, "sha256": _sha256(path)})
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "package_id": strategy_id,
            "strategy_id": strategy_id,
            # The aliases keep packages consumable by the Swift registry and
            # by older laboratory importers while strategy_id remains the
            # canonical field for this tool.
            "identifier": strategy_id,
            "version": chosen_version,
            "display_name": str(config.get("display_name") or config.get("name_zh") or strategy_id),
            "name_en": str(config.get("name_en") or config.get("english_name") or strategy_id),
            "runtime_handler": strategy_id,
            "runtime": {
                "strategy_type": strategy_id,
                "auto_submit_live_orders": False,
                "enabled_by_default": False,
            },
            "lifecycle": lifecycle,
            "source_of_truth": f"strategies/{strategy_id}/STRATEGY.md",
            "artifacts": artifacts,
        }
        (package_dir / MANIFEST_NAME).write_text(_json_dump(manifest), encoding="utf-8")
        # Validate before creating the archive; this catches missing required
        # files and ensures the artifact list is complete.
        _validate_manifest(manifest, package_dir, require_finalized=False)
        temporary_output = output_path.with_name(f".{output_path.name}.tmp-{os.getpid()}")
        with zipfile.ZipFile(temporary_output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(package_dir / MANIFEST_NAME, MANIFEST_NAME)
            for path in sorted(p for p in package_dir.rglob("*") if p.is_file() and p.name != MANIFEST_NAME):
                archive.write(path, path.relative_to(package_dir).as_posix())
        os.replace(temporary_output, output_path)
    return manifest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="策略实验室配置包安装、升级、校验和卸载工具")
    parser.add_argument("--strategies-dir", default=str(DEFAULT_STRATEGIES_DIR), help="运行时策略包目录，默认 %(default)s")
    sub = parser.add_subparsers(dest="command", required=True)
    validate = sub.add_parser("validate", help="校验策略包")
    validate.add_argument("package")
    validate.add_argument("--allow-unfinalized", action="store_true", help="允许校验实验中/候选包")
    install = sub.add_parser("install", aliases=["upgrade", "import"], help="安装或升级实验定稿包")
    install.add_argument("package")
    install.add_argument("--force", action="store_true", help="允许重装相同版本或修复未登记目录")
    install.add_argument("--strategies-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    uninstall = sub.add_parser("uninstall", aliases=["remove"], help="完全移除策略包")
    uninstall.add_argument("strategy_id")
    uninstall.add_argument("--force", action="store_true", help="允许删除未登记的残留目录")
    uninstall.add_argument("--strategies-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    listing = sub.add_parser("list", help="列出已安装策略")
    listing.add_argument("--json", action="store_true", dest="as_json")
    listing.add_argument("--strategies-dir", default=argparse.SUPPRESS, help=argparse.SUPPRESS)
    pack = sub.add_parser("pack", aliases=["build"], help="把实验室目录打包为策略包")
    pack.add_argument("strategy_dir")
    pack.add_argument("--output", required=True)
    pack.add_argument("--lifecycle", choices=sorted(LIFECYCLES), default="candidate")
    pack.add_argument("--finalized", action="store_true", help="将包标为实验定稿，可直接安装")
    pack.add_argument("--version")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "validate":
            manifest = validate_package(args.package, require_finalized=not args.allow_unfinalized)
            print(_json_dump(manifest), end="")
        elif args.command in ("install", "upgrade", "import"):
            print(_json_dump(install_package(args.package, args.strategies_dir, force=args.force)), end="")
        elif args.command in ("uninstall", "remove"):
            print(_json_dump(uninstall_package(args.strategy_id, args.strategies_dir, force=args.force)), end="")
        elif args.command == "list":
            root = Path(args.strategies_dir).expanduser().resolve()
            registry = _read_registry(root)
            # Include packages installed through the Swift HTTP API even when
            # no CLI registry.json has been written yet.
            if root.is_dir():
                for child in root.iterdir():
                    if child.is_dir() and not child.name.startswith("."):
                        discovered = _discover_record(root, child.name)
                        if discovered:
                            registry["packages"].setdefault(child.name, discovered)
            print(_json_dump(registry) if args.as_json else "\n".join(f"{k}\t{v.get('version', '?')}\t{v.get('display_name', '')}" for k, v in sorted(registry["packages"].items())))
        elif args.command in ("pack", "build"):
            lifecycle = "finalized" if args.finalized else args.lifecycle
            print(_json_dump(pack_strategy(args.strategy_dir, args.output, lifecycle=lifecycle, version=args.version)), end="")
        return 0
    except PackageError as exc:
        print(f"strategy-package: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"strategy-package: 文件操作失败：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
