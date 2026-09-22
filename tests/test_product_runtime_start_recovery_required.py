from __future__ import annotations

import unittest
from pathlib import Path
from types import SimpleNamespace

from autosport.continuous_session import SessionState
from autosport.product_runtime import (
    AutonomousProductRuntime,
    ProductCompositionError,
    ProductCompositionManifest,
)


class _Collector:
    def __init__(self) -> None:
        self.stopped = True
        self.stop_error: Exception | None = None
        self.resume_calls = 0
        self.stop_calls: list[str] = []

    def status(self) -> dict[str, object]:
        return {
            "stopped_at": "2026-09-22T13:52:00+00:00" if self.stopped else None,
            "stop_reason": "stopped" if self.stopped else None,
        }

    def resume(self) -> None:
        self.resume_calls += 1
        self.stopped = False

    def stop(self, reason: str) -> None:
        self.stop_calls.append(reason)
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped = True


class _Coordinator:
    def __init__(self) -> None:
        self.state = SessionState.STOPPED
        self.resume_error: Exception | None = None
        self.stop_calls: list[str] = []
        self.tick_calls = 0

    def status(self):
        return SimpleNamespace(state=self.state)

    def resume(self) -> None:
        if self.resume_error is not None:
            raise self.resume_error
        self.state = SessionState.RUNNING

    def pause(self) -> None:
        self.state = SessionState.PAUSED

    def stop(self, reason: str) -> None:
        self.stop_calls.append(reason)
        self.state = SessionState.STOPPED

    def tick(self):
        self.tick_calls += 1
        return SimpleNamespace(cycle_index=self.tick_calls)


class _NoopStore:
    def close(self) -> None:
        pass


class _Lease:
    authority_active = True

    def release(self) -> None:
        self.authority_active = False


def _runtime() -> tuple[AutonomousProductRuntime, _Collector, _Coordinator]:
    collector = _Collector()
    coordinator = _Coordinator()
    runtime = AutonomousProductRuntime(
        workspace=Path("unused"),
        manifest=ProductCompositionManifest("provider-a", "100"),
        coordinator=coordinator,
        collector=collector,
        market_store=_NoopStore(),
        lifecycle=object(),
        mirror=object(),
        invalidations=object(),
        dependencies=object(),
        _runtime_lease=_Lease(),
    )
    return runtime, collector, coordinator


class ProductRuntimeStartRecoveryRequiredTests(unittest.TestCase):
    def test_failed_start_and_failed_compensation_is_explicit_recovery_required(self) -> None:
        runtime, collector, coordinator = _runtime()
        coordinator.resume_error = RuntimeError("session resume failed")
        collector.stop_error = RuntimeError("collector compensation failed")

        with self.assertRaisesRegex(ProductCompositionError, "RECOVERY_REQUIRED") as caught:
            runtime.start()

        self.assertIsInstance(caught.exception.__cause__, RuntimeError)
        self.assertIn("session resume failed", str(caught.exception.__cause__))
        self.assertFalse(collector.stopped)
        self.assertEqual(coordinator.state, SessionState.STOPPED)
        self.assertEqual(collector.stop_calls, ["runtime_start_failed"])
        self.assertEqual(coordinator.stop_calls, ["runtime_start_failed"])

        for action in (runtime.status, runtime.start, runtime.pause, runtime.tick):
            with self.assertRaisesRegex(ProductCompositionError, "RECOVERY_REQUIRED"):
                action()
        self.assertEqual(collector.resume_calls, 1)
        self.assertEqual(coordinator.tick_calls, 0)

        collector.stop_error = None
        self.assertEqual(
            runtime.stop("operator_recovery_stop").state,
            SessionState.STOPPED,
        )
        self.assertEqual(runtime.status().state, SessionState.STOPPED)


if __name__ == "__main__":
    unittest.main()
