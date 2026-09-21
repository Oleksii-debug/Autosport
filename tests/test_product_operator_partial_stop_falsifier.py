from __future__ import annotations

import tempfile
import unittest
from dataclasses import dataclass
from pathlib import Path

from autosport.product_operator import ProductOperatorController, ProductOperatorError
from autosport.product_runtime import AutonomousProductRuntime, ProductCompositionManifest


@dataclass(frozen=True, slots=True)
class _Status:
    state: str
    stop_reason: str | None


class _Collector:
    def __init__(self) -> None:
        self.state = "RUNNING"
        self.resume_calls = 0
        self.stop_reasons: list[str] = []

    def resume(self) -> None:
        self.resume_calls += 1
        self.state = "RUNNING"

    def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)
        self.state = "STOPPED"


class _Coordinator:
    def __init__(self) -> None:
        self.state = "RUNNING"
        self.stop_reason: str | None = None
        self.resume_calls = 0
        self.stop_reasons: list[str] = []
        self.stop_error: Exception | None = RuntimeError(
            "injected coordinator stop failure"
        )

    def resume(self) -> None:
        self.resume_calls += 1
        self.state = "RUNNING"
        self.stop_reason = None

    def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)
        if self.stop_error is not None:
            raise self.stop_error
        self.state = "STOPPED"
        self.stop_reason = reason

    def status(self) -> _Status:
        return _Status(state=self.state, stop_reason=self.stop_reason)

    def tick(self) -> object:
        raise AssertionError("tick is outside this falsifier")


class _MarketStore:
    def close(self) -> None:
        return None


class PartialStopSplitStateFalsifiers(unittest.TestCase):
    def _runtime(
        self, directory: str
    ) -> tuple[AutonomousProductRuntime, _Collector, _Coordinator]:
        collector = _Collector()
        coordinator = _Coordinator()
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
        return runtime, collector, coordinator

    def _inject_partial_stop(
        self,
        operator: ProductOperatorController,
        collector: _Collector,
        coordinator: _Coordinator,
    ) -> None:
        with self.assertRaisesRegex(RuntimeError, "injected coordinator stop failure"):
            operator.stop("operator_requested_stop")

        self.assertEqual(collector.state, "STOPPED")
        self.assertEqual(coordinator.state, "RUNNING")
        self.assertEqual(
            collector.stop_reasons,
            ["operator_requested_stop"],
        )
        self.assertEqual(
            coordinator.stop_reasons,
            ["operator_requested_stop"],
        )

    def test_status_must_not_certify_running_after_partial_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, collector, coordinator = self._runtime(directory)
            operator = ProductOperatorController(runtime)
            self._inject_partial_stop(operator, collector, coordinator)

            try:
                snapshot = operator.status()
            except ProductOperatorError:
                return

            self.assertNotEqual(
                snapshot.state,
                "RUNNING",
                "operator status must fail closed when collector and coordinator "
                "durable lifecycle state disagree",
            )

    def test_start_must_not_return_running_while_collector_remains_stopped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, collector, coordinator = self._runtime(directory)
            operator = ProductOperatorController(runtime)
            self._inject_partial_stop(operator, collector, coordinator)

            try:
                status = operator.start()
            except ProductOperatorError:
                return

            if status.state == "RUNNING":
                self.assertEqual(
                    collector.state,
                    "RUNNING",
                    "RUNNING may be returned only after collector state is coherent",
                )
                self.assertEqual(coordinator.state, "RUNNING")


if __name__ == "__main__":
    unittest.main()
