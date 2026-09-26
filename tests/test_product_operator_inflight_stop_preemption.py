from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path

from autosport.product_operator import ProductOperatorController
from autosport.product_runtime import AutonomousProductRuntime, ProductCompositionManifest


@dataclass(frozen=True, slots=True)
class _Status:
    state: str
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class _Tick:
    cycle_index: int


class _BlockingCoordinator:
    def __init__(self) -> None:
        self.state = "RUNNING"
        self.stop_reason: str | None = None
        self.stop_reasons: list[str] = []
        self.cycle_index = 0
        self.entered_tick = threading.Event()
        self.release_tick = threading.Event()

    def status(self) -> _Status:
        return _Status(state=self.state, stop_reason=self.stop_reason)

    def resume(self) -> None:
        self.state = "RUNNING"
        self.stop_reason = None

    def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)
        self.state = "STOPPED"
        self.stop_reason = reason

    def tick(self) -> _Tick:
        if self.state != "RUNNING":
            raise AssertionError("coordinator must be RUNNING before tick")
        self.entered_tick.set()
        if not self.release_tick.wait(5):
            raise AssertionError("test cleanup failed to release blocked tick")
        self.cycle_index += 1
        return _Tick(cycle_index=self.cycle_index)


class _Collector:
    def __init__(self) -> None:
        self.stop_called = threading.Event()
        self.stop_reasons: list[str] = []

    def resume(self) -> None:
        return None

    def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)
        self.stop_called.set()


class _MarketStore:
    def close(self) -> None:
        return None


class ProductOperatorInflightStopPreemptionTests(unittest.TestCase):
    def test_stop_reaches_canonical_runtime_before_blocked_tick_is_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            collector = _Collector()
            coordinator = _BlockingCoordinator()
            runtime = AutonomousProductRuntime(
                workspace=Path(directory),
                manifest=ProductCompositionManifest(
                    source_id="provider-a",
                    initial_bankroll="100",
                ),
                coordinator=coordinator,  # type: ignore[arg-type]
                collector=collector,  # type: ignore[arg-type]
                market_store=_MarketStore(),  # type: ignore[arg-type]
                lifecycle=object(),  # type: ignore[arg-type]
                mirror=object(),  # type: ignore[arg-type]
                invalidations=object(),  # type: ignore[arg-type]
                dependencies=object(),  # type: ignore[arg-type]
            )
            operator = ProductOperatorController(runtime)

            tick_done = threading.Event()
            stop_done = threading.Event()
            thread_errors: list[BaseException] = []

            def run_tick() -> None:
                try:
                    operator.tick()
                except BaseException as exc:
                    thread_errors.append(exc)
                finally:
                    tick_done.set()

            def request_stop() -> None:
                try:
                    operator.stop("operator_requested_stop")
                except BaseException as exc:
                    thread_errors.append(exc)
                finally:
                    stop_done.set()

            tick_thread = threading.Thread(target=run_tick, name="blocked-product-tick")
            stop_thread = threading.Thread(target=request_stop, name="concurrent-product-stop")
            tick_thread.start()
            self.assertTrue(
                coordinator.entered_tick.wait(1),
                "tick never entered the blocking canonical coordinator",
            )
            stop_thread.start()

            try:
                stop_preempted_tick = collector.stop_called.wait(1)
                tick_was_still_inflight = not tick_done.is_set()
            finally:
                coordinator.release_tick.set()
                tick_thread.join(2)
                stop_thread.join(2)

            self.assertTrue(
                stop_preempted_tick,
                "operator STOP was serialized behind an in-flight runtime tick",
            )
            self.assertTrue(
                tick_was_still_inflight,
                "STOP must become observable before the blocked tick completes",
            )
            self.assertFalse(tick_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertTrue(stop_done.is_set())
            self.assertEqual(thread_errors, [])
            self.assertEqual(collector.stop_reasons, ["operator_requested_stop"])
            self.assertEqual(coordinator.stop_reasons, ["operator_requested_stop"])


if __name__ == "__main__":
    unittest.main()
