from __future__ import annotations

import unittest

import autosport.proposal_risk_evaluation_precommit_authority as precommit_authority
import autosport.proposal_risk_execution_evidence_authority as execution_authority
from test_proposal_risk_execution_evidence_authority import (
    _canonical_precommit,
    _row_impl,
)


class ProposalRiskCapabilityTransferGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.precommit = _canonical_precommit(self)

    def _result(self):
        return execution_authority.derive_product_proposal_risk_execution_evidence(
            self.precommit,
            (
                _row_impl(self.precommit, "member-a", source="source-a"),
                _row_impl(self.precommit, "member-b", source="source-b"),
            ),
            evaluated_at="2026-10-04T10:02:00+00:00",
        )

    def test_canonical_precommit_still_binds_identity(self) -> None:
        self.assertTrue(self.precommit.binding_identity_proven)
        self.assertTrue(self.precommit.proposal_target_identity_proven)
        self.assertTrue(self.precommit.scientific_precommit_proven)

    def test_stolen_precommit_token_cannot_bind_forged_exact_instance(self) -> None:
        token = self.precommit._proposal_risk_evaluation_precommit_capability
        forged = object.__new__(
            precommit_authority.ProductProposalRiskEvaluationPrecommit
        )
        with self.assertRaisesRegex(
            AttributeError,
            "may be bound only by canonical product issuance",
        ):
            object.__setattr__(
                forged,
                "_proposal_risk_evaluation_precommit_capability",
                token,
            )
        self.assertFalse(forged.binding_identity_proven)

    def test_exposed_private_precommit_binder_cannot_bind_directly(self) -> None:
        forged = object.__new__(
            precommit_authority.ProductProposalRiskEvaluationPrecommit
        )
        binder = precommit_authority._build.__kwdefaults__["_bind"]
        with self.assertRaisesRegex(
            AttributeError,
            "may be bound only by canonical product issuance",
        ):
            binder(forged)
        self.assertFalse(forged.binding_identity_proven)

    def test_canonical_execution_result_still_binds_identity(self) -> None:
        result = self._result()
        self.assertTrue(result.execution_evidence_identity_proven)
        self.assertTrue(result.fixed_n_cohort_complete)
        self.assertTrue(result.statistical_bound_computed)
        self.assertFalse(result.product_execution_provenance_proven)
        self.assertFalse(result.proposal_target_risk_qualified)

    def test_stolen_execution_token_cannot_bind_forged_exact_instance(self) -> None:
        result = self._result()
        token = result._execution_evidence_capability
        forged = object.__new__(
            execution_authority.ProductProposalRiskExecutionEvidence
        )
        with self.assertRaisesRegex(
            AttributeError,
            "may be bound only by canonical product issuance",
        ):
            object.__setattr__(forged, "_execution_evidence_capability", token)
        self.assertFalse(forged.execution_evidence_identity_proven)
        self.assertFalse(forged.fixed_n_cohort_complete)
        self.assertFalse(forged.statistical_bound_computed)
        self.assertFalse(forged.proposal_target_risk_qualified)

    def test_capability_descriptors_hide_mutable_backing_slot_handles(self) -> None:
        precommit_descriptor = type(self.precommit).__dict__[
            "_proposal_risk_evaluation_precommit_capability"
        ]
        result_descriptor = type(self._result()).__dict__["_execution_evidence_capability"]
        for descriptor in (precommit_descriptor, result_descriptor):
            with self.subTest(descriptor=descriptor):
                self.assertTrue(
                    getattr(
                        descriptor,
                        "_autosport_proposal_risk_capability_gate",
                        False,
                    )
                )
                self.assertFalse(hasattr(descriptor, "_slot"))
                self.assertFalse(hasattr(descriptor, "slot"))


if __name__ == "__main__":
    unittest.main()
