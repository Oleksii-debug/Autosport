from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.causal_collector_legacy as causal_collector_legacy_module
import autosport.product_runtime as product_runtime_module

from autosport.causal_collector import (
    CollectorDelta,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    canonical_event_digest,
    digest_source_payload,
)
from autosport.domain import MarketEvent
from autosport.event_lifecycle import CatalogPage
from autosport.ingestion_health import SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.product_runtime import (
    ProductCompositionError,
    build_autonomous_product_runtime,
)
from autosport.storage import SQLiteMarketStore


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
                self.assertIs(
                    runtime.coordinator.desktop_consumer._acknowledgement_clock,
                    clock,
                )
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

    def test_product_desktop_authority_graph_rejects_post_build_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event()
            delta = _delta(event)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(resolved_event=event),
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                desktop = runtime.coordinator.desktop_consumer
                replacements = {
                    "collector": object(),
                    "checkpoint": object(),
                    "resolve_event": lambda _delta: event,
                    "apply_event": lambda _delta, _event: None,
                    "lookup_application_receipt": lambda _delta: None,
                    "_acknowledgement_clock": lambda: clock.value,
                    "_on_application_receipt": lambda *_args: None,
                    "drain": lambda **_kwargs: (),
                    "_PROTECTED_AUTHORITY_FIELDS": frozenset(),
                    "_product_authority_snapshot": (),
                    "_product_authority_sealed": False,
                }
                for name, replacement in replacements.items():
                    with self.subTest(authority=name):
                        original = getattr(desktop, name)
                        with self.assertRaisesRegex(
                            ProductCompositionError,
                            "product desktop authority field",
                        ):
                            setattr(desktop, name, replacement)
                        current = getattr(desktop, name)
                        if name == "drain":
                            self.assertIs(current.__func__, original.__func__)
                            self.assertNotIn("drain", desktop.__dict__)
                        else:
                            self.assertIs(current, original)

                original_apply = desktop.apply_event
                desktop.__dict__["apply_event"] = lambda _delta, _event: None
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "changed after composition",
                ):
                    desktop.drain(as_of=clock.value)
                self.assertFalse(
                    DesktopDeltaCheckpointStore(root / "desktop_acks.json").has_ack(
                        delta.delta_id
                    )
                )
                desktop.__dict__["apply_event"] = original_apply

                self.assertTrue(runtime.collector.delta_store.append(delta))
                self.assertEqual(
                    desktop.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
                self.assertEqual(runtime.mirror.snapshot(), (event,))
                receipt = DesktopDeltaCheckpointStore(
                    root / "desktop_acks.json"
                ).application_receipt(delta)
                self.assertIsNotNone(receipt)
                self.assertEqual(
                    receipt.canonical_event_digest,
                    delta.canonical_event_digest,
                )
            finally:
                runtime.close()

    def test_runtime_ack_clock_allows_application_after_causal_cutoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event()
            delta = _delta(event)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(resolved_event=event),
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                self.assertEqual(
                    runtime.coordinator.desktop_consumer.drain(
                        as_of="2026-09-20T13:57:59+00:00",
                    ),
                    (delta.delta_id,),
                )
                self.assertEqual(runtime.mirror.snapshot(), (event,))
                self.assertEqual(runtime.invalidations.pending_count, 1)
                receipt = DesktopDeltaCheckpointStore(
                    root / "desktop_acks.json"
                ).application_receipt(delta)
                self.assertIsNotNone(receipt)
                self.assertEqual(receipt.applied_at, clock.value)
            finally:
                runtime.close()

    def test_runtime_preload_excludes_unreceipted_market_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteMarketStore(root / "market.db")
            try:
                own = _event()
                other = MarketEvent.from_dict(
                    {
                        **own.to_dict(),
                        "source_id": "provider-b",
                        "sequence": 2,
                    }
                )
                self.assertTrue(store.append(own))
                self.assertTrue(store.append(other))
                self.assertEqual(len(store.current_by_source()), 2)
            finally:
                store.close()

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source("provider-a"),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertEqual(runtime.mirror.snapshot(), ())
                self.assertEqual(runtime.invalidations.pending_count, 0)
                self.assertEqual(
                    len(runtime.market_store.current_by_source()),
                    2,
                )
            finally:
                runtime.close()

    def test_runtime_restart_preloads_latest_desktop_applied_not_newer_generic_quote(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            applied = _event()
            delta = _delta(applied)
            source = _Source(resolved_event=applied)

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                self.assertEqual(
                    runtime.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
                receipt = DesktopDeltaCheckpointStore(
                    root / "desktop_acks.json"
                ).application_receipt(delta)
                self.assertIsNotNone(receipt)
            finally:
                runtime.close()

            generic_newer = MarketEvent.from_dict(
                {
                    **applied.to_dict(),
                    "sequence": 99,
                    "observed_ts": "2026-09-20T13:58:05+00:00",
                    "ingest_ts": "2026-09-20T13:58:06+00:00",
                    "metadata": {"origin": "generic-import"},
                }
            )
            store = SQLiteMarketStore(root / "market.db")
            try:
                self.assertTrue(store.append(generic_newer))
                generic_current = store.current_by_source()[
                    (applied.source_id, applied.quote_key)
                ]
                self.assertEqual(generic_current.sequence, 99)
            finally:
                store.close()

            restored = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                snapshot = restored.mirror.snapshot()
                self.assertEqual(len(snapshot), 1)
                self.assertEqual(snapshot[0], applied)
                self.assertEqual(snapshot[0].sequence, 1)
                self.assertEqual(restored.invalidations.pending_count, 1)
                self.assertEqual(
                    restored.market_store.current_by_source()[
                        (applied.source_id, applied.quote_key)
                    ].sequence,
                    99,
                )
            finally:
                restored.close()

    def test_runtime_restart_ignores_application_receipt_dispatch_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            applied = _event()
            delta = _delta(applied)
            source = _Source(resolved_event=applied)

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                self.assertEqual(
                    runtime.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
                self.assertEqual(runtime.mirror.snapshot(), (applied,))
            finally:
                runtime.close()

            generic_newer = MarketEvent.from_dict(
                {
                    **applied.to_dict(),
                    "decimal_odds": "9.99",
                    "sequence": 99,
                    "observed_ts": "2026-09-20T13:58:05+00:00",
                    "ingest_ts": "2026-09-20T13:58:06+00:00",
                    "metadata": {"origin": "forged-restart-receipt"},
                }
            )
            store = SQLiteMarketStore(root / "market.db")
            try:
                self.assertTrue(store.append(generic_newer))
            finally:
                store.close()

            forged_receipt = DesktopApplicationReceipt(
                delta_id=delta.delta_id,
                canonical_event_digest=canonical_event_digest(generic_newer),
                receipt_id="forged-receipt",
                applied_at=clock.value,
            )

            with patch.object(
                causal_collector_legacy_module._CanonicalDesktopApplicationStore,
                "completed_receipts_for_source",
                lambda _self, _source_id: (forged_receipt,),
            ):
                restored = build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    snapshot = restored.mirror.snapshot()
                    self.assertEqual(snapshot, (applied,))
                    self.assertEqual(str(snapshot[0].decimal_odds), "1.80")
                finally:
                    restored.close()

    def test_runtime_restart_rejects_receipt_without_durable_health_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            applied = _event()
            delta = _delta(applied)
            source = _Source(resolved_event=applied)

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            health_path = root / "source_health.json"
            pristine_health = health_path.read_text(encoding="utf-8")
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                self.assertEqual(
                    runtime.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
                self.assertEqual(runtime.mirror.snapshot(), (applied,))
            finally:
                runtime.close()

            health_path.write_text(pristine_health, encoding="utf-8")
            with self.assertRaisesRegex(
                ProductCompositionError,
                "cannot verify desktop application receipts for product runtime restart",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

    def test_restart_reader_module_rebind_cannot_authorize_generic_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            applied = _event()
            delta = _delta(applied)
            source = _Source(resolved_event=applied)

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                self.assertEqual(
                    runtime.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
            finally:
                runtime.close()

            generic_newer = MarketEvent.from_dict(
                {
                    **applied.to_dict(),
                    "decimal_odds": "9.99",
                    "sequence": 99,
                    "observed_ts": "2026-09-20T13:58:05+00:00",
                    "ingest_ts": "2026-09-20T13:58:06+00:00",
                    "metadata": {"origin": "generic-import"},
                }
            )
            store = SQLiteMarketStore(root / "market.db")
            try:
                self.assertTrue(store.append(generic_newer))
            finally:
                store.close()

            def forged_restart_reader(**_kwargs):
                return (generic_newer,)

            with patch.object(
                product_runtime_module,
                "_desktop_applied_current_for_source",
                forged_restart_reader,
                create=True,
            ):
                restored = product_runtime_module.build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    snapshot = restored.mirror.snapshot()
                    self.assertEqual(len(snapshot), 1)
                    self.assertEqual(snapshot[0].dedupe_key, applied.dedupe_key)
                    self.assertEqual(snapshot[0].sequence, 1)
                    self.assertEqual(str(snapshot[0].decimal_odds), "1.80")
                finally:
                    restored.close()

    def test_restart_receipt_digest_ignores_runtime_event_serializer_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            applied = _event()
            delta = _delta(applied)
            source = _Source(resolved_event=applied)

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                self.assertEqual(
                    runtime.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
            finally:
                runtime.close()

            generic_newer = MarketEvent.from_dict(
                {
                    **applied.to_dict(),
                    "decimal_odds": "9.99",
                    "sequence": 99,
                    "observed_ts": "2026-09-20T13:58:05+00:00",
                    "ingest_ts": "2026-09-20T13:58:06+00:00",
                    "metadata": {"origin": "generic-import"},
                }
            )
            store = SQLiteMarketStore(root / "market.db")
            try:
                self.assertTrue(store.append(generic_newer))
            finally:
                store.close()

            forged_payload = applied.to_dict()

            def forged_to_dict(_event):
                return dict(forged_payload)

            with patch.object(MarketEvent, "to_dict", forged_to_dict):
                restored = build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    snapshot = restored.mirror.snapshot()
                    self.assertEqual(len(snapshot), 1)
                    self.assertEqual(snapshot[0].dedupe_key, applied.dedupe_key)
                    self.assertEqual(snapshot[0].sequence, 1)
                    self.assertEqual(str(snapshot[0].decimal_odds), "1.80")
                finally:
                    restored.close()

    def test_delivery_resolver_module_rebind_cannot_publish_generic_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            applied = _event()
            delta = _delta(applied)
            source = _Source(resolved_event=applied)
            generic = MarketEvent.from_dict(
                {
                    **applied.to_dict(),
                    "decimal_odds": "9.99",
                    "sequence": 99,
                    "observed_ts": "2026-09-20T13:58:05+00:00",
                    "ingest_ts": "2026-09-20T13:58:06+00:00",
                    "metadata": {"origin": "forged-delivery"},
                }
            )

            def forged_delivery_resolver(**_kwargs):
                return generic

            with patch.object(
                product_runtime_module,
                "_desktop_applied_event_for_receipt",
                forged_delivery_resolver,
                create=True,
            ):
                runtime = product_runtime_module.build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=clock,
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
                try:
                    self.assertTrue(runtime.collector.delta_store.append(delta))
                    self.assertEqual(
                        runtime.coordinator.desktop_consumer.drain(as_of=clock.value),
                        (delta.delta_id,),
                    )
                    snapshot = runtime.mirror.snapshot()
                    self.assertEqual(snapshot, (applied,))
                    self.assertEqual(str(snapshot[0].decimal_odds), "1.80")
                finally:
                    runtime.close()

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
                    self.assertEqual(runtime.mirror.snapshot(), ())
                    self.assertEqual(runtime.invalidations.pending_count, 0)
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
                self.assertEqual(restored.mirror.snapshot(), ())
                self.assertEqual(restored.invalidations.pending_count, 0)
                self.assertEqual(
                    restored.coordinator.desktop_consumer.drain(as_of=clock.value),
                    (delta.delta_id,),
                )
                self.assertEqual(restored.mirror.snapshot(), (event,))
                self.assertEqual(restored.invalidations.pending_count, 1)
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
