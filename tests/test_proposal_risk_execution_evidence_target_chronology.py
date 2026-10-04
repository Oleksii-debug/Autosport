from __future__ import annotations

import unittest

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
