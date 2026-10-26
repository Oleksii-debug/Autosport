from __future__ import annotations

import unittest

from autosport.deployment_identity import (
    DeploymentIdentityError,
    DeploymentRestartIdentity,
    RestartIdentityVerification,
    verify_restart_identity,
)


class RestartIdentityVerificationConstructionTests(unittest.TestCase):
    SHA_A = "a" * 64
    SHA_B = "b" * 64
    SHA_C = "c" * 64
    SHA_D = "d" * 64
    CREATED = "2026-09-21T08:00:00+00:00"

    @classmethod
    def _initial(cls) -> DeploymentRestartIdentity:
        return DeploymentRestartIdentity(
            deployment_id="paper-campaign-20260921-a",
            generation=7,
            restart_sequence=0,
            strategy_version_id="strategy-v12",
            strategy_artifact_sha256=cls.SHA_A,
            model_version_id="model-v17",
            model_artifact_sha256=cls.SHA_B,
            config_sha256=cls.SHA_C,
            scientific_registry_sha256=cls.SHA_D,
            created_at=cls.CREATED,
            observed_at=cls.CREATED,
            predecessor_fingerprint_sha256=None,
            evidence_refs=("evidence://deployment/start/001",),
        )

    @classmethod
    def _restart(
        cls,
        previous: DeploymentRestartIdentity,
    ) -> DeploymentRestartIdentity:
        return DeploymentRestartIdentity(
            deployment_id=previous.deployment_id,
            generation=previous.generation,
            restart_sequence=previous.restart_sequence + 1,
            strategy_version_id=previous.strategy_version_id,
            strategy_artifact_sha256=previous.strategy_artifact_sha256,
            model_version_id=previous.model_version_id,
            model_artifact_sha256=previous.model_artifact_sha256,
            config_sha256=previous.config_sha256,
            scientific_registry_sha256=previous.scientific_registry_sha256,
            created_at=previous.created_at,
            observed_at="2026-09-21T08:05:00+00:00",
            predecessor_fingerprint_sha256=previous.fingerprint_sha256,
            evidence_refs=("evidence://deployment/restart/001",),
        )

    def test_factory_result_remains_coherent(self) -> None:
        previous = self._initial()
        observed = self._restart(previous)

        result = verify_restart_identity(previous, observed)

        self.assertIs(type(result), RestartIdentityVerification)
        self.assertTrue(result.accepted)
        self.assertEqual(result.reasons, ())
        self.assertEqual(
            result.expected_restart_sequence,
            result.observed_restart_sequence,
        )

    def test_direct_constructor_cannot_mint_accepted_noncontiguous_sequence(self) -> None:
        with self.assertRaises((DeploymentIdentityError, TypeError, ValueError)):
            RestartIdentityVerification(
                expected_fingerprint_sha256=self.SHA_A,
                observed_fingerprint_sha256=self.SHA_B,
                expected_restart_sequence=1,
                observed_restart_sequence=99,
                accepted=True,
                reasons=(),
            )

    def test_direct_constructor_cannot_mint_accepted_result_with_rejection_reasons(self) -> None:
        with self.assertRaises((DeploymentIdentityError, TypeError, ValueError)):
            RestartIdentityVerification(
                expected_fingerprint_sha256=self.SHA_A,
                observed_fingerprint_sha256=self.SHA_B,
                expected_restart_sequence=1,
                observed_restart_sequence=1,
                accepted=True,
                reasons=("restart_sequence_not_contiguous",),
            )

    def test_direct_constructor_cannot_mint_rejected_result_without_reason(self) -> None:
        with self.assertRaises((DeploymentIdentityError, TypeError, ValueError)):
            RestartIdentityVerification(
                expected_fingerprint_sha256=self.SHA_A,
                observed_fingerprint_sha256=self.SHA_B,
                expected_restart_sequence=1,
                observed_restart_sequence=1,
                accepted=False,
                reasons=(),
            )


if __name__ == "__main__":
    unittest.main()
