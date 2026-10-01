#!/usr/bin/env python3
"""Causal 15-minute backtest for the extreme-wick altcoin short candidate.

The input is the repository's confirmed 5-minute JSONL export.  The module
deliberately has no pandas dependency so a clean checkout can reproduce the
research.  It never uses a candle from the right side of a signal bar when
building that bar's features.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import itertools
import json
import math
import statistics
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

FIVE_MINUTES = 5 * 60 * 1000
FIFTEEN_MINUTES = 15 * 60 * 1000
DAY_15M = 96
UTC = timezone.utc
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config" / "strategy.json"
DEFAULT_DATA = Path(__file__).resolve().parents[3] / "data/kline/okx/swap/5m"


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class Candidate:
    index: int
    ts: int
    gain24: float
    atr: float
    rsi: float
    wick_ratio: float
    rejection_atr: float
    volume_ratio: float
    quote_volume24h: float
    prior_highs: tuple[tuple[int, float], ...]


@dataclass(frozen=True)
class Params:
    lookback_bars: int
    upper_wick_min: float
    extension_atr_min: float
    rejection_atr_min: float
    confirmation_window: int
    stop_atr: float
    tp2_r: float

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_ts: int
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    tp2: float
    net_r: float
    exit_reason: str
    bars_held: int


def finite_number(value, default=None):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def load_config(path: Path) -> dict:
    return json.loads(path.read_text())


def excluded_symbols(config_path: Path, config: dict) -> set[str]:
    excluded = {str(x).upper() for x in config.get("hard_filters", {}).get("excluded_symbols", [])}
    # Keep the research universe aligned with the repository's canonical
    # asset-class exclusions when that file is available.
    canonical = config_path.parents[2] / "sweep_reversal_short" / "config" / "universe.json"
    try:
        excluded.update(str(x).upper() for x in json.loads(canonical.read_text()).get("exclude", []))
    except (OSError, ValueError, json.JSONDecodeError):
        pass
    return excluded


def symbol_from_path(path: Path) -> str:
    return path.name.split("_USDT_SWAP_5m_", 1)[0].upper()


def choose_paths(data_dir: Path, excluded: set[str], allowed: set[str] | None = None) -> list[tuple[str, Path]]:
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in data_dir.glob("*_USDT_SWAP_5m_*.jsonl.gz"):
        symbol = symbol_from_path(path)
        if symbol not in excluded and (allowed is None or symbol in allowed):
            grouped[symbol].append(path)
    # Export suffixes contain the end timestamp, so lexical order chooses the
    # most recent snapshot without looking into the future candle values.
    return [(symbol, sorted(paths, key=lambda p: p.name)[-1]) for symbol, paths in sorted(grouped.items())]


def iter_rows(path: Path):
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get("confirmed") is not True:
                    continue
                ts = int(row.get("timestamp_ms"))
                values = [finite_number(row.get(key)) for key in ("open", "high", "low", "close", "quote_volume")]
                if ts <= 0 or any(x is None for x in values):
                    continue
                o, h, low, c, qv = values
                if min(o, h, low, c) <= 0 or qv < 0 or h < max(o, c) or low > min(o, c) or h < low:
                    continue
                yield Bar(ts, o, h, low, c, qv)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue


def load_bars(path: Path) -> list[Bar]:
    by_ts = {bar.ts: bar for bar in iter_rows(path)}
    return [by_ts[ts] for ts in sorted(by_ts)]


def aggregate_15m(bars: list[Bar]) -> list[Bar]:
    """Build only complete 3-child buckets and retain no unconfirmed candle."""
    groups: dict[int, dict[int, Bar]] = defaultdict(dict)
    for bar in bars:
        bucket = bar.ts // FIFTEEN_MINUTES * FIFTEEN_MINUTES
        groups[bucket][bar.ts] = bar
    result: list[Bar] = []
    for bucket in sorted(groups):
        children = groups[bucket]
        expected = [bucket + step * FIVE_MINUTES for step in range(3)]
        if [x for x in expected if x in children] != expected:
            continue
        values = [children[x] for x in expected]
        result.append(Bar(bucket, values[0].open, max(x.high for x in values),
                          min(x.low for x in values), values[-1].close,
                          sum(x.quote_volume for x in values)))
    return result


def contiguous(bars: list[Bar]) -> list[bool]:
    result = [False] * len(bars)
    if bars:
        result[0] = True
    for i in range(1, len(bars)):
        result[i] = result[i - 1] and bars[i].ts - bars[i - 1].ts == FIFTEEN_MINUTES
    return result


def atr_at(bars: list[Bar], index: int, period: int) -> float:
    if index < period - 1:
        return 0.0
    values = []
    for i in range(index - period + 1, index + 1):
        previous = bars[i - 1].close if i else bars[i].open
        values.append(max(bars[i].high - bars[i].low, abs(bars[i].high - previous), abs(bars[i].low - previous)))
    return sum(values) / period


def rsi_at(bars: list[Bar], index: int, period: int) -> float:
    if index < period:
        return 0.0
    gains = losses = 0.0
    for i in range(index - period + 1, index + 1):
        change = bars[i].close - bars[i - 1].close
        gains += max(change, 0.0)
        losses += max(-change, 0.0)
    if losses == 0:
        return 100.0 if gains > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + gains / losses)


def build_candidates(bars: list[Bar], config: dict) -> list[Candidate]:
    signal = config["signal_parameters"]
    atr_period = int(signal["atr_period"])
    rsi_period = int(signal["rsi_period"])
    max_lookback = max(int(x) for x in config["training"]["grid"]["lookback_bars"])
    minimum = max(DAY_15M, atr_period, rsi_period, max_lookback, 20)
    contiguous_flags = contiguous(bars)
    out: list[Candidate] = []
    for i in range(minimum, len(bars) - 1):
        if not contiguous_flags[i] or not contiguous_flags[i - DAY_15M + 1]:
            continue
        close = bars[i].close
        prior_close = bars[i - DAY_15M].close
        if prior_close <= 0:
            continue
        # The user's range describes the intraday pump that created the wick.
        # Use the event high against the prior 24h close so a sharp rejection
        # whose close has already fallen below +50% is still in the universe.
        gain = max(bars[i].high, close) / prior_close - 1.0
        quote_volume24h = sum(bars_quote.quote_volume for bars_quote in bars[i - DAY_15M + 1:i + 1])
        gain_max = config["hard_filters"].get("gain24h_max_exclusive")
        if gain <= float(config["hard_filters"]["gain24h_min"]) or (gain_max is not None and gain >= float(gain_max)):
            continue
        if quote_volume24h < float(config["hard_filters"].get("quote_volume24h_min", 0.0)):
            continue
        atr = atr_at(bars, i, atr_period)
        if atr <= 0:
            continue
        candle_range = max(bars[i].high - bars[i].low, 1e-12)
        wick = (bars[i].high - max(bars[i].open, close)) / candle_range
        rejection = (bars[i].high - close) / atr
        prior_volume = sum(bars[j].quote_volume for j in range(i - 20, i)) / 20
        volume_ratio = bars[i].quote_volume / prior_volume if prior_volume > 0 else 0.0
        prior_highs = tuple((lookback, max(bars[j].high for j in range(i - lookback, i)))
                            for lookback in config["training"]["grid"]["lookback_bars"])
        out.append(Candidate(i, bars[i].ts, gain, atr, rsi_at(bars, i, rsi_period),
                             wick, rejection, volume_ratio, quote_volume24h, prior_highs))
    return out


def prior_high(candidate: Candidate, lookback: int) -> float:
    return dict(candidate.prior_highs).get(lookback, 0.0)


def net_leg_r(entry: float, exit_price: float, fraction: float, risk: float, fee: float, slip: float) -> float:
    effective_entry = entry
    effective_exit = exit_price * (1.0 + slip)
    gross = fraction * (effective_entry - effective_exit)
    costs = fee * fraction * (effective_entry + effective_exit)
    return (gross - costs) / risk


def simulate_trade(symbol: str, bars: list[Bar], candidate: Candidate, params: Params,
                   config: dict, confirmation_index: int) -> Trade | None:
    signal = config["signal_parameters"]
    costs = config["costs"]
    entry_index = confirmation_index + 1
    if entry_index >= len(bars) or bars[entry_index].ts - bars[confirmation_index].ts != FIFTEEN_MINUTES:
        return None
    entry = bars[entry_index].open * (1.0 - float(costs["slippage_one_way"]))
    stop = bars[candidate.index].high + params.stop_atr * candidate.atr
    risk = stop - entry
    if risk <= 0 or not (float(signal["min_risk_atr"]) * candidate.atr <= risk <= float(signal["max_risk_atr"]) * candidate.atr):
        return None
    tp1_r = float(signal["tp1_r"])
    tp1 = entry - tp1_r * risk
    tp2 = entry - params.tp2_r * risk
    fee = float(costs["fee_rate_one_way"])
    slip = float(costs["slippage_one_way"])
    stop_price = stop
    tp1_done = False
    net_r = 0.0
    exit_index = min(len(bars) - 1, entry_index + int(signal["max_hold_bars"]) - 1)
    reason = "时间离场"
    exit_price = bars[exit_index].close
    for j in range(entry_index, exit_index + 1):
        bar = bars[j]
        if j > entry_index and bar.ts - bars[j - 1].ts != FIFTEEN_MINUTES:
            return None
        # An opening gap and a same-bar stop/target collision are both
        # resolved conservatively in favour of the stop.
        if bar.open >= stop_price:
            net_r += net_leg_r(entry, bar.open, 1.0 - (0.5 if tp1_done else 0.0), risk, fee, slip)
            exit_price, exit_index, reason = bar.open, j, "止损(跳空)"
            break
        if bar.high >= stop_price:
            remaining = 1.0 - (float(signal["tp1_fraction"]) if tp1_done else 0.0)
            net_r += net_leg_r(entry, stop_price, remaining, risk, fee, slip)
            exit_price, exit_index, reason = stop_price, j, "止损"
            break
        if not tp1_done and bar.low <= tp1:
            net_r += net_leg_r(entry, tp1, float(signal["tp1_fraction"]), risk, fee, slip)
            tp1_done = True
            stop_price = entry
        if bar.low <= tp2:
            remaining = 1.0 - float(signal["tp1_fraction"]) if tp1_done else 1.0
            net_r += net_leg_r(entry, tp2, remaining, risk, fee, slip)
            exit_price, exit_index, reason = tp2, j, "止盈"
            break
    else:
        remaining = 1.0 - float(signal["tp1_fraction"]) if tp1_done else 1.0
        net_r += net_leg_r(entry, exit_price, remaining, risk, fee, slip)
    return Trade(symbol, candidate.ts, bars[entry_index].ts, bars[exit_index].ts,
                 entry, exit_price, stop, tp2, net_r, reason, exit_index - entry_index + 1)


def confirm_index(bars: list[Bar], candidate: Candidate, params: Params, config: dict) -> int | None:
    retest_atr = float(config["signal_parameters"]["retest_atr"])
    level = prior_high(candidate, params.lookback_bars)
    for j in range(candidate.index + 1, min(len(bars), candidate.index + 1 + params.confirmation_window)):
        if bars[j].ts - bars[j - 1].ts != FIFTEEN_MINUTES:
            break
        bearish = bars[j].close < bars[j].open
        breakdown = bars[j].close < bars[candidate.index].low
        retest = (level - retest_atr * candidate.atr <= bars[j].high <= level + retest_atr * candidate.atr
                  and bars[j].close < level)
        if bearish and (breakdown or retest):
            return j
    return None


def find_trades(symbol: str, bars: list[Bar], params: Params, config: dict,
                start_ts: int | None = None, end_ts: int | None = None,
                strict_end: bool = False, candidates: list[Candidate] | None = None) -> list[Trade]:
    trades: list[Trade] = []
    cooldown_until = -1
    for candidate in candidates if candidates is not None else build_candidates(bars, config):
        if start_ts is not None and candidate.ts < start_ts:
            continue
        if end_ts is not None and candidate.ts >= end_ts:
            continue
        if candidate.index <= cooldown_until:
            continue
        level = prior_high(candidate, params.lookback_bars)
        if level <= 0:
            continue
        signal = config["signal_parameters"]
        extension = (bars[candidate.index].high - level) / candidate.atr
        if not (candidate.wick_ratio >= params.upper_wick_min
                and extension >= params.extension_atr_min
                and candidate.rejection_atr >= params.rejection_atr_min
                and candidate.volume_ratio >= float(signal["volume_multiple"])
                and candidate.rsi >= float(signal["rsi_min"])
                and bars[candidate.index].close < bars[candidate.index].open
                and bars[candidate.index].close <= level - float(signal["close_below_atr"]) * candidate.atr):
            continue
        confirmation = confirm_index(bars, candidate, params, config)
        if confirmation is None:
            continue
        trade = simulate_trade(symbol, bars, candidate, params, config, confirmation)
        if trade is None:
            continue
        if end_ts is not None and strict_end and trade.exit_ts >= end_ts:
            continue
        trades.append(trade)
        cooldown_until = next((i for i, bar in enumerate(bars) if bar.ts == trade.exit_ts), len(bars) - 1) + int(signal["cooldown_bars"])
    return trades


def metrics(trades: list[Trade]) -> dict:
    # A portfolio equity curve is chronological even though the loader walks
    # symbols alphabetically.  Sorting here keeps drawdown and any downstream
    # report independent of filesystem/symbol order.
    ordered = sorted(trades, key=lambda t: (t.exit_ts, t.entry_ts, t.symbol))
    values = [t.net_r for t in ordered]
    equity = peak = drawdown = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    wins = sum(v > 0 for v in values)
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    return {"trades": len(values), "wins": wins, "win_rate": wins / len(values) if values else 0.0,
            "total_r": sum(values), "avg_net_r": statistics.fmean(values) if values else 0.0,
            # 999 is a finite sentinel so reports remain strict JSON when a
            # small sample has no losing trade.
            "profit_factor": gains / losses if losses else (999.0 if gains else 0.0),
            "max_drawdown_r": drawdown, "symbols": len({t.symbol for t in trades}),
            "exit_reasons": dict(sorted({x: sum(t.exit_reason == x for t in trades) for x in {t.exit_reason for t in trades}}.items()))}


def enforce_portfolio_limit(trades: list[Trade], max_concurrent: int = 1) -> list[Trade]:
    """Apply the strategy-level one-position rule after symbol replay."""
    if max_concurrent < 1:
        return []
    accepted: list[Trade] = []
    active: list[int] = []
    for trade in sorted(trades, key=lambda t: (t.entry_ts, t.symbol)):
        active = [exit_ts for exit_ts in active if exit_ts > trade.entry_ts]
        if len(active) >= max_concurrent:
            continue
        accepted.append(trade)
        active.append(trade.exit_ts)
    return accepted


def grouped_metrics(trades: list[Trade], key) -> dict:
    groups: dict[str, list[Trade]] = defaultdict(list)
    for trade in trades:
        groups[str(key(trade))].append(trade)
    return {name: metrics(values) for name, values in sorted(groups.items())}


def grid(config: dict) -> list[Params]:
    values = config["training"]["grid"]
    keys = ("lookback_bars", "upper_wick_min", "extension_atr_min", "rejection_atr_min", "confirmation_window", "stop_atr", "tp2_r")
    return [Params(*items) for items in itertools.product(*(values[key] for key in keys))]


def write_csv(path: Path, rows: Iterable[dict]) -> None:
    rows = list(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def load_series(data_dir: Path, config_path: Path, config: dict):
    canonical = config_path.parents[2] / "sweep_reversal_short" / "config" / "universe.json"
    try:
        allowed = {str(x).upper() for x in json.loads(canonical.read_text()).get("altcoins", [])}
    except (OSError, ValueError, json.JSONDecodeError):
        allowed = None
    for symbol, path in choose_paths(data_dir, excluded_symbols(config_path, config), allowed):
        bars = aggregate_15m(load_bars(path))
        if len(bars) >= DAY_15M * 2:
            yield symbol, bars


def run_train(data_dir: Path, config_path: Path, output_dir: Path) -> dict:
    config = load_config(config_path)
    series = [(symbol, bars, build_candidates(bars, config))
              for symbol, bars in load_series(data_dir, config_path, config)]
    if not series:
        raise RuntimeError("没有可用的完整 15m 数据")
    minimum_ts = min(bars[0].ts for _, bars, _ in series)
    maximum_ts = max(bars[-1].ts for _, bars, _ in series)
    # One 60/30/30 chronological window is available in this six-month
    # export.  A second shifted window is added when the data span permits it.
    day = 24 * 60 * 60 * 1000
    windows = []
    cursor = minimum_ts
    while cursor + 120 * day <= maximum_ts:
        windows.append((cursor, cursor + 60 * day, cursor + 90 * day, cursor + 120 * day))
        cursor += 30 * day
    params_list = grid(config)
    rows = []
    selected = []
    for window_no, (train_start, train_end, valid_end, test_end) in enumerate(windows, 1):
        train_map: dict[Params, list[Trade]] = defaultdict(list)
        valid_map: dict[Params, list[Trade]] = defaultdict(list)
        for symbol, bars, candidates in series:
            for params in params_list:
                train_map[params].extend(find_trades(symbol, bars, params, config, train_start, train_end, True, candidates))
                valid_map[params].extend(find_trades(symbol, bars, params, config, train_end, valid_end, True, candidates))
        for params in params_list:
            train_map[params] = enforce_portfolio_limit(train_map[params])
            valid_map[params] = enforce_portfolio_limit(valid_map[params])
        def rank(params):
            train, valid = metrics(train_map[params]), metrics(valid_map[params])
            if train["trades"] < int(config["training"]["minimum_train_trades"]):
                return (-1e9, -1e9, -1e9)
            return (valid["avg_net_r"] if valid["trades"] >= int(config["training"]["minimum_validation_trades"]) else train["avg_net_r"],
                    valid["total_r"], train["trades"])
        chosen = max(params_list, key=rank)
        selected.append((window_no, chosen))
        test_trades: list[Trade] = []
        for symbol, bars, candidates in series:
            test_trades.extend(find_trades(symbol, bars, chosen, config, valid_end, test_end, True, candidates))
        test_trades = enforce_portfolio_limit(test_trades)
        for params in params_list:
            rows.append({"window": window_no, "params": json.dumps(params.as_dict(), sort_keys=True),
                         "train": json.dumps(metrics(train_map[params]), sort_keys=True),
                         "validation": json.dumps(metrics(valid_map[params]), sort_keys=True),
                         "selected": params == chosen, "test": json.dumps(metrics(test_trades) if params == chosen else {}, sort_keys=True)})
    oos: list[Trade] = []
    for window_no, params in selected:
        window = windows[window_no - 1]
        for symbol, bars, candidates in series:
            oos.extend(find_trades(symbol, bars, params, config, window[2], window[3], True, candidates))
    oos = enforce_portfolio_limit(oos)
    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "grid.csv", rows)
    write_csv(output_dir / "trades_oos.csv", [asdict(t) for t in oos])
    report = {"strategy": config["strategy"], "version": config["version"], "data": {"symbols": len(series), "start": minimum_ts, "end": maximum_ts},
              "windows": [{"window": no, "params": params.as_dict()} for no, params in selected],
              "oos": metrics(oos),
              "oos_by_symbol": grouped_metrics(oos, lambda trade: trade.symbol),
              "oos_by_month": grouped_metrics(oos, lambda trade: datetime.fromtimestamp(trade.entry_ts / 1000, UTC).strftime("%Y-%m")),
              "acceptance": {}, "status": "FAIL"}
    acceptance = {"minimum_oos_trades": report["oos"]["trades"] >= int(config["training"]["minimum_oos_trades"]),
                  "minimum_symbols": report["oos"]["symbols"] >= 5, "positive_expectancy": report["oos"]["avg_net_r"] > 0,
                  "profit_factor": report["oos"]["profit_factor"] >= 1.2, "drawdown": report["oos"]["max_drawdown_r"] <= 10}
    report["acceptance"] = acceptance
    report["status"] = "PASS" if all(acceptance.values()) else "FAIL"
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return report


def run_scan(data_dir: Path, config_path: Path, output: Path) -> None:
    config = load_config(config_path)
    params = Params(int(config["signal_parameters"]["lookback_bars"]), float(config["signal_parameters"]["upper_wick_min"]),
                    float(config["signal_parameters"]["extension_atr_min"]), float(config["signal_parameters"]["rejection_atr_min"]),
                    int(config["signal_parameters"]["confirmation_window"]), float(config["signal_parameters"]["stop_atr"]), float(config["signal_parameters"]["tp2_r"]))
    events = []
    for symbol, bars in load_series(data_dir, config_path, config):
        for candidate in build_candidates(bars, config):
            level = prior_high(candidate, params.lookback_bars)
            if level <= 0:
                continue
            extension = (bars[candidate.index].high - level) / candidate.atr
            if (candidate.wick_ratio >= params.upper_wick_min and extension >= params.extension_atr_min
                    and candidate.rejection_atr >= params.rejection_atr_min and candidate.volume_ratio >= float(config["signal_parameters"]["volume_multiple"])
                    and candidate.rsi >= float(config["signal_parameters"]["rsi_min"])
                    and bars[candidate.index].close < bars[candidate.index].open
                    and bars[candidate.index].close <= level - float(config["signal_parameters"]["close_below_atr"]) * candidate.atr):
                events.append({"symbol": symbol, "timestamp": datetime.fromtimestamp(candidate.ts / 1000, UTC).isoformat(),
                               "gain24": round(candidate.gain24, 6), "wick_ratio": round(candidate.wick_ratio, 4),
                               "extension_atr": round(extension, 4), "rsi": round(candidate.rsi, 2), "volume_ratio": round(candidate.volume_ratio, 3)})
    write_csv(output, events)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("scan", "train"):
        command = sub.add_parser(name)
        command.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
        command.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
        if name == "scan":
            command.add_argument("--output", type=Path, default=Path("events.csv"))
        else:
            command.add_argument("--output-dir", type=Path, default=Path(__file__).resolve().parents[1] / "results")
    args = parser.parse_args(argv)
    if args.command == "scan":
        run_scan(args.data_dir, args.config, args.output)
    else:
        report = run_train(args.data_dir, args.config, args.output_dir)
        print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
