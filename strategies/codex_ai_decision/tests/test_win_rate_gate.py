"""Exercise v1.3 win-rate boundaries without market or model requests."""

import unittest

from test_four_hour_scan import BTC, assessment, decision, snapshot
from backend.ai_policy import validate_decision
from backend.ai_schema import AIConfig


class WinRateGateTests(unittest.TestCase):
    def setUp(self):
        self.source = snapshot()
        self.config = AIConfig(enabled=True, mode="shadow", allowedInstruments=(BTC,))

    def test_selected_open_and_eligible_hold_use_fifty_percent_boundary(self):
        for action in ("open", "hold"):
            for rate in (.45, .48, .499, .5, .56):
                with self.subTest(action=action, winRate=rate):
                    row = decision(self.source, action=action)
                    plan = assessment()
                    row["winRate"] = plan["winRate"] = rate
                    row["assessments"] = [plan]
                    result = validate_decision(row, self.source, self.config)
                    self.assertEqual(result.accepted, rate >= .5, result.reason)
                    if rate < .5:
                        self.assertIn("assessment winRate must be >= 0.50", result.reason)

    def test_low_win_rate_can_remain_visible_as_an_ineligible_assessment(self):
        row = decision(self.source, action="hold")
        plan = assessment()
        plan.update(winRate=.48, entryEligible=False, unmetConditions=["胜率低于50%"])
        row["assessments"] = [plan]
        self.assertTrue(validate_decision(row, self.source, self.config).accepted)

    def test_close_and_cancel_are_allowed_below_the_open_win_rate_gate(self):
        self.source.account.update(
            positions=[{"instrumentID": BTC, "quantity": 1, "side": "long"}],
            pendingOrders=[{"id": "pending-1", "instrumentID": BTC, "status": "live", "quantity": 1}],
        )
        for action in ("close", "cancel"):
            with self.subTest(action=action):
                row = decision(self.source, action=action)
                row["winRate"] = .48
                result = validate_decision(row, self.source, self.config)
                self.assertTrue(result.accepted, result.reason)


if __name__ == "__main__":
    unittest.main()
