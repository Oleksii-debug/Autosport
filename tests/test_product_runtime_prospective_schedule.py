from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path

from autosport.collector_service import CollectorServiceConfig
from autosport.continuous_session import SessionState, SessionStoppedError
from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import build_autonomous_product_runtime


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-20T13:58:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def __init__(self) -> None:
        self.catalog_calls = 0
        self.delta_calls = 0

    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        self.delta_calls += 1
        return ()

    def resolve_event(self, delta):
        raise AssertionError("no market delta should be resolved in this test")


class ProductRuntimeProspectiveScheduleTests(unittest.TestCase):
    def test_runtime_ticks_bind_one_frozen_schedule_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                first = runtime.tick()
                self.assertEqual(first.cycle_index, 1)
                first_evidence = runtime.collector.delta_store.collector_schedule_evidence(
                    source_id="provider-a",
                    run_id="product:provider-a",
                    start_slot_ordinal=0,
                    end_slot_ordinal=0,
                )
                schedule_id = first_evidence["schedule_id"]
                anchor_at = first_evidence["anchor_at"]
                self.assertEqual(first_evidence["bound_start_count"], 1)
                self.assertEqual(first_evidence["missing_start_count"], 0)
                self.assertEqual(first_evidence["slots"][0]["cycle_seq"], 1)
            finally:
                runtime.close()

            clock.value = "2026-09-20T13:58:35+00:00"
            restored = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                second = restored.tick()
                self.assertEqual(second.cycle_index, 2)
                evidence = restored.collector.delta_store.collector_schedule_evidence(
                    source_id="provider-a",
                    run_id="product:provider-a",
                    start_slot_ordinal=0,
                    end_slot_ordinal=1,
                )
                self.assertEqual(evidence["schedule_id"], schedule_id)
                self.assertEqual(evidence["anchor_at"], anchor_at)
                self.assertEqual(evidence["bound_start_count"], 2)
                self.assertEqual(evidence["missing_start_count"], 0)
                self.assertEqual(
                    tuple(slot["cycle_seq"] for slot in evidence["slots"]),
                    (1, 2),
                )
                self.assertEqual(
                    evidence["slots"][1]["attempted_at"],
                    "2026-09-20T13:58:35+00:00",
                )
                self.assertTrue(evidence["slots"][1]["started_late"])
            finally:
                restored.close()

    def test_frozen_finite_evaluation_window_survives_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            config = CollectorServiceConfig(evaluation_slot_count=2)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=lambda _: None,
                collector_config=config,
                initial_bankroll="100",
            )
            try:
                runtime.tick()
                first_evidence = runtime.collector.delta_store.collector_schedule_evidence(
                    source_id="provider-a",
                    run_id="product:provider-a",
                    start_slot_ordinal=0,
                    end_slot_ordinal=1,
                )
                schedule_id = first_evidence["schedule_id"]
                self.assertEqual(first_evidence["evaluation_start_slot_ordinal"], 0)
                self.assertEqual(first_evidence["evaluation_end_slot_ordinal"], 1)
                self.assertEqual(first_evidence["bound_start_count"], 1)
                self.assertEqual(first_evidence["missing_start_count"], 1)
            finally:
                runtime.close()

            clock.value = "2026-09-20T13:58:35+00:00"
            restored = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=lambda _: None,
                collector_config=CollectorServiceConfig(evaluation_slot_count=2),
                initial_bankroll="100",
            )
            try:
                restored.tick()
                evidence = restored.collector.delta_store.collector_schedule_evidence(
                    source_id="provider-a",
                    run_id="product:provider-a",
                    start_slot_ordinal=0,
                    end_slot_ordinal=1,
                )
                self.assertEqual(evidence["schedule_id"], schedule_id)
                self.assertEqual(evidence["evaluation_start_slot_ordinal"], 0)
                self.assertEqual(evidence["evaluation_end_slot_ordinal"], 1)
                self.assertEqual(evidence["bound_start_count"], 2)
                self.assertEqual(evidence["missing_start_count"], 0)
            finally:
                restored.close()

    def test_scheduled_wait_refreshes_causal_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            sleeps: list[float] = []

            def advance_to_due(seconds: float) -> None:
                sleeps.append(seconds)
                self.assertEqual(seconds, 30.0)
                clock.value = "2026-09-20T13:58:30+00:00"

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=advance_to_due,
                initial_bankroll="100",
            )
            try:
                first = runtime.tick()
                self.assertEqual(first.last_success_at, "2026-09-20T13:58:00+00:00")
                self.assertEqual(sleeps, [])

                second = runtime.tick()
                self.assertEqual(sleeps, [30.0])
                self.assertEqual(
                    second.last_success_at,
                    "2026-09-20T13:58:30+00:00",
                )
                self.assertEqual(
                    runtime.status().last_success_at,
                    "2026-09-20T13:58:30+00:00",
                )
            finally:
                runtime.close()

    def test_stop_interrupts_scheduled_wait_before_provider_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                initial_bankroll="100",
            )
            errors: list[BaseException] = []
            hostile_calls: list[str] = []
            entered_wait = threading.Event()

            class _ObservedEvent:
                def __init__(self, delegate) -> None:
                    self._delegate = delegate

                def is_set(self) -> bool:
                    return self._delegate.is_set()

                def set(self) -> None:
                    self._delegate.set()

                def clear(self) -> None:
                    self._delegate.clear()

                def wait(self, timeout: float) -> bool:
                    if timeout != 30.0:
                        raise AssertionError(f"unexpected scheduled wait: {timeout}")
                    entered_wait.set()
                    return self._delegate.wait(timeout)

            class _HostileCompatibilityController:
                def request(self, _reason: str) -> None:
                    hostile_calls.append("runtime-alias-request")
                    raise AssertionError("mutable runtime compatibility alias is not STOP authority")

                def clear(self) -> None:
                    hostile_calls.append("runtime-alias-clear")
                    raise AssertionError("mutable runtime compatibility alias is not STOP authority")

            try:
                first = runtime.tick()
                self.assertEqual(first.cycle_index, 1)
                self.assertEqual(source.catalog_calls, 1)
                self.assertEqual(source.delta_calls, 1)
                self.assertFalse(hasattr(runtime.collector, "wait_for_stop"))

                stop_source = runtime.collector.stop_requested
                stop_source._event = _ObservedEvent(stop_source._event)

                def hostile_wait(_seconds: float) -> bool:
                    hostile_calls.append("instance-wait")
                    raise AssertionError("instance wait shadow is not canonical STOP authority")

                stop_source.wait = hostile_wait
                runtime._stop_controller = _HostileCompatibilityController()

                worker = threading.Thread(
                    target=lambda: self._capture_tick_error(runtime, errors),
                    daemon=True,
                )
                worker.start()
                self.assertTrue(entered_wait.wait(2.0))
                self.assertTrue(worker.is_alive())

                runtime.stop("operator_stop")
                worker.join(2.0)
                self.assertFalse(worker.is_alive())
                self.assertEqual(hostile_calls, [])
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], SessionStoppedError)
                self.assertEqual(source.catalog_calls, 1)
                self.assertEqual(source.delta_calls, 1)

                status = runtime.status()
                self.assertEqual(status.state, SessionState.STOPPED)
                self.assertEqual(status.last_error_code, "operator_stop")
                evidence = runtime.collector.delta_store.collector_schedule_evidence(
                    source_id="provider-a",
                    run_id="product:provider-a",
                    start_slot_ordinal=0,
                    end_slot_ordinal=1,
                )
                self.assertEqual(evidence["bound_start_count"], 1)
                self.assertEqual(evidence["missing_start_count"], 1)
                self.assertIsNone(evidence["slots"][1]["cycle_seq"])
            finally:
                runtime.close()

    @staticmethod
    def _capture_tick_error(runtime, errors: list[BaseException]) -> None:
        try:
            runtime.tick()
        except BaseException as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
