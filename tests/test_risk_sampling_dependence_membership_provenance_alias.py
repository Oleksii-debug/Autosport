from dataclasses import replace
import json
import unittest

from autosport.risk_sampling_dependence import (
    inspect_fixed_n_iid_sampling_structure,
)
from autosport.risk_sampling_membership import ResolvedFixedNRiskMembership


class ParentMembershipProvenanceAliasTests(unittest.TestCase):
    MEMBERS = ("run-001", "run-002", "run-003")
    DESIGN_SHA = "1" * 64
    PROTOCOL_SHA = "2" * 64
    DATASET_MANIFEST_SHA = "3" * 64
    FRAME_SHA = "4" * 64

    def _membership(self) -> ResolvedFixedNRiskMembership:
        return ResolvedFixedNRiskMembership(
            research_protocol_id="risk-fixed-n-protocol",
            protocol_sha256=self.PROTOCOL_SHA,
            protocol_record_sha256="9" * 64,
            dataset_snapshot_id="risk-fixed-n-dataset",
            dataset_manifest_sha256=self.DATASET_MANIFEST_SHA,
            dataset_record_sha256="a" * 64,
            causal_cutoff="2026-09-01T00:00:00+00:00",
            outcome_reveal_after="2026-09-10T00:00:00+00:00",
            precommitted_at="2026-09-02T12:05:00+00:00",
            planned_run_ids=self.MEMBERS,
            sampling_frame_sha256=self.FRAME_SHA,
            design_sha256=self.DESIGN_SHA,
        )

    def _manifest(self) -> str:
        payload = {
            "kind": "autosport-risk-iid-resample-with-replacement-v1",
            "experiment_id": "iid-risk-exp-parent-provenance",
            "membership_design_sha256": self.DESIGN_SHA,
            "research_protocol_id": "risk-fixed-n-protocol",
            "protocol_sha256": self.PROTOCOL_SHA,
            "dataset_snapshot_id": "risk-fixed-n-dataset",
            "dataset_manifest_sha256": self.DATASET_MANIFEST_SHA,
            "sampling_frame_sha256": self.FRAME_SHA,
            "initial_capital_state_sha256": "5" * 64,
            "stake_policy_sha256": "6" * 64,
            "horizon_sha256": "7" * 64,
            "sampler_kind": "IID_RESAMPLE_WITH_REPLACEMENT_V1",
            "with_replacement": True,
            "rng_algorithm": "PCG64",
            "rng_version": "numpy-compatible-contract-v1",
            "randomization_root_sha256": "8" * 64,
            "planned_n": len(self.MEMBERS),
            "planned_member_ids": list(self.MEMBERS),
            "stopping_rule": "FIXED_N_NO_EARLY_STOP",
            "risk_scope": "SIMULATOR_DISTRIBUTION_ONLY",
            "randomization_authority": "PRODUCT_PRECOMMIT_REQUIRED",
        }
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def test_parent_record_or_chronology_provenance_cannot_alias_iid_identity(self):
        original = self._membership()
        manifest = self._manifest()
        original_structure = inspect_fixed_n_iid_sampling_structure(
            original,
            sampling_manifest_json=manifest,
        )

        variants = {
            "protocol_record": replace(
                original,
                protocol_record_sha256="b" * 64,
            ),
            "dataset_record": replace(
                original,
                dataset_record_sha256="c" * 64,
            ),
            "chronology": replace(
                original,
                causal_cutoff="2026-08-31T23:00:00+00:00",
                precommitted_at="2026-09-03T12:05:00+00:00",
                outcome_reveal_after="2026-09-11T00:00:00+00:00",
            ),
        }

        for label, rebound_membership in variants.items():
            with self.subTest(label=label):
                rebound_structure = inspect_fixed_n_iid_sampling_structure(
                    rebound_membership,
                    sampling_manifest_json=manifest,
                )
                self.assertNotEqual(
                    original_structure,
                    rebound_structure,
                    "distinct exact parent membership provenance must not "
                    "resolve to one IID sampling identity",
                )


if __name__ == "__main__":
    unittest.main()
