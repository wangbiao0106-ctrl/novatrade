#!/usr/bin/env python3
"""Generate HLSR short signals from confirmed OHLCV bars.

The generator is deliberately read-only. It never connects to an exchange and
never submits an order. Input may be a JSON/JSONL file or a gzip-compressed
JSONL file containing 5-minute or 15-minute candles.
"""

from __future__ import annotations

import argparse
import gzip
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from altcoin_backtest import Bar, atr, ema, rolling_sum, short_entry_price
from high_short_strategy import Params, rejection_reasons, resample


STANDARD_PARAMS = Params(
    swing_lookback=6,
    wick_ratio=0.6,
    volume_multiple=1.0,
    minimum_rejection_score=2,
    confirmation_window=4,
    stop_atr=0.25,
    trail_bars=2,
    allow_range=True,
    zone_required="any",
    reject_depth_atr=0.1,
)


def load_params(path: Path | None) -> Params:
    """Load signal parameters from the standard JSON config when provided."""
    if path is None:
        return STANDARD_PARAMS
    payload = json.loads(path.read_text())
    values = payload.get("signal_parameters", payload)
    return Params(
        swing_lookback=int(values["swing_lookback"]),
        wick_ratio=float(values["wick_ratio"]),
        volume_multiple=float(values["volume_multiple"]),
        minimum_rejection_score=int(values["minimum_rejection_score"]),
        confirmation_window=int(values["confirmation_window"]),
        stop_atr=float(values["stop_atr"]),
        trail_bars=int(values["trail_bars"]),
        allow_range=bool(values["allow_range"]),
        zone_required=str(values["zone_required"]),
        reject_depth_atr=float(values.get("reject_depth_atr", STANDARD_PARAMS.reject_depth_atr)),
    )


@dataclass(frozen=True)
class Signal:
    strategy: str
    symbol: str
    status: str
    signal_ts: int
    entry_ts: int | None
    sweep_ts: int
    entry: float
    sweep_high: float
    stop: float
    risk_per_unit: float
    tp1: float
    tp2: float
    tp3: float
    partial_plan: str
    regime: str
    zone: str
    confirmation: str
    gain_24h: float
    quote_volume_24h: float
    rejection_score: int
    rejection_reasons: tuple[str, ...]
    invalidation: str
    leverage: float
    params: dict


def _open_text(path: Path):
    return gzip.open(path, "rt") if path.suffix == ".gz" else path.open("rt")


def _rows(path: Path) -> Iterable[dict]:
    with _open_text(path) as handle:
        content = handle.read()
    if content.lstrip().startswith("["):
        yield from json.loads(content)
        return
    for line in content.splitlines():
        if line.strip():
            yield json.loads(line)


def load_bars(path: Path, source_minutes: int = 5) -> list[Bar]:
    """把源 K 线聚合为 15 分钟 Bar。

    `source_minutes` 决定"一个完整 15m 桶需要几根源 K 线"（5 → 3 根）。
    桶内源 K 线数量不足时整根丢弃：否则导出末尾那根只含 1-2 根 5m 的"幽灵 bar"
    会被当成已收盘 15m K 线参与确认判断。
    """
    groups: dict[int, list[Bar]] = {}
    interval = 15 * 60_000
    expected_children = max(1, interval // (max(1, int(source_minutes)) * 60_000))
    for row in _rows(path):
        if not row.get("confirmed", True):
            continue
        ts = int(row.get("timestamp_ms", row.get("ts")))
        bar = Bar(
            ts=ts,
            open=float(row["open"]),
            high=float(row["high"]),
            low=float(row["low"]),
            close=float(row["close"]),
            volume=float(row.get("volume", 0.0)),
            quote_volume=float(row.get("quote_volume", row.get("volCcyQuote", row.get("volume", 0.0)))),
        )
        bucket = ts // interval * interval
        groups.setdefault(bucket, []).append(bar)
    bars: list[Bar] = []
    for bucket in sorted(groups):
        children = sorted(groups[bucket], key=lambda value: value.ts)
        if len(children) < expected_children:
            continue
        bars.append(Bar(
            bucket,
            children[0].open,
            max(child.high for child in children),
            min(child.low for child in children),
            children[-1].close,
            sum(child.volume for child in children),
            sum(child.quote_volume for child in children),
        ))
    return bars


def _completed_index_map(bars: list[Bar], higher_bars, interval_ms: int) -> list[int]:
    """Map each lower-timeframe bar to the latest completed higher bar."""
    pointer = -1
    result: list[int] = []
    for bar in bars:
        while pointer + 1 < len(higher_bars) and higher_bars[pointer + 1].ts + interval_ms <= bar.ts:
            pointer += 1
        result.append(pointer)
    return result


def _candidate_signal(
    symbol: str,
    bars: list[Bar],
    closes: list[float],
    highs: list[float],
    lows: list[float],
    volumes: list[float],
    h1_indices: list[int],
    htf_indices: list[int],
    ranges: list[float],
    regimes: list[tuple[str, float, float, float, float, float]],
    sweep_index: int,
    confirmation_index: int,
    params: Params,
    slippage: float,
) -> Signal | None:
    if sweep_index < max(150, params.swing_lookback + 20):
        return None
    gain_24h = closes[sweep_index] / closes[sweep_index - 96] - 1 if closes[sweep_index - 96] else 0.0
    quote_volume_24h = rolling_sum(volumes, sweep_index, 96)
    if gain_24h <= 0.40 or quote_volume_24h <= 30_000_000:
        return None

    htf_index = htf_indices[sweep_index]
    h1_index = h1_indices[sweep_index]
    if htf_index < 0 or h1_index < 20:
        return None
    regime, _, resistance, midpoint, major_low, htf_atr = regimes[htf_index]
    if regime not in ({"bearish", "range"} if params.allow_range else {"bearish"}):
        return None

    previous_swing_high = max(highs[sweep_index - params.swing_lookback:sweep_index])
    if not (highs[sweep_index] > previous_swing_high and closes[sweep_index] < previous_swing_high):
        return None
    mean_volume = sum(volumes[sweep_index - 20:sweep_index]) / 20
    reasons = rejection_reasons(
        params,
        open_price=bars[sweep_index].open, high=highs[sweep_index], low=lows[sweep_index],
        close=closes[sweep_index], previous_swing_high=previous_swing_high,
        atr14=ranges[sweep_index], volume=volumes[sweep_index], mean_volume=mean_volume,
    )
    if len(reasons) < params.minimum_rejection_score:
        return None
    if not (sweep_index < confirmation_index <= sweep_index + params.confirmation_window):
        return None
    break_of_local_low = closes[confirmation_index] < min(lows[sweep_index + 1:confirmation_index]) if confirmation_index > sweep_index + 1 else False
    failed_retest = (
        highs[confirmation_index] >= previous_swing_high * 0.995
        and closes[confirmation_index] < bars[confirmation_index].open
        and closes[confirmation_index] < previous_swing_high
    )
    confirmation = "break_of_local_low" if break_of_local_low else "failed_retest" if failed_retest else ""
    if not confirmation:
        return None

    entry_index = confirmation_index + 1
    entry_ts = bars[entry_index].ts if entry_index < len(bars) else None
    entry = short_entry_price(bars[entry_index].open, slippage) if entry_ts is not None else closes[confirmation_index]
    entry_source = "next_open" if entry_ts is not None else "confirmation_close_estimate"
    sweep_atr = ranges[sweep_index]
    stop = highs[sweep_index] + params.stop_atr * sweep_atr
    risk = stop - entry
    if risk <= entry * 0.002 or risk > entry * 0.15:
        return None
    zone = "normal_extension" if highs[sweep_index] <= resistance + htf_atr else "primary_sweep" if highs[sweep_index] <= resistance + 2 * htf_atr else "extreme_sweep"
    if params.zone_required != "any" and zone != params.zone_required:
        return None

    support = min(lows[max(0, confirmation_index - 32):confirmation_index + 1])
    tp1 = support if support < entry else entry - risk
    tp2 = midpoint if midpoint < tp1 else entry - 2 * risk
    tp3 = major_low if major_low < tp2 else entry - 3 * risk
    targets = sorted({round(target, 12) for target in (tp1, tp2, tp3) if target < entry}, reverse=True)
    while len(targets) < 3:
        targets.append(entry - risk * (len(targets) + 1))
    targets = targets[:3]
    return Signal(
        strategy="HLSR",
        symbol=symbol,
        status="READY" if entry_ts is not None else "PENDING_NEXT_OPEN",
        signal_ts=bars[confirmation_index].ts,
        entry_ts=entry_ts,
        sweep_ts=bars[sweep_index].ts,
        entry=entry,
        sweep_high=highs[sweep_index],
        stop=stop,
        risk_per_unit=risk,
        tp1=targets[0],
        tp2=targets[1],
        tp3=targets[2],
        partial_plan="TP1:30%,TP2:30%,TP3:40%",
        regime=regime,
        zone=zone,
        confirmation=confirmation,
        gain_24h=gain_24h,
        quote_volume_24h=quote_volume_24h,
        rejection_score=len(rejection_reasons),
        rejection_reasons=rejection_reasons,
        invalidation="close_above_sweep_high_or_stop",
        leverage=2.0,
        params={**params.as_dict(), "entry_reference": entry_source},
    )


def generate_signals(symbol: str, bars: list[Bar], params: Params = STANDARD_PARAMS, slippage: float = 0.0002) -> list[dict]:
    """Return confirmed HLSR signals in chronological order."""
    if len(bars) < 180:
        return []
    hourly = resample(bars, 60)
    four_hour = resample(bars, 240)
    h1_indices = _completed_index_map(bars, hourly, 60 * 60_000)
    htf_indices = _completed_index_map(bars, four_hour, 240 * 60_000)
    ranges = atr(bars, 14)
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.quote_volume for bar in bars]
    htf_ranges = atr([Bar(bar.ts, bar.open, bar.high, bar.low, bar.close, 0.0, bar.quote_volume) for bar in four_hour], 14)
    htf_closes = [bar.close for bar in four_hour]
    htf_highs = [bar.high for bar in four_hour]
    htf_lows = [bar.low for bar in four_hour]
    fast_ema = ema(htf_closes, 20)
    slow_ema = ema(htf_closes, 50)
    regimes: list[tuple[str, float, float, float, float, float]] = []
    for index in range(len(four_hour)):
        recent_high = max(htf_highs[max(0, index - 19):index + 1])
        major_low = min(htf_lows[max(0, index - 19):index + 1])
        midpoint = (recent_high + major_low) / 2
        width = (recent_high - major_low) / max(midpoint, 1e-12)
        htf_atr = htf_ranges[index]
        # 与 high_short_strategy.regime_at 保持一致：EMA50 至少需要 55 根已收盘
        # 的 4H K 线才有意义，不足时状态必须是 unknown（不允许开仓），不能按
        # EMA20/EMA50 的早期数值猜状态。
        if index < 54:
            state = "unknown"
        elif htf_closes[index] < fast_ema[index] < slow_ema[index]:
            state = "bearish"
        elif htf_closes[index] > fast_ema[index] > slow_ema[index]:
            state = "bullish"
        elif width <= max(0.08, htf_atr / max(midpoint, 1e-12) * 8):
            state = "range"
        else:
            state = "transition"
        regimes.append((state, recent_high, recent_high + htf_atr * 0.25, midpoint, major_low, htf_atr))
    signals: list[Signal] = []
    cooldown = -1
    for confirmation_index in range(151, len(bars)):
        if confirmation_index <= cooldown:
            continue
        for sweep_index in range(max(150, confirmation_index - params.confirmation_window), confirmation_index):
            signal = _candidate_signal(symbol, bars, closes, highs, lows, volumes, h1_indices, htf_indices, ranges, regimes, sweep_index, confirmation_index, params, slippage)
            if signal is not None:
                signals.append(signal)
                cooldown = confirmation_index + 16
                break
    return [asdict(signal) for signal in signals]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--symbol", default="UNKNOWN-USDT-SWAP")
    parser.add_argument("--source-minutes", type=int, choices=(5, 15), default=5)
    parser.add_argument("--config", type=Path, help="策略 JSON 配置文件")
    parser.add_argument("--slippage", type=float, default=0.0002)
    parser.add_argument("--all", action="store_true", help="输出全部历史信号，而不是最近一个")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    bars = load_bars(args.input, args.source_minutes)
    signals = generate_signals(args.symbol, bars, params=load_params(args.config), slippage=args.slippage)
    payload = signals if args.all else (signals[-1] if signals else None)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
