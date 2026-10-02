#!/usr/bin/env python3
"""Causal analysis of the two 15-minute pending-order short setups.

Both setups use completed 15-minute candles.  The repository's confirmed
5-minute exports are aggregated into complete 15-minute bars before any
feature is calculated.  No order is filled on the candle that reveals the
setup: a sell limit becomes active on the following candle.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import statistics
import sys
from collections import defaultdict, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

SRC = Path(__file__).resolve().parent
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
from extreme_wick_short import (  # noqa: E402
    Bar,
    FIFTEEN_MINUTES,
    aggregate_15m,
    choose_paths,
    excluded_symbols,
    finite_number,
    load_bars,
    load_config,
)

UTC = timezone.utc
DAY_15M = 96
THIRTY_DAYS_15M = 30 * DAY_15M
DEFAULT_CONFIG = SRC.parent / "config" / "strategy.json"
DEFAULT_DATA = SRC.parents[3] / "data/kline/okx/swap/5m"


@dataclass(frozen=True)
class Observation:
    symbol: str
    index: int
    timestamp: int
    intraday_gain: float
    thirty_day_low: float
    low_multiple: float


@dataclass(frozen=True)
class PendingSignal:
    strategy: str
    symbol: str
    signal_index: int
    signal_ts: int
    order_price: float
    anchor_index: int
    anchor_high: float
    atr: float


@dataclass(frozen=True)
class Trade:
    strategy: str
    symbol: str
    signal_ts: int
    order_ts: int
    fill_ts: int
    exit_ts: int
    order_price: float
    entry: float
    exit: float
    stop: float
    target: float
    net_r: float
    exit_reason: str
    bars_held: int


def contiguous_flags(bars: list[Bar]) -> list[bool]:
    flags = [False] * len(bars)
    if bars:
        flags[0] = True
    for index in range(1, len(bars)):
        flags[index] = flags[index - 1] and bars[index].ts - bars[index - 1].ts == FIFTEEN_MINUTES
    return flags


def atr_at(bars: list[Bar], index: int, period: int) -> float:
    if index < period - 1:
        return 0.0
    true_ranges = []
    for cursor in range(index - period + 1, index + 1):
        previous_close = bars[cursor - 1].close if cursor else bars[cursor].open
        true_ranges.append(max(bars[cursor].high - bars[cursor].low,
                               abs(bars[cursor].high - previous_close),
                               abs(bars[cursor].low - previous_close)))
    return sum(true_ranges) / period


def load_series(data_dir: Path, config_path: Path, config: dict):
    canonical = config_path.parents[2] / "sweep_reversal_short" / "config" / "universe.json"
    try:
        allowed = {str(x).upper() for x in json.loads(canonical.read_text()).get("altcoins", [])}
    except (OSError, ValueError, json.JSONDecodeError):
        allowed = None
    excluded = excluded_symbols(config_path, config)
    for symbol, path in choose_paths(data_dir, excluded, allowed):
        raw = load_bars(path)
        bars = aggregate_15m(raw)
        if len(bars) >= THIRTY_DAYS_15M + 20:
            yield symbol, bars


def observations(symbol: str, bars: list[Bar], config: dict) -> list[Observation]:
    settings = config["observation_filter"]
    gain_min = float(settings["intraday_gain_gt"])
    multiple_min = float(settings["thirty_day_low_multiple_min"])
    atr_flags = contiguous_flags(bars)
    result: list[Observation] = []
    # O(n) rolling minimum; recomputing min over 2,880 bars for every candle
    # makes a six-month, 288-symbol scan needlessly quadratic.
    lows = [bar.low for bar in bars]
    prior_low = [0.0] * len(bars)
    queue: deque[int] = deque()
    for cursor, value in enumerate(lows):
        while queue and queue[0] < cursor - THIRTY_DAYS_15M:
            queue.popleft()
        if cursor >= THIRTY_DAYS_15M and queue:
            prior_low[cursor] = lows[queue[0]]
        while queue and lows[queue[-1]] >= value:
            queue.pop()
        queue.append(cursor)
    start = max(THIRTY_DAYS_15M, DAY_15M)
    # The last candle cannot reveal a setup whose pending order has not had a
    # following candle yet.
    for index in range(start, len(bars) - 1):
        if not atr_flags[index] or not atr_flags[index - THIRTY_DAYS_15M + 1]:
            continue
        prior_close = bars[index - DAY_15M].close
        # Exclude the current event candle so its wick cannot manufacture its
        # own denominator by printing a new low.
        thirty_day_low = prior_low[index]
        if prior_close <= 0 or thirty_day_low <= 0:
            continue
        intraday_gain = max(bars[index].high, bars[index].close) / prior_close - 1.0
        low_multiple = max(bars[index].high, bars[index].close) / thirty_day_low
        if intraday_gain <= gain_min or low_multiple < multiple_min:
            continue
        result.append(Observation(symbol, index, bars[index].ts, intraday_gain, thirty_day_low, low_multiple))
    return result


def small_up_baseline(bars: list[Bar], pump_index: int, settings: dict) -> bool:
    count = int(settings["pre_bars"])
    if pump_index < count:
        return False
    prior = bars[pump_index - count:pump_index]
    returns = [(bar.close / bar.open - 1.0) if bar.open else 0.0 for bar in prior]
    net = prior[-1].close / prior[0].open - 1.0 if prior[0].open else 0.0
    total_range = max(bar.high for bar in prior) / min(bar.low for bar in prior) - 1.0
    positive = sum(value > 0 for value in returns)
    return (settings["pre_net_min"] <= net <= settings["pre_net_max"]
            and total_range <= settings["pre_range_max"]
            and max(abs(value) for value in returns) <= settings["pre_bar_move_max"]
            and positive / count >= settings["pre_positive_fraction"])


def small_down_sequence(bars: list[Bar], signal_index: int, settings: dict) -> bool:
    count = int(settings["pre_bars"])
    if signal_index + 1 < count:
        return False
    # The observation candle is the sixth (default) down candle. Its close
    # confirms the sequence, so the order can become active only on the next
    # candle while keeping the whole pattern causal.
    prior = bars[signal_index - count + 1:signal_index + 1]
    returns = [(bar.close / bar.open - 1.0) if bar.open else 0.0 for bar in prior]
    net = prior[-1].close / prior[0].open - 1.0 if prior[0].open else 0.0
    negative = sum(value < 0 for value in returns)
    return (settings["pre_net_down_min"] <= -net <= settings["pre_net_down_max"]
            and max(abs(value) for value in returns) <= settings["pre_bar_move_max"]
            and negative / count >= settings["pre_negative_fraction"])


def mode_one_signals(symbol: str, bars: list[Bar], obs: dict[int, Observation], config: dict) -> list[PendingSignal]:
    settings = config["pending_modes"]["mode_one_15m_pump_pullback"]
    signals: list[PendingSignal] = []
    for index in sorted(obs):
        if index + 1 >= len(bars) or not small_up_baseline(bars, index, settings):
            continue
        pump = bars[index]
        previous_close = bars[index - 1].close
        # The full 15m high is known only after the candle closes, so using it
        # here remains causal while matching "sudden +20%" even when the pump
        # candle starts to reject before its close.
        pump_gain = pump.high / previous_close - 1.0 if previous_close else 0.0
        if pump_gain < float(settings["pump_gain_min"]):
            continue
        pullback = bars[index + 1]
        pullback_drop = (pullback.open - pullback.close) / pullback.open if pullback.open else 0.0
        if not (pullback.close < pullback.open
                and float(settings["small_pullback_min"])
                <= pullback_drop <= float(settings["small_pullback_max"])
                and pullback.low / pump.close >= float(settings["pullback_low_floor"])):
            continue
        atr = atr_at(bars, index, int(config["risk"]["atr_period"]))
        if atr <= 0:
            continue
        signals.append(PendingSignal("mode_one_15m_pump_pullback", symbol, index + 1,
                                     pullback.ts, pullback.open, index, pump.high, atr))
    return signals


def mode_one_stage_counts(bars: list[Bar], obs: dict[int, Observation], config: dict) -> dict[str, int]:
    """Explain sparse results without relaxing the configured rule."""
    settings = config["pending_modes"]["mode_one_15m_pump_pullback"]
    counts = {"observation_bars": len(obs), "small_up_baseline": 0,
              "pump_at_least_20pct": 0, "baseline_and_pump": 0,
              "small_pullback_after_pump": 0, "full_pattern": 0}
    for index in sorted(obs):
        if index + 1 >= len(bars):
            continue
        baseline_ok = small_up_baseline(bars, index, settings)
        if baseline_ok:
            counts["small_up_baseline"] += 1
        previous_close = bars[index - 1].close
        if previous_close <= 0 or bars[index].high / previous_close - 1.0 < float(settings["pump_gain_min"]):
            continue
        counts["pump_at_least_20pct"] += 1
        if baseline_ok:
            counts["baseline_and_pump"] += 1
        pullback = bars[index + 1]
        drop = (pullback.open - pullback.close) / pullback.open if pullback.open else 0.0
        if (pullback.close < pullback.open and float(settings["small_pullback_min"]) <= drop <= float(settings["small_pullback_max"])
                and pullback.low / bars[index].close >= float(settings["pullback_low_floor"])):
            counts["small_pullback_after_pump"] += 1
            if baseline_ok:
                counts["full_pattern"] += 1
    return counts


def utc_day(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp / 1000, UTC).strftime("%Y-%m-%d")


def running_day_highs(bars: list[Bar]) -> list[int]:
    result = [-1] * len(bars)
    current_day = None
    high_index = -1
    for index, bar in enumerate(bars):
        day = utc_day(bar.ts)
        if day != current_day:
            current_day, high_index = day, index
        elif high_index < 0 or bar.high > bars[high_index].high:
            high_index = index
        result[index] = high_index
    return result


def mode_two_signals(symbol: str, bars: list[Bar], obs: dict[int, Observation], config: dict) -> list[PendingSignal]:
    settings = config["pending_modes"]["mode_two_15m_downtrend_to_day_high_close"]
    day_high = running_day_highs(bars)
    signals: list[PendingSignal] = []
    for index in sorted(obs):
        count = int(settings["pre_bars"])
        if index < count or not small_down_sequence(bars, index, settings):
            continue
        anchor_index = day_high[index - count]
        if anchor_index < 0 or anchor_index > index - count:
            continue
        if utc_day(bars[anchor_index].ts) != utc_day(bars[index].ts):
            continue
        # The down sequence may not reclaim a fresh intraday high after the
        # daily high anchor; this keeps the limit level causal and meaningful.
        if max(bar.high for bar in bars[index - count + 1:index + 1]) > bars[anchor_index].high * (1.0 + float(settings["anchor_high_tolerance"])):
            continue
        atr = atr_at(bars, index, int(config["risk"]["atr_period"]))
        if atr <= 0:
            continue
        signals.append(PendingSignal("mode_two_15m_downtrend_to_day_high_close", symbol, index,
                                     bars[index].ts, bars[anchor_index].close, anchor_index,
                                     bars[anchor_index].high, atr))
    return signals


def net_r(entry: float, exit_price: float, risk: float, fee: float, slip: float) -> float:
    effective_exit = exit_price * (1.0 + slip)
    return (entry - effective_exit - fee * (entry + effective_exit)) / risk


def fill_pending(signal: PendingSignal, bars: list[Bar], config: dict) -> tuple[int, float] | None:
    settings = config["risk"]
    first = signal.signal_index + 1
    last = min(len(bars), first + int(settings["limit_valid_bars"]))
    for index in range(first, last):
        if bars[index].ts - bars[index - 1].ts != FIFTEEN_MINUTES:
            return None
        bar = bars[index]
        if bar.open >= signal.order_price:
            return index, bar.open
        if bar.high >= signal.order_price:
            return index, signal.order_price
    return None


def simulate(signal: PendingSignal, bars: list[Bar], config: dict, fill_index: int, raw_entry: float) -> Trade | None:
    risk_config = config["risk"]
    costs = config["costs"]
    slip = float(costs["slippage_one_way"])
    fee = float(costs["fee_rate_one_way"])
    entry = raw_entry * (1.0 - slip)
    stop = signal.anchor_high + float(risk_config["stop_atr"]) * signal.atr
    risk = stop - entry
    if risk <= 0 or risk < float(risk_config["min_risk_atr"]) * signal.atr or risk > float(risk_config["max_risk_atr"]) * signal.atr:
        return None
    target = entry - float(risk_config["target_r"]) * risk
    last = min(len(bars) - 1, fill_index + int(risk_config["max_hold_bars"]) - 1)
    reason = "时间离场"
    exit_price = bars[last].close
    exit_index = last
    for index in range(fill_index, last + 1):
        bar = bars[index]
        if index > fill_index and bar.ts - bars[index - 1].ts != FIFTEEN_MINUTES:
            return None
        if bar.open >= stop:
            reason, exit_price, exit_index = "止损(跳空)", bar.open, index
            break
        if bar.high >= stop:  # stop has priority over a same-bar target
            reason, exit_price, exit_index = "止损", stop, index
            break
        if bar.low <= target:
            reason, exit_price, exit_index = "止盈", target, index
            break
    return Trade(signal.strategy, signal.symbol, signal.signal_ts, bars[signal.signal_index + 1].ts,
                 bars[fill_index].ts, bars[exit_index].ts, signal.order_price, entry,
                 exit_price, stop, target, net_r(entry, exit_price, risk, fee, slip),
                 reason, exit_index - fill_index + 1)


def metrics(trades: list[Trade]) -> dict:
    ordered = sorted(trades, key=lambda trade: (trade.exit_ts, trade.fill_ts, trade.symbol))
    values = [trade.net_r for trade in ordered]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    wins = sum(value > 0 for value in values)
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    return {"trades": len(values), "wins": wins, "win_rate": wins / len(values) if values else 0.0,
            "total_r": sum(values), "avg_net_r": statistics.fmean(values) if values else 0.0,
            "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
            "max_drawdown_r": drawdown, "symbols": len({trade.symbol for trade in trades}),
            "exit_reasons": dict(sorted({reason: sum(t.exit_reason == reason for t in trades)
                                           for reason in {t.exit_reason for t in trades}}.items()))}


def grouped_metrics(trades: list[Trade], key) -> dict[str, dict]:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        groups[str(key(trade))].append(trade)
    return {group: metrics(groups[group]) for group in sorted(groups)}


def run_mode(symbol: str, bars: list[Bar], signals: list[PendingSignal], config: dict) -> tuple[list[Trade], dict]:
    trades: list[Trade] = []
    pending = filled = rejected = 0
    cooldown_until = -1
    for signal in signals:
        if signal.signal_index <= cooldown_until:
            continue
        pending += 1
        fill = fill_pending(signal, bars, config)
        if fill is None:
            continue
        filled += 1
        trade = simulate(signal, bars, config, *fill)
        if trade is None:
            rejected += 1
            continue
        trades.append(trade)
        exit_index = next((i for i, bar in enumerate(bars) if bar.ts == trade.exit_ts), len(bars) - 1)
        cooldown_until = exit_index + int(config["risk"]["cooldown_bars"])
    summary = {"signals": len(signals), "pending_orders": pending, "fills": filled,
               "unfilled": pending - filled, "risk_rejected": rejected,
               "fill_rate": filled / pending if pending else 0.0}
    summary.update(metrics(trades))
    return trades, summary


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def analyze(data_dir: Path, config_path: Path, output_dir: Path) -> dict:
    config = load_config(config_path)
    output_dir.mkdir(parents=True, exist_ok=True)
    all_observations: list[Observation] = []
    mode_trades: dict[str, list[Trade]] = defaultdict(list)
    mode_signals: dict[str, int] = defaultdict(int)
    mode_orders: dict[str, dict] = defaultdict(lambda: {"signals": 0, "pending_orders": 0, "fills": 0, "unfilled": 0, "risk_rejected": 0})
    diagnostics = {"mode_one": {"observation_bars": 0, "small_up_baseline": 0,
                                 "pump_at_least_20pct": 0, "baseline_and_pump": 0,
                                 "small_pullback_after_pump": 0, "full_pattern": 0}}
    symbols = 0
    starts: list[int] = []
    ends: list[int] = []
    for symbol, bars in load_series(data_dir, config_path, config):
        symbols += 1
        starts.append(bars[0].ts)
        ends.append(bars[-1].ts)
        obs = observations(symbol, bars, config)
        all_observations.extend(obs)
        obs_map = {item.index: item for item in obs}
        stages = mode_one_stage_counts(bars, obs_map, config)
        for key, value in stages.items():
            diagnostics["mode_one"][key] += value
        mode_signals_local = {
            "mode_one_15m_pump_pullback": mode_one_signals(symbol, bars, obs_map, config),
            "mode_two_15m_downtrend_to_day_high_close": mode_two_signals(symbol, bars, obs_map, config),
        }
        for mode, signals in mode_signals_local.items():
            trades, summary = run_mode(symbol, bars, signals, config)
            mode_trades[mode].extend(trades)
            for key in mode_orders[mode]:
                mode_orders[mode][key] += summary.get(key, 0)
    mode_reports = {}
    for mode, trades in mode_trades.items():
        report = dict(mode_orders[mode])
        report.update(metrics(trades))
        report["fill_rate"] = report["fills"] / report["pending_orders"] if report["pending_orders"] else 0.0
        report["by_symbol"] = grouped_metrics(trades, lambda trade: trade.symbol)
        report["by_month_utc"] = grouped_metrics(
            trades, lambda trade: datetime.fromtimestamp(trade.fill_ts / 1000, UTC).strftime("%Y-%m"))
        mode_reports[mode] = report
    all_trades = [trade for trades in mode_trades.values() for trade in trades]
    report = {"strategy": config["strategy"], "version": config["version"],
              "data": {"symbols": symbols, "start": min(starts) if starts else None, "end": max(ends) if ends else None,
                       "timeframe_minutes": 15},
              "observation_pool": {"observations": len(all_observations),
                                   "intraday_gain_gt": config["observation_filter"]["intraday_gain_gt"],
                                   "thirty_day_low_multiple_min": config["observation_filter"]["thirty_day_low_multiple_min"]},
              "modes": mode_reports, "diagnostics": diagnostics,
              "combined_filled_trades": metrics(all_trades),
              "analysis_scope": {
                  "portfolio_constraints_applied": False,
                  "cooldown_scope": "per_mode_per_symbol",
                  "combined_metrics": "unfiltered_merge_of_mode_results",
              },
              "status": "research_only"}
    write_csv(output_dir / "observations.csv", [asdict(item) for item in all_observations])
    write_csv(output_dir / "limit_trades.csv", [asdict(item) for item in all_trades])
    (output_dir / "limit_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=SRC.parent / "results")
    args = parser.parse_args(argv)
    print(json.dumps(analyze(args.data_dir, args.config, args.output_dir), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
