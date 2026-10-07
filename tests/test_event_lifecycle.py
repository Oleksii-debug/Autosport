from __future__ import annotations

import json
import tempfile
import unittest
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import pytest

import autosport.event_lifecycle as event_lifecycle_module
from autosport.domain import MarketEvent
from autosport.event_lifecycle import (
    CatalogConflictError,
    CatalogLifecycleError,
    CatalogCursorError,
    CatalogEvent,
    CatalogPage,
    ContinuousEventLifecycle,
    EvidenceEligibility,
    EventPhase,
    canonical_event_identity,
    canonical_event_identity_aliases,
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

    def test_lifecycle_read_modify_write_uses_existing_durable_path_lock(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            ContinuousEventLifecycle(path)

            depth = 0
            real_read = ContinuousEventLifecycle._read
            real_migrate = ContinuousEventLifecycle._migrate_legacy_state
            real_atomic_write = event_lifecycle_module.atomic_write_json

            @contextmanager
            def observed_lock(candidate: str | Path):
                nonlocal depth
                self.assertEqual(Path(candidate), path)
                depth += 1
                try:
                    yield
                finally:
                    depth -= 1

            def guarded_read(instance: ContinuousEventLifecycle):
                self.assertGreater(depth, 0)
                return real_read(instance)

            def guarded_migrate(instance: ContinuousEventLifecycle):
                self.assertGreater(depth, 0)
                return real_migrate(instance)

            def guarded_atomic_write(candidate: str | Path, payload: dict):
                self.assertGreater(depth, 0)
                return real_atomic_write(candidate, payload)

            with (
                patch.object(event_lifecycle_module, "durable_path_lock", observed_lock),
                patch.object(ContinuousEventLifecycle, "_read", guarded_read),
                patch.object(
                    ContinuousEventLifecycle,
                    "_migrate_legacy_state",
                    guarded_migrate,
                ),
                patch.object(
                    event_lifecycle_module,
                    "atomic_write_json",
                    guarded_atomic_write,
                ),
            ):
                lifecycle = ContinuousEventLifecycle(path)
                event = self._catalog_event(available_offset=1)
                changed = lifecycle.apply_page(
                    self._page(1, event),
                    discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
                )
                self.assertEqual(changed, (event.identity,))

            reopened = ContinuousEventLifecycle(path)
            self.assertIsNotNone(reopened.get(event.identity))

    def test_same_provider_event_id_across_sports_remains_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ContinuousEventLifecycle(Path(directory) / "catalog.json")
            table_tennis = self._catalog_event(
                sport="table_tennis",
                available_offset=1,
            )
            soccer = self._catalog_event(
                sport="soccer",
                available_offset=2,
            )
            lifecycle.apply_page(
                self._page(1, table_tennis),
                discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
            )
            lifecycle.apply_page(
                self._page(2, soccer),
                discovered_at=(self.START + timedelta(seconds=2)).isoformat(),
            )

            records = {record.sport: record for record in lifecycle.records()}
            self.assertEqual(set(records), {"table_tennis", "soccer"})
            self.assertEqual(records["table_tennis"].event_id, "event-1")
            self.assertEqual(records["soccer"].event_id, "event-1")
            self.assertNotEqual(
                records["table_tennis"].identity,
                records["soccer"].identity,
            )

    def test_catalog_page_rejects_catalog_event_subclass_before_dispatch(self) -> None:
        class HostileCatalogEvent(CatalogEvent):
            def validate(self) -> None:
                raise AssertionError("CatalogEvent subclass dispatch must not execute")

        canonical = self._catalog_event()
        hostile = HostileCatalogEvent(
            source_id=canonical.source_id,
            sport=canonical.sport,
            event_id=canonical.event_id,
            phase=canonical.phase,
            available_at=canonical.available_at,
            scheduled_start_at=canonical.scheduled_start_at,
            completion_ref=canonical.completion_ref,
            settlement_ref=canonical.settlement_ref,
        )
        page = CatalogPage(
            source_id="provider-a",
            stream_epoch="epoch-1",
            cursor="cursor-1",
            position=1,
            events=(hostile,),
        )

        with self.assertRaisesRegex(TypeError, "exact CatalogEvent"):
            page.validate()

    def test_apply_page_rejects_catalog_page_subclass_before_dispatch(self) -> None:
        class HostileCatalogPage(CatalogPage):
            def validate(self) -> None:
                raise AssertionError("CatalogPage subclass dispatch must not execute")

        canonical = self._page(1, self._catalog_event())
        hostile = HostileCatalogPage(
            source_id=canonical.source_id,
            stream_epoch=canonical.stream_epoch,
            cursor=canonical.cursor,
            position=canonical.position,
            events=canonical.events,
            epoch_changed=canonical.epoch_changed,
        )

        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ContinuousEventLifecycle(Path(directory) / "catalog.json")
            with self.assertRaisesRegex(TypeError, "exact CatalogPage"):
                lifecycle.apply_page(
                    hostile,
                    discovered_at=self.START.isoformat(),
                )

    def test_lifecycle_identity_is_sport_scoped_and_deterministic(self) -> None:
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
        self.assertTrue(table_tennis.startswith("sport-v2-"))
        self.assertTrue(soccer.startswith("sport-v2-"))
        self.assertNotEqual(table_tennis, soccer)
        self.assertEqual(
            table_tennis,
            canonical_event_identity(
                source_id="provider-a",
                sport="table_tennis",
                event_id="event-1",
            ),
        )

    def test_schema_v1_migrates_only_from_its_exact_durable_sport(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            lifecycle = ContinuousEventLifecycle(path)
            event = self._catalog_event(
                sport="table_tennis",
                available_offset=1,
            )
            lifecycle.apply_page(
                self._page(1, event),
                discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = raw["events"].pop(event.identity)
            legacy_identity = "provider-a:event-1"
            record["identity"] = legacy_identity
            raw["events"][legacy_identity] = record
            raw["schema_version"] = 1
            path.write_text(
                json.dumps(raw, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

            restarted = ContinuousEventLifecycle(path)
            migrated = restarted.get(event.identity)
            self.assertIsNotNone(migrated)
            assert migrated is not None
            self.assertEqual(migrated.sport, "table_tennis")
            self.assertIsNone(restarted.get(legacy_identity))
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 2)
            self.assertEqual(set(persisted["events"]), {event.identity})

    def test_schema_v1_without_durable_sport_fails_closed_without_rewrite(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            lifecycle = ContinuousEventLifecycle(path)
            event = self._catalog_event(
                sport="table_tennis",
                available_offset=1,
            )
            lifecycle.apply_page(
                self._page(1, event),
                discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
            )
            raw = json.loads(path.read_text(encoding="utf-8"))
            record = raw["events"].pop(event.identity)
            legacy_identity = "provider-a:event-1"
            record["identity"] = legacy_identity
            del record["sport"]
            raw["events"][legacy_identity] = record
            raw["schema_version"] = 1
            path.write_text(
                json.dumps(raw, ensure_ascii=False, sort_keys=True),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(
                CatalogLifecycleError,
                "legacy migration contains invalid evidence",
            ):
                ContinuousEventLifecycle(path)
            persisted = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["schema_version"], 1)
            self.assertNotIn("sport", persisted["events"][legacy_identity])

    def test_event_id_with_source_scope_delimiter_fails_before_persistence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ContinuousEventLifecycle(Path(directory) / "catalog.json")
            ambiguous = self._catalog_event(event_id="scope:event")
            with self.assertRaisesRegex(ValueError, "source-scope delimiter"):
                lifecycle.apply_page(
                    self._page(1, ambiguous),
                    discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
                )
            self.assertEqual(lifecycle.records(), ())

    def test_pipe_delimiters_are_rejected_from_selector_components(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved identity delimiter"):
            canonical_event_identity(
                source_id="provider|a",
                sport="table_tennis",
                event_id="event-1",
            )
        with self.assertRaisesRegex(ValueError, "reserved identity delimiter"):
            canonical_event_identity(
                source_id="provider-a",
                sport="table_tennis",
                event_id="event|1",
            )

    def test_colon_bearing_source_id_remains_valid_with_provider_event_id(self) -> None:
        value = canonical_event_identity(
            source_id="parlayapi:table_tennis",
            sport="table_tennis",
            event_id="event-1",
        )
        self.assertTrue(value.startswith("sport-v2-"))

    def test_completed_state_is_hidden_before_local_discovery_time(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            lifecycle = ContinuousEventLifecycle(Path(directory) / "catalog.json")
            pre = self._event()
            identity = canonical_event_identity(
                source_id=pre.source_id,
                sport=pre.sport,
                event_id=pre.event_id,
            )
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

    def test_register_eligible_retires_schema_v1_dependency_while_v2_waits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = ContinuousEventLifecycle(root / "catalog.json")
            event = self._catalog_event(
                sport="table_tennis",
                event_id="event-1",
                available_offset=1,
            )
            lifecycle.apply_page(
                self._page(1, event),
                discovered_at=(self.START + timedelta(seconds=1)).isoformat(),
            )
            store = SQLiteMarketStore(root / "market.db")
            try:
                registered: list[str] = []
                retired: list[str] = []
                result = lifecycle.register_eligible(
                    store,
                    as_of=(self.START + timedelta(seconds=5)).isoformat(),
                    required_history=timedelta(0),
                    register_input=lambda input_id, **_: registered.append(input_id),
                    retire_input=retired.append,
                )
                self.assertEqual(result, ())
                self.assertEqual(registered, [])
                self.assertEqual(retired, ["catalog:provider-a:event-1"])
                self.assertNotEqual(
                    retired[0],
                    f"catalog:{event.identity}",
                )
            finally:
                store.close()

    def test_register_eligible_uses_existing_sport_aware_live_dependency_seam(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            lifecycle = ContinuousEventLifecycle(root / "catalog.json")
            table_tennis = self._catalog_event(
                sport="table_tennis",
                event_id="shared-event",
            )
            soccer = self._catalog_event(
                sport="soccer",
                event_id="shared-event",
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
                        event_id=self._stored_event_id("shared-event"),
                        observed_offset=0,
                        ingest_offset=0,
                    )
                )
                store.append(
                    self._event(
                        sport="soccer",
                        event_id=self._stored_event_id("shared-event"),
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
                        "soccer": "provider-a:shared-event",
                        "table_tennis": "provider-a:shared-event",
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
                self.assertEqual(registered, (f"catalog:{event.identity}",))
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


class CanonicalEventIdentityAliasTests(unittest.TestCase):
    def test_schema_v2_aliases_round_trip_exact_sport_source_and_event(self) -> None:
        identity = canonical_event_identity(
            source_id="provider:region",
            sport="table_tennis",
            event_id="event-1",
        )

        self.assertEqual(
            canonical_event_identity_aliases(identity),
            (identity, "provider:region:event-1", "event-1"),
        )

    def test_legacy_alias_uses_rightmost_source_scope_separator(self) -> None:
        self.assertEqual(
            canonical_event_identity_aliases("provider:region:event-1"),
            ("provider:region:event-1", "event-1"),
        )

    def test_provider_local_legacy_alias_revalidates_provider_event_identity(self) -> None:
        self.assertEqual(
            canonical_event_identity_aliases("event-1"),
            ("event-1",),
        )
        with self.assertRaisesRegex(ValueError, "reserved identity delimiter"):
            canonical_event_identity_aliases("event|forged")

    def test_scoped_legacy_alias_revalidates_source_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "reserved identity delimiter"):
            canonical_event_identity_aliases("provider|forged:event-1")

    def test_noncanonical_schema_v2_encoding_fails_closed(self) -> None:
        identity = canonical_event_identity(
            source_id="provider-a",
            sport="table_tennis",
            event_id="event-1",
        )

        with self.assertRaisesRegex(ValueError, "canonical"):
            canonical_event_identity_aliases(identity + "A")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("source_id", "provider-a\nforged"),
        ("source_id", "provider-a\tforged"),
        ("event_id", "event-1\rforged"),
        ("event_id", "event-1\x7fforged"),
    ),
)
def test_canonical_event_identity_rejects_control_aliases(field: str, value: str) -> None:
    kwargs = {"source_id": "provider-a", "sport": "table_tennis", "event_id": "event-1"}
    kwargs[field] = value
    with pytest.raises(ValueError, match="canonical"):
        canonical_event_identity(**kwargs)


def test_canonical_event_identity_rejects_non_utf8_identity_text() -> None:
    with pytest.raises(ValueError, match="UTF-8"):
        canonical_event_identity(source_id="provider-a", sport="table_tennis", event_id="event-\ud800")
