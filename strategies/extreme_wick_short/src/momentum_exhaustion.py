#!/usr/bin/env python3
"""Causal 15m momentum-exhaustion short backtest.

The input is confirmed OKX 5m data.  Complete 15m candles are built before
the signal is evaluated.  A signal is confirmed on the exhaustion candle and
the short is entered at the following 15m open, so no part of the entry uses
future data.
"""
from __future__ import annotations

import argparse
import csv
import json
import statistics
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from extreme_wick_short import Bar, FIFTEEN_MINUTES

# Keep imports compatible with direct execution from this directory.
from extreme_wick_short import aggregate_15m, choose_paths, excluded_symbols, load_bars, load_config

UTC = timezone.utc
DAY_15M = 96
THIRTY_DAYS_15M = 30 * DAY_15M
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "strategy.json"
DEFAULT_DATA = Path(__file__).resolve().parents[3] / "data/kline/okx/swap/5m"
ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Event:
    symbol: str
    index: int
    timestamp: int
    intraday_gain: float
    low_multiple: float
    rsi: float
    rsi_delta: float
    upper_wick_ratio: float
    close_position: float
    volume_ratio: float
    breakout_pct: float
    atr_pct: float
    anchor_high: float
    atr: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_ts: int
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    target: float
    risk: float
    net_r: float
    leveraged_return_pct: float
    exit_reason: str
    bars_held: int


def wilder_rsi(closes: list[float], period: int) -> list[float | None]:
    """Return RSI using Wilder smoothing, with no value before warm-up."""
    result: list[float | None] = [None] * len(closes)
    if period <= 0 or len(closes) <= period:
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


def atr_values(bars: list[Bar], period: int) -> list[float]:
    result = [0.0] * len(bars)
    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        previous = bars[index - 1].close if index else bar.open
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
        if index >= period - 1:
            result[index] = sum(true_ranges[index - period + 1:index + 1]) / period
    return result


def simple_average(values: list[float], period: int) -> list[float]:
    result = [0.0] * len(values)
    running = 0.0
    for index, value in enumerate(values):
        running += value
        if index >= period:
            running -= values[index - period]
        if index >= period - 1:
            result[index] = running / period
    return result


def prior_rolling_low(values: list[float], window: int) -> list[float]:
    """Causal rolling minimum that excludes the current candle."""
    result = [0.0] * len(values)
    queue: deque[int] = deque()
    for index, value in enumerate(values):
        if queue:
            result[index] = values[queue[0]]
        while queue and queue[0] < index - window:
            queue.popleft()
        while queue and values[queue[-1]] >= value:
            queue.pop()
        queue.append(index)
    return result


def load_series(data_dir: Path, config_path: Path, config: dict):
    canonical = ROOT / "strategies" / "sweep_reversal_short" / "config" / "universe.json"
    try:
        allowed = {str(x).upper() for x in json.loads(canonical.read_text()).get("altcoins", [])}
    except (OSError, ValueError, json.JSONDecodeError):
        allowed = None
    excluded = excluded_symbols(config_path, config)
    for symbol, path in choose_paths(data_dir, excluded, allowed):
        bars = aggregate_15m(load_bars(path))
        if len(bars) >= THIRTY_DAYS_15M + 60:
            yield symbol, bars


def load_all_series(data_dir: Path, config_path: Path, config: dict) -> list[tuple[str, list[Bar]]]:
    """Read and aggregate the complete research universe once.

    Keeping this materialized list as the backtest input makes later signal
    and parameter passes reuse the same in-memory candles instead of opening
    every gzip export again.
    """
    return list(load_series(data_dir, config_path, config))


def build_events(symbol: str, bars: list[Bar], config: dict) -> list[Event]:
    observation = config["observation_filter"]
    params = config["signal_parameters"]
    gain_min = float(observation["intraday_gain_gt"])
    low_multiple_min = float(observation["thirty_day_low_multiple_min"])
    rsi_period = int(params["rsi_period"])
    volume_period = int(params["volume_period"])
    breakout_window = int(params["breakout_lookback_bars"])
    atr_period = int(params["atr_period"])
    closes = [bar.close for bar in bars]
    volumes = [bar.quote_volume for bar in bars]
    rsis = wilder_rsi(closes, rsi_period)
    atrs = atr_values(bars, atr_period)
    volume_means = simple_average(volumes, volume_period)
    lows = prior_rolling_low([bar.low for bar in bars], THIRTY_DAYS_15M)
    gap_prefix = [0]
    for index in range(1, len(bars)):
        gap_prefix.append(gap_prefix[-1] + int(bars[index].ts - bars[index - 1].ts != FIFTEEN_MINUTES))
    events: list[Event] = []
    warmup = max(THIRTY_DAYS_15M, DAY_15M, rsi_period + 1, volume_period, breakout_window, atr_period)
    for index in range(warmup, len(bars) - int(config["risk"]["max_hold_bars"]) - 1):
        window_start = index - THIRTY_DAYS_15M + 1
        if gap_prefix[index + 1] - gap_prefix[window_start] > 0:
            continue
        prior_close = bars[index - DAY_15M].close
        low30 = lows[index]
        if prior_close <= 0 or low30 <= 0:
            continue
        intraday_gain = max(bars[index].high, bars[index].close) / prior_close - 1.0
        low_multiple = max(bars[index].high, bars[index].close) / low30
        if intraday_gain <= gain_min or low_multiple < low_multiple_min:
            continue
        bar = bars[index]
        bar_range = max(bar.high - bar.low, 1e-12)
        rsi = rsis[index]
        previous_rsi = rsis[index - 1]
        if rsi is None or previous_rsi is None or atrs[index] <= 0:
            continue
        prior_high = max(item.high for item in bars[index - breakout_window:index])
        events.append(Event(
            symbol, index, bar.ts, intraday_gain, low_multiple, rsi, rsi - previous_rsi,
            (bar.high - max(bar.open, bar.close)) / bar_range,
            (bar.close - bar.low) / bar_range,
            volumes[index] / volume_means[index] if volume_means[index] > 0 else 0.0,
            bar.high / prior_high - 1.0 if prior_high > 0 else 0.0,
            atrs[index] / bar.close if bar.close > 0 else 0.0, bar.high, atrs[index],
        ))
    return events


def qualifies(event: Event, bars: list[Bar], config: dict) -> bool:
    params = config["signal_parameters"]
    bar = bars[event.index]
    return (
        bar.close < bar.open
        and event.upper_wick_ratio >= float(params["upper_wick_min"])
        and event.close_position <= float(params["close_position_max"])
        and event.rsi >= float(params["rsi_min"])
        and (not params["rsi_must_fall"] or event.rsi_delta < 0)
        and event.volume_ratio >= float(params["volume_multiple"])
        and event.breakout_pct >= float(params["breakout_min_pct"])
    )


def net_r(entry: float, exit_price: float, risk: float, fee: float, slippage: float) -> float:
    effective_exit = exit_price * (1.0 + slippage)
    return (entry - effective_exit - fee * (entry + effective_exit)) / risk


def simulate(event: Event, bars: list[Bar], config: dict) -> Trade:
    params = config["risk"]
    costs = config["costs"]
    entry_index = event.index + 1
    entry = bars[entry_index].open * (1.0 - float(costs["slippage_one_way"]))
    stop = event.anchor_high + float(params["stop_atr"]) * event.atr
    risk = stop - entry
    target = entry - float(params["target_r"]) * risk
    last = min(len(bars) - 1, entry_index + int(params["max_hold_bars"]) - 1)
    exit_index = last
    exit_price = bars[last].close
    reason = "时间离场"
    for index in range(entry_index, last + 1):
        bar = bars[index]
        if bar.open >= stop:
            exit_index, exit_price, reason = index, bar.open, "止损(跳空)"
            break
        if bar.high >= stop:
            exit_index, exit_price, reason = index, stop, "止损"
            break
        if bar.low <= target:
            exit_index, exit_price, reason = index, target, "止盈"
            break
    nr = net_r(entry, exit_price, risk, float(costs["fee_rate_one_way"]), float(costs["slippage_one_way"]))
    leveraged_return = ((entry - exit_price * (1.0 + float(costs["slippage_one_way"]))) / entry
                        - float(costs["fee_rate_one_way"]) * (1.0 + exit_price / entry)) * float(config["position_management"]["leverage"]) * 100.0
    return Trade(event.symbol, event.timestamp, bars[entry_index].ts, bars[exit_index].ts, entry,
                 exit_price, stop, target, risk, nr, leveraged_return, reason, exit_index - entry_index + 1)


def metrics(trades: list[Trade], leverage: float) -> dict:
    ordered = sorted(trades, key=lambda trade: (trade.exit_ts, trade.entry_ts, trade.symbol))
    values = [trade.net_r for trade in ordered]
    leveraged = [trade.leveraged_return_pct for trade in ordered]
    equity = peak = drawdown = 0.0
    for value in leveraged:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    wins = sum(value > 0 for value in values)
    return {
        "trades": len(values), "wins": wins, "win_rate": wins / len(values) if values else 0.0,
        "total_r": sum(values), "avg_net_r": statistics.fmean(values) if values else 0.0,
        "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
        "total_leveraged_return_pct": sum(leveraged),
        "avg_leveraged_return_pct": statistics.fmean(leveraged) if leveraged else 0.0,
        "max_drawdown_leveraged_pct": drawdown, "leverage": leverage,
        "symbols": len({trade.symbol for trade in trades}),
        "exit_reasons": dict(sorted({reason: sum(t.exit_reason == reason for t in trades)
                                      for reason in {t.exit_reason for t in trades}}.items())),
    }


def grouped_metrics(trades: list[Trade], key, leverage: float) -> dict[str, dict]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        groups[str(key(trade))].append(trade)
    return {group: metrics(groups[group], leverage) for group in sorted(groups)}


def run(data_dir: Path, config_path: Path, output_dir: Path) -> dict:
    config = load_config(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    all_events: list[Event] = []
    all_trades: list[Trade] = []
    symbols = 0
    starts: list[int] = []
    ends: list[int] = []
    cooldown = int(config["risk"]["cooldown_bars"])
    qualified_signals = 0
    # Deliberately materialize the full universe before any indicator or
    # trade loop.  Parameter scans can now reuse this list without disk I/O.
    series = load_all_series(data_dir, config_path, config)
    for symbol, bars in series:
        symbols += 1
        starts.append(bars[0].ts)
        ends.append(bars[-1].ts)
        events = build_events(symbol, bars, config)
        all_events.extend(events)
        next_allowed = -1
        for event in events:
            if not qualifies(event, bars, config):
                continue
            qualified_signals += 1
            if event.index <= next_allowed:
                continue
            stop = event.anchor_high + float(config["risk"]["stop_atr"]) * event.atr
            entry = bars[event.index + 1].open * (1.0 - float(config["costs"]["slippage_one_way"]))
            risk_atr = (stop - entry) / event.atr if event.atr > 0 else 0.0
            if risk_atr < float(config["risk"]["min_risk_atr"]) or risk_atr > float(config["risk"]["max_risk_atr"]):
                continue
            last_index = min(len(bars) - 1, event.index + int(config["risk"]["max_hold_bars"]))
            if any(bars[cursor].ts - bars[cursor - 1].ts != FIFTEEN_MINUTES
                   for cursor in range(event.index + 1, last_index + 1)):
                continue
            trade = simulate(event, bars, config)
            all_trades.append(trade)
            exit_index = next((i for i, bar in enumerate(bars) if bar.ts == trade.exit_ts), event.index + 1)
            next_allowed = exit_index + cooldown
    leverage = float(config["position_management"]["leverage"])
    report = {
        "strategy": config["strategy"], "version": config["version"],
        "data": {"symbols": symbols, "start": min(starts) if starts else None,
                  "end": max(ends) if ends else None, "timeframe_minutes": 15},
        "observation_pool": {"events": len(all_events), "intraday_gain_gt": config["observation_filter"]["intraday_gain_gt"],
                             "thirty_day_low_multiple_min": config["observation_filter"]["thirty_day_low_multiple_min"]},
        "signal_events": qualified_signals,
        "metrics": metrics(all_trades, leverage),
        "by_symbol": grouped_metrics(all_trades, lambda trade: trade.symbol, leverage),
        "by_month_utc": grouped_metrics(all_trades, lambda trade: datetime.fromtimestamp(trade.entry_ts / 1000, UTC).strftime("%Y-%m"), leverage),
        "selected_parameters": config["signal_parameters"] | config["risk"],
        "execution": {"entry": "next_15m_open", "same_bar_priority": "stop_first", "leverage": leverage,
                      "costs": config["costs"], "cooldown_bars": cooldown},
        "status": "research_only",
    }
    write_csv(output_dir / "momentum_exhaustion_observations.csv", [asdict(item) for item in all_events])
    write_csv(output_dir / "momentum_exhaustion_trades.csv", [asdict(item) for item in all_trades])
    (output_dir / "momentum_exhaustion_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return report


def write_csv(path: Path, rows: list[dict]) -> None:
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_CONFIG.parents[1] / "results")
    args = parser.parse_args(argv)
    print(json.dumps(run(args.data_dir, args.config, args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
