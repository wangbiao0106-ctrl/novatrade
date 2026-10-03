#!/usr/bin/env python3
"""Causal 5m surge and waterfall short experiment.

The script intentionally uses only Python's standard library.  It reads one
confirmed 5m export at a time, resets rolling windows after timestamp gaps,
and writes every derived artifact below this strategy's results directory.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
import time
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple


FIVE_MINUTES = 5 * 60 * 1000
DAY = 24 * 60 * 60 * 1000
UTC = timezone.utc
LAB = Path(__file__).resolve().parents[1]
ROOT = LAB.parents[1]
DEFAULT_CONFIG = LAB / "config" / "strategy.json"
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_OUTPUT = LAB / "results"


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    index: int
    timestamp: int
    threshold: float
    breakout_4h: bool
    excursion_return: float
    body_return: float
    prior_four_hour_high: float
    limit_price: float
    atr: float
    signal_high: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_index: int
    signal_ts: int
    threshold: float
    breakout_4h: bool
    mode: str
    entry_index: int
    entry_ts: int
    fill_wait_bars: int
    order_price: float
    entry: float
    exit_index: int
    exit_ts: int
    exit: float
    stop: float
    target: float
    distance: float
    net_r: float
    notional_return: float
    adverse_return: float
    reason: str


def finite(value: object) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def iso(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, UTC).isoformat().replace("+00:00", "Z")


def report_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def symbol_from_path(path: Path) -> str:
    return path.name.split("_USDT_SWAP_5m_", 1)[0].upper()


def load_excluded(config_path: Path) -> set[str]:
    canonical = config_path.parents[2] / "sweep_reversal_short" / "config" / "universe.json"
    try:
        payload = json.loads(canonical.read_text(encoding="utf-8"))
        return {str(value).upper() for value in payload.get("exclude", [])}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def choose_paths(data_dir: Path, excluded: set[str]) -> list[tuple[str, Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in data_dir.glob("*_USDT_SWAP_5m_*.jsonl.gz"):
        symbol = symbol_from_path(path)
        if symbol not in excluded:
            grouped[symbol].append(path)
    return [(symbol, sorted(paths, key=lambda item: item.name)[-1])
            for symbol, paths in sorted(grouped.items())]


def load_bars(path: Path) -> list[Bar]:
    by_timestamp: dict[int, Bar] = {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get("confirmed") is not True:
                    continue
                values = [finite(row.get(field)) for field in
                          ("open", "high", "low", "close", "quote_volume")]
                timestamp = int(row.get("timestamp_ms"))
                if timestamp <= 0 or any(value is None for value in values):
                    continue
                opening, high, low, close, quote_volume = values
                if min(opening, high, low, close) <= 0 or quote_volume < 0:
                    continue
                if high < max(opening, close) or low > min(opening, close) or high < low:
                    continue
                by_timestamp[timestamp] = Bar(timestamp, opening, high, low, close, quote_volume)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return [by_timestamp[timestamp] for timestamp in sorted(by_timestamp)]


def segment_ids(bars: list[Bar]) -> list[int]:
    result: list[int] = []
    segment = 0
    for index, bar in enumerate(bars):
        if index and bar.ts != bars[index - 1].ts + FIVE_MINUTES:
            segment += 1
        result.append(segment)
    return result


def rolling_max(values: list[float], segments: list[int], width: int) -> list[Optional[float]]:
    """Inclusive rolling max, with a fresh window after every data gap."""
    result: list[Optional[float]] = [None] * len(values)
    queue: deque[int] = deque()
    start = 0
    for index, value in enumerate(values):
        if index and segments[index] != segments[index - 1]:
            queue.clear()
            start = index
        while queue and queue[0] < index - width + 1:
            queue.popleft()
        while queue and values[queue[-1]] <= value:
            queue.pop()
        queue.append(index)
        if index - start + 1 >= width:
            result[index] = values[queue[0]]
    return result


def atr_values(bars: list[Bar], segments: list[int], period: int) -> list[float]:
    result = [0.0] * len(bars)
    true_ranges: deque[float] = deque()
    total = 0.0
    current_segment = None
    for index, bar in enumerate(bars):
        if current_segment != segments[index]:
            true_ranges.clear()
            total = 0.0
            current_segment = segments[index]
        previous_close = bars[index - 1].close if index and segments[index] == segments[index - 1] else bar.open
        true_range = max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close))
        true_ranges.append(true_range)
        total += true_range
        if len(true_ranges) > period:
            total -= true_ranges.popleft()
        if len(true_ranges) == period:
            result[index] = total / period
    return result


def body_top(bar: Bar) -> float:
    """The top of a candle body: close for bullish, open for bearish."""
    return bar.close if bar.close >= bar.open else bar.open


def build_signals(symbol: str, bars: list[Bar], config: dict) -> list[Signal]:
    signal_config = config["signal"]
    threshold_values = [float(value) for value in signal_config["thresholds_exclusive"]]
    prior_four = int(signal_config["prior_four_hour_high_bars"])
    limit_window = int(signal_config["limit_reference_bars"])
    atr_period = int(config["risk"]["atr_period"])
    segments = segment_ids(bars)
    highs = [bar.high for bar in bars]
    body_tops = [body_top(bar) for bar in bars]
    prior_highs = rolling_max(highs, segments, prior_four)
    limit_tops = rolling_max(body_tops, segments, limit_window)
    atrs = atr_values(bars, segments, atr_period)
    signals: list[Signal] = []
    start = max(prior_four, limit_window, atr_period)
    for index in range(start, len(bars) - 1):
        if segments[index] != segments[index - 1] or segments[index] != segments[index + 1]:
            continue
        prior_four_high = prior_highs[index - 1]
        limit_price = limit_tops[index]
        if prior_four_high is None or limit_price is None or atrs[index] <= 0:
            continue
        previous_close = bars[index - 1].close
        bar = bars[index]
        excursion = bar.high / previous_close - 1.0 if previous_close > 0 else 0.0
        body_return = bar.close / bar.open - 1.0 if bar.open > 0 else 0.0
        breakout = bar.high > prior_four_high
        for threshold in threshold_values:
            if excursion <= threshold:
                continue
            signals.append(Signal(symbol, index, bar.ts, threshold, breakout, excursion,
                                  body_return, prior_four_high, limit_price, atrs[index], bar.high))
    return signals


def simulate_order(signal: Signal, bars: list[Bar], mode: str, config: dict) -> Tuple[Optional[Trade], Optional[str]]:
    entry_config = config["entry"]
    risk = config["risk"]
    costs = config["costs"]
    first = signal.index + 1
    if first >= len(bars) or bars[first].ts != signal.timestamp + FIVE_MINUTES:
        return None, "entry_gap"
    raw_entry = None
    fill_index = None
    fill_wait = 0
    order_price = signal.limit_price if mode == "limit_recent_72_body_top" else bars[first].open
    if mode == "next_5m_open":
        raw_entry, fill_index = bars[first].open, first
    elif mode == "limit_recent_72_body_top":
        end = min(len(bars), first + int(entry_config["limit_wait_bars"]))
        for index in range(first, end):
            if index > first and bars[index].ts != bars[index - 1].ts + FIVE_MINUTES:
                return None, "entry_gap"
            if bars[index].open >= signal.limit_price:
                raw_entry, fill_index = bars[index].open, index
                break
            if bars[index].high >= signal.limit_price:
                raw_entry, fill_index = signal.limit_price, index
                break
            fill_wait += 1
        if fill_index is None:
            return None, "limit_unfilled"
    else:
        raise ValueError(f"unknown entry mode: {mode}")

    entry = raw_entry * (1.0 - float(costs["slippage_one_way"]))
    stop = max(signal.signal_high, raw_entry) + float(risk["stop_atr"]) * signal.atr
    distance = stop - entry
    if distance <= 0:
        return None, "invalid_stop"
    if distance / entry > float(risk["max_stop_distance_pct"]):
        return None, "stop_distance"
    target = entry - float(risk["target_r"]) * distance
    if target <= 0:
        return None, "invalid_target"
    last = min(len(bars) - 1, fill_index + int(risk["max_hold_bars"]) - 1)
    reason = "data_end" if last == len(bars) - 1 else "timeout"
    exit_price = bars[last].close
    exit_index = last
    exit_ts = bars[last].ts + FIVE_MINUTES
    worst_price = entry
    for index in range(fill_index, last + 1):
        bar = bars[index]
        if index > fill_index and bar.ts != bars[index - 1].ts + FIVE_MINUTES:
            exit_price, exit_index, exit_ts, reason = bars[index - 1].close, index - 1, bars[index - 1].ts + FIVE_MINUTES, "data_gap"
            break
        if bar.open >= stop:
            worst_price = max(worst_price, bar.open)
            exit_price, exit_index, exit_ts, reason = bar.open, index, bar.ts, "stop_gap"
            break
        worst_price = max(worst_price, min(bar.high, stop))
        if bar.high >= stop:
            exit_price, exit_index, exit_ts, reason = stop, index, bar.ts + FIVE_MINUTES, "stop"
            break
        if bar.low <= target:
            exit_price, exit_index, exit_ts, reason = target, index, bar.ts + FIVE_MINUTES, "target"
            break
    exit_price *= 1.0 + float(costs["slippage_one_way"])
    fee = float(costs["fee_rate_one_way"]) * (entry + exit_price)
    net = entry - exit_price - fee - entry * float(costs.get("funding_rate_round_trip", 0.0))
    adverse_exit = worst_price * (1.0 + float(costs["slippage_one_way"]))
    adverse = (entry - adverse_exit - float(costs["fee_rate_one_way"]) * (entry + adverse_exit)) / entry
    return Trade(signal.symbol, signal.index, signal.timestamp, signal.threshold, signal.breakout_4h,
                 mode, fill_index, bars[fill_index].ts, fill_wait, order_price, entry,
                 exit_index, exit_ts, exit_price, stop, target, distance, net / distance,
                 net / entry, adverse, reason), None


def trade_metrics(trades: list[Trade]) -> dict:
    ordered = sorted(trades, key=lambda item: (item.exit_ts, item.symbol, item.entry_ts))
    values = [trade.net_r for trade in ordered]
    wins = [value for value in values if value > 0]
    losses = [-value for value in values if value < 0]
    equity = peak = 0.0
    max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return {
        "trades": len(trades),
        "wins": len(wins),
        "win_rate": len(wins) / len(trades) if trades else None,
        "total_r": sum(values),
        "avg_net_r": statistics.fmean(values) if values else None,
        "profit_factor": sum(wins) / sum(losses) if losses else (None if not wins else None),
        "max_drawdown_r": max_drawdown,
        "symbols": len({trade.symbol for trade in trades}),
        "avg_adverse_return_pct": statistics.fmean(trade.adverse_return for trade in trades) * 100 if trades else None,
        "median_fill_wait_bars": statistics.median([trade.fill_wait_bars for trade in trades]) if trades else None,
        "exit_reasons": dict(Counter(trade.reason for trade in trades)),
    }


def portfolio(trades: list[Trade], config: dict) -> tuple[list[Trade], list[dict], dict]:
    management = config["position_management"]
    risk = config["risk"]
    equity = peak = 10000.0
    drawdown = adverse_drawdown = 0.0
    active_until = -1
    next_allowed: dict[str, int] = {}
    current_day = None
    day_start_equity = equity
    counts: Counter[str] = Counter()
    admitted: list[Trade] = []
    rows: list[dict] = []
    for trade in sorted(trades, key=lambda item: (item.entry_ts, item.symbol, item.signal_index)):
        if trade.entry_ts < active_until:
            counts["global_concurrency"] += 1
            continue
        if trade.signal_index <= next_allowed.get(trade.symbol, -1):
            counts["symbol_cooldown"] += 1
            continue
        day = trade.entry_ts // DAY
        if day != current_day:
            current_day, day_start_equity = day, equity
        if equity <= day_start_equity * (1.0 - float(management["account_daily_loss_limit_pct"]) / 100.0):
            counts["realized_daily_loss_gate"] += 1
            continue
        notional = min(equity * float(management["leverage"]),
                       equity * float(management["risk_per_trade_pct"]) / 100.0 * trade.entry / trade.distance)
        pnl = notional * trade.notional_return
        row = asdict(trade)
        row.update(notional_usdt=notional, margin_usdt=notional / float(management["leverage"]),
                   equity_before=equity, pnl_usdt=pnl, equity_after=equity + pnl)
        rows.append(row)
        admitted.append(trade)
        equity += pnl
        peak = max(peak, equity)
        drawdown = max(drawdown, 1.0 - equity / peak)
        adverse_drawdown = max(adverse_drawdown, 1.0 - (equity + notional * trade.adverse_return) / peak)
        active_until = trade.exit_ts
        next_allowed[trade.symbol] = trade.exit_index + int(risk["cooldown_bars"])
    return admitted, rows, {
        "initial_equity_usdt": 10000.0,
        "final_equity_usdt": equity,
        "account_return_pct": (equity / 10000.0 - 1.0) * 100.0,
        "max_realized_drawdown_pct": drawdown * 100.0,
        "conservative_adverse_drawdown_pct": max(drawdown, adverse_drawdown) * 100.0,
        "admission_rejected": dict(counts),
    }


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    fields = list(rows[0])
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def phase_metrics(trades: list[Trade], split: int) -> dict:
    before = [trade for trade in trades if trade.signal_ts < split]
    after = [trade for trade in trades if trade.signal_ts >= split]
    return {"before_split": trade_metrics(before), "after_split": trade_metrics(after)}


def run(data_dir: Path, config_path: Path, output_dir: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    output_dir.mkdir(parents=True, exist_ok=True)
    split = int(datetime.fromisoformat(config["research"]["split_utc"]).timestamp() * 1000)
    excluded = load_excluded(config_path)
    started = time.monotonic()
    modes = ["next_5m_open", "limit_recent_72_body_top"]
    thresholds = sorted(float(value) for value in config["signal"]["thresholds_exclusive"])
    variant_names = [
        f"gain_gt_{int(threshold * 100)}pct_{'breakout_4h' if breakout else 'no_breakout_4h'}|{mode}"
        for threshold in thresholds for breakout in (True, False) for mode in modes
    ]
    raw_trades: dict[str, list[Trade]] = defaultdict(list)
    signal_counts: Counter[str] = Counter()
    signal_symbols: dict[str, set[str]] = defaultdict(set)
    rejected: dict[str, Counter[str]] = defaultdict(Counter)
    event_rows = []
    sources = []
    bars_total = 0
    start_ts = None
    end_ts = None
    paths = choose_paths(data_dir, excluded)
    for number, (symbol, path) in enumerate(paths, 1):
        bars = load_bars(path)
        bars_total += len(bars)
        if bars:
            start_ts = bars[0].ts if start_ts is None else min(start_ts, bars[0].ts)
            end_ts = bars[-1].ts if end_ts is None else max(end_ts, bars[-1].ts)
        events = build_signals(symbol, bars, config) if len(bars) >= 73 else []
        for signal in events:
            base_key = f"gain_gt_{int(signal.threshold * 100)}pct_{'breakout_4h' if signal.breakout_4h else 'no_breakout_4h'}"
            event_rows.append({"symbol": symbol, "signal_index": signal.index,
                               "signal_utc": iso(signal.timestamp), "threshold": signal.threshold,
                               "breakout_4h": signal.breakout_4h,
                               "excursion_return_pct": signal.excursion_return * 100,
                               "body_return_pct": signal.body_return * 100,
                               "prior_four_hour_high": signal.prior_four_hour_high,
                               "limit_body_top_price": signal.limit_price, "atr": signal.atr})
            for mode in modes:
                name = f"{base_key}|{mode}"
                signal_counts[name] += 1
                signal_symbols[name].add(symbol)
                trade, reason = simulate_order(signal, bars, mode, config)
                if trade is None:
                    rejected[name][reason or "unknown"] += 1
                else:
                    raw_trades[name].append(trade)
        sources.append({"symbol": symbol, "path": report_path(path), "bytes": path.stat().st_size,
                        "bars": len(bars), "signals": len(events)})
        if number % 50 == 0 or number == 1:
            print(f"loaded {number} symbols; {len(event_rows)} signals", flush=True)
    report = {
        "strategy": config["strategy"], "config": config,
        "data": {"symbols": len(paths), "bars": bars_total,
                 "start_utc": iso(start_ts) if start_ts else None,
                 "end_utc_last_bar": iso(end_ts) if end_ts else None,
                 "market_data_read_passes": 1, "sources": sources},
        "signal_counts": {}, "variants": {}, "runtime_seconds": None,
        "limitations": [
            "当前存续合约造成幸存者偏差。",
            "限价触价使用 OHLC 理想成交，没有排队、部分成交或盘口深度模型。",
            "历史资金费率不可用，按 0 处理；也没有模拟强平。",
            "止损、止盈和仓位预算是固定研究基线，不能把最佳组合视为样本外结论。",
        ],
    }
    write_csv(output_dir / "signals.csv", event_rows)
    split_iso = iso(split)
    for name in variant_names:
        base_key = name.rsplit("|", 1)[0]
        threshold = float(base_key.split("_")[2][:-3]) / 100.0
        breakout = "_no_breakout_4h" not in base_key
        matching = [row for row in event_rows if row["threshold"] == threshold and row["breakout_4h"] == breakout]
        report["signal_counts"][base_key] = {
            "events": len(matching), "symbols": len({row["symbol"] for row in matching}),
            "before_split_events": sum(row["signal_utc"] < split_iso for row in matching),
            "after_split_events": sum(row["signal_utc"] >= split_iso for row in matching),
        }
        trades = raw_trades[name]
        admitted, rows, account = portfolio(trades, config)
        report["variants"][name] = {
            "signals": signal_counts[name], "signal_symbols": len(signal_symbols[name]),
            "orders_filled": len(trades), "fill_rate": len(trades) / signal_counts[name] if signal_counts[name] else None,
            "rejected": dict(rejected[name]), "raw_trade_metrics": trade_metrics(trades),
            "portfolio_metrics": trade_metrics(admitted), "portfolio_account": account,
            "portfolio_phases": phase_metrics(admitted, split),
        }
        write_csv(output_dir / (name.replace("|", "_") + "_trades.csv"), rows)
    report["runtime_seconds"] = time.monotonic() - started
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    summary_rows = []
    for name, item in report["variants"].items():
        metrics = item["portfolio_metrics"]
        summary_rows.append({"variant": name, "signals": item["signals"], "signal_symbols": item["signal_symbols"],
                             "orders_filled": item["orders_filled"], "fill_rate": item["fill_rate"],
                             "trades": metrics["trades"], "win_rate": metrics["win_rate"],
                             "profit_factor": metrics["profit_factor"], "total_r": metrics["total_r"],
                             "max_drawdown_r": metrics["max_drawdown_r"],
                             "account_return_pct": item["portfolio_account"]["account_return_pct"],
                             "account_drawdown_pct": item["portfolio_account"]["max_realized_drawdown_pct"]})
    write_csv(output_dir / "summary.csv", summary_rows)
    print(f"loaded {len(paths)} symbols, {bars_total} bars; wrote {len(summary_rows)} variants in {report['runtime_seconds']:.1f}s", flush=True)
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    run(args.data_dir, args.config, args.output_dir)


if __name__ == "__main__":
    main()
