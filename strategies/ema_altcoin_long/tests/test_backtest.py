import importlib.util
import sys
import unittest
from pathlib import Path


SRC = Path(__file__).resolve().parents[1] / "src" / "backtest.py"
SPEC = importlib.util.spec_from_file_location("ema_altcoin_long_backtest", SRC)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


class BacktestRulesTests(unittest.TestCase):
    def test_resample_requires_twelve_consecutive_bars(self):
        complete = [MODULE.Bar(i * 300_000, 1, 1, 1, 1, 1) for i in range(12)]
        self.assertEqual(len(MODULE.resample(complete)), 1)
        missing = complete[:5] + complete[6:]
        self.assertEqual(MODULE.resample(missing), [])

    def test_slippage_is_applied_in_the_adverse_direction(self):
        self.assertAlmostEqual(MODULE.execution_price(100, "buy", 0.0002), 100.02)
        self.assertAlmostEqual(MODULE.exit_price(100, "buy", 0.0002), 99.98)

    def test_portfolio_filter_caps_open_positions(self):
        trades = [
            MODULE.Trade("AAA", 0, 1, 10, 1, 1, 1, 1, 1, 1, "target"),
            MODULE.Trade("BBB", 0, 2, 11, 1, 1, 1, 1, 1, 1, "target"),
            MODULE.Trade("CCC", 0, 3, 12, 1, 1, 1, 1, 1, 1, "target"),
        ]
        accepted = MODULE.portfolio_filter(trades, max_concurrent=2, max_open_r=2)
        self.assertEqual([trade.symbol for trade in accepted], ["AAA", "BBB"])
        ranked = MODULE.portfolio_filter(list(reversed(trades)), max_concurrent=1, max_open_r=1, symbol_rank={"BBB": 1, "AAA": 0, "CCC": 2})
        self.assertEqual([trade.symbol for trade in ranked], ["AAA"])


GATE_OPEN = (110.0, 105.0, 100.0)   # close > EMA60 > 六小时前 EMA60
GATE_SHUT = (100.0, 105.0, 110.0)   # 门控关闭

PARAMS = dict(ema_fast=20, ema_slow=60, ema_trend=120, atr_period=14, cluster_atr=0.75,
              breakout_bars=4, pullback_bars=6, pullback_atr=0.35, min_atr_pct=0.004,
              min_breakout_atr=0.3, min_spread_atr=0.5, stop_atr=1.25, target_r=2.5,
              max_hold_bars=96, fee_rate=0.0005, slippage=0.0002)


def _bar(index, close, high=None, low=None, open_=None):
    return MODULE.Bar(index * 3_600_000, close if open_ is None else open_,
                      close + 0.25 if high is None else high,
                      close - 0.25 if low is None else low, close, 1000.0)


def _open_position_series():
    """构造"均线密集 → 突破 → 首次回踩"的进场序列，返回 (bars, 跌破止损 bar 的下标)。

    700 根匀速上行保证多头排列与密集状态；随后是突破 K 线、回踩 K 线（收盘仍在
    EMA20 上方）、入场 K 线和一根开盘即跳空跌破止损的 K 线。
    """
    warmup = 700
    bars = [_bar(i, 100 + 0.005 * i) for i in range(warmup)]
    atr = MODULE.atr(bars)
    prior_high = max(bar.h for bar in bars[warmup - 4:warmup])
    bars.append(_bar(warmup, prior_high + 0.45 * atr[warmup - 1], open_=bars[warmup - 1].c, low=bars[warmup - 1].c - 0.01))
    e20 = MODULE.ema([bar.c for bar in bars], 20)
    atr = MODULE.atr(bars)
    touch = e20[warmup] + 0.35 * atr[warmup]
    bars.append(_bar(warmup + 1, e20[warmup] + 0.05, high=e20[warmup] + 0.10, low=touch - 0.02, open_=e20[warmup] + 0.06))
    bars.append(_bar(warmup + 2, bars[warmup + 1].c + 0.02, open_=bars[warmup + 1].c, low=bars[warmup + 1].c - 0.10, high=bars[warmup + 1].c + 0.05))
    atr = MODULE.atr(bars)
    stop = bars[warmup + 2].o - 1.25 * atr[warmup + 1]
    breach = warmup + 3
    bars.append(_bar(breach, stop - 0.40, open_=stop - 0.30, high=stop - 0.05, low=stop - 0.60))
    bars.append(_bar(breach + 1, stop - 0.45))
    return bars, breach


class ExitManagementTests(unittest.TestCase):
    def test_stop_is_managed_while_the_btc_gate_is_closed(self):
        """门控只关闭新信号：门控关闭期间已持仓仍必须按止损离场。

        修复前门控用 continue 短路在持仓管理之前，这根跳空跌破止损的 K 线被整根
        跳过，本用例会得到 0 笔成交。
        """
        bars, breach = _open_position_series()
        gate = {bar.ts: (GATE_OPEN if bar.ts <= bars[breach - 1].ts else GATE_SHUT) for bar in bars}
        trades = MODULE.simulate("TEST", bars, PARAMS, 0, len(bars), gate)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "stop")
        self.assertEqual(trades[0].exit_ts, bars[breach].ts)
        # 跳空穿越止损，成交价取更差的开盘价，亏损必须大于名义 1.25R。
        self.assertLess(trades[0].r, -1.25)

    def test_entry_uses_the_next_bar_open_with_slippage(self):
        """入场价 = 回踩确认 K 线的**下一根**开盘价，并向不利方向加单边滑点。"""
        bars, _ = _open_position_series()
        gate = {bar.ts: GATE_OPEN for bar in bars}
        trades = MODULE.simulate("TEST", bars, PARAMS, 0, len(bars), gate)
        self.assertEqual(len(trades), 1)
        # 回踩确认 K 线是 warmup+1，入场在 warmup+2 的开盘。
        expected = bars[702].o * (1 + PARAMS["slippage"])
        self.assertAlmostEqual(trades[0].entry, expected, places=10)

    def test_gate_without_enough_btc_history_blocks_new_entries(self):
        """BTC 暖机不足（state 为 None）时只关闭新开仓，不产生任何成交。"""
        bars, _ = _open_position_series()
        gate = {bar.ts: None for bar in bars}
        self.assertEqual(MODULE.simulate("TEST", bars, PARAMS, 0, len(bars), gate), [])

    def test_window_end_position_is_recorded_not_dropped(self):
        """窗口末端仍持仓必须按最后一根收盘价记为 window_end，不能静默丢弃。"""
        bars, breach = _open_position_series()
        gate = {bar.ts: GATE_OPEN for bar in bars}
        # 把跌破止损的那根换成既不止损也不止盈的横盘 K 线，让头寸跨到窗口末端。
        flat = bars[breach - 1].c
        bars[breach] = _bar(breach, flat, open_=flat, high=flat + 0.05, low=flat - 0.05)
        trades = MODULE.simulate("TEST", bars, PARAMS, 0, breach + 1, gate)
        self.assertEqual(len(trades), 1)
        self.assertEqual(trades[0].reason, "window_end")
        self.assertEqual(trades[0].exit_ts, bars[breach].ts)
        expected_exit = MODULE.exit_price(bars[breach].c, "buy", PARAMS["slippage"])
        self.assertAlmostEqual(trades[0].exit, expected_exit, places=10)
        expected_net = expected_exit - trades[0].entry - (trades[0].entry + expected_exit) * PARAMS["fee_rate"]
        self.assertAlmostEqual(trades[0].r, expected_net / trades[0].risk, places=10)

    def test_entry_must_fill_inside_the_window(self):
        """确认 K 线的下一根已越出窗口时不得开仓（训练段不能用测试段开盘价成交）。"""
        bars, breach = _open_position_series()
        gate = {bar.ts: GATE_OPEN for bar in bars}
        # 确认 K 线是 breach-2，成交价来自 breach-1；窗口在 breach-1 处结束，
        # 成交根已经在窗口外，因此不得开仓。
        self.assertEqual(MODULE.simulate("TEST", bars, PARAMS, 0, breach - 1, gate), [])
        # 窗口包含成交根时同一个信号正常开仓，证明上一条不是因为信号本身不存在。
        self.assertEqual(len(MODULE.simulate("TEST", bars, PARAMS, 0, breach, gate)), 1)

    def test_periods_and_risk_distance_come_from_the_config(self):
        """周期与风险参数必须取自 config，而不是代码里的默认值。"""
        bars, _ = _open_position_series()
        gate = {bar.ts: GATE_OPEN for bar in bars}
        # ema_trend 大于样本长度 → 暖机不足，没有成交。
        too_long = dict(PARAMS, ema_trend=len(bars) + 10)
        self.assertEqual(MODULE.simulate("TEST", bars, too_long, 0, len(bars), gate), [])
        # stop_atr 减半 → 止损距离（= stop_atr × ATR）同步减半。
        base = MODULE.simulate("TEST", bars, PARAMS, 0, len(bars), gate)
        tighter = MODULE.simulate("TEST", bars, dict(PARAMS, stop_atr=PARAMS["stop_atr"] * 0.5), 0, len(bars), gate)
        self.assertEqual(len(base), 1)
        self.assertEqual(len(tighter), 1)
        base_risk = base[0].entry - base[0].stop
        tighter_risk = tighter[0].entry - tighter[0].stop
        self.assertAlmostEqual(tighter_risk, base_risk * 0.5, places=10)


if __name__ == "__main__":
    unittest.main()
