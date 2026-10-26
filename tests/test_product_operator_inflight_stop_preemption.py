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
        self.stopped = False
        self._durable_stop_reason: str | None = None

    def status(self) -> dict[str, object]:
        return {
            "stopped_at": "2026-09-29T05:15:00+00:00" if self.stopped else None,
            "stop_reason": self._durable_stop_reason if self.stopped else None,
        }

    def resume(self) -> None:
        self.stopped = False
        self._durable_stop_reason = None

    def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)
        self.stopped = True
        self._durable_stop_reason = reason
        self.stop_called.set()


class _MarketStore:
    def close(self) -> None:
        return None


class _RuntimeLease:
    authority_active = True

    def release(self) -> None:
        self.authority_active = False


class _StartTransitionStore:
    def pending(self):
        return None


class ProductOperatorInflightStopPreemptionTests(unittest.TestCase):
    def test_stop_intent_preempts_tick_but_durable_stop_waits_for_quiescence(self) -> None:
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
                _runtime_lease=_RuntimeLease(),  # type: ignore[arg-type]
                _start_transition_store=_StartTransitionStore(),  # type: ignore[arg-type]
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
                # ProductOperatorController wires its event-backed request object into
                # the collector's existing cancellation port. Intent must be observable
                # promptly even though runtime.status()/stop() are correctly waiting on
                # AutonomousProductRuntime._operation_fence behind this active tick.
                stop_intent_preempted_tick = collector.stop_requested.wait(1)  # type: ignore[attr-defined]
                tick_was_still_inflight = not tick_done.is_set()
                durable_stop_waited_for_quiescence = not collector.stop_called.is_set()
                stop_reason = collector.stop_reason()  # type: ignore[attr-defined]
            finally:
                coordinator.release_tick.set()
                tick_thread.join(2)
                stop_thread.join(2)

            self.assertTrue(
                stop_intent_preempted_tick,
                "operator STOP intent was serialized behind an in-flight runtime tick",
            )
            self.assertTrue(
                tick_was_still_inflight,
                "STOP intent must become observable before the blocked tick completes",
            )
            self.assertTrue(
                durable_stop_waited_for_quiescence,
                "durable collector STOP must not race an active runtime mutation",
            )
            self.assertEqual(stop_reason, "operator_requested_stop")
            self.assertFalse(tick_thread.is_alive())
            self.assertFalse(stop_thread.is_alive())
            self.assertTrue(stop_done.is_set())
            self.assertEqual(thread_errors, [])
            self.assertEqual(collector.stop_reasons, ["operator_requested_stop"])
            self.assertEqual(coordinator.stop_reasons, ["operator_requested_stop"])
            self.assertFalse(collector.stop_requested())  # type: ignore[attr-defined]


if __name__ == "__main__":
    unittest.main()
