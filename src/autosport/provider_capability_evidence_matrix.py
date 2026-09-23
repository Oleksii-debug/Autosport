"""Versioned, fail-honest provider capability evidence matrix.

Composes current-main BookmakerCapabilityProfile and BookmakerIntegrationEvidence.
No provider I/O, credentials, execution, settlement, or money-moving authority.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from hashlib import sha256
import json
from typing import Iterable
from weakref import WeakValueDictionary, finalize

from .bookmaker_capability import (
    BookmakerCapability,
    BookmakerCapabilityProfile,
    BookmakerCapabilityState,
)
from .bookmaker_integration_boundary import BookmakerIntegrationEvidence

SCHEMA_VERSION = 1


class ProviderCapabilityEvidenceMatrixError(ValueError):
    pass


class ProviderCapabilityTruthGrade(str, Enum):
    UNKNOWN_UNPROVEN = "unknown_unproven"
    DECLARED_DOCUMENTED = "declared_documented"
    CONFIGURED = "configured"
    AUTHENTICATED_READ_PROVEN = "authenticated_read_proven"
    WRITE_PERMISSION_PROVEN = "write_permission_proven"
    OBSERVED_OPERATIONAL = "observed_operational"
    DEGRADED_OR_DELAYED = "degraded_or_delayed"
    REVOKED_OR_UNAVAILABLE = "revoked_or_unavailable"


_WRITE = frozenset(
    {
        BookmakerCapability.PLACE_BET,
        BookmakerCapability.CANCEL_BET,
        BookmakerCapability.CASHOUT,
    }
)
_CURRENT = frozenset(
    {
        ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN,
        ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN,
        ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL,
    }
)
_REQUIRES_SUPPORTED = frozenset(
    {
        ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN,
        ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN,
        ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL,
        ProviderCapabilityTruthGrade.DEGRADED_OR_DELAYED,
    }
)


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderCapabilityEvidenceMatrixError(
            f"{field} must be a non-empty trimmed string"
        )
    return value


def _hash(value: object, field: str) -> str:
    value = _text(value, field)
    if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
        raise ProviderCapabilityEvidenceMatrixError(
            f"{field} must be a lowercase SHA-256 digest"
        )
    return value


def _time(value: object, field: str) -> datetime:
    value = _text(value, field)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise ProviderCapabilityEvidenceMatrixError(f"{field} must be ISO-8601") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ProviderCapabilityEvidenceMatrixError(f"{field} must include timezone")
    return parsed


def _scope(values: object, field: str) -> tuple[str, ...]:
    if type(values) is not tuple:
        raise ProviderCapabilityEvidenceMatrixError(f"{field} must be a tuple")
    normalized = tuple(_text(value, field) for value in values)
    if normalized != tuple(sorted(normalized)):
        raise ProviderCapabilityEvidenceMatrixError(f"{field} must be sorted")
    if len(set(normalized)) != len(normalized):
        raise ProviderCapabilityEvidenceMatrixError(f"{field} must be unique")
    return normalized


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProviderCapabilityEvidence:
    capability: BookmakerCapability
    profile_state: BookmakerCapabilityState
    grade: ProviderCapabilityTruthGrade
    profile_id: str
    integration_evidence_id: str
    environment: str | None = None
    application_mode: str | None = None
    observed_at: str | None = None
    expires_at: str | None = None
    evidence_ref: str | None = None
    evidence_sha256: str | None = None
    endpoint_operation: str | None = None
    sport_scope: tuple[str, ...] = ()
    market_scope: tuple[str, ...] = ()
    quality_constraint: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.capability) is not BookmakerCapability:
            raise ProviderCapabilityEvidenceMatrixError("capability must be exact enum")
        if type(self.profile_state) is not BookmakerCapabilityState:
            raise ProviderCapabilityEvidenceMatrixError("profile_state must be exact enum")
        if type(self.grade) is not ProviderCapabilityTruthGrade:
            raise ProviderCapabilityEvidenceMatrixError("grade must be exact enum")
        _hash(self.profile_id, "profile_id")
        _hash(self.integration_evidence_id, "integration_evidence_id")
        _scope(self.sport_scope, "sport_scope")
        _scope(self.market_scope, "market_scope")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ProviderCapabilityEvidenceMatrixError("schema_version must be exactly 1")

        if self.grade is ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN:
            if self.environment is not None or self.application_mode is not None:
                raise ProviderCapabilityEvidenceMatrixError(
                    "unknown evidence cannot carry environment/application scope"
                )
            if any(
                value is not None
                for value in (
                    self.observed_at,
                    self.expires_at,
                    self.evidence_ref,
                    self.evidence_sha256,
                    self.endpoint_operation,
                    self.quality_constraint,
                )
            ) or self.sport_scope or self.market_scope:
                raise ProviderCapabilityEvidenceMatrixError(
                    "unknown evidence cannot carry positive assertions"
                )
            return

        _text(self.environment, "environment")
        _text(self.application_mode, "application_mode")
        if (
            self.grade in _REQUIRES_SUPPORTED
            and self.profile_state is not BookmakerCapabilityState.SUPPORTED
        ):
            raise ProviderCapabilityEvidenceMatrixError(
                "positive/degraded evidence requires profile_state=supported"
            )

        observed = _time(self.observed_at, "observed_at")
        _text(self.evidence_ref, "evidence_ref")
        _hash(self.evidence_sha256, "evidence_sha256")
        _text(self.endpoint_operation, "endpoint_operation")
        if self.expires_at is not None:
            if _time(self.expires_at, "expires_at") <= observed:
                raise ProviderCapabilityEvidenceMatrixError(
                    "expires_at must be later than observed_at"
                )
        elif self.grade in _CURRENT:
            raise ProviderCapabilityEvidenceMatrixError(
                f"{self.grade.value} requires expires_at"
            )

        if (
            self.grade is ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN
            and self.capability in _WRITE
        ):
            raise ProviderCapabilityEvidenceMatrixError(
                "authenticated-read evidence cannot qualify write capability"
            )
        if (
            self.grade is ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN
            and self.capability not in _WRITE
        ):
            raise ProviderCapabilityEvidenceMatrixError(
                "write-permission evidence requires write capability"
            )
        if self.grade is ProviderCapabilityTruthGrade.DEGRADED_OR_DELAYED:
            _text(self.quality_constraint, "quality_constraint")
        elif self.quality_constraint is not None:
            _text(self.quality_constraint, "quality_constraint")

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_canonical_dict())

    def is_current(self, at_time: str) -> bool:
        at = _time(at_time, "at_time")
        if self.grade is ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN:
            return False
        observed = _time(self.observed_at, "observed_at")
        if at < observed:
            return False
        return self.expires_at is None or at <= _time(self.expires_at, "expires_at")

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "capability": self.capability.value,
            "profile_state": self.profile_state.value,
            "grade": self.grade.value,
            "profile_id": self.profile_id,
            "integration_evidence_id": self.integration_evidence_id,
            "environment": self.environment,
            "application_mode": self.application_mode,
            "observed_at": self.observed_at,
            "expires_at": self.expires_at,
            "evidence_ref": self.evidence_ref,
            "evidence_sha256": self.evidence_sha256,
            "endpoint_operation": self.endpoint_operation,
            "sport_scope": list(self.sport_scope),
            "market_scope": list(self.market_scope),
            "quality_constraint": self.quality_constraint,
            "schema_version": self.schema_version,
        }


_ISSUED_EVIDENCE: WeakValueDictionary[int, ProviderCapabilityEvidence] = WeakValueDictionary()
_ISSUED_EVIDENCE_SEALS: dict[int, str] = {}


def issue_provider_capability_evidence(
    *,
    capability: BookmakerCapability,
    profile_state: BookmakerCapabilityState,
    grade: ProviderCapabilityTruthGrade,
    profile_id: str,
    integration_evidence_id: str,
    environment: str | None = None,
    application_mode: str | None = None,
    observed_at: str | None = None,
    expires_at: str | None = None,
    evidence_ref: str | None = None,
    evidence_sha256: str | None = None,
    endpoint_operation: str | None = None,
    sport_scope: tuple[str, ...] = (),
    market_scope: tuple[str, ...] = (),
    quality_constraint: str | None = None,
) -> ProviderCapabilityEvidence:
    """Issue one exact in-process evidence object after structural validation.

    Weak/negative facts may be issued here after structural validation. Current
    observed authority is deliberately not generically issuable: authenticated reads,
    operational observations, and write permission all require a provider-specific
    sealed upstream verifier. Caller refs, hashes, endpoint names, or exact-object
    identity cannot prove that an authenticated/provider operation actually occurred.
    Reconstructing/copying a dataclass does not recreate issuance authority, and restart
    time alone cannot renew freshness.
    """

    fact = ProviderCapabilityEvidence(
        capability=capability,
        profile_state=profile_state,
        grade=grade,
        profile_id=profile_id,
        integration_evidence_id=integration_evidence_id,
        environment=environment,
        application_mode=application_mode,
        observed_at=observed_at,
        expires_at=expires_at,
        evidence_ref=evidence_ref,
        evidence_sha256=evidence_sha256,
        endpoint_operation=endpoint_operation,
        sport_scope=sport_scope,
        market_scope=market_scope,
        quality_constraint=quality_constraint,
    )
    if fact.grade is ProviderCapabilityTruthGrade.AUTHENTICATED_READ_PROVEN:
        raise ProviderCapabilityEvidenceMatrixError(
            "authenticated-read current authority requires sealed upstream provider verification"
        )
    if fact.grade is ProviderCapabilityTruthGrade.WRITE_PERMISSION_PROVEN or (
        fact.grade is ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL
        and fact.capability in _WRITE
    ):
        raise ProviderCapabilityEvidenceMatrixError(
            "write-capability current authority requires sealed upstream provider verification"
        )
    if fact.grade is ProviderCapabilityTruthGrade.OBSERVED_OPERATIONAL:
        raise ProviderCapabilityEvidenceMatrixError(
            "observed-operational current authority requires sealed upstream provider verification"
        )
    issuance_key = id(fact)
    _ISSUED_EVIDENCE[issuance_key] = fact
    _ISSUED_EVIDENCE_SEALS[issuance_key] = fact.evidence_id
    finalize(fact, _ISSUED_EVIDENCE_SEALS.pop, issuance_key, None)
    return fact


def _is_product_issued(fact: ProviderCapabilityEvidence) -> bool:
    issuance_key = id(fact)
    if _ISSUED_EVIDENCE.get(issuance_key) is not fact:
        return False
    original_seal = _ISSUED_EVIDENCE_SEALS.get(issuance_key)
    if original_seal is None:
        return False
    try:
        return fact.evidence_id == original_seal
    except (
        AttributeError,
        ProviderCapabilityEvidenceMatrixError,
        TypeError,
        ValueError,
    ):
        return False


_ISSUED_MATRICES: WeakValueDictionary[int, object] = WeakValueDictionary()
_ISSUED_MATRIX_SEALS: dict[int, str] = {}


@dataclass(frozen=True, slots=True, weakref_slot=True)
class ProviderCapabilityEvidenceMatrix:
    profile: BookmakerCapabilityProfile
    integration: BookmakerIntegrationEvidence
    environment: str
    application_mode: str
    matrix_version: int
    facts: tuple[ProviderCapabilityEvidence, ...]
    as_of: str
    matrix_ref: str
    predecessor_matrix_id: str | None = None
    schema_version: int = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if type(self.profile) is not BookmakerCapabilityProfile:
            raise ProviderCapabilityEvidenceMatrixError("profile must be exact profile")
        if type(self.integration) is not BookmakerIntegrationEvidence:
            raise ProviderCapabilityEvidenceMatrixError("integration must be exact evidence")
        self.integration.verify_profile(self.profile)
        _text(self.environment, "environment")
        _text(self.application_mode, "application_mode")
        _text(self.matrix_ref, "matrix_ref")
        if type(self.matrix_version) is not int or self.matrix_version < 1:
            raise ProviderCapabilityEvidenceMatrixError("matrix_version must be positive int")
        if type(self.schema_version) is not int or self.schema_version != SCHEMA_VERSION:
            raise ProviderCapabilityEvidenceMatrixError("schema_version must be exactly 1")
        if self.matrix_version == 1:
            if self.predecessor_matrix_id is not None:
                raise ProviderCapabilityEvidenceMatrixError("v1 cannot name predecessor")
        elif self.predecessor_matrix_id is None:
            raise ProviderCapabilityEvidenceMatrixError("successor requires predecessor")
        else:
            _hash(self.predecessor_matrix_id, "predecessor_matrix_id")

        as_of = _time(self.as_of, "as_of")
        if _time(self.profile.observed_at, "profile.observed_at") > as_of:
            raise ProviderCapabilityEvidenceMatrixError("as_of predates profile")
        if _time(self.integration.observed_at, "integration.observed_at") > as_of:
            raise ProviderCapabilityEvidenceMatrixError("as_of predates integration")
        if type(self.facts) is not tuple:
            raise ProviderCapabilityEvidenceMatrixError("facts must be tuple")
        for fact in self.facts:
            if type(fact) is not ProviderCapabilityEvidence:
                raise ProviderCapabilityEvidenceMatrixError("facts must be exact evidence")
        expected = tuple(sorted(BookmakerCapability, key=lambda item: item.value))
        if tuple(f.capability for f in self.facts) != expected:
            raise ProviderCapabilityEvidenceMatrixError(
                "facts must contain every capability once in canonical order"
            )
        for fact in self.facts:
            if (
                fact.grade is not ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN
                and not _is_product_issued(fact)
            ):
                raise ProviderCapabilityEvidenceMatrixError(
                    "positive capability evidence must be product-issued exact object"
                )
            if (
                fact.grade is not ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN
                and (
                    fact.environment != self.environment
                    or fact.application_mode != self.application_mode
                )
            ):
                raise ProviderCapabilityEvidenceMatrixError(
                    "fact environment/application scope mismatch"
                )
            if fact.profile_id != self.profile.profile_id:
                raise ProviderCapabilityEvidenceMatrixError("fact profile binding mismatch")
            if fact.integration_evidence_id != self.integration.evidence_id:
                raise ProviderCapabilityEvidenceMatrixError(
                    "fact integration binding mismatch"
                )
            if fact.profile_state is not self.profile.state_of(fact.capability):
                raise ProviderCapabilityEvidenceMatrixError("fact profile_state mismatch")
            if fact.observed_at is not None and _time(fact.observed_at, "fact.observed_at") > as_of:
                raise ProviderCapabilityEvidenceMatrixError("fact observed_at after as_of")

    @property
    def matrix_id(self) -> str:
        return _digest(self.to_canonical_dict())

    @property
    def provider_write_authorized(self) -> bool:
        return False

    @property
    def execution_authorized(self) -> bool:
        return False

    @property
    def real_money_execution(self) -> bool:
        return False

    def fact_for(self, capability: BookmakerCapability) -> ProviderCapabilityEvidence:
        if type(capability) is not BookmakerCapability:
            raise ProviderCapabilityEvidenceMatrixError("capability must be exact enum")
        if not _is_product_issued_matrix(self):
            raise ProviderCapabilityEvidenceMatrixError(
                "capability matrix must be product-issued exact object with unchanged payload"
            )
        return next(fact for fact in self.facts if fact.capability is capability)

    def qualifies(
        self,
        capability: BookmakerCapability,
        *,
        accepted_grades: frozenset[ProviderCapabilityTruthGrade],
        at_time: str,
    ) -> bool:
        if type(accepted_grades) is not frozenset or not accepted_grades:
            raise ProviderCapabilityEvidenceMatrixError(
                "accepted_grades must be non-empty frozenset"
            )
        if any(type(grade) is not ProviderCapabilityTruthGrade for grade in accepted_grades):
            raise ProviderCapabilityEvidenceMatrixError("accepted_grades must be exact enums")
        requested_at = _time(at_time, "at_time")
        if not _is_product_issued_matrix(self):
            return False
        if requested_at > _time(self.as_of, "as_of"):
            return False
        fact = self.fact_for(capability)
        if (
            fact.grade is not ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN
            and (
                fact.environment != self.environment
                or fact.application_mode != self.application_mode
            )
        ):
            return False
        if (
            fact.grade is not ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN
            and not _is_product_issued(fact)
        ):
            return False
        # This query has no requested sport/market scope. A fact restricted to
        # either axis cannot therefore be treated as provider-wide authority.
        # Scoped consumers need an explicit scoped resolver instead of silently
        # dropping the evidence restriction.
        if fact.sport_scope or fact.market_scope:
            return False
        if fact.grade is ProviderCapabilityTruthGrade.REVOKED_OR_UNAVAILABLE:
            return False
        return fact.grade in accepted_grades and fact.is_current(at_time)

    def to_canonical_dict(self) -> dict[str, object]:
        return {
            "profile_id": self.profile.profile_id,
            "integration_evidence_id": self.integration.evidence_id,
            "environment": self.environment,
            "application_mode": self.application_mode,
            "matrix_version": self.matrix_version,
            "facts": [fact.to_canonical_dict() for fact in self.facts],
            "as_of": self.as_of,
            "matrix_ref": self.matrix_ref,
            "predecessor_matrix_id": self.predecessor_matrix_id,
            "provider_write_authorized": False,
            "execution_authorized": False,
            "real_money_execution": False,
            "schema_version": self.schema_version,
        }


def _register_product_matrix(
    matrix: ProviderCapabilityEvidenceMatrix,
) -> ProviderCapabilityEvidenceMatrix:
    issuance_key = id(matrix)
    _ISSUED_MATRICES[issuance_key] = matrix
    _ISSUED_MATRIX_SEALS[issuance_key] = matrix.matrix_id
    finalize(matrix, _ISSUED_MATRIX_SEALS.pop, issuance_key, None)
    return matrix


def _is_product_issued_matrix(matrix: ProviderCapabilityEvidenceMatrix) -> bool:
    issuance_key = id(matrix)
    if _ISSUED_MATRICES.get(issuance_key) is not matrix:
        return False
    original_seal = _ISSUED_MATRIX_SEALS.get(issuance_key)
    if original_seal is None:
        return False
    try:
        return matrix.matrix_id == original_seal
    except (
        AttributeError,
        ProviderCapabilityEvidenceMatrixError,
        TypeError,
        ValueError,
    ):
        return False


def _digest(value: dict[str, object]) -> str:
    return sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
    ).hexdigest()


def build_provider_capability_evidence_matrix(
    profile: BookmakerCapabilityProfile,
    integration: BookmakerIntegrationEvidence,
    *,
    environment: str,
    application_mode: str,
    matrix_version: int,
    as_of: str,
    matrix_ref: str,
    evidence: Iterable[ProviderCapabilityEvidence] = (),
    predecessor_matrix_id: str | None = None,
) -> ProviderCapabilityEvidenceMatrix:
    if type(profile) is not BookmakerCapabilityProfile:
        raise ProviderCapabilityEvidenceMatrixError(
            "profile must be an exact BookmakerCapabilityProfile"
        )
    if type(integration) is not BookmakerIntegrationEvidence:
        raise ProviderCapabilityEvidenceMatrixError(
            "integration must be exact BookmakerIntegrationEvidence"
        )
    integration.verify_profile(profile)
    supplied: dict[BookmakerCapability, ProviderCapabilityEvidence] = {}
    for fact in evidence:
        if type(fact) is not ProviderCapabilityEvidence:
            raise ProviderCapabilityEvidenceMatrixError("evidence must be exact facts")
        if fact.capability in supplied:
            raise ProviderCapabilityEvidenceMatrixError("duplicate capability evidence")
        supplied[fact.capability] = fact
    facts = []
    for capability in sorted(BookmakerCapability, key=lambda item: item.value):
        facts.append(
            supplied.get(capability)
            or ProviderCapabilityEvidence(
                capability=capability,
                profile_state=profile.state_of(capability),
                grade=ProviderCapabilityTruthGrade.UNKNOWN_UNPROVEN,
                profile_id=profile.profile_id,
                integration_evidence_id=integration.evidence_id,
            )
        )
    return _register_product_matrix(
        ProviderCapabilityEvidenceMatrix(
            profile=profile,
            integration=integration,
            environment=environment,
            application_mode=application_mode,
            matrix_version=matrix_version,
            facts=tuple(facts),
            as_of=as_of,
            matrix_ref=matrix_ref,
            predecessor_matrix_id=predecessor_matrix_id,
        )
    )


def validate_capability_matrix_successor(
    previous: ProviderCapabilityEvidenceMatrix,
    current: ProviderCapabilityEvidenceMatrix,
) -> None:
    if type(previous) is not ProviderCapabilityEvidenceMatrix or type(
        current
    ) is not ProviderCapabilityEvidenceMatrix:
        raise ProviderCapabilityEvidenceMatrixError("successors must be exact matrices")
    if current.predecessor_matrix_id != previous.matrix_id:
        raise ProviderCapabilityEvidenceMatrixError("predecessor binding mismatch")
    if current.matrix_version != previous.matrix_version + 1:
        raise ProviderCapabilityEvidenceMatrixError("version must advance by one")
    old_scope = (
        previous.profile.venue_id,
        previous.profile.account_id,
        previous.environment,
        previous.application_mode,
    )
    new_scope = (
        current.profile.venue_id,
        current.profile.account_id,
        current.environment,
        current.application_mode,
    )
    if new_scope != old_scope:
        raise ProviderCapabilityEvidenceMatrixError("successor scope changed")
    if _time(current.as_of, "current.as_of") < _time(previous.as_of, "previous.as_of"):
        raise ProviderCapabilityEvidenceMatrixError("successor time moved backwards")
    if not _is_product_issued_matrix(previous) or not _is_product_issued_matrix(current):
        raise ProviderCapabilityEvidenceMatrixError(
            "successor matrices must be product-issued exact objects with unchanged payload"
        )
