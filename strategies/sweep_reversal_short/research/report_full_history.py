#!/usr/bin/env python3
"""已上线规则在全部可用历史上的复核（当前 `data/kline/okx/swap/5m` 全量，约 18 个月）。

与 `report_live_rule.py` 共用同一个 `collect()`（1h 结构 + 15m 收盘确认 + 市价做空 +
薄盘/5×ATR/15% 止损距离过滤 + fail-closed BTC 门控），区别只有三点：

1. 数据来自独立预处理目录 `results/full_history/data`（由 `prep.py` 从全量 5m 折叠，
   不覆盖 §5 历史基线使用的 `results/data`）；
2. 生产选币范围取数据里全部通过运行时资产类别过滤的山寨币（含 v1.4 定稿后上市的
   合约），而不是 `config/universe.json` 的 295 币快照；
3. 结果按「v1.4 研究窗口之前（新样本外）/ 研究窗口 / 之后」分段，并把单仓序列按
   全池仓位契约（名义 = 1 × 资金池，单笔亏损 = 止损距离）折算成资金池路径。

用法：
  python3 strategies/sweep_reversal_short/research/report_full_history.py              # 首次自动预处理（数分钟）
  python3 strategies/sweep_reversal_short/research/report_full_history.py --fee 0.001
  python3 strategies/sweep_reversal_short/research/report_full_history.py --refresh-data

输出：`results/full_history/report.json` 与逐笔 CSV（目录被 .gitignore 忽略，按需重建）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import engine as E  # noqa: E402

ROOT = os.path.join(HERE, "..")
OUT = os.path.join(ROOT, "results", "full_history")
DATA = os.path.join(OUT, "data")
M15_MS = 900_000


def ensure_data(refresh: bool) -> None:
    if refresh or not os.path.exists(os.path.join(DATA, "meta.csv")):
        import prep
        prep.main(["--output-dir", DATA])


def periods_from_config() -> tuple[int, int]:
    """v1.4 研究窗口（config.backtest.period，形如 '2026-03-31 ~ 2026-09-27'）。"""
    payload = json.load(open(os.path.join(ROOT, "config", "strategy.json")))
    start_text, end_text = [part.strip() for part in payload["backtest"]["period"].split("~")]
    start = int(pd.Timestamp(start_text, tz="UTC").value // 1e6)
    end = int((pd.Timestamp(end_text, tz="UTC") + pd.Timedelta(days=1)).value // 1e6)
    return start, end


def label_periods(ts: np.ndarray, start: int, end: int) -> np.ndarray:
    return np.where(ts < start, "A_fresh_before_window",
                    np.where(ts < end, "B_v1_4_window", "C_after_window"))


def pool_path(df: pd.DataFrame) -> dict:
    """全池仓位契约下的资金池路径：名义 = 1 × 池，单笔收益 = pnl_R × 止损距离。"""
    if df.empty:
        return {"trades": 0}
    ret = (df.pnl_R * df.risk / df.entry).to_numpy()
    equity = peak = 1.0
    drawdown = 0.0
    for value in ret:
        equity *= 1 + value
        peak = max(peak, equity)
        drawdown = max(drawdown, 1 - equity / peak)
    losses = ret[ret < 0]
    return {
        "trades": int(len(ret)),
        "final_multiple": float(equity),
        "max_drawdown_pct": float(drawdown * 100),
        "worst_trade_pct": float(-ret.min() * 100) if len(ret) else 0.0,
        "median_loss_pct": float(-np.median(losses) * 100) if len(losses) else 0.0,
        "median_stop_distance_pct": float((df.risk / df.entry).median() * 100),
    }


def bootstrap(values: np.ndarray, draws: int = 20_000, seed: int = 0) -> dict:
    if len(values) == 0:
        return {}
    rng = np.random.default_rng(seed)
    means = np.array([rng.choice(values, len(values)).mean() for _ in range(draws)])
    return {"mean_R": float(values.mean()), "ci95_low": float(np.percentile(means, 2.5)),
            "ci95_high": float(np.percentile(means, 97.5)), "p_mean_le_0": float((means <= 0).mean())}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fee", type=float, default=None, help="单边手续费率，默认取 config 的 0.0005")
    parser.add_argument("--refresh-data", action="store_true", help="重新从全量 5m 预处理")
    args = parser.parse_args()
    os.makedirs(OUT, exist_ok=True)
    ensure_data(args.refresh_data)
    E.OUT_DIR = DATA
    import live_signal as LS
    import report_live_rule as R
    fee = R.FEE if args.fee is None else args.fee
    runtime = R.RUNTIME_UNIVERSE
    limit, floor = int(runtime["limit"]), float(runtime["min_quote_volume_24h_usdt"])

    data1 = E.load_tf("1h")
    data15 = E.load_tf("15m")
    bd = data1["BTC"]
    btc = (bd["t"], bd["c"], E.sma(bd["c"], 200))
    symbols = sorted(sym for sym in data1 if sym != "BTC" and LS.eligible_runtime_alt(sym))
    window_start, window_end = periods_from_config()

    rows, counters = R.collect("all", data1, data15, btc, fee, symbols=symbols)
    if not rows:
        raise SystemExit("没有可复现的成交")
    df = pd.DataFrame(rows)
    df["rank_at_signal"], df["quote_volume_24h"] = R.live_universe_rank(rows, R.rolling_quote_volume_24h(data1, symbols))
    df["period"] = label_periods(df.signal_ts.to_numpy(), window_start, window_end)
    df["month"] = pd.to_datetime(df.entry_ts, unit="ms", utc=True).dt.strftime("%Y-%m")
    df = df.sort_values("entry_ts")
    live = df[(df.rank_at_signal <= limit) & (df.quote_volume_24h >= floor)].copy()
    slot = R.single_slot(live)
    suffix = f"_fee{fee * 10_000:.0f}bps"
    df.to_csv(os.path.join(OUT, f"all_eligible{suffix}.csv"), index=False)
    live.to_csv(os.path.join(OUT, f"live_pool{suffix}.csv"), index=False)
    slot.to_csv(os.path.join(OUT, f"live_single_slot{suffix}.csv"), index=False)

    def section(frame: pd.DataFrame, monthly: bool = False) -> dict:
        part = {"full": R.stats(frame),
                "by_period": {name: R.stats(group) for name, group in frame.groupby("period")}}
        if monthly:
            part["monthly"] = {name: R.stats(group) for name, group in frame.groupby("month")}
        return part

    fresh = live[live.period == "A_fresh_before_window"].pnl_R.to_numpy()
    report = {
        "rule": "1h structure + first 15m bearish close confirmation + market short (stop distance <= 15%)",
        "data": {"start": int(min(d["t"][0] for d in data1.values())),
                 "end": int(max(d["t"][-1] for d in data1.values())),
                 "symbols_1h": len(data1), "runtime_eligible_alts": len(symbols)},
        "window_v1_4": {"start": window_start, "end_exclusive": window_end},
        "costs": {"fee_per_side": fee, "slippage": 0.0},
        "guards": {"min_atr_pct": R.MIN_ATR_PCT, "max_risk_atr": R.MAX_RISK_ATR,
                   "max_stop_distance_pct": R.MAX_STOP_PCT * 100, "btc_gate": True},
        "universe_rule": {"ranking": "rolling 24h quote volume at structure bar close among all runtime-eligible alts in the data",
                          "limit": limit, "min_quote_volume_24h_usdt": floor,
                          "note": "与 report_live_rule.py --pool live 的差别：这里包含 universe.json 快照之外、后来上市的合规山寨币"},
        "structures": counters,
        "all_eligible": section(df),
        "live_pool": section(live, monthly=True),
        "live_pool_fresh_bootstrap": bootstrap(fresh),
        "live_single_slot": section(slot, monthly=True),
        "live_single_slot_pool_path": {"full": pool_path(slot),
                                       "by_period": {name: pool_path(group) for name, group in slot.groupby("period")},
                                       "contract": "notional = 1 × pool available capital; per-trade loss = stop distance; realized PnL compounds"},
        "limitations": [
            "只含当前仍上市的合约（幸存者偏差）；后期上市合约历史较短",
            "未计资金费率与滑点；手续费按 fee_per_side",
            "生产口径用 1h 报价成交额求和近似 ticker volCcy24h × last",
        ],
    }
    with open(os.path.join(OUT, f"report{suffix}.json"), "w") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2, default=float)

    for name in ("all_eligible", "live_pool", "live_single_slot"):
        part = report[name]
        full = part["full"]
        print(f"{name:17s} n={full['trades']:3d} 胜率={full['win_rate']:.1%} 期望={full['expect_R']:+.3f}R "
              f"合计={full['expect_R'] * full['trades']:+.2f}R 回撤={full['max_drawdown_R']:.2f}R")
        for period, stats in sorted(part["by_period"].items()):
            print(f"    {period:24s} n={stats['trades']:3d} 胜率={stats['win_rate']:.1%} 期望={stats['expect_R']:+.3f}R "
                  f"合计={stats['expect_R'] * stats['trades']:+.2f}R 回撤={stats['max_drawdown_R']:.2f}R")
    path = report["live_single_slot_pool_path"]["full"]
    print(f"全池仓位折算（单仓）: 终值 {path['final_multiple']:.2f}x 最大回撤 {path['max_drawdown_pct']:.1f}% "
          f"单笔最大亏损 {path['worst_trade_pct']:.1f}% 亏损中位 {path['median_loss_pct']:.1f}%")
    print("新样本外 bootstrap:", report["live_pool_fresh_bootstrap"])
    print("已写入:", os.path.join(OUT, f"report{suffix}.json"))


if __name__ == "__main__":
    main()
