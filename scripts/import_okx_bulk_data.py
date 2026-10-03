#!/usr/bin/env python3
"""Import OKX's monthly bulk candles and fill the current month via REST.

The OKX historical-data download contains one-minute candles.  This importer
aggregates them into the repository's five-minute JSONL schema and uses the
public REST endpoint only for the not-yet-published current month.
"""

from __future__ import annotations

import argparse
import csv
import gzip
import json
import os
import time
import urllib.error
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from io import BytesIO, TextIOWrapper
from pathlib import Path
from typing import Any

try:
    from export_market_data import (
        ContractResult,
        RequestLimiter,
        fetch_candles,
        is_complete_candle,
        live_swap_symbols,
        output_filename,
        parse_utc,
        iso_utc,
    )
except ImportError:  # pragma: no cover - supports package-style invocation
    from scripts.export_market_data import (
        ContractResult,
        RequestLimiter,
        fetch_candles,
        is_complete_candle,
        live_swap_symbols,
        output_filename,
        parse_utc,
        iso_utc,
    )


UTC = timezone.utc
BAR = "5m"
BAR_MILLISECONDS = 5 * 60 * 1000
BULK_BASE = "https://static.okx.com/cdn/okex/traderecords/candlesticks/monthly"


def month_start(value: datetime) -> datetime:
    return value.astimezone(UTC).replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def next_month(value: datetime) -> datetime:
    if value.month == 12:
        return value.replace(year=value.year + 1, month=1)
    return value.replace(month=value.month + 1)


def month_keys(start: datetime, stop: datetime) -> list[tuple[int, int]]:
    cursor = month_start(start)
    result: list[tuple[int, int]] = []
    while cursor < stop:
        result.append((cursor.year, cursor.month))
        cursor = next_month(cursor)
    return result


def decimal(value: str | None) -> Decimal | None:
    if value is None or not value.strip():
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def bulk_url(symbol: str, year: int, month: int) -> str:
    ym = f"{year:04d}{month:02d}"
    filename = f"{symbol}-candlesticks-{year:04d}-{month:02d}.zip"
    return f"{BULK_BASE}/{ym}/{filename}?v=999"


def download_bulk(symbol: str, year: int, month: int) -> bytes | None:
    request = urllib.request.Request(
        bulk_url(symbol, year, month),
        headers={"User-Agent": "NovaTrade-market-import/1.0", "Accept": "application/zip"},
    )
    for attempt in range(5):
        try:
            with urllib.request.urlopen(request, timeout=90) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            if error.code not in {408, 425, 429, 500, 502, 503, 504}:
                raise
            if attempt == 4:
                raise
            time.sleep(min(1.0 * (2**attempt), 12.0))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 4:
                raise
            time.sleep(min(1.0 * (2**attempt), 12.0))
    return None


def aggregate_month(
    symbol: str,
    payload: bytes,
    start_ms: int,
    stop_ms: int,
    candles: list[dict[str, Any]],
    bucket: dict[str, Any] | None,
) -> dict[str, Any] | None:
    with zipfile.ZipFile(BytesIO(payload)) as archive:
        names = [name for name in archive.namelist() if name.lower().endswith(".csv")]
        if len(names) != 1:
            raise RuntimeError(f"expected one CSV for {symbol}, found {names}")
        with archive.open(names[0], "r") as binary:
            stream = TextIOWrapper(binary, encoding="utf-8", newline="")
            for row in csv.DictReader(stream):
                if row.get("instrument_name") != symbol:
                    continue
                try:
                    timestamp_ms = int(row["open_time"])
                except (KeyError, TypeError, ValueError):
                    continue
                if timestamp_ms < start_ms or timestamp_ms >= stop_ms:
                    continue
                open_ = decimal(row.get("open"))
                high = decimal(row.get("high"))
                low = decimal(row.get("low"))
                close = decimal(row.get("close"))
                volume = decimal(row.get("vol"))
                quote_volume = decimal(row.get("vol_quote"))
                if None in (open_, high, low, close, volume, quote_volume):
                    continue
                if row.get("confirm", "1") != "1":
                    continue
                bucket_start = timestamp_ms // BAR_MILLISECONDS * BAR_MILLISECONDS
                if bucket is None or bucket["timestamp_ms"] != bucket_start:
                    if bucket is not None:
                        candles.append(bucket_to_candle(bucket))
                    bucket = {
                        "timestamp_ms": bucket_start,
                        "open": open_,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "quote_volume": quote_volume,
                    }
                else:
                    bucket["high"] = max(bucket["high"], high)
                    bucket["low"] = min(bucket["low"], low)
                    bucket["close"] = close
                    bucket["volume"] += volume
                    bucket["quote_volume"] += quote_volume
    return bucket


def bucket_to_candle(bucket: dict[str, Any]) -> dict[str, Any]:
    return {
        "timestamp": iso_utc(bucket["timestamp_ms"]),
        "timestamp_ms": bucket["timestamp_ms"],
        "open": decimal_text(bucket["open"]),
        "high": decimal_text(bucket["high"]),
        "low": decimal_text(bucket["low"]),
        "close": decimal_text(bucket["close"]),
        "volume": decimal_text(bucket["volume"]),
        "quote_volume": decimal_text(bucket["quote_volume"]),
        "confirmed": True,
    }


def import_symbol(
    symbol: str,
    output_dir: Path,
    start_ms: int,
    end_ms: int,
    bulk_months: list[tuple[int, int]],
    tail_start_ms: int,
    limiter: RequestLimiter,
    force: bool,
) -> ContractResult:
    filename = output_filename(symbol, BAR, datetime.fromtimestamp(start_ms / 1000, UTC), datetime.fromtimestamp(end_ms / 1000, UTC))
    destination = output_dir / filename
    if destination.exists() and not force:
        try:
            with gzip.open(destination, "rt", encoding="utf-8") as stream:
                rows = [json.loads(line) for line in stream if line.strip()]
            if rows and rows[-1]["timestamp_ms"] >= end_ms - BAR_MILLISECONDS and all(is_complete_candle(row) for row in rows):
                return ContractResult(symbol, filename, len(rows), rows[0]["timestamp"], rows[-1]["timestamp"], "skipped_existing")
        except Exception:
            pass

    candles: list[dict[str, Any]] = []
    bucket: dict[str, Any] | None = None
    try:
        for year, month in bulk_months:
            payload = download_bulk(symbol, year, month)
            if payload is not None:
                bucket = aggregate_month(symbol, payload, start_ms, tail_start_ms, candles, bucket)
        if bucket is not None:
            candles.append(bucket_to_candle(bucket))

        tail = fetch_candles(symbol, tail_start_ms, end_ms, BAR, limiter)
        merged = {candle["timestamp_ms"]: candle for candle in candles}
        merged.update({candle["timestamp_ms"]: candle for candle in tail})
        candles = [merged[key] for key in sorted(merged) if start_ms <= key <= end_ms]
        if candles and not is_complete_candle(candles[-1]):
            candles.pop()
        if not candles:
            raise RuntimeError("no candles available")

        temporary = destination.with_suffix(destination.suffix + ".part")
        with gzip.open(temporary, "wt", encoding="utf-8", newline="\n") as stream:
            for candle in candles:
                stream.write(json.dumps(candle, ensure_ascii=False, separators=(",", ":")))
                stream.write("\n")
        os.replace(temporary, destination)
        return ContractResult(symbol, filename, len(candles), candles[0]["timestamp"], candles[-1]["timestamp"], "exported")
    except Exception as error:
        destination.with_suffix(destination.suffix + ".part").unlink(missing_ok=True)
        return ContractResult(symbol, None, 0, None, None, "failed", str(error))


def main() -> int:
    parser = argparse.ArgumentParser(description="Import OKX monthly bulk 1m candles as 5m JSONL")
    parser.add_argument("--start", required=True, help="UTC ISO-8601 start")
    parser.add_argument("--end", required=True, help="UTC ISO-8601 end")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workers", type=int, default=8)
    parser.add_argument("--symbols", nargs="+")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    if args.workers <= 0:
        raise SystemExit("--workers must be positive")
    start = parse_utc(args.start)
    end = parse_utc(args.end)
    if start >= end:
        raise SystemExit("start must be earlier than end")
    output_dir = args.output.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    limiter = RequestLimiter()
    symbols = sorted(set(args.symbols)) if args.symbols else live_swap_symbols(limiter)
    if not symbols:
        raise SystemExit("no symbols found")
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    # Monthly ZIPs use the OKX UTC+8 calendar boundary: the file named
    # 2026-10 starts at 2026-09-30 16:00Z.  The current-month file is not
    # published yet, so REST must fill that final eight-hour segment too.
    tail_start = max(start, month_start(end) - timedelta(hours=8))
    tail_start_ms = int(tail_start.timestamp() * 1000)
    bulk_months = month_keys(start, tail_start)
    print(f"importing {len(symbols)} contracts, {start.isoformat()} .. {end.isoformat()}, bulk_months={len(bulk_months)}")

    results: list[ContractResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [executor.submit(import_symbol, symbol, output_dir, start_ms, end_ms, bulk_months, tail_start_ms, limiter, args.force) for symbol in symbols]
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            print(f"[{index}/{len(futures)}] {result.instrument_id}: {result.status} ({result.candle_count} candles)")
            if result.error:
                print(f"  error: {result.error}")

    results.sort(key=lambda item: item.instrument_id)
    manifest = {
        "schema_version": 1,
        "source": "OKX historical market data monthly 1m ZIP + public REST current month",
        "generated_at": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "interval": BAR,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "candle_fields": ["timestamp", "timestamp_ms", "open", "high", "low", "close", "volume", "quote_volume", "confirmed"],
        "contracts": [asdict(result) for result in results],
        "summary": {
            "requested_contracts": len(results),
            "exported_contracts": sum(result.status in {"exported", "skipped_existing"} for result in results),
            "updated_contracts": 0,
            "skipped_contracts": sum(result.status == "skipped_existing" for result in results),
            "failed_contracts": sum(result.status == "failed" for result in results),
            "total_candles": sum(max(result.candle_count, 0) for result in results),
        },
    }
    manifest_path = output_dir / "manifest.json"
    temporary = manifest_path.with_suffix(".json.part")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, manifest_path)
    failed = manifest["summary"]["failed_contracts"]
    if not failed:
        current_files = {result.file for result in results if result.file}
        for stale in output_dir.glob("*.jsonl.gz"):
            if stale.name not in current_files:
                stale.unlink()
    print(f"manifest: {manifest_path} ({len(results)} contracts, failed={failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
