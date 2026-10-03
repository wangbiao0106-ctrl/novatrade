#!/usr/bin/env python3
"""Causal 15m failed-breakout reversal short backtest.

Confirmed 5m candles are aggregated into 15m bars. A setup is observed only
after its close; a later confirmation close is required before the next 15m
open can be sold short. The implementation is intentionally dependency-free.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

FIVE_MINUTES = 5 * 60 * 1000
FIFTEEN_MINUTES = 15 * 60 * 1000
DAY_15M = 96
UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/strategy.json"


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class Setup:
    symbol: str
    index: int
    ts: int
    atr: float
    ema20: float
    ema60: float
    rsi: float
    volume_ratio: float
    breakout_atr: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    setup_ts: int
    confirmation_ts: int
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    tp1: float
    tp2: float
    net_r: float
    exit_reason: str
    bars_held: int


def finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def symbol_from_path(path: Path) -> str:
    return path.name.split("_USDT_SWAP_5m_", 1)[0].upper()


def load_bars(path: Path) -> list[Bar]:
    opener = gzip.open if path.suffix == ".gz" else open
    by_ts: dict[int, Bar] = {}
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get("confirmed") is not True:
                    continue
                ts = int(row["timestamp_ms"])
                values = [finite(row.get(key)) for key in ("open", "high", "low", "close", "quote_volume")]
                if ts <= 0 or any(value is None for value in values):
                    continue
                o, h, low, close, volume = values
                if min(o, h, low, close) <= 0 or volume < 0 or h < max(o, close) or low > min(o, close) or h < low:
                    continue
                by_ts[ts] = Bar(ts, o, h, low, close, volume)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return [by_ts[ts] for ts in sorted(by_ts)]


def aggregate_15m(bars: list[Bar]) -> list[Bar]:
    groups: dict[int, dict[int, Bar]] = defaultdict(dict)
    for bar in bars:
        bucket = bar.ts // FIFTEEN_MINUTES * FIFTEEN_MINUTES
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


def contiguous(bars: list[Bar], index: int, count: int) -> bool:
    return index >= count - 1 and all(
        bars[position].ts - bars[position - 1].ts == FIFTEEN_MINUTES
        for position in range(index - count + 1, index + 1)
    )


def ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return result
    seed = sum(values[:period]) / period
    result[period - 1] = seed
    alpha = 2.0 / (period + 1.0)
    previous = seed
    for index in range(period, len(values)):
        previous = (values[index] - previous) * alpha + previous
        result[index] = previous
    return result


def atr_values(bars: list[Bar], period: int) -> list[float]:
    result = [0.0] * len(bars)
    ranges: list[float] = []
    for index, bar in enumerate(bars):
        previous = bars[index - 1].close if index else bar.open
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
        if index >= period - 1:
            result[index] = sum(ranges[index - period + 1:index + 1]) / period
    return result


def rsi_values(closes: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(closes)
    if len(closes) <= period:
        return result
    gains = sum(max(closes[i] - closes[i - 1], 0.0) for i in range(1, period + 1)) / period
    losses = sum(max(closes[i - 1] - closes[i], 0.0) for i in range(1, period + 1)) / period
    result[period] = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
    for index in range(period + 1, len(closes)):
        change = closes[index] - closes[index - 1]
        gains = (gains * (period - 1) + max(change, 0.0)) / period
        losses = (losses * (period - 1) + max(-change, 0.0)) / period
        result[index] = 100.0 if losses == 0 else 100.0 - 100.0 / (1.0 + gains / losses)
    return result


def wick_ratio(bar: Bar) -> float:
    return (bar.high - max(bar.open, bar.close)) / max(bar.high - bar.low, 1e-12)


def body_ratio(bar: Bar) -> float:
    return abs(bar.open - bar.close) / max(bar.high - bar.low, 1e-12)


def close_position(bar: Bar) -> float:
    return (bar.close - bar.low) / max(bar.high - bar.low, 1e-12)


def confirmation_matches(setup_bar: Bar, confirmation_bar: Bar, confirmation_ema20: float,
                         setup_close: float, confirmation_atr: float, config: dict) -> bool:
    signal = config["signal"]
    if confirmation_bar.close >= setup_bar.high:
        return False
    if confirmation_bar.close >= confirmation_ema20 or confirmation_bar.close >= setup_close:
        return False
    if confirmation_bar.close >= setup_close - float(signal["confirmation_atr_break_min"]) * confirmation_atr:
        if confirmation_bar.low >= setup_bar.low:
            return False
    return confirmation_bar.close < confirmation_bar.open


def build_setups(symbol: str, bars: list[Bar], config: dict) -> list[Setup]:
    signal = config["signal"]
    fast = int(signal["ema_fast_period"])
    slow = int(signal["ema_slow_period"])
    atr_period = int(signal["atr_period"])
    rsi_period = int(signal["rsi_period"])
    volume_period = int(signal["volume_period"])
    lookback = int(signal["prior_high_lookback"])
    quote_volume_min = float(config["universe"].get("quote_volume24h_min_usdt", 0.0))
    closes = [bar.close for bar in bars]
    volumes = [bar.quote_volume for bar in bars]
    fast_ema = ema(closes, fast)
    slow_ema = ema(closes, slow)
    atrs = atr_values(bars, atr_period)
    rsis = rsi_values(closes, rsi_period)
    warmup = max(DAY_15M, slow, atr_period, rsi_period + 1, volume_period, lookback)
    setups: list[Setup] = []
    for index in range(warmup, len(bars) - 1):
        if not contiguous(bars, index, warmup + 1):
            continue
        bar = bars[index]
        atr = atrs[index]
        e20, e60, rsi, previous_rsi = fast_ema[index], slow_ema[index], rsis[index], rsis[index - 1]
        if atr <= 0 or e20 is None or e60 is None or rsi is None or previous_rsi is None:
            continue
        prior_high = max(item.high for item in bars[index - lookback:index])
        quote_volume24h = sum(item.quote_volume for item in bars[index - DAY_15M + 1:index + 1])
        if quote_volume24h < quote_volume_min:
            continue
        prior_volume = sum(volumes[index - volume_period:index]) / volume_period
        volume_ratio = bar.quote_volume / prior_volume if prior_volume > 0 else 0.0
        breakout_atr = (bar.high - prior_high) / atr
        if not (
            e20 > e60 and bar.close > e60
            and breakout_atr >= float(signal["breakout_min_atr"])
            and bar.close < bar.open
            and body_ratio(bar) >= float(signal["body_ratio_min"])
            and wick_ratio(bar) >= float(signal["upper_wick_ratio_min"])
            and close_position(bar) <= float(signal["close_position_max"])
            and (bar.high - bar.low) >= float(signal["range_atr_min"]) * atr
            and volume_ratio >= float(signal["volume_multiple_min"])
            and rsi >= float(signal["rsi_min"])
            and (not signal["rsi_must_fall"] or rsi < previous_rsi)
            and abs(bar.close - e20) <= float(signal["ema20_distance_atr_max"]) * atr
        ):
            continue
        setups.append(Setup(symbol, index, bar.ts, atr, e20, e60, rsi, volume_ratio, breakout_atr))
    return setups


def find_confirmation(setup: Setup, bars: list[Bar], fast_ema: list[float | None], atrs: list[float], config: dict) -> int | None:
    signal = config["signal"]
    last = min(len(bars) - 2, setup.index + int(signal["confirmation_bars"]))
    for index in range(setup.index + 1, last + 1):
        if bars[index].ts - bars[index - 1].ts != FIFTEEN_MINUTES:
            return None
        if bars[index].close >= bars[setup.index].high:
            return None
        ema20 = fast_ema[index]
        if ema20 is not None and confirmation_matches(
            bars[setup.index], bars[index], ema20, bars[setup.index].close, atrs[index], config
        ):
            return index
    return None


def leg_r(entry: float, exit_price: float, fraction: float, risk: float, fee: float, slip: float) -> float:
    effective_exit = exit_price * (1.0 + slip)
    return fraction * ((entry - effective_exit) - fee * (entry + exit_price)) / risk


def simulate(setup: Setup, confirmation_index: int, bars: list[Bar], config: dict) -> Trade | None:
    risk_config = config["risk"]
    costs = config["costs"]
    entry_index = confirmation_index + 1
    if entry_index >= len(bars) or bars[entry_index].ts - bars[confirmation_index].ts != FIFTEEN_MINUTES:
        return None
    entry = bars[entry_index].open * (1.0 - float(costs["slippage_one_way"]))
    stop = bars[setup.index].high + float(risk_config["stop_atr"]) * setup.atr
    risk = stop - entry
    if not (float(risk_config["min_risk_atr"]) * setup.atr <= risk <= float(risk_config["max_risk_atr"]) * setup.atr):
        return None
    tp1 = entry - float(risk_config["tp1_r"]) * risk
    tp2 = entry - float(risk_config["tp2_r"]) * risk
    fee = float(costs["fee_rate_one_way"])
    slip = float(costs["slippage_one_way"])
    first_fraction = float(risk_config["tp1_fraction"])
    remaining = 1.0
    current_stop = stop
    tp1_done = False
    net = 0.0
    last = min(len(bars) - 1, entry_index + int(risk_config["max_hold_bars"]) - 1)
    exit_index = last
    exit_price = bars[last].close
    reason = "时间离场"
    gap_exit = False
    for index in range(entry_index, last + 1):
        bar = bars[index]
        if index > entry_index and bar.ts - bars[index - 1].ts != FIFTEEN_MINUTES:
            exit_index, exit_price, reason = index - 1, bars[index - 1].close, "数据缺口离场"
            gap_exit = True
            break
        if bar.open >= current_stop:
            net += leg_r(entry, bar.open, remaining, risk, fee, slip)
            exit_index, exit_price, reason = index, bar.open, "止损(跳空)"
            remaining = 0.0
            break
        if bar.high >= current_stop:
            net += leg_r(entry, current_stop, remaining, risk, fee, slip)
            exit_index, exit_price, reason = index, current_stop, "止损"
            remaining = 0.0
            break
        if not tp1_done and bar.low <= tp1:
            net += leg_r(entry, tp1, first_fraction, risk, fee, slip)
            remaining -= first_fraction
            tp1_done = True
            current_stop = entry
            exit_index, exit_price, reason = index, tp1, "TP1后管理"
            # OHLC cannot reveal whether a same-bar rebound hit the new
            # breakeven stop. Activate that protection on the next bar.
            continue
        if tp1_done and bar.low <= tp2:
            net += leg_r(entry, tp2, remaining, risk, fee, slip)
            exit_index, exit_price, reason = index, tp2, "TP2"
            remaining = 0.0
            break
    if remaining > 0 and tp1_done and not gap_exit:
        exit_index, exit_price, reason = last, bars[last].close, "TP1后时间离场"
    if remaining > 0:
        net += leg_r(entry, exit_price, remaining, risk, fee, slip)
    return Trade(setup.symbol, setup.ts, bars[confirmation_index].ts, bars[entry_index].ts,
                 bars[exit_index].ts, entry, exit_price, stop, tp1, tp2, net, reason,
                 exit_index - entry_index + 1)


def choose_paths(data_dir: Path, config: dict) -> list[tuple[str, Path]]:
    excluded = {str(item).upper() for item in config["universe"]["excluded_symbols"]}
    canonical = ROOT / "strategies/sweep_reversal_short/config/universe.json"
    allowed: set[str] | None = None
    try:
        allowed = {str(item).upper() for item in json.loads(canonical.read_text()).get("altcoins", [])}
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in data_dir.glob("*_USDT_SWAP_5m_*.jsonl.gz"):
        symbol = symbol_from_path(path)
        if symbol not in excluded and (allowed is None or symbol in allowed):
            grouped[symbol].append(path)
    return [(symbol, sorted(paths, key=lambda item: item.name)[-1]) for symbol, paths in sorted(grouped.items())]


def backtest(data_dir: Path, config: dict) -> tuple[list[Trade], dict]:
    candidates: list[Trade] = []
    symbols = bars_loaded = 0
    for symbol, path in choose_paths(data_dir, config):
        bars = aggregate_15m(load_bars(path))
        if len(bars) < int(config["universe"]["minimum_contiguous_15m_bars"]):
            continue
        symbols += 1
        bars_loaded += len(bars)
        setups = build_setups(symbol, bars, config)
        closes = [bar.close for bar in bars]
        fast_ema = ema(closes, int(config["signal"]["ema_fast_period"]))
        atrs = atr_values(bars, int(config["signal"]["atr_period"]))
        for setup in setups:
            confirmation = find_confirmation(setup, bars, fast_ema, atrs, config)
            if confirmation is not None:
                trade = simulate(setup, confirmation, bars, config)
                if trade is not None:
                    candidates.append(trade)
    ordered_candidates = sorted(candidates, key=lambda trade: (trade.entry_ts, trade.symbol))
    selected: list[Trade] = []
    last_global_exit = -10**30
    last_exit_by_symbol: dict[str, int] = {}
    daily_realized_r: dict[str, float] = defaultdict(float)
    risk_pct = float(config["position_management"]["risk_per_trade_pct"])
    daily_loss_limit_pct = float(config["position_management"]["daily_realized_loss_limit_pct"])
    daily_loss_limit_r = -daily_loss_limit_pct / risk_pct if risk_pct > 0 else 0.0
    cooldown = int(config["risk"]["cooldown_bars"]) * FIFTEEN_MINUTES
    for trade in ordered_candidates:
        # The strategy instance owns one position globally; the symbol-level
        # cooldown also prevents repeated entries in one reversal sequence.
        if trade.entry_ts <= last_global_exit:
            continue
        previous_symbol_exit = last_exit_by_symbol.get(trade.symbol)
        if previous_symbol_exit is not None and trade.entry_ts < previous_symbol_exit + cooldown:
            continue
        entry_day = datetime.fromtimestamp(trade.entry_ts / 1000, UTC).date().isoformat()
        if daily_realized_r[entry_day] <= daily_loss_limit_r:
            continue
        selected.append(trade)
        last_global_exit = trade.exit_ts
        last_exit_by_symbol[trade.symbol] = trade.exit_ts
        exit_day = datetime.fromtimestamp(trade.exit_ts / 1000, UTC).date().isoformat()
        daily_realized_r[exit_day] += trade.net_r
    ordered = sorted(selected, key=lambda trade: (trade.exit_ts, trade.entry_ts, trade.symbol))
    values = [trade.net_r for trade in ordered]
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    equity = peak = max_drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    report = {
        "strategy": config["strategy"], "version": config["version"],
        "generated_at": datetime.now(UTC).isoformat(), "symbols": symbols,
        "bars_15m": bars_loaded, "trades": len(values),
        "wins": sum(value > 0 for value in values),
        "win_rate": sum(value > 0 for value in values) / len(values) if values else 0.0,
        "total_r": sum(values), "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
        "max_drawdown_r": max_drawdown, "risk_budget_return_pct": sum(values) * float(config["position_management"]["risk_per_trade_pct"]),
        "cost_assumptions": config["costs"],
        "daily_loss_limit_mode": "realized_r_at_exit",
        "status": "research_only",
    }
    return ordered, report


def write_results(trades: list[Trade], report: dict, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output_dir / "trades.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(asdict(trades[0]).keys()) if trades else list(Trade.__dataclass_fields__.keys()))
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in trades)


def main() -> int:
    parser = argparse.ArgumentParser(description="Backtest failed-breakout 15m confirmation shorts.")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "results")
    parser.add_argument("--scan", action="store_true", help="compatibility flag; the command always runs the full scan")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    trades, report = backtest(args.data_dir, config)
    write_results(trades, report, args.output_dir)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
