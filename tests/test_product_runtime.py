from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from autosport.causal_collector import (
    CollectorDelta,
    DesktopDeltaCheckpointStore,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.collector_service import CollectorServiceConfig
from autosport.continuous_session import SessionState, SessionStoppedError
from autosport.domain import MarketEvent
from autosport.event_lifecycle import CatalogPage
from autosport.ingestion_health import SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.product_runtime import (
    ProductCompositionError,
    build_autonomous_product_runtime,
)


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-20T13:58:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    def __init__(
        self,
        source_id: str = "provider-a",
        *,
        resolved_event: MarketEvent | None = None,
    ) -> None:
        self.source_id = source_id
        self.stream_epoch = "epoch-1"
        self.resolved_event = resolved_event
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
        if self.resolved_event is None:
            raise AssertionError("no market delta should be resolved in this test")
        if delta.event_id != self.resolved_event.event_id:
            raise AssertionError("unexpected collector delta event")
        return self.resolved_event


class _CrashAfterPersistBus(MarketEventBus):
    """Simulate process loss after SQLite/subscriber delivery but before app progress."""

    crashed = False

    def publish(self, event):
        accepted = super().publish(event)
        if not type(self).crashed:
            type(self).crashed = True
            raise RuntimeError("crash-after-market-persist")
        return accepted


def _event() -> MarketEvent:
    return MarketEvent.from_dict(
        {
            "event_id": "event-1",
            "market_id": "winner",
            "selection_id": "player-a",
            "decimal_odds": "1.80",
            "observed_ts": "2026-09-20T13:57:55+00:00",
            "source_id": "provider-a",
            "sequence": 1,
            "market_type": "winner",
            "status": "open",
            "source_ts": "2026-09-20T13:57:54+00:00",
            "ingest_ts": "2026-09-20T13:57:56+00:00",
            "metadata": {},
            "score_state": None,
        }
    )


def _delta(event: MarketEvent) -> CollectorDelta:
    return CollectorDelta(
        schema_version=1,
        delta_id="delta-1",
        source_id=event.source_id,
        lawful_terms_ref="terms:provider-a:v1",
        retention_ref="retention:provider-a:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key=event.dedupe_key,
        event_id=event.event_id,
        source_payload_digest=digest_source_payload('{"provider":"payload"}'),
        canonical_event_digest=canonical_event_digest(event),
        source_observed_at="2026-09-20T13:57:55+00:00",
        collector_received_at="2026-09-20T13:57:56+00:00",
        collector_committed_at="2026-09-20T13:57:57+00:00",
        desktop_available_at="2026-09-20T13:57:58+00:00",
    )


class AutonomousProductCompositionTests(unittest.TestCase):
    def test_clean_workspace_builds_and_restart_restores_same_session(self) -> None:
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
                first_status = runtime.status()
                self.assertEqual(first_status.cycles_completed, 0)
                self.assertEqual(first_status.source_id, "provider-a")
                session_id = first_status.session_id

                result = runtime.tick()
                self.assertEqual(result.cycle_index, 1)
                self.assertEqual(runtime.status().cycles_completed, 1)
            finally:
                runtime.close()

            restored = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                restored_status = restored.status()
                self.assertEqual(restored_status.session_id, session_id)
                self.assertEqual(restored_status.cycles_completed, 1)
                self.assertEqual(restored.manifest.source_id, "provider-a")
                self.assertEqual(restored.manifest.initial_bankroll, "100")
            finally:
                restored.close()

    def test_runtime_ticks_bind_one_frozen_prospective_schedule_across_restart(
        self,
    ) -> None:
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
                first_evidence = (
                    runtime.collector.delta_store.collector_schedule_evidence(
                        source_id="provider-a",
                        run_id="product:provider-a",
                        start_slot_ordinal=0,
                        end_slot_ordinal=0,
                    )
                )
                schedule_id = first_evidence["schedule_id"]
                anchor_at = first_evidence["anchor_at"]
                self.assertEqual(first_evidence["bound_start_count"], 1)
                self.assertEqual(first_evidence["missing_start_count"], 0)
                self.assertEqual(first_evidence["slots"][0]["cycle_seq"], 1)
                self.assertEqual(
                    first_evidence["slots"][0]["due_at"],
                    "2026-09-20T13:58:00+00:00",
                )
                self.assertIsNone(runtime.collector.status()["stopped_at"])
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
                evidence = (
                    restored.collector.delta_store.collector_schedule_evidence(
                        source_id="provider-a",
                        run_id="product:provider-a",
                        start_slot_ordinal=0,
                        end_slot_ordinal=1,
                    )
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
                    evidence["slots"][1]["due_at"],
                    "2026-09-20T13:58:30+00:00",
                )
                self.assertEqual(
                    evidence["slots"][1]["attempted_at"],
                    "2026-09-20T13:58:35+00:00",
                )
                self.assertTrue(evidence["slots"][1]["started_late"])
                self.assertIsNone(restored.collector.status()["stopped_at"])
            finally:
                restored.close()

    def test_runtime_accepts_frozen_finite_evaluation_window_across_restart(
        self,
    ) -> None:
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
                first_evidence = (
                    runtime.collector.delta_store.collector_schedule_evidence(
                        source_id="provider-a",
                        run_id="product:provider-a",
                        start_slot_ordinal=0,
                        end_slot_ordinal=1,
                    )
                )
                schedule_id = first_evidence["schedule_id"]
                self.assertEqual(
                    first_evidence["evaluation_start_slot_ordinal"],
                    0,
                )
                self.assertEqual(
                    first_evidence["evaluation_end_slot_ordinal"],
                    1,
                )
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
                collector_config=CollectorServiceConfig(
                    evaluation_slot_count=2,
                ),
                initial_bankroll="100",
            )
            try:
                restored.tick()
                evidence = (
                    restored.collector.delta_store.collector_schedule_evidence(
                        source_id="provider-a",
                        run_id="product:provider-a",
                        start_slot_ordinal=0,
                        end_slot_ordinal=1,
                    )
                )
                self.assertEqual(evidence["schedule_id"], schedule_id)
                self.assertEqual(
                    evidence["evaluation_start_slot_ordinal"],
                    0,
                )
                self.assertEqual(
                    evidence["evaluation_end_slot_ordinal"],
                    1,
                )
                self.assertEqual(evidence["bound_start_count"], 2)
                self.assertEqual(evidence["missing_start_count"], 0)
                self.assertEqual(
                    tuple(slot["cycle_seq"] for slot in evidence["slots"]),
                    (1, 2),
                )
            finally:
                restored.close()

    def test_scheduled_wait_refreshes_runtime_causal_cutoff_before_session_work(
        self,
    ) -> None:
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
                evidence = runtime.collector.delta_store.collector_schedule_evidence(
                    source_id="provider-a",
                    run_id="product:provider-a",
                    start_slot_ordinal=0,
                    end_slot_ordinal=1,
                )
                self.assertEqual(
                    evidence["slots"][1]["due_at"],
                    "2026-09-20T13:58:30+00:00",
                )
                self.assertEqual(
                    evidence["slots"][1]["attempted_at"],
                    "2026-09-20T13:58:30+00:00",
                )
                self.assertFalse(evidence["slots"][1]["started_before_due"])
                self.assertFalse(evidence["slots"][1]["started_late"])
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
            entered_wait = threading.Event()
            original_wait = runtime.collector.wait_for_stop
            self.assertIsNotNone(original_wait)

            def observed_wait(seconds: float) -> bool:
                self.assertEqual(seconds, 30.0)
                entered_wait.set()
                assert original_wait is not None
                return original_wait(seconds)

            runtime.collector.wait_for_stop = observed_wait
            errors: list[BaseException] = []
            try:
                first = runtime.tick()
                self.assertEqual(first.cycle_index, 1)
                self.assertEqual(source.catalog_calls, 1)
                self.assertEqual(source.delta_calls, 1)

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
                self.assertEqual(len(errors), 1)
                self.assertIsInstance(errors[0], SessionStoppedError)
                self.assertEqual(source.catalog_calls, 1)
                self.assertEqual(source.delta_calls, 1)

                status = runtime.status()
                self.assertEqual(status.state, SessionState.STOPPED)
                self.assertEqual(status.last_error_code, "operator_stop")
                self.assertNotEqual(
                    status.last_error_code,
                    "CollectorServiceStoppedError",
                )
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

    def test_restart_with_different_source_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source("provider-a"),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "source_id conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source("provider-b"),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

    def test_restart_with_changed_initial_bankroll_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "initial_bankroll conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="101",
                )

    def test_invalid_initial_bankroll_does_not_publish_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                ValueError,
                "initial_bankroll must construct a valid PaperBook",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="not-a-number",
                )
            self.assertFalse((root / "product_composition.json").exists())

    def test_corrupt_manifest_fails_closed_before_runtime_construction(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
            (root / "product_composition.json").write_text(
                '{"schema":"autosport.autonomous_product_composition"}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ProductCompositionError,
                "manifest schema mismatch",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

    def test_crash_after_market_persist_replays_canonical_application_exactly_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event()
            delta = _delta(event)
            source = _Source(resolved_event=event)
            _CrashAfterPersistBus.crashed = False

            with patch(
                "autosport.product_runtime.MarketEventBus",
                _CrashAfterPersistBus,
            ):
                runtime = build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    self.assertTrue(runtime.collector.delta_store.append(delta))
                    with self.assertRaisesRegex(
                        RuntimeError,
                        "crash-after-market-persist",
                    ):
                        runtime.coordinator.desktop_consumer.drain(
                            as_of=clock.value,
                        )
                    self.assertEqual(len(runtime.market_store.events(event.event_id)), 1)
                    self.assertEqual(
                        SourceHealthStore(root / "source_health.json")
                        .get(source.source_id)
                        .poll_count,
                        0,
                    )
                finally:
                    runtime.close()

            restored = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertEqual(
                    restored.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
                self.assertEqual(len(restored.market_store.events(event.event_id)), 1)
                health = SourceHealthStore(root / "source_health.json").get(source.source_id)
                self.assertEqual(health.poll_count, 1)
                self.assertEqual(health.total_received, 1)
                self.assertEqual(health.total_accepted, 1)
                self.assertEqual(health.last_cursor, delta.source_cursor)

                receipt = DesktopDeltaCheckpointStore(
                    root / "desktop_acks.json"
                ).application_receipt(delta)
                self.assertIsNotNone(receipt)
                self.assertTrue(receipt.receipt_id.startswith("canonical-desktop:"))

                self.assertEqual(
                    restored.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (),
                )
                self.assertEqual(
                    SourceHealthStore(root / "source_health.json")
                    .get(source.source_id)
                    .poll_count,
                    1,
                )
            finally:
                restored.close()


if __name__ == "__main__":
    unittest.main()
