from __future__ import annotations

import csv
import importlib.util
import io
import sys
import tempfile
import unittest
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "strategies" / "personal_trading_style_backtest" / "src" / "backtest.py"
spec = importlib.util.spec_from_file_location("personal_style_backtest", SOURCE)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
assert spec.loader is not None
spec.loader.exec_module(module)


UTC8 = timezone(timedelta(hours=8))


def bill_row(timestamp: str, order_id: str, symbol: str, side: str, quantity: str, price: str, pnl: str, fee: str) -> list[str]:
    return [
        order_id,
        order_id,
        timestamp,
        "永续合约",
        symbol,
        side,
        quantity,
        "张",
        price,
        pnl,
        fee,
        "USDT",
        "0",
        "0",
        "0",
        "1000",
        "USDT",
    ]


class PersonalStyleBacktestTests(unittest.TestCase):
    def test_order_groups_use_weighted_price_and_preserve_realized_pnl(self):
        rows = [
            module.BillRow(datetime(2026, 8, 1, tzinfo=UTC8), "永续合约", "ALT-USDT-SWAP", "卖出", "1", Decimal("2"), Decimal("10"), Decimal("1"), Decimal("0"), Decimal("0")),
            module.BillRow(datetime(2026, 8, 1, 0, 0, 1, tzinfo=UTC8), "永续合约", "ALT-USDT-SWAP", "卖出", "1", Decimal("4"), Decimal("20"), Decimal("2"), Decimal("-0.1"), Decimal("0")),
        ]
        groups = module.aggregate_order_groups(rows)
        self.assertEqual(len(groups), 1)
        self.assertEqual(groups[0].quantity, Decimal("6"))
        self.assertEqual(groups[0].average_price, Decimal("100") / Decimal("6"))
        self.assertEqual(groups[0].realized_pnl, Decimal("3"))
        self.assertEqual(groups[0].net_pnl, Decimal("2.9"))
        self.assertEqual(groups[0].last_timestamp, datetime(2026, 8, 1, 0, 0, 1, tzinfo=UTC8))

    def test_liquidation_event_falls_back_to_position_balance_change(self):
        row = module.BillRow(
            datetime(2026, 8, 1, tzinfo=UTC8), "永续合约", "ALT-USDT-SWAP", "强平惩罚费", "liq",
            Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("0"), Decimal("-5"),
        )
        event = module.collect_events([row])[0]
        self.assertEqual(event.net_pnl, Decimal("-5"))

    def test_short_episode_is_inferred_from_zero_sell_then_realized_buy(self):
        orders = [
            module.OrderGroup("open", "ALT-USDT-SWAP", datetime(2026, 8, 1, tzinfo=UTC8), "卖出", Decimal("10"), Decimal("100")),
            module.OrderGroup("add", "ALT-USDT-SWAP", datetime(2026, 8, 1, 0, 5, tzinfo=UTC8), "卖出", Decimal("5"), Decimal("45")),
            module.OrderGroup("close", "ALT-USDT-SWAP", datetime(2026, 8, 1, 0, 20, tzinfo=UTC8), "买入", Decimal("15"), Decimal("120"), Decimal("30"), Decimal("-0.3")),
        ]
        episodes = module.infer_episodes(orders, [])
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].direction, "short")
        self.assertEqual(episodes[0].entry_orders, 2)
        self.assertEqual(episodes[0].add_count, 1)
        self.assertEqual(episodes[0].net_pnl, Decimal("29.7"))

    def test_prior_loss_guard_excludes_only_entry_time_loss(self):
        start = datetime(2026, 8, 1, tzinfo=UTC8)
        orders = [
            module.OrderGroup("prior", "ALT-USDT-SWAP", start - timedelta(hours=1), "买入", Decimal("1"), Decimal("1"), Decimal("-5")),
            module.OrderGroup("entry", "ALT-USDT-SWAP", start, "卖出", Decimal("1"), Decimal("1")),
        ]
        episodes = module.infer_episodes(orders + [
            module.OrderGroup("exit", "ALT-USDT-SWAP", start + timedelta(minutes=1), "买入", Decimal("1"), Decimal("1"), Decimal("2")),
        ], [])
        self.assertEqual(len(episodes), 1)
        self.assertLess(episodes[0].prior_6h_net, Decimal("0"))
        self.assertFalse(module.policy_matches(episodes[0], {"direction": "short", "require_prior_6h_nonnegative": True}))

    def test_zip_reader_skips_metadata_and_reads_utf8_bom(self):
        header = ["id", "关联订单id", "时间", "账单类型", "交易品种", "交易类型", "数量", "交易单位", "成交价", "收益", "手续费", "手续费单位", "仓位余额变动", "仓位余额", "交易账户余额变动", "交易账户余额", "交易账户余额单位"]
        buffer = io.StringIO()
        writer = csv.writer(buffer, lineterminator="\n")
        writer.writerow(header)
        writer.writerow(bill_row("2026-08-01 00:00:00", "o1", "ALT-USDT-SWAP", "卖出", "1", "1", "0", "-0.1"))
        with tempfile.TemporaryDirectory() as directory:
            archive_path = Path(directory) / "ledger.zip"
            member = "ledger.csv"
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                content = "用户ID:1,账户类型:Main,时区:UTC+8\n" + "".join([]) + "" + "\ufeff" + buffer.getvalue()
                archive.writestr(member, content.encode("utf-8"))
            rows, manifest = module.read_ledger(archive_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].symbol, "ALT-USDT-SWAP")
        self.assertEqual(manifest["row_count"], 1)


if __name__ == "__main__":
    unittest.main()
