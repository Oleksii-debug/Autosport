from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import autosport.causal_collector_legacy as causal_collector_legacy_module
import autosport.product_runtime as product_runtime_module

from autosport.causal_collector import (
    ApplicationReceiptError,
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
        configuration_sha256: str = "1" * 64,
    ) -> None:
        self.source_id = source_id
        self.stream_epoch = "epoch-1"
        self.product_source_configuration_sha256 = configuration_sha256
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


class _AlternateResolverSource(_Source):
    def resolve_event(self, delta):
        event = _Source.resolve_event(self, delta)
        if event.status != "open":
            raise AssertionError("alternate resolver requires an open market")
        return event


class _OpaqueResolverDispatchSource(_Source):
    def __getattribute__(self, name):
        if name == "resolve_event":
            return lambda _delta: _event()
        return object.__getattribute__(self, name)


class _FallbackResolverDispatchSource(_Source):
    def __getattr__(self, name):
        if name == "unexpected_authority":
            return "forged"
        raise AttributeError(name)


class _MutatingCatalogSource(_Source):
    def fetch_catalog_page(self, checkpoint):
        self.product_source_configuration_sha256 = "2" * 64
        return _Source.fetch_catalog_page(self, checkpoint)


class _MutatingDeltaSource(_Source):
    def fetch_deltas(self, checkpoint, records, max_items):
        self.product_source_configuration_sha256 = "2" * 64
        return ()


class _MutatingResolverSource(_Source):
    def resolve_event(self, delta):
        event = _Source.resolve_event(self, delta)
        self.product_source_configuration_sha256 = "2" * 64
        return event


class _MutatingCatalogEpochSource(_Source):
    def fetch_catalog_page(self, checkpoint):
        self.stream_epoch = "epoch-2"
        return _Source.fetch_catalog_page(self, checkpoint)


class _MutatingDeltaEpochSource(_Source):
    def fetch_deltas(self, checkpoint, records, max_items):
        self.stream_epoch = "epoch-2"
        return ()


class _LearningHandoff:
    settlement_learning_handoff_implementation_id = "test-learning-handoff-v1"
    _AUTHORITY_FIELDS = frozenset(
        {"target", "settlement_learning_configuration_sha256"}
    )

    def __init__(self, configuration_sha256: str = "c" * 64) -> None:
        self.target = []
        self.calls = []
        self.settlement_learning_configuration_sha256 = configuration_sha256

    def prepare_settlement(self, *, paper_book_path, resolutions, at):
        self.calls.append(("prepare", paper_book_path, resolutions, at))
        return ("prepared",)

    def reconcile_after_settlement(
        self,
        *,
        paper_book_path,
        resolutions,
        settled_ticket_ids,
        at,
    ):
        self.calls.append(
            ("reconcile", paper_book_path, resolutions, settled_ticket_ids, at)
        )
        return ("reconciled",)


class _MutatingLearningHandoff(_LearningHandoff):
    def prepare_settlement(self, *, paper_book_path, resolutions, at):
        object.__setattr__(self, "target", [])
        return ("prepared",)


def _replacement_source_resolve_event(self, delta):
    if delta.event_id == "never":
        raise AssertionError("replacement resolver executable semantics")
    return self.resolved_event


def _replacement_learning_reconcile(
    self,
    *,
    paper_book_path,
    resolutions,
    settled_ticket_ids,
    at,
):
    if at == "never":
        raise AssertionError("replacement learning handoff executable semantics")
    return ("forged",)


def _replacement_fetch_catalog_page(self, checkpoint):
    return CatalogPage(
        source_id=self.source_id,
        stream_epoch=self.stream_epoch,
        cursor="replacement-catalog",
        position=99,
        events=(),
    )


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
                self.assertIsNotNone(restored.manifest.source_resolver_identity)
            finally:
                restored.close()

    def test_product_learning_handoff_is_proxied_and_authority_roots_are_reproved(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handoff = _LearningHandoff()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                settlement_learning_handoff=handoff,
            )
            proxy = runtime.coordinator.settlement_learning_handoff
            try:
                self.assertIsNot(proxy, handoff)
                self.assertEqual(
                    proxy.prepare_settlement(
                        paper_book_path=root / "paper_book.json",
                        resolutions=(),
                        at="2026-09-20T13:58:00+00:00",
                    ),
                    ("prepared",),
                )
                original_target = handoff.target
                object.__setattr__(handoff, "target", [])
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "settlement learning authority field 'target' changed after composition",
                ):
                    proxy.reconcile_after_settlement(
                        paper_book_path=root / "paper_book.json",
                        resolutions=(),
                        settled_ticket_ids=(),
                        at="2026-09-20T13:58:00+00:00",
                    )
                object.__setattr__(handoff, "target", original_target)
            finally:
                runtime.close()

    def test_product_learning_handoff_rejects_in_call_authority_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handoff = _MutatingLearningHandoff()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                settlement_learning_handoff=handoff,
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "settlement learning authority field 'target' changed after composition",
                ):
                    runtime.coordinator.settlement_learning_handoff.prepare_settlement(
                        paper_book_path=root / "paper_book.json",
                        resolutions=(),
                        at="2026-09-20T13:58:00+00:00",
                    )
            finally:
                runtime.close()

    def test_product_learning_handoff_rejects_class_method_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            handoff = _LearningHandoff()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                settlement_learning_handoff=handoff,
            )
            original = _LearningHandoff.reconcile_after_settlement
            try:
                _LearningHandoff.reconcile_after_settlement = (
                    _replacement_learning_reconcile
                )
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "settlement learning handoff authority changed after product composition",
                ):
                    runtime.coordinator.settlement_learning_handoff.reconcile_after_settlement(
                        paper_book_path=root / "paper_book.json",
                        resolutions=(),
                        settled_ticket_ids=(),
                        at="2026-09-20T13:58:00+00:00",
                    )
            finally:
                _LearningHandoff.reconcile_after_settlement = original
                runtime.close()

    def test_settlement_learning_handoff_identity_is_stable_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                settlement_learning_handoff=_LearningHandoff(),
            )
            expected = first.manifest.settlement_learning_handoff_identity
            first.close()

            restarted = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                settlement_learning_handoff=_LearningHandoff(),
            )
            try:
                self.assertIsNotNone(expected)
                self.assertEqual(
                    restarted.manifest.settlement_learning_handoff_identity,
                    expected,
                )
            finally:
                restarted.close()

    def test_restart_rejects_removed_settlement_learning_handoff(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                settlement_learning_handoff=_LearningHandoff(),
            )
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "settlement learning handoff identity conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

    def test_restart_rejects_changed_settlement_learning_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                settlement_learning_handoff=_LearningHandoff("c" * 64),
            )
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "settlement learning handoff identity conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                    settlement_learning_handoff=_LearningHandoff("d" * 64),
                )

    def test_restart_rejects_changed_settlement_learning_semantics(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
                settlement_learning_handoff=_LearningHandoff(),
            )
            runtime.close()
            original = _LearningHandoff.reconcile_after_settlement
            try:
                _LearningHandoff.reconcile_after_settlement = (
                    _replacement_learning_reconcile
                )
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "settlement learning handoff identity conflicts with durable product composition",
                ):
                    build_autonomous_product_runtime(
                        workspace=root,
                        source=_Source(),
                        clock=_Clock(),
                        sleep=lambda _: None,
                        initial_bankroll="100",
                        settlement_learning_handoff=_LearningHandoff(),
                    )
            finally:
                _LearningHandoff.reconcile_after_settlement = original

    def test_product_collector_rejects_post_build_source_reassignment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_autonomous_product_runtime(
                workspace=Path(directory),
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "product collector authority field 'source' is immutable",
                ):
                    runtime.collector.source = object()
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "product collector authority field '_source_identity' is immutable",
                ):
                    runtime.collector._source_identity = object()
                self.assertEqual(runtime.status().source_id, "provider-a")
            finally:
                runtime.close()

    def test_product_collector_detects_coordinated_direct_source_tamper_before_cycle(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_autonomous_product_runtime(
                workspace=Path(directory),
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            raw = runtime.collector.__dict__
            original_source = raw["source"]
            original_identity = raw["_source_identity"]
            forged = object()
            try:
                raw["source"] = forged
                raw["_source_identity"] = forged
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "product collector authority field 'source' changed after composition",
                ):
                    runtime.tick()
                self.assertEqual(runtime.lifecycle.records(), ())
            finally:
                raw["source"] = original_source
                raw["_source_identity"] = original_identity
                runtime.close()

    def test_product_runtime_authority_graph_is_immutable_after_build(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_autonomous_product_runtime(
                workspace=Path(directory),
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "product runtime authority field 'coordinator' is immutable",
                ):
                    runtime.coordinator = object()
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "product runtime authority field 'market_store' is immutable",
                ):
                    runtime.market_store = object()
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "product runtime authority field 'tick' is immutable",
                ):
                    runtime.tick = lambda: None
                self.assertEqual(runtime.status().source_id, "provider-a")
            finally:
                runtime.close()

    def test_product_runtime_rejects_object_setattr_authority_bypass(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = build_autonomous_product_runtime(
                workspace=Path(directory),
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            original = runtime.coordinator
            try:
                object.__setattr__(runtime, "coordinator", object())
                self.assertIs(runtime.coordinator, original)
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "product runtime authority field 'coordinator' changed after composition",
                ):
                    runtime.status()
            finally:
                object.__setattr__(runtime, "coordinator", original)
                runtime.close()

    def test_builder_ignores_product_desktop_consumer_module_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forged_calls = []

            def forged_consumer(*_args, **_kwargs):
                forged_calls.append(True)
                raise AssertionError("module rebind must not replace product consumer")

            with patch.object(
                product_runtime_module,
                "_ProductDesktopDeltaConsumer",
                forged_consumer,
                create=True,
            ):
                runtime = build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
            try:
                self.assertEqual(forged_calls, [])
                desktop = runtime.coordinator.desktop_consumer
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "product desktop authority field",
                ):
                    desktop.resolve_event = lambda _delta: None
            finally:
                runtime.close()

    def test_builder_closure_binds_canonical_composition_constructors(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            forged_calls = []

            def forged(name):
                def fail(*_args, **_kwargs):
                    forged_calls.append(name)
                    raise AssertionError(f"{name} module rebind must be ignored")

                return fail

            replacements = {
                "PaperBook": forged("PaperBook"),
                "_ProductRuntimeLease": forged("_ProductRuntimeLease"),
                "_source_resolver_identity": forged("_source_resolver_identity"),
                "_settlement_authority_identity": forged("_settlement_authority_identity"),
                "_ManifestStore": forged("_ManifestStore"),
                "ContinuousEventLifecycle": forged("ContinuousEventLifecycle"),
                "SQLiteMarketStore": forged("SQLiteMarketStore"),
                "MarketMirror": forged("MarketMirror"),
                "BoundedMirrorInvalidationBuffer": forged(
                    "BoundedMirrorInvalidationBuffer"
                ),
                "MarketEventBus": forged("MarketEventBus"),
                "SourceHealthStore": forged("SourceHealthStore"),
                "FocusedMirrorDependencyIndex": forged(
                    "FocusedMirrorDependencyIndex"
                ),
                "CollectorDeltaStore": forged("CollectorDeltaStore"),
                "HeadlessCollectorService": forged("HeadlessCollectorService"),
                "DesktopDeltaCheckpointStore": forged(
                    "DesktopDeltaCheckpointStore"
                ),
                "ContinuousSessionCoordinator": forged(
                    "ContinuousSessionCoordinator"
                ),
                "_ProductStartTransitionStore": forged(
                    "_ProductStartTransitionStore"
                ),
            }
            with patch.multiple(product_runtime_module, **replacements):
                runtime = build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
            try:
                self.assertEqual(forged_calls, [])
                self.assertEqual(runtime.status().source_id, "provider-a")
            finally:
                runtime.close()

    def test_builder_closure_binds_canonical_application_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event()
            delta = _delta(event)
            forged_calls = []

            def forged_application(*_args, **_kwargs):
                forged_calls.append("constructor")
                raise AssertionError("module application rebind must be ignored")

            def forged_apply(*_args, **_kwargs):
                forged_calls.append("apply")
                raise AssertionError("class apply rebind must be ignored")

            def forged_lookup(*_args, **_kwargs):
                forged_calls.append("lookup")
                return None

            with (
                patch.object(
                    product_runtime_module,
                    "CanonicalDesktopApplication",
                    forged_application,
                ),
                patch.object(
                    causal_collector_legacy_module.CanonicalDesktopApplication,
                    "apply",
                    forged_apply,
                ),
                patch.object(
                    causal_collector_legacy_module.CanonicalDesktopApplication,
                    "lookup_receipt",
                    forged_lookup,
                ),
            ):
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
                        runtime.coordinator.desktop_consumer.drain(as_of=clock.value),
                        (delta.delta_id,),
                    )
                finally:
                    runtime.close()

            self.assertEqual(forged_calls, [])

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
                raw_desktop_state = object.__getattribute__(desktop, "__dict__")
                raw_desktop_state["apply_event"] = lambda _delta, _event: None
                raw_desktop_state["_product_authority_snapshot"] = tuple(
                    (name, raw_desktop_state.get(name))
                    for name in desktop._SNAPSHOT_FIELDS
                )
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
                raw_desktop_state["apply_event"] = original_apply
                raw_desktop_state.pop("_product_authority_snapshot", None)

                raw_desktop_state["drain"] = lambda **_kwargs: ()
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "drain authority changed",
                ):
                    desktop.drain(as_of=clock.value)
                raw_desktop_state.pop("drain")

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
                recovered = desktop.lookup_application_receipt(delta)
                self.assertEqual(recovered, receipt)
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

    def test_post_delivery_ack_failure_retries_receipt_without_duplicate_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            event = _event()
            delta = _delta(event)
            clock_values = iter(
                (
                    "2026-09-20T13:58:00+00:00",
                    "2026-09-20T13:58:00+00:00",
                    "2026-09-20T13:58:02+00:00",
                    "2026-09-20T13:58:01+00:00",
                    "2026-09-20T13:58:03+00:00",
                    "2026-09-20T13:58:04+00:00",
                )
            )
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(resolved_event=event),
                clock=lambda: next(clock_values),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                desktop = runtime.coordinator.desktop_consumer
                with self.assertRaisesRegex(
                    ApplicationReceiptError,
                    "clock moved backward after receipt delivery",
                ):
                    desktop.drain(as_of="2026-09-20T13:58:00+00:00")

                checkpoint = DesktopDeltaCheckpointStore(
                    root / "desktop_acks.json"
                )
                self.assertFalse(checkpoint.has_ack(delta.delta_id))
                self.assertEqual(runtime.mirror.snapshot(), (event,))
                self.assertEqual(runtime.invalidations.pending_count, 1)
                self.assertEqual(len(runtime.market_store.events(event.event_id)), 1)
                health_after_first = SourceHealthStore(
                    root / "source_health.json"
                ).get(event.source_id)
                self.assertEqual(health_after_first.poll_count, 1)

                self.assertEqual(
                    desktop.drain(as_of="2026-09-20T13:58:00+00:00"),
                    (delta.delta_id,),
                )
                self.assertTrue(checkpoint.has_ack(delta.delta_id))
                self.assertEqual(runtime.mirror.snapshot(), (event,))
                self.assertEqual(runtime.invalidations.pending_count, 1)
                self.assertEqual(len(runtime.market_store.events(event.event_id)), 1)
                health_after_retry = SourceHealthStore(
                    root / "source_health.json"
                ).get(event.source_id)
                self.assertEqual(health_after_retry, health_after_first)
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

    def test_source_resolver_identity_rejects_malformed_configuration_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                ProductCompositionError,
                "product_source_configuration_sha256 must be lowercase SHA-256 hex",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(configuration_sha256="not-a-digest"),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
            self.assertFalse((root / "product_composition.json").exists())

    def test_source_resolver_identity_rejects_opaque_instance_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                ProductCompositionError,
                "canonical object attribute lookup",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_OpaqueResolverDispatchSource(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
            self.assertFalse((root / "product_composition.json").exists())

    def test_source_resolver_identity_rejects_fallback_attribute_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(
                ProductCompositionError,
                "fallback attribute dispatch",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_FallbackResolverDispatchSource(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
            self.assertFalse((root / "product_composition.json").exists())

    def test_source_identity_rejects_instance_acquisition_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _Source()
            source.fetch_catalog_page = lambda _checkpoint: CatalogPage(
                source_id=source.source_id,
                stream_epoch=source.stream_epoch,
                cursor="forged",
                position=0,
                events=(),
            )
            with self.assertRaisesRegex(
                ProductCompositionError,
                "per-instance fetch_catalog_page shadowing",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
            self.assertFalse((root / "product_composition.json").exists())

    def test_source_resolver_identity_rejects_instance_resolver_shadow(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _Source()
            source.resolve_event = lambda _delta: _event()
            with self.assertRaisesRegex(
                ProductCompositionError,
                "per-instance resolve_event shadowing",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=source,
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )
            self.assertFalse((root / "product_composition.json").exists())

    def test_catalog_evidence_is_not_published_if_source_authority_changes_in_fetch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _MutatingCatalogSource(configuration_sha256="1" * 64)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "source resolver authority changed after product composition",
                ):
                    runtime.tick()

                self.assertEqual(runtime.lifecycle.records(), ())
                self.assertEqual(
                    runtime.collector.delta_store.deltas_after_commit(
                        source_id=source.source_id,
                    ),
                    (),
                )
            finally:
                runtime.close()

    def test_delta_fetch_result_is_not_released_if_source_authority_changes(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _MutatingDeltaSource(configuration_sha256="1" * 64)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "source resolver authority changed after product composition",
                ):
                    runtime.collector.source.fetch_deltas(None, (), 10)

                self.assertEqual(
                    runtime.collector.delta_store.deltas_after_commit(
                        source_id=source.source_id,
                    ),
                    (),
                )
            finally:
                runtime.close()

    def test_catalog_page_is_not_released_if_stream_epoch_changes_during_fetch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _MutatingCatalogEpochSource()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "source stream_epoch changed during catalog acquisition",
                ):
                    runtime.tick()
                self.assertEqual(runtime.lifecycle.records(), ())
            finally:
                runtime.close()

    def test_delta_result_is_not_released_if_stream_epoch_changes_during_fetch(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _MutatingDeltaEpochSource()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "source stream_epoch changed during delta acquisition",
                ):
                    runtime.collector.source.fetch_deltas(None, (), 10)
                self.assertEqual(
                    runtime.collector.delta_store.deltas_after_commit(
                        source_id=source.source_id,
                    ),
                    (),
                )
            finally:
                runtime.close()

    def test_resolver_result_is_not_released_if_source_authority_changes_during_resolution(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event()
            delta = _delta(event)
            source = _MutatingResolverSource(
                resolved_event=event,
                configuration_sha256="1" * 64,
            )
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
                    ProductCompositionError,
                    "source resolver authority changed after product composition",
                ):
                    runtime.coordinator.desktop_consumer.drain(as_of=clock.value)

                self.assertFalse(
                    DesktopDeltaCheckpointStore(
                        root / "desktop_acks.json"
                    ).has_ack(delta.delta_id)
                )
                self.assertEqual(runtime.market_store.events(event.event_id), [])
                self.assertEqual(
                    SourceHealthStore(root / "source_health.json")
                    .get(event.source_id)
                    .poll_count,
                    0,
                )
            finally:
                runtime.close()

    def test_tick_rejects_post_build_acquisition_rebind_before_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = _Source()
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            original = _Source.fetch_catalog_page
            before = runtime.collector.status()
            try:
                _Source.fetch_catalog_page = _replacement_fetch_catalog_page
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "source resolver authority changed after product composition",
                ):
                    runtime.tick()
                self.assertEqual(runtime.collector.status(), before)
            finally:
                _Source.fetch_catalog_page = original
                runtime.close()

    def test_desktop_resolution_rejects_post_build_resolver_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event()
            delta = _delta(event)
            source = _Source(resolved_event=event)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            original = _Source.resolve_event
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                _Source.resolve_event = _replacement_source_resolve_event
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "source resolver authority changed after product composition",
                ):
                    runtime.coordinator.desktop_consumer.drain(as_of=clock.value)

                self.assertFalse(
                    DesktopDeltaCheckpointStore(
                        root / "desktop_acks.json"
                    ).has_ack(delta.delta_id)
                )
                self.assertEqual(runtime.market_store.events(event.event_id), [])
            finally:
                _Source.resolve_event = original
                runtime.close()

    def test_desktop_resolution_rejects_post_build_source_config_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event()
            delta = _delta(event)
            source = _Source(resolved_event=event, configuration_sha256="1" * 64)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                source.product_source_configuration_sha256 = "2" * 64
                with self.assertRaisesRegex(
                    ProductCompositionError,
                    "source resolver authority changed after product composition",
                ):
                    runtime.coordinator.desktop_consumer.drain(as_of=clock.value)

                self.assertFalse(
                    DesktopDeltaCheckpointStore(
                        root / "desktop_acks.json"
                    ).has_ack(delta.delta_id)
                )
                self.assertEqual(runtime.market_store.events(event.event_id), [])
                self.assertEqual(
                    SourceHealthStore(root / "source_health.json")
                    .get(event.source_id)
                    .poll_count,
                    0,
                )
            finally:
                runtime.close()

    def test_restart_allows_legitimate_stream_epoch_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first_source = _Source()
            first_source.stream_epoch = "epoch-1"
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=first_source,
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            original_identity = runtime.manifest.source_resolver_identity
            runtime.close()

            second_source = _Source()
            second_source.stream_epoch = "epoch-2"
            restored = build_autonomous_product_runtime(
                workspace=root,
                source=second_source,
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertEqual(
                    restored.manifest.source_resolver_identity,
                    original_identity,
                )
                self.assertEqual(restored.collector.source.stream_epoch, "epoch-2")
            finally:
                restored.close()

    def test_restart_rejects_changed_source_config_with_same_resolver(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(configuration_sha256="1" * 64),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            original_identity = runtime.manifest.source_resolver_identity
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "source resolver identity conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(configuration_sha256="2" * 64),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

            self.assertIsNotNone(original_identity)

    def test_restart_rejects_changed_resolver_with_same_source_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            original_identity = runtime.manifest.source_resolver_identity
            runtime.close()

            with self.assertRaisesRegex(
                ProductCompositionError,
                "source resolver identity conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_AlternateResolverSource(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

            self.assertIsNotNone(original_identity)

    def test_version_three_manifest_without_learning_identity_reopens_without_handoff(
        self,
    ) -> None:
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

            manifest_path = root / "product_composition.json"
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw["schema_version"] = 3
            raw.pop("settlement_learning_handoff_identity")
            manifest_path.write_text(
                json.dumps(raw, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            restored = build_autonomous_product_runtime(
                workspace=root,
                source=_Source(),
                clock=_Clock(),
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertIsNone(
                    restored.manifest.settlement_learning_handoff_identity
                )
            finally:
                restored.close()

    def test_version_three_manifest_rejects_new_learning_authority(self) -> None:
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

            manifest_path = root / "product_composition.json"
            raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            raw["schema_version"] = 3
            raw.pop("settlement_learning_handoff_identity")
            manifest_path.write_text(
                json.dumps(raw, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ProductCompositionError,
                "settlement learning handoff identity conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                    settlement_learning_handoff=_LearningHandoff(),
                )

    def test_legacy_manifest_without_source_resolver_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            root.mkdir(parents=True, exist_ok=True)
            (root / "product_composition.json").write_text(
                '{"schema":"autosport.autonomous_product_composition",'
                '"schema_version":2,"source_id":"provider-a",'
                '"initial_bankroll":"100","settlement_authority_identity":null}',
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                ProductCompositionError,
                "source resolver identity conflicts with durable product composition",
            ):
                build_autonomous_product_runtime(
                    workspace=root,
                    source=_Source(),
                    clock=_Clock(),
                    sleep=lambda _: None,
                    initial_bankroll="100",
                )

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

            runtime = build_autonomous_product_runtime(
                workspace=root,
                source=source,
                clock=clock,
                sleep=lambda _: None,
                initial_bankroll="100",
            )
            try:
                self.assertTrue(runtime.collector.delta_store.append(delta))
                with patch.object(
                    MarketEventBus,
                    "_notify",
                    side_effect=RuntimeError("crash-after-market-persist"),
                ):
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
