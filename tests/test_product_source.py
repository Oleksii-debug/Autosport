from __future__ import annotations

import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import autosport.product_source as product_source_module
from autosport.causal_collector import (
    CollectorDeltaStore,
    DesktopApplicationReceipt,
    DesktopDeltaCheckpointStore,
    StreamCheckpoint,
)
from autosport.collector_retention import CollectorRetentionManager
from autosport.domain import MarketType
from autosport.event_lifecycle import CatalogCheckpoint, EventPhase
from autosport.parlayapi_provider import ParlayApiTableTennisProvider
from autosport.product_source import (
    ParlayApiProductSource,
    ProductSourceError,
    ProductSourcePayloadError,
    ProductSourceStateError,
    create_parlay_product_source,
)
from autosport.providers import ProviderBatch, ProviderQuote


_SOURCE_ID = "parlayapi:table_tennis"


class _Provider:
    source_id = _SOURCE_ID

    def __init__(self, batches: list[ProviderBatch]) -> None:
        self.batches = list(batches)

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if max_items <= 0:
            raise AssertionError("source requested an invalid provider batch bound")
        if not self.batches:
            raise AssertionError("unexpected provider read")
        return self.batches.pop(0)


def _quote(
    *,
    odds: str = "1.80",
    sequence: int = 1,
    provider_event_id: str = "event-1",
    exchange_side: str | None = None,
    observed_ts: str = "2026-09-20T17:34:00+00:00",
    source_ts: str | None = "2026-09-20T17:33:59+00:00",
) -> ProviderQuote:
    return ProviderQuote(
        provider_event_id=provider_event_id,
        provider_market_id="book:h2h",
        provider_selection_id="player-a",
        decimal_odds=Decimal(odds),
        observed_ts=observed_ts,
        sequence=sequence,
        market_type=MarketType.WINNER,
        status="open",
        source_ts=source_ts,
        metadata={
            "provider": "parlayapi",
            "sport_key": "table_tennis",
            "commence_time": "2026-09-20T18:00:00+00:00",
            "home_team": "Player A",
            "away_team": "Player B",
            "bookmaker_key": "book",
            "market_key": "h2h",
        },
        sport="table_tennis",
        exchange_side=exchange_side,
    )


def _batch(
    *,
    cursor: str,
    odds: str = "1.80",
    sequence: int = 1,
    provider_event_id: str = "event-1",
) -> ProviderBatch:
    return ProviderBatch(
        source_id=_SOURCE_ID,
        quotes=(
            _quote(
                odds=odds,
                sequence=sequence,
                provider_event_id=provider_event_id,
            ),
        ),
        cursor=cursor,
    )


def _catalog_checkpoint(page) -> CatalogCheckpoint:
    return CatalogCheckpoint(
        source_id=page.source_id,
        stream_epoch=page.stream_epoch,
        cursor=page.cursor,
        position=page.position,
        page_sha256=page.digest,
    )


def _stream_checkpoint(delta) -> StreamCheckpoint:
    return StreamCheckpoint(
        delta.source_id,
        delta.stream_epoch,
        delta.source_cursor,
        delta.cursor_position,
        delta.delta_id,
    )


def _archive_pending_delta(source: ParlayApiProductSource, delta) -> None:
    event = source.resolve_event(delta)
    store = source._require_collector_store()
    store._append_with_runtime_stream_epoch(
        delta,
        activated_at=delta.collector_committed_at,
        event=event,
    )


def _ack_retention_delta(
    checkpoint: DesktopDeltaCheckpointStore,
    delta,
    *,
    ordinal: int,
) -> None:
    checkpoint.ack(
        delta,
        application_receipt=DesktopApplicationReceipt(
            delta_id=delta.delta_id,
            canonical_event_digest=delta.canonical_event_digest,
            receipt_id=f"product-source-retention:{ordinal}:{delta.delta_id}",
            applied_at=f"2026-09-20T17:40:0{ordinal}+00:00",
        ),
        acknowledged_at=f"2026-09-20T17:41:0{ordinal}+00:00",
    )


class ParlayApiProductSourceTests(unittest.TestCase):
    def test_source_payload_evidence_binds_exchange_side(self) -> None:
        back = _quote(exchange_side="back")
        lay = _quote(exchange_side="lay")

        self.assertNotEqual(
            ParlayApiProductSource._quote_payload_bytes(back),
            ParlayApiProductSource._quote_payload_bytes(lay),
        )

    def test_source_payload_evidence_covers_every_provider_quote_field(self) -> None:
        payload = json.loads(
            ParlayApiProductSource._quote_payload_bytes(
                _quote(exchange_side="back")
            ).decode("utf-8")
        )
        self.assertEqual(set(payload), set(ProviderQuote.__dataclass_fields__))

    def test_provider_observed_ts_rejects_nonzero_submicrosecond_precision(self) -> None:
        quote = _quote(observed_ts="2026-09-20T17:34:00.1234567+00:00")
        batch = ProviderBatch(source_id=_SOURCE_ID, quotes=(quote,), cursor="snapshot-1")
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([batch]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "precision finer than microseconds",
            ):
                source.fetch_catalog_page(None)

    def test_provider_source_ts_rejects_nonzero_submicrosecond_precision(self) -> None:
        quote = _quote(source_ts="2026-09-20T17:33:59.9999999+00:00")
        batch = ProviderBatch(source_id=_SOURCE_ID, quotes=(quote,), cursor="snapshot-1")
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([batch]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "precision finer than microseconds",
            ):
                source.fetch_catalog_page(None)

    def test_exact_submicrosecond_zero_tail_remains_representable(self) -> None:
        instant = ParlayApiProductSource._instant(
            "2026-09-20T17:34:00.1234560+00:00",
            "test instant",
        )
        self.assertEqual(instant.microsecond, 123456)

    def test_truncated_provider_page_must_fill_requested_acquisition_bound(self) -> None:
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(_quote(),),
            cursor="snapshot-1",
            quality_flags=("TRUNCATED_BATCH",),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([batch]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "must fill requested acquisition page",
            ):
                source.fetch_catalog_page(None)

    def test_truncated_provider_page_fails_at_exact_snapshot_capacity(self) -> None:
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(_quote(),),
            cursor="snapshot-1",
            quality_flags=("TRUNCATED_BATCH",),
        )
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([batch]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with (
                patch.object(ParlayApiProductSource, "_READ_BATCH_ITEMS", 1),
                patch.object(ParlayApiProductSource, "_MAX_SNAPSHOT_ITEMS", 1),
                self.assertRaisesRegex(
                    ProductSourcePayloadError,
                    "exceeds bounded source capacity",
                ),
            ):
                source.fetch_catalog_page(None)

    def test_snapshot_becomes_restart_safe_catalog_delta_and_event(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:operator-approved",
                retention_ref="retention:parlayapi:operator-approved",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )

            page = source.fetch_catalog_page(None)
            self.assertEqual(page.position, 0)
            self.assertEqual(page.cursor, "snapshot-1")
            self.assertEqual(len(page.events), 1)
            self.assertEqual(page.events[0].event_id, "event-1")
            self.assertEqual(page.events[0].identity, f"{_SOURCE_ID}:event-1")
            self.assertEqual(page.events[0].phase, EventPhase.PRE_MATCH)

            deltas = source.fetch_deltas(None, (), 10)
            self.assertEqual(len(deltas), 1)
            delta = deltas[0]
            self.assertEqual(delta.cursor_position, 0)
            self.assertEqual(delta.source_cursor, "snapshot-1")
            self.assertEqual(delta.event_id, f"{_SOURCE_ID}:event-1")
            event = source.resolve_event(delta)
            self.assertEqual(event.decimal_odds, Decimal("1.80"))
            self.assertEqual(event.event_id, delta.event_id)

            _archive_pending_delta(source, delta)
            collector_checkpoint = _stream_checkpoint(delta)
            self.assertEqual(source.fetch_deltas(collector_checkpoint, (), 10), ())

            restored = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-2", odds="1.90", sequence=2)]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:operator-approved",
                retention_ref="retention:parlayapi:operator-approved",
                clock=lambda: "2026-09-20T17:34:03+00:00",
            )
            self.assertEqual(restored.resolve_event(delta), event)

            page2 = restored.fetch_catalog_page(_catalog_checkpoint(page))
            self.assertEqual(page2.position, 1)
            self.assertEqual(page2.cursor, "snapshot-2")
            next_deltas = restored.fetch_deltas(collector_checkpoint, (), 10)
            self.assertEqual(len(next_deltas), 1)
            self.assertEqual(next_deltas[0].cursor_position, 1)
            self.assertEqual(
                restored.resolve_event(next_deltas[0]).decimal_odds,
                Decimal("1.90"),
            )

    def test_uncommitted_snapshot_is_replayed_instead_of_fetching_past_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            first_page = source.fetch_catalog_page(None)
            first_delta = source.fetch_deltas(None, (), 1)[0]

            restored = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:03+00:00",
            )
            replay_page = restored.fetch_catalog_page(_catalog_checkpoint(first_page))
            self.assertEqual(replay_page.digest, first_page.digest)
            replay_delta = restored.fetch_deltas(None, (), 1)[0]
            self.assertEqual(replay_delta, first_delta)

    def test_pending_snapshot_freezes_compliance_provenance_across_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:acquired",
                retention_ref="retention:parlayapi:acquired",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            source.fetch_catalog_page(None)

            restored = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:post-restart",
                retention_ref="retention:parlayapi:post-restart",
                clock=lambda: "2026-09-20T17:34:03+00:00",
            )
            delta = restored.fetch_deltas(None, (), 10)[0]

            self.assertEqual(delta.lawful_terms_ref, "terms:parlayapi:acquired")
            self.assertEqual(delta.retention_ref, "retention:parlayapi:acquired")

    def test_pending_snapshot_captures_compliance_provenance_before_provider_io(self) -> None:
        class _MutatingProvider(_Provider):
            mutate = None

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                if self.mutate is not None:
                    self.mutate()
                return super().read_batch(max_items)

        with tempfile.TemporaryDirectory() as directory:
            provider = _MutatingProvider([_batch(cursor="snapshot-1")])
            source = ParlayApiProductSource(
                provider,
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:before-io",
                retention_ref="retention:parlayapi:before-io",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )

            def mutate_source_provenance() -> None:
                source.lawful_terms_ref = "terms:parlayapi:mutated-during-io"
                source.retention_ref = "retention:parlayapi:mutated-during-io"

            provider.mutate = mutate_source_provenance
            source.fetch_catalog_page(None)
            delta = source.fetch_deltas(None, (), 10)[0]

            self.assertEqual(delta.lawful_terms_ref, "terms:parlayapi:before-io")
            self.assertEqual(delta.retention_ref, "retention:parlayapi:before-io")

    def test_pending_snapshot_rejects_provider_rebinding_during_provider_io(self) -> None:
        class _MutatingProvider(_Provider):
            mutate = None

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                if self.mutate is not None:
                    self.mutate()
                return super().read_batch(max_items)

        with tempfile.TemporaryDirectory() as directory:
            first = _MutatingProvider([_batch(cursor="snapshot-1")])
            second = _Provider([_batch(cursor="snapshot-1")])
            source = ParlayApiProductSource(
                first,
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            first.mutate = lambda: setattr(source, "provider", second)

            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "provider changed during acquisition",
            ):
                source.fetch_catalog_page(None)

            self.assertEqual(len(second.batches), 1)

    def test_pending_snapshot_rejects_normalizer_rebinding_during_provider_io(self) -> None:
        class _MutatingProvider(_Provider):
            mutate = None

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                if self.mutate is not None:
                    self.mutate()
                return super().read_batch(max_items)

        with tempfile.TemporaryDirectory() as directory:
            provider = _MutatingProvider([_batch(cursor="snapshot-1")])
            source = ParlayApiProductSource(
                provider,
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )

            class _HostileNormalizer:
                calls = 0

                def normalize(self, source_id, quote):
                    self.calls += 1
                    raise AssertionError("rebound normalizer must not execute")

            hostile = _HostileNormalizer()
            provider.mutate = lambda: setattr(source, "normalizer", hostile)

            with self.assertRaisesRegex(
                ProductSourceStateError,
                "normalizer changed during acquisition",
            ):
                source.fetch_catalog_page(None)

            self.assertEqual(hostile.calls, 0)

    def test_legacy_event_migration_commits_before_source_history_clear(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            source.fetch_catalog_page(None)
            delta = source.fetch_deltas(None, (), 1)[0]
            event = source.resolve_event(delta)
            store = source._require_collector_store()
            store._append_with_runtime_stream_epoch(
                delta,
                activated_at=delta.collector_committed_at,
            )
            state = source._read_state()
            state["event_cache"] = {delta.delta_id: event.to_dict()}
            state["last_committed_quote_digests"] = {
                event.quote_key: delta.canonical_event_digest
            }
            state["last_committed_dedupe_digests"] = {
                event.dedupe_key: delta.canonical_event_digest
            }
            source._write_state(state)

            with patch.object(
                source,
                "_write_state",
                side_effect=ProductSourceStateError("injected source publish failure"),
            ):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "injected source publish failure",
                ):
                    source._migrate_legacy_history_to_collector_store()

            self.assertEqual(store.resolve_event(delta), event)
            still_legacy = source._read_state()
            self.assertIn(delta.delta_id, still_legacy["event_cache"])

            source._migrate_legacy_history_to_collector_store()
            migrated = source._read_state()
            self.assertEqual(migrated["event_cache"], {})
            self.assertEqual(migrated["last_committed_quote_digests"], {})
            self.assertEqual(migrated["last_committed_dedupe_digests"], {})
            self.assertEqual(store.resolve_event(delta), event)

    def test_legacy_migration_accepts_multi_price_canonical_retirement_without_resurrection(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider(
                    [
                        _batch(
                            cursor="snapshot-1",
                            odds="1.80",
                            sequence=1,
                            provider_event_id="event-1",
                        ),
                        _batch(
                            cursor="snapshot-2",
                            odds="1.90",
                            sequence=2,
                            provider_event_id="event-1",
                        ),
                        _batch(
                            cursor="snapshot-3",
                            odds="2.00",
                            sequence=3,
                            provider_event_id="event-2",
                        ),
                    ]
                ),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )

            first_page = source.fetch_catalog_page(None)
            first = source.fetch_deltas(None, (), 1)[0]
            first_event = source.resolve_event(first)
            _archive_pending_delta(source, first)
            source.fetch_deltas(_stream_checkpoint(first), (), 1)

            second_page = source.fetch_catalog_page(_catalog_checkpoint(first_page))
            second = source.fetch_deltas(_stream_checkpoint(first), (), 1)[0]
            second_event = source.resolve_event(second)
            _archive_pending_delta(source, second)
            source.fetch_deltas(_stream_checkpoint(second), (), 1)

            source.fetch_catalog_page(_catalog_checkpoint(second_page))
            third = source.fetch_deltas(_stream_checkpoint(second), (), 1)[0]
            third_event = source.resolve_event(third)
            _archive_pending_delta(source, third)
            store = source._require_collector_store()

            self.assertEqual(first_event.quote_key, second_event.quote_key)
            self.assertNotEqual(
                first.canonical_event_digest,
                second.canonical_event_digest,
            )

            legacy = source._read_state()
            legacy["pending"] = None
            legacy["event_cache"] = {
                first.delta_id: first_event.to_dict(),
                second.delta_id: second_event.to_dict(),
            }
            legacy["last_committed_quote_digests"] = {
                second_event.quote_key: second.canonical_event_digest
            }
            legacy["last_committed_dedupe_digests"] = {
                first_event.dedupe_key: first.canonical_event_digest,
                second_event.dedupe_key: second.canonical_event_digest,
            }
            source._write_state(legacy)

            desktop = DesktopDeltaCheckpointStore(
                workspace / "desktop-retention-checkpoint.json"
            )
            _ack_retention_delta(desktop, first, ordinal=1)
            _ack_retention_delta(desktop, second, ordinal=2)
            _ack_retention_delta(desktop, third, ordinal=3)

            connection = store._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                generation = connection.execute(
                    "SELECT MAX(generation) FROM collector_epoch_activations_v1 "
                    "WHERE source_id=?",
                    (source.source_id,),
                ).fetchone()[0]
                self.assertIsInstance(generation, int)
                connection.execute(
                    "INSERT INTO collector_epoch_activations_v1("
                    "source_id, generation, stream_epoch, activated_at"
                    ") VALUES(?,?,?,?)",
                    (
                        source.source_id,
                        int(generation) + 1,
                        "post-retention-test-epoch",
                        "2026-09-20T17:42:00+00:00",
                    ),
                )
                connection.commit()
            finally:
                if connection.in_transaction:
                    connection.rollback()
                connection.close()

            manager = CollectorRetentionManager(store)
            plan = manager.preview(
                source_id=source.source_id,
                stream_epoch=source.stream_epoch,
                desktop_checkpoint=desktop,
            )
            self.assertEqual(
                plan.delete_delta_ids,
                (first.delta_id, second.delta_id),
            )
            self.assertIn(third.delta_id, plan.retained_delta_ids)
            result = manager.compact(
                plan,
                desktop_checkpoint=desktop,
                compacted_at="2026-09-20T17:43:00+00:00",
            )
            self.assertEqual(
                result.deleted_delta_ids,
                (first.delta_id, second.delta_id),
            )
            for retired in (first, second):
                self.assertIsNone(store.get(retired.delta_id))
                with self.assertRaisesRegex(ValueError, "not retained exactly"):
                    store.resolve_event(retired)
            self.assertEqual(store.resolve_event(third), third_event)

            restored = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            restored.bind_collector_store(store)

            migrated = restored._read_state()
            self.assertEqual(migrated["event_cache"], {})
            self.assertEqual(migrated["last_committed_quote_digests"], {})
            self.assertEqual(migrated["last_committed_dedupe_digests"], {})
            for retired in (first, second):
                self.assertIsNone(store.get(retired.delta_id))
                with self.assertRaisesRegex(ValueError, "not retained exactly"):
                    store.resolve_event(retired)
            self.assertEqual(store.resolve_event(third), third_event)

    def test_legacy_digest_migration_verifies_in_bounded_chunks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            state = source._read_state()
            digest = "a" * 64
            state["last_committed_quote_digests"] = {
                "quote-1": digest,
                "quote-2": digest,
                "quote-3": digest,
            }
            source._write_state(state)
            store = source._require_collector_store()
            calls = []

            def fake_maps(*, source_id, stream_epoch, quote_keys, dedupe_keys):
                calls.append((quote_keys, dedupe_keys))
                return (
                    {key: digest for key in quote_keys},
                    {key: digest for key in dedupe_keys},
                )

            with (
                patch.object(source, "_LEGACY_HISTORY_VERIFY_CHUNK", 2),
                patch.object(store, "event_digest_maps", side_effect=fake_maps),
            ):
                source._migrate_legacy_history_to_collector_store()

            self.assertEqual(
                [len(quote_keys) for quote_keys, _ in calls],
                [2, 1],
            )
            migrated = source._read_state()
            self.assertEqual(migrated["last_committed_quote_digests"], {})
            self.assertEqual(migrated["last_committed_dedupe_digests"], {})
            self.assertEqual(migrated["event_cache"], {})

    def test_pending_snapshot_rejects_provider_batch_subclass(self) -> None:
        class _BatchSubclass(ProviderBatch):
            pass

        class _SubclassProvider:
            source_id = _SOURCE_ID

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                del max_items
                return _BatchSubclass(
                    source_id=_SOURCE_ID,
                    quotes=(_quote(),),
                    cursor="snapshot-1",
                )

        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _SubclassProvider(),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "exact ProviderBatch",
            ):
                source.fetch_catalog_page(None)

    def test_pending_snapshot_revalidates_mutated_provider_quote_fields(self) -> None:
        quote = _quote()
        object.__setattr__(quote, "provider_event_id", "forged:event")
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(),
            cursor="snapshot-1",
        )
        object.__setattr__(batch, "quotes", (quote,))

        class _MutatedEvidenceProvider:
            source_id = _SOURCE_ID

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                del max_items
                return batch

        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _MutatedEvidenceProvider(),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "acquisition-boundary validation",
            ):
                source.fetch_catalog_page(None)

    def test_pending_snapshot_rejects_decimal_subclass_before_virtual_dispatch(self) -> None:
        calls: list[str] = []

        class _HostileDecimal(Decimal):
            def is_finite(self):
                calls.append("is_finite")
                raise AssertionError("Decimal subclass virtual dispatch must not run")

        quote = _quote()
        object.__setattr__(quote, "decimal_odds", _HostileDecimal("1.80"))
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(),
            cursor="snapshot-1",
        )
        object.__setattr__(batch, "quotes", (quote,))

        class _ProviderWithHostileOdds:
            source_id = _SOURCE_ID

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                del max_items
                return batch

        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _ProviderWithHostileOdds(),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "non-canonical acquisition value types",
            ):
                source.fetch_catalog_page(None)

        self.assertEqual(calls, [])

    def test_pending_snapshot_rejects_string_subclass_before_validation_dispatch(self) -> None:
        calls: list[str] = []

        class _HostileText(str):
            def strip(self, *args, **kwargs):
                calls.append("strip")
                raise AssertionError("str subclass validation dispatch must not run")

        quote = _quote()
        object.__setattr__(quote, "provider_market_id", _HostileText("book:h2h"))
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(),
            cursor="snapshot-1",
        )
        object.__setattr__(batch, "quotes", (quote,))

        class _ProviderWithHostileText:
            source_id = _SOURCE_ID

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                del max_items
                return batch

        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _ProviderWithHostileText(),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "non-canonical acquisition value types",
            ):
                source.fetch_catalog_page(None)

        self.assertEqual(calls, [])

    def test_pending_snapshot_does_not_coerce_noncanonical_metadata_containers(self) -> None:
        quote = _quote()
        quote.metadata["tuple_value"] = ("must", "not", "coerce")
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(quote,),
            cursor="snapshot-1",
        )

        class _TupleMetadataProvider:
            source_id = _SOURCE_ID

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                del max_items
                return batch

        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _TupleMetadataProvider(),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "non-canonical JSON value type",
            ):
                source.fetch_catalog_page(None)

    def test_pending_snapshot_rejects_metadata_container_subclasses(self) -> None:
        class _HostileDict(dict):
            pass

        quote = _quote()
        object.__setattr__(quote, "metadata", _HostileDict(quote.metadata))
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(),
            cursor="snapshot-1",
        )
        object.__setattr__(batch, "quotes", (quote,))

        class _MetadataSubclassProvider:
            source_id = _SOURCE_ID

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                del max_items
                return batch

        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _MetadataSubclassProvider(),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "non-canonical acquisition value types",
            ):
                source.fetch_catalog_page(None)

    def test_pending_snapshot_freezes_mutable_provider_metadata_before_use(self) -> None:
        quote = _quote()
        quote.metadata["nested"] = {"value": "original"}
        batch = ProviderBatch(
            source_id=_SOURCE_ID,
            quotes=(quote,),
            cursor="snapshot-1",
        )

        class _MetadataProvider:
            source_id = _SOURCE_ID

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                del max_items
                return batch

        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _MetadataProvider(),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            page = source.fetch_catalog_page(None)
            quote.metadata["nested"]["value"] = "mutated-after-return"
            delta = source.fetch_deltas(None, (), 10)[0]
            event = source.resolve_event(delta)

            self.assertEqual(page.events[0].event_id, "event-1")
            self.assertEqual(event.metadata["nested"]["value"], "original")

    def test_pending_snapshot_rejects_provider_read_code_mutation_during_io(self) -> None:
        class _SelfMutatingProvider:
            source_id = _SOURCE_ID

            def __init__(self) -> None:
                self.mutate = None
                self.batches = [_batch(cursor="snapshot-1")]

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                if max_items <= 0 or not self.batches:
                    raise AssertionError("unexpected provider read")
                result = self.batches.pop(0)
                if self.mutate is not None:
                    self.mutate()
                return result

        provider = _SelfMutatingProvider()
        read_func = provider.read_batch.__func__
        original_code = read_func.__code__

        def forged_read_batch(self, max_items: int = 1000) -> ProviderBatch:
            raise AssertionError("mutated provider executable must never gain authority")

        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                provider,
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            provider.mutate = lambda: setattr(
                read_func,
                "__code__",
                forged_read_batch.__code__,
            )
            try:
                with self.assertRaisesRegex(
                    ProductSourcePayloadError,
                    "provider read executable changed during acquisition",
                ):
                    source.fetch_catalog_page(None)
            finally:
                read_func.__code__ = original_code

    def test_pending_snapshot_rejects_normalizer_code_mutation_during_provider_io(self) -> None:
        class _MutatingProvider(_Provider):
            mutate = None

            def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                result = super().read_batch(max_items)
                if self.mutate is not None:
                    self.mutate()
                return result

        with tempfile.TemporaryDirectory() as directory:
            provider = _MutatingProvider([_batch(cursor="snapshot-1")])
            source = ParlayApiProductSource(
                provider,
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            normalize_func = source.normalizer.normalize.__func__
            original_code = normalize_func.__code__

            def forged_normalize(self, source_id, quote):
                raise AssertionError("mutated normalizer executable must never gain authority")

            provider.mutate = lambda: setattr(
                normalize_func,
                "__code__",
                forged_normalize.__code__,
            )
            try:
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "normalizer executable changed during acquisition",
                ):
                    source.fetch_catalog_page(None)
            finally:
                normalize_func.__code__ = original_code

    def test_legacy_unassigned_pending_without_provenance_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            with self.assertRaisesRegex(
                ProductSourceStateError,
                "lacks acquisition compliance provenance",
            ):
                source._validate_pending(
                    {
                        "catalog_cursor": "legacy-snapshot",
                        "catalog_position": 0,
                        "catalog_events": [],
                        "quality_flags": [],
                        "items": [],
                        "assigned": False,
                        "confirmed": False,
                    }
                )

    def test_assigned_pending_delta_must_match_frozen_compliance_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            source.fetch_catalog_page(None)
            source.fetch_deltas(None, (), 10)
            state = source._read_state()
            pending = dict(state["pending"])
            pending["lawful_terms_ref"] = "terms:parlayapi:substituted"

            with self.assertRaisesRegex(
                ProductSourceStateError,
                "pending delta is not bound to source evidence",
            ):
                source._validate_pending(pending)

    def test_catalog_checkpoint_must_match_exact_cursor_and_page_digest(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            page = source.fetch_catalog_page(None)
            forged_cursor = CatalogCheckpoint(
                source_id=page.source_id,
                stream_epoch=page.stream_epoch,
                cursor="forged-cursor",
                position=page.position,
                page_sha256=page.digest,
            )
            with self.assertRaisesRegex(ProductSourceStateError, "pending exact page"):
                source.fetch_catalog_page(forged_cursor)

            forged_digest = CatalogCheckpoint(
                source_id=page.source_id,
                stream_epoch=page.stream_epoch,
                cursor=page.cursor,
                position=page.position,
                page_sha256="0" * 64,
            )
            with self.assertRaisesRegex(ProductSourceStateError, "pending exact page"):
                source.fetch_catalog_page(forged_digest)

            replay = source.fetch_catalog_page(_catalog_checkpoint(page))
            self.assertEqual(replay.digest, page.digest)

    def test_collector_checkpoint_must_match_exact_cursor_and_delta_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            source.fetch_catalog_page(None)
            delta = source.fetch_deltas(None, (), 1)[0]

            forged_cursor = StreamCheckpoint(
                delta.source_id,
                delta.stream_epoch,
                "forged-cursor",
                delta.cursor_position,
                delta.delta_id,
            )
            with self.assertRaisesRegex(ProductSourceStateError, "exact durable delta"):
                source.fetch_deltas(forged_cursor, (), 1)

            forged_delta = StreamCheckpoint(
                delta.source_id,
                delta.stream_epoch,
                delta.source_cursor,
                delta.cursor_position,
                "forged-delta-id",
            )
            with self.assertRaisesRegex(ProductSourceStateError, "exact durable delta"):
                source.fetch_deltas(forged_delta, (), 1)

            self.assertEqual(source.fetch_deltas(_stream_checkpoint(delta), (), 1), ())

    def test_same_causal_identity_cannot_change_price(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            page = source.fetch_catalog_page(None)
            delta = source.fetch_deltas(None, (), 1)[0]
            _archive_pending_delta(source, delta)
            checkpoint = _stream_checkpoint(delta)
            source.fetch_deltas(checkpoint, (), 1)

            restored = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-2", odds="1.95", sequence=1)]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:03+00:00",
            )
            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "without advancing its causal identity",
            ):
                restored.fetch_catalog_page(_catalog_checkpoint(page))

    def test_missing_retained_collector_event_payload_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider(
                    [
                        _batch(cursor="snapshot-1"),
                        _batch(cursor="snapshot-2", odds="1.90", sequence=2),
                    ]
                ),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            page = source.fetch_catalog_page(None)
            delta = source.fetch_deltas(None, (), 1)[0]
            _archive_pending_delta(source, delta)
            source.fetch_deltas(_stream_checkpoint(delta), (), 1)
            source.fetch_catalog_page(_catalog_checkpoint(page))

            store = source._require_collector_store()
            connection = store._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "DELETE FROM collector_event_payloads_v1 WHERE delta_id=?",
                    (delta.delta_id,),
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaises(ProductSourceStateError):
                source.resolve_event(delta)

    def test_lazy_store_creation_ignores_mutable_module_constructor_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            hostile_calls = []

            class AttackerStore:
                def __init__(self, *args, **kwargs):
                    hostile_calls.append((args, kwargs))
                    raise AssertionError("hostile store constructor executed")

            with patch.object(
                product_source_module,
                "CollectorDeltaStore",
                AttackerStore,
            ):
                store = source._require_collector_store()

            self.assertIs(type(store), CollectorDeltaStore)
            self.assertEqual(hostile_calls, [])

    def test_collector_store_binding_rejects_subclass_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )

            class ForgedCollectorDeltaStore(CollectorDeltaStore):
                pass

            forged = ForgedCollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            with self.assertRaisesRegex(
                TypeError,
                "exact canonical CollectorDeltaStore",
            ):
                source.bind_collector_store(forged)

            self.assertIsNone(source._collector_store)

    def test_collector_store_binding_rejects_wrong_path_when_resolve_is_rebound(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            wrong_store = CollectorDeltaStore(
                Path(directory) / "wrong-workspace" / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            hostile_calls = []

            def hostile_resolve(path, *, strict=False):
                hostile_calls.append((path, strict))
                return source._collector_store_path

            with patch.object(Path, "resolve", hostile_resolve):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "canonical workspace store",
                ):
                    source.bind_collector_store(wrong_store)

            self.assertEqual(hostile_calls, [])
            self.assertIsNone(source._collector_store)

    def test_collector_store_binding_fails_closed_on_path_equality_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            wrong_store = CollectorDeltaStore(
                Path(directory) / "wrong-workspace" / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            hostile_calls = []

            def hostile_eq(left, right):
                hostile_calls.append((left, right))
                return True

            with patch.object(Path, "__eq__", hostile_eq):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "path comparison dispatch was replaced",
                ):
                    source.bind_collector_store(wrong_store)

            self.assertEqual(hostile_calls, [])
            self.assertIsNone(source._collector_store)

    def test_bound_store_reuse_fails_closed_on_path_equality_rebind(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            store = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            source.bind_collector_store(store)
            hostile_calls = []

            def hostile_eq(left, right):
                hostile_calls.append((left, right))
                return True

            with patch.object(Path, "__eq__", hostile_eq):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "path comparison dispatch was replaced",
                ):
                    source._require_collector_store()

            self.assertEqual(hostile_calls, [])

    def test_bound_store_instance_method_shadow_cannot_redirect_history_reads(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            store = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            source.bind_collector_store(store)
            hostile_calls = []

            def hostile(*args, **kwargs):
                hostile_calls.append((args, kwargs))
                raise AssertionError("hostile instance dispatch executed")

            store.event_digest_maps = hostile
            page = source.fetch_catalog_page(None)

            self.assertEqual(page.cursor, "snapshot-1")
            self.assertEqual(hostile_calls, [])

    def test_bound_store_transitive_connect_shadow_fails_before_hostile_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            store = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            source.bind_collector_store(store)
            source.fetch_catalog_page(None)
            delta = source.fetch_deltas(None, (), 1)[0]
            _archive_pending_delta(source, delta)
            source.fetch_deltas(_stream_checkpoint(delta), (), 1)
            hostile_calls = []

            def hostile_connect():
                hostile_calls.append(True)
                raise AssertionError("hostile transitive _connect executed")

            with patch.object(store, "_connect", hostile_connect):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "durable-history dispatch was replaced",
                ):
                    source.resolve_event(delta)

            self.assertEqual(hostile_calls, [])

    def test_bound_store_staticmethod_code_mutation_fails_before_hostile_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            store = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            source.bind_collector_store(store)
            descriptor = vars(CollectorDeltaStore)["_bounded_identity_keys"]
            original_bounded_keys = descriptor.__func__
            original_code = original_bounded_keys.__code__

            def hostile_bounded_keys(values, field):
                raise AssertionError(
                    "hostile in-place _bounded_identity_keys code executed"
                )

            try:
                original_bounded_keys.__code__ = hostile_bounded_keys.__code__
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "durable-history dispatch was replaced",
                ):
                    source.fetch_catalog_page(None)
            finally:
                original_bounded_keys.__code__ = original_code

    def test_bound_store_classmethod_code_mutation_fails_before_hostile_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            store = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            source.bind_collector_store(store)
            descriptor = vars(CollectorDeltaStore)["_path_file_identity"]
            original_path_identity = descriptor.__func__
            original_code = original_path_identity.__code__

            def hostile_path_identity(cls, path):
                raise AssertionError(
                    "hostile in-place _path_file_identity code executed"
                )

            try:
                original_path_identity.__code__ = hostile_path_identity.__code__
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "durable-history dispatch was replaced",
                ):
                    source.fetch_catalog_page(None)
            finally:
                original_path_identity.__code__ = original_code

    def test_bound_store_class_method_replacement_fails_before_hostile_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            store = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            source.bind_collector_store(store)
            hostile_calls = []

            def hostile(*args, **kwargs):
                hostile_calls.append((args, kwargs))
                raise AssertionError("hostile class dispatch executed")

            with patch.object(CollectorDeltaStore, "event_digest_maps", hostile):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "durable-history dispatch was replaced",
                ):
                    source.fetch_catalog_page(None)

            self.assertEqual(hostile_calls, [])

    def test_failed_explicit_collector_store_binding_rolls_back_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            store = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )

            with patch.object(
                source,
                "_migrate_legacy_history_to_collector_store",
                side_effect=ProductSourceStateError("injected migration failure"),
            ):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "injected migration failure",
                ):
                    source.bind_collector_store(store)

            self.assertIsNone(source._collector_store)

    def test_failed_lazy_collector_store_binding_rolls_back_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )

            with patch.object(
                source,
                "_migrate_legacy_history_to_collector_store",
                side_effect=ProductSourceStateError("injected migration failure"),
            ):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "injected migration failure",
                ):
                    source._require_collector_store()

            self.assertIsNone(source._collector_store)

    def test_collector_store_binding_is_same_object_idempotent_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            first = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            source.bind_collector_store(first)
            source.bind_collector_store(first)

            replacement = CollectorDeltaStore(
                workspace / "collector_deltas.json",
                max_bytes=4 * 1024 * 1024,
            )
            with self.assertRaisesRegex(
                ProductSourceStateError,
                "authority cannot be replaced",
            ):
                source.bind_collector_store(replacement)

            self.assertIs(source._require_collector_store(), first)

    def test_construction_defers_collector_store_until_canonical_binding(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            store_path = workspace / "collector_deltas.json"

            self.assertIsNone(source._collector_store)
            self.assertFalse(store_path.exists())

            store = CollectorDeltaStore(store_path, max_bytes=4 * 1024 * 1024)
            source.bind_collector_store(store)

            self.assertIs(source._require_collector_store(), store)
            self.assertEqual(store.configured_max_bytes, 4 * 1024 * 1024)

    def test_oversized_legacy_history_migrates_before_current_state_cap(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            source.fetch_catalog_page(None)
            delta = source.fetch_deltas(None, (), 1)[0]
            event = source.resolve_event(delta)
            store = source._require_collector_store()
            store._append_with_runtime_stream_epoch(
                delta,
                activated_at=delta.collector_committed_at,
            )

            legacy = source._read_state()
            legacy["pending"] = None
            legacy["event_cache"] = {delta.delta_id: event.to_dict()}
            legacy["last_committed_quote_digests"] = {
                event.quote_key: delta.canonical_event_digest
            }
            legacy["last_committed_dedupe_digests"] = {
                event.dedupe_key: delta.canonical_event_digest
            }
            source._write_state(legacy)

            cleared = dict(source._read_state())
            cleared["event_cache"] = {}
            cleared["last_committed_quote_digests"] = {}
            cleared["last_committed_dedupe_digests"] = {}
            cleared_rendered = (
                json.dumps(
                    source._seal_state(cleared),
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                + "\n"
            ).encode("utf-8")
            legacy_size = source.state_path.stat().st_size
            bound = len(cleared_rendered) + 64
            self.assertLess(bound, legacy_size)

            with patch.object(ParlayApiProductSource, "_MAX_STATE_BYTES", bound):
                restored = ParlayApiProductSource(
                    _Provider([]),
                    workspace=workspace,
                    authority_root=authority_root,
                    lawful_terms_ref="terms:parlayapi:v1",
                    retention_ref="retention:parlayapi:v1",
                )
                self.assertTrue(restored._legacy_oversized_state)
                budgeted = CollectorDeltaStore(
                    workspace / "collector_deltas.json",
                    max_bytes=4 * 1024 * 1024,
                )
                restored.bind_collector_store(budgeted)

                self.assertFalse(restored._legacy_oversized_state)
                self.assertLessEqual(restored.state_path.stat().st_size, bound)
                self.assertEqual(budgeted.resolve_event(delta), event)

    def test_state_reader_rejects_symlink_and_byte_bound_before_parse(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            original = source.state_path.read_bytes()
            oversized = source.state_path.with_name("oversized.json")
            oversized.write_bytes(b"x" * 65)
            old_path = source.state_path
            source.state_path = oversized
            old_bound = source._MAX_STATE_BYTES
            source._MAX_STATE_BYTES = 64
            try:
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "bounded canonical file",
                ):
                    source._read_state_unlocked()
            finally:
                source._MAX_STATE_BYTES = old_bound
                source.state_path = old_path
            self.assertEqual(source.state_path.read_bytes(), original)

    def test_state_reader_requires_canonical_no_follow_descriptor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )

            with patch.object(
                product_source_module,
                "_open_read_only_descriptor",
                side_effect=OSError("injected no-follow rejection"),
            ) as no_follow:
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "cannot verify durable product source state",
                ):
                    source._read_state_unlocked()

            no_follow.assert_called_once_with(source.state_path)

    def test_oversized_candidate_does_not_prepare_monotonic_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider([]),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            current = source._read_state()
            candidate = dict(current)
            candidate["event_cache"] = {"unpublishable": {"payload": "x" * 256}}
            sealed = source._seal_state(candidate)

            with (
                patch.object(source, "_MAX_STATE_BYTES", 64),
                patch.object(source._authority, "prepare") as prepare,
            ):
                with self.assertRaisesRegex(
                    ProductSourceStateError,
                    "exceeds bounded checkpoint capacity",
                ):
                    source._publish_state_locked(
                        sealed,
                        observed_state_sha256=str(current["state_sha256"]),
                    )

            prepare.assert_not_called()

    def test_two_instances_cannot_last_writer_win_pending_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            second = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-2", odds="1.90", sequence=2)]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:03+00:00",
            )

            class _InterleavingProvider:
                source_id = _SOURCE_ID

                def __init__(self) -> None:
                    self.called = False

                def read_batch(self, max_items: int = 1000) -> ProviderBatch:
                    if self.called:
                        raise AssertionError("stale writer must not fetch twice")
                    self.called = True
                    second.fetch_catalog_page(None)
                    return _batch(cursor="snapshot-1")

            first = ParlayApiProductSource(
                _InterleavingProvider(),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            with self.assertRaisesRegex(
                ProductSourceStateError,
                "stale product source writer generation",
            ):
                first.fetch_catalog_page(None)

            replay = first.fetch_catalog_page(None)
            self.assertEqual(replay.cursor, "snapshot-2")

    def test_workspace_identity_prevents_silent_cross_workspace_state_sharing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = ParlayApiProductSource(
                _Provider([]),
                workspace=root / "workspace-a",
                authority_root=root / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            second = ParlayApiProductSource(
                _Provider([]),
                workspace=root / "workspace-b",
                authority_root=root / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            self.assertNotEqual(first.workspace_instance_id, second.workspace_instance_id)
            self.assertNotEqual(first.state_path, second.state_path)

    def test_rollback_to_older_valid_state_is_rejected_by_external_authority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory) / "workspace"
            authority_root = Path(directory) / "authority"
            source = ParlayApiProductSource(
                _Provider([_batch(cursor="snapshot-1")]),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-20T17:34:02+00:00",
            )
            initial_bytes = source.state_path.read_bytes()
            source.fetch_catalog_page(None)
            source.state_path.write_bytes(initial_bytes)

            with self.assertRaisesRegex(
                ProductSourceStateError,
                "stale, rolled back",
            ):
                ParlayApiProductSource(
                    _Provider([]),
                    workspace=workspace,
                    authority_root=authority_root,
                    lawful_terms_ref="terms:parlayapi:v1",
                    retention_ref="retention:parlayapi:v1",
                )

    def test_factory_requires_canonical_product_workspace_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AUTOSPORT_PARLAY_API_KEY": "test-only-api-key",
                "AUTOSPORT_PARLAY_LAWFUL_TERMS_REF": "terms:parlayapi:v1",
                "AUTOSPORT_PARLAY_RETENTION_REF": "retention:parlayapi:v1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ProductSourceError, "AUTOSPORT_PRODUCT_WORKSPACE"):
                create_parlay_product_source()

    def test_factory_requires_secret_and_operator_authority_environment(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "AUTOSPORT_PRODUCT_WORKSPACE": "workspace",
                "AUTOSPORT_PARLAY_LAWFUL_TERMS_REF": "terms:parlayapi:v1",
                "AUTOSPORT_PARLAY_RETENTION_REF": "retention:parlayapi:v1",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(ProductSourceError, "AUTOSPORT_PARLAY_API_KEY"):
                create_parlay_product_source()


    def test_factory_constructor_dependencies_are_import_composed(self) -> None:
        class AttackerProvider:
            source_id = _SOURCE_ID

            def __init__(self, *, api_key: str) -> None:
                self.api_key = api_key

            def read_batch(self, max_items: int = 1000):
                raise AssertionError("rebound provider constructor must not run")

        class AttackerSource:
            def __init__(self, provider, **kwargs) -> None:
                self.provider = provider
                self.kwargs = kwargs

        def forbidden_required_env(_name: str) -> str:
            raise AssertionError("rebound environment resolver must not run")

        canonical_factory = create_parlay_product_source
        with tempfile.TemporaryDirectory() as directory:
            workspace = str(Path(directory) / "workspace")
            with (
                patch.object(
                    product_source_module,
                    "ParlayApiTableTennisProvider",
                    AttackerProvider,
                ),
                patch.object(
                    product_source_module,
                    "ParlayApiProductSource",
                    AttackerSource,
                ),
                patch.object(
                    product_source_module,
                    "_required_env",
                    forbidden_required_env,
                ),
                patch.dict(
                    "os.environ",
                    {
                        "AUTOSPORT_PARLAY_API_KEY": "test-only-api-key",
                        "AUTOSPORT_PRODUCT_WORKSPACE": workspace,
                        "AUTOSPORT_PARLAY_LAWFUL_TERMS_REF": "terms:parlayapi:v1",
                        "AUTOSPORT_PARLAY_RETENTION_REF": "retention:parlayapi:v1",
                        "AUTOSPORT_MONOTONIC_AUTHORITY_ROOT": str(
                            Path(directory) / "monotonic-authority"
                        ),
                    },
                    clear=True,
                ),
            ):
                source = canonical_factory()

        self.assertIs(type(source), ParlayApiProductSource)
        self.assertIs(type(source.provider), ParlayApiTableTennisProvider)
        self.assertEqual(
            canonical_factory.__module__,
            "autosport.product_source",
        )
        self.assertEqual(
            canonical_factory.__name__,
            "create_parlay_product_source",
        )


if __name__ == "__main__":
    unittest.main()
