#!/usr/bin/env python3
"""Verify ten-minute scan cadence and migration without changing live state."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "backend"))

from ai_schema import AIConfig  # noqa: E402
from ai_worker import AIWorker  # noqa: E402


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now


class ScanScheduleTests(unittest.TestCase):
    @staticmethod
    def _load_worker(root: Path) -> AIWorker:
        # Python 3.9 binds asyncio.Event during construction, so match the
        # production startup path by constructing workers inside a loop.
        async def load() -> AIWorker:
            return AIWorker(state_dir=root)

        return asyncio.run(load())

    def _scan_starts(
        self, duration: float, *, interval: float = 600,
        updated_interval: float | None = None, update_at: float = 150,
    ) -> tuple[list[float], list[float]]:
        """Run two starts using a virtual clock, including a sleeping update."""
        async def run() -> tuple[list[float], list[float]]:
            clock = _Clock()
            starts: list[float] = []
            waits: list[float] = []
            updated = False
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(
                    config=AIConfig(enabled=True, mode="shadow", decisionIntervalSeconds=interval),
                    state_dir=Path(directory),
                )

                async def scan() -> None:
                    starts.append(clock.time())
                    if len(starts) == 2:
                        worker._stop.set()
                    else:
                        clock.now += duration

                async def wait_for(awaitable, *, timeout: float):
                    nonlocal updated
                    # The virtual wait consumes the coroutine without binding
                    # asyncio.Event to our minimal monotonic clock.
                    awaitable.close()
                    waits.append(timeout)
                    if updated_interval is not None and not updated:
                        self.assertLess(clock.time(), update_at)
                        self.assertLess(update_at, clock.time() + timeout)
                        clock.now = update_at
                        worker.update_config({"decisionIntervalSeconds": updated_interval}, clear_halt=False)
                        self.assertTrue(worker._schedule_changed.is_set())
                        updated = True
                        return True
                    clock.now += timeout
                    raise asyncio.TimeoutError

                worker.run_once = scan
                with patch("ai_worker.asyncio.get_running_loop", return_value=clock), patch(
                    "ai_worker.asyncio.wait_for", new=wait_for,
                ):
                    await worker._loop()
            return starts, waits

        return asyncio.run(run())

    def test_collection_and_model_runtime_count_toward_the_scan_period(self) -> None:
        starts, waits = self._scan_starts(90)
        self.assertEqual(starts, [0, 600])
        self.assertEqual(waits, [510])

    def test_overruns_skip_missed_scan_slots_without_catch_up_calls(self) -> None:
        for duration, next_start, wait in ((650, 1200, 550), (1300, 1800, 500)):
            with self.subTest(duration=duration):
                starts, waits = self._scan_starts(duration)
                self.assertEqual(starts, [0, next_start])
                self.assertEqual(waits, [wait])

    def test_finishing_exactly_on_a_slot_does_not_skip_that_slot(self) -> None:
        starts, waits = self._scan_starts(600)
        self.assertEqual(starts, [0, 600])
        self.assertEqual(waits, [])

    def test_sleeping_interval_increase_recalculates_from_the_previous_start(self) -> None:
        starts, waits = self._scan_starts(90, updated_interval=1200)
        self.assertEqual(starts, [0, 1200])
        self.assertEqual(waits, [510, 1050])

    def test_sleeping_interval_decrease_recalculates_without_an_immediate_scan(self) -> None:
        starts, waits = self._scan_starts(90, interval=1200, updated_interval=600)
        self.assertEqual(starts, [0, 600])
        self.assertEqual(waits, [1110, 450])

    def test_stop_interrupts_a_ten_minute_sleep(self) -> None:
        async def run() -> None:
            scanned = asyncio.Event()
            starts = 0
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(config=AIConfig(enabled=True, mode="shadow"), state_dir=Path(directory))

                async def scan() -> None:
                    nonlocal starts
                    starts += 1
                    scanned.set()

                worker.run_once = scan
                await worker.start()
                await asyncio.wait_for(scanned.wait(), timeout=1)
                await asyncio.wait_for(worker.stop(), timeout=1)
                self.assertIsNone(worker._task)
                self.assertTrue(worker._stop.is_set())
                self.assertEqual(starts, 1)

        asyncio.run(run())

    def test_a_halted_scan_stops_before_scheduling_another_slot(self) -> None:
        async def run() -> None:
            with tempfile.TemporaryDirectory() as directory:
                worker = AIWorker(config=AIConfig(enabled=True, mode="shadow"), state_dir=Path(directory))

                async def scan() -> None:
                    worker.status = replace(worker.status, state="halted", consecutiveFailures=3)

                worker.run_once = scan
                with patch("ai_worker.asyncio.wait_for", side_effect=AssertionError("halted worker must not wait")):
                    await worker._loop()

        asyncio.run(run())

    def test_new_and_missing_field_defaults_use_ten_minutes(self) -> None:
        self.assertEqual(AIConfig().decisionIntervalSeconds, 600)
        self.assertEqual(AIConfig.from_dict({}).decisionIntervalSeconds, 600)

    def test_old_default_migration_preserves_disabled_and_halted_state(self) -> None:
        for interval in (30, 30.0):
            for enabled, mode, state, failures in (
                (False, "disabled", "stopped", 2),
                (False, "halted", "halted", 3),
                (True, "paper-active", "halted", 3),
            ):
                with self.subTest(interval=interval, enabled=enabled, mode=mode, state=state):
                    with tempfile.TemporaryDirectory() as directory:
                        root = Path(directory)
                        config = AIConfig(
                            enabled=enabled, mode=mode, allowedInstruments=("ETH-USDT-SWAP",),
                            decisionIntervalSeconds=interval, marginPerOrderUSD=250, maxLeverage=3,
                        ).to_dict()
                        (root / "ai-config.json").write_text(json.dumps(config), encoding="utf-8")
                        observed_at = "2026-10-09T11:00:00Z"
                        (root / "ai-state.json").write_text(json.dumps({
                            "state": state, "consecutiveFailures": failures,
                            "observedInstruments": ["ETH-USDT-SWAP"], "observationUpdatedAt": observed_at,
                        }), encoding="utf-8")
                        worker = self._load_worker(root)
                        migrated = dict(config, decisionIntervalSeconds=600.0)
                        self.assertEqual(worker.get_config(), migrated)
                        self.assertEqual(json.loads((root / "ai-config.json").read_text()), migrated)
                        self.assertEqual(worker.status.state, state)
                        self.assertEqual(worker.status.consecutiveFailures, failures)
                        self.assertEqual(worker.observed_instruments, ["ETH-USDT-SWAP"])
                        self.assertEqual(worker.observation_updated_at, observed_at)
                        self.assertEqual(worker.status.enabled, False if state == "halted" else enabled)
                        self.assertIsNone(worker._task)
                        restarted = self._load_worker(root)
                        self.assertEqual(restarted.get_config(), migrated)
                        self.assertEqual(restarted.status.state, state)
                        self.assertEqual(restarted.status.consecutiveFailures, failures)

    def test_custom_scan_interval_is_not_overwritten_by_default_migration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AIConfig(enabled=False, mode="disabled", decisionIntervalSeconds=100).to_dict()
            (root / "ai-config.json").write_text(json.dumps(config), encoding="utf-8")
            worker = self._load_worker(root)
            self.assertEqual(worker.get_config(), config)
            self.assertEqual(json.loads((root / "ai-config.json").read_text()), config)
            self.assertIsNone(worker._task)


if __name__ == "__main__":
    unittest.main()
