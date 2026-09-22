from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass


def _require_canonical_text(value: object, label: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty canonical string")
    return value


def _require_optional_canonical_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    return _require_canonical_text(value, label)


def _require_bool(value: object, label: str) -> bool:
    if type(value) is not bool:
        raise ValueError(f"{label} must be a bool")
    return value


def _require_unique_keys(value: object, label: str) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise ValueError(f"{label} must be a non-empty tuple")
    keys = tuple(_require_canonical_text(item, f"{label} item") for item in value)
    if len(keys) != len(set(keys)):
        raise ValueError(f"{label} must contain unique keys")
    return keys


@dataclass(frozen=True, slots=True)
class MachineReleaseProof:
    key: str
    passed: bool
    evidence_ref: str

    def __post_init__(self) -> None:
        _require_canonical_text(self.key, "key")
        _require_bool(self.passed, "passed")
        _require_canonical_text(self.evidence_ref, "evidence_ref")


@dataclass(frozen=True, slots=True)
class ReleaseTruthClaims:
    human_tested: bool = False
    human_test_evidence_ref: str | None = None
    nvda_verified: bool = False
    nvda_evidence_ref: str | None = None
    real_money_execution: bool = False
    real_money_evidence_ref: str | None = None
    whole_product_complete: bool = False

    def __post_init__(self) -> None:
        _require_bool(self.human_tested, "human_tested")
        _require_bool(self.nvda_verified, "nvda_verified")
        _require_bool(self.real_money_execution, "real_money_execution")
        _require_bool(self.whole_product_complete, "whole_product_complete")
        _require_optional_canonical_text(
            self.human_test_evidence_ref, "human_test_evidence_ref"
        )
        _require_optional_canonical_text(
            self.nvda_evidence_ref, "nvda_evidence_ref"
        )
        _require_optional_canonical_text(
            self.real_money_evidence_ref, "real_money_evidence_ref"
        )


@dataclass(frozen=True, slots=True)
class ReleaseTruthAuditInput:
    release_id: str
    required_machine_proof_keys: tuple[str, ...]
    machine_proofs: tuple[MachineReleaseProof, ...]
    claims: ReleaseTruthClaims

    def __post_init__(self) -> None:
        _require_canonical_text(self.release_id, "release_id")
        _require_unique_keys(
            self.required_machine_proof_keys, "required_machine_proof_keys"
        )
        if type(self.machine_proofs) is not tuple:
            raise ValueError("machine_proofs must be a tuple")
        if not all(isinstance(item, MachineReleaseProof) for item in self.machine_proofs):
            raise ValueError("machine_proofs must contain MachineReleaseProof values")
        keys = [item.key for item in self.machine_proofs]
        if len(keys) != len(set(keys)):
            raise ValueError("machine proof keys must be unique")
        if not isinstance(self.claims, ReleaseTruthClaims):
            raise ValueError("claims must be ReleaseTruthClaims")


@dataclass(frozen=True, slots=True, order=True)
class FailedMachineProof:
    key: str
    evidence_ref: str


@dataclass(frozen=True, slots=True, order=True)
class TruthClaimProblem:
    claim: str
    reason: str


@dataclass(frozen=True, slots=True)
class ReleaseTruthAudit:
    evidence_id: str
    missing_machine_proofs: tuple[str, ...]
    failed_machine_proofs: tuple[FailedMachineProof, ...]
    truth_claim_problems: tuple[TruthClaimProblem, ...]
    human_tested: bool
    human_test_evidence_ref: str | None
    nvda_verified: bool
    nvda_evidence_ref: str | None
    real_money_execution: bool
    real_money_evidence_ref: str | None
    whole_product_complete_claimed: bool

    @property
    def machine_evidence_complete(self) -> bool:
        return not self.missing_machine_proofs and not self.failed_machine_proofs

    @property
    def human_evidence_complete(self) -> bool:
        return self.human_tested and self.human_test_evidence_ref is not None

    @property
    def nvda_evidence_complete(self) -> bool:
        return self.nvda_verified and self.nvda_evidence_ref is not None

    @property
    def physical_acceptance_evidence_complete(self) -> bool:
        return self.human_evidence_complete and self.nvda_evidence_complete

    @property
    def release_handoff_evidence_complete(self) -> bool:
        return self.machine_evidence_complete and self.physical_acceptance_evidence_complete

    @property
    def truth_claims_evidence_safe(self) -> bool:
        return not self.truth_claim_problems

    @property
    def grants_release_authority(self) -> bool:
        return False

    @property
    def grants_execution_authority(self) -> bool:
        return False

    @property
    def proves_whole_product_complete(self) -> bool:
        return False


def _evidence_id(audit_input: ReleaseTruthAuditInput) -> str:
    claims = audit_input.claims
    payload = {
        "release_id": audit_input.release_id,
        "required_machine_proof_keys": sorted(
            audit_input.required_machine_proof_keys
        ),
        "machine_proofs": [
            {
                "key": proof.key,
                "passed": proof.passed,
                "evidence_ref": proof.evidence_ref,
            }
            for proof in sorted(audit_input.machine_proofs, key=lambda proof: proof.key)
        ],
        "claims": {
            "human_tested": claims.human_tested,
            "human_test_evidence_ref": claims.human_test_evidence_ref,
            "nvda_verified": claims.nvda_verified,
            "nvda_evidence_ref": claims.nvda_evidence_ref,
            "real_money_execution": claims.real_money_execution,
            "real_money_evidence_ref": claims.real_money_evidence_ref,
            "whole_product_complete": claims.whole_product_complete,
        },
    }
    encoded = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def audit_release_truth(audit_input: ReleaseTruthAuditInput) -> ReleaseTruthAudit:
    """Audit release claims without converting machine proof into human truth.

    Positive HUMAN_TESTED, NVDA_VERIFIED, and REAL_MONEY_EXECUTION inputs are
    requests to audit claimed truth, not trusted truth themselves. Evidence-reference
    text is descriptive metadata only: this module has no independent physical or
    real-execution evidence resolver, so positive claims remain unverified and the
    corresponding returned truth flags stay false. WHOLE_PRODUCT_COMPLETE is always
    outside this narrow audit authority. The returned audit grants neither release
    nor execution authority.
    """

    if not isinstance(audit_input, ReleaseTruthAuditInput):
        raise ValueError("audit_input must be a ReleaseTruthAuditInput")

    proof_by_key = {proof.key: proof for proof in audit_input.machine_proofs}
    required_keys = tuple(sorted(audit_input.required_machine_proof_keys))
    missing = tuple(key for key in required_keys if key not in proof_by_key)
    failed = tuple(
        FailedMachineProof(key, proof_by_key[key].evidence_ref)
        for key in required_keys
        if key in proof_by_key and not proof_by_key[key].passed
    )

    claims = audit_input.claims
    problems: list[TruthClaimProblem] = []
    if claims.human_tested:
        reason = (
            "positive human-tested truth requires its own evidence reference"
            if claims.human_test_evidence_ref is None
            else (
                "positive human-tested truth requires independently verified "
                "product-issued physical evidence; evidence reference text alone "
                "is not authority"
            )
        )
        problems.append(TruthClaimProblem("HUMAN_TESTED", reason))
    if claims.nvda_verified:
        reason = (
            "positive NVDA verification requires its own evidence reference"
            if claims.nvda_evidence_ref is None
            else (
                "positive NVDA verification requires independently verified "
                "product-issued physical evidence; evidence reference text alone "
                "is not authority"
            )
        )
        problems.append(TruthClaimProblem("NVDA_VERIFIED", reason))
    if claims.real_money_execution:
        reason = (
            "positive real-money execution truth requires its own evidence reference"
            if claims.real_money_evidence_ref is None
            else (
                "positive real-money execution truth requires independently verified "
                "product-issued execution evidence; evidence reference text alone "
                "is not authority"
            )
        )
        problems.append(TruthClaimProblem("REAL_MONEY_EXECUTION", reason))
    if claims.whole_product_complete:
        problems.append(
            TruthClaimProblem(
                "WHOLE_PRODUCT_COMPLETE",
                "whole-product completion is outside release-truth audit authority",
            )
        )

    return ReleaseTruthAudit(
        evidence_id=_evidence_id(audit_input),
        missing_machine_proofs=missing,
        failed_machine_proofs=failed,
        truth_claim_problems=tuple(problems),
        human_tested=False,
        human_test_evidence_ref=claims.human_test_evidence_ref,
        nvda_verified=False,
        nvda_evidence_ref=claims.nvda_evidence_ref,
        real_money_execution=False,
        real_money_evidence_ref=claims.real_money_evidence_ref,
        whole_product_complete_claimed=claims.whole_product_complete,
    )
