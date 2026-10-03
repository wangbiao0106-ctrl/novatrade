#!/usr/bin/env python3
"""`grid_0580` 固定参数在全部可用历史上的复核（当前 `data/kline/okx/swap/5m` 全量，约 18 个月）。

复用 `strategies/extreme_wick_short/src/sample_expansion.py` 的观察池、信号匹配和撮合逻辑，
对照四种执行口径，并把正式规则（信号收盘市价成交 + 24h 报价成交额 ≥ 1000 万 USDT +
止损距离 ≤ 15%）单列：

* `next_open`：原研究口径，信号后下一根 15m 开盘成交；
* `signal_close`：正式规则口径，信号 K 线收盘价成交（含单边 2bp 滑点）；
* 流动性门槛：无 / `quote_volume_24h_min_usdt`；
* `formal_rule`：signal_close + 流动性门槛 + 止损距离 ≤ `max_stop_distance_pct`。

结果按「原研究窗口之前（新样本外）/ 原研究窗口 / 之后」分段。资金池路径按全池仓位契约
折算（名义 = 1 × 资金池，单笔收益 = 名义收益率），同时保留原研究的 1% 风险口径作对照。

用法：
  python3 strategies/double_pump_exhaustion_short/research/report_full_history.py
输出：`results/full_history/report.json` 与各口径逐笔 CSV。
"""
from __future__ import annotations

import copy
import json
import statistics
import sys
import time
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
LAB = HERE.parent
ROOT = LAB.parents[1]
sys.path.insert(0, str(ROOT / "strategies/extreme_wick_short/src"))
sys.path.insert(0, str(ROOT / "strategies/sweep_reversal_short/research"))
import sample_expansion as SE  # noqa: E402
import momentum_exhaustion as base  # noqa: E402
from extreme_wick_short import aggregate_15m, choose_paths, excluded_symbols, load_bars  # noqa: E402
import live_signal as LS  # noqa: E402

OUT = LAB / "results" / "full_history"
MS = SE.MS
STRATEGY_CONFIG = json.loads((LAB / "config" / "strategy.json").read_text())
GRID_CONFIG = ROOT / "strategies/extreme_wick_short/results/return_maximization/grid_0580_config.json"
# 原研究窗口（grid_0580 的训练 + 回顾验证区间）。
WINDOW_START = 1_774_915_200_000   # 2026-03-31T00:00Z
WINDOW_END = 1_790_812_800_000     # 2026-10-01T00:00Z（不含）


def simulate_signal_close(event, bars, config, params):
    """正式规则：信号 K 线收盘后市价成交，持仓从下一根开始；其余与 SE.simulate 相同。"""
    risk_config, costs = config["risk"], config["costs"]
    start = event.index + 1
    if start >= len(bars) or bars[start].ts != bars[event.index].ts + MS:
        return None, "entry_gap"
    entry = bars[event.index].close * (1 - costs["slippage_one_way"])
    stop = event.anchor_high + params["stop_atr"] * event.atr
    distance = stop - entry
    if not risk_config["min_risk_atr"] <= distance / event.atr <= risk_config["max_risk_atr"]:
        return None, "risk_distance"
    target = entry - params["target_r"] * distance
    if target <= 0:
        return None, "invalid_target"
    last = min(len(bars) - 1, start + risk_config["max_hold_bars"] - 1)
    reason = "data_end" if last == len(bars) - 1 else "timeout"
    exit_price, exit_index, exit_ts = bars[last].close, last, bars[last].ts + MS
    worst = entry
    for j in range(start, last + 1):
        bar = bars[j]
        if j > start and bar.ts != bars[j - 1].ts + MS:
            exit_price, exit_index, exit_ts, reason = bars[j - 1].close, j - 1, bars[j - 1].ts + MS, "data_gap"
            break
        if bar.open >= stop:
            worst = max(worst, bar.open)
            exit_price, exit_index, exit_ts, reason = bar.open, j, bar.ts, "stop_gap"
            break
        worst = max(worst, min(bar.high, stop))
        if bar.high >= stop:
            exit_price, exit_index, exit_ts, reason = stop, j, bar.ts + MS, "stop"
            break
        if bar.low <= target:
            exit_price, exit_index, exit_ts, reason = target, j, bar.ts + MS, "target"
            break
    exit_price *= 1 + costs["slippage_one_way"]
    net = entry - exit_price - costs["fee_rate_one_way"] * (entry + exit_price)
    adverse_exit = worst * (1 + costs["slippage_one_way"])
    adverse = (entry - adverse_exit - costs["fee_rate_one_way"] * (entry + adverse_exit)) / entry
    return SE.Outcome(event.symbol, event.index, event.timestamp + MS, bars[start].ts, exit_ts, exit_index,
                      entry, exit_price, stop, target, net / distance, net / entry,
                      net / entry * config["position_management"]["leverage"] * 100, adverse, reason), None


def period(ts: int) -> str:
    if ts < WINDOW_START:
        return "A_fresh_before_window"
    return "B_original_window" if ts < WINDOW_END else "C_after_window"


def summarize(trades) -> dict:
    if not trades:
        return {"trades": 0}
    result = SE.metrics(trades)
    result["exit_reasons"] = dict(Counter(t.reason for t in trades))
    result["by_period"] = {name: SE.metrics([t for t in trades if period(t.signal_ts) == name])
                           for name in sorted({period(t.signal_ts) for t in trades})}
    result["by_month"] = {name: SE.metrics([t for t in trades if SE.iso(t.signal_ts)[:7] == name])
                          for name in sorted({SE.iso(t.signal_ts)[:7] for t in trades})}
    return result


def full_pool_path(trades) -> dict:
    """全池仓位契约：名义 = 1 × 池权益，单笔收益 = 名义收益率，已实现盈亏滚仓。"""
    ordered = sorted(trades, key=lambda t: (t.entry_ts, t.symbol))
    equity = peak = 1.0
    drawdown = 0.0
    for t in ordered:
        equity *= 1 + t.notional_return
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    losses = [-t.notional_return for t in ordered if t.notional_return < 0]
    return {"trades": len(ordered), "final_multiple": equity, "max_drawdown_pct": drawdown * 100,
            "worst_trade_pct": max(losses) * 100 if losses else 0.0,
            "median_loss_pct": statistics.median(losses) * 100 if losses else 0.0}


def main() -> None:
    started = time.monotonic()
    OUT.mkdir(parents=True, exist_ok=True)
    config = json.loads(GRID_CONFIG.read_text())
    params = dict(config["signal_parameters"], stop_atr=config["risk"]["stop_atr"], target_r=config["risk"]["target_r"])
    liquidity_floor = float(STRATEGY_CONFIG["universe"]["quote_volume_24h_min_usdt"])
    max_stop_pct = float(STRATEGY_CONFIG["position_management"]["max_stop_distance_pct"]) / 100
    excluded = excluded_symbols(base.DEFAULT_CONFIG, config)
    series = {}
    for symbol, path in choose_paths(ROOT / "data/kline/okx/swap/5m", excluded, None):
        if not LS.eligible_runtime_alt(symbol):
            continue
        bars = aggregate_15m(load_bars(path))
        if len(bars) > 96:
            series[symbol] = bars
    print(f"loaded {len(series)} symbols in {time.monotonic() - started:.0f}s", flush=True)
    events = {symbol: SE.features(symbol, bars, config) for symbol, bars in series.items()}
    observations = sum(len(items) for items in events.values())
    print("observations >100%:", observations, flush=True)

    def quote_volume_24h(bars, index):
        return sum(bar.quote_volume for bar in bars[index - 95:index + 1])

    report = {
        "parameters": params,
        "data": {"symbols": len(series), "observations": observations,
                 "start_utc": SE.iso(min(b[0].ts for b in series.values())),
                 "end_utc": SE.iso(max(b[-1].ts + MS for b in series.values()))},
        "original_window": {"start_utc": SE.iso(WINDOW_START), "end_utc_exclusive": SE.iso(WINDOW_END)},
        "formal_rule": {"entry": "signal_close", "quote_volume_24h_min_usdt": liquidity_floor,
                        "max_stop_distance_pct": max_stop_pct * 100},
        "variants": {},
    }
    variants = [("next_open|no_liquidity_floor", "next_open", 0.0, None),
                ("next_open|liquidity_floor", "next_open", liquidity_floor, None),
                ("signal_close|no_liquidity_floor", "signal_close", 0.0, None),
                ("signal_close|liquidity_floor", "signal_close", liquidity_floor, None),
                ("formal_rule", "signal_close", liquidity_floor, max_stop_pct)]
    for name, entry_mode, floor, stop_cap in variants:
        raw, rejected = [], Counter()
        signals = 0
        for symbol, items in events.items():
            bars = series[symbol]
            for event in items:
                if not SE.matches(event, bars, params):
                    continue
                signals += 1
                if floor and quote_volume_24h(bars, event.index) < floor:
                    rejected["liquidity"] += 1
                    continue
                simulate = SE.simulate if entry_mode == "next_open" else simulate_signal_close
                trade, reason = simulate(event, bars, config, params)
                if trade is None:
                    rejected[reason] += 1
                    continue
                if stop_cap is not None and (trade.stop - trade.entry) / trade.entry > stop_cap:
                    rejected["stop_distance"] += 1
                    continue
                raw.append(trade)
        single = SE.per_symbol_filter(raw, config["risk"]["cooldown_bars"])
        research_config = copy.deepcopy(config)
        research_config["position_management"]["risk_per_trade_pct"] = 1.0
        admitted, rows, account = SE.portfolio(raw, research_config)
        base.write_csv(OUT / f"{name.replace('|', '_')}_portfolio_trades.csv", rows)
        report["variants"][name] = {
            "signals": signals, "rejected": dict(rejected),
            "per_symbol_single": summarize(single),
            "portfolio": summarize(admitted),
            "account_1pct_risk_research_convention": account,
            "full_pool_path": {"full": full_pool_path(admitted),
                               "by_period": {label: full_pool_path([t for t in admitted if period(t.signal_ts) == label])
                                             for label in sorted({period(t.signal_ts) for t in admitted})}},
        }
        portfolio = report["variants"][name]["portfolio"]
        print(f"\n== {name} == signals={signals} rejected={dict(rejected)}", flush=True)
        print(f"  portfolio n={portfolio['trades']} win={(portfolio.get('win_rate') or 0):.1%} "
              f"total={portfolio.get('total_r', 0):+.2f}R PF={portfolio.get('profit_factor')} "
              f"dd={portfolio.get('max_drawdown_r', 0):.2f}R exits={portfolio.get('exit_reasons')}")
        for label, metrics in portfolio.get("by_period", {}).items():
            print(f"    {label:24s} n={metrics['trades']:3d} win={(metrics['win_rate'] or 0):.1%} "
                  f"total={metrics['total_r']:+.2f}R PF={metrics['profit_factor']} dd={metrics['max_drawdown_r']:.2f}R")
        path = report["variants"][name]["full_pool_path"]
        print(f"  全池仓位: 终值 {path['full']['final_multiple']:.2f}x 回撤 {path['full']['max_drawdown_pct']:.1f}% | "
              + " | ".join(f"{k}: {v['final_multiple']:.2f}x/{v['max_drawdown_pct']:.1f}%" for k, v in path["by_period"].items()))
    report["limitations"] = [
        "只含当前仍上市的合约（幸存者偏差）",
        "原研究窗口（2026-03-31 至 2026-09-30）的历史已被 864 组网格查看过，只有窗口之前的区间是新样本外",
        "资金费率未计；滑点按单边 2bp、手续费单边 6bp",
    ]
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
    print("已写入:", OUT / "report.json")


if __name__ == "__main__":
    main()
