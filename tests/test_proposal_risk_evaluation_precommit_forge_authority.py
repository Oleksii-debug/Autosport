from __future__ import annotations

import unittest
from dataclasses import fields

from autosport.proposal_risk_evaluation_precommit_authority import (
    ProductProposalRiskEvaluationPrecommit,
)


class ProductProposalRiskEvaluationPrecommitForgeTests(unittest.TestCase):
    def test_direct_construction_is_closed(self) -> None:
        with self.assertRaises(TypeError):
            ProductProposalRiskEvaluationPrecommit()

    def test_object_new_cannot_mint_positive_authority(self) -> None:
        forged = object.__new__(ProductProposalRiskEvaluationPrecommit)

        self.assertFalse(forged.binding_identity_proven)
        self.assertFalse(forged.proposal_target_identity_proven)
        self.assertFalse(forged.scientific_precommit_proven)
        self.assertFalse(forged.proposal_target_counterfactual_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_real_money_authority)

    def test_precommit_has_no_caller_supplied_risk_result_field(self) -> None:
        names = {item.name for item in fields(ProductProposalRiskEvaluationPrecommit)}

        self.assertNotIn("upper_bound", names)
        self.assertNotIn("ruin_count", names)
        self.assertNotIn("result_sha256", names)
        self.assertIn("target_sha256", names)
        self.assertIn("scientific_precommit_sha256", names)
        self.assertIn("binding_sha256", names)


if __name__ == "__main__":
    unittest.main()
