from __future__ import annotations

import multiprocessing
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from autosport.continuous_session import SessionState
from autosport.event_lifecycle import CatalogPage
from autosport.product_runtime import (
    AutonomousProductRuntime,
    ProductCompositionError,
    ProductCompositionManifest,
    build_autonomous_product_runtime,
)


class Collector:
    def __init__(self, stopped: bool) -> None:
        self.stopped = stopped
        self.reason = "prior_stop" if stopped else None
        self.resume_calls = 0
        self.stop_calls: list[str] = []
        self.stop_error: Exception | None = None

    def status(self) -> dict[str, object]:
        return {
            "stopped_at": "2026-09-21T12:00:00+00:00" if self.stopped else None,
            "stop_reason": self.reason if self.stopped else None,
        }

    def resume(self) -> None:
        self.resume_calls += 1
        self.stopped = False
        self.reason = None

    def stop(self, reason: str) -> None:
        self.stop_calls.append(reason)
        if self.stop_error is not None:
            raise self.stop_error
        self.stopped = True
        self.reason = reason


class Coordinator:
    def __init__(self, state: SessionState) -> None:
        self.state = state
        self.resume_calls = 0
        self.pause_calls = 0
        self.stop_calls: list[str] = []
        self.tick_calls = 0
        self.resume_error: Exception | None = None
        self.stop_error: Exception | None = None

    def status(self):
        return SimpleNamespace(state=self.state)

    def resume(self) -> None:
        self.resume_calls += 1
        if self.resume_error is not None:
            raise self.resume_error
        self.state = SessionState.RUNNING

    def pause(self) -> None:
        self.pause_calls += 1
        self.state = SessionState.PAUSED

    def stop(self, reason: str) -> None:
        self.stop_calls.append(reason)
        if self.stop_error is not None:
            raise self.stop_error
        self.state = SessionState.STOPPED

    def tick(self):
        self.tick_calls += 1
        return SimpleNamespace(cycle_index=self.tick_calls)




class Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def fetch_catalog_page(self, checkpoint):
        return CatalogPage(
            source_id=self.source_id,
            stream_epoch=self.stream_epoch,
            cursor="catalog-1",
            position=1,
            events=(),
        )

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()

    def resolve_event(self, delta):
        raise AssertionError("no market delta should be resolved")


def _crash_at_start_prefix(
    workspace: str,
    prefix: int,
    ready,
) -> None:
    runtime = build_autonomous_product_runtime(
        workspace=Path(workspace),
        source=Source(),
        clock=lambda: "2026-09-22T14:05:00+00:00",
        sleep=lambda _: None,
        initial_bankroll="100",
    )
    runtime.stop("crash_test_precondition")
    runtime._start_transition_store.begin(
        collector_was_stopped=True,
        session_pre_state=SessionState.STOPPED.value,
    )
    if prefix >= 1:
        runtime.collector.resume()
    if prefix >= 2:
        runtime.coordinator.resume()
    ready.set()
    os._exit(81 + prefix)


class Noop:
    authority_active = True

    def close(self) -> None:
        pass

    def release(self) -> None:
        self.authority_active = False


class TransitionStore:
    def __init__(self) -> None:
        self.generation = 0
        self.phase: str | None = None
        self.collector_was_stopped: bool | None = None
        self.session_pre_state: str | None = None

    def pending(self):
        if self.phase not in {"STARTING", "RECOVERY_REQUIRED"}:
            return None
        return {
            "generation": self.generation,
            "phase": self.phase,
            "collector_was_stopped": self.collector_was_stopped,
            "session_pre_state": self.session_pre_state,
        }

    def begin(self, *, collector_was_stopped: bool, session_pre_state: str) -> int:
        if self.pending() is not None:
            raise ProductCompositionError(
                "unfinished product START transition requires recovery"
            )
        self.generation += 1
        self.phase = "STARTING"
        self.collector_was_stopped = collector_was_stopped
        self.session_pre_state = session_pre_state
        return self.generation

    def _require_generation(self, generation: int) -> None:
        if generation != self.generation:
            raise ProductCompositionError(
                "durable product START transition generation changed"
            )

    def mark_completed(self, generation: int) -> None:
        self._require_generation(generation)
        self.phase = "COMPLETED"

    def mark_rolled_back(self, generation: int) -> None:
        self._require_generation(generation)
        self.phase = "ROLLED_BACK"

    def mark_recovery_required(self, generation: int) -> None:
        self._require_generation(generation)
        self.phase = "RECOVERY_REQUIRED"


def runtime(stopped: bool, state: SessionState):
    collector = Collector(stopped)
    coordinator = Coordinator(state)
    value = AutonomousProductRuntime(
        workspace=Path("unused"),
        manifest=ProductCompositionManifest("provider-a", "100"),
        coordinator=coordinator,
        collector=collector,
        market_store=Noop(),
        lifecycle=object(),
        mirror=object(),
        invalidations=object(),
        dependencies=object(),
        _runtime_lease=Noop(),
        _start_transition_store=TransitionStore(),
    )
    return value, collector, coordinator


class ProductRuntimeLifecycleCoherenceTests(unittest.TestCase):
    def test_partial_start_is_compensated_to_stop(self) -> None:
        value, collector, coordinator = runtime(True, SessionState.STOPPED)
        coordinator.resume_error = RuntimeError("resume failed")

        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            value.start()

        self.assertEqual(collector.resume_calls, 1)
        self.assertEqual(collector.stop_calls, ["runtime_start_failed"])
        self.assertEqual(coordinator.stop_calls, ["runtime_start_failed"])
        self.assertTrue(collector.stopped)
        self.assertEqual(coordinator.state, SessionState.STOPPED)
        self.assertEqual(value.status().state, SessionState.STOPPED)

    def test_failed_post_start_coherence_check_also_compensates_to_stop(self) -> None:
        value, collector, coordinator = runtime(True, SessionState.STOPPED)

        with patch.object(coordinator, "resume", return_value=None):
            with self.assertRaisesRegex(
                ProductCompositionError,
                "lifecycle authorities disagree",
            ):
                value.start()

        self.assertTrue(collector.stopped)
        self.assertEqual(collector.stop_calls, ["runtime_start_failed"])
        self.assertEqual(coordinator.stop_calls, ["runtime_start_failed"])
        self.assertEqual(coordinator.state, SessionState.STOPPED)
        self.assertEqual(value.status().state, SessionState.STOPPED)

    def test_pause_from_stopped_is_rejected_without_splitting_lifecycle(self) -> None:
        value, collector, coordinator = runtime(True, SessionState.STOPPED)

        with self.assertRaisesRegex(
            ProductCompositionError,
            "cannot pause a stopped product runtime",
        ):
            value.pause()

        self.assertEqual(coordinator.pause_calls, 0)
        self.assertTrue(collector.stopped)
        self.assertEqual(coordinator.state, SessionState.STOPPED)
        self.assertEqual(value.status().state, SessionState.STOPPED)

    def test_repeated_pause_is_idempotent_without_durable_rewrite(self) -> None:
        value, collector, coordinator = runtime(False, SessionState.PAUSED)

        self.assertEqual(value.pause().state, SessionState.PAUSED)

        self.assertEqual(coordinator.pause_calls, 0)
        self.assertFalse(collector.stopped)
        self.assertEqual(coordinator.state, SessionState.PAUSED)

    def test_running_pause_preserves_coherent_paused_state(self) -> None:
        value, collector, coordinator = runtime(False, SessionState.RUNNING)

        self.assertEqual(value.pause().state, SessionState.PAUSED)

        self.assertEqual(coordinator.pause_calls, 1)
        self.assertFalse(collector.stopped)
        self.assertEqual(coordinator.state, SessionState.PAUSED)

    def test_partial_stop_blocks_positive_work_until_explicit_stop_recovery(self) -> None:
        value, collector, coordinator = runtime(False, SessionState.RUNNING)
        coordinator.stop_error = RuntimeError("stop failed")

        with self.assertRaisesRegex(RuntimeError, "stop failed"):
            value.stop("operator_stop")

        self.assertTrue(collector.stopped)
        self.assertEqual(coordinator.state, SessionState.RUNNING)
        for action in (value.status, value.start, value.pause, value.tick):
            with self.assertRaisesRegex(
                ProductCompositionError,
                "lifecycle authorities disagree",
            ):
                action()
        self.assertEqual(collector.resume_calls, 0)
        self.assertEqual(coordinator.tick_calls, 0)

        coordinator.stop_error = None
        self.assertEqual(
            value.stop("operator_recovery_stop").state,
            SessionState.STOPPED,
        )

    def test_reverse_split_state_is_also_rejected(self) -> None:
        value, collector, coordinator = runtime(False, SessionState.STOPPED)

        with self.assertRaisesRegex(
            ProductCompositionError,
            "lifecycle authorities disagree",
        ):
            value.status()
        with self.assertRaisesRegex(
            ProductCompositionError,
            "lifecycle authorities disagree",
        ):
            value.start()

        self.assertEqual(collector.resume_calls, 0)
        self.assertEqual(coordinator.resume_calls, 0)

    def test_stop_attempts_both_authorities_when_collector_stop_errors(self) -> None:
        value, collector, coordinator = runtime(False, SessionState.RUNNING)
        collector.stop_error = RuntimeError("collector stop failed")

        with self.assertRaisesRegex(RuntimeError, "collector stop failed"):
            value.stop("operator_stop")

        self.assertEqual(coordinator.stop_calls, ["operator_stop"])
        self.assertEqual(coordinator.state, SessionState.STOPPED)
        with self.assertRaisesRegex(
            ProductCompositionError,
            "lifecycle authorities disagree",
        ):
            value.status()

        collector.stop_error = None
        self.assertEqual(
            value.stop("operator_recovery_stop").state,
            SessionState.STOPPED,
        )


    def test_split_stop_state_is_detected_after_real_runtime_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = build_autonomous_product_runtime(
                workspace=Path(directory),
                source=Source(),
                clock=lambda: "2026-09-21T12:00:00+00:00",
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with patch.object(
                    first.coordinator,
                    "stop",
                    side_effect=RuntimeError("durable session stop failed"),
                ):
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "durable session stop failed",
                    ):
                        first.stop("operator_stop")
            finally:
                first.close()

            restored = build_autonomous_product_runtime(
                workspace=Path(directory),
                source=Source(),
                clock=lambda: "2026-09-21T12:01:00+00:00",
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                for action in (restored.status, restored.start, restored.tick):
                    with self.assertRaisesRegex(
                        ProductCompositionError,
                        "lifecycle authorities disagree",
                    ):
                        action()

                self.assertEqual(
                    restored.stop("operator_recovery_stop").state,
                    SessionState.STOPPED,
                )
                self.assertEqual(restored.status().state, SessionState.STOPPED)
            finally:
                restored.close()


    def test_successful_start_commits_transition_and_repeated_start_is_idempotent(self) -> None:
        value, collector, coordinator = runtime(True, SessionState.STOPPED)
        transitions = value._start_transition_store

        self.assertEqual(value.start().state, SessionState.RUNNING)
        self.assertEqual(transitions.phase, "COMPLETED")
        self.assertEqual(transitions.generation, 1)
        self.assertEqual(collector.resume_calls, 1)
        self.assertEqual(coordinator.resume_calls, 1)

        self.assertEqual(value.start().state, SessionState.RUNNING)
        self.assertEqual(transitions.generation, 1)
        self.assertEqual(collector.resume_calls, 1)
        self.assertEqual(coordinator.resume_calls, 1)

    def test_failed_start_with_failed_compensation_stays_recovery_required(self) -> None:
        value, collector, coordinator = runtime(True, SessionState.STOPPED)
        coordinator.resume_error = RuntimeError("resume failed")
        collector.stop_error = RuntimeError("collector compensation failed")

        with self.assertRaisesRegex(RuntimeError, "resume failed"):
            value.start()

        transitions = value._start_transition_store
        self.assertEqual(transitions.phase, "RECOVERY_REQUIRED")
        for action in (value.status, value.start, value.pause, value.tick):
            with self.assertRaisesRegex(
                ProductCompositionError,
                "START transition requires recovery",
            ):
                action()

        collector.stop_error = None
        self.assertEqual(
            value.stop("operator_recovery_stop").state,
            SessionState.STOPPED,
        )
        self.assertEqual(transitions.phase, "ROLLED_BACK")
        self.assertEqual(value.status().state, SessionState.STOPPED)

    def test_restart_recovers_every_durable_start_prefix_to_stopped(self) -> None:
        context = multiprocessing.get_context("spawn")
        for prefix in (0, 1, 2):
            with self.subTest(prefix=prefix), tempfile.TemporaryDirectory() as directory:
                ready = context.Event()
                process = context.Process(
                    target=_crash_at_start_prefix,
                    args=(directory, prefix, ready),
                )
                process.start()
                self.assertTrue(
                    ready.wait(20),
                    f"child did not reach START crash prefix {prefix}",
                )
                process.join(20)
                if process.is_alive():
                    process.terminate()
                    process.join(10)
                    self.fail(f"child did not exit at START crash prefix {prefix}")
                self.assertEqual(process.exitcode, 81 + prefix)

                restored = build_autonomous_product_runtime(
                    workspace=Path(directory),
                    source=Source(),
                    clock=lambda: "2026-09-22T14:06:00+00:00",
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    self.assertEqual(
                        restored.status().state,
                        SessionState.STOPPED,
                    )
                    self.assertIsNone(
                        restored._start_transition_store.pending()
                    )
                    self.assertIsNotNone(
                        restored.collector.status()["stopped_at"]
                    )
                finally:
                    restored.close()



if __name__ == "__main__":
    unittest.main()
