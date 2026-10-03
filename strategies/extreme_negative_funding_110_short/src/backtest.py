#!/usr/bin/env python3
"""Causal research runner for the extreme negative-funding 110% short.

Only confirmed, contiguous 5m candles are used. Funding is an explicit input
and is never inferred from prices or from another symbol.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


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
    quote_volume: float = 0.0


@dataclass(frozen=True)
class FundingSnapshot:
    symbol: str
    timestamp_ms: int
    funding_rate: float
    symbol_max_negative_rate: float


@dataclass(frozen=True)
class Signal:
    symbol: str
    index: int
    timestamp: int
    day_open: float
    running_high: float
    gain: float
    funding_rate: Optional[float]
    funding_cap: Optional[float]


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_index: int
    signal_ts: int
    entry_index: int
    entry_ts: int
    order_price: float
    entry: float
    initial_stop: float
    tp1: float
    tp2: float
    tp1_exit: Optional[float]
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


def report_path(path: Path) -> str:
    try:
        return path.resolve().relative_to(ROOT.resolve()).as_posix()
    except ValueError:
        return path.name


def load_bars(path: Path) -> tuple[list[Bar], dict[str, int]]:
    """Read confirmed 5m rows, deduplicate timestamps, and report quality."""
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
                opening, high, low, close, quote_volume = values
                if min(opening, high, low, close) <= 0 or quote_volume < 0:
                    raise ValueError("invalid positive OHLCV")
                if high < max(opening, close) or low > min(opening, close) or high < low:
                    raise ValueError("invalid OHLC")
                if ts in raw:
                    quality["duplicate_rows"] += 1
                raw[ts] = Bar(ts, opening, high, low, close, quote_volume)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                quality["invalid_rows"] += 1
    bars = [raw[ts] for ts in sorted(raw)]
    quality["confirmed_rows"] = len(bars)
    quality["timestamp_gaps"] = sum(
        1 for left, right in zip(bars, bars[1:]) if right.ts - left.ts != FIVE_MINUTES
    )
    return bars, dict(quality)


def load_funding(path: Optional[Path]) -> tuple[dict[str, list[FundingSnapshot]], str, int]:
    """Load explicit funding snapshots; missing input is a first-class state."""
    if path is None:
        return {}, "unavailable", 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return {}, "unavailable", 0
    rows = payload.get("snapshots", []) if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        return {}, "unavailable", 0
    result: dict[str, list[FundingSnapshot]] = defaultdict(list)
    invalid = 0
    for row in rows:
        if not isinstance(row, dict):
            invalid += 1
            continue
        symbol = str(row.get("symbol", "")).upper()
        ts = row.get("timestamp_ms")
        rate = finite(row.get("funding_rate", row.get("rate")))
        cap = finite(row.get("symbol_max_negative_rate", row.get("max_negative_rate")))
        try:
            ts = int(ts)
        except (TypeError, ValueError):
            ts = 0
        if not symbol or ts <= 0 or rate is None or cap is None:
            invalid += 1
            continue
        result[symbol].append(FundingSnapshot(symbol, ts, rate, cap))
    for values in result.values():
        values.sort(key=lambda item: item.timestamp_ms)
    return dict(result), ("available" if result else "unavailable"), invalid


def funding_at(
    symbol: str,
    asof_ms: int,
    funding: dict[str, list[FundingSnapshot]],
    max_age_ms: int,
) -> tuple[Optional[FundingSnapshot], str]:
    values = funding.get(symbol.upper(), [])
    eligible = [item for item in values if item.timestamp_ms <= asof_ms]
    if not eligible:
        return None, "funding_missing"
    snapshot = eligible[-1]
    if asof_ms - snapshot.timestamp_ms > max_age_ms:
        return None, "funding_stale"
    if snapshot.symbol_max_negative_rate >= 0:
        return None, "funding_cap_missing_or_nonnegative"
    if snapshot.funding_rate > snapshot.symbol_max_negative_rate:
        return None, "funding_not_at_symbol_max_negative"
    return snapshot, "eligible"


def day_signals(
    symbol: str,
    bars: list[Bar],
    config: dict,
    funding: dict[str, list[FundingSnapshot]],
    ignore_funding: bool = False,
) -> tuple[list[Signal], Counter[str], int]:
    """Generate at most one first-crossing signal per complete UTC day."""
    signal_config = config["signal"]
    gain_gate = float(signal_config["utc_day_gain_gt"])
    trigger_gain = float(signal_config["trigger_gain_pct"])
    max_age = int(float(signal_config["funding_max_age_minutes"]) * 60 * 1000)
    counts: Counter[str] = Counter()
    signals: list[Signal] = []
    day_open = 0.0
    running_high = 0.0
    day_valid = False
    qualified = False
    triggered = False
    last_day: Optional[int] = None
    previous_ts: Optional[int] = None
    for index, bar in enumerate(bars):
        day = bar.ts // DAY
        new_day = last_day is None or day != last_day
        gap = previous_ts is not None and bar.ts - previous_ts != FIVE_MINUTES
        if new_day:
            day_open = bar.open if bar.ts % DAY == 0 else 0.0
            day_valid = bar.ts % DAY == 0
            running_high = bar.high
            qualified = False
            triggered = False
            counts["utc_days_seen"] += 1
        elif gap:
            day_valid = False
            counts["days_invalidated_by_gap"] += 1
        if day_valid and day_open > 0:
            running_high = max(running_high, bar.high)
            gain = running_high / day_open - 1.0
            if gain > gain_gate:
                qualified = True
                counts["bars_above_80pct"] += 1
            if qualified and not triggered and running_high >= day_open * (1.0 + trigger_gain):
                triggered = True
                counts["price_trigger_candidates"] += 1
                if ignore_funding:
                    signals.append(Signal(symbol, index, bar.ts, day_open, running_high, gain, None, None))
                    counts["funding_bypassed_signals"] += 1
                else:
                    snapshot, reason = funding_at(symbol, bar.ts + FIVE_MINUTES, funding, max_age)
                    if snapshot is None:
                        counts[reason] += 1
                    else:
                        signals.append(Signal(symbol, index, bar.ts, day_open, running_high, gain,
                                              snapshot.funding_rate, snapshot.symbol_max_negative_rate))
                        counts["funding_eligible_signals"] += 1
        last_day = day
        previous_ts = bar.ts
    return signals, counts, len(signals)


def _exit_value(fraction: float, entry: float, raw_exit: float, slip: float, fee: float) -> float:
    effective = raw_exit * (1.0 + slip)
    return fraction * (entry - effective) - fraction * fee * (entry + effective)


def simulate(
    signal: Signal,
    bars: list[Bar],
    config: dict,
    entry_wait_bars: int = 1,
    initial_stop_pct: Optional[float] = None,
) -> tuple[Optional[Trade], Optional[str]]:
    """Simulate one signal with optional research-only entry/risk overrides.

    Defaults preserve the strategy configuration exactly. ``entry_wait_bars``
    counts complete 5-minute bars after the signal bar before activation;
    ``initial_stop_pct`` overrides only the initial stop distance.
    """
    entry_config = config["entry"]
    risk = config["risk"]
    costs = config["costs"]
    if entry_wait_bars < 1:
        return None, "invalid_entry_wait"
    order_price = signal.day_open * float(entry_config["limit_price_factor"])
    first = signal.index + entry_wait_bars
    if first >= len(bars) or bars[first].ts != signal.timestamp + entry_wait_bars * FIVE_MINUTES:
        return None, "entry_gap"
    if any(bars[index].ts - bars[index - 1].ts != FIVE_MINUTES
           for index in range(signal.index + 1, first + 1)):
        return None, "entry_gap"
    fill_index: Optional[int] = None
    raw_entry: Optional[float] = None
    fill_at_open = False
    for index in range(first, len(bars)):
        bar = bars[index]
        if bar.ts // DAY != signal.timestamp // DAY:
            break
        if index > first and bar.ts - bars[index - 1].ts != FIVE_MINUTES:
            return None, "entry_gap"
        if bar.open >= order_price:
            fill_index, raw_entry, fill_at_open = index, bar.open, True
            break
        if bar.high >= order_price:
            fill_index, raw_entry, fill_at_open = index, order_price, False
            break
    if fill_index is None or raw_entry is None:
        return None, "limit_unfilled"
    slip = float(costs["slippage_one_way"])
    fee = float(costs["fee_rate_one_way"])
    entry = raw_entry * (1.0 - slip)
    stop_pct = (float(risk["initial_stop_above_entry_pct"])
                if initial_stop_pct is None else float(initial_stop_pct))
    if stop_pct < 0:
        return None, "invalid_stop"
    stop = entry * (1.0 + stop_pct)
    tp1 = entry * (1.0 - float(risk["tp1_favorable_move_pct"]))
    tp2 = entry * (1.0 - float(risk["tp1_favorable_move_pct"]) -
                   float(risk["tp2_additional_favorable_move_pct_from_entry"]))
    if tp2 <= 0:
        return None, "invalid_target"
    start_exit = fill_index if fill_at_open else fill_index + 1
    if start_exit >= len(bars):
        return None, "data_end_before_exit"
    remaining = 1.0
    partial = 0.0
    tp1_exit: Optional[float] = None
    exit_index = start_exit
    exit_ts = bars[start_exit].ts + FIVE_MINUTES
    final_exit = bars[start_exit].close
    reason = "data_end"
    previous_index = fill_index
    for index in range(start_exit, len(bars)):
        bar = bars[index]
        if bar.ts // DAY < signal.timestamp // DAY:
            break
        if index > previous_index and bar.ts - bars[index - 1].ts != FIVE_MINUTES:
            final_exit = bars[index - 1].close
            exit_index, exit_ts, reason = index - 1, bars[index - 1].ts + FIVE_MINUTES, "data_gap"
            break
        previous_index = index
        # The active stop was set before this bar opened. Stop wins ties.
        if bar.open >= stop:
            final_exit = bar.open
            exit_index, exit_ts = index, bar.ts
            reason = "stop_gap" if bar.open > stop else ("breakeven_stop_gap" if partial else "stop_gap")
            break
        if bar.high >= stop:
            final_exit = stop
            exit_index, exit_ts = index, bar.ts + FIVE_MINUTES
            reason = "breakeven_stop" if partial else "stop"
            break
        if partial == 0.0 and bar.low <= tp1:
            partial = 0.5
            remaining = 0.5
            tp1_exit = tp1
            # Breakeven is active from the next candle, not retroactively.
            stop = entry
        if partial and bar.low <= tp2:
            final_exit = tp2
            exit_index, exit_ts, reason = index, bar.ts + FIVE_MINUTES, "target_2"
            break
        if index == len(bars) - 1 or (index + 1 < len(bars) and bars[index + 1].ts // DAY != signal.timestamp // DAY):
            final_exit = bar.close
            exit_index, exit_ts, reason = index, bar.ts + FIVE_MINUTES, "utc_day_end"
            break
    else:
        final_exit = bars[-1].close
        exit_index, exit_ts, reason = len(bars) - 1, bars[-1].ts + FIVE_MINUTES, "data_end"
    if reason.startswith("stop") or reason.startswith("breakeven_stop"):
        net = _exit_value(partial, entry, tp1, slip, fee) + _exit_value(remaining, entry, final_exit, slip, fee) if partial else _exit_value(1.0, entry, final_exit, slip, fee)
    elif reason == "target_2":
        net = _exit_value(0.5, entry, tp1, slip, fee) + _exit_value(0.5, entry, tp2, slip, fee)
    else:
        net = _exit_value(partial, entry, tp1, slip, fee) + _exit_value(remaining, entry, final_exit, slip, fee) if partial else _exit_value(1.0, entry, final_exit, slip, fee)
    net_return = net / entry
    return Trade(signal.symbol, signal.index, signal.timestamp, fill_index, bars[fill_index].ts,
                 order_price, entry, entry * (1.0 + stop_pct), tp1, tp2, tp1_exit, final_exit,
                 exit_index, exit_ts, reason, net_return, net_return * 100.0, exit_index - fill_index + 1), None


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as stream:
        if not rows:
            stream.write("\n")
            return
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


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
    """Use the repository's canonical altcoin pool when configured."""
    source = config.get("universe", {}).get("source")
    field = config.get("universe", {}).get("allowed_symbols_field")
    if not source or not field:
        return None
    source_path = ROOT / source
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        values = payload.get(field)
        if not isinstance(values, list):
            return set()
        return {str(value).upper() for value in values}
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return set()


def choose_paths(data_dir: Path, allowed: Optional[set[str]] = None) -> list[tuple[str, Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in data_dir.glob("*_USDT_SWAP_5m_*.jsonl.gz"):
        symbol = symbol_from_path(path)
        if allowed is not None and symbol not in allowed:
            continue
        grouped[symbol].append(path)
    return [(symbol, sorted(paths)[-1]) for symbol, paths in sorted(grouped.items())]


def run(
    data_dir: Path,
    config_path: Path,
    output_dir: Path,
    funding_path: Optional[Path],
    ignore_funding: bool = False,
) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    funding, funding_status, invalid_funding_rows = load_funding(funding_path)
    allowed = allowed_symbols(config)
    output_dir.mkdir(parents=True, exist_ok=True)
    all_signals: list[Signal] = []
    all_trades: list[Trade] = []
    counts: Counter[str] = Counter()
    sources: list[dict] = []
    for symbol, path in choose_paths(data_dir, allowed):
        bars, quality = load_bars(path)
        signals, local_counts, _ = day_signals(symbol, bars, config, funding, ignore_funding=ignore_funding)
        counts.update(local_counts)
        all_signals.extend(signals)
        for signal in signals:
            trade, reason = simulate(signal, bars, config)
            if trade is None:
                counts[f"trade_rejected_{reason}"] += 1
            else:
                all_trades.append(trade)
        sources.append({"symbol": symbol, "file": report_path(path), "bars": len(bars), "quality": quality,
                        "signals": len(signals)})
    signal_rows = [asdict(signal) | {"signal_utc": iso(signal.timestamp)} for signal in all_signals]
    trade_rows = [asdict(trade) | {"signal_utc": iso(trade.signal_ts), "entry_utc": iso(trade.entry_ts),
                                   "exit_utc": iso(trade.exit_ts)} for trade in all_trades]
    write_csv(output_dir / "signals.csv", signal_rows)
    write_csv(output_dir / "trades.csv", trade_rows)
    metrics = trade_metrics(all_trades)
    report = {
        "strategy": config["strategy"],
        "config": config,
        "validation_mode": "price_only_ignore_funding" if ignore_funding else "funding_gated",
        "funding": {"status": "ignored" if ignore_funding else funding_status,
                     "mode": "bypassed_for_validation" if ignore_funding else "required_gate",
                     "input": str(funding_path) if funding_path else None,
                     "invalid_rows": invalid_funding_rows,
                     "message": ("Funding gate intentionally bypassed for price-only validation."
                                 if ignore_funding else
                                 ("Funding snapshots unavailable; no candidate can pass the funding gate."
                                  if funding_status != "available" else None))},
        "data": {"directory": str(data_dir), "symbols": len(sources), "sources": sources},
        "counts": dict(counts),
        "results": {"signals": len(all_signals), "trades": len(all_trades),
                    "return_basis": "net_return_per_trade_relative_to_entry_notional",
                    "overlapping_positions_serialized": False,
                    **metrics},
        "limitations": ["Funding is an entry filter only; funding PnL is not applied.",
                        "OHLC cannot establish intrabar order; stop is checked before targets.",
                        "No queue, partial fill beyond the defined 50% exit, liquidation, or portfolio contention model."],
    }
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="scan 5m data and write this strategy's report")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    funding_group = parser.add_mutually_exclusive_group()
    funding_group.add_argument("--funding", type=Path, help="explicit funding snapshot JSON")
    funding_group.add_argument("--ignore-funding", action="store_true",
                               help="bypass the funding gate for price-only validation")
    args = parser.parse_args()
    if not args.scan:
        parser.error("--scan is required; this command never runs without an explicit research scan")
    report = run(args.data_dir, args.config, args.output_dir, args.funding,
                 ignore_funding=args.ignore_funding)
    print(json.dumps({"funding_status": report["funding"]["status"],
                      "funding_mode": report["funding"]["mode"],
                      "signals": report["results"]["signals"],
                      "trades": report["results"]["trades"], "report": str(args.output_dir / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
