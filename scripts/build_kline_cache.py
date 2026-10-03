#!/usr/bin/env python3
"""Build reusable Parquet caches from the raw OKX 5m JSONL exports.

The raw files remain the source of truth.  This command reads each compressed
5m file once and writes all requested higher timeframes for that contract in
one pass.  A cache manifest records the source manifest digest, so repeated
backtests do not decompress or parse the same files again.

PyArrow is intentionally imported lazily: aggregation helpers and the unit
tests remain usable without the optional Parquet dependency installed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator

BASE_INTERVAL_MS = 5 * 60 * 1000
TIMEFRAME_FACTORS = {"15m": 3, "30m": 6, "1h": 12, "4h": 48}
DEFAULT_TIMEFRAMES = tuple(TIMEFRAME_FACTORS)
UTC = timezone.utc


def iso_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def source_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_source_candles(path: Path) -> Iterator[dict[str, Any]]:
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            try:
                candle = json.loads(line)
                timestamp_ms = int(candle["timestamp_ms"])
                if not all(field in candle for field in ("open", "high", "low", "close", "volume", "quote_volume")):
                    raise ValueError("missing OHLCV field")
                if candle.get("confirmed") is not True:
                    continue
                yield {
                    "timestamp_ms": timestamp_ms,
                    "open": float(candle["open"]),
                    "high": float(candle["high"]),
                    "low": float(candle["low"]),
                    "close": float(candle["close"]),
                    "volume": float(candle["volume"]),
                    "quote_volume": float(candle["quote_volume"]),
                }
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                raise ValueError(f"{path}:{line_number}: invalid candle: {error}") from error


@dataclass
class _Bar:
    bucket_ms: int
    first_ms: int
    last_ms: int
    count: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float

    @classmethod
    def start(cls, candle: dict[str, Any], bucket_ms: int) -> "_Bar":
        return cls(bucket_ms, candle["timestamp_ms"], candle["timestamp_ms"], 1,
                   candle["open"], candle["high"], candle["low"], candle["close"],
                   candle["volume"], candle["quote_volume"])

    def add(self, candle: dict[str, Any]) -> None:
        self.last_ms = candle["timestamp_ms"]
        self.count += 1
        self.high = max(self.high, candle["high"])
        self.low = min(self.low, candle["low"])
        self.close = candle["close"]
        self.volume += candle["volume"]
        self.quote_volume += candle["quote_volume"]

    def complete(self, factor: int) -> bool:
        return self.count == factor and self.first_ms == self.bucket_ms and self.last_ms == self.bucket_ms + (factor - 1) * BASE_INTERVAL_MS

    def row(self) -> dict[str, Any]:
        return {
            "timestamp_ms": self.bucket_ms,
            "timestamp": iso_utc(self.bucket_ms),
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
            "quote_volume": self.quote_volume,
            "confirmed": True,
        }


def aggregate_candles(candles: Iterable[dict[str, Any]], timeframes: Iterable[str]) -> dict[str, list[dict[str, Any]]]:
    """Aggregate sorted 5m candles into complete bars for every timeframe."""
    selected = tuple(timeframes)
    states: dict[str, _Bar | None] = {timeframe: None for timeframe in selected}
    output: dict[str, list[dict[str, Any]]] = {timeframe: [] for timeframe in selected}
    for candle in candles:
        timestamp_ms = candle["timestamp_ms"]
        for timeframe in selected:
            factor = TIMEFRAME_FACTORS[timeframe]
            bucket_ms = (timestamp_ms // (BASE_INTERVAL_MS * factor)) * (BASE_INTERVAL_MS * factor)
            current = states[timeframe]
            if current is not None and current.bucket_ms != bucket_ms:
                if current.complete(factor):
                    output[timeframe].append(current.row())
                current = None
            if current is None:
                states[timeframe] = _Bar.start(candle, bucket_ms)
            else:
                # A duplicate or out-of-order 5m row invalidates this bucket;
                # the source exporter normally guarantees sorted unique rows.
                if timestamp_ms <= current.last_ms:
                    raise ValueError(f"non-increasing candle timestamp: {timestamp_ms}")
                current.add(candle)
    for timeframe, current in states.items():
        if current is not None and current.complete(TIMEFRAME_FACTORS[timeframe]):
            output[timeframe].append(current.row())
    return output


def load_pyarrow() -> tuple[Any, Any]:
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq
    except ImportError as error:
        raise SystemExit(
            "Parquet cache requires PyArrow. Install it with "
            "`python3 -m pip install pyarrow` and rerun this command."
        ) from error
    return pa, pq


def parquet_table(pa: Any, symbol: str, rows: list[dict[str, Any]]) -> Any:
    data = {
        "instrument_id": [symbol] * len(rows),
        "timestamp_ms": [row["timestamp_ms"] for row in rows],
        "timestamp": [row["timestamp"] for row in rows],
        "open": [row["open"] for row in rows],
        "high": [row["high"] for row in rows],
        "low": [row["low"] for row in rows],
        "close": [row["close"] for row in rows],
        "volume": [row["volume"] for row in rows],
        "quote_volume": [row["quote_volume"] for row in rows],
        "confirmed": [True] * len(rows),
    }
    return pa.table(data)


def write_parquet(pa: Any, pq: Any, destination: Path, symbol: str, rows: list[dict[str, Any]], compression: str) -> None:
    temporary = destination.with_suffix(destination.suffix + ".part")
    temporary.unlink(missing_ok=True)
    table = parquet_table(pa, symbol, rows)
    pq.write_table(table, temporary, compression=compression, use_dictionary=["instrument_id", "timestamp"])
    os.replace(temporary, destination)


def cache_is_current(cache_manifest_path: Path, expected_digest: str, timeframes: tuple[str, ...], symbols: set[str], output_dir: Path) -> bool:
    try:
        cache_manifest = json.loads(cache_manifest_path.read_text(encoding="utf-8"))
        if cache_manifest.get("source_manifest_sha256") != expected_digest:
            return False
        if tuple(cache_manifest.get("timeframes", ())) != timeframes:
            return False
        cached_symbols = set(cache_manifest.get("symbols", ()))
        if cached_symbols != symbols:
            return False
        return all((output_dir / timeframe / f"{symbol}.parquet").is_file() for timeframe in timeframes for symbol in symbols)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def build_cache(input_dir: Path, output_dir: Path, timeframes: tuple[str, ...], force: bool, compression: str) -> dict[str, Any]:
    pa, pq = load_pyarrow()
    source_manifest_path = input_dir / "manifest.json"
    if not source_manifest_path.is_file():
        raise SystemExit(f"source manifest not found: {source_manifest_path}")
    source_manifest = json.loads(source_manifest_path.read_text(encoding="utf-8"))
    if source_manifest.get("interval") != "5m":
        raise SystemExit(f"expected a 5m source manifest, got {source_manifest.get('interval')!r}")
    entries = [entry for entry in source_manifest.get("contracts", []) if entry.get("status") != "failed" and entry.get("file")]
    if not entries:
        raise SystemExit("source manifest contains no usable contracts")
    symbols = {entry["instrument_id"] for entry in entries}
    digest = source_digest(source_manifest_path)
    manifest_path = output_dir / "cache_manifest.json"
    if not force and cache_is_current(manifest_path, digest, timeframes, symbols, output_dir):
        cached = json.loads(manifest_path.read_text(encoding="utf-8"))
        print(f"cache is current: {manifest_path} ({cached['summary']['total_rows']} rows)")
        return cached

    for timeframe in timeframes:
        (output_dir / timeframe).mkdir(parents=True, exist_ok=True)
    counts = {timeframe: 0 for timeframe in timeframes}
    contracts = []
    for index, entry in enumerate(sorted(entries, key=lambda item: item["instrument_id"]), 1):
        symbol = entry["instrument_id"]
        source_path = input_dir / entry["file"]
        if not source_path.is_file():
            raise SystemExit(f"source file missing for {symbol}: {source_path}")
        aggregated = aggregate_candles(read_source_candles(source_path), timeframes)
        for timeframe in timeframes:
            destination = output_dir / timeframe / f"{symbol}.parquet"
            write_parquet(pa, pq, destination, symbol, aggregated[timeframe], compression)
            counts[timeframe] += len(aggregated[timeframe])
        contracts.append({"instrument_id": symbol, "source_file": entry["file"], "rows": {tf: len(aggregated[tf]) for tf in timeframes}})
        print(f"[{index}/{len(entries)}] {symbol}: " + ", ".join(f"{tf}={len(aggregated[tf])}" for tf in timeframes))

    valid_files = {f"{symbol}.parquet" for symbol in symbols}
    for timeframe in timeframes:
        for stale in (output_dir / timeframe).glob("*.parquet"):
            if stale.name not in valid_files:
                stale.unlink()
    cache_manifest = {
        "schema_version": 1,
        "source_manifest": str(source_manifest_path),
        "source_manifest_sha256": digest,
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "base_interval": "5m",
        "timeframes": list(timeframes),
        "symbols": sorted(symbols),
        "contracts": contracts,
        "summary": {"contracts": len(contracts), "total_rows": sum(counts.values()), "rows_by_timeframe": counts},
    }
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(cache_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, manifest_path)
    print(f"cache: {manifest_path} ({len(contracts)} contracts, {sum(counts.values())} rows)")
    return cache_manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build reusable 15m/30m/1h/4h Parquet caches from OKX 5m exports")
    parser.add_argument("--input", type=Path, default=Path("data/kline/okx/swap/5m"), help="raw 5m export directory")
    parser.add_argument("--output", type=Path, default=Path(".cache/kline/okx/swap"), help="rebuildable cache directory")
    parser.add_argument("--timeframes", nargs="+", choices=tuple(TIMEFRAME_FACTORS), default=list(DEFAULT_TIMEFRAMES), help="timeframes built in one source pass")
    parser.add_argument("--compression", choices=("zstd", "snappy", "gzip", "brotli", "none"), default="zstd")
    parser.add_argument("--force", action="store_true", help="rebuild even when the source manifest is unchanged")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    timeframes = tuple(dict.fromkeys(args.timeframes))
    compression = None if args.compression == "none" else args.compression
    build_cache(args.input.expanduser(), args.output.expanduser(), timeframes, args.force, compression)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
