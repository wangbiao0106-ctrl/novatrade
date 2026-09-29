#!/usr/bin/env python3
"""HLSR: High-Level Liquidity Sweep Reversal short backtest."""

from __future__ import annotations

import argparse
import csv
import itertools
import json
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from altcoin_backtest import (
    Bar,
    EXCLUDED_BASES,
    Result,
    Trade,
    atr,
    beta_interval,
    bootstrap_positive_probability,
    candidate_symbols,
    ema,
    load_bars,
    rolling_sum,
    parse_utc,
    short_entry_price,
)

UTC = timezone.utc
PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_DATA_DIR = PROJECT_ROOT / "data/kline/okx/swap/15m"
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "strategies/hlsr/results"
# 选币只允许使用窗口前 60 天（第一折训练期）的成交额；用窗口末尾或整段数据
# 排名会把测试期信息带进样本外结果。
SELECTION_DAYS = 60
# 机器参数真源：strategy.json 的 signal_parameters / hard_filters /
# position_management / costs 都由代码读取，不再是装饰字段。
LAB_CONFIG = PROJECT_ROOT / "strategies" / "hlsr" / "config" / "strategy.json"
# 配置缺键时的兜底默认值（全仓库唯一副本；配置存在时一律以它为准）。
DEFAULT_MIN_GAIN_24H = 0.40
DEFAULT_MIN_QUOTE_VOLUME_24H = 30_000_000.0
DEFAULT_COOLDOWN_BARS = 16
DEFAULT_PARTIAL_FRACTIONS = (0.30, 0.30, 0.40)


def load_lab_config(path: Path | None = None) -> dict:
    """读取实验室机器参数真源，返回 {signal, hard, position, costs}。"""
    payload = json.loads(Path(path or LAB_CONFIG).read_text())
    return {
        "signal": payload.get("signal_parameters", {}),
        "hard": payload.get("hard_filters", {}),
        "position": payload.get("position_management", {}),
        "costs": payload.get("costs", {}),
    }


def fixed_parameters(config: dict | None = None) -> dict:
    """不参与搜索、但必须在网格里保持一致、且来自配置的参数。"""
    config = config or load_lab_config()
    hard = config["hard"]
    position = config["position"]
    return {
        "min_gain_24h": float(hard.get("gain_24h_gt", DEFAULT_MIN_GAIN_24H)),
        "min_quote_volume_24h": float(hard.get("quote_volume_24h_gt", DEFAULT_MIN_QUOTE_VOLUME_24H)),
        "cooldown_bars": int(position.get("cooldown_bars", DEFAULT_COOLDOWN_BARS)),
        "partial_fractions": tuple(float(value) for value in position.get("partial_targets", DEFAULT_PARTIAL_FRACTIONS)),
    }


@dataclass(frozen=True)
class HTFBar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float


def resample(bars: list[Bar], minutes: int) -> list[HTFBar]:
    interval = minutes * 60_000
    groups: dict[int, list[Bar]] = {}
    for bar in bars:
        groups.setdefault(bar.ts // interval * interval, []).append(bar)
    result = []
    for ts, values in sorted(groups.items()):
        values.sort(key=lambda value: value.ts)
        result.append(HTFBar(ts, values[0].open, max(v.high for v in values), min(v.low for v in values), values[-1].close, sum(v.quote_volume for v in values)))
    return result


def latest_completed_index(bars: list[HTFBar], timestamp: int, interval: int) -> int:
    """Return the newest HTF bucket closed before a lower-timeframe bar."""
    return next((index for index in range(len(bars) - 1, -1, -1) if bars[index].ts + interval <= timestamp), -1)


@dataclass(frozen=True)
class Params:
    swing_lookback: int
    wick_ratio: float
    volume_multiple: float
    minimum_rejection_score: int
    confirmation_window: int
    stop_atr: float
    trail_bars: int
    allow_range: bool
    zone_required: str
    # 拒绝评分第 4 项"失败突破"的深度阈值（× ATR14）。必须大于 0，否则第 4 项
    # 会退化成与"扫顶"前置条件重复的恒真项。
    reject_depth_atr: float = 0.1
    # 以下字段来自 config/strategy.json，不参与网格搜索；默认值与配置文件一致，
    # 保证不读配置时行为不变。
    min_gain_24h: float = DEFAULT_MIN_GAIN_24H
    min_quote_volume_24h: float = DEFAULT_MIN_QUOTE_VOLUME_24H
    cooldown_bars: int = DEFAULT_COOLDOWN_BARS
    partial_fractions: tuple[float, ...] = DEFAULT_PARTIAL_FRACTIONS

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class HighShortTrade:
    symbol: str
    entry_ts: int
    exit_ts: int
    entry: float
    sweep_high: float
    stop: float
    tp1: float
    tp2: float
    tp3: float
    net_r: float
    regime: str
    zone: str
    confirmation: str
    partials: str
    invalidated: bool
    exit_reason: str = "managed"


def regime_at(bars: list[HTFBar], index: int) -> tuple[str, float, float, float, float, float]:
    """Return regime, recent high, resistance, midpoint, major low and ATR."""
    index = min(index, len(bars) - 1)
    closes = [bar.close for bar in bars[: index + 1]]
    highs = [bar.high for bar in bars[: index + 1]]
    lows = [bar.low for bar in bars[: index + 1]]
    if len(closes) < 55:
        return "unknown", highs[-1], highs[-1], highs[-1], lows[-1], 0.0
    fast, slow = ema(closes, 20)[-1], ema(closes, 50)[-1]
    htf_atr = atr([Bar(bar.ts, bar.open, bar.high, bar.low, bar.close, 0, bar.quote_volume) for bar in bars[: index + 1]], 14)[-1]
    recent_high = max(highs[-20:])
    major_low = min(lows[-20:])
    midpoint = (recent_high + major_low) / 2
    width = (recent_high - major_low) / max(midpoint, 1e-12)
    if closes[-1] < fast < slow:
        state = "bearish"
    elif closes[-1] > fast > slow:
        state = "bullish"
    elif width <= max(0.08, htf_atr / max(midpoint, 1e-12) * 8):
        state = "range"
    else:
        state = "transition"
    resistance = recent_high + htf_atr * 0.25
    return state, recent_high, resistance, midpoint, major_low, htf_atr


def rejection_reasons(params: Params, *, open_price: float, high: float, low: float, close: float,
                      previous_swing_high: float, atr14: float, volume: float,
                      mean_volume: float) -> tuple[str, ...]:
    """拒绝评分四项的唯一实现，回测器和实时生成器共用。

    第 4 项（失败突破）定义为"深度失败突破"：收盘必须比被扫的前高低出
    `reject_depth_atr × ATR14`。只判断 `close < previous_swing_high` 会与"扫顶"
    的前置条件完全重复而恒为真，使评分整体虚高一分，等于把
    `minimum_rejection_score` 静默降一档。
    """
    candle_range = max(high - low, 1e-12)
    upper_wick = (high - max(open_price, close)) / candle_range
    checks = (
        ("large_upper_wick", upper_wick >= params.wick_ratio),
        ("bearish_close", close < open_price),
        ("volume_expansion", volume > mean_volume * params.volume_multiple),
        ("failed_breakout", close < previous_swing_high - params.reject_depth_atr * atr14),
    )
    return tuple(name for name, passed in checks if passed)


def aggregate_result(params: Params, trades: list[HighShortTrade]) -> Result:
    values = [trade.net_r for trade in trades]
    wins = sum(value > 0 for value in values)
    losses = sum(value < 0 for value in values)
    positive = [value for value in values if value > 0]
    negative = [-value for value in values if value < 0]
    equity = peak = drawdown = 0.0
    consecutive = current = 0
    for value in values:
        equity += value
        peak = max(peak, equity)
        drawdown = max(drawdown, peak - equity)
        current = current + 1 if value < 0 else 0
        consecutive = max(consecutive, current)
    return Result(params.as_dict(), len(values), wins, losses, wins / len(values) if values else 0.0,
                  beta_interval(wins, losses), sum(values), sum(values) / len(values) if values else 0.0,
                  (sum(positive) / len(positive)) / (sum(negative) / len(negative)) if positive and negative else 0.0,
                  sum(positive) / sum(negative) if positive and negative else 0.0, drawdown, consecutive,
                  bootstrap_positive_probability([Trade(t.symbol, t.entry_ts, t.exit_ts, t.entry, t.stop, t.tp3, t.net_r, "win" if t.net_r > 0 else "loss", t.partials, 0.0) for t in trades]))


def backtest(symbol: str, bars: list[Bar], params: Params, fee: float, slippage: float, funding: float) -> tuple[Result, list[HighShortTrade]]:
    if len(bars) < 300:
        return aggregate_result(params, []), []
    # 市场状态只用 4H：此前额外算了一条 1H 序列，但只用于 `h1_index < 20` 这个
    # 恒真 guard，1H 从未参与任何判定。文档已同步为"15m 入场 + 4H 状态"。
    four_hour = resample(bars, 240)
    closes = [bar.close for bar in bars]
    highs = [bar.high for bar in bars]
    lows = [bar.low for bar in bars]
    volumes = [bar.quote_volume for bar in bars]
    ranges = atr(bars, 14)
    trades: list[HighShortTrade] = []
    cooldown = -1
    index = max(150, params.swing_lookback + 20)
    while index < len(bars) - 2:
        if index <= cooldown:
            index += 1
            continue
        gain24 = closes[index] / closes[index - 96] - 1 if closes[index - 96] else 0.0
        quote24 = rolling_sum(volumes, index, 96)
        # The user's >40% requirement is a hard filter, not an optimized hint.
        # 阈值来自 config/strategy.json 的 hard_filters。
        if gain24 <= params.min_gain_24h or quote24 <= params.min_quote_volume_24h:
            index += 1
            continue
        # A resampled HTF bar contains all of its 15m children.  At the
        # current 15m close, the bucket containing that bar is still forming;
        # only completed buckets may influence a historical signal.
        htf_interval = 240 * 60_000
        htf_index = latest_completed_index(four_hour, bars[index].ts, htf_interval)
        if htf_index < 0:
            index += 1
            continue
        regime, recent_high, resistance, midpoint, major_low, htf_atr = regime_at(four_hour, htf_index)
        if regime not in ({"bearish", "range"} if params.allow_range else {"bearish"}):
            index += 1
            continue
        previous_swing_high = max(highs[index - params.swing_lookback:index])
        sweep = highs[index] > previous_swing_high and closes[index] < previous_swing_high
        if not sweep:
            index += 1
            continue
        mean_volume = sum(volumes[index - 20:index]) / 20
        reasons = rejection_reasons(
            params,
            open_price=bars[index].open, high=highs[index], low=lows[index], close=closes[index],
            previous_swing_high=previous_swing_high, atr14=ranges[index],
            volume=volumes[index], mean_volume=mean_volume,
        )
        if len(reasons) < params.minimum_rejection_score:
            index += 1
            continue
        local_low = lows[index]
        confirmation_index = None
        confirmation = ""
        for confirm in range(index + 1, min(index + params.confirmation_window + 1, len(bars) - 1)):
            local_low = min(local_low, lows[confirm])
            break_of_local_low = closes[confirm] < min(lows[index + 1:confirm]) if confirm > index + 1 else False
            failed_retest = highs[confirm] >= previous_swing_high * 0.995 and closes[confirm] < bars[confirm].open and closes[confirm] < previous_swing_high
            if break_of_local_low:
                confirmation_index, confirmation = confirm, "break_of_local_low"
                break
            if failed_retest:
                confirmation_index, confirmation = confirm, "failed_retest"
                break
        if confirmation_index is None:
            index += 1
            continue
        entry_bar = bars[confirmation_index + 1]
        entry = short_entry_price(entry_bar.open, slippage)
        sweep_atr = ranges[index]
        stop = highs[index] + params.stop_atr * sweep_atr
        risk = stop - entry
        if risk <= entry * 0.002 or risk > entry * 0.15:
            index = confirmation_index + 1
            continue
        zone = "normal_extension" if highs[index] <= resistance + htf_atr else "primary_sweep" if highs[index] <= resistance + 2 * htf_atr else "extreme_sweep"
        if params.zone_required != "any" and zone != params.zone_required:
            index = confirmation_index + 1
            continue
        support = min(lows[max(0, confirmation_index - 32):confirmation_index + 1])
        tp1 = support if support < entry else entry - risk
        tp2 = midpoint if midpoint < tp1 else entry - 2 * risk
        tp3 = major_low if major_low < tp2 else entry - 3 * risk
        # Structural targets may arrive out of order; a short must take the
        # nearest lower target first, then progressively lower targets.
        targets = sorted({round(target, 12) for target in (tp1, tp2, tp3) if target < entry}, reverse=True)
        while len(targets) < 3:
            targets.append(entry - risk * (len(targets) + 1))
        targets = targets[:3]
        current_stop, remaining, net_r = stop, 1.0, 0.0
        partials: list[str] = []
        hit_targets: set[int] = set()
        invalidated = False
        exit_ts = bars[-1].ts
        outcome = "managed"
        for future_index in range(confirmation_index + 1, len(bars)):
            future = bars[future_index]
            if future.open >= current_stop:
                net_r += (entry - future.open) / risk * remaining
                exit_ts = future.ts
                outcome = "stop_gap"
                remaining = 0.0
                break
            if future.high >= current_stop:
                # 真实触价：按止损价成交。
                net_r += (entry - current_stop) / risk * remaining
                exit_ts = future.ts
                outcome = "stop"
                remaining = 0.0
                break
            if future.close > highs[index]:
                # 收盘重新站上扫顶高点即失效退出，但该 bar 并未触及止损价，
                # 不能按从未成交的止损价记账：按该 bar 收盘价离场。
                net_r += (entry - future.close) / risk * remaining
                exit_ts = future.ts
                outcome = "invalidation"
                invalidated = True
                remaining = 0.0
                break
            for target_index, (price, fraction) in enumerate(zip(targets, params.partial_fractions), 1):
                if target_index not in hit_targets and remaining + 1e-9 >= fraction and future.low <= price:
                    net_r += (entry - price) / risk * fraction
                    remaining = max(0.0, remaining - fraction)
                    hit_targets.add(target_index)
                    partials.append(f"TP{target_index}")
                    if target_index == 1:
                        current_stop = min(current_stop, entry)
                    elif target_index >= 2:
                        # 跟踪窗口长度取自配置的 trail_bars（含当根）。
                        trail = max(1, int(params.trail_bars))
                        current_stop = min(current_stop, max(highs[max(confirmation_index + 1, future_index - trail + 1):future_index + 1]))
                    if remaining <= 1e-9:
                        exit_ts, outcome = future.ts, "targets"
                        break
            if remaining <= 1e-9:
                break
        if remaining > 0:
            # 窗口末端的强制标记离场：单独记原因，避免混进正常离场统计。
            net_r += (entry - bars[-1].close) / risk * remaining
            exit_ts = bars[-1].ts
            outcome = "window_end"
        # Entry slippage is already reflected in `entry`; charge the second
        # one-way slip on the eventual buy-to-cover exit.
        net_r -= (fee * 2 + slippage + funding) * entry / risk
        trades.append(HighShortTrade(symbol, entry_bar.ts, exit_ts, entry, highs[index], stop, *targets, net_r, regime, zone, confirmation, ",".join(partials), invalidated, outcome))
        cooldown = confirmation_index + max(0, int(params.cooldown_bars))
        index = confirmation_index + 1
    return aggregate_result(params, trades), trades


def acceptance_criteria(oos: dict, actual_reward_risk: float, positive_probability: float,
                        minimum_trades: int = 30) -> dict:
    """研究接受标准（唯一实现，两个报告脚本共用；DESIGN.md 记录同一份定义）。

    此前 `high_short_strategy` 只判 3 项、`hlsr_market_export` 判 5 项，同一策略
    在不同脚本里可能一个 PASS 一个 FAIL。
    """
    return {
        "win_rate_gt_50pct": oos["win_rate"] > 0.50,
        "positive_mean_probability_gt_50pct": positive_probability > 0.50,
        "actual_reward_risk_gte_2": actual_reward_risk >= 2.0,
        "average_net_r_positive": oos["avg_net_r"] > 0,
        "sample_sufficient_30_trades": oos["trades"] >= minimum_trades,
    }


def reward_risk_ratio(net_values: list[float]) -> float:
    """实际平均收益/风险：正 R 均值 ÷ 亏损 R 的绝对值均值。

    与 `aggregate_result` 里 `negative = [-value ...]` 的口径一致；直接用负值相加
    会得到负数比值。
    """
    positive = [value for value in net_values if value > 0]
    negative = [-value for value in net_values if value < 0]
    return (sum(positive) / len(positive)) / (sum(negative) / len(negative)) if positive and negative else 0.0


def parameter_grid(fixed: dict | None = None) -> list[Params]:
    """唯一的参数搜索空间（384 组），回测与市场导出共用。

    `fixed` 用来注入不参与搜索、但必须来自 config/strategy.json 的字段
    （硬过滤阈值、冷却、分批比例）。
    """
    grid = [Params(*values) for values in itertools.product(
        (6, 12), (0.4, 0.6), (1.0, 1.5), (1, 2), (4, 8), (0.25, 0.5), (2,), (False, True),
        ("any", "primary_sweep", "extreme_sweep"))]
    if fixed:
        grid = [replace(item, **fixed) for item in grid]
    return grid


def load_period(symbols: list[str], start_ms: int, end_ms: int, data_dir: Path) -> dict[str, list[Bar]]:
    return {symbol: load_bars(symbol, start_ms, end_ms, data_dir) for symbol in symbols}


def _is_eligible_symbol(symbol: str) -> bool:
    parts = symbol.split("-")
    return len(parts) == 3 and parts[1:] == ["USDT", "SWAP"] and parts[0] not in EXCLUDED_BASES


def _selection_cache_path(data_dir: Path, start_ms: int, end_ms: int, limit: int) -> Path:
    return data_dir / f"high_short_symbols_{start_ms}_{end_ms}_{limit}.json"


def cached_symbols(data_dir: Path, start_ms: int, end_ms: int, limit: int) -> list[str]:
    """Load the previously volume-ranked live universe for this exact run."""
    path = _selection_cache_path(data_dir, start_ms, end_ms, limit)
    try:
        values = json.loads(path.read_text())
    except (OSError, ValueError):
        return []
    if not isinstance(values, list):
        return []
    symbols = [value for value in values if isinstance(value, str) and _is_eligible_symbol(value)]
    return symbols[:limit]


def cached_bar_symbols(data_dir: Path, start_ms: int, end_ms: int, limit: int) -> list[str]:
    """Rank existing bar caches by first-60-day quote volume.

    选币只能用窗口早期的数据。此前用窗口最后 24h 的成交额排名，等于用测试期末尾
    的信息挑标的（选择性前视），会把样本外指标系统性抬高。
    """
    suffix = f"_15m_{start_ms}_{end_ms}.json"
    ranked: list[tuple[float, str]] = []
    for path in sorted(data_dir.glob(f"*_15m_{start_ms}_{end_ms}.json")):
        if not path.name.endswith(suffix):
            continue
        symbol = path.name[:-len(suffix)].replace("_", "-")
        if not _is_eligible_symbol(symbol):
            continue
        try:
            rows = json.loads(path.read_text())
            volume = sum(float(row.get("quote_volume", 0)) for row in rows[:96 * SELECTION_DAYS])
        except (AttributeError, OSError, TypeError, ValueError):
            continue
        ranked.append((-volume, symbol))
    ranked.sort()
    return [symbol for _, symbol in ranked[:limit]]


def save_cached_symbols(data_dir: Path, start_ms: int, end_ms: int, limit: int, symbols: list[str]) -> None:
    path = _selection_cache_path(data_dir, start_ms, end_ms, limit)
    data_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(symbols, separators=(",", ":")))


def select_symbols(data_dir: Path, start_ms: int, end_ms: int, limit: int, cache_dir: Path | None = None) -> list[str]:
    cache_dir = cache_dir or data_dir
    cached = cached_symbols(cache_dir, start_ms, end_ms, limit)
    if len(cached) >= limit:
        return cached
    local = cached_bar_symbols(data_dir, start_ms, end_ms, limit)
    if len(local) >= limit:
        save_cached_symbols(cache_dir, start_ms, end_ms, limit, local)
        return local
    try:
        symbols = candidate_symbols(limit)
        if not symbols:
            raise RuntimeError("OKX returned no eligible symbols")
    except Exception as error:
        symbols = cached_bar_symbols(data_dir, start_ms, end_ms, limit)
        if not symbols:
            raise RuntimeError(f"unable to select symbols from OKX or local bar caches: {error}") from error
    save_cached_symbols(cache_dir, start_ms, end_ms, limit, symbols)
    return symbols


def aggregate_data(data: dict[str, list[Bar]], params: Params, fee: float, slip: float, funding: float) -> tuple[Result, list[HighShortTrade]]:
    trades = sorted(
        (trade for symbol, bars in data.items() for trade in backtest(symbol, bars, params, fee, slip, funding)[1]),
        key=lambda trade: (trade.entry_ts, trade.symbol, trade.exit_ts),
    )
    return aggregate_result(params, trades), trades


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbols", type=int, default=50)
    parser.add_argument("--days", type=int, default=180)
    parser.add_argument("--end", help="固定结束时间，ISO-8601，例如 2026-09-26T19:00:00+00:00")
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--config", type=Path, default=LAB_CONFIG, help="实验室机器参数真源")
    parser.add_argument("--fee-rate", type=float, default=None, help="默认取 config 的 costs.fee_rate_one_way")
    parser.add_argument("--slippage", type=float, default=None, help="默认取 config 的 costs.slippage")
    parser.add_argument("--funding-rate", type=float, default=None, help="默认取 config 的 costs.funding_rate")
    args = parser.parse_args()
    lab = load_lab_config(args.config)
    costs = lab["costs"]
    if args.fee_rate is None: args.fee_rate = float(costs.get("fee_rate_one_way", 0.0006))
    if args.slippage is None: args.slippage = float(costs.get("slippage", 0.0002))
    if args.funding_rate is None: args.funding_rate = float(costs.get("funding_rate", 0.0))
    fixed = fixed_parameters(lab)
    if args.symbols < 1:
        parser.error("--symbols must be at least 1")
    if args.days < 180:
        parser.error("--days must be at least 180 for the three 60/30/30-day walk-forward folds")
    if args.fee_rate < 0 or args.slippage < 0:
        parser.error("--fee-rate and --slippage must be non-negative")
    now = datetime.now(UTC)
    end = parse_utc(args.end) if args.end else now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    start = end - timedelta(days=args.days)
    start_ms, end_ms = int(start.timestamp() * 1000), int(end.timestamp() * 1000)
    data_dir, output_dir = args.data_dir, args.output_dir
    symbols = select_symbols(data_dir, start_ms, end_ms, args.symbols, output_dir)
    data = load_period(symbols, start_ms, end_ms, data_dir)
    params_list = parameter_grid(fixed)
    folds = []
    for fold, offset in enumerate((0, 30, 60), 1):
        train_start, train_end = start + timedelta(days=offset), start + timedelta(days=offset + 60)
        val_end, test_end = train_end + timedelta(days=30), train_end + timedelta(days=60)
        cut = lambda values, lo, hi: [bar for bar in values if lo <= bar.ts < hi]
        sets = [{symbol: cut(bars, int(lo.timestamp() * 1000), int(hi.timestamp() * 1000)) for symbol, bars in data.items()} for lo, hi in ((train_start, train_end), (train_end, val_end), (val_end, test_end))]
        scored = [(aggregate_data(sets[0], params, args.fee_rate, args.slippage, args.funding_rate)[0], params) for params in params_list]
        eligible = [item for item in scored if item[0].trades >= 5]
        if eligible:
            scored = eligible
        scored.sort(key=lambda item: (-item[0].avg_net_r, -item[0].win_rate, item[0].max_drawdown_r))
        train_result, chosen = scored[0]
        validation, _ = aggregate_data(sets[1], chosen, args.fee_rate, args.slippage, args.funding_rate)
        test, test_trades = aggregate_data(sets[2], chosen, args.fee_rate, args.slippage, args.funding_rate)
        folds.append({"fold": fold, "params": chosen.as_dict(), "train": asdict(train_result), "validation": asdict(validation), "test": asdict(test), "test_trades": [asdict(trade) for trade in test_trades]})
        print(f"fold={fold} train={train_result.trades}/{train_result.win_rate:.1%} validation={validation.trades}/{validation.win_rate:.1%} test={test.trades}/{test.win_rate:.1%}")
    all_trades = [trade for fold in folds for trade in fold["test_trades"]]
    oos_values = [trade["net_r"] for trade in all_trades]
    oos_wins = sum(value > 0 for value in oos_values)
    oos_result = {"trades": len(oos_values), "wins": oos_wins, "win_rate": oos_wins / len(oos_values) if oos_values else 0, "total_r": sum(oos_values), "avg_net_r": sum(oos_values) / len(oos_values) if oos_values else 0}
    oos_probability = bootstrap_positive_probability([
        Trade(trade["symbol"], trade["entry_ts"], trade["exit_ts"], trade["entry"], trade["stop"],
              trade["tp3"], trade["net_r"], "win" if trade["net_r"] > 0 else "loss", trade["partials"], 0.0)
        for trade in all_trades])
    criteria = acceptance_criteria(oos_result, reward_risk_ratio(oos_values),
                                   oos_probability["positive_mean_probability"])
    passed = all(criteria.values())
    report = {"strategy": "HLSR", "strategy_name_en": "High-Level Liquidity Sweep Reversal", "strategy_name_zh": "高位流动性扫顶反转策略", "core_formula": "high_value_zone + liquidity_sweep + rejection + structure_break + right_side_confirmation = short", "window": {"start": start.isoformat(), "end": end.isoformat()}, "market": "OKX Perpetual", "entry_timeframe": "15m", "htf": ["4H"], "leverage": float(lab["position"].get("leverage", 2.0)), "hard_filters": dict(lab["hard"]), "symbols": symbols, "parameter_count": len(params_list), "folds": folds, "sample_out_of_sample": oos_result, "acceptance": criteria, "actual_reward_risk": reward_risk_ratio(oos_values), "passed": passed, "survivorship_bias": "current live USDT swaps are used because historical listing metadata is unavailable", "core_entry_module": ["value_zone", "liquidity_sweep", "rejection", "structure_confirmation"], "risk_management_module": ["position_size", "leverage", "stop_distance", "volatility", "funding_rate", "open_interest", "liquidation_data"], "assumptions": {"same_bar_priority": "stop_first", "fee_rate_one_way": args.fee_rate, "slippage_one_way": args.slippage, "funding_rate_per_trade": args.funding_rate, "reentry": "cooldown after exit; no averaging down"}}
    output_dir.mkdir(parents=True, exist_ok=True)
    prefix = f"high_short_{args.days}d"
    (output_dir / f"{prefix}_report.json").write_text(json.dumps(report, indent=2))
    with (output_dir / f"{prefix}_trades.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(HighShortTrade.__dataclass_fields__), lineterminator="\n")
        writer.writeheader()
        writer.writerows(all_trades)
    print(f"out_of_sample={oos_result} status={'PASS' if passed else 'FAIL'}")


if __name__ == "__main__":
    main()
