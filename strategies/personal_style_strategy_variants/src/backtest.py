#!/usr/bin/env python3
"""Batch replay of several price strategies inferred from the user's trade style.

The runner reads the latest manifest snapshot for every eligible contract, then
generates all strategy variants while each symbol is resident in memory. It is
research code only: it does not place orders or import runtime Swift code.
"""
from __future__ import annotations

import argparse
import csv
import gzip
import hashlib
import json
import math
import statistics
from collections import Counter, deque
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import numpy as np


BAR_MS = 5 * 60 * 1000
UTC = timezone.utc
ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA = ROOT / "data/kline/okx/swap/5m"
DEFAULT_MANIFEST = DEFAULT_DATA / "manifest.json"
DEFAULT_UNIVERSE = ROOT / "strategies/sweep_reversal_short/config/universe.json"
DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "config/strategy.json"
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "results"


@dataclass(frozen=True)
class Event:
    variant: str
    symbol: str
    signal_ts: int
    entry_ts: int
    entry_index: int
    direction: str
    stop_raw: float
    target_r: float
    max_hold: int
    atr: float
    ret24: float
    liquidity_24h: float


@dataclass(frozen=True)
class Trade:
    variant: str
    cost: str
    symbol: str
    direction: str
    signal_ts: int
    entry_ts: int
    exit_ts: int
    entry: float
    exit: float
    stop: float
    target: float
    risk: float
    gross_r: float
    net_r: float
    mae_r: float
    mfe_r: float
    exit_reason: str
    bars_held: int
    ret24: float
    liquidity_24h: float


def finite(value: object) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    if len(values) < window:
        return out
    cs = np.concatenate(([0.0], np.cumsum(values, dtype=float)))
    out[window - 1 :] = (cs[window:] - cs[:-window]) / window
    return out


def rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    if len(values) < window:
        return out
    cs = np.concatenate(([0.0], np.cumsum(values, dtype=float)))
    cs2 = np.concatenate(([0.0], np.cumsum(values * values, dtype=float)))
    mean = (cs[window:] - cs[:-window]) / window
    second = (cs2[window:] - cs2[:-window]) / window
    out[window - 1 :] = np.sqrt(np.maximum(second - mean * mean, 0.0))
    return out


def rolling_sum(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    if len(values) < window:
        return out
    cs = np.concatenate(([0.0], np.cumsum(values, dtype=float)))
    out[window - 1 :] = cs[window:] - cs[:-window]
    return out


def rolling_extreme(values: np.ndarray, window: int, maximum: bool) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    q: deque[int] = deque()
    for i, value in enumerate(values):
        while q and q[0] <= i - window:
            q.popleft()
        while q:
            old = values[q[-1]]
            if (maximum and old <= value) or ((not maximum) and old >= value):
                q.pop()
            else:
                break
        q.append(i)
        if i >= window - 1:
            out[i] = values[q[0]]
    return out


def ema(values: np.ndarray, span: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    if not len(values):
        return out
    alpha = 2.0 / (span + 1.0)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1.0 - alpha) * out[i - 1]
    return out


def symbol_from_path(path: Path) -> str:
    return path.name.split("_USDT_SWAP_5m_", 1)[0].upper()


def load_bars(path: Path) -> tuple[np.ndarray, ...]:
    rows: dict[int, tuple[float, float, float, float, float]] = {}
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            try:
                row = json.loads(line)
                if row.get("confirmed") is not True:
                    continue
                ts = int(row["timestamp_ms"])
                values = [finite(row.get(k)) for k in ("open", "high", "low", "close", "quote_volume")]
                if ts <= 0 or any(v is None for v in values):
                    continue
                o, h, low, c, vol = (float(v) for v in values)
                if min(o, h, low, c) <= 0 or vol < 0 or h < max(o, c) or low > min(o, c) or h < low:
                    continue
                rows[ts] = (o, h, low, c, vol)
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    ordered = sorted(rows.items())
    if not ordered:
        return tuple(np.array([], dtype=float) for _ in range(5))
    ts = np.array([r[0] for r in ordered], dtype=np.int64)
    data = np.array([r[1] for r in ordered], dtype=float)
    return ts, data[:, 0], data[:, 1], data[:, 2], data[:, 3], data[:, 4]


def choose_paths(data_dir: Path, manifest_path: Path, allowed: set[str], max_symbols: int | None = None) -> dict[str, Path]:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    selected: dict[str, Path] = {}
    for contract in manifest.get("contracts", []):
        instrument = str(contract.get("instrument_id", ""))
        symbol = instrument.split("-", 1)[0].upper()
        candidate = data_dir / str(contract.get("file", ""))
        if symbol in allowed and candidate.is_file():
            selected[symbol] = candidate
    if max_symbols:
        selected = dict(sorted(selected.items())[:max_symbols])
    return selected


def load_universe(path: Path) -> tuple[set[str], set[str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    exclude = {str(v).upper() for v in data.get("exclude", [])}
    # Keep the broad major-asset exclusion explicit. The historical universe
    # snapshot predates the latest config and does not list every major token.
    exclude.update({"BTC", "ETH", "SOL", "BNB", "XRP", "DOGE", "ADA", "TRX", "USDT", "USDC", "DAI"})
    listed = {str(v).upper() for v in data.get("altcoins", [])}
    # The curated altcoin list is the safest way to exclude stock/ETF/index
    # contracts present in the OKX export. Fall back to any non-excluded symbol
    # only when an older universe file has no altcoins field.
    return exclude, listed or {"*"}


def true_range(high: np.ndarray, low: np.ndarray, close: np.ndarray) -> np.ndarray:
    previous = np.concatenate(([close[0]], close[:-1]))
    return np.maximum(high - low, np.maximum(np.abs(high - previous), np.abs(low - previous)))


def rsi14(close: np.ndarray) -> np.ndarray:
    delta = np.diff(close, prepend=close[0])
    gains = np.maximum(delta, 0.0)
    losses = np.maximum(-delta, 0.0)
    avg_gain = rolling_mean(gains, 14)
    avg_loss = rolling_mean(losses, 14)
    rs = np.divide(avg_gain, avg_loss, out=np.full(close.shape, np.inf), where=avg_loss > 0)
    return 100.0 - 100.0 / (1.0 + rs)


def median_window(values: np.ndarray, window: int) -> np.ndarray:
    out = np.full(values.shape, np.nan, dtype=float)
    for i in range(window - 1, len(values)):
        out[i] = float(np.median(values[i - window + 1 : i + 1]))
    return out


def build_events(symbol: str, arrays: tuple[np.ndarray, ...], liquidity_threshold: float) -> list[Event]:
    ts, op, high, low, close, volume = arrays
    n = len(ts)
    if n < 340:
        return []
    gaps = np.diff(ts) != BAR_MS
    tr = true_range(high, low, close)
    atr = rolling_mean(tr, 14)
    rsi = rsi14(close)
    ema9 = ema(close, 9)
    ema20 = ema(close, 20)
    ret24 = np.full(n, np.nan)
    ret24[288:] = close[288:] / close[:-288] - 1.0
    ret6 = np.full(n, np.nan)
    ret6[72:] = close[72:] / close[:-72] - 1.0
    high24_incl = rolling_extreme(high, 288, True)
    high24_prior = np.concatenate(([np.nan], high24_incl[:-1]))
    high12 = rolling_extreme(high, 12, True)
    high12_prior = np.concatenate(([np.nan], high12[:-1]))
    vol24 = rolling_sum(volume, 288)
    vol20_prior = np.concatenate(([np.nan], median_window(volume, 20)[:-1]))
    range_size = np.maximum(high - low, 1e-12)
    upper_wick = (high - np.maximum(op, close)) / range_size
    close_position = (close - low) / range_size
    bearish = close < op
    bullish = close > op
    valid = np.isfinite(atr) & np.isfinite(ret24) & (vol24 >= liquidity_threshold)
    events: list[Event] = []

    def contiguous(i: int, lookback: int, hold: int) -> bool:
        end = min(n - 1, i + 1 + hold)
        return not gaps[max(0, i - lookback) : end].any()

    def append_event(variant: str, i: int, direction: str, stop_raw: float, target_r: float, hold: int) -> None:
        if i + 1 >= n or not valid[i] or not np.isfinite(stop_raw) or not contiguous(i, 300, hold):
            return
        if direction == "short" and stop_raw <= op[i + 1] * 0.995:
            return
        if direction == "long" and stop_raw >= op[i + 1] * 1.005:
            return
        events.append(Event(variant, symbol, int(ts[i]), int(ts[i + 1]), i + 1, direction, float(stop_raw), target_r, hold, float(atr[i]), float(ret24[i]), float(vol24[i])))

    # A: high momentum followed by a fully confirmed bearish exhaustion candle.
    mask_a = valid & (ret24 >= 0.10) & (close >= 0.95 * high24_incl) & bearish
    mask_a &= (upper_wick >= 0.40) & (close_position <= 0.50) & (close < np.concatenate(([np.nan], close[:-1])))
    mask_a &= (rsi >= 50.0) & (rsi < np.concatenate(([np.nan], rsi[:-1])))
    for i in np.flatnonzero(mask_a):
        append_event("A_exhaustion_short", int(i), "short", max(high[i], high[i - 1]) + 0.25 * atr[i], 1.0, 72)

    # B: a second failed breakout of the rolling 24h high within 12 bars.
    fake = valid & (high > high24_prior) & (close < high24_prior) & bearish & (upper_wick >= 0.30)
    for i in np.flatnonzero(fake):
        i = int(i)
        if i < 12:
            continue
        prior = [j for j in range(i - 12, i) if fake[j]]
        if prior:
            start = min(prior)
            append_event("B_double_sweep_short", i, "short", float(np.max(high[start : i + 1]) + 0.30 * atr[i]), 1.2, 72)

    # C: rebound into the previous high on lower volume after a 5-20% pullback.
    prev_high = high24_prior
    prior_close = np.concatenate(([np.nan], close[:-1]))
    retrace = prior_close / prev_high - 1.0
    mask_c = valid & np.isfinite(prev_high) & (retrace <= -0.03) & (retrace >= -0.20)
    mask_c &= (close / np.maximum(prior_close, 1e-12) - 1.0 >= 0.03)
    mask_c &= (high >= 0.97 * prev_high) & (volume <= 0.90 * np.maximum(np.concatenate(([np.nan], volume[:-1])), 1e-12))
    mask_c &= (close / np.maximum(high, 1e-12) >= 0.97)
    for i in np.flatnonzero(mask_c):
        append_event("C_pump_retest_short", int(i), "short", max(high[i], high[i - 1]) + 0.30 * atr[i], 1.5, 72)

    # D: a constrained long: pullback first, then a bullish break with volume.
    prev12_high = high12_prior
    prev_retrace = prior_close / prev12_high - 1.0
    mask_d = valid & (ret24 >= 0.08) & (prev_retrace <= -0.02) & (prev_retrace >= -0.08)
    mask_d &= bullish & (close > ema20) & (close > np.concatenate(([np.nan], high[:-1])))
    mask_d &= volume >= 1.20 * vol20_prior
    for i in np.flatnonzero(mask_d):
        append_event("D_breakout_pullback_long", int(i), "long", min(low[i], low[i - 1]) - 0.25 * atr[i], 1.0, 48)

    # E: extreme positive 5m return relative to the preceding 48 returns.
    logret = np.zeros(n, dtype=float)
    logret[1:] = np.log(np.maximum(close[1:], 1e-12) / np.maximum(close[:-1], 1e-12))
    mu = np.concatenate(([np.nan], rolling_mean(logret, 48)[:-1]))
    sd = np.concatenate(([np.nan], rolling_std(logret, 48)[:-1]))
    z = np.divide(logret - mu, sd, out=np.full(n, np.nan), where=sd > 1e-12)
    # The extreme return is confirmed on the previous bar; the current bar
    # must turn bearish, so the confirmation candle cannot be both the positive
    # spike and the bearish exit signal.
    z_prev = np.concatenate(([np.nan], z[:-1]))
    mask_e = valid & (z_prev >= 1.5) & (ret6 >= 0.05) & bearish & (close < ema9)
    for i in np.flatnonzero(mask_e):
        append_event("E_extreme_reversion_short", int(i), "short", op[i + 1] + 2.0 * atr[i], 0.8, 36)
    return events


def execute(event: Event, arrays: tuple[np.ndarray, ...], fee: float, slippage: float, cost_name: str) -> Trade | None:
    ts, op, high, low, close, _ = arrays
    i = event.entry_index
    if i >= len(ts):
        return None
    raw_entry = float(op[i])
    entry = raw_entry * (1.0 + slippage if event.direction == "long" else 1.0 - slippage)
    stop = event.stop_raw
    risk = (entry - stop) if event.direction == "long" else (stop - entry)
    if risk <= 0:
        return None
    risk_pct = risk / entry
    max_risk = 0.15 if event.variant != "D_breakout_pullback_long" else 0.10
    if risk_pct < 0.005 or risk_pct > max_risk:
        return None
    target = entry + event.target_r * risk if event.direction == "long" else entry - event.target_r * risk
    last = min(len(ts) - 1, i + event.max_hold - 1)
    exit_i = last
    raw_exit = float(close[last])
    reason = "time_exit"
    for j in range(i, last + 1):
        if j > i and ts[j] - ts[j - 1] != BAR_MS:
            exit_i, raw_exit, reason = j - 1, float(close[j - 1]), "data_gap_exit"
            break
        if event.direction == "short":
            if op[j] >= stop:
                exit_i, raw_exit, reason = j, float(op[j]), "stop_gap"
                break
            if high[j] >= stop:
                exit_i, raw_exit, reason = j, stop, "stop"
                break
            if low[j] <= target:
                exit_i, raw_exit, reason = j, target, "target"
                break
        else:
            if op[j] <= stop:
                exit_i, raw_exit, reason = j, float(op[j]), "stop_gap"
                break
            if low[j] <= stop:
                exit_i, raw_exit, reason = j, stop, "stop"
                break
            if high[j] >= target:
                exit_i, raw_exit, reason = j, target, "target"
                break
    exit_price = raw_exit * (1.0 + slippage if event.direction == "short" else 1.0 - slippage)
    signed_move = (entry - exit_price) if event.direction == "short" else (exit_price - entry)
    net_move = signed_move - fee * (entry + exit_price)
    highs = high[i : exit_i + 1]
    lows = low[i : exit_i + 1]
    if event.direction == "short":
        adverse = float(np.max(highs) - entry) / risk
        favorable = float(entry - np.min(lows)) / risk
    else:
        adverse = float(entry - np.min(lows)) / risk
        favorable = float(np.max(highs) - entry) / risk
    return Trade(event.variant, cost_name, event.symbol, event.direction, event.signal_ts, int(ts[i]), int(ts[exit_i] + BAR_MS), entry, exit_price, stop, target, risk, signed_move / risk, net_move / risk, adverse, favorable, reason, exit_i - i + 1, event.ret24, event.liquidity_24h)


def accept_capacity(trades: Iterable[Trade]) -> tuple[list[Trade], int]:
    active_exit = 0
    last_entry: dict[str, int] = {}
    accepted: list[Trade] = []
    skipped = 0
    for trade in sorted(trades, key=lambda x: (x.entry_ts, x.symbol)):
        previous = last_entry.get(trade.symbol)
        if trade.entry_ts < active_exit or (previous is not None and trade.entry_ts - previous < 12 * BAR_MS):
            skipped += 1
            continue
        accepted.append(trade)
        active_exit = trade.exit_ts
        last_entry[trade.symbol] = trade.entry_ts
    return accepted, skipped


def split_bounds(trades: list[Trade]) -> tuple[int, int]:
    values = [t.signal_ts for t in trades]
    lo, hi = min(values), max(values)
    return int(lo + 0.60 * (hi - lo)), int(lo + 0.80 * (hi - lo))


def split_name(ts: int, bounds: tuple[int, int]) -> str:
    return "train" if ts <= bounds[0] else "validation" if ts <= bounds[1] else "test"


def stats(trades: list[Trade]) -> dict[str, object]:
    values = [t.net_r for t in trades]
    wins = sum(v > 0 for v in values)
    gains = sum(v for v in values if v > 0)
    losses = -sum(v for v in values if v < 0)
    equity = peak = drawdown = 0.0
    for value in [t.net_r for t in sorted(trades, key=lambda x: (x.exit_ts, x.symbol))]:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
    return {
        "trades": len(values),
        "wins": wins,
        "win_rate": wins / len(values) if values else 0.0,
        "total_r": sum(values),
        "expectancy_r": statistics.fmean(values) if values else 0.0,
        "profit_factor": gains / losses if losses else (999999.0 if gains else 0.0),
        "max_drawdown_r": drawdown,
        "mean_mae_r": statistics.fmean(t.mae_r for t in trades) if trades else 0.0,
        "mean_mfe_r": statistics.fmean(t.mfe_r for t in trades) if trades else 0.0,
        "symbols": len({t.symbol for t in trades}),
        "exit_reasons": dict(Counter(t.exit_reason for t in trades)),
    }


def robustness(trades: list[Trade], bounds: tuple[int, int]) -> dict[str, object]:
    train = [t for t in trades if split_name(t.signal_ts, bounds) == "train"]
    top_symbol = max((s for s in {t.symbol for t in train}), key=lambda s: sum(t.net_r for t in train if t.symbol == s), default=None)
    by_day = Counter()
    for t in train:
        day = datetime.fromtimestamp(t.signal_ts / 1000, UTC).date().isoformat()
        by_day[day] += t.net_r
    top_days = {d for d, _ in by_day.most_common(3)}
    no_symbol = [t for t in trades if t.symbol != top_symbol]
    no_days = [t for t in trades if datetime.fromtimestamp(t.signal_ts / 1000, UTC).date().isoformat() not in top_days]
    return {"top_train_symbol": top_symbol, "top_train_days": sorted(top_days), "without_top_symbol": stats(no_symbol), "without_top_train_days": stats(no_days)}


def run(data_dir: Path, manifest_path: Path, universe_path: Path, config_path: Path, output_dir: Path, max_symbols: int | None = None) -> dict[str, object]:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    exclude, listed = load_universe(universe_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    allowed = {str(c.get("instrument_id", "")).split("-", 1)[0].upper() for c in manifest.get("contracts", [])}
    allowed -= exclude
    if "*" not in listed:
        allowed &= listed
    paths = choose_paths(data_dir, manifest_path, allowed, max_symbols)
    all_events: list[Event] = []
    costs = [("base_5bp", 0.0005, 0.0005), ("stress_10bp", 0.001, 0.001)]
    simulated: dict[tuple[str, str], list[Trade]] = {}
    inventory = {"manifest_contracts": len(manifest.get("contracts", [])), "selected_files": len(paths), "eligible_symbols": sorted(paths), "bars": 0, "symbols_with_events": 0}
    for symbol, path in sorted(paths.items()):
        arrays = load_bars(path)
        inventory["bars"] += len(arrays[0])
        events = build_events(symbol, arrays, float(config["data"]["quote_volume_threshold_24h_usdt"]))
        if events:
            inventory["symbols_with_events"] += 1
            all_events.extend(events)
            for event in events:
                for cost_name, fee, slippage in costs:
                    trade = execute(event, arrays, fee, slippage, cost_name)
                    if trade:
                        simulated.setdefault((event.variant, cost_name), []).append(trade)
    if not all_events:
        raise RuntimeError("no strategy events generated")
    output_dir.mkdir(parents=True, exist_ok=True)
    summary_rows: list[dict[str, object]] = []
    report_variants: dict[str, object] = {}
    all_saved: list[dict[str, object]] = []
    for (variant, cost_name), raw in simulated.items():
        accepted, skipped = accept_capacity(raw)
        bounds = split_bounds(accepted) if accepted else (0, 0)
        variant_report: dict[str, object] = {"cost": cost_name, "raw_trades": len(raw), "capacity_skipped": skipped, "walk_forward": {}, "monthly": {}, "robustness": robustness(accepted, bounds) if accepted else {}}
        for split in ("all", "train", "validation", "test"):
            scoped = accepted if split == "all" else [t for t in accepted if split_name(t.signal_ts, bounds) == split]
            row = {"variant": variant, "cost": cost_name, "split": split, "capacity_skipped": skipped if split == "all" else 0, **stats(scoped)}
            summary_rows.append(row)
            variant_report["walk_forward"][split] = row
        months = sorted({datetime.fromtimestamp(t.signal_ts / 1000, UTC).strftime("%Y-%m") for t in accepted})
        for month in months:
            variant_report["monthly"][month] = stats([t for t in accepted if datetime.fromtimestamp(t.signal_ts / 1000, UTC).strftime("%Y-%m") == month])
        for trade in accepted:
            item = asdict(trade)
            item["signal_time"] = datetime.fromtimestamp(trade.signal_ts / 1000, UTC).isoformat()
            item["entry_time"] = datetime.fromtimestamp(trade.entry_ts / 1000, UTC).isoformat()
            item["exit_time"] = datetime.fromtimestamp(trade.exit_ts / 1000, UTC).isoformat()
            all_saved.append(item)
        report_variants.setdefault(variant, {})[cost_name] = variant_report
    trade_fields = list(all_saved[0].keys()) if all_saved else []
    with (output_dir / "trades.csv").open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=trade_fields)
        if trade_fields:
            writer.writeheader()
            writer.writerows(all_saved)
    with (output_dir / "summary.csv").open("w", encoding="utf-8", newline="") as stream:
        fields = list(summary_rows[0].keys())
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(summary_rows)
    (output_dir / "data_inventory.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report = {
        "strategy_id": config["strategy_id"],
        "version": config["version"],
        "data_inventory": inventory,
        "variants": report_variants,
        "costs": {"base_5bp": {"fee_one_way": 0.0005, "slippage_one_way": 0.0005}, "stress_10bp": {"fee_one_way": 0.001, "slippage_one_way": 0.001}},
        "source_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "config_sha256": hashlib.sha256(config_path.read_bytes()).hexdigest(),
        "limitations": ["OHLC cannot resolve intrabar order; stop is prioritized.", "Funding, mark price, contract multiplier and order-book queue are not modeled.", "All signals are research rules and have not been connected to runtime."]
    }
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
    print(json.dumps({"data_inventory": inventory, "summary": summary_rows}, ensure_ascii=False, indent=2))
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--universe-config", type=Path, default=DEFAULT_UNIVERSE)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--max-symbols", type=int, default=None, help="smoke-test limit; omit for all eligible symbols")
    args = parser.parse_args()
    run(args.data_dir, args.manifest, args.universe_config, args.config, args.output_dir, args.max_symbols)


if __name__ == "__main__":
    main()
