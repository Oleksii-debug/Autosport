"""Deterministic provider-output governance matching.

The governance record is intentionally declarative: owner_approval_reference and
owner_approval_sha256 identify claimed approval evidence, but this module does
not yet own a durable owner-approval issuer/resolver.  Therefore a
caller-constructed governance record can be validated and matched, but it cannot
by itself mint positive lawful-use authority.  Otherwise-valid requests fail
closed until a separate product-owned durable approval boundary is integrated.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Mapping
from urllib.parse import urlsplit


_SCHEMA_VERSION = 1
_HEX = frozenset("0123456789abcdef")
_AUTHORITY_FIELDS = frozenset(
    {
        "schema_version",
        "authority_id",
        "provider_id",
        "service_id",
        "authority_version",
        "terms_reference",
        "terms_sha256",
        "owner_approval_reference",
        "owner_approval_sha256",
        "valid_from",
        "valid_until",
        "grants",
    }
)
_GRANT_FIELDS = frozenset(
    {"purpose", "artifact_class", "retention_policy", "max_retention_seconds"}
)


def _text(value: object, name: str) -> str:
    if type(value) is not str or not value or value != value.strip():
        raise ValueError(f"{name} must be a non-empty canonical string")
    value.encode("utf-8")
    return value


def _sha256(value: object, name: str) -> str:
    text = _text(value, name).lower()
    if len(text) != 64 or any(char not in _HEX for char in text):
        raise ValueError(f"{name} must be a canonical SHA-256 hex string")
    return text


def _instant(value: object, name: str) -> datetime:
    text = _text(value, name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _canonical_instant(value: object, name: str) -> str:
    parsed = _instant(value, name)
    if parsed.microsecond:
        rendered = parsed.isoformat(timespec="microseconds")
    else:
        rendered = parsed.isoformat(timespec="seconds")
    return rendered.replace("+00:00", "Z")


def _reference(value: object, name: str, *, public_https: bool) -> str:
    text = _text(value, name)
    if "?" in text or "#" in text:
        raise ValueError(f"{name} must not contain query or fragment data")
    parsed = urlsplit(text)
    if not parsed.scheme:
        raise ValueError(f"{name} must be an absolute reference")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError(f"{name} must not contain credentials")
    scheme = parsed.scheme.lower()
    if public_https:
        if scheme != "https" or not parsed.hostname:
            raise ValueError(f"{name} must be an absolute HTTPS reference")
    elif scheme not in {"https", "urn"}:
        raise ValueError(f"{name} must use HTTPS or URN")
    elif scheme == "https" and not parsed.hostname:
        raise ValueError(f"{name} HTTPS reference must identify a host")
    return text


def _canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _digest(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise ValueError(f"non-finite JSON constant: {value}")


class RetentionPolicy(StrEnum):
    FORBIDDEN = "FORBIDDEN"
    BOUNDED = "BOUNDED"
    UNBOUNDED = "UNBOUNDED"


@dataclass(frozen=True, slots=True)
class ProviderOutputGrant:
    purpose: str
    artifact_class: str
    retention_policy: RetentionPolicy
    max_retention_seconds: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "purpose", _text(self.purpose, "purpose"))
        object.__setattr__(
            self, "artifact_class", _text(self.artifact_class, "artifact_class")
        )
        if not isinstance(self.retention_policy, RetentionPolicy):
            try:
                policy = RetentionPolicy(self.retention_policy)
            except (TypeError, ValueError) as exc:
                raise ValueError("retention_policy is unsupported") from exc
            object.__setattr__(self, "retention_policy", policy)

        if self.retention_policy is RetentionPolicy.BOUNDED:
            if (
                isinstance(self.max_retention_seconds, bool)
                or not isinstance(self.max_retention_seconds, int)
                or self.max_retention_seconds <= 0
            ):
                raise ValueError(
                    "BOUNDED retention requires positive max_retention_seconds"
                )
            try:
                timedelta(seconds=self.max_retention_seconds)
            except OverflowError as exc:
                raise ValueError(
                    "BOUNDED max_retention_seconds exceeds supported datetime range"
                ) from exc
        elif self.max_retention_seconds is not None:
            raise ValueError(
                "max_retention_seconds is allowed only for BOUNDED retention"
            )

    @property
    def key(self) -> tuple[str, str]:
        return (self.purpose, self.artifact_class)

    def to_payload(self) -> dict[str, Any]:
        return {
            "purpose": self.purpose,
            "artifact_class": self.artifact_class,
            "retention_policy": self.retention_policy.value,
            "max_retention_seconds": self.max_retention_seconds,
        }


@dataclass(frozen=True, slots=True)
class ProviderOutputGovernanceAuthority:
    provider_id: str
    service_id: str
    authority_version: str
    terms_reference: str
    terms_sha256: str
    owner_approval_reference: str
    owner_approval_sha256: str
    valid_from: str
    valid_until: str
    grants: tuple[ProviderOutputGrant, ...]

    def __post_init__(self) -> None:
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        object.__setattr__(self, "service_id", _text(self.service_id, "service_id"))
        object.__setattr__(
            self,
            "authority_version",
            _text(self.authority_version, "authority_version"),
        )
        object.__setattr__(
            self,
            "terms_reference",
            _reference(self.terms_reference, "terms_reference", public_https=True),
        )
        object.__setattr__(
            self, "terms_sha256", _sha256(self.terms_sha256, "terms_sha256")
        )
        object.__setattr__(
            self,
            "owner_approval_reference",
            _reference(
                self.owner_approval_reference,
                "owner_approval_reference",
                public_https=False,
            ),
        )
        object.__setattr__(
            self,
            "owner_approval_sha256",
            _sha256(self.owner_approval_sha256, "owner_approval_sha256"),
        )
        valid_from = _canonical_instant(self.valid_from, "valid_from")
        valid_until = _canonical_instant(self.valid_until, "valid_until")
        if _instant(valid_from, "valid_from") >= _instant(valid_until, "valid_until"):
            raise ValueError("valid_until must be later than valid_from")
        object.__setattr__(self, "valid_from", valid_from)
        object.__setattr__(self, "valid_until", valid_until)

        if not isinstance(self.grants, tuple) or not self.grants:
            raise ValueError("grants must be a non-empty tuple")
        if not all(isinstance(grant, ProviderOutputGrant) for grant in self.grants):
            raise ValueError("grants must contain ProviderOutputGrant values")
        keys = [grant.key for grant in self.grants]
        if len(keys) != len(set(keys)):
            raise ValueError("grants must not contain ambiguous duplicate purpose/artifact pairs")
        object.__setattr__(self, "grants", tuple(sorted(self.grants, key=lambda g: g.key)))

    def identity_payload(self) -> dict[str, Any]:
        return {
            "schema_version": _SCHEMA_VERSION,
            "provider_id": self.provider_id,
            "service_id": self.service_id,
            "authority_version": self.authority_version,
            "terms_reference": self.terms_reference,
            "terms_sha256": self.terms_sha256,
            "owner_approval_reference": self.owner_approval_reference,
            "owner_approval_sha256": self.owner_approval_sha256,
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "grants": [grant.to_payload() for grant in self.grants],
        }

    @property
    def authority_id(self) -> str:
        return _digest(self.identity_payload())

    def to_payload(self) -> dict[str, Any]:
        payload = self.identity_payload()
        payload["authority_id"] = self.authority_id
        return payload

    def to_json(self) -> str:
        return _canonical_json(self.to_payload())

    @classmethod
    def from_json(cls, raw: str) -> "ProviderOutputGovernanceAuthority":
        text = _text(raw, "raw")
        try:
            payload = json.loads(
                text,
                object_pairs_hook=_reject_duplicate_keys,
                parse_constant=_reject_nonfinite,
            )
        except json.JSONDecodeError as exc:
            raise ValueError("raw must be valid canonical JSON") from exc
        if type(payload) is not dict:
            raise ValueError("authority JSON root must be an object")
        if frozenset(payload) != _AUTHORITY_FIELDS:
            raise ValueError("authority JSON schema fields do not match version 1")
        if payload.get("schema_version") != _SCHEMA_VERSION:
            raise ValueError("unsupported authority schema_version")
        grants_payload = payload.get("grants")
        if type(grants_payload) is not list or not grants_payload:
            raise ValueError("grants must be a non-empty list")
        grants: list[ProviderOutputGrant] = []
        raw_keys: list[tuple[str, str]] = []
        for item in grants_payload:
            if type(item) is not dict or frozenset(item) != _GRANT_FIELDS:
                raise ValueError("grant JSON schema fields do not match version 1")
            grant = ProviderOutputGrant(
                purpose=item["purpose"],
                artifact_class=item["artifact_class"],
                retention_policy=item["retention_policy"],
                max_retention_seconds=item["max_retention_seconds"],
            )
            grants.append(grant)
            raw_keys.append(grant.key)
        if raw_keys != sorted(raw_keys):
            raise ValueError("grants must be in canonical purpose/artifact order")

        authority = cls(
            provider_id=payload["provider_id"],
            service_id=payload["service_id"],
            authority_version=payload["authority_version"],
            terms_reference=payload["terms_reference"],
            terms_sha256=payload["terms_sha256"],
            owner_approval_reference=payload["owner_approval_reference"],
            owner_approval_sha256=payload["owner_approval_sha256"],
            valid_from=payload["valid_from"],
            valid_until=payload["valid_until"],
            grants=tuple(grants),
        )
        if payload["authority_id"] != authority.authority_id:
            raise ValueError("authority_id does not match canonical authority content")
        if text != authority.to_json():
            raise ValueError("authority JSON is not canonical")
        return authority

    def grant_for(self, purpose: str, artifact_class: str) -> ProviderOutputGrant | None:
        key = (_text(purpose, "purpose"), _text(artifact_class, "artifact_class"))
        for grant in self.grants:
            if grant.key == key:
                return grant
        return None


@dataclass(frozen=True, slots=True)
class ProviderOutputUseRequest:
    authority_id: str
    provider_id: str
    service_id: str
    artifact_sha256: str
    purpose: str
    artifact_class: str
    acquired_at: str
    requested_retain_until: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "authority_id", _sha256(self.authority_id, "authority_id"))
        object.__setattr__(self, "provider_id", _text(self.provider_id, "provider_id"))
        object.__setattr__(self, "service_id", _text(self.service_id, "service_id"))
        object.__setattr__(
            self, "artifact_sha256", _sha256(self.artifact_sha256, "artifact_sha256")
        )
        object.__setattr__(self, "purpose", _text(self.purpose, "purpose"))
        object.__setattr__(
            self, "artifact_class", _text(self.artifact_class, "artifact_class")
        )
        object.__setattr__(
            self, "acquired_at", _canonical_instant(self.acquired_at, "acquired_at")
        )
        if self.requested_retain_until is not None:
            object.__setattr__(
                self,
                "requested_retain_until",
                _canonical_instant(
                    self.requested_retain_until, "requested_retain_until"
                ),
            )


@dataclass(frozen=True, slots=True, init=False)
class ProviderOutputUseDecision:
    allowed: bool
    reason: str
    authority_id: str
    artifact_sha256: str
    purpose: str
    artifact_class: str
    decided_at: str
    retention_policy: RetentionPolicy | None
    max_retention_seconds: int | None

    def __init__(self, *args: object, **kwargs: object) -> None:
        raise TypeError(
            "ProviderOutputUseDecision is product-issued; "
            "use decide_provider_output_use()"
        )

    def __post_init__(self) -> None:
        if type(self.allowed) is not bool:
            raise ValueError("allowed must be bool")
        object.__setattr__(self, "reason", _text(self.reason, "reason"))
        object.__setattr__(self, "authority_id", _sha256(self.authority_id, "authority_id"))
        object.__setattr__(
            self, "artifact_sha256", _sha256(self.artifact_sha256, "artifact_sha256")
        )
        object.__setattr__(self, "purpose", _text(self.purpose, "purpose"))
        object.__setattr__(
            self, "artifact_class", _text(self.artifact_class, "artifact_class")
        )
        object.__setattr__(
            self, "decided_at", _canonical_instant(self.decided_at, "decided_at")
        )
        if self.retention_policy is not None and not isinstance(
            self.retention_policy, RetentionPolicy
        ):
            raise ValueError("retention_policy is unsupported")
        if self.retention_policy is RetentionPolicy.BOUNDED:
            if (
                isinstance(self.max_retention_seconds, bool)
                or not isinstance(self.max_retention_seconds, int)
                or self.max_retention_seconds <= 0
            ):
                raise ValueError(
                    "BOUNDED decision requires positive max_retention_seconds"
                )
        elif self.max_retention_seconds is not None:
            raise ValueError(
                "max_retention_seconds is allowed only for BOUNDED decision"
            )
        if self.allowed:
            if self.reason != "ALLOWED":
                raise ValueError("allowed decision must use ALLOWED reason")
            if self.retention_policy is None:
                raise ValueError("allowed decision requires exact grant evidence")
        elif self.reason == "ALLOWED":
            raise ValueError("denied decision cannot use ALLOWED reason")


def _issue_provider_output_use_decision(
    *,
    allowed: bool,
    reason: str,
    authority_id: str,
    artifact_sha256: str,
    purpose: str,
    artifact_class: str,
    decided_at: str,
    retention_policy: RetentionPolicy | None,
    max_retention_seconds: int | None,
) -> ProviderOutputUseDecision:
    decision = object.__new__(ProviderOutputUseDecision)
    for name, value in (
        ("allowed", allowed),
        ("reason", reason),
        ("authority_id", authority_id),
        ("artifact_sha256", artifact_sha256),
        ("purpose", purpose),
        ("artifact_class", artifact_class),
        ("decided_at", decided_at),
        ("retention_policy", retention_policy),
        ("max_retention_seconds", max_retention_seconds),
    ):
        object.__setattr__(decision, name, value)
    decision.__post_init__()
    return decision


def decide_provider_output_use(
    authority: ProviderOutputGovernanceAuthority,
    request: ProviderOutputUseRequest,
    *,
    decided_at: str,
) -> ProviderOutputUseDecision:
    if not isinstance(authority, ProviderOutputGovernanceAuthority):
        raise ValueError("authority must be ProviderOutputGovernanceAuthority")
    if not isinstance(request, ProviderOutputUseRequest):
        raise ValueError("request must be ProviderOutputUseRequest")

    decision_time = _instant(decided_at, "decided_at")
    canonical_decision_time = _canonical_instant(decided_at, "decided_at")
    acquired_at = _instant(request.acquired_at, "acquired_at")
    valid_from = _instant(authority.valid_from, "valid_from")
    valid_until = _instant(authority.valid_until, "valid_until")

    reason = "ALLOWED"
    grant: ProviderOutputGrant | None = None
    if request.authority_id != authority.authority_id:
        reason = "AUTHORITY_ID_MISMATCH"
    elif request.provider_id != authority.provider_id or request.service_id != authority.service_id:
        reason = "PROVIDER_SERVICE_MISMATCH"
    elif not (valid_from <= acquired_at < valid_until):
        reason = "ACQUISITION_OUTSIDE_AUTHORITY_VALIDITY"
    elif decision_time < acquired_at:
        reason = "DECISION_PRECEDES_ACQUISITION"
    elif not (valid_from <= decision_time < valid_until):
        reason = "AUTHORITY_NOT_ACTIVE_AT_DECISION"
    else:
        grant = authority.grant_for(request.purpose, request.artifact_class)
        if grant is None:
            reason = "NO_EXACT_GRANT"
        elif request.requested_retain_until is not None:
            retain_until = _instant(
                request.requested_retain_until, "requested_retain_until"
            )
            if retain_until < decision_time:
                reason = "RETENTION_HORIZON_PRECEDES_DECISION"
            elif grant.retention_policy is RetentionPolicy.FORBIDDEN:
                reason = "PERSISTENCE_FORBIDDEN"
            elif grant.retention_policy is RetentionPolicy.BOUNDED:
                assert grant.max_retention_seconds is not None
                maximum_retention = timedelta(seconds=grant.max_retention_seconds)
                if retain_until - acquired_at > maximum_retention:
                    reason = "RETENTION_HORIZON_EXCEEDS_GRANT"

    # owner_approval_* is declarative evidence identity only.  A self-consistent
    # caller-authored record is not proof that the owner issued or approved it.
    # Preserve exact structural/grant/retention diagnostics, but never mint
    # positive use authority until a separate durable product-owned approval
    # resolver is integrated and re-resolved here.
    if reason == "ALLOWED":
        reason = "OWNER_APPROVAL_UNRESOLVED"
    allowed = False
    return _issue_provider_output_use_decision(
        allowed=allowed,
        reason=reason,
        authority_id=authority.authority_id,
        artifact_sha256=request.artifact_sha256,
        purpose=request.purpose,
        artifact_class=request.artifact_class,
        decided_at=canonical_decision_time,
        retention_policy=grant.retention_policy if grant is not None else None,
        max_retention_seconds=grant.max_retention_seconds if grant is not None else None,
    )
