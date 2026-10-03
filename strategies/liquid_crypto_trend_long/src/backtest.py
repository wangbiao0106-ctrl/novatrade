#!/usr/bin/env python3
"""Liquid crypto perpetual trend following, long only.

The lab rule source is ``strategies/liquid_crypto_trend_long/STRATEGY.md`` and
the machine parameters are ``config/strategy.json``.  This module reads the
checked-in raw OKX 5m JSONL exports under ``data/kline/okx/swap/5m``, rebuilds
the confirmed 1h structure the rule is defined on, and replays the rule with
declared costs.

Every decision at hour ``t`` uses only bars up to and including ``t``: the
1h close, the trailing 24h quote volume, and the trailing realised volatility.
The module never reads the strategy lab at runtime.
"""
from __future__ import annotations

import argparse
import gzip
import json
import math
import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[3]
LAB = Path(__file__).resolve().parents[1]
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_CONFIG = LAB / "config" / "strategy.json"
DEFAULT_UNIVERSE = LAB / "config" / "universe.json"
DEFAULT_OUTPUT = LAB / "results"
HOUR_MS = 3600 * 1000
BARS_PER_YEAR = 24 * 365


# --------------------------------------------------------------------------- #
# raw data
# --------------------------------------------------------------------------- #
def symbol_of(path: Path) -> str:
    return path.name.split("_USDT_SWAP_5m_")[0]


def aggregate_file(path: Path) -> dict[int, list[float]]:
    """Fold one 5m export into ``{hour_open_ms: [o, h, l, c, quote_volume, rows]}``."""
    acc: dict[int, list[float]] = {}
    with gzip.open(path, "rt") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            ts = row.get("timestamp_ms")
            if ts is None or row.get("confirmed") is not True:
                continue
            hour = (int(ts) // HOUR_MS) * HOUR_MS
            try:
                o = float(row["open"]); h = float(row["high"])
                l = float(row["low"]); c = float(row["close"])
                qv = float(row.get("quote_volume") or 0.0)
            except (KeyError, TypeError, ValueError):
                continue
            bucket = acc.get(hour)
            if bucket is None:
                acc[hour] = [o, h, l, c, qv, 1.0]
            else:
                bucket[1] = max(bucket[1], h)
                bucket[2] = min(bucket[2], l)
                bucket[3] = c
                bucket[4] += qv
                bucket[5] += 1.0
    return acc


def load_hourly(data_dir: Path, symbols: set[str], cache_dir: Path | None) -> dict[str, np.ndarray]:
    """Return ``{symbol: array[ts, o, h, l, c, quote_volume]}`` for ``symbols``.

    A symbol is usually covered by several export windows that overlap in time.
    Overlapping hours are resolved by keeping the window with the most 5m rows
    for that hour, never by summing the windows together.
    """
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
    collected: dict[str, dict[int, list[float]]] = {s: {} for s in symbols}
    newest: dict[str, float] = {s: 0.0 for s in symbols}
    for path in sorted(data_dir.glob("*.jsonl.gz")):
        symbol = symbol_of(path)
        if symbol not in collected:
            continue
        newest[symbol] = max(newest[symbol], path.stat().st_mtime)
        for hour, bar in aggregate_file(path).items():
            current = collected[symbol].get(hour)
            if current is None or bar[5] >= current[5]:
                collected[symbol][hour] = bar
    panel: dict[str, np.ndarray] = {}
    for symbol, acc in collected.items():
        cached = cache_dir / f"{symbol}.npy" if cache_dir is not None else None
        if cached is not None and cached.exists() and cached.stat().st_mtime >= newest[symbol]:
            panel[symbol] = np.load(cached)
            continue
        if not acc:
            continue
        hours = sorted(acc)
        array = np.array([[h, *acc[h][:5]] for h in hours], dtype=np.float64)
        if cached is not None:
            np.save(cached, array)
        panel[symbol] = array
    return panel


def build_panel(panel: dict[str, np.ndarray]) -> dict:
    """Align every symbol onto one hourly axis; return price/volume matrices."""
    symbols = sorted(panel)
    axis = np.unique(np.concatenate([panel[s][:, 0] for s in symbols]))
    index = {ts: i for i, ts in enumerate(axis)}
    prices = np.full((len(axis), len(symbols)), np.nan)
    volume = np.full((len(axis), len(symbols)), np.nan)
    for j, symbol in enumerate(symbols):
        array = panel[symbol]
        rows = np.array([index[ts] for ts in array[:, 0]])
        prices[rows, j] = array[:, 4]
        volume[rows, j] = array[:, 5]
    returns = np.full_like(prices, np.nan)
    returns[:-1] = prices[1:] / prices[:-1] - 1.0
    return {"symbols": symbols, "axis": axis, "prices": prices,
            "volume": volume, "returns": returns}


# --------------------------------------------------------------------------- #
# rule
# --------------------------------------------------------------------------- #
def rolling_sum(matrix: np.ndarray, window: int, min_bars: int) -> np.ndarray:
    """Trailing ``window``-hour sum that is NaN until ``min_bars`` real bars exist."""
    valid = ~np.isnan(matrix)
    filled = np.where(valid, matrix, 0.0)
    cumulative = np.cumsum(filled, axis=0)
    counts = np.cumsum(valid, axis=0)
    rows = np.arange(matrix.shape[0])
    lag = np.maximum(rows - window, 0)
    total = cumulative - np.where(rows[:, None] >= window, cumulative[lag], 0.0)
    seen = counts - np.where(rows[:, None] >= window, counts[lag], 0.0)
    enough = (seen >= min_bars) & (rows[:, None] >= 0)
    return np.where(enough, total, np.nan)


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    valid = ~np.isnan(values)
    filled = np.where(valid, values, 0.0)
    cumulative = np.cumsum(filled)
    counts = np.cumsum(valid)
    rows = np.arange(len(values))
    lag = np.maximum(rows - window, 0)
    total = cumulative - np.where(rows >= window, cumulative[lag], 0.0)
    seen = counts - np.where(rows >= window, counts[lag], 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where((seen == window), total / window, np.nan)
    return mean


def rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    valid = ~np.isnan(values)
    filled = np.where(valid, values, 0.0)
    cumulative = np.cumsum(filled)
    cumulative_sq = np.cumsum(filled * filled)
    counts = np.cumsum(valid)
    rows = np.arange(len(values))
    lag = np.maximum(rows - window, 0)
    total = cumulative - np.where(rows >= window, cumulative[lag], 0.0)
    total_sq = cumulative_sq - np.where(rows >= window, cumulative_sq[lag], 0.0)
    seen = counts - np.where(rows >= window, counts[lag], 0.0)
    with np.errstate(invalid="ignore", divide="ignore"):
        mean = np.where(seen == window, total / window, np.nan)
        var = np.where(seen == window, total_sq / window - mean * mean, np.nan)
    return np.sqrt(np.maximum(var, 0.0))


def eligibility(volume: np.ndarray, minimum: float, lookback: int, min_bars: int) -> np.ndarray:
    """Trailing ``lookback``-hour quote volume above ``minimum``."""
    return rolling_sum(volume, lookback, min_bars) >= minimum


def trend_positions(prices: np.ndarray, ma_window: int, hysteresis: float,
                    max_direction_bars: int) -> np.ndarray:
    """+1 while close > MA*(1+h), -1 while close < MA*(1-h), else hold.

    Inside the band the previous direction is carried forward, so the run
    length counts hours since the direction last changed.  A direction may not
    be held longer than ``max_direction_bars`` hours; once the cap is reached
    the symbol stays flat until the band flips it.

    The carry-forward is a forward fill of the last non-zero band signal.  The
    run length counts *available* bars only: a missing bar forces the symbol
    flat and does not advance the duration counter, so a data gap can never
    silently extend a holding period.
    """
    out = np.zeros_like(prices)
    rows = np.arange(prices.shape[0])
    for j in range(prices.shape[1]):
        close = prices[:, j]
        mean = rolling_mean(close, ma_window)
        ready = ~np.isnan(mean) & ~np.isnan(close)
        signal = np.where(ready & (close > mean * (1.0 + hysteresis)), 1.0,
                          np.where(ready & (close < mean * (1.0 - hysteresis)), -1.0, 0.0))
        non_zero = signal != 0.0
        seen = np.maximum.accumulate(non_zero)
        latest = np.maximum.accumulate(np.where(non_zero, rows, 0))
        direction = np.where(seen, signal[latest], 0.0)
        changed = np.zeros(len(rows), dtype=bool)
        if len(rows):
            changed[0] = direction[0] != 0.0
            changed[1:] = direction[1:] != direction[:-1]
        last_change = np.maximum.accumulate(np.where(changed, rows, 0))
        sequence = np.cumsum(ready)
        run = sequence - sequence[last_change] + 1
        out[:, j] = np.where(ready & seen, np.where(run > max_direction_bars, 0.0, direction), 0.0)
    return out


def apply_vol_target(positions: np.ndarray, returns: np.ndarray, target_annual: float,
                     lookback: int, cap: float) -> np.ndarray:
    """Scale each symbol toward ``target_annual`` realised volatility."""
    scaled = np.zeros_like(positions)
    for j in range(positions.shape[1]):
        vol = rolling_std(np.nan_to_num(returns[:, j]), lookback) * math.sqrt(BARS_PER_YEAR)
        with np.errstate(invalid="ignore", divide="ignore"):
            ratio = np.where(vol > 0, target_annual / np.where(vol > 0, vol, 1.0), np.nan)
        scaled[:, j] = positions[:, j] * np.nan_to_num(np.clip(ratio, 0.0, cap))
    return scaled


# --------------------------------------------------------------------------- #
# evaluation
# --------------------------------------------------------------------------- #
@dataclass
class Metrics:
    hours: int
    total_return: float
    annual_return: float
    max_drawdown: float
    sharpe: float
    volatility: float
    calmar: float

    def as_dict(self) -> dict:
        return {"hours": self.hours, "total_return": self.total_return,
                "annual_return": self.annual_return, "max_drawdown": self.max_drawdown,
                "sharpe": self.sharpe, "volatility": self.volatility, "calmar": self.calmar}


def metrics(series: np.ndarray) -> Metrics:
    series = np.nan_to_num(series)
    equity = np.cumprod(1.0 + series)
    drawdown = float((equity / np.maximum.accumulate(equity) - 1.0).min())
    hours = len(series)
    annual = float(equity[-1] ** (BARS_PER_YEAR / hours) - 1.0) if hours else 0.0
    std = float(series.std(ddof=0))
    if std <= 1e-12:          # a constant series has no meaningful Sharpe
        std = 0.0
    return Metrics(hours=hours, total_return=float(equity[-1] - 1.0), annual_return=annual,
                   max_drawdown=drawdown,
                   sharpe=float(series.mean() / std * math.sqrt(BARS_PER_YEAR)) if std > 0 else 0.0,
                   volatility=std * math.sqrt(BARS_PER_YEAR),
                   calmar=annual / abs(drawdown) if drawdown < 0 else float("nan"))


def portfolio(panel: dict, positions: np.ndarray, eligible: np.ndarray, fee_rate: float) -> np.ndarray:
    """Equal-weight the eligible symbols; charge ``fee_rate`` per unit turnover."""
    returns = np.nan_to_num(panel["returns"])
    held = np.nan_to_num(positions)
    active = np.maximum((eligible & ~np.isnan(panel["returns"])).sum(axis=1), 1)
    turnover = np.abs(np.diff(np.concatenate([np.zeros((1, held.shape[1])), held]), axis=0))
    net = (held * returns - fee_rate * turnover).sum(axis=1) / active
    return net


def equal_weight_benchmark(panel: dict, eligible: np.ndarray) -> np.ndarray:
    returns = np.nan_to_num(panel["returns"])
    active = np.maximum(eligible.sum(axis=1), 1)
    return np.where(eligible, returns, 0.0).sum(axis=1) / active


def full_universe_benchmark(panel: dict) -> np.ndarray:
    returns = np.nan_to_num(panel["returns"])
    known = ~np.isnan(panel["prices"])
    active = np.maximum(known.sum(axis=1), 1)
    return np.where(known, returns, 0.0).sum(axis=1) / active


def symbol_benchmark(panel: dict, symbol: str) -> np.ndarray:
    """Buy-and-hold series for one symbol, zero where it is unavailable."""
    if symbol not in panel["symbols"]:
        return np.zeros(len(panel["axis"]))
    column = panel["symbols"].index(symbol)
    return np.nan_to_num(panel["returns"][:, column])


def month_slices(axis: np.ndarray) -> list[tuple[str, int, int]]:
    """Contiguous calendar-month slices over the hourly axis."""
    stamps = np.array(axis, dtype="datetime64[ms]")
    keys = (stamps.astype("datetime64[M]"))
    out = []
    start = 0
    for i in range(1, len(keys) + 1):
        if i == len(keys) or keys[i] != keys[start]:
            label = str(keys[start])[:7]
            months = (stamps[start:i] + np.timedelta64(8, "h")).astype("datetime64[M]")
            out.append((str(months[0])[:7], start, i))
            start = i
    return out


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> dict:
    with path.open() as handle:
        return json.load(handle)


def run(config: dict, universe: dict, data_dir: Path, cache_dir: Path | None) -> dict:
    excluded = {s.upper() for s in universe.get("exclude", [])}
    symbols = {p.name.split("_USDT_SWAP_5m_")[0] for p in data_dir.glob("*.jsonl.gz")}
    symbols = {s for s in symbols if s not in excluded}
    panel = build_panel(load_hourly(data_dir, symbols, cache_dir))

    signal = config["signal"]
    sizing = config["position"]
    scope = config["universe"]
    costs = config["costs"]

    eligible = eligibility(panel["volume"], scope["min_quote_volume_24h_usdt"],
                           scope["volume_lookback_bars"], scope["volume_min_bars"])
    positions = trend_positions(panel["prices"], signal["ma_window_bars"],
                                signal["hysteresis_pct"] / 100.0,
                                signal["max_direction_bars"])
    positions = apply_vol_target(positions, panel["returns"], sizing["vol_target_annual"],
                                 sizing["vol_lookback_bars"], sizing["vol_scale_cap"])
    positions = positions * eligible
    if sizing["direction"] == "long_only":
        positions = np.where(positions > 0, positions, 0.0)

    fee = costs["fee_rate_one_way"] + costs["slippage_one_way"]
    net = portfolio(panel, positions, eligible, fee)
    basket = equal_weight_benchmark(panel, eligible)
    broad = full_universe_benchmark(panel)
    bitcoin = symbol_benchmark(panel, config.get("benchmarks", {}).get("reference_symbol", "BTC"))

    months = month_slices(panel["axis"])
    per_month = {}
    for label, lo, hi in months:
        per_month[label] = {"strategy": float(np.cumprod(1 + net[lo:hi])[-1] - 1),
                            "basket": float(np.cumprod(1 + basket[lo:hi])[-1] - 1)}
    traded = net[np.any(np.nan_to_num(positions) != 0, axis=1)]
    return {
        "strategy": metrics(net).as_dict(),
        "benchmark_eligible_basket": metrics(basket).as_dict(),
        "benchmark_full_universe": metrics(broad).as_dict(),
        "benchmark_reference_symbol": metrics(bitcoin).as_dict(),
        "per_month": per_month,
        "coverage": {
            "symbols": len(panel["symbols"]),
            "hours": int(len(panel["axis"])),
            "first_hour_utc": str(np.datetime64(int(panel["axis"][0]), "ms")),
            "last_hour_utc": str(np.datetime64(int(panel["axis"][-1]), "ms")),
            "average_eligible_symbols": float(eligible.sum(axis=1).mean()),
            "average_gross_exposure": float(np.abs(np.nan_to_num(positions)).sum(axis=1).mean()
                                            / max(eligible.sum(axis=1).mean(), 1e-9)),
            "hours_holding": int(len(traded)),
            "fee_rate_one_way": fee,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA,
                        help="confirmed OKX 5m JSONL.GZ directory (default: data/kline/okx/swap/5m)")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--universe", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--cache-dir", type=Path, default=None,
                        help="optional 1h npz cache directory (default: <output-dir>/cache/1h)")
    parser.add_argument("--no-write", action="store_true", help="print the report only")
    args = parser.parse_args()

    config = load_config(args.config)
    universe = load_config(args.universe) if args.universe.exists() else {"exclude": []}
    cache = None if args.no_write and args.cache_dir is None else (args.cache_dir or args.output_dir / "cache" / "1h")
    report = run(config, universe, args.data_dir, cache)

    text = json.dumps(report, indent=2, ensure_ascii=False)
    print(text)
    if not args.no_write:
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "backtest_report.json").write_text(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
