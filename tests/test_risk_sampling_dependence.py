from dataclasses import replace
import json
import unittest

from autosport.risk_sampling_dependence import (
    IidSamplingOccurrence,
    RiskSamplingDependenceError,
    inspect_fixed_n_iid_occurrences,
    inspect_fixed_n_iid_sampling_structure,
    resolve_fixed_n_iid_sampling_authority,
)
from autosport.risk_sampling_membership import ResolvedFixedNRiskMembership


class RiskSamplingDependenceTests(unittest.TestCase):
    MEMBERS = ("run-001", "run-002", "run-003")
    DESIGN_SHA = "1" * 64
    PROTOCOL_SHA = "2" * 64
    DATASET_MANIFEST_SHA = "3" * 64
    FRAME_SHA = "4" * 64
    CAPITAL_SHA = "5" * 64
    STAKE_SHA = "6" * 64
    HORIZON_SHA = "7" * 64
    ROOT_SHA = "8" * 64

    def _membership(self):
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

    def _manifest(self, **overrides):
        payload = {
            "kind": "autosport-risk-iid-resample-with-replacement-v1",
            "experiment_id": "iid-risk-exp-001",
            "membership_design_sha256": self.DESIGN_SHA,
            "membership_protocol_record_sha256": "9" * 64,
            "membership_dataset_record_sha256": "a" * 64,
            "membership_causal_cutoff": "2026-09-01T00:00:00+00:00",
            "membership_precommitted_at": "2026-09-02T12:05:00+00:00",
            "membership_outcome_reveal_after": "2026-09-10T00:00:00+00:00",
            "research_protocol_id": "risk-fixed-n-protocol",
            "protocol_sha256": self.PROTOCOL_SHA,
            "dataset_snapshot_id": "risk-fixed-n-dataset",
            "dataset_manifest_sha256": self.DATASET_MANIFEST_SHA,
            "sampling_frame_sha256": self.FRAME_SHA,
            "initial_capital_state_sha256": self.CAPITAL_SHA,
            "stake_policy_sha256": self.STAKE_SHA,
            "horizon_sha256": self.HORIZON_SHA,
            "sampler_kind": "IID_RESAMPLE_WITH_REPLACEMENT_V1",
            "with_replacement": True,
            "rng_algorithm": "PCG64",
            "rng_version": "numpy-compatible-contract-v1",
            "randomization_root_sha256": self.ROOT_SHA,
            "planned_n": 3,
            "planned_member_ids": list(self.MEMBERS),
            "stopping_rule": "FIXED_N_NO_EARLY_STOP",
            "risk_scope": "SIMULATOR_DISTRIBUTION_ONLY",
            "randomization_authority": "PRODUCT_PRECOMMIT_REQUIRED",
        }
        payload.update(overrides)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _occurrences(self, structure):
        return tuple(
            IidSamplingOccurrence(
                member_id=member_id,
                member_index=index,
                stream_sha256=structure.member_stream_sha256[index],
                draw_transcript_sha256=f"{index + 11:064x}",
                initial_capital_state_sha256=self.CAPITAL_SHA,
                sampling_frame_sha256=self.FRAME_SHA,
                protocol_sha256=self.PROTOCOL_SHA,
                stake_policy_sha256=self.STAKE_SHA,
                horizon_sha256=self.HORIZON_SHA,
                complete=True,
            )
            for index, member_id in enumerate(self.MEMBERS)
        )

    def test_structural_design_is_deterministic_but_not_positive_iid_authority(self):
        membership = self._membership()
        first = inspect_fixed_n_iid_sampling_structure(
            membership,
            sampling_manifest_json=self._manifest(),
        )
        second = inspect_fixed_n_iid_sampling_structure(
            membership,
            sampling_manifest_json=self._manifest(),
        )
        self.assertEqual(first, second)
        self.assertEqual(first.planned_member_ids, self.MEMBERS)
        self.assertEqual(first.planned_n, 3)
        self.assertEqual(len(set(first.member_stream_sha256)), 3)
        self.assertFalse(first.iid_qualified)
        self.assertFalse(first.grants_real_money_authority)

    def test_rejects_without_replacement_and_adaptive_stopping(self):
        cases = (
            (
                self._manifest(with_replacement=False),
                "requires sampling with replacement",
            ),
            (
                self._manifest(stopping_rule="STOP_AFTER_ZERO_RUINS_20"),
                "forbids adaptive or early stopping",
            ),
        )
        for manifest, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                RiskSamplingDependenceError,
                message,
            ):
                inspect_fixed_n_iid_sampling_structure(
                    self._membership(),
                    sampling_manifest_json=manifest,
                )

    def test_rejects_wrong_sampler_and_scope(self):
        cases = (
            (
                self._manifest(sampler_kind="WITHOUT_REPLACEMENT"),
                "IID_RESAMPLE_WITH_REPLACEMENT_V1",
            ),
            (
                self._manifest(risk_scope="REAL_WORLD_RUIN_PROBABILITY"),
                "frozen simulator distribution",
            ),
        )
        for manifest, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                RiskSamplingDependenceError,
                message,
            ):
                inspect_fixed_n_iid_sampling_structure(
                    self._membership(),
                    sampling_manifest_json=manifest,
                )

    def test_rejects_member_relabeling_or_count_drift(self):
        cases = (
            (
                self._manifest(
                    planned_member_ids=["run-001", "run-003", "run-002"]
                ),
                "membership/order differs",
            ),
            (self._manifest(planned_n=2), "planned_n must equal"),
        )
        for manifest, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                RiskSamplingDependenceError,
                message,
            ):
                inspect_fixed_n_iid_sampling_structure(
                    self._membership(),
                    sampling_manifest_json=manifest,
                )

    def test_rejects_protocol_dataset_frame_or_membership_rebinding(self):
        cases = (
            (
                self._manifest(membership_design_sha256="b" * 64),
                "exact fixed-N membership design",
            ),
            (
                self._manifest(protocol_sha256="c" * 64),
                "protocol digest mismatch",
            ),
            (
                self._manifest(membership_protocol_record_sha256="b" * 64),
                "protocol record provenance mismatch",
            ),
            (
                self._manifest(membership_dataset_record_sha256="c" * 64),
                "dataset record provenance mismatch",
            ),
            (
                self._manifest(
                    membership_causal_cutoff="2026-08-31T23:00:00+00:00"
                ),
                "causal cutoff provenance mismatch",
            ),
            (
                self._manifest(
                    membership_precommitted_at="2026-09-03T12:05:00+00:00"
                ),
                "precommit chronology mismatch",
            ),
            (
                self._manifest(
                    membership_outcome_reveal_after="2026-09-11T00:00:00+00:00"
                ),
                "outcome reveal chronology mismatch",
            ),
            (
                self._manifest(dataset_manifest_sha256="d" * 64),
                "dataset manifest digest mismatch",
            ),
            (
                self._manifest(sampling_frame_sha256="e" * 64),
                "sampling frame identity mismatch",
            ),
        )
        for manifest, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(
                RiskSamplingDependenceError,
                message,
            ):
                inspect_fixed_n_iid_sampling_structure(
                    self._membership(),
                    sampling_manifest_json=manifest,
                )

    def test_parent_membership_provenance_cannot_alias_iid_identity(self):
        original = self._membership()
        original_manifest = self._manifest()
        original_structure = inspect_fixed_n_iid_sampling_structure(
            original,
            sampling_manifest_json=original_manifest,
        )

        variants = (
            (
                replace(original, protocol_record_sha256="b" * 64),
                {"membership_protocol_record_sha256": "b" * 64},
                "protocol record provenance mismatch",
            ),
            (
                replace(original, dataset_record_sha256="c" * 64),
                {"membership_dataset_record_sha256": "c" * 64},
                "dataset record provenance mismatch",
            ),
            (
                replace(
                    original,
                    causal_cutoff="2026-08-31T23:00:00+00:00",
                    precommitted_at="2026-09-03T12:05:00+00:00",
                    outcome_reveal_after="2026-09-11T00:00:00+00:00",
                ),
                {
                    "membership_causal_cutoff": "2026-08-31T23:00:00+00:00",
                    "membership_precommitted_at": "2026-09-03T12:05:00+00:00",
                    "membership_outcome_reveal_after": "2026-09-11T00:00:00+00:00",
                },
                "causal cutoff provenance mismatch",
            ),
        )

        for rebound_membership, rebound_manifest_fields, mismatch in variants:
            with self.subTest(mismatch=mismatch):
                with self.assertRaisesRegex(
                    RiskSamplingDependenceError,
                    mismatch,
                ):
                    inspect_fixed_n_iid_sampling_structure(
                        rebound_membership,
                        sampling_manifest_json=original_manifest,
                    )

                rebound_structure = inspect_fixed_n_iid_sampling_structure(
                    rebound_membership,
                    sampling_manifest_json=self._manifest(
                        **rebound_manifest_fields
                    ),
                )
                self.assertNotEqual(
                    original_structure.manifest_sha256,
                    rebound_structure.manifest_sha256,
                )
                self.assertNotEqual(original_structure, rebound_structure)

    def test_complete_occurrence_set_restarts_to_same_root(self):
        structure = inspect_fixed_n_iid_sampling_structure(
            self._membership(),
            sampling_manifest_json=self._manifest(),
        )
        occurrences = self._occurrences(structure)
        first = inspect_fixed_n_iid_occurrences(structure, occurrences)
        second = inspect_fixed_n_iid_occurrences(structure, occurrences)
        self.assertEqual(first, second)
        self.assertEqual(first.planned_member_ids, self.MEMBERS)
        self.assertFalse(first.iid_qualified)
        self.assertFalse(first.grants_real_money_authority)

    def test_incomplete_or_reordered_members_cannot_change_denominator(self):
        structure = inspect_fixed_n_iid_sampling_structure(
            self._membership(),
            sampling_manifest_json=self._manifest(),
        )
        occurrences = self._occurrences(structure)
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "all precommitted fixed-N members must complete",
        ):
            inspect_fixed_n_iid_occurrences(structure, occurrences[:2])

        reordered = (occurrences[1], occurrences[0], occurrences[2])
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "member_index must equal",
        ):
            inspect_fixed_n_iid_occurrences(structure, reordered)

    def test_rejects_rebound_stream_or_changed_initial_state(self):
        structure = inspect_fixed_n_iid_sampling_structure(
            self._membership(),
            sampling_manifest_json=self._manifest(),
        )
        occurrences = list(self._occurrences(structure))

        occurrences[1] = IidSamplingOccurrence(
            member_id=occurrences[1].member_id,
            member_index=1,
            stream_sha256=occurrences[0].stream_sha256,
            draw_transcript_sha256=occurrences[1].draw_transcript_sha256,
            initial_capital_state_sha256=self.CAPITAL_SHA,
            sampling_frame_sha256=self.FRAME_SHA,
            protocol_sha256=self.PROTOCOL_SHA,
            stake_policy_sha256=self.STAKE_SHA,
            horizon_sha256=self.HORIZON_SHA,
            complete=True,
        )
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "stream does not match",
        ):
            inspect_fixed_n_iid_occurrences(structure, tuple(occurrences))

        occurrences = list(self._occurrences(structure))
        occurrences[2] = IidSamplingOccurrence(
            member_id=occurrences[2].member_id,
            member_index=2,
            stream_sha256=occurrences[2].stream_sha256,
            draw_transcript_sha256=occurrences[2].draw_transcript_sha256,
            initial_capital_state_sha256="f" * 64,
            sampling_frame_sha256=self.FRAME_SHA,
            protocol_sha256=self.PROTOCOL_SHA,
            stake_policy_sha256=self.STAKE_SHA,
            horizon_sha256=self.HORIZON_SHA,
            complete=True,
        )
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "initial capital state differs",
        ):
            inspect_fixed_n_iid_occurrences(structure, tuple(occurrences))

    def test_unresolved_path_is_not_a_non_ruin_trial(self):
        structure = inspect_fixed_n_iid_sampling_structure(
            self._membership(),
            sampling_manifest_json=self._manifest(),
        )
        occurrences = list(self._occurrences(structure))
        current = occurrences[0]
        occurrences[0] = IidSamplingOccurrence(
            member_id=current.member_id,
            member_index=current.member_index,
            stream_sha256=current.stream_sha256,
            draw_transcript_sha256=current.draw_transcript_sha256,
            initial_capital_state_sha256=current.initial_capital_state_sha256,
            sampling_frame_sha256=current.sampling_frame_sha256,
            protocol_sha256=current.protocol_sha256,
            stake_policy_sha256=current.stake_policy_sha256,
            horizon_sha256=current.horizon_sha256,
            complete=False,
        )
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "unresolved or incomplete path",
        ):
            inspect_fixed_n_iid_occurrences(structure, tuple(occurrences))

    def test_positive_resolution_fails_closed_without_product_precommit(self):
        membership = self._membership()
        structure = inspect_fixed_n_iid_sampling_structure(
            membership,
            sampling_manifest_json=self._manifest(),
        )
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "membership lacks non-backdateable product-owned pre-outcome precommit authority",
        ):
            resolve_fixed_n_iid_sampling_authority(
                membership,
                sampling_manifest_json=self._manifest(),
                occurrences=self._occurrences(structure),
            )

    def test_stream_identity_rejects_crlf_delimiter_aliases(self):
        collision_manifests = (
            self._manifest(experiment_id="exp\n0\nmember"),
            self._manifest(planned_member_ids=["member\n1\nz", "run-002", "run-003"]),
            self._manifest(experiment_id="exp\r0\rmember"),
        )
        for manifest in collision_manifests:
            with self.subTest(manifest=manifest):
                with self.assertRaisesRegex(
                    RiskSamplingDependenceError,
                    "must not contain CR/LF delimiters",
                ):
                    inspect_fixed_n_iid_sampling_structure(
                        self._membership(),
                        sampling_manifest_json=manifest,
                    )

    def test_uppercase_sha_text_is_not_canonical_identity(self):
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "canonical SHA-256 hex string",
        ):
            inspect_fixed_n_iid_sampling_structure(
                self._membership(),
                sampling_manifest_json=self._manifest(
                    randomization_root_sha256="A" * 64
                ),
            )

    def test_noncanonical_json_and_unknown_fields_fail_closed(self):
        payload = json.loads(self._manifest())
        noncanonical = json.dumps(payload, sort_keys=True)
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "canonical JSON serialization",
        ):
            inspect_fixed_n_iid_sampling_structure(
                self._membership(),
                sampling_manifest_json=noncanonical,
            )

        payload["extra"] = "not-allowed"
        unknown = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        with self.assertRaisesRegex(
            RiskSamplingDependenceError,
            "fields do not match",
        ):
            inspect_fixed_n_iid_sampling_structure(
                self._membership(),
                sampling_manifest_json=unknown,
            )


if __name__ == "__main__":
    unittest.main()
