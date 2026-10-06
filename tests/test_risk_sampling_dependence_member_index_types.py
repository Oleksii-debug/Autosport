from decimal import Decimal
import unittest

from autosport.risk_sampling_dependence import (
    IidSamplingOccurrence,
    ResolvedFixedNIidSamplingStructure,
    RiskSamplingDependenceError,
    inspect_fixed_n_iid_occurrences,
)


class RiskSamplingDependenceMemberIndexTypeTests(unittest.TestCase):
    def _structure(self) -> ResolvedFixedNIidSamplingStructure:
        return ResolvedFixedNIidSamplingStructure(
            experiment_id="iid-index-type-falsifier",
            membership_design_sha256="1" * 64,
            membership_protocol_record_sha256="2" * 64,
            membership_dataset_record_sha256="3" * 64,
            membership_causal_cutoff="2026-09-01T00:00:00+00:00",
            membership_precommitted_at="2026-09-02T00:00:00+00:00",
            membership_outcome_reveal_after="2026-09-03T00:00:00+00:00",
            research_protocol_id="risk-index-type",
            protocol_sha256="4" * 64,
            dataset_snapshot_id="dataset-index-type",
            dataset_manifest_sha256="5" * 64,
            sampling_frame_sha256="6" * 64,
            initial_capital_state_sha256="7" * 64,
            stake_policy_sha256="8" * 64,
            horizon_sha256="9" * 64,
            rng_algorithm="PCG64",
            rng_version="v1",
            randomization_root_sha256="a" * 64,
            planned_member_ids=("run-001",),
            member_stream_sha256=("b" * 64,),
            manifest_sha256="c" * 64,
        )

    def _occurrence(self, member_index: object) -> IidSamplingOccurrence:
        return IidSamplingOccurrence(
            member_id="run-001",
            member_index=member_index,  # type: ignore[arg-type]
            stream_sha256="b" * 64,
            draw_transcript_sha256="d" * 64,
            initial_capital_state_sha256="7" * 64,
            sampling_frame_sha256="6" * 64,
            protocol_sha256="4" * 64,
            stake_policy_sha256="8" * 64,
            horizon_sha256="9" * 64,
            complete=True,
        )

    def test_equal_but_non_integer_member_indices_fail_closed(self) -> None:
        structure = self._structure()
        for forged_index in (0.0, Decimal("0"), False):
            with self.subTest(forged_index=repr(forged_index)), self.assertRaisesRegex(
                RiskSamplingDependenceError,
                "exact integer equal to its frozen member position",
            ):
                inspect_fixed_n_iid_occurrences(
                    structure,
                    (self._occurrence(forged_index),),
                )

    def test_exact_integer_member_index_remains_valid_structural_evidence(self) -> None:
        resolved = inspect_fixed_n_iid_occurrences(
            self._structure(),
            (self._occurrence(0),),
        )
        self.assertEqual(resolved.planned_member_ids, ("run-001",))
        self.assertFalse(resolved.iid_qualified)
        self.assertFalse(resolved.grants_real_money_authority)


if __name__ == "__main__":
    unittest.main()
