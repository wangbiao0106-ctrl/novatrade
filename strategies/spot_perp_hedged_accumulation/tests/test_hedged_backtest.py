from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SOURCE = ROOT / "strategies" / "spot_perp_hedged_accumulation" / "src" / "hedged_backtest.py"
spec = importlib.util.spec_from_file_location("spha_backtest", SOURCE)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class HedgedBacktestTests(unittest.TestCase):
    def test_aggregation_drops_incomplete_or_non_contiguous_hours(self):
        complete = [module.Bar(index * module.FIVE_MINUTES, 100, 101, 99, 100, 1) for index in range(12)]
        missing = [module.Bar(module.HOUR + index * module.FIVE_MINUTES, 100, 101, 99, 100, 1) for index in range(11)]
        rows = module.aggregate_to_hour(complete + missing)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].ts, 0)
        self.assertEqual(rows[0].quote_volume, 12)

    def test_initial_reserve_and_cross_margin_allocation(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.enter_portfolio(10_000, 100, config)
        self.assertEqual(config["portfolio"]["margin_mode"], "cross")
        self.assertAlmostEqual(portfolio.reserve_cash, 2_000.0, places=9)
        self.assertAlmostEqual(portfolio.spot_qty * 100, 4_000.0, places=6)
        self.assertAlmostEqual(portfolio.short_qty * 100, 4_000.0, places=6)
        self.assertAlmostEqual(portfolio.spot_qty, portfolio.short_qty, places=9)
        self.assertAlmostEqual(portfolio.core_spot_qty, portfolio.core_short_qty, places=9)
        self.assertAlmostEqual(portfolio.tactical_spot_qty, 0.0, places=9)
        self.assertAlmostEqual(portfolio.notional_deviation(100), 0.0, places=9)

    def test_fixed_core_grid_buy_uses_reserve_without_moving_short(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.enter_portfolio(10_000, 100, config)
        core_spot = portfolio.core_spot_qty
        core_short = portfolio.core_short_qty
        reserve_before = portfolio.reserve_cash
        bought = module._buy_tactical_spot_from_reserve(portfolio, 250, 90, config, config["costs"])
        self.assertGreater(bought, 0.0)
        self.assertAlmostEqual(portfolio.core_spot_qty, core_spot, places=9)
        self.assertAlmostEqual(portfolio.core_short_qty, core_short, places=9)
        self.assertAlmostEqual(portfolio.short_qty, core_short, places=9)
        self.assertAlmostEqual(portfolio.delta_base, portfolio.tactical_spot_qty, places=9)
        self.assertLess(portfolio.reserve_cash, reserve_before)

    def test_fixed_core_grid_sell_cannot_sell_core_spot(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.enter_portfolio(10_000, 100, config)
        module._buy_tactical_spot_from_reserve(portfolio, 250, 90, config, config["costs"])
        core_spot = portfolio.core_spot_qty
        reserve_before = portfolio.reserve_cash
        sold = module._sell_tactical_spot_to_reserve(portfolio, portfolio.tactical_spot_qty, 100, config["costs"])
        self.assertGreater(sold, 0.0)
        self.assertAlmostEqual(portfolio.spot_qty, core_spot, places=9)
        self.assertAlmostEqual(portfolio.short_qty, portfolio.core_short_qty, places=9)
        self.assertAlmostEqual(portfolio.tactical_spot_qty, 0.0, places=9)
        self.assertGreater(portfolio.reserve_cash, reserve_before)

    def test_fixed_core_grid_caps_tactical_inventory(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.enter_portfolio(10_000, 100, config)
        cap = portfolio.core_spot_qty * config["rebalance"]["max_tactical_spot_fraction_of_core"]
        module._buy_tactical_spot_from_reserve(portfolio, 10_000, 90, config, config["costs"])
        self.assertAlmostEqual(portfolio.tactical_spot_qty, cap, places=9)
        self.assertAlmostEqual(portfolio.core_spot_qty, portfolio.core_short_qty, places=9)

    def test_position_equity_is_equal_at_entry_while_leverage_changes_margin(self):
        for leverage, expected_collateral in ((2.0, 2_000.0), (5.0, 800.0), (10.0, 400.0)):
            config = module.merge_config(None)
            config["portfolio"]["leverage"] = leverage
            config["costs"]["fee_rate_one_way"] = 0.0
            config["costs"]["slippage_one_way"] = 0.0
            portfolio = module.enter_portfolio(10_000, 100, config)
            self.assertAlmostEqual(portfolio.spot_position_equity(100), 4_000.0, places=9)
            self.assertAlmostEqual(portfolio.short_position_equity(100), 4_000.0, places=9)
            self.assertAlmostEqual(portfolio.position_equity_deviation(100), 0.0, places=9)
            self.assertAlmostEqual(portfolio.collateral, expected_collateral, places=9)
            self.assertAlmostEqual(portfolio.short_cross_buffer(), 4_000.0 - expected_collateral, places=9)

    def test_position_equity_rebalance_down_targets_equity_and_buys_spot(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.enter_portfolio(10_000, 100, config)
        closed, bought = module._reduce_short_and_buy_spot(portfolio, 90, config, config["costs"])
        self.assertGreater(closed, 0.0)
        self.assertGreater(bought, 0.0)
        self.assertLess(portfolio.position_equity_deviation(90), 1e-8)
        self.assertGreater(portfolio.spot_qty, portfolio.short_qty)
        self.assertGreater(portfolio.reserve_cash, 2_000.0)

    def test_position_equity_rebalance_up_adds_short_even_when_quantities_start_equal(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.enter_portfolio(10_000, 100, config)
        added = module._add_short_from_reserve_to_gap(portfolio, 110, config, config["costs"])
        self.assertGreater(added, 0.0)
        self.assertLess(portfolio.position_equity_deviation(110), 1e-8)
        self.assertGreater(portfolio.short_qty, portfolio.spot_qty)
        self.assertGreaterEqual(portfolio.reserve_cash, module._reserve_floor(portfolio, config))

    def test_reserve_floor_with_unhedged_spot_requires_flattening(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.enter_portfolio(10_000, 100, config)
        portfolio.reserve_cash = module._reserve_floor(portfolio, config)
        self.assertFalse(module._reserve_floor_exit_required(portfolio, 110, config))
        portfolio.tactical_spot_qty = 1.0
        self.assertTrue(module._reserve_floor_exit_required(portfolio, 110, config))
        self.assertTrue(module._reserve_floor_exit_required(portfolio, 90, config))

    def test_down_rebalance_releases_short_and_buys_half_credit(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.Portfolio(0, 30, 40, 100, 2_000, 0, 2_000, 10_000)
        closed, bought = module._reduce_short_and_buy_spot(portfolio, 100, config, config["costs"])
        self.assertAlmostEqual(closed, 8.0, places=9)
        self.assertAlmostEqual(bought, 2.0, places=9)
        self.assertAlmostEqual(portfolio.spot_qty, 32.0, places=9)
        self.assertAlmostEqual(portfolio.short_qty, 32.0, places=9)
        self.assertAlmostEqual(portfolio.reserve_cash, 2_200.0, places=9)

        balanced = module.Portfolio(0, 40, 40, 100, 2_000, 0, 2_000, 10_000)
        self.assertEqual(module._reduce_short_and_buy_spot(balanced, 100, config, config["costs"]), (0.0, 0.0))
        self.assertEqual((balanced.spot_qty, balanced.short_qty), (40, 40))

    def test_up_rebalance_uses_reserve_without_selling_spot(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.Portfolio(0, 40, 30, 100, 1_500, 0, 2_000, 10_000)
        added = module._add_short_from_reserve_to_gap(portfolio, 100, config, config["costs"])
        self.assertAlmostEqual(added, 10.0, places=9)
        self.assertAlmostEqual(portfolio.spot_qty, 40.0, places=9)
        self.assertAlmostEqual(portfolio.short_qty, 40.0, places=9)
        self.assertAlmostEqual(portfolio.reserve_cash, 1_500.0, places=9)
        self.assertGreaterEqual(portfolio.reserve_cash, module._reserve_floor(portfolio, config))

    def test_up_rebalance_skips_when_reserve_is_at_floor(self):
        config = module.merge_config(None)
        portfolio = module.Portfolio(0, 40, 30, 100, 1_500, 0, 1_000, 10_000)
        added = module._add_short_from_reserve_to_gap(portfolio, 100, config, config["costs"])
        self.assertEqual(added, 0.0)
        self.assertEqual(portfolio.short_qty, 30.0)

    def test_down_rebalance_respects_deviation_cap_after_price_gap(self):
        config = module.merge_config(None)
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        portfolio = module.Portfolio(0, 40, 44, 100, 2_000, 0, 2_000, 10_000)
        module._reduce_short_and_buy_spot(portfolio, 20, config, config["costs"])
        self.assertLessEqual(portfolio.notional_deviation(20), 0.20 + 1e-9)
        self.assertGreaterEqual(portfolio.reserve_cash, module._reserve_floor(portfolio, config))

    def test_notional_deviation_is_zero_for_equal_legs_and_twenty_percent_at_1_1_ratio_band(self):
        equal = module.Portfolio(0, 1, 1, 100, 0, 0)
        tilted = module.Portfolio(0, 1.1, 0.9, 100, 0, 0)
        self.assertAlmostEqual(equal.notional_deviation(100), 0.0)
        self.assertAlmostEqual(tilted.notional_deviation(100), 0.20)

    def test_summary_uses_compounded_returns(self):
        trades = [
            module.Trade("BTC", 1, 2, 3, "ema_pullback", 100, 100, 10_000, 11_000, 10, 0, 0, 0, 0, "max_hold"),
            module.Trade("BTC", 4, 5, 6, "ema_pullback", 100, 100, 10_000, 9_000, -10, 0, 0, 0, 0, "max_hold"),
        ]
        result = module.summary(trades)
        self.assertAlmostEqual(result["compound_return_pct"], -1.0)
        self.assertAlmostEqual(result["max_drawdown_pct"], 10.0)

    def test_over_cap_correction_bypasses_price_action_limit(self):
        bars = [module.Bar(index * module.HOUR, 100, 100, 100, 100) for index in range(4)]
        config = module.merge_config(None)
        config["rebalance"]["mode"] = "equity_rebalance"
        config["portfolio"]["research_max_hold_bars"] = 3
        config["rebalance"]["max_actions_per_campaign"] = 0
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        original_signal_finder = module.find_signals
        original_entry = module.enter_portfolio
        module.find_signals = lambda symbol, rows, cfg: [
            module.Signal(symbol, 0, rows[0].ts, "test", None, 100, 100)
        ]
        module.enter_portfolio = lambda capital, price, cfg: module.Portfolio(
            capital, 1.2, 0.8, 100, 0, 0, 1_500, 10_000
        )
        try:
            _, rebalances = module.backtest("BTC", bars, config=config)
        finally:
            module.find_signals = original_signal_finder
            module.enter_portfolio = original_entry
        self.assertEqual(len(rebalances), 1)
        self.assertEqual(rebalances[0].action, "add_short_from_reserve")
        self.assertLessEqual(rebalances[0].notional_deviation_pct, 0.20 + 1e-9)

    def test_fixed_core_backtest_only_moves_tactical_spot(self):
        bars = [
            module.Bar(0 * module.HOUR, 100, 100, 100, 100),
            module.Bar(1 * module.HOUR, 100, 100, 95, 95),
            module.Bar(2 * module.HOUR, 95, 100, 95, 100),
            module.Bar(3 * module.HOUR, 100, 100, 100, 100),
            module.Bar(4 * module.HOUR, 100, 100, 100, 100),
        ]
        config = module.merge_config(None)
        config["portfolio"]["research_max_hold_bars"] = 4
        config["rebalance"]["max_actions_per_campaign"] = 4
        config["rebalance"]["buy_reserve_fraction"] = 1.0
        config["costs"]["fee_rate_one_way"] = 0.0
        config["costs"]["slippage_one_way"] = 0.0
        original_signal_finder = module.find_signals
        module.find_signals = lambda symbol, rows, cfg: [
            module.Signal(symbol, 0, rows[0].ts, "test", None, 100, 100)
        ]
        try:
            trades, rebalances = module.backtest("BTC", bars, config=config)
        finally:
            module.find_signals = original_signal_finder
        self.assertEqual([item.action for item in rebalances], ["buy_tactical_spot", "sell_tactical_spot"])
        self.assertTrue(all(abs(item.short_qty - rebalances[0].short_qty) < 1e-9 for item in rebalances))
        self.assertLessEqual(max(abs(item.delta_base) for item in rebalances) / rebalances[0].short_qty, 0.10 + 1e-9)
        self.assertEqual(trades[0].exit_reason, "data_end")


if __name__ == "__main__":
    unittest.main()
