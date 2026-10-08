from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import unittest

from autosport.domain import MarketEvent, MarketType
from autosport.reference_price_evidence import (
    ReferenceCandidateCoverage,
    ReferenceCandidateDisposition,
    ReferenceCandidateNamespace,
    ReferenceObservation,
    ReferencePriceDecisionResolution,
    ReferencePriceEvidenceError,
    ReferencePriceProtocol,
    ReferencePriceResolutionState,
    ReferenceTargetInclusionPolicy,
    build_reference_price_evidence,
    resolve_reference_price_decision_evidence,
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


    def target_event(self, odds: str = "2.05") -> MarketEvent:
        return self.event(
            "target-transport",
            odds,
            bookmaker_key="target-provider",
            sequence=99,
        )

    def test_missing_frozen_sources_are_explicit_insufficient_not_numeric_consensus(
        self,
    ) -> None:
        protocol = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            minimum_sources=2,
        )
        resolution = resolve_reference_price_decision_evidence(
            (
                self.event("provider-b", "2.10", sequence=2),
                self.event("provider-a", "2.00", sequence=1),
            ),
            target_event=self.target_event(),
            decision_ts=self.DECISION,
            protocol=protocol,
        )

        self.assertIs(
            resolution.state,
            ReferencePriceResolutionState.INSUFFICIENT_REFERENCE_EVIDENCE,
        )
        self.assertIsNone(resolution.consensus_evidence)
        self.assertFalse(resolution.provider_origin_verified)
        self.assertFalse(resolution.executable_quote_verified)
        self.assertFalse(resolution.fair_probability_verified)
        self.assertFalse(resolution.fill_fidelity_verified)
        missing = {
            (candidate.namespace, candidate.source_id)
            for candidate in resolution.candidates
            if candidate.disposition
            is ReferenceCandidateDisposition.MISSING_REQUIRED_OBSERVATION
        }
        self.assertEqual(
            missing,
            {
                (ReferenceCandidateNamespace.TRANSPORT_SOURCE, "provider-c"),
                (ReferenceCandidateNamespace.PRICE_SOURCE, "provider-c"),
            },
        )
        payload = resolution.to_dict()
        self.assertEqual(
            payload["state"],
            "INSUFFICIENT_REFERENCE_EVIDENCE",
        )
        self.assertIsNone(payload["consensus_evidence"])
        self.assertNotIn("median_decimal_odds", payload)

    def test_all_reference_candidates_missing_remains_explicit_and_deterministic(
        self,
    ) -> None:
        protocol = self.protocol(("provider-a", "provider-b"))
        first = resolve_reference_price_decision_evidence(
            (),
            target_event=self.target_event(),
            decision_ts=self.DECISION,
            protocol=protocol,
        )
        second = resolve_reference_price_decision_evidence(
            (),
            target_event=self.target_event(),
            decision_ts=self.DECISION,
            protocol=protocol,
        )

        self.assertEqual(first.resolution_id, second.resolution_id)
        self.assertEqual(first.to_dict(), second.to_dict())
        self.assertEqual(
            {
                candidate.disposition
                for candidate in first.candidates
            },
            {ReferenceCandidateDisposition.MISSING_REQUIRED_OBSERVATION},
        )
        self.assertEqual(len(first.candidates), 4)

    def test_insufficient_resolution_is_permutation_stable_and_binds_target_bytes(
        self,
    ) -> None:
        protocol = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            minimum_sources=2,
        )
        a = self.event("provider-a", "2.00", sequence=1)
        b = self.event("provider-b", "2.10", sequence=2)
        forward = resolve_reference_price_decision_evidence(
            (a, b),
            target_event=self.target_event("2.05"),
            decision_ts=self.DECISION,
            protocol=protocol,
        )
        reverse = resolve_reference_price_decision_evidence(
            (b, a),
            target_event=self.target_event("2.05"),
            decision_ts=self.DECISION,
            protocol=protocol,
        )
        changed_target = resolve_reference_price_decision_evidence(
            (a, b),
            target_event=self.target_event("2.06"),
            decision_ts=self.DECISION,
            protocol=protocol,
        )

        self.assertEqual(forward.resolution_id, reverse.resolution_id)
        self.assertEqual(forward.to_dict(), reverse.to_dict())
        self.assertNotEqual(forward.resolution_id, changed_target.resolution_id)
        self.assertNotEqual(
            forward.target_event_sha256,
            changed_target.target_event_sha256,
        )

    def test_complete_resolution_delegates_to_existing_strict_consensus_builder(
        self,
    ) -> None:
        protocol = self.protocol(("provider-a", "provider-b"))
        events = (
            self.event("provider-a", "2.00", sequence=1),
            self.event("provider-b", "2.10", sequence=2),
        )
        direct = build_reference_price_evidence(
            events,
            decision_ts=self.DECISION,
            protocol=protocol,
        )
        resolution = resolve_reference_price_decision_evidence(
            events,
            target_event=self.target_event(),
            decision_ts=self.DECISION,
            protocol=protocol,
        )

        self.assertIs(
            resolution.state,
            ReferencePriceResolutionState.PREDECLARED_CONSENSUS_REFERENCE,
        )
        self.assertIsNotNone(resolution.consensus_evidence)
        assert resolution.consensus_evidence is not None
        self.assertEqual(resolution.consensus_evidence.evidence_id, direct.evidence_id)
        self.assertEqual(
            {
                candidate.disposition
                for candidate in resolution.candidates
            },
            {ReferenceCandidateDisposition.QUALIFIED_OBSERVATION},
        )

    def test_missing_coverage_cannot_hide_unexpected_or_duplicate_price_sources(
        self,
    ) -> None:
        protocol = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            minimum_sources=2,
        )
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "transport source is outside frozen eligible universe",
        ):
            resolve_reference_price_decision_evidence(
                (
                    self.event("provider-a", "2.00"),
                    self.event("provider-x", "2.10"),
                ),
                target_event=self.target_event(),
                decision_ts=self.DECISION,
                protocol=protocol,
            )

        aggregator_protocol = self.protocol(
            ("aggregator",),
            eligible_price_source_ids=("book-a", "book-b", "book-c"),
            minimum_sources=2,
        )
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "distinct independent price source_ids",
        ):
            resolve_reference_price_decision_evidence(
                (
                    self.event(
                        "aggregator",
                        "2.00",
                        bookmaker_key="book-a",
                        sequence=1,
                    ),
                    self.event(
                        "aggregator",
                        "2.10",
                        bookmaker_key="book-a",
                        sequence=2,
                    ),
                ),
                target_event=self.target_event(),
                decision_ts=self.DECISION,
                protocol=aggregator_protocol,
            )

    def test_resolution_target_price_source_must_match_frozen_protocol(self) -> None:
        protocol = self.protocol(("provider-a", "provider-b"))
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "target event price-source identity",
        ):
            resolve_reference_price_decision_evidence(
                (),
                target_event=self.event(
                    "other-target-transport",
                    "2.05",
                    bookmaker_key="other-target",
                ),
                decision_ts=self.DECISION,
                protocol=protocol,
            )

    def test_public_resolution_constructor_rejects_cross_market_candidate_bytes(
        self,
    ) -> None:
        protocol = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            minimum_sources=2,
        )
        legitimate = resolve_reference_price_decision_evidence(
            (
                self.event("provider-a", "2.00", sequence=1),
                self.event("provider-b", "2.10", sequence=2),
            ),
            target_event=self.target_event(),
            decision_ts=self.DECISION,
            protocol=protocol,
        )
        wrong_json = ReferenceObservation.from_event(
            self.event(
                "provider-a",
                "2.00",
                selection_id="other-selection",
            )
        ).event_canonical_json
        forged_candidates = tuple(
            (
                ReferenceCandidateCoverage(
                    namespace=candidate.namespace,
                    source_id=candidate.source_id,
                    disposition=candidate.disposition,
                    event_canonical_jsons=(wrong_json,),
                )
                if candidate.namespace
                is ReferenceCandidateNamespace.TRANSPORT_SOURCE
                and candidate.source_id == "provider-a"
                else candidate
            )
            for candidate in legitimate.candidates
        )

        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "candidate event market identity",
        ):
            ReferencePriceDecisionResolution(
                target_event_canonical_json=legitimate.target_event_canonical_json,
                protocol=protocol,
                decision_ts=self.DECISION,
                candidates=forged_candidates,
                state=ReferencePriceResolutionState.INSUFFICIENT_REFERENCE_EVIDENCE,
                consensus_evidence=None,
            )

    def test_insufficient_constructor_rejects_present_missing_namespace_contradiction(
        self,
    ) -> None:
        protocol = self.protocol(
            ("aggregator",),
            eligible_price_source_ids=("book-a", "book-b"),
            minimum_sources=2,
        )
        legitimate = resolve_reference_price_decision_evidence(
            (
                self.event(
                    "aggregator",
                    "2.00",
                    bookmaker_key="book-a",
                    sequence=1,
                ),
            ),
            target_event=self.target_event(),
            decision_ts=self.DECISION,
            protocol=protocol,
        )
        forged_candidates = tuple(
            (
                ReferenceCandidateCoverage(
                    namespace=candidate.namespace,
                    source_id=candidate.source_id,
                    disposition=(
                        ReferenceCandidateDisposition.MISSING_REQUIRED_OBSERVATION
                    ),
                    event_canonical_jsons=(),
                )
                if candidate.namespace is ReferenceCandidateNamespace.PRICE_SOURCE
                and candidate.source_id == "book-a"
                else candidate
            )
            for candidate in legitimate.candidates
        )

        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "candidate namespace projections must contain identical observation bytes",
        ):
            ReferencePriceDecisionResolution(
                target_event_canonical_json=legitimate.target_event_canonical_json,
                protocol=protocol,
                decision_ts=self.DECISION,
                candidates=forged_candidates,
                state=ReferencePriceResolutionState.INSUFFICIENT_REFERENCE_EVIDENCE,
                consensus_evidence=None,
            )

    def test_insufficient_constructor_rejects_alternate_dual_namespace_bytes(
        self,
    ) -> None:
        protocol = self.protocol(
            ("aggregator",),
            eligible_price_source_ids=("book-a", "book-b"),
            minimum_sources=2,
        )
        legitimate = resolve_reference_price_decision_evidence(
            (
                self.event(
                    "aggregator",
                    "2.00",
                    bookmaker_key="book-a",
                    sequence=1,
                ),
            ),
            target_event=self.target_event(),
            decision_ts=self.DECISION,
            protocol=protocol,
        )
        alternate_json = ReferenceObservation.from_event(
            self.event(
                "aggregator",
                "9.00",
                bookmaker_key="book-a",
                sequence=1,
            )
        ).event_canonical_json
        forged_candidates = tuple(
            (
                ReferenceCandidateCoverage(
                    namespace=candidate.namespace,
                    source_id=candidate.source_id,
                    disposition=ReferenceCandidateDisposition.PRESENT_UNQUALIFIED,
                    event_canonical_jsons=(alternate_json,),
                )
                if candidate.namespace is ReferenceCandidateNamespace.PRICE_SOURCE
                and candidate.source_id == "book-a"
                else candidate
            )
            for candidate in legitimate.candidates
        )

        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "candidate namespace projections must contain identical observation bytes",
        ):
            ReferencePriceDecisionResolution(
                target_event_canonical_json=legitimate.target_event_canonical_json,
                protocol=protocol,
                decision_ts=self.DECISION,
                candidates=forged_candidates,
                state=ReferencePriceResolutionState.INSUFFICIENT_REFERENCE_EVIDENCE,
                consensus_evidence=None,
            )

    def test_positive_resolution_constructor_binds_candidate_bytes_to_consensus(
        self,
    ) -> None:
        protocol = self.protocol(("provider-a", "provider-b"))
        legitimate = resolve_reference_price_decision_evidence(
            (
                self.event("provider-a", "2.00", sequence=1),
                self.event("provider-b", "2.10", sequence=2),
            ),
            target_event=self.target_event(),
            decision_ts=self.DECISION,
            protocol=protocol,
        )
        alternate_json = ReferenceObservation.from_event(
            self.event("provider-a", "9.00", sequence=1)
        ).event_canonical_json
        forged_candidates = tuple(
            (
                ReferenceCandidateCoverage(
                    namespace=candidate.namespace,
                    source_id=candidate.source_id,
                    disposition=candidate.disposition,
                    event_canonical_jsons=(alternate_json,),
                )
                if candidate.namespace
                is ReferenceCandidateNamespace.TRANSPORT_SOURCE
                and candidate.source_id == "provider-a"
                else candidate
            )
            for candidate in legitimate.candidates
        )

        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "qualified candidate bytes do not match consensus evidence",
        ):
            ReferencePriceDecisionResolution(
                target_event_canonical_json=legitimate.target_event_canonical_json,
                protocol=protocol,
                decision_ts=self.DECISION,
                candidates=forged_candidates,
                state=ReferencePriceResolutionState.PREDECLARED_CONSENSUS_REFERENCE,
                consensus_evidence=legitimate.consensus_evidence,
            )

    def test_missing_resolution_does_not_launder_incompatible_price_semantics(
        self,
    ) -> None:
        protocol = self.protocol(
            ("provider-a", "provider-b", "provider-c"),
            minimum_sources=2,
        )
        with self.assertRaisesRegex(
            ReferencePriceEvidenceError,
            "price_semantics does not match frozen protocol",
        ):
            resolve_reference_price_decision_evidence(
                (
                    self.event(
                        "provider-a",
                        "2.00",
                        price_semantics="last_traded_price",
                    ),
                    self.event("provider-b", "2.10"),
                ),
                target_event=self.target_event(),
                decision_ts=self.DECISION,
                protocol=protocol,
            )


if __name__ == "__main__":
    unittest.main()
