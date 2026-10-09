"""Deterministic market arithmetic for the complete AI observation pool.

These facts complement the untouched raw snapshot. They do not choose a
direction, estimate probabilities, propose orders, or authorize trading.
"""

from __future__ import annotations

from datetime import datetime
import math
from typing import Any, Mapping

try:
    from .ai_schema import AISnapshot, MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES, PRIMARY_ENTRY_INTERVAL
except ImportError:  # bundled backend modules are launched as scripts
    from ai_schema import AISnapshot, MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES, PRIMARY_ENTRY_INTERVAL


_INTERVALS = ("5m", "15m", "1H", "4H")
_METHODOLOGY = {
    "primaryEntryInterval": PRIMARY_ENTRY_INTERVAL,
    "primaryEntryMinimumConfirmedCandles": MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES,
    "entryAnalysis": "Use confirmed 4H trend, structure, support/resistance and conditional long/short entry plans. Shorter intervals, tickers, books and funding are execution/risk context; forming 4H candles cannot confirm an entry.",
    "candleScope": "Only confirmed=true candles, in supplied oldest-to-newest order. Invalid OHLC breaks the usable trailing segment; forming candles are excluded.",
    "returnsPercent": "(latest close / close N confirmed bars earlier - 1) * 100; N+1 usable bars required.",
    "sma": "Simple arithmetic mean of the last N usable confirmed closes; N bars required.",
    "recent20HighLow": "Maximum high and minimum low of the last 20 usable confirmed candles; 20 bars required.",
    "meanTrueRange14": "Simple mean of 14 true ranges max(high-low, abs(high-previousClose), abs(low-previousClose)); 15 usable bars required. No Wilder smoothing or seed.",
    "volumeRatioToPrevious20": "Latest confirmed volume / simple mean of the preceding 20 volumes; 21 usable bars required. Missing/invalid volume or zero preceding mean yields null.",
    "spreadBasisPoints": "(ask-bid) / ((ask+bid)/2) * 10000; both positive and ask>=bid required.",
    "top5SizeImbalance": "(bid size - ask size) / (bid size + ask size), using the first up to 5 supplied levels per side. Sizes retain exchange contract units; malformed levels yield null side totals.",
    "fundingRate": "Native exchange funding ratio, not percent.",
    "unknown": "Insufficient, malformed, or non-finite quantities are null; missing quantities never become zero.",
}


def _number(value: Any, *, positive: bool = False, nonnegative: bool = False) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(result) or (positive and result <= 0) or (nonnegative and result < 0):
        return None
    return result


def _finite(value: float) -> float | None:
    return value if math.isfinite(value) else None


def _mean(values: list[float]) -> float | None:
    if not values:
        return None
    try:
        return _finite(math.fsum(value / len(values) for value in values))
    except (ValueError, OverflowError):
        return None


def _sum(values: list[float]) -> float | None:
    try:
        return _finite(math.fsum(values))
    except (ValueError, OverflowError):
        return None


def _millis(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _timestamp(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if parsed.tzinfo is not None else None


def _row(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _spread(bid: float | None, ask: float | None) -> float | None:
    if bid is None or ask is None or ask < bid:
        return None
    # Half each price first, avoiding overflow when both finite prices are large.
    midpoint = bid / 2 + ask / 2
    return _finite((ask - bid) / midpoint * 10000) if midpoint > 0 else None


def _ticker(value: Any) -> dict[str, Any]:
    row = _row(value)
    bid = _number(row.get("bidPx"), positive=True)
    ask = _number(row.get("askPx"), positive=True)
    return {
        "lastPrice": _number(row.get("last"), positive=True),
        "bidPrice": bid, "askPrice": ask,
        "spreadBasisPoints": _spread(bid, ask),
        "sourceTimestampUnixMilliseconds": _millis(row.get("ts")),
    }


def _book_side(value: Any, *, bid: bool) -> dict[str, Any]:
    levels = value[:5] if isinstance(value, list) else []
    parsed: list[tuple[float, float]] = []
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        price = _number(level[0], positive=True)
        size = _number(level[1], nonnegative=True)
        if price is not None and size is not None:
            parsed.append((price, size))
    invalid = len(levels) - len(parsed)
    return {
        "levelCount": len(levels), "invalidLevelCount": invalid,
        "bestPrice": (max if bid else min)(price for price, _ in parsed) if parsed and not invalid else None,
        "sizeContracts": _sum([size for _, size in parsed]) if parsed and not invalid else None,
    }


def _book(value: Any) -> dict[str, Any]:
    row = _row(value)
    bid = _book_side(row.get("bids"), bid=True)
    ask = _book_side(row.get("asks"), bid=False)
    bid_size, ask_size = bid["sizeContracts"], ask["sizeContracts"]
    imbalance = None
    if bid_size is not None and ask_size is not None and max(bid_size, ask_size) > 0:
        scale = max(bid_size, ask_size)
        imbalance = (bid_size / scale - ask_size / scale) / (bid_size / scale + ask_size / scale)
    return {
        "bidLevelCount": bid["levelCount"], "askLevelCount": ask["levelCount"],
        "invalidBidLevelCount": bid["invalidLevelCount"], "invalidAskLevelCount": ask["invalidLevelCount"],
        "bestBidPrice": bid["bestPrice"], "bestAskPrice": ask["bestPrice"],
        "bidTop5SizeContracts": bid_size, "askTop5SizeContracts": ask_size,
        "top5SizeImbalance": imbalance,
        "spreadBasisPoints": _spread(bid["bestPrice"], ask["bestPrice"]),
        "sourceTimestampUnixMilliseconds": _millis(row.get("ts")),
    }


def _candles(value: Any) -> dict[str, Any]:
    rows = value if isinstance(value, list) else []
    confirmed_count = invalid_count = valid_count = forming_count = 0
    usable: list[dict[str, Any]] = []
    for value in rows:
        row = _row(value)
        if row.get("confirmed") is not True:
            if row.get("confirmed") is False:
                forming_count += 1
            else:
                # An unknown confirmation cannot establish a closed-bar sequence.
                usable = []
            continue
        confirmed_count += 1
        prices = {key: _number(row.get(key), positive=True) for key in ("open", "high", "low", "close")}
        if any(price is None for price in prices.values()) or not (
            prices["low"] <= min(prices["open"], prices["close"])
            <= max(prices["open"], prices["close"]) <= prices["high"]
        ):
            invalid_count += 1
            usable = []
            continue
        valid_count += 1
        usable.append({**prices, "volume": _number(row.get("volume"), nonnegative=True), "timestamp": _timestamp(row.get("timestamp"))})
    closes = [row["close"] for row in usable]
    count = len(usable)
    result: dict[str, Any] = {
        "receivedRowCount": len(rows), "confirmedRowCount": confirmed_count,
        "validConfirmedRowCount": valid_count, "invalidConfirmedOHLCCount": invalid_count,
        "formingRowCount": forming_count, "usableTrailingConfirmedRowCount": count,
        "lastConfirmedAt": usable[-1]["timestamp"] if usable else None,
        "lastConfirmedClose": closes[-1] if closes else None,
    }
    for bars in (1, 3, 12):
        result[f"return{bars}BarPercent"] = _finite((closes[-1] / closes[-1-bars] - 1) * 100) if count >= bars + 1 else None
    for bars in (20, 50):
        result[f"sma{bars}"] = _mean(closes[-bars:]) if count >= bars else None
    result["recent20High"] = max(row["high"] for row in usable[-20:]) if count >= 20 else None
    result["recent20Low"] = min(row["low"] for row in usable[-20:]) if count >= 20 else None
    ranges = [
        max(row["high"] - row["low"], abs(row["high"] - previous["close"]), abs(row["low"] - previous["close"]))
        for previous, row in zip(usable[-15:-1], usable[-14:])
    ] if count >= 15 else []
    result["meanTrueRange14"] = _mean(ranges) if count >= 15 else None
    volume_ratio = None
    if count >= 21:
        volumes = [row["volume"] for row in usable[-21:]]
        if all(volume is not None for volume in volumes):
            previous_mean = _mean(volumes[:-1])
            if previous_mean is not None and previous_mean > 0:
                volume_ratio = _finite(volumes[-1] / previous_mean)
    result["volumeRatioToPrevious20"] = volume_ratio
    return result


def _primary_entry_quality(facts: Mapping[str, Any]) -> dict[str, Any]:
    count = facts["usableTrailingConfirmedRowCount"]
    can_open = count >= MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES
    return {
        "interval": PRIMARY_ENTRY_INTERVAL,
        "minimumConfirmedCandles": MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES,
        "usableTrailingConfirmedRowCount": count,
        "canOpen": can_open,
        "error": None if can_open else (
            f"{PRIMARY_ENTRY_INTERVAL} requires at least {MIN_PRIMARY_ENTRY_CONFIRMED_CANDLES} valid confirmed OHLC candles; "
            f"usable trailing history is {count}"
        ),
    }


def primary_entry_quality(snapshot: AISnapshot, instrument_id: str) -> dict[str, Any]:
    """Share the measured primary-history gate between prompts and policy.

    Confirmation and OHLC validity use the same trailing-segment arithmetic as
    the market summaries. This gate never chooses a direction or restricts
    existing position/order management.
    """
    rows = snapshot.candles.get(f"{instrument_id}/{PRIMARY_ENTRY_INTERVAL}") if isinstance(snapshot.candles, Mapping) else None
    return _primary_entry_quality(_candles(rows))


def market_facts(snapshot: AISnapshot) -> dict[str, Any]:
    """Summarize each observed contract, including rows with missing resources."""
    instruments: dict[str, Any] = {}
    for instrument in snapshot.observed_instruments():
        funding = _row(snapshot.fundingRates.get(instrument))
        timeframes = {interval: _candles(snapshot.candles.get(f"{instrument}/{interval}")) for interval in _INTERVALS}
        instruments[instrument] = {
            "ticker": _ticker(snapshot.tickers.get(instrument)),
            "orderBook": _book(snapshot.orderBook.get(instrument)),
            "fundingRate": {
                "rate": _number(funding.get("fundingRate")),
                "nextFundingTimeUnixMilliseconds": _millis(funding.get("nextFundingTime")),
            },
            "timeframes": timeframes,
            "primaryEntryQuality": _primary_entry_quality(timeframes[PRIMARY_ENTRY_INTERVAL]),
        }
    return {"methodology": dict(_METHODOLOGY), "instruments": instruments}
