from __future__ import annotations

from autosport.release_truth import (
    MachineReleaseProof,
    ReleaseTruthAuditInput,
    ReleaseTruthClaims,
    audit_release_truth,
)


def _complete_machine_proofs() -> tuple[MachineReleaseProof, ...]:
    return (
        MachineReleaseProof("ci", True, "artifact://ci"),
        MachineReleaseProof(
            "windows_candidate",
            True,
            "artifact://windows-candidate",
        ),
        MachineReleaseProof("paper_replay", True, "artifact://paper-replay"),
    )


def _audit(claims: ReleaseTruthClaims):
    return audit_release_truth(
        ReleaseTruthAuditInput(
            release_id="release-candidate-caller-ref-falsifier",
            required_machine_proof_keys=(
                "ci",
                "windows_candidate",
                "paper_replay",
            ),
            machine_proofs=_complete_machine_proofs(),
            claims=claims,
        )
    )


def test_caller_created_refs_cannot_complete_physical_acceptance() -> None:
    try:
        claims = ReleaseTruthClaims(
            human_tested=True,
            human_test_evidence_ref="artifact://caller-created-human-label",
            nvda_verified=True,
            nvda_evidence_ref="artifact://caller-created-nvda-label",
        )
    except (TypeError, ValueError):
        # A repaired contract may reject unissued physical-evidence inputs at
        # construction time instead of carrying them into the audit.
        return

    audit = _audit(claims)

    assert not audit.physical_acceptance_evidence_complete
    assert not audit.release_handoff_evidence_complete
    assert not audit.truth_claims_evidence_safe


def test_caller_created_ref_cannot_make_real_money_claim_evidence_safe() -> None:
    try:
        claims = ReleaseTruthClaims(
            real_money_execution=True,
            real_money_evidence_ref="artifact://caller-created-real-money-label",
        )
    except (TypeError, ValueError):
        return

    audit = _audit(claims)

    assert not (audit.real_money_execution and audit.truth_claims_evidence_safe)
