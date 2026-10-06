from __future__ import annotations

import unittest

from autosport.proposal_risk_target_authority import ProductProposalRiskTarget


class ProductProposalRiskTargetForgeAuthorityTests(unittest.TestCase):
    def test_object_new_forge_cannot_claim_product_identity(self) -> None:
        # __new__ rejects direct construction, but object.__new__ bypasses that
        # surface. A caller-minted exact instance must never claim that canonical
        # product re-resolution occurred merely by reading a positive property.
        forged = object.__new__(ProductProposalRiskTarget)

        self.assertFalse(
            forged.proposal_target_identity_proven,
            "caller-forged target must not claim canonical product re-resolution",
        )
        self.assertFalse(forged.proposal_target_counterfactual_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_real_money_authority)


if __name__ == "__main__":
    unittest.main()
