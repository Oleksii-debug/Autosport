from __future__ import annotations

import copy
import unittest

from autosport.model_lifecycle import (
    ModelDriftState,
    ModelEligibility,
    ModelLifecycleError,
    ModelLifecycleRevision,
    ModelLifecycleState,
    evaluate_model_eligibility,
    validate_lifecycle_successor,
)


class ModelLifecycleTests(unittest.TestCase):
    CREATED = "2026-09-21T06:00:00+00:00"
    UPDATED = "2026-09-21T07:00:00+00:00"
    KNOWLEDGE_UNTIL = "2026-09-22T06:00:00+00:00"
    DRIFT_OBSERVED = "2026-09-21T06:55:00+00:00"
    DRIFT_UNTIL = "2026-09-21T08:55:00+00:00"
    ARTIFACT_SHA = "a" * 64

    @classmethod
    def _revision(cls, **overrides) -> ModelLifecycleRevision:
        values = {
            "model_version_id": "model-v17",
            "model_artifact_sha256": cls.ARTIFACT_SHA,
            "revision": 1,
            "lifecycle_state": ModelLifecycleState.ACTIVE,
            "drift_state": ModelDriftState.STABLE,
            "created_at": cls.CREATED,
            "updated_at": cls.UPDATED,
            "knowledge_valid_until": cls.KNOWLEDGE_UNTIL,
            "drift_observed_at": cls.DRIFT_OBSERVED,
            "drift_valid_until": cls.DRIFT_UNTIL,
            "evidence_refs": ("evidence://drift/model-v17/001",),
            "reason_code": "initial_qualification",
        }
        values.update(overrides)
        return ModelLifecycleRevision(**values)

    def test_round_trip_and_fingerprint_bind_exact_model_artifact(self) -> None:
        revision = self._revision()
        payload = revision.to_dict()
        restored = ModelLifecycleRevision.from_dict(copy.deepcopy(payload))

        self.assertEqual(restored, revision)
        self.assertEqual(restored.fingerprint_sha256, revision.fingerprint_sha256)
        self.assertEqual(len(revision.fingerprint_sha256), 64)

        changed = self._revision(model_artifact_sha256="b" * 64)
        self.assertNotEqual(changed.fingerprint_sha256, revision.fingerprint_sha256)

    def test_strict_schema_and_json_types_fail_closed(self) -> None:
        payload = self._revision().to_dict()

        extra = dict(payload)
        extra["execution_authorized"] = True
        with self.assertRaisesRegex(ModelLifecycleError, "exactly canonical fields"):
            ModelLifecycleRevision.from_dict(extra)

        missing = dict(payload)
        missing.pop("reason_code")
        with self.assertRaisesRegex(ModelLifecycleError, "exactly canonical fields"):
            ModelLifecycleRevision.from_dict(missing)

        bool_schema = dict(payload)
        bool_schema["schema_version"] = True
        with self.assertRaisesRegex(ModelLifecycleError, "unsupported"):
            ModelLifecycleRevision.from_dict(bool_schema)

        float_schema = dict(payload)
        float_schema["schema_version"] = 1.0
        with self.assertRaisesRegex(ModelLifecycleError, "unsupported"):
            ModelLifecycleRevision.from_dict(float_schema)

        numeric_enum = dict(payload)
        numeric_enum["drift_state"] = 2
        with self.assertRaisesRegex(ModelLifecycleError, "JSON string"):
            ModelLifecycleRevision.from_dict(numeric_enum)

        numeric_revision = dict(payload)
        numeric_revision["revision"] = True
        with self.assertRaisesRegex(ModelLifecycleError, "positive non-boolean integer"):
            ModelLifecycleRevision.from_dict(numeric_revision)

    def test_identity_timestamp_and_evidence_are_canonical(self) -> None:
        with self.assertRaisesRegex(ModelLifecycleError, "canonical text"):
            self._revision(model_version_id=" model-v17")
        with self.assertRaisesRegex(ModelLifecycleError, "lowercase SHA-256"):
            self._revision(model_artifact_sha256="A" * 64)
        with self.assertRaisesRegex(ModelLifecycleError, "UTC \+00:00"):
            self._revision(updated_at="2026-09-21T09:00:00+02:00")
        with self.assertRaisesRegex(ModelLifecycleError, "timezone-aware"):
            self._revision(updated_at="2026-09-21T07:00:00")
        with self.assertRaisesRegex(ModelLifecycleError, "sorted and unique"):
            self._revision(evidence_refs=("evidence://z", "evidence://a"))
        with self.assertRaisesRegex(ModelLifecycleError, "sorted and unique"):
            self._revision(evidence_refs=("evidence://a", "evidence://a"))

    def test_temporal_contract_prevents_future_or_overlong_drift_evidence(self) -> None:
        with self.assertRaisesRegex(ModelLifecycleError, "updated_at must not precede"):
            self._revision(updated_at="2026-09-21T05:59:59+00:00")
        with self.assertRaisesRegex(ModelLifecycleError, "strictly after created_at"):
            self._revision(knowledge_valid_until=self.CREATED)
        with self.assertRaisesRegex(ModelLifecycleError, "must not precede created_at"):
            self._revision(drift_observed_at="2026-09-21T05:59:59+00:00")
        with self.assertRaisesRegex(ModelLifecycleError, "must not exceed updated_at"):
            self._revision(drift_observed_at="2026-09-21T07:00:01+00:00")
        with self.assertRaisesRegex(ModelLifecycleError, "strictly after drift_observed_at"):
            self._revision(drift_valid_until=self.DRIFT_OBSERVED)
        with self.assertRaisesRegex(ModelLifecycleError, "must not exceed knowledge_valid_until"):
            self._revision(drift_valid_until="2026-09-22T06:00:01+00:00")

    def test_known_drift_and_non_active_states_require_evidence(self) -> None:
        with self.assertRaisesRegex(ModelLifecycleError, "known drift state requires"):
            self._revision(evidence_refs=())

        unknown_active = self._revision(
            drift_state=ModelDriftState.UNKNOWN,
            evidence_refs=(),
        )
        result = evaluate_model_eligibility(
            unknown_active,
            evaluated_at="2026-09-21T07:30:00+00:00",
        )
        self.assertFalse(result.eligible)
        self.assertEqual(
            result.reasons,
            ("drift_unknown", "missing_lifecycle_evidence"),
        )

        with self.assertRaisesRegex(ModelLifecycleError, "non-active lifecycle"):
            self._revision(
                lifecycle_state=ModelLifecycleState.QUARANTINED,
                drift_state=ModelDriftState.UNKNOWN,
                evidence_refs=(),
            )

    def test_expired_state_cannot_be_declared_before_expiry_boundary(self) -> None:
        with self.assertRaisesRegex(ModelLifecycleError, "at or after knowledge expiry"):
            self._revision(lifecycle_state=ModelLifecycleState.EXPIRED)

        expired = self._revision(
            revision=2,
            lifecycle_state=ModelLifecycleState.EXPIRED,
            updated_at=self.KNOWLEDGE_UNTIL,
            drift_observed_at="2026-09-22T05:55:00+00:00",
            drift_valid_until=self.KNOWLEDGE_UNTIL,
            evidence_refs=("evidence://expiry/model-v17",),
            reason_code="knowledge_horizon_reached",
        )
        self.assertEqual(expired.lifecycle_state, ModelLifecycleState.EXPIRED)

    def test_eligibility_is_fail_closed_at_exact_expiry_boundaries(self) -> None:
        revision = self._revision()

        eligible = evaluate_model_eligibility(
            revision,
            evaluated_at="2026-09-21T07:30:00+00:00",
        )
        self.assertIsInstance(eligible, ModelEligibility)
        self.assertTrue(eligible.eligible)
        self.assertEqual(eligible.reasons, ())
        self.assertEqual(eligible.model_version_id, revision.model_version_id)
        self.assertEqual(
            eligible.lifecycle_fingerprint_sha256,
            revision.fingerprint_sha256,
        )

        at_drift_expiry = evaluate_model_eligibility(
            revision,
            evaluated_at=self.DRIFT_UNTIL,
        )
        self.assertFalse(at_drift_expiry.eligible)
        self.assertEqual(at_drift_expiry.reasons, ("drift_evidence_expired",))

        at_knowledge_expiry = evaluate_model_eligibility(
            revision,
            evaluated_at=self.KNOWLEDGE_UNTIL,
        )
        self.assertFalse(at_knowledge_expiry.eligible)
        self.assertIn("knowledge_expired", at_knowledge_expiry.reasons)
        self.assertIn("drift_evidence_expired", at_knowledge_expiry.reasons)

    def test_drift_warning_breach_quarantine_and_retirement_are_ineligible(self) -> None:
        warning = self._revision(drift_state=ModelDriftState.WARNING)
        warning_result = evaluate_model_eligibility(
            warning,
            evaluated_at="2026-09-21T07:30:00+00:00",
        )
        self.assertEqual(warning_result.reasons, ("drift_warning",))

        breach = self._revision(drift_state=ModelDriftState.BREACH)
        breach_result = evaluate_model_eligibility(
            breach,
            evaluated_at="2026-09-21T07:30:00+00:00",
        )
        self.assertEqual(breach_result.reasons, ("drift_breach",))

        quarantined = self._revision(
            lifecycle_state=ModelLifecycleState.QUARANTINED,
            drift_state=ModelDriftState.BREACH,
            reason_code="drift_threshold_breach",
        )
        quarantine_result = evaluate_model_eligibility(
            quarantined,
            evaluated_at="2026-09-21T07:30:00+00:00",
        )
        self.assertEqual(
            quarantine_result.reasons,
            ("lifecycle_quarantined", "drift_breach"),
        )

        retired = self._revision(
            lifecycle_state=ModelLifecycleState.RETIRED,
            reason_code="superseded_after_review",
        )
        retired_result = evaluate_model_eligibility(
            retired,
            evaluated_at="2026-09-21T07:30:00+00:00",
        )
        self.assertEqual(retired_result.reasons, ("lifecycle_retired",))

    def test_successor_preserves_identity_and_cannot_extend_knowledge_horizon(self) -> None:
        previous = self._revision()
        candidate = self._revision(
            revision=2,
            updated_at="2026-09-21T07:30:00+00:00",
            drift_observed_at="2026-09-21T07:25:00+00:00",
            drift_valid_until="2026-09-21T09:25:00+00:00",
            evidence_refs=("evidence://drift/model-v17/002",),
            reason_code="scheduled_drift_refresh",
        )
        validate_lifecycle_successor(previous, candidate)

        with self.assertRaisesRegex(ModelLifecycleError, "preserve model_version_id"):
            validate_lifecycle_successor(
                previous,
                self._revision(
                    model_version_id="model-v18",
                    revision=2,
                    updated_at="2026-09-21T07:30:00+00:00",
                    drift_observed_at="2026-09-21T07:25:00+00:00",
                ),
            )
        with self.assertRaisesRegex(ModelLifecycleError, "preserve model_artifact_sha256"):
            validate_lifecycle_successor(
                previous,
                self._revision(
                    model_artifact_sha256="b" * 64,
                    revision=2,
                    updated_at="2026-09-21T07:30:00+00:00",
                    drift_observed_at="2026-09-21T07:25:00+00:00",
                ),
            )
        with self.assertRaisesRegex(ModelLifecycleError, "contiguous"):
            validate_lifecycle_successor(
                previous,
                self._revision(
                    revision=3,
                    updated_at="2026-09-21T07:30:00+00:00",
                    drift_observed_at="2026-09-21T07:25:00+00:00",
                ),
            )
        with self.assertRaisesRegex(ModelLifecycleError, "extend knowledge validity"):
            validate_lifecycle_successor(
                previous,
                self._revision(
                    revision=2,
                    updated_at="2026-09-21T07:30:00+00:00",
                    knowledge_valid_until="2026-09-23T06:00:00+00:00",
                    drift_observed_at="2026-09-21T07:25:00+00:00",
                    drift_valid_until="2026-09-21T09:25:00+00:00",
                ),
            )

    def test_retired_and_expired_models_cannot_reactivate(self) -> None:
        retired = self._revision(
            lifecycle_state=ModelLifecycleState.RETIRED,
            reason_code="retired_for_replacement",
        )
        with self.assertRaisesRegex(ModelLifecycleError, "terminal"):
            validate_lifecycle_successor(
                retired,
                self._revision(
                    revision=2,
                    updated_at="2026-09-21T07:30:00+00:00",
                    drift_observed_at="2026-09-21T07:25:00+00:00",
                ),
            )

        expired = self._revision(
            revision=2,
            lifecycle_state=ModelLifecycleState.EXPIRED,
            updated_at=self.KNOWLEDGE_UNTIL,
            drift_observed_at="2026-09-22T05:55:00+00:00",
            drift_valid_until=self.KNOWLEDGE_UNTIL,
            evidence_refs=("evidence://expiry/model-v17",),
            reason_code="knowledge_horizon_reached",
        )
        with self.assertRaisesRegex(ModelLifecycleError, "cannot become active again"):
            validate_lifecycle_successor(
                expired,
                self._revision(
                    revision=3,
                    updated_at="2026-09-22T06:10:00+00:00",
                    knowledge_valid_until=self.KNOWLEDGE_UNTIL,
                    drift_observed_at="2026-09-22T05:58:00+00:00",
                    drift_valid_until="2026-09-22T05:59:00+00:00",
                    evidence_refs=("evidence://reactivation/attempt",),
                ),
            )

    def test_quarantine_release_requires_new_stable_drift_evidence(self) -> None:
        quarantined = self._revision(
            lifecycle_state=ModelLifecycleState.QUARANTINED,
            drift_state=ModelDriftState.BREACH,
            reason_code="drift_threshold_breach",
        )

        with self.assertRaisesRegex(ModelLifecycleError, "stable drift evidence"):
            validate_lifecycle_successor(
                quarantined,
                self._revision(
                    revision=2,
                    lifecycle_state=ModelLifecycleState.ACTIVE,
                    drift_state=ModelDriftState.WARNING,
                    updated_at="2026-09-21T07:30:00+00:00",
                    drift_observed_at="2026-09-21T07:25:00+00:00",
                    evidence_refs=("evidence://warning/model-v17",),
                ),
            )

        with self.assertRaisesRegex(ModelLifecycleError, "newer drift observation"):
            validate_lifecycle_successor(
                quarantined,
                self._revision(
                    revision=2,
                    lifecycle_state=ModelLifecycleState.ACTIVE,
                    drift_state=ModelDriftState.STABLE,
                    updated_at="2026-09-21T07:30:00+00:00",
                    evidence_refs=("evidence://stable/model-v17",),
                ),
            )

        released = self._revision(
            revision=2,
            lifecycle_state=ModelLifecycleState.ACTIVE,
            drift_state=ModelDriftState.STABLE,
            updated_at="2026-09-21T07:30:00+00:00",
            drift_observed_at="2026-09-21T07:25:00+00:00",
            drift_valid_until="2026-09-21T09:25:00+00:00",
            evidence_refs=("evidence://stable/model-v17",),
            reason_code="quarantine_review_cleared",
        )
        validate_lifecycle_successor(quarantined, released)

    def test_contract_does_not_mint_execution_or_release_truth(self) -> None:
        payload_keys = set(self._revision().to_dict())
        for forbidden in (
            "execution_authorized",
            "real_money_execution",
            "human_tested",
            "nvda_verified",
            "v1_ready",
            "whole_product_complete",
        ):
            self.assertNotIn(forbidden, payload_keys)


if __name__ == "__main__":
    unittest.main()
