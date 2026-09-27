from __future__ import annotations

import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

import autosport.product_source as product_source_module
from autosport.causal_collector import StreamCheckpoint
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


def _quote(*, odds: str = "1.80", sequence: int = 1) -> ProviderQuote:
    return ProviderQuote(
        provider_event_id="event-1",
        provider_market_id="book:h2h",
        provider_selection_id="player-a",
        decimal_odds=Decimal(odds),
        observed_ts="2026-09-20T17:34:00+00:00",
        sequence=sequence,
        market_type=MarketType.WINNER,
        status="open",
        source_ts="2026-09-20T17:33:59+00:00",
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
    )


def _batch(*, cursor: str, odds: str = "1.80", sequence: int = 1) -> ProviderBatch:
    return ProviderBatch(
        source_id=_SOURCE_ID,
        quotes=(_quote(odds=odds, sequence=sequence),),
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


class ParlayApiProductSourceTests(unittest.TestCase):
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

    def test_missing_durable_event_cache_fails_closed(self) -> None:
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
            state_path = source.state_path
            raw = state_path.read_text(encoding="utf-8")
            state_path.write_text(
                raw.replace(f'"{delta.delta_id}":', '"missing-delta":', 1),
                encoding="utf-8",
            )
            with self.assertRaises(ProductSourceStateError):
                source.resolve_event(delta)

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
