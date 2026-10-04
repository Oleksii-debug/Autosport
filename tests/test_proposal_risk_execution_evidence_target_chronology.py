from __future__ import annotations

import unittest
from decimal import Decimal
from pathlib import Path

import autosport.proposal_risk_evaluation_precommit_authority as precommit_authority
from autosport.proposal_risk_execution_evidence_authority import (
    ProductProposalRiskExecutionEvidenceError,
    derive_product_proposal_risk_execution_evidence,
)
from test_proposal_risk_execution_evidence_authority import (
    _canonical_precommit,
    _row_impl,
)


class ProductProposalRiskExecutionEvidenceTargetChronologyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.precommit = _canonical_precommit(self)

    def _row(self, member_id: str, **kwargs: object):
        return _row_impl(self.precommit, member_id, **kwargs)

    def _result(self):
        return derive_product_proposal_risk_execution_evidence(
            self.precommit,
            (
                self._row("member-a", source="source-a"),
                self._row("member-b", source="source-b"),
            ),
            evaluated_at="2026-10-04T10:02:00+00:00",
        )

    def test_product_precommit_slots_reject_object_setattr_mutation(self) -> None:
        original = self.precommit.ruin_threshold
        with self.assertRaisesRegex(AttributeError, "write-once product evidence"):
            object.__setattr__(self.precommit, "ruin_threshold", Decimal("999"))
        self.assertEqual(self.precommit.ruin_threshold, original)
        self.assertTrue(self.precommit.binding_identity_proven)

    def test_precommit_issuer_rejects_binding_descriptor_rebinding(self) -> None:
        binding_type = precommit_authority.ProductProposalRiskEvaluationPrecommit
        original = binding_type.__dict__["ruin_threshold"]
        try:
            binding_type.ruin_threshold = property(lambda self: Decimal("0"))
            with self.assertRaisesRegex(
                precommit_authority.ProductProposalRiskEvaluationPrecommitError,
                "binding descriptor ruin_threshold changed",
            ):
                precommit_authority.issue_product_proposal_risk_evaluation_precommit(
                    Path("/"),
                    target_sha256="0" * 64,
                    membership=None,
                    registry_path="unused",
                    sampling_manifest_json="{}",
                )
        finally:
            binding_type.ruin_threshold = original

    def test_member_assertion_slots_reject_object_setattr_mutation(self) -> None:
        row = self._row("member-a", source="source-a")
        original = row.minimum_equity
        with self.assertRaisesRegex(AttributeError, "write-once product evidence"):
            object.__setattr__(row, "minimum_equity", Decimal("0"))
        self.assertEqual(row.minimum_equity, original)

    def test_product_result_slots_reject_object_setattr_mutation(self) -> None:
        result = self._result()
        original = result.ruin_probability_upper_bound
        with self.assertRaisesRegex(AttributeError, "write-once product evidence"):
            object.__setattr__(
                result,
                "ruin_probability_upper_bound",
                Decimal("0"),
            )
        self.assertEqual(result.ruin_probability_upper_bound, original)
        self.assertTrue(result.execution_evidence_identity_proven)
        self.assertFalse(result.proposal_target_risk_qualified)

    def test_member_assertion_after_reveal_but_before_target_is_rejected(self) -> None:
        pre_target = self._row(
            "member-a",
            source="source-a",
            observed_at="2026-09-18T13:19:59+00:00",
        )
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "predates the proposal target decision",
        ):
            derive_product_proposal_risk_execution_evidence(
                self.precommit,
                (pre_target, self._row("member-b", source="source-b")),
                evaluated_at="2026-10-04T10:02:00+00:00",
            )

    def test_evaluation_after_reveal_but_before_target_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ProductProposalRiskExecutionEvidenceError,
            "evaluation cannot precede the proposal target decision",
        ):
            derive_product_proposal_risk_execution_evidence(
                self.precommit,
                (
                    self._row("member-a", source="source-a"),
                    self._row("member-b", source="source-b"),
                ),
                evaluated_at="2026-09-18T13:19:59+00:00",
            )


if __name__ == "__main__":
    unittest.main()
