from __future__ import annotations

import tempfile
import threading
import unittest
from dataclasses import dataclass
from pathlib import Path

from autosport.product_operator import ProductOperatorController, ProductOperatorError
from autosport.product_runtime import AutonomousProductRuntime, ProductCompositionManifest


@dataclass(frozen=True, slots=True)
class _Status:
    state: str
    stop_reason: str | None


@dataclass(frozen=True, slots=True)
class _Tick:
    cycle_index: int


class _Collector:
    def __init__(self) -> None:
        self.resume_calls = 0
        self.stop_reasons: list[str] = []

    def resume(self) -> None:
        self.resume_calls += 1

    def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)


class _Coordinator:
    def __init__(self) -> None:
        self.resume_calls = 0
        self.stop_reasons: list[str] = []
        self.cycle_index = 0
        self.state = "STOPPED"
        self.stop_reason: str | None = None

    def resume(self) -> None:
        self.resume_calls += 1
        self.state = "RUNNING"
        self.stop_reason = None

    def stop(self, reason: str) -> None:
        self.stop_reasons.append(reason)
        self.state = "STOPPED"
        self.stop_reason = reason

    def status(self) -> _Status:
        return _Status(state=self.state, stop_reason=self.stop_reason)

    def tick(self) -> _Tick:
        if self.state != "RUNNING":
            raise AssertionError("canonical coordinator must be running before tick")
        self.cycle_index += 1
        return _Tick(cycle_index=self.cycle_index)


class _MarketStore:
    def __init__(self) -> None:
        self.close_calls = 0

    def close(self) -> None:
        self.close_calls += 1


class ProductOperatorControllerTests(unittest.TestCase):
    def _runtime(self, directory: str) -> tuple[
        AutonomousProductRuntime,
        _Collector,
        _Coordinator,
        _MarketStore,
    ]:
        collector = _Collector()
        coordinator = _Coordinator()
        market_store = _MarketStore()
        runtime = AutonomousProductRuntime(
            workspace=Path(directory),
            manifest=ProductCompositionManifest(
                source_id="provider-a",
                initial_bankroll="100",
            ),
            coordinator=coordinator,  # type: ignore[arg-type]
            collector=collector,  # type: ignore[arg-type]
            market_store=market_store,  # type: ignore[arg-type]
            lifecycle=object(),  # type: ignore[arg-type]
            mirror=object(),  # type: ignore[arg-type]
            invalidations=object(),  # type: ignore[arg-type]
            dependencies=object(),  # type: ignore[arg-type]
        )
        return runtime, collector, coordinator, market_store

    def test_lifecycle_reuses_one_canonical_runtime_without_implicit_loop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, collector, coordinator, market_store = self._runtime(directory)
            operator = ProductOperatorController(runtime)

            self.assertEqual(operator.status().state, "STOPPED")
            first_status = operator.start()
            self.assertEqual(first_status.state, "RUNNING")
            self.assertEqual(collector.resume_calls, 1)
            self.assertEqual(coordinator.resume_calls, 1)

            self.assertEqual(operator.start().state, "RUNNING")
            self.assertEqual(collector.resume_calls, 1)
            self.assertEqual(coordinator.resume_calls, 1)

            self.assertEqual(operator.tick().cycle_index, 1)
            running = operator.status()
            self.assertEqual(running.state, "RUNNING")
            self.assertEqual(running.controller_tick_count, 1)

            stopped = operator.stop("operator_requested_stop")
            self.assertEqual(stopped.state, "STOPPED")
            self.assertEqual(collector.stop_reasons, ["operator_requested_stop"])
            self.assertEqual(coordinator.stop_reasons, ["operator_requested_stop"])
            self.assertEqual(operator.status().state, "STOPPED")

            operator.stop("operator_requested_stop")
            self.assertEqual(collector.stop_reasons, ["operator_requested_stop"])
            self.assertEqual(coordinator.stop_reasons, ["operator_requested_stop"])

            operator.start()
            self.assertEqual(collector.resume_calls, 2)
            self.assertEqual(operator.tick().cycle_index, 2)
            self.assertEqual(operator.status().controller_tick_count, 2)

            operator.stop()
            operator.close()
            operator.close()
            self.assertEqual(market_store.close_calls, 1)
            self.assertEqual(operator.status().state, "CLOSED")

    def test_fresh_controller_preserves_canonical_stopped_truth_until_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, collector, coordinator, _ = self._runtime(directory)
            coordinator.stop_reason = "durable_prior_stop"

            operator = ProductOperatorController(runtime)

            snapshot = operator.status()
            self.assertEqual(snapshot.state, "STOPPED")
            self.assertEqual(snapshot.canonical_status.state, "STOPPED")
            self.assertEqual(snapshot.canonical_status.stop_reason, "durable_prior_stop")
            self.assertEqual(collector.resume_calls, 0)
            self.assertEqual(coordinator.resume_calls, 0)

            started = operator.start()
            self.assertEqual(started.state, "RUNNING")
            self.assertEqual(operator.status().state, "RUNNING")
            self.assertEqual(collector.resume_calls, 1)
            self.assertEqual(coordinator.resume_calls, 1)
            operator.stop()
            operator.close()

    def test_attach_to_running_runtime_reuses_canonical_state_and_persists_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, collector, coordinator, _ = self._runtime(directory)
            runtime.start()

            operator = ProductOperatorController(runtime)

            self.assertEqual(operator.status().state, "RUNNING")
            self.assertEqual(operator.start().state, "RUNNING")
            self.assertEqual(collector.resume_calls, 1)
            self.assertEqual(coordinator.resume_calls, 1)
            self.assertEqual(operator.tick().cycle_index, 1)

            stopped = operator.stop("operator_requested_stop")
            self.assertEqual(stopped.state, "STOPPED")
            self.assertEqual(collector.stop_reasons, ["operator_requested_stop"])
            self.assertEqual(coordinator.stop_reasons, ["operator_requested_stop"])
            self.assertEqual(operator.status().state, "STOPPED")
            operator.close()

    def test_external_start_after_attach_is_reconciled_before_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, collector, coordinator, _ = self._runtime(directory)
            operator = ProductOperatorController(runtime)
            runtime.start()

            self.assertEqual(operator.status().state, "RUNNING")
            stopped = operator.stop("external_surface_stop")

            self.assertEqual(stopped.state, "STOPPED")
            self.assertEqual(collector.stop_reasons, ["external_surface_stop"])
            self.assertEqual(coordinator.stop_reasons, ["external_surface_stop"])
            operator.close()

    def test_tick_before_start_and_invalid_stop_reason_have_zero_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, collector, coordinator, _ = self._runtime(directory)
            operator = ProductOperatorController(runtime)

            with self.assertRaisesRegex(ProductOperatorError, "started before tick"):
                operator.tick()
            for reason in ("", " operator_stop", "operator_stop "):
                with self.assertRaisesRegex(ProductOperatorError, "stop reason"):
                    operator.stop(reason)

            self.assertEqual(coordinator.cycle_index, 0)
            self.assertEqual(collector.stop_reasons, [])
            self.assertEqual(coordinator.stop_reasons, [])
            operator.close()

    def test_close_releases_resources_without_inventing_durable_stop(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, collector, coordinator, market_store = self._runtime(directory)
            operator = ProductOperatorController(runtime)
            operator.start()

            operator.close()

            self.assertEqual(market_store.close_calls, 1)
            self.assertEqual(collector.stop_reasons, [])
            self.assertEqual(coordinator.stop_reasons, [])
            snapshot = operator.status()
            self.assertEqual(snapshot.state, "CLOSED")
            self.assertEqual(snapshot.canonical_status.state, "RUNNING")
            for operation in (operator.start, operator.tick, operator.stop):
                with self.assertRaisesRegex(ProductOperatorError, "closed"):
                    operation()

    def test_concurrent_tick_requests_are_serialized_without_background_work(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, _, coordinator, _ = self._runtime(directory)
            operator = ProductOperatorController(runtime)
            operator.start()
            workers = 8
            barrier = threading.Barrier(workers + 1)
            results: list[int] = []
            errors: list[BaseException] = []
            result_lock = threading.Lock()

            def run_tick() -> None:
                try:
                    barrier.wait()
                    cycle_index = operator.tick().cycle_index
                    with result_lock:
                        results.append(cycle_index)
                except BaseException as exc:  # pragma: no cover - failure evidence
                    with result_lock:
                        errors.append(exc)

            threads = [threading.Thread(target=run_tick) for _ in range(workers)]
            for thread in threads:
                thread.start()
            barrier.wait()
            for thread in threads:
                thread.join()

            self.assertEqual(errors, [])
            self.assertEqual(sorted(results), list(range(1, workers + 1)))
            self.assertEqual(coordinator.cycle_index, workers)
            self.assertEqual(operator.status().controller_tick_count, workers)
            operator.stop()
            operator.close()

    def test_constructor_rejects_runtime_subclasses(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime, _, _, _ = self._runtime(directory)

            class _RuntimeSubclass(AutonomousProductRuntime):
                pass

            substitute = _RuntimeSubclass(
                workspace=runtime.workspace,
                manifest=runtime.manifest,
                coordinator=runtime.coordinator,
                collector=runtime.collector,
                market_store=runtime.market_store,
                lifecycle=runtime.lifecycle,
                mirror=runtime.mirror,
                invalidations=runtime.invalidations,
                dependencies=runtime.dependencies,
            )
            with self.assertRaisesRegex(TypeError, "exact AutonomousProductRuntime"):
                ProductOperatorController(substitute)
            runtime.close()


if __name__ == "__main__":
    unittest.main()
