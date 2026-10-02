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
import math
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
BAR_MILLISECONDS = {"5m": 5 * 60 * 1000}


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


def existing_result(symbol: str, filename: str, destination: Path,
                    start_ms: int, end_ms: int, bar: str) -> ContractResult | None:
    """Return a skip result only when an existing export covers the request.

    A syntactically valid but truncated gzip file used to be treated as
    complete because only its row count was inspected.  That silently removed
    the missing history from every subsequent backtest.  Check boundaries,
    continuity and values before allowing the skip path: the last bar of a
    fresh export is still open (`confirmed` false, historically stored as the
    string "0"), and research loaders drop unconfirmed bars, so treating such
    a file as complete would silently lose that bar forever.
    """
    try:
        candles = read_candles(destination)
        timestamps = sorted(candles)
        interval = BAR_MILLISECONDS[bar]
        expected = list(range(timestamps[0], timestamps[-1] + interval, interval)) if timestamps else []
        structurally_valid = all(is_complete_candle(candles[timestamp]) for timestamp in timestamps)
        if (not timestamps or timestamps[0] > start_ms or
                timestamps[-1] < end_ms - interval or timestamps != expected or
                not structurally_valid):
            return None
        return ContractResult(
            symbol,
            filename,
            len(timestamps),
            candles[timestamps[0]]["timestamp"] if timestamps else None,
            candles[timestamps[-1]]["timestamp"] if timestamps else None,
            "skipped_existing",
        )
    except Exception:
        # A corrupt or truncated prior file is never treated as complete; the
        # caller will fetch into a temporary file and replace it atomically.
        return None


# A candle is usable only when it is closed and carries finite positive
# prices.  OKX writes `confirmed` as the string "1"/"0" and trims trailing
# zeros, so a text field can hold "0.0" or "" where a number is expected.
_STATUS_WORDS = {"": 0.0, "0": 0.0, "false": 0.0, "no": 0.0, "n": 0.0,
                 "1": 1.0, "true": 1.0, "yes": 1.0, "y": 1.0}
_REQUIRED_FIELDS = ("timestamp", "open", "high", "low", "close", "quote_volume")


def _finite(value: Any) -> float | None:
    """Return a finite float for an OKX numeric field, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        text = value.strip()
        if text in _STATUS_WORDS:
            return _STATUS_WORDS[text]
        try:
            number = float(text)
        except ValueError:
            return None
    else:
        return None
    return number if math.isfinite(number) else None


def is_complete_candle(candle: dict[str, Any]) -> bool:
    """True when a stored candle is closed and internally valid."""
    if not isinstance(candle, dict):
        return False
    for field in _REQUIRED_FIELDS:
        if field not in candle:
            return False
    if _finite(candle.get("timestamp_ms")) is None:
        return False
    confirmed = candle.get("confirmed")
    closed = confirmed if isinstance(confirmed, bool) else _finite(confirmed) == 1.0
    if not closed:
        return False
    prices = {field: _finite(candle.get(field)) for field in ("open", "high", "low", "close", "quote_volume")}
    if any(value is None for value in prices.values()):
        return False
    open_, high, low, close = (prices[field] for field in ("open", "high", "low", "close"))
    if min(open_, high, low, close) <= 0:
        return False
    # An inverted range cannot come from OKX; refuse to treat such a file as
    # a valid backtest input instead of silently trading on it.
    return high >= max(open_, close, low) and low <= min(open_, close, high)


def read_candles(path: Path) -> dict[int, dict[str, Any]]:
    candles: dict[int, dict[str, Any]] = {}
    with gzip.open(path, "rt", encoding="utf-8") as stream:
        for line in stream:
            if not line.strip():
                continue
            candle = json.loads(line)
            timestamp_ms = int(candle["timestamp_ms"])
            candles[timestamp_ms] = candle
    return candles


def export_symbol(
    symbol: str,
    output_dir: Path,
    filename: str,
    start_ms: int,
    end_ms: int,
    bar: str,
    limiter: RequestLimiter,
    force: bool,
    update_from: Path | None = None,
) -> ContractResult:
    destination = output_dir / filename
    if update_from is None and destination.exists() and not force:
        if existing := existing_result(symbol, filename, destination, start_ms, end_ms, bar):
            return existing
    temporary = destination.with_suffix(destination.suffix + ".part")
    try:
        previous: dict[int, dict[str, Any]] = {}
        fetch_start_ms = start_ms
        if update_from is not None and update_from.exists():
            previous = read_candles(update_from)
            if previous:
                interval_ms = BAR_MILLISECONDS[bar]
                fetch_start_ms = max(start_ms, max(previous) - interval_ms)
        fetched = fetch_candles(symbol, fetch_start_ms, end_ms, bar, limiter)
        merged = previous
        merged.update({candle["timestamp_ms"]: candle for candle in fetched})
        candles = [merged[key] for key in sorted(merged) if start_ms <= key <= end_ms]
        # Extending an earlier export re-fetches its last bar, but a run that
        # ends while that bar is still forming must not persist the open bar:
        # the next run would skip the file and the bar would stay unconfirmed
        # forever. Dropping it makes the boundary check re-fetch it.
        if candles and not is_complete_candle(candles[-1]):
            candles.pop()
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
            "updated" if update_from is not None else "exported",
        )
    except Exception as error:
        temporary.unlink(missing_ok=True)
        return ContractResult(symbol, None, 0, None, None, "failed", str(error))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export all live OKX USDT linear swap 5m candles for AI analysis")
    parser.add_argument("--days", type=int, default=180, help="number of recent UTC days (default: 180)")
    parser.add_argument("--bar", default="5m", choices=("5m",), help="candle interval (default: 5m)")
    parser.add_argument("--output", type=Path, default=Path("data/kline/okx/swap/5m"), help="output directory")
    parser.add_argument("--workers", type=int, default=4, help="parallel contract downloads (default: 4)")
    parser.add_argument("--symbols", nargs="+", help="optional instrument IDs; defaults to every live USDT linear swap")
    parser.add_argument("--start", help="UTC ISO-8601 start; overrides --days")
    parser.add_argument("--end", help="UTC ISO-8601 end; defaults to now")
    parser.add_argument("--update", action="store_true", help="extend the existing manifest to the latest time without redownloading history")
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
    manifest_path = output_dir / "manifest.json"
    previous_manifest: dict[str, Any] | None = None
    if args.update:
        if not manifest_path.exists():
            raise SystemExit(f"--update requires an existing manifest: {manifest_path}")
        previous_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if args.start:
            raise SystemExit("--update uses the existing manifest start; remove --start")
        if args.symbols:
            raise SystemExit("--update refreshes the complete manifest; remove --symbols")
    limiter = RequestLimiter()
    symbols = sorted(set(args.symbols)) if args.symbols else live_swap_symbols(limiter)
    if not symbols:
        raise SystemExit("no live USDT linear swap contracts found")
    if args.update:
        start = parse_utc(previous_manifest["start"])
    if start >= end:
        raise SystemExit("update end must be later than the existing manifest start")
    previous_files = {
        entry["instrument_id"]: entry["file"]
        for entry in (previous_manifest or {}).get("contracts", [])
        if entry.get("instrument_id") and entry.get("file")
    }
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
                output_dir / previous_files[symbol] if args.update and symbol in previous_files else None,
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
            "exported_contracts": sum(result.status in {"exported", "updated"} for result in results),
            "updated_contracts": sum(result.status == "updated" for result in results),
            "skipped_contracts": sum(result.status == "skipped_existing" for result in results),
            "failed_contracts": sum(result.status == "failed" for result in results),
            "total_candles": sum(max(result.candle_count, 0) for result in results),
        },
    }
    temporary_manifest = manifest_path.with_suffix(".json.part")
    temporary_manifest.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary_manifest, manifest_path)
    failed = manifest["summary"]["failed_contracts"]
    if args.update and not failed:
        current_files = {result.file for result in results if result.file}
        old_files = {entry.get("file") for entry in (previous_manifest or {}).get("contracts", [])}
        for old_file in old_files - current_files:
            if old_file:
                (output_dir / old_file).unlink(missing_ok=True)
    print(f"manifest: {manifest_path} ({len(results)} contracts, failed={failed})")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
