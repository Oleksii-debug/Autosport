from __future__ import annotations

import pytest

from autosport.release_truth import (
    MachineReleaseProof,
    ReleaseTruthAuditInput,
    ReleaseTruthClaims,
    audit_release_truth,
)


class HostileMachineReleaseProof(MachineReleaseProof):
    """Expose a value different from the frozen base slot through subclass dispatch."""

    def __init__(self) -> None:
        MachineReleaseProof.key.__set__(self, "ci")
        MachineReleaseProof.passed.__set__(self, False)
        MachineReleaseProof.evidence_ref.__set__(self, "artifact://failed-ci")

    @property
    def passed(self) -> bool:
        return True


class ShadowClaims(ReleaseTruthClaims):
    pass


class ShadowAuditInput(ReleaseTruthAuditInput):
    pass


def test_machine_proof_subclass_cannot_override_release_evidence_dispatch() -> None:
    hostile = HostileMachineReleaseProof()
    assert MachineReleaseProof.passed.__get__(hostile, HostileMachineReleaseProof) is False
    assert hostile.passed is True

    with pytest.raises(ValueError, match="exact MachineReleaseProof"):
        ReleaseTruthAuditInput(
            release_id="release-1",
            required_machine_proof_keys=("ci",),
            machine_proofs=(hostile,),
            claims=ReleaseTruthClaims(),
        )


def test_claims_subclass_is_not_release_truth_authority() -> None:
    with pytest.raises(ValueError, match="exact ReleaseTruthClaims"):
        ReleaseTruthAuditInput(
            release_id="release-1",
            required_machine_proof_keys=("ci",),
            machine_proofs=(MachineReleaseProof("ci", True, "artifact://ci"),),
            claims=ShadowClaims(),
        )


def test_audit_input_subclass_cannot_enter_release_truth_evaluator() -> None:
    hostile_input = ShadowAuditInput(
        release_id="release-1",
        required_machine_proof_keys=("ci",),
        machine_proofs=(MachineReleaseProof("ci", True, "artifact://ci"),),
        claims=ReleaseTruthClaims(),
    )

    with pytest.raises(ValueError, match="exact ReleaseTruthAuditInput"):
        audit_release_truth(hostile_input)
