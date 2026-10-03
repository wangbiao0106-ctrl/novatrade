#!/usr/bin/env python3
"""Backtest the previous-day close reversal short pattern.

The pattern uses complete UTC days built from confirmed 5m candles:
the previous day must close more than 100% above its open, and the following
day must close between 0% and 10% above its own open. The short is entered at
that following day's close. No stop is specified by the rule, so positions
remain open until the 20% partial and cumulative 40% targets, data end, or a
data gap.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


LAB = Path(__file__).resolve().parents[1]
ROOT = LAB.parents[1]
SOURCE = LAB / "src" / "backtest.py"
DEFAULT_CONFIG = LAB / "config" / "strategy.json"
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_OUTPUT = LAB / "results" / "prev_day_close_reversal"
SPLIT_UTC = datetime(2026, 4, 1, tzinfo=timezone.utc)


def load_backtest_module():
    spec = importlib.util.spec_from_file_location("extreme_negative_funding_backtest", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BT = load_backtest_module()


@dataclass(frozen=True)
class DailySignal:
    symbol: str
    previous_day: str
    entry_day: str
    previous_day_gain: float
    entry_day_gain: float
    entry_index: int
    entry_ts: int
    day_open: float
    entry_close: float


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def iso_day(day: int) -> str:
    return datetime.fromtimestamp(day * BT.DAY / 1000, timezone.utc).date().isoformat()


def complete_days(bars: list) -> list[dict]:
    grouped: dict[int, list[tuple[int, object]]] = {}
    for index, bar in enumerate(bars):
        grouped.setdefault(bar.ts // BT.DAY, []).append((index, bar))
    days: list[dict] = []
    for day, rows in sorted(grouped.items()):
        if len(rows) != 288:
            continue
        if rows[0][1].ts != day * BT.DAY or rows[-1][1].ts != day * BT.DAY + 287 * BT.FIVE_MINUTES:
            continue
        if any(right[1].ts - left[1].ts != BT.FIVE_MINUTES for left, right in zip(rows, rows[1:])):
            continue
        first_index, first = rows[0]
        last_index, last = rows[-1]
        days.append({
            "day": day,
            "first_index": first_index,
            "last_index": last_index,
            "open": first.open,
            "close": last.close,
            "gain": last.close / first.open - 1.0,
        })
    return days


def make_signals(symbol: str, bars: list) -> tuple[list[DailySignal], Counter[str]]:
    days = complete_days(bars)
    counts: Counter[str] = Counter()
    counts["complete_utc_days"] = len(days)
    signals: list[DailySignal] = []
    for previous, current in zip(days, days[1:]):
        if current["day"] != previous["day"] + 1:
            counts["nonconsecutive_day_pairs"] += 1
            continue
        if previous["gain"] <= 1.0:
            continue
        if not 0.0 <= current["gain"] <= 0.10:
            continue
        counts["qualifying_day_pairs"] += 1
        entry_bar = bars[current["last_index"]]
        signals.append(DailySignal(
            symbol=symbol,
            previous_day=iso_day(previous["day"]),
            entry_day=iso_day(current["day"]),
            previous_day_gain=previous["gain"],
            entry_day_gain=current["gain"],
            entry_index=current["last_index"],
            entry_ts=entry_bar.ts + BT.FIVE_MINUTES,
            day_open=current["open"],
            entry_close=current["close"],
        ))
    return signals, counts


def simulate(signal: DailySignal, bars: list, config: dict):
    costs = config["costs"]
    slip = float(costs["slippage_one_way"])
    fee = float(costs["fee_rate_one_way"])
    entry = signal.entry_close * (1.0 - slip)
    tp1 = entry * 0.80
    tp2 = entry * 0.60
    start = signal.entry_index + 1
    if start >= len(bars):
        return None, "data_end_before_exit"
    partial = 0.0
    remaining = 1.0
    tp1_exit: Optional[float] = None
    final_exit = bars[start].close
    exit_index = start
    exit_ts = bars[start].ts + BT.FIVE_MINUTES
    reason = "data_end"
    previous_index = signal.entry_index
    for index in range(start, len(bars)):
        bar = bars[index]
        if bar.ts - bars[previous_index].ts != BT.FIVE_MINUTES:
            final_exit = bars[previous_index].close
            exit_index = previous_index
            exit_ts = bars[previous_index].ts + BT.FIVE_MINUTES
            reason = "data_gap"
            break
        previous_index = index
        if partial == 0.0 and bar.low <= tp1:
            partial = 0.5
            remaining = 0.5
            tp1_exit = tp1
        if partial and bar.low <= tp2:
            final_exit = tp2
            exit_index = index
            exit_ts = bar.ts + BT.FIVE_MINUTES
            reason = "target_2"
            break
    else:
        final_exit = bars[-1].close
        exit_index = len(bars) - 1
        exit_ts = bars[-1].ts + BT.FIVE_MINUTES
        reason = "data_end"

    if partial:
        net = BT._exit_value(partial, entry, tp1, slip, fee) + BT._exit_value(remaining, entry, final_exit, slip, fee)
    else:
        net = BT._exit_value(1.0, entry, final_exit, slip, fee)
    net_return = net / entry
    trade = BT.Trade(
        signal.symbol,
        signal.entry_index,
        signal.entry_ts,
        signal.entry_index,
        signal.entry_ts,
        signal.entry_close,
        entry,
        0.0,
        tp1,
        tp2,
        tp1_exit,
        final_exit,
        exit_index,
        exit_ts,
        reason,
        net_return,
        net_return * 100.0,
        exit_index - signal.entry_index + 1,
    )
    return trade, None


def metrics(trades: list) -> dict:
    return BT.trade_metrics(trades)


def is_validation(ts: int) -> bool:
    return datetime.fromtimestamp(ts / 1000, timezone.utc) >= SPLIT_UTC


def run(data_dir: Path, config_path: Path, output_dir: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    allowed = BT.allowed_symbols(config)
    sources: list[tuple[str, Path, list]] = []
    quality_totals: Counter[str] = Counter()
    all_signals: list[DailySignal] = []
    all_trades: list = []
    counts: Counter[str] = Counter()
    for symbol, path in BT.choose_paths(data_dir, allowed):
        bars, quality = BT.load_bars(path)
        sources.append((symbol, path, bars))
        quality_totals.update(quality)
        signals, local_counts = make_signals(symbol, bars)
        counts.update(local_counts)
        all_signals.extend(signals)
        for signal in signals:
            trade, reason = simulate(signal, bars, config)
            if trade is None:
                counts[f"trade_rejected_{reason}"] += 1
            else:
                all_trades.append(trade)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "signals.csv", [asdict(signal) | {"entry_utc": BT.iso(signal.entry_ts)}
                                            for signal in all_signals])
    write_csv(output_dir / "trades.csv", [asdict(trade) | {
        "signal_utc": BT.iso(trade.signal_ts),
        "entry_utc": BT.iso(trade.entry_ts),
        "exit_utc": BT.iso(trade.exit_ts),
    } for trade in all_trades])
    result_metrics = metrics(all_trades)
    train_trades = [trade for trade in all_trades if not is_validation(trade.entry_ts)]
    validation_trades = [trade for trade in all_trades if is_validation(trade.entry_ts)]
    train_signals = [signal for signal in all_signals if not is_validation(signal.entry_ts)]
    validation_signals = [signal for signal in all_signals if is_validation(signal.entry_ts)]
    report = {
        "strategy": config["strategy"],
        "study": "previous_day_close_gt_100_following_day_close_0_to_10_short",
        "validation_mode": "price_only_ignore_funding",
        "rule": {
            "previous_day_close_gain_gt": 1.0,
            "following_day_close_gain_min": 0.0,
            "following_day_close_gain_max": 0.10,
            "entry": "following UTC-day close",
            "leverage": 1.0,
            "tp1": "20% favorable move; close 50%",
            "tp2": "40% cumulative favorable move; close remaining 50%",
            "stop": "none specified",
        },
        "event_definition": "one qualifying consecutive complete UTC-day pair per symbol",
        "data": {"directory": str(data_dir), "symbols": len(sources), "quality_totals": dict(quality_totals)},
        "counts": dict(counts),
        "results": {
            "signals": len(all_signals),
            "trades": len(all_trades),
            "unfilled_or_rejected": len(all_signals) - len(all_trades),
            "return_basis": "net_return_per_trade_relative_to_entry_notional",
            "overlapping_positions_serialized": False,
            **result_metrics,
            "train": {"signals": len(train_signals), "trades": len(train_trades), **metrics(train_trades)},
            "validation": {"signals": len(validation_signals), "trades": len(validation_trades), **metrics(validation_trades)},
        },
        "limitations": [
            "Funding is ignored and no funding PnL is applied.",
            "No stop was specified; positions remain open until targets or data termination.",
            "Fixed-notional trade returns are reported; overlapping positions are not serialized into an account simulation.",
            "OHLC cannot establish the intrabar order between TP1 and TP2 beyond the defined low-price checks.",
        ],
    }
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="run the daily-pair research scan")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.scan:
        parser.error("--scan is required")
    report = run(args.data_dir, args.config, args.output_dir)
    print(json.dumps({
        "signals": report["results"]["signals"],
        "trades": report["results"]["trades"],
        "win_rate": report["results"]["win_rate"],
        "total_net_return": report["results"]["total_net_return"],
        "compound_return": report["results"]["compound_return"],
        "profit_factor": report["results"]["profit_factor"],
        "report": str(args.output_dir / "report.json"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
