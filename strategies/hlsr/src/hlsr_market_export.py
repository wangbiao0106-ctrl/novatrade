#!/usr/bin/env python3
"""Run HLSR against the supplied 5-minute market_export archive."""

from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import json
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from high_short_strategy import Params, aggregate_data, aggregate_result, backtest
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


def hard_filter_stats(bars: list[Bar]) -> dict[str, int]:
    closes = [bar.close for bar in bars]
    quotes = [bar.quote_volume for bar in bars]
    gain_hits = volume_hits = composite_hits = 0
    for index in range(96, len(bars)):
        gain_ok = closes[index] > closes[index - 96] * 1.40
        volume_ok = rolling_sum(quotes, index, 96) > 30_000_000
        gain_hits += int(gain_ok)
        volume_hits += int(volume_ok)
        composite_hits += int(gain_ok and volume_ok)
    return {"bars": len(bars), "gain_hits": gain_hits, "volume_hits": volume_hits, "composite_hits": composite_hits}


def export_grid() -> list[Params]:
    values = itertools.product((6, 12), (0.4, 0.6), (1.0, 1.5), (1, 2), (4, 8), (0.25, 0.5), (2,), (True,), ("any", "primary_sweep"))
    return [Params(*value) for value in values]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--market-export", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--max-symbols", type=int, default=50)
    parser.add_argument("--min-events", type=int, default=3)
    parser.add_argument("--fee-rate", type=float, default=0.0006)
    parser.add_argument("--slippage", type=float, default=0.0002)
    parser.add_argument("--funding-rate", type=float, default=0.0)
    args = parser.parse_args()
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
        stats[symbol] = hard_filter_stats([bar for bar in bars if bar.ts < selection_end])
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
    params_list = export_grid()
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
    positive = [value for value in values if value > 0]
    negative = [-value for value in values if value < 0]
    actual_rr = (sum(positive) / len(positive)) / (sum(negative) / len(negative)) if positive and negative else 0
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
    sample_sufficient = oos["trades"] >= 30
    criteria = {
        "win_rate_gt_50pct": oos["win_rate"] > 0.50,
        "positive_mean_probability_gt_50pct": oos_positive_probability["positive_mean_probability"] > 0.50,
        "actual_reward_risk_gte_2": actual_rr >= 2.0,
        "average_net_r_positive": oos["avg_net_r"] > 0,
        "sample_sufficient_30_trades": sample_sufficient,
    }
    passed = all(criteria.values())
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    report = {"strategy": "HLSR", "source": str(root), "timeframe": "5m source -> 15m entry", "window": {"start": start.isoformat(), "end": end.isoformat()}, "source_files": len(paths), "eligible_symbols": len(eligible), "selected_symbols": selected, "hard_filters": {"gain_24h_gt": 0.40, "quote_volume_24h_gt": 30_000_000}, "screening_stats": {symbol: stats[symbol] for symbol in selected}, "parameter_count": len(params_list), "folds": folds, "sample_out_of_sample": {**oos, "beta": oos_beta, "positive_probability": oos_positive_probability}, "actual_reward_risk": actual_rr, "risk_reward_requirement": "reward/risk >= 2.0 (interpreted from risk:reward <= 1:2)", "acceptance": criteria, "passed": passed}
    (output / "hlsr_market_export_report.json").write_text(json.dumps(report, indent=2))
    with (output / "hlsr_market_export_trades.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(all_trades[0]) if all_trades else ["symbol", "entry_ts", "net_r"])
        writer.writeheader()
        writer.writerows(all_trades)
    print(f"source_files={len(paths)} eligible={len(eligible)} selected={len(selected)} out_of_sample={oos} actual_RR={actual_rr:.2f} status={'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
