from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import unittest

from autosport.domain import MarketEvent, MarketType
from autosport.reference_price_evidence import (
    ReferencePriceEvidenceError,
    build_reference_price_evidence,
)


class ReferencePriceEvidenceTests(unittest.TestCase):
    DECISION = "2026-09-21T08:30:10+00:00"

    @staticmethod
    def event(
        source: str,
        odds: str,
        *,
        observed_ts: str = "2026-09-21T08:30:00+00:00",
        source_ts: str | None = "2026-09-21T08:29:59+00:00",
        ingest_ts: str = "2026-09-21T08:30:01+00:00",
        status: str = "open",
        event_id: str = "event-1",
        market_id: str = "market-1",
        selection_id: str = "selection-1",
        price_semantics: str = "best_available_to_back",
        execution_quote_verified: bool = False,
        sequence: int = 1,
    ) -> MarketEvent:
        return MarketEvent(
            event_id=event_id,
            market_id=market_id,
            selection_id=selection_id,
            decimal_odds=Decimal(odds),
            observed_ts=observed_ts,
            source_id=source,
            sequence=sequence,
            market_type=MarketType.WINNER,
            status=status,
            source_ts=source_ts,
            ingest_ts=ingest_ts,
            metadata={
                "price_semantics": price_semantics,
                "execution_quote_verified": execution_quote_verified,
            },
            sport="table_tennis",
            market_semantics_id="winner.match.v1",
        )

    def build(self, events, **kwargs):
        return build_reference_price_evidence(
            events,
            decision_ts=kwargs.pop("decision_ts", self.DECISION),
            max_age_seconds=kwargs.pop("max_age_seconds", 30),
            max_skew_seconds=kwargs.pop("max_skew_seconds", 5),
            **kwargs,
        )

    def test_builds_order_independent_exact_median_without_stronger_truth_claims(self) -> None:
        first = self.event("provider-a", "2.10")
        second = self.event("provider-b", "1.90")
        third = self.event("provider-c", "2.00")

        forward = self.build((first, second, third))
        reverse = self.build((third, second, first))

        self.assertEqual(forward, reverse)
        self.assertEqual(
            forward.source_ids,
            ("provider-a", "provider-b", "provider-c"),
        )
        self.assertEqual(forward.median_decimal_odds, Decimal("2.00"))
        self.assertEqual(forward.min_decimal_odds, Decimal("1.90"))
        self.assertEqual(forward.max_decimal_odds, Decimal("2.10"))
        self.assertEqual(forward.price_semantics, "best_available_to_back")
        self.assertFalse(forward.executable_quote_verified)
        self.assertFalse(forward.fair_probability_verified)
        self.assertFalse(forward.fill_fidelity_verified)
        self.assertEqual(len(forward.evidence_id), 64)

    def test_even_source_count_uses_exact_decimal_median(self) -> None:
        evidence = self.build(
            (
                self.event("provider-a", "1.90"),
                self.event("provider-b", "2.10"),
            )
        )

        self.assertEqual(evidence.median_decimal_odds, Decimal("2.00"))

    def test_exact_event_bytes_are_committed_into_evidence_identity(self) -> None:
        first = self.event("provider-a", "2.00")
        second = self.event("provider-b", "2.10")
        baseline = self.build((first, second))

        changed = replace(
            second,
            metadata={
                **second.metadata,
                "execution_quote_verified": True,
            },
        )
        revised = self.build((first, changed))

        self.assertNotEqual(baseline.evidence_id, revised.evidence_id)
        self.assertNotEqual(
            baseline.observations[1].event_sha256,
            revised.observations[1].event_sha256,
        )
        self.assertFalse(revised.executable_quote_verified)

    def test_duplicate_source_cannot_gain_consensus_weight(self) -> None:
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "distinct source_id"
        ):
            self.build(
                (
                    self.event("provider-a", "2.00", sequence=1),
                    self.event("provider-a", "2.10", sequence=2),
                )
            )

    def test_market_selection_identity_mismatch_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "one exact market selection"
        ):
            self.build(
                (
                    self.event("provider-a", "2.00"),
                    self.event(
                        "provider-b",
                        "2.10",
                        selection_id="selection-2",
                    ),
                )
            )

    def test_price_semantics_must_be_explicit_and_like_for_like(self) -> None:
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "cannot mix price semantics"
        ):
            self.build(
                (
                    self.event(
                        "provider-a",
                        "2.00",
                        price_semantics="last_traded_price",
                    ),
                    self.event(
                        "provider-b",
                        "2.10",
                        price_semantics="best_available_to_back",
                    ),
                )
            )

        missing = self.event("provider-b", "2.10")
        missing.metadata.pop("price_semantics")
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "explicit price_semantics"
        ):
            self.build((self.event("provider-a", "2.00"), missing))

    def test_suspended_or_closed_observation_is_not_reference_price_evidence(self) -> None:
        for status in ("suspended", "closed"):
            with self.subTest(status=status):
                with self.assertRaisesRegex(
                    ReferencePriceEvidenceError, "open market status"
                ):
                    self.build(
                        (
                            self.event("provider-a", "2.00"),
                            self.event("provider-b", "2.10", status=status),
                        )
                    )

    def test_future_and_stale_clocks_fail_closed(self) -> None:
        future = self.event(
            "provider-b",
            "2.10",
            observed_ts="2026-09-21T08:30:11+00:00",
            source_ts="2026-09-21T08:30:11+00:00",
            ingest_ts="2026-09-21T08:30:11+00:00",
        )
        with self.assertRaisesRegex(ReferencePriceEvidenceError, "future"):
            self.build((self.event("provider-a", "2.00"), future))

        stale = self.event(
            "provider-b",
            "2.10",
            observed_ts="2026-09-21T08:29:00+00:00",
            source_ts="2026-09-21T08:29:00+00:00",
            ingest_ts="2026-09-21T08:29:01+00:00",
        )
        with self.assertRaisesRegex(ReferencePriceEvidenceError, "stale"):
            self.build((self.event("provider-a", "2.00"), stale))

    def test_reingesting_old_source_timestamp_does_not_refresh_quote(self) -> None:
        old_source = self.event(
            "provider-b",
            "2.10",
            observed_ts="2026-09-21T08:30:00+00:00",
            source_ts="2026-09-21T08:20:00+00:00",
            ingest_ts="2026-09-21T08:30:01+00:00",
        )

        with self.assertRaisesRegex(ReferencePriceEvidenceError, "source_ts is stale"):
            self.build((self.event("provider-a", "2.00"), old_source))

    def test_source_and_receipt_skew_are_bounded_independently(self) -> None:
        source_skew = self.event(
            "provider-b",
            "2.10",
            observed_ts="2026-09-21T08:30:00+00:00",
            source_ts="2026-09-21T08:29:50+00:00",
            ingest_ts="2026-09-21T08:30:01+00:00",
        )
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "source observations exceed"
        ):
            self.build(
                (self.event("provider-a", "2.00"), source_skew),
                max_skew_seconds=5,
            )

        receipt_skew = self.event(
            "provider-b",
            "2.10",
            observed_ts="2026-09-21T08:30:00+00:00",
            source_ts="2026-09-21T08:29:59+00:00",
            ingest_ts="2026-09-21T08:30:08+00:00",
        )
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "receipt observations exceed"
        ):
            self.build(
                (self.event("provider-a", "2.00"), receipt_skew),
                max_skew_seconds=5,
            )

    def test_causal_clock_reversal_fails_closed(self) -> None:
        reversed_observation = self.event(
            "provider-b",
            "2.10",
            observed_ts="2026-09-21T08:30:02+00:00",
            source_ts="2026-09-21T08:29:59+00:00",
            ingest_ts="2026-09-21T08:30:01+00:00",
        )

        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "observed_ts cannot be after ingest_ts"
        ):
            self.build((self.event("provider-a", "2.00"), reversed_observation))

    def test_minimum_source_contract_cannot_be_weakened_below_two(self) -> None:
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "minimum_sources must be at least 2"
        ):
            self.build((self.event("provider-a", "2.00"),), minimum_sources=1)


if __name__ == "__main__":
    unittest.main()
