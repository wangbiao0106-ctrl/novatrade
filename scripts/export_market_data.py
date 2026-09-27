#!/usr/bin/env python3
"""Export recent OKX swap candles for offline strategy analysis.

The exporter intentionally talks to OKX's public REST API directly. It does
not need API credentials and writes one compressed JSONL file per contract so
an interrupted run can be resumed without repeating completed contracts.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://www.okx.com/api/v5"
UTC = timezone.utc
PAGE_SIZE = 300


def utc_now() -> datetime:
    return datetime.now(UTC)


def iso_utc(timestamp_ms: int) -> str:
    return datetime.fromtimestamp(timestamp_ms / 1000, UTC).isoformat().replace("+00:00", "Z")


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    result = datetime.fromisoformat(text)
    return (result if result.tzinfo else result.replace(tzinfo=UTC)).astimezone(UTC)


class RequestLimiter:
    """Keep aggregate request rate below OKX's public market-data limit."""

    def __init__(self, minimum_interval: float = 0.12) -> None:
        self.minimum_interval = minimum_interval
        self._lock = threading.Lock()
        self._last_request = 0.0

    def wait(self) -> None:
        with self._lock:
            now = time.monotonic()
            delay = self.minimum_interval - (now - self._last_request)
            if delay > 0:
                time.sleep(delay)
            self._last_request = time.monotonic()


def http_json(path: str, params: dict[str, str], limiter: RequestLimiter, attempts: int = 6) -> dict[str, Any]:
    query = urllib.parse.urlencode(params)
    url = f"{API_BASE}{path}?{query}"
    last_error: Exception | None = None
    for attempt in range(attempts):
        limiter.wait()
        try:
            request = urllib.request.Request(
                url,
                headers={"User-Agent": "NovaTrade-market-export/1.0", "Accept": "application/json"},
            )
            with urllib.request.urlopen(request, timeout=45) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("code") != "0":
                raise RuntimeError(f"OKX API error {payload.get('code')}: {payload.get('msg')}")
            return payload
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, RuntimeError, json.JSONDecodeError) as error:
            last_error = error
            if isinstance(error, urllib.error.HTTPError) and error.code not in {408, 425, 429, 500, 502, 503, 504}:
                break
            time.sleep(min(0.5 * (2**attempt), 12.0))
    raise RuntimeError(f"request failed after {attempts} attempts: {url}: {last_error}")


def live_swap_symbols(limiter: RequestLimiter) -> list[str]:
    rows = http_json("/public/instruments", {"instType": "SWAP"}, limiter).get("data", [])
    symbols = {
        row.get("instId")
        for row in rows
        if row.get("state") == "live"
        and row.get("settleCcy") == "USDT"
        and row.get("ctType") == "linear"
        and str(row.get("instId", "")).endswith("-USDT-SWAP")
    }
    return sorted(symbol for symbol in symbols if symbol)


def decode_candle(row: list[str]) -> dict[str, Any] | None:
    """Convert an OKX candle row while preserving decimal text exactly."""
    if len(row) < 8:
        return None
    try:
        timestamp_ms = int(row[0])
    except (TypeError, ValueError):
        return None
    return {
        "timestamp": iso_utc(timestamp_ms),
        "timestamp_ms": timestamp_ms,
        "open": row[1],
        "high": row[2],
        "low": row[3],
        "close": row[4],
        "volume": row[5],
        "quote_volume": row[7],
        "confirmed": len(row) < 9 or row[8] == "1",
    }


def fetch_candles(
    symbol: str,
    start_ms: int,
    end_ms: int,
    bar: str,
    limiter: RequestLimiter,
) -> list[dict[str, Any]]:
    cursor = str(end_ms)
    result: dict[int, dict[str, Any]] = {}
    while True:
        payload = http_json(
            "/market/history-candles",
            {"instId": symbol, "bar": bar, "limit": str(PAGE_SIZE), "after": cursor},
            limiter,
        )
        rows = [decode_candle(row) for row in payload.get("data", [])]
        candles = [row for row in rows if row is not None]
        if not candles:
            break
        for candle in candles:
            if start_ms <= candle["timestamp_ms"] <= end_ms:
                result[candle["timestamp_ms"]] = candle
        oldest = min(candle["timestamp_ms"] for candle in candles)
        if oldest <= start_ms or len(candles) < PAGE_SIZE:
            break
        next_cursor = str(oldest)
        if next_cursor == cursor:
            raise RuntimeError(f"pagination cursor did not advance for {symbol}")
        cursor = next_cursor
    return [result[key] for key in sorted(result)]


@dataclass
class ContractResult:
    instrument_id: str
    file: str | None
    candle_count: int
    first_timestamp: str | None
    last_timestamp: str | None
    status: str
    error: str | None = None


def output_filename(symbol: str, bar: str, start: datetime, end: datetime) -> str:
    safe = symbol.replace("-", "_")
    return f"{safe}_{bar}_{start.strftime('%Y%m%dT%H%M%SZ')}_{end.strftime('%Y%m%dT%H%M%SZ')}.jsonl.gz"


def existing_result(symbol: str, filename: str, destination: Path) -> ContractResult | None:
    count = 0
    first: str | None = None
    last: str | None = None
    try:
        with gzip.open(destination, "rt", encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                candle = json.loads(line)
                timestamp = candle.get("timestamp")
                if first is None:
                    first = timestamp
                last = timestamp
                count += 1
        return ContractResult(symbol, filename, count, first, last, "skipped_existing")
    except Exception:
        # A corrupt or truncated prior file is never treated as complete; the
        # caller will fetch into a temporary file and replace it atomically.
        return None


def export_symbol(
    symbol: str,
    output_dir: Path,
    filename: str,
    start_ms: int,
    end_ms: int,
    bar: str,
    limiter: RequestLimiter,
    force: bool,
) -> ContractResult:
    destination = output_dir / filename
    if destination.exists() and not force:
        if existing := existing_result(symbol, filename, destination):
            return existing
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        candles = fetch_candles(symbol, start_ms, end_ms, bar, limiter)
        with gzip.open(temporary, "wt", encoding="utf-8", newline="\n") as stream:
            for candle in candles:
                stream.write(json.dumps(candle, ensure_ascii=False, separators=(",", ":")))
                stream.write("\n")
        os.replace(temporary, destination)
        return ContractResult(
            symbol,
            filename,
            len(candles),
            candles[0]["timestamp"] if candles else None,
            candles[-1]["timestamp"] if candles else None,
            "exported",
        )
    except Exception as error:
        temporary.unlink(missing_ok=True)
        return ContractResult(symbol, None, 0, None, None, "failed", str(error))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export all live OKX USDT linear swap 5m candles for AI analysis")
    parser.add_argument("--days", type=int, default=180, help="number of recent UTC days (default: 180)")
    parser.add_argument("--bar", default="5m", choices=("5m",), help="candle interval (default: 5m)")
    parser.add_argument("--output", type=Path, default=Path("data/market_export"), help="output directory")
    parser.add_argument("--workers", type=int, default=4, help="parallel contract downloads (default: 4)")
    parser.add_argument("--symbols", nargs="+", help="optional instrument IDs; defaults to every live USDT linear swap")
    parser.add_argument("--start", help="UTC ISO-8601 start; overrides --days")
    parser.add_argument("--end", help="UTC ISO-8601 end; defaults to now")
    parser.add_argument("--force", action="store_true", help="redownload files that already exist")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.days <= 0 or args.workers <= 0:
        raise SystemExit("--days and --workers must be positive")
    end = parse_utc(args.end) if args.end else utc_now()
    start = parse_utc(args.start) if args.start else end - timedelta(days=args.days)
    if start >= end:
        raise SystemExit("start must be earlier than end")
    output_dir = args.output.expanduser()
    output_dir.mkdir(parents=True, exist_ok=True)
    limiter = RequestLimiter()
    symbols = sorted(set(args.symbols)) if args.symbols else live_swap_symbols(limiter)
    if not symbols:
        raise SystemExit("no live USDT linear swap contracts found")
    filename_by_symbol = {symbol: output_filename(symbol, args.bar, start, end) for symbol in symbols}
    print(f"exporting {len(symbols)} contracts, {start.isoformat()} .. {end.isoformat()}, bar={args.bar}")
    results: list[ContractResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                export_symbol,
                symbol,
                output_dir,
                filename_by_symbol[symbol],
                int(start.timestamp() * 1000),
                int(end.timestamp() * 1000),
                args.bar,
                limiter,
                args.force,
            )
            for symbol in symbols
        ]
        for index, future in enumerate(as_completed(futures), 1):
            result = future.result()
            results.append(result)
            detail = f"{result.candle_count} candles" if result.candle_count >= 0 else "already present"
            print(f"[{index}/{len(futures)}] {result.instrument_id}: {result.status} ({detail})")
            if result.error:
                print(f"  error: {result.error}")

    results.sort(key=lambda item: item.instrument_id)
    manifest = {
        "schema_version": 1,
        "source": "OKX public REST API /market/history-candles",
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "interval": args.bar,
        "start": start.isoformat().replace("+00:00", "Z"),
        "end": end.isoformat().replace("+00:00", "Z"),
        "candle_fields": ["timestamp", "timestamp_ms", "open", "high", "low", "close", "volume", "quote_volume", "confirmed"],
        "contracts": [asdict(result) for result in results],
        "summary": {
            "requested_contracts": len(results),
            "exported_contracts": sum(result.status == "exported" for result in results),
            "skipped_contracts": sum(result.status == "skipped_existing" for result in results),
            "failed_contracts": sum(result.status == "failed" for result in results),
            "total_candles": sum(max(result.candle_count, 0) for result in results),
        },
    }
    manifest_path = output_dir / "manifest.json"
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_manifest, manifest_path)
    failed = manifest["summary"]["failed_contracts"]
    print(f"manifest: {manifest_path} ({len(results)} contracts, failed={failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
