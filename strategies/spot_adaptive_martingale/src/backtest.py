#!/usr/bin/env python3
"""Price-layer backtest for a finite, spot-only adaptive martingale grid.

The repository contains OKX USDT perpetual candles, not matched spot trades.
The candles are used as a price proxy for the requested spot study. Signals
are formed on a confirmed 1h close and orders fill at the next 1h open.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path


HOUR_MS = 3_600_000
ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data/kline/okx/swap/5m"
LAB_DIR = ROOT / "strategies/spot_adaptive_martingale"
DEFAULT_CONFIG = LAB_DIR / "config/strategy.json"
DEFAULT_OUTPUT = LAB_DIR / "results"


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


@dataclass(frozen=True)
class Trade:
    symbol: str
    side: str
    timestamp: int
    price: float
    quantity: float
    quote: float
    fee: float
    reason: str
    level: int
    equity_after: float


def ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    out: list[float] = []
    for value in values:
        out.append(value if not out else alpha * value + (1.0 - alpha) * out[-1])
    return out


def atr(bars: list[Bar], period: int) -> list[float]:
    true_ranges: list[float] = []
    for i, bar in enumerate(bars):
        previous = bars[i - 1].close if i else bar.close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
    return ema(true_ranges, period)


def resample_1h(rows: list[Bar]) -> list[Bar]:
    groups: dict[int, list[Bar]] = {}
    for bar in rows:
        hour = bar.ts // HOUR_MS * HOUR_MS
        groups.setdefault(hour, []).append(bar)
    result: list[Bar] = []
    for hour, group in sorted(groups.items()):
        group.sort(key=lambda item: item.ts)
        expected = [hour + i * 300_000 for i in range(12)]
        if [item.ts for item in group] != expected:
            continue
        result.append(Bar(hour, group[0].open, max(x.high for x in group),
                          min(x.low for x in group), group[-1].close,
                          sum(x.quote_volume for x in group)))
    return result


def load_bars(path: Path) -> list[Bar]:
    rows: dict[int, Bar] = {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get("confirmed") is not True:
                    continue
                bar = Bar(int(row["timestamp_ms"]), float(row["open"]), float(row["high"]),
                          float(row["low"]), float(row["close"]), float(row.get("quote_volume", 0.0)))
                if min(bar.open, bar.high, bar.low, bar.close) <= 0 or bar.low > min(bar.open, bar.close) or bar.high < max(bar.open, bar.close):
                    continue
                rows[bar.ts] = bar
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return resample_1h(list(rows.values()))


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def baseline_buy_hold(bars: list[Bar], start: int, capital: float, fee: float, slip: float) -> dict[str, float]:
    entry = bars[start].open * (1.0 + slip)
    qty = capital / (entry * (1.0 + fee))
    curve = [capital]
    for bar in bars[start:]:
        curve.append(qty * bar.close)
    peak = curve[0]
    drawdown = 0.0
    for equity in curve:
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak * 100.0)
    final = curve[-1]
    return {"final_equity": final, "return_pct": (final / capital - 1.0) * 100.0,
            "max_drawdown_pct": drawdown}


def simulate(symbol: str, bars: list[Bar], config: dict[str, object], capital: float = 10_000.0) -> tuple[dict[str, object], list[Trade], list[dict[str, float]]]:
    if len(bars) < 150:
        raise ValueError(f"{symbol}: only {len(bars)} complete hourly bars")
    fees = config["costs"]  # type: ignore[index]
    grid = config["grid"]  # type: ignore[index]
    risk = config["risk"]  # type: ignore[index]
    initial_fraction = float(config["portfolio"]["initial_spot_fraction"])  # type: ignore[index]
    min_inventory_fraction = float(config["portfolio"]["min_inventory_fraction_of_initial"])  # type: ignore[index]
    fee = float(fees["fee_rate"])
    slip = float(fees["slippage_rate"])
    closes = [bar.close for bar in bars]
    fast = ema(closes, int(config["indicators"]["ema_fast"]))  # type: ignore[index]
    slow = ema(closes, int(config["indicators"]["ema_slow"]))  # type: ignore[index]
    ranges = atr(bars, int(config["indicators"]["atr_period"]))  # type: ignore[index]
    start = max(int(config["indicators"]["ema_slow"]), 120)  # type: ignore[index]

    cash = capital
    quantity = 0.0
    average_cost = 0.0
    min_average_cost = math.inf
    anchor = 0.0
    level = 0
    max_level_seen = 0
    pending: tuple[str, float, str, int] | None = None
    cooldown_until = -1
    fees_paid = 0.0
    realized = 0.0
    buys = sells = 0
    emergency_exits = 0
    initial_quantity = 0.0
    trades: list[Trade] = []
    equity_curve: list[dict[str, float]] = []

    def equity(price: float) -> float:
        return cash + quantity * price

    def execute(bar: Bar, order: tuple[str, float, str, int]) -> None:
        nonlocal cash, quantity, average_cost, min_average_cost, anchor, level, fees_paid, realized, buys, sells, emergency_exits, initial_quantity
        side, requested_qty, reason, decision_index = order
        if side == "buy":
            price = bar.open * (1.0 + slip)
            max_quote = max(0.0, cash - float(risk["reserve_floor_quote"]))
            requested_qty = min(requested_qty, max_quote / (price * (1.0 + fee)))
            if requested_qty <= 0:
                return
            quote = requested_qty * price
            fee_paid = quote * fee
            cash -= quote + fee_paid
            average_cost = (average_cost * quantity + quote + fee_paid) / (quantity + requested_qty)
            quantity += requested_qty
            min_average_cost = min(min_average_cost, average_cost)
            fees_paid += fee_paid
            buys += 1
            if anchor <= 0:
                anchor = price
            if reason == "initial_position":
                initial_quantity = quantity
        else:
            floor_qty = 0.0 if reason == "emergency_stop" else initial_quantity * min_inventory_fraction
            requested_qty = min(requested_qty, max(0.0, quantity - floor_qty))
            if requested_qty <= 0:
                return
            price = bar.open * (1.0 - slip)
            quote = requested_qty * price
            fee_paid = quote * fee
            cash += quote - fee_paid
            realized += (price - average_cost) * requested_qty - fee_paid
            quantity -= requested_qty
            fees_paid += fee_paid
            sells += 1
            if reason == "emergency_stop":
                emergency_exits += 1
            if quantity <= 1e-12:
                quantity = 0.0
                average_cost = 0.0
            anchor = price
            level = 0
        trades.append(Trade(symbol, side, bar.ts, price, requested_qty, quote, fee_paid, reason, level, equity(bar.close)))

    initial_done = False
    for i in range(start, len(bars)):
        bar = bars[i]
        if pending is not None:
            execute(bar, pending)
            cooldown_until = i + int(grid["cooldown_bars"])
            pending = None
        if not initial_done:
            initial_quote = capital * initial_fraction
            execute(bar, ("buy", initial_quote / (bar.open * (1.0 + slip) * (1.0 + fee)), "initial_position", i))
            initial_done = True
        marked = equity(bar.close)
        equity_curve.append({"timestamp": bar.ts, "close": bar.close, "cash": cash,
                             "spot_qty": quantity, "average_cost": average_cost, "equity": marked,
                             "level": float(level)})
        if i >= len(bars) - 1 or i <= cooldown_until or quantity <= 0:
            continue
        spacing = clamp(max(float(grid["min_step_pct"]), float(grid["atr_multiplier"]) * ranges[i] / bar.close),
                        float(grid["min_step_pct"]), float(grid["max_step_pct"]))
        trend_down = slow[i] < slow[max(0, i - int(config["indicators"]["trend_slope_bars"]))] and bar.close < slow[i]
        inventory_quote = quantity * bar.close
        max_inventory = capital * float(risk["max_inventory_fraction"])
        if (bar.close <= anchor * (1.0 - spacing * (level + 1))
                and level < int(grid["max_levels"])
                and inventory_quote < max_inventory
                and cash > float(risk["reserve_floor_quote"])
                and (trend_down or bar.close < fast[i])):
            order_quote = float(grid["base_order_quote"]) * float(grid["multiplier"]) ** level
            if trend_down:
                order_quote *= float(grid["downtrend_order_fraction"])
            room = max_inventory - inventory_quote
            order_quote = min(order_quote, room, cash - float(risk["reserve_floor_quote"]))
            if order_quote > 0:
                pending = ("buy", order_quote / (bar.close * (1.0 + slip) * (1.0 + fee)), "martingale_buy", i)
                level += 1
                max_level_seen = max(max_level_seen, level)
                continue
        take_profit = float(grid["take_profit_pct"])
        if (bar.close >= average_cost * (1.0 + take_profit) and bar.close > fast[i]
                and bar.close >= anchor * (1.0 + spacing * 0.5)):
            pending = ("sell", quantity * float(grid["sell_fraction"]), "oscillation_take_profit", i)
            continue
        if (level >= int(grid["max_levels"])
                and bar.close <= average_cost * (1.0 - float(risk["emergency_drawdown_pct"]))):
            pending = ("sell", quantity, "emergency_stop", i)

    final_price = bars[-1].close
    final_equity = equity(final_price)
    peak = capital
    max_dd = 0.0
    for row in equity_curve:
        peak = max(peak, row["equity"])
        max_dd = max(max_dd, (peak - row["equity"]) / peak * 100.0)
    baseline = baseline_buy_hold(bars, start, capital, fee, slip)
    report: dict[str, object] = {
        "symbol": symbol, "bars": len(bars), "start_timestamp": bars[start].ts,
        "end_timestamp": bars[-1].ts, "initial_capital": capital,
        "final_equity": final_equity, "return_pct": (final_equity / capital - 1.0) * 100.0,
        "max_drawdown_pct": max_dd, "buy_hold_return_pct": baseline["return_pct"],
        "buy_hold_max_drawdown_pct": baseline["max_drawdown_pct"], "trades": len(trades),
        "buys": buys, "sells": sells, "fees_paid": fees_paid, "realized_pnl": realized,
        "final_cash": cash, "final_spot_qty": quantity, "final_average_cost": average_cost,
        "initial_spot_qty": initial_quantity,
        "minimum_average_cost": None if math.isinf(min_average_cost) else min_average_cost,
        "max_level_seen": max_level_seen, "emergency_exits": emergency_exits,
        "data_note": "OKX USDT perpetual OHLCV used as spot price proxy; no spot fills, basis, funding, or order book.",
    }
    return report, trades, equity_curve


def write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = sorted({key for row in rows for key in row}) if rows else []
    with path.open("w", newline="", encoding="utf-8") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def run_batch(config_path: Path = DEFAULT_CONFIG, output_dir: Path = DEFAULT_OUTPUT) -> dict[str, object]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    symbols = list(config["universe"]["symbols"])
    reports: list[dict[str, object]] = []
    for symbol in symbols:
        paths = sorted(DATA_DIR.glob(f"{symbol}_USDT_SWAP_5m_*.jsonl.gz"))
        if not paths:
            reports.append({"symbol": symbol, "available": False})
            continue
        bars = load_bars(paths[-1])
        report, trades, equity_curve = simulate(symbol, bars, config)
        report["available"] = True
        report["source"] = str(paths[-1].relative_to(ROOT))
        reports.append(report)
        symbol_dir = output_dir / symbol.lower()
        write_csv(symbol_dir / "trades.csv", [asdict(trade) for trade in trades])
        write_csv(symbol_dir / "equity.csv", equity_curve)
        (symbol_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    available = [row for row in reports if row.get("available")]
    pooled = {
        "symbols_requested": symbols,
        "symbols_available": [row["symbol"] for row in available],
        "mean_return_pct": sum(float(row["return_pct"]) for row in available) / len(available) if available else 0.0,
        "mean_max_drawdown_pct": sum(float(row["max_drawdown_pct"]) for row in available) / len(available) if available else 0.0,
        "mean_buy_hold_return_pct": sum(float(row["buy_hold_return_pct"]) for row in available) / len(available) if available else 0.0,
        "mean_buy_hold_max_drawdown_pct": sum(float(row["buy_hold_max_drawdown_pct"]) for row in available) / len(available) if available else 0.0,
        "symbols_outperforming_buy_hold": sum(float(row["return_pct"]) > float(row["buy_hold_return_pct"]) for row in available),
        "reports": reports,
        "config": str(config_path.relative_to(ROOT)),
        "limitations": ["price-layer study using swap OHLCV as spot proxy", "no funding, basis, spot order book, or fill synchronization", "finite martingale still carries material gap and liquidity risk"],
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "report.json").write_text(json.dumps(pooled, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(output_dir / "summary.csv", reports)
    return pooled


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--data-file", type=Path)
    parser.add_argument("--symbol", default="BTC")
    args = parser.parse_args()
    if args.data_file:
        config = json.loads(args.config.read_text(encoding="utf-8"))
        report, trades, equity_curve = simulate(args.symbol, load_bars(args.data_file), config)
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        write_csv(args.output_dir / "trades.csv", [asdict(trade) for trade in trades])
        write_csv(args.output_dir / "equity.csv", equity_curve)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    print(json.dumps(run_batch(args.config, args.output_dir), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
