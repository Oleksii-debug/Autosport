from __future__ import annotations

import unittest

from autosport.model_lifecycle import (
    ModelDriftState,
    ModelLifecycleRevision,
    ModelLifecycleState,
    evaluate_model_eligibility,
    validate_lifecycle_successor,
)


class ModelLifecycleRevisionAvailabilityFalsifierTests(unittest.TestCase):
    CREATED = "2026-09-21T06:00:00+00:00"
    KNOWLEDGE_UNTIL = "2026-09-22T06:00:00+00:00"
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
            "updated_at": "2026-09-21T07:00:00+00:00",
            "knowledge_valid_until": cls.KNOWLEDGE_UNTIL,
            "drift_observed_at": "2026-09-21T06:55:00+00:00",
            "drift_valid_until": "2026-09-21T08:55:00+00:00",
            "evidence_refs": ("evidence://drift/model-v17/001",),
            "reason_code": "initial_qualification",
        }
        values.update(overrides)
        return ModelLifecycleRevision(**values)

    def test_revision_cannot_authorize_before_its_updated_at(self) -> None:
        revision = self._revision()

        before_publication = evaluate_model_eligibility(
            revision,
            evaluated_at="2026-09-21T06:58:00+00:00",
        )
        self.assertFalse(before_publication.eligible)
        self.assertIn(
            "lifecycle_revision_not_yet_available",
            before_publication.reasons,
        )

        at_publication = evaluate_model_eligibility(
            revision,
            evaluated_at=revision.updated_at,
        )
        self.assertTrue(at_publication.eligible)
        self.assertNotIn(
            "lifecycle_revision_not_yet_available",
            at_publication.reasons,
        )

    def test_quarantine_release_cannot_be_backdated_before_release_revision(self) -> None:
        quarantined = self._revision(
            lifecycle_state=ModelLifecycleState.QUARANTINED,
            drift_state=ModelDriftState.BREACH,
            reason_code="drift_threshold_breach",
        )
        released = self._revision(
            revision=2,
            lifecycle_state=ModelLifecycleState.ACTIVE,
            drift_state=ModelDriftState.STABLE,
            updated_at="2026-09-21T07:30:00+00:00",
            drift_observed_at="2026-09-21T07:25:00+00:00",
            drift_valid_until="2026-09-21T09:25:00+00:00",
            evidence_refs=("evidence://stable/model-v17/002",),
            reason_code="quarantine_review_cleared",
        )
        validate_lifecycle_successor(quarantined, released)

        before_release_revision = evaluate_model_eligibility(
            released,
            evaluated_at="2026-09-21T07:26:00+00:00",
        )
        self.assertFalse(before_release_revision.eligible)
        self.assertIn(
            "lifecycle_revision_not_yet_available",
            before_release_revision.reasons,
        )

        at_release_revision = evaluate_model_eligibility(
            released,
            evaluated_at=released.updated_at,
        )
        self.assertTrue(at_release_revision.eligible)


if __name__ == "__main__":
    unittest.main()
