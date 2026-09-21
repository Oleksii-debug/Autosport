from __future__ import annotations

import inspect

from autosport.forward_evidence_completeness import VerificationCode, VerificationResult


def _structural_pass() -> VerificationResult:
    return VerificationResult(
        ok=True,
        codes=(VerificationCode.PASS,),
        protocol_sha256="a" * 64,
        terminal_root_sha256="b" * 64,
        candidate_count=1,
    )


def test_structural_pass_is_machine_labeled_nonpromotion() -> None:
    result = _structural_pass()

    # A structural verifier may legitimately say PASS for the evidence universe
    # supplied to it, while still lacking product-owned proof that the external
    # source/candidate universe itself was exhaustive. That authority boundary
    # must travel in the machine result rather than live only in prose/docs.
    assert result.verification_scope == "STRUCTURAL_ONLY"
    assert result.external_universe_authority_resolved is False
    assert result.promotion_ready is False


def test_nonpromotion_truth_labels_are_not_caller_mintable() -> None:
    parameters = inspect.signature(VerificationResult).parameters

    # These are authority/truth outputs, not caller assertions. A caller that can
    # pass True into VerificationResult(...) could launder a structural PASS into
    # promotion-quality completeness without any product-owned universe witness.
    assert "verification_scope" not in parameters
    assert "external_universe_authority_resolved" not in parameters
    assert "promotion_ready" not in parameters
