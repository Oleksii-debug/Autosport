from __future__ import annotations

"""Fail-closed evidence gate for recorded source-use authorization.

The module records and evaluates product evidence that a human-approved
authorization artifact covers one exact source identity, one declared purpose,
and one UTC validity interval.  An ``ALLOW`` result means only that the recorded
evidence is complete, current and scope-matched.  It is not a legal opinion and
never infers permission from a source type, URL, provider name, or public
availability.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class SourceRightsManifestError(ValueError):
    """Source-rights evidence is malformed or internally inconsistent."""


class SourcePurpose(StrEnum):
    HISTORICAL_RESEARCH = "HISTORICAL_RESEARCH"
    MODEL_TRAINING = "MODEL_TRAINING"
    PAPER_DECISION_SUPPORT = "PAPER_DECISION_SUPPORT"
    LIVE_READ_ONLY = "LIVE_READ_ONLY"
    EVIDENCE_ARCHIVAL = "EVIDENCE_ARCHIVAL"


class SourceRightsDecisionCode(StrEnum):
    ALLOW = "ALLOW"
    BLOCK = "BLOCK"
    INVALID_EVIDENCE = "INVALID_EVIDENCE"


class SourceRightsReason(StrEnum):
    AUTHORIZATION_MATCHED = "AUTHORIZATION_MATCHED"
    SOURCE_IDENTITY_MISMATCH = "SOURCE_IDENTITY_MISMATCH"
    PURPOSE_NOT_PERMITTED = "PURPOSE_NOT_PERMITTED"
    AUTHORIZATION_NOT_YET_EFFECTIVE = "AUTHORIZATION_NOT_YET_EFFECTIVE"
    AUTHORIZATION_EXPIRED = "AUTHORIZATION_EXPIRED"
    MALFORMED_MANIFEST = "MALFORMED_MANIFEST"


@dataclass(frozen=True, order=True, slots=True)
class SourceIdentity:
    family: str
    source_id: str
    revision: str
    content_sha256: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "family", _text(self.family, "source family"))
        object.__setattr__(self, "source_id", _text(self.source_id, "source_id"))
        object.__setattr__(self, "revision", _text(self.revision, "source revision"))
        object.__setattr__(
            self,
            "content_sha256",
            _sha256(self.content_sha256, "source content_sha256"),
        )

    def to_dict(self) -> dict[str, str]:
        return {
            "family": self.family,
            "source_id": self.source_id,
            "revision": self.revision,
            "content_sha256": self.content_sha256,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SourceIdentity":
        _exact_keys(
            raw,
            {"family", "source_id", "revision", "content_sha256"},
            "SourceIdentity",
        )
        return cls(
            family=_string(raw["family"], "family"),
            source_id=_string(raw["source_id"], "source_id"),
            revision=_string(raw["revision"], "revision"),
            content_sha256=_string(raw["content_sha256"], "content_sha256"),
        )


@dataclass(frozen=True, slots=True)
class SourceRightsManifest:
    manifest_id: str
    source: SourceIdentity
    authorization_artifact_id: str
    authorization_artifact_sha256: str
    permitted_purposes: tuple[SourcePurpose, ...]
    approved_by: str
    approved_at: datetime
    effective_at: datetime
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "manifest_id", _text(self.manifest_id, "manifest_id"))
        if type(self.source) is not SourceIdentity:
            raise SourceRightsManifestError("source must be exact SourceIdentity")
        object.__setattr__(
            self,
            "authorization_artifact_id",
            _text(self.authorization_artifact_id, "authorization_artifact_id"),
        )
        object.__setattr__(
            self,
            "authorization_artifact_sha256",
            _sha256(
                self.authorization_artifact_sha256,
                "authorization_artifact_sha256",
            ),
        )
        if type(self.permitted_purposes) is not tuple or not self.permitted_purposes:
            raise SourceRightsManifestError(
                "permitted_purposes must be a non-empty tuple"
            )
        purposes: list[SourcePurpose] = []
        for value in self.permitted_purposes:
            if type(value) is not SourcePurpose:
                raise SourceRightsManifestError(
                    "permitted_purposes must contain SourcePurpose values"
                )
            purposes.append(value)
        canonical = tuple(sorted(purposes, key=lambda item: item.value))
        if len(set(canonical)) != len(canonical):
            raise SourceRightsManifestError("permitted_purposes must be unique")
        if self.permitted_purposes != canonical:
            raise SourceRightsManifestError(
                "permitted_purposes must be sorted canonically"
            )
        object.__setattr__(self, "approved_by", _text(self.approved_by, "approved_by"))

        approved_at = _utc(self.approved_at, "approved_at")
        effective_at = _utc(self.effective_at, "effective_at")
        expires_at = _utc(self.expires_at, "expires_at")
        if approved_at > effective_at:
            raise SourceRightsManifestError(
                "authorization cannot become effective before human approval"
            )
        if expires_at <= effective_at:
            raise SourceRightsManifestError("expires_at must be after effective_at")
        object.__setattr__(self, "approved_at", approved_at)
        object.__setattr__(self, "effective_at", effective_at)
        object.__setattr__(self, "expires_at", expires_at)

    @property
    def manifest_sha256(self) -> str:
        return _digest(self.payload())

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "manifest_id": self.manifest_id,
            "source": self.source.to_dict(),
            "authorization_artifact_id": self.authorization_artifact_id,
            "authorization_artifact_sha256": self.authorization_artifact_sha256,
            "permitted_purposes": [item.value for item in self.permitted_purposes],
            "approved_by": self.approved_by,
            "approved_at": _datetime_text(self.approved_at),
            "effective_at": _datetime_text(self.effective_at),
            "expires_at": _datetime_text(self.expires_at),
        }

    def to_dict(self) -> dict[str, Any]:
        raw = self.payload()
        raw["manifest_sha256"] = self.manifest_sha256
        return raw

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "SourceRightsManifest":
        expected = {
            "schema_version",
            "manifest_id",
            "source",
            "authorization_artifact_id",
            "authorization_artifact_sha256",
            "permitted_purposes",
            "approved_by",
            "approved_at",
            "effective_at",
            "expires_at",
            "manifest_sha256",
        }
        _exact_keys(raw, expected, "SourceRightsManifest")
        if raw["schema_version"] != SCHEMA_VERSION:
            raise SourceRightsManifestError("unsupported source-rights schema_version")
        purposes_raw = raw["permitted_purposes"]
        if type(purposes_raw) is not list or not purposes_raw:
            raise SourceRightsManifestError(
                "permitted_purposes must be a non-empty JSON list"
            )
        item = cls(
            manifest_id=_string(raw["manifest_id"], "manifest_id"),
            source=SourceIdentity.from_dict(_mapping(raw["source"], "source")),
            authorization_artifact_id=_string(
                raw["authorization_artifact_id"], "authorization_artifact_id"
            ),
            authorization_artifact_sha256=_string(
                raw["authorization_artifact_sha256"],
                "authorization_artifact_sha256",
            ),
            permitted_purposes=tuple(
                SourcePurpose(_string(value, "permitted purpose"))
                for value in purposes_raw
            ),
            approved_by=_string(raw["approved_by"], "approved_by"),
            approved_at=_parse_datetime(raw["approved_at"], "approved_at"),
            effective_at=_parse_datetime(raw["effective_at"], "effective_at"),
            expires_at=_parse_datetime(raw["expires_at"], "expires_at"),
        )
        if _string(raw["manifest_sha256"], "manifest_sha256") != item.manifest_sha256:
            raise SourceRightsManifestError("manifest digest mismatch")
        return item


@dataclass(frozen=True, slots=True)
class SourceRightsDecision:
    code: SourceRightsDecisionCode
    reason: SourceRightsReason
    source: SourceIdentity
    requested_purpose: SourcePurpose
    use_at: datetime
    manifest_id: str | None
    manifest_sha256: str | None
    authorization_artifact_id: str | None
    authorization_artifact_sha256: str | None
    legal_conclusion: bool = False

    def __post_init__(self) -> None:
        if type(self.code) is not SourceRightsDecisionCode:
            raise SourceRightsManifestError("code must be SourceRightsDecisionCode")
        if type(self.reason) is not SourceRightsReason:
            raise SourceRightsManifestError("reason must be SourceRightsReason")
        if type(self.source) is not SourceIdentity:
            raise SourceRightsManifestError("source must be exact SourceIdentity")
        if type(self.requested_purpose) is not SourcePurpose:
            raise SourceRightsManifestError("requested_purpose must be SourcePurpose")
        object.__setattr__(self, "use_at", _utc(self.use_at, "use_at"))
        if self.legal_conclusion is not False:
            raise SourceRightsManifestError(
                "source-rights gate cannot emit a legal conclusion"
            )
        if self.code is SourceRightsDecisionCode.INVALID_EVIDENCE:
            if any(
                value is not None
                for value in (
                    self.manifest_id,
                    self.manifest_sha256,
                    self.authorization_artifact_id,
                    self.authorization_artifact_sha256,
                )
            ):
                raise SourceRightsManifestError(
                    "invalid evidence cannot expose trusted manifest authority"
                )
        else:
            if self.manifest_id is None or self.manifest_sha256 is None:
                raise SourceRightsManifestError(
                    "validly parsed decision must bind manifest identity"
                )
            if self.authorization_artifact_id is None or self.authorization_artifact_sha256 is None:
                raise SourceRightsManifestError(
                    "validly parsed decision must bind authorization artifact"
                )

    @property
    def allowed(self) -> bool:
        return self.code is SourceRightsDecisionCode.ALLOW


ManifestInput = SourceRightsManifest | Mapping[str, Any]


def evaluate_source_rights(
    manifest: ManifestInput,
    *,
    source: SourceIdentity,
    purpose: SourcePurpose,
    use_at: datetime,
) -> SourceRightsDecision:
    """Evaluate recorded authorization evidence for one exact source use.

    This is an evidence/policy gate, not legal advice.  It deliberately refuses
    to infer permission from public accessibility, provider type, source family,
    or any other heuristic outside the supplied human-approved artifact record.
    """

    if type(source) is not SourceIdentity:
        raise SourceRightsManifestError("source must be exact SourceIdentity")
    if type(purpose) is not SourcePurpose:
        raise SourceRightsManifestError("purpose must be exact SourcePurpose")
    checked_use_at = _utc(use_at, "use_at")

    try:
        checked = _coerce_manifest(manifest)
    except (SourceRightsManifestError, TypeError, ValueError, KeyError):
        return SourceRightsDecision(
            code=SourceRightsDecisionCode.INVALID_EVIDENCE,
            reason=SourceRightsReason.MALFORMED_MANIFEST,
            source=source,
            requested_purpose=purpose,
            use_at=checked_use_at,
            manifest_id=None,
            manifest_sha256=None,
            authorization_artifact_id=None,
            authorization_artifact_sha256=None,
        )

    common = {
        "source": source,
        "requested_purpose": purpose,
        "use_at": checked_use_at,
        "manifest_id": checked.manifest_id,
        "manifest_sha256": checked.manifest_sha256,
        "authorization_artifact_id": checked.authorization_artifact_id,
        "authorization_artifact_sha256": checked.authorization_artifact_sha256,
    }
    if checked.source != source:
        return SourceRightsDecision(
            code=SourceRightsDecisionCode.BLOCK,
            reason=SourceRightsReason.SOURCE_IDENTITY_MISMATCH,
            **common,
        )
    if purpose not in checked.permitted_purposes:
        return SourceRightsDecision(
            code=SourceRightsDecisionCode.BLOCK,
            reason=SourceRightsReason.PURPOSE_NOT_PERMITTED,
            **common,
        )
    if checked_use_at < checked.effective_at:
        return SourceRightsDecision(
            code=SourceRightsDecisionCode.BLOCK,
            reason=SourceRightsReason.AUTHORIZATION_NOT_YET_EFFECTIVE,
            **common,
        )
    if checked_use_at >= checked.expires_at:
        return SourceRightsDecision(
            code=SourceRightsDecisionCode.BLOCK,
            reason=SourceRightsReason.AUTHORIZATION_EXPIRED,
            **common,
        )
    return SourceRightsDecision(
        code=SourceRightsDecisionCode.ALLOW,
        reason=SourceRightsReason.AUTHORIZATION_MATCHED,
        **common,
    )


def _coerce_manifest(value: ManifestInput) -> SourceRightsManifest:
    if type(value) is SourceRightsManifest:
        # Re-run canonical construction so callers cannot bypass validation with
        # object.__setattr__ or malformed runtime containers before evaluation.
        return SourceRightsManifest(
            manifest_id=value.manifest_id,
            source=value.source,
            authorization_artifact_id=value.authorization_artifact_id,
            authorization_artifact_sha256=value.authorization_artifact_sha256,
            permitted_purposes=value.permitted_purposes,
            approved_by=value.approved_by,
            approved_at=value.approved_at,
            effective_at=value.effective_at,
            expires_at=value.expires_at,
        )
    if isinstance(value, Mapping):
        return SourceRightsManifest.from_dict(value)
    raise SourceRightsManifestError("manifest must be SourceRightsManifest or mapping")


def _text(value: object, field: str) -> str:
    if type(value) is not str or not value or value != value.strip() or "\x00" in value:
        raise SourceRightsManifestError(f"{field} must be non-empty canonical text")
    value.encode("utf-8")
    return value


def _string(value: object, field: str) -> str:
    if type(value) is not str:
        raise SourceRightsManifestError(f"{field} must be a string")
    return value


def _sha256(value: object, field: str) -> str:
    raw = _text(value, field)
    if _SHA256_RE.fullmatch(raw) is None:
        raise SourceRightsManifestError(f"{field} must be lowercase SHA-256 hex")
    return raw


def _utc(value: object, field: str) -> datetime:
    if type(value) is not datetime or value.tzinfo is None or value.utcoffset() is None:
        raise SourceRightsManifestError(f"{field} must be timezone-aware datetime")
    if value.utcoffset() != timedelta(0):
        raise SourceRightsManifestError(f"{field} must use UTC offset +00:00")
    return value.astimezone(timezone.utc)


def _parse_datetime(value: object, field: str) -> datetime:
    raw = _text(value, field)
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceRightsManifestError(f"{field} must be ISO-8601") from exc
    return _utc(parsed, field)


def _datetime_text(value: datetime) -> str:
    return _utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _mapping(value: object, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SourceRightsManifestError(f"{field} must be an object")
    return value


def _exact_keys(raw: Mapping[str, Any], expected: set[str], label: str) -> None:
    if type(raw) is not dict:
        raise SourceRightsManifestError(f"{label} must be a plain object")
    actual = set(raw)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise SourceRightsManifestError(
            f"{label} keys mismatch: missing={missing}, extra={extra}"
        )


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise SourceRightsManifestError(
            "source-rights evidence must be canonical finite JSON"
        ) from exc


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "SCHEMA_VERSION",
    "SourceIdentity",
    "SourcePurpose",
    "SourceRightsDecision",
    "SourceRightsDecisionCode",
    "SourceRightsManifest",
    "SourceRightsManifestError",
    "SourceRightsReason",
    "evaluate_source_rights",
]
