from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


_MANIFEST_KIND = "autosport_source_rights_manifest"
_MAX_TEXT_LENGTH = 4096
_REQUIRED_FIELDS = {
    "schema_version",
    "kind",
    "source_identity",
    "authorized_scopes",
    "effective_at",
    "expires_at",
    "human_approved",
    "approved_by",
    "approval_reference",
    "approved_at",
}
_AUTHORIZATION_ISSUER = object()


class SourceRightsManifestError(ValueError):
    """Raised when source-rights evidence is malformed or does not authorize a use."""


@dataclass(frozen=True, slots=True)
class SourceRightsManifest:
    """Verified immutable snapshot of a human-approved source-rights manifest.

    This record is a deterministic policy/evidence gate. It is not a legal opinion,
    does not prove provider entitlement by itself, and grants no provider-write,
    settlement, execution, or real-money authority.
    """

    manifest_path: str
    manifest_sha256: str
    source_identity: str
    authorized_scopes: tuple[str, ...]
    effective_at: datetime
    expires_at: datetime
    approved_by: str
    approval_reference: str
    approved_at: datetime
    manifest_bytes: bytes = field(repr=False)


@dataclass(frozen=True, slots=True, init=False)
class SourceRightsAuthorization:
    """Positive authorization result issued only by :func:`authorize_source_use`."""

    source_identity: str
    required_scope: str
    checked_at: datetime
    manifest_sha256: str
    approved_by: str
    approval_reference: str

    def __init__(
        self,
        *,
        source_identity: str,
        required_scope: str,
        checked_at: datetime,
        manifest_sha256: str,
        approved_by: str,
        approval_reference: str,
        _issuer: object | None = None,
    ) -> None:
        if _issuer is not _AUTHORIZATION_ISSUER:
            raise SourceRightsManifestError(
                "SourceRightsAuthorization can only be issued by authorize_source_use"
            )
        object.__setattr__(self, "source_identity", source_identity)
        object.__setattr__(self, "required_scope", required_scope)
        object.__setattr__(self, "checked_at", checked_at)
        object.__setattr__(self, "manifest_sha256", manifest_sha256)
        object.__setattr__(self, "approved_by", approved_by)
        object.__setattr__(self, "approval_reference", approval_reference)


def _strict_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SourceRightsManifestError(f"duplicate JSON object key: {key}")
        result[key] = value
    return result


def _reject_nonfinite(value: str) -> None:
    raise SourceRightsManifestError(f"non-finite JSON number: {value}")


def _canonical_text(value: object, *, field_name: str) -> str:
    if type(value) is not str or not value or len(value) > _MAX_TEXT_LENGTH:
        raise SourceRightsManifestError(
            f"{field_name} must be a non-empty canonical string without surrounding whitespace"
        )
    if value != value.strip():
        raise SourceRightsManifestError(
            f"{field_name} must be a non-empty canonical string without surrounding whitespace"
        )
    return value


def _timestamp(value: object, *, field_name: str) -> datetime:
    text = _canonical_text(value, field_name=field_name)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SourceRightsManifestError(
            f"{field_name} must be an ISO-8601 timestamp"
        ) from exc
    if parsed.tzinfo is None:
        raise SourceRightsManifestError(
            f"{field_name} must include an explicit timezone"
        )
    if type(parsed.tzinfo) is not timezone or parsed.utcoffset() != timedelta(0):
        raise SourceRightsManifestError(f"{field_name} must use UTC")
    return parsed


def _runtime_timestamp(value: object, *, field_name: str) -> datetime:
    if type(value) is not datetime or type(value.tzinfo) is not timezone:
        raise SourceRightsManifestError(f"{field_name} must be a built-in UTC datetime")
    if value.utcoffset() != timedelta(0):
        raise SourceRightsManifestError(f"{field_name} must use UTC")
    return value


def _scopes(value: object) -> tuple[str, ...]:
    if type(value) is not list or not value:
        raise SourceRightsManifestError(
            "authorized_scopes must be a non-empty list"
        )
    normalized: list[str] = []
    for item in value:
        scope = _canonical_text(item, field_name="authorized_scopes item")
        if "*" in scope:
            raise SourceRightsManifestError(
                "authorized_scopes must use explicit scopes; wildcard scopes are forbidden"
            )
        normalized.append(scope)
    if len(set(normalized)) != len(normalized):
        raise SourceRightsManifestError("authorized_scopes must not contain duplicates")
    return tuple(sorted(normalized))


def _runtime_scopes(value: object) -> tuple[str, ...]:
    if type(value) is not tuple or not value:
        raise SourceRightsManifestError("manifest authorized_scopes snapshot is malformed")
    normalized = tuple(
        _canonical_text(item, field_name="manifest authorized_scopes item")
        for item in value
    )
    if normalized != tuple(sorted(normalized)) or len(set(normalized)) != len(normalized):
        raise SourceRightsManifestError("manifest authorized_scopes snapshot is malformed")
    if any("*" in scope for scope in normalized):
        raise SourceRightsManifestError("manifest authorized_scopes snapshot is malformed")
    return normalized


def _validated_projection(
    payload: bytes,
) -> tuple[str, tuple[str, ...], datetime, datetime, str, str, datetime]:
    try:
        raw = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_strict_object,
            parse_constant=_reject_nonfinite,
        )
    except UnicodeDecodeError as exc:
        raise SourceRightsManifestError(
            "source-rights manifest must be valid UTF-8 JSON"
        ) from exc
    except json.JSONDecodeError as exc:
        raise SourceRightsManifestError(
            "source-rights manifest must be valid JSON"
        ) from exc
    except RecursionError as exc:
        raise SourceRightsManifestError(
            "source-rights manifest must be valid JSON"
        ) from exc

    if type(raw) is not dict:
        raise SourceRightsManifestError("source-rights manifest must be a JSON object")
    fields = set(raw)
    if fields != _REQUIRED_FIELDS:
        missing = sorted(_REQUIRED_FIELDS - fields)
        unknown = sorted(fields - _REQUIRED_FIELDS)
        details: list[str] = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if unknown:
            details.append("unknown=" + ",".join(unknown))
        raise SourceRightsManifestError(
            "source-rights manifest fields must match schema exactly"
            + (": " + " ".join(details) if details else "")
        )

    if type(raw["schema_version"]) is not int or raw["schema_version"] != 1:
        raise SourceRightsManifestError(
            "source-rights manifest schema_version must be exact integer 1"
        )
    if raw["kind"] != _MANIFEST_KIND:
        raise SourceRightsManifestError(
            f"source-rights manifest kind must be {_MANIFEST_KIND}"
        )
    if raw["human_approved"] is not True:
        raise SourceRightsManifestError(
            "source-rights manifest must explicitly set human_approved=true"
        )

    source_identity = _canonical_text(
        raw["source_identity"],
        field_name="source_identity",
    )
    authorized_scopes = _scopes(raw["authorized_scopes"])
    effective_at = _timestamp(raw["effective_at"], field_name="effective_at")
    expires_at = _timestamp(raw["expires_at"], field_name="expires_at")
    approved_by = _canonical_text(raw["approved_by"], field_name="approved_by")
    approval_reference = _canonical_text(
        raw["approval_reference"],
        field_name="approval_reference",
    )
    approved_at = _timestamp(raw["approved_at"], field_name="approved_at")

    if approved_at > effective_at:
        raise SourceRightsManifestError(
            "approved_at must not be later than effective_at"
        )
    if expires_at <= effective_at:
        raise SourceRightsManifestError(
            "expires_at must be later than effective_at"
        )

    return (
        source_identity,
        authorized_scopes,
        effective_at,
        expires_at,
        approved_by,
        approval_reference,
        approved_at,
    )


def _validated_manifest_snapshot(
    manifest: SourceRightsManifest,
) -> tuple[str, tuple[str, ...], datetime, datetime, str, str, datetime]:
    if type(manifest.manifest_bytes) is not bytes:
        raise SourceRightsManifestError("source-rights manifest snapshot bytes are malformed")
    digest = _canonical_text(manifest.manifest_sha256, field_name="manifest_sha256")
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise SourceRightsManifestError("source-rights manifest snapshot digest is malformed")
    return (
        _canonical_text(manifest.source_identity, field_name="manifest source_identity"),
        _runtime_scopes(manifest.authorized_scopes),
        _runtime_timestamp(manifest.effective_at, field_name="manifest effective_at"),
        _runtime_timestamp(manifest.expires_at, field_name="manifest expires_at"),
        _canonical_text(manifest.approved_by, field_name="manifest approved_by"),
        _canonical_text(
            manifest.approval_reference,
            field_name="manifest approval_reference",
        ),
        _runtime_timestamp(manifest.approved_at, field_name="manifest approved_at"),
    )


def load_source_rights_manifest(path: str | Path) -> SourceRightsManifest:
    """Load and validate one exact source-rights manifest byte snapshot."""

    manifest_path = Path(path)
    try:
        payload = manifest_path.read_bytes()
    except OSError as exc:
        raise SourceRightsManifestError(
            f"source-rights manifest is not readable: {manifest_path}"
        ) from exc

    (
        source_identity,
        authorized_scopes,
        effective_at,
        expires_at,
        approved_by,
        approval_reference,
        approved_at,
    ) = _validated_projection(payload)

    return SourceRightsManifest(
        manifest_path=str(manifest_path),
        manifest_sha256=hashlib.sha256(payload).hexdigest(),
        source_identity=source_identity,
        authorized_scopes=authorized_scopes,
        effective_at=effective_at,
        expires_at=expires_at,
        approved_by=approved_by,
        approval_reference=approval_reference,
        approved_at=approved_at,
        manifest_bytes=payload,
    )


def authorize_source_use(
    manifest: SourceRightsManifest,
    *,
    source_identity: str,
    required_scope: str,
    at: datetime,
) -> SourceRightsAuthorization:
    """Authorize one exact source/scope use at one instant.

    Scope matching is exact and case-sensitive. The valid interval is
    [effective_at, expires_at); equality with expires_at is expired.
    """

    if type(manifest) is not SourceRightsManifest:
        raise SourceRightsManifestError(
            "manifest must be an exact verified SourceRightsManifest"
        )

    requested_source = _canonical_text(
        source_identity,
        field_name="requested source_identity",
    )
    scope = _canonical_text(required_scope, field_name="required_scope")
    if "*" in scope:
        raise SourceRightsManifestError(
            "required_scope must be explicit; wildcard scopes are forbidden"
        )
    checked_at = _runtime_timestamp(at, field_name="authorization check time")

    object_projection = _validated_manifest_snapshot(manifest)
    if hashlib.sha256(manifest.manifest_bytes).hexdigest() != manifest.manifest_sha256:
        raise SourceRightsManifestError(
            "source-rights manifest snapshot digest is inconsistent"
        )

    projection = _validated_projection(manifest.manifest_bytes)
    if projection != object_projection:
        raise SourceRightsManifestError(
            "source-rights manifest snapshot fields are inconsistent"
        )

    if requested_source != manifest.source_identity:
        raise SourceRightsManifestError(
            "source_identity is not authorized by this manifest"
        )
    if scope not in manifest.authorized_scopes:
        raise SourceRightsManifestError(
            "required_scope is not explicitly authorized by this manifest"
        )
    if checked_at < manifest.effective_at:
        raise SourceRightsManifestError(
            "source-rights authorization is not yet effective"
        )
    if checked_at >= manifest.expires_at:
        raise SourceRightsManifestError(
            "source-rights authorization has expired"
        )

    return SourceRightsAuthorization(
        source_identity=manifest.source_identity,
        required_scope=scope,
        checked_at=checked_at,
        manifest_sha256=manifest.manifest_sha256,
        approved_by=manifest.approved_by,
        approval_reference=manifest.approval_reference,
        _issuer=_AUTHORIZATION_ISSUER,
    )
