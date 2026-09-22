from __future__ import annotations

import hashlib
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch, sentinel

from autosport.domain import MarketType
from autosport.product_source import (
    ParlayApiProductSource,
    ProductSourcePayloadError,
    create_parlay_sport_product_source,
)
from autosport.providers import ProviderBatch, ProviderQuote


class _Provider:
    def __init__(self, sport_key: str, batches: list[ProviderBatch] | None = None) -> None:
        self.sport_key = sport_key
        self.source_id = f"parlayapi:{sport_key}"
        self.batches = list(batches or [])

    def read_batch(self, max_items: int = 1000) -> ProviderBatch:
        if max_items <= 0:
            raise AssertionError("source requested an invalid provider batch bound")
        if not self.batches:
            raise AssertionError("unexpected provider read")
        return self.batches.pop(0)


def _quote(
    *,
    sport_key: str,
    event_id: str = "event-1",
    sequence: int = 1,
) -> ProviderQuote:
    return ProviderQuote(
        provider_event_id=event_id,
        provider_market_id="book:h2h",
        provider_selection_id="selection-a",
        decimal_odds=Decimal("1.90"),
        observed_ts="2026-09-21T12:00:00+00:00",
        sequence=sequence,
        market_type=MarketType.WINNER,
        status="open",
        source_ts="2026-09-21T11:59:59+00:00",
        metadata={
            "provider": "parlayapi",
            "sport_key": sport_key,
            "commence_time": "2026-09-21T18:00:00+00:00",
            "home_team": "Alpha",
            "away_team": "Beta",
            "bookmaker_key": "book",
            "market_key": "h2h",
        },
        sport=sport_key,
    )


def _batch(*, source_sport: str, quote_sport: str | None = None) -> ProviderBatch:
    return ProviderBatch(
        source_id=f"parlayapi:{source_sport}",
        quotes=(_quote(sport_key=quote_sport or source_sport),),
        cursor=f"{source_sport}-snapshot-1",
    )


class MultiSportProductSourceTests(unittest.TestCase):
    def test_table_tennis_keeps_exact_legacy_stream_epoch(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider("table_tennis"),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )

            self.assertEqual(
                source.stream_epoch,
                "parlayapi-table-tennis-product-v1",
            )
            self.assertEqual(source.sport_key, "table_tennis")
            namespace = hashlib.sha256(
                b"parlayapi:table_tennis\0parlayapi-table-tennis-product-v1"
            ).hexdigest()
            self.assertEqual(
                source.state_dir,
                (Path(directory) / "workspace").expanduser().resolve(strict=False)
                / ".autosport"
                / "product-sources"
                / namespace,
            )

    def test_distinct_sports_get_distinct_durable_stream_and_state_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            authority_root = root / "authority"

            table_tennis = ParlayApiProductSource(
                _Provider("table_tennis"),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )
            basketball = ParlayApiProductSource(
                _Provider("basketball_nba"),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
            )

            self.assertEqual(
                basketball.stream_epoch,
                "parlayapi-basketball_nba-product-v1",
            )
            self.assertNotEqual(table_tennis.stream_epoch, basketball.stream_epoch)
            self.assertNotEqual(table_tennis.state_path, basketball.state_path)

    def test_basketball_provider_flows_through_product_source_with_sport_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider(
                    "basketball_nba",
                    [_batch(source_sport="basketball_nba")],
                ),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-21T12:00:01+00:00",
            )

            page = source.fetch_catalog_page(None)
            self.assertEqual(page.stream_epoch, "parlayapi-basketball_nba-product-v1")
            self.assertEqual(page.source_id, "parlayapi:basketball_nba")
            self.assertEqual(page.events[0].sport, "basketball_nba")

            delta = source.fetch_deltas(None, (), 1)[0]
            event = source.resolve_event(delta)
            self.assertEqual(delta.stream_epoch, page.stream_epoch)
            self.assertEqual(event.sport, "basketball_nba")

    def test_basketball_product_source_reopens_exact_durable_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            authority_root = root / "authority"
            source = ParlayApiProductSource(
                _Provider(
                    "basketball_nba",
                    [_batch(source_sport="basketball_nba")],
                ),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-21T12:00:01+00:00",
            )
            page = source.fetch_catalog_page(None)
            delta = source.fetch_deltas(None, (), 1)[0]
            event = source.resolve_event(delta)

            reopened = ParlayApiProductSource(
                _Provider("basketball_nba"),
                workspace=workspace,
                authority_root=authority_root,
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-21T12:00:02+00:00",
            )

            self.assertEqual(reopened.stream_epoch, page.stream_epoch)
            self.assertEqual(reopened.state_path, source.state_path)
            self.assertEqual(reopened.resolve_event(delta), event)

    def test_product_source_rejects_quote_sport_that_conflicts_with_source_scope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = ParlayApiProductSource(
                _Provider(
                    "basketball_nba",
                    [
                        _batch(
                            source_sport="basketball_nba",
                            quote_sport="table_tennis",
                        )
                    ],
                ),
                workspace=Path(directory) / "workspace",
                authority_root=Path(directory) / "authority",
                lawful_terms_ref="terms:parlayapi:v1",
                retention_ref="retention:parlayapi:v1",
                clock=lambda: "2026-09-21T12:00:01+00:00",
            )

            with self.assertRaisesRegex(
                ProductSourcePayloadError,
                "sport conflicts with product source identity",
            ):
                source.fetch_catalog_page(None)

    def test_generic_sport_factory_is_explicit_authenticated_composition(self) -> None:
        env = {
            "AUTOSPORT_PARLAY_API_KEY": "test-only-key",
            "AUTOSPORT_PRODUCT_WORKSPACE": r"C:\Autosport data\workspace",
            "AUTOSPORT_PARLAY_LAWFUL_TERMS_REF": "terms:parlayapi:v1",
            "AUTOSPORT_PARLAY_RETENTION_REF": "retention:parlayapi:v1",
        }
        with (
            patch.dict("os.environ", env, clear=True),
            patch(
                "autosport.product_source.ParlayApiSportProvider",
                return_value=sentinel.provider,
            ) as provider_type,
            patch(
                "autosport.product_source.ParlayApiProductSource",
                return_value=sentinel.source,
            ) as source_type,
        ):
            result = create_parlay_sport_product_source("basketball_nba")

        self.assertIs(result, sentinel.source)
        provider_type.assert_called_once_with(
            "basketball_nba",
            api_key="test-only-key",
        )
        source_type.assert_called_once_with(
            sentinel.provider,
            workspace=r"C:\Autosport data\workspace",
            lawful_terms_ref="terms:parlayapi:v1",
            retention_ref="retention:parlayapi:v1",
        )

    def test_generic_sport_factory_rejects_reserved_dataset_scope_identity(self) -> None:
        with self.assertRaisesRegex(ValueError, "canonical product sport identity"):
            create_parlay_sport_product_source("unknown")


if __name__ == "__main__":
    unittest.main()
