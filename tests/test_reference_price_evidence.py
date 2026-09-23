from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import unittest

from autosport.domain import MarketEvent, MarketType
from autosport.reference_price_evidence import (
    ReferencePriceEvidenceError,
    ReferencePriceProtocol,
    ReferenceTargetInclusionPolicy,
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
        bookmaker_key: str | None = None,
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
                "bookmaker_key": source if bookmaker_key is None else bookmaker_key,
            },
            sport="table_tennis",
            market_semantics_id="winner.match.v1",
        )

    def protocol(
        self,
        source_ids,
        *,
        target_price_source_id: str = "target-provider",
        target_inclusion_policy: ReferenceTargetInclusionPolicy = (
            ReferenceTargetInclusionPolicy.EXCLUDE
        ),
        price_semantics: str = "best_available_to_back",
        max_age_seconds: int = 30,
        max_skew_seconds: int = 5,
        minimum_sources: int = 2,
        eligible_price_source_ids=None,
    ) -> ReferencePriceProtocol:
        return ReferencePriceProtocol(
            eligible_source_ids=tuple(source_ids),
            target_price_source_id=target_price_source_id,
            target_inclusion_policy=target_inclusion_policy,
            price_semantics=price_semantics,
            max_age_seconds=max_age_seconds,
            max_skew_seconds=max_skew_seconds,
            minimum_sources=minimum_sources,
            eligible_price_source_ids=(
                None
                if eligible_price_source_ids is None
                else tuple(eligible_price_source_ids)
            ),
        )

    def build(self, events, **kwargs):
        materialized = tuple(events)
        protocol = kwargs.pop("protocol", None)
        decision_ts = kwargs.pop("decision_ts", self.DECISION)
        if protocol is None:
            eligible_source_ids = kwargs.pop(
                "eligible_source_ids",
                tuple(sorted({event.source_id for event in materialized})),
            )
            eligible_price_source_ids = kwargs.pop(
                "eligible_price_source_ids",
                tuple(
                    sorted(
                        {
                            event.metadata.get("bookmaker_key", event.source_id)
                            for event in materialized
                        }
                    )
                ),
            )
            protocol = self.protocol(
                eligible_source_ids,
                target_price_source_id=kwargs.pop(
                    "target_price_source_id",
                    "target-provider",
                ),
                target_inclusion_policy=kwargs.pop(
                    "target_inclusion_policy",
                    ReferenceTargetInclusionPolicy.EXCLUDE,
                ),
                price_semantics=kwargs.pop(
                    "protocol_price_semantics",
                    "best_available_to_back",
                ),
                max_age_seconds=kwargs.pop("max_age_seconds", 30),
                max_skew_seconds=kwargs.pop("max_skew_seconds", 5),
                minimum_sources=kwargs.pop("minimum_sources", 2),
                eligible_price_source_ids=eligible_price_source_ids,
            )
        if kwargs:
            raise AssertionError(f"unexpected test helper kwargs: {kwargs!r}")
        return build_reference_price_evidence(
            materialized,
            decision_ts=decision_ts,
            protocol=protocol,
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
        self.assertEqual(forward.lower_median_decimal_odds, Decimal("2.00"))
        self.assertEqual(forward.upper_median_decimal_odds, Decimal("2.00"))
        self.assertEqual(forward.min_decimal_odds, Decimal("1.90"))
        self.assertEqual(forward.max_decimal_odds, Decimal("2.10"))
        self.assertEqual(forward.price_semantics, "best_available_to_back")
        self.assertFalse(forward.executable_quote_verified)
        self.assertFalse(forward.fair_probability_verified)
        self.assertFalse(forward.fill_fidelity_verified)
        self.assertEqual(len(forward.evidence_id), 64)

    def test_even_source_count_preserves_observed_median_band(self) -> None:
        evidence = self.build(
            (
                self.event("provider-a", "1.90"),
                self.event("provider-b", "2.10"),
            )
        )

        self.assertIsNone(evidence.median_decimal_odds)
        self.assertEqual(evidence.lower_median_decimal_odds, Decimal("1.90"))
        self.assertEqual(evidence.upper_median_decimal_odds, Decimal("2.10"))

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

    def test_observational_truth_flags_cannot_be_caller_promoted(self) -> None:
        evidence = self.build(
            (
                self.event("provider-a", "2.00", execution_quote_verified=True),
                self.event("provider-b", "2.10", execution_quote_verified=True),
            )
        )

        for field_name in (
            "executable_quote_verified",
            "fair_probability_verified",
            "fill_fidelity_verified",
        ):
            with self.subTest(field_name=field_name):
                with self.assertRaises(ValueError):
                    replace(evidence, **{field_name: True})

    def test_duplicate_source_cannot_gain_consensus_weight(self) -> None:
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError, "distinct independent price source_ids"
        ):
            self.build(
                (
                    self.event("provider-a", "2.00", sequence=1),
                    self.event("provider-a", "2.10", sequence=2),
                ),
                eligible_source_ids=("provider-a", "provider-b"),
                eligible_price_source_ids=("provider-a", "provider-b"),
            )

    def test_same_bookmaker_through_two_aggregators_cannot_double_count(self) -> None:
        protocol = self.protocol(
            ("parlayapi:football", "the-odds-api:football"),
            eligible_price_source_ids=("pinnacle", "draftkings"),
            minimum_sources=2,
        )
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "distinct independent price source_ids",
        ):
            self.build(
                (
                    self.event(
                        "parlayapi:football",
                        "2.00",
                        bookmaker_key="pinnacle",
                    ),
                    self.event(
                        "the-odds-api:football",
                        "2.02",
                        bookmaker_key="pinnacle",
                    ),
                ),
                protocol=protocol,
            )

    def test_two_bookmakers_from_one_aggregator_are_independent_when_bound(self) -> None:
        protocol = self.protocol(
            ("parlayapi:football",),
            eligible_price_source_ids=("draftkings", "pinnacle"),
            minimum_sources=2,
        )
        evidence = self.build(
            (
                self.event(
                    "parlayapi:football",
                    "2.00",
                    bookmaker_key="pinnacle",
                    sequence=1,
                ),
                self.event(
                    "parlayapi:football",
                    "2.04",
                    bookmaker_key="draftkings",
                    sequence=2,
                ),
            ),
            protocol=protocol,
        )

        self.assertEqual(set(evidence.source_ids), {"parlayapi:football"})
        self.assertEqual(set(evidence.price_source_ids), {"pinnacle", "draftkings"})
        self.assertIsNone(evidence.median_decimal_odds)
        self.assertEqual(evidence.lower_median_decimal_odds, Decimal("2.00"))
        self.assertEqual(evidence.upper_median_decimal_odds, Decimal("2.04"))

    def test_missing_underlying_price_source_identity_fails_closed(self) -> None:
        first = self.event("provider-a", "2.00")
        second = self.event("provider-b", "2.10")
        second = replace(
            second,
            metadata={
                key: value
                for key, value in second.metadata.items()
                if key != "bookmaker_key"
            },
        )
        protocol = self.protocol(
            ("provider-a", "provider-b"),
            eligible_price_source_ids=("provider-a", "provider-b"),
        )
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "metadata.bookmaker_key",
        ):
            self.build((first, second), protocol=protocol)

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
                ),
                protocol_price_semantics="last_traded_price",
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


    def test_frozen_provider_universe_rejects_provider_shopping(self) -> None:
        protocol = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            minimum_sources=2,
        )

        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "exactly cover frozen eligible transport-source universe",
        ):
            self.build(
                (
                    self.event("provider-a", "2.00"),
                    self.event("provider-b", "2.10"),
                ),
                protocol=protocol,
            )

        complete = self.build(
            (
                self.event("provider-a", "2.00"),
                self.event("provider-b", "2.10"),
                self.event("provider-c", "1.95"),
            ),
            protocol=protocol,
        )
        self.assertEqual(
            complete.source_ids,
            ("provider-a", "provider-b", "provider-c"),
        )
        self.assertEqual(complete.protocol_id, protocol.protocol_id)

    def test_frozen_provider_universe_rejects_unexpected_source(self) -> None:
        protocol = self.protocol(("provider-a", "provider-b"))

        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "exactly cover frozen eligible transport-source universe",
        ):
            self.build(
                (
                    self.event("provider-a", "2.00"),
                    self.event("provider-b", "2.10"),
                    self.event("provider-c", "9.00"),
                ),
                protocol=protocol,
            )

    def test_target_inclusion_policy_is_structural_and_explicit(self) -> None:
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "excluded target_price_source_id",
        ):
            self.protocol(
                ("provider-a", "provider-b"),
                target_price_source_id="provider-a",
                target_inclusion_policy=ReferenceTargetInclusionPolicy.EXCLUDE,
            )

        included = self.protocol(
            ("provider-a", "provider-b"),
            target_price_source_id="provider-a",
            target_inclusion_policy=ReferenceTargetInclusionPolicy.INCLUDE,
        )
        evidence = self.build(
            (
                self.event("provider-a", "2.00"),
                self.event("provider-b", "2.10"),
            ),
            protocol=included,
        )
        self.assertIs(
            evidence.protocol.target_inclusion_policy,
            ReferenceTargetInclusionPolicy.INCLUDE,
        )
        self.assertIn("provider-a", evidence.source_ids)

    def test_target_policy_uses_underlying_price_source_identity(self) -> None:
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "excluded target_price_source_id",
        ):
            self.protocol(
                ("the-odds-api:football",),
                target_price_source_id="pinnacle",
                target_inclusion_policy=ReferenceTargetInclusionPolicy.EXCLUDE,
                eligible_price_source_ids=("draftkings", "pinnacle"),
                minimum_sources=2,
            )

        included = self.protocol(
            ("the-odds-api:football",),
            target_price_source_id="pinnacle",
            target_inclusion_policy=ReferenceTargetInclusionPolicy.INCLUDE,
            eligible_price_source_ids=("draftkings", "pinnacle"),
            minimum_sources=2,
        )
        evidence = self.build(
            (
                self.event(
                    "the-odds-api:football",
                    "2.00",
                    bookmaker_key="pinnacle",
                    sequence=1,
                ),
                self.event(
                    "the-odds-api:football",
                    "2.04",
                    bookmaker_key="draftkings",
                    sequence=2,
                ),
            ),
            protocol=included,
        )
        self.assertEqual(included.target_price_source_id, "pinnacle")
        self.assertIn("pinnacle", evidence.price_source_ids)
        self.assertEqual(set(evidence.source_ids), {"the-odds-api:football"})

    def test_excluded_target_cannot_enter_evidence_as_unexpected_source(self) -> None:
        protocol = self.protocol(
            ("provider-b", "provider-c"),
            target_price_source_id="provider-a",
            target_inclusion_policy=ReferenceTargetInclusionPolicy.EXCLUDE,
        )

        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "exactly cover frozen eligible transport-source universe",
        ):
            self.build(
                (
                    self.event("provider-a", "2.50"),
                    self.event("provider-b", "2.00"),
                    self.event("provider-c", "2.10"),
                ),
                protocol=protocol,
            )

    def test_protocol_identity_changes_when_policy_changes(self) -> None:
        baseline = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            max_age_seconds=30,
            minimum_sources=2,
        )
        wider_freshness = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            max_age_seconds=31,
            minimum_sources=2,
        )
        stronger_coverage = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            max_age_seconds=30,
            minimum_sources=3,
        )

        self.assertNotEqual(baseline.protocol_id, wider_freshness.protocol_id)
        self.assertNotEqual(baseline.protocol_id, stronger_coverage.protocol_id)
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "unsupported reference aggregation_method",
        ):
            ReferencePriceProtocol(
                eligible_source_ids=("provider-a", "provider-b"),
                target_price_source_id="target-provider",
                target_inclusion_policy=ReferenceTargetInclusionPolicy.EXCLUDE,
                price_semantics="best_available_to_back",
                max_age_seconds=30,
                max_skew_seconds=5,
                minimum_sources=2,
                aggregation_method="best_price_after_inspection",
            )

    def test_evidence_identity_binds_frozen_protocol(self) -> None:
        events = (
            self.event("provider-a", "2.00"),
            self.event("provider-b", "2.10"),
        )
        baseline = self.build(
            events,
            protocol=self.protocol(
                ("provider-a", "provider-b"),
                max_age_seconds=30,
            ),
        )
        different_policy = self.build(
            events,
            protocol=self.protocol(
                ("provider-a", "provider-b"),
                max_age_seconds=31,
            ),
        )

        self.assertNotEqual(baseline.protocol_id, different_policy.protocol_id)
        self.assertNotEqual(baseline.evidence_id, different_policy.evidence_id)

    def test_legacy_per_call_policy_knobs_cannot_mint_evidence(self) -> None:
        events = (
            self.event("provider-a", "2.00"),
            self.event("provider-b", "2.10"),
        )
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "frozen ReferencePriceProtocol is required",
        ):
            build_reference_price_evidence(
                events,
                decision_ts=self.DECISION,
                max_age_seconds=30,
                max_skew_seconds=5,
                minimum_sources=2,
            )

        protocol = self.protocol(("provider-a", "provider-b"))
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "per-call reference policy overrides are forbidden",
        ):
            build_reference_price_evidence(
                events,
                decision_ts=self.DECISION,
                protocol=protocol,
                max_age_seconds=60,
            )


if __name__ == "__main__":
    unittest.main()
