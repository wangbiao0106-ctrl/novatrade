#!/usr/bin/env python3
"""Regression tests for scripts/strategy_package.py."""

import json
import tempfile
import unittest
import zipfile
from pathlib import Path

try:  # Works both as ``python scripts/test_strategy_package.py`` and unittest module.
    from strategy_package import PackageError, install_package, pack_strategy, uninstall_package, validate_package
except ModuleNotFoundError:
    from scripts.strategy_package import PackageError, install_package, pack_strategy, uninstall_package, validate_package


class StrategyPackageTests(unittest.TestCase):
    def _lab(self, root: Path, version: str = "1.0.0") -> Path:
        lab = root / "hlsr"
        (lab / "config").mkdir(parents=True)
        (lab / "STRATEGY.md").write_text("# HLSR\n", encoding="utf-8")
        (lab / "README.md").write_text("lab\n", encoding="utf-8")
        (lab / "config" / "strategy.json").write_text(
            json.dumps(
                {
                    "strategy": "HLSR",
                    "version": version,
                    "display_name": "高位扫顶反转做空",
                    "source_of_truth": "strategies/hlsr/STRATEGY.md",
                }
            ),
            encoding="utf-8",
        )
        return lab

    def test_pack_validate_install_and_uninstall(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            lab = self._lab(root)
            package = root / "hlsr.zip"
            manifest = pack_strategy(lab, package, lifecycle="finalized")
            self.assertEqual(manifest["strategy_id"], "hlsr")
            self.assertEqual(validate_package(package, require_finalized=True)["lifecycle"], "finalized")
            installed = root / "strategies"
            install_package(package, installed)
            self.assertTrue((installed / "hlsr" / "config" / "strategy.json").is_file())
            uninstall_package("hlsr", installed)
            self.assertFalse((installed / "hlsr").exists())
            self.assertNotIn("hlsr", json.loads((installed / "registry.json").read_text())["packages"])

    def test_unfinalized_package_is_rejected_for_install(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "hlsr.zip"
            pack_strategy(self._lab(root), package, lifecycle="candidate")
            with self.assertRaises(PackageError):
                install_package(package, root / "strategies")

    def test_tamper_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "hlsr.zip"
            pack_strategy(self._lab(root), package, lifecycle="finalized")
            tampered = root / "tampered.zip"
            with zipfile.ZipFile(package) as source, zipfile.ZipFile(tampered, "w") as target:
                for entry in source.infolist():
                    data = source.read(entry)
                    if entry.filename == "STRATEGY.md":
                        data += b"tampered\n"
                    target.writestr(entry, data)
            with self.assertRaises(PackageError):
                validate_package(tampered, require_finalized=True)

    def test_downgrade_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            installed = root / "strategies"
            first = root / "first.zip"
            second = root / "second.zip"
            pack_strategy(self._lab(root / "first-lab", "2.0.0"), first, lifecycle="finalized")
            pack_strategy(self._lab(root / "second-lab", "1.0.0"), second, lifecycle="finalized")
            install_package(first, installed)
            with self.assertRaises(PackageError):
                install_package(second, installed)


if __name__ == "__main__":
    unittest.main()
