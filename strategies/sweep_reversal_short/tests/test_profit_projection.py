"""Checks for coverage, cash-flow timing and OHLC price reconstruction."""
from pathlib import Path
import sys
import unittest

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'research'))
import profit_projection as P


class ProfitProjectionTests(unittest.TestCase):
    def test_initial_loss_contributes_to_drawdown(self):
        equity, dd = P.equity_path(np.array([-.2, .1]))
        np.testing.assert_allclose(equity, [1, .8, .88])
        self.assertAlmostEqual(dd, .2)

    def test_r_buckets_cover_edges_and_extreme_values_once(self):
        values = np.array([-20, -1, -.5, 0, .5, 1, 2, 20])
        buckets = P.r_buckets(values)
        self.assertEqual(sum(buckets.values()), len(values))
        self.assertEqual(buckets['< -1R'], 1)
        self.assertEqual(buckets['[-1,-0.5)R'], 1)
        self.assertEqual(buckets['>= 2R'], 2)

    def test_rolling_windows_require_full_coverage_and_use_exits(self):
        start = int(pd.Timestamp('2025-01-15', tz='UTC').value // 1_000_000)
        end = start + 400 * P.DAY_MS
        trades = pd.DataFrame({'exit_recognition_ts': [start + 100 * P.DAY_MS, start + 390 * P.DAY_MS],
                               'pnl': [.1, .2], 'entry': [1., 1.]})
        windows = P.complete_rolling_windows(trades, start, end)
        self.assertEqual(windows[0]['trades'], 1)
        self.assertAlmostEqual(windows[0]['return'], .1)
        self.assertTrue(all(w['end_ts'] - w['start_ts'] == 365 * P.DAY_MS for w in windows))
        self.assertTrue(all(w['end_ts'] <= end for w in windows))
        self.assertEqual(windows[-1]['end_ts'], end)
        self.assertEqual(P.complete_rolling_windows(trades, start, start + 364 * P.DAY_MS), [])

    def test_exit_price_reconstruction_includes_gap_and_time_close(self):
        fee = .0005
        cases = [('sl', 1.3, 1.1), ('tp', .7, .9), ('time', 1.0, .9)]
        for kind, op, close in cases:
            with self.subTest(kind=kind):
                px = op if kind != 'time' else close
                frame = pd.DataFrame([{'symbol': 'X', 'entry_ts': 0, 'entry': 1., 'sl': 1.2, 'tp': .8,
                                       'hold_bars_15m': 1, 'kind': kind,
                                       'pnl': 1 - px - fee * (1 + px)}])
                data = {'X': {'t': np.array([0, P.M15_MS]), 'o': np.array([1., op]),
                              'c': np.array([1., close])}}
                got = P.recover_exits(frame, data, fee).iloc[0]
                self.assertAlmostEqual(got.exit_price, px)
                self.assertEqual(got.entry_execution_ts, P.M15_MS)
                self.assertEqual(got.exit_recognition_ts, 2 * P.M15_MS)

    def test_monthly_returns_use_exit_month_and_retain_empty_month(self):
        start = int(pd.Timestamp('2025-01-01', tz='UTC').value // 1_000_000)
        end = int(pd.Timestamp('2025-04-01', tz='UTC').value // 1_000_000)
        feb = int(pd.Timestamp('2025-02-03', tz='UTC').value // 1_000_000)
        frame = pd.DataFrame({'exit_recognition_ts': [feb], 'entry': [1.], 'pnl': [.1]})
        rows = P.monthly_returns(frame, start, end)
        self.assertEqual([r['trades'] for r in rows], [0, 1, 0])
        self.assertAlmostEqual(rows[-1]['equity_end'], 1.1)

    def test_annualization_uses_dataset_coverage_not_trade_span(self):
        start = int(pd.Timestamp('2025-01-01', tz='UTC').value // 1_000_000)
        end = start + 730 * P.DAY_MS
        frame = pd.DataFrame({'exit_recognition_ts': [start + 10 * P.DAY_MS, start + 20 * P.DAY_MS],
                              'entry': [1., 1.], 'pnl': [.1, .1], 'pnl_R': [1., 1.],
                              'risk': [.1, .1], 'exit_price': [.9, .9], 'kind': ['tp', 'tp']})
        report = {'data': {'start': start, 'end': end - P.H_MS}, 'costs': {'fee_per_side': .0005}}
        got = P.projection(frame, frame, report, draws=100, dd_draws=100)
        self.assertEqual(got['span_days'], 730)
        self.assertAlmostEqual(got['trades_per_year_observed'], 1)
        self.assertAlmostEqual(got['full_period']['annualized'], .1)
        self.assertEqual(got['_equity_curve'][0]['timestamp'], start)
        self.assertEqual(got['_equity_curve'][1]['timestamp'], frame.exit_recognition_ts.iloc[0])
        self.assertEqual(got['_equity_curve'][0]['equity'], 1)
        stressed = got['cost_sensitivity']['scenarios']
        self.assertTrue(all(a['final_multiple'] > b['final_multiple'] for a, b in zip(stressed, stressed[1:])))


if __name__ == '__main__':
    unittest.main()
