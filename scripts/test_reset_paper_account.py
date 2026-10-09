#!/usr/bin/env python3
"""Regression checks for the offline paper-account migration."""

import asyncio
import json
import socket
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    import reset_paper_account as reset
except ModuleNotFoundError:
    from scripts import reset_paper_account as reset


class ResetPaperAccountTests(unittest.TestCase):
    def _write(self, root, name, value):
        (root / name).write_text(json.dumps(value), encoding="utf-8")

    def _account(self, path):
        sys.path.insert(0, str(reset.ROOT))
        from backend.paper_trading import PaperTradingAccount

        async def load():
            return PaperTradingAccount(path)

        return asyncio.run(load())

    def test_reset_preserves_configs_credentials_and_backup_but_clears_financial_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            original_env = "# existing secrets\nOKX_API_KEY=keep-key\nOKX_SECRET_KEY=keep-secret\nOKX_PASSPHRASE=keep-pass\nOKX_DEMO=1\nexport OKX_REST_URL=https://demo.invalid/api/v5\nOKX_WS_URL=wss://demo.invalid/business\nNOVATRADE_TRADING_MODE=demo\nOTHER_SETTING=keep\n"
            (root / "backend.env").write_text(original_env, encoding="utf-8")
            strategy = {"id": "strategy-1", "enabled": True, "parameters": {"leverage": 2}, "capitalPoolPercent": 33.33}
            original_state = {"_fastapiCanonical": True, "strategies": [strategy], "orders": [{"id": "old-order"}], "fills": [{"id": "old-fill"}], "risk": {"equity": 104000}, "statuses": {"strategy-1": {"pnl": 99}}}
            self._write(root, "paper-state.json", original_state)
            self._write(root, "strategies.json", [strategy])
            ai_config = {"enabled": True, "mode": "demo-active", "allowedInstruments": ["BTC-USDT-SWAP"], "marginPerOrderUSD": 500, "maxDailyOrders": 20}
            self._write(root, "ai-config.json", ai_config)
            self._write(root, "ai-config-deepseek.json", {**ai_config, "provider": "deepseek-harness"})
            for name in ("order-ledger.json", "remote-reservations.json", "pending-remote-exits.json", "native-exit-state.json", "ai-state.json", "ai-state-deepseek.json", "runtime-log.json"):
                self._write(root, name, {"old": True})
            for name in ("runtime-log.jsonl", "ai-decisions.jsonl", "ai-decisions-deepseek.jsonl"):
                (root / name).write_text('{"old": true}\n', encoding="utf-8")
            (root / "strategy-packages").mkdir()
            self._write(root / "strategy-packages", "registry.json", {"packages": {"keep": True}})
            (root / "locald.token").write_text("keep-local-token", encoding="utf-8")
            with patch.object(reset, "ensure_backend_stopped") as stopped:
                result = reset.reset_paper_account(root)
            self.assertEqual(stopped.call_count, 2)
            account_path = root / "paper-account.json"
            # The runtime projection verifies the reset through the same API
            # used by account requests, rather than duplicating its schema.
            account = self._account(account_path)
            self.assertEqual(account.account_snapshot()["equityUSD"], 5000)
            self.assertEqual(account.positions(), [])
            self.assertEqual(account.pending_orders(), [])
            self.assertEqual(account.all_orders(), [])
            self.assertEqual(account.all_fills(), [])
            state = json.loads((root / "paper-state.json").read_text())
            self.assertEqual(state["orders"], [])
            self.assertEqual(state["fills"], [])
            self.assertEqual(state["risk"]["equity"], 5000)
            self.assertEqual(state["statuses"]["strategy-1"]["pnl"], 0)
            self.assertEqual(state["strategies"], [{**strategy, "enabled": False}])
            for name in ("ai-config.json",):
                config = json.loads((root / name).read_text())
                self.assertFalse(config["enabled"])
                self.assertEqual(config["mode"], "disabled")
                self.assertEqual(config["allowedInstruments"], ai_config["allowedInstruments"])
                self.assertEqual(config["marginPerOrderUSD"], 500)
            self.assertFalse((root / "ai-config-deepseek.json").exists())
            env = (root / "backend.env").read_text()
            for preserved in ("OKX_API_KEY=keep-key", "OKX_SECRET_KEY=keep-secret", "OKX_PASSPHRASE=keep-pass", "OTHER_SETTING=keep"):
                self.assertIn(preserved, env)
            self.assertIn("NOVATRADE_TRADING_MODE=paper\n", env)
            self.assertIn("NOVATRADE_PAPER_INITIAL_USDT=5000\n", env)
            self.assertIn("OKX_DEMO=0\n", env)
            self.assertIn(f"OKX_REST_URL={reset.PRODUCTION_REST_URL}\n", env)
            self.assertIn(f"OKX_WS_URL={reset.PRODUCTION_WS_URL}\n", env)
            self.assertNotIn("demo.invalid", env)
            backup = Path(result["backupDirectory"])
            self.assertEqual(backup.stat().st_mode & 0o777, 0o700)
            self.assertEqual((backup / "backend.env").stat().st_mode & 0o777, 0o600)
            self.assertTrue((backup / "ai-config-deepseek.json").exists())
            self.assertEqual((backup / "backend.env").read_text(), original_env)
            self.assertEqual(json.loads((backup / "paper-state.json").read_text()), original_state)
            self.assertTrue((backup / "order-ledger.json").is_file())
            self.assertTrue((backup / "ai-decisions.jsonl").is_file())
            self.assertFalse((root / "order-ledger.json").exists())
            self.assertFalse((root / "remote-reservations.json").exists())
            self.assertFalse((root / "pending-remote-exits.json").exists())
            self.assertFalse((root / "ai-state.json").exists())
            self.assertFalse((root / "ai-decisions.jsonl").exists())
            self.assertEqual((root / "locald.token").read_text(), "keep-local-token")
            self.assertEqual(json.loads((root / "strategy-packages/registry.json").read_text()), {"packages": {"keep": True}})

    def test_running_listener_is_rejected_before_state_directory_is_created(self):
        with tempfile.TemporaryDirectory() as temporary, socket.socket() as listener:
            root = Path(temporary) / "state-not-created"
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            port = listener.getsockname()[1]
            with self.assertRaisesRegex(reset.ResetError, "port .* is in use"):
                reset.reset_paper_account(root, port=port)
            self.assertFalse(root.exists())

    def test_running_backend_on_another_port_is_rejected(self):
        for command in (
            "12345 python3 /Applications/NovaTrade.app/Contents/MacOS/backend/main.py\n",
            "12345 python3 backend/main.py\n",
            "12345 python3 -m backend.main\n",
        ):
            with self.subTest(command=command), \
                 patch.object(reset.socket, "socket") as socket_factory, \
                 patch.object(reset.subprocess, "run", return_value=SimpleNamespace(stdout=command)):
                socket_factory.return_value.__enter__.return_value.connect_ex.return_value = 1
                with self.assertRaisesRegex(reset.ResetError, "backend PID 12345"):
                    reset.ensure_backend_stopped(8787)

    def test_malformed_config_and_invalid_balance_do_not_change_files(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "ai-config.json").write_text("not-json", encoding="utf-8")
            with patch.object(reset, "ensure_backend_stopped"):
                with self.assertRaises(reset.ResetError):
                    reset.reset_paper_account(root)
            self.assertEqual((root / "ai-config.json").read_text(), "not-json")
            self.assertFalse((root / "backups").exists())
            for balance in (0, -1, float("nan"), float("inf")):
                with self.subTest(balance=balance), self.assertRaises(reset.ResetError):
                    reset.reset_paper_account(root, balance)

    def test_repeated_reset_archives_previous_paper_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with patch.object(reset, "ensure_backend_stopped"):
                first = reset.reset_paper_account(root, 5000)
                second = reset.reset_paper_account(root, 7000)
            self.assertNotEqual(first["backupDirectory"], second["backupDirectory"])
            self.assertEqual(self._account(root / "paper-account.json").account_snapshot()["equityUSD"], 7000)
            archived = Path(second["backupDirectory"]) / "paper-account.json"
            self.assertEqual(self._account(archived).account_snapshot()["equityUSD"], 5000)

    def test_legacy_strategy_configs_are_preserved_and_disabled(self):
        old = {"id": "old-id", "enabled": True, "parameters": {"leverage": 3}}
        current = {"id": "new-id", "enabled": True, "parameters": {"leverage": 2}}
        self.assertEqual(reset._paused_strategies({"strategies": [old]}, [current]), [{**current, "enabled": False}])
        self.assertEqual(reset._paused_strategies({"_fastapiCanonical": True, "strategies": [old]}, [current]), [{**old, "enabled": False}])


if __name__ == "__main__":
    unittest.main()
