#!/usr/bin/env python3
"""Causal price replay for the personal trading-style bill experiment.

The bill-derived episodes provide a timestamp and direction label only.  This
module enters on the next confirmed 5m candle, applies fixed execution costs,
and exits with a pre-declared ATR stop, 1R target, or time limit.  It is a
risk-rule replay of observed behaviour, not a claim that the bill timestamp is
an independently generated signal.
"""
from __future__ import annotations

import argparse
import bisect
import csv
import gzip
import hashlib
import importlib.util
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence


ROOT = Path(__file__).resolve().parents[3]
LAB = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_CONFIG = LAB / "config/strategy.json"
BAR_MS = 5 * 60 * 1000
UTC = timezone.utc


def _load_bill_module():
    try:
        from backtest import (  # type: ignore
            Episode,
            aggregate_order_groups,
            collect_events,
            infer_episodes,
            read_ledger,
        )

        return Episode, aggregate_order_groups, collect_events, infer_episodes, read_ledger
    except ModuleNotFoundError:
        source = Path(__file__).with_name("backtest.py")
        spec = importlib.util.spec_from_file_location("personal_style_backtest", source)
        if spec is None or spec.loader is None:
            raise
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return (
            module.Episode,
            module.aggregate_order_groups,
            module.collect_events,
            module.infer_episodes,
            module.read_ledger,
        )


Episode, aggregate_order_groups, collect_events, infer_episodes, read_ledger = _load_bill_module()


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class FeatureSnapshot:
    return_6h: float
    return_24h: float
    distance_from_24h_high: float
    atr: float
    volume_ratio: float


@dataclass(frozen=True)
class PriceTrade:
    symbol: str
    direction: str
    behavior_entry_time: str
    entry_time: str
    exit_time: str
    entry: float
    exit: float
    stop: float
    target: float
    risk: float
    net_r: float
    net_return: float
    exit_reason: str
    bars_held: int
    return_6h: float
    return_24h: float
    distance_from_24h_high: float
    atr: float
    volume_ratio: float


def finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def symbol_from_path(path: Path) -> str:
    return path.name.split("_USDT_SWAP_5m_", 1)[0].upper()


def load_bars(path: Path) -> list[Bar]:
    """Read confirmed, valid candles and de-duplicate timestamps."""
    by_ts: dict[int, Bar] = {}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get("confirmed") is not True:
                    continue
                values = [finite(row.get(key)) for key in ("open", "high", "low", "close", "quote_volume")]
                ts = int(row["timestamp_ms"])
                if ts <= 0 or any(value is None for value in values):
                    continue
                o, high, low, close, volume = values
                if min(o, high, low, close) <= 0 or volume < 0 or high < max(o, close) or low > min(o, close) or high < low:
                    continue
                by_ts[ts] = Bar(ts, o, high, low, close, volume)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return [by_ts[ts] for ts in sorted(by_ts)]


def choose_paths(data_dir: Path, symbols: set[str]) -> dict[str, Path]:
    """Choose one newest snapshot per ledger symbol, avoiding duplicate exports."""
    manifest_path = data_dir / "manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            selected: dict[str, Path] = {}
            for contract in manifest.get("contracts", []):
                instrument = str(contract.get("instrument_id", ""))
                symbol = instrument.split("-", 1)[0].upper()
                filename = str(contract.get("file", ""))
                candidate = data_dir / filename
                if symbol in symbols and candidate.is_file():
                    selected[symbol] = candidate
            if selected:
                return selected
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    grouped: dict[str, list[Path]] = defaultdict(list)
    for path in data_dir.glob("*_USDT_SWAP_5m_*.jsonl.gz"):
        symbol = symbol_from_path(path)
        if symbol in symbols:
            grouped[symbol].append(path)
    return {symbol: sorted(paths, key=lambda item: item.name)[-1] for symbol, paths in grouped.items()}


def _true_range(bar: Bar, previous_close: float) -> float:
    return max(bar.high - bar.low, abs(bar.high - previous_close), abs(bar.low - previous_close))


def features_before_entry(bars: Sequence[Bar], entry_index: int, config: dict[str, object]) -> FeatureSnapshot | None:
    price = config["price_backtest"]
    assert isinstance(price, dict)
    atr_period = int(price["atr_period"])
    lookback_6h = int(price["lookback_6h_bars"])
    lookback_24h = int(price["lookback_24h_bars"])
    if entry_index <= max(atr_period, lookback_6h, lookback_24h):
        return None
    if bars[entry_index].ts - bars[entry_index - 1].ts != BAR_MS:
        return None
    prior = bars[:entry_index]
    close = prior[-1].close
    return_6h = close / prior[-lookback_6h].close - 1.0
    return_24h = close / prior[-lookback_24h].close - 1.0
    high_24h = max(bar.high for bar in prior[-lookback_24h:])
    distance = close / high_24h - 1.0
    ranges = [_true_range(prior[i], prior[i - 1].close if i else prior[i].open) for i in range(len(prior) - atr_period, len(prior))]
    atr = sum(ranges) / len(ranges) if ranges else 0.0
    volumes = [bar.quote_volume for bar in prior[-lookback_24h:]]
    median_volume = statistics.median(volumes) if volumes else 0.0
    volume_ratio = prior[-1].quote_volume / median_volume if median_volume > 0 else 0.0
    if not all(math.isfinite(value) for value in (return_6h, return_24h, distance, atr, volume_ratio)) or atr <= 0:
        return None
    return FeatureSnapshot(return_6h, return_24h, distance, atr, volume_ratio)


def _execution_price(raw_price: float, direction: str, slippage: float, entry: bool) -> float:
    if direction == "long":
        return raw_price * (1.0 + slippage) if entry else raw_price * (1.0 - slippage)
    return raw_price * (1.0 - slippage) if entry else raw_price * (1.0 + slippage)


def simulate_episode(episode: Episode, bars: Sequence[Bar], config: dict[str, object]) -> PriceTrade | None:
    price = config["price_backtest"]
    assert isinstance(price, dict)
    timestamps = [bar.ts for bar in bars]
    event_ms = int(episode.entry_time.astimezone(UTC).timestamp() * 1000)
    # bisect_right guarantees that the bill event's candle cannot provide a
    # future high/low used by the entry decision.
    entry_index = bisect.bisect_right(timestamps, event_ms)
    if entry_index >= len(bars) or entry_index == 0:
        return None
    features = features_before_entry(bars, entry_index, config)
    if features is None:
        return None
    if entry_index > 0 and bars[entry_index].ts - bars[entry_index - 1].ts != BAR_MS:
        return None
    direction = episode.direction
    slippage = float(price["slippage_one_way"])
    fee = float(price["fee_rate_one_way"])
    entry = _execution_price(bars[entry_index].open, direction, slippage, True)
    stop_atr = float(price["stop_atr"])
    target_r = float(price["target_r"])
    if direction == "long":
        stop = entry - stop_atr * features.atr
        risk = entry - stop
        target = entry + target_r * risk
    else:
        stop = entry + stop_atr * features.atr
        risk = stop - entry
        target = entry - target_r * risk
    last = min(len(bars) - 1, entry_index + int(price["max_hold_bars"]) - 1)
    exit_index = last
    raw_exit = bars[last].close
    reason = "time_exit"
    for index in range(entry_index, last + 1):
        bar = bars[index]
        if index > entry_index and bar.ts - bars[index - 1].ts != BAR_MS:
            exit_index = index - 1
            raw_exit = bars[exit_index].close
            reason = "data_gap_exit"
            break
        if direction == "long":
            if bar.open <= stop:
                exit_index, raw_exit, reason = index, bar.open, "stop_gap"; break
            if bar.low <= stop:
                exit_index, raw_exit, reason = index, stop, "stop"; break
            if bar.high >= target:
                exit_index, raw_exit, reason = index, target, "target"; break
        else:
            if bar.open >= stop:
                exit_index, raw_exit, reason = index, bar.open, "stop_gap"; break
            if bar.high >= stop:
                exit_index, raw_exit, reason = index, stop, "stop"; break
            if bar.low <= target:
                exit_index, raw_exit, reason = index, target, "target"; break
    exit_price = _execution_price(raw_exit, direction, slippage, False)
    signed_move = exit_price - entry if direction == "long" else entry - exit_price
    net_pnl_per_unit = signed_move - fee * (entry + exit_price)
    return PriceTrade(
        symbol=episode.symbol,
        direction=direction,
        behavior_entry_time=episode.entry_time.isoformat(),
        entry_time=datetime.fromtimestamp(bars[entry_index].ts / 1000, UTC).isoformat(),
        # Use candle end for portfolio occupancy even when a stop/target is
        # simulated inside the candle; OHLC cannot establish the exact fill
        # time without introducing an optimistic ordering assumption.
        exit_time=datetime.fromtimestamp((bars[exit_index].ts + BAR_MS) / 1000, UTC).isoformat(),
        entry=entry,
        exit=exit_price,
        stop=stop,
        target=target,
        risk=risk,
        net_r=net_pnl_per_unit / risk,
        net_return=net_pnl_per_unit / entry,
        exit_reason=reason,
        bars_held=exit_index - entry_index + 1,
        return_6h=features.return_6h,
        return_24h=features.return_24h,
        distance_from_24h_high=features.distance_from_24h_high,
        atr=features.atr,
        volume_ratio=features.volume_ratio,
    )


def policy_matches(trade: PriceTrade, policy: dict[str, object]) -> bool:
    direction = policy.get("direction", "any")
    if direction != "any" and trade.direction != direction:
        return False
    if "min_return_24h" in policy and trade.return_24h < float(policy["min_return_24h"]):
        return False
    if "max_return_24h" in policy and trade.return_24h > float(policy["max_return_24h"]):
        return False
    if "max_distance_from_24h_high" in policy and trade.distance_from_24h_high > float(policy["max_distance_from_24h_high"]):
        return False
    if "min_distance_from_24h_high" in policy and trade.distance_from_24h_high < float(policy["min_distance_from_24h_high"]):
        return False
    return True


def non_overlapping(trades: Sequence[PriceTrade], max_positions: int) -> tuple[list[PriceTrade], int]:
    """Apply the configured portfolio capacity to entry-labelled trades."""
    if max_positions < 1:
        raise ValueError("max_positions must be positive")
    active: list[int] = []
    accepted: list[PriceTrade] = []
    skipped = 0
    for trade in sorted(trades, key=lambda item: (item.entry_time, item.symbol)):
        entry_ms = int(datetime.fromisoformat(trade.entry_time).timestamp() * 1000)
        active = [exit_ms for exit_ms in active if exit_ms > entry_ms]
        if len(active) >= max_positions:
            skipped += 1
            continue
        active.append(int(datetime.fromisoformat(trade.exit_time).timestamp() * 1000))
        accepted.append(trade)
    return accepted, skipped


def metrics(trades: Sequence[PriceTrade]) -> dict[str, object]:
    values = [trade.net_r for trade in trades]
    gains = sum(value for value in values if value > 0)
    losses = -sum(value for value in values if value < 0)
    equity = peak = drawdown = 0.0
    for value in [trade.net_r for trade in sorted(trades, key=lambda item: (item.exit_time, item.symbol))]:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "wins": sum(value > 0 for value in values),
        "losses": sum(value < 0 for value in values),
        "win_rate": sum(value > 0 for value in values) / len(values) if values else 0.0,
        "total_r": sum(values),
        "average_r": statistics.fmean(values) if values else 0.0,
        "median_r": statistics.median(values) if values else 0.0,
        "profit_factor": gains / losses if losses else (999999999.0 if gains else 0.0),
        "max_drawdown_r": drawdown,
        "symbols": len({trade.symbol for trade in trades}),
        "exit_reasons": dict(Counter(trade.exit_reason for trade in trades)),
    }


def split_labels(trades: Sequence[PriceTrade], config: dict[str, object]) -> dict[int, str]:
    price = config["price_backtest"]
    assert isinstance(price, dict)
    train_ratio = float(price["train_ratio"])
    validation_ratio = float(price["validation_ratio"])
    ordered = sorted(trades, key=lambda item: item.entry_time)
    train_end = int(len(ordered) * train_ratio)
    validation_end = int(len(ordered) * (train_ratio + validation_ratio))
    return {id(trade): ("train" if index < train_end else "validation" if index < validation_end else "test") for index, trade in enumerate(ordered)}


def evaluate_policies(trades: Sequence[PriceTrade], config: dict[str, object]) -> tuple[list[dict[str, object]], dict[str, list[PriceTrade]]]:
    price = config["price_backtest"]
    assert isinstance(price, dict)
    labels = split_labels(trades, config)
    max_positions = int(price["max_concurrent_positions"])
    rows: list[dict[str, object]] = []
    selected: dict[str, list[PriceTrade]] = {}
    for policy in price["candidate_policies"]:
        assert isinstance(policy, dict)
        name = str(policy["name"])
        matched = [trade for trade in trades if policy_matches(trade, policy)]
        accepted, skipped = non_overlapping(matched, max_positions)
        selected[name] = accepted
        for split in ("all", "train", "validation", "test"):
            scoped = accepted if split == "all" else [trade for trade in accepted if labels[id(trade)] == split]
            rows.append({"policy": name, "split": split, "capacity_skipped": skipped if split == "all" else 0, **metrics(scoped)})
    return rows, selected


def choose_policy(rows: Sequence[dict[str, object]], config: dict[str, object]) -> str:
    price = config["price_backtest"]
    assert isinstance(price, dict)
    minimum = int(price["minimum_train_trades"])
    candidates = [row for row in rows if row["split"] == "train" and int(row["trades"]) >= minimum]
    if not candidates:
        return "all_behavior_entries"
    return str(max(candidates, key=lambda row: (float(row["total_r"]), float(row["profit_factor"])))["policy"])


def _json_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(ledger: Path, data_dir: Path, config_path: Path, output_dir: Path) -> dict[str, object]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    rows, manifest = read_ledger(ledger)
    groups = aggregate_order_groups(rows)
    events = collect_events(rows)
    episodes = infer_episodes(groups, events)
    paths = choose_paths(data_dir, {episode.symbol.split("-", 1)[0] for episode in episodes})
    bars_by_symbol = {symbol: load_bars(path) for symbol, path in paths.items()}
    simulated: list[PriceTrade] = []
    skipped = Counter()
    for episode in episodes:
        symbol = episode.symbol.split("-", 1)[0]
        bars = bars_by_symbol.get(symbol)
        if not bars:
            skipped["missing_symbol_data"] += 1
            continue
        trade = simulate_episode(episode, bars, config)
        if trade is None:
            skipped["insufficient_or_gapped_history"] += 1
        else:
            simulated.append(trade)
    evaluations, selected_by_policy = evaluate_policies(simulated, config)
    selected_policy = choose_policy(evaluations, config)
    selected_trades = selected_by_policy[selected_policy]
    selected_walk_forward = [row for row in evaluations if row["policy"] == selected_policy]
    validation = next((row for row in selected_walk_forward if row["split"] == "validation"), None)
    test = next((row for row in selected_walk_forward if row["split"] == "test"), None)
    promotion = {
        "status": "not_promoted",
        "reason": "validation_or_test_total_r_not_positive",
        "validation_total_r": validation["total_r"] if validation else None,
        "test_total_r": test["total_r"] if test else None,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    trade_fields = list(asdict(simulated[0]).keys()) if simulated else list(PriceTrade.__dataclass_fields__.keys())
    with (output_dir / "price_backtest_trades.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=trade_fields)
        writer.writeheader()
        writer.writerows(asdict(trade) for trade in selected_trades)
    fields = list(evaluations[0].keys()) if evaluations else ["policy", "split"]
    with (output_dir / "price_policy_evaluation.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(evaluations)
    report = {
        "strategy_id": config.get("strategy_id"),
        "version": config.get("version"),
        "selected_policy": selected_policy,
        "selected_policy_metrics": metrics(selected_trades),
        "selected_policy_walk_forward": selected_walk_forward,
        "promotion": promotion,
        "behavior_episodes": len(episodes),
        "price_trades_simulated": len(simulated),
        "skipped": dict(skipped),
        "ledger_source_sha256": manifest["source_sha256"],
        "config_sha256": _json_hash(config_path),
        "kline_sources": {symbol: path.name for symbol, path in paths.items()},
        "execution_assumptions": config["price_backtest"],
        "limitations": [
            "Bill episodes are observed entry labels, not independently generated signals.",
            "OHLC candles do not reveal intrabar order; a same-candle stop is resolved before target.",
            "Funding, liquidation, contract multiplier, minimum order size and mark-price effects are not modeled.",
            "Results are normalized in R and are not account USDT returns without a position-sizing rule.",
            "One month of bill labels is too short to establish a stable edge; validation and test results govern promotion.",
        ],
    }
    (output_dir / "price_backtest_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, required=True, help="OKX unified bill ZIP export")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA, help="confirmed OKX 5m JSONL.GZ directory")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=LAB / "results")
    args = parser.parse_args()
    run(args.ledger, args.data_dir, args.config, args.output_dir)


if __name__ == "__main__":
    main()
