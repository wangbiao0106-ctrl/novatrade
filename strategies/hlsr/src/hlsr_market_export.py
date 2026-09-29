#!/usr/bin/env python3
"""Run HLSR against the supplied 5-minute market_export archive."""

from __future__ import annotations

import argparse
import csv
import gzip
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from high_short_strategy import (LAB_CONFIG, Params, acceptance_criteria, aggregate_data,
                                aggregate_result, backtest, fixed_parameters, load_lab_config,
                                parameter_grid, reward_risk_ratio)
from altcoin_backtest import Bar, Trade as BaseTrade, beta_interval, bootstrap_positive_probability, rolling_sum

UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data/kline/okx/swap/5m"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "strategies/hlsr/results"
# 选币所用的训练期长度（天）。必须不晚于第一折测试期起点，避免选择性前视。
SELECTION_DAYS = 60


def load_15m(path: Path) -> list[Bar]:
    groups: dict[int, Bar] = {}
    with gzip.open(path, "rt") as handle:
        for line in handle:
            if not line.strip():
                continue
            row = json.loads(line)
            if not row.get("confirmed", True):
                continue
            ts = int(row["timestamp_ms"])
            bucket = ts // 900_000 * 900_000
            bar = Bar(ts, float(row["open"]), float(row["high"]), float(row["low"]), float(row["close"]), float(row.get("volume", 0)), float(row.get("quote_volume", 0)))
            previous = groups.get(bucket)
            groups[bucket] = bar if previous is None else Bar(bucket, previous.open, max(previous.high, bar.high), min(previous.low, bar.low), bar.close, previous.volume + bar.volume, previous.quote_volume + bar.quote_volume)
    return sorted(groups.values(), key=lambda bar: bar.ts)


def symbol_from_path(path: Path) -> str:
    # 文件名形如 BEAT_USDT_SWAP_5m_<start>_<end>.jsonl.gz；合约 id 需要的是
    # BEAT-USDT-SWAP。直接拼 "-USDT-SWAP" 会得到 BEAT_USDT_SWAP-USDT-SWAP。
    stem = path.name.split("_5m_")[0]
    if stem.endswith("_USDT_SWAP"):
        stem = stem[: -len("_USDT_SWAP")]
    return stem.replace("_", "-") + "-USDT-SWAP"


def hard_filter_stats(bars: list[Bar], min_gain: float, min_quote_volume: float) -> dict[str, int]:
    """硬筛选命中统计；阈值来自 config/strategy.json 的 hard_filters。"""
    closes = [bar.close for bar in bars]
    quotes = [bar.quote_volume for bar in bars]
    gain_hits = volume_hits = composite_hits = 0
    for index in range(96, len(bars)):
        gain_ok = closes[index] > closes[index - 96] * (1 + min_gain)
        volume_ok = rolling_sum(quotes, index, 96) > min_quote_volume
        gain_hits += int(gain_ok)
        volume_hits += int(volume_ok)
        composite_hits += int(gain_ok and volume_ok)
    return {"bars": len(bars), "gain_hits": gain_hits, "volume_hits": volume_hits, "composite_hits": composite_hits}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-export", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-symbols", type=int, default=50)
    parser.add_argument("--min-events", type=int, default=3)
    parser.add_argument("--config", type=Path, default=LAB_CONFIG, help="实验室机器参数真源")
    parser.add_argument("--fee-rate", type=float, default=None, help="默认取 config 的 costs.fee_rate_one_way")
    parser.add_argument("--slippage", type=float, default=None, help="默认取 config 的 costs.slippage")
    parser.add_argument("--funding-rate", type=float, default=None, help="默认取 config 的 costs.funding_rate")
    args = parser.parse_args()
    lab = load_lab_config(args.config)
    lab_fixed = fixed_parameters(lab)
    costs = lab["costs"]
    if args.fee_rate is None: args.fee_rate = float(costs.get("fee_rate_one_way", 0.0006))
    if args.slippage is None: args.slippage = float(costs.get("slippage", 0.0002))
    if args.funding_rate is None: args.funding_rate = float(costs.get("funding_rate", 0.0))
    root = args.market_export
    paths = sorted(root.glob("*_USDT_SWAP_5m_*.jsonl.gz"))
    loaded: dict[str, list[Bar]] = {}
    stats: dict[str, dict[str, int]] = {}
    for path in paths:
        symbol = symbol_from_path(path)
        loaded[symbol] = load_15m(path)
    if not loaded:
        raise SystemExit("market_export 中没有找到任何 K 线导出文件")
    # 标的池必须只用第一折训练期（数据起点起 60 天）的数据挑选。用整段 180 天
    # 的事件数选币会把测试期信息带进样本外指标（选择性前视）。
    data_start = min(bar.ts for bars in loaded.values() for bar in bars[:1])
    selection_end = data_start + int(SELECTION_DAYS * 86_400_000)
    for symbol, bars in loaded.items():
        stats[symbol] = hard_filter_stats([bar for bar in bars if bar.ts < selection_end],
                                          lab_fixed["min_gain_24h"], lab_fixed["min_quote_volume_24h"])
    eligible = sorted((value["composite_hits"], symbol) for symbol, value in stats.items() if value["composite_hits"] >= args.min_events)
    selected = [symbol for _, symbol in eligible[-args.max_symbols:]]
    selected.sort()
    if not selected:
        raise SystemExit("market_export中没有满足硬筛选事件数的合约")
    print(f"selection_window={datetime.fromtimestamp(data_start / 1000, UTC).isoformat()}"
          f"..{datetime.fromtimestamp(selection_end / 1000, UTC).isoformat()} ({SELECTION_DAYS} 天训练期)")
    timestamps = [bar.ts for symbol in selected for bar in loaded[symbol]]
    start = datetime.fromtimestamp(min(timestamps) / 1000, UTC)
    end = datetime.fromtimestamp(max(timestamps) / 1000, UTC) + timedelta(minutes=15)
    # 与 high_short_strategy 共用同一套 384 组搜索空间（此前这里只有 128 组，
    # 与 DESIGN.md 声明的范围不符）。
    params_list = parameter_grid(lab_fixed)
    folds = []
    for fold, offset in enumerate((0, 30, 60), 1):
        train_start, train_end = start + timedelta(days=offset), start + timedelta(days=offset + 60)
        val_end, test_end = train_end + timedelta(days=30), train_end + timedelta(days=60)
        cut = lambda bars, lo, hi: [bar for bar in bars if lo <= bar.ts < hi]
        sets = [{symbol: cut(loaded[symbol], int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)) for symbol in selected} for lo, hi in ((train_start, train_end), (train_end, val_end), (val_end, test_end))]
        scored = [(aggregate_data(sets[0], params, args.fee_rate, args.slippage, args.funding_rate)[0], params) for params in params_list]
        eligible_scores = [item for item in scored if item[0].trades >= 5]
        scored = eligible_scores or scored
        # The user requires win rate strictly above 50%; select robust
        # candidates by win rate first, then net R and drawdown.
        scored.sort(key=lambda item: (-item[0].win_rate, -item[0].avg_net_r, item[0].max_drawdown_r))
        train_result, chosen = scored[0]
        validation, _ = aggregate_data(sets[1], chosen, args.fee_rate, args.slippage, args.funding_rate)
        test, test_trades = aggregate_data(sets[2], chosen, args.fee_rate, args.slippage, args.funding_rate)
        folds.append({"fold": fold, "params": chosen.as_dict(), "train": asdict(train_result), "validation": asdict(validation), "test": asdict(test), "test_trades": [asdict(trade) for trade in test_trades]})
        print(f"fold={fold} train={train_result.trades}/{train_result.win_rate:.1%} validation={validation.trades}/{validation.win_rate:.1%} test={test.trades}/{test.win_rate:.1%}")
    all_trades = [trade for fold in folds for trade in fold["test_trades"]]
    values = [trade["net_r"] for trade in all_trades]
    wins = sum(value > 0 for value in values)
    oos = {"trades": len(values), "wins": wins, "win_rate": wins / len(values) if values else 0, "total_r": sum(values), "avg_net_r": sum(values) / len(values) if values else 0}
    actual_rr = reward_risk_ratio(values)
    oos_beta = beta_interval(wins, len(values) - wins)
    oos_positive_probability = bootstrap_positive_probability([
        BaseTrade(
            trade["symbol"], trade["entry_ts"], trade["exit_ts"], trade["entry"],
            trade["stop"], trade["tp3"], trade["net_r"],
            "win" if trade["net_r"] > 0 else "loss", trade["partials"], 0.0,
        )
        for trade in all_trades
    ])
    # Interpret “risk:reward <= 1:2” as reward/risk >= 2.0 in the report.
    # DESIGN.md:79 的接受标准包含"样本外至少 30 笔"；此前只把样本量放进
    # acceptance 字典、没有参与 passed，导致 9 笔样本也能被判通过。
    # 接受标准与 high_short_strategy 共用同一实现（此前这边判 5 项、那边判 3 项，
    # 同一策略可能一个 PASS 一个 FAIL）。
    criteria = acceptance_criteria(oos, actual_rr, oos_positive_probability["positive_mean_probability"])
    passed = all(criteria.values())
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    report = {"strategy": "HLSR", "source": str(root), "timeframe": "5m source -> 15m entry", "window": {"start": start.isoformat(), "end": end.isoformat()}, "source_files": len(paths), "eligible_symbols": len(eligible), "selected_symbols": selected, "hard_filters": dict(lab["hard"]), "screening_stats": {symbol: stats[symbol] for symbol in selected}, "parameter_count": len(params_list), "folds": folds, "sample_out_of_sample": {**oos, "beta": oos_beta, "positive_probability": oos_positive_probability}, "actual_reward_risk": actual_rr, "risk_reward_requirement": "reward/risk >= 2.0 (interpreted from risk:reward <= 1:2)", "acceptance": criteria, "passed": passed}
    (output / "hlsr_market_export_report.json").write_text(json.dumps(report, indent=2))
    with (output / "hlsr_market_export_trades.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(all_trades[0]) if all_trades else ["symbol", "entry_ts", "net_r"], lineterminator="\n")
        writer.writeheader()
        writer.writerows(all_trades)
    print(f"source_files={len(paths)} eligible={len(eligible)} selected={len(selected)} out_of_sample={oos} actual_RR={actual_rr:.2f} status={'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
