"""Regression checks for the HLSR lab/runtime integration contract."""

from __future__ import annotations

import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[3]
LAB = ROOT / "strategies" / "hlsr"


class HLSRIntegrationMetadataTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.config = json.loads((LAB / "config" / "strategy.json").read_text())
        cls.strategy_doc = (LAB / "STRATEGY.md").read_text()
        cls.readme = (LAB / "README.md").read_text()

    def test_name_and_paper_demo_status_are_stable(self):
        self.assertEqual(self.config["name_zh"], "高位扫顶反转")
        self.assertEqual(self.config["display_name"], "高位扫顶反转做空")
        self.assertEqual(self.config["name_en"], "High-Level Liquidity Sweep Reversal")
        self.assertEqual(self.config["runtime"]["strategy_type"], "hlsr")
        self.assertEqual(self.config["status"], "implemented_paper_demo")
        self.assertEqual(self.config["runtime"]["live_order_mode"], "okx_demo_only")
        self.assertFalse(self.config["runtime"]["auto_submit_live_orders"])

    def test_runtime_uses_15m_and_completed_4h_with_quote_volume(self):
        self.assertEqual(self.config["entry_timeframe_minutes"], 15)
        self.assertEqual(self.config["confirmation_timeframe_minutes"], 15)
        self.assertEqual(self.config["higher_timeframe_minutes"], 240)
        runtime = self.config["runtime"]
        self.assertEqual(runtime["higher_timeframe_minimum_history_bars"], 55)
        self.assertTrue(runtime["quote_volume_required"])
        self.assertEqual(runtime["quote_volume_field"], "volCcyQuote")
        self.assertEqual(self.config["position_management"]["partial_targets"], [0.3, 0.3, 0.4])
        self.assertEqual(self.config["position_management"]["cooldown_bars"], 16)

    def test_lab_docs_keep_name_and_research_limitations(self):
        for document in (self.strategy_doc, self.readme):
            self.assertIn("高位扫顶反转", document)
            self.assertIn("高位扫顶反转做空", document)
            self.assertIn("FAIL", document)
            self.assertTrue("paper" in document or "模拟盘" in document)

    def test_report_writer_uses_configured_name(self):
        source = (LAB / "src" / "high_short_strategy.py").read_text()
        self.assertIn('"strategy_name_zh": lab["name_zh"]', source)
        self.assertIn('"strategy_name_en": lab["name_en"]', source)
        self.assertIn('"strategy_display_name": lab["display_name"]', source)


if __name__ == "__main__":
    unittest.main()
