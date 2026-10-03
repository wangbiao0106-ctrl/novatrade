#!/usr/bin/env python3
"""Run the SPHA price-layer study over the available mainstream snapshots."""
from __future__ import annotations

import copy
import csv
import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
LAB = ROOT / "strategies" / "spot_perp_hedged_accumulation"
DATA = ROOT / "data" / "kline" / "okx" / "swap" / "5m"
CONFIG_PATH = LAB / "config" / "strategy.json"
SOURCE = LAB / "src" / "hedged_backtest.py"
spec = importlib.util.spec_from_file_location("spha_backtest", SOURCE)
assert spec is not None and spec.loader is not None
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)
BAR_CACHE: dict[str, list[module.Bar]] = {}


def files_by_symbol(symbols: list[str]) -> dict[str, Path]:
    selected: dict[str, Path] = {}
    for symbol in symbols:
        matches = sorted(DATA.glob(f"{symbol}_USDT_SWAP_5m_*.jsonl.gz"))
        if matches:
            selected[symbol] = matches[-1]
    return selected


def run(config: dict[str, object], symbols: list[str]) -> tuple[list[dict[str, object]], list[module.Trade]]:
    rows: list[dict[str, object]] = []
    all_trades: list[module.Trade] = []
    for symbol, path in files_by_symbol(symbols).items():
        if symbol not in BAR_CACHE:
            BAR_CACHE[symbol] = module.load_jsonl(path)
        trades, rebalances = module.backtest(symbol, BAR_CACHE[symbol], config=config)
        row = module.summary(trades)
        row.update({"symbol": symbol, "source": str(path.relative_to(ROOT)), "rebalances": len(rebalances)})
        rows.append(row)
        all_trades.extend(trades)
    return rows, all_trades


def cross_symbol_summary(rows: list[dict[str, object]]) -> dict[str, object]:
    """Summarize independent one-symbol instances without false pooled compounding."""
    active = [row for row in rows if int(row["trades"]) > 0]
    returns = [float(row["compound_return_pct"]) for row in active]
    drawdowns = [float(row["max_drawdown_pct"]) for row in active]
    win_rates = [float(row["win_rate"]) for row in active]
    equity_deviations = [float(row["mean_max_position_equity_deviation_pct"]) for row in active]
    deviations = [float(row["mean_max_notional_deviation_pct"]) for row in active]
    spot_gain = [float(row["mean_max_spot_gain_pct"]) for row in active]
    tactical_delta = [float(row.get("mean_max_delta_base_pct", 0.0)) for row in active]
    max_tactical_delta = [float(row.get("max_max_delta_base_pct", 0.0)) for row in active]
    reserve_end = [float(row["mean_reserve_end"]) for row in active]
    reserve_min = [float(row["mean_min_reserve_cash"]) for row in active]
    return {
        "symbols_with_trades": len(active),
        "campaigns": sum(int(row["trades"]) for row in active),
        "mean_symbol_compound_return_pct": sum(returns) / len(returns) if returns else 0.0,
        "median_symbol_compound_return_pct": sorted(returns)[len(returns) // 2] if returns else 0.0,
        "min_symbol_compound_return_pct": min(returns) if returns else 0.0,
        "max_symbol_compound_return_pct": max(returns) if returns else 0.0,
        "mean_symbol_win_rate": sum(win_rates) / len(win_rates) if win_rates else 0.0,
        "mean_symbol_max_drawdown_pct": sum(drawdowns) / len(drawdowns) if drawdowns else 0.0,
        "worst_symbol_max_drawdown_pct": max(drawdowns) if drawdowns else 0.0,
        "mean_campaign_max_position_equity_deviation_pct": (
            sum(equity_deviations) / len(equity_deviations) if equity_deviations else 0.0
        ),
        "max_campaign_position_equity_deviation_pct": max(equity_deviations) if equity_deviations else 0.0,
        "mean_campaign_max_notional_deviation_pct": sum(deviations) / len(deviations) if deviations else 0.0,
        "max_campaign_notional_deviation_pct": max(deviations) if deviations else 0.0,
        "mean_max_spot_gain_pct": sum(spot_gain) / len(spot_gain) if spot_gain else 0.0,
        "mean_max_delta_base_pct": sum(tactical_delta) / len(tactical_delta) if tactical_delta else 0.0,
        "max_max_delta_base_pct": max(max_tactical_delta) if max_tactical_delta else 0.0,
        "mean_reserve_end": sum(reserve_end) / len(reserve_end) if reserve_end else 0.0,
        "mean_min_reserve_cash": sum(reserve_min) / len(reserve_min) if reserve_min else 0.0,
        "total_rebalances": sum(int(row["rebalances"]) for row in active),
        "total_fees_paid": sum(float(row["total_fees_paid"]) for row in active),
        "total_funding_paid": sum(float(row["total_funding_paid"]) for row in active),
    }


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    symbols = list(config["universe"]["symbols"])  # type: ignore[index]
    baseline_rows, _ = run(config, symbols)
    sensitivity: list[dict[str, object]] = []
    for threshold in (0.03, 0.04, 0.08):
        for fraction in (0.10, 0.25, 0.50):
            candidate = copy.deepcopy(config)
            candidate["rebalance"]["threshold_pct"] = threshold  # type: ignore[index]
            candidate["rebalance"]["buy_reserve_fraction"] = fraction  # type: ignore[index]
            rows, _ = run(candidate, symbols)
            result = cross_symbol_summary(rows)
            result.update({"threshold_pct": threshold, "buy_reserve_fraction": fraction,
                           "symbols_with_trades": sum(row["trades"] > 0 for row in rows)})
            sensitivity.append(result)

    output = LAB / "results" / "mainstream"
    output.mkdir(parents=True, exist_ok=True)
    report = {
        "study": "price_layer_only",
        "symbols_requested": symbols,
        "symbols_available": sorted(row["symbol"] for row in baseline_rows),
        "baseline_by_symbol": baseline_rows,
        "baseline_summary": cross_symbol_summary(baseline_rows),
        "sensitivity": sensitivity,
        "limitations": ["spot and perpetual share one OHLCV proxy", "funding, basis, mark price and order book are not modelled"],
    }
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output / "by_symbol.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = sorted({key for row in baseline_rows for key in row})
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(baseline_rows)
    print(json.dumps(report["baseline_summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
