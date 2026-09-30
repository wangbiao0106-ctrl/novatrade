#!/usr/bin/env python3
"""复现"已上线规则"的绩效证据（1h 结构 + 15m 收盘确认 + 市价做空）。

这是 STRATEGY.md §5 / config/strategy.json 的 backtest 块所引用的证据来源，
口径必须与运行时 Sources/TradingService/StrategyEngine.swift 的
`evaluateWithConfirmation` 完全一致：

1. 1h 结构：pivot → 首次扫顶 s → 回落确认 k1 → 二次扫顶 j（j 必须是最后一根已
   确认 1h bar）；
2. 过滤：BTC 门控（按结构时刻对齐）、`ATR14[j] / 入场价 >= minATRPct`、
   `(止损 − 入场价) / ATR14[j] <= maxRiskATR`；
3. 确认窗口：半开区间 `[结构 bar 收盘, 结构 bar 收盘 + 60 分钟)`，即时间戳为
   +60/+75/+90/+105 分钟的 15m K 线（K 线时间戳是开盘时间）；
4. 入场：窗口内第一根 `close <= 1h 二次扫顶收盘价` 且 `close < open` 的已确认
   15m 阴线，入场价取该 K 线收盘价；
5. 出场：`止损 = max(H[s..j]) + 0.5 × ATR14[j]`，`止盈 = 入场价 − 2.2 × R`，
   入场后 384 根 15m 内按保守 OHLC 规则撮合（跳空按开盘价、同 bar 双触按止损）。

用法：
  python3 strategies/sweep_reversal_short/research/report_live_rule.py
  python3 strategies/sweep_reversal_short/research/report_live_rule.py --pool all
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

import engine as E
import tune_execution as T

HOUR_MS = 3_600_000
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CONFIG = os.path.join(ROOT, "config")
RESULTS = os.path.join(ROOT, "results")
SPLIT_TS = int(pd.Timestamp("2026-07-29", tz="UTC").value // 1e6)

# 参数全部来自实验室机器真源 config/strategy.json（唯一真源）。
DETECT, FILTER, COSTS = E.lab_parameters()
BUF_ATR = COSTS["buf_atr"]
TP_R = COSTS["tp_mult"]
MAX_HOLD_15M = COSTS["max_hold_bars"] * 4   # 96 根 1h = 384 根 15m
FEE = COSTS["fee"]
MIN_ATR_PCT = COSTS["min_atr_pct"] / 100    # config 里是百分数
MAX_RISK_ATR = COSTS["max_risk_atr"]


def btc_flags(ev, btc):
    """BTC 门控标记；唯一实现见 engine.btc_gate_flags（fail-closed）。

    `btc_flag = 1` 表示"不通过门控"。BTC 在信号时刻之前不足 200 根 1h K 线、索引
    越界、或收盘/SMA200 不可用时，一律置 1（不发新信号）。此前用
    `(close > sma).astype(int8)` 且 `sma` 走 `min_periods=1`，会把数据开头 8 天
    （BTC 历史不足 200 根）的事件误判为"门控开"，虚增了 6 笔成交。
    """
    E.btc_gate_flags(ev, btc)


def collect(pool: str, data1: dict, data15: dict, btc, fee: float = FEE,
            symbols=None) -> tuple[list[dict], dict]:
    if symbols is not None:
        pool_set = set(symbols)
    elif pool == "all":
        pool_set = set(json.load(open(os.path.join(CONFIG, "universe.json")))["altcoins"])
    else:
        pool_set = set(json.load(open(os.path.join(CONFIG, "universe_recommended.json")))["recommended_low_mid"])
    rows: list[dict] = []
    counters = {"events": 0, "no_15m": 0, "atr_guard": 0, "risk_guard": 0, "no_confirmation": 0}
    for sym in sorted(pool_set):
        d1 = data1.get(sym)
        d15 = data15.get(sym)
        if d1 is None:
            continue
        pre = E.precompute(d1, sma_lens=(200,), major_wins=(288,), mom_wins=(96,), eqh_wins=(96,))
        ev = E.detect_events(d1, pre, E.next_higher_high(d1["h"]), dict(DETECT))
        if ev is None:
            continue
        btc_flags(ev, btc)
        for i in np.flatnonzero(E.apply_filters(ev, FILTER)):
            counters["events"] += 1
            if d15 is None:
                counters["no_15m"] += 1
                continue
            k = int(ev["k"][i])
            structure_ts = int(d1["t"][k])
            structure_close = float(ev["entry"][i])
            atr_j = float(ev["atr_k"][i])
            # 半开区间 [结构收盘, 结构收盘 + 60 分钟)
            lo = structure_ts + HOUR_MS
            hi = lo + HOUR_MS
            t15, o15, c15 = d15["t"], d15["o"], d15["c"]
            p0 = int(np.searchsorted(t15, lo, side="left"))
            p1 = int(np.searchsorted(t15, hi, side="left"))
            entry_idx = -1
            for p in range(p0, p1):
                if c15[p] <= structure_close and c15[p] < o15[p]:
                    entry_idx = p
                    break
            if entry_idx < 0:
                counters["no_confirmation"] += 1
                continue
            entry = float(c15[entry_idx])
            if atr_j / entry < MIN_ATR_PCT:
                counters["atr_guard"] += 1
                continue
            stop = float(ev["ext"][i]) + BUF_ATR * atr_j
            risk = stop - entry
            if risk <= 0 or risk / atr_j > MAX_RISK_ATR:
                counters["risk_guard"] += 1
                continue
            event = {"ext": float(ev["ext"][i]), "atr_k": atr_j}
            trade = T.market_trade(sym, pool, "live_rule", d15, event, entry_idx, entry,
                                   structure_ts, MAX_HOLD_15M, 0.0, BUF_ATR, TP_R, fee)
            if trade is None:
                continue
            rows.append({
                "symbol": sym, "signal_ts": structure_ts, "entry_ts": int(t15[entry_idx]),
                "entry": entry, "sl": stop, "tp": entry - TP_R * risk, "risk": risk,
                "pnl": trade.pnl, "pnl_R": trade.pnl_R, "kind": trade.kind,
                "hold_bars_15m": trade.hold_bars, "atr": atr_j,
            })
    return rows, counters


def liquidity_tiers() -> dict[str, list[str]]:
    """按全期日均报价成交额把 295 个山寨币五等分，并给出 Top100 / 中低 177 对照。

    与 liquidity_tiers.py 的分层口径一致，但分层只用于**报告分组**，不参与选币，
    因此不引入前视。
    """
    meta = E.load_meta()
    uni = json.load(open(os.path.join(CONFIG, "universe.json")))
    rec = set(json.load(open(os.path.join(CONFIG, "universe_recommended.json")))["recommended_low_mid"])
    alts = [s for s in uni["altcoins"] if s in set(meta["sym"])]
    ordered = meta.set_index("sym").loc[alts].copy()
    days = ((ordered.t1 - ordered.t0) / 86_400_000).clip(lower=1)
    ordered = ordered.assign(qv_daily=ordered.tot_qv / days).sort_values("qv_daily", ascending=False)
    ranked = list(ordered.index)
    size = len(ranked) // 5
    tiers = {
        "Q1_lowest": ranked[4 * size:],
        "Q2": ranked[3 * size:4 * size],
        "Q3": ranked[2 * size:3 * size],
        "Q4": ranked[size:2 * size],
        "Q5_highest": ranked[:size],
        "top100_high": ranked[:100],
        "lowmid177": [s for s in ranked if s in rec],
    }
    return tiers


def daily_quote_volume(data1: dict) -> dict[str, pd.Series]:
    """每个标的按 UTC 日聚合的报价成交额（用于因果排名）。"""
    out: dict[str, pd.Series] = {}
    for sym, d in data1.items():
        series = pd.Series(d["qv"], index=pd.to_datetime(d["t"], unit="ms", utc=True))
        out[sym] = series.resample("1D").sum()
    return out


def rolling_rank(rows: list[dict], daily: dict[str, pd.Series], lookback_days: int = 30) -> list[int]:
    """每笔交易入场时点的**因果**全市场排名。

    只用入场时点之前的日线成交额（`index <= entry`，取最近 `lookback_days` 天的中位数），
    并且在包含未交易合约在内的全部标的里排名，因此不使用任何未来信息；排名只用于
    报告分组，不参与选币。
    """
    ranks: list[int] = []
    for row in rows:
        entry = pd.to_datetime(row["entry_ts"], unit="ms", utc=True)
        values = {}
        for sym, series in daily.items():
            past = series[series.index <= entry].tail(lookback_days)
            values[sym] = float(past.median()) if len(past) else 0.0
        mine = values.get(row["symbol"], 0.0)
        ranked = sorted(values.values(), reverse=True)
        ranks.append(int(np.searchsorted(-np.array(ranked), -mine)) + 1)
    return ranks


def stats(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"trades": 0}
    wins = df[df.pnl_R > 0]
    losses = df[df.pnl_R <= 0]
    ordered = df.sort_values("entry_ts")
    eq = ordered.pnl_R.cumsum().to_numpy()
    run = best = 0
    for value in ordered.pnl_R:
        run = run + 1 if value <= 0 else 0
        best = max(best, run)
    return {
        "trades": int(len(df)),
        "win_rate": float(len(wins) / len(df)),
        "avg_win_R": float(wins.pnl_R.mean()) if len(wins) else None,
        "avg_loss_R": float(-losses.pnl_R.mean()) if len(losses) else None,
        "ratio_R": float(wins.pnl_R.mean() / -losses.pnl_R.mean()) if len(losses) and losses.pnl_R.mean() else None,
        "expect_R": float(df.pnl_R.mean()),
        "tp_pct": float((df.kind == "tp").mean()),
        "sl_pct": float((df.kind == "sl").mean()),
        "time_pct": float((df.kind == "time").mean()),
        "max_drawdown_R": float(np.max(np.maximum.accumulate(eq) - eq)) if len(eq) else 0.0,
        "max_consecutive_loss": int(best),
        "median_hold_bars_15m": float(df.hold_bars_15m.median()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pool", default="lowmid", choices=["lowmid", "all"])
    parser.add_argument("--fee", type=float, default=FEE, help="单边手续费率，默认 0.0005")
    parser.add_argument("--by-tier", action="store_true",
                        help="按全期日均成交额五等分输出分层结果（只用于报告分组，不参与选币）")
    parser.add_argument("--rolling-rank", action="store_true",
                        help="按入场时点过去 30 天成交额的因果全市场排名分层（STRATEGY_SPEC §7.5）")
    parser.add_argument("--out", default=RESULTS)
    args = parser.parse_args()
    os.makedirs(args.out, exist_ok=True)

    data1 = E.load_tf("1h")
    data15 = E.load_tf("15m")
    bd = data1["BTC"]
    btc = (bd["t"], bd["c"], E.sma(bd["c"], 200))

    if args.by_tier:
        tiers = liquidity_tiers()
        summary = {"rule": "1h structure + first 15m bearish close confirmation + market short",
                   "universe": "liquidity_tiers(all 295)", "costs": {"fee_per_side": args.fee},
                   "tiers": {}}
        for name, symbols in tiers.items():
            rows, counters = collect("all", data1, data15, btc, args.fee, symbols=symbols)
            part = {"symbols": len(symbols), "structures": counters["events"]}
            part.update(stats(pd.DataFrame(rows)))
            summary["tiers"][name] = part
            print(f"{name:14s} 币数={len(symbols):3d} 笔数={part.get('trades',0):3d} "
                  f"胜率={part.get('win_rate') or 0:.1%} 期望={part.get('expect_R') or 0:+.2f}R")
        with open(os.path.join(args.out, "live_rule_report_tiers.json"), "w") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print("已写入:", os.path.join(args.out, "live_rule_report_tiers.json"))
        return

    if args.rolling_rank:
        rows, counters = collect("all", data1, data15, btc, args.fee)
        if not rows:
            raise SystemExit("没有可复现的成交")
        daily = daily_quote_volume(data1)
        ranked = pd.DataFrame(rows)
        ranked["rank_at_entry"] = rolling_rank(rows, daily)
        ranked["split"] = np.where(ranked.signal_ts < SPLIT_TS, "train", "test")
        ranked.sort_values("entry_ts").to_csv(
            os.path.join(args.out, "live_rule_trades_rolling_rank.csv"), index=False)
        buckets = {"top50": ranked.rank_at_entry <= 50, "top100": ranked.rank_at_entry <= 100,
                   "top200": ranked.rank_at_entry <= 200,
                   "rest_after_top100": ranked.rank_at_entry > 100}
        summary = {"rule": "1h structure + first 15m bearish close confirmation + market short",
                   "universe": "all alts, ranked causally at entry",
                   "costs": {"fee_per_side": args.fee, "slippage": 0.0},
                   "ranking": {"metric": "median daily quote volume",
                               "lookback_days": 30, "causal": True,
                               "note": "只用入场时点之前的日线成交额；排名只用于报告分组"},
                   "structures": counters, "buckets": {}}
        for name, mask in buckets.items():
            part = stats(ranked[mask])
            part["train"] = stats(ranked[mask & (ranked.split == "train")])
            part["test"] = stats(ranked[mask & (ranked.split == "test")])
            summary["buckets"][name] = part
            print(f"{name:18s} 笔数={part['trades']:3d} 胜率={part['win_rate']:.1%} "
                  f"盈亏比={part['ratio_R'] or 0:.2f} 期望={part['expect_R']:+.2f}R "
                  f"回撤={part['max_drawdown_R']:.2f}R | 训练 {part['train']['trades']} 笔 "
                  f"{part['train'].get('expect_R') or 0:+.2f}R / 测试 {part['test']['trades']} 笔 "
                  f"{part['test'].get('expect_R') or 0:+.2f}R")
        with open(os.path.join(args.out, "live_rule_report_rolling_rank.json"), "w") as handle:
            json.dump(summary, handle, ensure_ascii=False, indent=2)
        print("已写入:", os.path.join(args.out, "live_rule_report_rolling_rank.json"))
        return

    rows, counters = collect(args.pool, data1, data15, btc, args.fee)
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("没有可复现的成交")
    df["split"] = np.where(df.signal_ts < SPLIT_TS, "train", "test")
    df["month"] = pd.to_datetime(df.entry_ts, unit="ms", utc=True).dt.strftime("%Y-%m")
    df = df.sort_values("entry_ts")
    # 默认文件名 = 文档引用的基线（lowmid @ 0.05%/边）；其他口径加后缀，避免互相覆盖。
    suffix = "" if (args.pool == "lowmid" and args.fee == FEE) else f"_{args.pool}_fee{args.fee * 10_000:.0f}bps"
    df.to_csv(os.path.join(args.out, f"live_rule_trades{suffix}.csv"), index=False)

    summary = {
        "rule": "1h structure + first 15m bearish close confirmation + market short",
        "universe": args.pool,
        "window": {"start": int(df.entry_ts.min()), "end": int(df.entry_ts.max())},
        "costs": {"fee_per_side": args.fee, "slippage": 0.0},
        "guards": {"min_atr_pct": MIN_ATR_PCT, "max_risk_atr": MAX_RISK_ATR, "btc_gate": True,
                   "confirmation_window_minutes": 60, "max_hold_bars_15m": MAX_HOLD_15M},
        "structures": counters,
        "full": stats(df),
        "train": stats(df[df.split == "train"]),
        "test": stats(df[df.split == "test"]),
        "monthly": {month: stats(group) for month, group in df.groupby("month")},
    }
    with open(os.path.join(args.out, f"live_rule_report{suffix}.json"), "w") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    print(f"结构 {counters['events']} 个 → 成交 {summary['full']['trades']} 笔"
          f"（无确认 {counters['no_confirmation']}，薄盘过滤 {counters['atr_guard']}，"
          f"极端止损过滤 {counters['risk_guard']}）")
    for label in ("full", "train", "test"):
        part = summary[label]
        if not part.get("trades"):
            continue
        print(f"  {label:5s} 笔数={part['trades']:3d} 胜率={part['win_rate']:.1%} "
              f"盈亏比={part['ratio_R']:.2f} 期望={part['expect_R']:+.2f}R "
              f"回撤={part['max_drawdown_R']:.2f}R 连亏={part['max_consecutive_loss']} "
              f"TP/SL/时间={part['tp_pct']:.0%}/{part['sl_pct']:.0%}/{part['time_pct']:.0%}")
    print("月度:", " ".join(f"{m}:{s['trades']}笔/{s['expect_R']:+.2f}R" for m, s in sorted(summary["monthly"].items())))
    print("已写入:", os.path.join(args.out, f"live_rule_report{suffix}.json"))


if __name__ == "__main__":
    main()
