from __future__ import annotations

import unittest

from autosport.model_lifecycle import (
    ModelDriftState,
    ModelLifecycleError,
    ModelLifecycleRevision,
    ModelLifecycleState,
    validate_lifecycle_successor,
)


class ModelLifecycleDriftReversalFalsifierTests(unittest.TestCase):
    CREATED = "2026-09-21T06:00:00+00:00"
    KNOWLEDGE_UNTIL = "2026-09-22T06:00:00+00:00"
    DRIFT_OBSERVED = "2026-09-21T06:55:00+00:00"
    DRIFT_UNTIL = "2026-09-21T08:55:00+00:00"
    ARTIFACT_SHA = "a" * 64

    @classmethod
    def _revision(
        cls,
        *,
        revision: int,
        drift_state: ModelDriftState,
        updated_at: str,
        drift_observed_at: str | None = None,
        evidence_ref: str = "evidence://drift/model-v17/001",
    ) -> ModelLifecycleRevision:
        return ModelLifecycleRevision(
            model_version_id="model-v17",
            model_artifact_sha256=cls.ARTIFACT_SHA,
            revision=revision,
            lifecycle_state=ModelLifecycleState.ACTIVE,
            drift_state=drift_state,
            created_at=cls.CREATED,
            updated_at=updated_at,
            knowledge_valid_until=cls.KNOWLEDGE_UNTIL,
            drift_observed_at=drift_observed_at or cls.DRIFT_OBSERVED,
            drift_valid_until=cls.DRIFT_UNTIL,
            evidence_refs=(evidence_ref,),
            reason_code=(
                "drift_threshold_breach"
                if drift_state is ModelDriftState.BREACH
                else "stable_after_new_observation"
            ),
        )

    def test_breach_cannot_clear_to_stable_without_new_drift_observation(self) -> None:
        previous = self._revision(
            revision=1,
            drift_state=ModelDriftState.BREACH,
            updated_at="2026-09-21T07:00:00+00:00",
        )
        candidate = self._revision(
            revision=2,
            drift_state=ModelDriftState.STABLE,
            updated_at="2026-09-21T07:30:00+00:00",
        )

        # Both revisions are individually well-formed and preserve the exact
        # drift observation and expiry. The only semantic change is that the
        # later revision relabels the same observation from BREACH to STABLE.
        with self.assertRaises(ModelLifecycleError):
            validate_lifecycle_successor(previous, candidate)

    def test_newer_observation_can_support_a_stable_successor(self) -> None:
        previous = self._revision(
            revision=1,
            drift_state=ModelDriftState.BREACH,
            updated_at="2026-09-21T07:00:00+00:00",
        )
        candidate = self._revision(
            revision=2,
            drift_state=ModelDriftState.STABLE,
            updated_at="2026-09-21T07:30:00+00:00",
            drift_observed_at="2026-09-21T07:25:00+00:00",
            evidence_ref="evidence://drift/model-v17/002",
        )

        validate_lifecycle_successor(previous, candidate)


if __name__ == "__main__":
    unittest.main()
