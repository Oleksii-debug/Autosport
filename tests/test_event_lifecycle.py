from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from autosport.domain import MarketEvent
from autosport.event_lifecycle import (
    CatalogConflictError,
    CatalogCursorError,
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EvidenceEligibility,
    EventPhase,
    canonical_event_identity,
)
from autosport.ingestion import IngestionEngine
from autosport.market_bus import MarketEventBus
from autosport.market_mirror import MarketMirror
from autosport.market_mirror_runtime import (
    BoundedMirrorInvalidationBuffer,
    FocusedMirrorDependencyIndex,
)
from autosport.providers import InMemoryProvider, ProviderQuote
from autosport.storage import SQLiteMarketStore


class ContinuousEventLifecycleTests(unittest.TestCase):
    START = datetime(2026, 9, 19, 7, 0, tzinfo=timezone.utc)

    @classmethod
    def _event(
        cls,
        *,
        sport: str = "table_tennis",
        event_id: str = "event-1",
        sequence: int = 1,
        observed_offset: int = 0,
        ingest_offset: int | None = None,
        status: str = "open",
    ) -> MarketEvent:
        ingest_offset = observed_offset if ingest_offset is None else ingest_offset
        return MarketEvent(
            event_id=event_id,
            market_id="winner",
            selection_id="home",
            decimal_odds=Decimal("2.00"),
            observed_ts=(cls.START + timedelta(seconds=observed_offset)).isoformat(),
            source_id="provider-a",
            sequence=sequence,
            status=status,
            ingest_ts=(cls.START + timedelta(seconds=ingest_offset)).isoformat(),
            sport=sport,
        )

    @staticmethod
    def _stored_event_id(event_id: str) -> str:
        return f"provider-a:{event_id}"

    @classmethod
    def _catalog_event(
        cls,
        *,
        phase: EventPhase = EventPhase.PRE_MATCH,
        sport: str = "table_tennis",
        event_id: str = "event-1",
        available_offset: int = 0,
        completion_ref: str | None = None,
        settlement_ref: str | None = None,
    ) -> CatalogEvent:
        return CatalogEvent(
            source_id="provider-a",
            sport=sport,
            event_id=event_id,
            phase=phase,
            available_at=(cls.START + timedelta(seconds=available_offset)).isoformat(),
            scheduled_start_at=(cls.START + timedelta(minutes=10)).isoformat(),
            completion_ref=completion_ref,
            settlement_ref=settlement_ref,
        )

    @classmethod
    def _page(
        cls,
        position: int,
        event: CatalogEvent,
        *,
        epoch: str = "epoch-1",
        epoch_changed: bool = False,
    ) -> CatalogPage:
        return CatalogPage(
            source_id="provider-a",
            stream_epoch=epoch,
            cursor=f"cursor-{position}",
            position=position,
            events=(event,),
            epoch_changed=epoch_changed,
        )

    def test_same_provider_event_id_across_sports_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ContinuousEventLifecycle(Path(directory) / "catalog.json")
            lifecycle.apply_page(
                self._page(1, self._event(sport="table_tennis")),
                discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
            )
            with self.assertRaises(CatalogConflictError):
                lifecycle.apply_page(
                    self._page(2, self._event(sport="soccer", observed_offset=1)),
                    discovered_at=(self.START + timedelta(seconds=2)).isoformat(),
                )

    def test_lifecycle_identity_reuses_provider_scoped_market_event_identity(self) -> None:
        table_tennis = canonical_event_identity(
            source_id="provider-a",
            sport="table_tennis",
            event_id="event-1",
        )
        soccer = canonical_event_identity(
            source_id="provider-a",
            sport="soccer",
            event_id="event-1",
        )
        self.assertEqual(table_tennis, "provider-a:event-1")
        self.assertEqual(soccer, table_tennis)

    def test_completed_state_is_hidden_before_local_discovery_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ContinuousEventLifecycle(Path(directory) / "catalog.json")
            pre = self._event()
            identity = pre.source_id + ":" + pre.event_id
            lifecycle.apply_page(
                self._page(1, self._catalog_event()),
                discovered_at=(self.START + timedelta(seconds=2)).isoformat(),
            )
            lifecycle.apply_page(
                self._page(
                    2,
                    CatalogEvent(
                        source_id="provider-a",
                        sport="table_tennis",
                        event_id="event-1",
                        phase=EventPhase.COMPLETED,
                        available_at=(self.START + timedelta(seconds=4)).isoformat(),
                        completion_ref="provider-result:rev-1",
                    ),
                ),
                discovered_at=(self.START + timedelta(seconds=6)).isoformat(),
            )
            store = SQLiteMarketStore(Path(directory) / "market.db")
            try:
                before = lifecycle.assess_evidence(
                    identity,
                    store,
                    as_of=(self.START + timedelta(seconds=5)).isoformat(),
                    required_history=timedelta(0),
                )
                after = lifecycle.assess_evidence(
                    identity,
                    store,
                    as_of=(self.START + timedelta(seconds=6)).isoformat(),
                    required_history=timedelta(0),
                )
            finally:
                store.close()
            self.assertEqual(before.status, EvidenceEligibility.WAIT_EVIDENCE)
            self.assertEqual(after.status, EvidenceEligibility.COMPLETED)

            retired: list[str] = []
            lifecycle.register_eligible(
                store,
                as_of=(self.START + timedelta(seconds=5)).isoformat(),
                required_history=timedelta(0),
                register_input=lambda input_id, **_: None,
                retire_input=lambda input_id: retired.append(input_id),
            )
            self.assertEqual(retired, [])

            with self.assertRaisesRegex(
                CatalogConflictError,
                "discovery cutoff cannot move backwards",
            ):
                lifecycle.apply_page(
                    self._page(
                        3,
                        CatalogEvent(
                            source_id="provider-a",
                            sport="table_tennis",
                            event_id="event-1",
                            phase=EventPhase.COMPLETED,
                            available_at=(
                                self.START + timedelta(seconds=5)
                            ).isoformat(),
                            completion_ref="provider-result:rev-1",
                        ),
                    ),
                    discovered_at=(
                        self.START + timedelta(seconds=5)
                    ).isoformat(),
                )

    def test_post_start_discovery_progresses_same_identity_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog_lifecycle.json"
            lifecycle = ContinuousEventLifecycle(path)
            pre = self._catalog_event(available_offset=1)
            identity = pre.identity

            self.assertEqual(
                lifecycle.apply_page(
                    self._page(1, pre),
                    discovered_at=(self.START + timedelta(seconds=2)).isoformat(),
                ),
                (identity,),
            )
            self.assertEqual(
                lifecycle.get(identity).first_discovered_at,
                (self.START + timedelta(seconds=2)).isoformat(),
            )

            restarted = ContinuousEventLifecycle(path)
            live = self._catalog_event(
                phase=EventPhase.LIVE,
                available_offset=3,
            )
            self.assertEqual(
                restarted.apply_page(
                    self._page(2, live),
                    discovered_at=(self.START + timedelta(seconds=4)).isoformat(),
                ),
                (identity,),
            )
            record = restarted.get(identity)
            self.assertEqual(record.identity, identity)
            self.assertEqual(record.phase, EventPhase.LIVE)
            self.assertEqual(record.first_discovered_at, (self.START + timedelta(seconds=2)).isoformat())
            self.assertEqual(restarted.checkpoint("provider-a").position, 2)

    def test_catalog_retry_is_idempotent_and_cursor_gap_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ContinuousEventLifecycle(Path(directory) / "catalog.json")
            page = self._page(4, self._catalog_event(available_offset=1))
            discovered_at = (self.START + timedelta(seconds=2)).isoformat()
            lifecycle.apply_page(page, discovered_at=discovered_at)
            self.assertEqual(lifecycle.apply_page(page, discovered_at=discovered_at), ())

            with self.assertRaisesRegex(CatalogCursorError, "gap or regression"):
                lifecycle.apply_page(
                    self._page(6, self._catalog_event(available_offset=3)),
                    discovered_at=(self.START + timedelta(seconds=4)).isoformat(),
                )

    def test_stream_epoch_change_requires_explicit_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ContinuousEventLifecycle(Path(directory) / "catalog.json")
            lifecycle.apply_page(
                self._page(1, self._catalog_event()),
                discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
            )
            with self.assertRaisesRegex(CatalogCursorError, "epoch changed"):
                lifecycle.apply_page(
                    self._page(
                        0,
                        self._catalog_event(available_offset=2),
                        epoch="epoch-2",
                    ),
                    discovered_at=(self.START + timedelta(seconds=3)).isoformat(),
                )

            changed = lifecycle.apply_page(
                self._page(
                    0,
                    self._catalog_event(available_offset=2),
                    epoch="epoch-2",
                    epoch_changed=True,
                ),
                discovered_at=(self.START + timedelta(seconds=3)).isoformat(),
            )
            self.assertEqual(changed, (self._catalog_event().identity,))
            self.assertEqual(lifecycle.checkpoint("provider-a").stream_epoch, "epoch-2")

    def test_late_discovery_does_not_fabricate_required_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = ContinuousEventLifecycle(root / "catalog.json")
            event = self._catalog_event(
                phase=EventPhase.LIVE,
                available_offset=100,
            )
            lifecycle.apply_page(
                self._page(1, event),
                discovered_at=(self.START + timedelta(seconds=100)).isoformat(),
            )
            store = SQLiteMarketStore(root / "market.db")
            try:
                store.append(
                    self._event(
                        event_id=self._stored_event_id("event-1"),
                        observed_offset=0,
                        ingest_offset=95,
                    )
                )
                assessment = lifecycle.assess_evidence(
                    event.identity,
                    store,
                    as_of=(self.START + timedelta(seconds=100)).isoformat(),
                    required_history=timedelta(seconds=30),
                )
                self.assertEqual(
                    assessment.status,
                    EvidenceEligibility.WAIT_EVIDENCE,
                )
                self.assertIn("cannot backfill", assessment.detail)

                store.append(
                    self._event(
                        event_id=self._stored_event_id("event-1"),
                        sequence=2,
                        observed_offset=101,
                        ingest_offset=101,
                    )
                )
                later = lifecycle.assess_evidence(
                    event.identity,
                    store,
                    as_of=(self.START + timedelta(seconds=130)).isoformat(),
                    required_history=timedelta(seconds=30),
                )
                self.assertEqual(later.status, EvidenceEligibility.ELIGIBLE)
            finally:
                store.close()

    def test_register_eligible_uses_existing_sport_aware_live_dependency_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = ContinuousEventLifecycle(root / "catalog.json")
            table_tennis = self._catalog_event(
                sport="table_tennis",
                event_id="table-tennis-event",
            )
            soccer = self._catalog_event(
                sport="soccer",
                event_id="soccer-event",
            )
            lifecycle.apply_page(
                CatalogPage(
                    source_id="provider-a",
                    stream_epoch="epoch-1",
                    cursor="cursor-1",
                    position=1,
                    events=(table_tennis, soccer),
                ),
                discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
            )
            store = SQLiteMarketStore(root / "market.db")
            try:
                store.append(
                    self._event(
                        sport="table_tennis",
                        event_id=self._stored_event_id("table-tennis-event"),
                        observed_offset=0,
                        ingest_offset=0,
                    )
                )
                store.append(
                    self._event(
                        sport="soccer",
                        event_id=self._stored_event_id("soccer-event"),
                        observed_offset=0,
                        ingest_offset=0,
                    )
                )
                calls: list[tuple[str, dict[str, object]]] = []

                def register(input_id: str, **selectors: object) -> None:
                    calls.append((input_id, selectors))

                registered = lifecycle.register_eligible(
                    store,
                    as_of=(self.START + timedelta(seconds=5)).isoformat(),
                    required_history=timedelta(0),
                    register_input=register,
                )
                self.assertEqual(len(registered), 2)
                selectors = {
                    call[1]["sports"]: call[1]["event_ids"]
                    for call in calls
                }
                self.assertEqual(
                    selectors,
                    {
                        "soccer": "provider-a:soccer-event",
                        "table_tennis": "provider-a:table-tennis-event",
                    },
                )
            finally:
                store.close()

    def test_refresh_and_register_discovers_post_start_without_manual_event_list(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = ContinuousEventLifecycle(root / "catalog.json")
            store = SQLiteMarketStore(root / "market.db")
            try:
                store.append(
                    self._event(
                        event_id=self._stored_event_id("event-1"),
                        observed_offset=0,
                        ingest_offset=0,
                    )
                )
                pages = [
                    self._page(
                        1,
                        self._catalog_event(available_offset=1),
                    )
                ]

                def fetch(checkpoint):
                    self.assertIsNone(checkpoint)
                    return pages[0]

                calls: list[tuple[str, dict[str, object]]] = []
                registered = lifecycle.refresh_and_register(
                    fetch,
                    store,
                    source_id="provider-a",
                    discovered_at=(self.START + timedelta(seconds=2)).isoformat(),
                    required_history=timedelta(0),
                    register_input=lambda input_id, **selectors: calls.append(
                        (input_id, selectors)
                    ),
                )
                self.assertEqual(len(registered), 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0][1]["sports"], "table_tennis")
                self.assertEqual(calls[0][1]["event_ids"], "provider-a:event-1")
            finally:
                store.close()

    def test_completed_event_is_not_registered_and_unresolved_settlement_is_explicit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = ContinuousEventLifecycle(root / "catalog.json")
            completed = self._catalog_event(
                phase=EventPhase.COMPLETED,
                available_offset=5,
                completion_ref="provider-result:rev-7",
            )
            lifecycle.apply_page(
                self._page(1, completed),
                discovered_at=(self.START + timedelta(seconds=6)).isoformat(),
            )
            store = SQLiteMarketStore(root / "market.db")
            try:
                assessment = lifecycle.assess_evidence(
                    completed.identity,
                    store,
                    as_of=(self.START + timedelta(seconds=6)).isoformat(),
                    required_history=timedelta(0),
                )
                self.assertEqual(assessment.status, EvidenceEligibility.COMPLETED)
                self.assertIn("settlement remains unresolved", assessment.detail)

                calls: list[str] = []
                self.assertEqual(
                    lifecycle.register_eligible(
                        store,
                        as_of=(self.START + timedelta(seconds=6)).isoformat(),
                        required_history=timedelta(0),
                        register_input=lambda input_id, **_: calls.append(input_id),
                    ),
                    (),
                )
                self.assertEqual(calls, [])
            finally:
                store.close()

    def test_ingestion_normalizer_identity_matches_lifecycle_selector(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = SQLiteMarketStore(root / "market.db")
            try:
                bus = MarketEventBus(store)
                engine = IngestionEngine(
                    bus,
                    clock=lambda: (
                        self.START + timedelta(seconds=1)
                    ).isoformat(),
                )
                provider = InMemoryProvider(
                    "provider-a",
                    [
                        ProviderQuote(
                            provider_event_id="event-1",
                            provider_market_id="winner",
                            provider_selection_id="home",
                            decimal_odds=Decimal("2.00"),
                            observed_ts=self.START.isoformat(),
                            sequence=1,
                            source_ts=self.START.isoformat(),
                            sport="table_tennis",
                        )
                    ],
                )
                stats = engine.poll_once(provider)
                self.assertEqual(stats.accepted, 1)
                persisted = store.events("provider-a:event-1")
                self.assertEqual(len(persisted), 1)

                lifecycle = ContinuousEventLifecycle(root / "catalog.json")
                event = self._catalog_event(
                    sport="table_tennis",
                    event_id="event-1",
                    available_offset=1,
                )
                lifecycle.apply_page(
                    self._page(1, event),
                    discovered_at=(
                        self.START + timedelta(seconds=2)
                    ).isoformat(),
                )
                assessment = lifecycle.assess_evidence(
                    event.identity,
                    store,
                    as_of=(self.START + timedelta(seconds=2)).isoformat(),
                    required_history=timedelta(0),
                )
                self.assertEqual(assessment.status, EvidenceEligibility.ELIGIBLE)

                calls: list[tuple[str, dict[str, object]]] = []
                registered = lifecycle.register_eligible(
                    store,
                    as_of=(self.START + timedelta(seconds=2)).isoformat(),
                    required_history=timedelta(0),
                    register_input=lambda input_id, **selectors: calls.append(
                        (input_id, selectors)
                    ),
                )
                self.assertEqual(registered, ("catalog:provider-a:event-1",))
                self.assertEqual(calls[0][1]["event_ids"], persisted[0].event_id)
                self.assertEqual(calls[0][1]["sports"], persisted[0].sport)
            finally:
                store.close()

    def test_dependency_index_routes_same_local_event_id_by_sport(self) -> None:
        mirror = MarketMirror()
        table_tennis = self._event(sport="table_tennis")
        soccer = self._event(sport="soccer")
        mirror.apply(table_tennis)
        mirror.apply(soccer)
        index = FocusedMirrorDependencyIndex(mirror)
        index.register(
            "tt",
            source_ids="provider-a",
            sports="table_tennis",
            event_ids="event-1",
        )
        index.register(
            "soccer",
            source_ids="provider-a",
            sports="soccer",
            event_ids="event-1",
        )
        updates = BoundedMirrorInvalidationBuffer(mirror)
        changed = self._event(
            sport="table_tennis",
            sequence=2,
            observed_offset=1,
        )
        updates.accept_persisted(changed)
        affected = index.affected_inputs(updates.drain())
        self.assertEqual(affected, ("tt",))

    def test_mirror_sport_selector_keeps_same_local_ids_separate(self) -> None:
        mirror = MarketMirror()
        table_tennis = self._event(sport="table_tennis")
        soccer = self._event(sport="soccer")
        mirror.apply(table_tennis)
        mirror.apply(soccer)

        view = mirror.view(
            source_ids="provider-a",
            sports="table_tennis",
            event_ids="event-1",
        )
        self.assertEqual(len(view.events), 1)
        self.assertEqual(view.events[0].sport, "table_tennis")


if __name__ == "__main__":
    unittest.main()
