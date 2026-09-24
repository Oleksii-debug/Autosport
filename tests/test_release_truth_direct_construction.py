from __future__ import annotations

import pytest

from autosport.release_truth import ReleaseTruthAudit


def test_direct_release_audit_construction_cannot_mint_physical_truth() -> None:
    with pytest.raises(
        ValueError,
        match="cannot carry positive human/NVDA/real-money truth",
    ):
        ReleaseTruthAudit(
            evidence_id="a" * 64,
            missing_machine_proofs=(),
            failed_machine_proofs=(),
            truth_claim_problems=(),
            human_tested=True,
            human_test_evidence_ref="artifact://caller-human",
            nvda_verified=True,
            nvda_evidence_ref="artifact://caller-nvda",
            real_money_execution=False,
            real_money_evidence_ref=None,
            whole_product_complete_claimed=False,
        )


def test_direct_release_audit_construction_cannot_mint_real_money_truth() -> None:
    with pytest.raises(
        ValueError,
        match="cannot carry positive human/NVDA/real-money truth",
    ):
        ReleaseTruthAudit(
            evidence_id="b" * 64,
            missing_machine_proofs=(),
            failed_machine_proofs=(),
            truth_claim_problems=(),
            human_tested=False,
            human_test_evidence_ref=None,
            nvda_verified=False,
            nvda_evidence_ref=None,
            real_money_execution=True,
            real_money_evidence_ref="artifact://caller-real-money",
            whole_product_complete_claimed=False,
        )
