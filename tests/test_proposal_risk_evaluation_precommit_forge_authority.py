from __future__ import annotations

import unittest
from dataclasses import fields
from pathlib import Path

import autosport.proposal_risk_evaluation_precommit_authority as authority
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

    def test_dispatch_guard_root_rebinding_fails_closed(self) -> None:
        original = authority._require_dispatch
        try:
            authority._require_dispatch = lambda: None
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "dispatch guard root changed",
            ):
                authority.resolve_product_proposal_risk_evaluation_precommit(
                    Path("/"),
                    binding_sha256="0" * 64,
                    target_sha256="0" * 64,
                    membership=None,
                    registry_path="unused",
                    sampling_manifest_json="{}",
                )
        finally:
            authority._require_dispatch = original

    def test_internal_helper_rebinding_fails_closed(self) -> None:
        for name in (
            "_workspace_path",
            "_current_economic_state",
            "_resolve_inputs",
            "_material",
            "_build",
            "_text",
            "_sha",
            "_decimal_text",
            "_canonical_json",
            "_digest",
        ):
            with self.subTest(name=name):
                original = getattr(authority, name)
                try:
                    setattr(authority, name, lambda *args, **kwargs: None)
                    with self.assertRaisesRegex(
                        authority.ProductProposalRiskEvaluationPrecommitError,
                        "internal helper authority changed",
                    ):
                        authority._REQUIRE_DISPATCH_ORIGINAL()
                finally:
                    setattr(authority, name, original)

    def test_internal_helper_witness_root_rebinding_fails_closed(self) -> None:
        original = authority._PROPOSAL_RISK_EVALUATION_PRECOMMIT_HELPER_WITNESSES
        try:
            authority._PROPOSAL_RISK_EVALUATION_PRECOMMIT_HELPER_WITNESSES = ()
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "internal helper witness root changed",
            ):
                authority._REQUIRE_DISPATCH_ORIGINAL()
        finally:
            authority._PROPOSAL_RISK_EVALUATION_PRECOMMIT_HELPER_WITNESSES = original


if __name__ == "__main__":
    unittest.main()
