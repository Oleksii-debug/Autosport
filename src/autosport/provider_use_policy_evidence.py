from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Mapping
from urllib.parse import urlsplit


_SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")
_PROVIDER_RE = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*\Z", re.ASCII)
_REFERENCE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z", re.ASCII)


class ProviderUsePolicyError(ValueError):
    """Provider-use policy evidence is malformed, ambiguous, or not authoritative."""


class ProviderUseAuthorizationError(RuntimeError):
    """Requested downstream provider-data use is not authorized by bound evidence."""


class ProviderUsePurpose(str, Enum):
    EXECUTION_RUNTIME = "execution_runtime"
    INTERNAL_ANALYSIS = "internal_analysis"
    MODEL_TRAINING = "model_training"
    RETENTION = "retention"
    REDISTRIBUTION = "redistribution"
    BENCHMARKING = "benchmarking"
    THIRD_PARTY_EXPORT = "third_party_export"


class ProviderUsePolicyStatus(str, Enum):
    ALLOWED_BY_DOCUMENTED_POLICY = "allowed_by_documented_policy"
    REQUIRES_WRITTEN_PERMISSION = "requires_written_permission"
    PROHIBITED_BY_DOCUMENTED_POLICY = "prohibited_by_documented_policy"
    UNRESOLVED_REQUIRES_REVIEW = "unresolved_requires_review"


class ProviderUseDecisionBasis(str, Enum):
    DOCUMENTED_POLICY_TEXT = "documented_policy_text"
    REVIEWER_DECISION = "reviewer_decision"


class ProviderUseAuthorizationState(str, Enum):
    AUTHORIZED_BY_DOCUMENTED_POLICY = "authorized_by_documented_policy"
    AUTHORIZED_BY_WRITTEN_PERMISSION = "authorized_by_written_permission"
    PROVIDER_MISMATCH = "provider_mismatch"
    POLICY_SCOPE_MISMATCH = "policy_scope_mismatch"
    POLICY_NOT_OBSERVED_AT_USE = "policy_not_observed_at_use"
    POLICY_EFFECTIVE_TIME_UNRESOLVED = "policy_effective_time_unresolved"
    POLICY_NOT_EFFECTIVE = "policy_not_effective"
    POLICY_REVIEW_EXPIRED = "policy_review_expired"
    PROHIBITED_BY_DOCUMENTED_POLICY = "prohibited_by_documented_policy"
    REVIEW_REQUIRED = "review_required"
    WRITTEN_PERMISSION_REQUIRED = "written_permission_required"
    WRITTEN_PERMISSION_SCOPE_MISMATCH = "written_permission_scope_mismatch"
    WRITTEN_PERMISSION_NOT_OBSERVED_AT_USE = "written_permission_not_observed_at_use"
    WRITTEN_PERMISSION_NOT_EFFECTIVE = "written_permission_not_effective"
    WRITTEN_PERMISSION_EXPIRED = "written_permission_expired"


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("ascii")).hexdigest()


def _text(value: object, name: str, *, max_len: int = 256) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ProviderUsePolicyError(f"{name} must be non-empty canonical text")
    if len(value) > max_len or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ProviderUsePolicyError(f"{name} contains unsupported text")
    return value


def _provider_id(value: object) -> str:
    text = _text(value, "provider_id", max_len=64)
    if _PROVIDER_RE.fullmatch(text) is None:
        raise ProviderUsePolicyError("provider_id must be lowercase canonical ASCII")
    return text


def _reference(value: object, name: str) -> str:
    text = _text(value, name, max_len=128)
    if _REFERENCE_RE.fullmatch(text) is None:
        raise ProviderUsePolicyError(f"{name} must be a bounded non-secret reference")
    return text


def _optional_reference(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _reference(value, name)


def _sha256(value: object, name: str) -> str:
    text = _text(value, name, max_len=64).lower()
    if len(text) != 64 or any(ch not in _HEX for ch in text):
        raise ProviderUsePolicyError(f"{name} must be SHA-256 hex")
    return text


def _optional_sha256(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _sha256(value, name)


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name, max_len=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProviderUsePolicyError(f"{name} must be ISO-8601") from exc
    if parsed.tzinfo is None:
        raise ProviderUsePolicyError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _utc(value: object, name: str) -> str:
    return _instant(value, name).isoformat().replace("+00:00", "Z")


def _optional_utc(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _utc(value, name)


def _policy_url(value: object) -> str:
    text = _text(value, "source_url", max_len=512)
    parsed = urlsplit(text)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.hostname != parsed.hostname.lower()
    ):
        raise ProviderUsePolicyError(
            "source_url must be canonical HTTPS without credentials, query, or fragment"
        )
    if parsed.netloc != parsed.netloc.lower():
        raise ProviderUsePolicyError("source_url host must be lowercase canonical text")
    return text


@dataclass(frozen=True, slots=True)
class ProviderUsePurposeDecision:
    purpose: ProviderUsePurpose
    status: ProviderUsePolicyStatus
    source_section_ref: str
    basis: ProviderUseDecisionBasis = ProviderUseDecisionBasis.DOCUMENTED_POLICY_TEXT
    reviewer_decision_ref: str | None = None
    reviewer_decision_sha256: str | None = None

    def __post_init__(self) -> None:
        try:
            purpose = ProviderUsePurpose(self.purpose)
            status = ProviderUsePolicyStatus(self.status)
            basis = ProviderUseDecisionBasis(self.basis)
        except (TypeError, ValueError) as exc:
            raise ProviderUsePolicyError("purpose decision enum value is invalid") from exc
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "basis", basis)
        object.__setattr__(
            self,
            "source_section_ref",
            _reference(self.source_section_ref, "source_section_ref"),
        )
        reviewer = _optional_reference(
            self.reviewer_decision_ref,
            "reviewer_decision_ref",
        )
        reviewer_sha256 = _optional_sha256(
            self.reviewer_decision_sha256,
            "reviewer_decision_sha256",
        )
        if basis is ProviderUseDecisionBasis.REVIEWER_DECISION and (
            reviewer is None or reviewer_sha256 is None
        ):
            raise ProviderUsePolicyError(
                "reviewer basis requires reviewer_decision_ref and reviewer_decision_sha256"
            )
        if basis is ProviderUseDecisionBasis.DOCUMENTED_POLICY_TEXT and (
            reviewer is not None or reviewer_sha256 is not None
        ):
            raise ProviderUsePolicyError(
                "documented-policy basis cannot carry reviewer decision evidence"
            )
        object.__setattr__(self, "reviewer_decision_ref", reviewer)
        object.__setattr__(self, "reviewer_decision_sha256", reviewer_sha256)

    def to_payload(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose.value,
            "status": self.status.value,
            "source_section_ref": self.source_section_ref,
            "basis": self.basis.value,
            "reviewer_decision_ref": self.reviewer_decision_ref,
            "reviewer_decision_sha256": self.reviewer_decision_sha256,
        }


@dataclass(frozen=True, slots=True)
class ProviderUsePolicyEvidence:
    """Immutable provider-use evidence; never API or runtime capability authority."""

    provider_id: str
    source_url: str
    document_sha256: str
    observed_at_utc: str
    effective_at_utc: str | None
    review_due_at_utc: str
    decisions: tuple[ProviderUsePurposeDecision, ...]
    account_scope_ref: str | None = None
    application_scope_ref: str | None = None
    jurisdiction_scope_ref: str | None = None
    license_ref: str | None = None
    supersedes_evidence_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _provider_id(self.provider_id))
        object.__setattr__(self, "source_url", _policy_url(self.source_url))
        object.__setattr__(
            self,
            "document_sha256",
            _sha256(self.document_sha256, "document_sha256"),
        )
        object.__setattr__(
            self,
            "observed_at_utc",
            _utc(self.observed_at_utc, "observed_at_utc"),
        )
        object.__setattr__(
            self,
            "effective_at_utc",
            _optional_utc(self.effective_at_utc, "effective_at_utc"),
        )
        object.__setattr__(
            self,
            "review_due_at_utc",
            _utc(self.review_due_at_utc, "review_due_at_utc"),
        )
        observed = _instant(self.observed_at_utc, "observed_at_utc")
        review_due = _instant(self.review_due_at_utc, "review_due_at_utc")
        if review_due <= observed:
            raise ProviderUsePolicyError(
                "review_due_at_utc must be later than observed_at_utc"
            )
        if self.effective_at_utc is not None:
            effective = _instant(self.effective_at_utc, "effective_at_utc")
            if effective >= review_due:
                raise ProviderUsePolicyError(
                    "effective_at_utc must be earlier than review_due_at_utc"
                )

        if type(self.decisions) is not tuple:
            raise ProviderUsePolicyError("decisions must be a tuple")
        normalized: list[ProviderUsePurposeDecision] = []
        seen: set[ProviderUsePurpose] = set()
        for decision in self.decisions:
            if type(decision) is not ProviderUsePurposeDecision:
                raise ProviderUsePolicyError(
                    "decisions must contain exact ProviderUsePurposeDecision values"
                )
            if decision.purpose in seen:
                raise ProviderUsePolicyError("purpose decision is duplicated")
            seen.add(decision.purpose)
            normalized.append(decision)
        expected = set(ProviderUsePurpose)
        if seen != expected:
            raise ProviderUsePolicyError(
                "policy evidence must explicitly classify every provider-use purpose"
            )
        normalized.sort(key=lambda item: item.purpose.value)
        object.__setattr__(self, "decisions", tuple(normalized))

        for name in (
            "account_scope_ref",
            "application_scope_ref",
            "jurisdiction_scope_ref",
            "license_ref",
        ):
            object.__setattr__(
                self,
                name,
                _optional_reference(getattr(self, name), name),
            )
        object.__setattr__(
            self,
            "supersedes_evidence_id",
            _optional_sha256(self.supersedes_evidence_id, "supersedes_evidence_id"),
        )

    def decision_for(self, purpose: ProviderUsePurpose) -> ProviderUsePurposeDecision:
        try:
            requested = ProviderUsePurpose(purpose)
        except (TypeError, ValueError) as exc:
            raise ProviderUsePolicyError("purpose is invalid") from exc
        for decision in self.decisions:
            if decision.purpose is requested:
                return decision
        raise AssertionError("complete decision set invariant violated")

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "source_url": self.source_url,
            "document_sha256": self.document_sha256,
            "observed_at_utc": self.observed_at_utc,
            "effective_at_utc": self.effective_at_utc,
            "review_due_at_utc": self.review_due_at_utc,
            "account_scope_ref": self.account_scope_ref,
            "application_scope_ref": self.application_scope_ref,
            "jurisdiction_scope_ref": self.jurisdiction_scope_ref,
            "license_ref": self.license_ref,
            "supersedes_evidence_id": self.supersedes_evidence_id,
            "decisions": [decision.to_payload() for decision in self.decisions],
        }

    @property
    def evidence_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class ProviderWrittenPermissionEvidence:
    """Exact written exception/permission evidence; never a caller boolean override."""

    provider_id: str
    policy_evidence_id: str
    permission_ref: str
    document_sha256: str
    observed_at_utc: str
    valid_from_utc: str
    valid_until_utc: str | None
    purposes: tuple[ProviderUsePurpose, ...]
    account_scope_ref: str | None = None
    application_scope_ref: str | None = None
    jurisdiction_scope_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _provider_id(self.provider_id))
        object.__setattr__(
            self,
            "policy_evidence_id",
            _sha256(self.policy_evidence_id, "policy_evidence_id"),
        )
        object.__setattr__(
            self,
            "permission_ref",
            _reference(self.permission_ref, "permission_ref"),
        )
        object.__setattr__(
            self,
            "document_sha256",
            _sha256(self.document_sha256, "document_sha256"),
        )
        object.__setattr__(
            self,
            "observed_at_utc",
            _utc(self.observed_at_utc, "observed_at_utc"),
        )
        object.__setattr__(
            self,
            "valid_from_utc",
            _utc(self.valid_from_utc, "valid_from_utc"),
        )
        object.__setattr__(
            self,
            "valid_until_utc",
            _optional_utc(self.valid_until_utc, "valid_until_utc"),
        )
        if self.valid_until_utc is not None:
            if _instant(self.valid_until_utc, "valid_until_utc") <= _instant(
                self.valid_from_utc, "valid_from_utc"
            ):
                raise ProviderUsePolicyError(
                    "valid_until_utc must be later than valid_from_utc"
                )
        if type(self.purposes) is not tuple or not self.purposes:
            raise ProviderUsePolicyError("purposes must be a non-empty tuple")
        normalized: list[ProviderUsePurpose] = []
        seen: set[ProviderUsePurpose] = set()
        for purpose in self.purposes:
            try:
                item = ProviderUsePurpose(purpose)
            except (TypeError, ValueError) as exc:
                raise ProviderUsePolicyError(
                    "permission purpose is invalid"
                ) from exc
            if item in seen:
                raise ProviderUsePolicyError("permission purpose is duplicated")
            seen.add(item)
            normalized.append(item)
        normalized.sort(key=lambda item: item.value)
        object.__setattr__(self, "purposes", tuple(normalized))
        for name in (
            "account_scope_ref",
            "application_scope_ref",
            "jurisdiction_scope_ref",
        ):
            object.__setattr__(
                self,
                name,
                _optional_reference(getattr(self, name), name),
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "policy_evidence_id": self.policy_evidence_id,
            "permission_ref": self.permission_ref,
            "document_sha256": self.document_sha256,
            "observed_at_utc": self.observed_at_utc,
            "valid_from_utc": self.valid_from_utc,
            "valid_until_utc": self.valid_until_utc,
            "purposes": [purpose.value for purpose in self.purposes],
            "account_scope_ref": self.account_scope_ref,
            "application_scope_ref": self.application_scope_ref,
            "jurisdiction_scope_ref": self.jurisdiction_scope_ref,
        }

    @property
    def permission_evidence_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class ProviderUseRequest:
    provider_id: str
    purpose: ProviderUsePurpose
    use_at_utc: str
    account_scope_ref: str | None = None
    application_scope_ref: str | None = None
    jurisdiction_scope_ref: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _provider_id(self.provider_id))
        try:
            purpose = ProviderUsePurpose(self.purpose)
        except (TypeError, ValueError) as exc:
            raise ProviderUsePolicyError("purpose is invalid") from exc
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(
            self,
            "use_at_utc",
            _utc(self.use_at_utc, "use_at_utc"),
        )
        for name in (
            "account_scope_ref",
            "application_scope_ref",
            "jurisdiction_scope_ref",
        ):
            object.__setattr__(
                self,
                name,
                _optional_reference(getattr(self, name), name),
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "provider_id": self.provider_id,
            "purpose": self.purpose.value,
            "use_at_utc": self.use_at_utc,
            "account_scope_ref": self.account_scope_ref,
            "application_scope_ref": self.application_scope_ref,
            "jurisdiction_scope_ref": self.jurisdiction_scope_ref,
        }

    @property
    def request_id(self) -> str:
        return _digest(self.to_payload())


@dataclass(frozen=True, slots=True)
class ProviderUseAuthorization:
    request_id: str
    policy_evidence_id: str
    purpose: ProviderUsePurpose
    state: ProviderUseAuthorizationState
    permission_evidence_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "request_id",
            _sha256(self.request_id, "request_id"),
        )
        object.__setattr__(
            self,
            "policy_evidence_id",
            _sha256(self.policy_evidence_id, "policy_evidence_id"),
        )
        try:
            purpose = ProviderUsePurpose(self.purpose)
            state = ProviderUseAuthorizationState(self.state)
        except (TypeError, ValueError) as exc:
            raise ProviderUsePolicyError("authorization enum value is invalid") from exc
        object.__setattr__(self, "purpose", purpose)
        object.__setattr__(self, "state", state)
        object.__setattr__(
            self,
            "permission_evidence_id",
            _optional_sha256(
                self.permission_evidence_id,
                "permission_evidence_id",
            ),
        )
        if (
            state is ProviderUseAuthorizationState.AUTHORIZED_BY_WRITTEN_PERMISSION
            and self.permission_evidence_id is None
        ):
            raise ProviderUsePolicyError(
                "written-permission authorization requires permission evidence"
            )
        if (
            state is not ProviderUseAuthorizationState.AUTHORIZED_BY_WRITTEN_PERMISSION
            and self.permission_evidence_id is not None
        ):
            raise ProviderUsePolicyError(
                "non-permission authorization cannot carry permission evidence"
            )

    @property
    def authorized(self) -> bool:
        return self.state in {
            ProviderUseAuthorizationState.AUTHORIZED_BY_DOCUMENTED_POLICY,
            ProviderUseAuthorizationState.AUTHORIZED_BY_WRITTEN_PERMISSION,
        }

    def require_authorized(self) -> None:
        if not self.authorized:
            raise ProviderUseAuthorizationError(
                f"provider use is not authorized: {self.state.value}"
            )

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "request_id": self.request_id,
            "policy_evidence_id": self.policy_evidence_id,
            "purpose": self.purpose.value,
            "state": self.state.value,
            "permission_evidence_id": self.permission_evidence_id,
        }

    @property
    def authorization_id(self) -> str:
        return _digest(self.to_payload())


def _scope_matches(expected: str | None, actual: str | None) -> bool:
    return expected is None or expected == actual


def _authorization(
    *,
    policy: ProviderUsePolicyEvidence,
    request: ProviderUseRequest,
    state: ProviderUseAuthorizationState,
    permission: ProviderWrittenPermissionEvidence | None = None,
) -> ProviderUseAuthorization:
    return ProviderUseAuthorization(
        request_id=request.request_id,
        policy_evidence_id=policy.evidence_id,
        purpose=request.purpose,
        state=state,
        permission_evidence_id=(
            permission.permission_evidence_id
            if state is ProviderUseAuthorizationState.AUTHORIZED_BY_WRITTEN_PERMISSION
            and permission is not None
            else None
        ),
    )


def evaluate_provider_use(
    *,
    policy: ProviderUsePolicyEvidence,
    request: ProviderUseRequest,
    written_permission: ProviderWrittenPermissionEvidence | None = None,
) -> ProviderUseAuthorization:
    """Evaluate exact provider-use evidence without inferring rights from capability."""

    if type(policy) is not ProviderUsePolicyEvidence:
        raise TypeError("policy must be exact ProviderUsePolicyEvidence")
    if type(request) is not ProviderUseRequest:
        raise TypeError("request must be exact ProviderUseRequest")
    if written_permission is not None and type(written_permission) is not ProviderWrittenPermissionEvidence:
        raise TypeError(
            "written_permission must be exact ProviderWrittenPermissionEvidence or None"
        )

    if policy.provider_id != request.provider_id:
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.PROVIDER_MISMATCH,
        )

    for field in (
        "account_scope_ref",
        "application_scope_ref",
        "jurisdiction_scope_ref",
    ):
        if not _scope_matches(getattr(policy, field), getattr(request, field)):
            return _authorization(
                policy=policy,
                request=request,
                state=ProviderUseAuthorizationState.POLICY_SCOPE_MISMATCH,
            )

    use_at = _instant(request.use_at_utc, "use_at_utc")
    if use_at < _instant(policy.observed_at_utc, "observed_at_utc"):
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.POLICY_NOT_OBSERVED_AT_USE,
        )
    if policy.effective_at_utc is None:
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.POLICY_EFFECTIVE_TIME_UNRESOLVED,
        )
    if use_at < _instant(policy.effective_at_utc, "effective_at_utc"):
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.POLICY_NOT_EFFECTIVE,
        )
    if use_at >= _instant(policy.review_due_at_utc, "review_due_at_utc"):
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.POLICY_REVIEW_EXPIRED,
        )

    decision = policy.decision_for(request.purpose)
    if decision.status is ProviderUsePolicyStatus.ALLOWED_BY_DOCUMENTED_POLICY:
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.AUTHORIZED_BY_DOCUMENTED_POLICY,
        )
    if decision.status is ProviderUsePolicyStatus.PROHIBITED_BY_DOCUMENTED_POLICY:
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.PROHIBITED_BY_DOCUMENTED_POLICY,
        )
    if decision.status is ProviderUsePolicyStatus.UNRESOLVED_REQUIRES_REVIEW:
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.REVIEW_REQUIRED,
        )

    if written_permission is None:
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.WRITTEN_PERMISSION_REQUIRED,
        )
    if (
        written_permission.provider_id != request.provider_id
        or written_permission.policy_evidence_id != policy.evidence_id
        or request.purpose not in written_permission.purposes
    ):
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.WRITTEN_PERMISSION_SCOPE_MISMATCH,
        )
    for field in (
        "account_scope_ref",
        "application_scope_ref",
        "jurisdiction_scope_ref",
    ):
        if not _scope_matches(
            getattr(written_permission, field),
            getattr(request, field),
        ):
            return _authorization(
                policy=policy,
                request=request,
                state=ProviderUseAuthorizationState.WRITTEN_PERMISSION_SCOPE_MISMATCH,
            )

    if use_at < _instant(written_permission.observed_at_utc, "permission.observed_at_utc"):
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.WRITTEN_PERMISSION_NOT_OBSERVED_AT_USE,
        )
    if use_at < _instant(written_permission.valid_from_utc, "permission.valid_from_utc"):
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.WRITTEN_PERMISSION_NOT_EFFECTIVE,
        )
    if (
        written_permission.valid_until_utc is not None
        and use_at >= _instant(
            written_permission.valid_until_utc,
            "permission.valid_until_utc",
        )
    ):
        return _authorization(
            policy=policy,
            request=request,
            state=ProviderUseAuthorizationState.WRITTEN_PERMISSION_EXPIRED,
        )
    return _authorization(
        policy=policy,
        request=request,
        state=ProviderUseAuthorizationState.AUTHORIZED_BY_WRITTEN_PERMISSION,
        permission=written_permission,
    )


def require_policy_successor(
    *,
    previous: ProviderUsePolicyEvidence,
    successor: ProviderUsePolicyEvidence,
) -> None:
    """Validate immutable successor linkage; never mutates historical policy evidence."""

    if type(previous) is not ProviderUsePolicyEvidence or type(successor) is not ProviderUsePolicyEvidence:
        raise TypeError("previous and successor must be exact ProviderUsePolicyEvidence")
    if successor.provider_id != previous.provider_id:
        raise ProviderUsePolicyError("policy successor provider differs")
    if successor.supersedes_evidence_id != previous.evidence_id:
        raise ProviderUsePolicyError("policy successor does not bind previous evidence")
    if _instant(successor.observed_at_utc, "successor.observed_at_utc") <= _instant(
        previous.observed_at_utc,
        "previous.observed_at_utc",
    ):
        raise ProviderUsePolicyError("policy successor observation must move forward")
