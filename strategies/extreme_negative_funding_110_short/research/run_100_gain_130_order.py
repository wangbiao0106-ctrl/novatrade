#!/usr/bin/env python3
"""Backtest the price-only variant: >100% day gain, 130% limit short.

This keeps the established 5m execution and split-exit model while changing
only the qualification threshold and limit price. Funding is bypassed
explicitly for this sensitivity run and the baseline config is not modified.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


LAB = Path(__file__).resolve().parents[1]
ROOT = LAB.parents[1]
SOURCE = LAB / "src" / "backtest.py"
DEFAULT_CONFIG = LAB / "config" / "strategy.json"
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_OUTPUT = LAB / "results" / "gain_100_order_130"


def load_backtest_module():
    import importlib.util

    spec = importlib.util.spec_from_file_location("extreme_negative_funding_backtest", SOURCE)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {SOURCE}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


BT = load_backtest_module()


def run(data_dir: Path, config_path: Path, output_dir: Path, stop_pct: float = 0.2) -> dict:
    if stop_pct < 0:
        raise ValueError("stop_pct must be non-negative")
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["signal"]["utc_day_gain_gt"] = 1.0
    config["signal"]["trigger_gain_pct"] = 1.0
    config["entry"]["limit_price_factor"] = 2.3
    config["risk"]["initial_stop_above_entry_pct"] = stop_pct
    config["risk"]["tp1_favorable_move_pct"] = 0.2
    config["risk"]["tp2_additional_favorable_move_pct_from_entry"] = 0.2
    output_dir.mkdir(parents=True, exist_ok=True)
    variant_config_path = output_dir / "variant_config.json"
    variant_config_path.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    report = BT.run(data_dir, variant_config_path, output_dir, None, ignore_funding=True)
    report["study"] = "price_only_gain_gt_100_limit_130_short"
    report["variant"] = {
        "day_gain_gate": ">100%",
        "limit_price_factor": 2.3,
        "limit_price_description": "130% above UTC-day open",
        "leverage": 1.0,
        "initial_stop_above_entry_pct": stop_pct,
        "tp1_favorable_move_pct": 0.2,
        "tp1_close_fraction": 0.5,
        "tp2_cumulative_favorable_move_pct": 0.4,
        "tp2_price": "0.60 x effective entry",
        "entry_activation": "next contiguous 5m bar",
    }
    report["results"]["success_rate_definition"] = "win_rate: net_return > 0"
    report["limitations"].append(
        "This variant interprets 40% take profit as a cumulative 40% favorable move, "
        "with the established 20% partial exit and breakeven stop."
    )
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scan", action="store_true", help="run the independent price-only variant")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--stop-pct", type=float, default=0.2,
                        help="initial stop distance above entry, e.g. 0.30 for 30%%")
    args = parser.parse_args()
    if not args.scan:
        parser.error("--scan is required")
    report = run(args.data_dir, args.config, args.output_dir, stop_pct=args.stop_pct)
    results = report["results"]
    print(json.dumps({
        "signals": results["signals"],
        "trades": results["trades"],
        "win_rate": results["win_rate"],
        "total_net_return": results["total_net_return"],
        "compound_return": results["compound_return"],
        "profit_factor": results["profit_factor"],
        "max_drawdown": results["max_drawdown"],
        "report": str(args.output_dir / "report.json"),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
