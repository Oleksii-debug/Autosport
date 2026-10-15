import json
import tempfile
import unittest
from pathlib import Path

from autosport.risk_sampling_membership import (
    RiskSamplingMembershipError,
    inspect_fixed_n_risk_membership_structure,
    resolve_fixed_n_risk_membership,
)
from autosport.scientific_registry import (
    DatasetSnapshot,
    ResearchProtocol,
    ScientificRegistry,
)
from autosport.strategy_experiment import ScientificProtocolBinding


class RiskSamplingMembershipTests(unittest.TestCase):
    PROTOCOL_ID = "risk-fixed-n-protocol"
    DATASET_ID = "risk-fixed-n-dataset"
    MANIFEST_SHA = "a" * 64
    FRAME_SHA = "b" * 64
    CUTOFF = "2026-09-01T00:00:00+00:00"
    DATASET_AVAILABLE = "2026-09-02T00:00:00+00:00"
    FROZEN_AT = "2026-09-02T12:00:00+00:00"
    PROTOCOL_AVAILABLE = "2026-09-02T12:05:00+00:00"
    REVEAL_AFTER = "2026-09-10T00:00:00+00:00"

    def _design(self, **overrides):
        payload = {
            "kind": "autosport-risk-fixed-n-run-membership-v1",
            "dataset_snapshot_id": self.DATASET_ID,
            "planned_run_ids": ["run-001", "run-002", "run-003"],
            "planned_n": 3,
            "sampling_frame_sha256": self.FRAME_SHA,
            "risk_method": "CLOPPER_PEARSON_ONE_SIDED",
            "dependence_qualification": "SEPARATE_REQUIRED",
        }
        payload.update(overrides)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _binding(self, evaluation_design):
        return ScientificProtocolBinding(
            research_protocol_id=self.PROTOCOL_ID,
            research_question_id="risk-question",
            research_question_sha256="c" * 64,
            hypothesis_id="risk-hypothesis",
            hypothesis_sha256="d" * 64,
            inclusion_criteria="exact frozen run cohort",
            exclusion_criteria="no post-freeze cohort edits",
            lawful_source_requirements="canonical product evidence only",
            causal_cutoff=self.CUTOFF,
            evaluation_design=evaluation_design,
            feature_set_version="risk-path-v1",
            uncertainty_method="one-sided exact Clopper-Pearson",
            multiple_comparison_control="separate familywise authority required",
            robustness_checks=("restart re-resolution",),
            random_seed_policy="not applicable",
            stopping_rule="fixed N; no early stopping",
            promotion_rule="risk evidence only; no direct promotion",
            expected_artifacts=("risk-path observations",),
            code_config_sha256="e" * 64,
            frozen_at_utc=self.FROZEN_AT,
        )

    def _registry(
        self,
        root: Path,
        *,
        design=None,
        dataset_manifest=None,
        protocol_manifest=None,
        dataset_available=None,
        protocol_available=None,
        reveal_after=REVEAL_AFTER,
        dataset_cutoff=None,
    ):
        registry = ScientificRegistry.initialize_pristine(root / "scientific_registry.json")
        dataset_manifest = dataset_manifest or self.MANIFEST_SHA
        protocol_manifest = protocol_manifest or self.MANIFEST_SHA
        registry.append(
            DatasetSnapshot(
                dataset_snapshot_id=self.DATASET_ID,
                manifest_sha256=dataset_manifest,
                source_identity="canonical-risk-run-cohort",
                license_identity="internal-product-evidence",
                causal_cutoff=dataset_cutoff or self.CUTOFF,
                available_at_utc=dataset_available or self.DATASET_AVAILABLE,
                outcome_reveal_after=reveal_after,
            )
        )
        registry.append(
            ResearchProtocol(
                binding=self._binding(design or self._design()),
                source_sha256="f" * 64,
                environment_sha256="1" * 64,
                dataset_manifest_sha256=protocol_manifest,
                available_at_utc=protocol_available or self.PROTOCOL_AVAILABLE,
            )
        )
        return registry.path

    def test_resolves_exact_membership_and_restarts_identically(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(Path(tmp))
            first = inspect_fixed_n_risk_membership_structure(
                path,
                research_protocol_id=self.PROTOCOL_ID,
                dataset_snapshot_id=self.DATASET_ID,
            )
            second = inspect_fixed_n_risk_membership_structure(
                path,
                research_protocol_id=self.PROTOCOL_ID,
                dataset_snapshot_id=self.DATASET_ID,
            )

            self.assertEqual(first, second)
            self.assertEqual(first.planned_run_ids, ("run-001", "run-002", "run-003"))
            self.assertEqual(first.planned_n, 3)
            self.assertFalse(first.iid_qualified)
            self.assertEqual(first.dataset_manifest_sha256, self.MANIFEST_SHA)
            self.assertEqual(len(first.design_sha256), 64)

    def test_unknown_protocol_cannot_be_replaced_by_caller_assertion(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(Path(tmp))
            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "missing canonical ResearchProtocol",
            ):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id="caller-invented-protocol",
                    dataset_snapshot_id=self.DATASET_ID,
                )

    def test_rejects_planned_n_mismatch_and_duplicate_relabeling(self):
        cases = (
            (self._design(planned_n=4), "planned_n must equal"),
            (
                self._design(
                    planned_run_ids=["run-001", "run-001", "run-003"],
                    planned_n=3,
                ),
                "must not contain duplicates",
            ),
        )
        for design, message in cases:
            with self.subTest(message=message), tempfile.TemporaryDirectory() as tmp:
                path = self._registry(Path(tmp), design=design)
                with self.assertRaisesRegex(RiskSamplingMembershipError, message):
                    inspect_fixed_n_risk_membership_structure(
                        path,
                        research_protocol_id=self.PROTOCOL_ID,
                        dataset_snapshot_id=self.DATASET_ID,
                    )

    def test_rejects_noncanonical_or_reordered_membership(self):
        with tempfile.TemporaryDirectory() as tmp:
            raw = self._design(
                planned_run_ids=["run-002", "run-001", "run-003"]
            )
            path = self._registry(Path(tmp), design=raw)
            with self.assertRaisesRegex(RiskSamplingMembershipError, "lexical order"):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )

        with tempfile.TemporaryDirectory() as tmp:
            noncanonical = json.dumps(
                json.loads(self._design()),
                ensure_ascii=False,
                sort_keys=True,
            )
            path = self._registry(Path(tmp), design=noncanonical)
            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "canonical JSON serialization",
            ):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )

    def test_rejects_dataset_or_manifest_rebinding(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(
                Path(tmp),
                design=self._design(dataset_snapshot_id="other-dataset"),
            )
            with self.assertRaisesRegex(RiskSamplingMembershipError, "dataset identity"):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(
                Path(tmp),
                dataset_manifest="2" * 64,
                protocol_manifest="3" * 64,
            )
            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "manifest identities differ",
            ):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )

    def test_requires_dataset_to_exist_before_protocol_precommit(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(
                Path(tmp),
                dataset_available="2026-09-03T00:00:00+00:00",
                protocol_available="2026-09-02T12:05:00+00:00",
            )
            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "before the exact DatasetSnapshot was available",
            ):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )

    def test_requires_precommit_strictly_before_outcome_reveal(self):
        for protocol_available in (
            "2026-09-10T00:00:00+00:00",
            "2026-09-10T00:00:01+00:00",
        ):
            with self.subTest(
                protocol_available=protocol_available
            ), tempfile.TemporaryDirectory() as tmp:
                path = self._registry(
                    Path(tmp),
                    protocol_available=protocol_available,
                )
                with self.assertRaisesRegex(
                    RiskSamplingMembershipError,
                    "strictly before outcome reveal",
                ):
                    inspect_fixed_n_risk_membership_structure(
                        path,
                        research_protocol_id=self.PROTOCOL_ID,
                        dataset_snapshot_id=self.DATASET_ID,
                    )

    def test_requires_outcome_reveal_and_exact_causal_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(Path(tmp), reveal_after=None)
            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "outcome_reveal_after is required",
            ):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )

        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(
                Path(tmp),
                dataset_cutoff="2026-08-31T00:00:00+00:00",
            )
            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "causal cutoffs differ",
            ):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )

    def test_refuses_to_turn_membership_into_dependence_authority(self):
        for dependence in ("IID", "INDEPENDENT", "CALLER_ASSERTED"):
            with self.subTest(
                dependence=dependence
            ), tempfile.TemporaryDirectory() as tmp:
                path = self._registry(
                    Path(tmp),
                    design=self._design(dependence_qualification=dependence),
                )
                with self.assertRaisesRegex(
                    RiskSamplingMembershipError,
                    "keep dependence qualification separate",
                ):
                    inspect_fixed_n_risk_membership_structure(
                        path,
                        research_protocol_id=self.PROTOCOL_ID,
                        dataset_snapshot_id=self.DATASET_ID,
                    )

    def test_requires_supported_fixed_n_method(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(
                Path(tmp),
                design=self._design(risk_method="NORMAL_APPROXIMATION"),
            )
            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "risk method is unsupported",
            ):
                inspect_fixed_n_risk_membership_structure(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )


    def test_rejects_reversed_registry_append_order_despite_backdated_timestamps(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry = ScientificRegistry.initialize_pristine(
                Path(tmp) / "scientific_registry.json"
            )
            registry.append(
                ResearchProtocol(
                    binding=self._binding(self._design()),
                    source_sha256="f" * 64,
                    environment_sha256="1" * 64,
                    dataset_manifest_sha256=self.MANIFEST_SHA,
                    available_at_utc=self.PROTOCOL_AVAILABLE,
                )
            )
            registry.append(
                DatasetSnapshot(
                    dataset_snapshot_id=self.DATASET_ID,
                    manifest_sha256=self.MANIFEST_SHA,
                    source_identity="canonical-risk-run-cohort",
                    license_identity="internal-product-evidence",
                    causal_cutoff=self.CUTOFF,
                    available_at_utc=self.DATASET_AVAILABLE,
                    outcome_reveal_after=self.REVEAL_AFTER,
                )
            )
            self.assertFalse(
                registry.causal_precedes(
                    "DatasetSnapshot",
                    self.DATASET_ID,
                    "ResearchProtocol",
                    self.PROTOCOL_ID,
                )
            )

            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "append order must prove DatasetSnapshot precedes ResearchProtocol",
            ):
                inspect_fixed_n_risk_membership_structure(
                    registry.path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )

    def test_positive_resolution_rejects_backdateable_registry_chronology(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = self._registry(Path(tmp))
            registry = ScientificRegistry(path)
            self.assertTrue(
                registry.causal_precedes(
                    "DatasetSnapshot",
                    self.DATASET_ID,
                    "ResearchProtocol",
                    self.PROTOCOL_ID,
                )
            )

            structural = inspect_fixed_n_risk_membership_structure(
                path,
                research_protocol_id=self.PROTOCOL_ID,
                dataset_snapshot_id=self.DATASET_ID,
            )
            self.assertFalse(structural.causal_precommit_proven)
            self.assertFalse(structural.iid_qualified)

            with self.assertRaisesRegex(
                RiskSamplingMembershipError,
                "non-backdateable product-owned pre-outcome chronology authority",
            ):
                resolve_fixed_n_risk_membership(
                    path,
                    research_protocol_id=self.PROTOCOL_ID,
                    dataset_snapshot_id=self.DATASET_ID,
                )


if __name__ == "__main__":
    unittest.main()
