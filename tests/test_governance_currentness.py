import inspect
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
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
    _freshness_limit_exceeded,
    resolve_current_governance,
    resolve_governance_for_decision,
)


class GovernanceCurrentnessTests(unittest.TestCase):
    def setUp(self) -> None:
        self._temp = tempfile.TemporaryDirectory()
        self.registry = BookmakerCapabilityRegistry(
            Path(self._temp.name) / "bookmaker-capability.json"
        )
        self._base_now = datetime.now(timezone.utc).replace(microsecond=0)

    def tearDown(self) -> None:
        self._temp.cleanup()

    def _instant(self, seconds_from_base: int) -> str:
        return (self._base_now + timedelta(seconds=seconds_from_base)).isoformat()

    def _evidence(
        self,
        *,
        terms_version: str = "terms-v1",
        permission: GovernancePermissionState = GovernancePermissionState.PERMITTED,
        observed_at: str | None = None,
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
            observed_at=observed_at or self._instant(-300),
            source_ref=source_ref,
            source_payload_sha256=digest,
        )

    def _resolve_current(
        self,
        *,
        max_age_seconds: int = 600,
        jurisdiction: str = "SK",
    ):
        return resolve_current_governance(
            self.registry,
            venue_id="provider-x",
            account_id="account-1",
            jurisdiction=jurisdiction,
            max_age_seconds=max_age_seconds,
        )

    def test_historical_timestamp_only_resolution_never_mints_currentness(self):
        cutoff = self._instant(-3_600)

        # The append happens after the historical cutoff in product execution order,
        # while source metadata claims an observation before that cutoff.
        self.registry.register_governance(
            self._evidence(observed_at=self._instant(-7_200))
        )

        result = resolve_governance_for_decision(
            self.registry,
            venue_id="provider-x",
            account_id="account-1",
            jurisdiction="SK",
            decision_at=cutoff,
            max_age_seconds=10_000,
        )

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.HISTORICAL_AVAILABILITY_UNPROVEN,
        )
        self.assertIsNone(result.evidence_id)
        self.assertFalse(result.execution_authorized)

    def test_historical_absence_is_also_unproven(self):
        result = resolve_governance_for_decision(
            self.registry,
            venue_id="provider-x",
            account_id="account-1",
            jurisdiction="SK",
            decision_at=self._instant(-3_600),
            max_age_seconds=600,
        )

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.HISTORICAL_AVAILABILITY_UNPROVEN,
        )

    def test_positive_public_surface_has_no_caller_time_parameter(self):
        parameters = inspect.signature(resolve_current_governance).parameters
        self.assertNotIn("decision_at", parameters)
        self.assertNotIn("as_of", parameters)
        self.assertNotIn("clock", parameters)
        self.assertNotIn("now", parameters)

    def test_current_resolver_rejects_clock_closure_cell_rebinding(self):
        evidence = self._evidence()
        self.registry.register_governance(evidence)

        closure = resolve_current_governance.__closure__
        self.assertIsNotNone(closure)
        original_clock = time.time_ns
        clock_cells = [
            cell
            for cell in closure
            if cell.cell_contents is original_clock
        ]
        self.assertEqual(len(clock_cells), 1)
        clock_cell = clock_cells[0]
        forged_called = False

        def forged_time_ns():
            nonlocal forged_called
            forged_called = True
            return 1

        clock_cell.cell_contents = forged_time_ns
        try:
            with self.assertRaisesRegex(
                GovernanceCurrentnessError,
                "product clock callable authority changed",
            ):
                self._resolve_current()
        finally:
            clock_cell.cell_contents = original_clock

        self.assertFalse(forged_called)

    def test_current_resolver_rejects_time_module_global_rebinding(self):
        import autosport.governance_currentness as module

        original_module = module.time

        class ForgedTimeModule:
            @staticmethod
            def time_ns():
                raise AssertionError("forged clock must never run")

        module.time = ForgedTimeModule()
        try:
            with self.assertRaisesRegex(
                GovernanceCurrentnessError,
                "product clock module authority changed",
            ):
                self._resolve_current()
        finally:
            module.time = original_module

    def test_current_resolver_rejects_time_ns_dispatch_rebinding(self):
        original_clock = time.time_ns
        forged_called = False

        def forged_time_ns():
            nonlocal forged_called
            forged_called = True
            return 1

        time.time_ns = forged_time_ns
        try:
            with self.assertRaisesRegex(
                GovernanceCurrentnessError,
                "product clock callable authority changed",
            ):
                self._resolve_current()
        finally:
            time.time_ns = original_clock

        self.assertFalse(forged_called)

    def test_module_has_no_positive_historical_classifier_helper(self):
        import autosport.governance_currentness as module

        self.assertFalse(hasattr(module, "_resolve_current_at"))
        self.assertFalse(hasattr(module, "_resolve_at"))

    def test_fresh_permitted_evidence_is_bounded_support_not_execution_authority(self):
        evidence = self._evidence()
        self.assertTrue(self.registry.register_governance(evidence))

        result = self._resolve_current()

        self.assertEqual(result.state, GovernanceEvidenceState.SUPPORTS_PERMITTED)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.QUALIFIED_PERMITTED,
        )
        self.assertEqual(result.evidence_id, evidence.evidence_id)
        self.assertEqual(result.terms_version, "terms-v1")
        self.assertEqual(result.source_payload_sha256, "a" * 64)
        self.assertIsNotNone(result.evidence_age_seconds)
        self.assertGreaterEqual(result.evidence_age_seconds, 299.0)
        self.assertLess(result.evidence_age_seconds, 305.0)
        self.assertFalse(result.execution_authorized)

    def test_later_prohibited_evidence_supersedes_earlier_permitted(self):
        self.registry.register_governance(
            self._evidence(
                observed_at=self._instant(-600),
                digest="a" * 64,
            )
        )
        prohibited = self._evidence(
            terms_version="terms-v2",
            permission=GovernancePermissionState.PROHIBITED,
            observed_at=self._instant(-120),
            digest="b" * 64,
            source_ref="https://provider.example/terms-v2",
        )
        self.registry.register_governance(prohibited)

        result = self._resolve_current()

        self.assertEqual(result.state, GovernanceEvidenceState.SUPPORTS_PROHIBITED)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.QUALIFIED_PROHIBITED,
        )
        self.assertEqual(result.evidence_id, prohibited.evidence_id)
        self.assertFalse(result.execution_authorized)

    def test_stale_permitted_evidence_fails_closed(self):
        evidence = self._evidence(observed_at=self._instant(-3_600))
        self.registry.register_governance(evidence)

        result = self._resolve_current(max_age_seconds=300)

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(result.reason, GovernanceCurrentnessReason.STALE)
        self.assertEqual(result.evidence_id, evidence.evidence_id)
        self.assertGreater(result.evidence_age_seconds, 3_599.0)

    def test_future_only_evidence_is_not_visible_to_current_resolution(self):
        self.registry.register_governance(
            self._evidence(observed_at=self._instant(3_600))
        )

        result = self._resolve_current()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.NO_ELIGIBLE_EVIDENCE,
        )
        self.assertIsNone(result.evidence_id)
        self.assertIsNone(result.evidence_age_seconds)

    def test_jurisdiction_evidence_cannot_cross_scope(self):
        self.registry.register_governance(self._evidence(jurisdiction="GB"))

        result = self._resolve_current(jurisdiction="SK")

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.NO_ELIGIBLE_EVIDENCE,
        )

    def test_same_terms_version_with_changed_document_digest_fails_closed(self):
        self.registry.register_governance(
            self._evidence(
                observed_at=self._instant(-600),
                digest="a" * 64,
            )
        )
        latest = self._evidence(
            observed_at=self._instant(-300),
            digest="b" * 64,
            source_ref="https://provider.example/terms-v1-refresh",
        )
        self.registry.register_governance(latest)

        result = self._resolve_current()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.TERMS_DOCUMENT_CONFLICT,
        )
        self.assertEqual(result.evidence_id, latest.evidence_id)
        self.assertFalse(result.execution_authorized)

    def test_same_document_cannot_flip_after_conflicting_interpretation(self):
        self.registry.register_governance(
            self._evidence(
                permission=GovernancePermissionState.PROHIBITED,
                observed_at=self._instant(-600),
                digest="a" * 64,
            )
        )
        latest = self._evidence(
            permission=GovernancePermissionState.PERMITTED,
            observed_at=self._instant(-300),
            digest="a" * 64,
            source_ref="https://provider.example/terms-v1-recheck",
        )
        self.registry.register_governance(latest)

        result = self._resolve_current()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.TERMS_PERMISSION_CONFLICT,
        )
        self.assertEqual(result.evidence_id, latest.evidence_id)
        self.assertFalse(result.execution_authorized)

    def test_same_timestamp_conflicting_latest_records_are_ambiguous(self):
        observed_at = self._instant(-300)
        self.registry.register_governance(
            self._evidence(
                terms_version="terms-v1",
                observed_at=observed_at,
                digest="a" * 64,
            )
        )
        self.registry.register_governance(
            self._evidence(
                terms_version="terms-v2",
                permission=GovernancePermissionState.PROHIBITED,
                observed_at=observed_at,
                digest="b" * 64,
                source_ref="https://provider.example/terms-v2",
            )
        )

        result = self._resolve_current()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.AMBIGUOUS_LATEST,
        )
        self.assertIsNone(result.evidence_id)

    def test_recorded_unknown_remains_unknown(self):
        evidence = self._evidence(permission=GovernancePermissionState.UNKNOWN)
        self.registry.register_governance(evidence)

        result = self._resolve_current()

        self.assertEqual(result.state, GovernanceEvidenceState.UNKNOWN)
        self.assertEqual(
            result.reason,
            GovernanceCurrentnessReason.RECORDED_UNKNOWN,
        )
        self.assertEqual(result.evidence_id, evidence.evidence_id)

    def test_exact_durable_registry_and_positive_freshness_policy_are_required(self):
        class SubstituteRegistry(BookmakerCapabilityRegistry):
            pass

        substitute = SubstituteRegistry(Path(self._temp.name) / "substitute.json")
        with self.assertRaisesRegex(
            GovernanceCurrentnessError,
            "exact durable BookmakerCapabilityRegistry",
        ):
            resolve_current_governance(
                substitute,
                venue_id="provider-x",
                account_id="account-1",
                jurisdiction="SK",
                max_age_seconds=600,
            )

        with self.assertRaisesRegex(GovernanceCurrentnessError, "max_age_seconds"):
            self._resolve_current(max_age_seconds=True)

    def test_freshness_cutoff_is_exact_to_one_microsecond_without_authority_output(self):
        observed = datetime.fromisoformat("0001-01-01T00:00:00+00:00")
        max_age_seconds = 315_537_897_599
        at_boundary = datetime.fromisoformat("9999-12-31T23:59:59+00:00")
        one_microsecond_late = datetime.fromisoformat(
            "9999-12-31T23:59:59.000001+00:00"
        )

        self.assertFalse(
            _freshness_limit_exceeded(observed, at_boundary, max_age_seconds)
        )
        self.assertTrue(
            _freshness_limit_exceeded(
                observed,
                one_microsecond_late,
                max_age_seconds,
            )
        )


if __name__ == "__main__":
    unittest.main()
