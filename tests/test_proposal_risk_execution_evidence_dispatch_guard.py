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


if __name__ == "__main__":
    unittest.main()
