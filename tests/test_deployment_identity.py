from __future__ import annotations

import copy
import unittest

from autosport.deployment_identity import (
    DeploymentIdentityError,
    DeploymentRestartIdentity,
    RestartIdentityVerification,
    validate_restart_successor,
    verify_restart_identity,
)


class DeploymentIdentityTests(unittest.TestCase):
    CREATED = "2026-09-21T08:00:00+00:00"
    SHA_A = "a" * 64
    SHA_B = "b" * 64
    SHA_C = "c" * 64
    SHA_D = "d" * 64

    @classmethod
    def _initial(cls, **overrides) -> DeploymentRestartIdentity:
        values = {
            "deployment_id": "paper-campaign-20260921-a",
            "generation": 7,
            "restart_sequence": 0,
            "strategy_version_id": "strategy-v12",
            "strategy_artifact_sha256": cls.SHA_A,
            "model_version_id": "model-v17",
            "model_artifact_sha256": cls.SHA_B,
            "config_sha256": cls.SHA_C,
            "scientific_registry_sha256": cls.SHA_D,
            "created_at": cls.CREATED,
            "observed_at": cls.CREATED,
            "predecessor_fingerprint_sha256": None,
            "evidence_refs": ("evidence://deployment/start/001",),
        }
        values.update(overrides)
        return DeploymentRestartIdentity(**values)

    @classmethod
    def _restart(
        cls,
        previous: DeploymentRestartIdentity,
        **overrides,
    ) -> DeploymentRestartIdentity:
        values = {
            "deployment_id": previous.deployment_id,
            "generation": previous.generation,
            "restart_sequence": previous.restart_sequence + 1,
            "strategy_version_id": previous.strategy_version_id,
            "strategy_artifact_sha256": previous.strategy_artifact_sha256,
            "model_version_id": previous.model_version_id,
            "model_artifact_sha256": previous.model_artifact_sha256,
            "config_sha256": previous.config_sha256,
            "scientific_registry_sha256": previous.scientific_registry_sha256,
            "created_at": previous.created_at,
            "observed_at": "2026-09-21T08:05:00+00:00",
            "predecessor_fingerprint_sha256": previous.fingerprint_sha256,
            "evidence_refs": ("evidence://deployment/restart/001",),
        }
        values.update(overrides)
        return DeploymentRestartIdentity(**values)

    def test_round_trip_and_fingerprint_bind_all_exact_artifact_identities(self) -> None:
        identity = self._initial()
        payload = identity.to_dict()
        restored = DeploymentRestartIdentity.from_dict(copy.deepcopy(payload))

        self.assertEqual(restored, identity)
        self.assertEqual(restored.fingerprint_sha256, identity.fingerprint_sha256)
        self.assertEqual(len(identity.fingerprint_sha256), 64)

        changed = self._initial(model_artifact_sha256="e" * 64)
        self.assertNotEqual(changed.fingerprint_sha256, identity.fingerprint_sha256)

    def test_strict_schema_and_json_types_fail_closed(self) -> None:
        payload = self._initial().to_dict()

        extra = dict(payload)
        extra["execution_authorized"] = True
        with self.assertRaisesRegex(DeploymentIdentityError, "exactly canonical fields"):
            DeploymentRestartIdentity.from_dict(extra)

        missing = dict(payload)
        missing.pop("config_sha256")
        with self.assertRaisesRegex(DeploymentIdentityError, "exactly canonical fields"):
            DeploymentRestartIdentity.from_dict(missing)

        bool_schema = dict(payload)
        bool_schema["schema_version"] = True
        with self.assertRaisesRegex(DeploymentIdentityError, "unsupported"):
            DeploymentRestartIdentity.from_dict(bool_schema)

        float_schema = dict(payload)
        float_schema["schema_version"] = 1.0
        with self.assertRaisesRegex(DeploymentIdentityError, "unsupported"):
            DeploymentRestartIdentity.from_dict(float_schema)

        bool_generation = dict(payload)
        bool_generation["generation"] = True
        with self.assertRaisesRegex(DeploymentIdentityError, "positive non-boolean"):
            DeploymentRestartIdentity.from_dict(bool_generation)

        wrong_predecessor_type = dict(payload)
        wrong_predecessor_type["restart_sequence"] = 1
        wrong_predecessor_type["observed_at"] = "2026-09-21T08:01:00+00:00"
        wrong_predecessor_type["predecessor_fingerprint_sha256"] = 123
        with self.assertRaisesRegex(DeploymentIdentityError, "JSON string or null"):
            DeploymentRestartIdentity.from_dict(wrong_predecessor_type)

    def test_initial_and_restart_causality_are_canonical(self) -> None:
        with self.assertRaisesRegex(DeploymentIdentityError, "must not name a predecessor"):
            self._initial(predecessor_fingerprint_sha256="f" * 64)
        with self.assertRaisesRegex(DeploymentIdentityError, "must equal created_at"):
            self._initial(observed_at="2026-09-21T08:00:01+00:00")
        with self.assertRaisesRegex(DeploymentIdentityError, "requires predecessor"):
            self._initial(
                restart_sequence=1,
                observed_at="2026-09-21T08:01:00+00:00",
                predecessor_fingerprint_sha256=None,
            )
        with self.assertRaisesRegex(DeploymentIdentityError, "strictly after created_at"):
            self._initial(
                restart_sequence=1,
                predecessor_fingerprint_sha256="f" * 64,
            )
        with self.assertRaisesRegex(DeploymentIdentityError, "sorted and unique"):
            self._initial(evidence_refs=("evidence://z", "evidence://a"))
        with self.assertRaisesRegex(DeploymentIdentityError, "must not be empty"):
            self._initial(evidence_refs=())

    def test_exact_restart_successor_is_accepted(self) -> None:
        previous = self._initial()
        observed = self._restart(previous)

        verification = verify_restart_identity(previous, observed)
        self.assertIsInstance(verification, RestartIdentityVerification)
        self.assertTrue(verification.accepted)
        self.assertEqual(verification.reasons, ())
        self.assertEqual(verification.expected_restart_sequence, 1)
        self.assertEqual(verification.observed_restart_sequence, 1)
        self.assertEqual(
            verification.expected_fingerprint_sha256,
            previous.fingerprint_sha256,
        )
        self.assertEqual(
            verification.observed_fingerprint_sha256,
            observed.fingerprint_sha256,
        )
        validate_restart_successor(previous, observed)

    def test_restart_fails_closed_on_generation_strategy_model_config_or_registry_change(self) -> None:
        previous = self._initial()
        mutations = {
            "generation": previous.generation + 1,
            "strategy_version_id": "strategy-v13",
            "strategy_artifact_sha256": "1" * 64,
            "model_version_id": "model-v18",
            "model_artifact_sha256": "2" * 64,
            "config_sha256": "3" * 64,
            "scientific_registry_sha256": "4" * 64,
        }

        for field, value in mutations.items():
            with self.subTest(field=field):
                observed = self._restart(previous, **{field: value})
                verification = verify_restart_identity(previous, observed)
                self.assertFalse(verification.accepted)
                self.assertIn(f"identity_mismatch:{field}", verification.reasons)
                with self.assertRaisesRegex(
                    DeploymentIdentityError,
                    f"identity_mismatch:{field}",
                ):
                    validate_restart_successor(previous, observed)

    def test_restart_fails_closed_on_wrong_predecessor_sequence_or_time(self) -> None:
        previous = self._initial()

        wrong_predecessor = self._restart(
            previous,
            predecessor_fingerprint_sha256="f" * 64,
        )
        result = verify_restart_identity(previous, wrong_predecessor)
        self.assertFalse(result.accepted)
        self.assertIn("predecessor_fingerprint_mismatch", result.reasons)

        skipped_sequence = self._restart(previous, restart_sequence=2)
        result = verify_restart_identity(previous, skipped_sequence)
        self.assertFalse(result.accepted)
        self.assertIn("restart_sequence_not_contiguous", result.reasons)

        first = self._restart(previous)
        second_same_time = self._restart(
            first,
            observed_at=first.observed_at,
            evidence_refs=("evidence://deployment/restart/002",),
        )
        result = verify_restart_identity(first, second_same_time)
        self.assertFalse(result.accepted)
        self.assertIn("observed_at_not_strictly_forward", result.reasons)

    def test_created_at_is_immutable_across_restart(self) -> None:
        previous = self._initial()
        observed = self._restart(
            previous,
            created_at="2026-09-21T07:59:00+00:00",
        )
        result = verify_restart_identity(previous, observed)
        self.assertFalse(result.accepted)
        self.assertIn("identity_mismatch:created_at", result.reasons)

    def test_restart_rejects_subclass_before_virtual_dispatch(self) -> None:
        class ForgedFingerprintIdentity(DeploymentRestartIdentity):
            @property
            def fingerprint_sha256(self) -> str:
                return "f" * 64

        previous = self._initial()
        forged_previous = ForgedFingerprintIdentity(
            deployment_id=previous.deployment_id,
            generation=previous.generation,
            restart_sequence=previous.restart_sequence,
            strategy_version_id=previous.strategy_version_id,
            strategy_artifact_sha256=previous.strategy_artifact_sha256,
            model_version_id=previous.model_version_id,
            model_artifact_sha256=previous.model_artifact_sha256,
            config_sha256=previous.config_sha256,
            scientific_registry_sha256=previous.scientific_registry_sha256,
            created_at=previous.created_at,
            observed_at=previous.observed_at,
            predecessor_fingerprint_sha256=previous.predecessor_fingerprint_sha256,
            evidence_refs=previous.evidence_refs,
        )
        observed = self._restart(forged_previous)

        with self.assertRaisesRegex(TypeError, "exact DeploymentRestartIdentity"):
            verify_restart_identity(forged_previous, observed)
        with self.assertRaisesRegex(TypeError, "exact DeploymentRestartIdentity"):
            validate_restart_successor(forged_previous, observed)

        canonical_observed = self._restart(previous)
        forged_observed = ForgedFingerprintIdentity(
            deployment_id=canonical_observed.deployment_id,
            generation=canonical_observed.generation,
            restart_sequence=canonical_observed.restart_sequence,
            strategy_version_id=canonical_observed.strategy_version_id,
            strategy_artifact_sha256=canonical_observed.strategy_artifact_sha256,
            model_version_id=canonical_observed.model_version_id,
            model_artifact_sha256=canonical_observed.model_artifact_sha256,
            config_sha256=canonical_observed.config_sha256,
            scientific_registry_sha256=canonical_observed.scientific_registry_sha256,
            created_at=canonical_observed.created_at,
            observed_at=canonical_observed.observed_at,
            predecessor_fingerprint_sha256=(
                canonical_observed.predecessor_fingerprint_sha256
            ),
            evidence_refs=canonical_observed.evidence_refs,
        )
        with self.assertRaisesRegex(TypeError, "exact DeploymentRestartIdentity"):
            verify_restart_identity(previous, forged_observed)

    def test_contract_does_not_mint_execution_promotion_or_release_truth(self) -> None:
        keys = set(self._initial().to_dict())
        for forbidden in (
            "execution_authorized",
            "real_money_execution",
            "promotion_authorized",
            "human_tested",
            "nvda_verified",
            "v1_ready",
            "whole_product_complete",
        ):
            self.assertNotIn(forbidden, keys)


if __name__ == "__main__":
    unittest.main()
