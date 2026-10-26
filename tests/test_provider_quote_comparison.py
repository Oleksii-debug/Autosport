from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import unittest

from autosport.domain import MarketEvent
from autosport.market_mirror import MirrorSnapshot
from autosport.provider_quote_comparison import (
    ProviderQuoteComparison,
    ProviderQuoteComparisonError,
    ProviderQuotePoint,
    compare_provider_quotes,
)


AS_OF = datetime(2026, 9, 21, 13, 0, tzinfo=timezone.utc)


class ProviderQuoteComparisonTests(unittest.TestCase):
    @staticmethod
    def event(
        *,
        source: str,
        odds: str,
        sequence: int = 1,
        sport: str | None = "soccer",
        event_id: str = "event-1",
        market_id: str = "match-odds",
        selection_id: str = "home",
        competition_id: str | None = "league-1",
        market_semantics_id: str | None = "match-winner-3way-v1",
        status: str = "open",
        observed_ts: str = "2026-09-21T12:59:30+00:00",
        source_ts: str | None = "2026-09-21T12:59:25+00:00",
        ingest_ts: str = "2026-09-21T12:59:31+00:00",
        provider_source_class: str | None = "official-api",
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id=source,
            sequence=sequence,
            status=status,
            source_ts=source_ts,
            ingest_ts=ingest_ts,
            sport=sport,
            competition_id=competition_id,
            market_semantics_id=market_semantics_id,
            provider_source_class=provider_source_class,
        )

    def compare(self, *events: MarketEvent, revision: int = 7):
        return compare_provider_quotes(
            MirrorSnapshot(revision=revision, events=tuple(events)),
            as_of=AS_OF,
            max_age=timedelta(minutes=2),
        )

    def test_compares_same_semantic_quote_across_distinct_providers(self) -> None:
        result = self.compare(
            self.event(source="provider-a", odds="2.05"),
            self.event(source="provider-b", odds="2.10"),
            self.event(source="provider-c", odds="2.10"),
        )

        self.assertEqual(len(result), 1)
        comparison = result[0]
        self.assertEqual(comparison.provider_count, 3)
        self.assertEqual(comparison.best_decimal_odds, Decimal("2.10"))
        self.assertEqual(comparison.worst_decimal_odds, Decimal("2.05"))
        self.assertEqual(comparison.displayed_spread, Decimal("0.05"))
        self.assertEqual(
            comparison.best_source_ids,
            ("provider-b", "provider-c"),
        )
        self.assertFalse(
            comparison.to_payload()["claims"]["provider_universe_complete"]
        )
        self.assertFalse(comparison.to_payload()["claims"]["execution_feasible"])
        self.assertFalse(comparison.to_payload()["claims"]["arbitrage_proven"])

    def test_result_is_deterministic_under_input_order(self) -> None:
        a = self.event(source="provider-a", odds="2.01")
        b = self.event(source="provider-b", odds="2.11")

        forward = self.compare(a, b)[0]
        reverse = self.compare(b, a)[0]

        self.assertEqual(forward.to_payload(), reverse.to_payload())
        self.assertEqual(forward.comparison_sha256, reverse.comparison_sha256)
        self.assertEqual(
            tuple(quote.source_id for quote in forward.quotes),
            ("provider-a", "provider-b"),
        )

    def test_exact_decimal_spread_never_uses_float_arithmetic(self) -> None:
        comparison = self.compare(
            self.event(source="provider-a", odds="1.91"),
            self.event(source="provider-b", odds="2.02"),
        )[0]

        self.assertIsInstance(comparison.displayed_spread, Decimal)
        self.assertEqual(comparison.displayed_spread, Decimal("0.11"))

    def test_different_sports_are_not_cross_compared(self) -> None:
        result = self.compare(
            self.event(source="provider-a", odds="2.00", sport="soccer"),
            self.event(source="provider-b", odds="2.10", sport="table_tennis"),
        )
        self.assertEqual(result, ())

    def test_different_market_semantics_are_not_cross_compared(self) -> None:
        result = self.compare(
            self.event(
                source="provider-a",
                odds="2.00",
                market_semantics_id="match-winner-3way-v1",
            ),
            self.event(
                source="provider-b",
                odds="2.10",
                market_semantics_id="draw-no-bet-v1",
            ),
        )
        self.assertEqual(result, ())

    def test_missing_semantic_identity_is_not_guessed(self) -> None:
        result = self.compare(
            self.event(
                source="provider-a",
                odds="2.00",
                market_semantics_id=None,
            ),
            self.event(
                source="provider-b",
                odds="2.10",
                market_semantics_id=None,
            ),
        )
        self.assertEqual(result, ())

    def test_suspended_stale_and_future_quotes_are_excluded(self) -> None:
        valid_a = self.event(source="provider-a", odds="2.00")
        valid_b = self.event(source="provider-b", odds="2.10")
        suspended = self.event(
            source="provider-c",
            odds="9.99",
            status="suspended",
        )
        stale = self.event(
            source="provider-d",
            odds="9.99",
            source_ts="2026-09-21T12:50:00+00:00",
        )
        future = self.event(
            source="provider-e",
            odds="9.99",
            source_ts="2026-09-21T13:00:01+00:00",
            observed_ts="2026-09-21T13:00:01+00:00",
            ingest_ts="2026-09-21T13:00:01+00:00",
        )

        comparison = self.compare(valid_a, valid_b, suspended, stale, future)[0]
        self.assertEqual(
            tuple(quote.source_id for quote in comparison.quotes),
            ("provider-a", "provider-b"),
        )
        self.assertEqual(comparison.best_decimal_odds, Decimal("2.10"))

    def test_source_time_is_preferred_for_freshness(self) -> None:
        result = self.compare(
            self.event(
                source="provider-a",
                odds="2.00",
                observed_ts="2026-09-21T12:59:50+00:00",
                source_ts="2026-09-21T12:40:00+00:00",
                ingest_ts="2026-09-21T12:59:51+00:00",
            ),
            self.event(source="provider-b", odds="2.10"),
        )
        self.assertEqual(result, ())

    def test_missing_provider_source_time_is_not_promoted_from_receipt_time(self) -> None:
        result = self.compare(
            self.event(
                source="provider-a",
                odds="2.00",
                source_ts=None,
                observed_ts="2026-09-21T12:59:50+00:00",
                ingest_ts="2026-09-21T12:59:51+00:00",
            ),
            self.event(source="provider-b", odds="2.10"),
        )
        self.assertEqual(result, ())

    def test_future_local_observation_or_ingest_cannot_enter_comparison(self) -> None:
        valid = self.event(source="provider-a", odds="2.00")
        future_observed = self.event(
            source="provider-b",
            odds="2.10",
            observed_ts="2026-09-21T13:00:01+00:00",
            source_ts="2026-09-21T12:59:59+00:00",
            ingest_ts="2026-09-21T13:00:02+00:00",
        )
        future_ingest = self.event(
            source="provider-c",
            odds="2.20",
            ingest_ts="2026-09-21T13:00:01+00:00",
        )

        self.assertEqual(self.compare(valid, future_observed, future_ingest), ())

    def test_duplicate_source_authority_for_same_semantic_quote_fails_closed(self) -> None:
        snapshot = MirrorSnapshot(
            revision=4,
            events=(
                self.event(source="provider-a", odds="2.00", sequence=1),
                self.event(source="provider-a", odds="2.10", sequence=2),
                self.event(source="provider-b", odds="2.20", sequence=1),
            ),
        )
        with self.assertRaisesRegex(
            ProviderQuoteComparisonError,
            "duplicate source authority",
        ):
            compare_provider_quotes(
                snapshot,
                as_of=AS_OF,
                max_age=timedelta(minutes=2),
            )

    def test_snapshot_hash_binds_revision_and_all_snapshot_content(self) -> None:
        base = self.compare(
            self.event(source="provider-a", odds="2.00"),
            self.event(source="provider-b", odds="2.10"),
            revision=8,
        )[0]
        changed_quote = self.compare(
            self.event(source="provider-a", odds="2.00"),
            self.event(source="provider-b", odds="2.11"),
            revision=8,
        )[0]
        changed_revision = self.compare(
            self.event(source="provider-a", odds="2.00"),
            self.event(source="provider-b", odds="2.10"),
            revision=9,
        )[0]

        self.assertNotEqual(
            base.mirror_snapshot_sha256,
            changed_quote.mirror_snapshot_sha256,
        )
        self.assertNotEqual(
            base.mirror_snapshot_sha256,
            changed_revision.mirror_snapshot_sha256,
        )

    def test_provider_source_class_is_evidence_not_comparison_identity(self) -> None:
        comparison = self.compare(
            self.event(
                source="provider-a",
                odds="2.00",
                provider_source_class="official-api",
            ),
            self.event(
                source="provider-b",
                odds="2.10",
                provider_source_class="licensed-feed",
            ),
        )[0]

        classes = {
            quote.source_id: quote.provider_source_class
            for quote in comparison.quotes
        }
        self.assertEqual(
            classes,
            {
                "provider-a": "official-api",
                "provider-b": "licensed-feed",
            },
        )

    def test_direct_point_construction_rejects_invalid_evidence_fields(self) -> None:
        comparison = self.compare(
            self.event(source="provider-a", odds="2.00"),
            self.event(source="provider-b", odds="2.10"),
        )[0]
        point = comparison.quotes[0]

        with self.assertRaises(ProviderQuoteComparisonError):
            replace(point, source_id=" provider-a")
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(point, sequence=True)
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(point, decimal_odds=Decimal("NaN"))
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(point, observed_ts="not-a-time")
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(point, source_ts=None)
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(point, market_event_sha256="caller-minted")

    def test_direct_comparison_construction_rejects_noncanonical_container(self) -> None:
        comparison = self.compare(
            self.event(source="provider-a", odds="2.00"),
            self.event(source="provider-b", odds="2.10"),
        )[0]

        with self.assertRaises(ProviderQuoteComparisonError):
            replace(comparison, mirror_revision=True)
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(comparison, mirror_snapshot_sha256="caller-minted")
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(comparison, quotes=(comparison.quotes[0],))
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(
                comparison,
                quotes=(comparison.quotes[1], comparison.quotes[0]),
            )
        with self.assertRaises(ProviderQuoteComparisonError):
            replace(
                comparison,
                quotes=(comparison.quotes[0], comparison.quotes[0]),
            )

        self.assertIsInstance(comparison, ProviderQuoteComparison)
        self.assertTrue(
            all(type(point) is ProviderQuotePoint for point in comparison.quotes)
        )

    def test_minimum_sources_and_boundary_inputs_are_fail_closed(self) -> None:
        snapshot = MirrorSnapshot(
            revision=1,
            events=(self.event(source="provider-a", odds="2.00"),),
        )
        with self.assertRaises(ProviderQuoteComparisonError):
            compare_provider_quotes(
                snapshot,
                as_of=AS_OF,
                max_age=timedelta(minutes=1),
                minimum_sources=1,
            )
        with self.assertRaises(ProviderQuoteComparisonError):
            compare_provider_quotes(
                snapshot,
                as_of=datetime(2026, 9, 21, 13, 0),
                max_age=timedelta(minutes=1),
            )
        with self.assertRaises(ProviderQuoteComparisonError):
            compare_provider_quotes(
                snapshot,
                as_of=AS_OF,
                max_age=timedelta(seconds=-1),
            )


if __name__ == "__main__":
    unittest.main()
