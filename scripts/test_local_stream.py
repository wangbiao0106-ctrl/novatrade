#!/usr/bin/env python3
"""Smoke test for the direct FastAPI backend."""

import argparse
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time
import urllib.request


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--script", type=Path, default=Path(__file__).resolve().parents[1] / "backend/main.py")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="nova-fastapi-test-") as state:
        port = "18787"
        token = "fastapi-test-token"
        environment = dict(os.environ, NOVATRADE_STATE_DIR=state, NOVATRADE_AUTH_TOKEN=token, NOVATRADE_FASTAPI_PORT=port)
        process = subprocess.Popen(["python3", str(args.script)], env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            request = urllib.request.Request(f"http://127.0.0.1:{port}/health", headers={"Authorization": f"Bearer {token}"})
            deadline = time.monotonic() + 10
            while True:
                try:
                    with urllib.request.urlopen(request, timeout=0.5) as response:
                        assert response.status == 200
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise AssertionError("FastAPI backend did not become healthy")
                    time.sleep(0.1)
            print("PASS: FastAPI backend health and bearer authentication")
        finally:
            if process.poll() is None:
                process.send_signal(signal.SIGTERM)
                process.wait(timeout=5)


if __name__ == "__main__":
    main()
