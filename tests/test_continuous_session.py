from __future__ import annotations

import json
import tempfile
import unittest
from datetime import timedelta
from unittest.mock import patch
from decimal import Decimal
from pathlib import Path

from autosport.causal_collector import (
    CollectorDelta,
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
    GapState,
    GapStateError,
    SyncState,
    canonical_event_digest,
)
from autosport.agent_loop import AgentLoopPhase, AgentLoopRuntime, ExternalEffectState
from autosport.collector_service import HeadlessCollectorService
import autosport.continuous_session as continuous_session_module
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SessionPausedError,
    SettlementResolution,
    SessionState,
)
from autosport.decision_ledger import (
    ECONOMIC_DECISION_KIND,
    DecisionRecord,
    EconomicDecisionAuthority,
    JsonlDecisionLedger,
)
from autosport.domain import MarketEvent, MarketType, TicketLeg
from autosport.economic_goal import EconomicGoalContract
from autosport.economic_goal_provenance import provenance_for
from autosport.event_lifecycle import CatalogEvent, CatalogPage, ContinuousEventLifecycle, EventPhase
from autosport.market_bus import MarketEventBus
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    MarketMirror,
)
from autosport.learning_environment import (
    Action,
    CausalLearningEnvironment,
    EnvironmentIdentity,
    Observation,
)
from autosport.paper import PaperBook
from autosport.paper_settlement_learning import PaperSettlementLearningBridge
from autosport.providers import ProviderUnavailableError
from autosport.risk import PaperRiskPolicy
from autosport.storage import SQLiteMarketStore
from autosport.workspace_lock import (
    WorkspaceEconomicLock,
    WorkspaceEconomicLockBusyError,
)


class _Clock:
    def __init__(self) -> None:
        self.value = "2026-09-19T21:20:00+00:00"

    def __call__(self) -> str:
        return self.value


class _Source:
    source_id = "provider-a"
    stream_epoch = "epoch-1"

    def __init__(self, page: CatalogPage) -> None:
        self.page = page
        self.catalog_calls = 0

    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        return self.page

    def fetch_deltas(self, checkpoint, records, max_items):
        return ()


class _UnavailableSource(_Source):
    def fetch_catalog_page(self, checkpoint):
        self.catalog_calls += 1
        raise ProviderUnavailableError("provider unavailable")


class _DeltaSource(_Source):
    def __init__(self, page: CatalogPage, batches: list[tuple[CollectorDelta, ...]]) -> None:
        super().__init__(page)
        self.batches = list(batches)
        self.delta_calls = 0

    def fetch_deltas(self, checkpoint, records, max_items):
        index = min(self.delta_calls, len(self.batches) - 1)
        self.delta_calls += 1
        return self.batches[index]


class _OutcomeAuthority:
    def __init__(self, resolution: SettlementResolution) -> None:
        self.resolution = resolution
        self.calls = 0

    def resolve(self, record, *, as_of: str):
        self.calls += 1
        return self.resolution


class _MappedOutcomeAuthority:
    def __init__(self, resolutions: dict[str, SettlementResolution]) -> None:
        self.resolutions = dict(resolutions)

    def resolve(self, record, *, as_of: str):
        return self.resolutions.get(record.identity)


class _OneShotOutcomeAuthority:
    def __init__(self, resolution: SettlementResolution) -> None:
        self.resolution = resolution
        self.calls = 0

    def resolve(self, record, *, as_of: str):
        self.calls += 1
        if self.calls == 1:
            return self.resolution
        return None


def _event(
    *,
    phase: EventPhase,
    settlement_ref: str | None = None,
    event_id: str = "event-1",
) -> CatalogEvent:
    return CatalogEvent(
        source_id="provider-a",
        sport="table_tennis",
        event_id=event_id,
        phase=phase,
        available_at="2026-09-19T21:19:00+00:00",
        scheduled_start_at="2026-09-19T21:00:00+00:00",
        settlement_ref=settlement_ref,
    )


def _market_event(*, event_id: str = "event-1") -> MarketEvent:
    return MarketEvent(
        event_id=event_id,
        market_id="winner",
        selection_id="home",
        decimal_odds=Decimal("2.00"),
        observed_ts="2026-09-19T21:19:00+00:00",
        source_id="provider-a",
        sequence=1,
        market_type=MarketType.WINNER,
        status="open",
        source_ts="2026-09-19T21:19:00+00:00",
        ingest_ts="2026-09-19T21:19:00+00:00",
        sport="table_tennis",
    )


def _collector_delta(
    *,
    delta_id: str,
    gap_state: GapState,
    sync_state: SyncState,
    revision_of: str | None = None,
    revision_number: int = 0,
) -> CollectorDelta:
    event = _market_event(event_id="provider-a:event-1")
    return CollectorDelta(
        schema_version=1,
        delta_id=delta_id,
        source_id="provider-a",
        lawful_terms_ref="terms:provider-a:v1",
        retention_ref="retention:provider-a:v1",
        stream_epoch="epoch-1",
        source_cursor="1",
        cursor_position=1,
        event_dedupe_key=event.dedupe_key,
        event_id=event.event_id,
        source_payload_digest="a" * 64,
        canonical_event_digest=canonical_event_digest(event),
        source_observed_at="2026-09-19T21:19:00+00:00",
        collector_received_at="2026-09-19T21:19:01+00:00",
        collector_committed_at="2026-09-19T21:19:02+00:00",
        desktop_available_at="2026-09-19T21:19:03+00:00",
        revision_of=revision_of,
        revision_number=revision_number,
        gap_state=gap_state,
        sync_state=sync_state,
        gap_from_cursor=(
            "0" if gap_state in {GapState.DETECTED, GapState.RECOVERED} else None
        ),
        gap_to_cursor=(
            "1" if gap_state in {GapState.DETECTED, GapState.RECOVERED} else None
        ),
    )


def _build_coordinator(
    root: Path,
    source: _Source,
    clock: _Clock,
    *,
    outcome_authority=None,
    settlement_learning_handoff=None,
):
    market_store = SQLiteMarketStore(root / "market.db")
    lifecycle = ContinuousEventLifecycle(root / "catalog.json")
    mirror = MarketMirror()
    invalidations = BoundedMirrorInvalidationBuffer(mirror, max_dirty_keys=1)
    dependencies = FocusedMirrorDependencyIndex(mirror)
    collector_store = CollectorDeltaStore(root / "collector_deltas.json")
    collector = HeadlessCollectorService(
        delta_store=collector_store,
        lifecycle=lifecycle,
        source=source,
        state_path=root / "collector_state.json",
        run_id="collector-run-1",
        clock=clock,
        sleep=lambda _: None,
    )
    desktop = DesktopDeltaConsumer(
        collector_store,
        DesktopDeltaCheckpointStore(root / "desktop_acks.json"),
        resolve_event=lambda delta: _market_event(event_id=delta.event_id),
        apply_event=lambda delta, event: DesktopApplicationReceipt(
            delta_id=delta.delta_id,
            canonical_event_digest=canonical_event_digest(event),
            receipt_id=f"test-receipt:{delta.delta_id}",
            applied_at=clock(),
        ),
        lookup_application_receipt=lambda delta: None,
    )
    coordinator = ContinuousSessionCoordinator(
        workspace=root,
        collector=collector,
        lifecycle=lifecycle,
        market_store=market_store,
        desktop_consumer=desktop,
        invalidation_buffer=invalidations,
        dependency_index=dependencies,
        outcome_authority=outcome_authority,
        settlement_learning_handoff=settlement_learning_handoff,
        session_id="session-1",
        clock=clock,
        initial_bankroll="100",
    )
    return coordinator, market_store, lifecycle, mirror, invalidations, dependencies


class ContinuousSessionCoordinatorTests(unittest.TestCase):
    def test_tick_registers_new_event_and_persists_checkpoint_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-1",
                position=1,
                events=(_event(phase=EventPhase.PRE_MATCH),),
            )
            source = _Source(page)
            coordinator, store, _lifecycle, _mirror, _invalidations, dependencies = _build_coordinator(
                root, source, clock
            )
            try:
                store.append(_market_event(event_id="provider-a:event-1"))
                result = coordinator.tick()
                self.assertEqual(result.cycle_index, 1)
                self.assertEqual(result.registered_input_ids, ("catalog:provider-a:event-1",))
                self.assertEqual(dependencies.input_ids, ("catalog:provider-a:event-1",))
                self.assertEqual(coordinator.status().cycles_completed, 1)

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock
                )
                try:
                    self.assertEqual(restarted.session_id, "session-1")
                    self.assertEqual(restarted.status().cycles_completed, 1)
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_mixed_pre_match_live_and_late_events_share_one_session_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(
                        _event(phase=EventPhase.PRE_MATCH, event_id="event-1"),
                        _event(phase=EventPhase.LIVE, event_id="event-2"),
                    ),
                )
            )
            coordinator, store, lifecycle, _mirror, _invalidations, dependencies = _build_coordinator(
                root, source, clock
            )
            try:
                for event_id in ("event-1", "event-2", "event-3"):
                    store.append(_market_event(event_id=f"provider-a:{event_id}"))

                first = coordinator.tick()
                self.assertIn("catalog:provider-a:event-1", first.registered_input_ids)
                self.assertIn("catalog:provider-a:event-2", first.registered_input_ids)
                self.assertEqual(lifecycle.get("provider-a:event-2").phase, EventPhase.LIVE)

                source.page = CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-2",
                    position=2,
                    events=(
                        _event(phase=EventPhase.LIVE, event_id="event-1"),
                        _event(phase=EventPhase.LIVE, event_id="event-2"),
                        _event(phase=EventPhase.PRE_MATCH, event_id="event-3"),
                    ),
                )
                second = coordinator.tick()
                self.assertIn("catalog:provider-a:event-3", second.registered_input_ids)
                records = lifecycle.records()
                self.assertEqual(len(records), 3)
                self.assertEqual(len({item.identity for item in records}), 3)
                self.assertEqual(lifecycle.get("provider-a:event-1").phase, EventPhase.LIVE)

                restarted, restarted_store, restarted_lifecycle, *_rest = _build_coordinator(
                    root, source, clock
                )
                try:
                    third = restarted.tick()
                    self.assertEqual(restarted.session_id, "session-1")
                    self.assertEqual(len(restarted_lifecycle.records()), 3)
                    self.assertEqual(
                        len({item.identity for item in restarted_lifecycle.records()}),
                        3,
                    )
                    self.assertFalse(third.source_provider_unavailable)
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_provider_unavailable_is_reported_without_fresh_session_success(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _UnavailableSource(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                result = coordinator.tick()
                self.assertTrue(result.source_provider_unavailable)
                self.assertEqual(result.cycle_index, 0)
                self.assertIsNone(result.last_success_at)
                self.assertEqual(result.committed_delta_ids, ())
                self.assertEqual(result.delivered_delta_ids, ())

                status = coordinator.status()
                self.assertEqual(status.cycles_completed, 0)
                self.assertTrue(status.source_provider_unavailable)
                self.assertEqual(status.source_last_error_code, "ProviderUnavailableError")
                self.assertIsNone(status.source_last_success_at)
            finally:
                store.close()

    def test_pause_is_durable_and_resume_continues_same_session(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, _lifecycle, _mirror, _invalidations, _dependencies = _build_coordinator(
                root, source, clock
            )
            try:
                coordinator.pause()
                self.assertEqual(coordinator.status().state, SessionState.PAUSED)
                with self.assertRaises(SessionPausedError):
                    coordinator.tick()

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock
                )
                try:
                    self.assertEqual(restarted.status().state, SessionState.PAUSED)
                    restarted.resume()
                    self.assertEqual(restarted.status().state, SessionState.RUNNING)
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_continuous_state_mutation_obeys_workspace_economic_writer_lock(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                with WorkspaceEconomicLock(root):
                    with self.assertRaises(WorkspaceEconomicLockBusyError):
                        coordinator.pause()
                self.assertEqual(coordinator.status().state, SessionState.RUNNING)
                coordinator.pause()
                self.assertEqual(coordinator.status().state, SessionState.PAUSED)
            finally:
                store.close()

    def test_completed_event_waits_for_external_outcome_and_settles_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:1",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )

            book = PaperBook("100")
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")

            quote_key = leg.quote_key
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:1",
                quote_outcomes={quote_key: "win"},
                evidence_id="outcome-1",
                evidence_sha256="0" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)

            coordinator, store, _lifecycle, _mirror, _invalidations, _dependencies = _build_coordinator(
                root, source, clock, outcome_authority=authority
            )
            try:
                first = coordinator.tick()
                self.assertEqual(first.settled_ticket_ids, tuple(book.tickets))
                settled = PaperBook.load(root / "paper_book.json")
                self.assertEqual(settled.balance, Decimal("110"))
                settled_ticket = settled.tickets[first.settled_ticket_ids[0]]
                self.assertEqual(settled_ticket.settled_at, clock.value)

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock, outcome_authority=authority
                )
                try:
                    second = restarted.tick()
                    self.assertEqual(second.settled_ticket_ids, ())
                    settled_again = PaperBook.load(root / "paper_book.json")
                    self.assertEqual(settled_again.balance, Decimal("110"))
                    self.assertEqual(
                        settled_again.tickets[first.settled_ticket_ids[0]].settled_at,
                        clock.value,
                    )
                    self.assertEqual(authority.calls, 2)
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_shared_lifecycle_foreign_source_is_outside_session_settlement_authority(
        self,
    ) -> None:
        class _RecordingAuthority:
            def __init__(self) -> None:
                self.calls: list[str] = []

            def resolve(self, record, *, as_of: str):
                self.calls.append(record.identity)
                return None

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            foreign_event = CatalogEvent(
                source_id="provider-b",
                sport="table_tennis",
                event_id="event-foreign",
                phase=EventPhase.COMPLETED,
                available_at="2026-09-19T21:19:00+00:00",
                scheduled_start_at="2026-09-19T21:00:00+00:00",
                settlement_ref="provider-b-result:foreign",
            )
            foreign_page = CatalogPage(
                source_id="provider-b",
                stream_epoch="epoch-b",
                cursor="cursor-b",
                position=1,
                events=(foreign_event,),
            )
            leg = TicketLeg(
                event_id="event-foreign",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _RecordingAuthority()
            coordinator, store, lifecycle, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                lifecycle.apply_page(foreign_page, discovered_at=clock())
                result = coordinator.tick()
                self.assertEqual(authority.calls, [])
                self.assertEqual(result.settled_ticket_ids, ())
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_explicit_ticket_provider_provenance_fences_foreign_settlement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:foreign-fence",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-b",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:foreign-fence",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="foreign-provider-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, ())
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
                self.assertEqual(coordinator.status().cycles_completed, 1)
            finally:
                store.close()

    def test_settlement_scope_rebind_cannot_authorize_legacy_ticket_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:scope-rebind",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:scope-rebind",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="scope-rebind-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                coordinator._open_quote_keys_for_book = (
                    lambda *_args, **_kwargs: {leg.quote_key}
                )
                with patch.object(
                    continuous_session_module,
                    "_canonical_open_quote_keys_for_book",
                    lambda *_args, **_kwargs: {leg.quote_key},
                ):
                    result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, ())
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_settlement_scope_injection_argument_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                with self.assertRaisesRegex(
                    TypeError,
                    "settlement scope origin is internal product authority",
                ):
                    coordinator._settle(
                        resolutions=(),
                        settled_at=clock(),
                        _settlement_scope_resolver=lambda *_args: set(),
                    )
            finally:
                store.close()

    def test_continuous_session_authority_fields_are_immutable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                authorities = {
                    "workspace": coordinator.workspace,
                    "collector": coordinator.collector,
                    "lifecycle": coordinator.lifecycle,
                    "market_store": coordinator.market_store,
                    "desktop_consumer": coordinator.desktop_consumer,
                    "paper_book_path": coordinator.paper_book_path,
                    "outcome_authority": coordinator.outcome_authority,
                    "settlement_learning_handoff": (
                        coordinator.settlement_learning_handoff
                    ),
                    "_settlement_prepared_resolutions": (
                        coordinator._settlement_prepared_resolutions
                    ),
                    "_settlement_source_id": coordinator._settlement_source_id,
                    "clock": coordinator.clock,
                    "causal_view": coordinator.causal_view,
                    "initial_bankroll": coordinator.initial_bankroll,
                    "_state": coordinator._state,
                }
                for name, value in authorities.items():
                    with self.subTest(name=name):
                        with self.assertRaisesRegex(
                            ContinuousSessionError,
                            f"authority field {name} is immutable",
                        ):
                            setattr(coordinator, name, value)
            finally:
                store.close()

    def test_continuous_session_authority_deletion_cannot_reopen_setattr(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "authority field _authority_fields_sealed is immutable",
                ):
                    del coordinator._authority_fields_sealed
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "authority field _outcome_resolver is immutable",
                ):
                    coordinator._outcome_resolver = lambda *_args, **_kwargs: None
            finally:
                store.close()

    def test_settlement_resolution_collection_deletion_is_sealed(self) -> None:
        original = ContinuousSessionCoordinator._settlement_resolutions
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            del ContinuousSessionCoordinator._settlement_resolutions
        self.assertIs(
            ContinuousSessionCoordinator._settlement_resolutions,
            original,
        )

    def test_continuous_session_attribute_guard_deletion_is_sealed(self) -> None:
        for name in ("__setattr__", "__delattr__"):
            with self.subTest(name=name):
                original = getattr(ContinuousSessionCoordinator, name)
                with self.assertRaisesRegex(
                    TypeError,
                    "canonical settlement consumer entry binding is immutable",
                ):
                    delattr(ContinuousSessionCoordinator, name)
                self.assertIs(getattr(ContinuousSessionCoordinator, name), original)

    def test_continuous_session_authority_setattr_dispatch_is_sealed(self) -> None:
        original = ContinuousSessionCoordinator.__setattr__
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            ContinuousSessionCoordinator.__setattr__ = object.__setattr__
        self.assertIs(ContinuousSessionCoordinator.__setattr__, original)

    def test_tick_rejects_resolution_subclass_before_durable_evidence(self) -> None:
        class DerivedSettlementResolution(SettlementResolution):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:subclass",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            authority = _OutcomeAuthority(
                DerivedSettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:subclass",
                    quote_outcomes={"event-1|winner|home": "win"},
                    evidence_id="subclass-evidence",
                    evidence_sha256="b" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "canonical SettlementResolution",
                ):
                    coordinator.tick()
                self.assertEqual(coordinator.status().settlement_evidence, ())
            finally:
                store.close()

    def test_tick_rejects_future_evidence_despite_validate_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:future-precommit",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:future-precommit",
                    quote_outcomes={"event-1|winner|home": "win"},
                    evidence_id="future-precommit-evidence",
                    evidence_sha256="c" * 64,
                    available_at="2026-09-19T21:21:00+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                with patch.object(
                    SettlementResolution,
                    "validate",
                    lambda *_args, **_kwargs: None,
                ):
                    with self.assertRaisesRegex(
                        ContinuousSessionError,
                        "failed canonical validation",
                    ):
                        coordinator.tick()
                self.assertEqual(coordinator.status().settlement_evidence, ())
            finally:
                store.close()

    def test_settlement_resolution_collection_dispatch_is_sealed(self) -> None:
        original = ContinuousSessionCoordinator._settlement_resolutions
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            ContinuousSessionCoordinator._settlement_resolutions = lambda *_args, **_kwargs: ()
        self.assertIs(
            ContinuousSessionCoordinator._settlement_resolutions,
            original,
        )

    def test_settlement_collection_rejects_instance_records_retargeting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:lifecycle-records-shadow",
            )
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:lifecycle-records-shadow",
                    quote_outcomes={"event-1|winner|home": "win"},
                    evidence_id="lifecycle-records-shadow",
                    evidence_sha256="9" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, lifecycle, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
                outcome_authority=authority,
            )
            lifecycle.records = lambda: ()
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "lifecycle record dispatch changed",
                ):
                    coordinator._settlement_resolutions(as_of=clock())
                self.assertEqual(authority.calls, 0)
            finally:
                store.close()

    def test_settlement_collection_rejects_lifecycle_read_retargeting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, lifecycle, *_ = _build_coordinator(
                root,
                source,
                clock,
            )
            lifecycle._read = lambda: {
                "schema": "autosport.continuous_event_lifecycle",
                "schema_version": 1,
                "sources": {},
                "events": {},
            }
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "lifecycle record dispatch changed",
                ):
                    coordinator._settlement_resolutions(as_of=clock())
            finally:
                store.close()

    def test_settlement_collection_rejects_lifecycle_class_retargeting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            try:
                with patch.object(
                    ContinuousEventLifecycle,
                    "records",
                    lambda _self: (),
                ):
                    with self.assertRaisesRegex(
                        ContinuousSessionError,
                        "lifecycle record dispatch changed",
                    ):
                        coordinator._settlement_resolutions(as_of=clock())
            finally:
                store.close()

    def test_settlement_collection_rejects_nested_collector_source_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:collector-source-drift",
            )
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:collector-source-drift",
                    quote_outcomes={"event-1|winner|home": "win"},
                    evidence_id="collector-source-drift",
                    evidence_sha256="1" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
                outcome_authority=authority,
            )
            coordinator.collector._source_id = "provider-b"
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "collector source identity changed",
                ):
                    coordinator._settlement_resolutions(as_of=clock())
                self.assertEqual(authority.calls, 0)
                self.assertEqual(coordinator._settlement_source_id, "provider-a")
            finally:
                store.close()

    def test_settlement_collection_rejects_cutoff_before_durable_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:discovery-cutoff",
            )
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:discovery-cutoff",
                    quote_outcomes={"event-1|winner|home": "win"},
                    evidence_id="discovery-cutoff",
                    evidence_sha256="3" * 64,
                    available_at="2026-09-19T21:19:00+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
                outcome_authority=authority,
            )
            try:
                coordinator.tick()
                calls_after_tick = authority.calls
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "discovered after the evidence cutoff",
                ):
                    coordinator._settlement_resolutions(
                        as_of="2026-09-19T21:19:30+00:00"
                    )
                self.assertEqual(authority.calls, calls_after_tick)
            finally:
                store.close()

    def test_settlement_collection_rejects_missing_durable_discovery_chronology(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:missing-discovery",
            )
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:missing-discovery",
                    quote_outcomes={"event-1|winner|home": "win"},
                    evidence_id="missing-discovery",
                    evidence_sha256="5" * 64,
                    available_at="2026-09-19T21:19:00+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
                outcome_authority=authority,
            )
            try:
                coordinator.tick()
                raw = json.loads((root / "catalog.json").read_text(encoding="utf-8"))
                raw["events"][event.identity]["settlement_discovered_at"] = None
                (root / "catalog.json").write_text(
                    json.dumps(raw, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
                calls_after_tick = authority.calls
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "lacks durable discovery chronology",
                ):
                    coordinator._settlement_resolutions(as_of=clock())
                self.assertEqual(authority.calls, calls_after_tick)
            finally:
                store.close()

    def test_outcome_authority_method_retargeting_cannot_change_truth_origin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:resolver-bind",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:resolver-bind",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="resolver-bind-evidence",
                    evidence_sha256="d" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            authority.resolve = lambda *_args, **_kwargs: None
            try:
                result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, (ticket.ticket_id,))
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("110"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "won",
                )
            finally:
                store.close()

    def test_settlement_handoff_method_retargeting_cannot_change_dispatch(self) -> None:
        class Handoff:
            def __init__(self) -> None:
                self.prepare_calls = 0
                self.recovery_calls = 0
                self.reconcile_calls = 0

            def prepare_settlement(self, **_kwargs):
                self.prepare_calls += 1
                return ()

            def prepared_settlement_resolutions(self, **_kwargs):
                self.recovery_calls += 1
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                self.reconcile_calls += 1
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            handoff = Handoff()
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                settlement_learning_handoff=handoff,
            )
            handoff.prepare_settlement = (
                lambda **_kwargs: self.fail("retargeted prepare called")
            )
            handoff.prepared_settlement_resolutions = (
                lambda **_kwargs: self.fail("retargeted recovery called")
            )
            handoff.reconcile_after_settlement = (
                lambda **_kwargs: self.fail("retargeted reconcile called")
            )
            try:
                coordinator.tick()
                self.assertEqual(handoff.prepare_calls, 1)
                self.assertEqual(handoff.recovery_calls, 1)
                self.assertEqual(handoff.reconcile_calls, 1)
            finally:
                store.close()

    def test_pending_settlement_resolution_collection_is_sealed(self) -> None:
        original = ContinuousSessionCoordinator._pending_settlement_resolutions
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            ContinuousSessionCoordinator._pending_settlement_resolutions = (
                lambda *_args, **_kwargs: ()
            )
        self.assertIs(
            ContinuousSessionCoordinator._pending_settlement_resolutions,
            original,
        )

    def test_pending_settlement_retention_scope_is_sealed(self) -> None:
        original = ContinuousSessionCoordinator._retained_pending_settlement_ids
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            ContinuousSessionCoordinator._retained_pending_settlement_ids = (
                lambda *_args, **_kwargs: ()
            )
        self.assertIs(
            ContinuousSessionCoordinator._retained_pending_settlement_ids,
            original,
        )

    def test_pending_retention_scope_injection_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-retention-injection",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            try:
                with self.assertRaisesRegex(
                    TypeError,
                    "settlement scope origin is internal product authority",
                ):
                    coordinator._retained_pending_settlement_ids(
                        resolutions=(),
                        settled_at=clock(),
                        _settlement_scope_resolver=lambda *_args: set(),
                    )
            finally:
                store.close()

    def test_recovered_settlement_resolution_collection_is_sealed(self) -> None:
        original = ContinuousSessionCoordinator._recovered_settlement_resolutions
        with self.assertRaisesRegex(
            TypeError,
            "canonical settlement consumer entry binding is immutable",
        ):
            ContinuousSessionCoordinator._recovered_settlement_resolutions = (
                lambda *_args, **_kwargs: ()
            )
        self.assertIs(
            ContinuousSessionCoordinator._recovered_settlement_resolutions,
            original,
        )

    def test_recovered_settlement_evidence_cannot_mint_new_truth(self) -> None:
        class Handoff:
            def prepared_settlement_resolutions(self, *, paper_book_path):
                return (
                    SettlementResolution(
                        event_identity="provider-a:event-1",
                        settlement_ref="result:recovery-only",
                        quote_outcomes={"event-1|winner|home": "win"},
                        evidence_id="recovery-only-evidence",
                        evidence_sha256="a" * 64,
                        available_at="2026-09-19T21:19:30+00:00",
                    ),
                )

            def reconcile_after_settlement(self, **_kwargs):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-recovery-only",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                settlement_learning_handoff=Handoff(),
            )
            try:
                recovered = Handoff().prepared_settlement_resolutions(
                    paper_book_path=root / "paper_book.json",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "was not durably staged before P&L",
                ):
                    coordinator._state.validate_recovered_settlement_evidence(
                        settlement_evidence=recovered,
                    )
            finally:
                store.close()

    def test_recovered_settlement_cannot_reinterpret_staged_quote_outcomes(self) -> None:
        class Handoff:
            def __init__(self, resolution):
                self.resolution = resolution

            def prepared_settlement_resolutions(self, *, paper_book_path):
                return (self.resolution,)

            def reconcile_after_settlement(self, **_kwargs):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            original = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:recovery-digest",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="recovery-digest-evidence",
                evidence_sha256="b" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            handoff = Handoff(original)
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-recovery-digest",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                settlement_learning_handoff=handoff,
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(original,),
                )
                handoff.resolution = SettlementResolution(
                    event_identity=original.event_identity,
                    settlement_ref=original.settlement_ref,
                    quote_outcomes={"event-1|winner|home": "loss"},
                    evidence_id=original.evidence_id,
                    evidence_sha256=original.evidence_sha256,
                    available_at=original.available_at,
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "settlement outcome interpretation conflicts with durable evidence",
                ):
                    coordinator._state.validate_recovered_settlement_evidence(
                        settlement_evidence=(handoff.resolution,),
                    )
            finally:
                store.close()

    def test_cleared_historical_settlement_cannot_regain_recovery_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-recovery-cleared",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:recovery-cleared",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="recovery-cleared-evidence",
                evidence_sha256="c" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                coordinator._state.complete_pending_settlement_commit(
                    settlement_evidence=(resolution,),
                    retain_pending_evidence_ids=(),
                )
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (),
                )
                self.assertIn(
                    resolution.evidence_id,
                    {
                        item["evidence_id"]
                        for item in coordinator.status().settlement_evidence
                    },
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "no longer pending for recovery",
                ):
                    coordinator._state.validate_recovered_settlement_evidence(
                        settlement_evidence=(resolution,),
                    )
            finally:
                store.close()

    def test_recovered_settlement_state_rejects_duplicate_evidence_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-recovery-duplicate",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:recovery-duplicate",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="recovery-duplicate-evidence",
                evidence_sha256="d" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "repeats evidence_id",
                ):
                    coordinator._state.validate_recovered_settlement_evidence(
                        settlement_evidence=(resolution, resolution),
                    )
            finally:
                store.close()

    def test_prepared_recovery_requires_durable_completed_lifecycle(self) -> None:
        class Handoff:
            def prepared_settlement_resolutions(self, *, paper_book_path):
                del paper_book_path
                return (
                    SettlementResolution(
                        event_identity="provider-a:event-1",
                        settlement_ref="result:recovery-lifecycle",
                        quote_outcomes={"event-1|winner|home": "win"},
                        evidence_id="recovery-lifecycle-evidence",
                        evidence_sha256="e" * 64,
                        available_at="2026-09-19T21:19:30+00:00",
                    ),
                )

            def reconcile_after_settlement(self, **_kwargs):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-recovery-lifecycle",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
                settlement_learning_handoff=Handoff(),
            )
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "prepared settlement recovery conflicts with durable lifecycle",
                ):
                    coordinator._recovered_settlement_resolutions(as_of=clock())
            finally:
                store.close()

    def test_prepared_recovery_rechecks_sealed_collector_source_identity(self) -> None:
        class Handoff:
            def prepared_settlement_resolutions(self, *, paper_book_path):
                del paper_book_path
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-recovery-source-drift",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
                settlement_learning_handoff=Handoff(),
            )
            try:
                coordinator.collector.__dict__["_source_id"] = "provider-b"
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "collector source identity changed",
                ):
                    coordinator._recovered_settlement_resolutions(as_of=clock())
            finally:
                store.close()

    def test_recovered_settlement_requires_exact_tuple_contract(self) -> None:
        class Handoff:
            def prepared_settlement_resolutions(self, *, paper_book_path):
                return []

            def reconcile_after_settlement(self, **_kwargs):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-recovery-shape",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                settlement_learning_handoff=Handoff(),
            )
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "prepared settlement recovery must return a tuple",
                ):
                    coordinator._recovered_settlement_resolutions(as_of=clock())
            finally:
                store.close()

    def test_direct_settle_rejects_conflicting_duplicate_evidence_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            first = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:duplicate-id",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="duplicate-evidence",
                evidence_sha256="1" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            second = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:duplicate-id",
                quote_outcomes={"event-1|winner|home": "loss"},
                evidence_id="duplicate-evidence",
                evidence_sha256="1" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "evidence id has multiple authorities",
                ):
                    coordinator._settle(
                        resolutions=(first, second),
                        settled_at=clock(),
                    )
                self.assertFalse((root / "paper_book.json").exists())
            finally:
                store.close()

    def test_direct_settle_rejects_split_event_reference_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            first = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:shared",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="evidence:first",
                evidence_sha256="2" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            second = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:shared",
                quote_outcomes={"event-1|winner|away": "loss"},
                evidence_id="evidence:second",
                evidence_sha256="3" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "event/reference has multiple evidence authorities",
                ):
                    coordinator._settle(
                        resolutions=(first, second),
                        settled_at=clock(),
                    )
                self.assertFalse((root / "paper_book.json").exists())
            finally:
                store.close()

    def test_direct_settle_rejects_future_resolution_before_book_access(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            future = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:future",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="future-evidence",
                evidence_sha256="4" * 64,
                available_at="2026-09-19T21:21:00+00:00",
            )
            coordinator._load_book = lambda: self.fail("shadowed loader was called")
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "failed canonical validation",
                ):
                    coordinator._settle(
                        resolutions=(future,),
                        settled_at=clock(),
                    )
                self.assertFalse((root / "paper_book.json").exists())
            finally:
                store.close()

    def test_direct_settle_rejects_resolution_subclass_authority(self) -> None:
        class DerivedSettlementResolution(SettlementResolution):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            derived = DerivedSettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:derived",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="derived-evidence",
                evidence_sha256="5" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "non-canonical resolution evidence",
                ):
                    coordinator._settle(
                        resolutions=(derived,),
                        settled_at=clock(),
                    )
            finally:
                store.close()

    def test_direct_settle_validation_roots_ignore_module_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            future = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:future-rebind",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="future-rebind-evidence",
                evidence_sha256="6" * 64,
                available_at="2026-09-19T21:21:00+00:00",
            )
            try:
                with patch.object(
                    continuous_session_module,
                    "SettlementResolution",
                    object,
                ), patch.object(
                    continuous_session_module,
                    "_instant",
                    lambda *_args, **_kwargs: None,
                ):
                    with self.assertRaisesRegex(
                        ContinuousSessionError,
                        "failed canonical validation",
                    ):
                        coordinator._settle(
                            resolutions=(future,),
                            settled_at=clock(),
                        )
            finally:
                store.close()

    def test_direct_settle_ignores_shadowed_book_loader(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:loader-shadow",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            forged = PaperBook("100")
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            ticket = forged.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="result:loader-shadow",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="loader-shadow-evidence",
                evidence_sha256="7" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                coordinator.tick()
                coordinator._load_book = lambda: forged
                settled, evidence_ids = coordinator._settle(
                    resolutions=(resolution,),
                    settled_at=clock(),
                )
                self.assertEqual(settled, ())
                self.assertEqual(evidence_ids, ("loader-shadow-evidence",))
                self.assertEqual(forged.balance, Decimal("90"))
                self.assertEqual(ticket.status.value, "open")
                self.assertFalse((root / "paper_book.json").exists())
            finally:
                store.close()

    def test_direct_settle_rejects_lifecycle_lookup_retargeting_before_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:lifecycle-get-shadow",
            )
            coordinator, store, lifecycle, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
            )
            try:
                coordinator.tick()
                leg = TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                )
                book = PaperBook("100")
                ticket = book.open_ticket(
                    (leg,),
                    Decimal("10"),
                    placed_at="2026-09-19T21:19:30+00:00",
                    provider_source_ids=("provider-a",),
                )
                book.save(root / "paper_book.json")
                resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:lifecycle-get-shadow",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="lifecycle-get-shadow",
                    evidence_sha256="d" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
                lifecycle.get = lambda _identity: None
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "lifecycle lookup dispatch changed",
                ):
                    coordinator._settle(
                        resolutions=(resolution,),
                        settled_at=clock(),
                    )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "open",
                )
            finally:
                store.close()

    def test_direct_settle_rejects_resolution_ref_outside_durable_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:canonical-ref",
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
            )
            try:
                coordinator.tick()
                leg = TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                )
                book = PaperBook("100")
                ticket = book.open_ticket(
                    (leg,),
                    Decimal("10"),
                    placed_at="2026-09-19T21:19:30+00:00",
                    provider_source_ids=("provider-a",),
                )
                book.save(root / "paper_book.json")
                resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:forged-ref",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="forged-ref-evidence",
                    evidence_sha256="e" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "reference differs from durable lifecycle",
                ):
                    coordinator._settle(
                        resolutions=(resolution,),
                        settled_at=clock(),
                    )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_direct_settle_rejects_noncompleted_lifecycle_before_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.PRE_MATCH,
                settlement_ref=None,
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
            )
            try:
                coordinator.tick()
                leg = TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                )
                book = PaperBook("100")
                ticket = book.open_ticket(
                    (leg,),
                    Decimal("10"),
                    placed_at="2026-09-19T21:19:30+00:00",
                    provider_source_ids=("provider-a",),
                )
                book.save(root / "paper_book.json")
                resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:premature",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="premature-evidence",
                    evidence_sha256="f" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "event is not durably completed",
                ):
                    coordinator._settle(
                        resolutions=(resolution,),
                        settled_at=clock(),
                    )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_direct_settle_rejects_nested_collector_source_drift_before_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:collector-source-pnl",
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
            )
            try:
                coordinator.tick()
                leg = TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                )
                book = PaperBook("100")
                ticket = book.open_ticket(
                    (leg,),
                    Decimal("10"),
                    placed_at="2026-09-19T21:19:30+00:00",
                    provider_source_ids=("provider-a",),
                )
                book.save(root / "paper_book.json")
                coordinator.collector._source_id = "provider-b"
                resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:collector-source-pnl",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="collector-source-pnl",
                    evidence_sha256="2" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "collector source identity changed",
                ):
                    coordinator._settle(
                        resolutions=(resolution,),
                        settled_at=clock(),
                    )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_direct_settle_rejects_cutoff_before_lifecycle_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:direct-discovery-cutoff",
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(event,),
                    )
                ),
                clock,
            )
            try:
                coordinator.tick()
                leg = TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                )
                book = PaperBook("100")
                ticket = book.open_ticket(
                    (leg,),
                    Decimal("10"),
                    placed_at="2026-09-19T21:19:00+00:00",
                    provider_source_ids=("provider-a",),
                )
                book.save(root / "paper_book.json")
                resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:direct-discovery-cutoff",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="direct-discovery-cutoff",
                    evidence_sha256="4" * 64,
                    available_at="2026-09-19T21:19:00+00:00",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "discovered after the economic cutoff",
                ):
                    coordinator._settle(
                        resolutions=(resolution,),
                        settled_at="2026-09-19T21:19:30+00:00",
                    )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_direct_settle_keeps_canonical_provider_scoped_path_working(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:direct-valid",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                coordinator.tick()
                leg = TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                )
                book = PaperBook("100")
                ticket = book.open_ticket(
                    (leg,),
                    Decimal("10"),
                    placed_at="2026-09-19T21:19:30+00:00",
                    provider_source_ids=("provider-a",),
                )
                book.save(root / "paper_book.json")
                resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:direct-valid",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="direct-valid-evidence",
                    evidence_sha256="8" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
                settled, evidence_ids = coordinator._settle(
                    resolutions=(resolution,),
                    settled_at=clock(),
                )
                self.assertEqual(settled, (ticket.ticket_id,))
                self.assertEqual(evidence_ids, ("direct-valid-evidence",))
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("110"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "won",
                )
            finally:
                store.close()

    def test_direct_settle_uses_captured_durable_book_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="result:save-rebind",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                coordinator.tick()
                leg = TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                )
                book = PaperBook("100")
                ticket = book.open_ticket(
                    (leg,),
                    Decimal("10"),
                    placed_at="2026-09-19T21:19:30+00:00",
                    provider_source_ids=("provider-a",),
                )
                book.save(root / "paper_book.json")
                resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="result:save-rebind",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="save-rebind-evidence",
                    evidence_sha256="a" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
                with patch.object(
                    PaperBook,
                    "save",
                    lambda *_args, **_kwargs: None,
                ):
                    settled, evidence_ids = coordinator._settle(
                        resolutions=(resolution,),
                        settled_at=clock(),
                    )
                self.assertEqual(settled, (ticket.ticket_id,))
                self.assertEqual(evidence_ids, ("save-rebind-evidence",))
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("110"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "won",
                )
            finally:
                store.close()

    def test_settlement_validation_injection_argument_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                with self.assertRaisesRegex(
                    TypeError,
                    "settlement evidence validator is internal product authority",
                ):
                    coordinator._settle(
                        resolutions=(),
                        settled_at=clock(),
                        _settlement_resolution_validate=lambda *_args, **_kwargs: None,
                    )
            finally:
                store.close()

    def test_unscoped_legacy_ticket_cannot_consume_provider_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:unscoped",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:unscoped",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="unscoped-provider-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, ())
                self.assertEqual(
                    result.settlement_evidence_ids,
                    ("unscoped-provider-outcome",),
                )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_multi_provider_ticket_without_leg_provenance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:ambiguous-provider",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a", "provider-b"),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:ambiguous-provider",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="ambiguous-provider-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, ())
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_sportless_legacy_leg_cannot_authorize_pnl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:sportless",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:sportless",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="sportless-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, ())
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_full_event_identity_alias_cannot_replace_native_leg_event_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:identity-alias",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            self.assertNotEqual(event.identity, event.event_id)
            leg = TicketLeg(
                event_id=event.identity,
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:identity-alias",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="identity-alias-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, ())
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_settlement_outcome_cannot_cross_explicit_sport_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:sport-fence",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="football",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:sport-fence",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="wrong-sport-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, ())
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
            finally:
                store.close()

    def test_explicit_matching_provider_provenance_allows_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:matching-provider",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:matching-provider",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="matching-provider-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = coordinator.tick()
                self.assertEqual(result.settled_ticket_ids, (ticket.ticket_id,))
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("110"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "won")
            finally:
                store.close()

    def test_settled_bound_ticket_reaches_agent_loop_once_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:learning-1",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-learning-1",
                    position=1,
                    events=(event,),
                )
            )

            goal = EconomicGoalContract(
                goal_id="paper-learning-goal",
                revision=1,
                bankroll_id="paper-bankroll",
                currency="USD",
            )
            risk = PaperRiskPolicy(economic_goal=goal)
            book = PaperBook("100")
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:10+00:00",
                provider_source_ids=("provider-a",),
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
            )
            book.save(root / "paper_book.json")

            ledger = JsonlDecisionLedger(root / "decisions.jsonl")
            decision_action = "OPEN_PAPER_VALUE_TICKET"

            identity = EnvironmentIdentity(
                source_id="paper-learning-source",
                config_id="paper-learning-config",
                data_id="paper-learning-data",
                protocol_id="paper-learning-protocol",
                cutoff_ts="2026-09-19T21:20:00+00:00",
                seed=17,
            )
            environment = CausalLearningEnvironment(
                identity,
                episode_key="paper-learning-episode",
                policy_id="paper-learning-policy",
                admissible_actions=frozenset({"PAPER_PROPOSAL"}),
            )
            baseline = environment.checkpoint()
            runtime = AgentLoopRuntime.initialize_pristine(
                root / "agent-loop.json",
                loop_id="paper-learning-loop",
                environment_checkpoint=baseline,
                policy_id=environment.episode.policy_id,
                economic_goal_fingerprint=provenance_for(goal).contract_sha256,
                risk_fingerprint=risk.provenance_sha256,
                source_sha256="a" * 64,
                config_sha256="b" * 64,
                at="2026-09-19T21:18:59+00:00",
            )
            observation = Observation(
                environment_id=environment.environment_id,
                observed_at="2026-09-19T21:19:00+00:00",
                available_at="2026-09-19T21:19:01+00:00",
                evidence=(("market_state", "paper-learning-snapshot"),),
            )
            decision = DecisionRecord(
                replay_run_id="paper-learning-run",
                agent="paper-learning-fixture",
                observed_ts=observation.observed_at,
                action=decision_action,
                payload={
                    "ticket_id": ticket.ticket_id,
                    "quote_key": leg.quote_key,
                    "stake": str(ticket.stake),
                },
                context_hash=observation.observation_id,
                decision_id="paper-learning-decision-1",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(
                decision,
                EconomicDecisionAuthority(goal, risk),
            )
            runtime.begin_observation(
                observation,
                environment_identity=environment.identity,
                at="2026-09-19T21:19:01+00:00",
            )
            for phase in (
                AgentLoopPhase.OBSERVE,
                AgentLoopPhase.ASSESS,
                AgentLoopPhase.PLAN,
                AgentLoopPhase.DECIDE,
            ):
                runtime.advance(expected=phase, at="2026-09-19T21:19:02+00:00")
            action = environment.act(
                observation,
                action_type="PAPER_PROPOSAL",
                decision_at="2026-09-19T21:19:05+00:00",
                parameters=(
                    ("economic_decision_id", decision.decision_id),
                    ("paper_ticket_id", ticket.ticket_id),
                ),
            )
            runtime.commit_action(
                action,
                episode=environment.episode,
                observation=observation,
                effect_state=ExternalEffectState.PAPER_ONLY,
                at="2026-09-19T21:19:05+00:00",
            )

            bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=ledger,
                agent_loop=runtime,
                economic_goal=goal,
                risk_policy=risk,
            )
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )

            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:learning-1",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="paper-learning-outcome-1",
                evidence_sha256="c" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
                settlement_learning_handoff=bridge,
            )
            try:
                first = coordinator.tick()
                self.assertEqual(first.settled_ticket_ids, (ticket.ticket_id,))
                self.assertEqual(PaperBook.load(root / "paper_book.json").balance, Decimal("110"))
                first_snapshot = runtime.snapshot()
                self.assertIs(first_snapshot.phase, AgentLoopPhase.EVALUATE)
                self.assertIsNotNone(first_snapshot.transition_id)
                self.assertIsNotNone(first_snapshot.reward_id)
                bridge_state = json.loads(
                    (root / "paper_learning_bridge.json").read_text(encoding="utf-8")
                )
                self.assertIsNotNone(
                    bridge_state["bindings"][ticket.ticket_id]["settlement_intent"]
                )

                raw_loop = json.loads((root / "agent-loop.json").read_text(encoding="utf-8"))
                self.assertEqual(len(raw_loop["resolutions"]), 1)
                self.assertEqual(raw_loop["resolutions"][0]["reward_value"], "10.00")
                state_before_restart = first_snapshot.state_sha256

                restarted_runtime = AgentLoopRuntime(root / "agent-loop.json")
                restarted_bridge = PaperSettlementLearningBridge(
                    root / "paper_learning_bridge.json",
                    paper_book_path=root / "paper_book.json",
                    decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                    agent_loop=restarted_runtime,
                    economic_goal=goal,
                    risk_policy=risk,
                )
                restarted, restarted_store, *_ = _build_coordinator(
                    root,
                    source,
                    clock,
                    outcome_authority=authority,
                    settlement_learning_handoff=restarted_bridge,
                )
                try:
                    second = restarted.tick()
                    self.assertEqual(second.settled_ticket_ids, ())
                    self.assertEqual(
                        restarted_runtime.snapshot().state_sha256,
                        state_before_restart,
                    )
                    raw_after = json.loads(
                        (root / "agent-loop.json").read_text(encoding="utf-8")
                    )
                    self.assertEqual(len(raw_after["resolutions"]), 1)
                    self.assertEqual(
                        restarted_bridge.next_checkpoint(ticket.ticket_id).last_transition_id,
                        first_snapshot.transition_id,
                    )
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_same_tick_conflicting_settlement_evidence_fails_before_book_save(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            first_event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:1",
                event_id="event-1",
            )
            second_event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:2",
                event_id="event-2",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(first_event, second_event),
                )
            )

            book = PaperBook("100")
            first_leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            second_leg = TicketLeg(
                event_id="event-2",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book.open_ticket(
                (first_leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.open_ticket(
                (second_leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            before = PaperBook.load(root / "paper_book.json")
            before_statuses = {
                ticket_id: ticket.status.value
                for ticket_id, ticket in before.tickets.items()
            }

            reused_evidence_id = "outcome-conflict-same-tick"
            resolutions = {
                first_event.identity: SettlementResolution(
                    event_identity=first_event.identity,
                    settlement_ref="provider-result:1",
                    quote_outcomes={first_leg.quote_key: "win"},
                    evidence_id=reused_evidence_id,
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                ),
                second_event.identity: SettlementResolution(
                    event_identity=second_event.identity,
                    settlement_ref="provider-result:2",
                    quote_outcomes={second_leg.quote_key: "loss"},
                    evidence_id=reused_evidence_id,
                    evidence_sha256="1" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                ),
            }
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=_MappedOutcomeAuthority(resolutions),
            )
            try:
                with self.assertRaises(ContinuousSessionError):
                    coordinator.tick()

                after = PaperBook.load(root / "paper_book.json")
                self.assertEqual(after.balance, before.balance)
                self.assertEqual(
                    {
                        ticket_id: ticket.status.value
                        for ticket_id, ticket in after.tickets.items()
                    },
                    before_statuses,
                )
                self.assertEqual(coordinator.status().cycles_completed, 0)
                self.assertEqual(coordinator.status().settlement_evidence, ())
            finally:
                store.close()

    def test_conflicting_settlement_evidence_id_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:1",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:1",
                quote_outcomes={"provider-a:event-1:winner:home": "win"},
                evidence_id="outcome-conflict",
                evidence_sha256="0" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)
            coordinator, store, _lifecycle, _mirror, _invalidations, _dependencies = _build_coordinator(
                root, source, clock, outcome_authority=authority
            )
            try:
                coordinator.tick()
                authority.resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:1",
                    quote_outcomes=dict(resolution.quote_outcomes),
                    evidence_id="outcome-conflict",
                    evidence_sha256="1" * 64,
                    available_at=resolution.available_at,
                )
                with self.assertRaises(ContinuousSessionError):
                    coordinator.tick()
            finally:
                store.close()

    def test_durable_settlement_outcome_digest_ignores_module_retargeting(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-1",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:digest-root",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="digest-root-evidence",
                evidence_sha256="6" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                with patch.object(
                    continuous_session_module,
                    "_settlement_outcomes_sha256",
                    lambda _evidence: "f" * 64,
                ):
                    coordinator._state.record_settlement_evidence(
                        settlement_evidence=(resolution,)
                    )
                coordinator._state.validate_settlement_evidence(
                    settlement_evidence=(resolution,)
                )
                raw = json.loads(
                    (root / "continuous_session.json").read_text(encoding="utf-8")
                )
                self.assertNotEqual(
                    raw["settlement_outcome_digests"]["digest-root-evidence"],
                    "f" * 64,
                )
            finally:
                store.close()

    def test_same_settlement_evidence_cannot_change_quote_outcome_after_pnl_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:stable",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:stable",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="stable-outcome",
                evidence_sha256="0" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                first = coordinator.tick()
                self.assertEqual(len(first.settled_ticket_ids), 1)
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )

                authority.resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:stable",
                    quote_outcomes={leg.quote_key: "loss"},
                    evidence_id="stable-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "settlement outcome interpretation conflicts with durable evidence",
                ):
                    coordinator.tick()

                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )
                self.assertEqual(coordinator.status().cycles_completed, 1)
            finally:
                store.close()

    def test_future_ticket_cannot_settle_before_placement_and_retries_causally(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:future-ticket",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:21:00+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:future-ticket",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="future-ticket-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                with patch.object(
                    coordinator._state,
                    "record_failure",
                    side_effect=RuntimeError("checkpoint-failed"),
                ):
                    with self.assertRaisesRegex(
                        ValueError,
                        "settled_at must not precede placed_at",
                    ):
                        coordinator.tick()

                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(durable.tickets[ticket.ticket_id].status.value, "open")
                self.assertIsNone(durable.tickets[ticket.ticket_id].settled_at)
                self.assertEqual(coordinator.status().cycles_completed, 0)
                self.assertEqual(
                    tuple(
                        item["evidence_id"]
                        for item in coordinator.status().settlement_evidence
                    ),
                    ("future-ticket-outcome",),
                )

                clock.value = "2026-09-19T21:22:00+00:00"
                recovered = coordinator.tick()
                self.assertEqual(recovered.settled_ticket_ids, (ticket.ticket_id,))
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("110"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].settled_at,
                    "2026-09-19T21:22:00+00:00",
                )
                self.assertEqual(coordinator.status().cycles_completed, 1)
            finally:
                store.close()

    def test_pre_pnl_crash_replays_same_bound_evidence_exactly_once(
        self,
    ) -> None:
        class _FailBeforeSettlement:
            def prepare_settlement(self, **_kwargs):
                raise RuntimeError("pre-settlement crash")

            def reconcile_after_settlement(self, **_kwargs):
                return ()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:pre-crash",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:pre-crash",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="pre-crash-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
                settlement_learning_handoff=_FailBeforeSettlement(),
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "pre-settlement crash"):
                    coordinator.tick()
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("90"),
                )
                self.assertEqual(coordinator.status().cycles_completed, 0)
                self.assertEqual(
                    tuple(
                        item["evidence_id"]
                        for item in coordinator.status().settlement_evidence
                    ),
                    ("pre-crash-outcome",),
                )
            finally:
                store.close()

            restarted, restarted_store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                recovered = restarted.tick()
                self.assertEqual(len(recovered.settled_ticket_ids), 1)
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )
                self.assertEqual(restarted.status().cycles_completed, 1)

                replay = restarted.tick()
                self.assertEqual(replay.settled_ticket_ids, ())
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )
                self.assertEqual(restarted.status().cycles_completed, 2)
            finally:
                restarted_store.close()

    def test_pending_retention_clears_settled_ticket_but_keeps_other_open_ticket_truth(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event_one = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:selective-settled",
                event_id="event-1",
            )
            event_two = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:selective-partial",
                event_id="event-2",
            )
            event_three = _event(
                phase=EventPhase.PRE_MATCH,
                event_id="event-3",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-selective-retention",
                    position=1,
                    events=(event_one, event_two, event_three),
                )
            )
            first_leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            second_legs = (
                TicketLeg(
                    event_id="event-2",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                ),
                TicketLeg(
                    event_id="event-3",
                    market_id="winner",
                    selection_id="away",
                    locked_odds=Decimal("1.50"),
                    sport="table_tennis",
                ),
            )
            book = PaperBook("100")
            settled_ticket = book.open_ticket(
                (first_leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:10+00:00",
                provider_source_ids=("provider-a",),
            )
            open_ticket = book.open_ticket(
                second_legs,
                Decimal("10"),
                placed_at="2026-09-19T21:19:11+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            settled_resolution = SettlementResolution(
                event_identity=event_one.identity,
                settlement_ref="provider-result:selective-settled",
                quote_outcomes={first_leg.quote_key: "win"},
                evidence_id="selective-settled-evidence",
                evidence_sha256="3" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            partial_resolution = SettlementResolution(
                event_identity=event_two.identity,
                settlement_ref="provider-result:selective-partial",
                quote_outcomes={second_legs[0].quote_key: "win"},
                evidence_id="selective-partial-evidence",
                evidence_sha256="4" * 64,
                available_at="2026-09-19T21:19:31+00:00",
            )
            authority = _MappedOutcomeAuthority(
                {
                    event_one.identity: settled_resolution,
                    event_two.identity: partial_resolution,
                }
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = coordinator.tick()
                self.assertEqual(
                    result.settled_ticket_ids,
                    (settled_ticket.ticket_id,),
                )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(
                    durable.tickets[settled_ticket.ticket_id].status.value,
                    "won",
                )
                self.assertEqual(
                    durable.tickets[open_ticket.ticket_id].status.value,
                    "open",
                )
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (partial_resolution.evidence_id,),
                )
            finally:
                store.close()

    def test_partial_multileg_settlement_truth_accumulates_across_ticks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event_one = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:partial-one",
                event_id="event-1",
            )
            event_two = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:partial-two",
                event_id="event-2",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-partial-multileg",
                    position=1,
                    events=(event_one, event_two),
                )
            )
            legs = (
                TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                ),
                TicketLeg(
                    event_id="event-2",
                    market_id="winner",
                    selection_id="away",
                    locked_odds=Decimal("1.80"),
                    sport="table_tennis",
                ),
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                legs,
                Decimal("10"),
                placed_at="2026-09-19T21:19:10+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            first_resolution = SettlementResolution(
                event_identity=event_one.identity,
                settlement_ref="provider-result:partial-one",
                quote_outcomes={legs[0].quote_key: "win"},
                evidence_id="partial-one-evidence",
                evidence_sha256="5" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            second_resolution = SettlementResolution(
                event_identity=event_two.identity,
                settlement_ref="provider-result:partial-two",
                quote_outcomes={legs[1].quote_key: "win"},
                evidence_id="partial-two-evidence",
                evidence_sha256="6" * 64,
                available_at="2026-09-19T21:19:40+00:00",
            )
            authority = _MappedOutcomeAuthority(
                {event_one.identity: first_resolution}
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                first = coordinator.tick()
                self.assertEqual(first.settled_ticket_ids, ())
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "open",
                )
                first_state = json.loads(
                    (root / "continuous_session.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    [
                        item["evidence_id"]
                        for item in first_state["pending_settlement_resolutions"]
                    ],
                    [first_resolution.evidence_id],
                )
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (first_resolution.evidence_id,),
                )

                authority.resolutions = {
                    event_two.identity: second_resolution,
                }
                second = coordinator.tick()
                self.assertEqual(second.settled_ticket_ids, (ticket.ticket_id,))
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "won",
                )
                self.assertEqual(durable.balance, Decimal("126"))
                second_state = json.loads(
                    (root / "continuous_session.json").read_text(encoding="utf-8")
                )
                self.assertEqual(
                    second_state["pending_settlement_resolutions"],
                    [],
                )
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (),
                )
                self.assertEqual(second_state["cycles_completed"], 2)
            finally:
                store.close()

    def test_partial_void_then_win_preserves_exact_multileg_payout_across_ticks(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event_one = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:void-first",
                event_id="event-1",
            )
            event_two = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:win-second",
                event_id="event-2",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-void-win",
                    position=1,
                    events=(event_one, event_two),
                )
            )
            legs = (
                TicketLeg(
                    event_id="event-1",
                    market_id="winner",
                    selection_id="home",
                    locked_odds=Decimal("2.00"),
                    sport="table_tennis",
                ),
                TicketLeg(
                    event_id="event-2",
                    market_id="winner",
                    selection_id="away",
                    locked_odds=Decimal("1.80"),
                    sport="table_tennis",
                ),
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                legs,
                Decimal("10"),
                placed_at="2026-09-19T21:19:10+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            void_resolution = SettlementResolution(
                event_identity=event_one.identity,
                settlement_ref="provider-result:void-first",
                quote_outcomes={legs[0].quote_key: "void"},
                evidence_id="void-first-evidence",
                evidence_sha256="a" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            win_resolution = SettlementResolution(
                event_identity=event_two.identity,
                settlement_ref="provider-result:win-second",
                quote_outcomes={legs[1].quote_key: "win"},
                evidence_id="win-second-evidence",
                evidence_sha256="b" * 64,
                available_at="2026-09-19T21:19:40+00:00",
            )
            authority = _MappedOutcomeAuthority(
                {event_one.identity: void_resolution}
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                first = coordinator.tick()
                self.assertEqual(first.settled_ticket_ids, ())
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (void_resolution.evidence_id,),
                )

                authority.resolutions = {
                    event_two.identity: win_resolution,
                }
                second = coordinator.tick()
                self.assertEqual(second.settled_ticket_ids, (ticket.ticket_id,))
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "won",
                )
                self.assertEqual(durable.balance, Decimal("108"))
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (),
                )
            finally:
                store.close()

    def test_pending_settlement_recovery_rejects_clock_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:clock-rollback",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-clock-rollback",
                    position=1,
                    events=(event,),
                )
            )
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:clock-rollback",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="clock-rollback-evidence",
                evidence_sha256="c" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                coordinator.collector.run_cycle()
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                clock.value = "2026-09-19T21:19:20+00:00"
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "pending settlement recovery failed canonical validation",
                ):
                    coordinator._pending_settlement_resolutions(as_of=clock())
            finally:
                store.close()

    def test_staged_pending_settlement_recovers_before_learning_prepare(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:state-pending-crash",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-state-pending-crash",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:10+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:state-pending-crash",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="state-pending-crash-evidence",
                evidence_sha256="9" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OneShotOutcomeAuthority(resolution)
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                coordinator.collector.run_cycle()
                first_seen = coordinator._settlement_resolutions(as_of=clock())
                self.assertEqual(first_seen, (resolution,))
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=first_seen,
                )
                raw = json.loads(
                    (root / "continuous_session.json").read_text(encoding="utf-8")
                )
                self.assertEqual(raw["schema_version"], 4)
                self.assertEqual(
                    raw["pending_settlement_resolutions"][0]["evidence_id"],
                    resolution.evidence_id,
                )
                self.assertEqual(
                    raw["pending_settlement_resolutions"][0]["quote_outcomes"],
                    resolution.quote_outcomes,
                )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("90"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "open",
                )
                self.assertEqual(authority.calls, 1)
            finally:
                store.close()

            restarted, restarted_store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                result = restarted.tick()
                self.assertGreaterEqual(authority.calls, 2)
                self.assertEqual(result.settled_ticket_ids, (ticket.ticket_id,))
                self.assertIn(
                    resolution.evidence_id,
                    result.settlement_evidence_ids,
                )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("110"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "won",
                )
                raw = json.loads(
                    (root / "continuous_session.json").read_text(encoding="utf-8")
                )
                self.assertEqual(raw["pending_settlement_resolutions"], [])
                self.assertEqual(raw["cycles_completed"], 1)
            finally:
                restarted_store.close()

    def test_provider_outage_still_recovers_already_staged_settlement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:outage-recovery",
            )
            page = CatalogPage(
                source_id="provider-a",
                stream_epoch="epoch-1",
                cursor="cursor-outage-recovery",
                position=1,
                events=(event,),
            )
            source = _Source(page)
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:10+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:outage-recovery",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="outage-recovery-evidence",
                evidence_sha256="7" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
            )
            try:
                coordinator.collector.run_cycle()
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("90"),
                )
            finally:
                store.close()

            unavailable = _UnavailableSource(page)
            restarted, restarted_store, *_ = _build_coordinator(
                root,
                unavailable,
                clock,
            )
            try:
                result = restarted.tick()
                self.assertTrue(result.source_provider_unavailable)
                self.assertEqual(
                    result.settled_ticket_ids,
                    (ticket.ticket_id,),
                )
                self.assertIn(
                    resolution.evidence_id,
                    result.settlement_evidence_ids,
                )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("110"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "won",
                )
                raw = json.loads(
                    (root / "continuous_session.json").read_text(encoding="utf-8")
                )
                self.assertEqual(raw["pending_settlement_resolutions"], [])
                self.assertEqual(
                    restarted.status().pending_settlement_evidence_ids,
                    (),
                )
                self.assertEqual(raw["cycles_completed"], 0)
                self.assertEqual(
                    raw["last_error_code"],
                    "ProviderUnavailableError",
                )
            finally:
                restarted_store.close()

    def test_settlement_completion_cannot_drop_unprocessed_pending_truth(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-state-completion",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            first = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:completion-first",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="completion-first-evidence",
                evidence_sha256="e" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            second = SettlementResolution(
                event_identity="provider-a:event-2",
                settlement_ref="result:completion-second",
                quote_outcomes={"event-2|winner|away": "win"},
                evidence_id="completion-second-evidence",
                evidence_sha256="f" * 64,
                available_at="2026-09-19T21:19:31+00:00",
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(first, second),
                )
                coordinator._state.complete_pending_settlement_commit(
                    settlement_evidence=(first,),
                    retain_pending_evidence_ids=(),
                )
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (second.evidence_id,),
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "retained pending settlement identity was not processed",
                ):
                    coordinator._state.complete_pending_settlement_commit(
                        settlement_evidence=(second,),
                        retain_pending_evidence_ids=("foreign-evidence",),
                    )
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (second.evidence_id,),
                )
            finally:
                store.close()

    def test_direct_state_rejects_mutated_settlement_before_durable_publish(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-state-structural-validation",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:state-structural-validation",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="state-structural-validation-evidence",
                evidence_sha256="1" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                state_path = root / "continuous_session.json"
                before = state_path.read_text(encoding="utf-8")
                resolution.quote_outcomes["event-1|winner|home"] = "forged"
                with self.assertRaisesRegex(
                    ValueError,
                    "quote_outcomes contains unsupported outcome",
                ):
                    coordinator._state.record_settlement_evidence(
                        settlement_evidence=(resolution,),
                    )
                self.assertEqual(state_path.read_text(encoding="utf-8"), before)
                self.assertEqual(coordinator.status().settlement_evidence, ())
                self.assertEqual(
                    coordinator.status().pending_settlement_evidence_ids,
                    (),
                )
            finally:
                store.close()

    def test_direct_state_requires_exact_tuple_settlement_collection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-state-tuple-validation",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:state-tuple-validation",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="state-tuple-validation-evidence",
                evidence_sha256="2" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                state_path = root / "continuous_session.json"
                before = state_path.read_text(encoding="utf-8")
                with self.assertRaisesRegex(
                    TypeError,
                    "settlement_evidence must be a tuple",
                ):
                    coordinator._state.record_settlement_evidence(
                        settlement_evidence=[resolution],
                    )
                self.assertEqual(state_path.read_text(encoding="utf-8"), before)
            finally:
                store.close()

    def test_direct_state_rejects_resolution_subclass_after_global_rebind(self) -> None:
        class _ForgedResolution(SettlementResolution):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-state-type-validation",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            forged = _ForgedResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:state-type-validation",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="state-type-validation-evidence",
                evidence_sha256="3" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            original = continuous_session_module.SettlementResolution
            try:
                state_path = root / "continuous_session.json"
                before = state_path.read_text(encoding="utf-8")
                continuous_session_module.SettlementResolution = _ForgedResolution
                with self.assertRaisesRegex(
                    TypeError,
                    "settlement evidence must be canonical",
                ):
                    coordinator._state.record_settlement_evidence(
                        settlement_evidence=(forged,),
                    )
                self.assertEqual(state_path.read_text(encoding="utf-8"), before)
            finally:
                continuous_session_module.SettlementResolution = original
                store.close()

    def test_forged_completion_cannot_clear_valid_pending_settlement(self) -> None:
        class _ForgedResolution(SettlementResolution):
            pass

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-forged-completion",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:forged-completion",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="forged-completion-evidence",
                evidence_sha256="a" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            forged = _ForgedResolution(
                event_identity=resolution.event_identity,
                settlement_ref=resolution.settlement_ref,
                quote_outcomes=dict(resolution.quote_outcomes),
                evidence_id=resolution.evidence_id,
                evidence_sha256=resolution.evidence_sha256,
                available_at=resolution.available_at,
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                before = coordinator.status()
                with self.assertRaisesRegex(
                    TypeError,
                    "settlement evidence must be canonical",
                ):
                    coordinator._state.complete_pending_settlement_commit(
                        settlement_evidence=(forged,),
                        retain_pending_evidence_ids=(),
                    )
                after = coordinator.status()
                self.assertEqual(
                    after.pending_settlement_evidence_ids,
                    before.pending_settlement_evidence_ids,
                )
                self.assertEqual(after.settlement_evidence, before.settlement_evidence)
            finally:
                store.close()

    def test_invalid_success_settlement_collection_cannot_advance_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-invalid-success-collection",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:invalid-success-collection",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="invalid-success-collection-evidence",
                evidence_sha256="b" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                before = coordinator.status()
                with self.assertRaisesRegex(
                    TypeError,
                    "settlement_evidence must be a tuple",
                ):
                    coordinator._state.record_success(
                        at=clock(),
                        full_refresh=False,
                        settlement_evidence=[resolution],
                        retain_pending_evidence_ids=(),
                    )
                after = coordinator.status()
                self.assertEqual(after.cycles_completed, before.cycles_completed)
                self.assertEqual(after.last_success_at, before.last_success_at)
                self.assertEqual(after.settlement_evidence, ())
            finally:
                store.close()

    def test_duplicate_durable_settlement_evidence_fails_closed_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-duplicate-durable-settlement",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:duplicate-durable-settlement",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="duplicate-durable-settlement-evidence",
                evidence_sha256="4" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                state_path = root / "continuous_session.json"
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                raw["settlement_evidence"].append(
                    dict(raw["settlement_evidence"][0])
                )
                state_path.write_text(
                    json.dumps(raw, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "durable settlement evidence entry is duplicated",
                ):
                    coordinator.status()
            finally:
                store.close()

    def test_duplicate_pending_settlement_resolution_fails_closed_on_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-duplicate-pending-settlement",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:duplicate-pending-settlement",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="duplicate-pending-settlement-evidence",
                evidence_sha256="5" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                state_path = root / "continuous_session.json"
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                raw["pending_settlement_resolutions"].append(
                    {
                        **raw["pending_settlement_resolutions"][0],
                        "quote_outcomes": dict(
                            raw["pending_settlement_resolutions"][0]["quote_outcomes"]
                        ),
                    }
                )
                state_path.write_text(
                    json.dumps(raw, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "pending settlement resolution entry is duplicated",
                ):
                    coordinator.status()
            finally:
                store.close()

    def test_pending_settlement_digest_index_tamper_fails_on_status_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-pending-digest-index-tamper",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:pending-digest-index-tamper",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="pending-digest-index-tamper-evidence",
                evidence_sha256="6" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                state_path = root / "continuous_session.json"
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                raw["settlement_outcome_digests"][resolution.evidence_id] = "7" * 64
                state_path.write_text(
                    json.dumps(raw, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "pending settlement outcome digest conflicts with durable evidence",
                ):
                    coordinator.status()
            finally:
                store.close()

    def test_pending_settlement_read_integrity_ignores_module_rebinding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            coordinator, store, *_ = _build_coordinator(
                root,
                _Source(
                    CatalogPage(
                        source_id="provider-a",
                        stream_epoch="epoch-1",
                        cursor="cursor-pending-read-integrity",
                        position=1,
                        events=(_event(phase=EventPhase.PRE_MATCH),),
                    )
                ),
                clock,
            )
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:pending-read-integrity",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="pending-read-integrity-evidence",
                evidence_sha256="9" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            original_type = continuous_session_module.SettlementResolution
            original_hashlib = continuous_session_module.hashlib
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                state_path = root / "continuous_session.json"
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                raw["pending_settlement_resolutions"][0]["quote_outcomes"][
                    "event-1|winner|home"
                ] = "loss"
                state_path.write_text(
                    json.dumps(raw, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )

                class _ForgedResolution(SettlementResolution):
                    pass

                class _ForgedHashlib:
                    @staticmethod
                    def sha256(_payload):
                        class _ForgedDigest:
                            @staticmethod
                            def hexdigest():
                                return raw["settlement_outcome_digests"][
                                    resolution.evidence_id
                                ]

                        return _ForgedDigest()

                continuous_session_module.SettlementResolution = _ForgedResolution
                continuous_session_module.hashlib = _ForgedHashlib
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "pending settlement outcome digest conflicts with durable evidence",
                ):
                    coordinator.status()
            finally:
                continuous_session_module.SettlementResolution = original_type
                continuous_session_module.hashlib = original_hashlib
                store.close()

    def test_pending_settlement_outcomes_tamper_fails_digest_reproof(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-pending-tamper",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            resolution = SettlementResolution(
                event_identity="provider-a:event-1",
                settlement_ref="result:pending-tamper",
                quote_outcomes={"event-1|winner|home": "win"},
                evidence_id="pending-tamper-evidence",
                evidence_sha256="8" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            try:
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=(resolution,),
                )
                state_path = root / "continuous_session.json"
                raw = json.loads(state_path.read_text(encoding="utf-8"))
                raw["pending_settlement_resolutions"][0]["quote_outcomes"][
                    "event-1|winner|home"
                ] = "loss"
                state_path.write_text(
                    json.dumps(raw, sort_keys=True, separators=(",", ":")),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "pending settlement outcome digest conflicts with durable evidence",
                ):
                    coordinator.status()
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "pending settlement outcome digest conflicts with durable evidence",
                ):
                    coordinator._pending_settlement_resolutions(as_of=clock())
            finally:
                store.close()

    def test_learning_settlement_recovers_before_bridge_prepare_during_source_outage(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:prepared-crash",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-prepared-crash",
                    position=1,
                    events=(event,),
                )
            )

            goal = EconomicGoalContract(
                goal_id="prepared-crash-goal",
                revision=1,
                bankroll_id="prepared-crash-bankroll",
                currency="USD",
            )
            risk = PaperRiskPolicy(economic_goal=goal)
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            ticket = book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:10+00:00",
                provider_source_ids=("provider-a",),
                bankroll_id=goal.bankroll_id,
                currency=goal.currency,
            )
            book.save(root / "paper_book.json")

            identity = EnvironmentIdentity(
                source_id="prepared-crash-source",
                config_id="prepared-crash-config",
                data_id="prepared-crash-data",
                protocol_id="prepared-crash-protocol",
                cutoff_ts="2026-09-19T21:20:00+00:00",
                seed=23,
            )
            environment = CausalLearningEnvironment(
                identity,
                episode_key="prepared-crash-episode",
                policy_id="prepared-crash-policy",
                admissible_actions=frozenset({"PAPER_PROPOSAL"}),
            )
            baseline = environment.checkpoint()
            runtime = AgentLoopRuntime.initialize_pristine(
                root / "agent-loop.json",
                loop_id="prepared-crash-loop",
                environment_checkpoint=baseline,
                policy_id=environment.episode.policy_id,
                economic_goal_fingerprint=provenance_for(goal).contract_sha256,
                risk_fingerprint=risk.provenance_sha256,
                source_sha256="a" * 64,
                config_sha256="b" * 64,
                at="2026-09-19T21:18:59+00:00",
            )
            observation = Observation(
                environment_id=environment.environment_id,
                observed_at="2026-09-19T21:19:00+00:00",
                available_at="2026-09-19T21:19:01+00:00",
                evidence=(("market_state", "prepared-crash-snapshot"),),
            )
            ledger = JsonlDecisionLedger(root / "decisions.jsonl")
            decision = DecisionRecord(
                replay_run_id="prepared-crash-run",
                agent="prepared-crash-fixture",
                observed_ts=observation.observed_at,
                action="OPEN_PAPER_VALUE_TICKET",
                payload={
                    "ticket_id": ticket.ticket_id,
                    "quote_key": leg.quote_key,
                    "stake": str(ticket.stake),
                },
                context_hash=observation.observation_id,
                decision_id="prepared-crash-decision",
                decision_kind=ECONOMIC_DECISION_KIND,
            )
            ledger.append_economic(
                decision,
                EconomicDecisionAuthority(goal, risk),
            )
            runtime.begin_observation(
                observation,
                environment_identity=environment.identity,
                at="2026-09-19T21:19:01+00:00",
            )
            for phase in (
                AgentLoopPhase.OBSERVE,
                AgentLoopPhase.ASSESS,
                AgentLoopPhase.PLAN,
                AgentLoopPhase.DECIDE,
            ):
                runtime.advance(expected=phase, at="2026-09-19T21:19:02+00:00")
            action = environment.act(
                observation,
                action_type="PAPER_PROPOSAL",
                decision_at="2026-09-19T21:19:05+00:00",
                parameters=(
                    ("economic_decision_id", decision.decision_id),
                    ("paper_ticket_id", ticket.ticket_id),
                ),
            )
            runtime.commit_action(
                action,
                episode=environment.episode,
                observation=observation,
                effect_state=ExternalEffectState.PAPER_ONLY,
                at="2026-09-19T21:19:05+00:00",
            )
            bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=ledger,
                agent_loop=runtime,
                economic_goal=goal,
                risk_policy=risk,
            )
            bridge.bind_ticket(
                ticket_id=ticket.ticket_id,
                decision_id=decision.decision_id,
                environment=environment,
                observation=observation,
                action=action,
                baseline_checkpoint=baseline,
            )

            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:prepared-crash",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="prepared-crash-evidence",
                evidence_sha256="f" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OneShotOutcomeAuthority(resolution)
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
                settlement_learning_handoff=bridge,
            )
            try:
                coordinator.collector.run_cycle()
                first_seen = coordinator._settlement_resolutions(as_of=clock())
                self.assertEqual(first_seen, (resolution,))
                coordinator._state.record_settlement_evidence(
                    settlement_evidence=first_seen,
                )
                bridge_state = json.loads(
                    (root / "paper_learning_bridge.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertIsNone(
                    bridge_state["bindings"][ticket.ticket_id]["settlement_intent"]
                )
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").tickets[
                        ticket.ticket_id
                    ].status.value,
                    "open",
                )
                self.assertEqual(authority.calls, 1)
            finally:
                store.close()

            restarted_runtime = AgentLoopRuntime(root / "agent-loop.json")
            restarted_bridge = PaperSettlementLearningBridge(
                root / "paper_learning_bridge.json",
                paper_book_path=root / "paper_book.json",
                decision_ledger=JsonlDecisionLedger(root / "decisions.jsonl"),
                agent_loop=restarted_runtime,
                economic_goal=goal,
                risk_policy=risk,
            )
            restarted, restarted_store, *_ = _build_coordinator(
                root,
                _UnavailableSource(source.page),
                clock,
                outcome_authority=authority,
                settlement_learning_handoff=restarted_bridge,
            )
            try:
                result = restarted.tick()
                self.assertTrue(result.source_provider_unavailable)
                self.assertEqual(authority.calls, 1)
                self.assertEqual(result.settled_ticket_ids, (ticket.ticket_id,))
                self.assertIn(
                    resolution.evidence_id,
                    result.settlement_evidence_ids,
                )
                durable = PaperBook.load(root / "paper_book.json")
                self.assertEqual(durable.balance, Decimal("110"))
                self.assertEqual(
                    durable.tickets[ticket.ticket_id].status.value,
                    "won",
                )
                self.assertIs(
                    restarted_runtime.snapshot().phase,
                    AgentLoopPhase.EVALUATE,
                )
                bridge_state = json.loads(
                    (root / "paper_learning_bridge.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    bridge_state["bindings"][ticket.ticket_id]["status"],
                    "ACKED",
                )
                self.assertIsNotNone(
                    bridge_state["bindings"][ticket.ticket_id]["settlement_intent"]
                )
                session_state = json.loads(
                    (root / "continuous_session.json").read_text(
                        encoding="utf-8"
                    )
                )
                self.assertEqual(
                    session_state["pending_settlement_resolutions"],
                    [],
                )
                self.assertEqual(session_state["cycles_completed"], 0)
                self.assertEqual(
                    session_state["last_error_code"],
                    "ProviderUnavailableError",
                )
            finally:
                restarted_store.close()

    def test_settlement_evidence_is_durable_before_post_pnl_crash_and_blocks_reinterpretation(
        self,
    ) -> None:
        class _FailAfterSettlement:
            def prepare_settlement(self, **_kwargs):
                return ()

            def reconcile_after_settlement(self, **_kwargs):
                raise RuntimeError("post-settlement crash")

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:crash-bound",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:crash-bound",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="crash-bound-outcome",
                evidence_sha256="0" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
                settlement_learning_handoff=_FailAfterSettlement(),
            )
            try:
                with self.assertRaisesRegex(RuntimeError, "post-settlement crash"):
                    coordinator.tick()

                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )
                status = coordinator.status()
                self.assertEqual(status.cycles_completed, 0)
                self.assertEqual(
                    tuple(item["evidence_id"] for item in status.settlement_evidence),
                    ("crash-bound-outcome",),
                )
            finally:
                store.close()

            authority.resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:crash-bound",
                quote_outcomes={leg.quote_key: "loss"},
                evidence_id="crash-bound-outcome",
                evidence_sha256="0" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            restarted, restarted_store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "settlement outcome interpretation conflicts with durable evidence",
                ):
                    restarted.tick()
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )
                self.assertEqual(restarted.status().cycles_completed, 0)
            finally:
                restarted_store.close()

    def test_settlement_event_reference_cannot_change_evidence_identity_after_pnl_commit(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:stable",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            authority = _OutcomeAuthority(
                SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:stable",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="stable-outcome",
                    evidence_sha256="0" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
            )
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                coordinator.tick()
                authority.resolution = SettlementResolution(
                    event_identity=event.identity,
                    settlement_ref="provider-result:stable",
                    quote_outcomes={leg.quote_key: "win"},
                    evidence_id="replacement-outcome",
                    evidence_sha256="1" * 64,
                    available_at="2026-09-19T21:19:30+00:00",
                )
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "settlement event/reference conflicts with durable evidence",
                ):
                    coordinator.tick()
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )
                self.assertEqual(coordinator.status().cycles_completed, 1)
            finally:
                store.close()

    def test_legacy_v2_settlement_evidence_is_readable_but_not_reinterpretable(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            event = _event(
                phase=EventPhase.COMPLETED,
                settlement_ref="provider-result:legacy",
            )
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(event,),
                )
            )
            leg = TicketLeg(
                event_id="event-1",
                market_id="winner",
                selection_id="home",
                locked_odds=Decimal("2.00"),
                sport="table_tennis",
            )
            book = PaperBook("100")
            book.open_ticket(
                (leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
                provider_source_ids=("provider-a",),
            )
            book.save(root / "paper_book.json")
            resolution = SettlementResolution(
                event_identity=event.identity,
                settlement_ref="provider-result:legacy",
                quote_outcomes={leg.quote_key: "win"},
                evidence_id="legacy-outcome",
                evidence_sha256="0" * 64,
                available_at="2026-09-19T21:19:30+00:00",
            )
            authority = _OutcomeAuthority(resolution)
            coordinator, store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                coordinator.tick()
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )
            finally:
                store.close()

            state_path = root / "continuous_session.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 2
            state.pop("settlement_outcome_digests")
            state.pop("pending_settlement_resolutions")
            state_path.write_text(
                json.dumps(state, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            restarted, restarted_store, *_ = _build_coordinator(
                root,
                source,
                clock,
                outcome_authority=authority,
            )
            try:
                self.assertEqual(restarted.status().cycles_completed, 1)
                with self.assertRaisesRegex(
                    ContinuousSessionError,
                    "legacy settlement evidence lacks durable outcome interpretation",
                ):
                    restarted.tick()
                self.assertEqual(
                    PaperBook.load(root / "paper_book.json").balance,
                    Decimal("110"),
                )
                self.assertEqual(restarted.status().cycles_completed, 1)
            finally:
                restarted_store.close()

    def test_legacy_v3_session_state_upgrades_with_empty_pending_settlement_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-v3-upgrade",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            store.close()

            state_path = root / "continuous_session.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["schema_version"] = 3
            state.pop("pending_settlement_resolutions")
            state_path.write_text(
                json.dumps(state, sort_keys=True, separators=(",", ":")),
                encoding="utf-8",
            )

            restarted, restarted_store, *_ = _build_coordinator(
                root,
                source,
                clock,
            )
            try:
                self.assertEqual(restarted.status().cycles_completed, 0)
                restarted._state.record_failure(code="legacy-v3-upgrade-probe")
                upgraded = json.loads(
                    state_path.read_text(encoding="utf-8")
                )
                self.assertEqual(upgraded["schema_version"], 4)
                self.assertEqual(upgraded["pending_settlement_resolutions"], [])
                self.assertEqual(
                    upgraded["last_error_code"],
                    "legacy-v3-upgrade-probe",
                )
            finally:
                restarted_store.close()

    def test_unresolved_gap_is_durable_in_status_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            detected = _collector_delta(
                delta_id="gap-detected",
                gap_state=GapState.DETECTED,
                sync_state=SyncState.GAP_DETECTED,
            )
            source = _DeltaSource(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.LIVE),),
                ),
                [(detected,)],
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                with self.assertRaises(GapStateError):
                    coordinator.tick()

                status = coordinator.status()
                self.assertEqual(status.source_gap_state, GapState.DETECTED.value)
                self.assertEqual(status.source_sync_state, SyncState.GAP_DETECTED.value)
                self.assertEqual(status.source_state_delta_id, "gap-detected")
                self.assertEqual(
                    status.source_unresolved_gap_delta_ids,
                    ("gap-detected",),
                )

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock
                )
                try:
                    restarted_status = restarted.status()
                    self.assertEqual(
                        restarted_status.source_gap_state,
                        GapState.DETECTED.value,
                    )
                    self.assertEqual(
                        restarted_status.source_sync_state,
                        SyncState.GAP_DETECTED.value,
                    )
                    self.assertEqual(
                        restarted_status.source_unresolved_gap_delta_ids,
                        ("gap-detected",),
                    )
                finally:
                    restarted_store.close()
            finally:
                store.close()

    def test_gap_recovery_and_no_new_delta_keep_truthful_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            detected = _collector_delta(
                delta_id="gap-detected",
                gap_state=GapState.DETECTED,
                sync_state=SyncState.GAP_DETECTED,
            )
            recovered = _collector_delta(
                delta_id="gap-recovered",
                gap_state=GapState.RECOVERED,
                sync_state=SyncState.RECOVERED,
                revision_of="gap-detected",
                revision_number=1,
            )
            source = _DeltaSource(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.LIVE),),
                ),
                [(detected,), (recovered,), ()],
            )
            coordinator, store, *_ = _build_coordinator(root, source, clock)
            try:
                with self.assertRaises(GapStateError):
                    coordinator.tick()

                recovered_result = coordinator.tick()
                self.assertEqual(
                    recovered_result.source_gap_states,
                    (GapState.RECOVERED.value,),
                )
                self.assertEqual(
                    recovered_result.source_sync_states,
                    (SyncState.RECOVERED.value,),
                )
                recovered_status = coordinator.status()
                self.assertEqual(
                    recovered_status.source_gap_state,
                    GapState.RECOVERED.value,
                )
                self.assertEqual(
                    recovered_status.source_sync_state,
                    SyncState.RECOVERED.value,
                )
                self.assertEqual(
                    recovered_status.source_state_delta_id,
                    "gap-recovered",
                )
                self.assertEqual(
                    recovered_status.source_unresolved_gap_delta_ids,
                    (),
                )

                no_new_delta = coordinator.tick()
                self.assertEqual(
                    no_new_delta.source_gap_states,
                    (GapState.RECOVERED.value,),
                )
                self.assertEqual(
                    no_new_delta.source_sync_states,
                    (SyncState.RECOVERED.value,),
                )
                self.assertFalse(
                    coordinator.status().source_state_projection_backlog
                )
            finally:
                store.close()

    def test_routing_failure_retains_dirty_invalidation_for_retry(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, _lifecycle, _mirror, invalidations, dependencies = (
                _build_coordinator(root, source, clock)
            )
            try:
                dependencies.register(
                    "decision:event-1",
                    source_ids="provider-a",
                    sports="table_tennis",
                    event_ids="event-1",
                )
                invalidations.accept_persisted(_market_event())
                self.assertEqual(invalidations.pending_count, 1)

                with patch.object(
                    dependencies,
                    "affected_inputs",
                    side_effect=RuntimeError("route-failed"),
                ):
                    with self.assertRaisesRegex(RuntimeError, "route-failed"):
                        coordinator._drain_invalidations()

                self.assertEqual(invalidations.pending_count, 1)
                affected, full_refresh, backlog = coordinator._drain_invalidations()
                self.assertEqual(affected, ("decision:event-1",))
                self.assertFalse(full_refresh)
                self.assertFalse(backlog)
                self.assertEqual(invalidations.pending_count, 0)
            finally:
                store.close()

    def test_invalidation_overflow_becomes_full_refresh_fence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            clock = _Clock()
            source = _Source(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(_event(phase=EventPhase.PRE_MATCH),),
                )
            )
            coordinator, store, _lifecycle, mirror, invalidations, dependencies = _build_coordinator(
                root, source, clock
            )
            try:
                dependencies.register(
                    "catalog:provider-a:event-1",
                    source_ids="provider-a",
                    sports="table_tennis",
                    event_ids="event-1",
                )
                event = _market_event()
                invalidations.accept_persisted(event)
                second = MarketEvent(
                    event_id="event-2",
                    market_id="winner",
                    selection_id="home",
                    decimal_odds=Decimal("1.80"),
                    observed_ts="2026-09-19T21:19:01+00:00",
                    source_id="provider-a",
                    sequence=2,
                    market_type=MarketType.WINNER,
                    status="open",
                    source_ts="2026-09-19T21:19:01+00:00",
                    ingest_ts="2026-09-19T21:19:01+00:00",
                    sport="table_tennis",
                )
                invalidations.accept_persisted(second)
                status = coordinator.status()
                self.assertTrue(status.invalidation_full_refresh_required)
                self.assertEqual(status.invalidation_pending_count, 0)
                result = coordinator._drain_invalidations()
                self.assertEqual(result[1], True)
                self.assertFalse(result[2])
                self.assertEqual(result[0], ("catalog:provider-a:event-1",))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
