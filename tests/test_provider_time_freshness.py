from __future__ import annotations

import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from autosport.provider_time_freshness import (
    ProviderTimeEvidence,
    ProviderTimeStatus,
    assess_provider_time_freshness,
)




class _LyingDuration(timedelta):
    def __lt__(self, other: object) -> bool:
        return False


class _LyingDecision(datetime):
    def __sub__(self, other: object) -> timedelta:
        return timedelta(0)



class ProviderTimeFreshnessTests(unittest.TestCase):
    @staticmethod
    def evidence(**changes: object) -> ProviderTimeEvidence:
        values = {
            "source_updated_at": "2026-09-21T12:00:00+00:00",
            "received_wall_at": "2026-09-21T12:00:00.250000+00:00",
            "acquisition_started_monotonic_ns": 1_000_000_000,
            "received_monotonic_ns": 1_250_000_000,
            "sequence_id": 17,
        }
        values.update(changes)
        return ProviderTimeEvidence(**values)

    def assess(self, evidence: ProviderTimeEvidence, **changes: object):
        values = {
            "decision_at": datetime(2026, 9, 21, 12, 0, 1, tzinfo=timezone.utc),
            "max_quote_age": timedelta(seconds=2),
            "max_source_clock_skew": timedelta(milliseconds=100),
        }
        values.update(changes)
        return assess_provider_time_freshness(evidence, **values)

    def test_fresh_evidence_binds_all_time_dimensions_and_sequence(self) -> None:
        evidence = self.evidence()
        result = self.assess(evidence)

        self.assertEqual(result.status, ProviderTimeStatus.FRESH)
        self.assertFalse(hasattr(result, "eligible"))
        self.assertEqual(result.sequence_id, 17)
        self.assertEqual(result.transport_elapsed_ns, 250_000_000)
        self.assertEqual(result.source_to_receive_delay, timedelta(milliseconds=250))
        self.assertEqual(result.source_clock_skew, timedelta(0))
        self.assertEqual(result.quote_age, timedelta(seconds=1))
        self.assertEqual(result.evidence_id, evidence.evidence_id)

    def test_quote_age_boundary_is_inclusive_and_then_stale(self) -> None:
        evidence = self.evidence()
        exact = self.assess(
            evidence,
            decision_at=datetime(2026, 9, 21, 12, 0, 2, tzinfo=timezone.utc),
            max_quote_age=timedelta(seconds=2),
        )
        late = self.assess(
            evidence,
            decision_at=datetime(
                2026, 9, 21, 12, 0, 2, 1, tzinfo=timezone.utc
            ),
            max_quote_age=timedelta(seconds=2),
        )

        self.assertEqual(exact.status, ProviderTimeStatus.FRESH)
        self.assertEqual(late.status, ProviderTimeStatus.STALE)

    def test_negative_wall_latency_fails_even_inside_skew_diagnostic_boundary(self) -> None:
        evidence = self.evidence(
            source_updated_at="2026-09-21T12:00:00.300000+00:00",
            received_wall_at="2026-09-21T12:00:00.250000+00:00",
        )
        result = self.assess(
            evidence,
            max_source_clock_skew=timedelta(milliseconds=100),
        )

        self.assertEqual(result.status, ProviderTimeStatus.NEGATIVE_WALL_LATENCY)
        self.assertEqual(result.source_to_receive_delay, timedelta(milliseconds=-50))
        self.assertEqual(result.source_clock_skew, timedelta(milliseconds=50))

    def test_clock_skew_over_explicit_boundary_fails_without_clock_correction(self) -> None:
        evidence = self.evidence(
            source_updated_at="2026-09-21T12:00:00.500000+00:00",
            received_wall_at="2026-09-21T12:00:00.250000+00:00",
        )
        result = self.assess(
            evidence,
            max_source_clock_skew=timedelta(milliseconds=100),
        )

        self.assertEqual(result.status, ProviderTimeStatus.CLOCK_SKEW_EXCEEDED)
        self.assertEqual(result.source_to_receive_delay, timedelta(milliseconds=-250))
        self.assertEqual(result.source_clock_skew, timedelta(milliseconds=250))
        self.assertEqual(result.quote_age, timedelta(milliseconds=500))

    def test_negative_monotonic_latency_is_diagnostic_failure(self) -> None:
        evidence = self.evidence(
            acquisition_started_monotonic_ns=2_000,
            received_monotonic_ns=1_999,
        )
        result = self.assess(evidence)

        self.assertEqual(result.status, ProviderTimeStatus.NEGATIVE_MONOTONIC_LATENCY)
        self.assertEqual(result.transport_elapsed_ns, -1)

    def test_future_receipt_cannot_be_used_for_earlier_decision(self) -> None:
        evidence = self.evidence(
            source_updated_at="2026-09-21T12:00:00+00:00",
            received_wall_at="2026-09-21T12:00:02+00:00",
        )
        result = self.assess(
            evidence,
            decision_at=datetime(2026, 9, 21, 12, 0, 1, tzinfo=timezone.utc),
        )

        self.assertEqual(result.status, ProviderTimeStatus.FUTURE_RECEIPT)

    def test_timezone_equivalent_instants_compare_semantically_but_keep_exact_identity(self) -> None:
        utc_evidence = self.evidence()
        offset_evidence = replace(
            utc_evidence,
            source_updated_at="2026-09-21T14:00:00+02:00",
            received_wall_at="2026-09-21T14:00:00.250000+02:00",
        )

        utc_result = self.assess(utc_evidence)
        offset_result = self.assess(offset_evidence)

        self.assertEqual(utc_result.status, offset_result.status)
        self.assertEqual(utc_result.quote_age, offset_result.quote_age)
        self.assertEqual(
            utc_result.source_to_receive_delay,
            offset_result.source_to_receive_delay,
        )
        self.assertNotEqual(utc_result.evidence_id, offset_result.evidence_id)

    def test_opaque_sequence_identity_round_trips_without_numeric_surrogate(self) -> None:
        opaque = self.evidence(sequence_id="opaque-clk-token")
        result = self.assess(opaque)

        self.assertEqual(result.sequence_id, "opaque-clk-token")
        self.assertEqual(opaque.sequence_id, "opaque-clk-token")
        self.assertNotEqual(opaque.evidence_id, self.evidence(sequence_id=17).evidence_id)
        self.assertNotEqual(
            self.evidence(sequence_id=1).evidence_id,
            self.evidence(sequence_id="1").evidence_id,
        )

    def test_opaque_sequence_identity_is_bounded_and_canonical_text(self) -> None:
        for value in ("", " leading", "trailing ", "x" * 513):
            with self.subTest(value=value[:20]), self.assertRaises(ValueError):
                self.evidence(sequence_id=value)

    def test_evidence_identity_changes_for_each_bound_dimension(self) -> None:
        base = self.evidence()
        variants = (
            replace(base, source_updated_at="2026-09-21T11:59:59+00:00"),
            replace(base, received_wall_at="2026-09-21T12:00:00.251000+00:00"),
            replace(base, acquisition_started_monotonic_ns=999_999_999),
            replace(base, received_monotonic_ns=1_250_000_001),
            replace(base, sequence_id=18),
        )

        self.assertEqual(len({base.evidence_id, *(v.evidence_id for v in variants)}), 6)

    def test_rejects_naive_or_malformed_timestamps(self) -> None:
        for field, value in (
            ("source_updated_at", "2026-09-21T12:00:00"),
            ("received_wall_at", "not-a-time"),
        ):
            with self.subTest(field=field), self.assertRaises(ValueError):
                self.evidence(**{field: value})

    def test_rejects_invalid_monotonic_and_sequence_types_or_ranges(self) -> None:
        cases = (
            ("acquisition_started_monotonic_ns", True),
            ("received_monotonic_ns", -1),
            ("sequence_id", True),
            ("sequence_id", 1 << 63),
            ("sequence_id", -(1 << 63) - 1),
        )
        for field, value in cases:
            with self.subTest(field=field, value=value), self.assertRaises((TypeError, ValueError)):
                self.evidence(**{field: value})


    def test_rejects_caller_owned_temporal_subclasses(self) -> None:
        evidence = self.evidence(
            source_updated_at="2026-09-21T11:59:50+00:00",
            received_wall_at="2026-09-21T11:59:50.250000+00:00",
        )
        lying_limit = _LyingDuration(seconds=1)
        self.assertFalse(lying_limit < timedelta(0))
        self.assertFalse(timedelta(seconds=10) > lying_limit)

        with self.assertRaisesRegex(TypeError, "max_quote_age must be an exact timedelta"):
            self.assess(evidence, max_quote_age=lying_limit)

        with self.assertRaisesRegex(
            TypeError, "max_source_clock_skew must be an exact timedelta"
        ):
            self.assess(
                evidence,
                max_source_clock_skew=_LyingDuration(milliseconds=100),
            )

        lying_decision = _LyingDecision(
            2026,
            9,
            21,
            12,
            0,
            1,
            tzinfo=timezone.utc,
        )
        self.assertEqual(lying_decision - datetime(2026, 9, 21, 11, 59, 50, tzinfo=timezone.utc), timedelta(0))
        with self.assertRaisesRegex(TypeError, "decision_at must be an exact datetime"):
            self.assess(evidence, decision_at=lying_decision)

    def test_decision_and_policy_boundaries_must_be_explicit_and_valid(self) -> None:
        evidence = self.evidence()
        with self.assertRaisesRegex(ValueError, "decision_at must be timezone-aware"):
            assess_provider_time_freshness(
                evidence,
                decision_at=datetime(2026, 9, 21, 12, 0, 1),
                max_quote_age=timedelta(seconds=1),
            )
        with self.assertRaisesRegex(ValueError, "max_quote_age must be non-negative"):
            self.assess(evidence, max_quote_age=timedelta(microseconds=-1))
        with self.assertRaisesRegex(ValueError, "max_source_clock_skew must be non-negative"):
            self.assess(
                evidence,
                max_source_clock_skew=timedelta(microseconds=-1),
            )


if __name__ == "__main__":
    unittest.main()
