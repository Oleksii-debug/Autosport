import pytest

from autosport.release_truth import (
    MachineReleaseProof,
    ReleaseTruthAuditInput,
    ReleaseTruthClaims,
    audit_release_truth,
)


def proof(key: str, passed: bool = True) -> MachineReleaseProof:
    return MachineReleaseProof(
        key=key,
        passed=passed,
        evidence_ref=f"artifact://{key}",
    )


def audit_input(
    *,
    machine_proofs: tuple[MachineReleaseProof, ...],
    claims: ReleaseTruthClaims | None = None,
) -> ReleaseTruthAuditInput:
    return ReleaseTruthAuditInput(
        release_id="release-candidate-001",
        required_machine_proof_keys=("ci", "windows_candidate", "paper_replay"),
        machine_proofs=machine_proofs,
        claims=claims or ReleaseTruthClaims(),
    )


def complete_machine_proofs() -> tuple[MachineReleaseProof, ...]:
    return (proof("ci"), proof("windows_candidate"), proof("paper_replay"))


def test_machine_green_does_not_infer_human_or_nvda_truth() -> None:
    audit = audit_release_truth(
        audit_input(machine_proofs=complete_machine_proofs())
    )

    assert audit.machine_evidence_complete
    assert audit.human_tested is False
    assert audit.nvda_verified is False
    assert audit.human_evidence_complete is False
    assert audit.nvda_evidence_complete is False
    assert audit.physical_acceptance_evidence_complete is False
    assert audit.release_handoff_evidence_complete is False
    assert audit.truth_claims_evidence_safe
    assert audit.grants_release_authority is False
    assert audit.grants_execution_authority is False
    assert audit.proves_whole_product_complete is False


def test_caller_evidence_refs_cannot_complete_human_or_nvda_handoff() -> None:
    audit = audit_release_truth(
        audit_input(
            machine_proofs=complete_machine_proofs(),
            claims=ReleaseTruthClaims(
                human_tested=True,
                human_test_evidence_ref="artifact://caller-created-human-label",
                nvda_verified=True,
                nvda_evidence_ref="artifact://caller-created-nvda-label",
            ),
        )
    )

    assert audit.machine_evidence_complete
    assert audit.human_tested is False
    assert audit.nvda_verified is False
    assert audit.physical_acceptance_evidence_complete is False
    assert audit.release_handoff_evidence_complete is False
    assert audit.truth_claims_evidence_safe is False
    assert [item.claim for item in audit.truth_claim_problems] == [
        "HUMAN_TESTED",
        "NVDA_VERIFIED",
    ]
    assert audit.proves_whole_product_complete is False


def test_caller_real_money_ref_cannot_make_positive_truth_evidence_safe() -> None:
    audit = audit_release_truth(
        audit_input(
            machine_proofs=complete_machine_proofs(),
            claims=ReleaseTruthClaims(
                real_money_execution=True,
                real_money_evidence_ref="artifact://caller-created-real-money-label",
            ),
        )
    )

    assert audit.real_money_execution is False
    assert audit.truth_claims_evidence_safe is False
    assert [(item.claim, item.reason) for item in audit.truth_claim_problems] == [
        (
            "REAL_MONEY_EXECUTION",
            "positive real-money execution truth requires independently verified "
            "product-issued execution evidence; evidence reference text alone "
            "is not authority",
        )
    ]


def test_positive_manual_or_real_money_claims_without_evidence_fail_closed() -> None:
    audit = audit_release_truth(
        audit_input(
            machine_proofs=complete_machine_proofs(),
            claims=ReleaseTruthClaims(
                human_tested=True,
                nvda_verified=True,
                real_money_execution=True,
            ),
        )
    )

    assert [(item.claim, item.reason) for item in audit.truth_claim_problems] == [
        (
            "HUMAN_TESTED",
            "positive human-tested truth requires its own evidence reference",
        ),
        (
            "NVDA_VERIFIED",
            "positive NVDA verification requires its own evidence reference",
        ),
        (
            "REAL_MONEY_EXECUTION",
            "positive real-money execution truth requires its own evidence reference",
        ),
    ]
    assert audit.truth_claims_evidence_safe is False
    assert audit.release_handoff_evidence_complete is False


def test_whole_product_complete_remains_outside_audit_authority() -> None:
    audit = audit_release_truth(
        audit_input(
            machine_proofs=complete_machine_proofs(),
            claims=ReleaseTruthClaims(
                human_tested=True,
                human_test_evidence_ref="artifact://human",
                nvda_verified=True,
                nvda_evidence_ref="artifact://nvda",
                real_money_execution=True,
                real_money_evidence_ref="artifact://real-money",
                whole_product_complete=True,
            ),
        )
    )

    assert audit.release_handoff_evidence_complete is False
    assert audit.human_tested is False
    assert audit.nvda_verified is False
    assert audit.real_money_execution is False
    assert audit.whole_product_complete_claimed
    assert [item.claim for item in audit.truth_claim_problems] == [
        "HUMAN_TESTED",
        "NVDA_VERIFIED",
        "REAL_MONEY_EXECUTION",
        "WHOLE_PRODUCT_COMPLETE",
    ]
    assert audit.truth_claims_evidence_safe is False
    assert audit.proves_whole_product_complete is False


def test_missing_and_failed_machine_proofs_are_distinct() -> None:
    audit = audit_release_truth(
        audit_input(machine_proofs=(proof("ci"), proof("windows_candidate", False)))
    )

    assert audit.missing_machine_proofs == ("paper_replay",)
    assert [(item.key, item.evidence_ref) for item in audit.failed_machine_proofs] == [
        ("windows_candidate", "artifact://windows_candidate")
    ]
    assert audit.machine_evidence_complete is False
    assert audit.release_handoff_evidence_complete is False


def test_evidence_id_is_deterministic_across_machine_input_order() -> None:
    claims = ReleaseTruthClaims(
        human_tested=True,
        human_test_evidence_ref="artifact://human",
        nvda_verified=True,
        nvda_evidence_ref="artifact://nvda",
    )
    first = audit_release_truth(
        audit_input(machine_proofs=complete_machine_proofs(), claims=claims)
    )
    second = audit_release_truth(
        ReleaseTruthAuditInput(
            release_id="release-candidate-001",
            required_machine_proof_keys=(
                "paper_replay",
                "ci",
                "windows_candidate",
            ),
            machine_proofs=tuple(reversed(complete_machine_proofs())),
            claims=claims,
        )
    )

    assert first.evidence_id == second.evidence_id
    assert first.machine_evidence_complete == second.machine_evidence_complete


def test_non_required_machine_proof_does_not_mask_missing_required_proof() -> None:
    audit = audit_release_truth(
        audit_input(
            machine_proofs=(
                proof("ci"),
                proof("windows_candidate"),
                proof("unrelated_green_check"),
            )
        )
    )

    assert audit.missing_machine_proofs == ("paper_replay",)
    assert audit.machine_evidence_complete is False


def test_contract_rejects_duplicate_and_noncanonical_machine_identity() -> None:
    with pytest.raises(ValueError, match="unique keys"):
        ReleaseTruthAuditInput(
            release_id="release-candidate-001",
            required_machine_proof_keys=("ci", "ci"),
            machine_proofs=(),
            claims=ReleaseTruthClaims(),
        )

    with pytest.raises(ValueError, match="unique"):
        ReleaseTruthAuditInput(
            release_id="release-candidate-001",
            required_machine_proof_keys=("ci",),
            machine_proofs=(proof("ci"), proof("ci")),
            claims=ReleaseTruthClaims(),
        )

    with pytest.raises(ValueError, match="key"):
        MachineReleaseProof(
            key=" ci",
            passed=True,
            evidence_ref="artifact://ci",
        )

    with pytest.raises(ValueError, match="passed"):
        MachineReleaseProof(
            key="ci",
            passed=1,  # type: ignore[arg-type]
            evidence_ref="artifact://ci",
        )


def test_claims_reject_noncanonical_refs_and_non_boolean_truth() -> None:
    with pytest.raises(ValueError, match="human_test_evidence_ref"):
        ReleaseTruthClaims(
            human_tested=False,
            human_test_evidence_ref=" human ",
        )

    with pytest.raises(ValueError, match="nvda_verified"):
        ReleaseTruthClaims(nvda_verified=1)  # type: ignore[arg-type]
