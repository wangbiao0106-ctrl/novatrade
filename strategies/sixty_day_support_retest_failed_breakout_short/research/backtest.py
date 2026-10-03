#!/usr/bin/env python3
"""Causal 15m support-retest failed-breakout short backtest."""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import Counter, defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


FIVE_MINUTES = 5 * 60 * 1000
FIFTEEN_MINUTES = 15 * 60 * 1000
DAY = 24 * 60 * 60 * 1000
DAY_15M_BARS = 96
UTC = timezone.utc
LAB = Path(__file__).resolve().parents[1]
ROOT = LAB.parents[1]
DEFAULT_CONFIG = LAB / "config" / "strategy.json"
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_OUTPUT = LAB / "results"
SPLIT_UTC = datetime(2026, 4, 1, tzinfo=timezone.utc)


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
    signal_index: int
    signal_ts: int
    entry_ts: int
    day_open: float
    rolling_60d_low: float
    first_peak: float
    support: float
    breakout_high: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_index: int
    signal_ts: int
    entry_ts: int
    entry: float
    initial_stop: float
    tp1: float
    tp2: float
    runner_target: float
    tp1_exit: Optional[float]
    tp2_exit: Optional[float]
    final_exit: float
    exit_index: int
    exit_ts: int
    exit_reason: str
    net_return: float
    leveraged_return_pct: float
    bars_held: int


def finite(value: object) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def iso(ts: Optional[int]) -> Optional[str]:
    return datetime.fromtimestamp(ts / 1000, UTC).isoformat().replace("+00:00", "Z") if ts else None


def symbol_from_path(path: Path) -> str:
    return path.name.split("_USDT_SWAP_5m_", 1)[0].upper()


def load_bars(path: Path) -> tuple[list[Bar], dict[str, int]]:
    raw: dict[int, Bar] = {}
    quality: Counter[str] = Counter()
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get("confirmed") is not True:
                    quality["unconfirmed_rows"] += 1
                    continue
                ts = int(row["timestamp_ms"])
                values = [finite(row.get(key)) for key in ("open", "high", "low", "close", "quote_volume")]
                if ts <= 0 or ts % FIVE_MINUTES or any(value is None for value in values):
                    raise ValueError("invalid timestamp or value")
                opening, high, low, close, volume = values
                if min(opening, high, low, close) <= 0 or volume < 0:
                    raise ValueError("invalid OHLCV")
                if high < max(opening, close) or low > min(opening, close) or high < low:
                    raise ValueError("invalid OHLC")
                if ts in raw:
                    quality["duplicate_rows"] += 1
                raw[ts] = Bar(ts, opening, high, low, close, volume)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                quality["invalid_rows"] += 1
    bars = [raw[ts] for ts in sorted(raw)]
    quality["confirmed_rows"] = len(bars)
    quality["timestamp_gaps"] = sum(
        1 for left, right in zip(bars, bars[1:]) if right.ts - left.ts != FIVE_MINUTES
    )
    return bars, dict(quality)


def aggregate_15m(bars: list[Bar]) -> list[Bar]:
    groups: dict[int, dict[int, Bar]] = defaultdict(dict)
    for bar in bars:
        bucket = (bar.ts // FIFTEEN_MINUTES) * FIFTEEN_MINUTES
        groups[bucket][bar.ts] = bar
    result: list[Bar] = []
    for bucket in sorted(groups):
        children = groups[bucket]
        expected = [bucket + step * FIVE_MINUTES for step in range(3)]
        if any(ts not in children for ts in expected):
            continue
        values = [children[ts] for ts in expected]
        result.append(Bar(bucket, values[0].open, max(item.high for item in values),
                          min(item.low for item in values), values[-1].close,
                          sum(item.quote_volume for item in values)))
    return result


def rolling_prior_low(bars: list[Bar], window: int) -> list[Optional[float]]:
    result: list[Optional[float]] = [None] * len(bars)
    queue: deque[int] = deque()
    for index, bar in enumerate(bars):
        while queue and queue[0] <= index - window:
            queue.popleft()
        if queue:
            result[index] = bars[queue[0]].low
        while queue and bars[queue[-1]].low >= bar.low:
            queue.pop()
        queue.append(index)
    return result


def contiguous(bars: list[Bar], start: int, end: int) -> bool:
    return start >= 0 and end < len(bars) and all(
        bars[index].ts - bars[index - 1].ts == FIFTEEN_MINUTES
        for index in range(start + 1, end + 1)
    )


def day_open_at(bars: list[Bar]) -> list[Optional[float]]:
    opens: list[Optional[float]] = [None] * len(bars)
    current_day: Optional[int] = None
    opening: Optional[float] = None
    valid = False
    for index, bar in enumerate(bars):
        day = bar.ts // DAY
        if current_day != day:
            current_day = day
            opening = bar.open if bar.ts % DAY == 0 else None
            valid = opening is not None
        elif index and bars[index].ts - bars[index - 1].ts != FIFTEEN_MINUTES:
            valid = False
        opens[index] = opening if valid else None
    return opens


def pivot_high(bars: list[Bar], index: int, left: int = 2, right: int = 2) -> bool:
    if index < left or index + right >= len(bars):
        return False
    value = bars[index].high
    return value > max(bars[position].high for position in range(index - left, index)) and \
        value >= max(bars[position].high for position in range(index + 1, index + right + 1))


def pivot_low(bars: list[Bar], index: int, left: int = 2, right: int = 2) -> bool:
    if index < left or index + right >= len(bars):
        return False
    value = bars[index].low
    return value < min(bars[position].low for position in range(index - left, index)) and \
        value <= min(bars[position].low for position in range(index + 1, index + right + 1))


def find_signals(symbol: str, bars: list[Bar], config: dict) -> tuple[list[Signal], Counter[str]]:
    signal = config["signal"]
    lookback = int(float(signal["lookback_days"]) * DAY_15M_BARS)
    pullback_max = int(signal["pullback_max_bars"])
    breakout_max = int(signal["breakout_max_bars"])
    tolerance = float(signal["support_retest_tolerance"])
    breakdown = float(signal["support_break_tolerance"])
    rolling_low = rolling_prior_low(bars, lookback)
    opens = day_open_at(bars)
    counts: Counter[str] = Counter()
    signals: list[Signal] = []
    used_days: set[int] = set()
    counts["aggregated_15m_bars"] = len(bars)
    for peak in range(lookback + 2, len(bars) - 3):
        if rolling_low[peak] is None or opens[peak] is None:
            continue
        if bars[peak].high / rolling_low[peak] - 1.0 <= float(signal["lookback_gain_gt"]):
            continue
        if bars[peak].high / opens[peak] - 1.0 <= float(signal["utc_day_gain_gt"]):
            continue
        if not pivot_high(bars, peak):
            continue
        peak_day = bars[peak].ts // DAY
        if peak_day in used_days:
            continue
        support_end = min(len(bars) - 3, peak + pullback_max)
        support_index: Optional[int] = None
        for candidate in range(peak + 2, support_end + 1):
            if bars[candidate].ts // DAY != peak_day:
                break
            if not pivot_low(bars, candidate):
                continue
            if bars[candidate].low < bars[peak].high * 0.40:
                continue
            support_index = candidate
            break
        if support_index is None:
            continue
        support = bars[support_index].low
        retest_index: Optional[int] = None
        for retest in range(support_index + 3, min(support_end, support_index + pullback_max) + 1):
            if bars[retest].ts // DAY != peak_day:
                break
            if any(bars[position].low < support * (1.0 - breakdown)
                   for position in range(support_index + 1, retest)):
                break
            if support * (1.0 - breakdown) <= bars[retest].low <= support * (1.0 + tolerance) \
                    and bars[retest].close > support and bars[retest].close > bars[retest].open:
                retest_index = retest
                break
        if retest_index is None:
            continue
        breakout_index: Optional[int] = None
        for breakout in range(retest_index + 1, min(len(bars) - 2, retest_index + breakout_max) + 1):
            if bars[breakout].ts // DAY != peak_day:
                break
            if bars[breakout].high > bars[peak].high:
                breakout_index = breakout
                break
        if breakout_index is None:
            continue
        failure = breakout_index + 1
        if bars[failure].ts // DAY != peak_day:
            continue
        if bars[failure].high > bars[breakout_index].high or bars[failure].close >= bars[failure].open:
            continue
        if not contiguous(bars, peak - 2, failure):
            continue
        used_days.add(peak_day)
        signals.append(Signal(
            symbol=symbol,
            signal_index=failure,
            signal_ts=bars[failure].ts,
            entry_ts=bars[failure].ts + FIFTEEN_MINUTES,
            day_open=opens[peak],
            rolling_60d_low=rolling_low[peak],
            first_peak=bars[peak].high,
            support=support,
            breakout_high=bars[breakout_index].high,
        ))
        counts["signals"] += 1
    return signals, counts


def exit_value(fraction: float, entry: float, raw_exit: float, slip: float, fee: float) -> float:
    effective = raw_exit * (1.0 + slip)
    return fraction * (entry - effective) - fraction * fee * (entry + effective)


def simulate(signal: Signal, bars: list[Bar], config: dict) -> tuple[Optional[Trade], Optional[str]]:
    risk = config["risk"]
    costs = config["costs"]
    slip = float(costs["slippage_one_way"])
    fee = float(costs["fee_rate_one_way"])
    entry = bars[signal.signal_index].close * (1.0 - slip)
    stop = entry * (1.0 + float(risk["initial_stop_above_entry_pct"]))
    tp1 = entry * (1.0 - float(risk["tp1_favorable_move_pct"]))
    tp2 = entry * (1.0 - float(risk["tp2_favorable_move_pct"]))
    runner_target = entry * (1.0 - float(risk["runner_favorable_move_pct"]))
    max_hold = int(risk["max_hold_bars_15m"])
    start = signal.signal_index + 1
    if start >= len(bars):
        return None, "data_end_before_entry"
    partial_1 = False
    partial_2 = False
    tp1_exit: Optional[float] = None
    tp2_exit: Optional[float] = None
    final_exit = bars[start].close
    exit_index = start
    exit_ts = bars[start].ts + FIFTEEN_MINUTES
    reason = "time_exit"
    active_stop = stop
    previous_index = signal.signal_index
    last_index = min(len(bars) - 1, signal.signal_index + max_hold)
    for index in range(start, last_index + 1):
        bar = bars[index]
        if not contiguous(bars, previous_index, index):
            final_exit = bars[previous_index].close
            exit_index = previous_index
            exit_ts = bars[previous_index].ts + FIFTEEN_MINUTES
            reason = "data_gap"
            break
        previous_index = index
        if bar.open >= active_stop:
            final_exit = bar.open
            exit_index, exit_ts = index, bar.ts
            reason = "breakeven_stop_gap" if partial_1 else "stop_gap"
            break
        if bar.high >= active_stop:
            final_exit = active_stop
            exit_index, exit_ts = index, bar.ts + FIFTEEN_MINUTES
            reason = "breakeven_stop" if partial_1 else "stop"
            break
        if not partial_1 and bar.low <= tp1:
            partial_1 = True
            tp1_exit = tp1
            active_stop = entry
        if partial_1 and not partial_2 and bar.low <= tp2:
            partial_2 = True
            tp2_exit = tp2
        if partial_2 and bar.low <= runner_target:
            final_exit = runner_target
            exit_index, exit_ts, reason = index, bar.ts + FIFTEEN_MINUTES, "runner_target"
            break
        if index == last_index:
            final_exit = bar.close
            exit_index, exit_ts, reason = index, bar.ts + FIFTEEN_MINUTES, "time_exit"
            break
    fractions_closed = (0.50 if partial_1 else 0.0) + (0.20 if partial_2 else 0.0)
    remaining = max(0.0, 1.0 - fractions_closed)
    net = 0.0
    if partial_1:
        net += exit_value(0.50, entry, tp1, slip, fee)
    if partial_2:
        net += exit_value(0.20, entry, tp2, slip, fee)
    if remaining:
        net += exit_value(remaining, entry, final_exit, slip, fee)
    net_return = net / entry
    return Trade(signal.symbol, signal.signal_index, signal.signal_ts, signal.entry_ts,
                 entry, stop, tp1, tp2, runner_target, tp1_exit, tp2_exit, final_exit,
                 exit_index, exit_ts, reason, net_return, net_return * 100.0,
                 exit_index - signal.signal_index), None


def trade_metrics(trades: list[Trade]) -> dict:
    values = [trade.net_return for trade in sorted(trades, key=lambda item: (item.exit_ts, item.symbol, item.entry_ts))]
    wins = [value for value in values if value > 0]
    losses = [-value for value in values if value < 0]
    equity = peak = 1.0
    max_drawdown = 0.0
    for value in values:
        equity *= 1.0 + value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, 1.0 - equity / peak)
    return {
        "trades": len(values),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": len(wins) / len(values) if values else None,
        "total_net_return": sum(values),
        "compound_return": equity - 1.0,
        "average_net_return": sum(values) / len(values) if values else None,
        "profit_factor": sum(wins) / sum(losses) if losses else None,
        "max_drawdown": max_drawdown,
        "exit_reasons": dict(Counter(trade.exit_reason for trade in trades)),
    }


def allowed_symbols(config: dict) -> Optional[set[str]]:
    source = config.get("universe", {}).get("source")
    field = config.get("universe", {}).get("allowed_symbols_field")
    if not source or not field:
        return None
    try:
        payload = json.loads((ROOT / source).read_text(encoding="utf-8"))
        values = payload.get(field)
        return {str(value).upper() for value in values} if isinstance(values, list) else set()
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def choose_paths(data_dir: Path, allowed: Optional[set[str]]) -> list[tuple[str, Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in data_dir.glob("*_USDT_SWAP_5m_*.jsonl.gz"):
        symbol = symbol_from_path(path)
        if allowed is None or symbol in allowed:
            grouped[symbol].append(path)
    return [(symbol, sorted(paths)[-1]) for symbol, paths in sorted(grouped.items())]


def is_validation(ts: int) -> bool:
    return datetime.fromtimestamp(ts / 1000, UTC) >= SPLIT_UTC


def run(data_dir: Path, config_path: Path, output_dir: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    allowed = allowed_symbols(config)
    all_signals: list[Signal] = []
    all_trades: list[Trade] = []
    counts: Counter[str] = Counter()
    quality_totals: Counter[str] = Counter()
    sources = choose_paths(data_dir, allowed)
    for symbol, path in sources:
        bars_5m, quality = load_bars(path)
        quality_totals.update(quality)
        bars_15m = aggregate_15m(bars_5m)
        signals, local_counts = find_signals(symbol, bars_15m, config)
        counts.update(local_counts)
        all_signals.extend(signals)
        for signal in signals:
            trade, reason = simulate(signal, bars_15m, config)
            if trade is None:
                counts[f"trade_rejected_{reason}"] += 1
            else:
                all_trades.append(trade)
    output_dir.mkdir(parents=True, exist_ok=True)
    signal_rows = [asdict(signal) | {"signal_utc": iso(signal.signal_ts), "entry_utc": iso(signal.entry_ts)}
                   for signal in all_signals]
    trade_rows = [asdict(trade) | {"signal_utc": iso(trade.signal_ts), "entry_utc": iso(trade.entry_ts),
                                   "exit_utc": iso(trade.exit_ts)} for trade in all_trades]
    with (output_dir / "signals.csv").open("w", newline="", encoding="utf-8") as stream:
        if signal_rows:
            writer = csv.DictWriter(stream, fieldnames=list(signal_rows[0]))
            writer.writeheader(); writer.writerows(signal_rows)
    with (output_dir / "trades.csv").open("w", newline="", encoding="utf-8") as stream:
        if trade_rows:
            writer = csv.DictWriter(stream, fieldnames=list(trade_rows[0]))
            writer.writeheader(); writer.writerows(trade_rows)
    train_signals = [signal for signal in all_signals if not is_validation(signal.entry_ts)]
    validation_signals = [signal for signal in all_signals if is_validation(signal.entry_ts)]
    train_trades = [trade for trade in all_trades if not is_validation(trade.entry_ts)]
    validation_trades = [trade for trade in all_trades if is_validation(trade.entry_ts)]
    report = {
        "strategy": config["strategy"],
        "study": "sixty_day_pump_support_retest_failed_breakout_short",
        "validation_mode": "price_only",
        "parameters": config,
        "event_definition": "15m pivot high -> pivot support -> support retest -> strict prior-high breakout -> bearish next-bar failure",
        "data": {"directory": str(data_dir), "symbols": len(sources), "quality_totals": dict(quality_totals)},
        "counts": dict(counts),
        "results": {
            "signals": len(all_signals), "trades": len(all_trades),
            "unfilled_or_rejected": len(all_signals) - len(all_trades),
            "return_basis": "net_return_per_trade_relative_to_entry_notional",
            "overlapping_positions_serialized": False,
            **trade_metrics(all_trades),
            "train": {"signals": len(train_signals), "trades": len(train_trades), **trade_metrics(train_trades)},
            "validation": {"signals": len(validation_signals), "trades": len(validation_trades), **trade_metrics(validation_trades)},
        },
        "limitations": [
            "15m bars are aggregated from confirmed 5m candles; intrabar ordering remains unknown.",
            "The natural-language support/retest/failure pattern is represented by the documented pivot and tolerance rules.",
            "Runner target 15% and 96-bar expiry are explicit research assumptions because no runner target or expiry was supplied.",
            "Funding, liquidation, queue position, and portfolio contention are not modeled.",
        ],
    }
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.scan:
        parser.error("--scan is required")
    report = run(args.data_dir, args.config, args.output_dir)
    results = report["results"]
    print(json.dumps({"signals": results["signals"], "trades": results["trades"],
                      "win_rate": results["win_rate"], "profit_factor": results["profit_factor"],
                      "total_net_return": results["total_net_return"],
                      "compound_return": results["compound_return"],
                      "report": str(args.output_dir / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
