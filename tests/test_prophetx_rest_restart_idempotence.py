from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import hashlib
import json
import unittest

from autosport.ingestion import IngestionEngine
from autosport.ingestion_health import SourceHealthStore
from autosport.market_bus import MarketEventBus
from autosport.market_state_identity import (
    MarketStateIdentityError,
    PROPHETX_REST_MARKET_STATE_CONTRACT,
    semantic_market_state_identity,
)
from autosport.prophetx_marketdata import (
    ProphetXJsonResponse,
    ProphetXRestMarketProvider,
)
from autosport.provider_sequence_authority import SQLiteProviderSequenceAuthority
from autosport.providers import CanonicalNormalizer
from autosport.storage import SQLiteMarketStore


_AUTHORITY_ID = "tests.prophetx-rest-restart-idempotence.v1"


def _payload() -> dict[str, object]:
    return {
        "data": {
            "markets": [
                {
                    "event_id": 1001,
                    "market_id": "market-1",
                    "type": "moneyline",
                    "status": "open",
                    "sub_type": None,
                    "selections": [
                        [
                            {
                                "strike_id": "strike-a",
                                "outcome_id": "outcome-a",
                                "competitor_id": "team-a",
                                "name": "Team A",
                                "price": 150,
                                "quantity": "12.50",
                            },
                            {
                                "strike_id": "strike-a",
                                "outcome_id": "outcome-a",
                                "competitor_id": "team-a",
                                "name": "Team A",
                                "price": 140,
                                "quantity": "5",
                            },
                        ],
                        [
                            {
                                "strike_id": "strike-b",
                                "outcome_id": "outcome-b",
                                "competitor_id": "team-b",
                                "name": "Team B",
                                "price": -200,
                                "quantity": "9",
                            }
                        ],
                    ],
                }
            ]
        }
    }


def _response(payload: object, *, salt: str = "") -> ProphetXJsonResponse:
    encoded = (
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        + salt
    ).encode("utf-8")
    return ProphetXJsonResponse(
        payload=payload,
        status_code=200,
        headers={},
        body_sha256=hashlib.sha256(encoded).hexdigest(),
    )


class _SequenceClock:
    def __init__(self, values: list[str]) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


class ProphetXRestRestartIdempotenceTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.sequence_path = self.root / "provider-sequence.sqlite"
        self.market_path = self.root / "market.sqlite"
        self.health_path = self.root / "source-health.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _authority(self, *, create: bool) -> SQLiteProviderSequenceAuthority:
        return SQLiteProviderSequenceAuthority(
            self.sequence_path,
            authority_id=_AUTHORITY_ID,
            create=create,
        )

    def _provider(
        self,
        authority: SQLiteProviderSequenceAuthority,
        payloads: list[dict[str, object]],
        *,
        response_salts: list[str] | None = None,
        event_ids: tuple[int, ...] = (1001,),
        clocks: list[str] | None = None,
    ) -> ProphetXRestMarketProvider:
        pending_payloads = iter(deepcopy(payloads))
        pending_salts = iter(response_salts or [""] * len(payloads))

        def transport(_url, _headers, _timeout):
            return _response(next(pending_payloads), salt=next(pending_salts))

        return ProphetXRestMarketProvider(
            "secret-test-token",
            event_ids,
            sequence_authority=authority,
            transport=transport,
            clock=_SequenceClock(
                clocks
                or [
                    f"2026-10-04T12:00:{index:02d}+00:00"
                    for index in range(len(payloads))
                ]
            ),
        )

    @staticmethod
    def _normalized(provider: ProphetXRestMarketProvider):
        batch = provider.read_batch()
        normalizer = CanonicalNormalizer()
        return batch, tuple(
            normalizer.normalize(batch.source_id, quote) for quote in batch.quotes
        )

    def test_provider_marks_exact_versioned_semantic_state_contract(self) -> None:
        provider = self._provider(self._authority(create=True), [_payload()])
        batch, events = self._normalized(provider)

        self.assertEqual(len(events), 2)
        for quote in batch.quotes:
            self.assertEqual(
                quote.metadata["semantic_state_contract"],
                PROPHETX_REST_MARKET_STATE_CONTRACT,
            )
        for event in events:
            identity = semantic_market_state_identity(event)
            self.assertIsInstance(identity, str)
            self.assertTrue(
                identity.startswith(PROPHETX_REST_MARKET_STATE_CONTRACT + ":")
            )

    def test_acquisition_sequence_and_receipt_provenance_do_not_change_state_identity(
        self,
    ) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
            response_salts=["raw-a", "raw-b"],
            clocks=[
                "2026-10-04T12:00:00+00:00",
                "2026-10-04T12:00:01+00:00",
            ],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)

        self.assertNotEqual(first[0].sequence, second[0].sequence)
        self.assertNotEqual(
            first[0].metadata["response_sha256"],
            second[0].metadata["response_sha256"],
        )
        self.assertNotEqual(
            first[0].metadata["snapshot_fingerprint_sha256"],
            second[0].metadata["snapshot_fingerprint_sha256"],
        )
        self.assertEqual(
            semantic_market_state_identity(first[0]),
            semantic_market_state_identity(second[0]),
        )

    def test_semantic_depth_change_changes_state_identity_even_when_primary_odds_match(
        self,
    ) -> None:
        changed = _payload()
        changed["data"]["markets"][0]["selections"][0][1]["quantity"] = "6"
        provider = self._provider(
            self._authority(create=True),
            [_payload(), changed],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)

        self.assertEqual(first[0].decimal_odds, second[0].decimal_odds)
        self.assertNotEqual(
            semantic_market_state_identity(first[0]),
            semantic_market_state_identity(second[0]),
        )

    def test_request_scope_change_cannot_cross_semantic_deduplication(self) -> None:
        provider = self._provider(self._authority(create=True), [_payload()])
        _batch, events = self._normalized(provider)
        original = events[0]
        metadata = deepcopy(original.metadata)
        metadata["request_fingerprint_sha256"] = "0" * 64
        metadata["product_acquisition_sequence"] = original.sequence + 1
        changed_scope = replace(
            original,
            sequence=original.sequence + 1,
            observed_ts="2026-10-04T12:00:01+00:00",
            ingest_ts="2026-10-04T12:00:01+00:00",
            metadata=metadata,
        )

        self.assertNotEqual(
            semantic_market_state_identity(original),
            semantic_market_state_identity(changed_scope),
        )

    def test_identical_newer_snapshot_is_not_inserted_as_second_market_transition(
        self,
    ) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
            response_salts=["first", "same-state-different-raw"],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertEqual(store.append_many(first), 2)
            self.assertEqual(store.append_many(second), 0)
            self.assertEqual(len(store.events()), 2)
            current = store.current_by_source()
            self.assertEqual(
                {event.sequence for event in current.values()},
                {first[0].sequence},
            )
        finally:
            store.close()

    def test_equal_sequence_conflict_is_not_hidden_by_semantic_suppression(self) -> None:
        provider = self._provider(self._authority(create=True), [_payload()])
        _batch, events = self._normalized(provider)
        original = events[0]
        metadata = deepcopy(original.metadata)
        metadata["response_sha256"] = "f" * 64
        conflicting_same_acquisition = replace(original, metadata=metadata)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertTrue(store.append(original))
            with self.assertRaisesRegex(
                ValueError,
                "conflicting duplicate market event identity",
            ):
                store.append(conflicting_same_acquisition)
            self.assertEqual(len(store.events()), 1)
        finally:
            store.close()

    def test_legacy_event_without_contract_never_suppresses_first_versioned_event(
        self,
    ) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)
        legacy_metadata = deepcopy(first[0].metadata)
        legacy_metadata.pop("semantic_state_contract")
        legacy = replace(first[0], metadata=legacy_metadata)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertTrue(store.append(legacy))
            self.assertTrue(store.append(second[0]))
            self.assertEqual(len(store.events()), 2)
        finally:
            store.close()

    def test_unknown_future_contract_fails_closed_without_inheriting_watermark(
        self,
    ) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)
        metadata = deepcopy(second[0].metadata)
        metadata["semantic_state_contract"] = (
            "autosport.prophetx-rest-market-state.v2"
        )
        future_contract = replace(second[0], metadata=metadata)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertTrue(store.append(first[0]))
            with self.assertRaisesRegex(
                MarketStateIdentityError,
                "unsupported",
            ):
                store.append(future_contract)
            self.assertEqual(len(store.events()), 1)
        finally:
            store.close()

    def test_tampered_persisted_state_identity_fails_closed(self) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertTrue(store.append(first[0]))
            current_payload = first[0].to_dict()
            current_payload["metadata"]["request_fingerprint_sha256"] = "bad"
            tampered = json.dumps(
                current_payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            store.connection.execute(
                """UPDATE current_quotes
                   SET payload_json=?
                   WHERE source_id=? AND quote_key=?""",
                (tampered, first[0].source_id, first[0].quote_key),
            )
            store.connection.commit()

            with self.assertRaisesRegex(
                MarketStateIdentityError,
                "request fingerprint",
            ):
                store.append(second[0])
            self.assertEqual(len(store.events()), 1)
        finally:
            store.close()

    def test_same_process_ingestion_keeps_fresh_liveness_without_duplicate_delivery(
        self,
    ) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
            response_salts=["first", "second-raw"],
        )
        store = SQLiteMarketStore(self.market_path)
        health = SourceHealthStore(self.health_path)
        bus = MarketEventBus(store)
        delivered = []
        bus.subscribe(delivered.append)
        engine = IngestionEngine(
            bus,
            health_store=health,
            clock=_SequenceClock(
                [
                    "2026-10-04T12:00:10+00:00",
                    "2026-10-04T12:00:11+00:00",
                ]
            ),
        )
        try:
            first = engine.poll_once(provider)
            second = engine.poll_once(provider)

            self.assertEqual((first.received, first.accepted), (2, 2))
            self.assertEqual((second.received, second.accepted), (2, 0))
            self.assertEqual(len(delivered), 2)
            self.assertEqual(len(store.events()), 2)

            state = health.get("prophetx:sandbox")
            self.assertEqual(state.poll_count, 2)
            self.assertEqual(state.total_received, 4)
            self.assertEqual(state.total_accepted, 2)
            self.assertTrue(state.last_cursor.startswith("acquisition:2:sha256:"))
        finally:
            store.close()

    def test_identical_snapshot_is_suppressed_after_store_and_sequence_reopen(
        self,
    ) -> None:
        authority = self._authority(create=True)
        first_provider = self._provider(authority, [_payload()])
        store = SQLiteMarketStore(self.market_path)
        first_bus = MarketEventBus(store)
        first_engine = IngestionEngine(
            first_bus,
            clock=lambda: "2026-10-04T12:00:10+00:00",
        )
        self.assertEqual(first_engine.poll_once(first_provider).accepted, 2)
        store.close()

        reopened_authority = self._authority(create=False)
        second_provider = self._provider(
            reopened_authority,
            [_payload()],
            response_salts=["new-process-raw"],
            clocks=["2026-10-04T12:05:00+00:00"],
        )
        reopened_store = SQLiteMarketStore(self.market_path)
        delivered = []
        second_bus = MarketEventBus(reopened_store)
        second_bus.subscribe(delivered.append)
        second_engine = IngestionEngine(
            second_bus,
            clock=lambda: "2026-10-04T12:05:10+00:00",
        )
        try:
            stats = second_engine.poll_once(second_provider)
            self.assertEqual((stats.received, stats.accepted), (2, 0))
            self.assertEqual(delivered, [])
            self.assertEqual(len(reopened_store.events()), 2)
        finally:
            reopened_store.close()

    def test_changed_snapshot_after_restart_publishes_only_changed_selection(
        self,
    ) -> None:
        first_provider = self._provider(self._authority(create=True), [_payload()])
        store = SQLiteMarketStore(self.market_path)
        first_engine = IngestionEngine(
            MarketEventBus(store),
            clock=lambda: "2026-10-04T12:00:10+00:00",
        )
        self.assertEqual(first_engine.poll_once(first_provider).accepted, 2)
        store.close()

        changed = _payload()
        changed["data"]["markets"][0]["selections"][0][0]["price"] = 160
        second_provider = self._provider(
            self._authority(create=False),
            [changed],
            clocks=["2026-10-04T12:05:00+00:00"],
        )
        reopened_store = SQLiteMarketStore(self.market_path)
        delivered = []
        bus = MarketEventBus(reopened_store)
        bus.subscribe(delivered.append)
        second_engine = IngestionEngine(
            bus,
            clock=lambda: "2026-10-04T12:05:10+00:00",
        )
        try:
            stats = second_engine.poll_once(second_provider)
            self.assertEqual((stats.received, stats.accepted), (2, 1))
            self.assertEqual(len(delivered), 1)
            self.assertTrue(delivered[0].selection_id.endswith("strike-a"))
            self.assertEqual(len(reopened_store.events()), 3)
        finally:
            reopened_store.close()

    def test_crash_before_market_publication_cannot_advance_suppression_watermark(
        self,
    ) -> None:
        abandoned_provider = self._provider(
            self._authority(create=True),
            [_payload()],
        )
        abandoned_batch = abandoned_provider.read_batch()
        self.assertTrue(abandoned_batch.cursor.startswith("acquisition:1:sha256:"))

        retry_provider = self._provider(
            self._authority(create=False),
            [_payload()],
            clocks=["2026-10-04T12:01:00+00:00"],
        )
        store = SQLiteMarketStore(self.market_path)
        engine = IngestionEngine(
            MarketEventBus(store),
            clock=lambda: "2026-10-04T12:01:10+00:00",
        )
        try:
            stats = engine.poll_once(retry_provider)
            self.assertEqual((stats.received, stats.accepted), (2, 2))
            self.assertTrue(stats.cursor.startswith("acquisition:2:sha256:"))
            self.assertEqual(len(store.events()), 2)
        finally:
            store.close()

    def test_crash_after_market_commit_before_health_update_recovers_without_redelivery(
        self,
    ) -> None:
        first_provider = self._provider(self._authority(create=True), [_payload()])
        first_batch, first_events = self._normalized(first_provider)
        store = SQLiteMarketStore(self.market_path)
        bus = MarketEventBus(store)
        self.assertEqual(bus.publish_many(first_events), 2)
        self.assertTrue(first_batch.cursor.startswith("acquisition:1:sha256:"))
        store.close()

        retry_provider = self._provider(
            self._authority(create=False),
            [_payload()],
            response_salts=["retry-raw"],
            clocks=["2026-10-04T12:02:00+00:00"],
        )
        reopened_store = SQLiteMarketStore(self.market_path)
        health = SourceHealthStore(self.health_path)
        delivered = []
        retry_bus = MarketEventBus(reopened_store)
        retry_bus.subscribe(delivered.append)
        engine = IngestionEngine(
            retry_bus,
            health_store=health,
            clock=lambda: "2026-10-04T12:02:10+00:00",
        )
        try:
            stats = engine.poll_once(retry_provider)
            self.assertEqual((stats.received, stats.accepted), (2, 0))
            self.assertEqual(delivered, [])
            self.assertEqual(len(reopened_store.events()), 2)
            state = health.get("prophetx:sandbox")
            self.assertEqual(state.poll_count, 1)
            self.assertEqual(state.total_received, 2)
            self.assertEqual(state.total_accepted, 0)
            self.assertTrue(state.last_cursor.startswith("acquisition:2:sha256:"))
        finally:
            reopened_store.close()

    def test_provider_origin_authority_change_is_not_suppressed(self) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)
        metadata = deepcopy(second[0].metadata)
        metadata["provider_origin_verified"] = True
        metadata["provider_origin_authority"] = "fixed_sandbox_https"
        verified = replace(second[0], metadata=metadata)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertTrue(store.append(first[0]))
            self.assertTrue(store.append(verified))
            self.assertEqual(len(store.events()), 2)
        finally:
            store.close()

    def test_sequence_authority_identity_change_is_not_suppressed(self) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)
        metadata = deepcopy(second[0].metadata)
        metadata["sequence_authority_id"] = "different.authority.v1"
        rebound = replace(second[0], metadata=metadata)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertTrue(store.append(first[0]))
            self.assertTrue(store.append(rebound))
            self.assertEqual(len(store.events()), 2)
        finally:
            store.close()

    def test_market_status_change_is_not_suppressed(self) -> None:
        changed = _payload()
        changed["data"]["markets"][0]["status"] = "suspended"
        provider = self._provider(
            self._authority(create=True),
            [_payload(), changed],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertTrue(store.append(first[0]))
            self.assertTrue(store.append(second[0]))
            self.assertEqual(len(store.events()), 2)
        finally:
            store.close()

    def test_non_prophetx_event_storage_semantics_are_unchanged(self) -> None:
        provider = self._provider(
            self._authority(create=True),
            [_payload(), _payload()],
        )
        _first_batch, first = self._normalized(provider)
        _second_batch, second = self._normalized(provider)
        first_metadata = deepcopy(first[0].metadata)
        second_metadata = deepcopy(second[0].metadata)
        first_metadata.pop("semantic_state_contract")
        second_metadata.pop("semantic_state_contract")
        first_plain = replace(first[0], metadata=first_metadata)
        second_plain = replace(second[0], metadata=second_metadata)
        store = SQLiteMarketStore(self.market_path)
        try:
            self.assertTrue(store.append(first_plain))
            self.assertTrue(store.append(second_plain))
            self.assertEqual(len(store.events()), 2)
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
