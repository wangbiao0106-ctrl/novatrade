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
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

from altcoin_backtest import Bar, atr, ema, rolling_sum, short_entry_price
from high_short_strategy import (DEFAULT_COOLDOWN_BARS, DEFAULT_MIN_GAIN_24H,
                                 DEFAULT_MIN_QUOTE_VOLUME_24H, DEFAULT_PARTIAL_FRACTIONS,
                                 Params, rejection_reasons, resample)


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


@dataclass(frozen=True)
class LabConfig:
    """实验室机器参数真源的整体视图（生成器不再硬编码任何阈值）。"""
    params: Params
    min_gain_24h: float
    min_quote_volume_24h: float
    cooldown_bars: int
    leverage: float
    partial_fractions: tuple[float, ...]
    slippage: float = 0.0002

    @property
    def partial_plan(self) -> str:
        labels = ("TP1", "TP2", "TP3", "TP4")
        return ",".join(f"{labels[index]}:{fraction:.0%}" for index, fraction in enumerate(self.partial_fractions))

    @staticmethod
    def load(path: Path | None = None) -> "LabConfig":
        import high_short_strategy as base

        if path is None:
            raw = base.load_lab_config()
        else:
            raw = base.load_lab_config(Path(path))
        hard, position, costs = raw["hard"], raw["position"], raw["costs"]
        merged = dict(raw["signal"])
        merged.setdefault("gain_24h_gt", hard.get("gain_24h_gt", base.DEFAULT_MIN_GAIN_24H))
        merged.setdefault("quote_volume_24h_gt", hard.get("quote_volume_24h_gt", base.DEFAULT_MIN_QUOTE_VOLUME_24H))
        merged.setdefault("cooldown_bars", position.get("cooldown_bars", base.DEFAULT_COOLDOWN_BARS))
        merged.setdefault("partial_targets", position.get("partial_targets", base.DEFAULT_PARTIAL_FRACTIONS))
        return LabConfig(
            params=load_params_values(merged),
            slippage=float(costs.get("slippage", 0.0002)),
            min_gain_24h=float(hard.get("gain_24h_gt", 0.40)),
            min_quote_volume_24h=float(hard.get("quote_volume_24h_gt", 30_000_000)),
            cooldown_bars=int(position.get("cooldown_bars", 16)),
            leverage=float(position.get("leverage", 2.0)),
            partial_fractions=tuple(float(value) for value in position.get("partial_targets", (0.3, 0.3, 0.4))),
        )


def load_params_values(values: dict) -> Params:
    """由 config 的 signal_parameters 构造 Params。"""
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
        min_gain_24h=float(values.get("gain_24h_gt", DEFAULT_MIN_GAIN_24H)),
        min_quote_volume_24h=float(values.get("quote_volume_24h_gt", DEFAULT_MIN_QUOTE_VOLUME_24H)),
        cooldown_bars=int(values.get("cooldown_bars", DEFAULT_COOLDOWN_BARS)),
        partial_fractions=tuple(float(value) for value in values.get("partial_targets", DEFAULT_PARTIAL_FRACTIONS)),
    )


def load_params(path: Path | None) -> Params:
    """Load signal parameters from the standard JSON config when provided."""
    if path is None:
        return STANDARD_PARAMS
    payload = json.loads(path.read_text())
    values = dict(payload.get("signal_parameters", payload))
    values.setdefault("gain_24h_gt", payload.get("hard_filters", {}).get("gain_24h_gt", DEFAULT_MIN_GAIN_24H))
    values.setdefault("quote_volume_24h_gt", payload.get("hard_filters", {}).get("quote_volume_24h_gt", DEFAULT_MIN_QUOTE_VOLUME_24H))
    values.setdefault("cooldown_bars", payload.get("position_management", {}).get("cooldown_bars", DEFAULT_COOLDOWN_BARS))
    values.setdefault("partial_targets", payload.get("position_management", {}).get("partial_targets", DEFAULT_PARTIAL_FRACTIONS))
    return load_params_values(values)


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
    # Keep one source candle per timestamp.  A duplicate row must not make a
    # bucket appear complete when another child is missing.
    groups: dict[int, dict[int, Bar]] = {}
    interval = 15 * 60_000
    source_minutes = int(source_minutes)
    if source_minutes <= 0 or interval % (source_minutes * 60_000):
        raise ValueError("source_minutes must be a positive divisor of 15")
    expected_children = interval // (source_minutes * 60_000)
    for row in _rows(path):
        if not isinstance(row, dict):
            continue
        confirmed = row.get("confirmed", True)
        if isinstance(confirmed, str):
            confirmed = confirmed.strip().lower() not in {"", "0", "false", "no"}
        if not confirmed:
            continue
        try:
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
        except (KeyError, TypeError, ValueError, OverflowError):
            continue
        values = (bar.open, bar.high, bar.low, bar.close, bar.volume, bar.quote_volume)
        if (
            ts < 0
            or not all(math.isfinite(value) for value in values)
            or bar.open <= 0
            or bar.high <= 0
            or bar.low <= 0
            or bar.close <= 0
            or bar.volume < 0
            or bar.quote_volume < 0
            or bar.high < max(bar.open, bar.close)
            or bar.low > min(bar.open, bar.close)
            or bar.high < bar.low
        ):
            continue
        bucket = ts // interval * interval
        groups.setdefault(bucket, {})[ts] = bar
    bars: list[Bar] = []
    for bucket in sorted(groups):
        children_by_ts = groups[bucket]
        expected = [bucket + index * source_minutes * 60_000
                    for index in range(expected_children)]
        if sorted(children_by_ts) != expected:
            continue
        children = [children_by_ts[ts] for ts in expected]
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
    htf_indices: list[int],
    ranges: list[float],
    regimes: list[tuple[str, float, float, float, float, float]],
    sweep_index: int,
    confirmation_index: int,
    params: Params,
    slippage: float,
    leverage: float,
    partial_plan: str,
) -> Signal | None:
    if sweep_index < max(150, params.swing_lookback + 20):
        return None
    gain_24h = closes[sweep_index] / closes[sweep_index - 96] - 1 if closes[sweep_index - 96] else 0.0
    quote_volume_24h = rolling_sum(volumes, sweep_index, 96)
    if gain_24h <= params.min_gain_24h or quote_volume_24h <= params.min_quote_volume_24h:
        return None

    htf_index = htf_indices[sweep_index]
    if htf_index < 0:
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
        partial_plan=partial_plan,
        regime=regime,
        zone=zone,
        confirmation=confirmation,
        gain_24h=gain_24h,
        quote_volume_24h=quote_volume_24h,
        rejection_score=len(reasons),
        rejection_reasons=reasons,
        invalidation="close_above_sweep_high_or_stop",
        leverage=leverage,
        params={**params.as_dict(), "entry_reference": entry_source},
    )


def generate_signals(symbol: str, bars: list[Bar], params: Params | None = None,
                     slippage: float | None = None, config: LabConfig | None = None) -> list[dict]:
    """Return confirmed HLSR signals in chronological order."""
    if len(bars) < 180:
        return []
    config = config or LabConfig.load()
    # When callers omit explicit parameters, use the loaded machine source of
    # truth.  The old default bound STANDARD_PARAMS at function definition
    # time, so changing config/strategy.json affected the CLI but not library
    # callers (and therefore could silently invalidate tests and integrations).
    params = params or config.params
    if slippage is None:
        slippage = config.slippage
    partial_plan = config.partial_plan
    four_hour = resample(bars, 240)
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
            signal = _candidate_signal(symbol, bars, closes, highs, lows, volumes, htf_indices, ranges, regimes, sweep_index, confirmation_index, params, slippage, config.leverage, partial_plan)
            if signal is not None:
                signals.append(signal)
                cooldown = confirmation_index + max(0, int(params.cooldown_bars))
                break
    return [asdict(signal) for signal in signals]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--symbol", default="UNKNOWN-USDT-SWAP")
    parser.add_argument("--source-minutes", type=int, choices=(5, 15), default=5)
    parser.add_argument("--config", type=Path, help="策略 JSON 配置文件")
    parser.add_argument("--slippage", type=float, default=None, help="默认取 config 的 costs.slippage")
    parser.add_argument("--all", action="store_true", help="输出全部历史信号，而不是最近一个")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    bars = load_bars(args.input, args.source_minutes)
    config = LabConfig.load(args.config) if args.config else LabConfig.load()
    if args.slippage is None:
        args.slippage = config.slippage
    signals = generate_signals(args.symbol, bars, params=config.params, slippage=args.slippage, config=config)
    payload = signals if args.all else (signals[-1] if signals else None)
    encoded = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(encoded + "\n")
    else:
        print(encoded)


if __name__ == "__main__":
    main()
