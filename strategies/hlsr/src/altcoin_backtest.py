#!/usr/bin/env python3
"""15-minute second-top short backtest for OKX USDT linear swaps."""

from __future__ import annotations

import argparse
import csv
from concurrent.futures import ThreadPoolExecutor, as_completed
import itertools
import json
import math
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

API_BASE = "https://www.okx.com/api/v5"
UTC = timezone.utc
EXCLUDED_BASES = {"BTC", "ETH", "OKB", "SOL", "BNB", "XRP", "TRX", "TON"}
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data/kline/okx/swap/15m"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "strategies/hlsr/results"
BAR_INTERVAL_MS = 15 * 60 * 1000
# Universe selection must be made from information available at the beginning
# of the walk-forward window.  A 60-day training slice is long enough to
# avoid selecting a token from one anomalous day while remaining strictly
# before the first validation/test fold.
SELECTION_DAYS = 60


def parse_utc(value: str) -> datetime:
    """Parse an ISO-8601 timestamp, treating a missing offset as UTC."""
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def http_json(path: str, params: dict[str, str], attempts: int = 5) -> dict:
    url = f"{API_BASE}{path}?{urllib.parse.urlencode(params)}"
    last: Exception | None = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "OKXSelfTrader-backtest/2.0", "Accept": "application/json"})
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") != "0":
                raise RuntimeError(f"OKX API error {payload.get('code')}: {payload.get('msg')}")
            return payload
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError) as error:
            last = error
            time.sleep(min(2 ** attempt * 0.25, 5.0))
    raise RuntimeError(f"request failed after {attempts} attempts: {url}: {last}")


def number(value: str | float | int | None, default: float | None = 0.0) -> float | None:
    """把交易所返回值转成 float；缺失或解析失败返回 `default`。

    `None` / 空字符串必须视为缺失：`float(value or 0)` 会把它们悄悄变成 0.0，
    让"没有价格"的 K 线以 0 价进入 ATR、最高价等指标。调用方传
    `default=None` 时表示要丢弃这根 K 线。
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def short_entry_price(open_price: float, slippage: float) -> float:
    """Apply adverse one-way slippage to a short entry (sell lower)."""
    return open_price * (1 - slippage)


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


def decode_bar(row: list[str]) -> Bar | None:
    if len(row) < 8 or (len(row) >= 9 and row[8] != "1"):
        return None
    # 任何一个价格字段解析失败就丢弃这根 K 线，不能以 0 价进入指标。
    values = [number(row[column], default=None) for column in (1, 2, 3, 4, 5, 7)]
    if any(value is None for value in values):
        return None
    try:
        timestamp = int(row[0])
        bar = Bar(timestamp, *values)
        if not all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.quote_volume)):
            return None
        if timestamp <= 0 or bar.open <= 0 or bar.high <= 0 or bar.low <= 0 or bar.close <= 0 or bar.volume < 0 or bar.quote_volume < 0:
            return None
        if bar.high < max(bar.open, bar.close) or bar.low > min(bar.open, bar.close) or bar.high < bar.low:
            return None
        return bar
    except (TypeError, ValueError):
        return None


def load_bars(symbol: str, start_ms: int, end_ms: int, cache_dir: Path) -> list[Bar]:
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache = cache_dir / f"{symbol.replace('-', '_')}_15m_{start_ms}_{end_ms}.json"
    if cache.exists():
        try:
            cached = json.loads(cache.read_text())
            cached_bars = sorted((Bar(**row) for row in cached if isinstance(row, dict)), key=lambda bar: bar.ts)
            if _complete_bars(cached_bars, start_ms, end_ms):
                return cached_bars
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            cached = []
        # 空、损坏或截断缓存视为缺失，不能永久变成"这个标的没有 K 线"。
    cursor = str(end_ms)
    result: dict[int, Bar] = {}
    while True:
        payload = http_json("/market/history-candles", {"instId": symbol, "bar": "15m", "limit": "100", "after": cursor})
        decoded = [decode_bar(row) for row in payload.get("data", [])]
        decoded = [bar for bar in decoded if bar is not None]
        if not decoded:
            break
        for bar in decoded:
            if start_ms <= bar.ts < end_ms:
                result[bar.ts] = bar
        oldest = min(bar.ts for bar in decoded)
        if oldest <= start_ms or len(decoded) < 100:
            break
        cursor = str(oldest)
        time.sleep(0.08)
    bars = sorted(result.values(), key=lambda bar: bar.ts)
    # 只在确实取到完整连续请求区间时写缓存，避免把空结果或半截结果持久化。
    if _complete_bars(bars, start_ms, end_ms):
        cache.write_text(json.dumps([asdict(bar) for bar in bars], separators=(",", ":")))
    return bars


def _complete_bars(bars: list[Bar], start_ms: int, end_ms: int) -> bool:
    """Require a complete, ordered 15-minute ``[start_ms, end_ms)`` window."""
    if not bars or start_ms < 0 or end_ms <= start_ms or start_ms % BAR_INTERVAL_MS or end_ms % BAR_INTERVAL_MS:
        return False
    expected_count = (end_ms - start_ms) // BAR_INTERVAL_MS
    if len(bars) != expected_count or bars[0].ts != start_ms or bars[-1].ts != end_ms - BAR_INTERVAL_MS:
        return False
    return all(
        current.ts == start_ms + index * BAR_INTERVAL_MS
        and all(math.isfinite(value) for value in (current.open, current.high, current.low, current.close, current.volume, current.quote_volume))
        and current.open > 0 and current.high > 0 and current.low > 0 and current.close > 0
        and current.volume >= 0 and current.quote_volume >= 0
        and current.high >= max(current.open, current.close)
        and current.low <= min(current.open, current.close)
        and current.high >= current.low
        for index, current in enumerate(bars)
    )


def historical_candidate_symbols(data_dir: Path, start_ms: int, end_ms: int, limit: int) -> list[str]:
    """Rank local caches by quote volume in a complete training slice.

    The old selector ranked current 24-hour tickers.  That lets information
    from the end of a historical run choose its symbols and creates a
    selection look-ahead.  Local caches are the only source that can provide
    historical volume without downloading every listed instrument again.  A
    cache is ignored unless it contains the full 60-day slice, while missing
    candles after that slice do not create survivorship bias in selection.
    """
    if limit < 1 or start_ms < 0 or end_ms < start_ms:
        return []
    # Caches are generated from 15-minute exchange candles.  Requiring both
    # boundaries to be aligned makes the expected sequence unambiguous and
    # prevents a partial first/last candle from entering the ranking window.
    if start_ms % BAR_INTERVAL_MS or end_ms % BAR_INTERVAL_MS:
        return []
    suffix = f"_15m_{start_ms}_{end_ms}.json"
    required = SELECTION_DAYS * 96
    if start_ms + required * BAR_INTERVAL_MS > end_ms:
        return []
    ranked: list[tuple[float, str]] = []
    for path in sorted(data_dir.glob(f"*_15m_{start_ms}_{end_ms}.json")):
        if not path.name.endswith(suffix):
            continue
        symbol = path.name[:-len(suffix)].replace("_", "-")
        if not symbol.endswith("-USDT-SWAP") or symbol.split("-", 1)[0] in EXCLUDED_BASES:
            continue
        try:
            raw_rows = json.loads(path.read_text())
            if not isinstance(raw_rows, list):
                continue
            parsed_rows: list[tuple[int, float]] = []
            valid = True
            for row in raw_rows:
                if not isinstance(row, dict):
                    valid = False
                    break
                timestamp = number(row.get("ts"), default=None)
                quote_volume = number(row.get("quote_volume"), default=None)
                if (
                    timestamp is None
                    or quote_volume is None
                    or not math.isfinite(timestamp)
                    or timestamp != int(timestamp)
                    or timestamp < start_ms
                    or timestamp >= end_ms
                    or int(timestamp) % BAR_INTERVAL_MS
                    or not math.isfinite(quote_volume)
                    or quote_volume < 0
                ):
                    valid = False
                    break
                parsed_rows.append((int(timestamp), quote_volume))
            if not valid or len(parsed_rows) < required:
                continue
            parsed_rows.sort(key=lambda row: row[0])
            if any(current[0] == previous[0] for previous, current in zip(parsed_rows, parsed_rows[1:])):
                continue
            training_rows = parsed_rows[:required]
            for index, (timestamp, _) in enumerate(training_rows):
                if timestamp != start_ms + index * BAR_INTERVAL_MS:
                    valid = False
                    break
            if not valid:
                continue
            quote_volume = sum(volume for _, volume in training_rows)
            if not math.isfinite(quote_volume) or quote_volume <= 0:
                continue
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
        ranked.append((-quote_volume, symbol))
    ranked.sort()
    return [symbol for _, symbol in ranked[:limit]]


def candidate_symbols(
    limit: int,
    *,
    start_ms: int | None = None,
    end_ms: int | None = None,
    data_dir: Path | None = None,
    allow_live_fallback: bool = False,
) -> list[str]:
    """Return a deterministic candidate universe.

    When a complete historical cache exists, it is ranked first.  A current
    ticker fallback is available only when explicitly enabled because current
    volume is future information for a historical run.  Calls without a
    historical window retain the old live-universe behavior for interactive
    discovery.
    """
    historical: list[str] = []
    if start_ms is not None and end_ms is not None and data_dir is not None:
        historical = historical_candidate_symbols(data_dir, start_ms, end_ms, limit)
        if len(historical) >= limit:
            return historical[:limit]
        if not allow_live_fallback:
            return historical
    else:
        allow_live_fallback = True
    if not allow_live_fallback:
        return historical
    instruments = http_json("/public/instruments", {"instType": "SWAP"}).get("data", [])
    live = {item.get("instId"): item for item in instruments if item.get("state") == "live" and item.get("settleCcy") == "USDT" and item.get("ctType") == "linear" and item.get("instId", "").endswith("-USDT-SWAP") and item.get("baseCcy") not in EXCLUDED_BASES}
    tickers = http_json("/market/tickers", {"instType": "SWAP"}).get("data", [])
    volume = {item.get("instId"): number(item.get("volCcy24h")) for item in tickers}
    current = [item["instId"] for item in sorted(live.values(), key=lambda x: (-volume.get(x.get("instId"), 0), x.get("instId", "")))[:limit]]
    merged = historical + [symbol for symbol in current if symbol not in historical]
    return merged[:limit]


def ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1)
    result: list[float] = []
    for value in values:
        result.append(value if not result else alpha * value + (1 - alpha) * result[-1])
    return result


def atr(bars: list[Bar], period: int = 14) -> list[float]:
    ranges = []
    for index, bar in enumerate(bars):
        previous = bars[index - 1].close if index else bar.close
        ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
    return ema(ranges, period)


def rolling_sum(values: list[float], end: int, period: int) -> float:
    start = max(0, end - period + 1)
    return sum(values[start:end + 1])


def screening_stats(bars: list[Bar]) -> dict[str, int]:
    closes = [bar.close for bar in bars]
    quotes = [bar.quote_volume for bar in bars]
    gain_hits = volume_hits = composite_hits = 0
    for index in range(96, len(bars)):
        gain = closes[index] / closes[index - 96] - 1 if closes[index - 96] else 0
        volume = rolling_sum(quotes, index, 96)
        gain_ok, volume_ok = gain >= 0.40, volume >= 30_000_000
        gain_hits += int(gain_ok)
        volume_hits += int(volume_ok)
        composite_hits += int(gain_ok and volume_ok)
    return {"bars": len(bars), "gain_40pct_hits": gain_hits, "quote_volume_30m_hits": volume_hits, "composite_hits": composite_hits}


def beta_interval(wins: int, losses: int, simulations: int = 10000) -> dict[str, float]:
    rng = random.Random(17 + wins * 31 + losses * 7)
    samples = sorted(rng.betavariate(1 + wins, 1 + losses) for _ in range(simulations))
    return {"posterior_mean": (wins + 1) / (wins + losses + 2), "lower_95": samples[int(simulations * 0.025)], "upper_95": samples[int(simulations * 0.975) - 1]}


@dataclass(frozen=True)
class Params:
    peak_window: int
    retrace_pct: float
    tolerance_pct: float
    weakness: str
    stop_mode: str
    stop_atr: float
    stop_pct: float
    target_r: float
    trail_bars: int
    entry_mode: str
    cooldown_bars: int

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class Trade:
    symbol: str
    entry_ts: int
    exit_ts: int
    entry: float
    initial_stop: float
    final_target: float
    net_r: float
    outcome: str
    partials: str
    margin_return_pct: float


@dataclass(frozen=True)
class Result:
    params: dict
    trades: int
    wins: int
    losses: int
    win_rate: float
    beta: dict
    total_r: float
    avg_net_r: float
    average_win_loss_ratio: float
    profit_factor: float
    max_drawdown_r: float
    max_consecutive_losses: int
    positive_probability: dict
    leverage: float = 2.0


def bootstrap_positive_probability(trades: list[Trade], simulations: int = 2000) -> dict[str, float]:
    if not trades:
        return {"positive_mean_probability": 0.0, "mean_lower_95": 0.0, "mean_upper_95": 0.0}
    clusters: dict[tuple[str, str], list[float]] = {}
    for trade in trades:
        day = datetime.fromtimestamp(trade.entry_ts / 1000, UTC).date().isoformat()
        clusters.setdefault((trade.symbol, day), []).append(trade.net_r)
    groups = list(clusters.values())
    rng = random.Random(91)
    means = []
    for _ in range(simulations):
        sampled = [rng.choice(groups) for _ in groups]
        means.append(sum(v for g in sampled for v in g) / sum(len(g) for g in sampled))
    means.sort()
    return {"positive_mean_probability": sum(v > 0 for v in means) / len(means), "mean_lower_95": means[int(len(means) * 0.025)], "mean_upper_95": means[int(len(means) * 0.975) - 1]}


def empty_result(params: Params) -> Result:
    return Result(params.as_dict(), 0, 0, 0, 0.0, beta_interval(0, 0), 0.0, 0.0, 0.0, 0.0, 0.0, 0, {"positive_mean_probability": 0.0, "mean_lower_95": 0.0, "mean_upper_95": 0.0})


def partial_exit_plan(target_r: float) -> tuple[tuple[float, float, str], ...]:
    """Return one fill per target level with a bounded total allocation.

    A target at 1R or 2R is valid input for an ad-hoc ``Params`` instance,
    even though the production grid uses 3R, 4R and 6R.  Merge coincident
    levels so the same price cannot be filled repeatedly on later candles.
    """
    if not math.isfinite(target_r) or target_r <= 0:
        raise ValueError("target_r must be a positive finite number")
    raw = ((1.0, 0.30, "1R"), (2.0, 0.30, "2R"), (target_r, 0.40, f"{target_r:g}R"))
    merged: list[list[float | str]] = []
    positions: dict[float, int] = {}
    for level, fraction, name in raw:
        if level in positions:
            merged[positions[level]][1] = float(merged[positions[level]][1]) + fraction
            continue
        positions[level] = len(merged)
        merged.append([level, fraction, name])
    total = sum(float(item[1]) for item in merged)
    if total > 1.0 + 1e-9:
        raise ValueError("partial target fractions allocate more than the position")
    return tuple((float(level), float(fraction), str(name)) for level, fraction, name in merged)


def backtest(symbol: str, bars: list[Bar], btc: list[Bar], params: Params, fee_rate: float, slippage: float, funding_rate: float, risk_fraction: float = 0.005) -> tuple[Result, list[Trade]]:
    if len(bars) < 150 or len(btc) < 150:
        return empty_result(params), []
    partial_plan = partial_exit_plan(params.target_r)
    closes, highs, lows, quotes = ([bar.close for bar in bars], [bar.high for bar in bars], [bar.low for bar in bars], [bar.quote_volume for bar in bars])
    short_ema, rel_atr = ema(closes, 16), atr(bars)
    trades: list[Trade] = []
    index = max(100, params.peak_window + 10)
    cooldown = -1
    while index < len(bars) - 2:
        if index <= cooldown:
            index += 1
            continue
        gain24 = closes[index] / closes[index - 96] - 1 if closes[index - 96] else 0
        if gain24 < 0.40 or rolling_sum(quotes, index, 96) < 30_000_000:
            index += 1
            continue
        peak_start, peak_end = max(96, index - params.peak_window * 4), index - 4
        if peak_end <= peak_start:
            index += 1
            continue
        peak_index = peak_start + max(range(peak_end - peak_start + 1), key=lambda offset: highs[peak_start + offset])
        peak = highs[peak_index]
        pullback_low = min(lows[peak_index + 1:index]) if peak_index + 1 < index else peak
        retrace = 1 - pullback_low / peak if peak else 0
        near_peak = highs[index] >= peak * (1 - params.tolerance_pct)
        weak = {"prev_low": closes[index] < lows[index - 1], "bear_half": closes[index] < bars[index].open and closes[index] <= lows[index] + (highs[index] - lows[index]) * 0.5, "ema": closes[index] < short_ema[index]}[params.weakness]
        entry_timing = {
            "close_retest": near_peak and closes[index] >= peak * (1 - params.tolerance_pct),
            "wick_retest": near_peak and closes[index] < peak and closes[index] < bars[index].open,
            "breakdown_retest": near_peak and closes[index] < short_ema[index] and closes[index - 1] < short_ema[index - 1],
        }[params.entry_mode]
        if not (retrace >= params.retrace_pct and peak_index < index - 2 and entry_timing and weak):
            index += 1
            continue
        entry_bar = bars[index + 1]
        entry = short_entry_price(entry_bar.open, slippage)
        buffer = rel_atr[index] * params.stop_atr if params.stop_mode == "atr" else peak * params.stop_pct
        initial_stop, risk = peak + buffer, peak + buffer - entry
        if risk <= entry * 0.002 or risk > entry * 0.12:
            index += 1
            continue
        target, stop, remaining, net_r = entry - risk * params.target_r, initial_stop, 1.0, 0.0
        partials, reached_one, reached_two, outcome, exit_ts = [], False, False, "open", bars[-1].ts
        hit_targets: set[int] = set()
        for future_index in range(index + 1, len(bars)):
            future = bars[future_index]
            if future.open >= stop:
                net_r += (entry - future.open) / risk * remaining
                outcome, exit_ts, remaining = "loss", future.ts, 0
                break
            # The OHLC bar does not reveal the intrabar path.  The declared
            # policy is stop-first, so any high touching the active stop exits
            # at that stop even when the same bar never reaches a target.
            if future.high >= stop:
                net_r += (entry - stop) / risk * remaining
                outcome, exit_ts, remaining = "loss", future.ts, 0
                break
            for target_index, (level, fraction, name) in enumerate(partial_plan):
                if target_index in hit_targets:
                    continue
                if remaining + 1e-9 >= fraction and future.low <= entry - risk * level:
                    net_r += level * fraction
                    remaining = max(0.0, remaining - fraction)
                    hit_targets.add(target_index)
                    partials.append(name)
                    if level == 1.0:
                        reached_one, stop = True, min(stop, entry)
                    if level == 2.0:
                        reached_two = True
                    if remaining <= 1e-9:
                        outcome, exit_ts = "win", future.ts
                        break
            if remaining <= 1e-9:
                break
            if reached_one or reached_two:
                recent_high = max(highs[max(index + 1, future_index - params.trail_bars):future_index + 1])
                stop = min(stop, recent_high)
        if outcome == "open":
            net_r += (entry - bars[-1].close) / risk * remaining
            outcome, exit_ts = ("managed" if net_r >= 0 else "loss"), bars[-1].ts
        # Entry slippage is already reflected in `entry`; charge the second
        # one-way slip on the eventual buy-to-cover exit.
        net_r -= (fee_rate * 2 + slippage + funding_rate) * entry / risk
        margin_return = net_r * risk_fraction * 2 * risk / entry * 100
        trades.append(Trade(symbol, entry_bar.ts, exit_ts, entry, initial_stop, target, net_r, outcome, ",".join(partials), margin_return))
        cooldown, index = index + params.cooldown_bars, index + 1
    wins, losses = sum(t.net_r > 0 for t in trades), sum(t.net_r < 0 for t in trades)
    positive, negative = [t.net_r for t in trades if t.net_r > 0], [-t.net_r for t in trades if t.net_r < 0]
    equity = peak = max_dd = 0.0
    consecutive = current = 0
    for trade in trades:
        equity += trade.net_r
        peak, max_dd = max(peak, equity), max(max_dd, peak - equity)
        current = current + 1 if trade.net_r < 0 else 0
        consecutive = max(consecutive, current)
    rr = (sum(positive) / len(positive)) / (sum(negative) / len(negative)) if positive and negative else 0
    return Result(params.as_dict(), len(trades), wins, losses, wins / len(trades) if trades else 0, beta_interval(wins, losses), sum(t.net_r for t in trades), sum(t.net_r for t in trades) / len(trades) if trades else 0, rr, sum(positive) / sum(negative) if positive and negative else 0, max_dd, consecutive, bootstrap_positive_probability(trades)), trades


def aggregate(data: dict[str, list[Bar]], btc: list[Bar], params: Params, fee: float, slip: float, funding: float) -> tuple[Result, list[Trade]]:
    # Symbols are loaded concurrently, so dictionary insertion order is not a
    # market chronology.  Portfolio drawdown and loss streaks must follow the
    # actual entry sequence, with symbol as a deterministic tie-breaker.
    trades = sorted(
        (trade for symbol, bars in data.items() for trade in backtest(symbol, bars, btc, params, fee, slip, funding)[1]),
        key=lambda trade: (trade.entry_ts, trade.symbol, trade.exit_ts),
    )
    wins, losses = sum(t.net_r > 0 for t in trades), sum(t.net_r < 0 for t in trades)
    positive, negative = [t.net_r for t in trades if t.net_r > 0], [-t.net_r for t in trades if t.net_r < 0]
    equity = peak = max_dd = 0.0
    for trade in trades:
        equity += trade.net_r
        peak, max_dd = max(peak, equity), max(max_dd, peak - equity)
    consecutive = current = 0
    for trade in trades:
        current = current + 1 if trade.net_r < 0 else 0
        consecutive = max(consecutive, current)
    rr = (sum(positive) / len(positive)) / (sum(negative) / len(negative)) if positive and negative else 0
    result = Result(params.as_dict(), len(trades), wins, losses, wins / len(trades) if trades else 0, beta_interval(wins, losses), sum(t.net_r for t in trades), sum(t.net_r for t in trades) / len(trades) if trades else 0, rr, sum(positive) / sum(negative) if positive and negative else 0, max_dd, consecutive, bootstrap_positive_probability(trades))
    return result, trades


def grid() -> list[Params]:
    # Keep the walk-forward search tractable while covering each decision family.
    values = itertools.product((16, 32), (0.02,), (0.01, 0.02), ("prev_low", "bear_half", "ema"), ("atr", "pct"), (0.5, 1.0), (0.01,), (3.0, 4.0, 6.0), (2,), ("close_retest", "wick_retest", "breakdown_retest"), (8,))
    return [Params(*value) for value in values]


def choose(data: dict[str, list[Bar]], btc: list[Bar], fee: float, slip: float, funding: float, minimum: int) -> tuple[Result, list[Result]]:
    all_results = [aggregate(data, btc, params, fee, slip, funding)[0] for params in grid()]
    results = [result for result in all_results if result.trades >= minimum]
    # Sparse composite conditions should produce a report, not hide the fact
    # that the requested minimum sample size was unavailable.
    if not results:
        results = all_results
    results.sort(key=lambda r: (-(r.avg_net_r > 0), -(r.win_rate >= 0.5), -r.positive_probability["positive_mean_probability"], -r.avg_net_r, -r.trades, r.max_drawdown_r))
    return results[0], results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=50)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--min-trades", type=int, default=20)
    parser.add_argument("--fee-rate", type=float, default=0.0006)
    parser.add_argument("--slippage", type=float, default=0.0002)
    parser.add_argument("--funding-rate", type=float, default=0.0)
    parser.add_argument(
        "--allow-live-selection-fallback",
        action="store_true",
        help="允许在历史缓存不足时用当前 live 24h 成交额补齐标的（会引入选择前视）",
    )
    parser.add_argument("--end", help="固定结束时间，ISO-8601，例如 2026-09-26T19:00:00+00:00")
    args = parser.parse_args()
    if args.symbols < 1:
        parser.error("--symbols must be at least 1")
    if args.days < 180:
        parser.error("--days must be at least 180 for the three 60/30/30-day walk-forward folds")
    if args.min_trades < 0 or args.fee_rate < 0 or args.slippage < 0:
        parser.error("--min-trades, --fee-rate and --slippage must be non-negative")
    now = datetime.now(UTC)
    end = parse_utc(args.end) if args.end else now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    data_dir, output_dir = args.data_dir, args.output_dir
    historical_symbols = historical_candidate_symbols(data_dir, start_ms, end_ms, args.symbols)
    symbols = candidate_symbols(
        args.symbols,
        start_ms=start_ms,
        end_ms=end_ms,
        data_dir=data_dir,
        allow_live_fallback=args.allow_live_selection_fallback,
    )
    if len(historical_symbols) >= args.symbols:
        selection_method = "historical_cache_first_60_training_days"
    elif args.allow_live_selection_fallback and historical_symbols:
        selection_method = "historical_cache_first_60_training_days_plus_current_live_volume_fallback"
    elif args.allow_live_selection_fallback:
        selection_method = "current_live_24h_volume_fallback"
    else:
        selection_method = "historical_cache_only_incomplete"
    if not symbols:
        parser.error(
            "no candidate symbols found in the historical cache; add complete training caches "
            "or pass --allow-live-selection-fallback to opt into current-volume selection"
        )
    print(f"window={start.isoformat()}..{end.isoformat()} interval=15m symbols={len(symbols)}")
    btc = load_bars("BTC-USDT-SWAP", start_ms, end_ms, data_dir)
    data, skips = {}, {}
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(load_bars, symbol, start_ms, end_ms, data_dir): symbol for symbol in symbols}
        for count, future in enumerate(as_completed(futures), 1):
            symbol = futures[future]
            try:
                data[symbol] = future.result()
                print(f"[{count}/{len(symbols)}] {symbol}: {len(data[symbol])} bars")
            except Exception as error:
                skips[symbol] = str(error)
                print(f"skip {symbol}: {error}")
    parameters = grid()
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"15m_{args.days}d"
    with (output_dir / f"{prefix}_parameter_grid.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(parameters[0].as_dict()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(parameter.as_dict() for parameter in parameters)
    cut = lambda values, lo, hi: [bar for bar in values if lo <= bar.ts < hi]
    folds, selected = [], []
    for fold, offset in enumerate((0, 30, 60), 1):
        train_start, train_end = start + timedelta(days=offset), start + timedelta(days=offset + 60)
        val_end, test_end = train_end + timedelta(days=30), train_end + timedelta(days=60)
        ranges = [(train_start, train_end), (train_end, val_end), (val_end, test_end)]
        sets = [{symbol: cut(bars, int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)) for symbol, bars in data.items()} for lo, hi in ranges]
        btc_sets = [cut(btc, int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)) for lo, hi in ranges]
        best, ranked = choose(sets[0], btc_sets[0], args.fee_rate, args.slippage, args.funding_rate, args.min_trades)
        validation, _ = aggregate(sets[1], btc_sets[1], Params(**best.params), args.fee_rate, args.slippage, args.funding_rate)
        test, test_trades = aggregate(sets[2], btc_sets[2], Params(**best.params), args.fee_rate, args.slippage, args.funding_rate)
        selected.append(Params(**best.params))
        folds.append({"fold": fold, "train": asdict(best), "validation": asdict(validation), "test": asdict(test), "candidate_count": len(ranked), "minimum_train_trades": args.min_trades, "minimum_train_trades_met": best.trades >= args.min_trades, "trades": [asdict(trade) for trade in test_trades]})
        print(f"fold={fold} train={best.trades}/{best.win_rate:.1%} validation={validation.trades}/{validation.win_rate:.1%} test={test.trades}/{test.win_rate:.1%}")
    # Preserve fold order when several parameters have the same vote count;
    # set iteration is intentionally hash-randomized between Python processes.
    chosen = max(enumerate(selected), key=lambda item: (selected.count(item[1]), -item[0]))[1]
    final, _ = aggregate(data, btc, chosen, args.fee_rate, args.slippage, args.funding_rate)
    test_results = [fold["test"] for fold in folds]
    oos_trades, oos_r = sum(item["trades"] for item in test_results), sum(item["total_r"] for item in test_results)
    oos_wins = sum(item["wins"] for item in test_results)
    passed = all(item["win_rate"] >= 0.50 and item["avg_net_r"] > 0 and item["positive_probability"]["positive_mean_probability"] > 0.50 for item in test_results) and all(fold["minimum_train_trades_met"] for fold in folds)
    expected_bars = max(0, (end_ms - start_ms) // BAR_INTERVAL_MS)
    data_quality = {
        "BTC-USDT-SWAP": {"bars": len(btc), "expected_bars": expected_bars, "complete": _complete_bars(btc, start_ms, end_ms)},
        **{
            symbol: {"bars": len(bars), "expected_bars": expected_bars, "complete": _complete_bars(bars, start_ms, end_ms)}
            for symbol, bars in data.items()
        },
    }
    report = {"window": {"start": start.isoformat(), "end": end.isoformat()}, "interval": "15m", "leverage": 2.0, "symbols": list(data), "skips": skips, "screening_stats": {symbol: screening_stats(bars) for symbol, bars in data.items()}, "data_quality": data_quality, "selection_method": selection_method, "selection_training_days": SELECTION_DAYS, "survivorship_bias": "the instrument universe is still limited to contracts currently reported as live because historical listing metadata is unavailable", "assumptions": {"gain_window_bars": 96, "min_gain": 0.40, "min_quote_volume_24h": 30_000_000, "fee_rate_one_way": args.fee_rate, "slippage_one_way": args.slippage, "funding_rate_per_trade": args.funding_rate, "same_bar_priority": "stop_first", "risk_fraction": 0.005}, "selected_params": chosen.as_dict(), "final_all_period": asdict(final), "folds": folds, "sample_out_of_sample_trades": oos_trades, "sample_out_of_sample_win_rate": oos_wins / max(oos_trades, 1), "sample_out_of_sample_total_r": oos_r, "passed": passed}
    (output_dir / f"{prefix}_optimization_report.json").write_text(json.dumps(report, indent=2))
    with (output_dir / f"{prefix}_trades_test.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(Trade.__dataclass_fields__), lineterminator="\n")
        writer.writeheader()
        writer.writerows(trade for fold in folds for trade in fold["trades"])
    print(f"selected={chosen.as_dict()}")
    print(f"final_all_period trades={final.trades} win_rate={final.win_rate:.1%} avg_net_R={final.avg_net_r:.3f} actual_RR={final.average_win_loss_ratio:.2f}")
    print(f"out_of_sample trades={oos_trades} win_rate={oos_wins / max(oos_trades, 1):.1%} total_R={oos_r:.2f} status={'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
