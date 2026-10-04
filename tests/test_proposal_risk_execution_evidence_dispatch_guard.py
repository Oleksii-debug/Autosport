from __future__ import annotations

import unittest

import autosport.proposal_risk_execution_evidence_authority as evidence_authority
from autosport.proposal_risk_execution_evidence_authority import (
    ProductProposalRiskExecutionEvidenceError,
)


class ProductProposalRiskExecutionEvidenceDispatchGuardTests(unittest.TestCase):
    def test_public_deriver_is_package_sealed(self) -> None:
        self.assertIs(
            getattr(
                evidence_authority.derive_product_proposal_risk_execution_evidence,
                "_autosport_dispatch_sealed",
                False,
            ),
            True,
        )

    def test_helper_rebinding_is_rejected_before_input_dispatch(self) -> None:
        original = evidence_authority._mint
        try:
            evidence_authority._mint = lambda values: values
            with self.assertRaisesRegex(
                ProductProposalRiskExecutionEvidenceError,
                "helper _mint changed",
            ):
                evidence_authority.derive_product_proposal_risk_execution_evidence(
                    None,
                    (),
                    evaluated_at="2026-10-04T10:02:00+00:00",
                )
        finally:
            evidence_authority._mint = original

    def test_authority_type_rebinding_is_rejected_before_input_dispatch(self) -> None:
        original = evidence_authority.ProductProposalRiskExecutionEvidence
        try:
            evidence_authority.ProductProposalRiskExecutionEvidence = object
            with self.assertRaisesRegex(
                ProductProposalRiskExecutionEvidenceError,
                "result type was rebound",
            ):
                evidence_authority.derive_product_proposal_risk_execution_evidence(
                    None,
                    (),
                    evaluated_at="2026-10-04T10:02:00+00:00",
                )
        finally:
            evidence_authority.ProductProposalRiskExecutionEvidence = original

    def test_identity_binder_and_mint_key_are_not_module_capabilities(self) -> None:
        self.assertFalse(hasattr(evidence_authority, "_BIND_IDENTITY"))
        self.assertFalse(hasattr(evidence_authority, "_IDENTITY_PROVEN"))
        self.assertFalse(hasattr(evidence_authority, "_MINT_CAPABILITY"))
        with self.assertRaises(TypeError):
            evidence_authority._mint({})

    def test_module_minter_cannot_accept_a_caller_selected_capability(self) -> None:
        # This is the regression for the mutable-default escape hatch: the public
        # module minter must not retain the canonical expected token in its defaults.
        caller_capability = object()
        self.assertIsNone(evidence_authority._mint.__kwdefaults__)
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "mint capability is invalid",
        ):
            evidence_authority._mint(
                {},
                mint_capability=caller_capability,
            )

        # Even if caller code installs its own default, the sealed minter compares
        # against a closure-held canonical token and therefore still fails closed.
        original_defaults = evidence_authority._mint.__kwdefaults__
        try:
            evidence_authority._mint.__kwdefaults__ = {
                "mint_capability": caller_capability,
            }
            with self.assertRaisesRegex(
                ProductProposalRiskExecutionEvidenceError,
                "mint capability is invalid",
            ):
                evidence_authority._mint({})
        finally:
            evidence_authority._mint.__kwdefaults__ = original_defaults

    def test_precommit_positive_identity_descriptor_rebinding_is_rejected(self) -> None:
        precommit_type = evidence_authority.ProductProposalRiskEvaluationPrecommit
        original = precommit_type.__dict__["binding_identity_proven"]
        try:
            precommit_type.binding_identity_proven = property(lambda self: True)
            with self.assertRaisesRegex(
                ProductProposalRiskExecutionEvidenceError,
                "precommit descriptor binding_identity_proven changed",
            ):
                evidence_authority.derive_product_proposal_risk_execution_evidence(
                    None,
                    (),
                    evaluated_at="2026-10-04T10:02:00+00:00",
                )
        finally:
            precommit_type.binding_identity_proven = original

    def test_result_risk_truth_descriptor_rebinding_is_rejected(self) -> None:
        result_type = evidence_authority.ProductProposalRiskExecutionEvidence
        original = result_type.__dict__["proposal_target_risk_qualified"]
        try:
            result_type.proposal_target_risk_qualified = property(lambda self: True)
            with self.assertRaisesRegex(
                ProductProposalRiskExecutionEvidenceError,
                "result descriptor proposal_target_risk_qualified changed",
            ):
                evidence_authority.derive_product_proposal_risk_execution_evidence(
                    None,
                    (),
                    evaluated_at="2026-10-04T10:02:00+00:00",
                )
        finally:
            result_type.proposal_target_risk_qualified = original

    def test_result_execution_provenance_descriptor_rebinding_is_rejected(self) -> None:
        result_type = evidence_authority.ProductProposalRiskExecutionEvidence
        original = result_type.__dict__["product_execution_provenance_proven"]
        try:
            result_type.product_execution_provenance_proven = property(lambda self: True)
            with self.assertRaisesRegex(
                ProductProposalRiskExecutionEvidenceError,
                "result descriptor product_execution_provenance_proven changed",
            ):
                evidence_authority.derive_product_proposal_risk_execution_evidence(
                    None,
                    (),
                    evaluated_at="2026-10-04T10:02:00+00:00",
                )
        finally:
            result_type.product_execution_provenance_proven = original


if __name__ == "__main__":
    unittest.main()
