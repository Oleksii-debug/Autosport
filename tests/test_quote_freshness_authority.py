from dataclasses import replace
from datetime import datetime, timedelta, timezone
import unittest

from autosport.quote_freshness_authority import (
    QuoteFreshnessAuthority,
    QuoteFreshnessDecision,
    QuoteFreshnessEvidence,
    QuoteFreshnessIdentity,
    QuoteFreshnessTimestampKind,
    QuoteFreshnessVerdict,
    evaluate_quote_freshness,
)


UTC = timezone.utc
DECISION_AT = datetime(2026, 9, 21, 10, 0, 0, tzinfo=UTC)


def iso(offset_seconds: float) -> str:
    return (DECISION_AT + timedelta(seconds=offset_seconds)).isoformat()


class QuoteFreshnessAuthorityTests(unittest.TestCase):
    @staticmethod
    def identity(**changes: object) -> QuoteFreshnessIdentity:
        base: dict[str, object] = {
            "source_id": "parlayapi:table_tennis",
            "provider_id": "parlayapi",
            "source_mode": "official_api",
            "contract_version": "parlayapi-v1",
            "sport": "table_tennis",
            "event_id": "event-1",
            "market_id": "book:h2h",
            "selection_id": "player-a",
            "side": "BACK",
        }
        base.update(changes)
        return QuoteFreshnessIdentity(**base)  # type: ignore[arg-type]

    @classmethod
    def evidence(cls, **changes: object) -> QuoteFreshnessEvidence:
        base: dict[str, object] = {
            "identity": cls.identity(),
            "quote_id": "quote-1",
            "provider_sequence": 10,
            "source_ts": iso(-3),
            "observed_ts": iso(-2),
            "ingest_ts": iso(-1),
        }
        base.update(changes)
        return QuoteFreshnessEvidence(**base)  # type: ignore[arg-type]

    @classmethod
    def authority(cls, **changes: object) -> QuoteFreshnessAuthority:
        base: dict[str, object] = {
            "authority_id": "parlayapi-live-v1",
            "identity": cls.identity(),
            "timestamp_kind": QuoteFreshnessTimestampKind.PROVIDER_SOURCE,
            "max_age": timedelta(seconds=5),
            "max_future_skew": timedelta(0),
            "inclusive_max_age": True,
            "require_monotonic_provider_sequence": False,
        }
        base.update(changes)
        return QuoteFreshnessAuthority(**base)  # type: ignore[arg-type]

    def evaluate(self, **changes: object) -> QuoteFreshnessDecision:
        evidence = changes.pop("evidence", self.evidence())
        authority = changes.pop("authority", self.authority())
        decision_at = changes.pop("decision_at", DECISION_AT)
        previous = changes.pop("previous_provider_sequence", None)
        self.assertFalse(changes, f"unused changes: {changes}")
        return evaluate_quote_freshness(
            evidence,  # type: ignore[arg-type]
            decision_at=decision_at,  # type: ignore[arg-type]
            authority=authority,  # type: ignore[arg-type]
            previous_provider_sequence=previous,  # type: ignore[arg-type]
        )

    def test_provider_source_fresh_is_distinct_and_eligible(self) -> None:
        decision = self.evaluate()
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_PROVIDER)
        self.assertTrue(decision.decision_eligible)
        self.assertTrue(decision.provider_freshness_proven)
        self.assertEqual(decision.age, timedelta(seconds=3))

    def test_receipt_time_requires_explicit_authority_and_never_promotes(self) -> None:
        authority = self.authority(
            timestamp_kind=QuoteFreshnessTimestampKind.LOCAL_OBSERVED,
        )
        decision = self.evaluate(
            evidence=self.evidence(source_ts=None),
            authority=authority,
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_RECEIPT_ONLY)
        self.assertTrue(decision.decision_eligible)
        self.assertFalse(decision.provider_freshness_proven)
        self.assertEqual(decision.age, timedelta(seconds=2))

    def test_receipt_time_stays_receipt_only_even_when_source_time_exists(self) -> None:
        decision = self.evaluate(
            authority=self.authority(
                timestamp_kind=QuoteFreshnessTimestampKind.LOCAL_OBSERVED,
            )
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_RECEIPT_ONLY)
        self.assertFalse(decision.provider_freshness_proven)

    def test_provider_source_authority_does_not_fallback_to_receipt(self) -> None:
        decision = self.evaluate(evidence=self.evidence(source_ts=None))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)
        self.assertFalse(decision.decision_eligible)

    def test_provider_time_can_be_stale_while_receipt_is_recent(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(
                source_ts=iso(-6),
                observed_ts=iso(-1),
                ingest_ts=iso(-0.5),
            )
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.STALE)
        self.assertEqual(decision.age, timedelta(seconds=6))

    def test_receipt_authority_can_accept_recent_receipt_of_old_provider_quote(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(
                source_ts=iso(-60),
                observed_ts=iso(-1),
                ingest_ts=iso(-0.5),
            ),
            authority=self.authority(
                timestamp_kind=QuoteFreshnessTimestampKind.LOCAL_OBSERVED,
            ),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_RECEIPT_ONLY)
        self.assertFalse(decision.provider_freshness_proven)

    def test_inclusive_age_boundary_is_fresh(self) -> None:
        decision = self.evaluate(evidence=self.evidence(source_ts=iso(-5)))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_PROVIDER)

    def test_exclusive_age_boundary_is_stale(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(source_ts=iso(-5)),
            authority=self.authority(inclusive_max_age=False),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.STALE)

    def test_just_over_age_boundary_is_stale(self) -> None:
        decision = self.evaluate(evidence=self.evidence(source_ts=iso(-5.000001)))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.STALE)

    def test_zero_age_is_fresh(self) -> None:
        decision = self.evaluate(evidence=self.evidence(source_ts=iso(0)))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_PROVIDER)
        self.assertEqual(decision.age, timedelta(0))

    def test_provider_future_without_skew_allowance_is_future(self) -> None:
        decision = self.evaluate(evidence=self.evidence(source_ts=iso(0.001)))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FUTURE)
        self.assertFalse(decision.decision_eligible)

    def test_provider_future_within_explicit_skew_is_fresh_with_zero_age(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(source_ts=iso(0.5)),
            authority=self.authority(max_future_skew=timedelta(seconds=1)),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_PROVIDER)
        self.assertEqual(decision.age, timedelta(0))

    def test_provider_future_beyond_explicit_skew_is_future(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(source_ts=iso(1.000001)),
            authority=self.authority(max_future_skew=timedelta(seconds=1)),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FUTURE)

    def test_future_observed_time_is_causally_unavailable_even_with_provider_skew(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(observed_ts=iso(0.1), ingest_ts=iso(0.2)),
            authority=self.authority(max_future_skew=timedelta(seconds=5)),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FUTURE)

    def test_future_ingest_time_is_causally_unavailable(self) -> None:
        decision = self.evaluate(evidence=self.evidence(ingest_ts=iso(0.1)))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FUTURE)

    def test_missing_observed_time_is_unknown_even_for_provider_source_mode(self) -> None:
        decision = self.evaluate(evidence=self.evidence(observed_ts=None))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_missing_ingest_time_is_unknown_even_for_provider_source_mode(self) -> None:
        decision = self.evaluate(evidence=self.evidence(ingest_ts=None))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_observed_after_ingest_is_unknown(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(observed_ts=iso(-1), ingest_ts=iso(-2))
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_malformed_source_time_is_unknown_even_in_receipt_mode(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(source_ts="bad"),
            authority=self.authority(
                timestamp_kind=QuoteFreshnessTimestampKind.LOCAL_OBSERVED,
            ),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_naive_source_time_is_unknown(self) -> None:
        decision = self.evaluate(evidence=self.evidence(source_ts="2026-09-21T09:59:57"))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_naive_observed_time_is_unknown(self) -> None:
        decision = self.evaluate(evidence=self.evidence(observed_ts="2026-09-21T09:59:58"))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_naive_ingest_time_is_unknown(self) -> None:
        decision = self.evaluate(evidence=self.evidence(ingest_ts="2026-09-21T09:59:59"))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_naive_decision_time_is_unknown(self) -> None:
        decision = self.evaluate(decision_at=datetime(2026, 9, 21, 10, 0, 0))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_extreme_timestamp_utc_normalization_overflow_fails_closed(self) -> None:
        cases = (
            {"source_ts": "0001-01-01T00:00:00+14:00"},
            {"observed_ts": "0001-01-01T00:00:00+14:00"},
            {"ingest_ts": "9999-12-31T23:59:59-14:00"},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                decision = self.evaluate(evidence=self.evidence(**changes))
                self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_extreme_decision_time_utc_normalization_overflow_fails_closed(self) -> None:
        extreme = datetime(
            1, 1, 1, tzinfo=timezone(timedelta(hours=14))
        )
        decision = self.evaluate(decision_at=extreme)
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_non_datetime_decision_time_is_unknown(self) -> None:
        decision = self.evaluate(decision_at="2026-09-21T10:00:00+00:00")
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_offset_timestamps_are_normalized_to_utc(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(
                source_ts="2026-09-21T11:59:57+02:00",
                observed_ts="2026-09-21T11:59:58+02:00",
                ingest_ts="2026-09-21T11:59:59+02:00",
            )
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_PROVIDER)
        self.assertEqual(decision.age, timedelta(seconds=3))

    def test_monotonic_sequence_can_be_required(self) -> None:
        decision = self.evaluate(
            authority=self.authority(require_monotonic_provider_sequence=True),
            previous_provider_sequence=9,
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_PROVIDER)

    def test_missing_sequence_fails_when_monotonicity_required(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(provider_sequence=None),
            authority=self.authority(require_monotonic_provider_sequence=True),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_boolean_sequence_fails_when_monotonicity_required(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(provider_sequence=True),
            authority=self.authority(require_monotonic_provider_sequence=True),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_negative_sequence_fails_when_monotonicity_required(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(provider_sequence=-1),
            authority=self.authority(require_monotonic_provider_sequence=True),
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_replayed_sequence_fails_when_monotonicity_required(self) -> None:
        decision = self.evaluate(
            authority=self.authority(require_monotonic_provider_sequence=True),
            previous_provider_sequence=10,
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_regressed_sequence_fails_when_monotonicity_required(self) -> None:
        decision = self.evaluate(
            evidence=self.evidence(provider_sequence=9),
            authority=self.authority(require_monotonic_provider_sequence=True),
            previous_provider_sequence=10,
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_malformed_previous_sequence_fails_closed(self) -> None:
        decision = self.evaluate(
            authority=self.authority(require_monotonic_provider_sequence=True),
            previous_provider_sequence=True,
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_sequence_is_not_promoted_to_authority_when_not_required(self) -> None:
        decision = self.evaluate(evidence=self.evidence(provider_sequence=None))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.FRESH_PROVIDER)

    def test_malformed_evidence_object_fails_closed(self) -> None:
        decision = self.evaluate(evidence={})
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_malformed_authority_object_fails_closed(self) -> None:
        decision = self.evaluate(authority={})
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_empty_quote_id_fails_closed(self) -> None:
        decision = self.evaluate(evidence=self.evidence(quote_id=""))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_whitespace_quote_id_fails_closed(self) -> None:
        decision = self.evaluate(evidence=self.evidence(quote_id=" quote "))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_empty_authority_id_fails_closed(self) -> None:
        decision = self.evaluate(authority=self.authority(authority_id=""))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_negative_max_age_fails_closed(self) -> None:
        decision = self.evaluate(authority=self.authority(max_age=timedelta(microseconds=-1)))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_negative_future_skew_fails_closed(self) -> None:
        decision = self.evaluate(
            authority=self.authority(max_future_skew=timedelta(microseconds=-1))
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_boolean_policy_flags_must_be_real_booleans(self) -> None:
        decision = self.evaluate(authority=self.authority(inclusive_max_age=1))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)
        decision = self.evaluate(
            authority=self.authority(require_monotonic_provider_sequence=1)
        )
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_timestamp_kind_must_be_enum_not_lookalike_string(self) -> None:
        decision = self.evaluate(authority=self.authority(timestamp_kind="provider_source"))
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_identity_must_be_exact_for_every_authority_dimension(self) -> None:
        changes = {
            "source_id": "other:table_tennis",
            "provider_id": "other",
            "source_mode": "browser_adapter",
            "contract_version": "parlayapi-v2",
            "sport": "soccer",
            "event_id": "event-2",
            "market_id": "book:totals",
            "selection_id": "player-b",
            "side": "LAY",
        }
        for field, value in changes.items():
            with self.subTest(field=field):
                evidence = self.evidence(
                    identity=replace(self.identity(), **{field: value})
                )
                decision = self.evaluate(evidence=evidence)
                self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_identity_fields_must_be_nonempty_trimmed_utf8(self) -> None:
        for field in (
            "source_id",
            "provider_id",
            "source_mode",
            "contract_version",
            "sport",
            "event_id",
            "market_id",
            "selection_id",
        ):
            for value in ("", " x "):
                with self.subTest(field=field, value=value):
                    identity = replace(self.identity(), **{field: value})
                    decision = self.evaluate(evidence=self.evidence(identity=identity))
                    self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_side_must_be_back_or_lay(self) -> None:
        for side in ("back", "BUY", "", " BACK "):
            with self.subTest(side=side):
                identity = replace(self.identity(), side=side)
                decision = self.evaluate(evidence=self.evidence(identity=identity))
                self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_authority_identity_itself_must_be_valid(self) -> None:
        bad_authority = self.authority(identity=replace(self.identity(), provider_id=""))
        decision = self.evaluate(authority=bad_authority)
        self.assertEqual(decision.verdict, QuoteFreshnessVerdict.UNKNOWN)

    def test_zero_max_age_requires_exact_timestamp(self) -> None:
        authority = self.authority(max_age=timedelta(0))
        self.assertEqual(
            self.evaluate(
                evidence=self.evidence(source_ts=iso(0)), authority=authority
            ).verdict,
            QuoteFreshnessVerdict.FRESH_PROVIDER,
        )
        self.assertEqual(
            self.evaluate(
                evidence=self.evidence(source_ts=iso(-0.000001)), authority=authority
            ).verdict,
            QuoteFreshnessVerdict.STALE,
        )

    def test_age_sweep_is_monotone_at_inclusive_boundary(self) -> None:
        authority = self.authority(max_age=timedelta(milliseconds=100))
        fresh = 0
        stale = 0
        for milliseconds in range(0, 201):
            verdict = self.evaluate(
                evidence=self.evidence(source_ts=iso(-milliseconds / 1000)),
                authority=authority,
            ).verdict
            if milliseconds <= 100:
                self.assertEqual(verdict, QuoteFreshnessVerdict.FRESH_PROVIDER)
                fresh += 1
            else:
                self.assertEqual(verdict, QuoteFreshnessVerdict.STALE)
                stale += 1
        self.assertEqual((fresh, stale), (101, 100))

    def test_future_skew_sweep_has_exact_boundary(self) -> None:
        authority = self.authority(max_future_skew=timedelta(milliseconds=5))
        for milliseconds in range(0, 11):
            with self.subTest(milliseconds=milliseconds):
                verdict = self.evaluate(
                    evidence=self.evidence(source_ts=iso(milliseconds / 1000)),
                    authority=authority,
                ).verdict
                expected = (
                    QuoteFreshnessVerdict.FRESH_PROVIDER
                    if milliseconds <= 5
                    else QuoteFreshnessVerdict.FUTURE
                )
                self.assertEqual(verdict, expected)

    def test_decision_reason_does_not_overclaim_execution(self) -> None:
        decision = self.evaluate()
        self.assertNotIn("fill", decision.reason.lower())
        self.assertNotIn("profit", decision.reason.lower())
        self.assertNotIn("real-money", decision.reason.lower())


if __name__ == "__main__":
    unittest.main()
