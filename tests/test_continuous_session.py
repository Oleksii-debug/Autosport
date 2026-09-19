from __future__ import annotations

import tempfile
import unittest
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from autosport.causal_collector import (
    CollectorDeltaStore,
    DesktopDeltaCheckpointStore,
    DesktopDeltaConsumer,
)
from autosport.collector_service import HeadlessCollectorService
from autosport.continuous_session import (
    ContinuousSessionCoordinator,
    ContinuousSessionError,
    SessionPausedError,
    SettlementResolution,
    SessionState,
)
from autosport.domain import MarketEvent, MarketType, TicketLeg
from autosport.event_lifecycle import CatalogEvent, CatalogPage, ContinuousEventLifecycle, EventPhase
from autosport.market_bus import MarketEventBus
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
    MarketMirror,
)
from autosport.paper import PaperBook
from autosport.providers import ProviderUnavailableError
from autosport.storage import SQLiteMarketStore


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


def _build_coordinator(root: Path, source: _Source, clock: _Clock, *, outcome_authority=None):
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
        resolve_event=lambda delta: _market_event(),
        apply_event=lambda delta, event: None,
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
            book.open_ticket((leg,), Decimal("10"), placed_at="2026-09-19T21:19:30+00:00")
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

                restarted, restarted_store, *_ = _build_coordinator(
                    root, source, clock, outcome_authority=authority
                )
                try:
                    second = restarted.tick()
                    self.assertEqual(second.settled_ticket_ids, ())
                    settled_again = PaperBook.load(root / "paper_book.json")
                    self.assertEqual(settled_again.balance, Decimal("110"))
                    self.assertEqual(authority.calls, 2)
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
            )
            book.open_ticket(
                (second_leg,),
                Decimal("10"),
                placed_at="2026-09-19T21:19:30+00:00",
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
                self.assertGreater(status.invalidation_pending_count, 0)
                result = coordinator._drain_invalidations()
                self.assertEqual(result[1], True)
                self.assertFalse(result[2])
                self.assertEqual(result[0], ("catalog:provider-a:event-1",))
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
