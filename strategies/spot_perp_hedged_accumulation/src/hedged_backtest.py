#!/usr/bin/env python3
"""Causal price-layer research backtest for SPHA.

The repository contains perpetual OHLCV rather than matched spot and swap
executions.  This module therefore uses one confirmed price stream as a proxy
for both legs.  Signals and rebalances observed at a confirmed 1h close are
filled at the next complete 1h bar open.  Funding, basis, mark price,
contract rounding and order-book execution are deliberately not inferred.
"""
from __future__ import annotations

import argparse
import copy
import csv
import gzip
import json
import math
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable, Sequence


FIVE_MINUTES = 5 * 60 * 1000
HOUR = 60 * 60 * 1000


@dataclass(frozen=True)
class Bar:
    ts: int
    open: float
    high: float
    low: float
    close: float
    quote_volume: float = 0.0


@dataclass(frozen=True)
class Signal:
    symbol: str
    index: int
    timestamp: int
    kind: str
    rsi: float | None
    ema_fast: float
    ema_slow: float


@dataclass(frozen=True)
class Rebalance:
    index: int
    timestamp: int
    action: str
    price: float
    spot_qty: float
    short_qty: float
    hedge_ratio: float
    notional_deviation_pct: float
    equity: float
    reserve_cash: float = 0.0
    position_equity_deviation_pct: float = 0.0
    spot_position_equity: float = 0.0
    short_position_equity: float = 0.0
    collateral: float = 0.0
    short_cross_buffer: float = 0.0
    delta_base: float = 0.0
    tactical_spot_qty: float = 0.0


@dataclass(frozen=True)
class Trade:
    symbol: str
    signal_index: int
    entry_index: int
    exit_index: int
    signal_kind: str
    entry_price: float
    exit_price: float
    entry_equity: float
    exit_equity: float
    return_pct: float
    rebalances: int
    max_notional_deviation_pct: float
    fees_paid: float
    funding_paid: float
    exit_reason: str
    reserve_start: float = 0.0
    reserve_end: float = 0.0
    max_spot_qty: float = 0.0
    min_reserve_cash: float = 0.0
    max_spot_gain_pct: float = 0.0
    reserve_after_exit: float = 0.0
    max_position_equity_deviation_pct: float = 0.0
    max_delta_base_pct: float = 0.0


@dataclass
class Portfolio:
    cash: float
    spot_qty: float
    short_qty: float
    short_average: float
    collateral: float
    fee_paid: float
    reserve_cash: float = 0.0
    initial_capital: float = 0.0
    spot_allocated_capital: float = 0.0
    spot_average: float = 0.0
    short_allocated_capital: float = 0.0
    spot_fee_paid: float = 0.0
    short_fee_paid: float = 0.0
    funding_paid: float = 0.0
    core_spot_qty: float = 0.0
    tactical_spot_qty: float = 0.0
    core_spot_allocated_capital: float = 0.0
    tactical_spot_allocated_capital: float = 0.0
    tactical_spot_average: float = 0.0
    core_short_qty: float = 0.0

    def equity(self, price: float) -> float:
        short_mark = (self.short_average - price) * self.short_qty
        return self.cash + self.reserve_cash + self.spot_qty * price + self.collateral + short_mark

    @property
    def hedge_ratio(self) -> float:
        return self.short_qty / self.spot_qty if self.spot_qty > 0 else math.inf

    @property
    def delta_base(self) -> float:
        """Net base-asset exposure after the fixed core hedge."""
        return self.spot_qty - self.short_qty

    @property
    def core_hedge_ratio(self) -> float:
        return self.core_short_qty / self.core_spot_qty if self.core_spot_qty > 0 else math.inf

    def notional_deviation(self, price: float) -> float:
        spot = self.spot_qty * price
        short = self.short_qty * price
        mean = (spot + short) / 2.0
        return abs(spot - short) / mean if mean > 0 else 0.0

    def spot_position_equity(self, price: float) -> float:
        """Dollar equity assigned to the remaining spot position."""
        if self.core_spot_qty > 0 or self.tactical_spot_qty > 0:
            core_capital = self.core_spot_allocated_capital
            tactical_capital = self.tactical_spot_allocated_capital
            core_average = core_capital / self.core_spot_qty if self.core_spot_qty > 0 else 0.0
            tactical_average = self.tactical_spot_average if self.tactical_spot_qty > 0 else 0.0
            return (
                core_capital + (price - core_average) * self.core_spot_qty
                + tactical_capital + (price - tactical_average) * self.tactical_spot_qty
                - self.spot_fee_paid
            )
        if self.spot_allocated_capital > 0:
            average = self.spot_average if self.spot_average > 0 else price
            unrealized = (price - average) * self.spot_qty
            return self.spot_allocated_capital + unrealized - self.spot_fee_paid
        return self.spot_qty * price - self.spot_fee_paid

    def short_position_equity(self, price: float) -> float:
        """Dollar equity assigned to the remaining cross-margin short.

        ``short_allocated_capital`` includes the position's cross-margin
        buffer.  ``collateral`` remains the exchange-required margin and is
        reported separately for liquidation analysis.
        """
        capital = self.short_allocated_capital if self.short_allocated_capital > 0 else self.collateral
        unrealized = (self.short_average - price) * self.short_qty
        return capital + unrealized - self.short_fee_paid - self.funding_paid

    def short_cross_buffer(self) -> float:
        """Allocated short equity above exchange-required collateral."""
        return max(0.0, self.short_allocated_capital - self.collateral)

    def position_equity_gap(self, price: float) -> float:
        return self.spot_position_equity(price) - self.short_position_equity(price)

    def position_equity_deviation(self, price: float) -> float:
        spot = self.spot_position_equity(price)
        short = self.short_position_equity(price)
        mean = (spot + short) / 2.0
        return abs(spot - short) / mean if mean > 0 else math.inf


DEFAULT_CONFIG: dict[str, object] = {
    "signal": {
        "ema_fast": 20,
        "ema_slow": 60,
        "ema_slope_bars": 12,
        "rsi_period": 14,
        "pivot_left": 2,
        "pivot_right": 2,
        "divergence_min_separation_bars": 5,
        "divergence_max_separation_bars": 40,
        "divergence_price_lower_low_atr": 0.5,
        "divergence_rsi_higher_low_points": 5.0,
        "pullback_atr": 0.35,
        "atr_period": 14,
        "cooldown_bars": 12,
    },
    "rebalance": {
        "mode": "fixed_short_spot_grid",
        "threshold_pct": 0.04,
        "buy_reserve_fraction": 0.25,
        "sell_tactical_fraction": 1.0,
        "max_tactical_spot_fraction_of_core": 0.10,
        # Kept for legacy helper fixtures; the active fixed-core mode ignores it.
        "buy_spot_fraction_of_short_release": 0.50,
        "position_equity_deviation_trigger_pct": 0.20,
        "max_position_equity_deviation_pct": 0.20,
        "soft_rebalance_equity_deviation_pct": 0.20,
        "equity_metric": "allocated_position_equity",
        "notional_deviation_report_only": True,
        "max_notional_deviation_pct": 0.20,
        "max_actions_per_campaign": 12,
        "soft_rebalance_deviation_pct": 0.10,
    },
    "portfolio": {
        "active_capital_fraction": 0.80,
        "initial_spot_notional_fraction": 0.50,
        "initial_perpetual_notional_fraction": 0.50,
        "margin_mode": "cross",
        "leverage": 2.0,
        "research_max_hold_bars": 720,
    },
    "reserve": {
        "initial_cash_fraction": 0.20,
        "minimum_cash_fraction_of_initial_capital": 0.10,
        "exit_on_floor_with_unhedged_spot": True,
    },
    "costs": {"fee_rate_one_way": 0.0006, "slippage_one_way": 0.0002},
}


def merge_config(value: dict[str, object] | None) -> dict[str, object]:
    result = json.loads(json.dumps(DEFAULT_CONFIG))
    for section, fields in (value or {}).items():
        if isinstance(fields, dict) and isinstance(result.get(section), dict):
            result[section].update(fields)  # type: ignore[union-attr]
        else:
            result[section] = fields
    return result


def _number(raw: object, default: float = 0.0) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return value if math.isfinite(value) else default


def load_jsonl(path: Path) -> list[Bar]:
    """Load confirmed, valid candles and de-duplicate timestamps."""
    opener = gzip.open if path.suffix == ".gz" else open
    by_ts: dict[int, Bar] = {}
    with opener(path, "rt", encoding="utf-8") as stream:  # type: ignore[arg-type]
        for line in stream:
            try:
                raw = json.loads(line)
                if raw.get("confirmed", True) is False:
                    continue
                ts = int(raw.get("timestamp_ms", 0))
                values = [_number(raw[key]) for key in ("open", "high", "low", "close")]
                if ts <= 0 or min(values) <= 0:
                    continue
                op, high, low, close = values
                if high < max(op, close) or low > min(op, close) or high < low:
                    continue
                by_ts[ts] = Bar(ts, op, high, low, close, _number(raw.get("quote_volume")))
            except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue
    return [by_ts[ts] for ts in sorted(by_ts)]


def aggregate_to_hour(bars: Sequence[Bar], source_minutes: int = 5) -> list[Bar]:
    """Aggregate only complete, contiguous source bars into 1h bars."""
    if source_minutes <= 0 or 60 % source_minutes:
        raise ValueError("source_minutes must divide 60")
    expected = 60 // source_minutes
    step = source_minutes * 60 * 1000
    grouped: dict[int, list[Bar]] = {}
    for bar in sorted(bars, key=lambda item: item.ts):
        bucket = (bar.ts // HOUR) * HOUR
        grouped.setdefault(bucket, []).append(bar)
    output: list[Bar] = []
    for bucket in sorted(grouped):
        group = sorted(grouped[bucket], key=lambda item: item.ts)
        if len(group) != expected:
            continue
        if any(group[i].ts != bucket + i * step for i in range(expected)):
            continue
        output.append(Bar(bucket, group[0].open, max(item.high for item in group),
                          min(item.low for item in group), group[-1].close,
                          sum(item.quote_volume for item in group)))
    return output


def ema(values: Sequence[float], period: int) -> list[float]:
    if period <= 0:
        raise ValueError("EMA period must be positive")
    alpha = 2.0 / (period + 1.0)
    output: list[float] = []
    for value in values:
        output.append(value if not output else output[-1] + alpha * (value - output[-1]))
    return output


def atr(bars: Sequence[Bar], period: int) -> list[float]:
    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        prior = bars[index - 1].close if index else bar.close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - prior), abs(bar.low - prior)))
    return ema(true_ranges, period)


def rsi(values: Sequence[float], period: int) -> list[float | None]:
    if period <= 0:
        raise ValueError("RSI period must be positive")
    result: list[float | None] = [None] * len(values)
    if len(values) <= period:
        return result
    gains = [max(values[i] - values[i - 1], 0.0) for i in range(1, len(values))]
    losses = [max(values[i - 1] - values[i], 0.0) for i in range(1, len(values))]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value() -> float:
        if avg_loss == 0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + avg_gain / avg_loss)

    result[period] = value()
    for index in range(period + 1, len(values)):
        avg_gain = (avg_gain * (period - 1) + gains[index - 1]) / period
        avg_loss = (avg_loss * (period - 1) + losses[index - 1]) / period
        result[index] = value()
    return result


def _pivot_lows(bars: Sequence[Bar], left: int, right: int) -> list[tuple[int, int]]:
    """Return (pivot index, confirmation index), without future leakage."""
    found: list[tuple[int, int]] = []
    for confirmation in range(left + right, len(bars)):
        pivot = confirmation - right
        window = [bars[index].low for index in range(pivot - left, pivot + right + 1)]
        if bars[pivot].low == min(window) and window.count(bars[pivot].low) == 1:
            found.append((pivot, confirmation))
    return found


def find_signals(symbol: str, bars: Sequence[Bar], config: dict[str, object] | None = None) -> list[Signal]:
    """Find causal divergence and EMA pullback activations on 1h bars."""
    cfg = merge_config(config)
    signal_cfg = cfg["signal"]  # type: ignore[assignment]
    closes = [bar.close for bar in bars]
    fast = ema(closes, int(signal_cfg["ema_fast"]))
    slow = ema(closes, int(signal_cfg["ema_slow"]))
    atr_values = atr(bars, int(signal_cfg["atr_period"]))
    momentum = rsi(closes, int(signal_cfg["rsi_period"]))
    slope_bars = int(signal_cfg["ema_slope_bars"])
    left, right = int(signal_cfg["pivot_left"]), int(signal_cfg["pivot_right"])
    min_sep = int(signal_cfg["divergence_min_separation_bars"])
    max_sep = int(signal_cfg["divergence_max_separation_bars"])
    cooldown = int(signal_cfg["cooldown_bars"])
    signals: dict[int, Signal] = {}
    pivots: list[tuple[int, int, float]] = []

    def contiguous(start: int, end: int) -> bool:
        return start >= 0 and end < len(bars) and all(
            bars[index].ts - bars[index - 1].ts == HOUR for index in range(start + 1, end + 1)
        )

    def downtrend(index: int) -> bool:
        return index >= slope_bars and contiguous(index - slope_bars, index) and fast[index] < slow[index] and slow[index] < slow[index - slope_bars] and closes[index] <= slow[index]

    def uptrend(index: int) -> bool:
        return index >= slope_bars and contiguous(index - slope_bars, index) and fast[index] > slow[index] and slow[index] > slow[index - slope_bars] and closes[index] > fast[index]

    # The later pivot is known only at its right-side confirmation close.  A
    # signal is allowed on that same close only when price has recovered EMA20.
    for pivot, confirmation in _pivot_lows(bars, left, right):
        if not contiguous(pivot - left, confirmation):
            continue
        pivot_rsi = momentum[pivot]
        if pivot_rsi is None:
            continue
        pivots.append((pivot, confirmation, pivot_rsi))
        if len(pivots) < 2 or not downtrend(confirmation):
            continue
        previous_pivot, _, previous_rsi = pivots[-2]
        separation = pivot - previous_pivot
        price_lower = bars[pivot].low <= bars[previous_pivot].low - float(signal_cfg["divergence_price_lower_low_atr"]) * atr_values[pivot]
        rsi_higher = pivot_rsi >= previous_rsi + float(signal_cfg["divergence_rsi_higher_low_points"])
        if min_sep <= separation <= max_sep and price_lower and rsi_higher and closes[confirmation] > fast[confirmation]:
            signals[confirmation] = Signal(symbol, confirmation, bars[confirmation].ts, "bullish_divergence", pivot_rsi, fast[confirmation], slow[confirmation])

    for index, bar in enumerate(bars):
        if index == 0 or bars[index].ts - bars[index - 1].ts != HOUR or not uptrend(index) or atr_values[index] <= 0:
            continue
        touched = bar.low <= fast[index] + float(signal_cfg["pullback_atr"]) * atr_values[index]
        recovered = bar.close > fast[index] and bar.close > bar.open and bars[index - 1].close > fast[index - 1]
        if touched and recovered:
            signals.setdefault(index, Signal(symbol, index, bar.ts, "ema_pullback", momentum[index], fast[index], slow[index]))

    emitted: list[Signal] = []
    for index in sorted(signals):
        if not emitted or index - emitted[-1].index >= cooldown:
            emitted.append(signals[index])
    return emitted


def _slippage(price: float, side: str, rate: float) -> float:
    return price * (1.0 + rate if side == "buy" else 1.0 - rate)


def _fee(notional: float, rate: float) -> float:
    return abs(notional) * rate


def _buy_spot(portfolio: Portfolio, qty: float, price: float, costs: dict[str, object]) -> float:
    fill = _slippage(price, "buy", float(costs["slippage_one_way"]))
    notional = qty * fill
    fee = _fee(notional, float(costs["fee_rate_one_way"]))
    portfolio.cash -= notional + fee
    total = portfolio.spot_qty + qty
    portfolio.spot_average = ((portfolio.spot_average * portfolio.spot_qty) + fill * qty) / total
    portfolio.spot_allocated_capital += notional
    portfolio.spot_qty += qty
    portfolio.fee_paid += fee
    portfolio.spot_fee_paid += fee
    return qty


def _buy_spot_from_reserve(portfolio: Portfolio, budget: float, price: float,
                           costs: dict[str, object]) -> float:
    """Buy spot using only a stated reserve budget, including its fee."""
    budget = min(max(0.0, budget), max(0.0, portfolio.reserve_cash))
    if budget <= 0:
        return 0.0
    fill = _slippage(price, "buy", float(costs["slippage_one_way"]))
    fee_rate = float(costs["fee_rate_one_way"])
    qty = budget / (fill * (1.0 + fee_rate))
    notional = qty * fill
    fee = _fee(notional, fee_rate)
    portfolio.reserve_cash -= notional + fee
    total = portfolio.spot_qty + qty
    portfolio.spot_average = ((portfolio.spot_average * portfolio.spot_qty) + fill * qty) / total
    portfolio.spot_allocated_capital += notional
    portfolio.spot_qty += qty
    portfolio.fee_paid += fee
    portfolio.spot_fee_paid += fee
    return qty


def _sell_spot(portfolio: Portfolio, qty: float, price: float, costs: dict[str, object]) -> float:
    qty = min(max(0.0, qty), portfolio.spot_qty)
    prior_qty = portfolio.spot_qty
    fill = _slippage(price, "sell", float(costs["slippage_one_way"]))
    notional = qty * fill
    fee = _fee(notional, float(costs["fee_rate_one_way"]))
    portfolio.cash += notional - fee
    portfolio.spot_qty -= qty
    if portfolio.spot_allocated_capital > 0 and prior_qty > 0:
        portfolio.spot_allocated_capital = max(
            0.0, portfolio.spot_allocated_capital * (1.0 - qty / prior_qty)
        )
    portfolio.fee_paid += fee
    portfolio.spot_fee_paid += fee
    if portfolio.spot_qty <= 1e-12:
        portfolio.spot_qty = 0.0
        portfolio.core_spot_qty = 0.0
        portfolio.tactical_spot_qty = 0.0
        portfolio.core_spot_allocated_capital = 0.0
        portfolio.tactical_spot_allocated_capital = 0.0
        portfolio.tactical_spot_average = 0.0
    return qty


def _buy_tactical_spot_from_reserve(portfolio: Portfolio, budget: float, price: float,
                                    cfg: dict[str, object],
                                    costs: dict[str, object]) -> float:
    """Buy temporary spot inventory without touching the fixed core hedge."""
    reserve_floor = _reserve_floor(portfolio, cfg)
    budget = min(
        max(0.0, budget),
        max(0.0, portfolio.reserve_cash - reserve_floor),
    )
    if budget <= 0:
        return 0.0
    fill = _slippage(price, "buy", float(costs["slippage_one_way"]))
    fee_rate = float(costs["fee_rate_one_way"])
    qty = budget / (fill * (1.0 + fee_rate))
    rebalance_cfg = cfg["rebalance"]  # type: ignore[assignment]
    max_fraction = max(0.0, float(rebalance_cfg.get("max_tactical_spot_fraction_of_core", 0.0)))
    capacity = max(0.0, portfolio.core_spot_qty * max_fraction - portfolio.tactical_spot_qty)
    qty = min(qty, capacity)
    if qty <= 0:
        return 0.0
    notional = qty * fill
    fee = _fee(notional, fee_rate)
    portfolio.reserve_cash -= notional + fee
    portfolio.spot_qty += qty
    portfolio.tactical_spot_qty += qty
    portfolio.spot_allocated_capital += notional
    portfolio.tactical_spot_allocated_capital += notional
    total_tactical = portfolio.tactical_spot_qty
    prior_tactical = total_tactical - qty
    portfolio.tactical_spot_average = (
        (portfolio.tactical_spot_average * prior_tactical + fill * qty) / total_tactical
        if total_tactical > 0 else 0.0
    )
    portfolio.spot_average = (
        (portfolio.spot_average * (portfolio.spot_qty - qty) + fill * qty) / portfolio.spot_qty
        if portfolio.spot_qty > 0 else 0.0
    )
    portfolio.fee_paid += fee
    portfolio.spot_fee_paid += fee
    return qty


def _sell_tactical_spot_to_reserve(portfolio: Portfolio, qty: float, price: float,
                                   costs: dict[str, object]) -> float:
    """Sell only tactical spot inventory and return proceeds to reserve."""
    qty = min(max(0.0, qty), portfolio.tactical_spot_qty)
    if qty <= 0:
        return 0.0
    fill = _slippage(price, "sell", float(costs["slippage_one_way"]))
    notional = qty * fill
    fee = _fee(notional, float(costs["fee_rate_one_way"]))
    cost_basis = portfolio.tactical_spot_average * qty
    portfolio.reserve_cash += notional - fee
    portfolio.spot_qty -= qty
    portfolio.tactical_spot_qty -= qty
    portfolio.spot_allocated_capital = max(0.0, portfolio.spot_allocated_capital - cost_basis)
    portfolio.tactical_spot_allocated_capital = max(
        0.0, portfolio.tactical_spot_allocated_capital - cost_basis
    )
    if portfolio.tactical_spot_qty <= 1e-12:
        portfolio.tactical_spot_qty = 0.0
        portfolio.tactical_spot_allocated_capital = 0.0
        portfolio.tactical_spot_average = 0.0
    total_allocated = portfolio.spot_allocated_capital
    portfolio.spot_average = total_allocated / portfolio.spot_qty if portfolio.spot_qty > 0 else 0.0
    portfolio.fee_paid += fee
    portfolio.spot_fee_paid += fee
    return qty


def _close_short(portfolio: Portfolio, qty: float, price: float,
                 costs: dict[str, object], destination: str = "cash") -> float:
    qty = min(max(0.0, qty), portfolio.short_qty)
    if qty <= 0:
        return 0.0
    fill = _slippage(price, "buy", float(costs["slippage_one_way"]))
    fee = _fee(qty * fill, float(costs["fee_rate_one_way"]))
    release = portfolio.collateral * qty / portfolio.short_qty
    allocated_release = (
        (portfolio.short_allocated_capital if portfolio.short_allocated_capital > 0 else portfolio.collateral)
        * qty / portfolio.short_qty
    )
    cross_buffer_release = max(0.0, allocated_release - release)
    realized = (portfolio.short_average - fill) * qty
    net_release = allocated_release + realized - fee
    if destination == "reserve":
        # Move the position's cross buffer from free active cash into the
        # strategy reserve along with released required margin and PnL.
        portfolio.cash -= cross_buffer_release
        if net_release >= 0:
            portfolio.reserve_cash += net_release
        else:
            # A losing close consumes shared free cash; the reserve itself is
            # never allowed to become negative just because a short was closed.
            portfolio.cash += net_release
    else:
        portfolio.cash += net_release
    portfolio.collateral -= release
    if portfolio.short_allocated_capital > 0:
        portfolio.short_allocated_capital = max(
            0.0, portfolio.short_allocated_capital - allocated_release
        )
    portfolio.short_qty -= qty
    portfolio.fee_paid += fee
    portfolio.short_fee_paid += fee
    if portfolio.short_qty <= 1e-12:
        portfolio.short_qty = 0.0
        portfolio.short_average = 0.0
        portfolio.core_short_qty = 0.0
    return qty


def _add_short(portfolio: Portfolio, qty: float, price: float, leverage: float, costs: dict[str, object]) -> float:
    if qty <= 0:
        return 0.0
    fill = _slippage(price, "sell", float(costs["slippage_one_way"]))
    notional = qty * fill
    fee = _fee(notional, float(costs["fee_rate_one_way"]))
    margin = notional / max(leverage, 1e-9)
    portfolio.cash -= margin + fee
    portfolio.collateral += margin
    portfolio.short_allocated_capital += notional
    portfolio.fee_paid += fee
    portfolio.short_fee_paid += fee
    total = portfolio.short_qty + qty
    portfolio.short_average = ((portfolio.short_average * portfolio.short_qty) + fill * qty) / total
    portfolio.short_qty = total
    return qty


def _add_short_from_reserve(portfolio: Portfolio, qty: float, price: float,
                            leverage: float, costs: dict[str, object],
                            reserve_floor: float) -> float:
    """Add cross-margin short while preserving the strategy reserve floor."""
    if qty <= 0:
        return 0.0
    fill = _slippage(price, "sell", float(costs["slippage_one_way"]))
    fee_rate = float(costs["fee_rate_one_way"])
    margin_ratio = 1.0 / max(leverage, 1e-9)
    use_allocated_capital = portfolio.short_allocated_capital > 0
    per_unit_capital = fill if use_allocated_capital else fill * margin_ratio
    per_unit_spend = per_unit_capital + fill * fee_rate
    available = max(0.0, portfolio.reserve_cash - reserve_floor)
    qty = min(qty, available / per_unit_spend if per_unit_spend > 0 else 0.0)
    if qty <= 0:
        return 0.0
    notional = qty * fill
    fee = _fee(notional, fee_rate)
    margin = notional / max(leverage, 1e-9)
    allocated_capital = notional if use_allocated_capital else margin
    portfolio.reserve_cash -= allocated_capital + fee
    if use_allocated_capital:
        # The portion above required margin becomes additional cross free
        # collateral.  This transfer keeps reserve and account equity sane.
        portfolio.cash += allocated_capital - margin
    portfolio.collateral += margin
    portfolio.short_allocated_capital += allocated_capital
    portfolio.fee_paid += fee
    portfolio.short_fee_paid += fee
    total = portfolio.short_qty + qty
    portfolio.short_average = ((portfolio.short_average * portfolio.short_qty) + fill * qty) / total
    portfolio.short_qty = total
    return qty


def _reserve_floor(portfolio: Portfolio, cfg: dict[str, object]) -> float:
    reserve_cfg = cfg["reserve"]  # type: ignore[assignment]
    return max(0.0, portfolio.initial_capital * float(
        reserve_cfg["minimum_cash_fraction_of_initial_capital"]
    ))


def _reserve_floor_exit_required(portfolio: Portfolio, price: float,
                                 cfg: dict[str, object]) -> bool:
    """Stop the campaign if reserve is exhausted while spot remains ahead."""
    reserve_cfg = cfg["reserve"]  # type: ignore[assignment]
    enabled = bool(reserve_cfg.get("exit_on_floor_with_unhedged_spot", True))
    if not enabled or portfolio.reserve_cash > _reserve_floor(portfolio, cfg) + 1e-9:
        return False
    if cfg.get("rebalance", {}).get("mode") == "fixed_short_spot_grid":  # type: ignore[union-attr]
        return portfolio.tactical_spot_qty > 1e-12
    return portfolio.position_equity_gap(price) > 1e-9


def _spot_qty_limit_for_deviation(short_qty: float, max_deviation: float) -> float:
    """Maximum spot quantity when spot is the larger leg under the cap."""
    if max_deviation <= 0:
        return short_qty
    half = max_deviation / 2.0
    return short_qty * (1.0 + half) / max(1.0 - half, 1e-9)


def _apply_reduce_short_and_buy_spot(portfolio: Portfolio, close_qty: float,
                                     price: float, cfg: dict[str, object],
                                     costs: dict[str, object]) -> tuple[float, float]:
    """Apply one down-rebalance trial or final action."""
    rebalance_cfg = cfg["rebalance"]  # type: ignore[assignment]
    alpha = min(1.0, max(0.0, float(rebalance_cfg["buy_spot_fraction_of_short_release"])))
    reserve_before = portfolio.reserve_cash
    closed = _close_short(portfolio, close_qty, price, costs, destination="reserve")
    released = max(0.0, portfolio.reserve_cash - reserve_before)
    budget = min(
        alpha * released,
        max(0.0, portfolio.reserve_cash - _reserve_floor(portfolio, cfg)),
    )
    bought = _buy_spot_from_reserve(portfolio, budget, price, costs)
    return closed, bought


def _equity_rebalance_qty(portfolio: Portfolio, price: float, cfg: dict[str, object],
                          costs: dict[str, object], direction: str) -> float:
    """Find a quantity that moves the two allocated leg equities together.

    Fees, slippage, realized PnL and the reserve floor make a closed-form
    quantity unreliable.  A bounded trial search keeps the action causal and
    lets the same ledger code determine the final result.
    """
    if direction == "reduce_short":
        upper = portfolio.short_qty

        def trial(quantity: float) -> float:
            candidate = copy.deepcopy(portfolio)
            _apply_reduce_short_and_buy_spot(candidate, quantity, price, cfg, costs)
            return candidate.position_equity_gap(price)

        initial_gap = portfolio.position_equity_gap(price)
        if initial_gap >= 0 or upper <= 1e-12:
            return 0.0
    else:
        portfolio_cfg = cfg["portfolio"]  # type: ignore[assignment]
        leverage = float(portfolio_cfg["leverage"])
        fill = _slippage(price, "sell", float(costs["slippage_one_way"]))
        fee_rate = float(costs["fee_rate_one_way"])
        capital_per_unit = fill if portfolio.short_allocated_capital > 0 else fill / max(leverage, 1e-9)
        available = max(0.0, portfolio.reserve_cash - _reserve_floor(portfolio, cfg))
        upper = available / (capital_per_unit + fill * fee_rate) if capital_per_unit > 0 else 0.0

        def trial(quantity: float) -> float:
            candidate = copy.deepcopy(portfolio)
            portfolio_cfg = cfg["portfolio"]  # type: ignore[assignment]
            _add_short_from_reserve(
                candidate, quantity, price, float(portfolio_cfg["leverage"]),
                costs, _reserve_floor(candidate, cfg)
            )
            return candidate.position_equity_gap(price)

        initial_gap = portfolio.position_equity_gap(price)
        if initial_gap <= 0 or upper <= 1e-12:
            return 0.0

    low, high = 0.0, upper
    # The quantity may be reserve-limited.  Search for the first point at
    # which the gap changes sign, then choose the closer of both brackets.
    for _ in range(48):
        middle = (low + high) / 2.0
        gap = trial(middle)
        if (gap > 0) == (initial_gap > 0):
            low = middle
        else:
            high = middle
    candidates = [low, high]
    return min(candidates, key=lambda quantity: abs(trial(quantity)))


def _reduce_short_and_buy_spot(portfolio: Portfolio, price: float,
                               cfg: dict[str, object],
                               costs: dict[str, object]) -> tuple[float, float]:
    """Reduce short equity and buy part of the released capital as spot.

    Portfolios created by ``enter_portfolio`` use allocated-capital equity.
    The quantity fallback keeps this helper useful for small legacy fixtures
    that do not carry the new ledger fields.
    """
    rebalance_cfg = cfg["rebalance"]  # type: ignore[assignment]
    alpha = min(1.0, max(0.0, float(rebalance_cfg["buy_spot_fraction_of_short_release"])))
    if portfolio.short_qty <= 0:
        return 0.0, 0.0
    if portfolio.short_allocated_capital > 0 and portfolio.spot_allocated_capital > 0:
        if portfolio.position_equity_gap(price) >= 0:
            return 0.0, 0.0
        quantity = _equity_rebalance_qty(portfolio, price, cfg, costs, "reduce_short")
        if quantity <= 1e-12:
            return 0.0, 0.0
        return _apply_reduce_short_and_buy_spot(portfolio, quantity, price, cfg, costs)

    # Legacy quantity-gap fallback for hand-built fixtures without equity
    # allocation fields.
    gap = max(0.0, portfolio.short_qty - portfolio.spot_qty)
    if gap <= 1e-12:
        return 0.0, 0.0
    portfolio_cfg = cfg["portfolio"]  # type: ignore[assignment]
    max_deviation = float(rebalance_cfg.get("max_notional_deviation_pct", 0.20))
    margin_ratio = 1.0 / max(float(portfolio_cfg["leverage"]), 1e-9)
    # alpha is the cash fraction of the released cross-margin credit. With
    # leverage L, one unit of closed notional releases roughly P/L, so solve
    # q + alpha * q/L = gap before applying actual PnL and fees.
    close_qty = min(portfolio.short_qty, gap / (1.0 + alpha * margin_ratio))
    # Closing short can itself make spot the larger leg. Reserve room for the
    # subsequent spot buy so that the combined action cannot cross the cap.
    close_qty = min(close_qty, max(0.0, portfolio.short_qty - portfolio.spot_qty * (
        1.0 - max_deviation / 2.0
    ) / max(1.0 + max_deviation / 2.0, 1e-9)))
    reserve_before = portfolio.reserve_cash
    closed = _close_short(portfolio, close_qty, price, costs, destination="reserve")
    released = max(0.0, portfolio.reserve_cash - reserve_before)
    remaining_gap = max(0.0, portfolio.short_qty - portfolio.spot_qty)
    fill = _slippage(price, "buy", float(costs["slippage_one_way"]))
    fee_rate = float(costs["fee_rate_one_way"])
    target_budget = remaining_gap * fill * (1.0 + fee_rate)
    cap_buy_qty = max(0.0, _spot_qty_limit_for_deviation(
        portfolio.short_qty, max_deviation
    ) - portfolio.spot_qty)
    cap_budget = cap_buy_qty * fill * (1.0 + fee_rate)
    budget = min(alpha * released, target_budget,
                 cap_budget,
                 max(0.0, portfolio.reserve_cash - _reserve_floor(portfolio, cfg)))
    bought = _buy_spot_from_reserve(portfolio, budget, price, costs)
    return closed, bought


def _add_short_from_reserve_to_gap(portfolio: Portfolio, price: float,
                                   cfg: dict[str, object],
                                   costs: dict[str, object]) -> float:
    """Use reserve above its floor to close a spot-over-short equity gap."""
    if portfolio.short_allocated_capital > 0 and portfolio.spot_allocated_capital > 0:
        if portfolio.position_equity_gap(price) <= 0:
            return 0.0
        quantity = _equity_rebalance_qty(portfolio, price, cfg, costs, "add_short")
        if quantity <= 1e-12:
            return 0.0
        portfolio_cfg = cfg["portfolio"]  # type: ignore[assignment]
        return _add_short_from_reserve(
            portfolio, quantity, price, float(portfolio_cfg["leverage"]), costs,
            _reserve_floor(portfolio, cfg)
        )

    # Legacy quantity-gap fallback for hand-built fixtures without equity
    # allocation fields.
    gap = max(0.0, portfolio.spot_qty - portfolio.short_qty)
    if gap <= 1e-12:
        return 0.0
    portfolio_cfg = cfg["portfolio"]  # type: ignore[assignment]
    return _add_short_from_reserve(
        portfolio, gap, price, float(portfolio_cfg["leverage"]), costs,
        _reserve_floor(portfolio, cfg)
    )


def enter_portfolio(capital: float, price: float, cfg: dict[str, object]) -> Portfolio:
    portfolio_cfg = cfg["portfolio"]  # type: ignore[assignment]
    reserve_cfg = cfg["reserve"]  # type: ignore[assignment]
    costs = cfg["costs"]  # type: ignore[assignment]
    if str(portfolio_cfg.get("margin_mode", "cross")).lower() != "cross":
        raise ValueError("SPHA research requires cross margin mode")
    reserve_fraction = float(reserve_cfg["initial_cash_fraction"])
    active_fraction = float(portfolio_cfg["active_capital_fraction"])
    if reserve_fraction < 0 or active_fraction < 0 or not math.isclose(
        reserve_fraction + active_fraction, 1.0, rel_tol=1e-9, abs_tol=1e-9
    ):
        raise ValueError("reserve and active capital fractions must sum to 1")
    reserve_cash = capital * reserve_fraction
    active_capital = capital * active_fraction
    spot_notional = active_capital * float(portfolio_cfg["initial_spot_notional_fraction"])
    perpetual_notional = active_capital * float(portfolio_cfg["initial_perpetual_notional_fraction"])
    if not math.isclose(spot_notional, perpetual_notional, rel_tol=1e-9, abs_tol=1e-9):
        raise ValueError("price-layer study requires equal initial spot and perpetual notionals")
    portfolio = Portfolio(active_capital, 0.0, 0.0, 0.0, 0.0, 0.0,
                          reserve_cash, capital)
    spot_fill = _slippage(price, "buy", float(costs["slippage_one_way"]))
    spot_qty = spot_notional / spot_fill
    _buy_spot(portfolio, spot_qty, price, costs)
    portfolio.core_spot_qty = portfolio.spot_qty
    portfolio.core_spot_allocated_capital = portfolio.spot_allocated_capital
    # The hedge is defined in base units.  Use the actual spot quantity after
    # entry slippage so the two legs start at ratio 1.0 rather than silently
    # mixing quote notional and unfilled base quantity.
    _add_short(portfolio, spot_qty, price, float(portfolio_cfg["leverage"]), costs)
    portfolio.core_short_qty = portfolio.short_qty
    return portfolio


def _prepare_bars(bars: Sequence[Bar]) -> list[Bar]:
    ordered = sorted(bars, key=lambda item: item.ts)
    if len(ordered) >= 2:
        deltas = [ordered[index].ts - ordered[index - 1].ts for index in range(1, min(len(ordered), 1000))]
        if deltas and sum(delta == FIVE_MINUTES for delta in deltas) >= len(deltas) * 0.8:
            return aggregate_to_hour(ordered)
    return ordered


def _backtest_fixed_short_spot_grid(symbol: str, bars: Sequence[Bar], capital: float,
                                    cfg: dict[str, object]) -> tuple[list[Trade], list[Rebalance]]:
    """Backtest a fixed core hedge with reserve-funded tactical spot inventory."""
    bars = _prepare_bars(bars)
    if len(bars) < 3:
        return [], []
    signals = {signal.index: signal for signal in find_signals(symbol, bars, cfg)}
    portfolio_cfg = cfg["portfolio"]  # type: ignore[assignment]
    rebalance_cfg = cfg["rebalance"]  # type: ignore[assignment]
    costs = cfg["costs"]  # type: ignore[assignment]
    max_hold = int(portfolio_cfg["research_max_hold_bars"])
    threshold = float(rebalance_cfg["threshold_pct"])
    max_actions = int(rebalance_cfg["max_actions_per_campaign"])
    buy_fraction = min(1.0, max(0.0, float(rebalance_cfg["buy_reserve_fraction"])))
    sell_fraction = min(1.0, max(0.0, float(rebalance_cfg["sell_tactical_fraction"])))
    max_tactical_fraction = max(
        0.0, float(rebalance_cfg["max_tactical_spot_fraction_of_core"])
    )
    trades: list[Trade] = []
    rebalances: list[Rebalance] = []
    index = 0
    while index < len(bars) - 1:
        signal = signals.get(index)
        if signal is None:
            index += 1
            continue
        entry_index = index + 1
        entry_price = bars[entry_index].open
        portfolio = enter_portfolio(capital, entry_price, cfg)
        entry_equity = portfolio.equity(entry_price)
        reserve_start = portfolio.reserve_cash
        entry_spot_qty = portfolio.spot_qty
        core_qty = portfolio.core_spot_qty
        max_spot_qty = portfolio.spot_qty
        min_reserve_cash = portfolio.reserve_cash
        max_delta_base_pct = 0.0
        anchor = entry_price
        pending: str | None = None
        steps = 0
        max_campaign_deviation = portfolio.notional_deviation(entry_price)
        max_campaign_equity_deviation = portfolio.position_equity_deviation(entry_price)
        exit_index = min(len(bars) - 1, entry_index + max_hold - 1)
        exit_reason = "max_hold" if exit_index < len(bars) - 1 else "data_end"
        forced_exit_price: float | None = None
        for current in range(entry_index, exit_index + 1):
            bar = bars[current]
            if current > entry_index and bar.ts - bars[current - 1].ts != HOUR:
                exit_index = current - 1
                exit_reason = "data_gap"
                break
            if abs(portfolio.core_spot_qty - portfolio.core_short_qty) > 1e-12:
                forced_exit_price = bar.open
                exit_index = current
                exit_reason = "core_hedge_violation"
                pending = None
                break
            if pending is not None and current > entry_index:
                action = pending
                qty = 0.0
                if action == "buy_tactical_spot":
                    capacity = max(0.0, core_qty * max_tactical_fraction - portfolio.tactical_spot_qty)
                    fill = _slippage(bar.open, "buy", float(costs["slippage_one_way"]))
                    fee_rate = float(costs["fee_rate_one_way"])
                    available = max(0.0, portfolio.reserve_cash - _reserve_floor(portfolio, cfg))
                    budget = min(available * buy_fraction,
                                 capacity * fill * (1.0 + fee_rate))
                    qty = _buy_tactical_spot_from_reserve(portfolio, budget, bar.open, cfg, costs)
                elif action == "sell_tactical_spot":
                    qty = _sell_tactical_spot_to_reserve(
                        portfolio, portfolio.tactical_spot_qty * sell_fraction, bar.open, costs
                    )
                elif action == "reserve_floor_exit":
                    forced_exit_price = bar.open
                    exit_index = current
                    exit_reason = "reserve_floor_exit"
                    pending = None
                    break
                if qty > 0:
                    steps += 1
                    anchor = bar.open
                    max_spot_qty = max(max_spot_qty, portfolio.spot_qty)
                    min_reserve_cash = min(min_reserve_cash, portfolio.reserve_cash)
                    delta_pct = abs(portfolio.delta_base) / max(core_qty, 1e-12) * 100.0
                    max_delta_base_pct = max(max_delta_base_pct, delta_pct)
                    max_campaign_deviation = max(max_campaign_deviation, portfolio.notional_deviation(bar.open))
                    max_campaign_equity_deviation = max(
                        max_campaign_equity_deviation,
                        portfolio.position_equity_deviation(bar.open),
                    )
                    rebalances.append(Rebalance(
                        current, bar.ts, action, bar.open,
                        portfolio.spot_qty, portfolio.short_qty,
                        portfolio.hedge_ratio,
                        portfolio.notional_deviation(bar.open),
                        portfolio.equity(bar.open),
                        portfolio.reserve_cash,
                        portfolio.position_equity_deviation(bar.open),
                        portfolio.spot_position_equity(bar.open),
                        portfolio.short_position_equity(bar.open),
                        portfolio.collateral,
                        portfolio.short_cross_buffer(),
                        portfolio.delta_base,
                        portfolio.tactical_spot_qty,
                    ))
                    if _reserve_floor_exit_required(portfolio, bar.open, cfg):
                        forced_exit_price = bar.open
                        exit_index = current
                        exit_reason = "reserve_floor_exit"
                        pending = None
                        break
                pending = None
            if current >= exit_index:
                continue
            current_deviation = portfolio.position_equity_deviation(bar.close)
            max_campaign_equity_deviation = max(max_campaign_equity_deviation, current_deviation)
            max_campaign_deviation = max(max_campaign_deviation, portfolio.notional_deviation(bar.close))
            max_delta_base_pct = max(
                max_delta_base_pct,
                abs(portfolio.delta_base) / max(core_qty, 1e-12) * 100.0,
            )
            if _reserve_floor_exit_required(portfolio, bar.close, cfg):
                pending = "reserve_floor_exit"
            elif steps < max_actions:
                capacity = core_qty * max_tactical_fraction
                if bar.close <= anchor * (1.0 - threshold) and portfolio.tactical_spot_qty < capacity - 1e-12:
                    pending = "buy_tactical_spot"
                elif bar.close >= anchor * (1.0 + threshold) and portfolio.tactical_spot_qty > 1e-12:
                    pending = "sell_tactical_spot"
        final_price = forced_exit_price if forced_exit_price is not None else bars[exit_index].close
        reserve_before_exit = portfolio.reserve_cash
        _close_short(portfolio, portfolio.short_qty, final_price, costs, destination="reserve")
        _sell_spot(portfolio, portfolio.spot_qty, final_price, costs)
        min_reserve_cash = min(min_reserve_cash, portfolio.reserve_cash)
        final_equity = portfolio.equity(final_price)
        trades.append(Trade(
            symbol, signal.index, entry_index, exit_index, signal.kind,
            entry_price, final_price, entry_equity, final_equity,
            (final_equity / capital - 1.0) * 100.0, steps,
            max_campaign_deviation * 100.0,
            portfolio.fee_paid,
            0.0,
            exit_reason,
            reserve_start,
            reserve_before_exit,
            max_spot_qty,
            min_reserve_cash,
            ((max_spot_qty / entry_spot_qty - 1.0) * 100.0
             if entry_spot_qty > 0 else 0.0),
            portfolio.reserve_cash,
            max_campaign_equity_deviation * 100.0,
            max_delta_base_pct,
        ))
        index = exit_index + 1
    return trades, rebalances


def backtest(symbol: str, bars: Sequence[Bar], capital: float = 10_000.0,
             config: dict[str, object] | None = None) -> tuple[list[Trade], list[Rebalance]]:
    """Run one-symbol, one-position-at-a-time campaigns."""
    cfg = merge_config(config)
    rebalance_cfg = cfg["rebalance"]  # type: ignore[assignment]
    if rebalance_cfg.get("mode") == "fixed_short_spot_grid":
        return _backtest_fixed_short_spot_grid(symbol, bars, capital, cfg)
    bars = _prepare_bars(bars)
    if len(bars) < 3:
        return [], []
    signals = {signal.index: signal for signal in find_signals(symbol, bars, cfg)}
    portfolio_cfg = cfg["portfolio"]  # type: ignore[assignment]
    rebalance_cfg = cfg["rebalance"]  # type: ignore[assignment]
    costs = cfg["costs"]  # type: ignore[assignment]
    max_hold = int(portfolio_cfg["research_max_hold_bars"])
    max_deviation = float(rebalance_cfg.get(
        "position_equity_deviation_trigger_pct",
        rebalance_cfg.get("max_position_equity_deviation_pct", 0.20),
    ))
    threshold = float(rebalance_cfg["threshold_pct"])
    max_actions = int(rebalance_cfg["max_actions_per_campaign"])
    trades: list[Trade] = []
    rebalances: list[Rebalance] = []
    index = 0
    while index < len(bars) - 1:
        signal = signals.get(index)
        if signal is None:
            index += 1
            continue
        entry_index = index + 1
        entry_price = bars[entry_index].open
        portfolio = enter_portfolio(capital, entry_price, cfg)
        entry_equity = portfolio.equity(entry_price)
        reserve_start = portfolio.reserve_cash
        entry_spot_qty = portfolio.spot_qty
        max_spot_qty = portfolio.spot_qty
        min_reserve_cash = portfolio.reserve_cash
        anchor = entry_price
        pending: str | None = None
        steps = 0
        max_campaign_deviation = portfolio.notional_deviation(entry_price)
        max_campaign_equity_deviation = portfolio.position_equity_deviation(entry_price)
        exit_index = min(len(bars) - 1, entry_index + max_hold - 1)
        exit_reason = "max_hold" if exit_index < len(bars) - 1 else "data_end"
        forced_exit_price: float | None = None
        for current in range(entry_index, exit_index + 1):
            bar = bars[current]
            if current > entry_index and bar.ts - bars[current - 1].ts != HOUR:
                exit_index = current - 1
                exit_reason = "data_gap"
                break
            if pending is not None and current > entry_index:
                action = pending
                if action == "reduce_short_buy_spot":
                    closed, bought = _reduce_short_and_buy_spot(portfolio, bar.open, cfg, costs)
                    qty = closed + bought
                else:
                    qty = _add_short_from_reserve_to_gap(portfolio, bar.open, cfg, costs)
                if qty > 0:
                    steps += 1
                    anchor = bar.open
                    max_spot_qty = max(max_spot_qty, portfolio.spot_qty)
                    min_reserve_cash = min(min_reserve_cash, portfolio.reserve_cash)
                    max_campaign_deviation = max(max_campaign_deviation, portfolio.notional_deviation(bar.open))
                    max_campaign_equity_deviation = max(
                        max_campaign_equity_deviation,
                        portfolio.position_equity_deviation(bar.open),
                    )
                    rebalances.append(Rebalance(current, bar.ts, action, bar.open,
                                                portfolio.spot_qty, portfolio.short_qty,
                                                portfolio.hedge_ratio,
                                                portfolio.notional_deviation(bar.open),
                                                portfolio.equity(bar.open),
                                                portfolio.reserve_cash,
                                                portfolio.position_equity_deviation(bar.open),
                                                portfolio.spot_position_equity(bar.open),
                                                portfolio.short_position_equity(bar.open),
                                                portfolio.collateral,
                                                portfolio.short_cross_buffer()))
                if action == "add_short_from_reserve" and _reserve_floor_exit_required(
                    portfolio, bar.open, cfg
                ):
                    # The reserve floor is a hard stop for a residual net-long
                    # state: flatten both legs at this executable price.
                    forced_exit_price = bar.open
                    exit_index = current
                    exit_reason = "reserve_floor_exit"
                    pending = None
                    break
                pending = None
            if current >= exit_index:
                continue
            # A confirmed close beyond the equity trigger takes priority over
            # the price grid; execution happens on the next bar open.
            current_deviation = portfolio.position_equity_deviation(bar.close)
            equity_gap = portfolio.position_equity_gap(bar.close)
            max_campaign_equity_deviation = max(max_campaign_equity_deviation, current_deviation)
            max_campaign_deviation = max(max_campaign_deviation, portfolio.notional_deviation(bar.close))
            over_cap = current_deviation > max_deviation + 1e-12
            if over_cap:
                if equity_gap > 0 and portfolio.spot_qty > 0:
                    pending = "add_short_from_reserve"
                elif equity_gap < 0 and portfolio.short_qty > 0:
                    pending = "reduce_short_buy_spot"
            elif steps >= max_actions:
                continue
            # Once back within the cap, apply the price-anchor grid. A single
            # close that crosses several thresholds still schedules one action.
            elif bar.close <= anchor * (1.0 - threshold) and equity_gap < 0 and portfolio.short_qty > 0:
                pending = "reduce_short_buy_spot"
            elif bar.close >= anchor * (1.0 + threshold) and equity_gap > 0 and portfolio.spot_qty > 0:
                pending = "add_short_from_reserve"
        final_price = forced_exit_price if forced_exit_price is not None else bars[exit_index].close
        reserve_before_exit = portfolio.reserve_cash
        _close_short(portfolio, portfolio.short_qty, final_price, costs, destination="reserve")
        _sell_spot(portfolio, portfolio.spot_qty, final_price, costs)
        min_reserve_cash = min(min_reserve_cash, portfolio.reserve_cash)
        final_equity = portfolio.equity(final_price)
        trades.append(Trade(symbol, signal.index, entry_index, exit_index, signal.kind,
                            entry_price, final_price, entry_equity, final_equity,
                            (final_equity / capital - 1.0) * 100.0, steps,
                            max_campaign_deviation * 100.0,
                            portfolio.fee_paid,
                            0.0,
                            exit_reason,
                            reserve_start,
                            reserve_before_exit,
                            max_spot_qty,
                            min_reserve_cash,
                            ((max_spot_qty / entry_spot_qty - 1.0) * 100.0
                             if entry_spot_qty > 0 else 0.0),
                            portfolio.reserve_cash,
                            max_campaign_equity_deviation * 100.0))
        index = exit_index + 1
    return trades, rebalances


def summary(trades: Iterable[Trade]) -> dict[str, float | int | None]:
    rows = list(trades)
    returns = [trade.return_pct / 100.0 for trade in rows]
    wins = [item for item in returns if item > 0]
    losses = [item for item in returns if item <= 0]
    equity = peak = 1.0
    drawdown = 0.0
    for item in returns:
        equity *= 1.0 + item
        peak = max(peak, equity)
        drawdown = max(drawdown, (peak - equity) / peak)
    return {
        "trades": len(rows),
        "wins": len(wins),
        "win_rate": len(wins) / len(rows) if rows else 0.0,
        "compound_return_pct": (equity - 1.0) * 100.0,
        "average_return_pct": sum(item * 100.0 for item in returns) / len(rows) if rows else 0.0,
        "profit_factor": sum(wins) / abs(sum(losses)) if losses and sum(losses) else None,
        "max_drawdown_pct": drawdown * 100.0,
        "total_rebalances": sum(trade.rebalances for trade in rows),
        "mean_max_position_equity_deviation_pct": sum(
            trade.max_position_equity_deviation_pct for trade in rows
        ) / len(rows) if rows else 0.0,
        "mean_max_delta_base_pct": sum(trade.max_delta_base_pct for trade in rows) / len(rows) if rows else 0.0,
        "max_max_delta_base_pct": max((trade.max_delta_base_pct for trade in rows), default=0.0),
        "mean_max_notional_deviation_pct": sum(trade.max_notional_deviation_pct for trade in rows) / len(rows) if rows else 0.0,
        "total_fees_paid": sum(trade.fees_paid for trade in rows),
        "total_funding_paid": sum(trade.funding_paid for trade in rows),
        "mean_max_spot_gain_pct": sum(trade.max_spot_gain_pct for trade in rows) / len(rows) if rows else 0.0,
        "mean_reserve_end": sum(trade.reserve_end for trade in rows) / len(rows) if rows else 0.0,
        "mean_reserve_after_exit": sum(trade.reserve_after_exit for trade in rows) / len(rows) if rows else 0.0,
        "mean_min_reserve_cash": sum(trade.min_reserve_cash for trade in rows) / len(rows) if rows else 0.0,
    }


def write_outputs(output_dir: Path, trades: Sequence[Trade], rebalances: Sequence[Rebalance]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    report = {"summary": summary(trades), "trades": [asdict(item) for item in trades],
              "rebalances": [asdict(item) for item in rebalances]}
    (output_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with (output_dir / "trades.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = list(Trade.__dataclass_fields__)
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(item) for item in trades)
    with (output_dir / "rebalances.csv").open("w", newline="", encoding="utf-8") as stream:
        fields = list(Rebalance.__dataclass_fields__)
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(asdict(item) for item in rebalances)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-file", type=Path, required=True, help="confirmed OKX 5m JSONL(.gz) for one symbol")
    parser.add_argument("--symbol", default="UNKNOWN", help="symbol label")
    parser.add_argument("--config", type=Path, help="strategy.json")
    parser.add_argument("--capital", type=float, default=10_000.0)
    parser.add_argument("--output-dir", type=Path, default=Path("strategies/spot_perp_hedged_accumulation/results"))
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8")) if args.config else None
    trades, rebalances = backtest(args.symbol, load_jsonl(args.data_file), args.capital, config)
    write_outputs(args.output_dir, trades, rebalances)
    print(json.dumps(summary(trades), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
