#!/usr/bin/env python3
"""Parameter evidence for the liquid-crypto trend rule.

Scans the two parameters that actually move the result -- the direction
duration cap and the volatility target -- and runs a leave-one-month-out
selection so the recorded values are not a single in-sample peak.

Writes ``results/parameter_scan.csv``, ``results/walk_forward.json`` and
``results/benchmark_comparison.csv``.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import sys
from pathlib import Path

import numpy as np


LAB = Path(__file__).resolve().parents[1]
ROOT = Path(__file__).resolve().parents[3]


def load_backtest():
    spec = importlib.util.spec_from_file_location("lctl_backtest", LAB / "src" / "backtest.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def evaluate(bt, panel, config, universe, max_bars: int, vol_target: float) -> dict:
    scope = config["universe"]
    eligible = bt.eligibility(panel["volume"], scope["min_quote_volume_24h_usdt"],
                              scope["volume_lookback_bars"], scope["volume_min_bars"])
    positions = bt.trend_positions(panel["prices"], config["signal"]["ma_window_bars"],
                                   config["signal"]["hysteresis_pct"] / 100.0, max_bars)
    positions = bt.apply_vol_target(positions, panel["returns"], vol_target,
                                    config["position"]["vol_lookback_bars"],
                                    config["position"]["vol_scale_cap"])
    positions = np.where(positions * eligible > 0, positions * eligible, 0.0)
    fee = config["costs"]["fee_rate_one_way"] + config["costs"]["slippage_one_way"]
    net = bt.portfolio(panel, positions, eligible, fee)
    months = {}
    for label, lo, hi in bt.month_slices(panel["axis"]):
        months[label] = float(np.cumprod(1 + net[lo:hi])[-1] - 1)
    return {"metrics": bt.metrics(net).as_dict(), "months": months, "series": net}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "data/kline/okx/swap/5m")
    parser.add_argument("--config", type=Path, default=LAB / "config" / "strategy.json")
    parser.add_argument("--universe", type=Path, default=LAB / "config" / "universe.json")
    parser.add_argument("--output-dir", type=Path, default=LAB / "results")
    parser.add_argument("--cache-dir", type=Path, default=LAB / "results" / "cache" / "1h")
    args = parser.parse_args()

    bt = load_backtest()
    config = bt.load_config(args.config)
    universe = bt.load_config(args.universe)
    excluded = {s.upper() for s in universe.get("exclude", [])}
    symbols = {p.name.split("_USDT_SWAP_5m_")[0] for p in args.data_dir.glob("*.jsonl.gz")}
    symbols = {s for s in symbols if s not in excluded}
    panel = bt.build_panel(bt.load_hourly(args.data_dir, symbols, args.cache_dir))

    bars = [10 ** 6, 240, 168, 120, 96, 72, 60, 36]
    targets = [1.0, 1.5, 2.0, 2.5]
    rows = []
    cache: dict[tuple[int, float], dict] = {}
    for max_bars in bars:
        for vol_target in targets:
            result = evaluate(bt, panel, config, universe, max_bars, vol_target)
            cache[(max_bars, vol_target)] = result
            rows.append({
                "max_direction_bars": max_bars,
                "vol_target_annual": vol_target,
                "total_return": result["metrics"]["total_return"],
                "annual_return": result["metrics"]["annual_return"],
                "max_drawdown": result["metrics"]["max_drawdown"],
                "sharpe": result["metrics"]["sharpe"],
                "calmar": result["metrics"]["calmar"],
            })

    args.output_dir.mkdir(parents=True, exist_ok=True)
    with (args.output_dir / "parameter_scan.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    labels = sorted({label for result in cache.values() for label in result["months"]})
    walk = {"selection_monthly": [], "out_of_sample": {}}
    selected: list[tuple[int, float]] = []
    for held in labels:
        best, best_score = None, -1e9
        for key, result in cache.items():
            rest = [v for label, v in result["months"].items() if label != held]
            if len(rest) < 3:
                continue
            score = float(np.prod([1 + v for v in rest]) - 1)
            if score > best_score:
                best, best_score = key, score
        if best is None:
            continue
        selected.append(best)
        walk["selection_monthly"].append({"held_out": held, "selected": list(best),
                                          "month_return": cache[best]["months"][held]})
    walk["out_of_sample"]["compounded"] = float(np.prod([1 + row["month_return"]
                                                         for row in walk["selection_monthly"]]) - 1)
    walk["out_of_sample"]["distinct_selections"] = len(set(selected))
    walk["top_by_sharpe"] = max(rows, key=lambda r: r["sharpe"])
    walk["top_by_return"] = max(rows, key=lambda r: r["total_return"])
    (args.output_dir / "walk_forward.json").write_text(json.dumps(walk, indent=2, ensure_ascii=False) + "\n")

    basket = bt.equal_weight_benchmark(panel, bt.eligibility(
        panel["volume"], config["universe"]["min_quote_volume_24h_usdt"],
        config["universe"]["volume_lookback_bars"], config["universe"]["volume_min_bars"]))
    broad = bt.full_universe_benchmark(panel)
    bitcoin = bt.symbol_benchmark(panel, config.get("benchmarks", {}).get("reference_symbol", "BTC"))
    with (args.output_dir / "benchmark_comparison.csv").open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["series", "total_return", "annual_return", "max_drawdown", "sharpe"])
        current = cache[(config["signal"]["max_direction_bars"],
                         config["position"]["vol_target_annual"])]
        for name, series in (("strategy", current["series"]),
                             ("eligible_basket", basket),
                             ("full_universe", broad),
                             ("BTC", bitcoin)):
            stat = bt.metrics(series)
            writer.writerow([name, stat.total_return, stat.annual_return,
                             stat.max_drawdown, stat.sharpe])

    print(json.dumps({"scanned": len(rows), "walk_forward": walk["out_of_sample"],
                      "top_by_sharpe": walk["top_by_sharpe"]}, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
