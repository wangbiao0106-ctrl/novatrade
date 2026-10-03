#!/usr/bin/env python3
"""Run the fixed-core hedge and tactical-spot grid study."""
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


def data_file(symbol: str) -> Path | None:
    matches = sorted(DATA.glob(f"{symbol}_USDT_SWAP_5m_*.jsonl.gz"))
    return matches[-1] if matches else None


def run_one(symbol: str, leverage: float, config: dict[str, object], output: Path) -> dict[str, object]:
    path = data_file(symbol)
    if path is None:
        return {"symbol": symbol, "leverage": leverage, "available": False}
    candidate = copy.deepcopy(config)
    candidate["portfolio"]["leverage"] = leverage  # type: ignore[index]
    bars = module.load_jsonl(path)
    trades, rebalances = module.backtest(symbol, bars, config=candidate)
    row = module.summary(trades)
    row.update({
        "symbol": symbol,
        "leverage": leverage,
        "available": True,
        "source": str(path.relative_to(ROOT)),
        "rebalances": len(rebalances),
        "core_hedge_violations": sum(
            trade.exit_reason == "core_hedge_violation" for trade in trades
        ),
    })
    output.mkdir(parents=True, exist_ok=True)
    module.write_outputs(output, trades, rebalances)
    return row


def main() -> None:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    symbols = ["BTC", "ETH", "OKB"]
    leverages = [2.0, 3.0, 5.0, 10.0]
    output = LAB / "results" / "fixed_short_grid"
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        for leverage in leverages:
            rows.append(run_one(symbol, leverage, config, output / symbol.lower() / f"{leverage:g}x"))
    report = {
        "study": "fixed_core_hedge_tactical_spot_grid",
        "symbols": symbols,
        "leverages": leverages,
        "threshold_pct": config["rebalance"]["threshold_pct"],  # type: ignore[index]
        "max_tactical_spot_fraction_of_core": config["rebalance"]["max_tactical_spot_fraction_of_core"],  # type: ignore[index]
        "buy_reserve_fraction": config["rebalance"]["buy_reserve_fraction"],  # type: ignore[index]
        "rows": rows,
        "limitations": [
            "spot and perpetual share one OHLCV proxy",
            "funding, basis, mark price and order book are not modelled",
        ],
    }
    output.mkdir(parents=True, exist_ok=True)
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    fields = sorted({key for row in rows for key in row})
    with (output / "summary.csv").open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
