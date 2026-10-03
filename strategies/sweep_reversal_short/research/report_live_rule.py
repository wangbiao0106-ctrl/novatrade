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
  python3 strategies/sweep_reversal_short/research/report_live_rule.py --pool live

`--pool live` 复现生产扫描范围（config/strategy.json 的 `runtime_universe`）：
只用通过运行时资产类别过滤的山寨币（`live_signal.eligible_runtime_alt`，排除
主流币、稳定币和非加密合约），在结构 bar 收盘时刻按滚动 24h 报价成交额排名，
只保留成交额 ≥ `min_quote_volume_24h_usdt` 且排名 ≤ `limit` 的标的，并额外报告
"同一实例只持有一个币种"的单仓口径。
"""
from __future__ import annotations

import argparse
import json
import os

import numpy as np
import pandas as pd

import engine as E
import live_signal as LS
import tune_execution as T

HOUR_MS = 3_600_000
ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
CONFIG = os.path.join(ROOT, "config")
RESULTS = os.path.join(ROOT, "results")
SPLIT_TS = int(pd.Timestamp("2026-07-29", tz="UTC").value // 1e6)

# 参数全部来自实验室机器真源 config/strategy.json（唯一真源）。
DETECT, FILTER, COSTS = E.lab_parameters()
RUNTIME_UNIVERSE = json.load(open(os.path.join(CONFIG, "strategy.json")))["runtime_universe"]
M15_MS = 900_000
BUF_ATR = COSTS["buf_atr"]
TP_R = COSTS["tp_mult"]
MAX_HOLD_15M = COSTS["max_hold_bars"] * 4   # 96 根 1h = 384 根 15m
FEE = COSTS["fee"]
MIN_ATR_PCT = COSTS["min_atr_pct"] / 100    # config 里是百分数
MAX_RISK_ATR = COSTS["max_risk_atr"]
MAX_STOP_PCT = COSTS["max_stop_pct"] / 100     # 止损距离占入场价上限（运行时 StrategyType.maxStopDistancePercent）


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
    counters = {"events": 0, "no_15m": 0, "atr_guard": 0, "risk_guard": 0, "stop_distance_guard": 0, "no_confirmation": 0}
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
            # 执行层：止损距离 > 15% 的信号不下单（名义 = 1 × 池，即单笔亏损上限）。
            if risk / entry > MAX_STOP_PCT:
                counters["stop_distance_guard"] += 1
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


def rolling_quote_volume_24h(data1: dict, symbols) -> pd.DataFrame:
    """按小时网格的滚动 24h 报价成交额（USDT），列为标的、索引为 1h K 线开盘时间。

    某一行求和的是该 1h K 线本身及之前 24 小时内的 K 线，因此读取它的最早时刻
    是这根 K 线的收盘，不使用未来数据（`live_universe_rank` 只在结构 bar 收盘
    之后取用）。它对齐的是"截至结构 bar 开盘"的 24 小时窗口，而运行时在扫描时
    刻读到的是 OKX ticker 的 `volCcy24h × last`，即"截至结构 bar 之后"的 24 小时
    窗口；两者相差最多一个 1h bar。这是研究口径对运行时口径的近似，不是前视。
    """
    columns = {}
    for sym in symbols:
        d = data1.get(sym)
        if d is None:
            continue
        index = pd.to_datetime(d["t"], unit="ms", utc=True)
        rolled = pd.Series(d["qv"], index=index).rolling("24h").sum().to_numpy()
        columns[sym] = pd.Series(rolled, index=np.asarray(d["t"], dtype=np.int64))
    return pd.DataFrame(columns).sort_index()


def live_universe_rank(rows: list[dict], volumes: pd.DataFrame) -> tuple[list[float], list[float]]:
    """每个结构在 bar 收盘时刻的成交额排名和 24h 报价成交额（因果）。"""
    ranks: list[float] = []
    quote: list[float] = []
    for row in rows:
        ts = row["signal_ts"]
        if ts not in volumes.index:
            ranks.append(np.nan)
            quote.append(np.nan)
            continue
        snapshot = volumes.loc[ts].dropna()
        mine = snapshot.get(row["symbol"], np.nan)
        if np.isnan(mine):
            ranks.append(np.nan)
            quote.append(np.nan)
            continue
        ranks.append(float((snapshot > mine).sum() + 1))
        quote.append(float(mine))
    return ranks, quote


def single_slot(df: pd.DataFrame) -> pd.DataFrame:
    """同一实例最多持有一个币种：前一笔未平仓时，后续信号被并发限制拒绝。"""
    accepted = []
    busy_until = -1
    for _, row in df.sort_values(["entry_ts", "symbol"]).iterrows():
        if row["entry_ts"] < busy_until:
            continue
        accepted.append(row)
        busy_until = row["entry_ts"] + (row["hold_bars_15m"] + 1) * M15_MS
    return pd.DataFrame(accepted, columns=df.columns)


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
    parser.add_argument("--pool", default="lowmid", choices=["lowmid", "all", "live"],
                        help="live = 生产扫描范围（runtime_universe 的成交额排名上限与下限）")
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

    live = args.pool == "live"
    live_symbols = None
    if live:
        live_symbols = sorted(sym for sym in json.load(open(os.path.join(CONFIG, "universe.json")))["altcoins"]
                              if LS.eligible_runtime_alt(sym))
    rows, counters = collect(args.pool, data1, data15, btc, args.fee, symbols=live_symbols)
    df = pd.DataFrame(rows)
    if df.empty:
        raise SystemExit("没有可复现的成交")
    if live:
        limit = int(RUNTIME_UNIVERSE["limit"])
        floor = float(RUNTIME_UNIVERSE["min_quote_volume_24h_usdt"])
        df["rank_at_signal"], df["quote_volume_24h"] = live_universe_rank(rows, rolling_quote_volume_24h(data1, live_symbols))
        in_universe = (df.rank_at_signal <= limit) & (df.quote_volume_24h >= floor)
        counters["outside_live_universe"] = int((~in_universe).sum())
        df = df[in_universe].copy()
        if df.empty:
            raise SystemExit("生产扫描范围内没有可复现的成交")
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
        "leverage": COSTS["leverage"],
        "costs": {"fee_per_side": args.fee, "slippage": 0.0},
        "guards": {"min_atr_pct": MIN_ATR_PCT, "max_risk_atr": MAX_RISK_ATR, "max_stop_distance_pct": MAX_STOP_PCT * 100, "btc_gate": True,
                   "confirmation_window_minutes": 60, "max_hold_bars_15m": MAX_HOLD_15M},
        "structures": counters,
        "full": stats(df),
        "train": stats(df[df.split == "train"]),
        "test": stats(df[df.split == "test"]),
        "monthly": {month: stats(group) for month, group in df.groupby("month")},
    }
    if live:
        slot = single_slot(df)
        summary["universe_rule"] = {
            # The ranking window ends at the structure bar's open (the last 1h
            # bar it can include is the closed structure bar itself), while the
            # runtime reads OKX's trailing 24h ticker volume at scan time, at
            # most one 1h bar later. The choice is causal either way.
            "ranking_window": "24h ending at the structure bar open (runtime uses the trailing 24h at scan time)",
            "ranking": "rolling 24h quote volume (USDT) at structure bar close among runtime-eligible altcoins",
            "eligible_symbols": len(live_symbols),
            "limit": limit, "min_quote_volume_24h_usdt": floor, "causal": True,
        }
        summary["single_slot"] = {
            "rule": "max_concurrent_positions = 1；前一笔未平仓时后续信号被拒绝",
            "rejected": int(len(df) - len(slot)),
            "full": stats(slot),
            "train": stats(slot[slot.split == "train"]),
            "test": stats(slot[slot.split == "test"]),
        }
    with open(os.path.join(args.out, f"live_rule_report{suffix}.json"), "w") as handle:
        json.dump(summary, handle, ensure_ascii=False, indent=2)

    print(f"结构 {counters['events']} 个 → 成交 {summary['full']['trades']} 笔"
          f"（无确认 {counters['no_confirmation']}，薄盘过滤 {counters['atr_guard']}，"
          f"极端止损过滤 {counters['risk_guard']}"
          + (f"，不在生产扫描范围 {counters['outside_live_universe']}" if live else "") + "）")
    for label in ("full", "train", "test"):
        part = summary[label]
        if not part.get("trades"):
            continue
        print(f"  {label:5s} 笔数={part['trades']:3d} 胜率={part['win_rate']:.1%} "
              f"盈亏比={part['ratio_R']:.2f} 期望={part['expect_R']:+.2f}R "
              f"回撤={part['max_drawdown_R']:.2f}R 连亏={part['max_consecutive_loss']} "
              f"TP/SL/时间={part['tp_pct']:.0%}/{part['sl_pct']:.0%}/{part['time_pct']:.0%}")
    print("月度:", " ".join(f"{m}:{s['trades']}笔/{s['expect_R']:+.2f}R" for m, s in sorted(summary["monthly"].items())))
    if live:
        slot = summary["single_slot"]
        print(f"单仓口径：拒绝 {slot['rejected']} 笔")
        for label in ("full", "train", "test"):
            part = slot[label]
            if not part.get("trades"):
                continue
            print(f"  {label:5s} 笔数={part['trades']:3d} 胜率={part['win_rate']:.1%} "
                  f"期望={part['expect_R']:+.2f}R 总计={part['expect_R'] * part['trades']:+.2f}R "
                  f"回撤={part['max_drawdown_R']:.2f}R")
    print("已写入:", os.path.join(args.out, f"live_rule_report{suffix}.json"))


if __name__ == "__main__":
    main()
