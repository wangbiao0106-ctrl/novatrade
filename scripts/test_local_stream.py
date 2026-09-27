#!/usr/bin/env python3
"""Regression check for the daemon's Swift 6 / NIO WebSocket callback boundary.

Run `swift build --product okx-locald` first, then this script. `--live`
additionally verifies changing candle frames forwarded from OKX WSS. No account
or order channel is subscribed; daemon state uses a temporary directory.
"""
import argparse
import base64
from datetime import datetime
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import tempfile
import time
import urllib.request


class WebSocket:
    def __init__(self, port):
        self.socket = socket.create_connection(("127.0.0.1", port), timeout=3)
        self.buffer = b""
        key = base64.b64encode(os.urandom(16)).decode()
        self.socket.sendall((
            f"GET /api/v1/stream HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode())
        while b"\r\n\r\n" not in self.buffer:
            self._receive()
        headers, self.buffer = self.buffer.split(b"\r\n\r\n", 1)
        assert headers.startswith(b"HTTP/1.1 101 "), headers.decode()

    def _receive(self):
        chunk = self.socket.recv(65536)
        if not chunk:
            raise AssertionError("Daemon closed socket after upgrade; check for an actor-isolation crash")
        self.buffer += chunk

    def read(self, count):
        while len(self.buffer) < count:
            self._receive()
        data, self.buffer = self.buffer[:count], self.buffer[count:]
        return data

    def send(self, value):
        payload = json.dumps(value).encode()
        mask = os.urandom(4)
        if len(payload) < 126:
            header = bytes((0x81, 0x80 | len(payload)))
        else:
            header = bytes((0x81, 0x80 | 126)) + struct.pack("!H", len(payload))
        self.socket.sendall(header + mask + bytes(c ^ mask[i % 4] for i, c in enumerate(payload)))

    def event(self, timeout=5):
        self.socket.settimeout(timeout)
        first, second = self.read(2)
        length = second & 127
        if length == 126:
            length = struct.unpack("!H", self.read(2))[0]
        elif length == 127:
            length = struct.unpack("!Q", self.read(8))[0]
        mask = self.read(4) if second & 128 else None
        payload = self.read(length)
        if mask:
            payload = bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
        assert first & 15 == 1, f"Expected text frame, got opcode {first & 15}"
        return json.loads(payload)


def verify_live_subscription(ws, instrument, interval, interval_seconds):
    ws.send({"channels": ["candle"], "instrumentID": instrument, "interval": interval})
    first_candle = None
    connecting = False
    count = 0
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        event = ws.event(timeout=max(0.1, deadline - time.monotonic()))
        if event["type"] == "connection" and event.get("instrumentID") == instrument:
            connecting |= event.get("payload") == "okx_wss_connecting"
        if event["type"] != "candle":
            continue
        if not connecting:
            # A previous subscription may already have frames buffered while
            # the server switches its socket. The new connecting event marks
            # the start of the new subscription's checks.
            continue
        assert event.get("instrumentID") == instrument, "Old contract frame leaked into the new subscription"
        candle = json.loads(event["payload"])
        assert {"timestamp", "open", "high", "low", "close", "volume", "confirmed"} <= candle.keys()
        assert all(isinstance(candle[key], (int, float)) for key in ("open", "high", "low", "close", "volume"))
        assert isinstance(candle["confirmed"], bool)
        assert candle["low"] <= min(candle["open"], candle["close"])
        assert candle["high"] >= max(candle["open"], candle["close"])
        timestamp = datetime.fromisoformat(candle["timestamp"].replace("Z", "+00:00")).timestamp()
        assert int(timestamp) % interval_seconds == 0, "Candle timestamp does not match requested interval"
        # OKX can send data before its subscribe acknowledgement. Either
        # validated data or the acknowledgement establishes the upstream.
        count += 1
        if first_candle is not None and candle != first_candle:
            print(f"PASS: {instrument} {interval}: {count} OKX WSS candle frames; OHLC/volume changed")
            return
        first_candle = candle
    raise AssertionError(f"No changing {instrument} {interval} OKX WSS candles within 30 seconds")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="Also receive live OKX candle updates")
    parser.add_argument("--binary", type=Path, default=Path(__file__).resolve().parents[1] / ".build/debug/okx-locald")
    args = parser.parse_args()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory(prefix="nova-stream-test-") as state, tempfile.TemporaryFile() as log:
        env = dict(os.environ, OKX_LOCALD_PORT=str(port), OKX_LOCALD_STATE_DIR=state)
        process = subprocess.Popen([str(args.binary)], env=env, stdout=log, stderr=log)
        ws = None
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            deadline = time.monotonic() + 10
            while True:
                assert process.poll() is None, f"Daemon exited before listening: {process.returncode}"
                try:
                    with opener.open(f"http://127.0.0.1:{port}/health", timeout=0.5) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if time.monotonic() > deadline:
                        raise AssertionError("Daemon did not become healthy within 10 seconds")
                    time.sleep(0.1)
            ws = WebSocket(port)
            assert ws.event()["payload"] == "connected"
            ws.send({"channels": [], "instrumentID": "BTC-USDT-SWAP", "interval": "1m"})
            assert ws.event()["payload"] == "subscribed:"
            assert process.poll() is None
            print("PASS: external client receives connected + subscribed frames; daemon remains alive")
            if args.live:
                verify_live_subscription(ws, "BTC-USDT-SWAP", "1m", 60)
                verify_live_subscription(ws, "ETH-USDT-SWAP", "5m", 300)
                assert process.poll() is None
                print("PASS: same socket switches contract/interval without mixing candle streams")
        except BaseException:
            log.seek(0)
            print(log.read().decode(errors="replace"))
            raise
        finally:
            if ws:
                ws.socket.close()
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    main()
