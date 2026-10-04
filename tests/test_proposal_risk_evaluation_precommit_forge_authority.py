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
        self.assertFalse(forged.scientific_preoutcome_chronology_proven)
        self.assertFalse(forged.proposal_target_bound_after_scientific_precommit)
        self.assertFalse(forged.scientific_precommit_proves_proposal_execution_scope)
        self.assertFalse(forged.proposal_target_counterfactual_execution_proven)
        self.assertFalse(forged.risk_upper_bound_for_target)
        self.assertFalse(forged.grants_ticket_authority)
        self.assertFalse(forged.grants_real_money_authority)

    def test_precommit_has_no_caller_supplied_risk_result_field(self) -> None:
        names = {item.name for item in fields(ProductProposalRiskEvaluationPrecommit)}

        self.assertNotIn("upper_bound", names)
        self.assertNotIn("ruin_count", names)
        self.assertNotIn("result_sha256", names)
        self.assertNotIn("risk_target_scope", names)
        self.assertNotIn("stake_policy_sha256", names)
        self.assertNotIn("initial_capital_state_sha256", names)
        self.assertIn("scientific_risk_target_scope", names)
        self.assertIn("scientific_stake_policy_sha256", names)
        self.assertIn("scientific_initial_capital_state_sha256", names)
        self.assertIn("target_sha256", names)
        self.assertIn("scientific_precommit_sha256", names)
        self.assertIn("proposal_evaluation_scope", names)
        self.assertIn("membership_outcome_reveal_after", names)
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
            "_instant",
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

    def test_json_and_sha_dispatch_rebinding_fails_closed(self) -> None:
        original_json_dumps = authority.json.dumps
        original_sha256 = authority.hashlib.sha256
        try:
            authority.json.dumps = lambda *args, **kwargs: "{}"
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "dispatch changed",
            ):
                authority._REQUIRE_DISPATCH_ORIGINAL()
        finally:
            authority.json.dumps = original_json_dumps

        try:
            authority.hashlib.sha256 = lambda *args, **kwargs: object()
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "dispatch changed",
            ):
                authority._REQUIRE_DISPATCH_ORIGINAL()
        finally:
            authority.hashlib.sha256 = original_sha256

    def test_native_path_factory_output_is_accepted(self) -> None:
        workspace = Path.cwd().resolve()
        self.assertIs(type(workspace), authority._PATH_TYPE)
        self.assertEqual(authority._workspace_path(workspace), workspace.expanduser())

    def test_path_method_dispatch_rebinding_fails_closed(self) -> None:
        for name in ("expanduser", "is_absolute"):
            with self.subTest(name=name):
                original = getattr(authority._PATH_TYPE, name)
                try:
                    setattr(authority._PATH_TYPE, name, lambda *args, **kwargs: True)
                    with self.assertRaisesRegex(
                        authority.ProductProposalRiskEvaluationPrecommitError,
                        "pathlib authority changed",
                    ):
                        authority._REQUIRE_DISPATCH_ORIGINAL()
                finally:
                    setattr(authority._PATH_TYPE, name, original)

    def test_workspace_lock_transitive_dispatch_rebinding_fails_closed(self) -> None:
        for name in (
            "__init__",
            "acquire",
            "release",
            "__enter__",
            "__exit__",
            "_open_lock_handle",
            "_open_new_lock_handle",
            "_validate_existing_lock_path",
            "_validate_open_handle_identity",
            "_require_regular_file",
            "_require_single_link",
            "_lock_handle",
            "_unlock_handle",
        ):
            with self.subTest(name=name):
                original = authority.WorkspaceEconomicLock.__dict__[name]
                try:
                    setattr(
                        authority.WorkspaceEconomicLock,
                        name,
                        lambda *args, **kwargs: None,
                    )
                    with self.assertRaisesRegex(
                        authority.ProductProposalRiskEvaluationPrecommitError,
                        "WorkspaceEconomicLock authority changed",
                    ):
                        authority._REQUIRE_DISPATCH_ORIGINAL()
                finally:
                    setattr(authority.WorkspaceEconomicLock, name, original)

    def test_workspace_lock_witness_root_rebinding_fails_closed(self) -> None:
        original = authority._LOCK_METHOD_WITNESSES
        try:
            authority._LOCK_METHOD_WITNESSES = ()
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "dispatch changed",
            ):
                authority._REQUIRE_DISPATCH_ORIGINAL()
        finally:
            authority._LOCK_METHOD_WITNESSES = original

    def test_decision_record_and_ledger_internal_rebinding_fails_closed(self) -> None:
        original_record = authority.DecisionRecord.to_dict
        try:
            authority.DecisionRecord.to_dict = lambda self: {}
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "DecisionRecord authority changed",
            ):
                authority._REQUIRE_DISPATCH_ORIGINAL()
        finally:
            authority.DecisionRecord.to_dict = original_record

        original_ledger = authority.JsonlDecisionLedger._canonical_record
        try:
            authority.JsonlDecisionLedger._canonical_record = lambda *args, **kwargs: b"{}"
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "Decision Ledger authority changed",
            ):
                authority._REQUIRE_DISPATCH_ORIGINAL()
        finally:
            authority.JsonlDecisionLedger._canonical_record = original_ledger

    def test_protocol_constant_rebinding_fails_closed(self) -> None:
        original = authority._PROPOSAL_EVALUATION_SCOPE
        try:
            authority._PROPOSAL_EVALUATION_SCOPE = "FORGED_SCOPE"
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "dispatch changed",
            ):
                authority._REQUIRE_DISPATCH_ORIGINAL()
        finally:
            authority._PROPOSAL_EVALUATION_SCOPE = original

    def test_package_sealed_public_issuer_detects_rebinding(self) -> None:
        sealed = authority.issue_product_proposal_risk_evaluation_precommit
        original = authority.issue_product_proposal_risk_evaluation_precommit
        try:
            authority.issue_product_proposal_risk_evaluation_precommit = (
                lambda *args, **kwargs: None
            )
            with self.assertRaisesRegex(
                authority.ProductProposalRiskEvaluationPrecommitError,
                "public issuer was rebound",
            ):
                sealed(
                    Path("/"),
                    target_sha256="0" * 64,
                    membership=None,
                    registry_path="unused",
                    sampling_manifest_json="{}",
                )
        finally:
            authority.issue_product_proposal_risk_evaluation_precommit = original

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
