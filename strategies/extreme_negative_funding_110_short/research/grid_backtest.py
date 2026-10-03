#!/usr/bin/env python3
"""Run the small price-only parameter grid for the independent strategy study.

The production strategy rules remain unchanged; ``src/backtest.py`` exposes the
same simulator with optional research overrides. This runner varies only the
three requested dimensions:

* trigger gain: 110%, 120%, or 130% above the UTC-day open;
* activation wait: 1, 2, or 3 complete 5-minute bars after the signal bar;
* initial stop: 10%, 15%, or 20% above the effective short entry.

Funding is bypassed explicitly because this is a price-only sensitivity study.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path


LAB = Path(__file__).resolve().parents[1]
ROOT = LAB.parents[1]
SOURCE = LAB / "src" / "backtest.py"
DEFAULT_CONFIG = LAB / "config" / "strategy.json"
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_OUTPUT = LAB / "results" / "grid"
VALIDATION_SPLIT = datetime(2026, 4, 1, tzinfo=timezone.utc)
VALIDATION_MIN_EVENTS = 30
TRIGGER_GAINS = (1.10, 1.20, 1.30)
WAIT_BARS = (1, 2, 3)
STOP_PCTS = (0.10, 0.15, 0.20)


def load_backtest_module():
    spec = importlib.util.spec_from_file_location("extreme_negative_funding_backtest", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BT = load_backtest_module()


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("\n", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def variant_id(trigger_gain: float, wait_bars: int, stop_pct: float) -> str:
    return f"trigger_{round(trigger_gain * 100):03d}_wait{wait_bars}_stop{round(stop_pct * 100):02d}"


def is_validation(signal_ts: int) -> bool:
    return datetime.fromtimestamp(signal_ts / 1000, timezone.utc) >= VALIDATION_SPLIT


def aggregate_metrics(trades: list) -> dict:
    metrics = BT.trade_metrics(trades)
    return {
        "trades": metrics["trades"],
        "wins": metrics["wins"],
        "losses": metrics["losses"],
        "win_rate": metrics["win_rate"],
        "total_net_return": metrics["total_net_return"],
        "compound_return": metrics["compound_return"],
        "average_net_return": metrics["average_net_return"],
        "profit_factor": metrics["profit_factor"],
        "max_drawdown": metrics["max_drawdown"],
        "exit_reasons": metrics["exit_reasons"],
    }


def run(data_dir: Path, config_path: Path, output_dir: Path) -> dict:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    allowed = BT.allowed_symbols(config)
    sources: list[tuple[str, Path, list]] = []
    quality_counts: Counter[str] = Counter()
    for symbol, path in BT.choose_paths(data_dir, allowed):
        bars, quality = BT.load_bars(path)
        sources.append((symbol, path, bars))
        quality_counts.update(quality)

    signals_by_trigger: dict[float, dict[str, list]] = {}
    for trigger_gain in TRIGGER_GAINS:
        trigger_config = json.loads(json.dumps(config))
        trigger_config["signal"]["trigger_gain_pct"] = trigger_gain
        by_symbol: dict[str, list] = {}
        counts = Counter()
        for symbol, _path, bars in sources:
            signals, local_counts, _ = BT.day_signals(symbol, bars, trigger_config, {}, ignore_funding=True)
            by_symbol[symbol] = signals
            counts.update(local_counts)
        signals_by_trigger[trigger_gain] = by_symbol

    all_rows: list[dict] = []
    all_trade_rows: list[dict] = []
    variants: list[dict] = []
    for trigger_gain in TRIGGER_GAINS:
        for wait_bars in WAIT_BARS:
            for stop_pct in STOP_PCTS:
                current_id = variant_id(trigger_gain, wait_bars, stop_pct)
                signals = [signal for values in signals_by_trigger[trigger_gain].values() for signal in values]
                trades: list = []
                rejected = Counter()
                for symbol, _path, bars in sources:
                    for signal in signals_by_trigger[trigger_gain][symbol]:
                        trade, reason = BT.simulate(signal, bars, config,
                                                    entry_wait_bars=wait_bars,
                                                    initial_stop_pct=stop_pct)
                        if trade is None:
                            rejected[reason or "unknown"] += 1
                        else:
                            trades.append(trade)
                            all_trade_rows.append(asdict(trade) | {
                                "variant": current_id,
                                "trigger_gain_pct": trigger_gain,
                                "wait_bars": wait_bars,
                                "initial_stop_pct": stop_pct,
                                "signal_utc": BT.iso(trade.signal_ts),
                                "entry_utc": BT.iso(trade.entry_ts),
                                "exit_utc": BT.iso(trade.exit_ts),
                            })
                validation_signals = [signal for signal in signals if is_validation(signal.timestamp)]
                train_signals = [signal for signal in signals if not is_validation(signal.timestamp)]
                validation_trades = [trade for trade in trades if is_validation(trade.signal_ts)]
                train_trades = [trade for trade in trades if not is_validation(trade.signal_ts)]
                all_metrics = aggregate_metrics(trades)
                train_metrics = aggregate_metrics(train_trades)
                validation_metrics = aggregate_metrics(validation_trades)
                row = {
                    "variant": current_id,
                    "trigger_gain_pct": trigger_gain,
                    "wait_bars": wait_bars,
                    "initial_stop_pct": stop_pct,
                    "independent_events": len(signals),
                    "train_independent_events": len(train_signals),
                    "validation_independent_events": len(validation_signals),
                    "validation_events_at_least_30": len(validation_signals) >= VALIDATION_MIN_EVENTS,
                    "signals": len(signals),
                    "trades": len(trades),
                    "unfilled_or_rejected": sum(rejected.values()),
                    "train_trades": len(train_trades),
                    "validation_trades": len(validation_trades),
                    "validation_sufficient_for_selection": len(validation_signals) >= VALIDATION_MIN_EVENTS,
                    "win_rate": all_metrics["win_rate"],
                    "train_win_rate": train_metrics["win_rate"],
                    "validation_win_rate": validation_metrics["win_rate"],
                    "total_net_return": all_metrics["total_net_return"],
                    "compound_return": all_metrics["compound_return"],
                    "average_net_return": all_metrics["average_net_return"],
                    "profit_factor": all_metrics["profit_factor"],
                    "max_drawdown": all_metrics["max_drawdown"],
                    "train_profit_factor": train_metrics["profit_factor"],
                    "train_compound_return": train_metrics["compound_return"],
                    "validation_profit_factor": validation_metrics["profit_factor"],
                    "validation_compound_return": validation_metrics["compound_return"],
                    "validation_average_net_return": validation_metrics["average_net_return"],
                    "exit_reasons": all_metrics["exit_reasons"],
                    "rejections": dict(rejected),
                }
                variants.append(row)
                all_rows.append({key: (json.dumps(value, ensure_ascii=False, sort_keys=True)
                                       if isinstance(value, (dict, list)) else value)
                                 for key, value in row.items()})

    output_dir.mkdir(parents=True, exist_ok=True)
    write_csv(output_dir / "grid.csv", all_rows)
    write_csv(output_dir / "trades.csv", all_trade_rows)
    report = {
        "strategy": config["strategy"],
        "study": "small_parameter_grid_price_only",
        "baseline_unchanged": True,
        "validation_mode": "price_only_ignore_funding",
        "parameters": {
            "trigger_gain_pct": list(TRIGGER_GAINS),
            "activation_wait_bars": list(WAIT_BARS),
            "initial_stop_above_entry_pct": list(STOP_PCTS),
            "fixed": {
                "utc_day_gain_gt": config["signal"]["utc_day_gain_gt"],
                "limit_price_factor": config["entry"]["limit_price_factor"],
                "tp1_favorable_move_pct": config["risk"]["tp1_favorable_move_pct"],
                "tp2_additional_favorable_move_pct_from_entry": config["risk"]["tp2_additional_favorable_move_pct_from_entry"],
                "tp1_close_fraction": config["risk"]["tp1_close_fraction"],
                "fee_rate_one_way": config["costs"]["fee_rate_one_way"],
                "slippage_one_way": config["costs"]["slippage_one_way"],
            },
        },
        "independent_event_definition": "one first-crossing signal per (symbol, UTC day); event count is a grouping count, not proof of statistical independence",
        "validation": {
            "split_utc": VALIDATION_SPLIT.isoformat().replace("+00:00", "Z"),
            "minimum_validation_independent_events": VALIDATION_MIN_EVENTS,
            "selection_rule": "Do not select or merge a variant from this grid unless its validation event count is at least 30; this run does not auto-select a winner.",
            "caveat": "The available sample may be below the requested validation threshold. Results remain exploratory price-only sensitivity evidence and do not validate the funding-gated production candidate.",
        },
        "data": {
            "directory": str(data_dir),
            "symbols": len(sources),
            "quality_totals": dict(quality_counts),
        },
        "variants": variants,
        "outputs": {"grid_csv": str(output_dir / "grid.csv"), "trades_csv": str(output_dir / "trades.csv")},
    }
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="run the explicit 27-variant research grid")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    if not args.scan:
        parser.error("--scan is required")
    report = run(args.data_dir, args.config, args.output_dir)
    print(json.dumps({"variants": len(report["variants"]), "grid": report["outputs"]["grid_csv"],
                      "report": str(args.output_dir / "report.json")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
