import tempfile
import unittest
from pathlib import Path

from autosport.bookmaker_capability_registry import (
    BookmakerCapabilityRegistry,
    BookmakerGovernanceEvidence,
    GovernancePermissionState,
)
from autosport.governance_currentness import (
    GovernanceCurrentnessError,
    GovernanceCurrentnessReason,
    GovernanceEvidenceState,
    resolve_governance_for_decision,
)


class GovernanceCurrentnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.registry = BookmakerCapabilityRegistry(
            Path(self._temp.name) / "bookmaker-capability.json"
        )

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _evidence(
        self,
        *,
        terms_version: str = "terms-v1",
        permission: GovernancePermissionState = GovernancePermissionState.PERMITTED,
        observed_at: str = "2026-09-21T12:00:00+00:00",
        digest: str = "a" * 64,
        jurisdiction: str = "SK",
        source_ref: str = "https://provider.example/terms-v1",
    ) -> BookmakerGovernanceEvidence:
        return BookmakerGovernanceEvidence(
            venue_id="provider-x",
            account_id="account-1",
            jurisdiction=jurisdiction,
            terms_version=terms_version,
            automation_permission=permission,
            observed_at=observed_at,
            source_ref=source_ref,
            source_payload_sha256=digest,
        )

    def _resolve(
        self,
        *,
        decision_at: str = "2026-09-21T12:05:00+00:00",
        max_age_seconds: int = 600,
        jurisdiction: str = "SK",
    ):
        return resolve_governance_for_decision(
            self.registry,
            venue_id="provider-x",
            account_id="account-1",
            jurisdiction=jurisdiction,
            decision_at=decision_at,
            max_age_seconds=max_age_seconds,
        )

    def test_fresh_permitted_evidence_is_bounded_support_not_execution_authority(self):
        evidence = self._evidence()
        self.assertTrue(self.registry.register_governance(evidence))

        result = self._resolve()

        self.assertEqual(
            result.state,
            GovernanceEvidenceState.SUPPORTS_PERMITTED,
        )
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.QUALIFIED_PERMITTED,
        )
        self.assertEqual(result.evidence_id, evidence.evidence_id)
        self.assertEqual(result.terms_version, "terms-v1")
        self.assertEqual(result.source_payload_sha256, "a" * 64)
        self.assertEqual(result.evidence_age_seconds, 300.0)
        self.assertFalse(result.execution_authorized)

    def test_later_prohibited_evidence_supersedes_earlier_permitted(self):
        self.registry.register_governance(
            self._evidence(
                observed_at="2026-09-21T11:55:00+00:00",
                digest="a" * 64,
            )
        )
        prohibited = self._evidence(
            terms_version="terms-v2",
            permission=GovernancePermissionState.PROHIBITED,
            observed_at="2026-09-21T12:03:00+00:00",
            digest="b" * 64,
            source_ref="https://provider.example/terms-v2",
        )
        self.registry.register_governance(prohibited)

        result = self._resolve()

        self.assertEqual(
            result.state,
            GovernanceEvidenceState.SUPPORTS_PROHIBITED,
        )
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.QUALIFIED_PROHIBITED,
        )
        self.assertEqual(result.evidence_id, prohibited.evidence_id)
        self.assertFalse(result.execution_authorized)

    def test_stale_permitted_evidence_fails_closed(self):
        evidence = self._evidence(observed_at="2026-09-21T11:00:00+00:00")
        self.registry.register_governance(evidence)

        result = self._resolve(max_age_seconds=300)

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(result.reason, GovernanceCurrentnessReason.STALE)
        self.assertEqual(result.evidence_id, evidence.evidence_id)
        self.assertEqual(result.evidence_age_seconds, 3900.0)

    def test_future_only_evidence_is_not_visible_at_decision_cutoff(self):
        self.registry.register_governance(
            self._evidence(observed_at="2026-09-21T12:10:00+00:00")
        )

        result = self._resolve()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.NO_ELIGIBLE_EVIDENCE,
        )
        self.assertIsNone(result.evidence_id)
        self.assertIsNone(result.evidence_age_seconds)

    def test_jurisdiction_evidence_cannot_cross_scope(self):
        self.registry.register_governance(
            self._evidence(jurisdiction="GB")
        )

        result = self._resolve(jurisdiction="SK")

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.NO_ELIGIBLE_EVIDENCE,
        )

    def test_same_terms_version_with_changed_document_digest_fails_closed(self):
        self.registry.register_governance(
            self._evidence(
                observed_at="2026-09-21T11:50:00+00:00",
                digest="a" * 64,
            )
        )
        latest = self._evidence(
            observed_at="2026-09-21T12:00:00+00:00",
            digest="b" * 64,
            source_ref="https://provider.example/terms-v1-refresh",
        )
        self.registry.register_governance(latest)

        result = self._resolve()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.TERMS_DOCUMENT_CONFLICT,
        )
        self.assertEqual(result.evidence_id, latest.evidence_id)
        self.assertFalse(result.execution_authorized)

    def test_same_document_cannot_flip_to_permitted_after_conflicting_interpretation(self):
        self.registry.register_governance(
            self._evidence(
                permission=GovernancePermissionState.PROHIBITED,
                observed_at="2026-09-21T11:50:00+00:00",
                digest="a" * 64,
            )
        )
        latest = self._evidence(
            permission=GovernancePermissionState.PERMITTED,
            observed_at="2026-09-21T12:00:00+00:00",
            digest="a" * 64,
            source_ref="https://provider.example/terms-v1-recheck",
        )
        self.registry.register_governance(latest)

        result = self._resolve()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.TERMS_PERMISSION_CONFLICT,
        )
        self.assertEqual(result.evidence_id, latest.evidence_id)
        self.assertFalse(result.execution_authorized)

    def test_same_timestamp_conflicting_latest_records_are_ambiguous(self):
        self.registry.register_governance(
            self._evidence(
                terms_version="terms-v1",
                digest="a" * 64,
            )
        )
        self.registry.register_governance(
            self._evidence(
                terms_version="terms-v2",
                permission=GovernancePermissionState.PROHIBITED,
                digest="b" * 64,
                source_ref="https://provider.example/terms-v2",
            )
        )

        result = self._resolve()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.AMBIGUOUS_LATEST,
        )
        self.assertIsNone(result.evidence_id)

    def test_recorded_unknown_remains_unknown(self):
        evidence = self._evidence(
            permission=GovernancePermissionState.UNKNOWN,
        )
        self.registry.register_governance(evidence)

        result = self._resolve()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.RECORDED_UNKNOWN,
        )
        self.assertEqual(result.evidence_id, evidence.evidence_id)

    def test_exact_durable_registry_and_positive_freshness_policy_are_required(self):
        class SubstituteRegistry(BookmakerCapabilityRegistry):
            pass

        substitute = SubstituteRegistry(
            Path(self._temp.name) / "substitute.json"
        )
        with self.assertRaisesRegex(
            GovernanceCurrentnessError,
            "exact durable BookmakerCapabilityRegistry",
        ):
            resolve_governance_for_decision(
                substitute,
                venue_id="provider-x",
                account_id="account-1",
                jurisdiction="SK",
                decision_at="2026-09-21T12:05:00+00:00",
                max_age_seconds=600,
            )

        with self.assertRaisesRegex(
            GovernanceCurrentnessError,
            "max_age_seconds",
        ):
            self._resolve(max_age_seconds=True)


if __name__ == "__main__":
    unittest.main()
